"""体制识别多周期加权投票回归：根治「H4 滞后把下跌判成上涨」。

原为 backend/test_regime_vote.py 脚本式验证（无 def test_，从未被 pytest 收集）。
Phase -1 收编：拆为独立用例，纳入回归保护。

背景（实盘事故）：用户实盘中 H4 周期仍停在上涨形态、而 M5~H1 已全部转跌，
旧逻辑单看 H4 判 trend_up → 高位追多。加权投票让短周期能压过长周期滞后。
"""
import pytest

from app.services.regime_detect import detect_regime

_ALL_TFS = ("M5", "M15", "M30", "H1", "H4", "D1")


def _gen_bars(n: int, start: float, end: float) -> list:
    """线性生成 n 根 K 线（close 由 start 走到 end），high/low 围绕 close 微扰。"""
    bars = []
    for i in range(n):
        t = i / (n - 1)
        c = start + (end - start) * t
        bars.append({"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 100})
    return bars


def _build_tfs(down_tfs: set, up_tfs: set) -> dict:
    """未列入 down/up 的周期一律构造为横盘。"""
    tfs = {}
    for tf in _ALL_TFS:
        if tf in down_tfs:
            tfs[tf] = {"bars": _gen_bars(30, 2000, 1900)}   # 下跌
        elif tf in up_tfs:
            tfs[tf] = {"bars": _gen_bars(30, 1900, 2000)}   # 上涨
        else:
            tfs[tf] = {"bars": _gen_bars(30, 1950, 1950)}   # 横盘
    return tfs


@pytest.mark.unit
def test_short_tf_downtrend_overrides_h4_lag():
    """场景1（核心事故场景）：短周期全跌、H4/D1 仍滞后上涨 → 必须判 trend_down。"""
    tfs = _build_tfs(down_tfs={"M5", "M15", "M30", "H1"}, up_tfs={"H4", "D1"})
    r = detect_regime(tfs, 1900)

    assert r["regime"] == "trend_down", (
        f"短周期全跌却判 {r['regime']} —— H4 滞后未被压制"
    )
    # 注：detect_regime 已重构，旧的 multi_tf_trend / vote_score 字段被
    # entry_dir_5m（入场级方向）+ structure_dir_15m（结构级方向）取代。
    # Phase -1 收编时对齐新契约；核心不变式（短周期主导方向）保持不变。
    assert r["entry_dir_5m"] == "down", f"入场级方向应为 down，实际 {r['entry_dir_5m']}"
    assert r["structure_dir_15m"] == "down", (
        f"结构级方向应为 down，实际 {r['structure_dir_15m']}"
    )


@pytest.mark.unit
def test_all_tf_uptrend_detected():
    """场景2：全周期上涨 → trend_up（对称验证，确保无 BUY/SELL 偏置）。"""
    tfs = _build_tfs(down_tfs=set(), up_tfs=set(_ALL_TFS))
    r = detect_regime(tfs, 2000)
    assert r["regime"] == "trend_up", f"全周期上涨却判 {r['regime']}"


@pytest.mark.unit
def test_all_tf_flat_is_range_or_volatile():
    """场景3：全周期横盘 → range 或 volatile，不得误判为任一趋势。"""
    tfs = _build_tfs(down_tfs=set(), up_tfs=set())
    r = detect_regime(tfs, 1950)
    assert r["regime"] in ("range", "volatile"), f"全横盘却判 {r['regime']}"


@pytest.mark.unit
def test_h4_lag_does_not_flip_direction():
    """场景4（最强对照）：H4/D1 强上涨 vs 四个短周期下跌 → 权重必须偏向短周期。

    这是旧逻辑的直接失效点：只看 H4 会得出 trend_up 并高位追多。
    """
    tfs = _build_tfs(down_tfs={"M5", "M15", "M30", "H1"}, up_tfs={"H4", "D1"})
    r = detect_regime(tfs, 1900)
    assert r["regime"] == "trend_down", (
        f"H4 滞后仍主导了判定（得到 {r['regime']}），加权投票失效"
    )
