"""★ 2026-08-18 新增：市场体制检测回归测试。

背景：实盘 M15 已出现清晰下跌趋势（4435→4395），但 regime_detect 因 4H 仍偏多/
中性且 ER 门槛 0.45 过高，把趋势误判为「区间震荡」→ AI 执行均值回归逻辑、在下跌
途中反复摸顶做空、亏损。本测试锁定以下场景必须判为趋势而非 range。
"""
import pytest

from app.services.regime_detect import detect_regime


def _bars_from_closes(closes, spread=0.5):
    """用收盘价序列构造 OHLC  bars（open=前收，high/low=close±spread/2）。"""
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        h = max(o, c) + spread / 2
        l = min(o, c) - spread / 2
        bars.append({"open": o, "high": h, "low": l, "close": c, "volume": 100})
    return bars


def _make_tfs(m15_closes, h4_closes=None, spread=0.5):
    """构造 detect_regime 所需的多周期 raw 数据。"""
    tfs = {
        "M15": {"bars": _bars_from_closes(m15_closes, spread)},
        "M5": {"bars": _bars_from_closes(m15_closes[-60:], spread)},
    }
    if h4_closes:
        tfs["H4"] = {"bars": _bars_from_closes(h4_closes, spread)}
        tfs["H1"] = {"bars": _bars_from_closes(h4_closes[-60:], spread)}
    return tfs


def test_m15_downtrend_with_h4_neutral_is_trend_down():
    """M15 清晰下跌（连续更低低点）+ 4H 无方向 → 必须判为 trend_down，不能 range。"""
    # 4H 横盘/中性：围绕 4415 小幅波动，无明确方向
    h4_closes = [4410.0] * 10 + [4415.0] * 10 + [4412.0] * 10
    # M15 清晰下跌：从 4435 跌到 4395，低点逐低
    m15_closes = list(range(4435, 4394, -1))  # 41 bars, 4435→4395
    tfs = _make_tfs(m15_closes, h4_closes)
    r = detect_regime(tfs, current_price=4395.0)
    assert r["regime"] == "trend_down", f"期望 trend_down，得到 {r['regime']}（{r['label_zh']}）"
    assert r["structure_dir_15m"] == "down"


def test_m15_downtrend_with_h4_up_is_trend_down():
    """M15 清晰下跌 + 4H 仍残留旧涨势 → 短周期转向优先，判 trend_down。"""
    # 4H 缓慢上涨：仍在旧趋势中
    h4_closes = list(range(4380, 4420, 2))  # 20 bars, 4380→4418
    # M15 清晰下跌
    m15_closes = list(range(4435, 4394, -1))
    tfs = _make_tfs(m15_closes, h4_closes)
    r = detect_regime(tfs, current_price=4395.0)
    assert r["regime"] == "trend_down", f"期望 trend_down，得到 {r['regime']}（{r['label_zh']}）"


def test_m15_uptrend_with_h4_down_is_trend_up():
    """对称：M15 清晰上涨 + 4H 仍残留旧跌势 → 判 trend_up。"""
    h4_closes = list(range(4420, 4380, -2))
    m15_closes = list(range(4395, 4436, 1))  # 41 bars up
    tfs = _make_tfs(m15_closes, h4_closes)
    r = detect_regime(tfs, current_price=4435.0)
    assert r["regime"] == "trend_up", f"期望 trend_up，得到 {r['regime']}（{r['label_zh']}）"


def test_m15_weak_direction_stays_range():
    """M15 方向模糊、无连续结构 → 仍应保守判 range，不误判为趋势。"""
    # 4H 中性
    h4_closes = [4410.0] * 30
    # M15 无规则小幅波动，无明显 HH/HL 或 LH/LL
    m15_closes = [4410.0, 4412.0, 4411.0, 4413.0, 4412.0, 4414.0,
                  4413.0, 4412.0, 4414.0, 4413.0, 4412.0, 4411.0,
                  4412.0, 4413.0, 4412.0, 4411.0, 4412.0, 4413.0,
                  4412.0, 4411.0, 4412.0, 4413.0, 4412.0, 4411.0,
                  4412.0, 4413.0, 4412.0, 4411.0, 4412.0, 4413.0]
    tfs = _make_tfs(m15_closes, h4_closes)
    r = detect_regime(tfs, current_price=4412.0)
    assert r["regime"] in ("range", "volatile"), f"弱方向应保守，得到 {r['regime']}"
