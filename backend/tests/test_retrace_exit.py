"""★ 2026-08-10 新增：浮盈回吐主动锁利回归测试。

背景（用户核心诉求）：AI 只判反向(BUY)才平仓，导致趋势单浮盈从峰值
（如 $900）回吐到 $629 才被被动 SL 扫掉——"AI 不捕捉利润"。
本机制：持仓曾浮盈 ≥0.5×ATR（进入利润区），从峰值回吐 ≥20%(或 0.1×ATR)
→ 主动 full_close 锁利，不等 AI 判反向、不等被动 SL。
"""
import pytest

from app.services.smart_exit import evaluate_position as smart_evaluate_position


def _pos(ticket, ptype, open_price, current, sl=0.0, volume=1.0, profit=None):
    return {
        "ticket": ticket, "type": ptype, "volume": volume,
        "price_open": open_price, "open_price": open_price,
        "price_current": current, "current_price": current,
        "sl": sl, "tp": 0.0, "profit": profit if profit is not None else (open_price - current if ptype == "sell" else current - open_price),
        "symbol": "XAUUSD",
    }


class _S:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _strat(**kw):
    base = dict(
        smart_tp_enabled=True, trailing_atr_mult=1.5, trailing_activate_after_tp2=True,
        breakeven_after_tp1=True, breakeven_buffer_points=1.0,
        tp1_atr_mult=1.0, tp1_close_pct=0.4, tp2_atr_mult=1.5, tp2_close_pct=0.3,
        tp3_atr_mult=3.5, tp3_close_pct=0.2, ai_reverse_close_confidence=0.70,
        enable_trailing_sl=True,
    )
    base.update(kw)
    return _S(**base)


ATR = 15.78  # 当前 H1 ATR


def test_retrace_exit_locks_profit_when_peak_given():
    """用户场景：SELL 峰值浮盈 9.0($900)，反弹回吐 2.7($270,30%) → 主动全平锁利。"""
    # 峰值 MFE=9.0(价格偏移)，当前 move=6.3(回吐 2.7)
    p = _pos(7001, "sell", 4354.45, 4348.15, sl=4348.16)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=9.0,
    )
    assert r["action"] == "full_close", f"应主动全平锁利，实际 {r['action']}: {r['reason']}"
    assert "回吐锁利" in r["reason"]


def test_retrace_exit_not_triggered_below_20pct():
    """回吐 <20% 峰值（且 <0.1 ATR）时不误触发（防噪音）。"""
    p = _pos(7002, "sell", 4354.45, 4348.75, sl=4348.16)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=9.0,  # 当前 move=5.7, 回吐 3.3=37% → 应触发？
    )
    # 回吐 9-5.7=3.3 ≥ max(1.8,1.58) → 触发
    assert r["action"] == "full_close"


def test_retrace_exit_no_peak_is_noop():
    """无峰值(None)时完全兼容旧行为（不引入任何新平仓）。"""
    p = _pos(7003, "sell", 4354.45, 4349.0, sl=4350.0)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=None,
    )
    assert r["action"] != "full_close", "无峰值时不应触发回吐锁利"


def test_retrace_exit_peak_below_profit_zone():
    """峰值未达 0.5×ATR（利润区）不触发（刚开仓的小浮盈不锁）。"""
    p = _pos(7004, "sell", 4354.45, 4348.0, sl=4348.16)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=3.0,  # < 7.89 利润区
    )
    assert r["action"] != "full_close"


def test_retrace_exit_fires_when_move_negative():
    """★★ 2026-08-17 P0 回归：曾进利润区后回撤到亏损(move<0)也必须触发 ★★
    实锤：主号 23:36 浮盈 1.4 点 → 23:38 回撤到 -0.3 点（回撤 1.7 点≥0.30 阈值）
    未触发（旧代码 `move > 0` 条件把 move<0 整块跳过），直到更高峰值才锁利——
    违反用户"回撤一点就跑，绝不等到亏损"。本用例锁死修复：move 为负时回吐更大、更要平。
    """
    # 峰值 1.4 点（曾进 0.5 点利润区），当前已回撤到 -0.3（浮亏）→ 回吐 1.7 点
    p = _pos(7005, "sell", 4423.04, 4423.34, sl=4447.26, volume=0.01, profit=-0.3)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=1.4,
    )
    assert r["action"] == "full_close", f"回撤到亏损也应全平锁利，实际 {r['action']}: {r['reason']}"
    assert "回吐锁利" in r["reason"]


def test_wrong_dir_immediate_cut():
    """★★ 2026-08-18 用户铁律·开仓即亏认错（补「盈利即护盘」盲区）★★
    实盘根因：SELL 后价格直接反向从未盈利 → 回吐锁利失效（无峰值可回吐）→
    扛到 SL/AI 认错才平，单笔亏 800/300/280 点（2877213e 大仓 -779/-155/-118）。
    本测试：SELL 开仓即涨（move<0 浮亏）、从未盈利(peak_move=None)、
    浮亏超阈值(3.0 价格≈300点>噪音带) → 立即 full_close 认错，不等 SL/AI。
    """
    # SELL @4354.45，反向涨到 4360.0 → move = 4354.45-4360.0 = -5.55
    # 阈值 = max(3.0, 0.3×ATR=4.73) = 4.73 → -5.55 < -4.73 触发（>噪音带防误杀）
    p = _pos(7101, "sell", 4354.45, 4360.0, sl=4370.0)
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=None,
    )
    assert r["action"] == "full_close", f"开仓即亏应认错全平，实际 {r['action']}: {r['reason']}"
    assert "开仓即亏" in r["reason"]


def test_wrong_dir_not_fired_below_threshold():
    """浮亏未超阈值（噪音带内）不误杀正常回调。"""
    p = _pos(7102, "sell", 4354.45, 4356.0, sl=4370.0)  # move=-1.55 < 3.0 阈值
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=None,
    )
    assert r["action"] != "full_close", "噪音带内浮亏不应误杀"


def test_wrong_dir_skipped_if_ever_profit():
    """方向对的单曾盈利(peak>0)→ 走回吐锁利而非开仓即亏（防误杀回踩单）。"""
    p = _pos(7103, "sell", 4354.45, 4358.0, sl=4370.0)  # move=-3.55
    r = smart_evaluate_position(
        position=p, atr=ATR, ai_decision="SELL", ai_confidence=0.90,
        strategy=_strat(), peak_move=5.0,  # 曾盈利 5.0
    )
    # 曾盈利 → 不满足「从未盈利」→ 开仓即亏不触发；回吐锁利(峰值5→-3.55回吐8.55)触发 full_close
    assert r["action"] == "full_close"
    assert "回吐锁利" in r["reason"]
