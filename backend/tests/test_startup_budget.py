"""启动预算与 DB 自愈 —— 回归测试。

═══════════════════════════════════════════════════════════════════════
事故实录（2026-08-08 00:36 生产）
═══════════════════════════════════════════════════════════════════════
后端陷入每 ~4.6 分钟一轮的重启死循环，4 个客户账号全天不交易。

实测时间线（data/wanxiang_backend_12688.log，两次重启分毫不差）：
    00:36:58.393  lifespan:69  banner 打完
    00:40:16.448  lifespan:73  数据库: sqlite:///...
                               ↑ 中间 198.055s，**一条日志都没有**

198s 的算术：
    _raw_creator 内部退避 0.5+1+2+4+8+16 = 31.5s
    init_db 每轮 = 31.5s + retry_interval 1.5s = 33s
    init_db max_retry=6  →  6 × 33 = 198s   ← 与实测 198.055s 吻合

为什么全程静默：
    database.py 用 logging.getLogger("db")，而全项目日志走 loguru，
    这条链路的 warning 被完全吞掉 —— 198 秒的疯狂重试在日志里不存在。

为什么变成死循环：
    198s(init_db) + 67s(account_bootstrap 同步重试) = 265s 总启动耗时
    > supervisor 判死预算 STARTUP_GRACE(240s) + MAX_FAILS(4)×INTERVAL(5s) = 260s
    → supervisor 强杀 → 新进程再花 265s → 再被杀 → 无限循环。
    强杀本身又留下 hot journal，让下一轮更容易撞 readonly：自我强化。

═══════════════════════════════════════════════════════════════════════
铁律（本文件守护的不变量）
═══════════════════════════════════════════════════════════════════════
1. 启动路径必须有**时间上界**，且必须远小于 supervisor 判死预算。
2. DB 抖动的自愈是**后台职责**，绝不允许阻塞启动。
3. 任何 DB 失败必须在项目统一日志（loguru）中留痕 —— 静默失败等于没有。
4. init_db 永不抛异常（抛了整个服务起不来）。
"""
import sqlite3
import threading
import time

import pytest

pytestmark = pytest.mark.unit

# supervisor 的判死预算（supervisor.py: STARTUP_GRACE 240 + 4×5）
SUPERVISOR_KILL_BUDGET = 260.0
# 启动路径必须留出的安全系数：实际预算不得超过判死预算的 1/4
STARTUP_BUDGET_CEILING = 60.0


class _FakeClock:
    """记录被"睡掉"的时间，不真睡 —— 测试必须快且确定。"""

    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)

    @property
    def total(self):
        return sum(self.slept)


