"""★ 2026-08-10 新增：趋势强弱(ADX) 自适应手数回归测试。

背景（用户需求）："底线(min_lot)~红线(max_lot)之间按趋势强弱自动开单手数"。
手数引擎新增 `adx` 参数（实时 H1 ADX）→ 强趋势加码(×1.25)、震荡缩手(×0.75)、
取不到(None/0) → ×1.0 不干预（向后兼容，旧行为零变化）。

断言全部落在"手数随趋势强弱单调变化 + 兼容性"，不碰风控/钳制细节。
"""
import pytest

from app.services.intelligent_sizing import compute_intelligent_size


class _S:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


BASE = dict(
    min_lot_per_trade=0.01, max_lot_per_trade=0.05, max_position_lots=0.05,
    capital_source="live", base_capital=1000.0, sizing_scale_mode="auto",
    max_risk_per_trade_pct=2.0, volatility_factor=1.0, same_direction_decay=0.5,
    sizing_mode="smart",
)


def _size(adx, conf=0.75, balance=2741.78, atr=26.0):
    return compute_intelligent_size(
        balance=balance, atr=atr, signal_confidence=conf,
        same_direction_count=0, strategy=_S(**BASE), adx=adx,
    )


def test_adx_strong_trend_upsizes():
    """强趋势(ADX≥30)手数必须大于震荡(ADX≤20)，且至少开出 min_lot。"""
    weak = _size(15)["lots"]
    strong = _size(33)["lots"]
    assert strong > weak, f"强趋势手数{strong} 应大于 震荡{weak}"
    assert strong > 0.01, "强趋势连底线 min_lot 都开不出"


def test_adx_none_or_zero_is_neutral():
    """取不到 ADX(None/0) 时必须完全中立(×1.0)——与旧行为逐位等价。"""
    assert _size(None)["lots"] == _size(0)["lots"]


def test_adx_mid_linear_between_bounds():
    """20<ADX<30 线性区间：ADX 越大乘数越大。"""
    a23 = _size(23)["lots"]
    a27 = _size(27)["lots"]
    assert a27 > a23, f"ADX27手数{a27} 应大于 ADX23手数{a23}"


def test_adx_components_exposed():
    """决策明细必须暴露 adx 与 adx_mult（前端审计用）。"""
    c = _size(33)["components"]
    assert c.get("adx_mult", 0) >= 1.25
    assert c.get("adx") == 33


def test_adx_does_not_break_risk_cap():
    """即便趋势强加码，最终手数仍被 max_lot 钳制（红线不可突破）。"""
    lots = _size(60)["lots"]
    assert lots <= 0.05 + 1e-9, f"手数{lots} 突破红线 max_lot=0.05"
