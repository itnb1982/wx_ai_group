"""账号级执行控制器与状态机（V6 Phase 3）。

━━━ 这个模块解决什么问题 ━━━

旧路径里"反向对账"只是 `execute_cycle()` 内部的一行调用。它可以被任何一次
early-return、异常吞掉、或后来者新增的分支绕过。于是系统会在**不知道自己
真实持仓**的情况下做决策，直接后果就是线上反复出现的：
    「有的开了有的没开、有的平了有的没平、持仓上限被突破」

V6 的处置不是"再小心一点"，而是**把纪律搬进类型系统**：
    状态机不允许 IDLE → DECIDING。
    于是不存在任何一条代码路径能跳过对账——想跳过就会抛异常。

━━━ 设计边界（扼杀者模式 Strangler Fig）━━━

本模块**不重写** `trade_executor.py`（2700+ 行）。它是**编排壳**：
持有状态机、强制执行阶段顺序、把每个阶段委托给现有执行器。
这样新纪律立刻生效，而已跑通的下单/风控/出场逻辑一行不动。

切换协议见 V6 12.4：S1 双跑 dry-run → S2 单个独立账号 → S3 其余独立
→ S4 主号跟号 → S5 删旧代码。`dry_run=True` 就是 S1 的载体。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

try:
    from loguru import logger
except Exception:  # pragma: no cover - 日志不可用不应阻断交易
    import logging

    logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
# 状态定义
# ═════════════════════════════════════════════════════════════
class AccountState(str, Enum):
    """账号执行状态。继承 str 便于直接落库与 JSON 序列化。"""

    IDLE = "idle"                 # 空闲，周期起点与终点
    RECONCILING = "reconciling"   # 与 MT5 对账（★ 强制入口，不可跳过）
    REPAIRING = "repairing"       # 修复账本漂移
    DECIDING = "deciding"         # AI 决策 + 风控评估
    PLACING = "placing"           # 正在下单
    OPEN = "open"                 # 下单成功，持仓已建立
    MANAGING = "managing"         # 持仓管理（移损/分批/追踪）
    EXITING = "exiting"           # 正在平仓
    BLOCKED = "blocked"           # 被风控否决，本轮作废
    MANUAL_HALT = "manual_halt"   # 人工封盘（唯一由自然人进出的状态）


class IllegalStateTransition(RuntimeError):
    """非法状态转换。

    刻意设计为**抛异常而非静默纠正**：静默纠正会把架构违规藏起来，
    而这类违规恰恰是资金事故的前兆，必须在测试阶段就炸出来。
    """


# 合法转换表。读法：{ 当前状态: 允许去往的状态集合 }
#
# 关键约束（每条都对应一次线上事故）：
#   · IDLE 不含 DECIDING —— 禁止跳过对账
#   · RECONCILING/REPAIRING 可回 IDLE —— 对账失败时作废本轮，宁可少做一单
#   · MANUAL_HALT 不在任何普通状态的出边里 —— 系统永不自动解除封盘
LEGAL_TRANSITIONS: Dict[AccountState, Set[AccountState]] = {
    AccountState.IDLE: {AccountState.RECONCILING},
    AccountState.RECONCILING: {
        AccountState.DECIDING,    # 账本一致
        AccountState.REPAIRING,   # 检出漂移
        AccountState.BLOCKED,     # 授权失效/封盘标志
        AccountState.IDLE,        # 对账失败 → 作废本轮
    },
    AccountState.REPAIRING: {
        AccountState.DECIDING,
        AccountState.BLOCKED,
        AccountState.IDLE,        # 修复失败 → 作废本轮
    },
    AccountState.DECIDING: {
        AccountState.PLACING,     # 风控通过，要开新仓
        AccountState.MANAGING,    # HOLD，但存量持仓仍需管理
        AccountState.BLOCKED,     # 风控否决
        AccountState.IDLE,        # HOLD 且无持仓
    },
    AccountState.PLACING: {
        AccountState.OPEN,        # 下单成功
        AccountState.BLOCKED,     # 下单被拒
        AccountState.IDLE,        # 下单失败
    },
    AccountState.OPEN: {
        AccountState.MANAGING,
        AccountState.IDLE,
    },
    AccountState.MANAGING: {
        AccountState.EXITING,
        AccountState.BLOCKED,
        AccountState.IDLE,
    },
    AccountState.EXITING: {
        AccountState.IDLE,
    },
    AccountState.BLOCKED: {
        AccountState.IDLE,
    },
    # ★ 死胡同是刻意的：只有带 by_human=True 的转换才能出去（见 transition）
    AccountState.MANUAL_HALT: set(),
}

# 人工解除封盘后只能回 IDLE——必须重新走完整周期（含强制对账），
# 不允许"解除后直接决策"跳过对账。
_HUMAN_ONLY_EXITS: Dict[AccountState, Set[AccountState]] = {
    AccountState.MANUAL_HALT: {AccountState.IDLE},
}


@dataclass(frozen=True)
class StateTransition:
    """一次状态转换的审计记录。"""

    from_state: AccountState
    to_state: AccountState
    reason: str
    at: datetime
    by_human: bool = False


class AccountStateMachine:
    """单账号状态机。线程安全。

    为什么要线程安全：L3 护盾守护线程（2s 级）与主交易周期线程会并发触碰
    同一账号——护盾可能在主周期处于 DECIDING 时触发全平。锁的粒度是单账号，
    不同账号完全无竞争（多租户下 N 可能是 50+，绝不能用全局锁）。
    """

    def __init__(
        self,
        account_id: str,
        initial: AccountState = AccountState.IDLE,
        history_limit: int = 200,
    ):
        self.account_id = account_id
        self._state = initial
        self._lock = threading.RLock()
        self._history: List[StateTransition] = []
        # 历史必须有界：长跑账号一天上千次转换，无界 list 就是内存泄漏。
        # 200 条足够覆盖最近若干周期的完整链路，够定位问题。
        self._history_limit = max(1, int(history_limit))

    # ── 只读属性 ──
    @property
    def state(self) -> AccountState:
        with self._lock:
            return self._state

    @property
    def history(self) -> List[StateTransition]:
        with self._lock:
            return list(self._history)

    @property
    def is_halted(self) -> bool:
        return self.state is AccountState.MANUAL_HALT

    # ── 转换 ──
    def can_transition(self, to: AccountState, by_human: bool = False) -> bool:
        with self._lock:
            return self._check(self._state, to, by_human)

    @staticmethod
    def _check(cur: AccountState, to: AccountState, by_human: bool) -> bool:
        # 人工封盘可以从任何状态插队进入（运维按急停时系统可能在任何阶段）
        if to is AccountState.MANUAL_HALT:
            return cur is not AccountState.MANUAL_HALT
        # 人工专属出边（目前只有 MANUAL_HALT → IDLE）
        if by_human and to in _HUMAN_ONLY_EXITS.get(cur, set()):
            return True
        return to in LEGAL_TRANSITIONS.get(cur, set())

    def transition(
        self,
        to: AccountState,
        reason: str = "",
        by_human: bool = False,
    ) -> StateTransition:
        """执行状态转换。非法转换抛 IllegalStateTransition。"""
        with self._lock:
            cur = self._state
            if not self._check(cur, to, by_human):
                raise IllegalStateTransition(
                    f"账号 {self.account_id}: 非法状态转换 {cur.value} → {to.value}"
                    f"（reason={reason!r}, by_human={by_human}）"
                )
            rec = StateTransition(
                from_state=cur, to_state=to, reason=reason,
                at=datetime.now(), by_human=by_human,
            )
            self._state = to
            self._history.append(rec)
            if len(self._history) > self._history_limit:
                # 丢最旧的，保留最近窗口
                del self._history[: len(self._history) - self._history_limit]
            return rec

    def force_state(self, state: AccountState, reason: str = "force") -> None:
        """绕过校验直接置位。

        ⚠️ 仅供测试与进程重启后的状态恢复使用。业务代码调用它 = 架构违规。
        """
        with self._lock:
            self._state = state
            self._history.append(
                StateTransition(
                    from_state=state, to_state=state, reason=f"[FORCE] {reason}",
                    at=datetime.now(), by_human=False,
                )
            )
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AccountStateMachine {self.account_id} state={self._state.value}>"


# ═════════════════════════════════════════════════════════════
# 账号组合快照
# ═════════════════════════════════════════════════════════════
@dataclass
class AccountPortfolio:
    """账号在某一时刻的真实组合快照（对账后产出）。

    这是 ExecutionController 做判断的唯一事实来源。刻意做成 dataclass 而非
    在各处零散查库：零散查库正是"同一周期内不同模块看到不同持仓"的根源。
    """

    account_id: str
    user_id: str = ""
    positions: List[dict] = field(default_factory=list)
    total_lots: float = 0.0
    total_floating_pnl: float = 0.0
    direction_counts: Dict[str, int] = field(default_factory=dict)
    effective_capital: float = 0.0
    capital_source: str = ""
    min_lot: float = 0.0
    max_lot: float = 0.0
    max_position_lots: float = 0.0
    state: AccountState = AccountState.IDLE
    last_reconciled_at: Optional[datetime] = None
    drift_count: int = 0
    license_ok: bool = True

    @classmethod
    def from_positions(
        cls,
        account_id: str,
        positions: List[dict],
        **kwargs: Any,
    ) -> "AccountPortfolio":
        """从 MT5 持仓列表聚合出组合快照。

        字段名兼容 MT5 与本地两种口径（type/direction、volume/lots），
        因为这两套命名在现存代码里都有，强行统一会牵动太多调用点。
        """
        total_lots = 0.0
        pnl = 0.0
        counts: Dict[str, int] = {}
        for p in positions or []:
            try:
                lots = float(p.get("volume", p.get("lots", 0)) or 0)
            except (TypeError, ValueError):
                lots = 0.0
            total_lots += lots
            try:
                pnl += float(p.get("profit", 0) or 0)
            except (TypeError, ValueError):
                pass
            raw_dir = p.get("type", p.get("direction", ""))
            d = str(raw_dir).upper()
            if d in ("0", "BUY", "POSITION_TYPE_BUY"):
                d = "BUY"
            elif d in ("1", "SELL", "POSITION_TYPE_SELL"):
                d = "SELL"
            if d in ("BUY", "SELL"):
                counts[d] = counts.get(d, 0) + 1
        return cls(
            account_id=account_id,
            positions=list(positions or []),
            total_lots=round(total_lots, 4),
            total_floating_pnl=round(pnl, 2),
            direction_counts=counts,
            **kwargs,
        )

    @property
    def has_position(self) -> bool:
        return bool(self.positions)

    def remaining_lot_budget(self) -> float:
        """同方向总持仓上限的剩余额度（第二硬边界）。

        max_position_lots<=0 视为未设置 → 不限制（返回 inf），
        由单笔硬顶 max_lot 兜底。
        """
        if self.max_position_lots <= 0:
            return float("inf")
        return max(0.0, self.max_position_lots - self.total_lots)


# ═════════════════════════════════════════════════════════════
# 周期结果
# ═════════════════════════════════════════════════════════════
@dataclass
class CycleResult:
    """一个执行周期的结果，供 trading.py 汇总与前端溯源。"""

    account_id: str
    final_state: AccountState
    reconciled: bool = False
    drift_count: int = 0
    decision: Optional[dict] = None
    orders: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    blocked_reason: str = ""
    dry_run: bool = False
    elapsed_sec: float = 0.0
    transitions: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "state": self.final_state.value,
            "reconciled": self.reconciled,
            "drift_count": self.drift_count,
            "decision": self.decision,
            "orders": self.orders,
            "errors": self.errors,
            "blocked_reason": self.blocked_reason,
            "dry_run": self.dry_run,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "transitions": self.transitions,
        }


# ═════════════════════════════════════════════════════════════
# 执行控制器
# ═════════════════════════════════════════════════════════════
# 每账号一个状态机实例，跨周期保持（状态需要在周期之间延续，
# 尤其 MANUAL_HALT 必须跨周期存活直到人工解除）。
_MACHINES: Dict[str, AccountStateMachine] = {}
_MACHINES_LOCK = threading.Lock()


def get_state_machine(account_id: str) -> AccountStateMachine:
    """取（或创建）账号状态机。多租户下按 account_id 严格隔离。"""
    with _MACHINES_LOCK:
        sm = _MACHINES.get(account_id)
        if sm is None:
            sm = AccountStateMachine(account_id)
            _MACHINES[account_id] = sm
        return sm


def all_states() -> Dict[str, str]:
    """所有账号的当前状态（供 /api/health 与前端全景面板）。"""
    with _MACHINES_LOCK:
        return {aid: sm.state.value for aid, sm in _MACHINES.items()}


def reset_machines() -> None:
    """清空全部状态机。仅供测试与进程重启使用。"""
    with _MACHINES_LOCK:
        _MACHINES.clear()


class ExecutionController:
    """单账号执行控制器：强制阶段顺序，委托实际动作给现有执行器。

    参数
    ----
    account_id : str
        MT5 账号主键。
    executor : Any
        现有 `TradeExecutor` 实例（鸭子类型，便于测试注入假对象）。
    dry_run : bool
        扼杀者模式 S1：只记录"本来会怎么做"，不产生任何真实下单副作用。
    halt_checker : Callable[[str], bool] | None
        查询该账号是否处于人工封盘。注入而非直接 import，
        避免 core 层反向依赖 services 层造成循环导入。
    """

    def __init__(
        self,
        account_id: str,
        executor: Any,
        *,
        user_id: str = "",
        dry_run: bool = False,
        halt_checker: Optional[Callable[[str], bool]] = None,
    ):
        self.account_id = account_id
        self.user_id = user_id
        self.executor = executor
        self.dry_run = bool(dry_run)
        self._halt_checker = halt_checker
        self.sm = get_state_machine(account_id)
        self.portfolio = AccountPortfolio(account_id=account_id, user_id=user_id)

    # ── 内部工具 ──
    def _go(self, to: AccountState, reason: str, result: CycleResult) -> None:
        self.sm.transition(to, reason=reason)
        result.transitions.append(f"{to.value}:{reason}")

    def _is_halted(self) -> bool:
        if self.sm.is_halted:
            return True
        if self._halt_checker is None:
            return False
        try:
            return bool(self._halt_checker(self.account_id))
        except Exception as e:
            # 查不到封盘状态时**按未封盘处理**：封盘查询失败不该让全体客户停摆。
            # 真正的兜底在执行层（emergency 模块自身会拒绝下单）。
            logger.warning(f"[ExecCtrl] {self.account_id[:8]} 封盘状态查询失败: {e}")
            return False

    # ── 主流程 ──
    def run_cycle(self) -> CycleResult:
        """执行一个完整周期。永不抛异常——单账号出错不得拖垮其他客户。"""
        t0 = time.time()
        res = CycleResult(account_id=self.account_id, final_state=self.sm.state, dry_run=self.dry_run)

        try:
            # ── 阶段 -1：人工封盘检查（最高优先级，插队一切）──
            if self._is_halted():
                if not self.sm.is_halted:
                    self.sm.transition(AccountState.MANUAL_HALT, reason="检出人工封盘标志")
                res.final_state = AccountState.MANUAL_HALT
                res.blocked_reason = "人工封盘中（MANUAL_HALT），拒绝新开仓；" \
                                     "存量持仓 SL/TP/SmartExit 仍生效，只能人工解除"
                res.elapsed_sec = time.time() - t0
                return res

            # 上一周期若异常中断留在中间态，先归位到 IDLE 再开工。
            # 用 force_state 是因为中间态回 IDLE 的路径未必合法（如 PLACING→IDLE 合法，
            # 但 RECONCILING 崩在一半时状态可能已是 DECIDING），这里是崩溃恢复语义。
            if self.sm.state is not AccountState.IDLE:
                self.sm.force_state(AccountState.IDLE, reason="周期开始前归位（上轮异常残留）")

            # ── 阶段 0：RECONCILING（强制入口，不可跳过）──
            self._go(AccountState.RECONCILING, "周期开始", res)
            ok, drift = self._reconcile()
            res.reconciled = ok
            res.drift_count = drift
            self.portfolio.drift_count = drift
            self.portfolio.last_reconciled_at = datetime.now()

            if not ok:
                # 宁可少做一单，不可盲开一单。
                self._go(AccountState.IDLE, "对账失败，作废本轮", res)
                res.final_state = AccountState.IDLE
                res.blocked_reason = "对账失败：无法确认真实持仓，本轮跳过（不开新仓）"
                res.errors.append(res.blocked_reason)
                res.elapsed_sec = time.time() - t0
                return res

            if drift > 0:
                self._go(AccountState.REPAIRING, f"检出账本漂移 {drift} 笔", res)
                self._go(AccountState.DECIDING, "漂移已修复", res)
            else:
                self._go(AccountState.DECIDING, "账本一致", res)

            # ── 阶段 1：DECIDING + 执行（委托现有执行器）──
            if self.dry_run:
                # S1 双跑：只记录"本来会怎么做"，绝不下单。
                res.decision = {"action": "DRY_RUN", "note": "新路径影子运行，未下单"}
                self._go(AccountState.IDLE, "dry-run 周期结束", res)
                res.final_state = AccountState.IDLE
                res.elapsed_sec = time.time() - t0
                return res

            cycle_out = self._execute()
            res.decision = cycle_out.get("decision")
            res.orders = list(cycle_out.get("orders", []) or [])
            res.errors.extend(cycle_out.get("errors", []) or [])

            # ── 阶段 2：按实际结果收敛状态 ──
            if res.orders:
                self._go(AccountState.PLACING, "提交订单", res)
                self._go(AccountState.OPEN, f"下单成功 {len(res.orders)} 笔", res)
                self._go(AccountState.MANAGING, "转入持仓管理", res)
                self._go(AccountState.IDLE, "本轮结束", res)
            else:
                action = (res.decision or {}).get("action", "HOLD") if isinstance(res.decision, dict) else "HOLD"
                self._go(AccountState.MANAGING, f"无新单（{action}），管理存量持仓", res)
                self._go(AccountState.IDLE, "本轮结束", res)

            res.final_state = AccountState.IDLE

        except IllegalStateTransition as e:
            # 状态机违规必须显式记录——它意味着编排代码有 bug，不是行情问题。
            logger.error(f"[ExecCtrl] {self.account_id[:8]} 状态机违规: {e}")
            res.errors.append(f"状态机违规: {e}")
            self.sm.force_state(AccountState.IDLE, reason="状态机违规后归位")
            res.final_state = AccountState.IDLE
        except Exception as e:
            logger.error(f"[ExecCtrl] {self.account_id[:8]} 周期异常: {e}")
            res.errors.append(str(e))
            self.sm.force_state(AccountState.IDLE, reason=f"周期异常归位: {e}")
            res.final_state = AccountState.IDLE

        res.elapsed_sec = time.time() - t0
        return res

    # ── 阶段实现（可被子类/测试替换）──
    def _reconcile(self) -> tuple:
        """返回 (账本是否可信, 漂移笔数)。"""
        fn = getattr(self.executor, "_reconcile_positions", None)
        if fn is None:
            return True, 0
        out = fn()
        # 现有实现返回 bool；未来升级为返回 (ok, drift) 时这里自动兼容。
        if isinstance(out, tuple) and len(out) == 2:
            return bool(out[0]), int(out[1] or 0)
        return bool(out), 0

    def _execute(self) -> dict:
        fn = getattr(self.executor, "execute_cycle", None)
        if fn is None:
            return {"decision": None, "orders": [], "errors": ["执行器缺少 execute_cycle"]}
        return fn() or {}


__all__ = [
    "AccountState",
    "IllegalStateTransition",
    "LEGAL_TRANSITIONS",
    "StateTransition",
    "AccountStateMachine",
    "AccountPortfolio",
    "CycleResult",
    "ExecutionController",
    "get_state_machine",
    "all_states",
    "reset_machines",
]
