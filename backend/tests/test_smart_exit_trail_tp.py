# -*- coding: utf-8 -*-
"""
2026-08-16 审计 P0-1 回归测试：_trail_tp 方向修复。
旧实现 BUY 写成 current_price - trail_distance（TP 压现价下方→静默失效/瞬间平仓），
SELL 同理反向。本测试锁死正确方向：BUY TP 上移、SELL TP 下移。
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import smart_exit as se  # noqa: E402


def test_buy_trail_tp_above_price():
    # BUY：现价 3500，TP 初始 3505（上方 5 点，接近），ATR=10 → 新 TP=3508 高于现价且上移
    new_tp = se._trail_tp(current_tp=3505.0, pos_type="buy", current_price=3500.0,
                          atr=10.0, trail_mult=1.0)
    assert new_tp is not None, "BUY 追踪止盈不应返回 None"
    assert new_tp > 3500.0, f"BUY TP 必须高于现价，实际 {new_tp}"
    assert new_tp > 3505.0, f"BUY TP 只上移不回调，实际 {new_tp} <= 原 TP 3505"
    # 距离 = ATR*1.0*0.8 = 8 → 3508；应 > 3500+min_gap(3)=3503 ✓


def test_sell_trail_tp_below_price():
    # SELL：现价 3500，TP 初始 3495（下方 5 点，接近），ATR=10 → 新 TP=3492 低于现价且下移
    new_tp = se._trail_tp(current_tp=3495.0, pos_type="sell", current_price=3500.0,
                          atr=10.0, trail_mult=1.0)
    assert new_tp is not None, "SELL 追踪止盈不应返回 None"
    assert new_tp < 3500.0, f"SELL TP 必须低于现价，实际 {new_tp}"
    assert new_tp < 3495.0, f"SELL TP 只下移不回调，实际 {new_tp} >= 原 TP 3495"


def test_buy_trail_tp_not_lower_when_price_falls():
    # BUY 但价格未创新高：现价 3500，原 TP 3515（已高于现价+距离）→ 不回调，返回 None
    new_tp = se._trail_tp(current_tp=3515.0, pos_type="buy", current_price=3500.0,
                          atr=10.0, trail_mult=1.0)
    # 3500+8=3508 < 3515 → 新 TP 未超过原 TP → 不更新
    assert new_tp is None or new_tp >= 3515.0, "BUY TP 不得低于原 TP"


def test_sell_trail_tp_not_raise_when_price_rises():
    # SELL 但价格未创新低：现价 3500，原 TP 3485（已低于现价-距离）→ 不回调
    new_tp = se._trail_tp(current_tp=3485.0, pos_type="sell", current_price=3500.0,
                          atr=10.0, trail_mult=1.0)
    # 3500-8=3492 > 3485 → 新 TP 未低于原 TP → 不更新
    assert new_tp is None or new_tp <= 3485.0, "SELL TP 不得高于原 TP"


def test_zero_tp_returns_none():
    # current_tp <= 0 → 直接 None（不产生非法 TP）
    assert se._trail_tp(0, "buy", 3500.0, 10.0, 1.0) is None


def _run_all():
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed.append((fn.__name__, str(e)))
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{passed}/{len(funcs)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
