"""
持仓查询完整性 — "查询失败" 与 "真的空仓" 必须可区分（Phase 1 / V6）

背景（真实致命缺陷）：
    mt5_service.get_positions() 在 Worker 掉线 / 管道断开 / 命令超时时，
    与"账号确实一手没有"一样返回 []。调用方无从分辨。

    在只读场景里这最多是显示不准；但有两处是**破坏性动作**，它们把空列表
    当成事实依据去改账本、去平仓：

    1) _reconcile_positions —— live=[] ⇒ 本地全部 open trades 被判"MT5 已平"，
       批量写成 closed。AI 下一轮读账本以为空仓 → 重复开仓 → 超仓。
    2) _reconcile_against_leader —— leader_positions=[] ⇒ 跟号每一笔带 L{ticket}
       标记的持仓都 "不在主号持仓里" ⇒ 判定孤儿单 ⇒ **市价全平**。
       即：主号 Worker 抖一下，所有跟号仓位被清空。真金白银。

    代码里 702 行已有一条同源护栏（"主号自身不对自己对账…会被误判为孤儿单
    全部平仓（灾难性）"），说明这个坑踩过一次，但另一个入口没堵。

断言一律落在"MT5 到底收没收到 close_position"，不看返回值——
沿用 Phase 0 的教训：返回值说"我跳过了"而底层照样发单，是最典型的假安全。

同时沿用另一条教训：**拦截类测试必须先自证场景会发生**，
否则测的是空气。故每个拦截用例都配一条 self-proving 用例。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


LEADER_ID = "acc_leader_0001"
FOLLOWER_ID = "acc_follower_002"


def _follower_positions():
    """跟号持仓：comment 里带主号票号标记，这是孤儿单判定的依据。"""
    return [
        {"ticket": 9001, "type": "buy", "volume": 0.10, "price_open": 2000.0,
         "profit": 33.0, "symbol": "XAUUSD", "comment": "WXAI-L5001"},
        {"ticket": 9002, "type": "buy", "volume": 0.10, "price_open": 2001.0,
         "profit": -12.0, "symbol": "XAUUSD", "comment": "WXAI-L5002"},
    ]


def _make_follower(monkeypatch, *, leader_ok: bool, leader_positions=None):
    """搭一个跟号执行器；leader_ok=False 模拟主号持仓查询失败。

    关键：mock 同时提供新旧两种查询接口，且二者对"失败"的表达不同——
      get_positions()          → []            （旧接口，失败与空仓不可分）
      get_positions_checked()  → (False, [])   （新接口，明确说"我没查到"）
    未修复的代码走旧接口拿到 []，就会去平仓；用例即可抓到。
    """
    mock_mt5 = MagicMock()

    def _plain(account_id, symbol="XAUUSD"):
        if account_id == FOLLOWER_ID:
            return _follower_positions()
        return list(leader_positions or [])

    def _checked(account_id, symbol="XAUUSD"):
        if account_id == FOLLOWER_ID:
            return True, _follower_positions()
        if not leader_ok:
            return False, []          # 查询失败：拿不到主号真实持仓
        return True, list(leader_positions or [])

    mock_mt5.get_positions.side_effect = _plain
    mock_mt5.get_positions_checked.side_effect = _checked
    mock_mt5.close_position.return_value = {"ticket": 1, "profit": 0.0}
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)
    # 清空对账节流，否则第二个用例会被 30s 节流直接 return（假绿）
    te._RECON_LAST.clear()

    ex = TradeExecutor(
        account_id=FOLLOWER_ID,
        strategy=types.SimpleNamespace(),
        user_id="user_test",
        db=MagicMock(),
        engine=MagicMock(),
    )
    ex._fresh_strat = lambda field, default=None: default
    ex._follow_leader = True
    ex._is_leader = False
    ex._leader_account = lambda: types.SimpleNamespace(
        id=LEADER_ID, account_id="70000001"
    )
    ex._record_close = lambda *a, **k: None
    return ex, mock_mt5


# ─────────── 一、主副对账：主号查询失败绝不能触发平仓 ───────────

@pytest.mark.integration
def test_orphan_close_actually_fires_when_leader_query_succeeds():
    """自证场景：主号查得到、且确实已无该票 ⇒ 孤儿单必须被平。

    没有这条，下面的"不许平"可能只是因为压根没走到平仓分支（测空气）。
    """
    import pytest as _p
    monkeypatch = _p.MonkeyPatch()
    try:
        # 主号在线，但持仓里没有 L5001/L5002 → 跟号两笔都是真孤儿
        ex, mock_mt5 = _make_follower(
            monkeypatch, leader_ok=True,
            leader_positions=[{"ticket": 7777, "symbol": "XAUUSD"}],
        )
        ex._reconcile_against_leader()
        assert mock_mt5.close_position.call_count == 2, (
            "主号在线且票已不存在时，孤儿单本就该被清理——"
            "这条不通过说明后面的用例测的是空气"
        )
    finally:
        monkeypatch.undo()


@pytest.mark.integration
def test_leader_query_failure_must_not_close_follower_positions():
    """核心红线：主号持仓查询失败 ⇒ 一笔都不许平。

    查询失败意味着"我不知道主号现在有什么"，
    而不是"主号什么都没有"。据此平仓 = 拿噪声当事实。
    """
    import pytest as _p
    monkeypatch = _p.MonkeyPatch()
    try:
        ex, mock_mt5 = _make_follower(monkeypatch, leader_ok=False)
        ex._reconcile_against_leader()
        assert mock_mt5.close_position.call_count == 0, (
            f"主号查询失败却平了 {mock_mt5.close_position.call_count} 笔跟号持仓——"
            "主号 Worker 抖一下就清空跟号，这是真金白银的损失"
        )
    finally:
        monkeypatch.undo()


@pytest.mark.integration
def test_leader_genuinely_flat_still_closes_orphans():
    """反面保护：主号确实一手不剩（查询成功、返回空）⇒ 孤儿单仍要清。

    修复不能矫枉过正——把"真空仓"也当成"查询失败"，
    会让跟号在主号已离场后继续裸持，同样违背主副一致。
    """
    import pytest as _p
    monkeypatch = _p.MonkeyPatch()
    try:
        ex, mock_mt5 = _make_follower(monkeypatch, leader_ok=True, leader_positions=[])
        ex._reconcile_against_leader()
        assert mock_mt5.close_position.call_count == 2, (
            "主号确认空仓时孤儿单必须清理，否则跟号与主号失同步"
        )
    finally:
        monkeypatch.undo()


# ─────────── 二、反向对账：查询失败绝不能把本地持仓写成已平 ───────────

def _make_solo(monkeypatch, *, query_ok: bool, live=None):
    """搭一个独立账号执行器，带 2 笔本地 open trade。"""
    mock_mt5 = MagicMock()
    mock_mt5.get_positions.side_effect = lambda *a, **k: list(live or [])
    mock_mt5.get_positions_checked.side_effect = (
        lambda *a, **k: (True, list(live or [])) if query_ok else (False, [])
    )
    mock_mt5.get_recent_deals.return_value = {"deals": []}
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    te._RECON_LAST.clear()

    trades = [
        types.SimpleNamespace(mt5_ticket=9001, open_price=2000.0, close_price=None,
                              close_time=None, profit=None, net_profit=None,
                              result=None, exit_reason=None),
        types.SimpleNamespace(mt5_ticket=9002, open_price=2001.0, close_price=None,
                              close_time=None, profit=None, net_profit=None,
                              result=None, exit_reason=None),
    ]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = trades

    ex = TradeExecutor(
        account_id="acc_solo_001",
        strategy=types.SimpleNamespace(),
        user_id="user_test",
        db=db,
        engine=MagicMock(),
    )
    ex._fresh_strat = lambda field, default=None: default
    ex._safe_db_write = lambda fn, label="": fn(MagicMock())
    return ex, trades


@pytest.mark.integration
def test_reconcile_marks_closed_when_query_succeeds(monkeypatch):
    """自证场景：查询成功且 MT5 确实已无该票 ⇒ 本地必须补平。"""
    ex, trades = _make_solo(monkeypatch, query_ok=True, live=[])
    ex._reconcile_positions()
    assert all(t.close_time is not None for t in trades), (
        "查询成功且 MT5 无此单时本就该补平——不通过说明后面的用例测的是空气"
    )


@pytest.mark.integration
def test_reconcile_must_not_mark_closed_when_query_fails(monkeypatch):
    """核心红线：持仓查询失败 ⇒ 本地账本一个字都不许改。

    改了的后果不是"显示不准"：AI 下一轮读账本以为空仓，
    会在已有持仓之上重复开仓，直接突破 max_position_lots。
    """
    ex, trades = _make_solo(monkeypatch, query_ok=False, live=[])
    ex._reconcile_positions()
    assert all(t.close_time is None for t in trades), (
        "MT5 查询失败却把本地持仓全标记为已平——"
        "AI 会因此失明并重复开仓"
    )
    assert all(t.exit_reason is None for t in trades)


# ─────────── 三、mt5_service 层：checked 接口的语义契约 ───────────

def _svc():
    from app.services.mt5_service import MT5Service
    return MT5Service.__new__(MT5Service)


def test_checked_query_reports_success_with_data(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": [{"ticket": 1}]},
                        raising=False)
    ok, data = svc.get_positions_checked("acc", "XAUUSD")
    assert ok is True and len(data) == 1


def test_checked_query_distinguishes_empty_from_failure(monkeypatch):
    """同为空列表，ok 必须不同——这正是整个修复的立足点。"""
    svc = _svc()
    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": []}, raising=False)
    assert svc.get_positions_checked("acc") == (True, [])

    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": False, "error": "账号未连接"},
                        raising=False)
    ok, data = svc.get_positions_checked("acc")
    assert ok is False and data == [], "查询失败必须自报失败，不能伪装成空仓"


def test_checked_query_handles_null_payload(monkeypatch):
    """Worker 回 ok 但 data 为 None：算成功、按空仓处理，不得抛异常。"""
    svc = _svc()
    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": None}, raising=False)
    assert svc.get_positions_checked("acc") == (True, [])


def test_all_positions_checked_same_contract(monkeypatch):
    """全品种版本必须遵守同一套契约（篮子风控/紧急平仓依赖它）。"""
    svc = _svc()
    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": False, "error": "管道断开"},
                        raising=False)
    assert svc.get_all_positions_checked("acc") == (False, [])

    monkeypatch.setattr(svc, "_safe_send",
                        lambda *a, **k: {"ok": True, "data": [{"ticket": 2}]},
                        raising=False)
    ok, data = svc.get_all_positions_checked("acc")
    assert ok is True and data[0]["ticket"] == 2


# ─────────── 四、主执行体：对账未通过不得进入决策（V6 §5.4） ───────────

@pytest.fixture
def cycle_env(monkeypatch, tmp_path_factory, request):
    """搭 execute_cycle 运行环境；隔离紧急状态文件，避免读到生产 halt 态。"""
    import app.services.emergency as em
    base = tmp_path_factory.getbasetemp()
    f = base / f"pqi_{abs(hash(request.node.nodeid)) % 10**10}.json"
    monkeypatch.setattr(em, "STATE_FILE", str(f))
    monkeypatch.setattr(em, "_cache", None, raising=False)
    monkeypatch.setattr(em, "_cache_at", 0.0, raising=False)
    monkeypatch.setattr(em, "_cache_mtime", -1.0, raising=False)

    mock_mt5 = MagicMock()
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)
    te._RECON_LAST.clear()
    te._RECON_OK.clear()

    engine = MagicMock()
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 15.0, "d1_atr": 15.0},
        "current_price": 2005.0,
    }
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(
        id="acc_cycle", is_trading_enabled=True
    )
    db.query.return_value.filter.return_value.all.return_value = []

    ex = TradeExecutor(
        account_id="acc_cycle",
        strategy=types.SimpleNamespace(),
        user_id="user_test",
        db=db,
        engine=engine,
    )
    ex._fresh_strat = lambda field, default=None: default
    ex._reconcile_against_leader = lambda: None
    ex._check_loss_cooldown = lambda: ""
    ex._manage_positions = MagicMock()
    ex.exit_agent = MagicMock()
    return ex, mock_mt5, engine


@pytest.mark.integration
def test_cycle_blocks_open_when_ledger_untrusted(cycle_env):
    """对账未通过 ⇒ 绝不开新仓。宁可少做一单，不可盲开一单。"""
    ex, mock_mt5, _engine = cycle_env
    mock_mt5.get_positions_checked.return_value = (False, [])
    mock_mt5.get_all_positions_checked.return_value = (False, [])

    result = ex.execute_cycle()

    assert mock_mt5.place_order.call_count == 0, "账本不可信时开了新仓"
    assert result["placed"] is False
    assert any("对账" in e for e in result["errors"])


@pytest.mark.integration
def test_cycle_still_protects_positions_when_ledger_untrusted(cycle_env):
    """对账未通过但仓还在 ⇒ 止损止盈必须继续。挡新仓不等于放任持仓裸奔。"""
    ex, mock_mt5, _engine = cycle_env
    mock_mt5.get_positions_checked.return_value = (False, [])
    mock_mt5.get_all_positions_checked.return_value = (
        True, [{"ticket": 1, "symbol": "XAUUSD", "volume": 0.1}]
    )

    ex.execute_cycle()

    assert ex._manage_positions.call_count == 1, "持仓保护被误停 → 持仓裸奔"
    assert mock_mt5.place_order.call_count == 0


@pytest.mark.integration
def test_cycle_skips_ai_call_when_mt5_fully_down(cycle_env):
    """MT5 整体不可达 ⇒ 连 AI 都不该调。

    此时既拿不到持仓也管不了仓，每轮仍调云模型只是白烧 token。
    """
    ex, mock_mt5, engine = cycle_env
    mock_mt5.get_positions_checked.return_value = (False, [])
    mock_mt5.get_all_positions_checked.return_value = (False, [])

    ex.execute_cycle()

    assert engine.decide.call_count == 0, "MT5 全挂还在调用云 AI，纯属烧钱"
    assert ex._manage_positions.call_count == 0


@pytest.mark.integration
def test_cycle_proceeds_normally_when_ledger_trusted(cycle_env):
    """自证场景：对账通过 ⇒ 必须照常走到 AI 决策。

    没有这条，上面三条"被拦住"可能只是因为流程压根没跑起来。
    """
    ex, mock_mt5, engine = cycle_env
    mock_mt5.get_positions_checked.return_value = (True, [])
    mock_mt5.get_all_positions_checked.return_value = (True, [])

    ex.execute_cycle()

    assert engine.decide.call_count >= 1, (
        "对账正常时决策链路没跑起来——上面的拦截用例测的是空气"
    )
