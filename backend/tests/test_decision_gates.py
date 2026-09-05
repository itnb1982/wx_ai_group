"""确定性门控回归：_apply_decision_gates 在「体制门 × 空头约束 × 方向」全组合下的行为。

原为 backend/test_decision_gates.py 脚本式验证（无 def test_，从未被 pytest 收集）。
Phase -1 收编：改为参数化用例，纳入回归保护。

不依赖 MT5 / DB —— 通过 object.__new__ 绕过 __init__ 重依赖，
并 monkeypatch _fresh_strat 注入门控模式。
"""
import pytest

from app.services.trade_executor import TradeExecutor


class _FakeDecision:
    """最小 AI 决策替身，只暴露门控读取的两个字段。"""

    def __init__(self, decision: str, confidence: float = 0.80):
        self.decision = decision
        self.confidence = confidence


def _make_exec(regime_mode: str, short_mode: str) -> TradeExecutor:
    """构造仅用于测试 _apply_decision_gates 的裸实例。"""
    ex = object.__new__(TradeExecutor)

    def fake_fresh(field, default=None):
        if field == "regime_open_mode":
            return regime_mode
        if field == "short_guard_mode":
            return short_mode
        return default

    ex._fresh_strat = fake_fresh
    return ex


def _md(regime: str) -> dict:
    """常规行情。

    注意：门控内部会用 reversal_sentinel.evaluate(market_data) 重算哨兵，
    不采信外部注入的哨兵值 —— 这是防呆设计（只信原始行情）。
    因此要测「谷底 REVERSE_BUY」必须构造真能算出该信号的行情，见 _md_bottom()。
    """
    return {
        "regime": {
            "regime": regime,
            "at_stale_top": False,
            "at_stale_bottom": False,
            "extension_z": 0.0,
            "rsi_h1": 50.0,
        },
        "smc_features": {"per_tf": {}, "global_bias": "neutral"},
    }


def _md_bottom() -> dict:
    """趋势末端谷底行情 → 哨兵应重算出 REVERSE_BUY。"""
    return {
        "regime": {
            "regime": "trend_down",
            "at_stale_top": False,
            "at_stale_bottom": True,
            "extension_z": -3.0,
            "rsi_h1": 20.0,
        },
        "smc_features": {"per_tf": {}, "global_bias": "bearish"},
    }


# (用例名, 体制门模式, 空头约束模式, 方向, 体制, 期望放行, 期望拦截原因子串, 是否用谷底行情)
_CASES = [
    # ── 体制门 off：任何体制都放行 ──
    ("off+弱体制+BUY 放行", "off", "off", "BUY", "range", True, None, False),
    ("off+强趋势+BUY 放行", "off", "off", "BUY", "trend_up", True, None, False),
    # ── 体制门 soft：强趋势 0 惩罚，弱体制软惩罚但不拦截 ──
    ("soft+强趋势+BUY 0惩罚", "soft", "off", "BUY", "trend_up", True, None, False),
    ("soft+弱体制(range)+BUY 软惩罚不拦截", "soft", "off", "BUY", "range", True, None, False),
    ("soft+波动(volatile)+BUY 软惩罚不拦截", "soft", "off", "BUY", "volatile", True, None, False),
    # ── 体制门 hard（★ 2026-08-07 重设计后语义）──
    #   旧语义：非强趋势体制一律硬拦 → 震荡市完全不交易，砍掉大量可盈利信号。
    #   新语义：只硬拦「接飞刀/逆势」，震荡市双向放行以保护交易笔数。
    #   这是「提准非拦截」铁律的落地，下面的期望值刻意钉死该语义，
    #   若有人改回「震荡全拦」，这三条会立刻失败。
    ("hard+强趋势+BUY 放行", "hard", "off", "BUY", "trend_up", True, None, False),
    ("hard+震荡(range)+BUY 放行(不砍交易笔数)", "hard", "off", "BUY", "range", True, None, False),
    ("hard+波动(volatile)+BUY 放行(不砍交易笔数)", "hard", "off", "BUY", "volatile", True, None, False),
    # hard 真正该拦的：逆势接刀
    ("hard+空头体制+BUY 硬拦(逆势抄底)", "hard", "off", "BUY", "trend_down", False, "体制门", False),
    ("hard+多头体制+SELL 硬拦(逆势摸顶)", "hard", "off", "SELL", "trend_up", False, "体制门", False),
    # ── 空头约束 off：任何 SELL 放行 ──
    ("空头off+非空头+SELL 放行", "off", "off", "SELL", "trend_up", True, None, False),
    # ── 空头约束 soft：非空头软惩罚，空头体制放行，均不拦截 ──
    ("空头soft+空头体制+SELL 放行", "off", "soft", "SELL", "trend_down", True, None, False),
    ("空头soft+非空头+SELL 软惩罚不拦截", "off", "soft", "SELL", "trend_up", True, None, False),
    # ── 空头约束 hard：仅「体制转空 且 哨兵未判谷底」才放行 ──
    ("空头hard+空头体制+SELL 放行", "off", "hard", "SELL", "trend_down", True, None, False),
    ("空头hard+非空头+SELL 硬拦截", "off", "hard", "SELL", "trend_up", False, "空头约束", False),
    ("空头hard+真实谷底+SELL 硬拦截", "off", "hard", "SELL", "trend_down", False, "空头约束", True),
    # ── 组合：体制 hard + 空头 hard ──
    ("组合+强下跌+SELL 放行", "hard", "hard", "SELL", "trend_down", True, None, False),
    ("组合+强上涨+SELL 硬拦截(空头约束)", "hard", "hard", "SELL", "trend_up", False, "空头约束", False),
    ("组合+强上涨+BUY 放行", "hard", "hard", "BUY", "trend_up", True, None, False),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,regime_mode,short_mode,direction,regime,expect_passed,expect_block,use_bottom",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_decision_gate_matrix(
    name, regime_mode, short_mode, direction, regime, expect_passed, expect_block, use_bottom
):
    ex = _make_exec(regime_mode, short_mode)
    market = _md_bottom() if use_bottom else _md(regime)
    gate = ex._apply_decision_gates(_FakeDecision(direction, 0.80), market)

    assert gate["passed"] is expect_passed, (
        f"{name}: 期望 passed={expect_passed}，实际 {gate['passed']}，"
        f"block_reason={gate['block_reason']!r}"
    )
    if expect_block:
        assert expect_block in gate["block_reason"], (
            f"{name}: 拦截原因应含 {expect_block!r}，实际 {gate['block_reason']!r}"
        )