class _BoomSession:
    """永远打不开的 session：模拟 DB 持续 readonly。"""

    def __init__(self, *a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")


class _CountingSessionFactory:
    """前 fail_times 次失败，之后成功 —— 模拟瞬时抖动后自愈。"""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return _OkSession()


class _NoOpMeta:
    """替身元数据：真实 Base.metadata.create_all 会拿假 bind 去连库，
    那测的就不是启动预算而是 SQLAlchemy 了。此处只验证调度逻辑。"""

    @staticmethod
    def create_all(bind=None):
        return None


class _OkSession:
    def __init__(self):
        self.bind = object()
        self.committed = False
        self.closed = False

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture()
def db_mod():
    from app import database
    return database


# ══════════════════════════════════════════════════════════════════
# 缺陷组：以下每一条都复刻真实事故，修复前必须全红
# ══════════════════════════════════════════════════════════════════

def test_raw_creator_backoff_must_be_bounded(db_mod, monkeypatch):
    """_raw_creator 单次退避总预算不得吃掉半分钟。

    事故值：0.5+1+2+4+8+16 = 31.5s。它被 init_db 再乘 6 倍 → 198s。
    瞬时锁通常几秒内释放，31.5s 的等待既救不了真故障，又把启动拖死。
    """
    clock = _FakeClock()

    def always_readonly(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(sqlite3, "connect", always_readonly)

    # 必须精确断言契约异常 RuntimeError：若写成宽泛的 Exception，
    # 「_raw_creator 不接受 sleeper 参数」抛出的 TypeError 也会被吞掉，
    # 测试假绿通过 —— 假绿比红更危险。
    with pytest.raises(RuntimeError):
        db_mod._raw_creator(sleeper=clock)

    assert clock.total <= 10.0, (
        f"_raw_creator 退避总预算 {clock.total}s 过大（事故值 31.5s）。"
        f"它会被 init_db 的重试次数放大，直接拖垮启动。"
    )


def test_init_db_must_not_block_startup_for_minutes(db_mod):
    """DB 持续不可写时，init_db 必须快速认输，而不是阻塞几分钟。

    事故值 198s，超过 supervisor 判死预算的 3/4，直接导致重启死循环。
    """
    clock = _FakeClock()

    ok = db_mod.init_db(session_factory=_BoomSession, sleeper=clock)

    assert ok is False, "DB 持续不可写时 init_db 应返回 False"
    assert clock.total <= STARTUP_BUDGET_CEILING, (
        f"init_db 阻塞预算 {clock.total}s 超过上限 {STARTUP_BUDGET_CEILING}s"
        f"（事故值 198s，supervisor 判死线 {SUPERVISOR_KILL_BUDGET}s）"
    )


def test_init_db_end_to_end_budget_through_real_creator(db_mod, monkeypatch):
    """★ 端到端预算：必须走真实的 WriteSession -> _raw_creator 链路。

    ─────────────────────────────────────────────────────────────
    这条用例是反向验证逼出来的。上面那条用 _BoomSession 直接抛异常，
    **根本没走 _raw_creator**，只覆盖到 init_db 自己的 retry_interval。
    于是把 init_db 的重试次数改回事故值 6 次时，它竟然照样通过 ——
    198s 里有 189s 完全没被守护，是彻头彻尾的虚假安全感。

    真实预算 = init_db 轮数 × (_raw_creator 退避总和 + retry_interval)
    只有从 sqlite3.connect 那一层注入失败，才能量到这个乘积。
    """
    rec = []
    monkeypatch.setattr(db_mod, "_SLEEPER", rec.append)

    def always_readonly(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(sqlite3, "connect", always_readonly)

    ok = db_mod.init_db()  # 完全走生产默认路径，不注入任何替身

    assert ok is False
    total = sum(rec)
    assert total <= STARTUP_BUDGET_CEILING, (
        f"启动期 init_db 端到端阻塞 {total}s 超过上限 {STARTUP_BUDGET_CEILING}s。"
        f"事故值 198s（= 6 轮 × (31.5s 退避 + 1.5s 间隔)），"
        f"越过 supervisor {SUPERVISOR_KILL_BUDGET}s 判死线即成重启死循环。"
    )


def test_db_failure_must_be_visible_in_project_logger(db_mod):
    """DB 失败必须在 loguru 留痕 —— 事故中 198 秒重试一条日志都没有。

    database.py 当时用 logging.getLogger("db")，而全项目走 loguru，
    告警进了黑洞。不可观测的失败 = 排障时只能靠猜。
    """
    from loguru import logger

    captured = []
    sink_id = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    try:
        db_mod.init_db(session_factory=_BoomSession, sleeper=_FakeClock())
    finally:
        logger.remove(sink_id)

    joined = "\n".join(captured)
    assert "init_db" in joined, (
        "init_db 失败未在 loguru 留痕（事故中 198s 静默重试，日志里查无此事）。"
        f"实际捕获: {joined[:300]!r}"
    )


def test_db_must_self_heal_in_background_after_fast_fail(db_mod):
    """启动期快速认输之后，必须有后台自愈把 DB 救回来。

    快速失败若没有后台补偿，就只是把"卡死几分钟"换成"永久瘫痪"——更糟。
    """
    factory = _CountingSessionFactory(fail_times=2)
    done = threading.Event()

    t = db_mod.start_db_selfheal_daemon(
        session_factory=factory,
        interval=0.01,
        max_interval=0.02,
        max_rounds=20,
        metadata=_NoOpMeta,
        on_ready=lambda: done.set(),
    )

    assert t is not None, "DB 未就绪时必须启动后台自愈守护"
    assert done.wait(timeout=5.0), "后台自愈守护未能把 DB 救回来"
    t.join(timeout=3.0)
    assert not t.is_alive(), "自愈成功后守护线程必须自行退出，不得常驻空转"


# ══════════════════════════════════════════════════════════════════
# 护栏组：修复不得引入新问题
# ══════════════════════════════════════════════════════════════════

def test_init_db_never_raises(db_mod):
    """init_db 契约：永不抛异常。抛了 lifespan 就断，整个服务起不来。"""

    class Nasty:
        def __init__(self):
            raise RuntimeError("彻底炸了")

    assert db_mod.init_db(session_factory=Nasty, sleeper=_FakeClock()) is False


def test_init_db_succeeds_first_try_costs_nothing(db_mod):
    """正常情况：一次成功，不产生任何等待。"""
    clock = _FakeClock()
    factory = _CountingSessionFactory(fail_times=0)

    class _NoOpMeta:
        @staticmethod
        def create_all(bind=None):
            return None

    ok = db_mod.init_db(session_factory=factory, sleeper=clock, metadata=_NoOpMeta)

    assert ok is True
    assert clock.total == 0, "首次成功不应有任何 sleep"
    assert factory.calls == 1


def test_init_db_recovers_from_transient_hiccup(db_mod):
    """一次瞬时抖动应当被就地消化，不劳后台。"""
    clock = _FakeClock()
    factory = _CountingSessionFactory(fail_times=1)

    class _NoOpMeta:
        @staticmethod
        def create_all(bind=None):
            return None

    ok = db_mod.init_db(session_factory=factory, sleeper=clock, metadata=_NoOpMeta)

    assert ok is True, "单次瞬时抖动应能就地重试成功"
    assert clock.total <= STARTUP_BUDGET_CEILING


def test_selfheal_daemon_not_started_when_db_already_healthy(db_mod):
    """DB 本来就好：不起线程。无谓的常驻线程是资源泄漏。"""
    t = db_mod.start_db_selfheal_daemon(
        session_factory=_CountingSessionFactory(fail_times=0),
        interval=0.01,
        only_if_needed=True,
        metadata=_NoOpMeta,
    )
    assert t is None, "DB 已就绪却仍起了守护线程 —— 常驻空转即资源泄漏"


def test_selfheal_daemon_survives_unexpected_errors(db_mod):
    """自愈守护自身异常不得让线程炸掉后静默消失。"""

    class Exploding:
        def __init__(self):
            raise ValueError("非 DB 类异常")

    t = db_mod.start_db_selfheal_daemon(
        session_factory=Exploding,
        interval=0.01,
        max_interval=0.02,
        max_rounds=3,
    )
    if t is not None:
        t.join(timeout=5.0)
        assert not t.is_alive(), "达到最大轮次后守护线程应退出"


def test_startup_total_budget_far_below_supervisor_kill_line(db_mod):
    """端到端：init_db + 账号接入的启动总预算必须远低于 supervisor 判死线。

    这是本次事故的最终判据 —— 265s vs 260s，只差 5 秒就是死循环。
    """
    from app.services import account_bootstrap as ab

    db_clock = _FakeClock()
    db_mod.init_db(session_factory=_BoomSession, sleeper=db_clock)

    boot_clock = _FakeClock()

    def boom_factory():
        raise sqlite3.OperationalError("attempt to write a readonly database")

    ab.bootstrap(
        session_factory=boom_factory,
        decryptor=lambda x: x,
        connector=lambda **k: True,
        sleeper=boot_clock,
    )

    total = db_clock.total + boot_clock.total
    assert total <= STARTUP_BUDGET_CEILING, (
        f"启动总阻塞预算 {total}s 超过上限 {STARTUP_BUDGET_CEILING}s。"
        f"事故值 265s，supervisor 判死线 {SUPERVISOR_KILL_BUDGET}s —— "
        f"只差 5 秒就是无限重启死循环。"
    )
