"""
Phase 6 降级车道 — 平台健康监视器测试
=======================================
覆盖三条铁律：
  铁律一：L3 只停新开仓，绝不影响平仓路径（结构性守卫）
  铁律二：本地 8B 只在 L2 当副驾，L0/L1 不参与方向
  铁律三：降级立即 / 恢复需迟滞（连续健康 + 冷却）
外加：判定矩阵、手数系数、异常安全、人工锁档、并发安全。
"""
import threading
import time

import pytest

from app.services.platform_health_monitor import (
    CLOUD_PROBE_INTERVAL_SEC,
    COMPONENTS,
    FAIL_STREAK_TO_DOWN,
    LOT_MULTIPLIER,
    RECOVER_COOLDOWN_SEC,
    RECOVER_STREAK,
    STALE_SEC,
    ComponentHealth,
    DegradeLevel,
    PlatformHealthMonitor,
    allow_new_entry,
    current_level,
    degrade_enabled,
    get_monitor,
    lot_multiplier,
    report_fail,
    report_ok,
    reset_monitor,
    snapshot_dict,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mon():
    """每个用例一个全新监视器（不污染全局单例）。"""
    return PlatformHealthMonitor()


def _boot(m: PlatformHealthMonitor):
    """把所有组件推到「健康」状态（模拟系统正常跑了一轮）。"""
    for c in COMPONENTS:
        m.report_ok(c)
    return m


def _kill(m: PlatformHealthMonitor, name: str, times: int = FAIL_STREAK_TO_DOWN):
    for _ in range(times):
        m.report_fail(name, "boom")


# ============================================================
#  判定矩阵
# ============================================================
class TestClassificationMatrix:
    def test_all_healthy_is_l0(self, mon):
        _boot(mon)
        assert mon.level() == DegradeLevel.L0
        assert mon.allow_new_entry() is True
        assert mon.lot_multiplier() == 1.0

    def test_deepseek_down_is_l1(self, mon):
        _boot(mon)
        _kill(mon, "deepseek")
        assert mon.level() == DegradeLevel.L1
        assert mon.lot_multiplier() == pytest.approx(0.70)
        assert mon.allow_new_entry() is True

    def test_hunyuan_down_is_l1(self, mon):
        _boot(mon)
        _kill(mon, "hunyuan")
        assert mon.level() == DegradeLevel.L1

    def test_both_clouds_down_with_local_llm_is_l2(self, mon):
        _boot(mon)
        _kill(mon, "deepseek")
        _kill(mon, "hunyuan")
        assert mon.level() == DegradeLevel.L2
        assert mon.lot_multiplier() == pytest.approx(0.40)
        assert mon.allow_new_entry() is True, "L2 仍可开仓，只是缩手"
        assert mon.require_local_confirm() is True

    def test_both_clouds_down_without_local_llm_is_l3(self, mon):
        _boot(mon)
        _kill(mon, "deepseek")
        _kill(mon, "hunyuan")
        _kill(mon, "local_llm")
        assert mon.level() == DegradeLevel.L3
        assert mon.allow_new_entry() is False
        assert mon.lot_multiplier() == 0.0

    def test_market_data_down_is_l3_regardless(self, mon):
        """行情不可信 → 无论 AI 多健康都熔断（垃圾进垃圾出）。"""
        _boot(mon)
        _kill(mon, "market_data")
        assert mon.level() == DegradeLevel.L3
        assert mon.allow_new_entry() is False

    def test_chronos_down_alone_does_not_degrade(self, mon):
        """Chronos 是增强项不是必需项：它挂了云端双脑照常决策，不该降级。"""
        _boot(mon)
        _kill(mon, "chronos")
        assert mon.level() == DegradeLevel.L0

    def test_local_llm_down_alone_does_not_degrade(self, mon):
        """本地 LLM 在 L0 只是校对员，挂了不影响主链路。"""
        _boot(mon)
        _kill(mon, "local_llm")
        assert mon.level() == DegradeLevel.L0


# ============================================================
#  失败阈值：单次抖动不降级
# ============================================================
class TestFailStreak:
    def test_single_failure_does_not_degrade(self, mon):
        """云 API 偶发一次 5xx 就砍全体客户手数，是过度反应。"""
        _boot(mon)
        mon.report_fail("deepseek", "一次超时")
        assert mon.level() == DegradeLevel.L0

    def test_streak_reaches_threshold_degrades(self, mon):
        _boot(mon)
        for i in range(FAIL_STREAK_TO_DOWN):
            mon.report_fail("deepseek", f"fail{i}")
        assert mon.level() == DegradeLevel.L1

    def test_success_resets_streak(self, mon):
        _boot(mon)
        mon.report_fail("deepseek")
        mon.report_ok("deepseek")
        mon.report_fail("deepseek")
        assert mon.level() == DegradeLevel.L0, "成功应清零连败计数"


# ============================================================
#  铁律三：迟滞
# ============================================================
class TestHysteresis:
    def test_degrade_is_immediate(self, mon):
        """恶化立即生效，不等冷却——风险面前不讲究稳定性。"""
        _boot(mon)
        t0 = time.time()
        _kill(mon, "deepseek")
        assert mon.level() == DegradeLevel.L1
        assert time.time() - t0 < 1.0

    def test_recover_needs_streak(self, mon):
        _boot(mon)
        _kill(mon, "deepseek")
        assert mon.level() == DegradeLevel.L1
        # 绕过冷却（直接改内部时间戳），只验证 streak 条件
        mon._last_down_ts = time.time() - RECOVER_COOLDOWN_SEC - 1
        for i in range(RECOVER_STREAK - 1):
            mon.report_ok("deepseek")
            assert mon.level() == DegradeLevel.L1, f"第{i+1}次健康就恢复=没有迟滞"
        mon.report_ok("deepseek")
        assert mon.level() == DegradeLevel.L0

    def test_recover_needs_cooldown(self, mon):
        """streak 够了但冷却没过，仍不许恢复。"""
        _boot(mon)
        _kill(mon, "deepseek")
        for _ in range(RECOVER_STREAK + 3):
            mon.report_ok("deepseek")
        assert mon.level() == DegradeLevel.L1, "冷却未过就恢复=没有迟滞"
        snap = mon.snapshot()
        assert snap.pending_recover is True
        assert snap.raw_level == DegradeLevel.L0

    def test_flapping_does_not_thrash_lot_multiplier(self, mon):
        """反复抖动时手数系数必须稳定在保守档，不能 1.0/0.7 来回横跳。"""
        _boot(mon)
        seen = set()
        for _ in range(6):
            _kill(mon, "deepseek")
            seen.add(mon.lot_multiplier())
            mon.report_ok("deepseek")
            seen.add(mon.lot_multiplier())
        assert seen == {0.70}, f"抖动期间系数应恒为 0.70，实际 {seen}"

    def test_recover_resets_after_new_failure(self, mon):
        mon_ = _boot(mon)
        _kill(mon_, "deepseek")
        mon_._last_down_ts = time.time() - RECOVER_COOLDOWN_SEC - 1
        mon_.report_ok("deepseek")
        mon_.report_ok("deepseek")  # streak=2，差一次
        _kill(mon_, "deepseek")     # 再次失败
        mon_._last_down_ts = time.time() - RECOVER_COOLDOWN_SEC - 1
        mon_.report_ok("deepseek")
        assert mon_.level() == DegradeLevel.L1, "新失败后恢复计数必须清零重来"


# ============================================================
#  铁律一：L3 只关水龙头
# ============================================================
class TestL3OnlyStopsEntry:
    def test_l3_blocks_entry(self, mon):
        _boot(mon)
        _kill(mon, "market_data")
        assert mon.allow_new_entry() is False

    def test_module_has_no_close_or_flatten_api(self):
        """结构性守卫：本模块**不得**提供任何「平仓/清仓」接口。

        一旦有人给降级模块加上 force_close_all，铁律一就名存实亡。
        这条断言让这种改动在 CI 里当场炸掉。
        """
        import app.services.platform_health_monitor as phm

        banned = ("close_all", "flatten", "force_close", "liquidate", "close_positions")
        exported = dir(phm)
        for b in banned:
            hits = [n for n in exported if b in n.lower()]
            assert not hits, f"降级模块出现平仓类接口 {hits} → 违反铁律一（只关水龙头不抽水）"

    def test_source_never_touches_position_closing(self):
        """源码级守卫：不得 import 任何平仓相关服务。"""
        from pathlib import Path

        import app.services.platform_health_monitor as phm

        src = Path(phm.__file__).read_text(encoding="utf-8")
        for bad in ("smart_exit", "trade_executor", "mt5_service", "close_position"):
            assert bad not in src, f"降级模块引用了 {bad} → 存在越权平仓风险"


# ============================================================
#  铁律二：本地 8B 只在 L2 当副驾
# ============================================================
class TestLocalLLMRoleBoundary:
    def test_require_local_confirm_only_at_l2(self, mon):
        _boot(mon)
        assert mon.require_local_confirm() is False, "L0 不得启用副驾"
        _kill(mon, "deepseek")
        assert mon.require_local_confirm() is False, "L1 不得启用副驾"
        _kill(mon, "hunyuan")
        assert mon.require_local_confirm() is True, "L2 才启用副驾"

    def test_l3_does_not_require_local_confirm(self, mon):
        """L3 压根不开仓，谈不上『需要确认』——避免调用方误以为还能开。"""
        _boot(mon)
        _kill(mon, "market_data")
        assert mon.require_local_confirm() is False
        assert mon.allow_new_entry() is False


# ============================================================
#  手数系数
# ============================================================
class TestLotMultiplier:
    def test_multipliers_are_monotonic(self):
        vals = [LOT_MULTIPLIER[l] for l in
                (DegradeLevel.L0, DegradeLevel.L1, DegradeLevel.L2, DegradeLevel.L3)]
        assert vals == sorted(vals, reverse=True), "能力越弱手数必须越小"
        assert vals[0] == 1.0 and vals[-1] == 0.0

    def test_gap_between_levels_is_meaningful(self):
        """相邻档差距 ≥ 0.2，否则降级形同虚设。"""
        order = [DegradeLevel.L0, DegradeLevel.L1, DegradeLevel.L2]
        for a, b in zip(order, order[1:]):
            assert LOT_MULTIPLIER[a] - LOT_MULTIPLIER[b] >= 0.2

    def test_l3_multiplier_is_zero_double_safety(self, mon):
        """L3 双保险：即便调用方忘了查 allow_new_entry，手数也是 0。"""
        _boot(mon)
        _kill(mon, "market_data")
        assert mon.lot_multiplier() == 0.0


# ============================================================
#  异常安全
# ============================================================
class TestFailSafe:
    def test_unknown_component_is_ignored(self, mon):
        _boot(mon)
        mon.report_fail("不存在的组件", "typo")
        mon.report_ok("也不存在")
        assert mon.level() == DegradeLevel.L0

    def test_module_level_helpers_never_raise(self, monkeypatch):
        """监控自身故障 → 手数不砍(1.0)、不隐形停机(True)。"""
        import app.services.platform_health_monitor as phm

        class Boom:
            def lot_multiplier(self):
                raise RuntimeError("monitor exploded")

            def allow_new_entry(self):
                raise RuntimeError("monitor exploded")

            def require_local_confirm(self):
                raise RuntimeError("monitor exploded")

        monkeypatch.setattr(phm, "get_monitor", lambda: Boom())
        assert phm.lot_multiplier() == 1.0, "监控挂了不该误砍客户手数"
        assert phm.allow_new_entry() is True, "监控挂了不该变成隐形停机"
        assert phm.require_local_confirm() is False

    def test_report_never_raises(self, mon):
        for bad in (None, 123, object()):
            mon.report(bad, True)  # type: ignore[arg-type]
        assert mon.level() == DegradeLevel.L0

    def test_kill_switch_env(self, monkeypatch):
        monkeypatch.setenv("WX_DEGRADE_DISABLED", "1")
        assert degrade_enabled() is False
        monkeypatch.setenv("WX_DEGRADE_DISABLED", "0")
        assert degrade_enabled() is True
        monkeypatch.delenv("WX_DEGRADE_DISABLED", raising=False)
        assert degrade_enabled() is True


# ============================================================
#  staleness：沉默 ≠ 健康
# ============================================================
class TestStaleness:
    def test_never_reported_is_stale_not_up(self):
        c = ComponentHealth(name="deepseek")
        assert c.is_stale is True
        assert c.is_up is False
        assert c.is_down is False, "从未上报不等于失败"

    def test_old_report_goes_stale(self):
        c = ComponentHealth(name="deepseek")
        c.report(True)
        assert c.is_up is True
        c.last_report_ts = time.time() - STALE_SEC - 1
        assert c.is_stale is True
        assert c.is_up is False

    def test_cold_start_stale_market_does_not_trigger_l3(self, mon):
        """冷启动首轮行情必然 stale，此时判 L3 会让系统永远开不出第一单。"""
        assert mon.level() == DegradeLevel.L0
        assert mon.allow_new_entry() is True


# ============================================================
#  人工锁档
# ============================================================
class TestManualOverride:
    def test_manual_pin_overrides_auto(self, mon):
        _boot(mon)
        assert mon.level() == DegradeLevel.L0
        mon.set_manual_level(DegradeLevel.L3, "混沌演练")
        assert mon.level() == DegradeLevel.L3
        assert mon.allow_new_entry() is False
        snap = mon.snapshot()
        assert "演练" in snap.reason

    def test_manual_release_returns_to_auto(self, mon):
        _boot(mon)
        mon.set_manual_level(DegradeLevel.L2)
        assert mon.level() == DegradeLevel.L2
        mon.set_manual_level(None)
        assert mon.level() == DegradeLevel.L0

    def test_manual_pin_survives_reports(self, mon):
        _boot(mon)
        mon.set_manual_level(DegradeLevel.L1, "运维手动")
        _kill(mon, "market_data")  # 自动判定会到 L3
        assert mon.level() == DegradeLevel.L1, "人工锁档期间自动判定不得覆盖"


# ============================================================
#  云调用节流（自愈路径保留）
# ============================================================
class TestCloudCallThrottle:
    def test_healthy_cloud_always_callable(self, mon):
        _boot(mon)
        assert mon.allow_cloud_call("deepseek") is True

    def test_down_cloud_still_retried_at_l1(self, mon):
        """L1 下失联的云走半开窗口探活：不每轮白撞，但过窗口必放行一次，自愈不丢。

        ★ 2026-08-11 行为变更（云消耗优化）：原实现 L1 恒 True → DS 欠费期间
          每 60s 撞一次死接口，8/11 实测 465 次无效 402。改为统一半开窗口
          （180s 探活一次），省配额且保留自愈通道。
        """
        _boot(mon)
        _kill(mon, "deepseek")
        assert mon.level() == DegradeLevel.L1
        assert mon.allow_cloud_call("deepseek") is False, "失联云进入半开窗口，不每轮重试"
        # 自愈通道：越过探活窗口后必须放行一次（ok/fail 时间戳都推到窗口外）
        _old = time.time() - CLOUD_PROBE_INTERVAL_SEC - 1
        mon._comp["deepseek"].last_fail_ts = _old
        mon._comp["deepseek"].last_ok_ts = _old
        assert mon.allow_cloud_call("deepseek") is True, "探活窗口到点必须放行（自愈通道）"

    def test_down_cloud_skipped_at_l2(self, mon):
        """L2 下不再重试死云：60s 一轮里两次 30s 超时会吃光整轮。"""
        _boot(mon)
        _kill(mon, "deepseek")
        _kill(mon, "hunyuan")
        assert mon.level() == DegradeLevel.L2
        assert mon.allow_cloud_call("deepseek") is False
        assert mon.allow_cloud_call("hunyuan") is False

    def test_half_open_probe_reopens_after_interval(self, mon):
        """★ 自愈通道：L2 下每隔 CLOUD_PROBE_INTERVAL_SEC 必须放一次探活。

        删掉这个窗口会形成自锁——跳过调用→没有成功上报→永远判失联→
        系统永久卡在 L2，再也回不到全能力。
        """
        _boot(mon)
        _kill(mon, "deepseek")
        _kill(mon, "hunyuan")
        assert mon.allow_cloud_call("deepseek") is False
        old = time.time() - CLOUD_PROBE_INTERVAL_SEC - 1
        mon._comp["deepseek"].last_fail_ts = old
        mon._comp["deepseek"].last_ok_ts = old
        assert mon.allow_cloud_call("deepseek") is True, "半开窗口失效 = 永久卡降级档"

    def test_half_open_leads_back_to_l0(self, mon):
        """完整自愈链路：L2 →（探活成功 ×3 + 冷却）→ L0。"""
        _boot(mon)
        _kill(mon, "deepseek")
        _kill(mon, "hunyuan")
        assert mon.level() == DegradeLevel.L2
        mon._last_down_ts = time.time() - RECOVER_COOLDOWN_SEC - 1
        for _ in range(RECOVER_STREAK):
            mon.report_ok("deepseek")
            mon.report_ok("hunyuan")
        assert mon.level() == DegradeLevel.L0

    def test_unknown_component_defaults_to_callable(self, mon):
        assert mon.allow_cloud_call("不认识") is True


# ============================================================
#  快照与迁移记录
# ============================================================
class TestSnapshot:
    def test_snapshot_shape(self, mon):
        _boot(mon)
        d = mon.snapshot().as_dict()
        for k in ("level", "level_name", "label", "detail", "lot_multiplier",
                  "allow_new_entry", "require_local_confirm", "reason",
                  "raw_level", "pending_recover", "components", "since_sec"):
            assert k in d, f"快照缺字段 {k}"
        assert set(d["components"].keys()) == set(COMPONENTS)

    def test_transitions_recorded_and_bounded(self, mon):
        _boot(mon)
        for _ in range(60):
            _kill(mon, "deepseek")
            mon._last_down_ts = 0.0
            for _ in range(RECOVER_STREAK):
                mon.report_ok("deepseek")
        tr = mon.transitions(limit=1000)
        assert len(tr) <= 100, "迁移记录必须有界，否则长跑内存泄漏"
        assert all({"ts", "from", "to", "reason"} <= set(x) for x in tr)

    def test_reset_clears_state(self, mon):
        _boot(mon)
        _kill(mon, "market_data")
        assert mon.level() == DegradeLevel.L3
        mon.reset()
        assert mon.level() == DegradeLevel.L0
        assert mon.transitions() == []


# ============================================================
#  全局单例
# ============================================================
class TestSingleton:
    def test_singleton_identity(self):
        assert get_monitor() is get_monitor()

    def test_module_helpers_roundtrip(self):
        reset_monitor()
        try:
            for c in COMPONENTS:
                report_ok(c)
            assert current_level() == DegradeLevel.L0
            assert lot_multiplier() == 1.0
            assert allow_new_entry() is True
            for _ in range(FAIL_STREAK_TO_DOWN):
                report_fail("market_data", "断线")
            assert current_level() == DegradeLevel.L3
            assert allow_new_entry() is False
            assert snapshot_dict()["level_name"] == "L3"
        finally:
            reset_monitor()


# ============================================================
#  并发
# ============================================================
class TestThreadSafety:
    def test_concurrent_reports_do_not_corrupt(self, mon):
        _boot(mon)
        errors = []

        def worker(name, ok):
            try:
                for _ in range(200):
                    mon.report(name, ok)
                    mon.snapshot()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(c, i % 2 == 0))
            for i, c in enumerate(COMPONENTS * 3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, f"并发上报异常: {errors[:3]}"
        assert mon.level() in tuple(DegradeLevel)


# ============================================================
#  反向守卫（比对台铁律：故意改坏必须炸）
# ============================================================
class TestNegativeGuards:
    def test_guard_catches_removed_hysteresis(self, mon):
        """若有人把迟滞删掉（恢复立即生效），本用例必须失败。

        这里手动模拟「无迟滞」实现，断言它确实会被上面的迟滞用例判负——
        证明那些用例不是摆设。
        """
        _boot(mon)
        _kill(mon, "deepseek")
        # 模拟「无迟滞」：直接强制应用裸判定
        raw, reason = mon._classify_locked()
        mon._apply_locked(raw, reason, time.time())
        assert mon.level() == DegradeLevel.L1, "裸判定此刻仍是 L1（deepseek 仍在失联）"
        # 让 deepseek 恢复一次，若无迟滞就会立刻回 L0
        mon.report_ok("deepseek")
        raw2, _ = mon._classify_locked()
        assert raw2 == DegradeLevel.L0, "裸判定应已回 L0"
        assert mon.level() == DegradeLevel.L1, "但受迟滞保护，对外仍是 L1"

    def test_guard_catches_l3_allowing_entry(self):
        """若有人把 L3 的 allow 改成 True，这里必须炸。"""
        assert LOT_MULTIPLIER[DegradeLevel.L3] == 0.0
        m = PlatformHealthMonitor()
        _boot(m)
        _kill(m, "market_data")
        assert m.allow_new_entry() is False
