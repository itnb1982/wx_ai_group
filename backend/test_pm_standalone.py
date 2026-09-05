"""
Position Manager 独立单元验证（不连 MT5，不连本地模型）。
验证确定性停滞平仓 / 最小亏损平门槛 / 追踪锁利夹紧 / 一键回退 四条核心链路。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))          # backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # 项目根（runtime_paths）

from app.services import position_manager as pm


def _bars(closes, spread=1.0):
    """由收盘价序列造 M5 bar（high/low 围绕 close 展开一点）。"""
    out = []
    for i, c in enumerate(closes):
        out.append({"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 100, "time": ""})
    return out


def test_stall():
    """盈利单 + M5 窄幅 + 持够时间 + 未创新高 → 机械全平。"""
    pm._fetch_m5_bars = lambda acc, sym="XAUUSD": _bars([3300 + i * 0.3 for i in range(30)])
    agent = pm.PositionManagerAgent("testacc")
    pos = {"ticket": "111", "type": "buy", "open_price": 3290.0, "current_price": 3300.0,
           "sl": 3280.0, "profit": 100.0, "open_time": "2020-01-01T00:00:00"}
    # 把持仓时长造假为 200s
    import datetime
    pos["open_time"] = (datetime.datetime.now() - datetime.timedelta(seconds=200)).isoformat()
    res = agent._check_stall("buy", 3300.0, 100.0, peak=120.0, hold_sec=200, atr=10.0)
    assert res is not None and res["action"] == "full_close", f"stall 应触发全平, 得到 {res}"
    assert "利润停滞" in res["reason"], res
    print("[OK] 停滞机械平仓:", res["reason"])
    # 反例：利润还在峰值附近 → 不触发
    res2 = agent._check_stall("buy", 3300.0, 119.0, peak=120.0, hold_sec=200, atr=10.0)
    assert res2 is None, f"峰值附近不应触发, 得到 {res2}"
    print("[OK] 峰值附近不误杀: 返回 None")


def test_min_loss_gate():
    """浮亏 + M5 反转(BUY 被套: RSI低 + 跌破EMA20) + 亏损超硬SL阈值 → 门槛通过。"""
    # 造一段下跌收盘价，使 RSI<45 且 当前<EMA20
    closes = [3300 - i * 2.0 for i in range(30)]  # 单调下跌，末值 3242
    pm._fetch_m5_bars = lambda acc, sym="XAUUSD": _bars(closes)
    agent = pm.PositionManagerAgent("testacc")
    cur = closes[-1]  # 当前价=最新 M5 收盘（与行情一致）
    # BUY 在 3300 开，现价 3242（浮亏），硬 SL 3220（未破）
    ok = agent._check_min_loss_gate("buy", cur, 3300.0, 3220.0, profit=-300.0,
                                    hold_sec=120, atr=10.0)
    assert ok is True, f"下跌反转应判定开错单 (cur={cur})"
    print("[OK] 最小亏损门槛(下跌反转): 通过")
    # 反例：横盘不跌破 EMA → 不通过
    flat_closes = [3295 + (i % 2) * 0.5 for i in range(30)]
    pm._fetch_m5_bars = lambda acc, sym="XAUUSD": _bars(flat_closes)
    ok2 = agent._check_min_loss_gate("buy", flat_closes[-1], 3300.0, 3250.0, profit=-40.0,
                                     hold_sec=120, atr=10.0)
    assert ok2 is False, "横盘不应判开错单"
    print("[OK] 横盘不误判: 返回 False")


def test_clamp_trailing():
    """追踪 SL 必须夹在市价与硬 SL 之间，且留呼吸空间。"""
    agent = pm.PositionManagerAgent("testacc")
    # BUY 当前 3300，硬 SL 3280，ATR 10 → new_sl 应 < 3300-3 且 ≥3280
    ns = agent._clamp_trailing_sl("buy", 3300.0, 3280.0, proposed=3295.0, atr=10.0)
    assert 3280.0 <= ns < 3297.0, f"new_sl 越界 {ns}"
    print(f"[OK] BUY 追踪夹紧: proposed=3295 → new_sl={ns} (市价3300/硬SL3280)")
    # SELL 对称
    ns2 = agent._clamp_trailing_sl("sell", 3300.0, 3320.0, proposed=3305.0, atr=10.0)
    assert 3303.0 < ns2 <= 3320.0, f"SELL new_sl 越界 {ns2}"
    print(f"[OK] SELL 追踪夹紧: proposed=3305 → new_sl={ns2} (市价3300/硬SL3320)")


def test_disabled():
    """POSITION_MANAGER_ENABLED=False → 整层 None。"""
    from app.config import settings
    old = settings.POSITION_MANAGER_ENABLED
    settings.POSITION_MANAGER_ENABLED = False
    try:
        assert pm.get_position_manager("acc") is None, "禁用时应返回 None"
        print("[OK] 一键回退: get_position_manager 返回 None")
    finally:
        settings.POSITION_MANAGER_ENABLED = old


def test_evaluate_min_loss_no_local():
    """本地 8B 不可用时，确定性最小亏损门槛直接放行 full_close(min_loss_exit)。"""
    closes = [3300 - i * 2.0 for i in range(30)]
    pm._fetch_m5_bars = lambda acc, sym="XAUUSD": _bars(closes)
    agent = pm.PositionManagerAgent("testacc")
    cur = closes[-1]
    pos = {"ticket": "222", "type": "buy", "open_price": 3300.0, "current_price": cur,
           "sl": 3220.0, "profit": -300.0, "open_time": "2020-01-01T00:00:00"}
    import datetime
    pos["open_time"] = (datetime.datetime.now() - datetime.timedelta(seconds=120)).isoformat()
    # 关闭本地 8B，模拟「仅确定性门槛」
    import app.config as cfg_mod
    old = cfg_mod.settings.POSITION_MANAGER_LOCAL_ENABLED
    cfg_mod.settings.POSITION_MANAGER_LOCAL_ENABLED = False
    try:
        res = agent.evaluate(pos, atr=10.0, strategy=None, snap={})
        assert res is not None and res["action"] == "full_close" and res.get("min_loss_exit") is True
        print("[OK] 确定性最小亏损平放行:", res["reason"])
    finally:
        cfg_mod.settings.POSITION_MANAGER_LOCAL_ENABLED = old


if __name__ == "__main__":
    test_stall()
    test_min_loss_gate()
    test_clamp_trailing()
    test_disabled()
    test_evaluate_min_loss_no_local()
    print("\n==== Position Manager 单元验证全部通过 ====")
