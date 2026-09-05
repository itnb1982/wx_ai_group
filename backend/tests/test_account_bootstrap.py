"""
启动期 MT5 账号接入：瞬时 DB 抖动不得让全体客户静默失联（Phase 2 / 启动韧性）

═══ 这不是假想缺陷，是 2026-08-08 00:09 刚刚发生的生产事故 ═══
重启后端后，日志只留下一行 WARNING 就把 4 个客户账号全丢了：

    [启动] MT5 自动重连流程异常: 写引擎连接创建失败：DB 持续只读/锁定/无法打开
           | 最后真因: OperationalError: attempt to write a readonly database

事后独立进程探测同一个库：**0.00s 可写**，三种 URI 写法全部成功。
说明 DB 本身没坏，只是重启瞬间撞上了一次**瞬时锁**
（旧进程被 kill 留下 wx_prod.dat-journal + Defender 扫描 34MB 库文件）。
换句话说：一次几十秒的、必然会自愈的抖动，换来了**永久性的全员停摆**。

═══ 旧实现错在哪：all-or-nothing + 永不重试 ═══
main.py lifespan 里的原始结构（本测试用 `_legacy_bootstrap` 逐行复刻）：

    try:
        _db = SessionLocal()                    ← ★ 单点：这一句抖一下
        _accounts = _db.query(MT5Account).all()
        for _acc in _accounts:                  ← 整个循环一次都进不去
            try:  mt5_service.add_account(...)  ← per-account 保护形同虚设
            except: ...
    except Exception as _e:
        logger.warning(...)                     ← 吞掉，然后什么都不做

三重放大，缺一不可：
  ① **单点**：取列表失败 = 全员失败，per-account 的 try/except 根本没机会执行；
  ② **不重试**：`_raw_creator` 内部 6 次退避（31.5s）耗尽即放弃，
     lifespan 层再无第二次机会 —— 而这类锁往往 1 分钟后自己就好了；
  ③ **无自愈**：失败后没有任何后台补偿，进程能一直"健康"运行到天荒地老，
     却一个账号都没接上，除非人工重启。

═══ 为什么后果是赔付级：系统同时在三个地方撒谎 ═══
事故现场实测（pid 27444, uptime 398s）：
  · /api/health          → {"status": "ok", "mt5_connected": 0}   ← 监控绿灯
  · DB mt5_accounts 表   → 4 行全是 is_connected=1, status='ONLINE' ← 前端 4 个绿灯
    （上一次会话留下的**陈旧**状态，没人把它改回来）
  · 实际                  → 进程内 0 个 worker，一单也下不出去

多租户 SaaS 下这 4 行是 4 个**独立客户**。运维看监控一切正常，
客户看界面显示在线，实际全天不交易 —— 这是直接的赔付纠纷，
也彻底违背「多交易多赚钱」铁律（交易笔数归零是最极端的"腰斩"）。

═══ 本测试锁死的四条承诺 ═══
  1. 瞬时抖动必须重试扛过去，不能让全员陪葬；
  2. 真失败必须留下自愈通道（后台重连），不能静默放弃；
  3. 单个账号连不上，不许拖累其他客户；
  4. 没连上的账号必须在 DB 里标成非 ONLINE —— 宁可显示红灯，不许假装绿灯。

护栏组则保证修复本身不带来新麻烦：全成功要正确落 ONLINE、
零账号不算故障、守护线程连满即退出不空转、bootstrap 永不向 lifespan 抛异常
（一旦抛出去，整个后端服务会起不来，比不连账号严重得多）。
"""
import threading
import time

import pytest

from app.services import account_bootstrap as ab

pytestmark = pytest.mark.unit


# ─────────────────────────── 测试替身 ───────────────────────────
class FakeAccount:
    """复刻 MT5Account ORM 行里 bootstrap 真正用到的字段。"""

    def __init__(self, aid, name, login="100", server="S", terminal_path=""):
        self.id = aid
        self.name = name
        self.account_id = login
        self.password = "enc:" + name
        self.server = server
        self.terminal_path = terminal_path
        # 陈旧的"上次会话遗留"状态：事故现场就是这两个值在骗前端
        self.is_connected = True
        self.status = "ONLINE"
        self.status_message = None


class FakeSession:
    """按脚本决定第几次 query 抛错，用来模拟 DB 瞬时锁。"""

    def __init__(self, accounts, fail_times=0, exc=None):
        self._accounts = accounts
        self._fail_times = fail_times
        self._exc = exc or RuntimeError(
            "写引擎连接创建失败：DB 持续只读/锁定/无法打开 | 最后真因: "
            "OperationalError: attempt to write a readonly database"
        )
        self.calls = 0
        self.committed = 0
        self.closed = False

    def query(self, _model):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self

    def all(self):
        return list(self._accounts)

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._accounts[0] if self._accounts else None

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


