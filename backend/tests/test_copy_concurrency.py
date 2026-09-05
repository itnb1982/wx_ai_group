"""
跟号跟单：跨线程并发下的"只跟一次"承诺（Phase 2 / SignalBus）

═══ 为什么这条必须成立 ═══
copy_order 有**两条并发调用路径**（已在 routers/trading.py 证实）：
  ① 主自动循环   trading.py:277   `f_exec.copy_order(leader_signal)`
  ② 副号实时跟单守护线程（10s）  trading.py:439  `fexec.copy_order(fresh_sig)`
     —— main.py:138 起的 daemon 线程，与主循环**真并发**。
再叠加 trading.py:238 的独立账号 ThreadPoolExecutor，进程内同时有 4 类线程在跑。

_LAST_COPIED_SIGNAL 这张表的设计意图，代码注释写得明明白白：
    "主周期 copy_order + 守护线程补单 两条路径可能并发/重复调用，
     靠 comment 字段做去重不可靠…故在进程级内存做硬去重。"
**设计意图是"硬去重"，但实现没做到原子。**

═══ 缺陷：check-then-act 跨线程 TOCTOU ═══
    _is_copied(fid, ticket)        ← 加锁读，读完立刻放锁
    ... place_order(...)           ← 不可逆副作用，耗时数百毫秒
    _mark_copied(fid, ticket)      ← 再次加锁写
两次加锁之间有一个**几百毫秒的裸奔窗口**。两条线程都能在这个窗口里
读到"没跟过" → 双双下单 ⇒ 主号 1 单，跟号 2 单。

后果是真金白银：跟号双倍敞口、双倍手数、双倍风险，
且第二笔在本地账本里通常没有对应主号票号，后续镜像平仓也跟不上，
主号平完之后跟号还留着一条裸奔的反向风险。

Phase 1 修的是"记账炸掉导致标记丢失"（单线程时序问题），
**没有**修跨线程竞态——那次的 _mark_copied 上移只是把裸奔窗口
从"成交+记账"缩短到"成交"，窗口依然存在。本轮收口。

═══ 测试怎么保证确定性（不靠 sleep 撞运气）═══
把 place_order 变成一道"闸门"：
  · 第一个进来的线程 set() 一个 Event，然后在里面停留 250ms（模拟下单在途）；
  · 第二个线程**等到 Event 被 set 之后才出发**——即精确复刻
    "A 已经在下单途中、还没来得及标记"这一瞬间。
现实现：B 查到"没跟过" → 也下单 → place_order 被调 2 次（红）。
修复后：A 在下单**之前**就原子占坑 → B 抢不到 → 只调 1 次（绿）。
不使用 Barrier：修复后只有一个线程进得去，Barrier 会把自己吊死。
"""
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


FOLLOWER = "acc_follower_race"
LEADER_TICKET = 777001


def _signal(ticket=LEADER_TICKET, direction="BUY"):
    return {
        "direction": direction,
        "symbol": "XAUUSD",
        "entry": 2000.0,
        "sl": 1990.0,
        "tp": 2020.0,
        "confidence": 0.8,
        "ticket": ticket,
        "comment": "WXAI|BUY|C80%",
    }


class _OrderGate:
    """把 place_order 撑成一个可观测、可控时长的窗口。

    线程安全地记账（MagicMock 的调用记录并不保证线程安全，这里自己加锁）。
    """

    def __init__(self, hold: float = 0.25):
        self.hold = hold
        self.entered = threading.Event()   # 第一个线程进入下单窗口
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
            n = len(self.calls)
        self.entered.set()
        time.sleep(self.hold)              # 下单在途
        return {"ticket": 90000 + n, "price": 2000.3, "volume": 0.1}

    @property
    def count(self):
        with self._lock:
            return len(self.calls)


def _reset_globals():
    te._LAST_CLOSE_TS.clear()
    te._LAST_COPIED_SIGNAL.clear()
    te._LAST_OPEN_TS.clear()


