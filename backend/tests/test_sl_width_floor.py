"""窄 SL 硬下限回归测试。

根因：主号 #381546204 SELL 0.01 @4406.39 的 sl=4407.39（仅 100 点），
而同主号同分钟其他单 SL 均 2892 点 —— 开仓瞬间 atr 被取到异常极小值
（~1.0 而非正常 20+），sl_dollar=1.5×atr 算出 100 点窄 SL → broker 实时秒扫，
AI 周期级锁利永远赶不上（利润回吐到保本被扫）。

修复：compute_initial_sl_tp 加 ATR 硬下限(min_atr_floor=10) + SL 最小距离硬下限
(min_sl_distance=8.0 价格≈800 点)。纯加法护栏，不改开仓方向/风控逻辑。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Stub:
    pass


def _strat(**kw):
    s = _Stub()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_atr_anomaly_narrow_sl_blocked():
    """atr 异常小(1.0) 时 SL 必须被抬到硬下限，不得是 100 点窄 SL。"""
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat()
    r = compute_initial_sl_tp(side="SELL", entry_price=4406.39, atr=1.0,
                              strategy=strat, quality_regime="MID")
    dist = r["sl"] - 4406.39
    assert dist >= 8.0, f"SL 距离 {dist} 不得低于 MIN_SL_DIST=8.0（旧 bug=1.0）"
    assert dist >= 10.0, f"atr 硬下限应把 SL 抬到 ≥1500 点，实际 {dist}"


def test_atr_none_fallback():
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat()
    r = compute_initial_sl_tp(side="SELL", entry_price=4406.39, atr=None,
                              strategy=strat, quality_regime="MID")
    assert (r["sl"] - 4406.39) >= 8.0


def test_atr_normal_unchanged():
    """atr 正常(20) 时 SL≈3000 点，不被硬下限抬高。"""
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat()
    r = compute_initial_sl_tp(side="SELL", entry_price=4406.39, atr=20,
                              strategy=strat, quality_regime="MID")
    assert abs((r["sl"] - 4406.39) - 30.0) < 0.01


def test_low_regime_with_anomaly_atr():
    """LOW 啃头皮模式 + atr 异常(0.5) 也不得出窄 SL。"""
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat()
    r = compute_initial_sl_tp(side="SELL", entry_price=4406.39, atr=0.5,
                              strategy=strat, quality_regime="LOW")
    assert (r["sl"] - 4406.39) >= 8.0


def test_buy_symmetric():
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat()
    r = compute_initial_sl_tp(side="BUY", entry_price=4429.45, atr=1.0,
                              strategy=strat, quality_regime="MID")
    assert (4429.45 - r["sl"]) >= 8.0


def test_config_override():
    """config 可覆盖下限（如极端品种调 MIN_ATR）。"""
    from app.services.smart_exit import compute_initial_sl_tp
    strat = _strat(min_atr_floor=5.0, min_sl_distance=4.0)
    r = compute_initial_sl_tp(side="SELL", entry_price=4406.39, atr=1.0,
                              strategy=strat, quality_regime="MID")
    assert (r["sl"] - 4406.39) >= 4.0
