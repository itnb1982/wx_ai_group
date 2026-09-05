"""
万象Ai — 平台健康监视器 / 降级车道（V6 Phase 6 · §9.4）
==========================================================
职责：把「哪些智能组件还活着」这件事，收敛成一个**全平台唯一**的降级档位
     （DegradeLevel L0/L1/L2/L3），并据此下发三个可执行结论：

  1) allow_new_entry()   —— 还能不能开新仓（L3 = 不能）
  2) lot_multiplier()    —— 手数系数（能力越弱，下注越小）
  3) require_local_confirm() —— 是否强制「本地副驾 + Chronos 同向」双确认

★ 三条不可违背的铁律（写死在本模块，任何调用方不得绕过）
--------------------------------------------------------
铁律一【只关水龙头，不抽水】
    L3 只**停发新开仓信号**，绝不平仓、绝不强制减仓。已有持仓一律交回
    各账号自身的 SL/TP/SmartExit/护盾篮子处理。降级是「少开」不是「清仓」——
    在系统能力最弱的时刻去批量平仓，等于用最不可靠的判断力做最不可逆的操作。

铁律二【本地 8B 不在正常模式投方向票】
    Fin-Bias(ACL2026) 实证 7~8B 金融方向判断接近随机。故 L0/L1 时本地 LLM
    只做「校对员」（结构/自洽/幻觉价格），**不参与方向等权投票**；
    只有 L2（双云全失）才允许它当「副驾」，且必须 Chronos 同向 + 手数砍到 0.4。

铁律三【降级要快，恢复要慢（迟滞 Hysteresis）】
    降级立即生效（风险优先）；恢复必须同时满足
      ① 连续 RECOVER_STREAK 次观测健康  ② 距上次降级 ≥ RECOVER_COOLDOWN_SEC
    否则云 API 在边界抖动时，手数系数会 1.0/0.4/1.0/0.4 反复横跳，
    等于给每个客户随机化仓位——比稳定降级更危险。

设计约束
--------
* **被动上报，不主动轮询**：本模块不发起任何网络请求。各组件在自己本就要做的
  调用里顺手 `report_ok/report_fail` 即可。理由：监控自身若会超时/抛错，
  就成了新的故障源；而且 N 个客户共用一套信号源，没必要 N 倍探测。
* **零业务依赖**：不 import services/models/routers，可被任何层安全引用，
  也让单测无需起 DB/MT5。
* **全异常安全**：任何内部异常都不得让交易主链路崩溃；判不出来就按「更安全的
  那一侧」处理（组件状态未知 = 视为不可用，但 market_data 未知不直接判 L3，
  见 _classify 注释）。
* **多租户**：降级档是**平台级**的（全体客户共用同一 XAUUSD 信号源，
  故障相关性来自信号源而非资金）。它只熔断信号，绝不合并任何客户资金。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional


# ============================================================
#  档位定义
# ============================================================
class DegradeLevel(IntEnum):
    """降级档位。数值越大能力越弱；IntEnum 便于比较 `level >= DegradeLevel.L2`。"""

    L0 = 0  # 全绿：云端双脑 + Chronos（+ 本地校对员）
    L1 = 1  # 单云：DeepSeek / 混元 之一失联 → 单模型 + Chronos，缩手
    L2 = 2  # 双云全失：本地副驾（Qwen3-8B）+ Chronos 双确认，重度缩手
    L3 = 3  # 无可用决策能力 / 行情不可信 → 停发新开仓（持仓不动）


#: 各档手数系数。依据：能力衰减 → 下注衰减，且相邻档差距要足够大
#: （0.7/0.4 而非 0.9/0.8），否则降级形同虚设、起不到保护作用。
LOT_MULTIPLIER: Dict[DegradeLevel, float] = {
    DegradeLevel.L0: 1.00,
    DegradeLevel.L1: 0.70,
    DegradeLevel.L2: 0.40,
    DegradeLevel.L3: 0.00,
}

#: 档位人类可读说明（前端「降级指示灯」直接用）
LEVEL_LABEL: Dict[DegradeLevel, str] = {
    DegradeLevel.L0: "全能力运行",
    DegradeLevel.L1: "单云降级",
    DegradeLevel.L2: "本地副驾",
    DegradeLevel.L3: "信号熔断",
}

LEVEL_DETAIL: Dict[DegradeLevel, str] = {
    DegradeLevel.L0: "云端双脑 + 本地时序全部在线，正常开仓",
    DegradeLevel.L1: "一朵云失联，单模型 + Chronos 决策，手数降至 70%",
    DegradeLevel.L2: "双云失联，本地 Qwen3 副驾需 Chronos 同向，手数降至 40%",
    DegradeLevel.L3: "决策能力不可信，仅停发新开仓；持仓仍由 SL/TP/智能平仓守护",
}


# ============================================================
#  组件健康
# ============================================================
#: 受监控的组件。注意 local_llm 与 chronos 分开：前者是 LLM 副驾，
#: 后者是数值时序，二者角色不同，不能互相顶替。
COMPONENTS = ("deepseek", "hunyuan", "chronos", "local_llm", "market_data")

#: 连续失败几次才判定「失联」。1 次太敏感（云 API 偶发 5xx 很常见，
#: 一次抖动就砍全体客户手数不合理）；3 次太钝（60s 一轮 = 3 分钟才反应）。
FAIL_STREAK_TO_DOWN = 2

#: 上报数据的新鲜度上限。超过这个时间没有任何上报，视为「未知」
#: 而非「健康」——沉默不等于正常，这是监控设计的基本原则。
STALE_SEC = 300.0

#: 迟滞参数：恢复需连续健康观测次数 + 冷却秒数（见铁律三）
RECOVER_STREAK = 3
RECOVER_COOLDOWN_SEC = 120.0

#: 云探活半开窗口（断路器 half-open）。L2/L3 下不再每轮去撞死云
#: （60s 一轮里两次 30s 超时就把整轮吃光），但每隔这么久必须放一次探活请求，
#: 否则「跳过调用 → 没有成功上报 → 永远判失联」会形成自锁，系统再也回不到 L0。
CLOUD_PROBE_INTERVAL_SEC = 180.0


@dataclass
class ComponentHealth:
    """单个组件的滑动健康状态（不存历史明细，只存判定所需的最小量）。"""

    name: str
    ok_streak: int = 0
    fail_streak: int = 0
    last_ok_ts: float = 0.0
    last_fail_ts: float = 0.0
    last_report_ts: float = 0.0
    last_error: str = ""
    total_ok: int = 0
    total_fail: int = 0

    def report(self, ok: bool, error: str = "") -> None:
        now = time.time()
        self.last_report_ts = now
        if ok:
            self.ok_streak += 1
            self.fail_streak = 0
            self.last_ok_ts = now
            self.total_ok += 1
        else:
            self.fail_streak += 1
            self.ok_streak = 0
            self.last_fail_ts = now
            self.total_fail += 1
            if error:
                self.last_error = str(error)[:200]

    @property
    def is_stale(self) -> bool:
        """从未上报，或距上次上报超过 STALE_SEC。"""
        if self.last_report_ts <= 0:
            return True
        return (time.time() - self.last_report_ts) > STALE_SEC

    @property
    def is_down(self) -> bool:
        """判定「失联」：连续失败达阈值。

        注意：stale（长时间无上报）**不**在这里判 down —— 因为组件可能只是
        本轮没被调用到（例如 L2 下根本不会去调云 API，云自然不再上报）。
        stale 由 snapshot 如实暴露，交给判定矩阵按语义处理。
        """
        return self.fail_streak >= FAIL_STREAK_TO_DOWN

    @property
    def is_up(self) -> bool:
        """判定「在线」：有过成功上报、未失联、且不 stale。"""
        return (not self.is_down) and (not self.is_stale) and self.total_ok > 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "up": self.is_up,
            "down": self.is_down,
            "stale": self.is_stale,
            "ok_streak": self.ok_streak,
            "fail_streak": self.fail_streak,
            "total_ok": self.total_ok,
            "total_fail": self.total_fail,
            "last_error": self.last_error,
            "age_sec": (
                round(time.time() - self.last_report_ts, 1)
                if self.last_report_ts > 0
                else None
            ),
        }


@dataclass
class DegradeSnapshot:
    """一次对外暴露的完整降级视图（供 /api/health 与前端指示灯）。"""

    level: DegradeLevel
    label: str
    detail: str
    lot_multiplier: float
    allow_new_entry: bool
    require_local_confirm: bool
    reason: str
    raw_level: DegradeLevel  # 迟滞前的「裸判定」，用于观察抖动
    pending_recover: bool
    components: Dict[str, dict] = field(default_factory=dict)
    changed_ts: float = 0.0
    since_sec: float = 0.0

    def as_dict(self) -> dict:
        return {
            "level": int(self.level),
            "level_name": self.level.name,
            "label": self.label,
            "detail": self.detail,
            "lot_multiplier": self.lot_multiplier,
            "allow_new_entry": self.allow_new_entry,
            "require_local_confirm": self.require_local_confirm,
            "reason": self.reason,
            "raw_level": int(self.raw_level),
            "pending_recover": self.pending_recover,
            "components": self.components,
            "since_sec": round(self.since_sec, 1),
        }


# ============================================================
#  监视器
# ============================================================
class PlatformHealthMonitor:
    """平台级降级判定（线程安全）。

    典型用法（组件侧，一行接入）::

        from app.services.platform_health_monitor import report_ok, report_fail
        try:
            r = deepseek.analyze(...)
            report_ok("deepseek")
        except Exception as e:
            report_fail("deepseek", str(e))

    典型用法（消费侧）::

        from app.services.platform_health_monitor import get_monitor
        m = get_monitor()
        if not m.allow_new_entry():
            return "L3 熔断：本轮不开新仓（持仓不动）"
        lots *= m.lot_multiplier()
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._comp: Dict[str, ComponentHealth] = {
            n: ComponentHealth(name=n) for n in COMPONENTS
        }
        self._level: DegradeLevel = DegradeLevel.L0
        self._raw_level: DegradeLevel = DegradeLevel.L0
        self._reason: str = "初始化"
        self._changed_ts: float = time.time()
        self._last_down_ts: float = 0.0
        self._recover_streak: int = 0
        #: 人工锁档（运维手动把平台钉在某档，用于演练/应急）。None = 自动。
        self._manual_level: Optional[DegradeLevel] = None
        self._manual_reason: str = ""
        self._transitions: List[dict] = []

    # ---------- 上报 ----------
    def report(self, component: str, ok: bool, error: str = "") -> None:
        """组件上报一次调用结果。未知组件名静默忽略（防打错字炸主链路）。"""
        try:
            with self._lock:
                c = self._comp.get(component)
                if c is None:
                    return
                c.report(ok, error)
                self._reevaluate_locked()
        except Exception:
            # 监控永不上抛：宁可判不准，也不能让健康检查搞崩交易
            pass

    def report_ok(self, component: str) -> None:
        self.report(component, True)

    def report_fail(self, component: str, error: str = "") -> None:
        self.report(component, False, error)

    # ---------- 判定 ----------
    def _classify_locked(self) -> tuple:
        """裸判定（不含迟滞）：返回 (level, reason)。

        判定矩阵（自上而下短路）：
          1. 行情明确失联/脏污        → L3（连输入都不可信，谈何决策）
          2. 双云全失 且 本地 LLM 不可用 → L3
          3. 双云全失 且 本地 LLM 可用   → L2（副驾模式）
          4. 恰一朵云失联              → L1
          5. 其余                      → L0

        为什么 market_data「未知(stale)」不判 L3：
          冷启动首轮、或行情线程刚起来时必然 stale，此时判 L3 会让系统
          永远开不出第一单。只有**明确失败**（连续 report_fail）才熔断。
        """
        md = self._comp["market_data"]
        ds = self._comp["deepseek"]
        hy = self._comp["hunyuan"]
        llm = self._comp["local_llm"]

        if md.is_down:
            return DegradeLevel.L3, f"行情源失联（连续{md.fail_streak}次失败）"

        ds_down = ds.is_down
        hy_down = hy.is_down

        if ds_down and hy_down:
            if llm.is_up:
                return DegradeLevel.L2, "云端双脑均失联 → 本地 Qwen3 副驾接管（需 Chronos 同向）"
            return DegradeLevel.L3, "云端双脑均失联且本地 LLM 不可用 → 停发新开仓"

        if ds_down:
            return DegradeLevel.L1, "DeepSeek 失联 → 混元单模型 + Chronos"
        if hy_down:
            return DegradeLevel.L1, "混元失联 → DeepSeek 单模型 + Chronos"

        # ★ 2026-08-16 管理后台审计修复：AI 核心组件（ds/hy/llm）长期未上报（休市/无 AI 调用）时，
        #   不能谎报「全部组件在线」——组件实际状态未知（stale），前端会看到
        #   「组件全灰但 L0 绿色」的矛盾。如实标注，前端据此显示「监测中」。
        #   market_data 不参与判断：它每 5s 由行情循环上报、永不 stale，会令 all() 恒 False。
        if all(c.is_stale for c in (ds, hy, llm)):
            return DegradeLevel.L0, "组件状态待确认（休市/无调用，探活未刷新）"

        return DegradeLevel.L0, "全部组件在线"

    def _reevaluate_locked(self) -> None:
        """重算档位并施加迟滞。必须在持锁状态下调用。"""
        raw, reason = self._classify_locked()
        self._raw_level = raw
        now = time.time()

        if raw > self._level:
            # ---- 恶化：立即生效（fail-fast，风险优先） ----
            self._apply_locked(raw, reason, now)
            self._last_down_ts = now
            self._recover_streak = 0
            return

        if raw < self._level:
            # ---- 改善：迟滞把关 ----
            self._recover_streak += 1
            cooled = (now - self._last_down_ts) >= RECOVER_COOLDOWN_SEC
            if self._recover_streak >= RECOVER_STREAK and cooled:
                self._apply_locked(
                    raw,
                    f"{reason}（连续{self._recover_streak}次健康 + 冷却{RECOVER_COOLDOWN_SEC:.0f}s 已过）",
                    now,
                )
                self._recover_streak = 0
            # 未达标：维持当前（更保守的）档位，不动
            return

        # ---- 持平：清空恢复计数，刷新原因文案 ----
        self._recover_streak = 0
        self._reason = reason

    def _apply_locked(self, level: DegradeLevel, reason: str, now: float) -> None:
        prev = self._level
        self._level = level
        self._reason = reason
        self._changed_ts = now
        self._transitions.append(
            {
                "ts": now,
                "from": int(prev),
                "to": int(level),
                "reason": reason,
            }
        )
        if len(self._transitions) > 100:
            self._transitions = self._transitions[-100:]

    # ---------- 人工锁档（运维通道 / 混沌演练） ----------
    def set_manual_level(self, level: Optional[DegradeLevel], reason: str = "") -> None:
        """把平台钉在指定档位（None = 解除，回到自动判定）。

        这是给**人**的通道，不是自动规则；与 L-M 紧急处置一脉相承：
        允许自然人负责任地干预，且留痕可追责。
        """
        with self._lock:
            self._manual_level = level
            self._manual_reason = reason or ("人工锁档" if level is not None else "")

    @property
    def manual_level(self) -> Optional[DegradeLevel]:
        with self._lock:
            return self._manual_level

    # ---------- 消费接口 ----------
    def level(self) -> DegradeLevel:
        with self._lock:
            return self._manual_level if self._manual_level is not None else self._level

    def lot_multiplier(self) -> float:
        """当前档位的手数系数。L3 = 0.0（配合 allow_new_entry 双保险）。"""
        return LOT_MULTIPLIER.get(self.level(), 0.0)

    def allow_new_entry(self) -> bool:
        """★ 只关水龙头：L3 停发新开仓，**不影响任何平仓/止损路径**。"""
        return self.level() < DegradeLevel.L3

    def require_local_confirm(self) -> bool:
        """L2 副驾模式下必须 Chronos 同向才放行（本地 8B 不单独决定方向）。"""
        return self.level() == DegradeLevel.L2

    def allow_cloud_call(self, which: str) -> bool:
        """是否还值得去调这朵云（断路器 closed / open / half-open）。

        * 未失联            → True（closed，正常调用）
        * 失联（is_down）    → 半开窗口：距上次失败/成功超过 CLOUD_PROBE_INTERVAL_SEC
                               才放行一次探活（half-open），否则返回 False。
                               这是唯一的自愈通道，删掉它系统就会永久卡在降级档。

        ★ 2026-08-11 优化：原实现「失联但 L0/L1 → 恒 True」导致 DS 欠费期间
          每 60s 都去撞一次死接口（8/11 实测 465 次 402 无效失败），纯烧配额、
          白耗主循环。L1 已有另一朵云兜底 + 本地副驾补位，重试成本不再"可接受"，
          统一走半开窗口，失联云每 180s 最多探活一次。
        """
        with self._lock:
            c = self._comp.get(which)
            if c is None:
                return True
            if not c.is_down:
                return True
            last = max(c.last_fail_ts, c.last_ok_ts)
            return (time.time() - last) >= CLOUD_PROBE_INTERVAL_SEC

    def snapshot(self) -> DegradeSnapshot:
        with self._lock:
            lv = self._manual_level if self._manual_level is not None else self._level
            reason = self._manual_reason if self._manual_level is not None else self._reason
            return DegradeSnapshot(
                level=lv,
                label=LEVEL_LABEL.get(lv, "未知"),
                detail=LEVEL_DETAIL.get(lv, ""),
                lot_multiplier=LOT_MULTIPLIER.get(lv, 0.0),
                allow_new_entry=lv < DegradeLevel.L3,
                require_local_confirm=lv == DegradeLevel.L2,
                reason=reason,
                raw_level=self._raw_level,
                pending_recover=(
                    self._raw_level < self._level and self._manual_level is None
                ),
                components={k: v.as_dict() for k, v in self._comp.items()},
                changed_ts=self._changed_ts,
                since_sec=time.time() - self._changed_ts,
            )

    def transitions(self, limit: int = 20) -> List[dict]:
        with self._lock:
            return list(self._transitions[-limit:])

    def reset(self) -> None:
        """仅供测试/演练复位。"""
        with self._lock:
            for c in self._comp.values():
                c.ok_streak = c.fail_streak = 0
                c.last_ok_ts = c.last_fail_ts = c.last_report_ts = 0.0
                c.total_ok = c.total_fail = 0
                c.last_error = ""
            self._level = DegradeLevel.L0
            self._raw_level = DegradeLevel.L0
            self._reason = "初始化"
            self._changed_ts = time.time()
            self._last_down_ts = 0.0
            self._recover_streak = 0
            self._manual_level = None
            self._manual_reason = ""
            self._transitions.clear()


