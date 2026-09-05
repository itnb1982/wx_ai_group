"""
万象Ai — AI 自主仓位管理（Position Manager · 纯加法增强层）· 2026-08-14
=========================================================================

用户授权（「可以，开干」）：让 AI 大脑「按行情自主管理仓位」——
  ① 确定性「利润走不动」机械平仓层（亚秒/每周期抓利润停滞·不依赖大模型·最快最稳）
  ② 本地 qwen3:8b 高频管仓（零 token·多周期 M5 微观特征，判断「开错单最小亏损平」「追踪锁利」）

设计铁律（对齐系统红线，纯加法）：
  * 提准非拦截：本层只增强出场判断，不砍交易笔数、不新增过滤。
  * 零新 MT5 订单类型：只用既有的 full_close / partial_close / modify_sl_tp 路径。
  * 硬 SL 永不在 AI 手里移除：TRAIL_TIGHTEN 的 new_sl 永远在市价与硬 SL 之间（锁利方向）。
  * 复用现有红线：亏损单保护(_with_trend 闸门)、硬地板(_merge_hard_floor_sl)、
    浮盈回吐锁利(peak_move)、L2 反转防抖(_REVERSAL_STATE)、防重复减半(_PARTIAL_DONE)。
  * 一键回退：POSITION_MANAGER_ENABLED=False 即整层失效（原有 M1 云端 + 规则引擎完全不动）。
  * ★ 全账号统一生效（多客户并行，不写死账号数）。

融合优先级（在 trade_executor 持仓循环里合并）：
  stall 机械平仓（利润走不动）> min_loss 最小亏损平（开错单）> 本地 8B 追踪锁利。
"""

from __future__ import annotations

import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from loguru import logger
except Exception:  # pragma: no cover
    class _Nop:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger = _Nop()


# ── 进程级缓存（auto_loop 单进程单线程，所有账号共享）──────────────────
_PM_CALL_TS: Dict[Tuple[str, str], float] = {}   # (account_id, ticket) -> 上次本地 8B 调用时间（节流）
_PM_BARS_CACHE: Dict[str, Tuple[float, dict]] = {}  # account_id -> (ts, {"M5":[bar...]})
_PM_PEAK: Dict[Tuple[str, str], float] = {}       # (account_id, ticket) -> 利润峰值（停滞检测用）
# ── 本地 8B 非阻塞取票缓存（2026-08-15 修复：8B 实测 11~24s 同步阻塞主循环）──
#   key=(account_id, ticket) -> (ts, vote|None)：上一票缓存，主循环零延迟读。
#   _PM_INFLIGHT 防同一 ticket 重复起线程；节流逻辑与 vision_exit 一致。
_PM_VOTE_CACHE: Dict[Tuple[str, str], Tuple[float, object]] = {}
_PM_INFLIGHT: Dict[Tuple[str, str], bool] = {}
_PM_LOCK = threading.Lock()


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "POSITION_MANAGER_ENABLED", True))
    except Exception:
        return True


def _local_enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "POSITION_MANAGER_LOCAL_ENABLED", True))
    except Exception:
        return True


def _cfg(name: str, default):
    try:
        from app.config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


# ───────────────────── 指标工具（自算，不依赖第三方包）─────────────────────
def _ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _closes_from_bars(bars: List[dict]) -> List[float]:
    out = []
    for b in (bars or []):
        try:
            out.append(float(b.get("close")))
        except Exception:
            pass
    return out


def _hold_seconds(pos: dict) -> float:
    ot = pos.get("open_time")
    if isinstance(ot, str):
        try:
            dt = datetime.fromisoformat(ot)
            return max(0.0, (datetime.now() - dt).total_seconds())
        except Exception:
            return 0.0
    if isinstance(ot, (int, float)):
        try:
            return max(0.0, time.time() - float(ot))
        except Exception:
            return 0.0
    # 兜底：time 字段（部分路径返回 Unix 秒）
    t = pos.get("time")
    if isinstance(t, (int, float)):
        try:
            return max(0.0, time.time() - float(t))
        except Exception:
            return 0.0
    return 0.0


