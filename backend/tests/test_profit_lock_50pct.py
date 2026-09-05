"""★ 2026-08-10 浮盈达标即锁(partial_close 50%)回归测试。

用户场景：SELL 1手，浮盈峰值 $1211(12.11 价格偏移)，当前 $1091(10.91 价格偏移)。
用户诉求："早就可以平仓了"——等回吐才平太晚，看到大浮盈就该锁一部分。
本机制：持仓曾进入利润区(peak ≥0.5×ATR) 且 当前浮盈 ≥0.3×ATR → 平 50% 锁利、留 50% 让趋势奔跑。
"""
import pytest

from app.services.smart_exit import evaluate_position as smart_evaluate_position


def _pos(ticket, ptype, open_price, current, sl=0.0, volume=1.0):
    return {
        "ticket": ticket, "type": ptype, "volume": volume,
        "price_open": open_price, "open_price": open_price,
        "price_current": current, "current_price": current,
        "sl": sl, "tp": 0.0, "profit": open_price - current if ptype == "sell" else current - open_price,
        "symbol": "XAUUSD",
    }


class _S:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _strat():
    return _S(
        smart_tp_enabled=True, trailing_atr_mult=1.5, trailing_activate_after_tp2=True,
        breakeven_after_tp1=True, breakeven_buffer_points=1.0,
        tp1_atr_mult=1.0, tp1_close_pct=0.4, tp2_atr_mult=1.5, tp2_close_pct=0.3,
        tp3_atr_mult=3.5, tp3_close_pct=0.2, ai_reverse_close_confidence=0.70,
        enable_trailing_sl=True,
    )


ATR = 15.78


def test_user_scenario_partial_lock_50pct():
    """用户截图场景：peak=12.11(0.77×ATR), move=10.91(0.69×ATR) → 不再锁50%。

    ★ 2026-08-17 契约更新：用户裁决「不要锁50%，要么持有要么全走」——
    SMART_EXIT_LOCK50_ENABLED=False（config.py 权威源）。浮盈达标不再
    partial_close 50%，由「回吐全平」+「AI 出场」负责锁利/离场。
    本用例守护：禁用后该场景**不得**再出现 partial_close（防旧代码复活）。
    """
    p = _pos(7001, "sell", 4354.45, 4343.54, sl=4350.45)  # 浮盈 10.91
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=12.11,
    )
    assert r["action"] != "partial_close", (
        f"锁50%已禁用（SMART_EXIT_LOCK50_ENABLED=False），仍出现 partial_close: {r['action']}: {r['reason']}"
    )


def test_no_partial_lock_below_profit_zone():
    """未进利润区(peak < 0.5×ATR)不触发。"""
    p = _pos(7002, "sell", 4354.45, 4349.0, sl=4350.0)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=3.0,  # < 7.89 利润区
    )
    assert r["action"] != "partial_close"


def test_no_partial_lock_below_30pct_atr():
    """当前浮盈 <0.3×ATR 不触发(防噪音)。"""
    p = _pos(7003, "sell", 4354.45, 4350.0, sl=4350.0)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=10.0,  # 进利润区
    )
    # move=4.45 < 0.3×ATR=4.73 → 不触发
    assert r["action"] != "partial_close"


def test_no_peak_no_partial():
    """无峰值(None)时不误触发。"""
    p = _pos(7004, "sell", 4354.45, 4349.0, sl=4350.0)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=None,
    )
    assert r["action"] != "partial_close"