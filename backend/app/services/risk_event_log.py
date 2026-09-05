"""
风控事件写入 — 「为什么没开单」的持久化通道（Phase 4）

三条硬约束，任何改动都不得违背：

1. **绝不阻塞交易**。本模块所有函数全异常吞掉、只记日志。
   记录被拦这件事本身，绝不能反过来把开仓/平仓搞挂 —— 那就成了
   「为了看清楚为什么没交易，结果导致不能交易」的荒唐结局。

2. **自带保留窗口**。本系统已经吃过 ai_activities 无界增长的亏（task #294）：
   一张只写不删的表跑上几个月，SQLite 文件涨到几百兆、查询变慢、备份变重。
   新表从第一天就带修剪，不留给"以后再治理"。

3. **写入是采样触发修剪，不是每次都扫表**。每 PRUNE_EVERY 次写入才做一次
   环形修剪，避免把 O(删除) 的成本摊到每一笔拦截上。
"""
from __future__ import annotations

import json
import threading
from typing import Optional, Sequence

from loguru import logger

from app.utils.time_utils import _to_utc_iso  # ★ 2026-08-15 #5 收口：统一时区转换到 app.utils.time_utils

# 每账号保留的事件条数。取 500 的依据：主循环约 30~150s 一轮，
# 极端情况下（每轮都被拦）500 条约等于 4~20 小时的连续拦截记录，
# 足够覆盖"客户早上起来问昨晚为什么没交易"这个核心场景。
MAX_EVENTS_PER_ACCOUNT = 500

# 每 N 次写入触发一次修剪（采样修剪，摊薄成本）
PRUNE_EVERY = 50

_write_counter = 0
_counter_lock = threading.Lock()


# ── 事件码 → 客户能看懂的短标签 ──────────────────────────────
# 为什么标签表放在这里而不是各自的产生处：前端「本周最常见拦截原因」要把
# risk_engine / executor / degrade_gate 三个来源的码放进同一个柱状图，
# 标签必须来自同一张表，否则同一个概念会出现两种叫法。
#
# ★ 防漂移：tests/test_decision_provenance.py 里有两条自检——
#   ① RejectCode 的每个常量都必须在这张表里；
#   ② trade_executor.py 源码里写死的 codes=[...] 字面量也必须在表里。
#   新增码却忘了加标签 → 测试直接红，不会等到客户看见一串英文码才发现。
CODE_LABELS = {
    # risk_engine（六层风控）
    "SPREAD_DATA_UNAVAILABLE": "点差数据不可用",
    "SPREAD_TOO_WIDE": "点差过宽",
    "POSITION_DATA_UNAVAILABLE": "持仓数据不可用",
    "MAX_POSITIONS": "已达最大持仓数",
    "MAX_POSITION_LOTS": "已达最大总手数",
    "SAME_DIRECTION_LIMIT": "同方向持仓上限",
    "DAILY_PNL_DATA_UNAVAILABLE": "当日盈亏数据不可用",
    "DAILY_LOSS_LIMIT": "触及当日亏损上限",
    "EQUITY_DATA_UNAVAILABLE": "净值数据不可用",
    "DRAWDOWN_HALT": "触及回撤熔断",
    "PER_TRADE_RISK_LIMIT": "单笔风险超限",
    "MARKET_CLOSED_WEEKEND": "周末休市",
    "SESSION_DISABLED": "当前时段已关闭",
    # executor（执行层节流）
    "EXECUTOR_MAX_POSITIONS": "已达最大持仓数",
    "EXECUTOR_OPEN_INTERVAL": "同方向开仓冷却中",
    "EXECUTOR_CHURN_COOLDOWN": "刚平仓，抑制立即重开",
    "EXECUTOR_REVERSAL_COOLDOWN": "反手冷却中（方向刚翻转平仓，暂不反手）",
    "LOOKBACK_GUARD_BLOCK": "回顾护栏拦截（历史同点反向打脸模式）",
    # local_proofreader（Phase 9.1 下单前结构闸门）
    "PROOFREAD_STRUCTURAL_MAJOR": "本地校对员拦截结构性缺陷",
    # degrade_gate（平台降级）
    "DEGRADE_L3_CIRCUIT": "平台熔断，暂停新开仓",
    # license_gate（Phase 8 授权）
    # 这几条与上面所有码有本质区别：它们不是「行情/风险」原因，而是**商业状态**。
    # 前端要用不同配色区分，否则客户会以为是行情不好所以不开单，
    # 实际只是该续费了 —— 那是我们自己少收钱。
    "LICENSE_EXPIRED": "授权已到期",
    "LICENSE_UNLICENSED": "未授权 / 试用已结束",
    "LICENSE_MACHINE_MISMATCH": "授权与本机不匹配",
    "LICENSE_SUSPENDED": "授权已被停用",
    "LICENSE_CLOCK_TAMPERED": "系统时间异常",
    "LICENSE_QUOTA_EXCEEDED": "超出账号配额",
    "UNKNOWN": "未分类",
}

# 商业类事件码（前端据此换配色、给出「去续期」入口）。
# 显式列举而不是靠 startswith("LICENSE_") 判断——前缀判断会在某天有人
# 加了个 LICENSE_FEE_RISK 之类的风控码时悄悄归错类。
COMMERCIAL_CODES = frozenset({
    "LICENSE_EXPIRED", "LICENSE_UNLICENSED", "LICENSE_MACHINE_MISMATCH",
    "LICENSE_SUSPENDED", "LICENSE_CLOCK_TAMPERED", "LICENSE_QUOTA_EXCEEDED",
})


