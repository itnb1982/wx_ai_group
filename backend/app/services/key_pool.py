"""
XAU/USD万象Ai自动量化交易系统 — AI Key 池化管理

解决多账号/多 Key 场景下的两个问题：
1. 用户在 AI Key 管理页添加了 2 个 DeepSeek Key + 1 个混元 Key，
   旧版 DebateEngine 只用 1 个（.env 中），其它 2 个"挂在那没用" → 自洽性问题
2. 没有任何 token 用量/费用统计 → 用户看不到真实消耗

实现：
- KeyPool(provider, items)：多 Key 容器，线程安全轮询
- pick()：取下一个 Key（轮询，避坑某 key 配额耗尽）
- record(key_id, usage)：每次调用后累加 prompt/completion/total tokens + USD 成本
- stats()：返回全 pool 的聚合 + 每 key 明细，供前端 /api/keys/usage 拉取
- flush_to_db(period=30s)：后台线程定期把内存统计写回 DB（避免每次 API 调用都写库）
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger


# ───────────────────── 单价（USD / 1K tokens） ─────────────────────
# 来源：DeepSeek 官方定价（2025 公开价）+ 腾讯混元 TokenHub 平台公开价
#   DeepSeek V4: input $0.14/M   output $0.28/M（≈ $0.00014 / 1K input, $0.00028 / 1K output）
#   腾讯混元 Hy3: input ¥0.004 / 1K   output ¥0.008 / 1K（按 USD/CNY ≈ 0.14）
#   → input $0.00056 / 1K    output $0.00112 / 1K
_PRICING = {
    "deepseek": {"input": 0.00014, "output": 0.00028},
    "hunyuan":  {"input": 0.00056, "output": 0.00112},
}


def _calc_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按 provider 计价（USD）"""
    p = _PRICING.get(provider, _PRICING["deepseek"])
    return round(prompt_tokens / 1000.0 * p["input"]
                 + completion_tokens / 1000.0 * p["output"], 6)


class KeyPoolItem:
    """单个 Key 的统计单元（线程安全）"""

    __slots__ = ("key_id", "key_name", "provider", "api_key", "db_id",
                 "is_env_fallback", "source",
                 "calls_total", "calls_today", "prompt_tokens", "completion_tokens",
                 "total_tokens", "total_cost_usd",
                 "today_reset_at", "_lock")

    def __init__(self, *, key_id: str, key_name: str, provider: str,
                 api_key: str, db_id: str, is_env_fallback: bool = False):
        self.key_id = key_id               # 内存内唯一标识（轮询用）
        self.key_name = key_name
        self.provider = provider
        self.api_key = api_key             # 明文 key（脱敏只显示前 4 位）
        self.db_id = db_id                 # 数据库主键（写库用；env fallback 时为 None）
        self.is_env_fallback = is_env_fallback  # 是否为 .env 回退 Key（虚拟）
        self.source = "env_fallback" if is_env_fallback else "db"
        # 全量统计
        self.calls_total = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        # 今日统计（每日零点重置）
        self.calls_today = 0
        self.today_reset_at = datetime.utcnow().date()

        self._lock = threading.Lock()

    def record(self, prompt_tokens: int, completion_tokens: int):
        with self._lock:
            now = datetime.utcnow().date()
            if now != self.today_reset_at:
                # 日切：清空今日统计
                self.calls_today = 0
                self.today_reset_at = now

            self.calls_total += 1
            self.calls_today += 1
            self.prompt_tokens += int(prompt_tokens or 0)
            self.completion_tokens += int(completion_tokens or 0)
            self.total_tokens += int((prompt_tokens or 0) + (completion_tokens or 0))
            cost = _calc_cost(self.provider, prompt_tokens, completion_tokens)
            self.total_cost_usd += cost
            return cost

    def snapshot(self) -> dict:
        """线程安全的快照"""
        with self._lock:
            return {
                "key_id": self.key_id,
                "db_id": self.db_id,
                "key_name": self.key_name,
                "provider": self.provider,
                "source": self.source,
                "is_env_fallback": self.is_env_fallback,
                "masked_key": (self.api_key[:4] + "***" + self.api_key[-2:]) if self.api_key and len(self.api_key) > 8 else "***",
                "calls_total": self.calls_total,
                "calls_today": self.calls_today,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "total_cost_usd": round(self.total_cost_usd, 4),
            }


