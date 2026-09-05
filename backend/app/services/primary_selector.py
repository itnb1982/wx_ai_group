"""行情主号（行情数据源）统一选择器。

════════════════════════════════════════════════════════════════════════
为什么需要这个文件（2026-08-09 事故复盘）
════════════════════════════════════════════════════════════════════════
线上日志出现每 3 秒一次的
    [MarketAnalyzer] XAUUSD 行情数据不可用，使用模拟数据
根因不是行情接口坏了，而是**行情源选错了账号**：

原逻辑（在 dashboard.py / trading.py / ai_scanner.py 各写了一份）是
    先找 is_market_primary == True，找到就用；找不到才降级到 is_connected。
问题在于 is_market_primary 只是"用户配置的偏好标记"，**不代表这个账号此刻活着**。
本机现场：主号 liumanchun4 status=ERROR / is_connected=0，而 liumanchuan2
是 ONLINE 的。旧逻辑一把抓住那个死主号就不再往下看，于是行情永远取不到，
整个 AI 决策链静默退化到 _get_mock_snapshot() 的随机噪声上。

同一份逻辑被抄了三份，是它能长期存活的原因。这里收敛成单一实现。

════════════════════════════════════════════════════════════════════════
选择顺序（只读降级，绝不写库）
════════════════════════════════════════════════════════════════════════
    ① 主号且 Worker 真实存活      —— 正常路径，尊重用户配置
    ② 任意 Worker 真实存活的账号  —— 主号掉线时的行情续命
    ③ 主号（仅 DB 标记，未存活）  —— 让上层拿到 id 去触发重连
    ④ 该用户任意账号              —— 兜底，至少不是 None

安全性说明：
* 行情是**只读**数据，XAUUSD 报价对同一经纪商下的所有账号一致，
  因此"换个活账号取行情"不会污染任何账号的资金与仓位。
* 本模块**绝不修改** is_market_primary 字段。那是用户配置，
  掉线是临时状态，不能因为一次掉线就把用户的主号设置改掉。
* 多租户：user_id 为空时才做全局查询（仅供无用户上下文的后台线程用）。
"""

from typing import Optional

from loguru import logger

from app.models.mt5_account import MT5Account


def _alive_ids() -> set:
    """取 Worker 进程真实存活的账号集合；任何异常都退化成空集（走 DB 字段判断）。"""
    try:
        from app.services.mt5_service import mt5_service

        return mt5_service.alive_account_ids()
    except Exception:
        return set()


def pick_market_primary(db, user_id: Optional[str] = None) -> Optional[MT5Account]:
    """选出当前最适合当行情数据源的账号，返回 ORM 对象（可能为 None）。

    Args:
        db: SQLAlchemy Session（由调用方负责生命周期）
        user_id: 多租户隔离用；None 表示全局（仅限无用户上下文的后台线程）
    """
    try:
        q = db.query(MT5Account)
        if user_id:
            q = q.filter(MT5Account.user_id == user_id)
        accounts = q.all()
    except Exception as e:
        logger.warning(f"[PrimarySelector] 查询账号失败: {e}")
        return None

    if not accounts:
        return None

    alive = _alive_ids()

    def _is_live(a) -> bool:
        # Worker 存活是硬证据；拿不到进程信息时（alive 为空集）退回 DB 字段。
        if alive:
            return a.id in alive
        return bool(a.is_connected)

    primary = next((a for a in accounts if a.is_market_primary), None)

    # ① 主号活着 —— 最理想
    if primary is not None and _is_live(primary):
        return primary

    # ② 主号不可用，换一个活的顶上，保证 AI 拿到真实行情
    live = next((a for a in accounts if _is_live(a)), None)
    if live is not None:
        if primary is not None:
            logger.warning(
                f"[PrimarySelector] 行情主号 {primary.name} 不可用，"
                f"临时改用 {live.name} 取行情（不修改主号配置）"
            )
        return live

    # ③ 全都不活：返回主号，让上层拿着 id 去触发重连
    if primary is not None:
        return primary

    # ④ 兜底
    return accounts[0]


def pick_market_primary_id(db, user_id: Optional[str] = None) -> str:
    """同 pick_market_primary，只要 ID；无账号时返回空串（保持旧调用方语义）。"""
    acc = pick_market_primary(db, user_id)
    return acc.id if acc else ""
