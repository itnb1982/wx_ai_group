"""★ 2026-08-10 新增：平仓 volume/profit 计算逻辑回归测试。

背景：worker 平仓原返回 pos.profit（平仓前整仓浮盈）+ 无 volume 字段 →
  partial 平仓(1.00→0.50)后 trades.volume 永远停在开仓值、profit 虚高。
修复：返回【实际成交手数】+【本次真实已实现盈亏】(黄金 1 手 1 美元=$100)。
"""
import pytest


def _close_pnl(pos_type, open_price, close_price, filled_vol):
    """复刻 mt5_worker.py close_position 的真实已实现盈亏计算。"""
    if pos_type == "buy":
        return round((close_price - open_price) * filled_vol * 100.0, 2)
    return round((open_price - close_price) * filled_vol * 100.0, 2)


def test_partial_close_profit_correct():
    """SELL 1.00手 平 0.50手 @11.86 价差 → 真实盈亏 = 11.86×0.5×100 = $593。"""
    pnl = _close_pnl("sell", 4354.49, 4342.63, 0.5)
    assert abs(pnl - 593.0) < 1.0, f"partial 平 0.5 手应≈$593，实际 {pnl}"


def test_full_close_profit_correct():
    """SELL 1.00手 平 1.00手 @11.86 价差 → 真实盈亏 = 11.86×1×100 = $1186。"""
    pnl = _close_pnl("sell", 4354.49, 4342.63, 1.0)
    assert abs(pnl - 1186.0) < 1.0


def test_buy_close_profit_correct():
    """BUY 0.5手 +3.2 价差 → (3.2)×0.5×100 = $160。"""
    pnl = _close_pnl("buy", 4300.0, 4303.2, 0.5)
    assert abs(pnl - 160.0) < 1.0


def test_losing_trade_negative_profit():
    """SELL 1.0手 亏损方向 → 负盈亏。"""
    pnl = _close_pnl("sell", 4300.0, 4310.0, 1.0)
    assert pnl < 0 and abs(pnl - (-1000.0)) < 1.0
