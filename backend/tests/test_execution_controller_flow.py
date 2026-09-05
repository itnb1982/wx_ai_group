"""Phase 3 — ExecutionController 编排行为测试。

与 `test_execution_controller.py`（状态机纯契约）分开：这里验证**编排语义**，
即控制器把现有执行器按什么顺序、在什么条件下调用起来。

最重要的一条（`test_reconcile_failure_skips_execute_entirely`）：
对账失败时绝不能调用 `execute_cycle`。旧路径里对账只是一行 bool，
返回 False 后代码仍继续往下走做持仓管理，而持仓管理里存在开仓分支——
这就是"不知道自己有什么仓却下了单"的实际发生路径。
"""
import pytest

pytestmark = pytest.mark.contract

from app.core.execution_controller import (  # noqa: E402
    AccountPortfolio,
    AccountState,
    ExecutionController,
    reset_machines,
)


class _FakeExecutor:
    """假执行器：记录被调用了什么，不产生任何真实副作用。"""

    def __init__(self, reconcile_ok=True, drift=0, orders=None, decision=None, raises=None):
        self._reconcile_ok = reconcile_ok
        self._drift = drift
        self._orders = orders or []
        self._decision = decision or {"action": "HOLD", "confidence": 0.5}
        self._raises = raises
        self.calls = []

    def _reconcile_positions(self):
        self.calls.append("reconcile")
        if self._drift:
            return (self._reconcile_ok, self._drift)
        return self._reconcile_ok

    def execute_cycle(self):
        self.calls.append("execute")
        if self._raises:
            raise self._raises
        return {"decision": self._decision, "orders": self._orders, "errors": []}


@pytest.fixture(autouse=True)
def _clean_machines():
    reset_machines()
    yield
    reset_machines()


class TestControllerOrchestration:
    def test_reconcile_always_runs_before_execute(self):
        ex = _FakeExecutor()
        ExecutionController("acc-1", ex).run_cycle()
        assert ex.calls == ["reconcile", "execute"]

    def test_reconcile_failure_skips_execute_entirely(self):
        """宁可少做一单，不可盲开一单。"""
        ex = _FakeExecutor(reconcile_ok=False)
        res = ExecutionController("acc-1", ex).run_cycle()
        assert ex.calls == ["reconcile"], "对账失败后仍执行了决策 —— 资金事故级 bug"
        assert res.reconciled is False
        assert res.final_state is AccountState.IDLE
        assert "对账失败" in res.blocked_reason

    def test_drift_routes_through_repairing(self):
        ex = _FakeExecutor(drift=3)
        res = ExecutionController("acc-1", ex).run_cycle()
        assert res.drift_count == 3
        assert any("repairing" in t for t in res.transitions)

    def test_orders_produce_open_state_path(self):
        ex = _FakeExecutor(orders=[{"ticket": 1}], decision={"action": "BUY", "confidence": 0.7})
        res = ExecutionController("acc-1", ex).run_cycle()
        path = [t.split(":")[0] for t in res.transitions]
        assert path == ["reconciling", "deciding", "placing", "open", "managing", "idle"]

    def test_hold_path_skips_placing(self):
        ex = _FakeExecutor(orders=[])
        res = ExecutionController("acc-1", ex).run_cycle()
        path = [t.split(":")[0] for t in res.transitions]
        assert "placing" not in path
        assert path == ["reconciling", "deciding", "managing", "idle"]

    def test_dry_run_never_executes(self):
        """扼杀者 S1：影子运行只对账、绝不下单。"""
        ex = _FakeExecutor(orders=[{"ticket": 1}])
        res = ExecutionController("acc-1", ex, dry_run=True).run_cycle()
        assert "execute" not in ex.calls
        assert res.dry_run is True
        assert res.orders == []

    def test_executor_exception_never_propagates(self):
        """单账号炸掉不得拖垮其他客户——多租户硬要求。"""
        ex = _FakeExecutor(raises=RuntimeError("MT5 断线"))
        res = ExecutionController("acc-1", ex).run_cycle()
        assert res.final_state is AccountState.IDLE
        assert any("MT5 断线" in e for e in res.errors)

    def test_state_recovers_after_crash(self):
        """上轮崩在中间态，下轮必须能正常开工，不能永久卡死。"""
        ExecutionController("acc-1", _FakeExecutor(raises=RuntimeError("boom"))).run_cycle()
        ok = _FakeExecutor()
        res = ExecutionController("acc-1", ok).run_cycle()
        assert res.final_state is AccountState.IDLE
        assert ok.calls == ["reconcile", "execute"]

    def test_manual_halt_blocks_cycle(self):
        ex = _FakeExecutor()
        res = ExecutionController("acc-1", ex, halt_checker=lambda _aid: True).run_cycle()
        assert res.final_state is AccountState.MANUAL_HALT
        assert ex.calls == [], "封盘期间不得对账/下单"
        assert "人工封盘" in res.blocked_reason

    def test_halt_checker_failure_does_not_stop_everyone(self):
        """封盘查询异常按未封盘处理，否则一个 DB 抖动会停掉全体客户。"""

        def _boom(_aid):
            raise RuntimeError("db down")

        ex = _FakeExecutor()
        res = ExecutionController("acc-1", ex, halt_checker=_boom).run_cycle()
        assert res.final_state is AccountState.IDLE
        assert ex.calls == ["reconcile", "execute"]

    def test_machines_isolated_per_account(self):
        """多租户铁律：账号之间状态零串扰。"""
        a = ExecutionController("acc-A", _FakeExecutor(), halt_checker=lambda _a: True)
        b = ExecutionController("acc-B", _FakeExecutor())
        a.run_cycle()
        rb = b.run_cycle()
        assert a.sm.state is AccountState.MANUAL_HALT
        assert rb.final_state is AccountState.IDLE

    def test_result_serializable(self):
        """结果要能直接进 JSON 给前端做溯源。"""
        res = ExecutionController("acc-1", _FakeExecutor()).run_cycle()
        d = res.as_dict()
        assert d["account_id"] == "acc-1"
        assert d["state"] == "idle"
        assert isinstance(d["transitions"], list)


