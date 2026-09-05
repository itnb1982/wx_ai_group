"""
AI 决策溯源链 — Chronos 第三票必须结构化落到 DebateDecision（Phase 1 / V6）

这个文件盯的是一类特别隐蔽的故障：**值算出来了，但没交出去**。

Chronos 在 meta_agent 里实实在在参与了加权裁决（第三票），
三模型共振也算出来了，Q 分和 P10/P50/P90 也都拿到了 ——
但这些全留在函数局部变量里，没有一个写进 DebateDecision。

后果有两层，且都在真金白银地伤害系统：
  · 执行器 `getattr(ai_decision, "chronos_agree", False)` 恒为 False
    → 三模型共振豁免分支永不生效 → 所有非强趋势交易被无差别加 +0.03
      置信门槛 → 直接违背"提准非拦截"铁律，在砍交易笔数。
  · 前端只能从 reasoning 长文本里做字符串解析拿 Chronos 票和 Q 分
    → V6 要求的"三票 + Q + 分位"溯源无法结构化展示。

getattr 带默认值的写法让这类 bug 不报错、不告警、静默降级，
所以必须有测试把"字段确实被填了"钉死。
"""
import pytest

# 导入顺序不能反：app.core.__init__ 会拉起 deepseek_client → app.services →
# trade_executor → 回头 import app.core.debate_engine，形成环。
# 先让 app.services 完成初始化即可打破（现有 test_decision_gates 同此做法）。
# 这个环本身是待偿技术债，属后续解耦范围，不在本测试的整改边界内。
import app.services.trade_executor  # noqa: F401  （仅为破环，勿删）

from app.core.meta_agent import MetaAgent, DebateDecision


# ─────────────────── 构造裁决输入的最小脚手架 ───────────────────

def _analysis(decision: str, confidence: float = 0.8, risk_score: int = 4) -> dict:
    return {
        "decision": decision,
        "confidence": confidence,
        "risk_assessment": {"risk_score": risk_score},
    }


def _market(chronos_dir: str = "NEUTRAL", *, q=None, p10=None, p50=None,
            tp_ceiling=None, uncertainty: float = 0.0, regime: str = "") -> dict:
    """构造带 meta_quality 的行情快照。

    chronos_dir 为 NEUTRAL 时模拟"本地时序未出方向票"，
    此时 Chronos 不参与加权，也不应产生共振。

    注意 tp_ceiling 是 meta_quality 独立算出的 `chronos_tp_ceiling`，
    并非直接等于 p90（后者是原始分位，前者含方向感知与体制过滤）。
    """
    mq = {"chronos_dir": chronos_dir, "uncertainty": uncertainty}
    if regime:
        mq["regime"] = regime
    # ★ 2026-08-17 修复：生产 meta_quality 注入的 key 是 p10_final/p50_final
    #   （chronos_service 输出），测试旧 key p10/p50 导致透传恒 None。
    for k, v in (("q", q), ("p10_final", p10), ("p50_final", p50),
                 ("chronos_tp_ceiling", tp_ceiling)):
        if v is not None:
            mq[k] = v
    return {"meta_quality": mq}


def _judge(ds: str, hy: str, chronos: str, **mkw) -> DebateDecision:
    agent = MetaAgent()
    return agent.adjudicate(
        deepseek_analysis=_analysis(ds),
        hunyuan_analysis=_analysis(hy),
        deepseek_rebuttal={},
        hunyuan_rebuttal={},
        market_data=_market(chronos, **mkw),
    )


# ─────────────────── 三模型共振：豁免分支的命根子 ───────────────────

@pytest.mark.unit
def test_three_way_consensus_is_exposed_on_decision():
    """★ 核心：DS/HY/Chronos 三票同向时，chronos_agree 必须为 True。

    这个字段是执行器体制门"共振豁免"的唯一依据。它一旦恒为 False，
    共振（系统能给出的最高 conviction 信号）反而和普通信号一样挨罚。
    """
    d = _judge("BUY", "BUY", "BUY")
    assert d.decision == "BUY"
    assert getattr(d, "chronos_agree", None) is True, (
        "三模型同向却没标记共振——执行器的共振豁免会永久失效"
    )


@pytest.mark.unit
def test_no_consensus_when_chronos_disagrees():
    """Chronos 反向时绝不能误标共振——误标会让本该谨慎的单被放行。"""
    d = _judge("BUY", "BUY", "SELL")
    assert getattr(d, "chronos_agree", None) is False


@pytest.mark.unit
def test_no_consensus_when_chronos_neutral():
    """Chronos 没投方向票 = 没有第三方背书，只能算双模型共识，不是三方共振。"""
    d = _judge("BUY", "BUY", "NEUTRAL")
    assert getattr(d, "chronos_agree", None) is False


