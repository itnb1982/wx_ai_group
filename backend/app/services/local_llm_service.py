"""
万象Ai — 本地 LLM 服务（Qwen3-8B via Ollama）· V6 Phase 6 §9.4.4/9.4.5
=========================================================================
本机硬约束：RTX 3060 Ti **8GB**，桌面 + Chronos-2 常驻已占 ~1.5GB，空闲 ~6.4GB。
选型锁定：`qwen3:8b` Q4_K_M(5.0GB) + num_ctx=4096 + keep_alive=30m
         + 强制 `/no_think`（关思维链，省 token 省时延）+ temperature=0.3
         → 核算峰值 ~5.45GB，留 ~1GB 余量可行。备选 `qwen3:4b`。

★ 角色铁律（Fin-Bias, ACL2026 实证：7~8B 金融方向判断接近随机）
-----------------------------------------------------------------
本地 8B 的能力边界很清楚：**验证型任务它行，生成型方向判断它不行。**
故它在系统里只有两个身份，且**互斥**：

  身份 A「校对员」(proofread) —— L0/L1 常态
      只做三件确定性很强的事：
        1) JSON 结构完整性（该有的字段在不在、类型对不对）
        2) 自相矛盾检测（说 BUY 却写「下行风险主导」、SL 挂在错误一侧）
        3) 幻觉价格检测（引用的价格离真实盘口十万八千里）
      **绝不改方向、绝不投票**，只输出 issues 列表供审计与告警。
      —— 这类「拿着标准答案对照检查」的活，小模型胜任。

  身份 B「副驾」(copilot) —— 常态确认型（进 meta_agent 融合第五票）+ L2 关键路径补位，
      均只做「确认型」、不做「生成型」，且必须叠三道锁：
        ① Chronos 时序必须同向（数值模型背书）
        ② 置信度门槛 ≥ COPILOT_MIN_CONFIDENCE
        ③ 手数系数砍到 0.40（由 platform_health_monitor 下发）
      —— 宁可少赚，不能让近似随机的方向判断满仓上阵。

★ 显存纪律
-----------
L0 常态**不加载**本地模型（不占显存，把 8GB 留给 Chronos-2 和桌面）。
只有 (a) 显式开启校对员，或 (b) 降到 L2 需要副驾时，才 warm 模型。
`keep_alive=30m` 让降级期间连续调用不反复加载；恢复 L0 后自然过期释放。

★ 工程纪律
-----------
* 全异常安全：任何失败一律返回 None / 空结果，**绝不上抛**。上层据此当「不可用」。
* 可用性探测带缓存 + 负缓存：Ollama 没装是最常见情形（本机当前就没装），
  不能每轮都花 2s 去连一个必然失败的端口。
* HTTP 传输层可注入（`set_transport`），单测无需真起 Ollama。
* 不 import 任何 models/routers，零业务依赖。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:  # loguru 在测试环境也可用，但保持可选以免单测被日志牵连
    from loguru import logger
except Exception:  # pragma: no cover
    class _Nop:
        def __getattr__(self, _):
            return lambda *a, **k: None

    logger = _Nop()  # type: ignore


# ============================================================
#  配置
# ============================================================
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
FALLBACK_MODEL = "qwen3:4b"  # 显存告急时的降级选择

#: num_ctx 保守基线。
#: ★ 历史注释（"8192 会把峰值推过 6GB 触发 OOM"）已过时——那是视觉模型与主脑
#: 共挤**同一张** 3060 Ti 时代的结论。2026-08-16 视觉模型迁到第二张卡后，
#: 主脑卡（Ollama 11434）独占 8GB，qwen3:8b Q4_K_M 常驻仅 ~5.3GB，尚有 ~2.7GB 空闲。
#: 但探测失败 / 显存被别的进程挤占时仍必须退回 4096，故保留为基线值。
NUM_CTX = 4096
#: 扩展档：显存确实有余量时使用。上下文翻倍 = 一次能喂完整多周期 K 线 + 订单流
#: + 新闻 + 持仓上下文而不被截断，这是**纯加法提准**（不砍任何信号，只让模型看得更全）。
NUM_CTX_EXPANDED = 8192
#: 4096→8192 的增量成本 = KV cache 翻倍。
#: qwen3-8b：36 层 × 8 个 KV 头 × head_dim 128 × 2(K/V) × 2 bytes ≈ 144 KB/token
#: → 4096 tokens ≈ 576 MB，8192 tokens ≈ 1152 MB，**增量约 576 MB**。
#: 要求空闲 ≥ 1800MB（3 倍安全边际），因为推理峰值还有激活值与碎片开销。
CTX_EXPAND_FREE_MB = 1800
#: 显存水位会随别的进程（桌面渲染/远控软件）变动，故 5 分钟重新评估一次。
#: 不能每次调用都跑 nvidia-smi——那会把探测本身变成负载。
_CTX_TTL_SEC = 300.0
#: 常驻 30 分钟：降级期间通常持续数分钟到数十分钟，反复加载 5GB 权重不可接受。
KEEP_ALIVE = "30m"
#: 0.3 而非 0：留一点点随机性避免复读，但足够确定性以保证 JSON 稳定。
TEMPERATURE = 0.3

#: 校对员超时要短——它是「顺手检查」，不能拖慢主决策链。
#: 但首次调用要加载 ~5GB 权重进显存会超过 12s，故首调放宽到 90s（仅加载那一次）。
PROOFREAD_TIMEOUT = 40.0
PROOFREAD_TIMEOUT_FIRST = 90.0

#: ★ 2026-08-08：判定「模型是否还在显存里」的安全窗口。
#: Ollama keep_alive=30m，但那是**它自己**的计时；我们无法直接查询驻留状态。
#: 取 20min（<30min）留足安全边际：只要距上次成功调用超过 20 分钟，
#: 就当权重可能已被卸载，按冷启动给长超时。宁可多等一次，
#: 也不要因为按"温热"发 12s 超时而让校对静默失效。
#: 典型场景：周五收市 → 周一开市，中间几十小时无调用，模型必然已卸载。
HOT_WINDOW_SEC = 20 * 60

#: 结构性审计阈值（代码侧确定性检查，不依赖 LLM 算术）
SL_MIN_DIST_USD = 3.0    # 止损距入场过近（约 30 点），易秒触 → minor
RR_MIN_RATIO = 0.3       # 回报/风险 < 0.3，盈亏比失衡 → minor
PRICE_HALLUCINATION_RATIO = 0.05  # 决策价偏离真实盘口 > 5% → major（幻觉）
#: 副驾是关键路径（此时没有云可用），给宽一点。
COPILOT_TIMEOUT = 60.0
#: 仓位管理管仓：非关键路径（只是增强层，确定性层兜底），给稍紧一点避免拖交易循环。
POSITION_MANAGE_TIMEOUT = 60.0
#: 探活超时：端口没开会立刻 ECONNREFUSED，2s 足矣。
PROBE_TIMEOUT = 2.0

#: 探活结果缓存：成功缓存 60s，失败缓存 300s（负缓存更久，见上文理由）。
PROBE_TTL_OK = 60.0
PROBE_TTL_FAIL = 300.0

#: 副驾放行的最低置信度。0.60 与云端 `ai_reverse_close_confidence` 对齐，
#: 但副驾还要额外叠 Chronos 同向，实际门槛比云端更严。
COPILOT_MIN_CONFIDENCE = 0.60

# ------------------------------------------------------------
#  自适应自一致性（Adaptive Self-Consistency）—— 纯加法提准
# ------------------------------------------------------------
#: ★ 2026-08-18 落地。调研依据（≥3 源交叉验证）：
#:   ① Self-Consistency（Wang et al.）：多次采样 + 多数投票，MATH K=5 由 54%→65%；
#:      Wang & Wang 2025(arXiv:2503.16974) 证 **3~5 次聚合即可显著提升一致性**，
#:      再往上收益迅速衰减 → 故 K 上限锁 3，不做 K=5/20（本机时间预算不值）。
#:   ② Reliability-Aware Adaptive Self-Consistency（2026-01）：**按难度动态采样**——
#:      简单样本 K=1~2，困难样本才加采样。这是本实现的核心：绝大多数轮次只花 1 次
#:      推理，只有"边界情形"才追加，平均 K≈1.3~1.5，时间成本可控。
#:   ③ 该系列研究共同警示："self-consistency fixes randomness, not ignorance"——
#:      模型**系统性偏见**下多数投票会放大错误。故本实现只用于提升「本地脑自身
#:      判断的稳定性」，**最终仍受 copilot_gate 三道锁（Chronos 同向等外部验证器）
#:      约束**，绝不让自一致性替代外部背书。
#:
#: 硬件算账（2×RTX3060Ti 8GB 死上限）：追加采样**复用同一份已驻留权重**，
#: 显存增量 = 0MB，只吃时间。H4 级决策每 4 小时才换一根 K 线，而决策循环 ~40s，
#: 时间冗余约 360 倍 —— 在本机，**时间是最富余的资源、显存是最紧的**，
#: 所以提准动作一律设计成「吃时间、不吃显存」。
#:
#: 边界区间 [0.50, 0.78)：低于 0.50 基本会被 min_confidence(0.60) 挡掉，追加采样
#: 是浪费；高于 0.78 模型已很确信，追加大概率只是复读。只有中间这段"模糊地带"
#: 才是投票真正能改变结论的区域。
COPILOT_SC_LOW = 0.50
COPILOT_SC_HIGH = 0.78
#: 追加采样次数（首票 + 2 = 最多 3 票，与调研 ③ 的 3~5 下界对齐）。
COPILOT_SC_EXTRA = 2
#: 追加采样温度。0.7 落在调研建议的 0.7~0.9 下沿：既有分歧多样性，
#: 又不至于把 8B 的 JSON 格式稳定性搞崩（本机 8B 的 JSON 抖动本来就偏高）。
COPILOT_SC_TEMP = 0.7
#: 追加采样的单次超时。比首票(30s)紧——追加是增强项，不能拖垮决策循环；
#: 超时就用已有票，不影响主流程。
COPILOT_SC_TIMEOUT = 60.0
#: 总开关：置 "0" 关闭自一致性，退回单次采样（应急/对照实验用）。
def copilot_sc_enabled() -> bool:
    return os.getenv("WX_COPILOT_SELF_CONSISTENCY", "1").strip() not in ("0", "false", "False")


def _base_url() -> str:
    return (os.getenv("WX_OLLAMA_URL") or DEFAULT_BASE_URL).rstrip("/")


def _model_name() -> str:
    return os.getenv("WX_LOCAL_LLM_MODEL") or DEFAULT_MODEL


def local_llm_enabled() -> bool:
    """总开关：`WX_LOCAL_LLM_DISABLED=1` 彻底关闭本地 LLM（连探活都不做）。"""
    return os.getenv("WX_LOCAL_LLM_DISABLED", "").strip() not in ("1", "true", "True")


# ------------------------------------------------------------------
#  显存自适应上下文
# ------------------------------------------------------------------
#: 缓存：{"value": int, "ts": float, "reason": str}
_ctx_cache: dict = {"value": None, "ts": 0.0, "reason": ""}


def _self_port() -> int:
    """本服务实际使用的 Ollama 端口（不写死，跟随 WX_OLLAMA_URL）。"""
    try:
        tail = _base_url().rsplit(":", 1)[-1]
        return int(tail.split("/")[0])
    except Exception:
        return 11434


def resolve_num_ctx(force: bool = False) -> int:
    """按主脑卡**实测空闲显存**决定 num_ctx，探测不到就保守回退。

    为什么要动态判定而不是写死一个数：
      - 显存不是本进程独占——桌面渲染、远控软件、Ollama 自愈残留的僵尸
        llama-server 都会啃掉几百 MB 到几 GB。写死 8192 会在某天静默 OOM，
        模型被踢出显存 → 本地票消失 → 关云状态下有效方向票 <2 → 强制 HOLD
        → **整个系统静默停止交易**。这是最不能接受的失败模式。
      - 写死 4096 又浪费了迁卡后腾出来的 2.7GB，让模型看不到完整上下文。
    所以：能升就升，一旦水位变紧下一个周期自动降回来。

    环境变量 `WX_LOCAL_LLM_NUM_CTX` 可强制覆盖（运维应急用，跳过全部探测）。
    """
    forced = os.getenv("WX_LOCAL_LLM_NUM_CTX", "").strip()
    if forced.isdigit():
        v = int(forced)
        if 512 <= v <= 65536:
            return v

    now = time.time()
    if not force and _ctx_cache["value"] and (now - _ctx_cache["ts"]) < _CTX_TTL_SEC:
        return int(_ctx_cache["value"])

    value, reason = NUM_CTX, "默认基线"
    try:
        # 延迟 import：runtime_health 会反向读本模块的 DEFAULT_MODEL，
        # 顶层 import 会造成循环依赖。
        from . import runtime_health  # type: ignore

        snap = runtime_health.gpu_snapshot()
        if not snap.get("available"):
            reason = f"显存探测不可用（{snap.get('reason') or '未知'}）→ 保守基线"
        else:
            port = _self_port()
            mine = None
            for g in snap.get("gpus") or []:
                if not isinstance(g, dict):
                    continue
                if int(g.get("ollama_port") or 0) == port:
                    mine = g
                    break
            if mine is None:
                reason = f"未定位到端口 {port} 对应显卡 → 保守基线"
            else:
                free = int(mine.get("mem_free_mb") or 0)
                label = mine.get("windows_label") or f"GPU#{mine.get('index')}"
                if free >= CTX_EXPAND_FREE_MB:
                    value = NUM_CTX_EXPANDED
                    reason = f"{label} 空闲 {free}MB ≥ {CTX_EXPAND_FREE_MB}MB → 上下文扩展档"
                else:
                    reason = f"{label} 空闲仅 {free}MB < {CTX_EXPAND_FREE_MB}MB → 保守基线"
    except Exception as e:  # pragma: no cover - 探测异常绝不能拖垮决策链
        reason = f"探测异常（{type(e).__name__}）→ 保守基线"

    prev = _ctx_cache.get("value")
    _ctx_cache.update({"value": value, "ts": now, "reason": reason})
    if prev != value:
        try:
            logger.info(f"[local_llm] num_ctx {prev or '-'} → {value}：{reason}")
        except Exception:
            pass
    return value


def num_ctx_detail() -> dict:
    """给状态接口/前端用：当前档位 + 判定依据（可审计，不让运维猜）。"""
    v = resolve_num_ctx()
    return {
        "num_ctx": v,
        "baseline": NUM_CTX,
        "expanded": NUM_CTX_EXPANDED,
        "expanded_active": v >= NUM_CTX_EXPANDED,
        "require_free_mb": CTX_EXPAND_FREE_MB,
        "reason": _ctx_cache.get("reason") or "",
    }


# ============================================================
#  HTTP 传输层（可注入，便于单测）
# ============================================================
#: 签名: (method, url, payload|None, timeout) -> (status_code, body_text)
Transport = Callable[[str, str, Optional[dict], float], tuple]

_transport: Optional[Transport] = None
_transport_lock = threading.Lock()


def _default_transport(method: str, url: str, payload: Optional[dict], timeout: float):
    """默认走 httpx（requirements 已含）。任何异常上抛给调用方统一吞。"""
    import httpx

    with httpx.Client(timeout=timeout) as cli:
        if method.upper() == "GET":
            r = cli.get(url)
        else:
            r = cli.post(url, json=payload or {})
        return r.status_code, r.text


def set_transport(fn: Optional[Transport]) -> None:
    """注入自定义传输（测试用）。传 None 恢复默认。"""
    global _transport
    with _transport_lock:
        _transport = fn


def _call(method: str, path: str, payload: Optional[dict], timeout: float):
    fn = _transport or _default_transport
    return fn(method, f"{_base_url()}{path}", payload, timeout)


# ============================================================
#  数据结构
# ============================================================
@dataclass
class ProofreadResult:
    """校对员输出。`ok=True` 表示没发现问题。

    ★ 2026-08-08 审计修复：severity 必须**区分来源**，否则断路器会把否决权
    交给一个金融判断接近随机的 8B 模型（Fin-Bias, ACL2026）。

      · `code_severity` —— 纯算术结构审计的结论（SL/TP 方向反、价格幻觉）。
        100% 机械可验证，不含任何主观判断，**只有它有资格触发断路器**。
      · `llm_severity`  —— 8B 语义审计的结论（理由与方向自相矛盾等）。
        有参考价值，但会误报，**只记录告警，永不拦单**。
      · `severity`      —— 二者取高，仅用于展示/告警分级，不作拦单依据。

    调用方若要做「拦单」这类不可逆动作，必须读 `code_severity`，读 `severity`
    就是把主观判断混进了硬规则。
    """

    ok: bool
    issues: List[str] = field(default_factory=list)
    severity: str = "none"  # none / minor / major（展示用：取二者高者）
    code_severity: str = "none"  # 仅代码结构审计（唯一可作断路器依据）
    llm_severity: str = "none"   # 仅 LLM 语义审计（永不拦单）
    code_issues: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    model: str = ""
    raw: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "severity": self.severity,
            "code_severity": self.code_severity,
            "llm_severity": self.llm_severity,
            "code_issues": self.code_issues,
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
        }


@dataclass
class CopilotVote:
    """副驾方向票（仅 L2 使用）。"""

    decision: str  # BUY / SELL / HOLD
    confidence: float
    reason: str = ""
    latency_ms: float = 0.0
    model: str = ""
    # ---- 自适应自一致性审计（2026-08-18 新增，纯观测字段）----
    #: 实际采样次数（1 = 走了快路径没追加；2~3 = 触发了边界追加采样）
    samples: int = 1
    #: 各次采样的方向序列，如 ["BUY","BUY","HOLD"]，供事后审计投票质量
    sample_votes: tuple = ()
    #: 是否达成多数共识（3 票各不相同 → False，此时降级为 HOLD）
    consensus: bool = True

    def as_dict(self) -> dict:
        return {
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "reason": self.reason[:300],
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "source": "local_copilot",
            "samples": self.samples,
            "sample_votes": list(self.sample_votes),
            "consensus": self.consensus,
        }


@dataclass
class PositionManageVote:
    """仓位管理管仓票（本地 8B 增强层，纯加法）。

    action 取值（与现有执行路径对齐，不引入新动作）：
      HOLD           —— 不动（默认，模型证据不足时必须选这个）
      TRAIL_TIGHTEN  —— 上移 SL 锁定更多浮盈（仅在盈利单上生效，new_sl 必填）
      PARTIAL_EXIT   —— 部分平仓落袋（仅在盈利单上生效，close_pct 必填 0~1）
      FULL_MIN_LOSS  —— 认错单，找最小亏损位置全平（须配合确定性 M5 反转门槛，否则执行层拦截）
    """

    action: str = "HOLD"
    close_pct: float = 0.0
    new_sl: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    latency_ms: float = 0.0
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "close_pct": round(self.close_pct, 3),
            "new_sl": round(self.new_sl, 3),
            "confidence": round(self.confidence, 3),
            "reason": self.reason[:300],
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "source": "local_position_manager",
        }


# ============================================================
#  JSON 解析（小模型输出常带杂质，必须容错）
# ============================================================
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _extract_json(text: str) -> Optional[dict]:
    """从模型输出里抠出第一个 JSON 对象。

    小模型即使被要求「只输出 JSON」也常带前后缀（```json 围栏、
    `<think>` 残留、结尾解释）。这里逐层剥：
      去 think 块 → 去代码围栏 → 正则取最外层花括号 → json.loads
    """
    if not text:
        return None
    t = _THINK_BLOCK.sub("", text)
    t = t.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = _JSON_BLOCK.search(t)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _clean_decision(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in ("BUY", "LONG", "买入", "做多"):
        return "BUY"
    if s in ("SELL", "SHORT", "卖出", "做空"):
        return "SELL"
    return "HOLD"


def _clean_conf(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1.0:  # 模型有时输出 75 表示 75%
        f = f / 100.0
    return max(0.0, min(f, 1.0))


# ============================================================
#  服务
# ============================================================
class LocalLLMService:
    """Ollama 本地模型客户端（线程安全、懒加载、全异常安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._probe_ts: float = 0.0
        self._probe_ok: bool = False
        self._probe_reason: str = "未探测"
        self._models: List[str] = []
        self._warmed: bool = False
        # ★ 2026-08-08 审计修复：光有 `_warmed` 布尔位是不够的。
        #   它是**单向锁存**——一旦 True 永不回退，但 Ollama 的 keep_alive=30m
        #   到期后会把 5GB 权重踢出显存。休市几小时后模型早就被卸载了，
        #   `_warmed` 却还是 True → 下一笔校对只给 12s 常规超时，
        #   而冷加载要十几秒 → **必然 ReadTimeout**（已在实测中复现一次）。
        #   后果：周一开市第一笔决策的校对静默失效（fail-open 不拦单，
        #   安全但功能等于没有）。改用"上次成功调用时间戳"判断真实热度。
        self._last_call_ts: float = 0.0
        self._calls_ok = 0
        self._calls_fail = 0
        self._last_error: str = ""
        self._last_latency_ms: float = 0.0
        # ── 分职责工作量统计（供系统管理页展示"这个模型到底干了什么"）──
        # 只统计不落库：这些数字是运行期观测量，重启归零即可，
        # 为它们建表会在主链路上引入一次没必要的写库。
        self._proofread_runs = 0        # 校对执行次数
        self._proofread_issues = 0      # 校对查出问题的次数（非问题条数）
        self._copilot_runs = 0          # 副驾出票次数
        self._copilot_allowed = 0       # 副驾票通过三道锁的次数
        self._position_manage_runs = 0  # 仓位管理管仓调用次数
        self._last_activity: str = ""   # 最近一次工作的人话描述
        self._last_activity_ts: float = 0.0
        self._latency_samples: List[float] = []  # 最近 50 次时延，算 p50/p95

    # ---------- 内部：记录一次活动 ----------
    def _note(self, text: str) -> None:
        with self._lock:
            self._last_activity = text[:200]
            self._last_activity_ts = time.time()

    # ---------- 可用性 ----------
    def available(self, force: bool = False) -> bool:
        """探测 Ollama 是否可用（带正/负缓存）。

        注意：只探端口 + 模型清单，**不加载模型**（不占显存）。
        真正 warm 由第一次业务调用触发。
        """
        if not local_llm_enabled():
            with self._lock:
                self._probe_ok = False
                self._probe_reason = "已通过 WX_LOCAL_LLM_DISABLED 关闭"
            return False

        now = time.time()
        with self._lock:
            ttl = PROBE_TTL_OK if self._probe_ok else PROBE_TTL_FAIL
            if not force and self._probe_ts > 0 and (now - self._probe_ts) < ttl:
                return self._probe_ok

        ok, reason, models = self._do_probe()
        with self._lock:
            self._probe_ts = time.time()
            self._probe_ok = ok
            self._probe_reason = reason
            self._models = models
        return ok

    def _do_probe(self):
        try:
            code, body = _call("GET", "/api/tags", None, PROBE_TIMEOUT)
        except Exception as e:
            return False, f"Ollama 不可达: {type(e).__name__}", []
        if code != 200:
            return False, f"Ollama 返回 HTTP {code}", []
        try:
            data = json.loads(body or "{}")
            models = [str(m.get("name", "")) for m in (data.get("models") or [])]
        except Exception:
            return False, "Ollama 响应无法解析", []

        want = _model_name()
        # Ollama 的 name 常带 tag（qwen3:8b），也可能是 qwen3:8b-q4_K_M
        hit = any(m == want or m.startswith(want.split(":")[0] + ":") for m in models)
        if not hit:
            return False, f"未找到模型 {want}（已装: {models[:5]}）", models
        return True, "ok", models

    def _is_hot(self) -> bool:
        """权重是否**大概率**还驻留在显存里。

        Ollama 不提供"某模型当前是否已加载"的查询接口，所以只能推断：
        距上次成功调用不超过 HOT_WINDOW_SEC(20min) < keep_alive(30min)，
        就认为还热。判断保守——宁可误判为冷（多给超时预算，最多慢一点），
        也不要误判为热（超时太短 → 校对静默失效，断路器形同虚设）。
        """
        with self._lock:
            if not self._warmed or self._last_call_ts <= 0:
                return False
            return (time.time() - self._last_call_ts) < HOT_WINDOW_SEC

    # ---------- 底层生成 ----------
    def _generate(
        self,
        prompt: str,
        timeout: float,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        """调用 /api/generate。失败一律返回 None（不抛）。

        `/no_think`：Qwen3 的思维链开关。实时交易链路上思维链是纯成本
        （多花几百 token + 数秒），而校对/副驾都不需要长推理。

        `temperature`：留空 = 用全局 TEMPERATURE(0.3，为 JSON 稳定性调低)。
        ★ 2026-08-18 新增可覆盖：自适应自一致性（copilot）的**追加采样**需要
        更高温度才能产生有价值的分歧——温度 0.3 重复采样几乎输出同一答案，
        投票就退化成"同一个答案数三遍"，白花时间。首票仍走 0.3 保稳定，
        只有边界情形的追加票才升温，见 `COPILOT_SC_TEMP`。
        """
        if not self.available():
            return None
        payload = {
            "model": _model_name(),
            "prompt": f"/no_think\n{prompt}",
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "num_ctx": resolve_num_ctx(),
                "temperature": (
                    TEMPERATURE if temperature is None else max(0.0, min(1.5, float(temperature)))
                ),
            },
        }
        t0 = time.time()
        try:
            code, body = _call("POST", "/api/generate", payload, timeout)
        except Exception as e:
            with self._lock:
                self._calls_fail += 1
                self._last_error = f"{type(e).__name__}: {str(e)[:120]}"
                # 传输层异常 → 让下次 available() 重新探测（可能 Ollama 挂了）
                self._probe_ts = 0.0
            return None
        dt = (time.time() - t0) * 1000.0
        if code != 200:
            with self._lock:
                self._calls_fail += 1
                self._last_error = f"HTTP {code}"
            return None
        try:
            resp = json.loads(body or "{}").get("response", "")
        except Exception:
            with self._lock:
                self._calls_fail += 1
                self._last_error = "响应非 JSON"
            return None
        with self._lock:
            self._calls_ok += 1
            self._warmed = True
            self._last_call_ts = time.time()  # 记录真实热度（配合 _is_hot 判超时）
            self._last_latency_ms = dt
            # 滚动窗口：只留最近 50 次，够算分位又不会无界增长。
            self._latency_samples.append(dt)
            if len(self._latency_samples) > 50:
                self._latency_samples = self._latency_samples[-50:]
        return resp

    # ---------- 通用生成（供 OPRO 优化器等内部复用）----------
    def generate_text(self, prompt: str, timeout: float = 60.0) -> Optional[str]:
        """通用文本生成入口（关闭思维链、带超时与全异常保护）。

        与业务专用方法(proofread/copilot/position_manage)不同，本方法不做任何
        领域格式化，仅把 prompt 透传给 qwen3:8b 并返回原始响应文本。供 OPRO 风格
        优化器（本地 8B 充当优化器、从「解-得分」历史提出下一候选参数）等通用场景复用。
        失败/超时/不可用一律返回 None，调用方须自行决定降级策略。
        """
        if not self.available():
            return None
        return self._generate(prompt, timeout)

    # ---------- 身份 A：校对员 ----------
    def proofread(
        self, decision: dict, market_snapshot: Optional[dict] = None
    ) -> Optional[ProofreadResult]:
        """校对云端决策。返回 None = 本地模型不可用（不是「没问题」）。

        调用方必须区分 None 与 ok=True：前者是「没查」，后者是「查过没事」。
        把没查当没事，是监控设计里最经典的自欺。

        ★ Phase 9.1（2026-08-08）双层审计：
          (a) **代码侧结构性审计**（`_structural_audit`）：SL/TP 方向、止损过近、
              盈亏比、价格幻觉——纯算术，100% 可靠，不依赖 LLM 的算术能力。
          (b) **LLM 语义审计**（提示词）：只查「理由文本与方向是否自相矛盾」这种
              正则做不好、但 8B 语言理解刚好胜任的活。
          二者结果合并，severity 取高者。即使 LLM 偶发超时（首调加载权重），
          代码结构审计仍返回，断路器不失效。
        """
        if not decision:
            return None
        if not self.available():
            return None

        snap = market_snapshot or {}
        _cp_raw = snap.get("current_price")
        # 防御：market_analyzer 在生产环境把 current_price 返回为
        # {"bid","ask","last"} 字典，须拆出标量 last，否则 _structural_audit
        # 的 `price > 0` 会对 dict 抛 TypeError。
        # ★ 2026-08-17 修复：非 dict（纯 float）时透传原值——此前 else None
        #   导致 current_price=1000.0 被丢成 price=None，价格幻觉检查静默跳过。
        price = (
            _cp_raw.get("last") if isinstance(_cp_raw, dict) else _cp_raw
        ) or snap.get("price") or snap.get("close")

        # (a) 代码侧结构性审计（不依赖 LLM）
        code_issues, code_sev = self._structural_audit(decision, price)

        # (b) LLM 语义审计（只查理由-方向自相矛盾 + 字段齐全）
        prompt = self._build_proofread_prompt(decision, price, snap)
        # 冷/热自适应超时：不能只看 `_warmed`（单向锁存，见 __init__ 注释）。
        # 只要距上次成功调用超过 HOT_WINDOW_SEC，就按冷启动给长超时。
        timeout = PROOFREAD_TIMEOUT if self._is_hot() else PROOFREAD_TIMEOUT_FIRST
        t0 = time.time()
        raw = self._generate(prompt, timeout)
        llm_issues: List[str] = []
        llm_sev = "none"
        if raw is None:
            # LLM 不可用/超时：结构审计仍生效，不伪装成「查过没事」。
            logger.debug("[校对员] LLM 部分未返回（结构审计独立生效）")
        else:
            obj = _extract_json(raw) or {}
            issues_raw = obj.get("issues")
            if isinstance(issues_raw, list):
                llm_issues = [str(x)[:200] for x in issues_raw if str(x).strip()]
            elif isinstance(issues_raw, str) and issues_raw.strip():
                llm_issues = [issues_raw[:200]]
            s = str(obj.get("severity", "")).strip().lower()
            if s not in ("none", "minor", "major"):
                # ★ 2026-08-08 修复：原来这里是 `"major" if llm_issues else "none"`，
                #   等于「8B 输出格式不规范 + 报了任何疑点 → 直接判最高危」。
                #   8B 的 JSON 格式稳定性本来就差，这条会把大量格式抖动升级成 major。
                #   在旧断路器逻辑下（major → 强制 HOLD）就是**格式错误直接砍单**。
                #   降级为 minor：疑点照样记录告警，但不参与任何拦单判定。
                s = "minor" if llm_issues else "none"
            llm_sev = s

        # 合并：issues 去重（代码与 LLM 可能都报 SL 方向，避免重复）
        issues = []
        for it in code_issues + llm_issues:
            if it not in issues:
                issues.append(it)
        # severity 取高者
        if code_sev == "major" or llm_sev == "major":
            sev = "major"
        elif code_sev == "minor" or llm_sev == "minor":
            sev = "minor"
        else:
            sev = "none"

        with self._lock:
            self._proofread_runs += 1
            if issues:
                self._proofread_issues += 1
        self._note(
            f"校对决策 {decision.get('decision')}：" +
            (f"发现 {len(issues)} 处疑点(sev={sev})" if issues else "核对通过")
        )

        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("qwen3_proofread", "output",
                    input_fields=decision,
                    output={"ok": not issues, "severity": sev, "issues": issues[:3]},
                    adopted=1, consumer="下单前闸门",
                    notes=f"sev={sev}")
        except Exception:
            pass

        return ProofreadResult(
            ok=not issues,
            issues=issues,
            severity=sev,
            # ★ 分来源上报：断路器只被允许读 code_severity（见 ProofreadResult 文档）。
            code_severity=code_sev,
            llm_severity=llm_sev,
            code_issues=list(code_issues),
            latency_ms=(time.time() - t0) * 1000.0,
            model=_model_name(),
            raw=str(raw)[:1000] if raw else "(LLM 未返回，仅结构审计)",
        )

    @staticmethod
    def _structural_audit(decision: dict, price) -> tuple:
        """代码侧确定性结构审计——纯算术，不依赖 LLM。

        返回 (issues: List[str], severity: str)。
        severity 规则：
          major —— SL 方向反 / TP 方向反 / 价格幻觉（这类一旦成交必亏或无效）
          minor —— 止损过近 / 盈亏比失衡（风险偏高但不致立即爆）
        """
        issues: List[str] = []
        sev = "none"

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        d = str(decision.get("decision") or "").upper()
        entry = _f(decision.get("entry_price"))
        sl = _f(decision.get("stop_loss"))
        tp = _f(decision.get("take_profit"))

        if d in ("BUY", "SELL"):
            # —— 止损方向（major）——
            if entry is not None and sl is not None:
                if d == "BUY" and not (sl < entry):
                    issues.append(f"止损方向错误：BUY 的止损 {sl} 必须低于入场 {entry}")
                    sev = "major"
                if d == "SELL" and not (sl > entry):
                    issues.append(f"止损方向错误：SELL 的止损 {sl} 必须高于入场 {entry}")
                    sev = "major"
                # —— 止损过近（minor）——
                if sev != "major":
                    dist = abs(entry - sl)
                    if 0 < dist < SL_MIN_DIST_USD:
                        issues.append(
                            f"止损过近：距入场仅 {dist:.2f} 美元（<{SL_MIN_DIST_USD}），易秒触"
                        )
                        sev = "minor"
            # —— 止盈方向（major）——
            if entry is not None and tp is not None:
                if d == "BUY" and not (tp > entry):
                    issues.append(f"止盈方向错误：BUY 的止盈 {tp} 必须高于入场 {entry}")
                    sev = "major"
                if d == "SELL" and not (tp < entry):
                    issues.append(f"止盈方向错误：SELL 的止盈 {tp} 必须低于入场 {entry}")
                    sev = "major"
                # —— 盈亏比失衡（minor，仅当方向都对才评）——
                elif sev != "major" and sl is not None:
                    denom = abs(entry - sl)
                    if denom > 0:
                        rr = abs(tp - entry) / denom
                        if 0 < rr < RR_MIN_RATIO:
                            issues.append(
                                f"盈亏比失衡：回报/风险={rr:.2f}（<{RR_MIN_RATIO}），盈利空间不足以覆盖风险"
                            )
                            sev = "minor"

        # —— 价格幻觉（major）：决策里的价格偏离真实盘口 > 5% ——
        if price is not None and price > 0:
            for label, val in (("入场", entry), ("止损", sl), ("止盈", tp)):
                if val is not None and abs(val - price) / price > PRICE_HALLUCINATION_RATIO:
                    issues.append(
                        f"价格幻觉：{label}价 {val} 偏离真实盘口 {price:.2f} 超过 "
                        f"{int(PRICE_HALLUCINATION_RATIO * 100)}%"
                    )
                    sev = "major"

        return issues, sev

    @staticmethod
    def _orderflow_line(md: dict) -> str:
        """从市场快照抽取一行紧凑订单流摘要（CVD 方向 + 买枯/卖压），供本地 8B 提准降噪。"""
        of = (md or {}).get("orderflow") if isinstance(md, dict) else None
        if not isinstance(of, dict) or not of.get("available"):
            return "订单流CVD：暂不可用"
        src = of.get("source")
        sub = of.get(src) if src else None
        is_real = bool(sub.get("is_real_cvd")) if isinstance(sub, dict) else False
        tag = "真CVD(Binance)" if is_real else ("代理(" + str(src or "MT5") + ")")
        reading = of.get("reading") or "中性"
        parts = ["订单流CVD[" + tag + "] 读=" + str(reading)]
        if of.get("buy_pressure_dry"):
            parts.append("买盘枯竭")
        if of.get("sell_pressure_high"):
            parts.append("卖压放大")
        return " · ".join(parts)

    # ---------- 特征工程（纯加法提准，2026-08-18） ----------
    @staticmethod
    def _f(v):
        """宽松转 float，失败返回 None。行情字段可能是字符串/None/dict。"""
        try:
            if v is None or isinstance(v, (dict, list, tuple)):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _mtf_lines(h4: dict, h1: dict, m15: dict, price) -> str:
        """把 H4/H1/M15 三周期指标排成**并列矩阵**，并显式标注共振/冲突。

        为什么要显式算共振而不是让模型自己看：8B 级模型的跨行比对能力很弱，
        把三行数字丢给它，它经常只读第一行就下结论。把"三周期是否同向"这个
        结论直接算好喂进去，模型就不必做它不擅长的比对，只需做它擅长的
        语义权衡。这是**给模型减负、不是替模型决策**——最终方向仍由模型输出。

        调研依据：多周期共振（HTF bias → LTF entry）是黄金/外汇方向判断
        公认最强的单一结构性证据；反过来，周期冲突时的默认动作应当是不交易
        （多源一致："weekly 与 daily 冲突则 no-trade"）。此处只把冲突事实
        告知模型，**不做硬拦截**——保留模型在冲突中依然看多/看空的自由，
        符合"提准非拦截"红线。
        """
        _f = LocalLLMService._f
        rows = []
        dirs = {}
        for label, tf in (("H4", h4), ("H1", h1), ("M15", m15)):
            if not isinstance(tf, dict) or not tf:
                rows.append(f"  {label}: 数据缺失")
                continue
            e20 = _f(tf.get("ema20"))
            e50 = _f(tf.get("ema50"))
            rsi = _f(tf.get("rsi"))
            atr = _f(tf.get("atr"))
            trend = tf.get("trend") or "N/A"
            # 该周期的结构方向：EMA 快慢关系（最稳、最少歧义的方向代理）
            d = "N/A"
            if e20 is not None and e50 is not None:
                d = "多" if e20 > e50 else ("空" if e20 < e50 else "平")
                dirs[label] = d
            rows.append(
                f"  {label}: 趋势={trend} EMA20={e20 if e20 is not None else 'N/A'}"
                f" EMA50={e50 if e50 is not None else 'N/A'} → 结构偏{d}"
                f" | RSI={rsi if rsi is not None else 'N/A'}"
                f" ATR={atr if atr is not None else 'N/A'}"
            )
        # 共振结论
        # ★ 措辞必须按**实际有数据的周期数**动态生成：H4 缺失时只有 2 个周期，
        #   却写"三周期共振"是在给模型喂假信息（模型会据此高估证据强度）。
        vals = [v for v in dirs.values() if v in ("多", "空")]
        if len(vals) >= 2 and len(set(vals)) == 1:
            _tfs = "/".join(k for k, v in dirs.items() if v in ("多", "空"))
            _strength = "最强结构证据" if len(vals) >= 3 else "较强结构证据(仅部分周期有数据)"
            verdict = f"★{len(vals)}个周期共振：{_tfs} 全部偏{vals[0]}（{_strength}）"
        elif len(vals) >= 2:
            verdict = (
                "⚠周期冲突："
                + "/".join(f"{k}偏{v}" for k, v in dirs.items())
                + "（高层与低层不一致，除非有极强的其它证据，倾向 HOLD）"
            )
        else:
            verdict = "周期共振：数据不足，无法判定"
        return "\n".join(rows) + "\n  " + verdict

    @staticmethod
    def _extension_line(h4: dict, h1: dict, m15: dict, price) -> str:
        """价格延伸度（Z = |price - EMA50| / ATR）+ RSI 极端标注。

        ★ 这是本次提准的**关键新特征**，用于区分两种外观完全相同、
        但结局相反的行情：
          (a) 健康趋势中的回踩 —— 价格贴着均线走，Z 小 → 顺势入场胜率高
          (b) 趋势末端的接飞刀 —— 价格远离均线，Z 大 + RSI 极端 → 均值回归概率骤升
        指标健康度（ADX/DI/趋势标签）在 (a)(b) 两种情形下**看起来一模一样**，
        这正是过去"趋势看着很健康却买在最高点"的根因。延伸度是能分辨二者的
        少数特征之一。

        阈值依据（黄金专用，非通用股票口径）：
          · Z ≥ 2.5 —— 价格距基准均线 2.5 个 ATR，属统计罕见延伸，回归概率显著升高
          · Z ≥ 1.5 —— CTA 界常用的"趋势拥挤/衰竭"预警线
          · RSI > 72 / < 28 —— 黄金波动大，通用 70/30 过于频繁触发，实测 72/28 更贴合
        这些只作为**证据行文**喂给模型，不设任何硬门槛、不做降权、不拦单
        （红线：提准非拦截）。模型完全可以在 Z=3 时依然选择做多。
        """
        _f = LocalLLMService._f
        p = _f(price)
        if p is None:
            return "价格延伸度：当前价不可用，无法计算"
        parts = []
        flags = []
        for label, tf in (("H4", h4), ("H1", h1), ("M15", m15)):
            if not isinstance(tf, dict) or not tf:
                continue
            e50 = _f(tf.get("ema50"))
            atr = _f(tf.get("atr"))
            rsi = _f(tf.get("rsi"))
            if e50 is not None and atr and atr > 0:
                z = (p - e50) / atr
                side = "上方" if z >= 0 else "下方"
                parts.append(f"{label} Z={z:+.2f}({side})")
                if abs(z) >= 2.5:
                    flags.append(f"{label}延伸极端(|Z|≥2.5，统计罕见，均值回归概率高)")
                elif abs(z) >= 1.5:
                    flags.append(f"{label}延伸偏大(|Z|≥1.5，趋势拥挤预警)")
            if rsi is not None:
                if rsi > 72:
                    flags.append(f"{label} RSI={rsi:.0f} 超买(>72)")
                elif rsi < 28:
                    flags.append(f"{label} RSI={rsi:.0f} 超卖(<28)")
        if not parts:
            return "价格延伸度：EMA50/ATR 数据不足，无法计算"
        line = "价格延伸度(Z=距EMA50的ATR倍数)：" + " · ".join(parts)
        if flags:
            line += "\n  ⚠延伸警示：" + "；".join(flags[:4])
        else:
            line += "\n  延伸警示：无（价格贴近均线，属健康区间）"
        return line

    def _build_proofread_prompt(self, decision: dict, price, snap: dict) -> str:
        """校对提示词。刻意**不给市场观点**，只给「语义矛盾」这一条 LLM 擅长的检查——
        结构性算术（SL/TP 方向、止损过近、盈亏比、价格幻觉）已在代码侧 `_structural_audit`
        确定性完成，不让 8B 做它不擅长的算术。小模型在「有明确标准的对照检查」上准，
        在「开放判断」上接近随机。"""
        d = json.dumps(
            {
                "decision": decision.get("decision"),
                "confidence": decision.get("confidence"),
                "entry_price": decision.get("entry_price"),
                "stop_loss": decision.get("stop_loss"),
                "take_profit": decision.get("take_profit"),
                "reason": str(decision.get("reason", ""))[:500],
            },
            ensure_ascii=False,
        )
        return (
            "你是交易决策的**校对员**，不是分析师。禁止给出你自己的交易观点或方向建议。\n"
            "只做你唯一擅长的一项检查——**理由文本与交易方向是否自相矛盾**：\n"
            "  · 若 decision=BUY，理由却主张「下跌/空头/看跌/走弱」，记为问题；\n"
            "  · 若 decision=SELL，理由却主张「上涨/多头/看跌...（应为看涨）/走强」，记为问题；\n"
            "  · 若理由与方向一致或理由为空，不记录。\n"
            "（止损/止盈方向、价格是否合理等结构性检查已由代码完成，你无需重复。）\n\n"
            f"订单流上下文：{LocalLLMService._orderflow_line(snap)}\n\n"
            f"待校对决策：\n{d}\n\n"
            '只输出 JSON，格式：{"issues": ["问题1"], "severity": "none|minor|major"}\n'
            '没有问题就输出：{"issues": [], "severity": "none"}'
        )

    def warm(self) -> bool:
        """预热：触发一次极轻量生成，把 ~5GB 权重加载进显存，避免首笔校对超时。

        返回是否成功。失败一律返回 False（不抛），由调用方决定是否重试。
        """
        if not self.available():
            return False
        try:
            t0 = time.time()
            raw = self._generate(
                "/no_think\n请只回复 JSON：{\"ok\":true}", PROOFREAD_TIMEOUT_FIRST
            )
            ok = raw is not None
            if ok:
                with self._lock:
                    self._warmed = True
                logger.info(f"[校对员] 预热完成（{ (time.time()-t0)*1000:.0f}ms）")
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[校对员] 预热失败: {e}")
            return False

    # ---------- 身份 B：副驾 ----------
    def copilot(self, market_data: Optional[dict]) -> Optional[CopilotVote]:
        """L2 副驾方向票。返回 None = 不可用或未达门槛（调用方按 HOLD 处理）。

        ⚠ 调用方**必须**在拿到票后再叠 Chronos 同向校验，本方法只负责
        「本地模型怎么看」，不负责「能不能据此下单」。职责分离，防止
        以后有人图省事直接拿这张票去开仓。
        """
        if not self.available():
            return None
        md = market_data or {}
        prompt = self._build_copilot_prompt(md)
        t0 = time.time()
        raw = self._generate(prompt, COPILOT_TIMEOUT)
        if raw is None:
            return None
        obj = _extract_json(raw)
        if not obj:
            return None

        d0 = _clean_decision(obj.get("decision"))
        c0 = _clean_conf(obj.get("confidence"))
        r0 = str(obj.get("reason", ""))[:300]

        votes = [d0]
        confs = [c0]
        reasons = [r0]
        consensus = True

        # ---- 自适应自一致性：只在「边界情形」追加采样（详见 COPILOT_SC_* 注释）----
        # 快路径判据：非边界置信度 或 首票就是 HOLD。
        #   · 首票 HOLD 不追加——HOLD 是安全默认值，多花 40s 把 HOLD 投成 BUY 属于
        #     "为了给动作而给动作"，违背"证据不足必须 HOLD"的既有约束。
        #   · 置信度在 [LOW, HIGH) 之外不追加——见常量注释的成本收益分析。
        _boundary = (
            copilot_sc_enabled()
            and d0 in ("BUY", "SELL")
            and COPILOT_SC_LOW <= c0 < COPILOT_SC_HIGH
        )
        if _boundary:
            for _ in range(COPILOT_SC_EXTRA):
                _raw = self._generate(prompt, COPILOT_SC_TIMEOUT, temperature=COPILOT_SC_TEMP)
                if _raw is None:
                    break  # 超时/失败就用已有票，不阻塞决策链
                _obj = _extract_json(_raw)
                if not _obj:
                    continue
                votes.append(_clean_decision(_obj.get("decision")))
                confs.append(_clean_conf(_obj.get("confidence")))
                reasons.append(str(_obj.get("reason", ""))[:300])

        if len(votes) > 1:
            # 多数投票。平票/三票全异 → 无共识 → HOLD（不确定就别开仓，符合既有约束）
            tally: dict = {}
            for v in votes:
                tally[v] = tally.get(v, 0) + 1
            top = max(tally.values())
            winners = [k for k, n in tally.items() if n == top]
            if len(winners) == 1:
                final_d = winners[0]
                # 置信度只取「投给获胜方向」那几票的均值——把反对票的置信度
                # 混进来平均是错的（它们支持的是别的方向）。
                agree = [c for v, c in zip(votes, confs) if v == final_d]
                final_c = sum(agree) / len(agree) if agree else c0
                # 分歧惩罚：3 票里只有 2 票同向 → 该判断不够稳，置信度打折。
                # 这不是"拦截"（不改方向、不加门槛），而是让下游的手数引擎
                # 与 min_confidence 门槛拿到**更诚实**的置信度。
                if top < len(votes):
                    final_c *= 0.90
                final_r = next((r for v, r in zip(votes, reasons) if v == final_d), r0)
            else:
                final_d, final_c, consensus = "HOLD", 0.0, False
                final_r = f"自一致性无共识({'/'.join(votes)}) → 保守 HOLD"
        else:
            final_d, final_c, final_r = d0, c0, r0

        vote = CopilotVote(
            decision=final_d,
            confidence=round(final_c, 4),
            reason=final_r,
            latency_ms=(time.time() - t0) * 1000.0,
            model=_model_name(),
            samples=len(votes),
            sample_votes=tuple(votes),
            consensus=consensus,
        )
        with self._lock:
            self._copilot_runs += 1
        _sc_tag = (
            f"·自一致性{len(votes)}票[{'/'.join(votes)}]" if len(votes) > 1 else "·单票"
        )
        self._note(f"降级副驾出票 {vote.decision}（置信 {vote.confidence:.2f}）{_sc_tag}")
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("qwen3_copilot", "output",
                    input_fields=md,
                    output={"decision": vote.decision, "confidence": vote.confidence,
                            "reason": vote.reason, "samples": vote.samples,
                            "sample_votes": list(vote.sample_votes),
                            "consensus": vote.consensus},
                    adopted=1, consumer="copilot_gate",
                    notes=f"samples={vote.samples}")
        except Exception:
            pass
        return vote

    # ---------- 仓位管理管仓（Position Manager 增强层） ----------
    def position_manage(self, pm_context: dict) -> Optional[PositionManageVote]:
        """本地 8B 仓位管理判断。返回 None = 不可用/未达标（调用方按 HOLD 处理）。

        ⚠ 本方法只负责「本地模型怎么看这笔持仓」，不负责「能不能据此平仓」。
        真正的平仓动作由 trade_executor 在【确定性 M5 反转门槛】之上叠加执行，
        防止本地 8B 近似随机的方向判断被直接拿去砍仓。职责分离。
        """
        if not self.available():
            return None
        ctx = pm_context or {}
        if not ctx:
            return None
        prompt = self._build_position_manage_prompt(ctx)
        t0 = time.time()
        raw = self._generate(prompt, POSITION_MANAGE_TIMEOUT)
        if raw is None:
            return None
        obj = _extract_json(raw)
        if not obj:
            return None
        _act = str(obj.get("action", "HOLD")).strip().upper()
        if _act not in ("HOLD", "TRAIL_TIGHTEN", "PARTIAL_EXIT", "FULL_MIN_LOSS"):
            _act = "HOLD"
        vote = PositionManageVote(
            action=_act,
            close_pct=_clean_conf(obj.get("close_pct", 0)) if _act == "PARTIAL_EXIT" else 0.0,
            new_sl=float(obj.get("new_sl") or 0),
            confidence=_clean_conf(obj.get("confidence")),
            reason=str(obj.get("reason", ""))[:300],
            latency_ms=(time.time() - t0) * 1000.0,
            model=_model_name(),
        )
        with self._lock:
            self._position_manage_runs += 1
        self._note(f"仓位管理管仓 {vote.action}（置信 {vote.confidence:.2f}）")
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("qwen3_position_manage", "output",
                    input_fields=ctx,
                    output={"action": vote.action, "close_pct": vote.close_pct,
                            "new_sl": vote.new_sl, "confidence": vote.confidence,
                            "reason": vote.reason},
                    adopted=1, consumer="position_manager")
        except Exception:
            pass
        return vote

    # ---------- L2 反向平仓本地化（2026-08-19 定稿P0-2） ----------
    def evaluate_exits_local(self, payload: list, market_context: dict) -> dict:
        """本地 8B 出场评估（替代云端 DeepSeek evaluate_exits）。

        云端弃用后 ai_exit 原绑 deepseek_client 持续 error → L2 AI 反向平仓静默失效。
        本方法提供同构输出：{"decisions":[{ticket,action,close_pct,new_sl,confidence,reason}], "error":None}，
        供 ai_exit._bg_evaluate 无缝切换；action ∈ hold|partial_close|full_close|reverse_signal。

        返回 None 或 error dict 时，ai_exit 整笔回退 smart_exit 规则引擎（出场永不卡死）。
        """
        if not self.available():
            return {"decisions": [], "error": "local_llm_unavailable"}
        if not payload:
            return {"decisions": [], "error": None}
        prompt = self._build_exit_local_prompt(payload, market_context or {})
        t0 = time.time()
        raw = self._generate(prompt, POSITION_MANAGE_TIMEOUT)
        if raw is None:
            return {"decisions": [], "error": "timeout_or_empty"}
        obj = _extract_json(raw)
        if not obj:
            return {"decisions": [], "error": "parse_failed"}
        decisions = obj.get("decisions") or obj.get("decisions", [])
        if not isinstance(decisions, list) or not decisions:
            # 兼容单笔对象返回
            if isinstance(obj, dict) and obj.get("ticket"):
                decisions = [obj]
        out = []
        for d in decisions:
            if not isinstance(d, dict):
                continue
            t = str(d.get("ticket", "")).strip()
            if not t:
                continue
            act = str(d.get("action", "hold")).strip().upper()
            if act not in ("HOLD", "PARTIAL_CLOSE", "FULL_CLOSE", "REVERSE_SIGNAL"):
                act = "HOLD"
            out.append({
                "ticket": t,
                "action": act.lower(),
                "close_pct": _clean_conf(d.get("close_pct", 0)) if act == "PARTIAL_CLOSE" else 0.0,
                "new_sl": float(d.get("new_sl") or 0),
                "confidence": _clean_conf(d.get("confidence")),
                "reason": str(d.get("reason", ""))[:200],
            })
        self._note(f"L2本地反向平仓评估 {len(out)} 笔（{(time.time()-t0)*1000:.0f}ms）")
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("qwen3_exit_local", "output",
                    input_fields={"n_pos": len(payload)},
                    output={"n_decisions": len(out),
                            "actions": [x["action"] for x in out],
                            "latency_ms": round((time.time() - t0) * 1000.0, 1)},
                    adopted=1, consumer="ai_exit")
        except Exception:
            pass
        return {"decisions": out, "error": None}

    @staticmethod
    def _build_exit_local_prompt(payload: list, market_context: dict) -> str:
        """L2 反向平仓 prompt：持仓明细 + 行情背景 + 出场意图（含方向反转信号）。"""
        lines = []
        for p in payload:
            lines.append(
                f"#{p.get('ticket')} {p.get('type')} 开{p.get('open_price')} 现{p.get('current_price')} "
                f"盈亏${p.get('profit')} MFE${p.get('mfe')} MAE${p.get('mae')} "
                f"位移{p.get('move_atr')}×ATR 持{p.get('holding_minutes')}分钟 "
                f"SL={p.get('sl')} TP={p.get('tp')} v={p.get('volume')}"
            )
        _mc = market_context or {}
        _reg = _mc.get("regime") or "unknown"
        _atr = _mc.get("h1_atr") or "N/A"
        _ext = _mc.get("price_extension_z")
        _trend = _mc.get("trend_h1") or "N/A"
        _ma20 = _mc.get("ma20") or "N/A"
        _ma50 = _mc.get("ma50") or "N/A"
        _lessons = _mc.get("lessons") or []
        _lessons_str = "；".join(str(x)[:120] for x in _lessons[:3]) or "无"
        _phil = str(_mc.get("exit_philosophy") or "")[:200]
        return (
            "你是黄金(XAUUSD)持仓出场决策代理。基于持仓明细与市场背景，对每笔持仓输出出场意图。\n"
            "你的目标：盈利单让利润奔跑但结构转弱要收；亏损单若开错方向且反转证据充分则止损离场；"
            "方向反转信号(reverse_signal)只在证据充分时给出（会经系统连续2轮确认才平仓）。\n"
            "证据不足一律 hold，不要乱砍。\n\n"
            f"【持仓明细】\n" + "\n".join(lines) + "\n\n"
            f"【市场背景】Regime: {_reg} | H1 ATR: {_atr} | H1趋势: {_trend} | MA20: {_ma20} MA50: {_ma50}\n"
            f"价格延伸度Z(正=高位过度延伸, 负=低位超卖): {_ext}\n"
            f"历史教训: {_lessons_str}\n"
            f"出场哲学: {_phil}\n\n"
            "只输出 JSON，不要解释：\n"
            '{"decisions":[{"ticket":"持仓ticket","action":"hold|partial_close|full_close|reverse_signal",'
            '"close_pct":0.0,"new_sl":0.0,"confidence":0.0,"reason":"一句话"}]}'
        )


        _dir = ctx.get("direction", "N/A")
        _op = ctx.get("open_price", "N/A")
        _cp = ctx.get("current_price", "N/A")
        _pnl = ctx.get("floating_pnl", "N/A")
        _hold = ctx.get("hold_sec", "N/A")
        _atr = ctx.get("atr", "N/A")
        _hsl = ctx.get("hard_sl", "N/A")
        _hsl_dist = ctx.get("hard_sl_dist", "N/A")
        _peak = ctx.get("profit_peak", "N/A")
        _m5 = ctx.get("m5", {}) or {}
        _m5_rsi = _m5.get("rsi", "N/A")
        _m5_ema = _m5.get("ema20", "N/A")
        _m5_trend = _m5.get("trend", "N/A")
        _m5_closes = _m5.get("last_closes", []) or []
        _m5_str = ",".join(f"{c}" for c in _m5_closes[-6:]) if _m5_closes else "N/A"
        _h1 = ctx.get("h1_trend", "N/A")
        _regime = ctx.get("regime", "N/A")

        return (
            "你是黄金(XAUUSD)持仓管家。一笔已开仓部位交给你做实时管理决策。\n"
            "你的目标：①利润与行情成正比，行情走不动就获利了结，不在一个仓位耗着；"
            "②若开错方向、短时间不会朝盈利方向走，找最小亏损位置平仓。\n"
            "证据不足时务必输出 HOLD，不要为了给动作而乱砍。\n\n"
            f"持仓方向: {_dir}\n"
            f"开仓价: {_op}  当前价: {_cp}\n"
            f"浮动盈亏($): {_pnl}  持仓时长(s): {_hold}\n"
            f"ATR: {_atr}  硬止损价: {_hsl}  距硬止损点数: {_hsl_dist}\n"
            f"近期利润峰值($): {_peak}\n"
            f"M5 RSI: {_m5_rsi}  M5 EMA20: {_m5_ema}  M5 趋势: {_m5_trend}\n"
            f"M5 近6根收盘: {_m5_str}\n"
            f"H1 趋势: {_h1}  波动体制(Regime): {_regime}\n\n"
            "只输出 JSON：\n"
            '{"action":"HOLD|TRAIL_TIGHTEN|PARTIAL_EXIT|FULL_MIN_LOSS",'
            '"close_pct":0.0~1.0,"new_sl":0.0,"confidence":0.0~1.0,"reason":"一句话"}'
        )

    @staticmethod
    def _build_copilot_prompt(md: dict) -> str:
        def g(*keys, default="N/A"):
            for k in keys:
                v = md.get(k)
                if v not in (None, ""):
                    return v
            return default

        # ★ 2026-08-13 修复"副驾瞎子"根因：技术指标(atr/rsi/ema/trend)实际嵌在
        #   md["timeframes"][tf] 内部（market_analyzer._build_from_raw 构造），并非
        #   market_data 顶层键。原代码用 md.get('atr') 顶层取 → 永远 "N/A"。
        # ★★ 2026-08-18 提准重构（纯加法，不砍任何信号）：
        #   旧实现是 `_h1.get(x) or _m15.get(x) or _m5.get(x)` 的**兜底链**——
        #   意味着副驾**只看到一个周期**（H1 有值就永远看不到 M15/M5），更看不到 H4。
        #   而视觉脑看的是 H4/M15/M5。两个脑的视野几乎不重叠，谁也没有"跨周期共振"
        #   这个最强的方向证据。现改为 **H4/H1/M15 三周期并列**呈现：
        #   合上视觉脑的 H4/M15/M5，系统整体覆盖 H4→H1→M15→M5 四级。
        #   代价：prompt 从 ~250 token 涨到 ~600 token，**num_ctx 4096 完全容得下，
        #   显存增量 0MB**（KV cache 按 num_ctx 预分配，与实际 token 数无关）。
        _tf = md.get("timeframes", {}) or {}
        _h4 = _tf.get("H4", {}) or {}
        _h1 = _tf.get("H1", {}) or {}
        _m15 = _tf.get("M15", {}) or {}
        _m5 = _tf.get("M5", {}) or {}
        # 兜底链仅用于「单周期字段缺失」时的补位，不再用于跨周期替代
        _atr = _h1.get("atr") or _m15.get("atr") or _m5.get("atr") or "N/A"
        _rsi = _h1.get("rsi") or _m15.get("rsi") or _m5.get("rsi") or "N/A"
        _trend = _h1.get("trend") or _m15.get("trend") or _m5.get("trend") or "N/A"
        _ema_fast = _h1.get("ema20") or _m15.get("ema20") or "N/A"
        _ema_slow = _h1.get("ema50") or _m15.get("ema50") or "N/A"

        # current_price 是 dict（含 last/ask/bid），必须取标量而非整个对象
        _cp = g("current_price", "price", "close")
        if isinstance(_cp, dict):
            _cp = _cp.get("last") or _cp.get("ask") or _cp.get("bid") or "N/A"

        _mtf_block = LocalLLMService._mtf_lines(_h4, _h1, _m15, _cp)
        _ext_block = LocalLLMService._extension_line(_h4, _h1, _m15, _cp)

        # SMC / Regime 是顶层键（与 timeframes 不同），直接取
        _smc = md.get("smc_features", {}) or {}
        _smc_str = f"{_smc.get('global_bias', '')} {str(_smc.get('per_tf', ''))[:200]}".strip()
        _regime = md.get("regime", {}) or {}
        if isinstance(_regime, dict):
            _regime_str = _regime.get("label_zh") or _regime.get("regime") or g("regime")
        else:
            _regime_str = _regime

        # ★ 2026-08-18 结构化重构。依据：交易类 LLM 提示词工程的多源共识——
        #   ① 角色/指令/约束/输出格式四段式（RICE）显著降低幻觉、提升 JSON 稳定性；
        #   ② 强制"由高周期到低周期"的**固定推理顺序**，可防止模型挑对自己结论
        #      有利的那个周期下判断（timeframe cherry-picking，实测的头号失效模式）；
        #   ③ 强制给出"什么情况会证明我错"（反面检验），可压制确认偏误；
        #   ④ 结论字段放最后输出，让模型先完成推理再落方向。
        #   这些全部是**信息与顺序层面的加法**，不新增任何门槛/过滤/降权。
        return (
            "你是黄金(XAUUSD)交易副驾，具备机构订单流(SMC/ICT)与多周期结构分析能力。\n"
            "云端主模型当前不可用，由你给出方向判断。\n\n"
            "【硬性要求】\n"
            "1) 必须严格按「H4 → H1 → M15」由高到低的顺序推理，禁止只看单一周期就下结论。\n"
            "2) 高周期(H4)定方向基调，低周期(M15)只用于确认时机，不得用低周期推翻高周期。\n"
            "3) 三周期冲突、或证据互相矛盾时，输出 HOLD。不要为了给答案而猜方向。\n"
            "4) 价格已极端延伸(|Z|≥2.5)且 RSI 极端时，须警惕这是趋势末端而非顺势机会。\n"
            "5) confidence 必须反映真实把握：共振且延伸健康才给 0.7 以上；"
            "有任何冲突或延伸警示，给 0.5 上下。\n\n"
            f"【当前价】{_cp}\n"
            f"【波动体制(Regime)】{_regime_str}\n\n"
            "【多周期结构矩阵】\n"
            f"{_mtf_block}\n\n"
            # _ext_block 是多行块且自带标题，不能再用【】包——否则右括号会跑到
            # 第二行末尾（实测形如 "...健康区间）】"），既难读又干扰模型解析结构。
            f"{_ext_block}\n\n"
            f"【市场结构(SMC)】{_smc_str[:300]}\n"
            f"【订单流】{LocalLLMService._orderflow_line(md)}\n"
            f"【H1 补充】ATR={_atr} RSI={_rsi} EMA20/50={_ema_fast}/{_ema_slow} 趋势={_trend}\n\n"
            "只输出严格 JSON（不要解释、不要 markdown）：\n"
            '{"h4_bias":"多|空|中性","conflict":"共振|冲突|数据不足",'
            '"invalidation":"一句话说明什么情况会证明这个判断是错的",'
            '"decision":"BUY|SELL|HOLD","confidence":0.0~1.0,"reason":"一句话"}'
        )

    # ---------- 状态 ----------
    def status(self) -> dict:
        with self._lock:
            samples = sorted(self._latency_samples)
            n = len(samples)

            def _pct(p: float):
                if not n:
                    return None
                # 最近邻分位：样本量小（≤50）时线性插值意义不大，反而容易误读。
                idx = min(n - 1, max(0, int(round(p * (n - 1)))))
                return round(samples[idx], 1)

            return {
                "enabled": local_llm_enabled(),
                "available": self._probe_ok,
                "reason": self._probe_reason,
                "model": _model_name(),
                "base_url": _base_url(),
                "installed_models": self._models[:10],
                "warmed": self._warmed,
                "calls_ok": self._calls_ok,
                "calls_fail": self._calls_fail,
                "last_error": self._last_error,
                "last_latency_ms": round(self._last_latency_ms, 1),
                "latency_p50_ms": _pct(0.50),
                "latency_p95_ms": _pct(0.95),
                "num_ctx": resolve_num_ctx(),
                "num_ctx_detail": num_ctx_detail(),
                "keep_alive": KEEP_ALIVE,
                # ── 分职责工作量：让运维一眼看出这个模型在干活还是在摸鱼 ──
                "roles": {
                    "proofreader": {
                        "runs": self._proofread_runs,
                        "issues_found": self._proofread_issues,
                        "active": True,          # 校对员是 L0 常态职责
                    },
                    "copilot": {
                        "runs": self._copilot_runs,
                        "allowed": self._copilot_allowed,
                        "active": True,          # 常态确认型副驾（2026-08-14 升级）：进融合第五票
                        "note": "常态确认型副驾：仅当与有效时序方向同向+过阈+降权才计入融合投票（加法提准非拦截）",
                    },
                    "position_manager": {
                        "runs": self._position_manage_runs,
                        # ★ 2026-08-17 修复：硬编码 False 与 config 默认 True 矛盾 → 读真实开关，
                        #   否则前端/审计误判「持仓管家未启用」（实际在岗，空仓时 runs=0 正常）
                        "active": bool(getattr(
                            __import__("app.config", fromlist=["settings"]).settings,
                            "POSITION_MANAGER_LOCAL_ENABLED", True)),
                        "note": "持仓管家增强层：仅给「追踪锁利/部分落袋/最小亏损平」建议，"
                                "确定性停滞平仓与 M5 反转门槛由 trade_executor 把关",
                    },
                },
                "last_activity": self._last_activity,
                "last_activity_ago_s": (
                    round(time.time() - self._last_activity_ts, 1)
                    if self._last_activity_ts else None
                ),
            }

    def reset(self) -> None:
        """测试用复位。"""
        with self._lock:
            self._probe_ts = 0.0
            self._probe_ok = False
            self._probe_reason = "未探测"
            self._models = []
            self._warmed = False
            self._calls_ok = self._calls_fail = 0
            self._last_error = ""
            self._last_latency_ms = 0.0
            self._proofread_runs = self._proofread_issues = 0
            self._copilot_runs = self._copilot_allowed = 0
            self._position_manage_runs = 0
            self._last_activity = ""
            self._last_activity_ts = 0.0
            self._latency_samples = []


# ============================================================
#  单例 + 便捷入口
# ============================================================
_SERVICE: Optional[LocalLLMService] = None
_SERVICE_LOCK = threading.Lock()


def get_local_llm() -> LocalLLMService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = LocalLLMService()
    return _SERVICE


def is_available() -> bool:
    try:
        return get_local_llm().available()
    except Exception:
        return False


def proofread(decision: dict, market_snapshot: Optional[dict] = None):
    try:
        return get_local_llm().proofread(decision, market_snapshot)
    except Exception:
        return None


def copilot(market_data: Optional[dict]):
    try:
        return get_local_llm().copilot(market_data)
    except Exception:
        return None


def status_dict() -> dict:
    try:
        return get_local_llm().status()
    except Exception as e:  # pragma: no cover
        return {"available": False, "error": str(e)[:200]}


def reset_local_llm() -> None:
    get_local_llm().reset()


# ============================================================
#  副驾放行闸门（把「三道锁」固化成一个函数，杜绝调用方漏锁）
# ============================================================
def copilot_gate(
    vote: Optional[CopilotVote],
    chronos_dir: Optional[str],
    min_confidence: float = COPILOT_MIN_CONFIDENCE,
) -> dict:
    """判断本地副驾的票能否放行开仓。

    三道锁全过才 allow=True：
      ① 有票且方向非 HOLD
      ② 置信度 ≥ min_confidence
      ③ Chronos 方向同向（数值模型背书；NEUTRAL/None 都算不同向）

    返回 {"allow": bool, "decision": str, "confidence": float, "reason": str}
    """
    if vote is None:
        return {"allow": False, "decision": "HOLD", "confidence": 0.0,
                "reason": "本地副驾不可用"}
    if vote.decision not in ("BUY", "SELL"):
        return {"allow": False, "decision": "HOLD", "confidence": vote.confidence,
                "reason": "副驾给出 HOLD"}
    if vote.confidence < min_confidence:
        return {"allow": False, "decision": "HOLD", "confidence": vote.confidence,
                "reason": f"副驾置信 {vote.confidence:.2f} < 门槛 {min_confidence:.2f}"}

    cd = str(chronos_dir or "").strip().upper()
    if cd not in ("BUY", "SELL", "UP", "DOWN", "LONG", "SHORT"):
        return {"allow": False, "decision": "HOLD", "confidence": vote.confidence,
                "reason": f"Chronos 无明确方向({chronos_dir}) → 不放行"}
    cd_norm = "BUY" if cd in ("BUY", "UP", "LONG") else "SELL"
    if cd_norm != vote.decision:
        return {"allow": False, "decision": "HOLD", "confidence": vote.confidence,
                "reason": f"副驾{vote.decision} 与 Chronos{cd_norm} 相悖 → 不放行"}

    # 三道锁全过。统计放行数供系统管理页展示（失败静默，统计不能influence决策）。
    try:
        svc = get_local_llm()
        with svc._lock:  # noqa: SLF001 — 同模块内访问自身单例，非跨模块私有穿透
            svc._copilot_allowed += 1  # noqa: SLF001
    except Exception:
        pass

    return {
        "allow": True,
        "decision": vote.decision,
        "confidence": vote.confidence,
        "reason": f"副驾{vote.decision}({vote.confidence:.2f}) + Chronos 同向 → 放行(手数40%)",
    }
