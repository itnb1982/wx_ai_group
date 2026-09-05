"""
跟号跟单：策略配置必须对跟号同样生效 + 成交后状态必须原子落地（Phase 1 / V6）

缺陷 1：churn 冷却在跟号路径上是硬编码的
    主号 execute_cycle：
        _churn_cooldown = float(self._fresh_strat("churn_cooldown_seconds", 60.0) or 60.0)
        _is_churn_suppressed(..., cooldown=_churn_cooldown)      ← 读客户配置
    跟号 copy_order：
        _is_churn_suppressed(..., cooldown=60.0)                 ← 写死
    （而 _is_churn_suppressed 自身默认又是 90.0——同一个语义三个数字。）

    这条违反项目铁律「每账号策略独立可配、绝不写死」，而且伤害是双向的：
      · 客户把冷却调大（想更强地抑制平亏秒开）→ 主号生效、跟号仍按 60s 放行；
      · 客户把冷却调小（想更活跃）→ 跟号仍拦 60s ⇒ **漏跟主号这一单**，
        主副持仓从此不一致，且少做一笔交易 —— 直接撞上「多交易多赚钱」。
    第二个方向更要命：主副一致是这套跟单系统的核心承诺。

缺陷 2：跟单去重标记落在记账之后
    place_order 成交（不可逆）→ 构造 Trade → 写库 → 才 _mark_copied()。
    中间任何一步抛异常都会跳到 except，去重标记never落地，
    下一轮同一个主号票号会被**再跟一次** ⇒ 主号 1 单、跟号 2 单。
    成交是不可逆副作用，它一旦发生，配套的状态转移就必须立刻完成，
    不能被后续的记账代码打断（V6 状态机的基本要求）。

沿用既有口径：断言落在「MT5 到底收没收到 place_order」，不看返回值。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


FOLLOWER = "acc_follower_cfg_1"
LEADER_TICKET = 555001


def _signal(ticket=LEADER_TICKET):
    return {
        "direction": "BUY",
        "symbol": "XAUUSD",
        "entry": 2000.0,
        "sl": 1990.0,
        "tp": 2020.0,
        "confidence": 0.8,
        "ticket": ticket,
        "comment": "WXAI|BUY|C80%",
    }


def _build(monkeypatch, *, strat_vals=None, trade_raises=False):
    te._LAST_CLOSE_TS.clear()
    te._LAST_COPIED_SIGNAL.clear()
    te._LAST_OPEN_TS.clear()

    mock_mt5 = MagicMock()
    mock_mt5.get_account_info.return_value = {"balance": 3000.0, "equity": 3000.0}
    mock_mt5.place_order.return_value = {"ticket": 99001, "price": 2000.3, "volume": 0.1}
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te, "_positions_checked", lambda *a, **k: (True, []))
    monkeypatch.setattr(te.emergency, "allow_open", lambda *a, **k: (True, ""))

    if trade_raises:
        # 模拟成交之后的记账环节炸掉（字段变更 / 模型约束 / 任何未来重构引入的异常）
        def _boom(*a, **k):
            raise RuntimeError("记账阶段炸了")
        monkeypatch.setattr(te, "Trade", _boom)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(
        id=FOLLOWER, is_trading_enabled=True)

    engine = MagicMock()
    engine.market._get_current_price.return_value = {"ask": 2000.5, "bid": 2000.0}

    ex = TradeExecutor(account_id=FOLLOWER, strategy=types.SimpleNamespace(),
                       user_id="u_test", db=db, engine=engine)

    vals = dict(strat_vals or {})
    ex._fresh_strat = lambda field, default=None: vals.get(field, default)
    ex._check_loss_cooldown = lambda: ""
    ex._calc_position_size = lambda *a, **k: {"lots": 0.1}
    ex._cap_to_risk_limit = lambda *a, **k: (0.1, "")
    ex.risk_engine = MagicMock()
    ex.risk_engine.check_trade_allowed.return_value = types.SimpleNamespace(
        passed=True, reject_reasons=[])
    ex._safe_db_write = lambda fn, label="": None
    ex._push_feed = lambda *a, **k: None
    return ex, mock_mt5


def _just_closed(seconds_ago: float):
    """把"本跟号 BUY 方向刚平过仓"这个状态摆到指定秒数之前。"""
    te._LAST_CLOSE_TS[f"{FOLLOWER}:BUY"] = te.time.time() - seconds_ago


# ══════════════════════════════════════════════════════════════════
# 缺陷 1：churn 冷却必须读本账号策略配置
# ══════════════════════════════════════════════════════════════════

def test_follower_churn_honors_shorter_customer_setting(monkeypatch):
    """客户把冷却调短到 30s，45s 前平的仓不该再拦跟单。

    硬编码 60s 会在这里拦下 → 跟号漏跟主号这一单 → 主副持仓不一致 + 少赚一笔。
    """
    ex, mt5 = _build(monkeypatch, strat_vals={"churn_cooldown_seconds": 30.0})
    _just_closed(45)

    res = ex.copy_order(_signal())

    assert mt5.place_order.call_count == 1, (
        f"客户设 churn=30s、45s 前平的仓，本应放行跟单，却被拦下："
        f"{res['errors']} —— 跟号漏跟主号，主副持仓不一致"
    )


def test_follower_churn_honors_longer_customer_setting(monkeypatch):
    """客户把冷却调长到 300s，100s 前平的仓必须继续拦住。

    硬编码 60s 会放行 → 客户"更强抑制平亏秒开"的意图对跟号完全失效。
    """
    ex, mt5 = _build(monkeypatch, strat_vals={"churn_cooldown_seconds": 300.0})
    _just_closed(100)

    res = ex.copy_order(_signal())

    assert mt5.place_order.call_count == 0, (
        "客户设 churn=300s、100s 前刚平仓，跟号仍然下了单 —— 配置对跟号失效"
    )
    assert any("churn" in e or "抑制" in e for e in res["errors"]), \
        f"应给出 churn 抑制原因，实际 errors={res['errors']}"


@pytest.mark.parametrize("ago,should_place", [(45, False), (90, True)])
def test_follower_churn_default_behavior_unchanged(monkeypatch, ago, should_place):
    """等价性护栏：客户没配置时，行为必须与原来的 60s 完全一致。
    （45s 前 → 仍拦；90s 前 → 仍放行）"""
    ex, mt5 = _build(monkeypatch, strat_vals={})
    _just_closed(ago)

    ex.copy_order(_signal())

    assert mt5.place_order.call_count == (1 if should_place else 0), (
        f"未配置时 {ago}s 前平仓的行为变了（应与原 60s 硬编码等价）"
    )


def test_follower_churn_is_per_account(monkeypatch):
    """多账号铁律：churn 状态与配置都按账号隔离，别的账号刚平仓不该影响本跟号。"""
    ex, mt5 = _build(monkeypatch, strat_vals={"churn_cooldown_seconds": 300.0})
    te._LAST_CLOSE_TS["某个别的账号:BUY"] = te.time.time()   # 别人刚平仓

    ex.copy_order(_signal())

    assert mt5.place_order.call_count == 1, "别的账号平仓不该抑制本跟号"


# ══════════════════════════════════════════════════════════════════
# 缺陷 2：成交后去重标记必须原子落地
# ══════════════════════════════════════════════════════════════════

def test_dedupe_mark_survives_bookkeeping_failure(monkeypatch):
    """成交之后记账环节抛异常，去重标记也必须已经落地。

    否则下一轮同一个主号票号会被再跟一次：主号 1 单、跟号 2 单，真金白银。
    """
    ex, mt5 = _build(monkeypatch, trade_raises=True)

    ex.copy_order(_signal())

    assert mt5.place_order.call_count == 1, "第一次跟单应当已经成交"
    assert te._is_copied(FOLLOWER, LEADER_TICKET), (
        "成交后记账炸了，去重标记没落地 —— 下一轮会对同一张主号票重复跟单"
    )


def test_no_duplicate_copy_on_second_cycle_after_failure(monkeypatch):
    """端到端复现：记账炸掉之后的下一轮，绝不能对同一张主号票再下一单。"""
    ex1, mt5_1 = _build(monkeypatch, trade_raises=True)
    ex1.copy_order(_signal())
    assert mt5_1.place_order.call_count == 1

    # 下一轮：新执行器实例，但 _LAST_COPIED_SIGNAL 是模块级的，必须保住
    saved = dict(te._LAST_COPIED_SIGNAL)
    ex2, mt5_2 = _build(monkeypatch)          # 该构造会清空全局
    te._LAST_COPIED_SIGNAL.update(saved)      # 还原上一轮留下的去重标记

    res = ex2.copy_order(_signal())

    assert mt5_2.place_order.call_count == 0, (
        "同一张主号票被跟了第二次 —— 主号 1 单变成跟号 2 单"
    )
    assert any("重复" in e for e in res["errors"]), \
        f"应报重复复制，实际 errors={res['errors']}"


def test_successful_copy_still_marks_and_places(monkeypatch):
    """正常路径护栏：不能为了原子性把正常跟单改坏。"""
    ex, mt5 = _build(monkeypatch)

    res = ex.copy_order(_signal())

    assert mt5.place_order.call_count == 1, f"正常跟单未成交：{res['errors']}"
    assert res["order"] is not None, "正常跟单应返回订单详情"
    assert res["order"]["copied_from"] == LEADER_TICKET
    assert te._is_copied(FOLLOWER, LEADER_TICKET), "正常跟单后应标记已复制"
