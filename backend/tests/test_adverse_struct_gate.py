# -*- coding: utf-8 -*-
"""
反向即跑·结构确认门（2026-08-12）决策矩阵单测。

复刻 trade_executor._adverse_move_exit 内的门控表达式（与源码 3370-3371 行一致）：
    _confirm = (_dir=="buy"  and bias=="bearish") or
               (_dir=="sell" and bias=="bullish")
语义：BUY 顺势=看涨→不利=看跌才砍；SELL 顺势=看跌→不利=看涨才砍。
验证「提准非拦截」铁律：健康回踩不砍、真反转照砍、逼近SL必砍、结构未知回退原阈值。
无需 MT5/DB，纯逻辑回归。
"""
import sys

_MIN_PTS = 6.0
_MULT = 0.6


def decide(_dir, _open, _cur, _sl, _bias, _struct_ok):
    _sl_dist = abs(_sl - _open) if _sl > 0 else 0.0
    _adverse = (_cur - _open) if _dir == "sell" else (_open - _cur)
    if _adverse <= 0:
        return "HOLD_PROFIT"           # 仍在赚钱方向，不跑
    _th = max(_MIN_PTS, _sl_dist * _MULT)
    _near_sl = (_sl_dist > 0 and _adverse >= _sl_dist * 0.95)
    if _struct_ok and _bias and not _near_sl:
        _confirm = (_dir == "buy" and _bias == "bearish") or \
                   (_dir == "sell" and _bias == "bullish")
        if not _confirm:
            return "HOLD_STRUCT_OK"     # 健康回踩不砍，交原生SL
    if _adverse >= _th - 1e-9:
        return "CLOSE"
    return "HOLD_BELOW_TH"


def run():
    cases = [
        # (说明, dir, open, cur, sl, bias, struct_ok, 期望)
        ("A BUY回踩结构顺向(看涨)→不砍",   "buy",  2000, 1993,  1990, "bullish", True,  "HOLD_STRUCT_OK"),
        ("B BUY回踩结构确认反转(看跌)→砍",  "buy",  2000, 1993,  1990, "bearish", True,  "CLOSE"),
        ("C BUY逼近SL(≥0.95SL)保命必砍",    "buy",  2000, 1990.5, 1990, "bullish", True,  "CLOSE"),
        ("D BUY结构未知/过期回退原阈值砍",  "buy",  2000, 1993,  1990, "bullish", False, "CLOSE"),
        ("E BUY仍在盈利方向→不跑",          "buy",  2000, 2003,  1990, "bullish", True,  "HOLD_PROFIT"),
        ("F SELL回踩结构顺向(看跌)→不砍",   "sell", 2000, 2007,  2010, "bearish", True,  "HOLD_STRUCT_OK"),
        ("G SELL回踩结构确认反转(看涨)→砍",  "sell", 2000, 2007,  2010, "bullish", True,  "CLOSE"),
        ("H SELL逼近SL(≥0.95SL)保命必砍",    "sell", 2000, 2009.6, 2010, "bullish", True,  "CLOSE"),
        ("I SELL结构未知回退原阈值砍",       "sell", 2000, 2007,  2010, "bearish", False, "CLOSE"),
    ]
    fail = 0
    for desc, _dir, _o, _c, _s, _b, _ok, exp in cases:
        got = decide(_dir, _o, _c, _s, _b, _ok)
        ok = (got == exp)
        fail += (0 if ok else 1)
        print(f"[{'OK ' if ok else 'FAIL'}] {desc}: got={got} exp={exp}")
    print(f"\n结果: {len(cases)-fail}/{len(cases)} 通过" + ("" if fail == 0 else f", {fail} 失败"))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    run()