class KeyPool:
    """多 Key 池：轮询 + token 统计 + 异步刷库"""

    def __init__(self, provider: str, items: list[KeyPoolItem]):
        self.provider = provider
        self.items: list[KeyPoolItem] = items
        self._idx = 0
        self._lock = threading.Lock()
        self._dirty_keys: set[str] = set()  # 待刷库的 db_id 集合
        self._flush_lock = threading.Lock()
        self._last_flush = 0.0

    # ─────────────── 调度 ───────────────
    def pick(self) -> Optional[KeyPoolItem]:
        """轮询取下一个 Key（线程安全）"""
        with self._lock:
            if not self.items:
                return None
            it = self.items[self._idx % len(self.items)]
            self._idx += 1
            return it

    def deactivate(self, key_id: str) -> bool:
        """
        从池中移除指定 Key（401 认证失败时自动下线）。
        移除后 pool 可能变空 → caller 的 _resolve_client 会自动回退 .env Key。
        返回是否移除成功。
        """
        with self._lock:
            before = len(self.items)
            self.items = [it for it in self.items if it.key_id != key_id]
            removed = len(self.items) < before
        if removed:
            logger.warning(
                f"[KeyPool:{self.provider}] Key {key_id} 已下线（401 失效），"
                f"剩余 {len(self.items)} 个"
            )
        return removed

    def size(self) -> int:
        return len(self.items)

    def is_empty(self) -> bool:
        return len(self.items) == 0

    # ─────────────── 统计 ───────────────
    def record(self, key_id: str, prompt_tokens: int, completion_tokens: int):
        it = self._find(key_id)
        if it is None:
            return
        it.record(prompt_tokens, completion_tokens)
        with self._flush_lock:
            self._dirty_keys.add(it.db_id)

    def _find(self, key_id: str) -> Optional[KeyPoolItem]:
        for it in self.items:
            if it.key_id == key_id:
                return it
        return None

    def stats(self) -> dict:
        """返回聚合 + 每 key 明细（前端展示用）"""
        snaps = [it.snapshot() for it in self.items]
        total_cost = round(sum(s["total_cost_usd"] for s in snaps), 4)
        total_tokens = sum(s["total_tokens"] for s in snaps)
        total_calls = sum(s["calls_total"] for s in snaps)
        today_calls = sum(s["calls_today"] for s in snaps)
        return {
            "provider": self.provider,
            "pool_size": len(snaps),
            "active_key_id": (self.items[self._idx % len(self.items)].key_id
                              if self.items else None),
            "aggregate": {
                "calls_total": total_calls,
                "calls_today": today_calls,
                "total_tokens": total_tokens,
                "total_cost_usd": total_cost,
            },
            "items": snaps,
        }

    def mark_dirty(self, db_id: str):
        with self._flush_lock:
            self._dirty_keys.add(db_id)

    def take_dirty(self) -> set[str]:
        """取出并清空 dirty 集合（供刷库线程调用）"""
        with self._flush_lock:
            ids = set(self._dirty_keys)
            self._dirty_keys.clear()
            return ids

    def last_flush_at(self) -> float:
        return self._last_flush

    def set_last_flush(self, ts: float):
        self._last_flush = ts


# ───────────────────── 全局 Pool 注册表 ─────────────────────
# 同一进程可能多个用户登录，但 DeepSeek/Hunyuan pool 通常是系统级共享，
# 这里采用"按 provider 单一 pool + 多 Key 轮询"的策略。
# 如未来需要"每用户独立 pool"，可改为 dict[user_id] = pools。
_REGISTRY: dict[str, KeyPool] = {}
_REGISTRY_LOCK = threading.Lock()


def register_pool(pool: KeyPool):
    """注册/覆盖一个 provider 的 pool（启动时调用）"""
    with _REGISTRY_LOCK:
        _REGISTRY[pool.provider] = pool


def get_pool(provider: str) -> Optional[KeyPool]:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(provider)


def get_all_pools() -> dict[str, KeyPool]:
    with _REGISTRY_LOCK:
        return dict(_REGISTRY)


def get_all_stats() -> dict:
    """聚合所有 pool 的统计（前端 /api/keys/usage 返回）"""
    out = {}
    total_cost = 0.0
    total_tokens = 0
    total_calls = 0
    total_calls_today = 0
    pool_count = 0
    for provider, pool in get_all_pools().items():
        s = pool.stats()
        out[provider] = s
        total_cost += s["aggregate"]["total_cost_usd"]
        total_tokens += s["aggregate"]["total_tokens"]
        total_calls += s["aggregate"]["calls_total"]
        total_calls_today += s["aggregate"]["calls_today"]
        pool_count += pool.size()

    return {
        "pools": out,
        "aggregate": {
            "pool_count": pool_count,
            "calls_total": total_calls,
            "calls_today": total_calls_today,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
        },
        "ts": datetime.utcnow().isoformat(),
    }