class TestAccountPortfolio:
    def test_aggregates_mt5_positions(self):
        p = AccountPortfolio.from_positions("acc-1", [
            {"ticket": 1, "type": 0, "volume": 0.2, "profit": 10.5},
            {"ticket": 2, "type": 0, "volume": 0.3, "profit": -4.0},
            {"ticket": 3, "type": 1, "volume": 0.1, "profit": 2.0},
        ])
        assert p.total_lots == 0.6
        assert p.total_floating_pnl == 8.5
        assert p.direction_counts == {"BUY": 2, "SELL": 1}
        assert p.has_position is True

    def test_accepts_local_naming(self):
        """本地账本用 direction/lots，MT5 用 type/volume，两套都得认。"""
        p = AccountPortfolio.from_positions("acc-1", [
            {"direction": "BUY", "lots": 0.5, "profit": 1.0},
        ])
        assert p.total_lots == 0.5
        assert p.direction_counts == {"BUY": 1}

    def test_remaining_budget_respects_second_hard_bound(self):
        p = AccountPortfolio.from_positions("acc-1", [{"type": 0, "volume": 0.7}])
        p.max_position_lots = 1.0
        assert abs(p.remaining_lot_budget() - 0.3) < 1e-9

    def test_unset_position_cap_means_unlimited(self):
        p = AccountPortfolio.from_positions("acc-1", [{"type": 0, "volume": 5.0}])
        p.max_position_lots = 0
        assert p.remaining_lot_budget() == float("inf")

    def test_budget_never_negative(self):
        """已超限时返回 0 而非负数——负数会让下游 min() 算出负手数。"""
        p = AccountPortfolio.from_positions("acc-1", [{"type": 0, "volume": 2.0}])
        p.max_position_lots = 1.0
        assert p.remaining_lot_budget() == 0.0

    def test_malformed_position_does_not_crash(self):
        """MT5 偶发脏字段不能炸——聚合失败等于全系统失明。"""
        p = AccountPortfolio.from_positions("acc-1", [
            {"type": 0, "volume": "abc", "profit": None},
            {"type": 0, "volume": 0.1, "profit": 1.0},
        ])
        assert p.total_lots == 0.1
        assert p.total_floating_pnl == 1.0