# ============================================================
#  单例 + 模块级便捷入口
# ============================================================
_MONITOR: Optional[PlatformHealthMonitor] = None
_MONITOR_LOCK = threading.Lock()


def get_monitor() -> PlatformHealthMonitor:
    global _MONITOR
    if _MONITOR is None:
        with _MONITOR_LOCK:
            if _MONITOR is None:
                _MONITOR = PlatformHealthMonitor()
    return _MONITOR


def report_ok(component: str) -> None:
    get_monitor().report_ok(component)


def report_fail(component: str, error: str = "") -> None:
    get_monitor().report_fail(component, error)


def current_level() -> DegradeLevel:
    return get_monitor().level()


def lot_multiplier() -> float:
    """给 sizing 用的一行入口；任何异常都返回 1.0（不因监控故障误砍手数）。"""
    try:
        return get_monitor().lot_multiplier()
    except Exception:
        return 1.0


def allow_new_entry() -> bool:
    """给下单前置检查用；异常时返回 True（监控故障不应变成隐形停机）。"""
    try:
        return get_monitor().allow_new_entry()
    except Exception:
        return True


def require_local_confirm() -> bool:
    try:
        return get_monitor().require_local_confirm()
    except Exception:
        return False


def snapshot_dict() -> dict:
    try:
        return get_monitor().snapshot().as_dict()
    except Exception as e:  # pragma: no cover
        return {"level": 0, "level_name": "L0", "error": str(e)[:200]}


def reset_monitor() -> None:
    """测试用：复位单例内部状态（不重建对象，保持引用有效）。"""
    get_monitor().reset()


def degrade_enabled() -> bool:
    """总开关。`WX_DEGRADE_DISABLED=1` 可一键退回「无降级」旧行为。

    任何新机制都要留可关闭的后门：若降级车道本身出 bug 误判 L3，
    运维必须能在不改代码、不重新发版的前提下立刻恢复交易。
    """
    return os.getenv("WX_DEGRADE_DISABLED", "").strip() not in ("1", "true", "True")
