"""
NumPy 方向终审器单元测试。
"""
import numpy as np
import pytest

from app.services.numpy_direction_guard import NumpyDirectionGuard, DirectionGuardResult, max_conflict


def _make_trend(n: int, start: float = 2500.0, step: float = 1.0, noise: float = 0.3) -> np.ndarray:
    """构造带噪声的趋势序列。"""
    return start + np.arange(n) * step + np.random.randn(n) * noise


def test_hold_skips_review():
    guard = NumpyDirectionGuard()
    r = guard.review(list(np.arange(100) + 2500.0), 2600.0, "HOLD")
    assert r.conflict_level == "none"
    assert "HOLD" in r.reason


def test_too_short_data_skips():
    guard = NumpyDirectionGuard()
    r = guard.review([2500.0] * 20, 2500.0, "BUY")
    assert r.conflict_level == "none"
    assert "数据不足" in r.reason


def test_buy_at_extreme_top_blocked():
    """BUY 在超买+价格突破布林带上轨 → major 冲突，建议 HOLD。"""
    np.random.seed(1)
    closes = _make_trend(120, start=2500.0, step=0.2, noise=0.5)
    # 末端连续 10 根加速上冲，形成超买+突破上轨
    closes[-10:] += np.linspace(0, 35, 10)
    current = float(closes[-1] + 2.0)

    guard = NumpyDirectionGuard()
    r = guard.review(closes.tolist(), current, "BUY")
    assert r.conflict_level == "major"
    assert r.suggested_direction == "HOLD"
    assert "接飞刀" in r.reason or "上轨" in r.reason or "超买" in r.reason


def test_sell_at_extreme_bottom_blocked():
    """SELL 在超卖+价格跌破布林带下轨 → major 冲突，建议 HOLD。"""
    np.random.seed(2)
    closes = _make_trend(100, start=2600.0, step=-0.5)
    closes[-5:] += np.linspace(0, -25, 5)
    current = float(closes[-1] - 2.0)

    guard = NumpyDirectionGuard()
    r = guard.review(closes.tolist(), current, "SELL")
    assert r.conflict_level == "major"
    assert r.suggested_direction == "HOLD"


def test_sell_in_uptrend_is_conflict():
    """上升趋势中逆势 SELL → 至少 minor 冲突。"""
    np.random.seed(3)
    closes = _make_trend(120, start=2500.0, step=0.2, noise=0.5)
    current = float(closes[-1])

    guard = NumpyDirectionGuard()
    r = guard.review(closes.tolist(), current, "SELL")
    assert r.conflict_level in ("minor", "major")
    assert "趋势斜率为正" in r.reason or "延伸" in r.reason or "上轨" in r.reason


def test_buy_in_uptrend_passes():
    """正常上升趋势中 BUY → 无冲突。"""
    np.random.seed(4)
    closes = _make_trend(120, start=2500.0, step=0.15, noise=0.5)
    current = float(closes[-1])

    guard = NumpyDirectionGuard()
    r = guard.review(closes.tolist(), current, "BUY")
    assert r.conflict_level in ("none", "minor")


def test_features_exposed():
    guard = NumpyDirectionGuard()
    closes = _make_trend(100, start=2500.0, step=0.3)
    r = guard.review(closes.tolist(), float(closes[-1]), "BUY")
    assert "ma50" in r.features
    assert "rsi14" in r.features
    assert "bb_position" in r.features


def test_max_conflict_order():
    assert max_conflict("none", "major") == "major"
    assert max_conflict("minor", "none") == "minor"
    assert max_conflict("major", "minor") == "major"
    assert max_conflict("none", "none") == "none"
