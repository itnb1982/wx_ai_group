"""
跟号镜像主号出场：跨线程并发下的"只镜像一次"承诺（Phase 2 / SignalBus）

═══ 为什么这条比跟单竞态更伤钱 ═══
`_mirror_leader_exits` 有**两条并发调用路径**：
  ① 主自动循环          trade_executor.py:1900  `self._mirror_leader_exits(positions, ai_decision)`
  ② 副号实时跟单守护线程  routers/trading.py:428  `fexec._mirror_leader_exits(positions, None)`
     —— main.py 起的 10s daemon 线程，与主循环**真并发**。

而 `consume_leader_exit()` 名字叫"消费"，实现却是**非破坏性读取**：
    acts = [a for a in ent["actions"] if now - a.get("ts", 0) <= _BUS_TTL]
    return list(acts) if acts else None        ← 只读，不弹出
所以两条线程会**各自拿到同一批动作**，谁也拦不住谁。

紧接着又是一次典型的 check-then-act：
    if _is_mirrored(fid, lt, a): continue      ← 加锁读，读完放锁
    ... close_position(...)                    ← 不可逆副作用
    _mark_mirrored(fid, lt, a)                 ← 再次加锁写

═══ 真实损失：partial_close 被执行两次 = 整仓被平光 ═══
主号说"平 50%，剩下的让利润奔跑"。两条线程各平 50%：
  1.00 手 → A 平 0.50 → B 又平 0.50 → **持仓归零**。
本该留在场上继续赚钱的那条腿被平掉了 —— 这是直接的利润损失，
且违反「多交易多赚钱」铁律（主号还在持仓，跟号已经空仓，收益彻底脱钩）。

full_close 的双重执行相对温和：第二次会拿到"持仓不存在"，
现有代码把它当作已平处理。但 partial_close 没有这层保护
——它平的是"手数"，重复平仓在 MT5 侧是完全合法的两笔独立成交。

═══ 测试怎么保证确定性（不靠 sleep 撞运气）═══
把 close_position 变成一道"闸门"：
  · 第一个进来的线程 set() 一个 Event，然后在里面停留 250ms（模拟平仓在途）；
  · 第二个线程**等到 Event 被 set 之后才出发** —— 精确复刻
    "A 已经在平仓途中、还没来得及标记幂等"这一瞬间。
不使用 Barrier：修复后只有一个线程进得去，Barrier 会把自己吊死。
"""
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


FOLLOWER = "acc_mirror_race"
LEADER_ID = "leader_mirror_race"
LEADER_TICKET = "9901"
POS_TICKET = 8801


def _clear_state():
    """清干净所有相关全局，避免跨用例污染造成假绿。"""
    te.clear_leader_exit_bus()
    with te._MIRRORED_LOCK:
        te._MIRRORED.clear()
    with te._MIRROR_FAIL_LOCK:
        te._MIRROR_FAIL.clear()


@pytest.fixture(autouse=True)
def _isolate():
    _clear_state()
    yield
    _clear_state()


def _position(volume=1.00, sl=0.0):
    return {
        "ticket": POS_TICKET, "type": "buy", "volume": volume,
        "price_open": 2000.0, "sl": sl, "tp": 0.0, "profit": 50.0,
        "symbol": "XAUUSD",
        "comment": f"WXAI-L{LEADER_TICKET}",   # 镜像靠 comment 里的 L{主号票号} 对上号
    }


