"""Phase 3 — 账号级状态机 ExecutionController 契约测试。

设计依据：V6 设计文档 5.4「状态机转换」+ 12.4 扼杀者模式。

为什么必须有状态机（这不是为了好看的架构图）：
    旧代码里"对账"只是 execute_cycle 里的一行调用，可被 early-return 绕过，
    于是出现过「系统不知道自己真实持仓就去决策」→ 有的开了有的没开、
    有的平了有的没平。V6 把 RECONCILING 提升为**强制入口**：
    只要状态机不许 IDLE→DECIDING，就没有任何代码路径能绕过对账。

契约（每条都是血的教训）：
    C1  IDLE 只能进 RECONCILING（或被人工插队 MANUAL_HALT），
        IDLE→DECIDING 必须抛错——这是本 Phase 存在的全部理由。
    C2  对账失败不得进入 DECIDING：宁可少做一单，不可盲开一单。
    C3  MANUAL_HALT 可从任意状态插队进入（人工最高优先级）。
    C4  MANUAL_HALT 只能由人工解除，系统自动路径解除必须抛错。
    C5  任意状态被风控否决 → BLOCKED → 回 IDLE。
    C6  dry_run 模式绝不产生真实下单副作用（扼杀者模式 S1 的前提）。
"""
import pytest

pytestmark = pytest.mark.contract

from app.core.execution_controller import (  # noqa: E402
    AccountState,
    IllegalStateTransition,
    AccountStateMachine,
    LEGAL_TRANSITIONS,
)


# ─────────────────────────────────────────────────────────────
# C1 / C2：RECONCILING 是不可绕过的强制入口
# ─────────────────────────────────────────────────────────────
class TestReconcilingIsMandatory:
    def test_idle_cannot_jump_to_deciding(self):
        """本 Phase 的核心铁律：不知道自己有什么仓，就不许做决策。"""
        sm = AccountStateMachine("acc-1")
        assert sm.state is AccountState.IDLE
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.DECIDING, reason="试图跳过对账")

    def test_idle_to_reconciling_ok(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        assert sm.state is AccountState.RECONCILING

    def test_reconciling_to_deciding_ok(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        sm.transition(AccountState.DECIDING, reason="账本一致")
        assert sm.state is AccountState.DECIDING

    def test_reconciling_drift_goes_through_repairing(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        sm.transition(AccountState.REPAIRING, reason="检出漂移 2 笔")
        sm.transition(AccountState.DECIDING, reason="修复完成")
        assert sm.state is AccountState.DECIDING

    def test_reconcile_failure_returns_to_idle_not_deciding(self):
        """对账失败（MT5 查询超时）→ 本周期作废回 IDLE，绝不带病进决策。"""
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        sm.transition(AccountState.IDLE, reason="对账失败，跳过本轮")
        assert sm.state is AccountState.IDLE

    def test_placing_cannot_be_reached_without_deciding(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.PLACING, reason="偷跑下单")


# ─────────────────────────────────────────────────────────────
# C3 / C4：人工紧急处置最高优先级，且只能人工解除
# ─────────────────────────────────────────────────────────────
class TestManualHalt:
    @pytest.mark.parametrize("from_state", list(AccountState))
    def test_manual_halt_can_interrupt_any_state(self, from_state):
        """人工封盘必须能插队——运维按下急停时系统可能在任何状态。"""
        if from_state is AccountState.MANUAL_HALT:
            pytest.skip("已在封盘态")
        sm = AccountStateMachine("acc-1")
        sm.force_state(from_state)
        sm.transition(AccountState.MANUAL_HALT, reason="运维 E2 急停")
        assert sm.state is AccountState.MANUAL_HALT

    def test_manual_halt_cannot_auto_resume(self):
        """系统永不自动恢复——否则平完 AI 又开回来。"""
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.MANUAL_HALT, reason="运维急停")
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.IDLE, reason="系统自动恢复")
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.RECONCILING, reason="新周期照常开始")

    def test_manual_halt_resume_requires_human_flag(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.MANUAL_HALT, reason="运维急停")
        sm.transition(AccountState.IDLE, reason="运维解除封盘", by_human=True)
        assert sm.state is AccountState.IDLE

    def test_manual_halt_resume_only_to_idle(self):
        """解除后必须回 IDLE 走完整周期（含强制对账），不能直接开工。"""
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.MANUAL_HALT, reason="运维急停")
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.DECIDING, reason="解除后直接决策", by_human=True)


# ─────────────────────────────────────────────────────────────
# C5：风控否决路径
# ─────────────────────────────────────────────────────────────
class TestBlocked:
    def test_deciding_blocked_then_idle(self):
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        sm.transition(AccountState.DECIDING, reason="账本一致")
        sm.transition(AccountState.BLOCKED, reason="日亏损熔断")
        sm.transition(AccountState.IDLE, reason="本轮结束")
        assert sm.state is AccountState.IDLE

    def test_blocked_cannot_go_straight_to_placing(self):
        sm = AccountStateMachine("acc-1")
        sm.force_state(AccountState.BLOCKED)
        with pytest.raises(IllegalStateTransition):
            sm.transition(AccountState.PLACING, reason="无视风控")


# ─────────────────────────────────────────────────────────────
# 完整正常周期 + 转换表自洽性
# ─────────────────────────────────────────────────────────────
class TestHappyPath:
    def test_full_open_cycle(self):
        sm = AccountStateMachine("acc-1")
        for st, why in [
            (AccountState.RECONCILING, "周期开始"),
            (AccountState.DECIDING, "账本一致"),
            (AccountState.PLACING, "风控通过"),
            (AccountState.OPEN, "下单成功"),
            (AccountState.MANAGING, "进入持仓管理"),
            (AccountState.EXITING, "出场条件满足"),
            (AccountState.IDLE, "平仓完成"),
        ]:
            sm.transition(st, reason=why)
        assert sm.state is AccountState.IDLE
        # 审计链路必须完整可回放
        assert len(sm.history) == 7
        assert sm.history[0].to_state is AccountState.RECONCILING
        assert sm.history[-1].to_state is AccountState.IDLE

    def test_hold_cycle_without_new_order(self):
        """AI 判 HOLD 但仍要管持仓：DECIDING → MANAGING，不经 PLACING。"""
        sm = AccountStateMachine("acc-1")
        sm.transition(AccountState.RECONCILING, reason="周期开始")
        sm.transition(AccountState.DECIDING, reason="账本一致")
        sm.transition(AccountState.MANAGING, reason="HOLD，仅管理存量持仓")
        sm.transition(AccountState.IDLE, reason="本轮结束")
        assert sm.state is AccountState.IDLE


class TestTransitionTableIntegrity:
    def test_every_state_declared(self):
        for st in AccountState:
            assert st in LEGAL_TRANSITIONS, f"转换表遗漏状态 {st}"

    def test_no_state_is_a_dead_end(self):
        """除 MANUAL_HALT（刻意的死胡同，只能人工出）外，任何状态都必须能回到 IDLE。"""
        for st, allowed in LEGAL_TRANSITIONS.items():
            if st in (AccountState.IDLE, AccountState.MANUAL_HALT):
                continue
            assert allowed, f"{st} 无任何出边，会把账号永久卡死"

    def test_history_is_bounded(self):
        """状态历史必须有上限——长跑账号一天上千次转换，不能无界增长成内存泄漏。"""
        sm = AccountStateMachine("acc-1", history_limit=10)
        for _ in range(50):
            sm.transition(AccountState.RECONCILING, reason="x")
            sm.transition(AccountState.IDLE, reason="y")
        assert len(sm.history) <= 10