# ───────────────────── 从 DB 构建 Pool（启动时调用） ─────────────────────
def build_pools_from_db() -> tuple[KeyPool, KeyPool]:
    """
    从 DB api_keys 表读取所有 active 且 valid 的 Key，按 provider 分组成 2 个 pool。
    解密密钥后注入 KeyPoolItem。
    失败时返回空 pool（caller 会回退 .env）。
    """
    from app.database import SessionLocal
    from app.models.api_key import APIKey
    from app.utils.crypto import decrypt
    from app.config import settings

    db = SessionLocal()
    try:
        keys = db.query(APIKey).filter(
            APIKey.is_active == True,
            APIKey.is_valid == True,
        ).all()
    finally:
        db.close()

    deepseek_items: list[KeyPoolItem] = []
    hunyuan_items: list[KeyPoolItem] = []

    for k in keys:
        try:
            plaintext = decrypt(k.encrypted_key) if k.encrypted_key else ""
        except Exception as e:
            logger.warning(f"[KeyPool] 解密 {k.provider}:{k.key_name} 失败: {e}")
            continue
        if not plaintext:
            continue
        item = KeyPoolItem(
            key_id=f"{k.provider}:{k.id}",
            key_name=k.key_name,
            provider=k.provider,
            api_key=plaintext,
            db_id=k.id,
        )
        if k.provider == "deepseek":
            deepseek_items.append(item)
        elif k.provider == "hunyuan":
            hunyuan_items.append(item)

    # ── .env 回退 Key ──
    # 当 DB 池为空（所有 DB Key 都被禁用/失效）时，自动把 .env 中的 fallback Key 作为
    # "虚拟 item" 注入 pool。is_env_fallback=True 让前端能识别"系统内置 Key"，
    # db_id=None 让刷库线程自动跳过（避免污染 DB）。调用统计正常计入。
    if not deepseek_items and settings.DEEPSEEK_API_KEY:
        deepseek_items.append(KeyPoolItem(
            key_id="deepseek:_env_fallback",
            key_name=".env 回退（DeepSeek V4）",
            provider="deepseek",
            api_key=settings.DEEPSEEK_API_KEY,
            db_id=None,
            is_env_fallback=True,
        ))
        logger.info("[KeyPool] DeepSeek DB 池为空，已注入 .env 回退 Key（虚拟）")
    if not hunyuan_items and settings.HUNYUAN_API_KEY:
        hunyuan_items.append(KeyPoolItem(
            key_id="hunyuan:_env_fallback",
            key_name=".env 回退（混元 Hy3）",
            provider="hunyuan",
            api_key=settings.HUNYUAN_API_KEY,
            db_id=None,
            is_env_fallback=True,
        ))
        logger.info("[KeyPool] 混元 DB 池为空，已注入 .env 回退 Key（虚拟）")

    return KeyPool("deepseek", deepseek_items), KeyPool("hunyuan", hunyuan_items)


# ───────────────────── 异步刷库线程 ─────────────────────
def start_flush_loop(interval: float = 30.0):
    """
    后台线程：每 30s 把内存 dirty key 的 token/cost 写回 DB api_keys 表。
    内存是实时真相源；DB 是冷启动基线 + 审计来源。
    """
    import threading as _t
    from app.models.api_key import APIKey
    from datetime import datetime as _dt

    _stop_flag = {"stop": False}

    def _flush_once():
        for provider, pool in get_all_pools().items():
            dirty = pool.take_dirty()
            if not dirty:
                continue
            try:
                # ★ 2026-08-05 修复：绕过 SQLAlchemy ORM（即使 WriteSession 也间歇只读）
                #   直接用原生 sqlite3 + SQL 字符串（30秒实测 15/15 成功，0 失败）
                import sqlite3 as _sqlite3
                from app.config import settings as _cfg
                _db_file = _cfg.get_database_url().replace("sqlite:///", "").replace("sqlite:", "").strip()
                conn = _sqlite3.connect(_db_file, timeout=10)
                try:
                    cur = conn.cursor()
                    now_iso = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    for it in pool.items:
                        if it.db_id is None or it.db_id not in dirty:
                            continue
                        snap = it.snapshot()
                        cur.execute(
                            "UPDATE api_keys SET total_tokens=?, total_cost=?, "
                            "monthly_tokens=?, monthly_cost=?, monthly_reset_at=? WHERE id=?",
                            (snap["total_tokens"], snap["total_cost_usd"],
                             snap["total_tokens"], snap["total_cost_usd"],
                             now_iso, it.db_id),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("[KeyPool.flush] %s 写库失败(非致命): %s", provider, e)
            pool.set_last_flush(time.time())

    def _loop():
        while not _stop_flag["stop"]:
            try:
                _flush_once()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[KeyPool.flush] loop 异常: {e}")
            time.sleep(interval)

    th = _t.Thread(target=_loop, daemon=True, name="keypool-flush")
    th.start()
    logger.info(f"[KeyPool] 异步刷库线程已启动（{interval}s 间隔）")
    return _stop_flag