def _build(monkeypatch):
    mock_mt5 = MagicMock()
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    # ★ 2026-08-17 修复：测试未 mock _positions_checked → 真实查主号返回空
    #   → 「对账兜底」(2026-08-15 新增)把跟号 L 单当孤儿先平一次(reconcile_close)，
    #   之后 partial_close 又平一次 → close_position 2 次（"正常镜像被误伤"）。
    #   模拟主号 partial_close 后仍持有该仓：兜底不触发，只走广播动作。
    monkeypatch.setattr(
        te, "_positions_checked",
        lambda account_id, symbol="XAUUSD": (
            True,
            [{"ticket": LEADER_TICKET, "type": "buy", "volume": 1.0,
              "price_open": 2000.0, "sl": 1990.0, "tp": 0.0, "profit": 0.0,
              "symbol": "XAUUSD", "comment": ""}],
        ),
    )

    engine = MagicMock()
    ex = TradeExecutor(
        account_id=FOLLOWER,
        strategy=types.SimpleNamespace(),
        user_id="user_mirror_race",
        db=MagicMock(),
        engine=engine,
    )
    ex._fresh_strat = lambda field, default=None: default
    ex._is_leader = False
    ex._leader_account = lambda: types.SimpleNamespace(id=LEADER_ID)
    ex._auto_exit_blocked = lambda *_a, **_k: False
    # 记账与展示都不是本用例的靶子，桩掉以免噪声掩盖真正的断言
    ex._record_close = lambda *a, **k: None
    ex._push_feed = lambda *a, **k: None
    ex.exit_agent = MagicMock()
    return ex, mock_mt5


def _gate_close_position(mock_mt5, hold_seconds=0.25):
    """把 close_position 变成闸门：第一个线程进来后停留，暴露"平仓在途"窗口。"""
    entered = threading.Event()
    calls = []
    lk = threading.Lock()

    def _slow_close(account_id, ticket, volume=None, *a, **k):
        with lk:
            calls.append({"account_id": account_id, "ticket": ticket, "volume": volume})
            first = len(calls) == 1
        if first:
            entered.set()
            time.sleep(hold_seconds)
        return {"ok": True, "ticket": ticket, "volume": volume}

    mock_mt5.close_position.side_effect = _slow_close
    return entered, calls


def _race(ex, positions, entered, hold=0.25):
    """A 先跑；B 在 A 进入 close_position 之后才出发。"""
    errs = []

    def _run(tag):
        try:
            ex._mirror_leader_exits(positions, None)
        except Exception as e:      # pragma: no cover - 出错要看得见
            errs.append((tag, repr(e)))

    ta = threading.Thread(target=_run, args=("A",))
    ta.start()
    assert entered.wait(timeout=5), "第一条线程没能进入 close_position，用例失去意义"
    tb = threading.Thread(target=_run, args=("B",))
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert not errs, f"镜像线程抛异常: {errs}"


# ══════════════════════════════════════════════════════════════
#  缺陷用例：并发下重复镜像
# ══════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_concurrent_partial_close_executes_only_once(monkeypatch):
    """★ 核心：主号要求平 50%，并发两条线程只能真平一次。

    炸掉这条 = 跟号 1.00 手被平成 0 手，本该奔跑的利润腿没了。
    """
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)
    entered, calls = _gate_close_position(mock_mt5)

    _race(ex, [_position(volume=1.00)], entered)

    assert len(calls) == 1, (
        f"partial_close 被并发执行 {len(calls)} 次 → 平仓手数翻倍："
        f"{[c['volume'] for c in calls]}（合计 {sum(c['volume'] or 0 for c in calls)} 手，"
        f"持仓只有 1.00 手）"
    )


@pytest.mark.integration
def test_concurrent_partial_close_does_not_wipe_position(monkeypatch):
    """业务后果级断言：累计平仓量不得超过主号要求的比例。"""
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)
    entered, calls = _gate_close_position(mock_mt5)

    _race(ex, [_position(volume=1.00)], entered)

    closed = sum(c["volume"] or 0 for c in calls)
    assert closed <= 0.5 + 1e-9, (
        f"累计平仓 {closed} 手 > 主号要求的 0.50 手 → 跟号持仓被清空，"
        f"与主号仓位脱钩"
    )


@pytest.mark.integration
def test_concurrent_full_close_executes_only_once(monkeypatch):
    """全平同样只能发一次指令（第二次即便被 MT5 拒绝，也是无谓的往返与日志噪声）。"""
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "full_close")
    entered, calls = _gate_close_position(mock_mt5)

    _race(ex, [_position()], entered)

    assert len(calls) == 1, f"full_close 被并发执行 {len(calls)} 次"


