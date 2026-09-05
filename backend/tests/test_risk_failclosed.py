"""
风控失败关闭（fail-closed）是否真的会触发 — Phase 1 / V6

risk_engine.py 顶部记着 P0-1 的教训：父进程直接调 mt5.* 全返 None，
Layer1/2/2b/3/4 全部静默通过，"等于风控被悄悄关掉"。当时的修法是改走 IPC
并为每个数据层写上失败关闭。

问题是：**同一个教训正在以另一种形式复发。**

失败关闭的判据是"数据源返回 None"，但 IPC 层把失败伪装成了合法的空值：
    get_positions()      失败 → []                        （与"一手没有"同形）
    get_history_deals()  失败 → {"deals": [], ...}         （与"今天没成交"同形）
于是 _fetch_positions 永远返回 [] 而非 None、_fetch_daily_pnl 永远返回 0.0，
上层那几行 `if xxx is None: return False` 从来没有机会执行。

后果不是"风控偶尔不准"，而是**风控在最需要它的时刻集体失守**：
  · 持仓查询失败 → 当作零持仓 → 最大笔数/最大手数/同向并发 三道门全开 → 超仓
  · 日盈亏查询失败 → 当作今日盈亏 0 → 日亏损熔断失效 → 已亏 5% 还继续放行
而 MT5 查询失败恰恰高发于行情剧烈、Worker 繁忙的时候。

本文件只问一件事：数据源坏掉时，风控到底拦不拦。
每个"必须拦"的用例都配一条"数据正常时必须放行"，防止把矫枉过正当成修好了。
"""
from unittest.mock import MagicMock

import pytest

from app.services.risk_engine import RiskEngine


CFG = {
    "max_positions": 10,
    "max_position_lots": 5.0,
    "max_daily_loss_pct": 5.0,
    "capital_source": "live",
    "sizing_scale_mode": "fixed",
}


def _engine(mt5):
    return RiskEngine(strategy_config=dict(CFG), mt5_service=mt5, account_id="acc_risk")


def _mt5_positions(*, ok: bool, positions=None):
    """构造 mt5_service：ok=False 表示持仓查询失败（Worker 掉线/超时）。"""
    m = MagicMock()
    m.get_positions.return_value = list(positions or [])          # 旧接口：失败也回 []
    m.get_positions_checked.return_value = (ok, list(positions or []))
    return m


# ─────────── 一、持仓数据源失败 ⇒ 三道门必须关 ───────────

def test_position_limits_fail_closed_when_query_fails():
    """持仓查询失败 ⇒ 拒绝开仓。

    当成零持仓放行的后果是超仓：本来已满仓，风控却以为可以再来一单。
    """
    eng = _engine(_mt5_positions(ok=False))
    passed, reason = eng._check_position_limits("XAUUSD", new_volume=0.10, account_balance=10000.0)
    assert passed is False, "持仓数据不可用却放行开仓 —— 风控在裸奔"
    assert "不可用" in reason


def test_same_direction_fail_closed_when_query_fails():
    """同向并发检查同样依赖持仓数据，失败必须一起关。"""
    eng = _engine(_mt5_positions(ok=False))
    passed, reason = eng.check_same_direction("buy", max_concurrent=3)
    assert passed is False, "同向并发上限在数据缺失时失守"
    assert "不可用" in reason


def test_position_limits_pass_when_genuinely_flat():
    """自证 + 防矫枉过正：确认空仓（查询成功）时必须放行。

    没有这条，上面两条可能只是因为风控被改成了"一律拒绝"。
    """
    eng = _engine(_mt5_positions(ok=True, positions=[]))
    passed, reason = eng._check_position_limits("XAUUSD", new_volume=0.10, account_balance=10000.0)
    assert passed is True, f"确认空仓却拒绝开仓，交易机会被平白砍掉: {reason}"


def test_position_limits_still_blocks_real_overflow():
    """真实超仓仍要拦（证明这条链路本来就是活的）。"""
    eng = _engine(_mt5_positions(
        ok=True, positions=[{"volume": 3.0, "type": "buy"}, {"volume": 2.5, "type": "buy"}]))
    passed, reason = eng._check_position_limits("XAUUSD", new_volume=0.10, account_balance=10000.0)
    assert passed is False and "超最大持仓手数" in reason


