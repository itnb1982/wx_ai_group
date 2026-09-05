"""
万象Ai — 账号执行车道基础设施（Phase 3 / V6 第四层：执行控制层）

解决三件多租户真实问题，全部围绕铁律「账号数 N 是变量（1 ~ 50+）」：

1) 线程池膨胀
   原实现每个主周期 `ThreadPoolExecutor(max_workers=len(independents))` 现建现销。
   N=50 时每轮创建/销毁 50 条线程 = 线程风暴 + 上下文切换开销，且 MT5 IPC
   本身是瓶颈，超发线程只会排队。改为**进程级单例有界池**，上界与客户数解耦。

2) 同秒挤单（执行层错峰）
   N 个客户共用同一 XAUUSD 信号源 → 同一秒对同一品种打市价单，
   造成滑点递增、且容易被经纪商识别为「同一策略群」而做针对性处理。
   方案：**随机抖动（jitter）而非排队（queue）**。
   - 排队会把第 N 个客户拖后数十秒 → 违背「多交易多赚钱」（漏单/劣价）
   - 抖动只把下单时刻打散在一个很小的窗口内，期望延迟 = 窗口/2，
     且 N 越大窗口封顶不再增长（最坏 800ms）
   - N=1（单客户部署）**零延迟**，绝不为了并发防护惩罚单客户

3) 滑点归因缺失
   已有 execution_telemetry 是**经纪商级全局**统计，回答不了
   「滑点变差是行情原因还是我们自己挤单？」。这里补**并发上下文归因**：
   记录每笔成交时的并发账号数与实际抖动，按并发档位分组统计平均滑点。
   有了它才能判断错峰窗口该调大还是调小——而不是拍脑袋定参数。

设计红线：
  - 本模块零业务依赖（不 import services / models），可被任何层安全引用
  - 任何异常都不得外抛到交易主链路（错峰失败 = 不抖动，绝不阻断下单）
  - 所有容器有界（deque maxlen），进程长跑不泄漏
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ──────────────────────────────────────────────────────────────
# 1) 有界账号线程池
# ──────────────────────────────────────────────────────────────

#: 硬上界。即便 100 个客户也不会开 100 条下单线程——MT5 IPC 是串行瓶颈，
#: 超过这个数只是徒增争抢。可用环境变量 WX_LANE_MAX_WORKERS 覆盖。
_HARD_CAP = 32
#: 最小工作线程数（即便账号很少也保底这么多，避免冷启动延迟）。
#: 魔法常量解除：可用环境变量 WX_LANE_MIN_WORKERS 覆盖。
_MIN_WORKERS = int(os.environ.get("WX_LANE_MIN_WORKERS", "4") or "4")


def _default_max_workers() -> int:
    env = (os.environ.get("WX_LANE_MAX_WORKERS") or "").strip()
    if env:
        try:
            v = int(env)
            if v > 0:
                return min(v, _HARD_CAP)
        except ValueError:
            pass
    try:
        cpu = os.cpu_count() or 4
    except Exception:
        cpu = 4
    return max(_MIN_WORKERS, min(_HARD_CAP, cpu * 2))


class LanePool:
    """进程级单例有界线程池：账号并发执行的唯一入口。

    与直接用 ThreadPoolExecutor 的区别：
      - 复用：线程创建一次，跨周期常驻，不再每轮新建销毁
      - 有界：worker 数与客户数 N 解耦，N 涨不会打爆机器
      - 隔离：单个账号任务抛异常不影响其它账号（结果里带 error 字段）
    """

    def __init__(self, max_workers: Optional[int] = None, thread_name_prefix: str = "wx-lane"):
        self._max_workers = int(max_workers or _default_max_workers())
        self._prefix = thread_name_prefix
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def _ensure(self) -> ThreadPoolExecutor:
        # 懒创建：进程启动时不占线程，第一次真有并发需求才拉起
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix=self._prefix,
                    )
        return self._pool

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        return self._ensure().submit(fn, *args, **kwargs)

    def map_accounts(
        self,
        fn: Callable[[Any], Any],
        items: Sequence[Any],
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """对每个账号项并发执行 fn，返回与输入等长、顺序一致的结果列表。

        每项结果形如 ``{"item": 原项, "ok": bool, "result": 返回值, "error": 字符串}``。
        **绝不抛异常**：单账号失败只体现在该项 ok=False，不影响其它租户。
        """
        items = list(items)
        if not items:
            return []
        if len(items) == 1:
            # 单账号无需过池，省一次调度（N=1 部署是常态，不该有额外开销）
            it = items[0]
            try:
                return [{"item": it, "ok": True, "result": fn(it), "error": None}]
            except Exception as e:  # noqa: BLE001
                return [{"item": it, "ok": False, "result": None, "error": str(e)}]

        pool = self._ensure()
        futs: List[Tuple[Any, Future]] = [(it, pool.submit(fn, it)) for it in items]
        out: List[Dict[str, Any]] = []
        for it, f in futs:
            try:
                out.append({"item": it, "ok": True, "result": f.result(timeout=timeout), "error": None})
            except Exception as e:  # noqa: BLE001
                out.append({"item": it, "ok": False, "result": None, "error": str(e)})
        return out

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.shutdown(wait=wait)
                finally:
                    self._pool = None


#: 命名池注册表。**必须分层命名，不能共用一个池**：
#:   用户级任务(user) 内部会派发账号级任务(account)。若二者共用同一个池，
#:   当所有 worker 都被用户级任务占满、而它们又在等待账号级子任务完成时，
#:   子任务永远拿不到 worker → **线程池嵌套死锁**，整个交易引擎假死。
#:   分成两个池后依赖是单向的（user → account，account 从不反向等待 user），
#:   结构上就不可能成环。
_LANE_POOLS: Dict[str, LanePool] = {}
_LANE_POOL_LOCK = threading.Lock()

#: 各命名池的 worker 上界。用户级不宜过大（每个用户任务还会再挂一条超时守护线程）。
_POOL_LIMITS: Dict[str, int] = {
    "user": 16,
    "account": 32,
}


def get_lane_pool(name: str = "account") -> LanePool:
    """取得命名车道池（进程级单例，按 name 隔离）。

    - ``"account"``：账号级并发（独立账号各跑自身 AI 周期）
    - ``"user"``：用户级并发（每个客户一条主周期）
    """
    key = str(name or "account")
    pool = _LANE_POOLS.get(key)
    if pool is None:
        with _LANE_POOL_LOCK:
            pool = _LANE_POOLS.get(key)
            if pool is None:
                cap = _POOL_LIMITS.get(key)
                default = _default_max_workers()
                pool = LanePool(
                    max_workers=min(default, cap) if cap else default,
                    thread_name_prefix=f"wx-lane-{key}",
                )
                _LANE_POOLS[key] = pool
    return pool


def reset_lane_pool(name: Optional[str] = None) -> None:
    """仅供测试/热重载：释放命名池（name=None 释放全部）。"""
    with _LANE_POOL_LOCK:
        keys = [str(name)] if name else list(_LANE_POOLS.keys())
        for k in keys:
            p = _LANE_POOLS.pop(k, None)
            if p is not None:
                p.shutdown(wait=False)


# ──────────────────────────────────────────────────────────────
# 2) 下单错峰（jitter，不是 queue）
# ──────────────────────────────────────────────────────────────

#: 抖动窗口下界/上界（毫秒）。黄金一个 tick 常在数十毫秒级，
#: 800ms 上界是「打散足够、代价可忽略」的折中；再大就开始影响成交价了。
JITTER_MIN_WINDOW_MS = 200.0
JITTER_MAX_WINDOW_MS = 800.0
#: 每多一个并发账号，窗口增加的毫秒数
JITTER_PER_ACCOUNT_MS = 80.0


def compute_jitter_ms(concurrent_n: int, rng: Optional[random.Random] = None) -> float:
    """按当前并发账号数计算本次下单应等待的随机抖动（毫秒）。

    - ``concurrent_n <= 1`` → 返回 0.0（单客户部署零延迟，绝不无谓惩罚）
    - 窗口 = clamp(n * 80ms, 200ms, 800ms)，在 [0, 窗口] 上均匀取值
    - N 越大窗口封顶不再增长：N=50 时窗口 800ms，期望间隔 ≈16ms，
      既打散了同秒挤单，又不会把最后一个客户拖到几十秒后（那才是真亏钱）
    """
    try:
        n = int(concurrent_n)
    except (TypeError, ValueError):
        return 0.0
    if n <= 1:
        return 0.0
    window = min(JITTER_MAX_WINDOW_MS, max(JITTER_MIN_WINDOW_MS, n * JITTER_PER_ACCOUNT_MS))
    r = rng or random
    return float(r.uniform(0.0, window))


def apply_order_jitter(
    concurrent_n: int,
    sleeper: Optional[Callable[[float], None]] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """执行错峰等待，返回实际抖动毫秒数。

    异常安全：任何问题都返回 0.0 并直接放行下单——**错峰是优化，不是闸门**。
    """
    try:
        ms = compute_jitter_ms(concurrent_n, rng=rng)
        if ms <= 0:
            return 0.0
        (sleeper or time.sleep)(ms / 1000.0)
        return ms
    except Exception:  # noqa: BLE001
        return 0.0


# ──────────────────────────────────────────────────────────────
# 3) 并发滑点归因
# ──────────────────────────────────────────────────────────────

#: XAUUSD：1 pip = 0.1
_PIP = 0.1


@dataclass
class FillSample:
    account_id: Any
    side: str
    requested: float
    filled: float
    pip: float
    concurrent_n: int
    jitter_ms: float
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "side": self.side,
            "req": round(self.requested, 3),
            "fill": round(self.filled, 3),
            "pip": round(self.pip, 3),
            "n": self.concurrent_n,
            "jitter_ms": round(self.jitter_ms, 1),
            "ts": self.ts,
        }


class ConcurrencyAttribution:
    """把滑点归因到「并发挤单」还是「行情本身」。

    核心产出：``summary()["by_bucket"]`` —— 按并发档位（1 / 2-3 / 4-7 / 8+）
    分组的平均滑点。若高并发档显著劣于低并发档，说明我们自己在挤单，
    应当调大抖动窗口；若各档持平，说明滑点来自行情，加抖动是白加。
    **参数据此调整，而不是拍脑袋。**
    """

    _BUCKETS: Tuple[Tuple[str, int, int], ...] = (
        ("n=1", 1, 1),
        ("n=2-3", 2, 3),
        ("n=4-7", 4, 7),
        ("n>=8", 8, 10 ** 9),
    )

    def __init__(self, cap: int = 400):
        self._samples: deque = deque(maxlen=cap)
        self._lock = threading.Lock()

    def record(
        self,
        account_id: Any,
        side: str,
        requested: float,
        filled: float,
        concurrent_n: int = 1,
        jitter_ms: float = 0.0,
    ) -> Optional[FillSample]:
        """记录一笔成交。脏数据静默丢弃，绝不阻断交易链路。"""
        try:
            req = float(requested)
            fill = float(filled)
        except (TypeError, ValueError):
            return None
        if req <= 0 or fill <= 0:
            return None
        try:
            n = max(1, int(concurrent_n))
        except (TypeError, ValueError):
            n = 1
        try:
            j = max(0.0, float(jitter_ms))
        except (TypeError, ValueError):
            j = 0.0
        s = FillSample(
            account_id=account_id,
            side=str(side or "").upper(),
            requested=req,
            filled=fill,
            pip=abs(fill - req) / _PIP,
            concurrent_n=n,
            jitter_ms=j,
        )
        with self._lock:
            self._samples.append(s)
        return s

    @staticmethod
    def _bucket_of(n: int) -> str:
        for name, lo, hi in ConcurrencyAttribution._BUCKETS:
            if lo <= n <= hi:
                return name
        return "n>=8"

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"available": False, "count": 0, "by_bucket": {}, "by_account": {}}

        by_bucket: Dict[str, List[float]] = {}
        by_account: Dict[Any, List[float]] = {}
        for s in samples:
            by_bucket.setdefault(self._bucket_of(s.concurrent_n), []).append(s.pip)
            by_account.setdefault(s.account_id, []).append(s.pip)

        def _agg(v: List[float]) -> Dict[str, Any]:
            return {
                "count": len(v),
                "avg_pip": round(sum(v) / len(v), 3),
                "max_pip": round(max(v), 3),
            }

        pips = [s.pip for s in samples]
        bucket_stats = {k: _agg(v) for k, v in by_bucket.items()}
        return {
            "available": True,
            "count": len(samples),
            "avg_pip": round(sum(pips) / len(pips), 3),
            "max_pip": round(max(pips), 3),
            "by_bucket": bucket_stats,
            "by_account": {k: _agg(v) for k, v in by_account.items()},
            "crowding_suspected": self._crowding_suspected(bucket_stats),
        }

    @staticmethod
    def _crowding_suspected(bucket_stats: Dict[str, Dict[str, Any]]) -> bool:
        """高并发档平均滑点 ≥ 低并发档 1.5 倍且样本足够 → 判定存在挤单。"""
        low = bucket_stats.get("n=1")
        highs = [bucket_stats.get("n=4-7"), bucket_stats.get("n>=8")]
        if not low or low["count"] < 5:
            return False
        base = low["avg_pip"]
        if base <= 0:
            return False
        for h in highs:
            if h and h["count"] >= 5 and h["avg_pip"] >= base * 1.5:
                return True
        return False

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


_ATTRIBUTION: Optional[ConcurrencyAttribution] = None
_ATTRIBUTION_LOCK = threading.Lock()


def get_attribution() -> ConcurrencyAttribution:
    global _ATTRIBUTION
    if _ATTRIBUTION is None:
        with _ATTRIBUTION_LOCK:
            if _ATTRIBUTION is None:
                _ATTRIBUTION = ConcurrencyAttribution()
    return _ATTRIBUTION


def record_fill(
    account_id: Any,
    side: str,
    requested: float,
    filled: float,
    concurrent_n: int = 1,
    jitter_ms: float = 0.0,
) -> Optional[FillSample]:
    """模块级便捷入口。异常自吞，调用点无需 try。"""
    try:
        return get_attribution().record(
            account_id, side, requested, filled, concurrent_n, jitter_ms
        )
    except Exception:  # noqa: BLE001
        return None


# ──────────────────────────────────────────────────────────────
# 4) 并发规模广播（供下单点得知「此刻有几个账号在打单」）
# ──────────────────────────────────────────────────────────────


class ConcurrencyGauge:
    """当前活跃下单账号数的轻量计数器。

    下单点不知道全局有多少账号在跑；编排层知道。编排层在派发前
    ``set_active(n)``，下单点 ``current()`` 取值算抖动窗口。
    用简单赋值而非 in/out 计数，是因为「本轮有几个账号要打单」在派发时
    已完全确定，比引用计数更准也更不易泄漏。
    """

    def __init__(self):
        self._n = 1
        self._lock = threading.Lock()

    def set_active(self, n: int) -> int:
        try:
            v = max(1, int(n))
        except (TypeError, ValueError):
            v = 1
        with self._lock:
            self._n = v
        return v

    def current(self) -> int:
        with self._lock:
            return self._n


_GAUGE = ConcurrencyGauge()


def set_active_accounts(n: int) -> int:
    return _GAUGE.set_active(n)


def active_accounts() -> int:
    return _GAUGE.current()


__all__ = [
    "LanePool",
    "get_lane_pool",
    "reset_lane_pool",
    "compute_jitter_ms",
    "apply_order_jitter",
    "JITTER_MIN_WINDOW_MS",
    "JITTER_MAX_WINDOW_MS",
    "FillSample",
    "ConcurrencyAttribution",
    "get_attribution",
    "record_fill",
    "ConcurrencyGauge",
    "set_active_accounts",
    "active_accounts",
]