@pytest.mark.integration
def test_claim_happens_before_irreversible_close(monkeypatch):
    """占坑必须发生在 close_position **之前**。

    只断言"最终只平一次"是不够的——如果实现是"先平、再靠某种事后
    去重把第二笔吞掉"，钱已经花出去了。这条把顺序钉死。
    """
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)

    seen = {}

    def _probe_close(account_id, ticket, volume=None, *a, **k):
        # 进入不可逆点的这一刻，幂等表里必须已经有坑了
        seen["mirrored_at_close"] = te._is_mirrored(FOLLOWER, LEADER_TICKET, "partial_close")
        return {"ok": True, "ticket": ticket, "volume": volume}

    mock_mt5.close_position.side_effect = _probe_close
    ex._mirror_leader_exits([_position()], None)

    assert seen.get("mirrored_at_close") is True, (
        "进入 close_position 时幂等坑位尚未占下 → 并发线程仍可长驱直入"
    )


# ══════════════════════════════════════════════════════════════
#  反向护栏：证明修复没有把正常镜像给堵死
# ══════════════════════════════════════════════════════════════

@pytest.mark.integration
def test_single_thread_partial_close_still_works(monkeypatch):
    """单线程正常路径必须照常镜像——修复不能变成"谁也别想平"。"""
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)
    mock_mt5.close_position.return_value = {"ok": True, "ticket": POS_TICKET}

    ex._mirror_leader_exits([_position(volume=1.00)], None)

    assert mock_mt5.close_position.call_count == 1, "正常镜像被误伤"
    assert mock_mt5.close_position.call_args[0][1] == POS_TICKET


@pytest.mark.integration
def test_failed_close_can_retry_next_round(monkeypatch):
    """平仓失败后必须能在下一轮重试。

    这是与跟单侧同源的"占坑必须归还"要求：镜像失败还占着幂等坑，
    主号已平、跟号永远平不掉 ⇒ 裸奔的反向敞口，比重复平仓危险得多。
    """
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)

    mock_mt5.close_position.return_value = {"error": "context busy"}
    ex._mirror_leader_exits([_position()], None)
    assert mock_mt5.close_position.call_count == 1

    # 下一轮：MT5 恢复正常，必须能真的平掉
    mock_mt5.close_position.return_value = {"ok": True, "ticket": POS_TICKET}
    ex._mirror_leader_exits([_position()], None)
    assert mock_mt5.close_position.call_count == 2, (
        "上一轮平仓失败后占坑没归还 → 这笔镜像永久丢失（跟号裸奔）"
    )


@pytest.mark.integration
def test_different_actions_are_independent(monkeypatch):
    """幂等粒度是(跟号,主号票号,动作类型)：move_sl 不该被 partial_close 挡住。"""
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "move_sl", new_sl=1995.0,
                           leader_open_price=2000.0)  # ★ 生产发布总是带 leader_open_price（相对偏移基准）
    mock_mt5.close_position.return_value = {"ok": True, "ticket": POS_TICKET}
    mock_mt5.modify_sl_tp.return_value = {"ok": True}

    ex._mirror_leader_exits([_position(sl=1990.0)], None)  # ★ move_sl 需持仓已有 SL 才能算相对偏移

    assert mock_mt5.close_position.call_count == 1, "分批平未执行"
    assert mock_mt5.modify_sl_tp.call_count == 1, "move_sl 被同票号的其它动作误挡"


@pytest.mark.integration
def test_second_round_does_not_repeat_completed_action(monkeypatch):
    """已成功镜像的动作，下一轮不得重复执行（幂等的本职）。"""
    ex, mock_mt5 = _build(monkeypatch)
    te.publish_leader_exit(LEADER_ID, LEADER_TICKET, "partial_close", close_pct=0.5)
    mock_mt5.close_position.return_value = {"ok": True, "ticket": POS_TICKET}

    ex._mirror_leader_exits([_position()], None)
    ex._mirror_leader_exits([_position()], None)

    assert mock_mt5.close_position.call_count == 1, "同一动作被重复镜像"
