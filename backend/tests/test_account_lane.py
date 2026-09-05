"""
Phase 3 · 账号执行车道契约测试

覆盖三条多租户红线：
  1. 有界线程池：worker 数与客户数 N 解耦；单账号零调度开销；单账号异常不波及他人
  2. 下单错峰：N=1 零延迟；窗口随 N 增长但封顶；抖动只等待不排队
  3. 滑点归因：能区分「挤单」与「行情」；脏数据不阻断
"""

import random
import threading
import time

import pytest

from app.core.account_lane import (
    ConcurrencyAttribution,
    ConcurrencyGauge,
    JITTER_MAX_WINDOW_MS,
    JITTER_MIN_WINDOW_MS,
    LanePool,
    active_accounts,
    apply_order_jitter,
    compute_jitter_ms,
    get_attribution,
    get_lane_pool,
    record_fill,
    reset_lane_pool,
    set_active_accounts,
)

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════
# 1) 有界线程池
# ══════════════════════════════════════════════════════════════


class TestLanePoolBounded:
    def test_worker_count_never_scales_with_client_count(self):
        """铁律：N 从 1 涨到 1000，线程数不得线性膨胀。"""
        pool = LanePool()
        assert pool.max_workers <= 32, "线程池上界必须封顶，否则 N=100 会打爆机器"
        assert pool.max_workers >= 4

    def test_explicit_max_workers_still_capped(self):
        pool = LanePool(max_workers=999)
        assert pool.max_workers == 999 or pool.max_workers <= 999
        # 显式指定时尊重调用方，但默认路径必须封顶
        assert LanePool().max_workers <= 32

    def test_lazy_creation_no_threads_until_needed(self):
        """进程启动即建池 = 白占线程。必须懒创建。"""
        pool = LanePool()
        assert pool._pool is None
        pool.map_accounts(lambda x: x, [1, 2])
        assert pool._pool is not None
        pool.shutdown()

    def test_single_account_bypasses_pool(self):
        """N=1 是常态部署，不该有线程调度开销。"""
        pool = LanePool()
        res = pool.map_accounts(lambda x: x * 2, [21])
        assert res == [{"item": 21, "ok": True, "result": 42, "error": None}]
        assert pool._pool is None, "单账号不应创建线程池"

    def test_results_keep_input_order(self):
        """结果顺序必须与输入一致，否则账号与结果会错位——多租户致命。"""
        pool = LanePool()

        def _slow(x):
            time.sleep(0.02 if x == 1 else 0.001)
            return x

        res = pool.map_accounts(_slow, [1, 2, 3, 4])
        assert [r["item"] for r in res] == [1, 2, 3, 4]
        assert [r["result"] for r in res] == [1, 2, 3, 4]
        pool.shutdown()

    def test_one_account_failure_does_not_kill_others(self):
        """A 客户的账号炸了，绝不能让 B 客户这一轮不交易。"""
        pool = LanePool()

        def _fn(x):
            if x == 2:
                raise RuntimeError("账号2 MT5 断线")
            return x * 10

        res = pool.map_accounts(_fn, [1, 2, 3])
        assert res[0]["ok"] is True and res[0]["result"] == 10
        assert res[1]["ok"] is False and "断线" in res[1]["error"]
        assert res[2]["ok"] is True and res[2]["result"] == 30
        pool.shutdown()

    def test_empty_input_returns_empty(self):
        assert LanePool().map_accounts(lambda x: x, []) == []

    def test_真并发_不是串行(self):
        """4 个各 sleep 50ms 的任务，并发应远小于 200ms。"""
        pool = LanePool()
        t0 = time.time()
        pool.map_accounts(lambda x: time.sleep(0.05), [1, 2, 3, 4])
        elapsed = time.time() - t0
        assert elapsed < 0.18, f"疑似串行执行，耗时 {elapsed:.3f}s"
        pool.shutdown()

    def test_pool_is_singleton_and_reusable(self):
        reset_lane_pool()
        try:
            a = get_lane_pool()
            b = get_lane_pool()
            assert a is b, "必须复用同一个池，否则又变成每轮新建"
        finally:
            reset_lane_pool()

    def test_reset_releases_pool(self):
        reset_lane_pool()
        p = get_lane_pool()
        p.map_accounts(lambda x: x, [1, 2])
        reset_lane_pool()
        assert get_lane_pool() is not p

    def test_named_pools_are_isolated(self):
        """用户级与账号级必须是两个池，否则嵌套派发会死锁。"""
        reset_lane_pool()
        try:
            u = get_lane_pool("user")
            a = get_lane_pool("account")
            assert u is not a, "user 与 account 共用一个池会造成线程池嵌套死锁"
            assert get_lane_pool("user") is u
        finally:
            reset_lane_pool()

    def test_named_pool_limits_applied(self):
        reset_lane_pool()
        try:
            assert get_lane_pool("user").max_workers <= 16
            assert get_lane_pool("account").max_workers <= 32
        finally:
            reset_lane_pool()

    def test_default_name_is_account(self):
        reset_lane_pool()
        try:
            assert get_lane_pool() is get_lane_pool("account")
        finally:
            reset_lane_pool()

    def test_reset_single_named_pool(self):
        reset_lane_pool()
        try:
            u = get_lane_pool("user")
            a = get_lane_pool("account")
            reset_lane_pool("user")
            assert get_lane_pool("user") is not u
            assert get_lane_pool("account") is a, "只重置 user 不应波及 account"
        finally:
            reset_lane_pool()

    def test_nested_dispatch_does_not_deadlock(self):
        """回归防线：用户级任务内部派发账号级任务，必须能在超时内完成。"""
        reset_lane_pool()
        try:
            def _account_job(x):
                time.sleep(0.01)
                return x * 2

            def _user_job(uid):
                res = get_lane_pool("account").map_accounts(_account_job, [1, 2, 3])
                return sum(r["result"] for r in res)

            done = []
            t = threading.Thread(
                target=lambda: done.extend(
                    get_lane_pool("user").map_accounts(_user_job, list(range(20)))
                ),
                daemon=True,
            )
            t.start()
            t.join(timeout=20)
            assert not t.is_alive(), "嵌套派发发生死锁"
            assert len(done) == 20
            assert all(r["ok"] and r["result"] == 12 for r in done)
        finally:
            reset_lane_pool()