class SessionFactory:
    """每次调用返回一个新 FakeSession；前 fail_times 次整体失败。"""

    def __init__(self, accounts, fail_times=0):
        self.accounts = accounts
        self.fail_times = fail_times
        self.created = 0
        self.sessions = []

    def __call__(self):
        self.created += 1
        s = FakeSession(
            self.accounts,
            fail_times=1 if self.created <= self.fail_times else 0,
        )
        self.sessions.append(s)
        return s


def _decrypt(v):
    return (v or "").replace("enc:", "")


def _mk(n=4):
    return [FakeAccount(f"id{i}", f"liumanchun{i}") for i in range(1, n + 1)]


def _no_sleep(_s):
    """把退避睡眠短路掉，测试跑得快且确定。"""
    return None


# ═══════════════════ 对照组：旧 main.py 逻辑的铁证 ═══════════════════
def _legacy_bootstrap(session_factory, connector):
    """逐行复刻事故当天 main.py:71-92 的结构，仅用于留证。"""
    connected = []
    try:
        _db = session_factory()
        _accounts = _db.query(None).all()
        _db.close()
        for _acc in _accounts:
            try:
                if connector(account_id=_acc.id):
                    connected.append(_acc.id)
            except Exception:
                pass
    except Exception:
        pass  # ← 事故现场：只打一行 warning，然后什么都不做
    return connected


def test_legacy_loses_every_account_on_one_transient_hiccup():
    """铁证：旧逻辑下,一次瞬时抖动 = 4 个客户全部失联(而且永不重试)。"""
    accounts = _mk(4)
    factory = SessionFactory(accounts, fail_times=1)
    got = _legacy_bootstrap(factory, lambda account_id, **_k: True)

    assert got == [], "对照组前提失效：旧逻辑本应一个都连不上"
    assert factory.created == 1, "旧逻辑只试一次就永久放弃 —— 这正是事故根因"


# ═══════════════════ 缺陷组：新实现必须做到的事 ═══════════════════
def test_transient_db_lock_must_not_lose_all_accounts():
    """缺陷1：DB 抖一次就该重试扛过去,4 个客户一个都不能丢。"""
    accounts = _mk(4)
    factory = SessionFactory(accounts, fail_times=1)
    calls = []

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda **kw: calls.append(kw["account_id"]) or True,
        sleeper=_no_sleep,
    )

    assert res.total == 4
    assert len(res.connected) == 4, f"瞬时锁不该丢账号，实际只连上 {res.connected}"
    assert res.failed == []
    assert factory.created >= 2, "必须真的重试过取列表，而不是碰运气"


def test_permanent_db_failure_leaves_a_self_heal_path():
    """缺陷2：真失败也不许静默放弃,必须留下后台自愈通道。"""
    accounts = _mk(4)
    factory = SessionFactory(accounts, fail_times=99)  # 怎么试都失败

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda **_kw: True,
        sleeper=_no_sleep,
    )

    assert res.load_error is not None, "取列表失败必须如实记录，不能假装成功"
    assert res.needs_retry is True, "永久失败时必须要求后台重试，否则永远停摆"
    assert factory.created > 1, "至少要重试过"


def test_one_bad_account_must_not_drag_down_other_customers():
    """缺陷3：多租户下,一个客户连不上不许拖累另外三个。"""
    accounts = _mk(4)
    factory = SessionFactory(accounts)

    def connector(account_id, **_kw):
        if account_id == "id2":
            raise RuntimeError("terminal 僵死")
        return True

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=connector,
        sleeper=_no_sleep,
    )

    assert sorted(res.connected) == ["id1", "id3", "id4"]
    assert res.failed == ["id2"]
    assert res.needs_retry is True, "还有账号没连上，就必须继续自愈"


def test_unconnected_account_must_be_marked_offline_in_db():
    """缺陷4：连不上的账号必须落成非 ONLINE,禁止让前端显示假绿灯。"""
    accounts = _mk(2)
    factory = SessionFactory(accounts)

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda account_id, **_kw: account_id != "id2",
        sleeper=_no_sleep,
    )

    assert res.failed == ["id2"]
    bad = accounts[1]
    assert bad.is_connected is False, "没连上却仍写着 is_connected=True —— 就是这个在骗前端"
    assert str(bad.status).upper().endswith("ERROR") or str(bad.status).upper().endswith(
        "OFFLINE"
    ), f"状态必须改为 ERROR/OFFLINE，实际 {bad.status}"


