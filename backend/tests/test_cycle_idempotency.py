"""
单轮幂等性 — 一个决策周期内，持仓管理只能跑一次（Phase 1 / V6 ExecutionController 前置）

背景（真实缺陷，高频触发）：
    execute_cycle 里 Step 2（line ~1200）已经调过一次 self._manage_positions()。
    之后若命中以下任一「只拦开新仓」的闸门，代码又各调了一次：
        · E3 最大持仓数已满
        · 同方向开仓间隔冷却（open_interval_seconds，默认 180s）
        · churn 抑制（刚平仓禁止秒开，默认 60s）

    这三条都是**高频路径**，不是边缘情况：
      - 主号长期堆 8~9 笔仓（记忆："持仓永远堆积"）→ E3 常年命中；
      - 开仓冷却 180s 远大于主循环 27~111s → 绝大多数轮次都在冷却里。

    重复执行不是"多做一次无害的检查"，它有真实破坏性：

    1) L2 反转防抖被腰斩 —— 最严重
       _manage_positions 里 reversal_confirm_cycles(默认2) 的实现是往
       _REVERSAL_STATE 的 hist 里 append 一次，len(hist) >= need 即确认平仓。
       同一轮跑两次 ⇒ 一轮之内 hist 就从 1 涨到 2 ⇒ **立刻确认全平**。
       "连续 N 轮同向确认才平"的防抖等于不存在，一次假反转就清掉整批持仓。
       而命中分支恰恰是满仓/冷却期 —— 持仓最多、最需要防抖的时候。

    2) 分批平仓被执行两次 —— 直接砍利润
       第二次调用会重新查持仓，拿到的是已经平过一次的**剩余量**。
       1.0 手 / close_pct=0.5：第一次平 0.5 剩 0.5，第二次再平 0.25 →
       本轮实际平掉 75%，而不是设计的 50%。分批止盈本是"留仓位吃后续利润"，
       重复执行把肉砍了 —— 违反铁律「多交易多赚钱」。

    3) AI 出场 Agent 被重复调用 → 云 token 双倍烧。

    反证（说明这不是我臆想的问题）：
    L3 篮子锁利那段自带 120s 冷却（`time.time() - last_lock > 120`），
    第二次调用会被冷却挡掉。作者在 L3 上意识到了重入问题并防了，
    L2 防抖和分批平仓这两条却漏防。同一函数内一处防一处不防，是典型的遗漏。

测试口径沿用既有教训：
  · 断言一律落在「MT5 到底收没收到 close_position」，不看返回值（防假安全）；
  · 每条拦截类断言都配 self-proving 用例，证明"不修就必然发生"（防测空气）；
  · 全局字典逐个清空（_REVERSAL_STATE/_LAST_OPEN_TS/_LAST_CLOSE_TS/...），
    否则上一个用例的残留会让下一个用例假绿。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


ACC = "acc_cycle_idem_01"


def _decision(action="BUY", conf=0.90, position_intent="open"):
    """齐全字段的决策对象 —— 缺字段会让 execute_cycle 在 result["decision"] 处炸，
    那种炸法看起来像"拦截生效"，是最容易骗过自己的假绿。"""
    return types.SimpleNamespace(
        decision=action,
        confidence=conf,
        deepseek_vote=action,
        hunyuan_vote=action,
        deepseek_weight=0.5,
        hunyuan_weight=0.5,
        risk_level="medium",
        reasoning_summary="test",
        position_intent=position_intent,
        target_risk_pct=None,
        quality_regime="MID",
        chronos_tp_ceiling=None,
        chronos_p10=None,
    )


def _positions(n=2, volume=1.0, ptype="buy"):
    return [
        {"ticket": 7000 + i, "type": ptype, "volume": volume,
         "price_open": 2000.0, "profit": 5.0, "symbol": "XAUUSD",
         "sl": 1990.0, "tp": 2020.0, "comment": "WXAI"}
        for i in range(n)
    ]


def _clear_globals():
    """全局状态清零 —— 假绿三连里最阴的一条就是全局字典跨用例污染。"""
    te._REVERSAL_STATE.clear()
    te._LAST_OPEN_TS.clear()
    te._LAST_CLOSE_TS.clear()
    te._L3_LAST_LOCK.clear()
    te._RECON_LAST.clear()
    te._RECON_OK.clear()
    te._LEADER_EXIT_BUS.clear()
    te._MIRRORED.clear()
    # ★ 2026-08-10 模块级防切单标记（跨实例共享），跨用例必须清，否则
    #   参数化用例间互相污染（第一个用例标记 #7000/7001 → 第二个用例"已减半过"不触发）。
    if hasattr(te, "_PARTIAL_DONE"):
        te._PARTIAL_DONE.clear()


def _build(monkeypatch, *, exit_plan, positions, max_positions=8,
           decision=None, gate_passed=True, min_conf=0.5):
    """搭一个能真跑 execute_cycle 的主号执行器。

    只 mock 外部依赖（MT5 / 云 AI / DB），execute_cycle 与 _manage_positions
    的**控制流本身保持真实**——否则测的就不是那条 bug 了。
    """
    _clear_globals()
    decision = decision or _decision()

    mock_mt5 = MagicMock()
    mock_mt5.get_account_info.return_value = {"balance": 5000.0, "equity": 5000.0}
    mock_mt5.close_position.return_value = {"ticket": 1, "profit": 1.0}
    mock_mt5.modify_sl_tp.return_value = {}
    mock_mt5.place_order.return_value = {"ticket": 12345, "price": 2000.5, "volume": 0.1}
    mock_mt5.get_all_positions_checked.return_value = (True, positions)
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)
    # ★ 2026-08-17 修复：视觉看护生产者线程是真实 GPU 服务，不该在单测里启动——
    #   且上面 patch 了 time.sleep(全局单例模块) 会让看护线程的节流 sleep 变 no-op
    #   → 无限高速循环调 rescan（实测 19 万次）。测试统一禁用看护启动。
    monkeypatch.setattr(TradeExecutor, "_ensure_vision_exit", lambda self: None)

    # 持仓查询：两个入口都给同一份真实清单。
    # 这里顺带做"真正执行了几次持仓管理"的计数器 —— 必须量在幂等守卫**之后**的
    # 动作上。若把计数包在 _manage_positions 最外层，守卫跳过时调用照样计数，
    # 度量的就成了"调用次数"而非"执行次数"，修好了也显示为红（假红）。
    calls = {"n": 0}

    def _rescan(*a, **k):
        calls["n"] += 1
        return list(positions)

    monkeypatch.setattr(te, "get_all_positions_rescanned", _rescan)
    monkeypatch.setattr(te, "_positions_checked",
                        lambda *a, **k: (True, list(positions)))
    # 出场规则引擎：按用例指定的 plan 返回
    monkeypatch.setattr(te, "smart_evaluate_position",
                        lambda **kw: dict(exit_plan))
    monkeypatch.setattr(te, "compute_initial_sl_tp",
                        lambda **kw: {"sl": 1990.0, "tp": 2020.0})
    # 反转状态落盘：测试里不碰文件
    monkeypatch.setattr(te, "_save_reversal_state", lambda: None)
    # 人工紧急处置：放行
    monkeypatch.setattr(te.emergency, "allow_open", lambda *a, **k: (True, ""))
    monkeypatch.setattr(te.emergency, "allow_auto_exit", lambda *a, **k: (True, ""))

    # DB：按模型分发，绝不"同一个对象喂所有查询"（那会让 max_positions 读到假值）
    acc_row = types.SimpleNamespace(id=ACC, is_trading_enabled=True)
    strat_row = types.SimpleNamespace(max_positions=max_positions)

    def _query(model):
        q = MagicMock()
        row = acc_row if getattr(model, "__name__", "") == "MT5Account" else strat_row
        q.filter.return_value.first.return_value = row
        return q

    db = MagicMock()
    db.query.side_effect = _query

    engine = MagicMock()
    engine.decide.return_value = decision
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 20.0},
        "regime": {"regime": "range"},          # 非趋势 → 不触发顺势保护改写 action
    }
    engine.market._get_current_price.return_value = {"ask": 2000.5, "bid": 2000.0}
    engine.get_last_context.return_value = {}

    ex = TradeExecutor(account_id=ACC, strategy=types.SimpleNamespace(),
                       user_id="u_test", db=db, engine=engine)

    strat_vals = {
        "min_confidence": min_conf,
        "reversal_confirm_cycles": 2,
        "enable_l3_guard": False,        # 隔离 L3，避免它抢先全平污染断言
        "ai_exit_enabled": False,        # 不走云 AI 出场，聚焦规则引擎链路
        "open_interval_seconds": 180,
        "churn_cooldown_seconds": 60.0,
        "ai_reverse_close_confidence": 0.60,
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
        "passed": gate_passed, "min_conf_penalty": 0.0,
        "detail": "", "block_reason": "gate blocked"}
    ex._close_opposite_for_decision = lambda d: None
    ex._calc_position_size = lambda *a, **k: {"lots": 0.1}
    ex._cap_to_risk_limit = lambda *a, **k: (0.1, "")
    ex._push_feed = lambda *a, **k: None
    ex._record_close = lambda *a, **k: None
    ex._safe_db_write = lambda fn, label="": None

    return ex, mock_mt5, calls


# 三条会导致重复调用的高频闸门
def _arm_branch(ex, branch):
    """把执行器摆到指定闸门即将命中的状态。"""
    if branch == "max_positions":
        return                                    # 由 max_positions=1 触发
    if branch == "open_interval":
        te._LAST_OPEN_TS[f"{ACC}:BUY"] = te.time.time()
    elif branch == "churn":
        te._LAST_CLOSE_TS[f"{ACC}:BUY"] = te.time.time()


BRANCHES = ["max_positions", "open_interval", "churn"]


# ══════════════════════════════════════════════════════════════════
# 组 1：核心 —— 一轮只能跑一次持仓管理
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("branch", BRANCHES)
def test_manage_positions_runs_once_per_cycle(monkeypatch, branch):
    """三个「只拦开新仓」的闸门命中时，持仓管理都不该被跑第二遍。"""
    maxp = 1 if branch == "max_positions" else 8
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "hold", "reason": "", "new_sl": None, "new_tp": None},
        positions=_positions(n=2),
        max_positions=maxp,
    )
    _arm_branch(ex, branch)

    res = ex.execute_cycle()

    assert calls["n"] == 1, (
        f"[{branch}] 持仓管理在同一轮被执行了 {calls['n']} 次。"
        f"重复执行会腰斩 L2 反转防抖、把分批平仓平成双倍。errors={res['errors']}"
    )


@pytest.mark.parametrize("branch", BRANCHES)
def test_branch_is_actually_reached(monkeypatch, branch):
    """self-proving：证明上面那三条用例真的走到了目标闸门，
    而不是被更早的 return 挡掉（那样断言 calls==1 就是在测空气）。
    ★ 2026-08-10 信号塔同向去重：主号同向已有持仓时先被去重拦（比冷却/churn 更早），
      为验证冷却/churn 闸门本身可达，用 add 意图(金字塔加仓,明确放行)绕过去重。"""
    maxp = 1 if branch == "max_positions" else 8
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "hold", "reason": "", "new_sl": None, "new_tp": None},
        positions=_positions(n=2),
        max_positions=maxp,
        decision=_decision(position_intent="add"),
    )
    _arm_branch(ex, branch)

    res = ex.execute_cycle()
    joined = " ".join(res["errors"])

    expect = {
        "max_positions": "已达最大持仓数",
        "open_interval": "冷却中",
        "churn": "churn抑制中",
    }[branch]
    assert expect in joined, f"[{branch}] 未走到目标闸门，errors={res['errors']}"
    assert not res["placed"], f"[{branch}] 闸门命中却仍下单了"
    assert mt5.place_order.call_count == 0, f"[{branch}] 闸门命中却仍发出了开仓指令"


# ══════════════════════════════════════════════════════════════════
# 组 2：L2 反转防抖不得被腰斩（本 bug 危害最大的一条）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("branch", BRANCHES)
def test_l2_reversal_debounce_not_halved(monkeypatch, branch):
    """reversal_confirm_cycles=2 ⇒ 单独一轮绝不能确认平仓。

    未修复时：一轮内 hist 被 append 两次 → len=2 ≥ need=2 → 当场全平。
    这正是"假反转把整批持仓洗掉"的机制。
    """
    maxp = 1 if branch == "max_positions" else 8
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "reverse_signal", "reason": "AI反向",
                   "new_sl": None, "new_tp": None},
        positions=_positions(n=2, ptype="sell"),   # 持 SELL，AI 报 BUY = 反向
        max_positions=maxp,
    )
    _arm_branch(ex, branch)

    ex.execute_cycle()

    assert mt5.close_position.call_count == 0, (
        f"[{branch}] 一轮之内就把反转确认满了并平仓 "
        f"({mt5.close_position.call_count} 次)，2 轮防抖被压成 1 轮。"
    )
    hist = te._REVERSAL_STATE.get(ACC, {}).get("sell", [])
    assert len(hist) == 1, f"[{branch}] 单轮累计了 {len(hist)} 次反转确认，应为 1"


@pytest.mark.parametrize("n_pos", [2, 3, 8])
def test_l2_not_broken_by_position_count(monkeypatch, n_pos):
    """★ 独立缺陷：持仓笔数不得推进反转计数。

    _REVERSAL_STATE 按【方向】聚合计数，推进却写在【按每笔持仓】的循环里。
    持 N 笔同向仓时，第 2 笔就把 need=2 推满 → 一轮内当场平仓。
    N 越大砍得越多，而主号常年堆 8~9 笔同向仓。

    注意本用例**不布置任何闸门**（max_positions 放到 100、无冷却），
    走的是完全正常的路径 —— 用来证明这个缺陷独立于「重复调用」而存在。
    """
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "reverse_signal", "reason": "AI反向",
                   "new_sl": None, "new_tp": None},
        positions=_positions(n=n_pos, ptype="sell"),
        max_positions=100,
    )

    ex.execute_cycle()

    assert calls["n"] == 1, "本用例应只跑一次持仓管理（未布置闸门）"
    assert mt5.close_position.call_count == 0, (
        f"{n_pos} 笔同向持仓在**一轮内**就被平掉 "
        f"{mt5.close_position.call_count} 笔：反转防抖被持仓笔数推满，"
        f"'连续2轮确认'退化成'持够2笔就平'"
    )
    hist = te._REVERSAL_STATE.get(ACC, {}).get("sell", [])
    assert len(hist) == 1, (
        f"{n_pos} 笔持仓把计数推到了 {len(hist)}，一轮只该推进 1 次"
    )


def test_l2_still_confirms_across_two_real_cycles(monkeypatch):
    """反向护栏：修复不能矫枉过正把防抖变成"永远确认不了"。
    两个真实周期（各一次 execute_cycle）之后，第 2 轮必须确认并平仓。"""
    plan = {"action": "reverse_signal", "reason": "AI反向",
            "new_sl": None, "new_tp": None}
    pos = _positions(n=2, ptype="sell")

    ex1, mt5_1, _ = _build(monkeypatch, exit_plan=plan, positions=pos, max_positions=1)
    ex1.execute_cycle()
    assert mt5_1.close_position.call_count == 0, "第 1 轮就不该平"

    # 第 2 轮：新实例（生产里每轮都是 new TradeExecutor），但 _REVERSAL_STATE 是
    # 模块级的、跨轮累积 —— 所以这里**不能**清全局，清了就测不到累积。
    saved = {k: dict(v) for k, v in te._REVERSAL_STATE.items()}
    ex2, mt5_2, _ = _build(monkeypatch, exit_plan=plan, positions=pos, max_positions=1)
    te._REVERSAL_STATE.update(saved)

    ex2.execute_cycle()
    assert mt5_2.close_position.call_count == len(pos), (
        f"第 2 轮应确认反转并全平 {len(pos)} 笔，实际平了 "
        f"{mt5_2.close_position.call_count} 笔 —— 防抖被改成了永不触发"
    )


# ══════════════════════════════════════════════════════════════════
# 组 3：分批平仓不得翻倍（直接关系利润）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("branch", BRANCHES)
def test_partial_close_not_doubled(monkeypatch, branch):
    """每笔持仓在一轮里最多被分批平一次。

    未修复时同一笔会被平两次，1.0 手 50% 会变成实际平掉 75%，
    分批止盈留下来吃后续行情的仓位被提前砍掉。
    """
    maxp = 1 if branch == "max_positions" else 8
    pos = _positions(n=2, volume=1.0)
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "partial_close", "close_pct": 0.5,
                   "reason": "分批止盈", "new_sl": None, "new_tp": None},
        positions=pos,
        max_positions=maxp,
    )
    _arm_branch(ex, branch)

    ex.execute_cycle()

    assert mt5.close_position.call_count == len(pos), (
        f"[{branch}] {len(pos)} 笔持仓触发了 {mt5.close_position.call_count} 次分批平仓，"
        f"同一轮被平了两遍 → 实际平仓比例翻倍，砍掉本该吃肉的仓位"
    )
    for call in mt5.close_position.call_args_list:
        assert call.args[2] == pytest.approx(0.5), (
            f"[{branch}] 分批手数应为 1.0×50%=0.5，实际 {call.args[2]}"
        )


# ══════════════════════════════════════════════════════════════════
# 组 4：保护不能被削弱 —— 单次调用的分支必须照常执行
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("case,kwargs", [
    ("hold",       {"decision": _decision(action="HOLD")}),
    ("low_conf",   {"decision": _decision(conf=0.10), "min_conf": 0.65}),
    ("gate_block", {"gate_passed": False}),
])
def test_single_call_branches_still_protect(monkeypatch, case, kwargs):
    """HOLD / 置信不足 / 门控拦截 这三条分支里，持仓管理是**唯一一次**调用。
    幂等守卫绝不能把它们一起挡掉，否则持仓在这些轮次里彻底失去止损止盈保护。"""
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "full_close", "reason": "该走了",
                   "new_sl": None, "new_tp": None},
        positions=_positions(n=2),
        max_positions=8,
        **kwargs,
    )

    ex.execute_cycle()

    assert calls["n"] == 1, f"[{case}] 持仓管理被执行 {calls['n']} 次，应恰好 1 次"
    assert mt5.close_position.call_count == 2, (
        f"[{case}] 持仓保护没跑：full_close 应平掉 2 笔，实际 "
        f"{mt5.close_position.call_count} 笔 —— 守卫误伤了正常保护路径"
    )


def test_normal_open_path_still_manages_then_opens(monkeypatch):
    """一路放行到真正下单的正常路径：持仓管理跑 1 次，且订单照常发出。
    防止守卫把主链路一起改坏（交易笔数不能因为这个修复而下降）。
    ★ 2026-08-10 信号塔同向去重：主号同向已有持仓时不再开第二单（用户方案 A），
      但 AI 显式 position_intent=add(金字塔加仓) 仍放行 → 用 add 意图验证开仓路径。"""
    ex, mt5, calls = _build(
        monkeypatch,
        exit_plan={"action": "hold", "reason": "", "new_sl": None, "new_tp": None},
        positions=_positions(n=1),
        max_positions=8,
        decision=_decision(position_intent="add"),
    )

    res = ex.execute_cycle()

    assert calls["n"] == 1, f"正常路径持仓管理执行了 {calls['n']} 次"
    assert res["placed"] is True, f"正常路径未能开仓：{res['errors']}"
    assert mt5.place_order.call_count == 1, "正常路径应恰好发出 1 笔开仓指令"
