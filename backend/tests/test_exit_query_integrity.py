"""
出场链路查询完整性 — 防漏单函数不得在最需要它的时候放弃（Phase 1 / V6）

背景（真实缺陷，与 test_position_query_integrity 同族的最后一块）：

    get_all_positions_rescanned() 的注释写着"为防 MT5 竞态漏单而生"：
    多轮扫描取并集，杜绝"3 笔 SELL 只查到 2 笔"的漏平。
    但它内部调的是旧接口 get_all_positions()，而该接口在
    Worker 掉线 / 管道断开 / 命令超时时同样返回 []，并且函数里写着：

        if not positions:
            break        # ← 把"我查不到"当成"确认没有持仓"，第一轮就放弃

    于是这个防漏单函数在 MT5 真的抖动时，不但不重试，反而提前收工返回空。

    它是五道保护性防线的**共同数据源**：
        L1585 智能持仓管理（止损/止盈/追踪/AI 出场）
        L1943 L3 篮子盈利锁利
        L1991 篮子浮亏熔断（第④道防线）
        L2027 单笔浮亏熔断（第⑤道防线）
    四处拿到空列表后一律 `return` —— 也就是说 MT5 抖一下，
    止损、锁利、熔断全线静默失效，而持仓正在亏钱。

    同一族的第二处：开仓前 existing_positions = get_positions() or []，
    其 len() 直接喂给 E3「最大持仓数硬限制」。查询失败 ⇒ count=0 ⇒
    突破 max_positions 继续开仓。注意 execute_cycle 的对账门挡不住它：
    _reconcile_positions 每账号 60s 节流一次，节流期间沿用上轮结论(True)，
    而主循环周期 27~111s —— 节流窗口内这条路完全敞开。

断言一律落在"MT5 到底收没收到 close_position / place_order"，不看返回值。
每个拦截用例都配 self-proving 用例，先证明"不修就必然发生"。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor, get_all_positions_rescanned


ACC = "acc_exit_0001"


def _pos(ticket, profit=0.0, ptype="buy", volume=0.10):
    return {
        "ticket": ticket, "type": ptype, "volume": volume,
        "price_open": 2000.0, "profit": profit, "symbol": "XAUUSD",
        "sl": 0.0, "tp": 0.0, "comment": "",
    }


@pytest.fixture
def no_sleep(monkeypatch):
    """多轮扫描里的 time.sleep(gap) 在测试里没有意义，去掉以免拖慢。"""
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)


# ══════════════════════════════════════════════════════════════
# 一、get_all_positions_rescanned —— 五道防线的共同数据源
# ══════════════════════════════════════════════════════════════
@pytest.mark.unit
def test_rescan_retries_instead_of_giving_up_on_query_failure(monkeypatch, no_sleep):
    """查询失败不是"没有持仓"，必须继续重试到用完轮次。

    这正是 rescan 存在的意义：MT5 抖动时更要多看几眼，而不是第一眼
    没看见就转身走人。
    """
    mock_mt5 = MagicMock()
    calls = {"n": 0}

    def _checked(account_id):
        calls["n"] += 1
        return False, []          # 每一轮都查询失败

    mock_mt5.get_all_positions_checked.side_effect = _checked
    mock_mt5.get_all_positions.return_value = []      # 旧接口：失败与空仓同形
    monkeypatch.setattr(te, "mt5_service", mock_mt5)

    get_all_positions_rescanned(ACC, max_rounds=3, gap=0.1)

    assert calls["n"] == 3, (
        f"查询失败后只试了 {calls['n']} 轮就放弃 —— 防漏单函数在最需要它的时候提前收工"
    )


@pytest.mark.unit
def test_rescan_recovers_positions_when_first_round_fails(monkeypatch, no_sleep):
    """首轮失败、次轮成功 ⇒ 必须拿到真实持仓。

    这是"该平不平"的直接来源：未修版本首轮拿到 [] 就 break，
    调用方 `if not positions: return` ⇒ 止损/熔断当轮全部跳过。
    """
    mock_mt5 = MagicMock()
    seq = [(False, []), (True, [_pos(7001, -80.0), _pos(7002, -40.0)])]

    def _checked(account_id):
        return seq.pop(0) if seq else (True, [])

    mock_mt5.get_all_positions_checked.side_effect = _checked
    mock_mt5.get_all_positions.return_value = []
    monkeypatch.setattr(te, "mt5_service", mock_mt5)

    positions = get_all_positions_rescanned(ACC, max_rounds=3, gap=0.1)

    assert positions, "首轮查询失败就返回空 —— 持仓明明还在，五道防线全部落空"
    assert {p["ticket"] for p in positions} == {7001, 7002}


@pytest.mark.unit
def test_rescan_returns_none_when_all_rounds_fail(monkeypatch, no_sleep):
    """全部轮次都失败 ⇒ 返回 None（不可信），而不是 []（确认空仓）。

    None 与 [] 对调用方的 `if not positions: return` 行为一致（都跳过），
    所以零破坏；但语义上把"我不知道"和"确实没有"分开，
    日志能报警，后续也不会有人误把它当"已确认平完"的依据。
    """
    mock_mt5 = MagicMock()
    mock_mt5.get_all_positions_checked.return_value = (False, [])
    mock_mt5.get_all_positions.return_value = []
    monkeypatch.setattr(te, "mt5_service", mock_mt5)

    assert get_all_positions_rescanned(ACC, max_rounds=2, gap=0.1) is None


@pytest.mark.unit
def test_rescan_stops_early_when_confirmed_empty(monkeypatch, no_sleep):
    """自证 + 等价性：确认空仓（ok=True 且空）时仍要提前结束，不做无谓重试。

    没有这条，上面"必须重试"的用例可以靠"永远重试"作弊通过，
    但那会让每轮扫描白白多花 gap 秒，拖慢主循环。
    """
    mock_mt5 = MagicMock()
    calls = {"n": 0}

    def _checked(account_id):
        calls["n"] += 1
        return True, []

    mock_mt5.get_all_positions_checked.side_effect = _checked
    monkeypatch.setattr(te, "mt5_service", mock_mt5)

    result = get_all_positions_rescanned(ACC, max_rounds=3, gap=0.1)

    assert calls["n"] == 1, "确认空仓后还在重试，白白拖慢主循环"
    assert result == [] and result is not None, "确认空仓应返回 []（与'查不到'的 None 区分）"


@pytest.mark.unit
def test_rescan_merges_across_rounds(monkeypatch, no_sleep):
    """自证：多轮并集能力不能被本次改动破坏（这是函数的原始职责）。"""
    mock_mt5 = MagicMock()
    seq = [(True, [_pos(8001)]), (True, [_pos(8002)])]

    def _checked(account_id):
        return seq.pop(0) if seq else (True, [])

    mock_mt5.get_all_positions_checked.side_effect = _checked
    monkeypatch.setattr(te, "mt5_service", mock_mt5)

    positions = get_all_positions_rescanned(ACC, max_rounds=2, gap=0.1)

    assert {p["ticket"] for p in positions} == {8001, 8002}, "多轮并集能力被破坏"


@pytest.mark.unit
def test_rescan_falls_back_when_checked_api_absent(monkeypatch, no_sleep):
    """兼容性：注入的 mt5 对象没有 checked 接口时（旧 mock/旧插件），
    必须优雅回退到旧接口，不能抛 AttributeError 把整条出场链路打断。"""
    legacy = types.SimpleNamespace(
        get_all_positions=lambda account_id: [_pos(9001)]
    )
    monkeypatch.setattr(te, "mt5_service", legacy)

    positions = get_all_positions_rescanned(ACC, max_rounds=1, gap=0.1)

    assert [p["ticket"] for p in positions] == [9001]


# ══════════════════════════════════════════════════════════════
# 二、开仓闸门 —— E3 最大持仓数不得因查询失败而失守
# ══════════════════════════════════════════════════════════════
@pytest.fixture
def open_env(monkeypatch, tmp_path_factory, request):
    """搭一个能真正走到"开仓前闸门"的 execute_cycle 环境。

    关键构造：
      get_positions_checked() → (True, 8 笔持仓)   真相：已达 max_positions
      get_positions()         → []                 旧接口：这一刻查询失败
    未修复的代码读旧接口 ⇒ current_count=0 ⇒ 开出第 9 笔。
    """
    import app.services.emergency as em
    base = tmp_path_factory.getbasetemp()
    f = base / f"eqi_{abs(hash(request.node.nodeid)) % 10**10}.json"
    monkeypatch.setattr(em, "STATE_FILE", str(f))
    monkeypatch.setattr(em, "_cache", None, raising=False)
    monkeypatch.setattr(em, "_cache_at", 0.0, raising=False)
    monkeypatch.setattr(em, "_cache_mtime", -1.0, raising=False)

    mock_mt5 = MagicMock()
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)
    te._RECON_LAST.clear()
    te._RECON_OK.clear()
    # 开仓/churn 冷却是模块级全局字典，上一个用例真的开过单就会污染下一个，
    # 让自证用例误报"拦截过头"。逐个清空，保证用例之间互不干扰。
    for _g in ("_LAST_OPEN_TS", "_LAST_CLOSE_TS"):
        if hasattr(te, _g):
            getattr(te, _g).clear()

    mock_mt5.get_account_info.return_value = {"balance": 10000.0, "equity": 10000.0}
    mock_mt5.place_order.return_value = {"ticket": 123456, "price": 2005.0, "volume": 0.1}

    # 用真实 DebateDecision，而不是手搭 SimpleNamespace：
    # 后者少一个字段就在 execute_cycle 里抛 AttributeError 静默退出，
    # 使"没下单"的断言变成假绿（本文件第一版就踩了这个坑）。
    from app.core.meta_agent import DebateDecision
    decision = DebateDecision(
        decision="BUY", confidence=0.90,
        deepseek_weight=0.5, hunyuan_weight=0.5,
        deepseek_vote="BUY", hunyuan_vote="BUY",
        reasoning_summary="test", risk_level="low",
        consensus="strong", quality_regime="HIGH",
    )
    engine = MagicMock()
    engine.decide.return_value = decision
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 15.0, "d1_atr": 15.0},
        "current_price": 2005.0,
    }
    engine.market._get_current_price.return_value = {"ask": 2005.0, "bid": 2004.5}

    strategy = types.SimpleNamespace(
        max_risk_per_trade_pct=2.0, max_lot_per_trade=1.0,
        min_lot_per_trade=0.01, max_position_lots=5.0,
        max_positions=8, sizing_scale_mode="manual",
        capital_source="live", base_capital=0,
    )
    account = types.SimpleNamespace(
        id=ACC, is_trading_enabled=True, is_market_primary=True,
        user_id="user_test", account_name="test",
    )

    # db.query(Model) 必须按模型返回对应对象：查策略配置给 strategy，
    # 查账号给 account。统一返回同一个 SimpleNamespace 会在
    # `account.is_trading_enabled` 处抛 AttributeError，让 execute_cycle
    # 在到达开仓闸门之前就异常退出 —— 那样拦截用例会"假绿"。
    from app.models.strategy import StrategyConfig as _SC
    from app.models.mt5_account import MT5Account as _ACC_MODEL

    def _query(model, *_a, **_k):
        q = MagicMock()
        target = strategy if model is _SC else account
        q.filter.return_value.first.return_value = target
        q.filter.return_value.all.return_value = []
        q.filter.return_value.order_by.return_value.all.return_value = []
        q.filter.return_value.order_by.return_value.first.return_value = None
        return q

    db = MagicMock()
    db.query.side_effect = _query

    ex = TradeExecutor(
        account_id=ACC, strategy=strategy, user_id="user_test", db=db, engine=engine,
    )
    ex._fresh_strat = lambda field, default=None: getattr(strategy, field, default)
    ex._reconcile_against_leader = lambda: None
    ex._check_loss_cooldown = lambda: ""
    ex._manage_positions = MagicMock()
    ex._close_opposite_for_decision = MagicMock()
    ex._apply_decision_gates = lambda *_a, **_k: {
        "passed": True, "detail": "", "block_reason": "", "min_conf_penalty": 0.0,
    }
    ex.exit_agent = MagicMock()
    ex.risk_engine = MagicMock()
    ex.risk_engine.check_trade_allowed.return_value = types.SimpleNamespace(
        passed=True, reject_reasons=[], warnings=[], max_allowed_lots=5.0,
    )
    return ex, mock_mt5, engine


@pytest.mark.integration
def test_open_gate_blocks_when_query_fails_inside_recon_throttle(open_env):
    """复刻真实现场：对账处于 60s 节流窗口内（沿用上轮 True），
    而开仓前这一刻 MT5 查询失败 ⇒ 绝不开新仓。

    这是唯一能走到"开仓前闸门"的路径。若不预置节流状态，
    execute_cycle 会先被 _reconcile_positions 的对账门拦下 ——
    那样测的是对账门（已有用例覆盖），新闸门等于没测。
    反向验证时正是靠这条用例才发现上一版用例打偏了靶子。
    """
    import time as _t
    ex, mock_mt5, _engine = open_env
    te._RECON_LAST[ACC] = _t.time()      # 60s 内已对过账 → 本轮跳过对账
    te._RECON_OK[ACC] = True             # 沿用上轮结论：账本可信

    mock_mt5.get_positions_checked.return_value = (False, [])   # 开仓前这一刻查不到
    mock_mt5.get_all_positions_checked.return_value = (True, [])
    mock_mt5.get_positions.return_value = []                    # 旧接口同样报空

    ex.execute_cycle()

    assert mock_mt5.place_order.call_count == 0, (
        "节流窗口内查询失败仍开新仓 —— E3 最大持仓数闸门失守，可无限叠仓"
    )


@pytest.mark.integration
def test_open_gate_respects_true_position_count(open_env):
    """真相是"已满 8 笔"而旧接口报 0 笔 ⇒ 必须按真相拒开。

    这条直接刻画节流窗口内的现场：对账 60s 前刚成功过（结论 True），
    本轮 MT5 抖动，旧接口返回 [] —— 未修版本据此开出第 9 笔。
    """
    ex, mock_mt5, _engine = open_env
    at_cap = [_pos(6000 + i) for i in range(8)]
    mock_mt5.get_positions_checked.return_value = (True, at_cap)
    mock_mt5.get_all_positions_checked.return_value = (True, at_cap)
    mock_mt5.get_positions.return_value = []          # 旧接口谎报空仓

    ex.execute_cycle()

    assert mock_mt5.place_order.call_count == 0, (
        "按旧接口的 0 笔放行，实际已持有 8 笔 —— 突破 max_positions"
    )


@pytest.mark.integration
def test_open_proceeds_when_query_ok_and_below_cap(open_env):
    """自证场景：查询正常且未达上限 ⇒ 必须照常开仓。

    没有这条，上面两条"没开仓"可能只是因为链路根本没跑到下单，
    那就是在测空气 —— 而且会掩盖"改完之后再也不开单"的灾难。
    """
    ex, mock_mt5, _engine = open_env
    mock_mt5.get_positions_checked.return_value = (True, [_pos(6001)])
    mock_mt5.get_all_positions_checked.return_value = (True, [_pos(6001)])
    mock_mt5.get_positions.return_value = [_pos(6001)]

    ex.execute_cycle()

    assert mock_mt5.place_order.call_count == 1, (
        "查询正常、未达上限却不开单 —— 拦截过头，直接违反'多交易多赚钱'铁律"
    )
