"""
SignalBus —— 进程级共享状态总线（Phase 2 / V6）

═══ 这个模块解决什么问题 ═══
交易执行器里散落着近十张模块级 dict，用来承载"跨线程、跨周期"的隐式状态：
跟单去重、镜像幂等、开仓冷却、平仓 churn、对账节流……
它们各自配一把锁（有的还忘了用），各自写一遍 GC，各自定义一套 TTL 语义。

进程内同时有 4 类线程在动这些表（已在 routers/trading.py + main.py 证实）：
  ① 主自动循环          _auto_loop
  ② 副号实时跟单守护线程  _follower_mirror_loop（10s）
  ③ 利润锁利高频守护线程  _l3_profit_lock_monitor_loop（2s）
  ④ 独立账号并发执行     ThreadPoolExecutor
在这种环境下，"先查再写"这种两段式写法**根本不是去重**——两次加锁之间
的裸奔窗口足够让另一条线程整个走完一遍下单流程。

本模块提供一个基本原语来根除这类竞态：

    claim(key)  —— 原子 test-and-set。一次加锁内完成"检查 + 占坑"，
                   并发调用中**有且只有一个**返回 True。

以及配套的 release(key)：抢到坑却没能完成不可逆动作时必须归还。
这条同样是硬要求——占坑不归还 = TTL 窗口内永久漏做，
在跟单场景里就是"主号有单、跟号没跟"，比重复跟单更隐蔽。

═══ 顺带解决的三件事 ═══
· 统一 TTL 与 GC：过期回收只写一遍，不再每张表抄一遍 for 循环。
· 快照持久化：snapshot() 在锁内浅拷贝，避免 json.dump 直接遍历活字典时
  被另一线程插入新键打断（RuntimeError: dictionary changed size during iteration）。
· 测试可复位：reset_all() 一行清空所有表。历史上"跨用例全局字典污染"
  制造过假绿，靠人工逐个 clear() 迟早会漏。
"""
import threading
import time
from typing import Any, Dict, Optional


class TTLRegistry:
    """带 TTL 的线程安全状态表，支持原子占坑。

    内部存储 key -> (timestamp, payload)。timestamp 为写入时刻，
    用于 TTL 判定；payload 承载可选负载（如失败计数），不参与判定。
    """

    __slots__ = ("_name", "_ttl", "_d", "_lock")

    def __init__(self, name: str, ttl: Optional[float] = None):
        self._name = name
        self._ttl = ttl                      # None = 永不过期（纯时间戳表）
        self._d: Dict[Any, tuple] = {}
        self._lock = threading.RLock()

    # ── 供旧代码/测试直接触达底层的逃生口（迁移期保持兼容）──
    @property
    def data(self) -> dict:
        return self._d

    @property
    def lock(self):
        return self._lock

    @property
    def name(self) -> str:
        return self._name

    def _fresh(self, entry, ttl) -> bool:
        if entry is None:
            return False
        if ttl is None:
            return True
        return (time.time() - entry[0]) < ttl

    # ══════════════════ 核心原语 ══════════════════

    def claim(self, key, ttl: Optional[float] = None, payload: Any = None) -> bool:
        """原子占坑：未被占用（或已过期）则占下并返回 True，否则返回 False。

        检查与写入在**同一次加锁**内完成，这是它与 `is_active() + mark()`
        两段式写法的根本区别，也是它存在的全部理由。
        """
        eff = self._ttl if ttl is None else ttl
        with self._lock:
            if self._fresh(self._d.get(key), eff):
                return False
            self._d[key] = (time.time(), payload)
            return True

    def release(self, key) -> None:
        """归还坑位。抢到坑但不可逆动作没能完成时**必须**调用，否则造成漏做。"""
        with self._lock:
            self._d.pop(key, None)

    def mark(self, key, payload: Any = None) -> None:
        """无条件占位/续期（等价于旧的 _mark_xxx）。"""
        with self._lock:
            self._d[key] = (time.time(), payload)

    def is_active(self, key, ttl: Optional[float] = None) -> bool:
        """只读检查。注意：它**不能**用来做并发去重，只能当廉价预检。"""
        eff = self._ttl if ttl is None else ttl
        with self._lock:
            return self._fresh(self._d.get(key), eff)

    def ts(self, key, default: float = 0.0) -> float:
        """取写入时刻（不做 TTL 判定），用于"距上次 X 过了多久"这类冷却计算。"""
        with self._lock:
            entry = self._d.get(key)
            return entry[0] if entry else default

    def age(self, key) -> Optional[float]:
        with self._lock:
            entry = self._d.get(key)
            return None if entry is None else (time.time() - entry[0])

    def payload(self, key, default: Any = None) -> Any:
        with self._lock:
            entry = self._d.get(key)
            return entry[1] if entry else default

    def bump(self, key, ttl: Optional[float] = None, start: int = 0) -> int:
        """窗口内计数累加并返回当前值；窗口外视为从头开始（自带过期归零）。

        用于"连续失败预算"这类语义：预算窗口必须与业务幂等窗口对齐，
        否则会出现"幂等还拦着、预算已重置"的错配。
        """
        eff = self._ttl if ttl is None else ttl
        with self._lock:
            entry = self._d.get(key)
            cnt = (entry[1] if self._fresh(entry, eff) else start) or start
            cnt = int(cnt) + 1
            self._d[key] = (time.time(), cnt)
            return cnt

    # ══════════════════ 维护 ══════════════════

    def snapshot(self) -> dict:
        """锁内浅拷贝，供持久化/展示安全遍历（不要直接遍历活字典）。"""
        with self._lock:
            return {k: v for k, v in self._d.items()}

    def gc(self, ttl: Optional[float] = None) -> int:
        """回收过期条目，返回回收条数。"""
        eff = self._ttl if ttl is None else ttl
        if eff is None:
            return 0
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._d.items() if (now - v[0]) >= eff]
            for k in dead:
                self._d.pop(k, None)
            return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    def __repr__(self) -> str:
        return f"<TTLRegistry {self._name} size={len(self)} ttl={self._ttl}>"


