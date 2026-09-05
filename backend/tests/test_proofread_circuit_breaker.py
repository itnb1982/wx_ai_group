"""
Phase 9.1 校对员闭环断路器 — 决策层测试
=====================================
验证：sev=major 时，本地校对员把本笔决策降级为 HOLD（不改方向、不投票），
并打上 proofread_blocked / block_reason 标记，供执行器与前端识别。

不依赖真实 Ollama：直接 monkeypatch `local_llm_service.proofread`。
"""
import pytest

from app.core.meta_agent import DebateDecision
from app.services.local_llm_service import ProofreadResult

pytestmark = pytest.mark.unit


def _make_decision(decision="BUY"):
    return DebateDecision(
        decision=decision,
        confidence=0.8,
        deepseek_weight=0.5,
        hunyuan_weight=0.5,
        deepseek_vote=decision,
        hunyuan_vote=decision,
        reasoning_summary="测试决策",
        risk_level="medium",
    )


def test_major_severity_demotes_to_hold(monkeypatch):
    """代码审计 major → 决策被降级为 HOLD + 打标（断路器生效）。

    ★ 2026-08-08：止损挂反是**纯算术**可判定的，来源必须是 code_severity。
    原用例只设了合并后的 severity，未区分来源；补上 code_* 字段后语义才准确。
    """
    from app.core import debate_engine

    def fake_proofread(payload, snap):
        issues = ["止损方向错误：BUY 的止损 2660 必须低于入场 2650"]
        return ProofreadResult(
            ok=False,
            issues=issues,
            severity="major",
            code_severity="major",   # ← 算术审计判定，唯一可拦单的依据
            llm_severity="none",
            code_issues=list(issues),
            latency_ms=120.0,
            model="qwen3:8b",
        )

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("BUY")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert d.decision == "HOLD", "major 应将决策降级为 HOLD"
    assert d.proofread_blocked is True
    assert "校对员" in d.block_reason and "止损方向" in d.block_reason
    assert d.proofread_severity == "major"
    assert d.proofread_issues  # 问题已写回


def test_minor_severity_keeps_direction(monkeypatch):
    """minor 仅告警，不改方向、不拦单。"""
    from app.core import debate_engine

    def fake_proofread(payload, snap):
        return ProofreadResult(
            ok=False,
            issues=["止损过近：距入场仅 1.50 美元（<3），易秒触"],
            severity="minor",
            latency_ms=90.0,
            model="qwen3:8b",
        )

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("BUY")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert d.decision == "BUY", "minor 不应改方向"
    assert d.proofread_blocked is False
    assert d.proofread_severity == "minor"


def test_clean_decision_untouched(monkeypatch):
    """无问题决策保持原样，不降级。"""
    from app.core import debate_engine

    def fake_proofread(payload, snap):
        return ProofreadResult(
            ok=True, issues=[], severity="none", latency_ms=80.0, model="qwen3:8b"
        )

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("SELL")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert d.decision == "SELL"
    assert d.proofread_blocked is False


def test_hold_decision_skips_proofread(monkeypatch):
    """HOLD 决策不校对（无方向无可查），标记保持 skipped。"""
    from app.core import debate_engine

    called = {"n": 0}

    def fake_proofread(payload, snap):
        called["n"] += 1
        return ProofreadResult(ok=True, issues=[], severity="none",
                               latency_ms=1.0, model="qwen3:8b")

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("HOLD")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert called["n"] == 0, "HOLD 不应触发校对调用"
    assert d.proofread_status == "skipped"


# ============================================================================
#  ★ 2026-08-08 防回归：LLM 的主观 major 绝不允许拦单
#
#  背景：Qwen3-8B 的金融方向判断接近随机（Fin-Bias, ACL2026）。一旦让它的
#  主观结论触发断路器，就等于给了一个随机数发生器否决权，会砍掉本该赚钱的
#  交易——直接违背项目最高红线「提准非拦截」。
#
#  下面两个用例是这条红线的"电子围栏"：谁要是把断路器改回读 res.severity，
#  这里立刻红。请不要通过修改断言来"修复"它们。
# ============================================================================
def test_llm_only_major_must_not_block(monkeypatch):
    """LLM 判 major、代码审计判 none → 必须放行，只记录告警。"""
    from app.core import debate_engine

    def fake_proofread(payload, snap):
        return ProofreadResult(
            ok=False,
            issues=["理由文本提到看空，但方向是 BUY，疑似自相矛盾"],
            severity="major",      # 合并后的展示级别（会是 major）
            code_severity="none",  # ← 算术审计没发现任何结构问题
            llm_severity="major",  # ← 纯主观判断
            code_issues=[],
            latency_ms=800.0,
            model="qwen3:8b",
        )

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("BUY")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert d.decision == "BUY", (
        "8B 的主观判断把单子砍了——这会砍掉本该赚钱的交易，"
        "违背『提准非拦截』红线"
    )
    assert d.proofread_blocked is False
    # 但疑点必须如实记录，供人工复盘
    assert d.proofread_issues, "放行不等于不记录，疑点仍须写回"
    assert d.proofread_severity == "major"


def test_llm_malformed_output_must_not_block(monkeypatch):
    """LLM 输出格式抖动（漏 severity 字段）推断出的级别不得砍单。

    真实链路：`local_llm_service.proofread` 遇到缺字段时会推断为 minor，
    这里直接模拟其产物，确认断路器对它无动于衷。
    """
    from app.core import debate_engine

    def fake_proofread(payload, snap):
        return ProofreadResult(
            ok=False,
            issues=["缺 confidence"],
            severity="minor",
            code_severity="none",
            llm_severity="minor",
            code_issues=[],
            latency_ms=500.0,
            model="qwen3:8b",
        )

    monkeypatch.setattr(
        "app.services.local_llm_service.proofread", fake_proofread
    )

    engine = debate_engine.DebateEngine.__new__(debate_engine.DebateEngine)
    d = _make_decision("SELL")
    engine._apply_proofread(d, {"current_price": 2650.0})

    assert d.decision == "SELL"
    assert d.proofread_blocked is False
