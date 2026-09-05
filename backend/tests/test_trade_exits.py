"""★ 2026-08-10 新增：平仓明细(trade_exits)与主行累计语义回归测试。

背景：_record_close 原每次平仓 UPDATE 同一条 trades 行 → partial_close 多次平仓时
历史明细被最后一次覆盖（lium3 #377415351 的 18:05 平0.5手+716.50 被 18:22 平0.01手+10.34
覆盖），DB 聚合与 MT5 真实 deals 严重不符。result 字段还被塞 `closed_by_ai|长文本`，
导致 result='win'/'loss' 统计永远 0。

修复：每次平仓 INSERT 一条 trade_exits 明细；trades 主行 volume=开仓量(不覆盖)、
profit/net_profit=累计已实现、result=短标记(win/loss/breakeven/partial)。
本测试复刻核心判定与累计逻辑（纯函数，不依赖 DB）。
"""
import pytest


def _short_result(profit, partial):
    """复刻 _record_close 的 result 短标记判定。"""
    if partial:
        return "partial"
    if abs(profit) < 0.01:
        return "breakeven"
    return "win" if profit > 0 else "loss"


def _accumulate(main_vol, main_profit, exits, close_result, partial):
    """复刻主行累计 + 明细追加逻辑（简化模型）。"""
    pnl = float(close_result["profit"])
    vol = float(close_result["volume"])
    exits.append({
        "exit_volume": round(vol, 2),
        "profit": round(pnl, 2),
        "result": _short_result(pnl, partial),
        "partial": partial,
    })
    # 主行：volume 保持开仓量（不覆盖），profit 累计
    main_vol_new = main_vol if main_vol and main_vol > 0 else vol
    main_profit_new = round(main_profit + pnl, 2)
    return main_vol_new, main_profit_new


def test_partial_twice_keeps_two_exit_records():
    """两次 partial_close → trade_exits 应有 2 条明细（不覆盖丢失）。"""
    exits = []
    mv, mp = 1.00, 0.0
    # 第一次 partial：平 0.5 手，价差 14.33 → 716.50
    mv, mp = _accumulate(mv, mp, exits, {"volume": 0.5, "profit": 716.50}, True)
    # 第二次 partial：平 0.3 手，价差 5.37 → 161.10
    mv, mp = _accumulate(mv, mp, exits, {"volume": 0.3, "profit": 161.10}, True)
    assert len(exits) == 2, "两次 partial 应有 2 条明细"
    assert exits[0]["exit_volume"] == 0.5 and exits[0]["profit"] == 716.50
    assert exits[1]["exit_volume"] == 0.3 and exits[1]["profit"] == 161.10


def test_main_row_keeps_open_volume_accumulates_profit():
    """主行 volume 保持开仓量(1.00 不被覆盖)，profit 累计求和。"""
    exits = []
    mv, mp = 1.00, 0.0
    mv, mp = _accumulate(mv, mp, exits, {"volume": 0.5, "profit": 716.50}, True)
    mv, mp = _accumulate(mv, mp, exits, {"volume": 0.3, "profit": 161.10}, True)
    mv, mp = _accumulate(mv, mp, exits, {"volume": 0.01, "profit": 10.34}, True)
    assert mv == 1.00, "主行 volume 应保持开仓量 1.00"
    assert abs(mp - (716.50 + 161.10 + 10.34)) < 0.01, f"主行 profit 应累计，实际 {mp}"


def test_result_short_markers():
    """result 短标记：partial→partial / 盈利→win / 亏损→loss / 零→breakeven。"""
    assert _short_result(593.0, True) == "partial"
    assert _short_result(593.0, False) == "win"
    assert _short_result(-343.0, False) == "loss"
    assert _short_result(0.0, False) == "breakeven"
    assert _short_result(0.005, False) == "breakeven"


def test_full_close_single_exit_record():
    """full close（非 partial）→ 1 条明细，result 按盈亏判定。"""
    exits = []
    mv, mp = _accumulate(0.5, 0.0, exits, {"volume": 0.5, "profit": -343.0}, False)
    assert len(exits) == 1
    assert exits[0]["result"] == "loss"
    assert mp == -343.0