# ════════════════════════ 全局注册表 ════════════════════════
# TTL 取值沿用各表原有语义，不在收编过程中夹带行为变更。

#: 跟号已复制的主号票号。key=f"{follower_id}:{leader_ticket}"
COPIED = TTLRegistry("copied", ttl=300.0)

#: ★ 2026-08-19 毫秒级跟单：早信号已分发给该跟号（key=follower_id）。
#:   早信号(ticket=None)与 2a 兜底(真ticket)的 COPIED key 不同，若无此标记
#:   2a 会重复跟单导致双开。分发成功后 mark，2a 里 claim 到即跳过复制。
EARLY_COPIED = TTLRegistry("early_copied", ttl=120.0)

#: 跟号已镜像的主号出场动作。key=f"{follower_id}:{leader_ticket}:{action_type}"
MIRRORED = TTLRegistry("mirrored", ttl=600.0)

#: 镜像平仓连续失败预算。窗口与 MIRRORED 幂等窗口对齐，否则预算与幂等错配。
MIRROR_FAIL = TTLRegistry("mirror_fail", ttl=600.0)

#: 各账号各方向最近平仓时刻（churn 抑制）。TTL 由调用方按客户配置传入。
CLOSE_TS = TTLRegistry("close_ts", ttl=None)

#: 各账号各方向最近开仓时刻（open_interval 冷却）。TTL 由调用方按客户配置传入。
OPEN_TS = TTLRegistry("open_ts", ttl=None)


_ALL = (COPIED, MIRRORED, MIRROR_FAIL, CLOSE_TS, OPEN_TS)


def reset_all() -> None:
    """清空所有表。测试专用——避免跨用例状态污染造成假绿。"""
    for reg in _ALL:
        reg.clear()


def gc_all() -> Dict[str, int]:
    """统一回收所有表的过期条目，返回 {表名: 回收条数}。"""
    return {reg.name: reg.gc() for reg in _ALL}


def stats() -> Dict[str, int]:
    """各表当前条目数，用于可观测性/内存泄漏排查。"""
    return {reg.name: len(reg) for reg in _ALL}
