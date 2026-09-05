"""
XAU/USD万象Ai自动量化交易系统 — Meta-Agent 动态加权裁决器
基于历史准确率动态调整 DeepSeek V4 和 混元 Hy3 的权重
"""
from dataclasses import dataclass, field
from typing import Optional
import json
import os
import time as _time
import threading
import atexit
from loguru import logger
from app.config import settings
from app.core.decision_gates import consensus_dir_of, apply_contrarian_gate
from app.services.cloud_switch import effective_cloud_enabled

# ★ M3a：MetaAgent 可学习状态持久化文件（重启恢复，避免"失忆"）
# 路径：backend/meta_agent_state.json（与 daily_baseline.json 同根目录）
_META_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "meta_agent_state.json",
)
# 持久化落盘最小间隔（秒）：feedback 每笔都调用，限频避免频繁 IO
_META_SAVE_INTERVAL = 30.0
# ModelPerformance 需持久化的字段
_PERF_FIELDS = ("total_signals", "correct_signals", "total_profit",
                "avg_confidence", "recent_accuracy", "weight")

# ★ 2026-08-18 打破 HY 恒 HOLD 死锁：追踪混元连续 HOLD(无方向输出)轮数。
#   混元权重封顶 0.65 且 HOLD 永远不被罚 → recent_accuracy 维持高位 → 权重锁 0.65 →
#   每单落入 R2(一方向一观望) 高门槛 + 亚盘乘子双重压 → 永远不开单。
#   连续超 HY_HOLD_DECAY_ROUNDS 轮 → get_weights 强制混元封底、DS 封顶，让方向模型主导。
_HY_HOLD_STREAK = {"n": 0}


@dataclass
class ModelPerformance:
    """模型历史表现"""
    total_signals: int = 0
    correct_signals: int = 0
    total_profit: float = 0.0
    avg_confidence: float = 0.0
    recent_accuracy: float = 0.5  # 最近N次准确率
    weight: float = 0.5  # 当前权重

    @property
    def accuracy(self) -> float:
        if self.total_signals == 0:
            return 0.5
        return self.correct_signals / self.total_signals

    def update(self, was_correct: bool, profit: float, confidence: float, add_profit: bool = True):
        self.total_signals += 1
        if was_correct:
            self.correct_signals += 1
        # ★ 2026-08-06 修复 P0「利润重复累加」：total_profit 默认累加，但 feedback 对同一笔
        #   平仓会分别 update DS 和 HY 两次，若都累加则总利润被双计 → 统计失真。
        #   故 feedback 调用时对「第二模型」传 add_profit=False，确保 total_profit 每笔只计一次。
        if add_profit:
            self.total_profit += profit
        # 指数移动平均更新置信度
        self.avg_confidence = self.avg_confidence * 0.9 + confidence * 0.1
        # 近期准确率（EMA）
        self.recent_accuracy = self.recent_accuracy * 0.8 + (1.0 if was_correct else 0.0) * 0.2


@dataclass
class DebateDecision:
    """辩论最终决策"""
    decision: str  # BUY / SELL / HOLD
    confidence: float
    deepseek_weight: float
    hunyuan_weight: float
    deepseek_vote: str
    hunyuan_vote: str
    reasoning_summary: str
    risk_level: str  # low / medium / high / extreme
    # ★ 2026-08-13 审计P1-1：真实双脑置信度（区别于权重），供下游按置信度分桶算胜率/PF，
    #   不再错用权重替代（此前落库 deepseek_confidence 直接填 deepseek_weight，污染 PF 度量）。
    #   必须放在所有无默认值字段之后（dataclass 规则）。
    deepseek_confidence: float = None
    hunyuan_confidence: float = None
    # 置信校准层输出（raw meta_agent_confidence → 历史观测命中率）；None=未校准/透传
    # 必须放在所有无默认值字段之后（dataclass 规则）。
    calibrated_confidence: float = None
    plain_summary: str = ""  # 人话版解读，给普通用户秒懂
    # ★ 2026-08-11 本地副驾补位标记：DS 单云失败时由 Qwen3-8B 顶票。
    #   deepseek_vote 仍记录方向（裁决照常），但 feedback 统计必须跳过 DS 准确率
    #   ——那是本地 8B 的票，记到 DS 名下会污染「云端双脑权重」。
    deepseek_local_fallback: bool = False
    consensus: str = ""  # strong / moderate / disagreement
    # ── v4 Meta 质量陪审团（本地时序模型制衡语义大脑）──
    quality_regime: str = ""        # HIGH(≥0.7)/MID(0.5~0.7)/LOW(0.35~0.5)/VERY_LOW(<0.35)，空=未评估
    chronos_tp_ceiling: float = None  # Chronos P90 末价动态止盈天花板（BUY 盈利目标，仅 HIGH/MID 生效）
    chronos_p10: float = None         # Chronos P10 末价（预测下界）：SELL 盈利目标（价格跌至此=吃满）
    # ── Phase 1 溯源链补全（2026-08-07）──
    #   Chronos 早就是加权第三票，共振/权重/Q/分位也都算了，却全留在
    #   adjudicate 的局部变量里没交出来。后果实打实：
    #     · 执行器 getattr(ai,"chronos_agree",False) 恒 False
    #       → 三模型共振豁免永不生效，非强趋势一律 +0.03 门槛（在砍交易笔数）
    #     · 前端只能拿正则从 reasoning 长文本里啃 Chronos 票和 Q 分
    #   getattr 默认值让这类"值算了没交"的断链不报错、静默降级，故补成显式字段。
    chronos_agree: bool = False       # DS/HY/Chronos 三票同向（最高 conviction，享体制门豁免）
    chronos_vote: str = "HOLD"        # 本地时序方向票，走 _normalize_decision 三态制
    # 权重 0 = Chronos 未参与加权。前端必须靠它区分「Chronos 建议观望」
    # 与「Chronos 服务没跑起来」——两者票面都是 HOLD，但对用户意义完全不同。
    chronos_weight: float = 0.0
    q_score: float = None             # Meta 质量陪审团综合质量分 Q
    chronos_p50: float = None         # Chronos P50 末价（中位预测），前端画 P10-P50-P90 曲线用
    # ── v5 AI 自主仓位管理（2026-08-07）：让 AI 在建仓端也有发言权，而非纯机械手数 ──
    #   背景：此前手数=固定风险比例公式，AI 只能定方向+置信度，无法表达"缩手/加仓/用多大风险"；
    #   仓位安全全靠机械风控兜底，违背"持久仓位安全要靠 AI 自主管理"。现把仓位意图交还 AI。
    position_intent: str = "open"     # open(新开)/add(金字塔加仓)/reduce(主动缩手)
    target_risk_pct: float = None     # AI 自主决定的单笔风险占比(%)；None=沿用策略固定 max_risk_per_trade_pct
    portfolio_state: str = ""         # AI 看到的全局仓位快照摘要（决策透明化，便于审计）
    # ── 进场价位对齐（2026-08-14 根治「AI 想 4329 开空、执行 4315 市价开」）──
    #   entry_price = AI 期望的入场价（来自双脑 JSON entry_price 字段，回退解析 reasoning
    #   「反弹/回踩至 X(-Y)」）。SELL 想更高、BUY 想更低；为 None 表示「现在就市价进」。
    #   entry_style = "market"(立即市价) / "limit"(等回到 zone 再点火)。
    #   执行层据其决定「立即市价开」还是「推迟到 zone 再市价开」，绝不丢弃 AI 的价位指引。
    entry_price: float = None
    entry_style: str = "market"
    # ── 第一优先修复·解门锁监控（2026-08-15）：各方向门触发率与总HOLD率快照 ──
    gate_stats: dict = None
    # ── Phase 9 本地校对员（Qwen3-8B）：L0 常态对云端决策做确定性核对 ──
    #   下单前 SL/TP 已知阶段，stop_loss/take_profit 会被执行器回填到本对象，
    #   供校对员结构审计使用（见 trade_executor 下单前强制闸门）。
    stop_loss: float = None
    take_profit: float = None
    #   状态三态，**不可合并**：
    #     skipped —— 没查（本地模型没装/没起/超时）。这是「未知」，不是「没问题」。
    #     clean   —— 查过了，没发现问题。
    #     issues  —— 查出自相矛盾 / 止损挂反 / 幻觉价格。
    #   把 skipped 当 clean 是监控设计里最经典的自欺：模型挂了反而显示「一切正常」。
    #   ★ 校对员**绝不改方向、绝不投票**（7~8B 金融方向判断近随机，Fin-Bias ACL2026），
    #     issues 仅用于告警与审计留痕，不参与任何交易门控。
    proofread_status: str = "skipped"
    proofread_issues: list = None     # List[str]，仅 status=issues 时非空
    proofread_severity: str = "none"  # none / minor / major
    proofread_latency_ms: float = None
    # ── Phase 9.1 闭环断路器（2026-08-08）──
    #   sev=major 的结构性错（SL/TP 挂反、价格幻觉、理由与方向自相矛盾）时，
    #   校对员把本笔决策**降级为 HOLD**（不改方向、不投票，只拦"结构自杀单"），
    #   并打上这两个标记，供执行器 / 前端 / 审计识别"这单是被本地模型按住、不是 AI 自己观望"。
    proofread_blocked: bool = False
    block_reason: str = ""
    # ★ 2026-08-11：措施文案（"做了什么"）。由 status/severity/blocked 确定性派生，
    #   供前端把"系统对该疑点采取了什么措施"清晰展开，不再只有一句"有疑点"。
    proofread_action: str = ""
    # ── Phase 10 方向终审器（2026-08-08）──
    #   背景：云端双脑方向来自 M5/H4 趋势指标，存在滞后性；用户观察本地时序模型（Chronos 等）
    #   在实时行情中更贴近真实方向，要求引入"方向终审"。
    #   原则：① 不改方向、不投票，只做"统计冲突 → HOLD"；② 当前沙箱无法跑 PyTorch，
    #   先用纯 NumPy 规则版兜底，未来可无缝替换为 Chronos/TimesFM/Time-MoE/Moirai 等真实模型；
    #   ③ 所有字段透出给 decision_snapshot / 前端 / 审计。
    direction_guard_blocked: bool = False
    direction_guard_conflict: str = "none"  # none / minor / major
    direction_guard_score: float = 0.0       # -1~+1，统计看多/看空强度
    direction_guard_reason: str = ""
    direction_guard_model: str = "numpy"     # numpy / chronos-2 / timesfm-2.5 / time-moe / moirai
    # ★ 2026-08-15 第三批#4 纯加法：把规则③判定所依赖的原始特征也落库，
    #   使历史单可忠实回放规则③（末端接飞刀=趋势反向+价格延伸|Z|>Z_MINOR），
    #   不再只能重启后前向计数。缺省 None（不污染旧单/不强制新单），下游用 _f 安全读。
    direction_guard_price_to_ma_z: Optional[float] = None  # 价格距基准MA的标准差倍数（规则③核心）
    direction_guard_z_avg_5: Optional[float] = None        # 近5根平均延伸度（末端冲刺确证）
    direction_guard_rsi14: Optional[float] = None          # H1/周期 RSI(14) 动量极端
    # ── fusion_v2 时序融合票（2026-08-10）：4 模型聚合的第四票，透明化/审计用 ──
    ts_fusion_dir: str = "HOLD"          # 融合票方向（BUY/SELL/HOLD）
    ts_fusion_weight: float = 0.0        # 融合票作为第四票的权重（0=未启用/未参与）
    ts_fusion_conf: float = 0.0          # 融合票置信
    ts_fusion_agree: bool = False        # 4 模型是否同向
    ts_fusion_hit_avg: float = 0.0       # 参与模型近期命中率均值
    ts_fusion_models: int = 0            # 参与融合的可用模型数
    ts_fusion_note: str = ""
    # ── 视觉模型第四票（2026-08-14）：H4/M15 K线结构识别，加法增强方向准确率，非闸门 ──
    vision_dir: str = "HOLD"             # 视觉聚合方向（BUY/SELL/HOLD）
    vision_weight: float = 0.0           # 视觉票权重（0=未参与/不可用）
    vision_conf: float = 0.0             # 视觉票置信
    vision_agree: bool = False           # H4/M15/M5 是否同向
    vision_h4_dir: str = "HOLD"          # H4 单周期方向
    vision_m15_dir: str = "HOLD"         # M15 单周期方向
    vision_m5_dir: str = "HOLD"          # M5 单周期方向（实时管理微结构）
    vision_m5_conf: float = 0.0          # M5 单周期置信
    vision_note: str = ""
    # ── Qwen3-8B 常态确认型副驾第五票（2026-08-14）：仅确认时序方向，加法提准非拦截 ──
    copilot_dir: str = "HOLD"          # 副驾确认方向（必与 chronos_dir 同向才计入）
    copilot_weight: float = 0.0        # 副驾票权重（0=未参与/不可用/未过锁）
    copilot_conf: float = 0.0          # 副驾票置信
    copilot_agree: bool = False        # 副驾是否与有效时序方向同向
    copilot_note: str = ""
    # ── 辩论环 shadow（2026-08-17·walk-forward A/B 基线采集）──
    #   开关关闭时仍计算"若开启会缩到多少"，只记录不应用；None=未计算(BUY/SELL 才计算)。
    #   跑 1-2 周基线与 shadow 差异后无痛开启 DEBATE_RING_ENABLED。
    debate_ring_shadow: dict = None
    # ── 篮子级 AI 持仓管理（2026-08-17·用户铁律：开完仓核心任务=维护持仓）──
    #   双脑 position_action(hold/trim/close_all) 融合结果，执行层据此处置全部持仓。
    basket_action: str = "hold"
    basket_action_conf: float = 0.0
    basket_action_reason: str = ""
    basket_action_confirmed: bool = False      # 连续 2 轮确认通过才 True
    basket_action_confirm_note: str = ""

# ── Qwen3-8B 常态副驾票缓存（仿 vision_service 生产者模式，避免多账号每账号各调一次）──
# XAUUSD 为单标的，副驾确认票与账号无关，按刷新周期缓存 1 次推理供全账号并行复用。
_copilot_cache_lock = threading.Lock()
_copilot_cache = {"vote": None, "ts": 0.0}


def _get_cached_copilot_vote(svc, market_data, settings):
    """带刷新的副驾票缓存：周期内复用同一张票，跨账号只推理 1 次。"""
    _refresh = float(getattr(settings, "LOCAL_COPILOT_REFRESH_SEC", 15.0))
    _now = _time.time()
    with _copilot_cache_lock:
        if _copilot_cache.get("vote") is not None and (_now - _copilot_cache.get("ts", 0.0)) < _refresh:
            return _copilot_cache["vote"]
    # 缓存未命中 → 调一次（释放锁后再调，避免长推理持锁）
    _vote = svc.copilot(market_data)
    with _copilot_cache_lock:
        _copilot_cache["vote"] = _vote
        _copilot_cache["ts"] = _now
    return _vote


def _build_plain_summary(
    final_decision: str,
    debate_consensus: str,
    risk_level: str,
    risk_score: int,
    ds_final: str,
    hy_final: str,
    ds_confidence: float,
    hy_confidence: float,
    market_regime: str,
    chronos_dir: str = "NEUTRAL",
    chronos_weight: float = 0.0,
    chronos_agree: bool = False,
    final_confidence: float = 0.0,
) -> str:
    """
    把 Meta 裁决翻译成人话 — 一句话让普通用户看懂 AI 在想什么。

    设计原则：说人话、不堆术语、突出「该不该动手、为什么、风险多大」。
    """
    # 风险中文标签
    risk_zh = {
        "low": "风险低",
        "medium": "风险一般",
        "high": "风险偏高",
        "extreme": "风险极高",
    }.get(risk_level, "风险未知")

    # 市场体制中文标签
    regime_zh = {
        "strong_uptrend": "强势上涨",
        "strong_downtrend": "强势下跌",
        "uptrend": "上涨趋势",
        "downtrend": "下跌趋势",
        "ranging": "区间震荡",
        "normal": "正常波动",
        "高波动": "剧烈波动",
        "极端": "极端行情",
        "低波动": "平静行情",
    }.get(market_regime, market_regime or "当前")

    # ── ★ Grounding（2026-08-12 修复：方向断言 100% 来自真实投票，不靠置信度数字反推）──
    #   旧逻辑按 final_decision 硬写「看涨/看跌」并贴置信度，导致「三模型全 SELL 却文案写看涨」的颠倒。
    #   现在所有方向措辞都由 ds_final/hy_final/chronos_dir 的真实投票确定性派生；Meta 逆共识翻向时明确标注。
    ds_dir = _normalize_decision(ds_final)
    hy_dir = _normalize_decision(hy_final)
    ch_dir = _normalize_decision(chronos_dir)
    _zh = {"BUY": "看涨", "SELL": "看跌", "HOLD": "观望"}
    _parts = []
    if ds_dir in ("BUY", "SELL"):
        _parts.append(f"DeepSeek {_zh[ds_dir]}({ds_confidence:.0%})")
    elif ds_dir == "HOLD":
        _parts.append("DeepSeek 观望")
    if hy_dir in ("BUY", "SELL"):
        _parts.append(f"混元 {_zh[hy_dir]}({hy_confidence:.0%})")
    elif hy_dir == "HOLD":
        _parts.append("混元 观望")
    if ch_dir in ("BUY", "SELL"):
        _parts.append(f"Chronos {_zh[ch_dir]}")
    else:
        _parts.append("Chronos 未参与")
    _vote_desc = "、".join(_parts)
    _votes = [d for d in (ds_dir, hy_dir, ch_dir) if d in ("BUY", "SELL")]
    _buy_n = sum(1 for d in _votes if d == "BUY")
    _sell_n = sum(1 for d in _votes if d == "SELL")
    if _buy_n and not _sell_n:
        _maj, _all_agree = "BUY", (_buy_n == 3)
    elif _sell_n and not _buy_n:
        _maj, _all_agree = "SELL", (_sell_n == 3)
    elif _buy_n and _sell_n:
        _maj, _all_agree = "SPLIT", False
    else:
        _maj, _all_agree = "HOLD", True
    _split = (_maj == "SPLIT")
    _override = (final_decision in ("BUY", "SELL") and _maj in ("BUY", "SELL") and final_decision != _maj)

    # ── 极端风险：直接兜底 ──
    if risk_level == "extreme":
        if final_decision == "HOLD":
            return f"现在行情太吓人了，波动剧烈到危险级别，两个 AI 一致决定先不动，保住本金要紧。"
        return f"两个 AI 想动手，但当前风险等级过高，强制按住不让你做，安全第一。"

    # ── HOLD 场景 ──
    if final_decision == "HOLD":
        if debate_consensus == "disagreement":
            return (
                f"两个 AI 吵起来了 —— 一个觉得要涨、一个觉得要跌，谁也说服不了谁。"
                f"为不让你白送钱，决定先看戏、不开仓，等方向明朗再说。"
            )
        if debate_consensus == "moderate":
            if ds_final == "HOLD" and hy_final == "HOLD":
                return (
                    f"两个 AI 看完数据都觉得没戏 —— 现在是「{regime_zh}」，信号弱、方向不明，"
                    f"硬上大概率亏钱，所以建议先耐心等更好的入场时机。"
                )
            return (
                f"两个 AI 没吵出结果 —— 一个说要{('涨' if ds_final=='BUY' or hy_final=='BUY' else '跌')}，"
                f"另一个说还是稳着好；当前「{regime_zh}」不值得冒险，先按住不动。"
            )
        # strong consensus on HOLD
        return (
            f"两个 AI 都明确说：现在「{regime_zh}」没什么机会，{risk_zh}也犯不着去博，"
            f"别动比乱动强，等真正的好机会。"
        )

    # ── 进出场场景（方向断言 100% 来自真实投票，绝不以 final_decision 硬写方向）──
    if final_decision in ("BUY", "SELL"):
        _dir_single = "涨" if final_decision == "BUY" else "跌"
        direction_zh = "看涨" if final_decision == "BUY" else "看跌"
        action_zh = "买入做多" if final_decision == "BUY" else "卖出做空"
        # ★ 2026-08-14 修复：人话解读必须与执行层口径一致，避免「AI说开单但MT5没执行」的误会。
        #   当 Meta 置信低于执行门槛（默认58%）时，直接告诉用户「本轮不开仓」；
        #   当处于震荡市且非三脑共振时，提醒执行门槛可能因体制门软惩罚上调。
        # ★ 2026-08-17 修复（用户反馈"看不懂/矛盾"）：置信不足时主句直接改"观望"语气，
        #   不再先说"小仓位试水、设好止损"再补括号"本轮不开仓"——方向建议和不动手同屏出现
        #   会让普通用户懵。置信不足 = 本轮不动作，文案统一为「AI 有倾向但把握不足，先不动手」。
        _exec_note = ""
        _min_conf = float(getattr(settings, "RISK_MIN_CONFIDENCE", 0.58) or 0.58)
        _conf_short = final_confidence < _min_conf - 1e-9
        if _conf_short:
            _exec_note = f"（AI 把握不足：综合置信{final_confidence:.0%}低于执行门槛{_min_conf:.0%}，本轮选择不动手，继续盯盘等信号更明确）"
        elif not _all_agree and market_regime in ("ranging", "normal"):
            _exec_note = "（区间震荡且非三脑共振，执行门槛可能上调，若未达门槛则不开仓）"
        # ★ 逆共识：Meta 最终方向与多数真实投票相反 → 必须如实标注，不得粉饰为"多数看涨"
        if _override:
            return (
                f"⚠️ Meta 综合后倾向「{direction_zh}」（{action_zh}黄金），但真实投票是："
                f"{_vote_desc} —— 属逆共识操作，{risk_zh}，建议降仓、收紧止损，切勿盲目加注。{_exec_note}"
            )
        if _all_agree and _maj == final_decision:
            return (
                f"三模型共振「{direction_zh}」：{_vote_desc}，方向高度一致，"
                f"当前「{regime_zh}」{risk_zh}可控，可果断「{action_zh}」黄金。{_exec_note}"
            )
        if _maj == final_decision and len(_votes) >= 2:
            return (
                f"多数 AI 倾向「{direction_zh}」：{_vote_desc}，方向基本明朗，"
                f"当前「{regime_zh}」{risk_zh}，可考虑「{action_zh}」，控制仓位。{_exec_note}"
            )
        if _maj == final_decision:
            if _conf_short:
                return (
                    f"AI 内部仅部分模型看{_dir_single}（{_vote_desc}），Meta 综合倾向「{direction_zh}」，"
                    f"但当前「{regime_zh}」把握不足、{risk_zh}，本轮先不动手，继续观察。"
                )
            return (
                f"AI 内部仅部分模型看{_dir_single}（{_vote_desc}），"
                f"Meta 综合后倾向「{direction_zh}」，当前「{regime_zh}」{risk_zh}，"
                f"小仓位试水、设好止损。{_exec_note}"
            )
        return (
            f"AI 内部分歧（{_vote_desc}），Meta 综合后倾向「{direction_zh}」，"
            f"当前「{regime_zh}」{risk_zh}，建议小仓位试水、设好止损。{_exec_note}"
        )

    # 兜底
    return f"当前「{regime_zh}」下，AI 综合给出「{final_decision}」建议（{_vote_desc}），{risk_zh}。"


