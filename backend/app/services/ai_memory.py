"""
AI 工作剧场 — 进程内存环形缓冲（实时数据源保底）

为何存在：
  Windows Defender 实时扫描会对被写入的 SQLite 文件异步加只读锁，
  导致 ai_activities / evolution_logs 的落库间歇性失败（"attempt to write a readonly database"）。
  为保证「进化时间线 / 交易执行流」在任何环境下都实时鲜活，这里用进程内存
  deque 作为保底数据源：扫描器每产生一个真实事件都先推入内存（永远可写），
  dashboard 优先读内存，DB 仅作为可选的历史持久化兜底。

缓冲设计（2026-08-05 修复）：
  _activity_buf  → 扫描/信号/评估事件（scan/signal/evaluate），maxlen=400
  _trade_buf     → 真实交易动作（open/close/close_partial/sl），maxlen=200，独立于扫描
  _evolution_buf → 进化事件（权重更新/订单复盘/体制切换），maxlen=200

注意：内存数据随进程重启清空，属于「演示/实时」层；持久化由 DB 负责（Defender 解锁后自动生效）。
"""
from threading import Lock
from collections import deque
from datetime import datetime, timezone

# 实时活动流（扫描/信号/评估…），最多保留 400 条
_activity_buf: deque = deque(maxlen=400)
# 真实交易动作流（开仓/平仓/止损/部分平），独立缓冲不被扫描淹没，最多保留 200 条
_trade_buf: deque = deque(maxlen=200)
# 进化事件（初始化/权重更新/体制切换…），最多保留 200 条
_evolution_buf: deque = deque(maxlen=200)
_lock = Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def push_activity(item: dict) -> None:
    """推送一条 AI 活动事件（扫描器调用）。item 至少含 kind/symbol/direction 等字段。"""
    item = dict(item)
    item.setdefault("ts", _now_iso())
    with _lock:
        _activity_buf.append(item)


def push_trade_event(kind: str, detail: str, *, account_id: str = "",
                     account_name: str = "", account_login: str = "",
                     direction: str = "", confidence: float = 0.0,
                     pnl: float = 0.0, ticket: str = "",
                     open_price: float = 0.0, close_price: float = 0.0,
                     reason: str = "", extra: dict = None) -> None:
    """推送一条交易事件（开仓/平仓/止损/部分平仓）到独立交易缓冲。
    比 push_activity 多了账户信息，供前端「交易执行流」显示仓位来源。
    写入 _trade_buf（独立于 _activity_buf），不会被扫描事件淹没。

    ★ 2026-08-05 增强：补充 pnl/open_price/close_price/ticket/reason 结构化字段，
      供 AI 开仓决策注入「最近真实盈亏」使用（内存缓冲永远可写，
      不依赖被 Defender 锁死的 SQLite，根治 AI 越跑越笨）。
    """
    item = {
        "ts": _now_iso(),
        "kind": kind,
        "symbol": "XAUUSD",
        "direction": direction,
        "confidence": confidence,
        "detail": detail,
        "account_id": account_id,
        "account_name": account_name,
        "account_login": account_login,
        "pnl": round(float(pnl or 0), 2),
        "ticket": str(ticket or ""),
        "open_price": round(float(open_price or 0), 2),
        "close_price": round(float(close_price or 0), 2),
        "reason": str(reason or ""),
        **(extra or {}),
    }
    with _lock:
        _trade_buf.append(item)


def push_evolution(item: dict) -> None:
    """推送一条进化事件（初始化 / 权重更新等）。"""
    item = dict(item)
    item.setdefault("ts", _now_iso())
    with _lock:
        _evolution_buf.append(item)


def get_activities(limit: int = 50) -> list:
    with _lock:
        return list(_activity_buf)[-limit:]


def get_evolution(limit: int = 50) -> list:
    with _lock:
        return list(_evolution_buf)[-limit:]


def get_trades(limit: int = 50) -> list:
    """获取交易执行流（仅 open/close/close_partial/sl），独立缓冲不被扫描淹没。"""
    with _lock:
        return list(_trade_buf)[-limit:]


def count_activities_since(iso_ts: str) -> int:
    """统计 iso_ts 之后的活动数量（用于今日决策/扫描计数）。"""
    with _lock:
        return sum(1 for a in _activity_buf if a.get("ts", "") >= iso_ts)


def count_by_kinds(iso_ts: str, kinds: tuple) -> int:
    with _lock:
        return sum(
            1 for a in _activity_buf
            if a.get("ts", "") >= iso_ts and a.get("kind") in kinds
        )


def count_evolution() -> int:
    with _lock:
        return len(_evolution_buf)
