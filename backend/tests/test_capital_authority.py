"""L1 单测：本金与手数边界权威链。

这是 V6 §4.2/§4.3 铁律的可执行规格说明书。
任何人改动 capital_authority.py 后必须让本文件全绿，否则视为破坏权威链。
"""
import pytest

from app.services.capital_authority import (
    BROKER_MIN_LOT,
    LOT_HARD_BOUNDS_DEFAULT,
    LOT_SCALE_CAP,
    REF_CAPITAL,
    CapitalDecision,
    effective_capital,
    resolve_lot_bounds,
    risk_check_capital,
)

pytestmark = pytest.mark.unit


class _Strategy:
    """模拟 ORM 策略对象（只带用到的字段）"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ══════════════════════════════════════════════════════════
# 一、本金权威链：input > manual > live
# ══════════════════════════════════════════════════════════
class TestCapitalPriority:

    def test_input_capital_beats_everything(self):
        """客户输入本金权限最高，压过 manual 与实时余额"""
        s = _Strategy(capital_source="manual", base_capital=32000.0)
        d = effective_capital(s, balance=2408.0, input_capital=5000.0)
        assert d.value == 5000.0
        assert d.source == "input"
        assert d.balance == 2408.0  # 真实余额必须始终留档供审计

    def test_input_capital_read_from_strategy_field(self):
        """未显式传参时，从策略字段读 input_capital（字段落库后自动生效）"""
        s = _Strategy(input_capital=8000.0, capital_source="live", base_capital=0)
        d = effective_capital(s, balance=2408.0)
        assert (d.value, d.source) == (8000.0, "input")

    def test_manual_used_when_no_input(self):
        s = _Strategy(capital_source="manual", base_capital=32000.0)
        d = effective_capital(s, balance=2408.0)
        assert (d.value, d.source) == (32000.0, "manual")

    def test_live_is_default(self):
        s = _Strategy(capital_source="live", base_capital=32000.0)
        d = effective_capital(s, balance=2408.0)
        assert (d.value, d.source) == (2408.0, "live")

    def test_manual_with_zero_base_falls_back_to_live(self):
        """manual 但没填本金 → 不能返回 0 本金，必须回退实时余额"""
        s = _Strategy(capital_source="manual", base_capital=0)
        d = effective_capital(s, balance=2408.0)
        assert (d.value, d.source) == (2408.0, "live")

    def test_missing_capital_source_defaults_to_live(self):
        """旧库/旧 dict 没有 capital_source 字段 → 按 live 处理（向后兼容）"""
        d = effective_capital(_Strategy(base_capital=32000.0), balance=1500.0)
        assert (d.value, d.source) == (1500.0, "live")

    def test_dict_strategy_supported(self):
        d = effective_capital(
            {"capital_source": "manual", "base_capital": 7000.0}, balance=100.0
        )
        assert (d.value, d.source) == (7000.0, "manual")

    def test_none_strategy_never_crashes(self):
        """策略拿不到时不许抛异常，退回实时余额"""
        d = effective_capital(None, balance=999.0)
        assert (d.value, d.source) == (999.0, "live")

    @pytest.mark.parametrize("bad", ["abc", None, ""])
    def test_garbage_input_capital_ignored(self, bad):
        s = _Strategy(capital_source="live")
        d = effective_capital(s, balance=1000.0, input_capital=bad)
        assert d.source == "live"

    def test_negative_balance_clamped_to_zero(self):
        d = effective_capital(_Strategy(), balance=-50.0)
        assert d.value == 0.0

    def test_is_synthetic_flag(self):
        assert effective_capital(_Strategy(input_capital=1.0), 100.0).is_synthetic
        assert effective_capital(
            _Strategy(capital_source="manual", base_capital=1.0), 100.0
        ).is_synthetic
        assert not effective_capital(_Strategy(), 100.0).is_synthetic


# ══════════════════════════════════════════════════════════
# 二、等价性护栏：input 缺省时必须与重构前一模一样
# ══════════════════════════════════════════════════════════
class TestBackwardEquivalence:
    """复刻重构前 intelligent_sizing L115-120 的原始判断，逐例比对。"""

    @staticmethod
    def _legacy(capital_source, base_capital, balance):
        if str(capital_source).lower() == "manual" and base_capital > 0:
            return base_capital
        return balance

    @pytest.mark.parametrize("src", ["live", "manual", "MANUAL", "", None])
    @pytest.mark.parametrize("base", [0, 1000.0, 32000.0])
    @pytest.mark.parametrize("bal", [0.0, 2408.0, 989000.0])
    def test_equivalent_to_legacy_when_no_input_capital(self, src, base, bal):
        s = _Strategy(capital_source=src, base_capital=base)
        got = effective_capital(s, balance=bal).value
        want = self._legacy(src if src else "live", base, bal)
        assert got == want, f"口径漂移: src={src} base={base} bal={bal}"


# ══════════════════════════════════════════════════════════
# 三、手数边界
# ══════════════════════════════════════════════════════════
class TestLotBoundsLegacyScaling:
    """历史行为（enforce_hard_bounds=False）—— 保留以支持紧急回退与等价对拍"""

    def test_auto_scales_up_with_capital(self):
        """$10000 本金 = 10x 基准 → 上限放大 10 倍"""
        s = _Strategy(max_lot_per_trade=1.0, max_position_lots=1.0,
                      sizing_scale_mode="auto")
        b = resolve_lot_bounds(s, 10 * REF_CAPITAL, enforce_hard_bounds=False)
        assert b.scale == 10.0
        assert b.max_lot == 10.0
        assert b.max_position_lots == 10.0
        assert b.raw_max_lot == 1.0  # 原始硬边界值必须保留供溯源

    def test_auto_scale_capped_at_50x(self):
        s = _Strategy(max_lot_per_trade=1.0, max_position_lots=1.0)
        b = resolve_lot_bounds(s, 1_000_000.0, enforce_hard_bounds=False)
        assert b.scale == LOT_SCALE_CAP
        assert b.max_lot == 50.0

    def test_manual_mode_no_scaling(self):
        s = _Strategy(max_lot_per_trade=2.0, max_position_lots=3.0,
                      sizing_scale_mode="manual")
        b = resolve_lot_bounds(s, 100_000.0, enforce_hard_bounds=False)
        assert (b.max_lot, b.max_position_lots, b.scale) == (2.0, 3.0, 1.0)


class TestLotHardBounds:
    """V6 §4.3 手数硬边界铁律 —— 当前默认行为"""

    def test_default_is_hard_bounds_on(self):
        """不传参数时必须默认开启硬边界（防有人误改默认值）"""
        assert LOT_HARD_BOUNDS_DEFAULT is True
        s = _Strategy(max_lot_per_trade=1.0, max_position_lots=1.0)
        b = resolve_lot_bounds(s, 1_000_000.0)
        assert b.max_lot == 1.0, "默认必须挡住放大，否则客户设 1 手会被开成 50 手"

    def test_million_dollar_account_capped_at_setting(self):
        """生产实证复现：liumanchun1 $989k 设 1.0 手，绝不能再开出 11 手"""
        s = _Strategy(min_lot_per_trade=0.5, max_lot_per_trade=1.0,
                      max_position_lots=1.0, sizing_scale_mode="auto")
        b = resolve_lot_bounds(s, 988_998.0)
        assert b.max_lot == 1.0
        assert b.max_position_lots == 1.0

    def test_hard_bounds_still_allows_tightening(self):
        """硬边界只挡"放大"，不挡小本金"收紧"（收紧是更安全的方向）"""
        s = _Strategy(max_lot_per_trade=1.0, max_position_lots=1.0)
        b = resolve_lot_bounds(s, 500.0)
        assert b.max_lot == 0.5

    def test_strategy_field_can_override_global(self):
        """策略级字段优先于全局常量（客户可单账号回退）"""
        s = _Strategy(max_lot_per_trade=1.0, max_position_lots=1.0,
                      enforce_lot_hard_bounds=False)
        b = resolve_lot_bounds(s, 1_000_000.0)
        assert b.max_lot == 50.0

    def test_explicit_arg_beats_everything(self):
        s = _Strategy(max_lot_per_trade=1.0, enforce_lot_hard_bounds=False)
        assert resolve_lot_bounds(s, 1e6, enforce_hard_bounds=True).max_lot == 1.0


class TestLotBoundsCommon:

    def test_zero_balance_keeps_scale_one(self):
        b = resolve_lot_bounds(_Strategy(max_lot_per_trade=1.0), 0.0)
        assert b.scale == 1.0
        assert b.max_lot == 1.0

    def test_min_lot_never_below_broker_minimum(self):
        b = resolve_lot_bounds(_Strategy(min_lot_per_trade=0.001), 1000.0)
        assert b.min_lot == BROKER_MIN_LOT


# ══════════════════════════════════════════════════════════
# 四、风控反钳口径（根治"手数按设定本金算、风控按真余额拒"的打架）
# ══════════════════════════════════════════════════════════
class TestRiskCheckCapital:

    def test_live_uses_balance(self):
        d = CapitalDecision(2408.0, "live", "live(balance)", 2408.0)
        assert risk_check_capital(d) == 2408.0

    def test_manual_higher_than_balance_clamped(self):
        """设了 $32000 但真账户只有 $2408 → 风控只认 $2408（不许拿不存在的钱冒险）"""
        d = CapitalDecision(32000.0, "manual", "manual", 2408.0)
        assert risk_check_capital(d) == 2408.0

    def test_manual_lower_than_balance_respected(self):
        """客户主动调低到 $500（保守）→ 风控尊重低设定，不放大到余额"""
        d = CapitalDecision(500.0, "manual", "manual", 2408.0)
        assert risk_check_capital(d) == 500.0

    def test_balance_unavailable_falls_back_to_setting(self):
        """IPC 故障拿不到余额时不能返回 0（会导致永不开仓）"""
        d = CapitalDecision(5000.0, "input", "input", 0.0)
        assert risk_check_capital(d) == 5000.0