@pytest.mark.unit
def test_no_consensus_when_cloud_models_split():
    """云模型自己就打架时，即便 Chronos 站队其中一边也不构成三方共振。"""
    d = _judge("BUY", "SELL", "BUY")
    assert getattr(d, "chronos_agree", None) is False


# ─────────────────── 溯源字段：前端不该靠正则啃长文本 ───────────────────

@pytest.mark.unit
def test_chronos_vote_and_weight_are_structured():
    """Chronos 的票和权重必须是结构化字段。

    此前前端只能从 reasoning_summary 里字符串解析 "Chronos:SELL(w=0.25)"，
    文案一改就崩。
    """
    d = _judge("BUY", "BUY", "SELL")
    assert getattr(d, "chronos_vote", None) == "SELL"
    w = getattr(d, "chronos_weight", None)
    assert isinstance(w, float) and w > 0, "Chronos 参与了加权，权重却没交出来"


@pytest.mark.unit
def test_quality_score_and_quantiles_are_structured():
    """Q 分与 P50/P90 必须结构化落盘（P10 早已有字段，这里补齐另两个）。"""
    d = _judge("BUY", "BUY", "BUY",
               q=0.82, p10=4200.5, p50=4250.0, tp_ceiling=4300.5, regime="HIGH")
    assert getattr(d, "q_score", None) == pytest.approx(0.82)
    assert d.chronos_p10 == pytest.approx(4200.5)
    assert getattr(d, "chronos_p50", None) == pytest.approx(4250.0)
    assert getattr(d, "chronos_tp_ceiling", None) == pytest.approx(4300.5), (
        "止盈天花板未透传，方向感知止盈会退化"
    )
    assert d.quality_regime == "HIGH"


@pytest.mark.unit
def test_missing_meta_quality_degrades_quietly():
    """Chronos 服务挂掉时（meta_quality 缺失）必须安静降级，不能抛异常。

    本地时序模型是增强项，不是交易的前置依赖——它挂了系统要照常做决策。
    """
    agent = MetaAgent()
    d = agent.adjudicate(
        deepseek_analysis=_analysis("BUY"),
        hunyuan_analysis=_analysis("BUY"),
        deepseek_rebuttal={},
        hunyuan_rebuttal={},
        market_data={},          # 完全没有 meta_quality
    )
    assert d.decision in ("BUY", "SELL", "HOLD")
    assert getattr(d, "chronos_agree", None) is False
    # 票面走 _normalize_decision 的三态制（NEUTRAL→HOLD），与裁决内部口径一致
    assert getattr(d, "chronos_vote", "") == "HOLD"
    assert getattr(d, "q_score", None) is None
    # ★ 关键区分：票面 HOLD 有歧义——可能是"Chronos 建议观望"，
    #   也可能是"Chronos 压根没跑起来"。这两件事对用户意义完全不同。
    #   靠 weight=0 表达"没参与加权"，前端据此才能诚实展示而非谎称 AI 建议观望。
    assert getattr(d, "chronos_weight", None) == 0.0, (
        "Chronos 缺席时权重必须为 0，否则前端会把'服务没跑'误报成'AI建议观望'"
    )


# ─────────────────── 与执行器的实际契约 ───────────────────

@pytest.mark.unit
def test_executor_consensus_exemption_actually_fires():
    """★ 端到端契约：执行器读到的 chronos_agree 必须能真正触发豁免。

    单独断言字段值还不够——真正要证明的是"执行器那行 getattr 拿到了 True"。
    执行器代码：`if getattr(ai_decision, "chronos_agree", False):` → 豁免；
    否则 penalty += 0.03。这里模拟同一次读取。
    """
    consensus = _judge("BUY", "BUY", "BUY")
    split = _judge("BUY", "BUY", "NEUTRAL")

    def _penalty_of(dec) -> float:
        # 复刻 trade_executor 体制门(soft) 非强趋势分支的判定
        return 0.0 if getattr(dec, "chronos_agree", False) else 0.03

    assert _penalty_of(consensus) == 0.0, "三模型共振仍被加罚，共振豁免没生效"
    assert _penalty_of(split) == 0.03, "无共振时应保留 +0.03（提准非拦截的既定行为）"


@pytest.mark.unit
def test_provenance_fields_have_safe_defaults():
    """新字段必须全部带默认值。

    DebateDecision 在多处被构造（含测试与旧路径），
    任何一个必填新字段都会让那些构造点当场 TypeError。
    """
    d = DebateDecision(
        decision="HOLD",
        confidence=0.5,
        deepseek_weight=0.5,
        hunyuan_weight=0.5,
        deepseek_vote="HOLD",
        hunyuan_vote="HOLD",
        reasoning_summary="",
        risk_level="low",
    )
    assert d.chronos_agree is False
    assert d.chronos_vote == "HOLD"
    assert d.chronos_weight == 0.0
    assert d.q_score is None
    assert d.chronos_p50 is None