@pytest.mark.unit
def test_hard_mode_never_blocks_ranging_market():
    """★ 铁律护栏：hard 档在震荡/波动体制下必须双向放行。

    背景：曾有版本让 hard 档「非强趋势一律拦」，结果震荡市完全停止交易，
    交易笔数与净利润双双腰斩（用户明确定性为失败的过度过滤）。
    2026-08-07 重设计为「只拦接飞刀/逆势」。

    本用例是该决策的守门人：任何人把震荡市改回硬拦，这里立刻红。
    """
    ex = _make_exec("hard", "off")
    for regime in ("range", "volatile"):
        for direction in ("BUY", "SELL"):
            gate = ex._apply_decision_gates(_FakeDecision(direction, 0.80), _md(regime))
            assert gate["passed"] is True, (
                f"震荡市被硬拦会砍掉交易笔数（违反「多交易多赚钱」铁律）："
                f"{direction}@{regime} 被拦，原因={gate['block_reason']!r}"
            )


@pytest.mark.unit
def test_hard_mode_blocks_counter_trend_knife_catching():
    """hard 档必须拦住真正危险的「逆势接飞刀」：空头体制抄底、多头体制摸顶。"""
    ex = _make_exec("hard", "off")

    buy_in_downtrend = ex._apply_decision_gates(_FakeDecision("BUY", 0.80), _md("trend_down"))
    assert buy_in_downtrend["passed"] is False, "空头体制逆势 BUY 抄底应被硬拦"
    assert "体制门" in buy_in_downtrend["block_reason"]

    sell_in_uptrend = ex._apply_decision_gates(_FakeDecision("SELL", 0.80), _md("trend_up"))
    assert sell_in_uptrend["passed"] is False, "多头体制逆势 SELL 摸顶应被硬拦"
    assert "体制门" in sell_in_uptrend["block_reason"]


@pytest.mark.unit
def test_gate_off_mode_never_blocks():
    """双 off 模式是「零硬屏蔽」底线：任何方向 × 任何体制都不得拦截。

    对应用户铁律「零硬屏蔽」——off 档必须是真的全放行，
    否则等于偷偷加了一层过滤，会砍掉本可盈利的信号。
    """
    ex = _make_exec("off", "off")
    for direction in ("BUY", "SELL"):
        for regime in ("trend_up", "trend_down", "range", "volatile"):
            gate = ex._apply_decision_gates(_FakeDecision(direction, 0.80), _md(regime))
            assert gate["passed"] is True, (
                f"off 档不得拦截：{direction}@{regime} 被拦，"
                f"原因={gate['block_reason']!r}"
            )