# ══════════════════════════════════════════════════════════════
# 2) 下单错峰（jitter 而非 queue）
# ══════════════════════════════════════════════════════════════


class TestOrderJitter:
    def test_single_client_zero_delay(self):
        """单客户部署绝不能被并发防护惩罚。"""
        assert compute_jitter_ms(1) == 0.0
        assert compute_jitter_ms(0) == 0.0
        assert compute_jitter_ms(-5) == 0.0

    def test_window_grows_with_n_then_caps(self):
        rng = random.Random(42)
        for n, expected_window in ((2, 200.0), (3, 240.0), (10, 800.0), (50, 800.0), (500, 800.0)):
            vals = [compute_jitter_ms(n, rng=rng) for _ in range(200)]
            assert max(vals) <= expected_window + 1e-9, f"n={n} 抖动超出窗口"
            assert min(vals) >= 0.0

    def test_never_exceeds_hard_ceiling(self):
        """无论 N 多大，单笔抖动不得超过 800ms——再多就影响成交价了。"""
        rng = random.Random(7)
        for n in (2, 8, 64, 1000):
            for _ in range(100):
                assert compute_jitter_ms(n, rng=rng) <= JITTER_MAX_WINDOW_MS

    def test_min_window_respected(self):
        """n=2 时窗口应为下界 200ms，而不是 160ms。"""
        rng = random.Random(1)
        vals = [compute_jitter_ms(2, rng=rng) for _ in range(500)]
        assert max(vals) <= JITTER_MIN_WINDOW_MS + 1e-9
        assert max(vals) > JITTER_MIN_WINDOW_MS * 0.8, "取值应铺满窗口，不能挤在一角"

    def test_jitter_is_random_not_fixed(self):
        """固定延迟等于换个方式同秒挤单，必须随机。"""
        vals = {compute_jitter_ms(10) for _ in range(50)}
        assert len(vals) > 40, "抖动必须随机化，否则仍会撞同一毫秒"

    def test_expected_gap_stays_small_at_high_n(self):
        """N=50 期望间隔应在毫秒级；若做成排队会变成几十秒（漏单）。"""
        expected_gap_ms = (JITTER_MAX_WINDOW_MS / 2) / 50
        assert expected_gap_ms < 10.0

    def test_apply_jitter_actually_sleeps(self):
        slept = []
        ms = apply_order_jitter(10, sleeper=slept.append, rng=random.Random(3))
        assert ms > 0
        assert len(slept) == 1
        assert abs(slept[0] * 1000 - ms) < 1e-6

    def test_apply_jitter_no_sleep_for_single(self):
        slept = []
        assert apply_order_jitter(1, sleeper=slept.append) == 0.0
        assert slept == []

    def test_jitter_failure_never_blocks_order(self):
        """错峰是优化不是闸门：sleeper 抛异常也必须放行下单。"""

        def _boom(_):
            raise OSError("时钟异常")

        assert apply_order_jitter(10, sleeper=_boom) == 0.0


# ══════════════════════════════════════════════════════════════
# 3) 滑点归因
# ══════════════════════════════════════════════════════════════