def _build(monkeypatch, gate, *, order_fails=False):
    """构造一个跟号执行器。两条线程各建一个（现实中就是各自 new 一个）。"""
    mock_mt5 = MagicMock()
    mock_mt5.get_account_info.return_value = {"balance": 3000.0, "equity": 3000.0}
    if order_fails:
        mock_mt5.place_order.side_effect = lambda **kw: {"error": "无报价"}
    else:
        mock_mt5.place_order.side_effect = gate
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    monkeypatch.setattr(te, "_positions_checked", lambda *a, **k: (True, []))
    monkeypatch.setattr(te.emergency, "allow_open", lambda *a, **k: (True, ""))

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = types.SimpleNamespace(
        id=FOLLOWER, is_trading_enabled=True)

    engine = MagicMock()
    engine.market._get_current_price.return_value = {"ask": 2000.5, "bid": 2000.0}

    ex = TradeExecutor(account_id=FOLLOWER, strategy=types.SimpleNamespace(),
                       user_id="u_test", db=db, engine=engine)
    ex._fresh_strat = lambda field, default=None: default
    ex._check_loss_cooldown = lambda: ""
    ex._calc_position_size = lambda *a, **k: {"lots": 0.1}
    ex._cap_to_risk_limit = lambda *a, **k: (0.1, "")
    ex.risk_engine = MagicMock()
    ex.risk_engine.check_trade_allowed.return_value = types.SimpleNamespace(
        passed=True, reject_reasons=[])
    ex._safe_db_write = lambda fn, label="": None
    ex._push_feed = lambda *a, **k: None
    return ex


def _race(monkeypatch, gate, signal_a, signal_b, *, order_fails=False):
    """A 先出发；B 等到 A 已经进入下单窗口再出发。返回两者的 result。"""
    ex_a = _build(monkeypatch, gate, order_fails=order_fails)
    ex_b = _build(monkeypatch, gate, order_fails=order_fails)
    out = {}

    def run_a():
        out["a"] = ex_a.copy_order(signal_a)

    def run_b():
        # 精确等到"A 已在下单途中、尚未标记"的那一瞬间
        assert gate.entered.wait(3.0) or order_fails, "A 从未进入下单窗口，用例前提不成立"
        out["b"] = ex_b.copy_order(signal_b)

    ta = threading.Thread(target=run_a)
    tb = threading.Thread(target=run_b)
    ta.start()
    tb.start()
    ta.join(10)
    tb.join(10)
    assert not ta.is_alive() and not tb.is_alive(), "线程未在预期时间内结束"
    return out


# ════════════════════════ 缺陷用例 ════════════════════════

def test_concurrent_copy_places_only_one_order(monkeypatch):
    """★ 核心：主循环与守护线程同时跟同一张主号单，MT5 只能收到一次下单。

    这是整套跟单系统最基本的承诺：主号 1 单 → 跟号 1 单。
    """
    _reset_globals()
    gate = _OrderGate(hold=0.25)
    _race(monkeypatch, gate, _signal(), _signal())

    assert gate.count == 1, (
        f"同一张主号单#{LEADER_TICKET} 被并发跟了 {gate.count} 次 —— "
        f"跟号双倍敞口。去重表的 check 与 mark 之间存在跨线程裸奔窗口。"
    )


def test_second_thread_reports_duplicate_not_success(monkeypatch):
    """抢输的那条线程必须明确回报"已跟过"，不能假装自己成功下了单。

    否则上层 all_orders 会多计一笔不存在的订单，前端与统计跟着失真。
    """
    _reset_globals()
    gate = _OrderGate(hold=0.25)
    out = _race(monkeypatch, gate, _signal(), _signal())

    winners = [r for r in out.values() if r.get("order")]
    assert len(winners) == 1, (
        f"两条线程都声称下单成功({len(winners)}个 order)，"
        f"但 MT5 实际只应成交 1 笔"
    )


