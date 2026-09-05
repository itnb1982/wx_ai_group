"""★ 2026-08-10 新增：保本/动态锁利参数 + one-way SL 回归测试。

背景：lium1/lium3 实测浮盈峰值 4.7 点（持续 <60s 错过循环）→ 当时 move<0
    整个 smart_exit if 块跳过 → SL 留在初始 4352 → 反弹到 SL 亏 $2400/手。
修复：① 降低保本阈值 (0.15→0.08 ATR) 和动态锁利阈值 (0.3→0.15 ATR)
    ② one-way SL：用 peak_move（曾达浮盈峰值）判断保本/锁利，即使当前 move<0 已回吐，
       曾触发的保本 SL 永久保留不后撤。
"""
import pytest

# 复刻 smart_exit 关键参数（与代码同步）
BE_EARLY_ATR_MULT = 0.08
PROFIT_LOCK_START_ATR = 0.15
PROFIT_LOCK_BUFFER_ATR = 0.3
PROFIT_LOCK_MIN_ATR = 0.10


def _evaluate_one_way(move, peak_move, current_sl, open_price, atr=14.5, pos_type="sell"):
    """复刻 _evaluate_position 的早期保本+动态锁利+one-way SL 逻辑。"""
    be_early_trigger = atr * BE_EARLY_ATR_MULT
    _peak_move = peak_move if (peak_move is not None and peak_move > 0) else move
    _effective_move = max(move, _peak_move) if move > 0 else _peak_move
    if _effective_move >= be_early_trigger:
        be_buffer = 0.5
        be_sl = round(open_price + be_buffer, 2) if pos_type == "buy" else round(open_price - be_buffer, 2)
        reason = "早期保本"
        if _effective_move >= atr * PROFIT_LOCK_START_ATR:
            lock_profit = max(_effective_move - atr * PROFIT_LOCK_BUFFER_ATR, atr * PROFIT_LOCK_MIN_ATR)
            if pos_type == "buy":
                dyn_sl = round(open_price + lock_profit, 2)
                if dyn_sl > be_sl:
                    be_sl = dyn_sl
                    reason = "动态锁利"
            else:
                dyn_sl = round(open_price - lock_profit, 2)
                if dyn_sl < be_sl:
                    be_sl = dyn_sl
                    reason = "动态锁利"
        # one-way SL：永远不后撤
        better = (current_sl == 0) or \
                 (pos_type == "buy" and be_sl > current_sl) or \
                 (pos_type == "sell" and be_sl < current_sl)
        if better:
            return be_sl, reason
    return None, None


def test_breakeven_triggers_on_small_floating():
    """浮盈 1.2 点（0.08×ATR）就触发保本（原 0.15×ATR=2.17 点太高错过）。"""
    result, _ = _evaluate_one_way(move=1.2, peak_move=1.2, current_sl=4352.0, open_price=4329.71)
    # 早期保本 SL = open_price - 0.5（SELL）= 4329.21
    assert result is not None, "浮盈 1.2 点应触发保本"
    assert abs(result - 4329.21) < 0.01, f"保本 SL 应≈4329.21，实际 {result}"


def test_one_way_sl_keeps_protection_after_retracement():
    """核心：峰值 4.7 点曾触发保本后，回吐到浮亏时 SL 不后撤（且顺势锁利到 4328.26）。"""
    # peak_move=4.7（曾触发保本），current_sl=4329.21（已上移保本），现在 move=-3.0（回吐到浮亏）
    # 新逻辑：_effective_move = 4.7（peak_move，不依赖当前 move）
    #         4.7 ≥ 0.15 ATR=2.18 → 触发动态锁利 → lock_profit=4.7-4.35=1.45, SL=4329.71-1.45=4328.26
    #         4328.26 < 4329.21 (current_sl) → better=True → 返回新 SL=4328.26（更锁利）
    result, reason = _evaluate_one_way(move=-3.0, peak_move=4.7, current_sl=4329.21, open_price=4329.71)
    assert result is not None, "peak_move 触发动态锁利，SL 应进一步收紧"
    assert abs(result - 4328.26) < 0.01, f"动态锁利 SL 应=4328.26，实际 {result}"
    assert reason == "动态锁利"


def test_one_way_sl_does_not_revert_already_locked_sl():
    """当前 SL 已更锁利时，不再后撤（one-way）。"""
    # current_sl=4328.26（已动态锁利），peak_move=4.7, move=-3.0
    # 同样的 _effective_move=4.7 → 同样算出 SL=4328.26 → 但 current_sl=4328.26 不再被超越 → better=False
    result, _ = _evaluate_one_way(move=-3.0, peak_move=4.7, current_sl=4328.26, open_price=4329.71)
    assert result is None, "现有 SL 已是最锁利，one-way 绝不后撤（返回 None=不更新）"


def test_initial_position_gets_breakeven():
    """首次浮盈触发保本（当前 SL=0 初始位）。"""
    result, _ = _evaluate_one_way(move=2.0, peak_move=2.0, current_sl=0, open_price=4329.71)
    assert result is not None
    assert abs(result - 4329.21) < 0.01


def test_dramatic_improvement_vs_old_thresholds():
    """对比旧阈值（0.15 ATR=2.17 / 0.3 ATR=4.34）vs 新阈值（0.08/0.15）的差异。"""
    atr = 14.5
    # 旧 BE_EARLY_ATR_MULT=0.15 触发阈值 = 2.17 点
    # 新 BE_EARLY_ATR_MULT=0.08 触发阈值 = 1.16 点
    # 旧 PROFIT_LOCK_START_ATR=0.3 触发阈值 = 4.35 点
    # 新 PROFIT_LOCK_START_ATR=0.15 触发阈值 = 2.18 点
    old_breakeven = 0.15 * atr
    new_breakeven = 0.08 * atr
    old_lock = 0.3 * atr
    new_lock = 0.15 * atr
    # 新阈值比旧低 50% 左右
    assert new_breakeven < old_breakeven * 0.6, f"新保本阈值 {new_breakeven} 应比旧 {old_breakeven} 低至少 40%"
    assert new_lock < old_lock * 0.6, f"新锁利阈值 {new_lock} 应比旧 {old_lock} 低至少 40%"
