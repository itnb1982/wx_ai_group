# -*- coding: utf-8 -*-
"""
2026-08-16 视觉模型升级方案文档（参数全锁）
"""
from __future__ import annotations

import os
from typing import List, Tuple

# 摆动高低点窗口（fractal 半径）
_SWING_WINDOW = 3


def _find_swings(bars, window: int = _SWING_WINDOW) -> List[Tuple[int, float, str]]:
    """找摆动高低点（fractal 式：中心 i 是 [i-window, i+window] 窗口内的唯一最高/最低）。

    回归测试：单调递增应只产 H，单调递减应只产 L，震荡应产交替 H/L。
    """
    n = len(bars)
    if n < window * 2 + 1:
        return []
    highs = [float(b.get("high") or b.get("close") or 0) for b in bars]
    lows = [float(b.get("low") or b.get("close") or 0) for b in bars]
    out: List[Tuple[int, float, str]] = []
    for i in range(window, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] == max(seg_h) and seg_h.count(highs[i]) == 1:
            out.append((i, highs[i], "H"))
        if lows[i] == min(seg_l) and seg_l.count(lows[i]) == 1:
            out.append((i, lows[i], "L"))
    return out


def _swing_seq(swings: List[Tuple[int, float, str]]) -> str:
    """由最近摆动高低点推算结构状态文字（HH/HL/LH/LL 序列）。"""
    hs = [s for s in swings if s[2] == "H"]
    ls = [s for s in swings if s[2] == "L"]
    if len(hs) >= 2 and len(ls) >= 2:
        hh = "HH" if hs[-1][1] > hs[-2][1] else "LH"
        ll = "HL" if ls[-1][1] > ls[-2][1] else "LL"
        if hh == "HH" and ll == "HL":
            return f"{hh}+{ll} 上行结构"
        if hh == "LH" and ll == "LL":
            return f"{hh}+{ll} 下行结构"
        return f"{hh}+{ll} 震荡分歧"
    if len(hs) >= 2:
        return "HH" if hs[-1][1] > hs[-2][1] else "LH"
    if len(ls) >= 2:
        return "HL" if ls[-1][1] > ls[-2][1] else "LL"
    return ""


# 回归用例
def _make_bars(closes: List[float]) -> List[dict]:
    return [
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 100, "time": f"t{i}"}
        for i, c in enumerate(closes)
    ]


def test_monotonic_up_only_H():
    bars = _make_bars([float(i) for i in range(20)])
    sw = _find_swings(bars, window=3)
    kinds = [s[2] for s in sw]
    assert all(k == "H" for k in kinds), f"单调上行应只产 H，实际：{kinds}"


def test_monotonic_down_only_L():
    bars = _make_bars([float(20 - i) for i in range(20)])
    sw = _find_swings(bars, window=3)
    kinds = [s[2] for s in sw]
    assert all(k == "L" for k in kinds), f"单调下行应只产 L，实际：{kinds}"


def test_too_short_returns_empty():
    bars = _make_bars([1.0, 2.0, 3.0])  # n=3 < window*2+1=7
    assert _find_swings(bars, window=3) == []


def test_seq_uptrend():
    # HH+HL 应判"上行结构"
    sw = [(0, 100.0, "H"), (5, 95.0, "L"), (10, 110.0, "H"), (15, 105.0, "L")]
    seq = _swing_seq(sw)
    assert "上行结构" in seq, f"应为上行结构，实际：{seq}"


def test_seq_downtrend():
    # LH+LL 应判"下行结构"
    sw = [(0, 110.0, "H"), (5, 105.0, "L"), (10, 100.0, "H"), (15, 95.0, "L")]
    seq = _swing_seq(sw)
    assert "下行结构" in seq, f"应为下行结构，实际：{seq}"


def test_render_with_swings_returns_png_bytes():
    import app.services.vision_chart as vc
    bars = _make_bars([3400.0 + i * 0.5 for i in range(60)])
    img = vc.render_chart(bars, "XAUUSD H4", show_swings=True)
    assert img and img.startswith(b"\x89PNG"), "render 应返回 PNG bytes"
    assert len(img) > 1000


def test_render_without_swings_also_works():
    import app.services.vision_chart as vc
    bars = _make_bars([3400.0 - i * 0.3 for i in range(60)])
    img = vc.render_chart(bars, "XAUUSD M5", show_swings=False)
    assert img and img.startswith(b"\x89PNG")


def test_render_handles_broken_input_gracefully():
    import app.services.vision_chart as vc
    # 数据不足 → 返回 None
    assert vc.render_chart([], "empty") is None
    # 缺字段 → 渲染失败但返回 None（不抛异常）
    bad = [{"open": "x", "high": None, "low": None, "close": 1.0}]
    assert vc.render_chart(bad, "bad") is None


if __name__ == "__main__":
    # 脚本直接跑：把 backend 根加入 path 后才能 import app.services.vision_chart
    import sys
    _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for fn in funcs:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed.append((fn.__name__, str(e)))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  ERR   {fn.__name__}: {e}")
    print(f"\n{passed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)