# ═══════════════════ 护栏组：修复不得引入新问题 ═══════════════════
def test_all_success_marks_every_account_online():
    accounts = _mk(3)
    factory = SessionFactory(accounts)
    for a in accounts:  # 先弄脏，确认是被真正写对而非碰巧
        a.is_connected = False
        a.status = "ERROR"

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda **_kw: True,
        sleeper=_no_sleep,
    )

    assert len(res.connected) == 3 and res.failed == []
    assert res.needs_retry is False, "全连上了就不该再安排重试，避免线程空转"
    assert all(a.is_connected is True for a in accounts)
    assert all(str(a.status).upper().endswith("ONLINE") for a in accounts)


def test_zero_configured_accounts_is_not_a_failure():
    """全新部署没有任何账号,属正常状态,不许当故障、更不许起守护线程。"""
    factory = SessionFactory([])
    res = ab.bootstrap(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda **_kw: True,
        sleeper=_no_sleep,
    )
    assert res.total == 0 and res.connected == [] and res.failed == []
    assert res.needs_retry is False
    assert res.load_error is None


def test_bootstrap_never_raises_into_lifespan():
    """铁律：bootstrap 抛异常会让整个后端起不来,比不连账号严重得多。"""
    def exploding_factory():
        raise KeyboardInterrupt("模拟最恶劣的意外")

    res = ab.bootstrap(
        session_factory=exploding_factory,
        decryptor=_decrypt,
        connector=lambda **_kw: True,
        sleeper=_no_sleep,
    )
    assert res.load_error is not None
    assert res.needs_retry is True


def test_decrypt_failure_is_isolated_to_that_account():
    """密文损坏只该影响该客户,不该让其他客户陪葬。"""
    accounts = _mk(3)
    factory = SessionFactory(accounts)

    def bad_decrypt(v):
        if "liumanchun2" in (v or ""):
            raise ValueError("密文损坏")
        return _decrypt(v)

    res = ab.bootstrap(
        session_factory=factory,
        decryptor=bad_decrypt,
        connector=lambda **_kw: True,
        sleeper=_no_sleep,
    )
    assert sorted(res.connected) == ["id1", "id3"]
    assert res.failed == ["id2"]


# ═══════════════════ 自愈守护线程 ═══════════════════
def test_reconnect_daemon_retries_until_connected_then_exits():
    """守护线程要能把失联账号救回来,救回来之后必须自己退出、不空转。"""
    accounts = _mk(2)
    factory = SessionFactory(accounts)
    attempts = {"n": 0}
    done = threading.Event()

    def flaky(account_id, **_kw):
        # 前两轮全失败，第三轮起全成功 —— 模拟 Defender 扫完、锁释放
        if attempts["n"] < 4:
            attempts["n"] += 1
            return False
        return True

    t = ab.start_reconnect_daemon(
        session_factory=factory,
        decryptor=_decrypt,
        connector=flaky,
        interval=0.01,
        max_interval=0.02,
        on_settled=lambda _r: done.set(),
    )
    assert t is not None
    assert done.wait(timeout=5.0), "守护线程没能在合理时间内把账号救回来"
    t.join(timeout=3.0)
    assert not t.is_alive(), "全部连上后守护线程必须退出，否则永久空转"


def test_reconnect_daemon_not_started_when_nothing_to_fix():
    """没有失联账号时不该起线程 —— 白起一个线程就是资源泄漏。"""
    factory = SessionFactory([])
    t = ab.start_reconnect_daemon(
        session_factory=factory,
        decryptor=_decrypt,
        connector=lambda **_kw: True,
        interval=0.01,
        only_if_needed=True,
    )
    assert t is None


def test_daemon_survives_exceptions_and_keeps_trying():
    """守护线程内部异常不得让它自己死掉,否则自愈通道就断了。"""
    accounts = _mk(1)
    factory = SessionFactory(accounts)
    seen = {"n": 0}
    done = threading.Event()

    def explode_then_ok(account_id, **_kw):
        seen["n"] += 1
        if seen["n"] < 3:
            raise RuntimeError("MT5 终端还没起来")
        return True

    t = ab.start_reconnect_daemon(
        session_factory=factory,
        decryptor=_decrypt,
        connector=explode_then_ok,
        interval=0.01,
        max_interval=0.02,
        on_settled=lambda _r: done.set(),
    )
    assert done.wait(timeout=5.0), "遇到异常后守护线程死了，自愈通道断裂"
    t.join(timeout=3.0)
    assert seen["n"] >= 3