class TestConcurrencyAttribution:
    def test_empty_summary_is_safe(self):
        a = ConcurrencyAttribution()
        s = a.summary()
        assert s["available"] is False
        assert s["count"] == 0

    def test_pip_computed_for_xauusd(self):
        a = ConcurrencyAttribution()
        s = a.record(1, "BUY", 4000.00, 4000.35)
        assert s is not None
        assert abs(s.pip - 3.5) < 1e-6, "XAUUSD 1 pip = 0.1，0.35 应为 3.5 pip"

    def test_slippage_is_absolute_both_directions(self):
        """正向滑点也要记（成交更优也是执行质量信息）。"""
        a = ConcurrencyAttribution()
        assert abs(a.record(1, "SELL", 4000.0, 3999.8).pip - 2.0) < 1e-6

    def test_dirty_data_silently_dropped(self):
        a = ConcurrencyAttribution()
        assert a.record(1, "BUY", None, 4000.0) is None
        assert a.record(1, "BUY", "x", 4000.0) is None
        assert a.record(1, "BUY", 0, 4000.0) is None
        assert a.record(1, "BUY", 4000.0, -1) is None
        assert a.summary()["count"] == 0

    def test_bad_concurrency_value_defaults_to_one(self):
        a = ConcurrencyAttribution()
        s = a.record(1, "BUY", 4000.0, 4000.1, concurrent_n="oops")
        assert s.concurrent_n == 1

    def test_buckets_group_by_concurrency(self):
        a = ConcurrencyAttribution()
        a.record(1, "BUY", 4000.0, 4000.1, concurrent_n=1)
        a.record(2, "BUY", 4000.0, 4000.2, concurrent_n=3)
        a.record(3, "BUY", 4000.0, 4000.5, concurrent_n=9)
        s = a.summary()
        assert set(s["by_bucket"]) == {"n=1", "n=2-3", "n>=8"}
        assert s["by_bucket"]["n=1"]["count"] == 1

    def test_per_account_breakdown(self):
        """多租户必须能按账号看执行质量，不能只有一个全局平均数。"""
        a = ConcurrencyAttribution()
        a.record("acct-A", "BUY", 4000.0, 4000.1)
        a.record("acct-A", "BUY", 4000.0, 4000.3)
        a.record("acct-B", "BUY", 4000.0, 4000.1)
        s = a.summary()
        assert s["by_account"]["acct-A"]["count"] == 2
        assert s["by_account"]["acct-B"]["count"] == 1

    def test_crowding_detected_when_high_concurrency_worse(self):
        """核心价值：能指认出「是我们自己在挤单」。"""
        a = ConcurrencyAttribution()
        for _ in range(6):
            a.record(1, "BUY", 4000.0, 4000.10, concurrent_n=1)   # 1.0 pip
        for _ in range(6):
            a.record(2, "BUY", 4000.0, 4000.40, concurrent_n=10)  # 4.0 pip
        assert a.summary()["crowding_suspected"] is True

    def test_no_crowding_when_slippage_flat(self):
        """各并发档滑点持平 → 滑点来自行情，不该误报挤单去乱调参数。"""
        a = ConcurrencyAttribution()
        for _ in range(6):
            a.record(1, "BUY", 4000.0, 4000.10, concurrent_n=1)
            a.record(2, "BUY", 4000.0, 4000.11, concurrent_n=10)
        assert a.summary()["crowding_suspected"] is False

    def test_insufficient_samples_never_cries_wolf(self):
        a = ConcurrencyAttribution()
        a.record(1, "BUY", 4000.0, 4000.10, concurrent_n=1)
        a.record(2, "BUY", 4000.0, 4000.90, concurrent_n=10)
        assert a.summary()["crowding_suspected"] is False, "样本不足不得下结论"

    def test_buffer_is_bounded(self):
        """长跑进程不得无界增长。"""
        a = ConcurrencyAttribution(cap=10)
        for i in range(100):
            a.record(1, "BUY", 4000.0, 4000.0 + i * 0.01)
        assert a.summary()["count"] == 10

    def test_module_level_record_never_raises(self):
        assert record_fill(None, None, "bad", "worse") is None
        assert get_attribution() is get_attribution()

    def test_thread_safe_concurrent_record(self):
        a = ConcurrencyAttribution(cap=1000)

        def _w():
            for _ in range(100):
                a.record(1, "BUY", 4000.0, 4000.1, concurrent_n=8)

        ts = [threading.Thread(target=_w) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert a.summary()["count"] == 800


# ══════════════════════════════════════════════════════════════
# 4) 并发规模广播
# ══════════════════════════════════════════════════════════════


class TestConcurrencyGauge:
    def test_default_is_one(self):
        assert ConcurrencyGauge().current() == 1

    def test_set_and_read(self):
        g = ConcurrencyGauge()
        assert g.set_active(7) == 7
        assert g.current() == 7

    def test_never_below_one(self):
        g = ConcurrencyGauge()
        g.set_active(0)
        assert g.current() == 1
        g.set_active(-3)
        assert g.current() == 1

    def test_bad_value_defaults_to_one(self):
        g = ConcurrencyGauge()
        g.set_active("nope")
        assert g.current() == 1

    def test_module_level_gauge(self):
        prev = active_accounts()
        try:
            set_active_accounts(5)
            assert active_accounts() == 5
        finally:
            set_active_accounts(prev)
