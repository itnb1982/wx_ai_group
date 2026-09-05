# -*- coding: utf-8 -*-
"""结构突破 BOS/CHoCH 检测测试（2026-08-17 调研落地，SMC/ICT 趋势启动识别）。

调研依据（≥3 独立出处交叉验证）：
- TradingView BOS/CHOCH Demand&Supply：BOS=收盘破摆动点且趋势延续；CHoCH=逆势首破
- backtrex.com / liquidityhunters / forexmt4indicators：只认收盘价；多周期一致硬规则
- coinxsight / kaigai-fx：突破确认 = 收盘 + ADX>20 + displacement；不追突破蜡烛

场景（对应今晚 21:45-22:30 实盘教训：M15 四连涨 37 点 DS 却全程看空）：
① 区间上沿突破 → bullish BOS（趋势启动信号，应引导 AI 顺势）
② 下跌趋势中首破 → bullish CHoCH（反转预警）
③ 区间震荡 → 不误报
"""
import pytest

from app.services.regime_detect import detect_structure_break


def _mk_range_break():
    """12 根区间震荡 + 1 根大阳突破（21:45 实盘场景）。"""
    range_bars = []
    for i in range(12):
        o = 4387 + (i % 3)
        h = o + 5 if i % 2 == 0 else o + 3
        l = o - 4
        c = o + 1 if i % 2 == 0 else o - 1
        range_bars.append({"open": o, "high": h, "low": l, "close": c})
    tfs = {"M15": {"bars": range_bars + [
        {"open": 4385.8, "high": 4416.18, "low": 4384.71, "close": 4408.74}]}}
    tfs["H4"] = {"bars": [{"close": 4390 + i * 2, "high": 4392 + i * 2, "low": 4388 + i * 2}
                          for i in range(15)]}
    tfs["H1"] = {"bars": [{"close": 4385 + i, "high": 4387 + i, "low": 4383 + i}
                          for i in range(20)]}
    return tfs


def test_range_break_yields_bullish_bos():
    """① 区间上沿收盘突破 → bullish BOS（趋势启动）+ 高周期一致 + 突破有力。"""
    r = detect_structure_break(_mk_range_break())
    m15 = r.get("m15") or {}
    assert m15.get("bos") == "up", f"区间突破应识别 bullish BOS, 实际: {m15}"
    assert m15.get("broke_high") is True
    assert m15.get("displacement", 0) >= 0.8, "大阳突破应 displacement≥0.8×ATR（突破有力）"
    assert r.get("htf_aligned") is True, "4H/H1 同向上涨应多周期一致"
    assert r.get("advice_zh"), "应有中文结论注入决策链"


def test_downtrend_first_break_yields_choch():
    """② 单调下跌 + 大阳首破 → bullish CHoCH（反转预警，非延续）。"""
    dn = [{"open": 4415 - i * 3, "high": 4417 - i * 3, "low": 4413 - i * 3,
           "close": 4415 - i * 3 - 1} for i in range(12)]
    r = detect_structure_break({"M15": {"bars": dn + [
        {"open": 4400, "high": 4422, "low": 4399, "close": 4420}]}})
    m15 = r.get("m15") or {}
    assert m15.get("choch") == "up", f"下跌中首破应识别 CHoCH up, 实际: {m15}"
    assert m15.get("bos") is None
    assert m15.get("broke_high") is True
    assert "CHoCH" in (r.get("advice_zh") or ""), "结论应标注反转预警"


def test_range_no_false_positive():
    """③ 区间震荡无突破 → bos/choch 均为 None（不误报）。"""
    flat = [{"open": 4390, "high": 4395, "low": 4385, "close": 4390 + i % 2}
            for i in range(15)]
    r = detect_structure_break({"M15": {"bars": flat}})
    m15 = r.get("m15") or {}
    assert m15.get("bos") is None
    assert m15.get("choch") is None
    assert m15.get("broke_high") is False
    assert m15.get("broke_low") is False


def test_wick_does_not_count_as_break():
    """④ 只认收盘价：影线穿透但收盘回位不算突破（SMC 硬规则）。"""
    bars = [{"open": 4387, "high": 4395, "low": 4383, "close": 4390} for _ in range(12)]
    # 上影线刺破区间高点但收盘回落
    bars.append({"open": 4390, "high": 4400, "low": 4389, "close": 4392})
    r = detect_structure_break({"M15": {"bars": bars}})
    m15 = r.get("m15") or {}
    assert m15.get("broke_high") is False, "影线穿透收盘回落 = 流动性扫掠，不算突破"


def test_strong_down_trend_bearish_bos():
    """⑤ 下跌延续（LL）→ bearish BOS。"""
    dn = [{"open": 4415 - i * 3, "high": 4417 - i * 3, "low": 4413 - i * 3,
           "close": 4415 - i * 3 - 1} for i in range(13)]
    r = detect_structure_break({"M15": {"bars": dn}})
    m15 = r.get("m15") or {}
    assert m15.get("bos") == "down", f"LL 延续应识别 bearish BOS, 实际: {m15}"