def test_same_direction_pass_when_below_limit():
    """自证：查询成功且未达上限 ⇒ 放行。"""
    eng = _engine(_mt5_positions(ok=True, positions=[{"volume": 0.1, "type": "buy"}]))
    passed, _ = eng.check_same_direction("buy", max_concurrent=3)
    assert passed is True


# ─────────── 二、日盈亏数据源失败 ⇒ 熔断必须关 ───────────

def _mt5_deals(*, ok: bool, deals=None):
    m = MagicMock()
    m.get_positions_checked.return_value = (True, [])
    payload = {"deals": list(deals or []), "total_profit": 0.0, "count": 0}
    m.get_history_deals.return_value = payload                     # 旧接口：失败也回空壳
    m.get_history_deals_checked.return_value = (ok, payload)
    return m


def test_daily_loss_fail_closed_when_history_query_fails():
    """今日盈亏查不到 ⇒ 拒绝开仓。

    伪装成 0.0 的后果：已经亏了 5%，MT5 抖一下，熔断当场失效，亏损继续扩大。
    """
    eng = _engine(_mt5_deals(ok=False))
    passed, reason = eng._check_daily_loss(balance=10000.0)
    assert passed is False, "日盈亏数据缺失却放行 —— 日亏损熔断形同虚设"
    assert "不可用" in reason


def test_daily_loss_pass_when_no_deals_today():
    """自证 + 防矫枉过正：确认今天没成交（查询成功）时必须放行。"""
    eng = _engine(_mt5_deals(ok=True, deals=[]))
    passed, reason = eng._check_daily_loss(balance=10000.0)
    assert passed is True, f"今天没交易却拒绝开仓: {reason}"


def test_daily_loss_still_trips_on_real_loss():
    """真实亏损超限仍要熔断（证明链路是活的）。"""
    eng = _engine(_mt5_deals(ok=True, deals=[{"net_profit": -800.0}]))
    passed, reason = eng._check_daily_loss(balance=10000.0)
    assert passed is False and "日亏损超限" in reason


# ─────────── 三、mt5_service 层：历史成交 checked 契约 ───────────

def _svc():
    from app.services.mt5_service import MT5Service
    return MT5Service.__new__(MT5Service)


def test_history_deals_checked_distinguishes_empty_from_failure(monkeypatch):
    """同为"没有成交"，查询失败与今日无单必须可分辨。"""
    svc = _svc()
    empty = {"deals": [], "total_profit": 0.0, "count": 0}

    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": empty}, raising=False)
    ok, data = svc.get_history_deals_checked("acc")
    assert ok is True and data["deals"] == []

    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": False, "error": "超时"}, raising=False)
    ok, data = svc.get_history_deals_checked("acc")
    assert ok is False, "历史成交查询失败必须自报失败，不能返回空壳冒充无成交"


def test_history_deals_checked_survives_null_payload(monkeypatch):
    """Worker 回 ok 但 data 为 None：算成功、按无成交处理，不得抛异常。"""
    svc = _svc()
    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": None}, raising=False)
    ok, data = svc.get_history_deals_checked("acc")
    assert ok is True and data.get("deals") == []


# ─────────── 四、防回归：失败关闭的判据不能再被空值架空 ───────────

def test_fetch_positions_returns_none_on_failure():
    """_fetch_positions 的契约是"不可用返回 None"，必须真能返回 None。

    这正是 P0-1 修复写下的判据；它一旦永远返回 []，
    上层所有 `if positions is None` 都成了死代码。
    """
    eng = _engine(_mt5_positions(ok=False))
    assert eng._fetch_positions("XAUUSD") is None


def test_fetch_daily_pnl_returns_none_on_failure():
    eng = _engine(_mt5_deals(ok=False))
    assert eng._fetch_daily_pnl() is None


def test_fetch_positions_returns_list_when_available():
    """反向：数据可用时必须是列表（哪怕是空列表），不能返回 None。"""
    eng = _engine(_mt5_positions(ok=True, positions=[]))
    assert eng._fetch_positions("XAUUSD") == []
