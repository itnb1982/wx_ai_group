"""
人工紧急处置 — 交易链路集成测试（Phase 0 / V6）

单测证明 emergency 模块自身的档位逻辑是对的；
这个文件证明的是另一件事：**拦截点没有漏**。

断言一律落在"MT5 到底收没收到指令"（place_order / close_position 的调用记录），
而不是看函数返回值——返回值说"我拒绝了"但底层照样发单，是最典型的假安全。

特别针对一个真实结构风险：
  trading.py 的守护线程会绕过 execute_cycle，直接调
  _fast_l3_lock / _manage_positions / _mirror_leader_exits / _close_opposite_for_decision。
  只在主入口设防的话，HALT_ALL 期间这些旁路照样自动平仓——等于没停。
  故每条旁路都单独立一个用例。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor
import app.services.emergency as em


@pytest.fixture(autouse=True)
def isolated_emergency(tmp_path_factory, monkeypatch, request):
    """隔离状态文件，避免污染生产 backend/emergency_state.json。"""
    base = tmp_path_factory.getbasetemp()
    f = base / f"emi_{abs(hash(request.node.nodeid)) % 10**10}.json"
    monkeypatch.setattr(em, "STATE_FILE", str(f))
    monkeypatch.setattr(em, "_cache", None, raising=False)
    monkeypatch.setattr(em, "_cache_at", 0.0, raising=False)
    monkeypatch.setattr(em, "_cache_mtime", -1.0, raising=False)
    yield


@pytest.fixture
def executor_and_mt5(monkeypatch):
    engine = MagicMock()
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 15.0, "d1_atr": 15.0},
        "current_price": 2005.0,
    }
    engine.deepseek = MagicMock()

    mock_mt5 = MagicMock()
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)

    # 有持仓：若拦截失效，自动平仓逻辑就会真的去调 close_position，用例即可抓到
    mock_mt5.get_all_positions.return_value = [
        {"ticket": 1001, "type": "buy", "volume": 0.10, "price_open": 2000.0,
         "sl": 0.0, "tp": 0.0, "profit": 120.0, "symbol": "XAUUSD", "comment": "WXAI_L1"},
        {"ticket": 1002, "type": "sell", "volume": 0.10, "price_open": 2010.0,
         "sl": 0.0, "tp": 0.0, "profit": -80.0, "symbol": "XAUUSD", "comment": "WXAI_L2"},
    ]

    executor = TradeExecutor(
        account_id="acc_halt_test",
        strategy=types.SimpleNamespace(),
        user_id="user_test",
        db=MagicMock(),
        engine=engine,
    )
    executor._fresh_strat = lambda field, default=None: default
    executor.exit_agent = MagicMock()
    executor._is_leader = True
    # ★ 2026-08-17 修复：mock 主号持仓存在 → 对账兜底(reconcile_close)不误触发，
    #   否则真实查主号返回空 → 跟号 L 单被当孤儿先平一次 + 广播又平一次 = 2 次
    #   （与 mirror_concurrency 同源根因，2026-08-15 对账兜底新增后测试未同步）。
    monkeypatch.setattr(
        te, "_positions_checked",
        lambda account_id, symbol="XAUUSD": (
            True,
            [{"ticket": "7701", "type": "buy", "volume": 0.10,
              "price_open": 2000.0, "sl": 1995.0, "tp": 0.0, "profit": 0.0,
              "symbol": "XAUUSD", "comment": ""}],
        ),
    )
    return executor, mock_mt5


def _no_trading_side_effects(mock_mt5) -> bool:
    """MT5 完全没被下达任何交易指令。"""
    return (mock_mt5.place_order.call_count == 0
            and mock_mt5.close_position.call_count == 0
            and mock_mt5.modify_sl_tp.call_count == 0)


# ───────────────── 主入口 execute_cycle ─────────────────

@pytest.mark.integration
def test_execute_cycle_halt_new_blocks_open(executor_and_mt5):
    """HALT_NEW：主执行体必须在任何下单动作前就返回。"""
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_NEW, reason="盘面异常", by="tester")

    result = executor.execute_cycle()

    assert result["placed"] is False
    assert mock_mt5.place_order.call_count == 0, "HALT_NEW 期间绝不允许开新仓"
    assert any("紧急停止" in e for e in result["errors"])


@pytest.mark.integration
def test_execute_cycle_halt_new_still_protects_positions(executor_and_mt5):
    """★ HALT_NEW 的核心语义：封新仓，但持仓保护必须继续跑。

    如果这里连持仓管理都跳过了，人工停止会把浮盈单的保本/追踪一起停掉，
    等于让持仓在无人看管下裸奔——比不停更危险。
    """
    executor, mock_mt5 = executor_and_mt5
    called = {"manage": 0}

    def _spy(ai_decision):
        called["manage"] += 1

    executor._manage_positions = _spy
    em.halt(em.LEVEL_HALT_NEW, reason="", by="t")

    executor.execute_cycle()

    assert called["manage"] == 1, "HALT_NEW 必须仍然执行持仓管理"
    assert mock_mt5.place_order.call_count == 0


@pytest.mark.integration
def test_execute_cycle_halt_all_does_nothing(executor_and_mt5):
    """HALT_ALL：开仓和自动平仓全停，MT5 一条指令都不该收到。"""
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_ALL, reason="AI失灵", by="t")

    result = executor.execute_cycle()

    assert _no_trading_side_effects(mock_mt5), (
        f"HALT_ALL 期间 MT5 仍收到指令: place={mock_mt5.place_order.call_args_list} "
        f"close={mock_mt5.close_position.call_args_list}"
    )
    assert any("HALT_ALL" in e for e in result["errors"])


@pytest.mark.integration
def test_execute_cycle_halt_runs_before_db_query(executor_and_mt5):
    """★ 架构要求：E0 必须排在 E1(查库) 之前。

    人工停止要在"数据库都挂了"的时候依然生效。若 E0 排在查库之后，
    数据库一锁死，紧急停止就跟着失效——恰恰在最需要它的时刻。
    """
    executor, mock_mt5 = executor_and_mt5
    executor.db.query.side_effect = RuntimeError("database is locked")
    em.halt(em.LEVEL_HALT_ALL, reason="库挂了也要能停", by="t")

    result = executor.execute_cycle()   # 不应因数据库异常而崩

    assert _no_trading_side_effects(mock_mt5)
    assert any("紧急停止" in e or "HALT_ALL" in e for e in result["errors"])


@pytest.mark.integration
def test_execute_cycle_normal_is_unaffected(executor_and_mt5):
    """未封盘时不得引入任何行为改变——安全网不能变成新的拦截器。"""
    executor, mock_mt5 = executor_and_mt5
    seen = {"manage": 0}
    executor._manage_positions = lambda d: seen.__setitem__("manage", seen["manage"] + 1)

    result = executor.execute_cycle()

    # 正常路径下不应出现任何紧急停止相关的错误信息
    assert not any("紧急停止" in e or "HALT" in e for e in result["errors"]), result["errors"]


# ───────────────── 第二条开仓入口：跟单 ─────────────────

@pytest.mark.integration
def test_copy_order_blocked_when_halted(executor_and_mt5):
    """★ 跟单是独立于 execute_cycle 的第二条开仓通道。

    漏掉这里会出现"主号停了、跟号照跟"的荒谬场景。
    """
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_NEW, reason="", by="t")

    result = executor.copy_order({
        "direction": "BUY", "symbol": "XAUUSD", "entry": 2000.0,
        "sl": 1990.0, "tp": 2020.0, "confidence": 0.9, "ticket": 555,
    })

    assert mock_mt5.place_order.call_count == 0, "封盘期跟号不得跟单"
    assert any("紧急停止" in e for e in result["errors"])


@pytest.mark.integration
def test_copy_order_blocked_by_account_scope_only(executor_and_mt5):
    """只停这个跟号时它不跟，其他账号不受影响（多账号铁律）。"""
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_NEW, scope="acc_halt_test", reason="", by="t")

    executor.copy_order({"direction": "BUY", "symbol": "XAUUSD", "confidence": 0.9})
    assert mock_mt5.place_order.call_count == 0

    assert em.allow_open("some_other_account")[0] is True


# ───────── 旁路：守护线程绕过 execute_cycle 的四条路径 ─────────

@pytest.mark.integration
def test_manage_positions_bypass_blocked_on_halt_all(executor_and_mt5):
    """trading.py:259 会直接调 _manage_positions，必须自带防线。"""
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_ALL, reason="", by="t")

    executor._manage_positions(MagicMock(decision="BUY", confidence=0.9))

    assert _no_trading_side_effects(mock_mt5)


@pytest.mark.integration
def test_fast_l3_lock_bypass_blocked_on_halt_all(executor_and_mt5):
    """trading.py:194/367 的高频护盾线程直接调 _fast_l3_lock。"""
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_ALL, reason="", by="t")

    executor._fast_l3_lock()

    assert mock_mt5.close_position.call_count == 0, "HALT_ALL 期间篮子护盾不得自动平仓"


def _arm_mirror_scenario(executor, mock_mt5):
    """把"跟号镜像"布置成【不拦截就一定会平仓】的局面。

    ★ 这一步不能省：初版用例只是随手调一下 _mirror_leader_exits 就断言没平仓，
      结果把拦截整个拿掉它依然"通过"——因为没有主号广播时函数本来就直接 continue。
      那种用例测的是"这条路今天恰好没走"，不是"这条路被挡住了"。
    """
    te.clear_leader_exit_bus()
    executor._leader_account = lambda: types.SimpleNamespace(id="leader_acc")
    positions = [{
        "ticket": 8801, "type": "buy", "volume": 0.10, "price_open": 2000.0,
        "sl": 0.0, "tp": 0.0, "profit": 5.0, "symbol": "XAUUSD",
        "comment": "WXAI_L7701",       # L{主号票号} —— 镜像靠它对上号
    }]
    te.publish_leader_exit("leader_acc", "7701", "full_close")
    mock_mt5.close_position.return_value = {"ok": True, "ticket": 8801}
    return positions


@pytest.mark.integration
def test_mirror_scenario_really_closes_without_halt(executor_and_mt5):
    """先证明这个场景在不封盘时确实会平仓——否则下一条用例就是空转。"""
    executor, mock_mt5 = executor_and_mt5
    positions = _arm_mirror_scenario(executor, mock_mt5)

    executor._mirror_leader_exits(positions, None)

    assert mock_mt5.close_position.call_count == 1, \
        "场景没布置成功，后续的拦截断言将失去意义"


@pytest.mark.integration
def test_mirror_leader_exits_bypass_blocked_on_halt_all(executor_and_mt5):
    """trading.py:428 的跟号镜像线程直接调 _mirror_leader_exits。"""
    executor, mock_mt5 = executor_and_mt5
    positions = _arm_mirror_scenario(executor, mock_mt5)
    em.halt(em.LEVEL_HALT_ALL, reason="", by="t")

    executor._mirror_leader_exits(positions, None)

    assert mock_mt5.close_position.call_count == 0, \
        "HALT_ALL 期间跟号仍在镜像主号平仓"


def _arm_opposite_scenario(mock_mt5, monkeypatch=None):
    """布置"AI 反转 → 清反向仓"场景。

    ★ 2026-08-17：_close_opposite_for_decision 读 _positions_checked（非 get_positions），
      须同时 mock 之；且需覆盖 fixture 里返回"主号持仓"的通用 mock。
    """
    if monkeypatch is not None:
        monkeypatch.setattr(
            te, "_positions_checked",
            lambda account_id, symbol="XAUUSD": (
                True,
                [{"ticket": 9901, "type": "sell", "volume": 0.10,
                  "price_open": 2010.0, "sl": 0.0, "tp": 0.0, "profit": -30.0,
                  "symbol": "XAUUSD", "comment": "WXAI"}],
            ),
        )
    mock_mt5.get_positions.return_value = [
        {"ticket": 9901, "type": "sell", "volume": 0.10, "price_open": 2010.0,
         "sl": 0.0, "tp": 0.0, "profit": -30.0, "symbol": "XAUUSD", "comment": "WXAI"},
    ]
    mock_mt5.close_position.return_value = {"ok": True, "ticket": 9901}


@pytest.mark.integration
def test_opposite_scenario_really_closes_without_halt(executor_and_mt5, monkeypatch):
    """同样先自证：不封盘时这条路真的会平仓。"""
    executor, mock_mt5 = executor_and_mt5
    _arm_opposite_scenario(mock_mt5, monkeypatch)

    executor._close_opposite_for_decision(
        types.SimpleNamespace(decision="BUY", confidence=0.95)
    )

    assert mock_mt5.close_position.call_count >= 1, \
        "场景没布置成功，后续的拦截断言将失去意义"


@pytest.mark.integration
def test_close_opposite_bypass_blocked_on_halt_all(executor_and_mt5, monkeypatch):
    """trading.py:274 直接调 _close_opposite_for_decision。"""
    executor, mock_mt5 = executor_and_mt5
    _arm_opposite_scenario(mock_mt5, monkeypatch)
    em.halt(em.LEVEL_HALT_ALL, reason="", by="t")

    executor._close_opposite_for_decision(
        types.SimpleNamespace(decision="BUY", confidence=0.95)
    )

    assert mock_mt5.close_position.call_count == 0, \
        "HALT_ALL 期间反转平仓仍在执行"


@pytest.mark.integration
def test_bypass_paths_still_run_under_halt_new(executor_and_mt5):
    """★ 反向确认：HALT_NEW 不得误伤旁路的持仓保护。

    这条用例存在的意义是防止"一刀切"——如果实现图省事，
    把所有旁路在 HALT_NEW 下也一起挡掉，止损止盈就停了。
    """
    executor, mock_mt5 = executor_and_mt5
    em.halt(em.LEVEL_HALT_NEW, reason="", by="t")

    assert executor._auto_exit_blocked("测试") is False, \
        "HALT_NEW 绝不能冻结自动平仓，否则持仓失去止损保护"


@pytest.mark.integration
def test_auto_exit_blocked_helper_semantics(executor_and_mt5):
    executor, _ = executor_and_mt5

    assert executor._auto_exit_blocked("x") is False          # NORMAL

    em.halt(em.LEVEL_HALT_ALL, scope="acc_halt_test", reason="", by="t")
    assert executor._auto_exit_blocked("x") is True           # 账号级 HALT_ALL


# ───────────────── 跨进程：CLI 封盘后后端要能感知 ─────────────────

@pytest.mark.integration
def test_halt_written_by_other_process_takes_effect(executor_and_mt5, monkeypatch):
    """模拟 CLI（另一进程）写盘封盘，后端进程必须在下一轮读到并停手。"""
    executor, mock_mt5 = executor_and_mt5

    assert em.effective_level("acc_halt_test") == em.LEVEL_NORMAL   # 预热缓存

    import json
    with open(em.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "global": {"level": "HALT_ALL", "reason": "CLI封盘",
                       "at": "2026-08-07T20:00:00", "by": "console"},
            "accounts": {}, "flatten_requests": [],
        }, f)
    em._cache_at = 0.0   # 越过 0.5s TTL；真实场景等半秒即可

    executor.execute_cycle()

    assert _no_trading_side_effects(mock_mt5), "CLI 封盘后后端仍在交易"