# ★ 2026-08-05 20:50 修正（实盘审计发现：0.85导致2小时60+次有效信号全被杀）：
#   实盘数据：XAUUSD从4090涨到4205（+$115），DeepSeek连续输出BUY(65~75%)，
#   但混元频繁HOLD→触发R2分裂→85%门槛永远达不到→0笔交易。
#   2026-08-06 清理：原 SINGLE_MODEL_MIN_CONF(0.70) 为死代码（从未引用），
#   实际 R2 分裂门槛由 SPLIT_DECISION_MIN_CONF(0.55) 控制，此处删除冗余常量避免误导。
#   新阈值0.55=允许单一模型中等偏强信号（原始≈65%+）在同伴观望时独立开单。
#   保留双模型反向(R1)和极端风险拦截不变。
SPLIT_DECISION_MIN_CONF = 0.55
# ★ 2026-08-15 倾斜单(lean)下限：方向明确但共识不足时，只要归一化置信≥此值仍放行，
#   手数随置信缩放（提准非拦截·多交易）。低于此值才判 HOLD（纯噪声/无方向）。
#   取 0.42：显著高于随机(0.33)，确保仍有真实方向性倾斜，而非接飞刀。
LEAN_MIN_CONF = 0.42

# ── 第一优先修复·解门锁监控（2026-08-15）──
# 统计各方向门触发次数与总HOLD率，避免再盲调。铁律「提准非拦截」下，
# 方向门由硬 HOLD 改为温和降权，故监控口径从"拦截数"改为"门触发数"；
# 仍统计最终 HOLD 率，用于验证解锁后交易频率是否真实回升。
GATE_STATS = {"total_decisions": 0, "holds": 0, "traded": 0, "gates": {}}
_GATE_LOG_EVERY = 50
# ★ 2026-08-15 审计P3修复：多账号并发 adjudicate 对 GATE_STATS 做 read-modify-write，
#   无锁会丢更新/门触发率失真（仅监控，不影响交易，但审计基线要可信）。
_GATE_STATS_LOCK = threading.Lock()

# ★★ 2026-08-17 篮子级 AI 持仓管理：双脑 position_action 融合 + 防抖确认状态
#   用户铁律：开完仓核心任务 = 维护持仓（一仓一仓管，空仓才找机会）。
#   DS/HY 每次决策输出 position_action(hold/trim/close_all)，此处加权融合，
#   非 hold 动作需连续 CONFIRM_CYCLES 轮同向确认才可执行（防 AI 抖动误杀）。
_BASKET_ACTION_HISTORY: dict = {}   # account_id -> list[(ts, action)]（最近 N 轮）
_BASKET_ACTION_CONFIRM = 2          # 确认所需同向次数（窗口内累计，容忍中间 hold 抖动）
_BASKET_CONFIRM_WINDOW = 600.0      # 确认窗口秒数（10 分钟；模型意向轮间抖动 + 系统 2 分钟/轮 → 需跨更多轮累计）

def _gate_count(name):
    """记录某方向门本次触发（降权，非硬拦截）。"""
    with _GATE_STATS_LOCK:
        GATE_STATS["gates"][name] = GATE_STATS["gates"].get(name, 0) + 1


def _apply_sr_location_gate(final_decision: str, final_confidence: float, market_data: dict) -> float:
    """支撑/压力位置质量门（2026-08-19·提准非拦截）。

    识别当前价位是否已贴近关键支撑（SELL）或压力（BUY），若是则降权，
    避免"卖到支撑底 / 买到压力顶"。价格已突破关键位时不惩罚。
    """
    if final_decision not in ("BUY", "SELL"):
        return final_confidence
    if not getattr(settings, "SR_LOCATION_GATE_ENABLED", True):
        return final_confidence

    try:
        _kl = (market_data or {}).get("key_levels") or {}
        _sa = (market_data or {}).get("structure_anchors") or {}
        _tf = (market_data or {}).get("timeframes") or {}
        _m15 = _tf.get("M15") or {}
        _h1 = _tf.get("H1") or {}

        # 当前价
        _cp = float(
            (market_data or {}).get("current_price", {}).get("bid")
            or (market_data or {}).get("current_price", {}).get("last")
            or (_m15.get("latest") or {}).get("close")
            or 0
        )
        # ATR
        _atr = float(
            (_m15.get("atr") if isinstance(_m15, dict) else None)
            or (_h1.get("atr") if isinstance(_h1, dict) else None)
            or 0
        )
        if _cp <= 0 or _atr <= 0:
            return final_confidence

        # ★ 临时调试（验证 SR 门非空心门）：打印实际拿到的关键位
        logger.warning(
            f"[SR-GATE-DEBUG] cp={_cp:.2f} atr={_atr:.2f} "
            f"sl_anchor_buy={_sa.get('sl_anchor_buy')} sl_anchor_sell={_sa.get('sl_anchor_sell')} "
            f"kl.recent_low_20d={_kl.get('recent_low_20d')} kl.support={_kl.get('support')}"
        )

        _threshold_atr = float(getattr(settings, "SR_LOCATION_THRESHOLD_ATR", 1.0))
        _penalty_near = float(getattr(settings, "SR_LOCATION_PENALTY_NEAR", 0.55))
        _penalty_mid = float(getattr(settings, "SR_LOCATION_PENALTY_MID", 0.75))

        # 支撑：SMC sl_anchor_buy < 当前价；否则取 M15 最近 20 根低点；否则日图 20 日低
        _support = None
        if isinstance(_sa, dict) and _sa.get("sl_anchor_buy") and _sa["sl_anchor_buy"] < _cp:
            _support = float(_sa["sl_anchor_buy"])
        else:
            _m15_lows = _m15.get("lows") if isinstance(_m15, dict) else None
            if isinstance(_m15_lows, (list, tuple)) and len(_m15_lows) >= 10:
                _support = min(float(x) for x in _m15_lows[-20:] if isinstance(x, (int, float)))
        if _support is None and isinstance(_kl, dict) and _kl.get("recent_low_20d"):
            _support = float(_kl["recent_low_20d"])

        # 阻力：SMC sl_anchor_sell > 当前价；否则取 M15 最近 20 根高点；否则日图 20 日高
        _resistance = None
        if isinstance(_sa, dict) and _sa.get("sl_anchor_sell") and _sa["sl_anchor_sell"] > _cp:
            _resistance = float(_sa["sl_anchor_sell"])
        else:
            _m15_highs = _m15.get("highs") if isinstance(_m15, dict) else None
            if isinstance(_m15_highs, (list, tuple)) and len(_m15_highs) >= 10:
                _resistance = max(float(x) for x in _m15_highs[-20:] if isinstance(x, (int, float)))
        if _resistance is None and isinstance(_kl, dict) and _kl.get("recent_high_20d"):
            _resistance = float(_kl["recent_high_20d"])

        _near = _threshold_atr * 0.5 * _atr
        _mid = _threshold_atr * _atr

        if final_decision == "SELL" and _support is not None:
            _dist = _cp - _support
            if _dist < 0:
                logger.info(
                    f"[MetaAgent] 支撑压力门: SELL 已跌破支撑{_support:.2f}→不拦截"
                )
            elif _dist < _near:
                _old = final_confidence
                final_confidence = final_confidence * _penalty_near
                logger.warning(
                    f"[MetaAgent] 支撑压力门(强降权): SELL 贴支撑{_support:.2f} "
                    f"距{_dist:.2f}<{_near:.2f} → 置信{_old:.0%}→{final_confidence:.0%}"
                )
                _gate_count("支撑压力门")
            elif _dist < _mid:
                _old = final_confidence
                final_confidence = final_confidence * _penalty_mid
                logger.info(
                    f"[MetaAgent] 支撑压力门(中降权): SELL 近支撑{_support:.2f} "
                    f"距{_dist:.2f}<{_mid:.2f} → 置信{_old:.0%}→{final_confidence:.0%}"
                )
                _gate_count("支撑压力门")

        elif final_decision == "BUY" and _resistance is not None:
            _dist = _resistance - _cp
            if _dist < 0:
                logger.info(
                    f"[MetaAgent] 支撑压力门: BUY 已涨破阻力{_resistance:.2f}→不拦截"
                )
            elif _dist < _near:
                _old = final_confidence
                final_confidence = final_confidence * _penalty_near
                logger.warning(
                    f"[MetaAgent] 支撑压力门(强降权): BUY 贴阻力{_resistance:.2f} "
                    f"距{_dist:.2f}<{_near:.2f} → 置信{_old:.0%}→{final_confidence:.0%}"
                )
                _gate_count("支撑压力门")
            elif _dist < _mid:
                _old = final_confidence
                final_confidence = final_confidence * _penalty_mid
                logger.info(
                    f"[MetaAgent] 支撑压力门(中降权): BUY 近阻力{_resistance:.2f} "
                    f"距{_dist:.2f}<{_mid:.2f} → 置信{_old:.0%}→{final_confidence:.0%}"
                )
                _gate_count("支撑压力门")
    except Exception as _sr_err:
        logger.debug(f"[MetaAgent] 支撑压力门异常(降级无影响): {_sr_err}")

    return final_confidence


# ★★ 2026-08-17 篮子级 AI 持仓管理：双脑 position_action 融合（模块级纯函数）
#   规则（保守优先）：close_all > trim > hold；
#   双脑同档 → 加权置信；分歧 → 取保守档 + 置信×0.5；双 hold/无持仓 → hold。
def _fuse_basket_action(ds_analysis, hy_analysis, ds_c, hy_c, market_data):
    try:
        def _bconf(v, fallback=0.5):
            try:
                s = str(v or "").strip().rstrip("%").strip()
                if s in ("", "null", "None", "nan", "NaN", "inf", "-inf"):
                    return fallback
                c = float(s)
            except (TypeError, ValueError):
                return fallback
            import math as _m
            if not _m.isfinite(c):
                return fallback
            if c > 100.0:
                return 1.0
            if c > 1.0:
                return c / 100.0
            return max(0.0, min(1.0, c))

        def _extract(a):
            pa = (a or {}).get("position_action") or {}
            act = str(pa.get("action") or "hold").strip().lower()
            if act not in ("hold", "trim", "close_all"):
                act = "hold"
            conf = _bconf(pa.get("confidence"), 0.5)
            reason = str(pa.get("reason") or "")[:80]
            return act, conf, reason

        ds_act, ds_ca, ds_r = _extract(ds_analysis)
        hy_act, hy_ca, hy_r = _extract(hy_analysis)
        _rank = {"hold": 0, "trim": 1, "close_all": 2}
        _pos = (market_data or {}).get("my_open_positions") or []
        if not _pos:
            return "hold", 0.0, "无持仓"
        if ds_act == "hold" and hy_act == "hold":
            return "hold", 0.0, "双脑建议持有"
        _w = max(float(ds_c or 0) + float(hy_c or 0), 1e-9)
        if ds_act == hy_act:
            conf = (ds_ca * float(ds_c or 0) + hy_ca * float(hy_c or 0)) / _w
            return ds_act, round(conf, 3), f"双脑同向{ds_act}"
        # 分歧：取保守档（rank 高 = 更行动），置信降权（★ 2026-08-17 P1修复：
        #   ×0.5 使分歧档最大置信=0.5 < BASKET_AI_MIN_CONF(0.6) → close_all/trim
        #   在分歧/单云降级时恒不落地（死代码）。改 ×0.8，仍显著低于同向。
        stronger = ds_act if _rank[ds_act] >= _rank[hy_act] else hy_act
        conf = (ds_ca * float(ds_c or 0) + hy_ca * float(hy_c or 0)) / _w * 0.8
        return stronger, round(conf, 3), f"双脑分歧({ds_act}/{hy_act})取{stronger}"
    except Exception:
        return "hold", 0.0, "融合异常回退hold"


# 防抖确认：非 hold 动作需在确认窗口内累计 _BASKET_ACTION_CONFIRM 次同向才可执行。
# ★ 2026-08-17 P0 修复（用户实锤"AI 没专心护盘"）：原逻辑要求【连续】同向，
#   但模型 position_action 意向轮间不稳定（时 trim 时 hold），中间任何一轮 hold
#   即打断连续性 → 永远"确认中(1/2)" → AI 的减仓/全平意图从未执行。
#   改【窗口内累计】制：hold 不累计也不打断（模型意向抖动容忍），
#   10 分钟窗口内出现 ≥2 次同向动作即确认执行。
def _confirm_basket_action(account_id, action):
    now = _time.time()
    hist = _BASKET_ACTION_HISTORY.setdefault(account_id, [])
    hist.append((now, action))
    _BASKET_ACTION_HISTORY[account_id] = [
        h for h in hist if now - h[0] <= _BASKET_CONFIRM_WINDOW
    ]
    if action == "hold":
        return "hold", False, "hold 无需确认"
    # ★ 2026-08-17 P1 优化：close_all/trim 同属"护盘意向族"（都是 AI 认为该减仓），
    #   窗口内累计时合并计数——AI 意向在 close_all/trim 间切换时不再重置。
    #   实测：AI 意向轮间抖动（close_all→hold→trim），单动作累计难达 2 次 → 护盘从未执行。
    _family = {"close_all", "trim"}
    cnt = sum(
        1 for _ts, _a in _BASKET_ACTION_HISTORY[account_id]
        if _a == action or (_a in _family and action in _family)
    )
    if cnt >= _BASKET_ACTION_CONFIRM:
        return action, True, f"窗口内累计{cnt}次确认"
    return action, False, f"确认中({cnt}/{_BASKET_ACTION_CONFIRM})"


# ★ 2026-08-17 P1修复：执行成功后清除确认历史——否则 trim/close_all 历史残留
#   导致每轮 AI 输出同动作即恒 confirmed（防抖被击穿，可每 2 分钟减半一次直至碎单）。
def _reset_basket_action(account_id, action=None):
    try:
        if action is None:
            _BASKET_ACTION_HISTORY.pop(account_id, None)
        else:
            hist = _BASKET_ACTION_HISTORY.get(account_id)
            if hist:
                _BASKET_ACTION_HISTORY[account_id] = [
                    h for h in hist if h[1] != action
                ]
    except Exception:
        pass


def get_gate_stats_snapshot():
    """返回各方向门触发率与总HOLD率快照（解门锁监控，供 dashboard/审计读取）。"""
    _tot = max(1, GATE_STATS["total_decisions"])
    return {
        "total_decisions": GATE_STATS["total_decisions"],
        "hold_rate": round(GATE_STATS["holds"] / _tot, 3),
        "trade_rate": round(GATE_STATS["traded"] / _tot, 3),
        "gate_trigger_rates": {
            k: {"triggers": v, "rate": round(v / _tot, 3)}
            for k, v in GATE_STATS["gates"].items()
        },
    }


def _normalize_decision(d) -> str:
    """
    将模型返回的 decision 规范化为三态之一：BUY / SELL / HOLD。
    解决 LLM 返回小写(buy)、中文(观望/买入/做空)、同义词(long/short)导致
    adjudicate 的 == "BUY"/"SELL" 判断静默失效、从而乱开单的问题。
    任何无法识别的取值一律保守映射为 HOLD（不开单）。
    """
    if not d:
        return "HOLD"
    s = str(d).strip().lower()
    buy = {"buy", "b", "long", "做多", "买入", "看涨", "多", "bull"}
    sell = {"sell", "s", "short", "做空", "卖出", "看跌", "空", "bear"}
    hold = {"hold", "h", "neutral", "观望", "等待", "中性", "不操作",
            "no trade", "none", "nan", "待定", "观察"}
    if s in buy:
        return "BUY"
    if s in sell:
        return "SELL"
    return "HOLD"


def _parse_entry_from_reasoning(reasoning: str, decision_dir: str, current_price: float = None):
    """回退解析：从 reasoning 中文文本抓「反弹/回踩/等待至 X(-Y)」价位。

    仅当结构化 entry_price 缺失时启用（DeepSeek 已回传 entry_price；混元已补）。
    返回 AI 期望的入场价（float）或 None。
    SELL 想更高、BUY 想更低 → 取对应极值（更优盈亏比）。
    过滤明显异常（非 XAUUSD 价）：以 current_price 为中心 ±30% 动态区间；
    无当前价则回退 1500~6000 绝对带（仍剔除明显非XAU价）。
    """
    import re
    if not reasoning:
        return None
    # 优先显式区间：4322-4329 / 4322~4329 / 至4322-4329
    pats = re.findall(r"(\d{3,4})\s*[-~～至到]\s*(\d{3,4})", reasoning)
    cands = []
    for a, b in pats:
        try:
            a, b = float(a), float(b)
        except ValueError:
            continue
        cands.append(max(a, b) if decision_dir == "SELL" else min(a, b))
    # 动态金价区间：以当前价为中心 ±30%（无当前价则回退 1500~6000 绝对带）
    if current_price and 1000 <= current_price <= 10000:
        _lo, _hi = current_price * 0.7, current_price * 1.3
    else:
        _lo, _hi = 1500.0, 6000.0
    # 无区间则取首个落在区间的四位价（如「反弹至3350」）
    if not cands:
        singles = re.findall(r"(\d{3,4})", reasoning)
        for s in singles:
            try:
                v = float(s)
            except ValueError:
                continue
            if _lo <= v <= _hi:
                cands.append(v)
                break
    cands = [c for c in cands if _lo <= c <= _hi]
    if not cands:
        return None
    return max(cands) if decision_dir == "SELL" else min(cands)