def code_label(code: str) -> str:
    """码 → 中文标签。未登记的码原样返回（宁可露出英文码，也不要静默吞掉）。"""
    return CODE_LABELS.get(str(code or ""), str(code or ""))


def _bump_and_should_prune() -> bool:
    global _write_counter
    with _counter_lock:
        _write_counter += 1
        return _write_counter % PRUNE_EVERY == 0


def record_risk_event(
    *,
    user_id: str,
    mt5_account_id: Optional[str] = None,
    event_type: str = "reject",
    stage: str = "risk_engine",
    codes: Optional[Sequence[str]] = None,
    reasons: Optional[Sequence[str]] = None,
    symbol: str = "XAUUSD",
    direction: str = "",
    intended_lots: Optional[float] = None,
    confidence: Optional[float] = None,
    degrade_level: str = "",
    db_writer=None,
) -> bool:
    """记录一条风控事件。返回是否写入成功（失败不抛）。

    db_writer: 可选注入，签名 fn(callable(db)) -> Any。交易执行器有自己的
      `_safe_db_write`（带瞬锁重试、崩溃不阻塞交易），传进来复用即可；
      不传则本模块自行开 session。
    """
    if not user_id:
        return False
    try:
        from app.models.risk_event import RiskEvent

        code_list = [str(c) for c in (codes or []) if c]
        reason_list = [str(r) for r in (reasons or []) if r]

        evt = RiskEvent(
            user_id=str(user_id),
            mt5_account_id=str(mt5_account_id) if mt5_account_id else None,
            event_type=str(event_type or "reject")[:20],
            stage=str(stage or "risk_engine")[:30],
            codes=json.dumps(code_list, ensure_ascii=False) if code_list else None,
            reasons="; ".join(reason_list)[:2000] if reason_list else None,
            symbol=str(symbol or "XAUUSD")[:20],
            direction=str(direction or "").upper()[:10] or None,
            intended_lots=float(intended_lots) if intended_lots is not None else None,
            confidence=float(confidence) if confidence is not None else None,
            degrade_level=str(degrade_level or _degrade_now())[:4] or None,
        )

        if db_writer is not None:
            db_writer(lambda db: db.add(evt))
        else:
            _write_direct(evt)

        if _bump_and_should_prune():
            prune_account_events(mt5_account_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[风控事件] 写入失败（已忽略，不影响交易）: {e}")
        return False


def _write_direct(evt) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.add(evt)
        db.commit()
    finally:
        db.close()


def _degrade_now() -> str:
    try:
        from app.services.platform_health_monitor import current_level

        return str(current_level().name)
    except Exception:  # noqa: BLE001
        return ""


def prune_account_events(mt5_account_id: Optional[str],
                         keep: int = MAX_EVENTS_PER_ACCOUNT) -> int:
    """把某账号的事件裁剪到最近 keep 条，返回删除条数。失败返回 0。

    用「取第 keep+1 条的时间戳，删掉更旧的」而不是 ORDER BY + OFFSET 批删——
    后者在 SQLite 上要走一遍排序再逐行删，前者是一次范围删除。
    """
    if not mt5_account_id:
        return 0
    try:
        from app.database import SessionLocal
        from app.models.risk_event import RiskEvent

        db = SessionLocal()
        try:
            cutoff_row = (
                db.query(RiskEvent.created_at)
                .filter(RiskEvent.mt5_account_id == str(mt5_account_id))
                .order_by(RiskEvent.created_at.desc())
                .offset(keep)
                .limit(1)
                .first()
            )
            if not cutoff_row:
                return 0  # 还没超过保留窗口
            cutoff = cutoff_row[0]
            deleted = (
                db.query(RiskEvent)
                .filter(
                    RiskEvent.mt5_account_id == str(mt5_account_id),
                    RiskEvent.created_at <= cutoff,
                )
                .delete(synchronize_session=False)
            )
            db.commit()
            if deleted:
                logger.debug(f"[风控事件] 修剪账号 {mt5_account_id} 旧事件 {deleted} 条")
            return int(deleted or 0)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[风控事件] 修剪失败（已忽略）: {e}")
        return 0


def query_risk_events(
    *,
    user_id: str,
    mt5_account_id: Optional[str] = None,
    limit: int = 50,
) -> list:
    """查询最近的风控事件（给 API 用）。失败返回空列表。"""
    try:
        from app.database import SessionLocal
        from app.models.risk_event import RiskEvent

        limit = max(1, min(int(limit or 50), 200))
        db = SessionLocal()
        try:
            q = db.query(RiskEvent).filter(RiskEvent.user_id == str(user_id))
            if mt5_account_id:
                q = q.filter(RiskEvent.mt5_account_id == str(mt5_account_id))
            rows = q.order_by(RiskEvent.created_at.desc()).limit(limit).all()
            return [_row_to_dict(r) for r in rows]
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[风控事件] 查询失败: {e}")
        return []


def _row_to_dict(r) -> dict:
    try:
        codes = json.loads(r.codes) if r.codes else []
    except Exception:  # noqa: BLE001
        codes = []
    return {
        "id": r.id,
        "account_id": r.mt5_account_id,
        "event_type": r.event_type,
        "stage": r.stage,
        "codes": codes,
        "reasons": r.reasons or "",
        "symbol": r.symbol,
        "direction": r.direction,
        "intended_lots": r.intended_lots,
        "confidence": r.confidence,
        "degrade_level": r.degrade_level,
        "ts": _to_utc_iso(r.created_at) if r.created_at else None,
    }