def test_claim_is_taken_before_irreversible_order(monkeypatch):
    """占坑必须发生在 place_order **之前**。

    直接观测：当 A 还停在下单窗口里时，去重表里就应该已经有这张票号了。
    这是"先占坑再动手"与"先动手再占坑"的分水岭——后者无论把标记
    挪得多靠近成交点，都堵不住并发。
    """
    _reset_globals()
    gate = _OrderGate(hold=0.4)
    ex = _build(monkeypatch, gate)
    t = threading.Thread(target=lambda: ex.copy_order(_signal()))
    t.start()
    try:
        assert gate.entered.wait(3.0), "未进入下单窗口"
        time.sleep(0.05)  # 让出 GIL，确保观测点稳定落在窗口内
        assert te._is_copied(FOLLOWER, LEADER_TICKET), (
            "下单已在途中，去重表却还是空的 —— 此刻任何并发线程都会重复跟单"
        )
    finally:
        t.join(10)


# ════════════════════════ 反向护栏 ════════════════════════
# 收编去重表绝不能带来"漏跟"。漏跟违反「多交易多赚钱」，比重复跟更隐蔽。

def test_failed_order_releases_claim_for_retry(monkeypatch):
    """★ 护栏：下单失败必须归还坑位，否则这张主号单 300s 内永远跟不上。

    引入"先占坑"之后最容易踩的坑：占了坑但单没下成（无报价/被拒），
    若不归还，TTL 内守护线程的补单兜底会一直被自己挡在门外 ⇒ 漏跟。
    """
    _reset_globals()
    gate = _OrderGate(hold=0.0)
    ex_fail = _build(monkeypatch, gate, order_fails=True)
    r1 = ex_fail.copy_order(_signal())
    assert not r1.get("order"), "用例前提：这一单应当下失败"

    assert not te._is_copied(FOLLOWER, LEADER_TICKET), (
        "下单失败却仍占着去重坑位 —— 补单兜底会被永久挡住，主号这一单跟不上了"
    )

    # 补单兜底应当能真正把它跟上
    ex_ok = _build(monkeypatch, gate)
    r2 = ex_ok.copy_order(_signal())
    assert r2.get("order"), f"归还坑位后重试仍未成交: {r2.get('errors')}"
    assert gate.count == 1


def test_different_leader_tickets_never_block_each_other(monkeypatch):
    """护栏（多账号/多信号铁律）：不同主号票号各走各的，互不占坑。"""
    _reset_globals()
    gate = _OrderGate(hold=0.25)
    _race(monkeypatch, gate, _signal(ticket=777001), _signal(ticket=777002))

    assert gate.count == 2, (
        f"两张不同的主号单只跟成 {gate.count} 笔 —— 去重键把不同信号混为一谈了"
    )


def test_sequential_duplicate_still_blocked(monkeypatch):
    """护栏：并发之外，原有的串行去重语义不能退化。"""
    _reset_globals()
    gate = _OrderGate(hold=0.0)
    ex = _build(monkeypatch, gate)
    ex.copy_order(_signal())
    ex.copy_order(_signal())
    assert gate.count == 1, "同一张主号单被串行跟了两次"


def test_claim_expires_after_ttl(monkeypatch):
    """护栏：TTL 过期后必须可以重新跟单（占坑不是永久墓碑）。"""
    _reset_globals()
    gate = _OrderGate(hold=0.0)
    ex = _build(monkeypatch, gate)
    ex.copy_order(_signal())
    assert gate.count == 1

    # 把标记时间推回到 TTL 之外（SignalBus 条目形状为 (写入时刻, 负载)）
    with te._LAST_COPIED_LOCK:
        for k in list(te._LAST_COPIED_SIGNAL):
            te._LAST_COPIED_SIGNAL[k] = (time.time() - te._COPIED_TTL_SECONDS - 1, None)

    ex2 = _build(monkeypatch, gate)
    ex2.copy_order(_signal())
    assert gate.count == 2, "TTL 已过期，却仍被旧坑位挡住"
