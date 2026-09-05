"""
主号开仓：成交后的状态转移必须原子（Phase 1 / V6 ExecutionController 前置）

这一组是跟号缺陷 2 的**同族排查**。跟号那条已经确证并修掉了
（tests/test_follower_copy_config.py::test_dedupe_mark_survives_bookkeeping_failure）：
    place_order 成交（不可逆）→ …一堆记账/展示代码… → 才做状态转移
    中间任何一步抛异常 ⇒ 被外层 except 吞掉 ⇒ 状态转移永远没发生。

主号 execute_cycle 是同一个模式，而且后置代码段**更长**：

    line 1374  order_result = mt5_service.place_order(...)   ← 成交，不可逆
    line 1388  result["orders"].append(...)
    line 1404  result["placed"] / result["signal"]           （跟号靠它复制，在前面，安全）
    line 1416  Step 5：get_last_context() + Trade(...) + _safe_db_write
    line 1447  self._push_feed("open", ...)                  ← **没有任何 try 包裹**
    line 1453  logger.info(...)
    line 1455  _LAST_OPEN_TS[...] = time.time()              ← 冷却状态，排在最后

`_push_feed` 只是"推一条给前端看的活动流"，纯展示，却是整段里唯一没被保护的调用
（全文 5 个 _push_feed 调用点，另外 3 个都在 try 里；剩下那个是跟号开仓，
 已被上一轮修复顺带治好 —— 也就是说主号这条是眼下唯一还带病的）。

它一旦抛：
    · execute_cycle 的外层 except 捕获 → 只往 result["errors"] 里塞一条
    · **_LAST_OPEN_TS 没有更新**
    · 单子已经在 MT5 里成交了

后果不是"少了一条前端记录"，是 **open_interval 冷却整个失效**：
    冷却默认 180s，而主循环实测 27~111s ⇒ 下一轮（几十秒后）同方向直接再开一笔。
    open_interval 正是防同方向堆仓的主闸门（生产实测主号常年堆 8~9 笔仓），
    它失效意味着一个展示层的小故障能把仓位闸门整个掀掉。

修法（与跟号对称）：把 _LAST_OPEN_TS 上移到紧贴成交点。
额外根治：给 _push_feed 套外层保护 —— 展示路径永远不该把异常冒泡到交易链路，
        否则今天这行安全、明天有人往里加一句就又不安全了。

口径沿用既有教训：
  · 断言落在「MT5 实际收到几次 place_order」和「冷却状态到底有没有落地」；
  · 每条修复用例都配护栏用例，证明修复没有把正常行为也改坏
    （尤其是"下单失败时绝不能记冷却"——记了就会误杀后面真实的开仓机会，
      那是反过来违反「多交易多赚钱」）。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


ACC = "acc_leader_atomic_1"


def _decision(action="BUY", conf=0.90):
    """字段给齐 —— 缺字段会让 execute_cycle 在 result["decision"] 处炸，
    那种炸法看起来像"闸门生效"，是最容易骗过自己的假绿。"""
    return types.SimpleNamespace(
        decision=action,
        confidence=conf,
        deepseek_vote=action,
        hunyuan_vote=action,
        deepseek_weight=0.5,
        hunyuan_weight=0.5,
        risk_level="medium",
        reasoning_summary="test",
        position_intent="open",
        target_risk_pct=None,
        quality_regime="MID",
        chronos_tp_ceiling=None,
        chronos_p10=None,
    )


def _clear_globals():
    for d in (te._REVERSAL_STATE, te._LAST_OPEN_TS, te._LAST_CLOSE_TS,
              te._L3_LAST_LOCK, te._RECON_LAST, te._RECON_OK,
              te._LEADER_EXIT_BUS, te._MIRRORED):
        d.clear()


def _build(monkeypatch, *, feed_raises=False, order_fails=False):
    """主号执行器：只 mock 外部依赖，execute_cycle 控制流保持真实。"""
    _clear_globals()

    mock_mt5 = MagicMock()
    mock_mt5.get_account_info.return_value = {"balance": 5000.0, "equity": 5000.0}
    mock_mt5.get_all_positions_checked.return_value = (True, [])
    mock_mt5.modify_sl_tp.return_value = {}
    if order_fails:
        mock_mt5.place_order.return_value = {"error": "no money"}
    else:
        mock_mt5.place_order.return_value = {
            "ticket": 12345, "price": 2000.5, "volume": 0.1}
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(te, "get_all_positions_rescanned", lambda *a, **k: [])
    monkeypatch.setattr(te, "_positions_checked", lambda *a, **k: (True, []))
    monkeypatch.setattr(te, "compute_initial_sl_tp",
                        lambda **kw: {"sl": 1990.0, "tp": 2020.0})
    monkeypatch.setattr(te, "_save_reversal_state", lambda: None)
    monkeypatch.setattr(te.emergency, "allow_open", lambda *a, **k: (True, ""))
    monkeypatch.setattr(te.emergency, "allow_auto_exit", lambda *a, **k: (True, ""))

    acc_row = types.SimpleNamespace(id=ACC, is_trading_enabled=True)
    strat_row = types.SimpleNamespace(max_positions=8)

    def _query(model):
        q = MagicMock()
        row = acc_row if getattr(model, "__name__", "") == "MT5Account" else strat_row
        q.filter.return_value.first.return_value = row
        return q

    db = MagicMock()
    db.query.side_effect = _query

    engine = MagicMock()
    engine.decide.return_value = _decision()
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 20.0},
        "regime": {"regime": "range"},
    }
    engine.market._get_current_price.return_value = {"ask": 2000.5, "bid": 2000.0}
    engine.get_last_context.return_value = {}

    ex = TradeExecutor(account_id=ACC, strategy=types.SimpleNamespace(),
                       user_id="u_test", db=db, engine=engine)

    strat_vals = {
        "min_confidence": 0.5,
        "reversal_confirm_cycles": 2,
        "enable_l3_guard": False,
        "ai_exit_enabled": False,
        "open_interval_seconds": 180,
        "churn_cooldown_seconds": 60.0,
    }
    ex._fresh_strat = lambda field, default=None: strat_vals.get(field, default)
    ex._is_leader = True
    ex._follow_leader = False
    ex.exit_agent = None
    ex.risk_engine = MagicMock()
    ex.risk_engine.check_trade_allowed.return_value = types.SimpleNamespace(
        passed=True, reject_reasons=[])
    ex._reconcile_positions = lambda: True
    ex._reconcile_against_leader = lambda: None
    ex._check_loss_cooldown = lambda: ""
    ex._apply_decision_gates = lambda d, m: {
        "passed": True, "min_conf_penalty": 0.0, "detail": "", "block_reason": ""}
    ex._close_opposite_for_decision = lambda d: None
    ex._calc_position_size = lambda *a, **k: {"lots": 0.1}
    ex._cap_to_risk_limit = lambda *a, **k: (0.1, "")
    ex._record_close = lambda *a, **k: None
    ex._safe_db_write = lambda fn, label="": None

    if feed_raises:
        # 展示层故障注入。_push_feed 在主号开仓路径上没有任何 try 包裹，
        # 这是代码事实，不是假设出来的场景。
        def _boom(*a, **k):
            raise RuntimeError("活动流推送炸了")
        ex._push_feed = _boom
    else:
        ex._push_feed = lambda *a, **k: None

    return ex, mock_mt5


def _cooldown_ts():
    return te._LAST_OPEN_TS.get(f"{ACC}:BUY", 0)


# ══════════════════════════════════════════════════════════════════
# 缺陷：成交后的冷却状态排在记账/展示代码之后
# ══════════════════════════════════════════════════════════════════

def test_cooldown_ts_lands_even_if_feed_raises(monkeypatch):
    """单子已经成交了，冷却时间戳就必须落地 —— 哪怕活动流推送炸掉。

    成交是不可逆的，配套状态转移不能被后面的展示代码打断。
    """
    ex, mt5 = _build(monkeypatch, feed_raises=True)

    ex.execute_cycle()

    assert mt5.place_order.call_count == 1, "前提不成立：这一轮本应真的下单"
    assert _cooldown_ts() > 0, (
        "单子已在 MT5 成交，但 open_interval 冷却时间戳没有落地 —— "
        "一个纯展示的活动流故障把仓位闸门整个掀掉了"
    )


def test_no_double_open_next_cycle_after_feed_failure(monkeypatch):
    """业务后果级断言：上一轮成交后展示炸了，下一轮同方向绝不能再开。

    冷却 180s、主循环 27~111s —— 时间戳丢了就是几十秒后立刻补一刀，
    这正是"主号常年堆 8~9 笔同向仓"的其中一条路径。
    """
    ex, mt5 = _build(monkeypatch, feed_raises=True)

    ex.execute_cycle()          # 第 1 轮：成交，展示炸
    ex.execute_cycle()          # 第 2 轮：应被 open_interval 冷却拦住

    assert mt5.place_order.call_count == 1, (
        f"同方向 180s 冷却内被下了 {mt5.place_order.call_count} 单 —— "
        f"上一轮的冷却状态没落地，闸门形同虚设"
    )


def test_cooldown_ts_lands_on_normal_path(monkeypatch):
    """护栏：正常路径（展示不炸）本来就该落地，修复不能把它改坏。"""
    ex, mt5 = _build(monkeypatch)

    ex.execute_cycle()

    assert mt5.place_order.call_count == 1
    assert _cooldown_ts() > 0, "正常成交路径下冷却时间戳都没落地，说明改坏了"


def test_no_cooldown_ts_when_order_rejected(monkeypatch):
    """反向护栏：单子没成交，绝不能记冷却。

    记了就会白白抑制后面 180s 内所有真实开仓机会 —— 那是反过来违反
    「多交易多赚钱」。上移状态转移时最容易犯的错就是移到了成交判定之前。
    """
    ex, mt5 = _build(monkeypatch, order_fails=True)

    ex.execute_cycle()

    assert mt5.place_order.call_count == 1, "前提不成立：本应尝试下单"
    assert _cooldown_ts() == 0, (
        "下单被拒却记了开仓冷却 —— 接下来 180s 的真实机会会被自己误杀"
    )


def test_rejected_order_does_not_block_next_cycle(monkeypatch):
    """反向护栏（业务后果）：这一轮被拒，下一轮必须还能正常尝试。"""
    ex, mt5 = _build(monkeypatch, order_fails=True)

    ex.execute_cycle()
    ex.execute_cycle()

    assert mt5.place_order.call_count == 2, (
        f"上一轮下单失败后，本轮连尝试都没尝试（call_count={mt5.place_order.call_count}）"
        f" —— 失败被错记成了冷却"
    )


# ══════════════════════════════════════════════════════════════════
# 根治：展示层异常绝不允许冒泡进交易链路
# ══════════════════════════════════════════════════════════════════

def test_push_feed_swallows_internal_failure(monkeypatch):
    """_push_feed 是纯展示。它内部炸了，不该把异常抛给调用方。

    现状：① 内存缓冲 push_trade_event 这一步完全没保护
         ② 整个方法也没有外层 try
    只有 ② DB 持久化那半段被保护了 —— 保护了不要紧的，漏了要紧的。
    """
    ex, _mt5 = _build(monkeypatch)
    del ex._push_feed                      # 去掉桩，测真实实现

    import app.services.ai_memory as mem
    monkeypatch.setattr(mem, "push_trade_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("缓冲炸了")))

    ex._push_feed("open", "测试事件", direction="BUY", confidence=0.9)
    # 走到这里就算通过：没有异常冒出来


def test_open_still_completes_when_feed_internals_fail(monkeypatch):
    """业务后果级：活动流内部故障不得影响这一轮开仓与冷却记账。"""
    ex, mt5 = _build(monkeypatch)
    del ex._push_feed

    import app.services.ai_memory as mem
    monkeypatch.setattr(mem, "push_trade_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("缓冲炸了")))

    res = ex.execute_cycle()

    assert mt5.place_order.call_count == 1
    assert _cooldown_ts() > 0, "活动流内部故障把冷却记账带崩了"
    assert res.get("placed") is True, "活动流内部故障让本轮开仓被记为未成交"