class MetaAgent:
    """
    Meta-Agent — 动态加权裁决器
    根据两个模型的历史表现，动态调整权重融合最终决策
    """

    def __init__(self):
        self.deepseek_perf = ModelPerformance()
        self.hunyuan_perf = ModelPerformance()
        self._conf_calib = None  # 置信校准层（懒加载，零 DB 访问）
        self.decision_history: list = []  # 最近100次决策
        # evolution log 回调：fn(EvolutionLog-like dict) 外部实现写库
        self.evo_logger = None
        self._last_save = 0.0
        # ★ M3a：启动时恢复已学习权重（重启不失忆）
        self.load_state()
        # 进程退出前强制落盘（限频内也写一份，防重启丢最近反馈）
        try:
            atexit.register(self.save_state, force=True)
        except Exception:
            pass

    # ── 置信校准层（提准非拦截）──
    # 运行期仅加载离线生成的 data/confidence_calibration.json 做查表，
    # 零 DB 访问、零推理成本；缺失/关闭则透传（calibrated==raw）。
    def _get_calibrator(self):
        if not getattr(settings, "CONFIDENCE_CALIBRATION_ENABLED", True):
            return None
        if self._conf_calib is None:
            try:
                from app.core.confidence_calibrator import ConfidenceCalibrator
                self._conf_calib = ConfidenceCalibrator()
            except Exception:
                self._conf_calib = None
        return self._conf_calib

    def get_weights(self, market_regime: str = "normal") -> tuple:
        """
        获取动态权重
        基于: 近期准确率 × 市场适应性

        ★ 2026-08-06 修复 P0「DS独裁」：对 HY 权重设评分级地板（默认 0.15）。
          原逻辑 hy_weight 直接用 hy_perf.recent_accuracy，混元一旦连续判错，
          recent_accuracy 趋近 0 → 归一化后 HY 权重≈0，双模型制衡退化为 DS 单模型独裁，
          仅剩 R1 硬否决兜底（脆弱单点）。设地板后，即便 HY 短期失准，仍保留 15% 制衡力，
          "双模型辩论"真正名副其实，避免强趋势中 DS 集体误判无人纠正。
        """
        # ★ 2026-08-06 Fix2：打破 DS 独裁——权重不对称封顶（对称区间 [0.35, 0.65]）
        #   根因：旧逻辑 HY 权重 = recent_accuracy，混元一保守→准确率趋0→归一化后≈0，
        #   双模型制衡退化为 DS 单模型独裁。Fix1 已让 HY 的 HOLD 不再被罚（准确率只反映方向性判断），
        #   此处再封顶：无论战绩如何，两模型权重恒落 [0.35, 0.65]，永远互相制衡。
        #   HY_WEIGHT_FLOOR 默认从 0.15 提到 0.35；新增 DS_WEIGHT_CEIL 锁 0.65。
        _WEIGHT_FLOOR = float(getattr(settings, "HY_WEIGHT_FLOOR", 0.35))
        _WEIGHT_CEIL = float(getattr(settings, "DS_WEIGHT_CEIL", 0.65))

        ds_weight = self.deepseek_perf.recent_accuracy
        hy_weight = self.hunyuan_perf.recent_accuracy

        # 市场体制适应性调整
        # DeepSeek在趋势市场更强，混元在震荡市/高波动市更强
        regime_adjustments = {
            "strong_uptrend": (1.15, 0.85),
            "strong_downtrend": (1.15, 0.85),
            "uptrend": (1.10, 0.90),
            "downtrend": (1.10, 0.90),
            "ranging": (0.85, 1.15),
            "高波动": (0.80, 1.20),
            "极端": (0.70, 1.30),
            "低波动": (1.05, 0.95),
        }

        adj = regime_adjustments.get(market_regime, (1.0, 1.0))
        ds_weight *= adj[0]
        hy_weight *= adj[1]

        # ★ 封顶+地板（对称）：两模型权重恒在 [FLOOR, CEIL]
        ds_weight = min(max(ds_weight, _WEIGHT_FLOOR), _WEIGHT_CEIL)
        hy_weight = min(max(hy_weight, _WEIGHT_FLOOR), _WEIGHT_CEIL)

        # ★ 2026-08-18 打破 HY 恒 HOLD 死锁：混元连续 N 轮 HOLD(无方向输出)→
        #   其「保守观望」不应占用方向竞争权重（HOLD 不计入 decision_scores 却仍锁高权重
        #   使每单落入 R2 高门槛）。强制混元封底、DS 封顶，让方向模型主导，趋势明确时能开单；
        #   混元一旦重新给方向(_HY_HOLD_STREAK 归零)立即恢复竞争（不废其制衡力）。
        if _HY_HOLD_STREAK.get("n", 0) >= int(getattr(settings, "HY_HOLD_DECAY_ROUNDS", 8)):
            hy_weight = min(hy_weight, _WEIGHT_FLOOR)
            ds_weight = max(ds_weight, _WEIGHT_CEIL)

        # 归一化（夹取后总和可能<1，归一化恢复比例，但因双都在区间内，和必在[0.7,1.3]）
        total = ds_weight + hy_weight
        if total == 0:
            return 0.5, 0.5
        ds_w = ds_weight / total
        hy_w = hy_weight / total
        # 归一化后再夹一次，确保严格落在 [FLOOR, CEIL]（防归一化突破天花板）
        ds_w = min(ds_w, _WEIGHT_CEIL)
        hy_w = max(hy_w, _WEIGHT_FLOOR)
        _t2 = ds_w + hy_w
        if _t2 == 0:
            return 0.5, 0.5
        return ds_w / _t2, hy_w / _t2

    # ════════════════════════════════════════════════════════════════
    # ★ M3a：可学习状态持久化（重启恢复，进化不归零）
    # ════════════════════════════════════════════════════════════════
    def to_dict(self) -> dict:
        """序列化为可落库 dict（仅学习状态，不含回调/历史展示）。"""
        return {
            "version": 1,
            "deepseek_perf": {k: getattr(self.deepseek_perf, k) for k in _PERF_FIELDS},
            "hunyuan_perf": {k: getattr(self.hunyuan_perf, k) for k in _PERF_FIELDS},
            "saved_at": _time.time(),
        }

    def load_state(self):
        """启动时从 JSON 恢复权重；失败则保持默认（不阻断启动）。"""
        try:
            if not os.path.exists(_META_STATE_FILE):
                return
            with open(_META_STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            ds = d.get("deepseek_perf") or {}
            hy = d.get("hunyuan_perf") or {}
            for k in _PERF_FIELDS:
                if k in ds:
                    setattr(self.deepseek_perf, k, ds[k])
                if k in hy:
                    setattr(self.hunyuan_perf, k, hy[k])
            logger.info(
                f"[MetaAgent] 已恢复持久化权重 "
                f"DS_acc={self.deepseek_perf.recent_accuracy:.2f}(n={self.deepseek_perf.total_signals}) "
                f"HY_acc={self.hunyuan_perf.recent_accuracy:.2f}(n={self.hunyuan_perf.total_signals})"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MetaAgent] 权重恢复失败(使用默认): {e}")

    def save_state(self, force: bool = False):
        """落盘当前权重；限频(默认30s)避免频繁 IO。force=True 立即写(进程退出前用)。

        持久化根治(国际调研精髓 ≥3源: LangGraph/Microsoft Agent Framework/dd-ff):
        原子写(os.replace) + WinError 指数退避重试(3次) + 内存权重不丢(降级到下次周期)。
        Defender 实时扫描锁文件(WinError 5)时重试可绕过瞬时锁；彻底根治见
        backend/add_defender_exclusions.ps1(管理员运行排除目录)。
        """
        try:
            now = _time.time()
            if not force and (now - self._last_save) < _META_SAVE_INTERVAL:
                return
            self._last_save = now
            self._save_once(attempts=3)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MetaAgent] 权重持久化失败(内存保留): {e}")

    def _save_once(self, attempts: int = 3):
        last = None
        for i in range(attempts):
            try:
                tmp = _META_STATE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
                os.replace(tmp, _META_STATE_FILE)  # 原子替换，避免半写损坏
                logger.debug("[MetaAgent] 权重已持久化")
                return
            except (PermissionError, OSError) as e:
                # Defender 锁文件(WinError 5) → 指数退避重试，内存权重不丢
                last = e
                if i < attempts - 1:
                    _time.sleep(0.2 * (2 ** i))
                    continue
        logger.warning(f"[MetaAgent] 权重持久化失败(内存权重保留,下次重试): {last}")

    def adjudicate(
        self,
        deepseek_analysis: dict,
        hunyuan_analysis: dict,
        deepseek_rebuttal: dict,
        hunyuan_rebuttal: dict,
        market_data: dict,
    ) -> DebateDecision:
        """
        加权裁决 — 综合双模型初判 + 辩论反驳 + 风险量化
        """
        # ── 大脑审计：记录 meta_agent 接入（喂了什么）──
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("meta_agent", "input", input_fields=market_data)
        except Exception:
            pass
        # 提取最终立场（辩论后可能修正）并规范化（R4）
        ds_final = _normalize_decision(
            deepseek_rebuttal.get("decision", deepseek_analysis.get("decision", "HOLD"))
        )
        hy_final = _normalize_decision(
            hunyuan_rebuttal.get("decision", hunyuan_analysis.get("decision", "HOLD"))
        )

        # ★ 2026-08-16 审计P1修复：双脑置信未归一化——LLM 可能返回 "95"/"150%"/"0.85" 等
        #   非 [0,1] 值，旧代码直接 float() → ds_cw 超界 → 同向单票时 norm=decision_scores/
        #   active_weight 恒=1.0 → 置信门控形同虚设、手数不缩放。统一 clamp 到 [0,1]。
        # ★ 2026-08-16 审计终检修复：① float("nan")/inf 经 min/max clamp 会变成 1.0 满置信
        #   （min(1.0,nan) 返回 1.0）→ 门控/手数被击穿，须前置 isfinite 判空回退 0.5；
        #   ② "1.5"（=150%）被 /100 误除成 0.015（权重消失），应 >100 才 /100；
        #   ③ "95%" 带 % 号 float() 抛错→0.5，应先 strip。
        def _safe_conf(v, fallback=0.5):
            try:
                s = str(v or "").strip().rstrip("%").strip()
                if s in ("", "null", "None", "nan", "NaN", "inf", "-inf"):
                    return fallback
                c = float(s)
            except (TypeError, ValueError):
                return fallback
            import math
            if not math.isfinite(c):
                return fallback
            if c > 100.0:
                return 1.0
            if c > 1.0:
                return c / 100.0
            return max(0.0, min(1.0, c))
        ds_confidence = _safe_conf(deepseek_rebuttal.get("confidence", deepseek_analysis.get("confidence", 0.5)))
        hy_confidence = _safe_conf(hunyuan_rebuttal.get("confidence", hunyuan_analysis.get("confidence", 0.5)))

        # ★★ 2026-08-17 篮子级 AI 持仓管理：融合双脑 position_action（持仓处置建议）
        #   用户铁律：开完仓核心任务 = 维护持仓。DS/HY 已看到持仓篮并输出处置建议，
        #   此处加权融合 + 连续 2 轮防抖确认，挂到 decision.basket_action 供执行层消费。
        #   ★ 2026-08-17 修复：辩论反驳轮(rebuttal) prompt 无 position_action 字段，
        #     直接传 rebuttal 会永远归一 hold。回退规则：rebuttal 有则用 rebuttal，
        #     无则回退首轮分析(analysis) 的 position_action。
        _ds_pa_src = deepseek_rebuttal
        if not ((deepseek_rebuttal or {}).get("position_action") or {}).get("action"):
            _ds_pa_src = deepseek_analysis or {}
        _hy_pa_src = hunyuan_rebuttal
        if not ((hunyuan_rebuttal or {}).get("position_action") or {}).get("action"):
            _hy_pa_src = hunyuan_analysis or {}
        _basket_action, _basket_conf, _basket_reason = _fuse_basket_action(
            _ds_pa_src, _hy_pa_src,
            ds_confidence, hy_confidence, market_data,
        )
        # 防抖确认按调用方区分（多租户；adjudicate 无 account_id 参数，
        # 从 market_data 尽力取，取不到用全局 key——篮子确认只需主号级粒度）
        _ba_key = str((market_data or {}).get("account_id") or "primary")
        _basket_action, _basket_confirmed, _basket_confirm_note = _confirm_basket_action(
            _ba_key, _basket_action,
        )

        # 风险评分
        risk_assessment = hunyuan_analysis.get("risk_assessment", {}) or {}
        # ★ 2026-08-13 审计P2-2：混元失败/云禁用时 risk_assessment 为空 → 静默降级 risk_score=5，
        #   "极端风险强制HOLD"永不被触发。现从 market_data 的 regime 兜底推导风险分（缺失也只是默认5，不更激进）。
        if not risk_assessment:
            _rg = (market_data or {}).get("regime") or {}
            _regime_name = _rg.get("regime") if isinstance(_rg, dict) else None
            if _regime_name == "volatile":
                risk_assessment = {"risk_score": 7, "volatility_regime": "高"}
        # ★ 2026-08-13 审计P2-1：混元若返回字符串"7"，"7">=8 在 Py3 抛 TypeError → 异常冒泡被吞 → 本轮全账号不开仓（漏单）。
        try:
            risk_score = float(risk_assessment.get("risk_score", 5))
        except (TypeError, ValueError):
            risk_score = 5.0
        vol_regime = str(risk_assessment.get("volatility_regime", "正常"))
        if risk_score >= 8:
            risk_level = "extreme"
        elif risk_score >= 6:
            risk_level = "high"
        elif risk_score >= 3:
            risk_level = "medium"
        else:
            risk_level = "low"

        # 确定市场体制
        # ★ 2026-08-06 修正：体制主源改为 detect_regime（多周期融合H4+H1，价格行为灵敏），
        #   旧逻辑取 timeframes[H1/H4]["trend"]（滞后SMA20/50）→ 下跌初期误报 strong_uptrend
        #   → BUY 获体制背书 → 狂开逆势亏损单（用户实盘：趋势明显下降仍不断开BUY全亏）。
        trend_regime = "normal"
        _det = market_data.get("regime", {})
        _det_rg = _det.get("regime") if isinstance(_det, dict) else None
        if _det_rg and _det_rg != "unknown":
            _rg_map = {
                "trend_up": "uptrend", "strong_uptrend": "strong_uptrend",
                "trend_down": "downtrend", "strong_downtrend": "strong_downtrend",
                "range": "ranging", "volatile": "volatile",
            }
            trend_regime = _rg_map.get(_det_rg, "normal")
        # 兜底：detect_regime 不可用时才回退滞后 SMA（极罕见）
        if trend_regime == "normal":
            for tf_name in ["H1", "H4"]:
                tf_data = market_data.get("timeframes", {}).get(tf_name, {})
                trend = tf_data.get("trend", "")
                if trend and trend != "unknown":
                    trend_regime = trend
                    break

        market_regime = vol_regime if vol_regime in ["高波动", "极端"] else trend_regime

        # ★ 2026-08-18 打破 HY 恒 HOLD 死锁：更新混元连续 HOLD 计数（get_weights 消费）
        if hy_final == "HOLD":
            _HY_HOLD_STREAK["n"] += 1
        else:
            _HY_HOLD_STREAK["n"] = 0

        # 获取动态权重
        ds_weight, hy_weight = self.get_weights(market_regime)

        # ★ 有效云模型开关关闭时（主开关关闭 或 无可用 Key），云票权重归零，由本地时序融合票担任方向权威。
        _cloud_enabled = effective_cloud_enabled()
        if not _cloud_enabled:
            ds_weight = 0.0
            hy_weight = 0.0
            logger.info("[MetaAgent] ENABLE_CLOUD_MODELS=False，云模型权重归零，由本地时序融合票裁决")

        # ── 裁决规则优化 (R1-R4)：共识不足=不交易 ──
        # ★ 2026-08-07 重大修复：Chronos 本地时序大脑成为第三辩论角色，
        #   不再只是"质量陪审团"，而是与 DeepSeek/混元并列投票。
        #   当 Chronos 方向与云模型冲突时显著降权，同向时小幅奖励，
        #   根治「Chronos 看空但 DS/HY 看多仍盲目开仓」的失明问题。
        _mq = (market_data or {}).get("meta_quality") or {}
        chronos_dir = _normalize_decision(_mq.get("chronos_dir", "NEUTRAL"))
        chronos_unc = float(_mq.get("uncertainty") or 0.0)
        chronos_weight = float(getattr(settings, "CHRONOS_VOTE_WEIGHT", 0.25))
        chronos_reliability = max(0.1, 1.0 - chronos_unc)
        chronos_cw = chronos_weight * chronos_reliability if chronos_dir in ("BUY", "SELL") else 0.0

        # ── fusion_v2 时序融合第四票（2026-08-10）──
        # 把"第三票方向"的来源从单个 Chronos 升级为 4 模型融合票
        # （Chronos-2/TimesFM/Time-MoE/Moirai 聚合），其余裁决/共识/反向逻辑全复用。
        # legacy 模式：完全走旧逻辑（chronos_dir 来自 meta_quality），零回归。
        # ★ 多客户并行：融合票与账号无关，所有账号统一享用，各自独立执行。
        _decision_mode = str(getattr(settings, "DECISION_MODE", "legacy")).lower()
        ts_fusion_dir = "HOLD"
        ts_fusion_weight = 0.0
        ts_fusion_conf = 0.0
        ts_fusion_agree = False
        ts_fusion_hit_avg = 0.0
        ts_fusion_models = 0
        ts_fusion_note = ""
        if _decision_mode == "fusion_v2":
            try:
                from app.services.fusion_service import get_service as get_fusion
                _fv = get_fusion().get_fusion_vote()
                if _fv.available and _fv.direction in ("BUY", "SELL"):
                    # 融合票有效 → 替换第三票方向源 + 权重（Chronos 风险区间 p10/p90 仍保留用于 smart_exit）
                    ts_fusion_dir = _fv.direction
                    # 权重 = 基准权重 × 模型数/同向度缩放（2模型0.70 / 3模型0.85 / 4模型1.00，分歧再×0.85）
                    # ★ 定稿P2-3：LOCAL_WEIGHT_TUNING_ENABLED=True 时用微调权重（默认关，walk-forward后开）
                    _ts_w_base = float(getattr(settings, "TS_FUSION_VOTE_WEIGHT", 0.22))
                    if bool(getattr(settings, "LOCAL_WEIGHT_TUNING_ENABLED", False)):
                        _ts_w_base = float(getattr(settings, "TS_FUSION_VOTE_WEIGHT_TUNED", 0.26))
                    ts_fusion_weight = (_ts_w_base
                                        * float(getattr(_fv, "weight_scale", 1.0) or 1.0))
                    ts_fusion_conf = _fv.confidence
                    ts_fusion_agree = _fv.agree
                    ts_fusion_hit_avg = _fv.hit_rate_avg
                    ts_fusion_models = _fv.model_count
                    ts_fusion_note = _fv.note
                    # ★ 2026-08-18 第四处修复C：命中率过低(≤地板)的融合票不具备方向投票资格。
                    #   以 86% 置信投 BUY 却历史命中≈0% 是毒信号，会反向压死顺势单→降级 NEUTRAL 回退单 Chronos。
                    # ★ 2026-08-19 定稿P0-1 修正：单锚化时豁免地板——融合票=锚(Chronos)本身，
                    #   锚的质量已由竞技场验证(方向准确率53.5%/净点+319.4唯一正)，运行时 hit_rate
                    #   波动(参考面板未启动/统计未更新=0)不应误杀方向锚；地板仅用于多模型聚合防毒票。
                    _hit_floor = float(getattr(settings, "TS_FUSION_HIT_FLOOR", 0.45))
                    _single_anchor = bool(getattr(settings, "TS_FUSION_SINGLE_ANCHOR", True))
                    if (not _single_anchor) and ts_fusion_hit_avg < _hit_floor:
                        logger.warning(
                            f"[MetaAgent][fusion_v2] 融合票命中率{ts_fusion_hit_avg:.0%}"
                            f"<地板{_hit_floor:.0%}→弃用(NEUTRAL,回退Chronos)"
                        )
                    else:
                        chronos_dir = ts_fusion_dir
                        chronos_weight = ts_fusion_weight
                        chronos_reliability = max(0.1, _fv.confidence)
                        chronos_cw = chronos_weight * chronos_reliability
                        logger.info(
                            f"[MetaAgent][fusion_v2] {'Chronos锚' if _single_anchor else '4模型融合票'}"
                            f"={ts_fusion_dir}(w={ts_fusion_weight:.2f}"
                            f"|conf={ts_fusion_conf:.0%}|命中均={ts_fusion_hit_avg:.0%}|{ts_fusion_models}票)"
                        )
                else:
                    ts_fusion_note = _fv.note or "融合票不可用"
                    logger.warning(f"[MetaAgent][fusion_v2] 融合票不可用: {ts_fusion_note}（回退单Chronos）")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[MetaAgent][fusion_v2] 取融合票异常: {e}（回退单Chronos）")

        # ★ 2026-08-14 P0 修复：chrono_is_dir 原误定义在下方「方向状态区」(行800)，
        #   但副驾块(行767)与下方 chrono 累加(813)/active_weight(826) 均已前置引用 →
        #   运行时首次到副驾块即 NameError，adjudicate 整段中断、副驾第5路永久沉默。
        #   前移到 fusion_v2 之后(此时 chronos_dir 已含融合票覆盖值)，一处定义、全局可用。
        chrono_is_dir = chronos_dir in ("BUY", "SELL")

        # ── 视觉模型第四票（2026-08-14）：H4/M15 K线结构识别，加法增强方向准确率，非闸门 ──
        # 后台生产者线程按低频渲染图表→送视觉模型(CPU)→缓存 VisionVote；
        # 此处同步读取缓存票（零延迟）。仅把方向权重加进 decision_scores，绝不单独立闸门。
        vision_dir = "HOLD"
        vision_weight = 0.0
        vision_conf = 0.0
        vision_agree = False
        vision_h4_dir = "HOLD"
        vision_m15_dir = "HOLD"
        vision_m5_dir = "HOLD"
        vision_m5_conf = 0.0
        vision_note = ""
        # ★ 2026-08-19 修复：视觉(qwen2.5vl)是本地 Ollama 模型，本地模式也须参与投票，
        #   不应被云端闸(_cloud_enabled)挡在门外。原 2026-08-15 审计P3 修复误把本地模型当云端依赖，
        #   导致纯本机模式决策里视觉票恒为 HOLD/available=false、正确方向被丢弃。
        if getattr(settings, "VISION_VOTE_ENABLED", True):
            try:
                from app.services.vision_service import get_service as get_vision
                _vv = get_vision().get_vision_vote()
                if _vv and _vv.available and _vv.direction in ("BUY", "SELL"):
                    vision_dir = _vv.direction
                    vision_conf = _vv.confidence
                    # ★ 2026-08-19 审计P1落地：视觉置信度幻觉修正（诚实化打分·非拦截）。
                    #   实证（18:21/18:25 日志）：H4=HOLD/M15=HOLD/M5=SELL 三帧仅一帧给方向，
                    #   模型却报 95% 置信——LLM 自报置信系统性失准（FinBench/ECE 论文），
                    #   95% 直接乘权重会把幻觉放大 1.5~5 倍。
                    #   修正：内部三帧不一致(vision_agree=False)时，置信封顶到
                    #   VISION_CONF_DISAGREE_CAP(默认0.60)——只把幻觉置信压回诚实区间，
                    #   不拦截任何单（方向/权重不变，仅贡献分诚实化）。
                    if (not _vv.agree
                            and vision_conf >= float(getattr(settings, "VISION_CONF_DISAGREE_CAP", 0.60))):
                        _vc_before = vision_conf
                        vision_conf = float(getattr(settings, "VISION_CONF_DISAGREE_CAP", 0.60))
                        logger.warning(
                            f"[MetaAgent][vision] 置信幻觉修正: 三帧不一致仍报{_vc_before:.0%}"
                            f"→封顶{vision_conf:.0%}（内部H4={_vv.h4_dir}/M15={_vv.m15_dir}/M5={_vv.m5_dir}·诚实化打分非拦截）"
                        )
                    # 权重 = 基准权重 × 同向度/分歧度缩放（与融合票同构）
                    # ★ 定稿P2-3：LOCAL_WEIGHT_TUNING_ENABLED=True 时用微调权重（默认关，walk-forward后开）
                    _vw_base = float(getattr(settings, "VISION_VOTE_WEIGHT", 0.20))
                    if bool(getattr(settings, "LOCAL_WEIGHT_TUNING_ENABLED", False)):
                        _vw_base = float(getattr(settings, "VISION_VOTE_WEIGHT_TUNED", 0.32))
                    vision_weight = (_vw_base
                                    * float(getattr(_vv, "weight_scale", 1.0) or 1.0))
                    vision_agree = _vv.agree
                    vision_h4_dir = _vv.h4_dir
                    vision_m15_dir = _vv.m15_dir
                    vision_m5_dir = _vv.m5_dir
                    vision_m5_conf = _vv.m5_conf
                    vision_note = _vv.note
                    logger.info(
                        f"[MetaAgent][vision] 视觉票={vision_dir}(w={vision_weight:.2f}"
                        f"|conf={vision_conf:.0%}|H4={vision_h4_dir}/M15={vision_m15_dir}/M5={vision_m5_dir})"
                    )
                elif _vv is not None and _vv.note:
                    logger.debug(f"[MetaAgent][vision] 视觉票不可用: {_vv.note}")
            except Exception as _ve:  # noqa: BLE001
                logger.debug(f"[MetaAgent][vision] 取视觉票异常（降级忽略）: {_ve}")

        # ── Qwen3-8B 常态确认型副驾第五票（2026-08-14）：仅确认时序方向，加法提准非拦截 ──
        # 把 gpu0 上的 qwen3:8b 从「仅 L2 降级副驾」升为「常态确认型副驾」进融合投票。
        # 铁律：① 仅当 chrono_is_dir（时序有明确方向）才调 → 无方向可确认就不打扰；
        #   ② 复用 copilot_gate 三道锁（有票+非HOLD / 置信≥门槛 / 与时序同向）常态化；
        #   ③ 降权（基础权重×置信）加法并入 decision_scores，绝不自创方向/翻盘。
        #   调用经济性：按刷新周期缓存（仿视觉生产者），6 账号并行只推理 1 次。
        copilot_dir = "HOLD"
        copilot_weight = 0.0
        copilot_conf = 0.0
        copilot_agree = False
        copilot_note = ""
        # ★ 2026-08-19 修复：副驾 qwen3:8b 同为本地 Ollama 模型，不受云端闸限制（仅当时序有明确
        #   方向时才确认，避免无谓打扰）。原 _cloud_enabled 前置使本地模式副驾票恒被跳过。
        if getattr(settings, "LOCAL_COPILOT_VOTE_ENABLED", True) and chrono_is_dir:
            try:
                from app.services.local_llm_service import get_local_llm, copilot_gate
                _cv = _get_cached_copilot_vote(get_local_llm(), market_data, settings)
                if _cv is not None and _cv.decision in ("BUY", "SELL"):
                    _cg = copilot_gate(
                        _cv, chronos_dir,
                        min_confidence=float(getattr(settings, "LOCAL_COPILOT_MIN_CONFIDENCE", 0.60)),
                    )
                    if _cg.get("allow"):
                        copilot_dir = _cg["decision"]
                        copilot_conf = _cg["confidence"]
                        copilot_agree = True
                        # 降权：基础权重 × 置信（与视觉 weight_scale 同构，加法融合）
                        copilot_weight = float(getattr(settings, "LOCAL_COPILOT_VOTE_WEIGHT", 0.15))
                        copilot_note = _cg.get("reason", "")
                        logger.info(
                            f"[MetaAgent][copilot] 副驾确认票={copilot_dir}"
                            f"(贡献={copilot_weight * copilot_conf:.2f}|conf={copilot_conf:.0%}) → 进站"
                        )
                    else:
                        copilot_note = _cg.get("reason", "副驾未过锁")
                        logger.debug(f"[MetaAgent][copilot] 副驾未过三道锁: {copilot_note}")
            except Exception as _ce:  # noqa: BLE001
                logger.debug(f"[MetaAgent][copilot] 取副驾票异常（降级忽略）: {_ce}")

        decision_scores = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
        ds_cw = ds_confidence * ds_weight
        hy_cw = hy_confidence * hy_weight

        # 方向状态（已规范化）
        ds_is_dir = ds_final in ("BUY", "SELL")
        hy_is_dir = hy_final in ("BUY", "SELL")
        both_directional = ds_is_dir and hy_is_dir
        same_dir = both_directional and ds_final == hy_final          # 双模型同向共识
        three_way_consensus = same_dir and chrono_is_dir and chronos_dir == ds_final  # 三模型同向
        chronos_opposes = chrono_is_dir and ds_is_dir and hy_is_dir and chronos_dir != ds_final  # Chronos 与云共识反向
        # ★ 2026-08-18 第三处修复B：趋势明确且 Chronos/融合票反向 → 降权（提准非拦截）
        #   强跌趋势里 Chronos 短期看多(BUY) 会把 active_weight 分母撑大 → 顺势 SELL 归一化置信
        #   被压到 <0.58 → 不开单（结构性死区 cycle#4）。趋势明确时短期时序/融合反向票不配
        #   反向压制顺势单；同时降权 chronos_weight(分母) 与 chronos_cw(反向分)，让顺势单归一化回升达门槛。
        _TREND_STRONG = ("strong_downtrend", "downtrend", "strong_uptrend", "uptrend")
        # ★ 2026-08-18 第三处修复B（扩写·根治 R2 死锁）：趋势明确且「云方向模型与趋势同向、时序反向」
        #   → 降权反向时序票（提准非拦截）。覆盖两种情形：
        #   ① 双云共识(DS=HY=趋势同向) + 时序反向（原修复B）；
        #   ② 单云方向(DS=趋势同向) + HY=HOLD 分裂 + 时序反向（原漏掉的 R2 死锁根因）。
        #   实测：强跌趋势 DS=SELL+HY翻HOLD+融合BUY(命中0%)→SELL归一化36%<42%→R2死锁HOLD。降权后
        #   active_weight 分母回落，SELL 归一化回升破 lean 门槛→以小仓顺势开(提准非拦截)。
        #   不覆盖 oppose(一BUY一SELL真打架) 与 DS 逆趋势(云逆势)——那才是真冲突，仍尊重时序。
        _big_dir = ("BUY" if market_regime in ("uptrend", "strong_uptrend")
                    else "SELL" if market_regime in ("downtrend", "strong_downtrend") else "HOLD")
        _trend_aligned = (market_regime in _TREND_STRONG and ds_is_dir and ds_final == _big_dir)
        _chronos_reverse_to_trend = chrono_is_dir and chronos_dir != _big_dir
        _oppose = both_directional and ds_final != hy_final
        if (_trend_aligned and _chronos_reverse_to_trend
                and not _oppose
                and float(getattr(settings, "CHRONOS_TREND_OPPOSE_MULT", 0.25)) < 1.0):
            _co_mult = float(getattr(settings, "CHRONOS_TREND_OPPOSE_MULT", 0.25))
            chronos_weight *= _co_mult
            chronos_cw *= _co_mult
            logger.info(
                f"[MetaAgent] 趋势明确({market_regime})·时序反向降权×{_co_mult:.2f}"
                f"→w={chronos_weight:.3f}/cw={chronos_cw:.3f}(提准非拦截·不压顺势单)"
            )
        oppose = both_directional and ds_final != hy_final            # 一BUY一SELL 打架
        one_dir_one_hold = ds_is_dir != hy_is_dir                    # 一个方向一个观望
        both_hold = (not ds_is_dir) and (not hy_is_dir)              # 双观望

        if ds_is_dir:
            decision_scores[ds_final] += ds_cw
        if hy_is_dir:
            decision_scores[hy_final] += hy_cw
        if chrono_is_dir:
            decision_scores[chronos_dir] += chronos_cw
        # ★ 视觉第四票：加法加权（非闸门）。仅当视觉给出明确方向才计入，
        #   HOLD 不贡献；并入 active_weight 保证 final_confidence 归一化正确。
        vision_is_dir = vision_dir in ("BUY", "SELL")
        # ★ 2026-08-15 第三优先·视觉门控注意力（对标 FinGPT-Agent gated attention / GS-FUSE / CGCMA 调研）：
        #   原固定 VISION_VOTE_WEIGHT 加法 → 升级为动态门控：视觉贡献按「与其他模型共识一致性 + 市场上下文」
        #   动态开关；一致→打开(→1.0)，矛盾→优雅降级(→VISION_GATE_MIN)，绝不HOLD（提准非拦截）。
        #   原理：GS-FUSE「只在视觉对价格有增量价值时才开门」+ CGCMA「门在矛盾/陈旧时自动关闭、优雅退化为单模态」。
        #   ★ 2026-08-16 门控放宽（7b 升级配套）：MID 0.70→0.80、MIN 0.35→0.55（config 为权威源，此处仅兜底）。
        if getattr(settings, "VISION_GATE_ENABLED", True) and vision_is_dir:
            _consensus_dir = ds_final if ds_final == hy_final else (chronos_dir if chrono_is_dir else ds_final)
            _agree_consensus = (vision_dir == _consensus_dir)
            if _agree_consensus and vision_agree:
                _vision_gate = 1.0
            elif _agree_consensus or vision_agree:
                _vision_gate = float(getattr(settings, "VISION_GATE_MID", 0.80))
            else:
                _vision_gate = float(getattr(settings, "VISION_GATE_MIN", 0.55))
            _reg = str((market_data or {}).get("regime") or "").lower()
            if "chop" in _reg or "range" in _reg or "震荡" in _reg:
                _vision_gate *= float(getattr(settings, "VISION_GATE_CHOP_MULT", 0.85))
            vision_weight = vision_weight * _vision_gate
            logger.info(
                f"[MetaAgent][vision-gate] 门控={_vision_gate:.2f}"
                f"(共识一致={_agree_consensus}/视觉内部一致={vision_agree}/体制={_reg})→有效w={vision_weight:.2f}"
            )
        if vision_is_dir:
            decision_scores[vision_dir] += vision_weight * vision_conf
        # ★ Qwen3-8B 常态确认型副驾第五票：加法加权（非闸门）。仅与有效时序同向+过锁才计入，
        #   HOLD 不贡献；并入 active_weight 保证 final_confidence 归一化正确。
        copilot_is_dir = copilot_dir in ("BUY", "SELL")
        if copilot_is_dir:
            decision_scores[copilot_dir] += copilot_weight * copilot_conf

        active_weight = (ds_weight if ds_is_dir else 0) + (hy_weight if hy_is_dir else 0) + (chronos_weight if chrono_is_dir else 0) + (vision_weight if vision_is_dir else 0) + (copilot_weight if copilot_is_dir else 0)

        # ★ 2026-08-06 新方案：两模型反向 → 加权合并 + 分歧折扣（非硬HOLD）
        #   方向听"加权得分高"的一方；置信 = 胜者原始置信 × DISAGREE_PENALTY。
        #   配合 Fix1(HOLD中性) + Fix2(权重封顶[0.35,0.65])，HY 已是真平等对手，
        #   此处加权合并才真正有意义：听更可信模型的方向，但市场信号分裂→收着干。
        #   执行器另有 min_confidence(默认0.58) 门槛：胜者原始置信须≥0.58/0.75≈0.77
        #   才会实际开单；否则折扣后不过审→等同HOLD（安全兜底）。
        # ★ 云模型关闭（#416 修复）：双云禁用(HOLD,权重0)，本地时序融合票为方向权威。
        #   仅在「关云」时介入，云开启模式的既有裁决逻辑完全不动。
        if (not _cloud_enabled) and (chrono_is_dir or vision_is_dir or copilot_is_dir):
            # ★ 2026-08-19 修复：本地模式方向权威 = 本地模型加权共识（视觉/时序/副驾均为本地
            #   Ollama 模型，不依赖云端）。旧逻辑 `final_decision = chronos_dir` 让 Chronos 独断，
            #   而 Chronos-2 定位「风险区间估计、非方向终审」，在趋势中其 P50 均值回归会反向喊单，
            #   被 A门 100% 拦死→死锁 HOLD。现改为本地模型加权共识 + 体制趋势基线：
            #   体制明确趋势时给顺势方加成（趋势即方向权威），让顺势单得以开出（提准非拦截）。
            _local_scores = {"BUY": 0.0, "SELL": 0.0}
            if chrono_is_dir:
                _local_scores[chronos_dir] += chronos_cw
            if vision_is_dir:
                _local_scores[vision_dir] += vision_weight * vision_conf
            if copilot_is_dir:
                _local_scores[copilot_dir] += copilot_weight * copilot_conf
            # 体制方向基线：趋势明确时给顺势方加成（强跌→SELL 基线），逆势方不加分。
            _reg_dir = ("BUY" if market_regime in ("uptrend", "strong_uptrend")
                        else "SELL" if market_regime in ("downtrend", "strong_downtrend") else None)
            if _reg_dir:
                _local_scores[_reg_dir] += float(getattr(settings, "REGIME_LOCAL_BASE_WEIGHT", 0.20))
            _local_best = max(_local_scores, key=_local_scores.get)
            _local_sum = sum(_local_scores.values())
            # ★ 2026-08-19 定稿P1-1：本地共识观测字段（置信度=份额归一化的虚高隐患观测）。
            #   final_confidence=胜者得分/总得分 是相对份额非绝对可信度——票少同向时虚高
            #   （如视觉0.18+体制0.20 两票同向即 100%）。先落日志/brain_audit 积累分布，
            #   walk-forward 后再决定折价阈值（_local_sum<X→×Y），不做拍脑袋。
            _n_votes = int(bool(chrono_is_dir)) + int(bool(vision_is_dir)) + int(bool(copilot_is_dir)) + int(_reg_dir is not None)
            if _local_sum > 0 and _local_scores[_local_best] > 0:
                final_decision = _local_best
                final_confidence = _local_scores[_local_best] / _local_sum
                # ★ 定稿P2-2（默认关闭，walk-forward 后开）：份额归一化置信虚高折价。
                #   票少同向时 final_confidence 虚高（相对份额≠绝对可信度），
                #   开启后 _local_sum < 阈值 → 置信 × 折价因子，弱共识收着干（提准非拦截）。
                if bool(getattr(settings, "LOCAL_CONF_DISCOUNT_ENABLED", False)):
                    _disc_sum = float(getattr(settings, "LOCAL_CONF_DISCOUNT_SUM", 0.35))
                    _disc_f = float(getattr(settings, "LOCAL_CONF_DISCOUNT_FACTOR", 0.85))
                    if _local_sum < _disc_sum:
                        final_confidence = final_confidence * _disc_f
                        logger.info(
                            f"[MetaAgent] 本地共识置信折价: local_sum={_local_sum:.3f}<{_disc_sum:.2f}"
                            f"→×{_disc_f:.2f}→{final_confidence:.0%}"
                        )
                logger.info(
                    f"[MetaAgent] 本地加权共识裁决→{final_decision} 置信{final_confidence:.0%} "
                    f"(视觉={vision_dir if vision_is_dir else 'HOLD'} 时序={chronos_dir} "
                    f"副驾={copilot_dir} 体制={_reg_dir}) "
                    f"[观测] local_sum={_local_sum:.3f} n_votes={_n_votes}"
                )
            else:
                final_decision = "HOLD"
                final_confidence = 0.0
        elif oppose:
            final_decision = max(decision_scores, key=decision_scores.get)
            _winner_raw = ds_confidence if final_decision == ds_final else hy_confidence
            DISAGREE_PENALTY = float(getattr(settings, "DISAGREE_PENALTY", 0.75))
            final_confidence = _winner_raw * DISAGREE_PENALTY
            logger.warning(
                f"[MetaAgent] 裁决:两模型反向→加权合并(折扣{DISAGREE_PENALTY:.0%}) "
                f"→{final_decision} 置信{final_confidence:.0%} "
                f"DS={ds_final}({ds_confidence:.0%}|w={ds_weight:.2f}) "
                f"HY={hy_final}({hy_confidence:.0%}|w={hy_weight:.2f}) "
                f"Chronos={chronos_dir}(w={chronos_weight:.2f})"
            )
        # R3：双观望 → 强制 HOLD（不确定性高，不交易）
        elif both_hold:
            final_decision = "HOLD"
            final_confidence = (ds_confidence + hy_confidence) / 2
            logger.info(
                f"[MetaAgent] 裁决:R3 双模型观望→HOLD(不开单) "
                f"DS={ds_confidence:.0%} HY={hy_confidence:.0%} "
                f"Chronos={chronos_dir}"
            )
        # 共识同向 → 加权融合（共识奖励）+ 归一化
        elif same_dir:
            if active_weight > 0:
                if three_way_consensus:
                    decision_scores[ds_final] *= 1.15  # 三模型同向强共识奖励 +15%
                else:
                    decision_scores[ds_final] *= 1.1  # 双云模型共识奖励 +10%
            final_decision = max(decision_scores, key=decision_scores.get)
            final_confidence = decision_scores[final_decision] / active_weight if active_weight > 0 else 0.0
        # R2：单模型方向 + 另一观望 → 分裂决策，须达到更高门槛(0.85)才允许开单
        elif one_dir_one_hold:
            final_decision = max(decision_scores, key=decision_scores.get)
            norm = decision_scores[final_decision] / active_weight if active_weight > 0 else 0.0
            _r2_note = ""  # R2 短周期备注初始化（须在任何 += 之前）
            # ★★ 2026-08-18 第五处修复B：趋势背书加成（提准非拦截）★★
            #   趋势明确(强跌/跌/强涨/涨)且持方向方与趋势同向、另一模型沉默(HOLD) →
            #   短周期反弹噪音不应把顺势单压到死锁。给 norm 额外加成(趋势即方向权威)，
            #   让顺势单以(更)小手数开出，符合用户"趋势明确多开顺势单赚钱"铁律。
            #   不覆盖逆势单(B门/大周期过滤器已硬拦)与双模型共识(走 same_dir 分支)。
            _tb_regimes = ("strong_downtrend", "downtrend", "strong_uptrend", "uptrend")
            _tb_big = ("BUY" if market_regime in ("uptrend", "strong_uptrend")
                       else "SELL" if market_regime in ("downtrend", "strong_downtrend") else "HOLD")
            _tb_holder = ds_final if ds_is_dir else (hy_final if hy_is_dir else "HOLD")
            if (market_regime in _tb_regimes and _tb_holder in ("BUY", "SELL")
                    and _tb_holder == _tb_big and not both_directional):
                _tb_bonus = float(getattr(settings, "TREND_BACKING_BONUS", 0.10))
                norm += _tb_bonus
                _r2_note += f"趋势背书+{_tb_bonus:.0%}; "
                logger.info(f"[MetaAgent] R2趋势背书加成: 趋势{market_regime}·顺势{_tb_holder}+{_tb_bonus:.0%}→norm={norm:.0%}")
            # ★★ 2026-08-17 用户理念 P0 修复：R2 分裂「短周期加成」★★
            #   实测死区（cycle#4）：DS:SELL 55% + HY:HOLD + Chronos 反向 BUY(w=0.19)
            #   → active_weight 被反向票撑大 → norm=0.308/0.75=41% < 42% lean 下限
            #   → SELL 被否决（L1172 HOLD 0.0）。而 M15 动量加成(L2001)/弱锚制衡(L1290)
            #   全在 R2 之后 → HOLD 后不执行 → 结构性死区，用户「M5 盘面跌了就该动」
            #   理念无法落地。修复：R2 内部先做短周期加成（提准非拦截·加法）——
            #   ① 视觉 M5 方向与持方向一致且 conf≥0.8 → +0.05（用户盯盘信号）；
            #   ② M15 最近 5 根连跌/连涨与持方向一致 → +0.06。
            if final_decision in ("BUY", "SELL"):
                # ★★ 2026-08-17 P0 修复：短周期反向惩罚（对称于同向加成）★★
                #   事故（20:36）：DS 需求区抄底 BUY 58% + Chronos BUY，但视觉 M5=SELL 95%
                #   （明确看空）+ entry_dir_5m=down（结构算法：5m 下跌结构）→ 两重短周期
                #   强反向信号被系统无视，BUY 照常放行 → 接飞刀大仓 -331。
                #   原逻辑只做「短周期同向加成」（视觉 M5 同向 +0.05 / M15 连涨连跌 +0.06），
                #   反向信号零处理 → 逆短周期盘面的单子永远不被惩罚。
                #   修复（提准非拦截·对称设计）：短周期明确反向时降权——
                #   ① 视觉 M5 与持方向相反且 conf≥0.8 → -0.06（强反向背书，用户盯盘信号）；
                #   ② entry_dir_5m（结构算法输出）与持方向相反 → -0.05（下跌结构不做多/上涨结构不做空）；
                #   ③ M15 最近 5 根连涨/连跌与持方向相反 → -0.06（动量反向）。
                #   只降权不硬拦（不砍交易笔数）：置信仍过门槛照常开，但接飞刀单被压到门槛下。
                _r2_pen = 0.0
                if vision_m5_dir in ("BUY", "SELL") and vision_m5_dir != final_decision and vision_m5_conf >= 0.8:
                    _r2_pen += float(getattr(settings, "M5_VISION_REVERSE_PENALTY", 0.06))
                    _r2_note += f"视觉M5反向({vision_m5_dir} {vision_m5_conf:.0%})-{_r2_pen:.0%}; "
                # entry_dir_5m 是修复后的真实 SMC 结构方向（HH/HL/LH/LL），比视觉更确定
                # ★ 2026-08-17 20:48 修复方向映射 bug：_ed5 是 'up'/'down'，final_decision 是
                #   'BUY'/'SELL'，直接字符串比较永远不相等 → 顺势单(SELL+down/BUY+up)也被误罚。
                #   必须先映射 'up'→BUY、'down'→SELL 再比方向。
                try:
                    _rg5 = (market_data or {}).get("regime") or {}
                    _ed5 = str(_rg5.get("entry_dir_5m", "neutral") or "neutral").lower()
                    _ed5_dir = {"up": "BUY", "down": "SELL"}.get(_ed5)
                    if _ed5_dir and _ed5_dir != final_decision:
                        _r2_pen += float(getattr(settings, "STRUCT_DIR_REVERSE_PENALTY", 0.05))
                        _r2_note += f"5m结构{_ed5}反向-0.05; "
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _tf = (market_data or {}).get("timeframes") or {}
                    _m15_raw = _tf.get("M15")
                    _m15_bars = (_m15_raw.get("closes") if isinstance(_m15_raw, dict) else None) or []
                    if not isinstance(_m15_bars, (list, tuple)):
                        _m15_bars = []
                    _closes = [float(b) for b in _m15_bars if isinstance(b, (int, float))]
                    if len(_closes) >= 5:
                        _last5 = _closes[-5:]
                        _down5 = all(_last5[i + 1] < _last5[i] for i in range(4))
                        _up5 = all(_last5[i + 1] > _last5[i] for i in range(4))
                        _rev = (_down5 and final_decision == "BUY") or (_up5 and final_decision == "SELL")
                        if _rev:
                            _r2_pen += float(getattr(settings, "M15_MOMENTUM_REVERSE_PENALTY", 0.06))
                            _r2_note += f"M15{'5连跌' if _down5 else '5连涨'}反向-0.06; "
                except Exception:  # noqa: BLE001
                    pass
                if _r2_pen > 0:
                    norm -= _r2_pen
                    _r2_note += f"norm→{norm:.0%}"
                    logger.warning(
                        f"[MetaAgent] R2短周期反向惩罚: {_r2_note}（逆短周期盘面降权，提准非拦截）"
                    )
                # 同向加成（原有逻辑）
                if vision_m5_dir == final_decision and vision_m5_conf >= 0.8:
                    _mb5 = float(getattr(settings, "M5_VISION_BONUS", 0.05))
                    norm += _mb5
                    _r2_note += f"视觉M5({vision_m5_dir} {vision_m5_conf:.0%})背书+{_mb5:.0%}; "
                try:
                    _tf = (market_data or {}).get("timeframes") or {}
                    _m15_raw = _tf.get("M15")
                    _m15_bars = (_m15_raw.get("closes") if isinstance(_m15_raw, dict) else None) or []
                    if not isinstance(_m15_bars, (list, tuple)):
                        _m15_bars = []
                    _closes = [float(b) for b in _m15_bars if isinstance(b, (int, float))]
                    if len(_closes) >= 5:
                        _last5 = _closes[-5:]
                        _down5 = all(_last5[i + 1] < _last5[i] for i in range(4))
                        _up5 = all(_last5[i + 1] > _last5[i] for i in range(4))
                        _match = (_down5 and final_decision == "SELL") or (_up5 and final_decision == "BUY")
                        if _match:
                            _mb = float(getattr(settings, "M15_MOMENTUM_BONUS", 0.06))
                            norm += _mb
                            _r2_note += f"M15{'5连跌' if _down5 else '5连涨'}同向+{_mb:.0%}; "
                except Exception:  # noqa: BLE001
                    pass
                # ★★ 2026-08-17 结构突破 BOS 加成（趋势启动识别·提准非拦截）★★
                #   调研（≥3源交叉验证）：BOS=收盘突破摆动点且结构延续（趋势确认），
                #   是 SMC/ICT 的方向过滤器。M15 BOS 方向与决策同向时给加成——
                #   高周期一致（htf_aligned，SMC 多周期硬规则）= 强信号 +0.05；
                #   仅 M15 突破未获高周期确认 = 弱背书 +0.03。
                #   反向不惩罚（CHoCH/逆势场景由 DS 按 prompt 规则自行辩护，避免过度机制化）。
                try:
                    _sb = (market_data or {}).get("structure_break") or {}
                    _m15b = _sb.get("m15") or {}
                    _bos = _m15b.get("bos")
                    if _bos in ("up", "down"):
                        _bos_dir = "BUY" if _bos == "up" else "SELL"
                        if _bos_dir == final_decision:
                            _bbonus = 0.05 if _sb.get("htf_aligned") else 0.03
                            norm += _bbonus
                            _r2_note += (f"M15 BOS({_bos}"
                                         f"{'·高周期一致' if _sb.get('htf_aligned') else '·未确认'})"
                                         f"+{_bbonus:.0%}; ")
                except Exception:  # noqa: BLE001
                    pass
                if _r2_note:
                    logger.info(f"[MetaAgent] R2短周期加成: {_r2_note}norm→{norm:.0%}")
            # ★ 2026-08-15 复检P0修复：B门分支此前从未给 final_confidence 赋值
            #   （首次赋值点 L914/923/934/948 全在互斥分支），当 _b_counter 为真时
            #   L972 读取未绑定变量 → UnboundLocalError 崩掉整条 adjudicate——
            #   「逆势方向+另一观望+Chronos 未背书」恰是最该保守的市场场景，反而断链漏单。
            #   先赋 norm 兜底（B门降权/门槛逻辑随后覆盖）。
            final_confidence = norm
            # ★★ 2026-08-11 B门：HY HOLD 刹车 ★★
            #   双脑不一致（一方向一观望）且持方向方逆大周期趋势 → 直接 HOLD。
            #   根因（23:35 复盘）：DS=SELL 58% + HY=HOLD 25% + 大周期 bullish →
            #   旧逻辑 norm(0.70)≥0.55 放行开单，混元持续 HOLD 的谨慎被吃掉。
            #   大周期方向（detect_regime 融合 H4+H1）才是方向权威，小周期分歧不配开反向单。
            #   豁免：Chronos/融合票与持方向方同向（本地时序背书）→ 仍按原逻辑放行（提准非拦截）。
            _b_hy_hold = ds_is_dir != hy_is_dir and not both_hold
            _b_dir_holder = ds_final if ds_is_dir else (hy_final if hy_is_dir else "HOLD")
            _b_big_dir = "HOLD"
            if trend_regime in ("uptrend", "strong_uptrend"):
                _b_big_dir = "BUY"
            elif trend_regime in ("downtrend", "strong_downtrend"):
                _b_big_dir = "SELL"
            _b_counter = (
                _b_hy_hold and _b_dir_holder in ("BUY", "SELL") and _b_big_dir in ("BUY", "SELL")
                and _b_dir_holder != _b_big_dir
            )
            _b_ts_backed = chronos_dir == _b_dir_holder
            if _b_counter and not _b_ts_backed:
                # ★ 2026-08-17 修复：B 门注释承诺"双脑分歧 + 逆大周期 → 直接 HOLD"，
                #   实现却是"降权0.88保留方向"（置信仍可过执行门槛照常开单）。
                #   恢复硬 HOLD：混元持续观望的谨慎必须被尊重，逆大周期分裂单不开。
                logger.warning(
                    f"[MetaAgent] B门·HY刹车(硬HOLD): 双脑分歧({ds_final} {ds_confidence:.0%}/"
                    f"{hy_final} {hy_confidence:.0%}) 且方向{_b_dir_holder}逆大周期{_b_big_dir}"
                    f"→HOLD(提准非拦截, Chronos背书={_b_ts_backed})"
                )
                final_decision = "HOLD"
                final_confidence = 0.0
                _gate_count("B门")
            elif norm >= SPLIT_DECISION_MIN_CONF:
                final_confidence = norm
            elif norm >= LEAN_MIN_CONF:
                # ★ 2026-08-15 倾斜单(lean)：方向明确但共识不足 → 放行，手数随置信缩放。
                #   提准非拦截：不硬拦分歧信号，而是用更小手数管风险（compute_intelligent_size
                #   已按 signal_confidence 缩放）。既保"多交易多赚钱"，又不接纯噪声飞刀。
                final_decision = max(decision_scores, key=decision_scores.get)
                final_confidence = norm
                logger.info(
                    f"[MetaAgent] 裁决:R2 倾斜单(lean) norm={norm:.0%}≥{LEAN_MIN_CONF:.0%} "
                    f"→{final_decision} 放行(手数随置信缩放, 提准非拦截)"
                )
            else:
                logger.warning(
                    f"[MetaAgent] 裁决:R2 分裂决策(一方向一观望)置信{norm:.0%}"
                    f"<{LEAN_MIN_CONF:.0%}→HOLD({final_decision} 被否决) "
                    f"Chronos={chronos_dir}"
                )
                final_decision = "HOLD"
                final_confidence = 0.0
        # 兜底（异常分支，如双模型均返回无法识别）→ 不开单
        else:
            final_decision = "HOLD"
            final_confidence = 0.0

        # 极端风险强制HOLD
        if risk_level == "extreme" and final_decision != "HOLD":
            _pen = final_confidence * float(getattr(settings, "EXTREME_RISK_PENALTY", 0.60))
            logger.warning(
                f"[MetaAgent] 极端风险(软化): 风险极高→降权→{_pen:.0%}(保留方向·强惩罚非硬拦截)"
            )
            final_confidence = _pen
            _gate_count("极端风险门")

        # ★★ 2026-08-11 A门：大周期过滤器（H4/H1 趋势主导方向，杜绝小周期骗反向信号）★★
        #   根因（用户实盘复盘 23:35）：DeepSeek 观察到 H4/H1 bullish，却用 M15 反弹受阻 +
        #   CVD 背离判反转开 SELL → 价格 1 小时涨 14 美元，浮亏 -1345。大周期该主导方向，
        #   小周期只定入场。
        #   设计（海外调研：quantum-algo "4H sets directional bias, filters ~35-40% counter-trend
        #   losers"；LLM-TradeBot RegimeDetector；LARSA regime-weighted ensemble）：
        #   - 大周期明确上涨(uptrend/strong_uptrend) → 禁止 SELL
        #   - 大周期明确下跌(downtrend/strong_downtrend) → 禁止 BUY
        #   - 豁免1：三模型反向共振（DS+HY+Chronos 全同向反向）→ 极强反转共识放行
        #   - 豁免2：趋势末端(at_stale_top/bottom) → 接飞刀反转是合理抓顶/抓底场景
        #   这是「硬门」不是「提准」：逆大周期的方向直接不开，防止小周期噪音反复骗信号。
        _big_trend_dir = "HOLD"
        if trend_regime in ("uptrend", "strong_uptrend"):
            _big_trend_dir = "BUY"
        elif trend_regime in ("downtrend", "strong_downtrend"):
            _big_trend_dir = "SELL"
        if final_decision in ("BUY", "SELL") and _big_trend_dir in ("BUY", "SELL"):
            _counter_trend = (
                (_big_trend_dir == "BUY" and final_decision == "SELL")
                or (_big_trend_dir == "SELL" and final_decision == "BUY")
            )
            if _counter_trend:
                _rg_gate = (market_data or {}).get("regime") or {}
                _at_end = bool(_rg_gate.get("at_stale_top", False)) or bool(_rg_gate.get("at_stale_bottom", False))
                _ext_z = float(_rg_gate.get("extension_z", 0.0) or 0.0)
                # ★ 2026-08-12 盯盘根因修复（提准非拦截·对齐用户 2026-07-21 方法论）：
                #   原豁免「三模型共振即放行逆势单」过宽——今日实盘多笔 SELL 逆上涨趋势
                #   （#380501075/-703 #380548357/-644 #380641064/-584 等）正是 DS+HY+Chronos 全 SELL
                #   共振、被 A门放行，结果趋势延续上涨、巨亏。三模型可因"RSI超买/阻力"在健康
                #   上涨中齐卖，属「摸顶接飞刀」而非真反转。
                #   用户方法论：区分健康趋势与末端接飞刀须靠「价格延伸度 Z」而非指标健康度
                #   （Z>1.5 判趋势拥挤/衰竭，convextrade；>2.5×ATR 为统计罕见延伸）。
                #   故逆势豁免新增硬门槛：须价格已统计性延伸(|z|≥1.5，趋势拥挤/衰竭)或处趋势末端，
                #   否则逆势单一律 HOLD——即"无延伸度支撑的多模型共识"不再放行逆势（提准非拦截）。
                #   阈值 EXTENSION_REVERSE_Z 可随回测调参；方向无关（SELL/BUY 对称）。
                EXTENSION_REVERSE_Z = float(getattr(settings, "EXTENSION_REVERSE_Z", 1.5))
                _genuine_extension = (
                    (final_decision == "SELL" and _ext_z >= EXTENSION_REVERSE_Z)
                    or (final_decision == "BUY" and _ext_z <= -EXTENSION_REVERSE_Z)
                )
                # 逆势放行三选一：①三模型共振+Chronos同向+价格已统计性延伸 ②趋势末端(接飞刀合理) ③二者皆备
                _strong_reverse = (
                    three_way_consensus
                    and chronos_dir == final_decision
                    and (_genuine_extension or _at_end)
                )
                if not _strong_reverse:
                    # ★ 2026-08-17 修复：A 门注释承诺"硬门：逆大周期直接不开"，实现却是
                    #   "降权0.80保留方向"——置信仍可过 0.58 执行门槛照常开单（2026-08-11
                    #   用户实盘 -1345 的教训场景），且测试契约断言 HOLD。恢复硬拦截：
                    #   无延伸度支撑/非趋势末端的逆势单一律 HOLD（大周期主导方向）。
                    logger.warning(
                        f"[MetaAgent] A门·大周期过滤器(硬拦): 大周期={_big_trend_dir}({trend_regime}) "
                        f"逆势{final_decision}→HOLD(无延伸度支撑·提准非拦截, "
                        f"三模型共振={_strong_reverse},趋势末端={_at_end})"
                    )
                    final_decision = "HOLD"
                    final_confidence = 0.0
                    _gate_count("A门")

        # ★★ 2026-08-11 B门：HY HOLD 刹车（双脑不一致 + 大周期反向 → 直接 HOLD）★★
        #   根因（同 23:35 复盘）：DS=SELL 58% + HY=HOLD 25% + 大周期 bullish → 旧逻辑走
        #   one_dir_one_hold 且 norm(0.70)≥SPLIT_DECISION_MIN_CONF(0.55) → 放行开单。
        #   混元 23 分钟持续 HOLD 的谨慎信号被吃掉——双脑不一致且方向逆大周期时该保守。
        #   位置：R2 分裂决策分支内（final_decision 已算出但未最终返回前）。
        #   B门只拦「持方向方逆大周期」的单，顺势/大周期中性不受影响（提准非拦截）。

        # ── 反转哨兵门（Reversal Sentinel Gate，2026-08-05）──
        # 第3辩论角色：趋势末端反转制衡。当哨兵判定「山顶接飞刀」(REVERSE_SELL)
        # 而裁决方向=BUY，强制 HOLD（除非裁决置信极高≥0.92 且哨兵置信偏低）。
        # 只杀明显接飞刀单，不削减弱矛盾/正常顺势单 → 符合「提准非拦截」。
        _sentinel = (market_data or {}).get("reversal_sentinel") or {}
        _sig = _sentinel.get("signal", "NONE")
        _sconf = float(_sentinel.get("confidence", 0) or 0)
        if final_decision in ("BUY", "SELL"):
            _block = False
            if _sig == "REVERSE_SELL" and final_decision == "BUY":
                _block = final_confidence < 0.92
            elif _sig == "REVERSE_BUY" and final_decision == "SELL":
                _block = final_confidence < 0.92
            if _block:
                _pen = final_confidence * float(getattr(settings, "SENTINEL_GATE_PENALTY", 0.85))
                logger.warning(
                    f"[MetaAgent] 反转哨兵门(软化): 哨兵{_sig}(置信{_sconf:.0%})→降权→{_pen:.0%} "
                    f"(裁决置信{final_confidence:.0%}<0.92, 防趋势末端接飞刀·保留方向·提准非拦截)"
                )
                final_confidence = _pen
                _gate_count("反转哨兵门")

        # ── Chronos 一致性调节（本地时序大脑制衡云模型）──
        #   三模型同向 → 小幅奖励；Chronos 与裁决反向 → 显著降权（提准非拦截）。
        if final_decision in ("BUY", "SELL"):
            if three_way_consensus:
                _bonus = float(getattr(settings, "CHRONOS_AGREE_BONUS", 1.05))
                final_confidence = min(0.98, final_confidence * _bonus)
                logger.info(
                    f"[MetaAgent] Chronos三向共识奖励: 置信{final_confidence/_bonus:.0%}→{final_confidence:.0%}"
                )
            elif chronos_opposes:
                _pen = float(getattr(settings, "CHRONOS_OPPOSE_PENALTY", 0.85))
                # ★ 2026-08-17 弱锚门槛（用户"理念不符"质疑的技术根因）：
                #   弱锚（reliability<0.25）反向时制衡减半（0.85→0.93）——
                #   置信极低的锚不应享有完整反向否决权。实测：云双脑+用户一致看跌
                #   （cycle#6 DS:SELL）时，Chronos 弱 BUY(conf≈0.21) 反向压制 →
                #   SELL 置信 0.37 < 0.42 → 顺势空/平仓开不了，系统按兵不动。
                #   提准非拦截：只减反向压制力度，不翻向、不硬 HOLD。
                if chronos_reliability < 0.25:
                    _pen = float(getattr(settings, "CHRONOS_WEAK_OPPOSE_PENALTY", 0.93))
                    logger.warning(
                        f"[MetaAgent] Chronos弱锚反向制衡减半: reliability={chronos_reliability:.2f}<0.25 "
                        f"→ 惩罚{_pen:.2f}（弱锚不享强否决）"
                    )
                _before = final_confidence
                final_confidence = final_confidence * _pen
                logger.warning(
                    f"[MetaAgent] Chronos反向制衡: 云模型={final_decision} 但本地时序={chronos_dir} "
                    f"置信{_before:.0%}→{final_confidence:.0%}(提准非拦截)"
                )

        # ── 4H 方向偏置权重调节（★海外调研：regime调权重不拦截，代替旧体制门）──
        # 调研支撑（≥3源）：
        #   - quantum-algo.com: "4H sets the directional bias, filtering out ~35-40% counter-trend losers"
        #   - LLM-TradeBot: RegimeDetector 识别 Trending/Choppy → 动态调信号权重
        #   - LARSA: ensemble weights dynamically shifted based on regime (0.3/0.4/0.5 multiplier)
        # 设计：4H 偏置方向与决策同向 → 顺势+权重；反向 → 逆势-权重（软惩罚非拦截）；
        #      区间震荡 → 中性不加权（均值回归策略与趋势跟随策略机会均等）。
        if final_decision in ("BUY", "SELL"):
            _rg = (market_data or {}).get("regime") or {}
            _bias_4h = str(_rg.get("direction_bias", "neutral") or "neutral").lower()
            _at_top = bool(_rg.get("at_stale_top", False))
            _at_bottom = bool(_rg.get("at_stale_bottom", False))
            # ★ 2026-08-06 紧急修复：4H 偏置加成必须和价格位置一致。
            #   当 H4 偏置 up 但价格已跌破 H1 MA20（extension_z < 0），
            #   说明长周期偏置已滞后/失效，若继续给 BUY 加成会导致逆势抄底。
            #   只有当 extension_z 与偏置同向（同号）时才给顺势加成；反向则视为失效并惩罚。
            _ext_z = float(_rg.get("extension_z", 0.0) or 0.0)
            # ★ 2026-08-13 根因修复（山底接飞刀 / 山顶追多 · 滞后指标反向问题）：
            #   旧逻辑：4H偏置 down + ext_z<=0 即"valid"→SELL 顺势+10% 加成；up + ext_z>=0→BUY 加成。
            #   等于"价格越偏离均值(延伸越远)顺势加成越高"——正是滞后指标在
            #   山底疯狂 sell / 山顶疯狂 buy 的根源（用户实盘：山底开sell、山顶不开sell）。
            #   正确逻辑：真·顺势中段 ext_z 在中位附近(|z|∈[0,1.5))才加成；
            #   过度延伸(|z|≥1.5=趋势拥挤/衰竭, convextrade；≥2.5=统计罕见延伸,
            #   回归概率骤升, 用户2026-07-21方法论)应强降权而非加成 → 提准非拦截。
            _bias_valid = False
            if _bias_4h == "up" and 0 <= _ext_z < 1.5:
                _bias_valid = True
            elif _bias_4h == "down" and -1.5 < _ext_z <= 0:
                _bias_valid = True
            elif _bias_4h == "neutral":
                _bias_valid = True  # neutral 无加成也无惩罚

            if final_decision == "BUY":
                if _bias_4h == "up" and _bias_valid:
                    final_confidence = min(0.98, final_confidence * 1.10)
                    logger.info(f"[MetaAgent] 4H偏置=up顺势BUY(中段延伸Z={_ext_z:.2f}): 置信{final_confidence/1.10:.0%}→{final_confidence:.0%}")
                elif _bias_4h == "down" and not _at_bottom:
                    _pen = final_confidence * 0.88
                    logger.info(f"[MetaAgent] 4H偏置=down逆势BUY(非谷底): 置信{final_confidence:.0%}→{_pen:.0%}")
                    final_confidence = _pen
                elif _bias_4h == "up" and _ext_z >= 1.5:
                    # ★ 根因修复：上涨已过度延伸(山顶追多=接飞刀) → 强惩罚不加成
                    _pen = final_confidence * (0.70 if _ext_z >= 2.5 else 0.80)
                    logger.warning(
                        f"[MetaAgent] 4H偏置=up但价格已过度延伸(延伸Z={_ext_z:.2f}>=1.5)="
                        f"山顶追多,BUY降权: 置信{final_confidence:.0%}→{_pen:.0%}(提准非拦截)"
                    )
                    final_confidence = _pen
                elif _bias_4h == "up" and not _bias_valid:
                    # 偏置失效：价格已显著跌破均值，仍喊 BUY → 额外惩罚
                    _pen = final_confidence * 0.85
                    logger.warning(f"[MetaAgent] 4H偏置=up但价格跌破均值(延伸Z={_ext_z:.2f})，BUY偏置失效: 置信{final_confidence:.0%}→{_pen:.0%}")
                    final_confidence = _pen
                elif _bias_4h == "neutral":
                    pass
            else:  # SELL
                if _bias_4h == "down" and _bias_valid:
                    final_confidence = min(0.98, final_confidence * 1.10)
                    logger.info(f"[MetaAgent] 4H偏置=down顺势SELL(中段延伸Z={_ext_z:.2f}): 置信{final_confidence/1.10:.0%}→{final_confidence:.0%}")
                elif _bias_4h == "up" and not _at_top:
                    _pen = final_confidence * 0.88
                    logger.info(f"[MetaAgent] 4H偏置=up逆势SELL(非山顶): 置信{final_confidence:.0%}→{_pen:.0%}")
                    final_confidence = _pen
                elif _bias_4h == "down" and _ext_z <= -1.5:
                    # ★ 根因修复：下跌已过度延伸(山底接飞刀=卖在最低) → 强惩罚不加成
                    _pen = final_confidence * (0.70 if _ext_z <= -2.5 else 0.80)
                    logger.warning(
                        f"[MetaAgent] 4H偏置=down但价格已过度延伸(延伸Z={_ext_z:.2f}<=-1.5)="
                        f"山底接飞刀,SELL降权: 置信{final_confidence:.0%}→{_pen:.0%}(提准非拦截)"
                    )
                    final_confidence = _pen
                elif _bias_4h == "down" and not _bias_valid:
                    _pen = final_confidence * 0.85
                    logger.warning(f"[MetaAgent] 4H偏置=down但价格涨破均值(延伸Z={_ext_z:.2f})，SELL偏置失效: 置信{final_confidence:.0%}→{_pen:.0%}")
                    final_confidence = _pen
                elif _bias_4h == "neutral":
                    pass

            # 趋势末端防接飞刀（at_stale_top/bottom 是统计极端，开源硬惩罚）
            if final_decision == "BUY" and _at_top:
                logger.warning(f"[MetaAgent] 趋势末端山顶(at_stale_top)追BUY: 置信{final_confidence:.0%}→0.80 降权(提准非拦截·不再硬杀)")
                final_confidence = min(final_confidence, 0.80)
            elif final_decision == "SELL" and _at_bottom:
                logger.warning(f"[MetaAgent] 趋势末端谷底(at_stale_bottom)追SELL: 置信{final_confidence:.0%}→0.80 降权(提准非拦截·不再硬杀)")
                final_confidence = min(final_confidence, 0.80)

        # ★ 2026-08-06 修复：BUY/SELL 软惩罚对称化 + 修复 SELL 字符串匹配 bug。
        #   原 _trend_down 只匹配 ("down", "下跌"...)，但 regime_detect 实际返回 "downtrend"/"strong_downtrend"，
        #   导致真下跌行情也被判"无下行体制背书"而压置信；同时旧逻辑只惩罚 SELL，造成 BUY-only 偏置。
        #   现改为子串匹配，并对 BUY/SELL 做镜像处理：只有当方向获得对应体制背书
        #   （趋势同向 / 高波动 / 极端价位）时才不惩罚；否则置信×0.7 压低弱信号。
        #   这是"提准非拦截"，只削减明显错向/无背书的单，不砍好信号。
        if final_decision in ("BUY", "SELL"):
            _tr = str(trend_regime).lower()
            _vr = str(vol_regime).lower()
            if final_decision == "SELL":
                _backed = (
                    "downtrend" in _tr or "bearish" in _tr or "下跌" in _tr or "空头" in _tr
                    or _vr in ("高波动", "极端", "high", "extreme")
                    or bool(_sentinel.get("at_extreme_high")) or bool(_sentinel.get("overbought"))
                )
            else:  # BUY
                _backed = (
                    "uptrend" in _tr or "bullish" in _tr or "上涨" in _tr or "多头" in _tr
                    or _vr in ("高波动", "极端", "high", "extreme")
                    or bool(_sentinel.get("at_stale_bottom")) or bool(_sentinel.get("oversold"))
                )
            if not _backed:
                # ★ 2026-08-07 调研修正（海外实证·提准非拦截）：
                #   三模型同向共振 = 最高置信场景（TradePulse/TradingView：unanimous consensus
                #   conviction 最高，正是该放行；Kalshi 实盘加权置信≥0.50 即开，不分体制）。
                #   原「无体制背书×0.7」会把共振强信号(≈0.80)直接砍到0.56卡死在门槛下，
                #   与"多交易多赚钱"铁律及海外最佳实践相反 → 共振豁免体制惩罚。
                #   非共振单仍压一点(×0.85)但不砍死，区间整理靠"降仓"而非"禁开"处理。
                if three_way_consensus:
                    logger.info(
                        f"[MetaAgent] {final_decision}三模型共振豁免体制背书惩罚(提准非拦截·共识即放行)"
                    )
                else:
                    _pen = final_confidence * 0.85
                    logger.info(
                        f"[MetaAgent] {final_decision}提准软惩罚: 无对应体制背书(趋势={trend_regime}/波动={vol_regime})，"
                        f"置信{final_confidence:.0%}→{_pen:.0%}(提准非拦截·已放宽至0.85)"
                    )
                    final_confidence = _pen

        # ── 最终一致性拦截（价格延伸度 + RSI + Chronos反向）──
        # ★ 2026-08-07 实盘修复：防止云模型在明确趋势中逆势开仓。
        #   当价格已沿某一方向显著延伸（|z|>0.5）、RSI 确认该方向动能，
        #   且本地时序 Chronos 反对云模型方向时，直接 HOLD。
        #   这基于用户 2026-07-21 方法论：延伸度+RSI 是区分"健康回踩"与"趋势末端接飞刀"的关键。
        if final_decision in ("BUY", "SELL"):
            _rg = (market_data or {}).get("regime") or {}
            _bias = str(_rg.get("direction_bias", "neutral")).lower()
            _ext_z = float(_rg.get("extension_z", 0.0) or 0.0)
            _rsi = float(_rg.get("rsi_h1", 50.0) or 50.0)
            _chronos_oppose_dir = chronos_dir not in (final_decision, "NEUTRAL")
            _block = False
            _block_reason = ""
            if final_decision == "SELL":
                if (_bias == "up" or (_ext_z > 0.5 and _rsi > 55)) and _chronos_oppose_dir:
                    _block = True
                    _block_reason = (
                        f"趋势一致性拦截: 价格处于上涨延伸(z={_ext_z:.2f},rsi={_rsi:.0f},bias={_bias}) "
                        f"且Chronos={chronos_dir}反对SELL→HOLD"
                    )
            else:  # BUY
                if (_bias == "down" or (_ext_z < -0.5 and _rsi < 45)) and _chronos_oppose_dir:
                    _block = True
                    _block_reason = (
                        f"趋势一致性拦截: 价格处于下跌延伸(z={_ext_z:.2f},rsi={_rsi:.0f},bias={_bias}) "
                        f"且Chronos={chronos_dir}反对BUY→HOLD"
                    )
            if _block:
                _pen = final_confidence * float(getattr(settings, "CONSISTENCY_GATE_PENALTY", 0.85))
                logger.warning(
                    f"[MetaAgent] 最终一致性拦截(软化·Chronos退回提准角色): {_block_reason}→降权→{_pen:.0%}"
                )
                final_confidence = _pen
                _gate_count("最终一致性拦截")

        # ★★ 2026-08-11 新增 Fix A：SMC 订单流方向锚（机构订单流大脑 = smc_features.global_bias）★★
        #   根因（用户 16:30 实盘复盘）：16:20 cycle 日志 SMC全局偏向=bullish（机构订单流=看涨），
        #   但 meta_agent 决策层从不读 global_bias，只读了 regime(downtrend)/extension_z，被 net 为负的
        #   Chronos 融合票(SELL conf=98% 但 hit_avg 仅42%) 覆盖 → 逆订单流开 SELL @4362。
        #   之后价格涨到 4371+，SELL 浮亏不止损，扩大 13~28 分钟才平。
        #   设计（ICT/SMC 机构订单流原则 + 用户「行情在就多赚 / 行情不对立刻跑」铁律）：
        #     - smc=bullish 但 final=SELL → 逆机构订单流(错向) → 拦截并翻向 BUY（多赚）
        #     - smc=bearish 但 final=BUY → 逆机构订单流(错向) → 拦截并翻向 SELL
        #     - 豁免：反转哨兵确认真反转(REVERSE_* 且哨兵置信≥0.6) → 订单流已被真实推翻
        #     - 趋势末端(at_stale_top/bottom)不翻向（防接飞刀）
        #   这是「方向锚」不是「提准」：逆机构订单流的方向直接不开，改为顺订单流开（行情对就多赚）。
        if final_decision in ("BUY", "SELL"):
            _smc = (market_data or {}).get("smc_features") or {}
            _smc_bias = str(_smc.get("global_bias", "neutral") or "neutral").lower()
            _smc_guard = str(getattr(settings, "SMC_FLOW_GUARD", "soft")).lower()
            _against_flow = (
                (_smc_bias == "bullish" and final_decision == "SELL")
                or (_smc_bias == "bearish" and final_decision == "BUY")
            )
            # ★ 2026-08-14 根因修复（meta 系统性逆共识翻向 BUG）：
            #   计算三脑(DeepSeek/Hunyuan/Chronos)方向共识，用于约束 SMC 翻向。
            #   实证 8/13-8/14 共 15 笔 SMC=bullish 把【三脑 SELL 共识】翻成 BUY 全亏。
            #   规则：只要三脑已有方向共识，单 SMC 子信号(global_bias)不得翻向，至多降权。
            _brain_votes = [v for v in (ds_final, hy_final, chronos_dir)
                            if str(v).upper() in ("BUY", "SELL")]
            _brain_cons = None
            if _brain_votes:
                from collections import Counter as _Ctr
                _bc = _Ctr(str(v).upper() for v in _brain_votes)
                if _bc.most_common(1)[0][1] >= 2:  # 多数（≥2/3 或 2/2）
                    _brain_cons = _bc.most_common(1)[0][0]
            if _against_flow and _smc_guard != "off":
                _rg_g = (market_data or {}).get("regime") or {}
                _at_top_g = bool(_rg_g.get("at_stale_top", False))
                _at_bottom_g = bool(_rg_g.get("at_stale_bottom", False))
                # 真反转豁免：哨兵明确判反转且其方向与 smc 同向（即 smc 已被推翻）
                _sentinel_reverse = (
                    (_sig == "REVERSE_SELL" and _smc_bias == "bearish")
                    or (_sig == "REVERSE_BUY" and _smc_bias == "bullish")
                )
                if _sentinel_reverse and _sconf >= 0.6:
                    logger.info(
                        f"[MetaAgent] SMC订单流锚: smc={_smc_bias} 但哨兵{_sig}确认真反转"
                        f"(置信{_sconf:.0%})→豁免放行{final_decision}"
                    )
                # ★ 死代码标记（2026-08-15 审计收尾·注释认知陷阱修复）：
                #   本分支仅在 config.SMC_FLOW_GUARD == "hard" 时可达；
                #   当前 SMC_FLOW_GUARD = "soft"（见 config.py），故本分支【运行期不可达】。
                #   保留仅作历史对照，维护者勿误以为"SMC 仍会硬翻向"——soft 模式下
                #   订单流锚只做乘性降权(_pen = conf * SMC_SOFT_PENALTY)，绝不翻向/硬HOLD。
                elif _smc_guard == "hard":
                    _flip_to = "BUY" if _smc_bias == "bullish" else "SELL"
                    _at_extreme = (_flip_to == "BUY" and _at_top_g) or (_flip_to == "SELL" and _at_bottom_g)
                    # ★ 2026-08-14 根因修复：三脑已有方向共识 → SMC 仅降权、绝不翻向
                    if _brain_cons is not None:
                        _pen = final_confidence * 0.85
                        logger.warning(
                            f"[MetaAgent] SMC订单流锚(HARD): smc={_smc_bias} 但三脑共识={_brain_cons}"
                            f"→不翻向,仅降权→{_pen:.0%}(防单子信号否决多模型共识)"
                        )
                        final_confidence = _pen
                    # ★ 2026-08-12 根因修复（全 BUY 逆势亏损）：
                    #   单个 SMC 子信号(global_bias)不得以 HARD 级别否决「高置信多模型共识」。
                    #   实证：融合票 98% 判 SELL（价格确实在跌，方向正确），却被 smc=bullish
                    #   硬翻成 BUY → 系统每个周期在下跌市疯狂做多 → 全亏。
                    #   规则：final_confidence≥0.75（强共识）时 SMC 只能降权(soft惩罚)，不得翻向；
                    #        仅当决策本身置信偏低(<0.75)时，才允许 SMC 顺订单流翻向（保留 Fix A 初衷·提准非拦截）。
                    if final_confidence >= 0.75:
                        _pen = final_confidence * 0.85
                        logger.warning(
                            f"[MetaAgent] SMC订单流锚(HARD): smc={_smc_bias} 逆势{final_decision}"
                            f"但强共识{final_confidence:.0%}→不翻向,仅降权→{_pen:.0%}"
                            f"(防单子信号否决多模型共识)"
                        )
                        final_confidence = _pen
                    elif _at_extreme:
                        logger.warning(
                            f"[MetaAgent] SMC订单流锚(HARD): smc={_smc_bias} 逆势{final_decision}→HOLD"
                            f"(趋势末端{'山顶' if _at_top_g else '谷底'}不翻向,防接飞刀)"
                        )
                        final_decision = "HOLD"
                        final_confidence = 0.0
                    else:
                        _flip_conf = float(getattr(settings, "SMC_FLOW_FLIP_CONF", 0.66))
                        logger.warning(
                            f"[MetaAgent] SMC订单流锚(HARD): smc={_smc_bias} 逆势{final_decision}→翻向{_flip_to}"
                            f"(信心{_flip_conf:.0%},机构订单流为准,行情对就多赚)"
                        )
                        final_decision = _flip_to
                        final_confidence = min(0.98, _flip_conf)
                else:  # soft
                    # ★★ 2026-08-17 用户理念 P0 修复：SMC 锚「短周期背书豁免」★★
                    #   cycle#7 实况：R2 短周期加成后 norm=51% 放行 SELL（DS SELL 58% +
                    #   视觉 M5 SELL 95% + M15 动量），却被 SMC(bullish H4级背景) 无差别降权
                    #   ×0.85 → 43%×0.85=37% < 42% lean 下限 → 短周期明确下跌仍开不了空。
                    #   SMC 全局偏向是 H4 级机构订单流「背景」，而用户理念=开仓/平仓看同一
                    #   盘面（M5/M15 当下信号优先）。修复：当裁决有「短周期强背书」（视觉 M5
                    #   同向且 conf≥0.8，或 M15 最近 5 根同向连跌/连涨）时，SMC 背景降权豁免
                    #   （提准非拦截·保留方向不动手数，仅不再被长周期背景压死）。
                    _smc_short_backed = False
                    _smc_back_note = ""
                    # ★ 2026-08-18 第三处修复A：趋势明确时 SMC 软信号反向直接豁免（提准非拦截）
                    #   体制层已正确判强跌/强涨，SMC 全局偏向(H4级背景)不应在趋势明确时压低顺势单。
                    #   原豁免仅限"视觉M5同向≥0.8 或 M15严格5连跌"，真实趋势有反弹K时难触发→持续降权。
                    #   趋势强本身即短周期背书，直接豁免。
                    if not _smc_short_backed and getattr(settings, "SMC_TREND_EXEMPT", True) and market_regime in _TREND_STRONG:
                        _smc_short_backed = True
                        _smc_back_note = f"趋势明确({market_regime})·SMC背景不压短周期盘面"
                    if vision_m5_dir == final_decision and vision_m5_conf >= 0.8:
                        _smc_short_backed = True
                        _smc_back_note = f"视觉M5({vision_m5_dir} {vision_m5_conf:.0%})"
                    if not _smc_short_backed and final_decision in ("BUY", "SELL"):
                        try:
                            _tf = (market_data or {}).get("timeframes") or {}
                            _m15_raw = _tf.get("M15")
                            _m15_bars = (_m15_raw.get("closes") if isinstance(_m15_raw, dict) else None) or []
                            if not isinstance(_m15_bars, (list, tuple)):
                                _m15_bars = []
                            _closes = [float(b) for b in _m15_bars if isinstance(b, (int, float))]
                            if len(_closes) >= 5:
                                _l5 = _closes[-5:]
                                _down5 = all(_l5[i + 1] < _l5[i] for i in range(4))
                                _up5 = all(_l5[i + 1] > _l5[i] for i in range(4))
                                if (_down5 and final_decision == "SELL") or (_up5 and final_decision == "BUY"):
                                    _smc_short_backed = True
                                    _smc_back_note = f"M15{'5连跌' if _down5 else '5连涨'}"
                        except Exception:  # noqa: BLE001
                            pass
                    if _smc_short_backed:
                        logger.info(
                            f"[MetaAgent] SMC订单流锚(SOFT·豁免): smc={_smc_bias} 逆势{final_decision} "
                            f"但短周期背书{_smc_back_note}→豁免降权(长周期背景不压短周期盘面·用户理念)"
                        )
                    else:
                        _pen = final_confidence * float(getattr(settings, "SMC_SOFT_PENALTY", 0.85))
                        logger.warning(
                            f"[MetaAgent] SMC订单流锚(SOFT): smc={_smc_bias} 逆势{final_decision}→降权"
                            f"置信{final_confidence:.0%}→{_pen:.0%}(提准非拦截·软模式·Chronos退回提准角色)"
                        )
                        final_confidence = _pen
                        _gate_count("SMC订单流锚")

        # ★★ 2026-08-11 新增·真闭环：进化表→置信惩罚系数（硬约束，对齐 SOTA 折扣老虎机）★★
        # 把 EvolutionEngine 的「情境→期望盈亏」从"软提示文本"升级为对最终置信的乘子修正：
        #   smc:bullish 上下文里 BUY 历史负期望→乘子<1 压低置信（甚至跌破开仓门槛→HOLD），
        #   正期望→加成。指数衰减+收缩保证非平稳市场不学歪、小样本不过拟合。
        #   衔接：Fix A(结构方向锚) 先定方向 → 本修正(数据驱动) 再微调，互不打架。
        if final_decision in ("BUY", "SELL"):
            try:
                from app.services.local_rl import get_engine as _evo_get
                _evo = _evo_get()
                _mult = _evo.get_confidence_modifier(final_decision, market_data)
                if _mult is not None and abs(_mult - 1.0) > 1e-6:
                    _old = final_confidence
                    final_confidence = max(0.0, min(0.98, final_confidence * _mult))
                    logger.warning(
                        f"[MetaAgent] 真进化闭环: {final_decision} 置信{_old:.0%}"
                        f"→{final_confidence:.0%}(乘子{_mult:.3f}·数据驱动)"
                    )
            except Exception as _ee:
                logger.debug(f"[MetaAgent] 真进化闭环跳过: {_ee}")

        # 一致性评估
        ds_agree = deepseek_rebuttal.get("agree_with_opponent", False)
        hy_agree = hunyuan_rebuttal.get("agree_with_opponent", False)
        debate_consensus = "strong" if (ds_agree and hy_agree) else ("moderate" if (ds_agree or hy_agree) else "disagreement")

        # 构建推理摘要
        _chronos_vote_str = f"Chronos:{chronos_dir}"
        if chronos_weight > 0:
            _chronos_vote_str += f"(w={chronos_weight:.2f})"
        if three_way_consensus:
            _chronos_vote_str += "[共振]"
        elif chronos_opposes:
            _chronos_vote_str += "[反向制衡]"
        # fusion_v2：把融合票信息透出（替代单 Chronos 的展示语义，仍是第四票来源）
        _fusion_str = ""
        if _decision_mode == "fusion_v2" and ts_fusion_models > 0:
            _fusion_str = f" | 融合票:{ts_fusion_dir}(w={ts_fusion_weight:.2f}|conf={ts_fusion_conf:.0%}|{ts_fusion_models}模型|命中{ts_fusion_hit_avg:.0%})"
        # 视觉第四票透出（加法增强，非闸门）
        _vision_str = ""
        if vision_is_dir:
            _vision_str = f" | 视觉:{vision_dir}(w={vision_weight:.2f}|conf={vision_conf:.0%}|H4={vision_h4_dir}/M15={vision_m15_dir}/M5={vision_m5_dir})"
        # Qwen3-8B 常态副驾第五票透出（加法增强，非闸门）
        _copilot_str = ""
        if copilot_is_dir:
            _copilot_str = f" | 副驾:{copilot_dir}(w={copilot_weight:.2f}|conf={copilot_conf:.0%})"
        reasoning_summary = (
            f"DS: {ds_final}({ds_confidence:.0%}|w={ds_weight:.2f}) | "
            f"HY: {hy_final}({hy_confidence:.0%}|w={hy_weight:.2f}) | "
            f"{_chronos_vote_str}{_fusion_str}{_vision_str}{_copilot_str} | "
            f"共识: {debate_consensus} | "
            f"风险: {risk_level}({risk_score}/10) | "
            f"体制: {market_regime}"
        )

        # ★ 2026-08-13 审计修复：plain_summary 必须在【所有后处理闸门之后】生成。
        #   旧实现在此处(闸门前)生成 → 若下方新闻门(L1233)/逆共识门(L1248)/山顶抓顶
        #   修复(L1267)翻写了 final_decision，会出现「人话解读说看涨(买入)但实际开仓
        #   看跌(卖出)」的自相矛盾，正是用户"AI大脑完全不准"的直接症状。
        #   故此处仅占位，真正的 _build_plain_summary 调用移至 L1270 实例化前（最终定稿处）。
        plain_summary = ""

        # ── v4 Meta 质量陪审团结果（本地时序模型制衡语义大脑）──
        # 由 debate_engine Step 0.85 注入 market_data["meta_quality"];
        # Chronos 方向已作为第三票参与上面加权裁决；此处仅保留「提准」用途
        # （止盈 regime + 动态 TP 天花板）的字段读取。
        _mq = (market_data or {}).get("meta_quality") or {}
        _q_regime = str(_mq.get("regime", "") or "")
        _q_ceiling = _mq.get("chronos_tp_ceiling")
        if _q_regime:
            reasoning_summary += f" | Meta质量:{_q_regime}(Q={_mq.get('q')})"
            if _mq.get("notes"):
                reasoning_summary += f" 依据:{';'.join(_mq['notes'][:2])}"

        # ── v5 AI 自主仓位管理：基于共识/体制/持仓算仓位意图 + 风险占比 ──
        #   让 AI 从"只会定方向"升级为"能决定用多大风险、要不要加仓/缩手"。
        #   机械风控(max_positions / risk_engine 6层)仍全程兜底，本逻辑只做加法不删兜底。
        _pos = (market_data or {}).get("my_open_positions") or []
        _port = (market_data or {}).get("portfolio_state") or {}
        _same = sum(1 for p in _pos if p.get("direction") == final_decision)
        _total_lots = float(_port.get("total_lots", 0) or 0)
        _total_pos = int(_port.get("total_positions", len(_pos)) or 0)
        _is_trend = market_regime in ("uptrend", "strong_uptrend", "downtrend", "strong_downtrend")
        _strong = (debate_consensus == "strong") or three_way_consensus or (final_confidence >= 0.72)
        _intent = "open"
        _target_risk = None
        if final_decision in ("BUY", "SELL"):
            if _same > 0 and _is_trend and _strong and _total_lots < 8.0:
                # 趋势确认 + 高共识 + 仓位未爆 → AI 主动金字塔加仓（突破同向衰减，由硬上限兜底防爆仓）
                _intent = "add"
            elif _total_lots >= 6.0 or _total_pos >= 6:
                # 仓位已重 → AI 主动缩手（风险占比砍半），把"总敞口收缩"交还 AI 决策
                _intent = "reduce"
                _target_risk = 1.0
        _portfolio_summary = (
            f"持仓{_total_pos}笔/{_total_lots:.1f}手 浮{float(_port.get('total_floating_pnl', 0) or 0):+.0f}$ "
            f"同向{final_decision}{_same} 意图={_intent}"
            + (f" 风险占比→{_target_risk}%" if _target_risk else "")
        )
        reasoning_summary += f" | 仓位管理:{_portfolio_summary}"

        # ── 进场价位对齐（2026-08-14 根治「AI 想在 4329 开空、执行却在 4315 市价开」）──
        # 取双脑 final stance 的 entry_price（结构化 JSON 优先），回退解析 reasoning 文本里的
        # 「反弹/回踩至 X(-Y)」。SELL 想更高、BUY 想更低 → 取对应极值（更优盈亏比）。
        # 是否「值得等」由执行层据当前价+ATR 判定；此处只把 AI 的价位意愿交出去，绝不丢弃。
        _entry_price = None
        _entry_style = "market"
        if final_decision in ("BUY", "SELL"):
            # ★ 2026-08-15 审计P1修复：原结构化/回退入场价过滤写死 [4000,5000]，
            #   当前 XAU≈3300-3500 全被误拒→AI目标入场价被丢、退化成市价单。
            #   改为以当前价为中心 ±30% 动态区间（无价则回退 1500~6000）。
            _cp = (market_data or {}).get("current_price") or {}
            _cur = float(_cp.get("last") or _cp.get("bid") or 0) or float(
                ((market_data or {}).get("timeframes", {}).get("M5", {}) or {}).get("close", 0) or 0)
            _ep_lo, _ep_hi = (_cur * 0.7, _cur * 1.3) if (1000 <= _cur <= 10000) else (1500.0, 6000.0)
            _ecands = []
            # ★★ 2026-08-17 P0 修复（海外调研：arXiv 2504.10789 / AWS Builder / FinDebate）：
            #   只读 rebuttal 单源 → 辩论轮 JSON 被截断或未写价位时 entry_price 永久丢失
            #   → 执行层退化成市价追高（实证 19:39: DS 说"禁追多、等回踩 4398-4400"却市价开 4404）。
            #   正确做法（双源 + 多字段）：同时解析 analyze 原文与 rebuttal 修正版，
            #   且每个源都读 entry_price 结构化字段 + reasoning/revised_reasoning 文本回退。
            #   rebuttal 是最终方向来源（优先），analyze 是价位意愿备份（防截断丢失）。
            for _a in (deepseek_rebuttal, hunyuan_rebuttal, deepseek_analysis, hunyuan_analysis):
                if not isinstance(_a, dict):
                    continue
                _ep = _a.get("entry_price")
                try:
                    _ep = float(_ep)
                except (TypeError, ValueError):
                    _ep = None
                if _ep and _ep_lo <= _ep <= _ep_hi:
                    _ecands.append(_ep)
                if getattr(settings, "ENTRY_ZONE_PARSE_REASONING", True):
                    # rebuttal 的修正推理可能存于 reasoning（_polish 已映射）或 revised_reasoning（原始键）
                    _reason_text = str(_a.get("reasoning") or _a.get("revised_reasoning") or "")
                    _rp = _parse_entry_from_reasoning(_reason_text, final_decision, _cur)
                    if _rp:
                        _ecands.append(_rp)
            if _ecands:
                # SELL 想更高、BUY 想更低 → 取更优极值（结构化当前价会被 reasoning 的更好价覆盖）
                _entry_price = max(_ecands) if final_decision == "SELL" else min(_ecands)
                _entry_style = "limit"
        if _entry_price is not None:
            if _entry_style == "limit":
                reasoning_summary += f" | 目标入场:{_entry_price:.2f}(等回到zone再点火)"
            else:
                reasoning_summary += f" | 入场:{_entry_price:.2f}"

        # 新闻层透出（审计/面板可见，便于复盘舆情是否影响决策）
        _news_sum = (market_data or {}).get("news") or {}
        if _news_sum.get("has_news"):
            reasoning_summary += (
                f" | 新闻舆情:{_news_sum.get('bias')}(分{_news_sum.get('gold_sentiment_score'):+.2f})"
                + ("[高影响]" if _news_sum.get("high_impact_active") else "")
            )

        logger.info(
            f"[MetaAgent] 仓位管理决策: {_portfolio_summary} | intent={_intent} "
            f"target_risk={_target_risk}"
        )

        # ── 组合级·新闻感知置信闸门（2026-08-13 新增·提准非拦截）──
        # 调研(TradingAgents arXiv2412.20138 / goldprice.com / gainsium.com 2026)一致结论：
        #   新闻舆情是独立分析层、与宏观信号「互补非替代」。故本闸门只在
        #   「高影响事件窗口激活(FOMC/CPI/NFP/地缘) 且 决策方向与舆情明确相反」时，
        #   要求极高置信(NEWS_CONFLICT_MIN_CONF)才放行；否则 HOLD。
        #   平时一律不干预 → 保护"多交易多赚钱"及格线（不做 blanket 拦截）。
        _contrarian_downgraded = False
        # ── 置信校准（提准非拦截·安全默认零行为变化）──
        # 把 raw final_confidence 映射为历史观测命中率。
        # ★ 闸门默认仍用 raw final_confidence（_calib_conf），零行为变化、保护「交易笔数不腰斩」及格线；
        #   校准值仅用于诚实展示(calibrated_confidence 字段 / 审计日志)。
        #   若要令闸门也改用校准值(更紧、会拦截更多弱逆共识单)，须置
        #   CONFIDENCE_CALIBRATION_AFFECTS_GATES=True 且经 walk-forward 验证净盈利提升后（拦截须验证铁律）。
        _calib_conf = final_confidence           # 闸门阈值用 raw（默认无变化）
        _calib_display = final_confidence        # 展示/审计用诚实校准值
        try:
            _cal = self._get_calibrator()
            if _cal is not None and _cal.available():
                _calib_display = _cal.calibrate(final_confidence)
                if getattr(settings, "CONFIDENCE_CALIBRATION_AFFECTS_GATES", False):
                    _calib_conf = _calib_display
        except Exception:
            _calib_conf = final_confidence
            _calib_display = final_confidence
        if final_decision in ("BUY", "SELL"):
            _news = (market_data or {}).get("news") or {}
            if _news.get("has_news"):
                _ns = float(_news.get("gold_sentiment_score", 0) or 0)
                _bias_th = float(getattr(settings, "NEWS_SENTIMENT_BIAS_THRESHOLD", 0.30))
                _bias = "BUY" if _ns > _bias_th else ("SELL" if _ns < -_bias_th else "HOLD")
                _hi = bool(_news.get("high_impact_active"))
                # ★ 2026-08-18 第五处修复A：趋势明确且顺势单与趋势同向 → 新闻舆情(短期噪音)不拦截顺势单。
                #   强跌趋势里新闻舆情偏BUY(高影响事件持续窗口)会把顺势SELL反复压到<0.58死锁，
                #   违背用户"趋势明确多开顺势单赚钱"铁律。趋势本身即方向背书，舆情是短期噪音。
                _TREND_REGIMES_NEWS = ("strong_downtrend", "downtrend", "strong_uptrend", "uptrend")
                _big_news = ("BUY" if market_regime in ("uptrend", "strong_uptrend")
                             else "SELL" if market_regime in ("downtrend", "strong_downtrend") else "HOLD")
                if market_regime in _TREND_REGIMES_NEWS and final_decision == _big_news:
                    logger.info(
                        f"[MetaAgent] 新闻感知闸门: 趋势明确({market_regime})·顺势{final_decision}"
                        f"豁免舆情降权(舆情为短期噪音·提准非拦截)"
                    )
                else:
                    NEWS_CONFLICT_MIN = float(getattr(settings, "NEWS_CONFLICT_MIN_CONF", 0.80))
                    if _calib_conf < NEWS_CONFLICT_MIN:
                        _pen = final_confidence * float(getattr(settings, "NEWS_GATE_PENALTY", 0.85))
                        logger.warning(
                            f"[MetaAgent] 新闻感知闸门(软化): 高影响事件下逆舆情方向{final_decision}"
                            f"(舆情分{_ns:+.2f}→偏向{_bias}) 置信{final_confidence:.0%}"
                            f"<{NEWS_CONFLICT_MIN:.0%}→降权→{_pen:.0%}(提准非拦截)"
                        )
                        final_confidence = _pen
                        _gate_count("新闻感知闸门")
                    else:
                        logger.info(
                            f"[MetaAgent] 新闻感知闸门: 逆舆情但高置信{final_decision}"
                            f"({final_confidence:.0%}≥{NEWS_CONFLICT_MIN:.0%})放行"
                        )

        # ── 逆共识高置信闸门（2026-08-13 新增·提准非拦截·基于大脑审计「发现1」）──
        # 审计闭环(227笔/146平仓): META 逆三脑(DS/HY/Chronos)共识单胜率 51% < 共识单 56%。
        # 元智能体独立 override 在小幅拖累信号准度。提准非拦截:
        #   仅当 META 终裁逆三脑共识 且 置信<阈值时,**降级采用共识方向**(保留交易、不腰斩笔数),
        #   而非 HOLD/拦杀;仅高置信(≥阈值)才放行逆共识。
        if final_decision in ("BUY", "SELL") and getattr(settings, "CONSENSUS_OVERRIDE_ENABLED", True):
            _min = float(getattr(settings, "CONSENSUS_OVERRIDE_MIN_CONF", 0.80))
            final_decision, _contrarian_downgraded = apply_contrarian_gate(
                final_decision, _calib_conf, ds_final, hy_final, chronos_dir, _min)

        # ── 亚盘/清淡时段方向确认增强（2026-08-14·提准非拦截·治三单错方向根因）──
        # 三单错方向(#383232749/#383538133/#383644306)全部落在 session_info=亚盘(moderate)，
        # 根因=亚盘震荡/假突破下弱共识方向被市价追开。调研(United Kings Tokyo Compression /
        # fxroboteasy AI Gold Asia / Golden Goose Scalper / ALGOGENE Donchian)一致：亚盘假突破
        # 是 XAUUSD 最大亏损源，有效过滤=收盘突破确认+实体/回踩确认，且亚盘信号频率本就低。
        # 本系统 session_info 已采集却从未接入裁决 → 补此门控。原则(提准非拦截)：
        #   仅对「弱流动性时段 + 弱共识方向(无三脑共振且 conf<0.72)且无明确入场zone」降权，
        #   使其低于开仓门槛(≈0.5)→自然不市价追假突破；强共识/AI已给zone(limit)的单照常
        #   → 不腰斩交易笔数，只拦「市价追单」这一种最亏钱动作。
        if (final_decision in ("BUY", "SELL")
                and getattr(settings, "ASIAN_SESSION_DIR_GATE_ENABLED", True)):
            _sess = (market_data or {}).get("session_info") or {}
            if _sess:
                _sess_q = str(_sess.get("quality", ""))
                if _sess_q in ("moderate", "poor"):
                    # ★ 2026-08-15 修正（用户纠偏）：手数已锁最低0.02，无再降空间。
                    #   亚盘惩罚不再压新开仓置信(否则=硬拦截，违背提准非拦截)；
                    #   改为【仅限追单/add 生效】——亚盘清淡时段禁止金字塔加仓追假突破，
                    #   新开仓方向保持全置信(保交易笔数)。强共识/有zone的单照常。
                    if _intent == "add":
                        _pen = final_confidence * float(getattr(
                            settings, "ASIAN_SESSION_DIR_GATE_PENALTY", 0.45))
                        logger.warning(
                            f"[MetaAgent] 亚盘方向确认增强(仅追单): {_sess_q}时段+追单{_intent} "
                            f"{final_decision}({final_confidence:.0%})→降权→{_pen:.0%}(防亚盘追假突破)"
                        )
                        final_confidence = _pen
                        _gate_count("亚盘追单门")
                    else:
                        logger.info(
                            f"[MetaAgent] 亚盘方向确认增强: {_sess_q}时段新开仓{final_decision}"
                            f"({final_confidence:.0%}) 保持全置信(手数已最低0.02·提准非拦截)"
                        )

        # ── 2026-08-13 根因修复②：真山顶高置信反转 → 抓顶空单（解决"山顶不开sell"）──
        #   旧逻辑：reversal_sentinel 的 REVERSE_SELL 仅用于"否决 BUY→HOLD"，从不主动开 SELL，
        #   导致 AI 在真山顶只会观望、永远不在顶部做空（用户实盘：山顶不开sell）。
        #   现：当终裁为 HOLD、但哨兵确认真山顶(REVERSE_SELL 且置信≥0.65)时，翻为 SELL 抓顶空单。
        #   哨兵本身门控极严(at_top + 多重共振≥0.7 → conf≥0.74)，属"提准非拦截"的高质量反转空，
        #   对齐用户2026-07-21方法论(延伸度+RSI极端+SMC扫荡=反转)。仅极端风险时不放行，
        #   且不覆盖已确权的 BUY/SELL（只在 HOLD 时补开），保护"多交易多赚钱"及格线。
        if (final_decision == "HOLD"
                and _sig == "REVERSE_SELL"
                and _sconf >= 0.65
                and risk_level != "extreme"
                and bool((market_data or {}).get("regime", {}).get("at_stale_top", False))):
            logger.warning(
                f"[MetaAgent] 根因修复②抓顶空单: 终裁HOLD但真山顶 REVERSE_SELL(置信{_sconf:.0%})"
                f"→翻为 SELL 抓顶(提准非拦截·高质量反转空)"
            )
            final_decision = "SELL"
            final_confidence = min(0.95, max(final_confidence, _sconf))

        # ── 辩论环（TradingAgents 式·加法增强·提准非拦截）──
        #   消费已有多路信号(DS/HY/Chronos/融合票/视觉/副驾/SMC订单流/体制/风险)作"牛熊研究员+风控审议团"，
        #   对抗式综合：仅当多视角明显分歧或风险偏高时，乘性缩放置信(∈[0.80,1.0])。
        #   绝不改方向、绝不硬HOLD、绝不砍笔数(仅对"已被质疑的弱信号"软缩权，使其自然低于开仓门槛)。
        #   默认关闭；全部 getattr 灰度高可用；任何异常→降级无影响，绝不阻断决策链。
        # ★ 2026-08-17 shadow 埋点：开关关闭时也计算辩论环结果并记录(不应用)——
        #   供 walk-forward A/B 基线对比（"若开启会缩到多少" vs 实际最终置信），
        #   跑 1-2 周后无痛开启，无需二次部署。
        _dr_shadow = None
        if final_decision in ("BUY", "SELL"):
            try:
                from app.core.debate_ring import run_debate_ring
                _dr_ctx = {
                    "ds_final": ds_final, "ds_confidence": ds_confidence,
                    "hy_final": hy_final, "hy_confidence": hy_confidence,
                    "chronos_dir": chronos_dir, "chronos_weight": chronos_weight,
                    "ts_fusion_dir": ts_fusion_dir, "ts_fusion_conf": ts_fusion_conf,
                    "ts_fusion_agree": ts_fusion_agree,
                    "vision_dir": vision_dir, "vision_conf": vision_conf,
                    "copilot_dir": copilot_dir, "copilot_conf": copilot_conf,
                    "smc_bias": _smc_bias,
                    "market_regime": market_regime, "risk_level": risk_level, "risk_score": risk_score,
                    "at_stale_top": bool((market_data or {}).get("regime", {}).get("at_stale_top", False)),
                    "at_stale_bottom": bool((market_data or {}).get("regime", {}).get("at_stale_bottom", False)),
                    "session_quality": str(((market_data or {}).get("session_info") or {}).get("quality", "")),
                }
                if getattr(settings, "DEBATE_RING_ENABLED", False):
                    _dr_conf, _dr_detail = run_debate_ring(final_decision, final_confidence, _dr_ctx, settings)
                    if _dr_detail.get("applied"):
                        _gate_count("辩论环缩权")
                        logger.warning(
                            f"[辩论环] 缩权: {final_decision} 置信{final_confidence:.0%}→{_dr_conf:.0%} "
                            f"(牛{_dr_detail.get('bull')}/熊{_dr_detail.get('bear')}/中{_dr_detail.get('neutral')}"
                            f"·风险{_dr_detail.get('risk_flags')}) 提准非拦截"
                        )
                    final_confidence = _dr_conf
                else:
                    # ★ shadow 模式：只计算不应用，记录进快照供 A/B 对比
                    try:
                        _dr_shadow_conf, _dr_shadow_detail = run_debate_ring(
                            final_decision, final_confidence, _dr_ctx, settings, shadow=True
                        )
                        if _dr_shadow_detail.get("applied"):
                            _dr_shadow = {
                                "enabled": False,  # shadow：开关未开
                                "scaled_conf": round(float(_dr_shadow_conf), 4),
                                "orig_conf": round(float(final_confidence), 4),
                                "delta": round(float(_dr_shadow_conf) - float(final_confidence), 4),
                                "bull": _dr_shadow_detail.get("bull"),
                                "bear": _dr_shadow_detail.get("bear"),
                                "neutral": _dr_shadow_detail.get("neutral"),
                                "penalties": _dr_shadow_detail.get("total_penalty"),
                                "risk_flags": _dr_shadow_detail.get("risk_flags") or [],
                            }
                    except Exception as _drs_err:  # noqa: BLE001
                        logger.warning(f"[辩论环-shadow] 计算异常(忽略): {_drs_err}")
                        _dr_shadow = None
            except Exception as _dr_err:  # noqa: BLE001
                logger.warning(f"[辩论环] 异常降级(无影响): {_dr_err}")
                _dr_shadow = None

        # ★ 2026-08-17 M15 短周期动量加成（用户理念：开仓/平仓看同一盘面，5 根 M15 连跌=顺势空）。
        #   实测：M15 连跌但 Chronos/融合票时序预测反弹 BUY → SELL 置信 0.384 被压 <0.42 开不了空。
        #   短周期结构趋势=用户盘面视角；与云方向同向时给置信加成（提准非拦截，只加不减、不翻向）。
        if final_decision in ("BUY", "SELL"):
            try:
                _tf = (market_data or {}).get("timeframes") or {}
                _m15_raw = _tf.get("M15")
                _m15_bars = (_m15_raw.get("closes") if isinstance(_m15_raw, dict) else None) or []
                if not isinstance(_m15_bars, (list, tuple)):
                    _m15_bars = []
                _closes = [float(b) for b in _m15_bars if isinstance(b, (int, float))]
                if len(_closes) >= 5:
                    _last5 = _closes[-5:]
                    _down5 = all(_last5[i + 1] < _last5[i] for i in range(4))
                    _up5 = all(_last5[i + 1] > _last5[i] for i in range(4))
                    _match = (_down5 and final_decision == "SELL") or (_up5 and final_decision == "BUY")
                    if _match:
                        _mb = float(getattr(settings, "M15_MOMENTUM_BONUS", 0.06))
                        _old = final_confidence
                        final_confidence = min(0.95, final_confidence + _mb)
                        logger.info(
                            f"[MetaAgent] M15动量加成: {'5连跌' if _down5 else '5连涨'} 与{final_decision}同向 "
                            f"→ 置信{_old:.0%}→{final_confidence:.0%}(短周期盘面·用户理念)"
                        )
            except Exception as _me:  # noqa: BLE001
                logger.debug(f"[MetaAgent] M15动量加成跳过: {_me}")

        # ── 支撑/压力位置质量门（2026-08-19·提准非拦截·根治"卖到支撑底"）──
        #   模型能识别关键位（structure_anchors / key_levels），但此前裁决层未用之修正入场质量。
        #   本门不硬拦截顺势信号，仅在"SELL 已贴近支撑"或"BUY 已贴近压力"时降权，
        #   让弱共识低于开仓门槛自然 HOLD；强突破/回踩确认的单照常。
        final_confidence = _apply_sr_location_gate(final_decision, final_confidence, market_data)

        # ★ 2026-08-13 审计修复：在【全部后处理闸门】(新闻门/逆共识门/山顶抓顶/支撑压力门)之后，
        #   用【最终定稿】的 final_decision 生成人话解读，确保「展示方向」与「真实开仓方向」
        #   100% 一致，根除"说看涨却开卖单"的自相矛盾。方向措辞仍 100% 来自真实投票
        #   (_build_plain_summary 内部 Grounding)，不靠 final_decision 硬写，故翻向时也会
        #   如实标注"逆共识操作"。
        plain_summary = _build_plain_summary(
            final_decision=final_decision,
            debate_consensus=debate_consensus,
            risk_level=risk_level,
            risk_score=risk_score,
            ds_final=ds_final,
            hy_final=hy_final,
            ds_confidence=ds_confidence,
            hy_confidence=hy_confidence,
            market_regime=market_regime,
            chronos_dir=chronos_dir,
            chronos_weight=chronos_weight,
            chronos_agree=three_way_consensus,
            final_confidence=final_confidence,
        )

        decision = DebateDecision(
            decision=final_decision,
            confidence=round(final_confidence, 3),
            calibrated_confidence=round(_calib_display, 3),
            deepseek_weight=round(ds_weight, 2),
            hunyuan_weight=round(hy_weight, 2),
            deepseek_confidence=round(ds_confidence, 3),
            hunyuan_confidence=round(hy_confidence, 3),
            deepseek_vote=ds_final,
            hunyuan_vote=hy_final,
            # ★ 2026-08-11：DS 失联本地副驾补位 → 该票来自本地 8B，feedback 跳过 DS 统计
            deepseek_local_fallback=bool(
                (deepseek_analysis or {}).get("_local_copilot")
                or (deepseek_rebuttal or {}).get("_local_copilot")
            ),
            reasoning_summary=reasoning_summary,
            risk_level=risk_level,
            plain_summary=plain_summary,
            consensus=debate_consensus,
            quality_regime=_q_regime,
            chronos_tp_ceiling=_q_ceiling,
            chronos_p10=_mq.get("p10_final"),
            # ★ Phase 1：把上面已经算好的 Chronos 溯源全量交出去。
            #   这些值此前只进了日志和 reasoning 文本，导致下游拿不到结构化数据。
            chronos_agree=three_way_consensus,
            chronos_vote=chronos_dir,
            chronos_weight=round(chronos_weight, 3) if chrono_is_dir else 0.0,
            q_score=_mq.get("q"),
            chronos_p50=_mq.get("p50_final"),
            # ── fusion_v2 时序融合第四票（透明化/审计）──
            ts_fusion_dir=ts_fusion_dir,
            ts_fusion_weight=round(ts_fusion_weight, 3) if ts_fusion_dir in ("BUY", "SELL") else 0.0,
            ts_fusion_conf=round(ts_fusion_conf, 3),
            ts_fusion_agree=ts_fusion_agree,
            ts_fusion_hit_avg=round(ts_fusion_hit_avg, 3),
            ts_fusion_models=ts_fusion_models,
            ts_fusion_note=ts_fusion_note,
            # ── 视觉模型第四票（加法增强，非闸门）──
            vision_dir=vision_dir,
            vision_weight=round(vision_weight, 3) if vision_is_dir else 0.0,
            vision_conf=round(vision_conf, 3),
            vision_agree=vision_agree,
            vision_h4_dir=vision_h4_dir,
            vision_m15_dir=vision_m15_dir,
            vision_m5_dir=vision_m5_dir,
            vision_m5_conf=round(vision_m5_conf, 3),
            vision_note=vision_note,
            # ── Qwen3-8B 常态确认型副驾第五票（加法增强，非闸门）──
            copilot_dir=copilot_dir,
            copilot_weight=round(copilot_weight, 3) if copilot_is_dir else 0.0,
            copilot_conf=round(copilot_conf, 3),
            copilot_agree=copilot_agree,
            copilot_note=copilot_note,
            position_intent=_intent,
            target_risk_pct=_target_risk,
            portfolio_state=_portfolio_summary,
            # ── 进场价位对齐：把 AI 期望入场价交出去，执行层据其决定立即市价 or 推迟到 zone ──
            entry_price=round(_entry_price, 2) if _entry_price is not None else None,
            entry_style=_entry_style,
            # ── 解门锁监控快照：各门触发率 + 总HOLD率，供 dashboard/审计观测 ──
            gate_stats=get_gate_stats_snapshot(),
            # ── 辩论环 shadow（2026-08-17·A/B 基线采集）──
            #   开关关闭时仍计算"若开启会缩到多少"，供 walk-forward A/B 对比，
            #   跑 1-2 周基线与 shadow 差异后无痛开启 DEBATE_RING_ENABLED。
            debate_ring_shadow=_dr_shadow,
            # ── 篮子级 AI 持仓管理（2026-08-17）：双脑 position_action 融合结果 ──
            basket_action=_basket_action,
            basket_action_conf=round(_basket_conf, 3),
            basket_action_reason=_basket_reason,
            basket_action_confirmed=_basket_confirmed,
            basket_action_confirm_note=_basket_confirm_note,
        )

        # 记录到历史
        self.decision_history.append({
            "decision": final_decision,
            "ds_final": ds_final,
            "hy_final": hy_final,
            "ds_weight": ds_weight,
            "hy_weight": hy_weight,
            "regime": market_regime,
            "risk_level": risk_level,
        })
        if len(self.decision_history) > 100:
            self.decision_history = self.decision_history[-100:]

        logger.info(f"[MetaAgent] 裁决: {final_decision} 置信度:{final_confidence:.2f} 权重 DS:{ds_weight:.2f}/HY:{hy_weight:.2f}")

        # ── 第一优先修复·解门锁监控（2026-08-15）：累计统计 + 周期摘要 ──
        # ★ 2026-08-15 审计P3修复：统计与日志在锁内完成（多账号并发 read-modify-write 竞态）
        with _GATE_STATS_LOCK:
            GATE_STATS["total_decisions"] += 1
            if final_decision == "HOLD":
                GATE_STATS["holds"] += 1
            else:
                GATE_STATS["traded"] += 1
            if GATE_STATS["total_decisions"] % _GATE_LOG_EVERY == 0:
                _tot = GATE_STATS["total_decisions"]
                _gates = ", ".join(
                    f"{k}={v}({v / _tot:.0%})" for k, v in GATE_STATS["gates"].items()
                )
                logger.info(
                    f"[MetaAgent][解门锁监控] 累计决策{_tot} 总HOLD率{GATE_STATS['holds'] / _tot:.0%} "
                    f"交易率{GATE_STATS['traded'] / _tot:.0%} | 各门触发: {_gates or '无'}"
                )
        # ── 大脑审计：记录元智能体最终裁决输出 ──
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("meta_agent", "output",
                    output={"decision": final_decision, "confidence": round(final_confidence, 3),
                            "position_intent": _intent, "target_risk_pct": _target_risk,
                            "ds_vote": ds_final, "hy_vote": hy_final, "chronos_vote": chronos_dir,
                            "ts_fusion_dir": ts_fusion_dir, "vision_dir": vision_dir},
                    adopted=1, consumer="trade_executor",
                    notes=f"DS权{ds_weight:.2f}/HY权{hy_weight:.2f}" + (" [逆共识降级→共识方向]" if _contrarian_downgraded else "") + f" 校准后置信{_calib_display:.0%}")
        except Exception:
            pass
        return decision

    def feedback(self, decision: DebateDecision, was_profitable: bool, profit: float,
                 mt5_account_id: str = "", event_time=None, ticket=None):
        """交易反馈——更新模型权重，并把权重变化写入 evolution log

        Args:
            event_time: 触发此反馈的交易平仓时间（datetime）。
                       若传入，进化日志的 created_at 将使用此时间（与订单时间对齐）；
                       若不传，回退到写入时刻（datetime.utcnow）。
            ticket: 平仓订单的 MT5 票号（用于时间线展示"复盘哪一单"）。

        ★ 进化时间线覆盖保证：每次 feedback 都无条件写一条「订单复盘」记录，
          确保今日每一笔平仓订单都在时间线可见（产品亮点：AI 对每单都复盘进化）。
        """
        # ★ 2026-08-06 Fix1：HOLD 中性评分——根治 DS 独裁反馈螺旋
        #   旧逻辑：HY 投 HOLD 时，只要合并决策发生了盈利交易（profit!=0），
        #   HY 就被判"错"→ HY 准确率螺旋下跌→权重归零→DS 独裁。
        #   新逻辑：模型投 HOLD 返回 None（中性），不参与准确率统计（既不奖也不罚）；
        #   只有投 BUY/SELL 才用真实盈亏更新权重。HY 的权重只反映"它敢喊方向时的准度"，
        #   保守不再被系统性惩罚。配合 Fix2 封顶，双模型回到真平等。
        def _directional_correct(vote: str):
            if vote in ("BUY", "SELL"):
                return was_profitable
            return None  # HOLD：中性，不更新准确率

        # ★ 2026-08-11 本地副驾补位：DS 失联时其票来自本地 Qwen3-8B，
        #   记入 DS 准确率会污染「云端双脑」权重（本地 8B 方向接近随机，
        #   Fin-Bias ACL2026）→ 跳过 DS 统计，只让混元正常统计。
        _ds_fallback = bool(getattr(decision, "deepseek_local_fallback", False))
        ds_correct = None if _ds_fallback else _directional_correct(decision.deepseek_vote)
        hy_correct = _directional_correct(decision.hunyuan_vote)

        # 记录变化前权重/准确率
        ds_acc_before = round(self.deepseek_perf.recent_accuracy, 3)
        hy_acc_before = round(self.hunyuan_perf.recent_accuracy, 3)

        # ★ 利润只计一次：由"第一个参与统计的方向性模型"记录，HOLD(中性)模型不录利润
        _profit_recorded = False
        if ds_correct is not None:
            self.deepseek_perf.update(ds_correct, profit, decision.deepseek_weight,
                                      add_profit=not _profit_recorded)
            _profit_recorded = True
        if hy_correct is not None:
            self.hunyuan_perf.update(hy_correct, profit, decision.hunyuan_weight,
                                     add_profit=not _profit_recorded)
            _profit_recorded = True

        # ★ M3a：每笔反馈后落盘（限频30s；首次反馈立即写，确保重启可恢复）
        self.save_state()

        ds_acc_after = round(self.deepseek_perf.recent_accuracy, 3)
        hy_acc_after = round(self.hunyuan_perf.recent_accuracy, 3)

        logger.info(
            f"[MetaAgent] 反馈: DS{'✓' if ds_correct else '✗'}"
            f"{'(本地副驾)' if _ds_fallback else ''} "
            f"(acc:{ds_acc_before:.2f}→{ds_acc_after:.2f}) | "
            f"HY{'✓' if hy_correct else '✗'} "
            f"(acc:{hy_acc_before:.2f}→{hy_acc_after:.2f})"
        )

        # 写 evolution log（只记录有显著变化的或纠正/打脸场景）
        if self.evo_logger is None:
            return

        try:
            # ★ HOLD 中性标记：✓/✗/—（—表示观望不参与统计，避免误显示"错"）
            _ds_mark = "✓" if ds_correct is True else ("—" if ds_correct is None else "✗")
            _hy_mark = "✓" if hy_correct is True else ("—" if hy_correct is None else "✗")

            # ★ 每笔订单复盘记录（无条件，保证时间线覆盖今日全部平仓订单）
            self.evo_logger({
                "kind": "trade_review",
                "subject": f"订单#{ticket}" if ticket else "订单复盘",
                "before_value": None,
                "after_value": None,
                "delta": f"{'+' if was_profitable else '−'}{abs(profit):.2f}",
                "reason": (
                    f"复盘: DS={_ds_mark}({decision.deepseek_vote}) "
                    f"HY={_hy_mark}({decision.hunyuan_vote}) "
                    f"盈亏 {profit:+.2f}"
                ),
                "mt5_account_id": mt5_account_id,
                "event_time": event_time,
                "meta_json": {
                    "ticket": ticket,
                    "deepseek_vote": decision.deepseek_vote,
                    "hunyuan_vote": decision.hunyuan_vote,
                    "ds_correct": ds_correct,
                    "hy_correct": hy_correct,
                    "profit": profit,
                },
            })

            # ── 以下为"显著变化"才记录的权重演化（HOLD 中性模型跳过，不刷屏）──
            # DeepSeek 权重/准确率变化
            if ds_correct is not None and (abs(ds_acc_after - ds_acc_before) >= 0.01 or not ds_correct):
                self.evo_logger({
                    "kind": "weight_update",
                    "subject": "DeepSeek",
                    "before_value": f"acc={ds_acc_before}",
                    "after_value": f"acc={ds_acc_after}",
                    "delta": f"{ds_acc_after - ds_acc_before:+.3f}",
                    "reason": ("DeepSeek 方向正确" if ds_correct else
                               f"DeepSeek 方向错误（profit {profit:+.2f}）"),
                    "mt5_account_id": mt5_account_id,
                    "event_time": event_time,
                    "meta_json": {
                        "vote": decision.deepseek_vote,
                        "weight": decision.deepseek_weight,
                        "hunyuan_vote": decision.hunyuan_vote,
                        "hunyuan_weight": decision.hunyuan_weight,
                        "profit": profit,
                    },
                })
            if hy_correct is not None and (abs(hy_acc_after - hy_acc_before) >= 0.01 or not hy_correct):
                self.evo_logger({
                    "kind": "weight_update",
                    "subject": "Hunyuan",
                    "before_value": f"acc={hy_acc_before}",
                    "after_value": f"acc={hy_acc_after}",
                    "delta": f"{hy_acc_after - hy_acc_before:+.3f}",
                    "reason": ("Hunyuan 方向正确" if hy_correct else
                               f"Hunyuan 方向错误（profit {profit:+.2f}）"),
                    "mt5_account_id": mt5_account_id,
                    "event_time": event_time,
                    "meta_json": {
                        "vote": decision.hunyuan_vote,
                        "weight": decision.hunyuan_weight,
                        "deepseek_vote": decision.deepseek_vote,
                        "deepseek_weight": decision.deepseek_weight,
                        "profit": profit,
                    },
                })
            # 双模型一致方向性错误（均非 HOLD 且都错）→ 标记为系统级反思
            if ds_correct is False and hy_correct is False:
                self.evo_logger({
                    "kind": "consensus",
                    "subject": "DS+HY",
                    "before_value": "共识",
                    "after_value": "误判",
                    "delta": "↓",
                    "reason": f"双模型同向且都错（profit {profit:+.2f}），触发系统级反思",
                    "mt5_account_id": mt5_account_id,
                    "event_time": event_time,
                    "meta_json": {
                        "deepseek_vote": decision.deepseek_vote,
                        "hunyuan_vote": decision.hunyuan_vote,
                        "profit": profit,
                    },
                })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MetaAgent] evolution log 写入失败: {e}")