def _fetch_m5_bars(account_id: str, symbol: str = "XAUUSD") -> List[dict]:
    """从 Worker 拉取 M5 实时 K 线（带 15s 缓存，避免每个持仓都打 IPC）。"""
    now = time.time()
    with _PM_LOCK:
        c = _PM_BARS_CACHE.get(account_id)
        if c and (now - c[0]) < 15.0:
            return c[1].get("M5", []) or []
    try:
        from app.services.mt5_service import mt5_service
        md = mt5_service.get_market_data(account_id, symbol)
        bars = ((md or {}).get("timeframes", {}) or {}).get("M5", {}) or {}
        m5 = bars.get("bars", []) or []
    except Exception:
        m5 = []
    with _PM_LOCK:
        _PM_BARS_CACHE[account_id] = (now, {"M5": m5})
    return m5


class PositionManagerAgent:
    """单账号持仓管家（按 account_id 单例）。全部异常安全，任何失败返回 None。"""

    def __init__(self, account_id: str):
        self.account_id = account_id

    # ───────────────── 主入口 ─────────────────
    def evaluate(self, pos: dict, atr: float, strategy, snap: dict) -> Optional[dict]:
        """返回 plan dict（将并入 trade_executor 的 plan）或 None（不动）。

        返回结构：
          {"action": "full_close", "reason": str}                       # 停滞/最小亏损平仓
          {"action": "full_close", "reason": str, "min_loss_exit": True}  # 开错单·最小亏损平（豁免亏损单保护）
          {"action": "trail_tighten", "new_sl": float, "reason": str}     # 追踪锁利（仅盈利单）
        """
        if not _enabled():
            return None
        try:
            return self._evaluate(pos, atr, strategy, snap)
        except Exception as e:  # 任何异常都不允许冒泡进交易链路
            logger.warning(f"[仓位管家] 评估异常→不干预: {e}")
            return None

    def _evaluate(self, pos: dict, atr: float, strategy, snap: dict) -> Optional[dict]:
        sym = "XAUUSD"
        ptype = str(pos.get("type") or "").lower()
        if ptype not in ("buy", "sell"):
            return None
        ticket = str(pos.get("ticket"))
        key = (self.account_id, ticket)

        open_price = float(pos.get("open_price") or 0)
        current = float(pos.get("current_price") or pos.get("close") or 0)
        if open_price <= 0 or current <= 0:
            return None
        sl = float(pos.get("sl") or 0)
        profit = float(pos.get("profit") or 0)
        hold_sec = _hold_seconds(pos)
        atr = float(atr or 0)
        if atr <= 0:
            return None

        # 利润峰值（停滞检测：未创新高才算「耗着」）
        with _PM_LOCK:
            peak = _PM_PEAK.get(key, profit)
            if profit > peak:
                peak = profit
                _PM_PEAK[key] = peak
        if peak <= 0:
            peak = 0.0

        # ① 确定性「利润走不动」机械平仓（盈利单 + 窄幅震荡 + 未创新高 + 持够时间）
        stall_plan = self._check_stall(ptype, current, profit, peak, hold_sec, atr)
        if stall_plan is not None:
            return stall_plan

        # ② 确定性「开错单·最小亏损平」门槛（浮亏 + M5 反转确认）
        hard_sl_dist = self._hard_sl_dist(ptype, current, sl, open_price)
        min_loss_ok = self._check_min_loss_gate(ptype, current, open_price, sl, profit, hold_sec, atr)
        if min_loss_ok:
            # 须本地 8B 双确认（可用时）；不可用则仅确定性门槛放行
            if _local_enabled():
                vote = self._call_local(pos, ptype, open_price, current, profit, hold_sec,
                                        atr, sl, hard_sl_dist, peak, snap)
                if vote is not None and vote.action == "FULL_MIN_LOSS" \
                        and vote.confidence >= float(_cfg("PM_MIN_LOSS_LOCAL_CONF", 0.45)):
                    return {"action": "full_close",
                            "reason": f"[开错单·最小亏损平] {vote.reason[:120]}",
                            "min_loss_exit": True}
                # 本地 8B 不认同（HOLD/其他）→ 尊重，不强制砍（防假反转误杀）
                return None
            # 本地 8B 不可用 → 确定性门槛已足够，直接放行
            return {"action": "full_close",
                    "reason": "[开错单·最小亏损平·确定性门槛] M5反转确认+浮亏超硬SL阈值",
                    "min_loss_exit": True}

        # ③ 本地 8B 追踪锁利（仅盈利且趋势健康时增强，不砍单）
        if _local_enabled() and profit > 0:
            vote = self._call_local(pos, ptype, open_price, current, profit, hold_sec,
                                    atr, sl, hard_sl_dist, peak, snap)
            if vote is not None and vote.action == "TRAIL_TIGHTEN" and vote.new_sl > 0:
                new_sl = self._clamp_trailing_sl(ptype, current, sl, vote.new_sl, atr)
                if new_sl > 0:
                    return {"action": "trail_tighten", "new_sl": new_sl,
                            "reason": f"[追踪锁利] {vote.reason[:120]}"}
        return None

    # ───────────────── ① 停滞检测 ─────────────────
    def _check_stall(self, ptype: str, current: float, profit: float, peak: float,
                     hold_sec: float, atr: float) -> Optional[dict]:
        if profit <= 0:
            return None  # 仅对盈利单
        min_hold = float(_cfg("PM_STALL_MIN_HOLD_SEC", 90))
        if hold_sec < min_hold:
            return None
        # 未创新高（利润已回落或横住）
        peak_drop = float(_cfg("PM_STALL_PEAK_DROP", 0.95))
        if peak > 0 and profit >= peak * peak_drop:
            return None  # 利润还在峰值附近，没「耗着」
        bars = _fetch_m5_bars(self.account_id)
        if not bars or len(bars) < 3:
            return None
        n = int(_cfg("PM_STALL_BARS", 3))
        window = bars[-n:]
        hi = max(float(b.get("high", current)) for b in window)
        lo = min(float(b.get("low", current)) for b in window)
        band = hi - lo
        atr_mult = float(_cfg("PM_STALL_ATR_MULT", 0.6))
        if band < atr * atr_mult:
            return {"action": "full_close",
                    "reason": f"[利润停滞·机械平仓] M5 近{n}根波幅{band:.1f}<ATR×{atr_mult:.1f}"
                              f"({atr:.1f})，利润走不动即锁利离场"}
        return None

    # ───────────────── ② 最小亏损门槛 ─────────────────
    def _hard_sl_dist(self, ptype: str, current: float, sl: float, open_price: float) -> float:
        """当前价距硬 SL 的点数（正=还有缓冲）。无 SL 时返回大值（视为无硬地板）。"""
        if sl <= 0:
            return 1e9
        if ptype == "buy":
            return current - sl
        return sl - current

    def _planned_risk_dist(self, ptype: str, open_price: float, sl: float) -> float:
        """计划风险点数（开仓价到硬 SL）。"""
        if sl > 0:
            return abs(open_price - sl)
        return 0.0

    def _check_min_loss_gate(self, ptype: str, current: float, open_price: float, sl: float,
                             profit: float, hold_sec: float, atr: float) -> bool:
        if profit >= 0:
            return False  # 盈利单不走这条（走停滞/追踪）
        min_hold = float(_cfg("PM_MIN_LOSS_MIN_HOLD_SEC", 60))
        if hold_sec < min_hold:
            return False
        # 浮亏已超过硬 SL 的此比例（亏得明显，不是瞬时抖动）
        pct = float(_cfg("PM_MIN_LOSS_HARD_SL_PCT", 0.40))
        planned = self._planned_risk_dist(ptype, open_price, sl)
        if planned <= 0:
            # 无硬 SL 配置：用 ATR 作风险基准（浮亏 > ATR×pct 视为明显）
            planned = atr
        loss_dist = abs(current - open_price)
        if loss_dist < planned * pct:
            return False
        # M5 反转确认（自算 RSI + EMA20 结构破位）
        bars = _fetch_m5_bars(self.account_id)
        closes = _closes_from_bars(bars)
        if len(closes) < 22:
            return False
        rsi = _rsi(closes, 14)
        ema20 = _ema(closes, 20)
        rsi_thr = float(_cfg("PM_MIN_LOSS_M5_RSI", 45.0))
        ema_break = bool(_cfg("PM_MIN_LOSS_M5_EMA_BREAK", True))
        if ptype == "buy":
            # 多头被套：M5 走空（RSI 低位 + 价格跌破 EMA20）
            rsi_ok = rsi < rsi_thr
            ema_ok = (not ema_break) or (current < ema20)
            return rsi_ok and ema_ok
        else:
            # 空头被套：M5 走多（RSI 高位 + 价格升破 EMA20）
            rsi_ok = rsi > (100.0 - rsi_thr)
            ema_ok = (not ema_break) or (current > ema20)
            return rsi_ok and ema_ok

    # ───────────────── ③ 本地 8B 调用（非阻塞·异步取票+缓存，绝不主循环等 16s）─────────────────
    # 动机（2026-08-15 实测）：qwen3:8b 管仓单次 11~24s(均值≈16s) 且抖动大；原同步写法在
    #   _manage_positions 内直接 await，会阻塞同账号规则化 smart_exit 达数十秒，6 账号共享 GPU0
    #   时还会串行排队。改为「后台线程取票 + 进程缓存」，主循环永远零延迟读上一票，8B 慢/挂
    #   都不影响硬 SL/TP 与 smart_exit 的实时性（与 vision_exit 同一架构）。
    def _call_local(self, pos, ptype, open_price, current, profit, hold_sec, atr,
                    sl, hard_sl_dist, peak, snap):
        key = (self.account_id, str(pos.get("ticket")))
        now = time.time()
        interval = float(_cfg("POSITION_MANAGER_CALL_INTERVAL", 15.0))
        with _PM_LOCK:
            c = _PM_VOTE_CACHE.get(key)
            # ① 命中新鲜缓存 → 直接返回（零延迟，绝不阻塞持仓主循环）
            if c and (now - c[0]) < interval:
                return c[1]
            # ② 节流：距上次起线程不足 interval，或已有线程在飞 → 回退上一票/None，不阻塞
            last = _PM_CALL_TS.get(key, 0.0)
            if (now - last) < interval or _PM_INFLIGHT.get(key):
                return c[1] if c else None
            # ③ 起异步线程去取 8B 票，主循环立即返回（缓存或 None）
            _PM_CALL_TS[key] = now
            _PM_INFLIGHT[key] = True
        try:
            threading.Thread(
                target=self._fetch_local_async,
                args=(key, pos, ptype, open_price, current, profit, hold_sec,
                      atr, sl, hard_sl_dist, peak, snap),
                name=f"pm-8b-{key[1]}", daemon=True,
            ).start()
        except Exception:
            with _PM_LOCK:
                _PM_INFLIGHT[key] = False
        return c[1] if c else None

    def _fetch_local_async(self, key, pos, ptype, open_price, current, profit,
                           hold_sec, atr, sl, hard_sl_dist, peak, snap):
        """后台线程：真正调本地 8B 取管仓票并写入缓存；失败写 None（主循环据此保守处理）。"""
        try:
            from app.services.local_llm_service import get_local_llm
            llm = get_local_llm()
            if llm is None:
                with _PM_LOCK:
                    _PM_VOTE_CACHE[key] = (time.time(), None)
                return
            m5 = _fetch_m5_bars(self.account_id)
            closes = _closes_from_bars(m5)
            _m5_rsi = _rsi(closes, 14)
            _m5_ema = _ema(closes, 20)
            _reg = (snap or {}).get("regime") or {}
            if isinstance(_reg, dict):
                _reg_str = _reg.get("label_zh") or _reg.get("regime") or ""
            else:
                _reg_str = str(_reg)
            ctx = {
                "symbol": "XAUUSD",
                "direction": ptype.upper(),
                "open_price": open_price,
                "current_price": current,
                "floating_pnl": round(profit, 2),
                "hold_sec": round(hold_sec, 1),
                "atr": round(atr, 2),
                "hard_sl": sl,
                "hard_sl_dist": round(hard_sl_dist, 2),
                "profit_peak": round(peak, 2),
                "m5": {
                    "rsi": round(_m5_rsi, 1),
                    "ema20": round(_m5_ema, 2),
                    "trend": "down" if (ptype == "buy" and current < _m5_ema) else (
                        "up" if (ptype == "sell" and current > _m5_ema) else "side"),
                    "last_closes": [round(c, 2) for c in closes[-6:]],
                },
                "h1_trend": str((((snap or {}).get("smc_features") or {}).get("per_tf", {}) or {})
                                .get("H1", {}).get("bias", "")),
                "regime": _reg_str,
            }
            vote = llm.position_manage(ctx)
            with _PM_LOCK:
                _PM_VOTE_CACHE[key] = (time.time(), vote)
        except Exception as e:
            logger.warning(f"[仓位管家] 本地8B异步调用失败→跳过: {e}")
            with _PM_LOCK:
                _PM_VOTE_CACHE[key] = (time.time(), None)
        finally:
            with _PM_LOCK:
                _PM_INFLIGHT[key] = False

    # ───────────────── 追踪 SL 夹紧（红线：不可移除/松动硬 SL）─────────────────
    def _clamp_trailing_sl(self, ptype: str, current: float, sl: float, proposed: float, atr: float) -> float:
        """new_sl 必须落在「市价与硬 SL 之间」的保护侧，且距市价 ≥ ATR×min_mult 留呼吸空间。

        BUY: 0 < new_sl < current（且 ≤ 硬 SL 若已设）；SELL: new_sl > current（且 ≥ 硬 SL）。
        """
        min_mult = float(_cfg("PM_TRAIL_MIN_ATR_MULT", 0.3))
        floor_space = atr * min_mult
        if ptype == "buy":
            # 上限：不能比当前价高（否则变止损在市价上方=必被扫）
            hi = current - floor_space
            # 下限：若已有硬 SL，不能比硬 SL 更松（即不能 > 硬 SL）
            lo = sl if (sl and sl > 0) else 0.0
            new_sl = min(proposed, hi)
            new_sl = max(new_sl, lo)
            if new_sl <= 0 or new_sl >= current:
                return 0.0
            return round(new_sl, 2)
        else:
            lo = current + floor_space
            hi = sl if (sl and sl > 0) else 1e12
            new_sl = max(proposed, lo)
            new_sl = min(new_sl, hi)
            if new_sl <= current or new_sl >= 1e11:
                return 0.0
            return round(new_sl, 2)


# ── 单例工厂（按 account_id；禁用时返回 None，trade_executor 据此整层跳过）──
_PM_INSTANCES: Dict[str, Optional[PositionManagerAgent]] = {}


def get_position_manager(account_id: str) -> Optional[PositionManagerAgent]:
    if not _enabled():
        return None
    inst = _PM_INSTANCES.get(account_id)
    if inst is None and account_id not in _PM_INSTANCES:
        try:
            inst = PositionManagerAgent(account_id)
        except Exception:
            inst = None
        _PM_INSTANCES[account_id] = inst
    return inst
