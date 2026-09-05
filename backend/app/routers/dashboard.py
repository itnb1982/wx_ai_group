"""
仪表盘路由 — 实时状态 + WebSocket 推送 + AI 决策
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.orm import Session
from typing import Dict
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from app.database import get_db, SessionLocal
from app.models.user import User
from app.models.mt5_account import MT5Account
from app.models.trade import Trade
from app.models.evolution_log import EvolutionLog
from app.models.ai_activity import AIActivity
from app.services.mt5_service import mt5_service
from app.services.primary_selector import pick_market_primary, pick_market_primary_id
from app.services.daily_baseline import today_profit as _bl_today_profit
from app.services.ai_memory import (
    get_evolution as mem_get_evolution,
    get_activities as mem_get_activities,
    get_trades as mem_get_trades,
    count_evolution as mem_count_evolution,
    count_activities_since as mem_count_activities_since,
    count_by_kinds as mem_count_by_kinds,
)
from app.routers.auth import get_current_user
from app.core.debate_engine import DebateEngine
from app.services.market_session import get_session_state, local_to_server
from app.services.indicators import compute_indicators
from app.services.market_data import market_data_provider
# Phase 4 决策溯源：接口层与落库层共用同一个序列化函数，字段名以它为准。
from app.services.decision_snapshot import build_decision_snapshot
from app.services.risk_event_log import query_risk_events, _to_utc_iso
from loguru import logger

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"])

# 共识强度 → 百分比（用于「辩论擂台」量化展示）
_CONSENSUS_PCT = {
    "strong": 92,
    "moderate": 70,
    "disagreement": 35,
}

# 全局辩论引擎实例（懒加载）
_debate_engine: DebateEngine = None

# v4 Meta 质量陪审团实时计算缓存（20s TTL，避免高频轮询反复打 GPU 推理）
_MQ_CACHE: dict = {"t": 0, "v": {}}

# ── 90 天历史成交缓存（2026-08-09 性能修复）──
# /api/dashboard/accounts 被前端每 3s 轮询，而每个账号都要拉 90 天历史成交（重查询）。
# 实测该接口耗时 20.7s，前端超时直接红条。历史成交是慢变量，60s 缓存足够，
# 今日盈利仍走实时余额法，不受影响。
_DEALS_CACHE: Dict[str, tuple] = {}
_DEALS_CACHE_TTL = 60.0


def _get_hist_deals_cached(aid: str, now: datetime) -> dict:
    """取 90 天历史成交，带 60s TTL 缓存。查询失败时不污染缓存。"""
    hit = _DEALS_CACHE.get(aid)
    if hit and (time.time() - hit[0]) < _DEALS_CACHE_TTL:
        return hit[1]
    res = mt5_service.get_history_deals(
        aid, date_from=now - timedelta(days=90), date_end=now
    )
    if isinstance(res, dict) and res.get("deals") is not None:
        _DEALS_CACHE[aid] = (time.time(), res)
    return res


# ── 今日成交缓存（2026-08-09 性能修复第二轮）──
# 今日成交同样是每账号一次 IPC，在 3s 轮询下纯属重复开销。
# 今日盈利的实时性由「余额日变额法」保证（走 get_account_info 的实时余额），
# 今日成交仅用于建立当日开盘余额基线与订单计数，20s 陈旧完全可接受。
# 缓存键带上服务器日期，跨日自动失效，绝不会把昨天的成交当成今天。
_TODAY_DEALS_CACHE: Dict[str, tuple] = {}
_TODAY_DEALS_CACHE_TTL = 20.0


def _get_today_deals_cached(aid: str, today_start: datetime, now: datetime) -> dict:
    """取今日成交，带 20s TTL 缓存。查询失败时不污染缓存。"""
    key = f"{aid}:{today_start.date().isoformat()}"
    hit = _TODAY_DEALS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TODAY_DEALS_CACHE_TTL:
        return hit[1]
    res = mt5_service.get_history_deals(aid, date_from=today_start, date_end=now)
    if isinstance(res, dict) and res.get("deals") is not None:
        _TODAY_DEALS_CACHE[key] = (time.time(), res)
    return res


def _get_market_primary_id(user_id: str = None) -> str:
    """从数据库查找行情主号 ID（user_id 可选：指定则隔离多用户；缺省全局降级兼容后台线程）"""
    db = SessionLocal()
    try:
        # ★ 2026-08-09：统一走 primary_selector。旧逻辑只认 is_market_primary 标记，
        #   主号掉线时会把行情源指向死账号，导致全系统静默降级到模拟数据。
        return pick_market_primary_id(db, user_id)
    except Exception as e:
        logger.warning(f"[Dashboard] 查找行情主号失败: {e}")
        return ""
    finally:
        db.close()


def _get_market_primary_info(user_id: str = None) -> dict:
    """返回行情主号 MT5 的 login / name（作战图标注用，user_id 可选隔离多用户）"""
    db = SessionLocal()
    try:
        # ★ 与 _get_market_primary_id 用同一套选择规则，避免"标注的主号"和
        #   "实际取数的主号"是两个账号，让作战图显示与真实数据源对不上。
        primary = pick_market_primary(db, user_id)
        if not primary:
            return {"login": "", "name": "未连接", "server": ""}
        return {
            "login": str(primary.account_id or ""),
            "name": primary.name or "",
            "server": primary.server or "",
        }
    except Exception as e:
        logger.warning(f"[Dashboard] 查找行情主号信息失败: {e}")
        return {"login": "", "name": "未知", "server": ""}
    finally:
        db.close()


def _get_debate_engine() -> DebateEngine:
    global _debate_engine
    if _debate_engine is None:
        primary_id = _get_market_primary_id()
        logger.info(f"[Dashboard] 初始化 DebateEngine，行情主号={primary_id[:8] if primary_id else '无'}")
        # 构造 KeyPool（从 DB 加载多 Key，注入到 DebateEngine）
        from app.services.key_pool import build_pools_from_db, register_pool
        try:
            deepseek_pool, hunyuan_pool = build_pools_from_db()
            register_pool(deepseek_pool)
            register_pool(hunyuan_pool)
            logger.info(
                f"[Dashboard] KeyPool 初始化: deepseek={deepseek_pool.size()}个, "
                f"hunyuan={hunyuan_pool.size()}个"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Dashboard] KeyPool 构造失败，回退 .env 单 key: {e}")
            deepseek_pool = hunyuan_pool = None

        _debate_engine = DebateEngine(
            mt5_service=mt5_service,
            market_primary_id=primary_id,
            deepseek_pool=deepseek_pool,
            hunyuan_pool=hunyuan_pool,
        )
        # 注入 evolution log 回调：自进化事件落库
        _debate_engine.meta_agent.evo_logger = _evolution_log_writer
    return _debate_engine


def _evolution_log_writer(payload: dict):
    """MetaAgent 反馈触发的自进化日志落库（独立 Session，避免与请求事务冲突）"""
    # ★ 同时推入进程内存环形缓冲（实时保底，Defender 锁库也不丢实时进化）
    try:
        from app.services.ai_memory import push_evolution
        _evt = dict(payload)
        # ★ 2026-08-10 时区修复：原 datetime.utcnow().isoformat() 输出无时区 ISO，
        #   前端 new Date() 按本地(UTC+8)解析会把 UTC 07:30 当成本地 07:30（少 8h）。
        #   改为带时区的 UTC ISO（前端按 UTC 解析，再 toLocaleTimeString 转本地）。
        _evt["ts"] = (payload.get("event_time").isoformat()
                      if payload.get("event_time") else datetime.now(timezone.utc).isoformat())
        push_evolution(_evt)
    except Exception:
        pass

    sess = SessionLocal()
    try:
        # 通过 mt5_account_id 找 user_id（payload 可能没有 user_id）
        user_id = None
        if payload.get("mt5_account_id"):
            acc = sess.query(MT5Account).filter(MT5Account.id == payload["mt5_account_id"]).first()
            if acc:
                user_id = acc.user_id
        # 若依然没找到，取第一个用户
        if not user_id:
            u = sess.query(User).first()
            user_id = u.id if u else "system"

        import json as _json
        from datetime import datetime as _dt
        rec = EvolutionLog(
            user_id=user_id,
            mt5_account_id=payload.get("mt5_account_id") or None,
            kind=payload.get("kind", "weight_update"),
            subject=payload.get("subject"),
            before_value=payload.get("before_value"),
            after_value=payload.get("after_value"),
            delta=payload.get("delta"),
            reason=payload.get("reason"),
            # ★ 用交易真实平仓时间（若传入），否则回退到写入时刻
            # ★ 2026-08-10 时区修复：同 _evolution_log_writer 的 ts 一致，带 timezone.utc
            created_at=payload.get("event_time") or _dt.now(timezone.utc),
            meta_json=_json.dumps(payload.get("meta_json", {}), ensure_ascii=False)
                if payload.get("meta_json") else None,
        )
        sess.add(rec)
        sess.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[EvolutionLog] 写入失败: {e}")
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        sess.close()

# WebSocket 连接池
active_connections: Dict[str, WebSocket] = {}


@router.get("/summary")
def get_summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """仪表盘摘要数据（仅当前用户）"""
    accounts = db.query(MT5Account).filter(MT5Account.user_id == user.id).all()
    trades = db.query(Trade).filter(Trade.user_id == user.id).order_by(Trade.created_at.desc()).limit(50).all()

    total_balance = sum(a.balance or 0 for a in accounts)
    total_equity = sum(a.equity or 0 for a in accounts)
    total_profit = sum(a.profit or 0 for a in accounts)

    online_count = sum(1 for a in accounts if a.is_connected)
    trading_count = sum(1 for a in accounts if a.is_trading_enabled)

    return {
        "accounts": {
            "total": len(accounts),
            "online": online_count,
            "trading": trading_count,
        },
        "capital": {
            "total_balance": round(total_balance, 2),
            "total_equity": round(total_equity, 2),
            "total_profit": round(total_profit, 2),
        },
        "accounts_detail": [
            {
                "name": a.name,
                "server": a.server,
                "is_connected": a.is_connected,
                "is_trading": a.is_trading_enabled,
                "balance": a.balance,
                "equity": a.equity,
                "profit": a.profit,
                "margin_level": a.margin_level,
            }
            for a in accounts
        ],
        "recent_trades": [
            {
                "ticket": t.mt5_ticket,
                "action": t.action,
                "volume": t.volume,
                "profit": t.profit,
                "result": t.result,
                "meta_agent_decision": t.meta_agent_decision,
                "meta_agent_confidence": t.meta_agent_confidence,
                "time": _to_utc_iso(t.open_time),
            }
            for t in trades[:20]
        ],
    }


# ──────────────────────────────────────────────────────────────
# 需求 5：市场时钟（休市状态 + 开市倒计时，星迈时区）
# ──────────────────────────────────────────────────────────────
@router.get("/market-session")
def market_session(user: User = Depends(get_current_user)):
    """返回市场开/休市状态 + 倒计时（星迈 GMT+2/+3 DST 时区）"""
    return get_session_state()


# ──────────────────────────────────────────────────────────────
# 系统健康检查（故障报警用）
# ──────────────────────────────────────────────────────────────
@router.get("/system-health")
def system_health(user: User = Depends(get_current_user)):
    """
    返回各核心模块健康状态，前端 MarketClock 故障报警用。
    检测：MT5 账号连通 / 行情主号时区可达 / 外部数据源
    """
    faults = []
    db = SessionLocal()
    try:
        # ★ 2026-08-09 多租户越权修复 ★
        # 原为 db.query(MT5Account).all()——**没有按 user_id 过滤**，
        # 会把平台上所有客户的账号都查出来，并把他人的账号名与账号号
        # 直接写进当前用户看到的故障文案里（数据泄漏）。
        # 铁律：一个交易账号 = 一个独立客户，任何查询都必须带租户隔离。
        accounts = db.query(MT5Account).filter(MT5Account.user_id == user.id).all()
        acct_snap = [(a.id, a.name, a.account_id) for a in accounts]
    except Exception as e:
        logger.error(f"[系统健康] 账号查询失败: {e}")
        acct_snap = []
    finally:
        db.close()

    # 2026-08-09 性能修复：原为串行逐账号探测 server_info，实测 5.9s。改为并行。
    # ★ 2026-08-16 管理后台审计修复：连通性判定改用 **ping**（get_all_accounts_status），
    #   与 /api/health 口径完全一致。原用 get_server_info（含 7 天 session 采集），
    #   REAL 账号（詹启东/詹启东3）间歇返回非 dict 形态 → 被形态防御拦成 error →
    #   健康面板误报「离线或不可达」（用户肉眼所见）。ping 只测 Worker 存活，稳定可靠；
    #   server_info 仅作时区/交易时段功能（失败时由 market_session 静态兜底），不当连通性判据。
    try:
        _sts = mt5_service.get_all_accounts_status(
            {aid for aid, _an, _al in acct_snap} if acct_snap else set())
        _online_ids = {s["account_id"] for s in _sts if s.get("connected")}
    except Exception:
        _online_ids = set()

    online, offline = 0, 0
    for aid, aname, alogin in acct_snap:
        if aid in _online_ids:
            online += 1
        else:
            offline += 1
            faults.append({
                "module": f"MT5:{aname}",
                "level": "error",
                "message": f"MT5 账号 {aname}(#{alogin}) 离线或不可达",
            })

    # ★ 2026-08-08 Phase 3：执行层并发健康。多租户下 N 个客户共用同一 XAUUSD
    #   信号源，同秒打单会互相挤单抬高滑点。这里把「按并发档位分组的滑点」暴露出来，
    #   并在确认挤单时升级为一条可见故障——运维据此调错峰窗口，而不是拍脑袋。
    execution = {"available": False}
    try:
        from app.core.account_lane import active_accounts, get_attribution, get_lane_pool
        execution = get_attribution().summary()
        execution["active_accounts"] = active_accounts()
        execution["lane_workers"] = {
            "user": get_lane_pool("user").max_workers,
            "account": get_lane_pool("account").max_workers,
        }
        if execution.get("crowding_suspected"):
            faults.append({
                "module": "执行层",
                "level": "warn",
                "message": (
                    f"检测到同秒挤单：高并发时段平均滑点显著高于单账号时段"
                    f"（样本 {execution.get('count', 0)} 笔），建议调大下单错峰窗口"
                ),
            })
    except Exception:
        pass

    # ★ 2026-08-08 Phase 6：平台降级档位。前端「降级指示灯」与运维告警共用此数据。
    #   降级不是错误——L1/L2 时系统仍在正常交易（只是缩手），故用 warn 而非 error；
    #   只有 L3（停发新开仓）才升级为 error，因为那意味着业务实质中断。
    degrade = {"level": 0, "level_name": "L0"}
    try:
        from app.services.platform_health_monitor import degrade_enabled, snapshot_dict

        degrade = snapshot_dict()
        degrade["enabled"] = degrade_enabled()
        _lv = int(degrade.get("level", 0))
        if _lv >= 3:
            faults.append({
                "module": "AI决策层",
                "level": "error",
                "message": (
                    f"L3 熔断：{degrade.get('reason')} → 已停发新开仓信号；"
                    f"已有持仓仍由止损/止盈/智能平仓守护"
                ),
            })
        elif _lv >= 1:
            # ★ 2026-08-11 欠费可见性：L1 降级文案带上组件最近错误（如
            #   "402 Insufficient Balance"），让运维一眼看出是欠费而非抖动。
            _comp = degrade.get("components") or {}
            _ds_err = (_comp.get("deepseek") or {}).get("last_error") or ""
            _hy_err = (_comp.get("hunyuan") or {}).get("last_error") or ""
            _err_txt = ""
            if "Insufficient" in _ds_err or "402" in _ds_err:
                _err_txt = f"；⚠ DeepSeek 报「{_ds_err[:60]}」——疑似欠费，请尽快充值"
            elif "Insufficient" in _hy_err or "402" in _hy_err:
                _err_txt = f"；⚠ 混元报「{_hy_err[:60]}」——疑似欠费，请尽快充值"
            faults.append({
                "module": "AI决策层",
                "level": "warn",
                "message": (
                    f"{degrade.get('level_name')} {degrade.get('label')}：{degrade.get('reason')}"
                    f"；手数已自动降至 {degrade.get('lot_multiplier', 1.0) * 100:.0f}%"
                    f"{_err_txt}"
                ),
            })
    except Exception:
        pass

    # 本地 LLM（Qwen3-8B 校对员/副驾）状态：L0 常态不加载模型，
    # available=False 在 L0 属正常（不占显存），不算故障。
    local_llm = {"available": False}
    try:
        from app.services.local_llm_service import status_dict as _llm_status

        local_llm = _llm_status()
    except Exception:
        pass
    try:
        from app.core.market_analyzer import get_orderflow_status
        _orderflow_status = get_orderflow_status()
    except Exception:
        _orderflow_status = {"available": False, "live": False, "source": None, "is_real_cvd": False}
    return {
        "ok": len(faults) == 0,
        "faults": faults,
        "modules": {
            "mt5_online": online,
            "mt5_offline": offline,
        },
        "degrade": degrade,
        "local_llm": local_llm,
        "orderflow_status": _orderflow_status,
        "execution": execution,
        "checked_at": datetime.utcnow().isoformat(),
    }


# ──────────────────────────────────────────────────────────────
# 需求 3：多周期行情图 + 指标参数 + 趋势
# ──────────────────────────────────────────────────────────────
_ALLOWED_TF = {"M5", "M15", "M30", "H1", "H4", "D1"}


# ──────────────────────────────────────────────────────────────
# ★ P0-1 作战图 AI 布防层：汇总当前用户所有账号 XAUUSD 持仓，
#   生成「AI 当前在图上布了什么防」的结构化数据（净方向/入场区/SL-TP/AI 判读）。
#   前端在 K 线图上叠加可视化，兑现"AI 工作可视化"铁律。
# ──────────────────────────────────────────────────────────────
_AIDEF_CACHE = {"ts": 0.0, "user": None, "data": None}
_EQUITY_CACHE = {"ts": 0.0, "user": None, "days": None, "data": None}


def _summarize_positions(positions: list) -> dict:
    """把一组持仓压缩成 AI 作战布防摘要（净方向 / 加权入场 / SL-TP 带 / 多空力度）。"""
    # ★ 2026-08-11 防御：剔除任何非 dict 元素（如 worker 偶发返回字符串被 extend 成字符），
    #   否则 p.get(...) 抛 'str' object has no attribute 'get' → /api/dashboard/market-chart 返回 500
    positions = [p for p in (positions or []) if isinstance(p, dict)]
    buys = [p for p in positions if str(p.get("type", "")).lower() == "buy"]
    sells = [p for p in positions if str(p.get("type", "")).lower() == "sell"]
    total = len(positions)
    buy_lot = sum(float(p.get("volume", 0) or 0) for p in buys)
    sell_lot = sum(float(p.get("volume", 0) or 0) for p in sells)
    net_lot = sell_lot - buy_lot  # 正=净空，负=净多
    if net_lot > 1e-4:
        net_bias = "short"
    elif net_lot < -1e-4:
        net_bias = "long"
    else:
        net_bias = "neutral"
    gross = buy_lot + sell_lot or 1.0
    bias_strength = round(abs(net_lot) / gross * 100, 1)  # 多空一边倒程度(%)

    def wavg(items, key):
        tot = sum(float(p.get("volume", 0) or 0) for p in items)
        if tot <= 0:
            return None
        return sum(float(p.get(key, 0) or 0) * float(p.get("volume", 0) or 0) for p in items) / tot

    avg_entry = wavg(positions, "open_price")
    sl_vals = [float(p.get("sl", 0) or 0) for p in positions if float(p.get("sl", 0) or 0) > 0]
    tp_vals = [float(p.get("tp", 0) or 0) for p in positions if float(p.get("tp", 0) or 0) > 0]
    avg_sl = round(sum(sl_vals) / len(sl_vals), 2) if sl_vals else None
    avg_tp = round(sum(tp_vals) / len(tp_vals), 2) if tp_vals else None
    float_pnl = round(sum(float(p.get("profit", 0) or 0) for p in positions), 2)

    # AI 人话判读（高端、客户视角）
    if net_bias == "short":
        bias_cn, arrow = "看空", "▼"
    elif net_bias == "long":
        bias_cn, arrow = "看多", "▲"
    else:
        bias_cn, arrow = "中性观望", "■"
    if total == 0:
        ai_read = "当前无持仓 · AI 静观其变"
    else:
        ai_read = (
            f"AI {bias_cn} {arrow} · {total} 笔持仓布防中"
            f"（净{'空' if net_bias == 'short' else '多' if net_bias == 'long' else '平'} "
            f"{abs(net_lot):.2f} 手 · 力度 {bias_strength}%）"
        )

    return {
        "symbol": "XAUUSD",
        "total": total,
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_lot": round(buy_lot, 2),
        "sell_lot": round(sell_lot, 2),
        "net_bias": net_bias,
        "bias_strength": bias_strength,
        "avg_entry": round(avg_entry, 2) if avg_entry else None,
        "avg_sl": avg_sl,
        "avg_tp": avg_tp,
        "float_pnl": float_pnl,
        "ai_read": ai_read,
    }


def _collect_combo_positions(user_id: str) -> dict:
    """汇总当前用户所有 MT5 账号的 XAUUSD 持仓 → AI 作战布防摘要。"""
    db = SessionLocal()
    try:
        accounts = db.query(MT5Account).filter(MT5Account.user_id == user_id).all()
    except Exception:
        accounts = []
    finally:
        db.close()
    all_pos = []
    for acc in accounts:
        try:
            ps = mt5_service.get_positions(acc.id, "XAUUSD")
        except Exception:
            ps = []
        if ps:
            all_pos.extend(ps)
    return _summarize_positions(all_pos)


@router.get("/vision-status")
def vision_status(user: User = Depends(get_current_user)):
    """视觉模型第四票生产者运行状态（盯盘/排障用）。

    返回：enabled / 模型 / 生产者是否已启动 / 累计生产次数 / 成功次数 /
    最后错误 / 当前缓存票（方向·置信·H4·M15·权重缩放·结构备注）。
    """
    try:
        from app.services.vision_service import get_service as get_vision
        svc = get_vision()
        st = svc.status()
        return {"ok": True, **st}
    except Exception as e:  # noqa: BLE001
        logger.error(f"[VisionStatus] 查询失败: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/market-chart")
def market_chart(tf: str = "H1", user: User = Depends(get_current_user)):
    """返回选定周期 K 线 + 全量指标参数 + 趋势判读"""
    if tf not in _ALLOWED_TF:
        tf = "H1"
    primary_id = _get_market_primary_id(user.id)
    if not primary_id:
        return {"error": "行情主号未连接", "tf": tf}
    md = mt5_service.get_market_data(primary_id)
    if "error" in md:
        return {"error": md["error"], "tf": tf}

    tf_data = md.get("timeframes", {}).get(tf, {})
    bars = tf_data.get("bars", [])
    indicators = compute_indicators(bars)

    # 实时报价 + 点差
    current = md.get("current", {})
    # 宏观镜像（DXY / VIX / 相关性）—— 只读后台缓存，0 阻塞、0 超时
    ext = market_data_provider.get_external_snapshot()
    macro = {
        "dxy": ext.get("dxy"),
        "vix": ext.get("vix"),
        "correlation": ext.get("correlation"),
    }
    # 趋势判读增强（叠加宏观）
    trend = indicators.get("trend", "中性")
    if ext.get("correlation") and ext["correlation"].get("signal") == "dxy_up_gold_down":
        trend += " · 美元走强压制黄金"
    elif ext.get("correlation") and ext["correlation"].get("signal") == "dxy_down_gold_up":
        trend += " · 美元走弱利好黄金"

    # 行情主号（作战图标注：当前用的是哪一个 MT5）
    mt5_info = _get_market_primary_info(user.id)

    # ★ P0-1 AI 作战布防（30s 缓存，避免每次刷新都查 4 个账号 MT5）
    global _AIDEF_CACHE
    _now = time.time()
    if _AIDEF_CACHE.get("data") is not None and _AIDEF_CACHE.get("user") == user.id and _now - _AIDEF_CACHE.get("ts", 0) < 30:
        ai_defense = _AIDEF_CACHE["data"]
    else:
        ai_defense = _collect_combo_positions(user.id)
        _AIDEF_CACHE = {"ts": _now, "user": user.id, "data": ai_defense}

    return {
        "symbol": md.get("symbol", "XAUUSD"),
        "tf": tf,
        "server_time": md.get("timestamp"),
        "mt5": mt5_info,
        "current": current,
        "bars": bars,
        "indicators": indicators,
        "macro": macro,
        "trend": trend,
        "ai_defense": ai_defense,
    }


@router.get("/equity-curve")
def equity_curve(days: int = 30, user: User = Depends(get_current_user)):
    """组合盈利/净值曲线（按日聚合累计净盈亏）—— 兑现"持续盈利"宗旨的可视化数据源。"""
    global _EQUITY_CACHE
    _now = time.time()
    if (_EQUITY_CACHE.get("data") is not None
            and _EQUITY_CACHE.get("user") == user.id
            and _EQUITY_CACHE.get("days") == days
            and _now - _EQUITY_CACHE.get("ts", 0) < 60):
        return _EQUITY_CACHE["data"]

    db = SessionLocal()
    try:
        accounts = db.query(MT5Account).filter(MT5Account.user_id == user.id).all()
        # 在 session 关闭前把主键取出：关闭后再访问 ORM 属性会触发
        # DetachedInstanceError，并行化后更容易踩到。
        acct_ids = [a.id for a in accounts]
    except Exception:
        acct_ids = []
    finally:
        db.close()

    date_from = datetime.now() - timedelta(days=days)
    date_end = datetime.now()

    def _fetch_deals(aid: str) -> dict:
        try:
            return mt5_service.get_history_deals(aid, date_from=date_from, date_end=date_end)
        except Exception as e:
            logger.warning(f"[净值曲线] 账号 {aid} 历史成交拉取失败: {e}")
            return {}

    # 2026-08-09 性能修复：原为串行逐账号拉取，实测 11.9s。改为并行。
    if len(acct_ids) <= 1:
        results = [_fetch_deals(a) for a in acct_ids]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(
            max_workers=min(len(acct_ids), 8), thread_name_prefix="dash-eq"
        ) as ex:
            results = list(ex.map(_fetch_deals, acct_ids))

    daily = {}  # "YYYY-MM-DD" -> 当日净盈亏累计
    for res in results:
        for d in res.get("deals", []):
            t = d.get("time")
            if not t:
                continue
            try:
                ts = int(t)
                dt = datetime.fromtimestamp(ts)
            except Exception:
                continue
            day = dt.strftime("%Y-%m-%d")
            net = float(d.get("net_profit", 0) or 0)
            daily[day] = daily.get(day, 0.0) + net

    days_sorted = sorted(daily.keys())
    series = []
    cum = 0.0
    for day in days_sorted:
        cum += daily[day]
        series.append({"date": day, "daily": round(daily[day], 2), "cum": round(cum, 2)})

    out = {
        "symbol": "XAUUSD",
        "days": days,
        "series": series,
        "total_cum": round(cum, 2),
        "total_daily": round(sum(daily.values()), 2),
    }
    _EQUITY_CACHE = {"ts": _now, "user": user.id, "days": days, "data": out}
    return out


# ──────────────────────────────────────────────────────────────
# 需求 1 + 4：弹性账户网格（1~N 账号）+ 每账号详情
# ──────────────────────────────────────────────────────────────
def _build_account_detail(db, account) -> dict:
    """构建单账号详情（持仓含时长 / 今日盈利 / 历史总盈利 / 今日订单 / 历史订单）"""
    aid = account.id
    now = datetime.now()

    # 实时持仓（含持仓时长，分钟）
    positions = mt5_service.get_positions(aid)
    pos_out = []
    float_profit = 0.0
    for p in positions:
        try:
            ot = datetime.fromisoformat(p["open_time"])
            holding = int((now - ot).total_seconds() // 60)
        except Exception:
            holding = 0
        float_profit += float(p.get("profit", 0) or 0)
        pos_out.append({
            "ticket": p.get("ticket"),
            "type": p.get("type"),
            "volume": p.get("volume"),
            "open_price": p.get("open_price"),
            "current_price": p.get("current_price"),
            "sl": p.get("sl"),
            "tp": p.get("tp"),
            "profit": p.get("profit"),
            "swap": p.get("swap"),
            "open_time": p.get("open_time"),
            "holding_minutes": holding,
        })

    # 「今日」以星迈服务器时间界定，避免本机时差导致日切算错
    server_now = local_to_server(now)
    server_today = server_now.date()
    # server 今日 0 点（当作终端时区传给 MT5，与服务器日切对齐）
    today_start = datetime(server_today.year, server_today.month, server_today.day)

    from datetime import timedelta

    # ════════════════════════════════════════════════════════════
    # ★ 今日盈利：余额日变额法（与 MT5 终端「今日盈亏」口径完全一致）
    #   今日盈利 = 当前余额 − 今日开盘余额。
    #   数据源 = get_account_info().balance（与账户管理页同源，已验证可靠，
    #   实时、不依赖 Deals/Orders 历史，彻底规避历史窗口 / 时区边界误差）。
    #   今日开盘余额基线：当日首次调用时用 Deals 反推建立
    #   （开盘余额 = 当前余额 − 今日已实现盈亏），建立后当日全部走余额日变额。
    #   历史盈利仍用 Deals 累计（长周期，对窗口误差不敏感，与终端「历史」总和对齐）。
    # ════════════════════════════════════════════════════════════

    # 全量历史成交：90天前 ~ 现在（历史盈利 / 历史订单用）——60s 缓存，见 _get_hist_deals_cached
    mt5_all_deals = _get_hist_deals_cached(aid, now)
    # 今日区间：今日0点 ~ 现在（仅用于首日 / 跨日建立开盘余额基线）——20s 缓存
    mt5_today_deals = _get_today_deals_cached(aid, today_start, now)

    def _realized_profit(deals_result: dict) -> float:
        """只统计 entry>=1（平仓/反手/强平）的已实现净盈亏"""
        return round(
            sum((d.get("net_profit", 0) or 0) for d in deals_result.get("deals", []) if (d.get("entry", 0) or 0) >= 1),
            2,
        )

    # 实时余额 / 净值（与账户管理页同源，权威）
    current_balance = account.balance
    current_equity = account.equity
    try:
        _info = mt5_service.get_account_info(aid)
        if "error" not in _info:
            current_balance = float(_info.get("balance", current_balance) or current_balance)
            current_equity = float(_info.get("equity", current_equity) or current_equity)
    except Exception:
        pass

    # 今日盈利：余额日变额法（基线自动建立 / 跨日自动重建）
    today_profit = _bl_today_profit(aid, server_today, current_balance, _realized_profit(mt5_today_deals))
    # 历史盈利 = 90天内全部平仓成交净盈亏累计（含今日）
    hist_profit = _realized_profit(mt5_all_deals)
    # 当前浮盈 = 持仓实时浮动汇总
    float_pnl = round(float_profit, 2)

    # 订单数 = 平仓笔数（Worker 已按 entry>=1 统计 close_count）
    today_orders = mt5_today_deals.get("close_count", 0) or 0
    hist_orders = mt5_all_deals.get("close_count", 0) or 0

    return {
        "id": aid,
        "name": account.name,
        "login": account.account_id,
        "server": account.server,
        "is_primary": account.is_market_primary,
        "is_connected": account.is_connected,
        "is_trading": account.is_trading_enabled,
        "balance": current_balance,
        "equity": current_equity,
        "margin_level": account.margin_level,
        "today_profit": today_profit,          # 今日平仓已实现（不含浮动）
        "hist_profit": hist_profit,            # 全部已实现累计（含今日）
        "float_pnl": float_pnl,                # 当前持仓浮动盈亏汇总
        "today_orders": today_orders,          # 今日订单数
        "hist_orders": hist_orders,            # 全部历史订单数
        "position_count": len(pos_out),
        "positions": pos_out,
    }


def _safe_build_detail(db, account) -> dict:
    """构建单账号详情，失败时降级返回 DB 快照。

    多租户铁律：一个账号 = 一个客户。任何单账号的 MT5 故障都**不得**
    让整个仪表盘 500，否则一个客户的终端挂了会拖垮所有人的面板。
    """
    try:
        return _build_account_detail(db, account)
    except Exception as e:
        logger.error(f"[仪表盘] 账号 {getattr(account, 'name', '?')} 详情构建失败，已降级: {e}")
        return {
            "id": account.id,
            "name": account.name,
            "login": account.account_id,
            "server": account.server,
            "is_primary": account.is_market_primary,
            "is_connected": False,
            "is_trading": account.is_trading_enabled,
            "balance": account.balance or 0,
            "equity": account.equity or 0,
            "margin_level": account.margin_level or 0,
            "today_profit": 0.0,
            "hist_profit": 0.0,
            "float_pnl": 0.0,
            "today_orders": 0,
            "hist_orders": 0,
            "position_count": 0,
            "positions": [],
            "degraded": True,
            "degraded_reason": str(e)[:200],
        }


def _build_accounts_payload(db, user_id: str) -> dict:
    """构建账号列表（弹性，1~N）+ 组合总览聚合。

    抽成独立函数，供请求线程（冷启动）与后台刷新线程共用。
    多租户铁律：只查该 user_id 名下账号，账号数 N 为纯变量，不做任何数量假设。
    """
    accounts = db.query(MT5Account).filter(MT5Account.user_id == user_id).all()

    # ★ 2026-08-09 性能根因修复 ★
    # 原实现是串行 list comprehension：N 个账号的 MT5 查询逐个排队，
    # 叠加 mt5_service 里那把全局命令锁，实测 4 账号耗时 20.7s，
    # 而前端每 3s 轮询本接口 → 必然超时 → 全屏红条、界面像"瘫痪"。
    # 现改为按账号并行（各账号有独立 Worker 进程 + 独立命令锁，天然可并发）。
    # 并行前先把 ORM 属性读进内存：SQLAlchemy Session 非线程安全，
    # 绝不能让子线程触发懒加载。
    for _a in accounts:
        _ = (_a.id, _a.name, _a.account_id, _a.server, _a.is_market_primary,
             _a.is_connected, _a.is_trading_enabled, _a.balance, _a.equity,
             _a.margin_level)

    if len(accounts) <= 1:
        details = [_safe_build_detail(db, a) for a in accounts]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(
            max_workers=min(len(accounts), 8), thread_name_prefix="dash-acct"
        ) as ex:
            details = list(ex.map(lambda a: _safe_build_detail(db, a), accounts))

    # 客户视角排序：①活跃优先(在线且交易中) ②同档内按当前总盈利(浮盈+今日)降序
    # ——客户第一眼确认"系统在跑"，其次看"谁在赚钱"
    def _acct_rank(d):
        active = 1 if (d["is_connected"] and d["is_trading"]) else 0
        pnl = (d.get("float_pnl") or 0) + (d.get("today_profit") or 0)
        return (active, pnl)
    details.sort(key=_acct_rank, reverse=True)

    portfolio = {
        "account_count": len(details),
        "online": sum(1 for d in details if d["is_connected"]),
        "trading": sum(1 for d in details if d["is_trading"]),
        "total_balance": round(sum(d["balance"] or 0 for d in details), 2),
        "total_equity": round(sum(d["equity"] or 0 for d in details), 2),
        "today_profit": round(sum(d["today_profit"] or 0 for d in details), 2),
        "hist_profit": round(sum(d["hist_profit"] or 0 for d in details), 2),
        "total_positions": sum(d["position_count"] for d in details),
    }
    return {"accounts": details, "portfolio": portfolio}


# ── /api/dashboard/accounts 快照缓存（2026-08-09 雪崩根因修复第二轮）──
# 本接口是主面板核心数据源。即便账号级已并行化，每个账号内部仍要串行多次
# MT5 IPC（持仓 / 今日成交 / 账户信息 / 90天历史），实测 4 账号 9~12s，
# 远超前端 3s 的轮询节奏 → 请求必然堆积 → 争抢 IPC → 越来越慢 → 雪崩。
# 前端已改为自调度（同一时刻只有一轮在飞），后端这里再加一层
# stale-while-revalidate，双保险：
#   ① 命中新鲜快照 → 毫秒返回；
#   ② 快照过期     → 先返回旧快照，同时起后台线程刷新（调用方不等待）；
#   ③ 完全无快照   → 仅进程刚起的第一次才同步构建。
# 按 user_id 分键，严守多租户隔离，绝不串号。
_ACCT_CACHE: Dict[str, dict] = {}     # {user_id: {"ts": float, "data": dict}}
_ACCT_REFRESHING: set = set()         # 正在后台刷新的 user_id，防止重复起线程
_ACCT_CACHE_LOCK = threading.Lock()
_ACCT_FRESH_TTL = 3.0                 # 新鲜期，与前端轮询节奏对齐


def _refresh_accounts_async(user_id: str) -> None:
    """后台刷新某租户的账号快照。同一 user 同时只允许一个刷新线程。"""
    with _ACCT_CACHE_LOCK:
        if user_id in _ACCT_REFRESHING:
            return
        _ACCT_REFRESHING.add(user_id)

    def _work():
        # 后台线程必须用自己的 Session：请求线程的 db 在响应返回后就关闭了
        db = SessionLocal()
        try:
            data = _build_accounts_payload(db, user_id)
            with _ACCT_CACHE_LOCK:
                _ACCT_CACHE[user_id] = {"ts": time.time(), "data": data}
        except Exception as e:
            # 刷新失败不污染旧快照：前端继续看到上一次的真实数据
            logger.error(f"[仪表盘] 账号快照后台刷新失败 user={user_id}: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass
            with _ACCT_CACHE_LOCK:
                _ACCT_REFRESHING.discard(user_id)

    threading.Thread(target=_work, name="acct-refresh", daemon=True).start()


@router.get("/accounts")
def get_accounts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """账号列表（弹性，1~N）+ 组合总览聚合（SWR 缓存，见上方说明）"""
    uid = user.id
    with _ACCT_CACHE_LOCK:
        hit = _ACCT_CACHE.get(uid)

    if hit:
        age = time.time() - hit["ts"]
        if age >= _ACCT_FRESH_TTL:
            _refresh_accounts_async(uid)   # 过期：后台刷新，本次仍立即返回旧快照
        out = dict(hit["data"])
        out["cache_age_sec"] = round(age, 2)
        return out

    # 冷启动：进程内该租户还没有任何快照，同步构建一次
    data = _build_accounts_payload(db, uid)
    with _ACCT_CACHE_LOCK:
        _ACCT_CACHE[uid] = {"ts": time.time(), "data": data}
    out = dict(data)
    out["cache_age_sec"] = 0.0
    return out


@router.get("/debug-history")
def debug_history(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """诊断：穿透 Worker 核对每个账号 MT5 历史成交原始数据（含入金）/真实交易/今日"""
    from datetime import timedelta
    now = datetime.now()
    server_now = local_to_server(now)
    today_start = datetime(server_now.year, server_now.month, server_now.day)
    out = []
    for acc in db.query(MT5Account).filter(MT5Account.user_id == user.id).all():
        aid = acc.id
        # 全量
        all_d = mt5_service.get_history_deals(aid, date_from=now - timedelta(days=90), date_end=now)
        # 今日
        today_d = mt5_service.get_history_deals(aid, date_from=today_start, date_end=now)
        # 最近20笔原始deals（诊断时间格式）
        recent = mt5_service.get_recent_deals(aid, limit=20)
        # 最近20笔原始orders（与deals对比——MT5终端"历史"标签页显示的是orders）
        recent_orders = mt5_service.get_recent_orders(aid, limit=20)
        out.append({
            "name": acc.name,
            "login": acc.account_id,
            "raw_total_90d": all_d.get("raw_count"),      # 含入金等所有成交；-1=查询失败
            "real_trades_90d": all_d.get("count"),        # 仅 BUY/SELL
            "real_profit_90d": all_d.get("total_profit"),
            "real_trades_today": today_d.get("count"),
            "real_profit_today": today_d.get("total_profit"),
            "today_start_iso": today_start.isoformat(),
            "now_iso": now.isoformat(),
            "recent_deals": recent.get("recent", []),
            "recent_orders": recent_orders.get("recent", []),
            "orders_total_raw": recent_orders.get("total_raw", 0),
        })
    return {"debug": out}


# ── 临时诊断路由结束（debug-ai-quality 用完已删，AI/magic 区分改动保留在 worker） ──


@router.get("/accounts/{account_id}/detail")
def get_account_detail(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """单账号完整详情"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        return {"error": "账号不存在"}
    return _build_account_detail(db, account)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """实时数据推送（★ 2026-08-15 复检P1修复：加 JWT 鉴权 + 按用户账号集过滤推送。

    原实现无鉴权且每 5s 推送全平台账号连接状态（get_all_accounts_status() 不带
    account_ids）——任意连接者可越权读取所有账号 UUID/连接态（上轮 P0 修复漏掉此调用点）。
    浏览器 WebSocket 无法带自定义 header → token 走 query param；无 token/无效 → 4401 关闭。
    """
    try:
        from jose import jwt as _jwt
        from jose.exceptions import JWTError as _JWTErr
        from app.config import settings as _st
        _tok = (websocket.query_params.get("token") or "").strip()
        _payload = _jwt.decode(_tok, _st.SECRET_KEY, algorithms=[_st.JWT_ALGORITHM])
        _uid = _payload.get("sub")
        if not _uid:
            await websocket.close(code=4401)
            return
    except Exception:
        await websocket.close(code=4401)
        return

    # 按当前用户账号集过滤推送（与 accounts.py /status 同口径，杜绝全平台泄漏）
    try:
        _s = SessionLocal()
        try:
            _owned = {r[0] for r in _s.query(MT5Account.id).filter(MT5Account.user_id == _uid).all()}
        finally:
            _s.close()
    except Exception:
        _owned = set()

    await websocket.accept()
    client_id = str(id(websocket))
    active_connections[client_id] = websocket

    try:
        # ★★ 2026-08-17 P0 修复（22:11-22:13 全服务卡死 110s 根因）：
        #   原代码在 async 事件循环里【直接同步调用】 mt5_service.get_all_accounts_status()，
        #   内部对每个账号 _send_cmd（ping 3s 超时 + 账号锁无超时等待）。
        #   当某 Worker 失联/管道错乱（如 2877213e get_positions 返回 dict）且账号锁被
        #   交易循环/快监线程占用时，lock.acquire() 无限等待 → 事件循环被阻塞 110-150s，
        #   全部 HTTP 请求（含 /api/health 心跳）排队断连 = 系统级卡死。
        #   修复：同步 IPC 一律丢回线程池（asyncio.to_thread），事件循环永不阻塞。
        while True:
            # 定时推送账户状态（仅当前用户账号集；空集时推空列表不推全平台）
            # ★ to_thread：同步 MT5 IPC 丢线程池，绝不阻塞事件循环（2026-08-17 P0）
            status = await asyncio.to_thread(
                mt5_service.get_all_accounts_status, _owned if _owned else set()
            )
            await websocket.send_json({
                "type": "account_status",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": status,
            })
            await asyncio.sleep(5)  # 每5秒推送一次
    except WebSocketDisconnect:
        pass
    finally:
        active_connections.pop(client_id, None)


@router.post("/decision")
def run_ai_decision(user: User = Depends(get_current_user)):
    """
    触发双模型 AI 辩论决策
    返回 DeepSeek + 混元 Hy3 的独立判断 + Meta-Agent 综合裁决
    """
    try:
        engine = _get_debate_engine()
        # ★ 2026-08-06 token 降本：交易循环每 60s 已刷新一次辩论快照(last_debate)，
        #   若用户点击「触发AI辩论决策」时快照仍新鲜(<90s)，直接复用，避免重复烧 6 次 LLM 调用。
        _ld = getattr(engine, "last_debate", None)
        _fresh = False
        if _ld and _ld.get("ts") and _ld.get("decision"):
            try:
                from datetime import datetime as _dt
                _age = time.time() - _dt.fromisoformat(_ld["ts"]).timestamp()
                _fresh = 0 <= _age < 90
            except Exception:
                _fresh = False
        if _fresh:
            d = _ld["decision"]
            logger.info("[AI决策] 复用交易循环最近辩论快照(<90s)，跳过重复 LLM 调用")
            _mc = engine.get_last_context()
            return {
                "timestamp": _ld.get("ts"),
                "cached": True,
                "deepseek": {"decision": d.deepseek_vote, "confidence": d.deepseek_weight},
                "hunyuan": {"decision": d.hunyuan_vote, "confidence": d.hunyuan_weight},
                "meta": {
                    "decision": d.decision,
                    "confidence": d.confidence,
                    "risk_level": d.risk_level,
                    "reasoning": d.reasoning_summary,
                    "quality_regime": getattr(d, "quality_regime", ""),
                    "chronos_tp_ceiling": getattr(d, "chronos_tp_ceiling", None),
                },
                # ★ Phase 4：溯源快照。上面那几个字段是历史遗留（前端老组件还在读），
                #   新增字段一律只进 provenance —— 单一权威序列化点，
                #   杜绝"接口各挑各的字段"造成的命名分裂（详见 decision_snapshot 模块头）。
                "provenance": build_decision_snapshot(d),
                "meta_quality": (_mc.get("market_data") or {}).get("meta_quality", {}),
                "market_context": _mc,
            }
        # ★ 2026-08-13 审计F2：前端预览调 decide() 未传 account_id → if account_id 守卫跳过持仓/成交注入，
        #   导致「AI推理」面板在看不见账本下拍板。现取行情主号 account_id 传入（与实盘决策同视角）。
        _acct_id = None
        try:
            db = SessionLocal()
            try:
                _primary = pick_market_primary(db, user.id)
                if _primary:
                    _acct_id = _primary.account_id
            finally:
                db.close()
        except Exception:
            _acct_id = None
        decision = engine.decide(debate_rounds=2, account_id=_acct_id)
        _mc = engine.get_last_context()

        return {
            "timestamp": datetime.now().isoformat(),
            "deepseek": {
                "decision": decision.deepseek_vote,
                "confidence": decision.deepseek_weight,
            },
            "hunyuan": {
                "decision": decision.hunyuan_vote,
                "confidence": decision.hunyuan_weight,
            },
            "meta": {
                "decision": decision.decision,
                "confidence": decision.confidence,
                "risk_level": decision.risk_level,
                "reasoning": decision.reasoning_summary,
                "quality_regime": getattr(decision, "quality_regime", ""),
                "chronos_tp_ceiling": getattr(decision, "chronos_tp_ceiling", None),
            },
            "provenance": build_decision_snapshot(decision),
            "meta_quality": (_mc.get("market_data") or {}).get("meta_quality", {}),
            "market_context": _mc,
        }
    except Exception as e:
        import traceback
        return {
            "error": str(e),
            "detail": traceback.format_exc(),
            "deepseek": {"decision": "HOLD", "confidence": 0},
            "hunyuan": {"decision": "HOLD", "confidence": 0},
            "meta": {"decision": "HOLD", "confidence": 0, "risk_level": "extreme", "reasoning": f"系统错误: {e}"},
        }


def trigger_initial_decision(rounds: int = 1) -> None:
    """
    启动自触发决策：在后台线程跑一次双模型辩论，使「辩论擂台」冷启动即有内容。
    失败不影响其他模块（LLM 不可用 / 超时均被吞掉）。
    """
    try:
        engine = _get_debate_engine()
        engine.decide(debate_rounds=rounds)
        logger.info("[AI] 启动自触发决策完成，辩论擂台已就绪")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[AI] 启动自触发决策失败(不影响其他模块): {e}")


# ──────────────────────────────────────────────────────────────
# AI 工作剧场：辩论擂台 / 进化时间线 / 交易执行流 三个面板的真实数据源
# ──────────────────────────────────────────────────────────────
def _truncate_reasoning(text: str, max_len: int = 180) -> str:
    if not text:
        return ""
    t = str(text).replace("\n", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _scalar(x):
    """把 Chronos 返回的分位值（可能是 float / numpy 标量 / list / ndarray）统一转成 float。

    根因：get_ai_flow 曾用 f"{_p10:.2f}" 直接格式化，而 chronos_p10 等字段实际是
    list / ndarray，触发 TypeError: unsupported format string passed to list.__format__
    → /api/dashboard/ai-flow 直接 500。这里容错提取首元素为标量，失败返回 None。
    2026-08-09 增强：递归剥壳，处理 list/ndarray 多层嵌套（如 [[value]]）。
    """
    if x is None:
        return None
    # 先尝试当标量转 float（覆盖 float/int/numpy scalar）
    try:
        return float(x)
    except Exception:
        pass
    # 可索引对象：递归取第一个元素
    try:
        if hasattr(x, "__len__") and len(x) > 0:
            return _scalar(x[0])
    except Exception:
        pass
    return None


@router.get("/ai-flow")
async def get_ai_flow(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    debate_limit: int = 12,
    evolution_limit: int = 16,
    feed_limit: int = 20,
):
    """
    AI 工作剧场：拉取「辩论快照 / 进化日志 / 交易执行流」三类真实数据

    优先级：先取 debate_engine 内存中最近一次辩论（带完整原始论据），
    其余槽位从 trades + evolution_logs 历史表回填。
    """
    import json as _json

    # ── 1. 辩论擂台 ──
    debate_items = []
    engine = _get_debate_engine()
    # ★ 2026-08-14：副驾第5路票（Qwen3-8B 常态确认型副驾），供前端渲染，缺快照时为 None
    _copilot_vote = None
    if engine.last_debate:
        snap = engine.last_debate
        dec = snap.get("decision")
        # ★ 真实 Meta 权重（与「裁决」卡一致），不再回退到模型自评准确率
        ds_w = round(float(getattr(dec, "deepseek_weight", 0.5) or 0.5), 2)
        hy_w = round(float(getattr(dec, "hunyuan_weight", 0.5) or 0.5), 2)

        def _reasoning_with_factors(item: dict) -> str:
            """推理文本 + 关键因子合并，保证辩论卡永远有实质内容（不空）。
            反驳轮可能只返回 revised_reasoning，也作为 reasoning 回退。"""
            r = (item or {}).get("reasoning") or ""
            if not r:
                r = (item or {}).get("revised_reasoning") or ""
            kf = (item or {}).get("key_factors") or []
            if kf:
                kf_txt = "；".join(kf) if isinstance(kf, list) else str(kf)
                return ((r + " ｜ 关键因子: " + kf_txt).strip("｜ ").strip()) if r else kf_txt
            return r

        ds = snap.get("ds_final") or snap.get("ds_initial") or {}
        hy = snap.get("hy_final") or snap.get("hy_initial") or {}
        # DeepSeek 一条
        debate_items.append({
            "ts": snap.get("ts"),
            "who": "ds",
            "model": "DeepSeek",
            "decision": ds.get("decision", "HOLD"),
            "confidence": round(float(ds.get("confidence", 0)) * 100, 0),
            "weight": ds_w,
            "stance": "激进" if ds.get("decision") == "BUY" else ("保守" if ds.get("decision") == "SELL" else "观望"),
            "reasoning": _truncate_reasoning(
                _reasoning_with_factors(ds)
                or _reasoning_with_factors(snap.get("ds_initial") or {})
                or ""
            ),
            "round": snap.get("rounds", 1),
        })
        # Hunyuan 一条
        debate_items.append({
            "ts": snap.get("ts"),
            "who": "hy",
            "model": "混元",
            "decision": hy.get("decision", "HOLD"),
            "confidence": round(float(hy.get("confidence", 0)) * 100, 0),
            "weight": hy_w,
            "stance": "激进" if hy.get("decision") == "BUY" else ("保守" if hy.get("decision") == "SELL" else "稳健"),
            "reasoning": _truncate_reasoning(
                _reasoning_with_factors(hy)
                or _reasoning_with_factors(snap.get("hy_initial") or {})
                or ""
            ),
            "round": snap.get("rounds", 1),
        })
        # ★ Phase 4：Chronos 第三票。
        #   此前辩论擂台只画两条气泡，本地时序模型的意见在前端完全不可见 ——
        #   而它恰恰是云端断线时唯一还在工作的模型，把它藏起来等于让客户
        #   在最需要信任系统的时刻看到一片空白。
        #   available=False 必须显示「未参与」而不是「投了 HOLD」，
        #   两者票面同为 HOLD 但含义相反（见 decision_snapshot 模块头「语义契约」）。
        if dec is not None:
            _cw = float(getattr(dec, "chronos_weight", 0.0) or 0.0)
            _cv = str(getattr(dec, "chronos_vote", "HOLD") or "HOLD")
            _avail = _cw > 0.0
            _p10 = _scalar(getattr(dec, "chronos_p10", None))
            _p50 = _scalar(getattr(dec, "chronos_p50", None))
            _p90 = _scalar(getattr(dec, "chronos_tp_ceiling", None))
            _q = _scalar(getattr(dec, "q_score", None))
            if _avail:
                _bits = []
                if _p10 is not None and _p90 is not None:
                    _bits.append(f"分位区间 P10={_p10:.2f} / P50={_p50:.2f} / P90={_p90:.2f}"
                                 if _p50 is not None else f"分位区间 P10={_p10:.2f} / P90={_p90:.2f}")
                if _q is not None:
                    _bits.append(f"质量分 Q={float(_q):.2f}")
                _bits.append("与云端一致" if getattr(dec, "chronos_agree", False) else "与云端分歧")
                _creason = "；".join(_bits)
            else:
                _creason = "本地时序模型本轮未参与加权（服务未就绪或预测不可用）"
            debate_items.append({
                "ts": snap.get("ts"),
                "who": "chronos",
                "model": "Chronos-2",
                "decision": _cv if _avail else "N/A",
                "confidence": round(_cw * 100, 0),
                "weight": round(_cw, 2),
                "available": _avail,
                "stance": "时序" if _avail else "未参与",
                "reasoning": _creason,
                "round": snap.get("rounds", 1),
            })

        # Meta 裁决
        dec = snap.get("decision")
        if dec:
            debate_items.append({
                "ts": snap.get("ts"),
                "who": "meta",
                "model": "Meta 裁决",
                "decision": dec.decision,
                "confidence": round(float(dec.confidence) * 100, 0),
                "weight": 1.0,
                "stance": "终裁",
                "reasoning": _truncate_reasoning(dec.reasoning_summary, 220),
                "round": snap.get("rounds", 1),
                "plain_summary": getattr(dec, "plain_summary", "") or "",
                "consensus": getattr(dec, "consensus", "") or "",
                "consensus_pct": _CONSENSUS_PCT.get(getattr(dec, "consensus", ""), 50),
                "quality_regime": getattr(dec, "quality_regime", "") or "",
                "chronos_tp_ceiling": getattr(dec, "chronos_tp_ceiling", None),
                # 完整快照挂在 Meta 卡上：前端画分位带 / 降级徽标 / 仓位意图都从这里取，
                # 不再各自 getattr 拼字段（那正是命名分裂的来源）。
                "provenance": build_decision_snapshot(dec),
            })

            # ★ 2026-08-14：副驾第5路票（Qwen3-8B 常态确认型副驾，进融合投票）
            #   从冻结快照取 votes.copilot（vote/weight/confidence/agree/available/note），
            #   前端「本地多模型融合擂台」据此渲染第5路确认票气泡。
            _copilot_vote = build_decision_snapshot(dec).get("copilot") or {}

    # 历史回填（从 trades 取最近 meta_agent 决策 + 双方观点）
    hist_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.meta_agent_decision.isnot(None),
    ).order_by(Trade.created_at.desc()).limit(debate_limit * 2).all()

    seen_ts = {it.get("ts") for it in debate_items if it.get("ts")}
    for t in hist_trades:
        ts = _to_utc_iso(t.created_at) or ""
        if ts in seen_ts:
            continue
        if t.deepseek_decision:
            debate_items.append({
                "ts": ts,
                "who": "ds",
                "model": "DeepSeek",
                "decision": t.deepseek_decision,
                "confidence": round(float(t.deepseek_confidence or 0) * 100, 0),
                "weight": round(float(t.deepseek_confidence or 0), 2),
                "stance": "激进" if t.deepseek_decision == "BUY" else "保守",
                "reasoning": _truncate_reasoning(t.deepseek_reasoning or t.debate_summary, 180),
                "round": 1,
            })
        if t.hunyuan_decision:
            debate_items.append({
                "ts": ts,
                "who": "hy",
                "model": "混元",
                "decision": t.hunyuan_decision,
                "confidence": round(float(t.hunyuan_confidence or 0) * 100, 0),
                "weight": round(float(t.hunyuan_confidence or 0), 2),
                "stance": "稳健" if t.hunyuan_decision != "BUY" else "激进",
                "reasoning": _truncate_reasoning(t.hunyuan_reasoning or t.debate_summary, 180),
                "round": 1,
            })
        if len(debate_items) >= debate_limit * 3:
            break

    # ── 2. 进化时间线（内存实时缓冲优先，DB 兜底历史）──
    evo_rows = db.query(EvolutionLog).filter(
        EvolutionLog.user_id == user.id,
    ).order_by(EvolutionLog.created_at.desc()).limit(evolution_limit).all()

    _kind_label = {
        "weight_update": "权重更新",
        "trade_review": "订单复盘",
        "consensus": "共识事件",
        "regime_switch": "体制切换",
        "risk_promote": "风险升级",
        "mfe_promote": "MFE 回灌",
        "init": "引擎初始化",
        "cycle_record": "轮次裁决",
    }

    evolution_items = []
    # 内存实时事件（最新在前）
    for e in reversed(mem_get_evolution(evolution_limit)):
        evolution_items.append({
            "ts": e.get("ts"),
            "kind": e.get("kind", "init"),
            "label": e.get("label") or _kind_label.get(e.get("kind", ""), e.get("kind", "init")),
            "subject": e.get("subject") or "-",
            "before": e.get("before"),
            "after": e.get("after"),
            "delta": e.get("delta"),
            "reason": e.get("reason") or "",
        })
    # DB 历史兜底（Defender 未锁库时补充更早记录，去重以 ts+kind 为准）
    _seen = {(it.get("ts"), it.get("kind")) for it in evolution_items}
    for e in evo_rows:
        key = (_to_utc_iso(e.created_at), e.kind)
        if key in _seen:
            continue
        evolution_items.append({
            "ts": _to_utc_iso(e.created_at),
            "kind": e.kind,
            "label": _kind_label.get(e.kind, e.kind),
            "subject": e.subject or "-",
            "before": e.before_value,
            "after": e.after_value,
            "delta": e.delta,
            "reason": e.reason or "",
        })
    evolution_items = evolution_items[:evolution_limit]

    # ── 3. 交易执行流（AI 活动流：扫描 / 评估 / 信号 / 开仓 / 平仓）──
    feed_rows = db.query(AIActivity).filter(
        AIActivity.user_id == user.id,
    ).order_by(AIActivity.created_at.desc()).limit(feed_limit).all()

    def _make_feed_text(a: dict) -> str:
        """生成 feed 文本（不含账户名前缀，账户名由前端徽章单独渲染）"""
        return a.get("detail") or a.get("text") or ""

    _feed_tag_map = {
        "scan": ("t-mon", "扫描"),
        "evaluate": ("t-mon", "评估"),
        "signal": ("t-sig", "信号"),
        "open": ("t-buy", "开仓"),
        "close": ("t-ok", "平仓"),
        "close_partial": ("t-partial", "部分平"),
        "sl": ("t-loss", "止损"),
    }
    feed_items = []
    # ── 3a. 独立交易缓冲（open/close/sl，不被扫描淹没）──
    for a in reversed(mem_get_trades(feed_limit)):
        k = a.get("kind", "open")
        tag, tag_text = _feed_tag_map.get(k, ("t-mon", "事件"))
        feed_items.append({
            "ts": a.get("ts"),
            "kind": k,
            "tag": tag,
            "tag_text": tag_text,
            "text": _make_feed_text(a),
            "direction": a.get("direction") or "",
            "confidence": round(float(a.get("confidence") or 0) * 100, 0) if a.get("confidence") else 0,
            "timeframe": a.get("timeframe") or "",
            "account_name": a.get("account_name") or "",
            "account_login": a.get("account_login") or "",
        })
    # ── 3b. 扫描/评估事件（补充填充剩余位）──
    mem_acts = list(reversed(mem_get_activities(feed_limit * 2)))
    for a in mem_acts:
        k = a.get("kind", "scan")
        if k not in ("scan", "evaluate"):
            continue
        if len(feed_items) >= feed_limit:
            break
        tag, tag_text = _feed_tag_map.get(k, ("t-mon", "事件"))
        feed_items.append({
            "ts": a.get("ts"),
            "kind": k,
            "tag": tag,
            "tag_text": tag_text,
            "text": a.get("detail") or a.get("text") or "",
            "direction": a.get("direction") or "",
            "confidence": round(float(a.get("confidence") or 0) * 100, 0) if a.get("confidence") else 0,
            "timeframe": a.get("timeframe") or "",
            "account_name": "",
            "account_login": "",
        })
    # DB 历史兜底（带账户信息）
    _seen_feed = {(it.get("ts"), it.get("kind")) for it in feed_items}
    for a in feed_rows:
        key = (_to_utc_iso(a.created_at), a.kind)
        if key in _seen_feed:
            continue
        if len(feed_items) >= feed_limit:
            break
        tag, tag_text = _feed_tag_map.get(a.kind, ("t-mon", "事件"))
        # 查询账户名
        acct_name = ""
        acct_login = ""
        if a.mt5_account_id:
            acct = db.query(MT5Account).filter(MT5Account.id == a.mt5_account_id).first()
            if acct:
                acct_name = acct.name or ""
                acct_login = acct.account_id or ""
        feed_items.append({
            "ts": _to_utc_iso(a.created_at),
            "kind": a.kind,
            "tag": tag,
            "tag_text": tag_text,
            "text": a.detail or "",
            "direction": a.direction or "",
            "confidence": round(float(a.confidence or 0) * 100, 0) if a.confidence else 0,
            "timeframe": a.timeframe or "",
            "account_name": acct_name,
            "account_login": acct_login,
        })
    feed_items = feed_items[:feed_limit]

    # ── 4. 计数器（商业化卖点：AI 一直在干活；内存 + DB 双重统计）──
    today = datetime.utcnow().date()
    today_iso = today.isoformat()  # 形如 "2026-08-03"，ISO 字符串比较即按日切
    _db_evo = db.query(EvolutionLog).filter(EvolutionLog.user_id == user.id).count()
    _db_dec = db.query(AIActivity).filter(
        AIActivity.user_id == user.id,
        AIActivity.created_at >= today,
    ).count()
    _db_scan = db.query(AIActivity).filter(
        AIActivity.user_id == user.id,
        AIActivity.created_at >= today,
        AIActivity.kind.in_(["scan", "signal", "evaluate"]),
    ).count()
    ai_iterations = mem_count_evolution() + _db_evo
    decisions_today = mem_count_activities_since(today_iso) + _db_dec
    scans_today = mem_count_by_kinds(today_iso, ("scan", "signal", "evaluate")) + _db_scan

    # ── v4 Meta 质量陪审团 / Chronos 时序模型：供前端「AI 信号质量」可视化 ──
    # ★ 关键修复：交易引擎(trade_executor)与 dashboard 引擎是两套独立 DebateEngine 实例，
    #   dashboard 引擎从没跑过交易 cycle，get_last_context() 在其上恒为空 → 前端永远拿不到
    #   meta_quality。故此处直接对最新行情快照「实时计算」meta_quality（Chronos 已懒加载常驻，
    #   推理约 1s），既与引擎实例解耦，又保证大屏数据永远是最新的真实 Chronos 制衡结果。
    #   加 20s TTL 缓存，避免前端高频轮询反复打 GPU 推理。
    global _MQ_CACHE
    _now_ts = time.time()
    if _MQ_CACHE.get("v") and (_now_ts - _MQ_CACHE.get("t", 0)) < 20:
        _mq = _MQ_CACHE["v"]
    else:
        _mq = {}
        try:
            from app.services.meta_quality import evaluate_meta_quality_async
            _snap = engine.market.get_market_snapshot()
            if not _snap.get("error") and not _snap.get("simulated"):
                _mq = await evaluate_meta_quality_async(_snap) or {}
                _MQ_CACHE = {"t": _now_ts, "v": _mq}
        except Exception as _mqe:
            logger.warning(f"[Dashboard] Meta质量陪审团实时计算失败(降级空): {_mqe}")
            _mq = {}

    # 2026-08-12：本地模式决策溯源链路（右侧辩论擂台关云态可视化）
    _local_models = _collect_local_models()
    _fusion_vote = _collect_fusion_vote()
    _proofread = _collect_proofread(engine)
    _live_status = _collect_live_status()

    # ── 2026-08-17：篮子级 AI 持仓管理（用户铁律可视化）──
    #   从最近一条含 basket 的决策快照读最新持仓处置（hold/trim/close_all + 确认态），
    #   前端 AI 工作剧场据此展示"AI 正在护仓"。无记录/空仓 → 前端自然显示"无持仓待处置"。
    _basket = _collect_latest_basket()

    # ── 2026-08-17：投票席严谨可视化（用户要求：擂台必须体现多少模型参与裁决）──
    #   从最近决策快照提取 votes（DS/HY/Chronos/fusion/vision/copilot 六票真实状态：
    #   方向/权重/是否生效），前端据此渲染"投票席"，客户一眼看清本轮谁投了票、谁观望、谁未启用。
    _voting = _collect_latest_votes()

    return {
        "debate": debate_items,
        "evolution": evolution_items,
        "feed": feed_items,
        "basket": _basket,
        "voting": _voting,
        "weights": {
            "deepseek": round(engine.meta_agent.deepseek_perf.recent_accuracy, 3),
            "hunyuan": round(engine.meta_agent.hunyuan_perf.recent_accuracy, 3),
            "deepseek_signals": engine.meta_agent.deepseek_perf.total_signals,
            "hunyuan_signals": engine.meta_agent.hunyuan_perf.total_signals,
        },
        "counters": {
            "ai_iterations": ai_iterations,
            "decisions_today": decisions_today,
            "scans_today": scans_today,
        },
        # v4 本地模型制衡可视化数据
        "meta_quality": _mq,
        # ★ 2026-08-11 全盘可视化：本地多模型融合擂台数据源
        "cloud_enabled": _cloud_models_enabled(),
        "local_models": _local_models,
        "fusion_vote": _fusion_vote,
        "proofread": _proofread,
        # 实时状态：自动交易循环 + Key 来源（让仪表盘一眼看清 AI 是否在工作）
        "live_status": _live_status,
        # ★ 2026-08-12：本地模式右侧「决策溯源」专用链路数据
        "local_trace": _build_local_trace(engine, _local_models, _fusion_vote, _proofread, _live_status),
        # ★ 2026-08-14：副驾第5路票（Qwen3-8B 常态确认型副驾），前端独立渲染
        "copilot_vote": _copilot_vote,
        "ts": datetime.now().isoformat(),
    }


def _collect_latest_basket() -> dict:
    """从最近决策快照提取篮子级持仓处置（2026-08-17）。

    返回 {"available": bool, "action": str, "confirmed": bool, ...}。
    多账号场景取最近一条含 basket.action != hold 的快照（hold 无信息量），
    全部为空/无记录 → available=False，前端显示「暂无持仓处置」。
    """
    try:
        from sqlalchemy import desc
        from app.models.trade import Trade
        # ★ 2026-08-17 修复：get_db 是 FastAPI 依赖，不能直接调（generator 无 query）；
        #   用 SessionLocal 直接开 session。
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            _rows = _db.query(Trade).filter(
                Trade.decision_snapshot.isnot(None),
                Trade.decision_snapshot != "",
            ).order_by(desc(Trade.created_at)).limit(20).all()
        finally:
            _db.close()
        _latest = {"available": False, "action": "hold", "confirmed": False,
                   "confidence": 0.0, "reason": "", "confirm_note": ""}
        for _t in _rows:
            try:
                import json as _json
                _snap = _json.loads(_t.decision_snapshot or "{}")
            except Exception:  # noqa: BLE001
                continue
            _b = _snap.get("basket") or {}
            if not isinstance(_b, dict):
                continue
            _act = str(_b.get("action") or "hold")
            # 空仓态默认 hold 无信息量，跳过；有处置意图（trim/close_all）才展示
            if _act in ("hold",):
                continue
            _latest = {
                "available": True,
                "action": _act,
                "confirmed": bool(_b.get("confirmed")),
                "confidence": float(_b.get("confidence") or 0),
                "reason": str(_b.get("reason") or "")[:160],
                "confirm_note": str(_b.get("confirm_note") or ""),
                "ts": str(_t.created_at or "")[:19],
            }
            break
        return _latest
    except Exception as _be:  # noqa: BLE001
        logger.warning(f"[ai-flow] 篮子快照采集失败: {_be}")
        return {"available": False, "action": "hold", "confirmed": False}


def _collect_latest_votes() -> dict:
    """从最近决策快照提取投票席真实状态（2026-08-17 · 严谨可视化）。

    返回 {"available": bool, "decision": str, "confidence": float, "seats": [..]}
    seats 每席：{key, name, vote, weight, available(是否计票), role}
    available 判定与裁决器一致：weight>0 且 vote∈(BUY/SELL) 才算"本票已计"；
    HOLD 但 weight>0 = "观望未计票"；weight=0 = "未参与/未启用"。
    """
    try:
        from sqlalchemy import desc
        from app.models.trade import Trade
        # ★ 2026-08-17 修复：get_db 是 FastAPI 依赖（yield generator），不能直接调；
        #   用 SessionLocal 直接开 session，用完关闭（与 _collect_latest_basket 同构）。
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            _rows = _db.query(Trade).filter(
                Trade.decision_snapshot.isnot(None),
                Trade.decision_snapshot != "",
            ).order_by(desc(Trade.created_at)).limit(10).all()
        finally:
            _db.close()
        for _t in _rows:
            try:
                import json as _json
                _snap = _json.loads(_t.decision_snapshot or "{}")
            except Exception:  # noqa: BLE001
                continue
            _votes = (_snap or {}).get("votes") or {}
            if not isinstance(_votes, dict) or not _votes:
                continue
            _order = [
                ("deepseek", "DeepSeek", "云端主脑·激进派"),
                ("hunyuan", "混元", "云端主脑·稳健派"),
                ("chronos", "Chronos-2", "时序锚·风险区间"),
                ("fusion", "融合票(4时序)", "第四票·聚合"),
                ("vision", "Vision(7b)", "视觉结构票"),
                ("copilot", "Qwen3-8B", "确认型副驾"),
            ]
            _seats = []
            for _k, _name, _role in _order:
                _v = _votes.get(_k) or {}
                if not isinstance(_v, dict):
                    continue
                _vote = str(_v.get("vote") or "HOLD")
                _w = float(_v.get("weight") or 0)
                _conf = float(_v.get("confidence") or 0)
                _is_dir = _vote in ("BUY", "SELL")
                _counted = _w > 0 and _is_dir
                _seat = {
                    "key": _k,
                    "name": _name,
                    "role": _role,
                    "vote": _vote,
                    "weight": round(_w, 3),
                    "confidence": round(_conf, 3),
                    "counted": _counted,
                    # 状态三态：counted=已计票 / weight>0=观望未计 / else=未参与
                    "state": "counted" if _counted else ("watch" if _w > 0 else "absent"),
                }
                if _k == "fusion":
                    _seat["models"] = int(_v.get("models") or 0)
                    _seat["note"] = str(_v.get("note") or "")[:80]
                _seats.append(_seat)
            _counted_n = sum(1 for s in _seats if s["counted"])
            return {
                "available": True,
                "decision": str(_snap.get("decision") or "HOLD"),
                "confidence": float(_snap.get("confidence") or 0),
                "counted_seats": _counted_n,
                "total_seats": len(_seats),
                "seats": _seats,
                "ts": str(_t.created_at or "")[:19],
                # ★ 2026-08-17：HOLD 决策不落 trades → 快照可能滞后几小时。
                #   标注"最近一次含票裁决的时间"，前端明示是历史裁决而非本轮实时。
                "is_latest": False,
            }
        return {"available": False, "seats": [], "counted_seats": 0, "total_seats": 0}
    except Exception as _ve:  # noqa: BLE001
        logger.warning(f"[ai-flow] 投票席采集失败: {_ve}")
        return {"available": False, "seats": [], "counted_seats": 0, "total_seats": 0}


def _collect_live_status() -> dict:
    """
    收集当前实时状态：自动循环运行状态 + Key 来源 + 云模型有效开关。
    前端 AI 工作剧场 / 仪表盘顶部用此判断"AI 是否在工作"。
    """
    from app.services.key_pool import get_all_pools
    from app.services.cloud_switch import cloud_status as _cloud_status
    from app.routers.trading import _auto_status, _auto_thread, _auto_running, _start_auto_internal  # 同一进程内全局状态

    # ── 自愈守卫：自动交易线程意外死亡时（如 lifespan 启动失败 / 异常退出），
    #    在仪表盘轮询（每3秒必调本函数）时自动重新拉起，确保"AI 在工作"不依赖手动操作 ──
    _thread_alive = bool(_auto_thread and _auto_thread.is_alive())
    if (not _thread_alive) and _auto_running:
        # 标记说在跑但其实线程已死 → 重置并重启
        logger.warning("[自愈] 自动交易线程已丢失，重新拉起")
        try:
            _start_auto_internal()
            _thread_alive = True
        except Exception as _e:
            logger.error(f"[自愈] 重新拉起失败: {_e}")
    auto_loop = dict(_auto_status or {})  # 复制一份，避免引用冲突
    auto_loop["thread_alive"] = _thread_alive

    # Key 来源：DB / .env fallback
    key_sources: dict[str, str] = {}
    for provider, pool in get_all_pools().items():
        if pool.size() == 0:
            key_sources[provider] = "missing"
        else:
            # 看pool里是否有env fallback虚拟item
            has_env = any(getattr(it, "is_env_fallback", False) for it in pool.items)
            only_env = has_env and pool.size() == 1
            if only_env:
                key_sources[provider] = "env_fallback_only"
            elif has_env:
                key_sources[provider] = "db+env"
            else:
                key_sources[provider] = "db"

    # 云端模型有效状态：主开关 AND 至少一个可用 Key 源
    _cs = _cloud_status()

    return {
        "auto_loop": auto_loop,
        "key_sources": key_sources,
        "cloud_enabled": _cs["effective_enabled"],
        "cloud_master_enabled": _cs["master_enabled"],
        "cloud_mode_label": _cs["mode_label"],
        "cloud_sub_label": _cs["sub_label"],
        "ts": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────────────────────
# 2026-08-12：本地模式「决策溯源·辩论擂台」专用链路
#   云模型关闭时，右侧辩论区不能空白，要把本地 4 时序模型 → 融合法官 →
#   Qwen3-8B 校对 → Meta-Agent 终裁 → 交易执行 的完整决策链可视化出来。
# ──────────────────────────────────────────────────────────────
def _build_local_trace(
    engine,
    local_models: list,
    fusion_vote: dict,
    proofread: dict,
    live_status: dict,
) -> list:
    """构造本地模式决策溯源 trace（右侧辩论擂台关云态数据）。"""
    trace = []
    ts_now = datetime.now().isoformat()

    # 1) 4 个本地时序模型：每人一票
    #    从 fusion_vote.per_model 按模型名找质量权重 qw，比单用 score 更准。
    _qw_map = {
        (pm.get("name") or "").strip(): float(pm.get("qw") or 0)
        for pm in (fusion_vote.get("per_model") or [])
        if pm.get("name")
    }
    for m in local_models or []:
        key = m.get("key") or ""
        if not key.startswith("ts_"):
            continue
        direction = m.get("direction") or "HOLD"
        confidence = round(float(m.get("confidence") or 0) * 100, 0)
        score = float(m.get("score") or 0)
        name = (m.get("name") or key.replace("ts_", "")).strip()
        qw = _qw_map.get(name, score)
        lo = m.get("lo")
        hi = m.get("hi")
        hit = m.get("hit_rate")
        bits = []
        if lo is not None and hi is not None:
            bits.append(f"预测区间 [{lo:.2f}, {hi:.2f}]")
        if score:
            bits.append(f"方向强度 {score:.2f}")
        if hit is not None:
            bits.append(f"命中率 {float(hit):.0%}")
        trace.append({
            "ts": ts_now,
            "who": key,
            "model": name,
            "decision": direction,
            "confidence": confidence,
            "weight": round(qw if qw else abs(score), 2),
            "available": bool(m.get("available")),
            "stance": "时序",
            "reasoning": "；".join(bits) or f"{direction} @ {confidence}%",
        })

    # 2) 融合法官（fusion_v2 第四票）
    if fusion_vote and fusion_vote.get("available"):
        direction = fusion_vote.get("direction") or "HOLD"
        confidence = round(float(fusion_vote.get("confidence") or 0) * 100, 0)
        note = fusion_vote.get("note") or ""
        trace.append({
            "ts": ts_now,
            "who": "fusion",
            "model": "融合法官",
            "decision": direction,
            "confidence": confidence,
            "weight": round(float(fusion_vote.get("weight_scale") or 0), 2),
            "stance": "融合",
            "reasoning": (
                f"{fusion_vote.get('model_count', 0)} 个时序模型加权融合，"
                f"平均命中率 {float(fusion_vote.get('hit_rate_avg') or 0):.0%}。"
                f"{note}"
            ).strip(),
        })

    # 2.5) Qwen3-8B 副驾第5路确认票（常态确认型副驾，仅 Chronos 有明确方向时发声）
    #   与「校对员」是同一模型的两个角色：校对是事后查结构一致性，副驾是事前同向确认加权重。
    _cop_v = None
    try:
        _snap = engine.last_debate
        _dec = _snap.get("decision") if _snap else None
        if _dec is not None:
            _cop_v = build_decision_snapshot(_dec).get("copilot") or {}
    except Exception:  # noqa: BLE001
        _cop_v = None
    if _cop_v and _cop_v.get("available"):
        _cop_dir = _cop_v.get("vote") or "HOLD"
        _cop_w = round(float(_cop_v.get("weight") or 0), 2)
        _cop_conf = round(float(_cop_v.get("confidence") or 0) * 100, 0)
        trace.append({
            "ts": ts_now,
            "who": "copilot",
            "model": "Qwen3-8B 副驾",
            "decision": _cop_dir,
            "confidence": _cop_conf,
            "weight": _cop_w,
            "stance": "副驾",
            "available": True,
            "reasoning": (
                f"常态确认型副驾：与 Chronos 方向同向时加权重 {_cop_w}"
                + (f"；{_cop_v.get('note')}" if _cop_v.get("note") else "")
            ).strip(),
        })

    # 3) Qwen3-8B 校对员
    if proofread:
        status = proofread.get("status") or "skipped"
        issues = proofread.get("issues") or []
        action = proofread.get("action") or ""
        trace.append({
            "ts": proofread.get("ts") or ts_now,
            "who": "qwen",
            "model": "Qwen3-8B 校对员",
            "decision": proofread.get("decision") or "HOLD",
            "confidence": 0,
            "stance": "校对",
            "reasoning": (
                f"校对状态：{status}"
                + (f"；发现问题：{'；'.join(issues)}" if issues else "")
                + (f"；处置：{action}" if action else "")
            ).strip(),
        })

    # 4) Meta-Agent 终裁
    snap = engine.last_debate
    if snap:
        dec = snap.get("decision")
        if dec:
            trace.append({
                "ts": snap.get("ts") or ts_now,
                "who": "meta",
                "model": "Meta 裁决",
                "decision": dec.decision,
                "confidence": round(float(dec.confidence) * 100, 0),
                "weight": 1.0,
                "stance": "终裁",
                "reasoning": _truncate_reasoning(
                    getattr(dec, "reasoning_summary", None) or getattr(dec, "summary", None) or str(dec.decision),
                    220,
                ),
                "plain_summary": getattr(dec, "plain_summary", "") or "",
            })

    # 5) 交易执行（最新一次 auto_loop 真实成交）
    auto_loop = live_status.get("auto_loop") or {}
    last_result = auto_loop.get("last_result") or {}
    orders = last_result.get("orders") or []
    if orders:
        first = orders[0]
        decision_dict = last_result.get("decision") or {}
        trace.append({
            "ts": last_result.get("timestamp") or ts_now,
            "who": "execute",
            "model": "交易执行",
            "decision": first.get("type") or decision_dict.get("action") or "HOLD",
            "confidence": round(float(decision_dict.get("confidence") or 0) * 100, 0),
            "stance": "已执行",
            "reasoning": (
                f"主号 {first.get('account_name') or first.get('account_login') or '主账号'} "
                f"开仓 {first.get('volume')} 手 @ {first.get('price')}，"
                f"SL {first.get('sl')} / TP {first.get('tp')}；"
                f"同步跟单 {len(orders) - 1} 个账号。"
            ),
        })

    return trace


# ──────────────────────────────────────────────────────────────
# 全盘可视化（2026-08-11）：本地多模型融合擂台数据源聚合
#   local_models  → 5 个本地模型实时气泡（Qwen3-8B 走 GPU，其余 4 时序走 CPU）
#   fusion_vote   → 4 时序模型加权融合裁判票（fusion_v2 第四票）
#   proofread     → 最近一次 Qwen3-8B 校对挑战印章
# 三者供前端「本地多模型融合擂台」渲染，与云端双脑辩论并列展示。
# ──────────────────────────────────────────────────────────────
def _cloud_models_enabled() -> bool:
    """云端双脑有效开关（与 debate_engine 同口径）。True=仍跑 DeepSeek/混元。"""
    try:
        from app.services.cloud_switch import effective_cloud_enabled

        return effective_cloud_enabled()
    except Exception:
        return True


def _collect_local_models() -> list:
    """5 个本地模型实时气泡（供前端融合擂台）。

    模型清单（与用户 2026-08-10 决策一致）：
      · Qwen3-8B          —— GPU，校对员/副驾（L2）
      · Chronos-2(120M)   —— CPU，风险区间/动态止盈
      · TimesFM-2.5(200M) —— CPU，时序预测
      · Time-MoE(200M)    —— CPU，时序预测
      · Moirai(447M)      —— CPU（独立 venv 子进程），时序预测
    """
    out: list = []
    # ── 4 个本地时序模型（CPU）──
    try:
        from app.services.ts_reference_service import get_service as get_ts

        # 幂等启动：首次访问擂台即触发模型加载线程，避免依赖用户先打开参考面板
        get_ts().ensure_started()
        snap = get_ts().get_snapshot()
        for m in (snap.get("models") or []):
            out.append({
                "key": "ts_" + str(m.get("name") or ""),
                "name": m.get("name"),
                "device": "CPU",
                "role": "时序预测",
                "direction": m.get("direction", "HOLD"),
                "available": bool(m.get("available")),
                "confidence": m.get("confidence"),
                "score": m.get("score"),
                "lo": m.get("lo"),
                "hi": m.get("hi"),
                "pred_end": m.get("pred_end"),
                "hit_rate": m.get("hit_rate"),
                "color": m.get("color") or "var(--blue)",
            })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ai-flow] 本地时序模型快照取失败: {e}")

    # ── Qwen3-8B（GPU）──
    try:
        from app.services.local_llm_service import status_dict as _qwen_status

        q = _qwen_status() or {}
        _roles = q.get("roles") or {}
        out.append({
            "key": "qwen",
            "name": "Qwen3-8B",
            "device": "GPU",
            "role": "常态确认型副驾 · 校对员(L2)",
            "direction": "N/A",
            "available": bool(q.get("available")),
            "warmed": bool(q.get("warmed")),
            "model": q.get("model"),
            "reason": q.get("reason"),
            "proofread_runs": (_roles.get("proofreader") or {}).get("runs", 0),
            "copilot_allowed": (_roles.get("copilot") or {}).get("allowed", 0),
            "color": "var(--purple)",
        })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ai-flow] Qwen 状态取失败: {e}")

    return out


def _collect_fusion_vote() -> dict:
    """融合票（fusion_v2 第四票）序列化，供前端「融合法官」渲染。"""
    try:
        from app.services.fusion_service import get_service as get_fusion

        fv = get_fusion().get_fusion_vote()
        return {
            "available": fv.available,
            "direction": fv.direction,
            "confidence": fv.confidence,
            "score": fv.score,
            "lo": fv.lo,
            "hi": fv.hi,
            "agree": fv.agree,
            "hit_rate_avg": fv.hit_rate_avg,
            "model_count": fv.model_count,
            "weight_scale": fv.weight_scale,
            "per_model": fv.per_model,
            "note": fv.note,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ai-flow] 融合票聚合失败: {e}")
        return {"available": False, "note": "融合票不可用"}


def _collect_proofread(engine) -> dict:
    """最近一次 Qwen3-8B 校对印章（从最近一次辩论裁决快照取，避免重复推理）。"""
    try:
        snap = engine.last_debate
        if not snap:
            return {"status": "skipped", "skip_reason": "no_snapshot", "reason": "尚无辩论快照"}
        dec = snap.get("decision")
        if not dec:
            return {"status": "skipped", "skip_reason": "no_snapshot", "reason": "尚无裁决"}
        prov = build_decision_snapshot(dec)
        pr = prov.get("proofread") or {}
        # ★ 2026-08-17：skipped 语义区分——HOLD 决策跳过（设计行为）vs 模型不可用（告警）
        _st = pr.get("status", "skipped")
        _skip_reason = ""
        if _st == "skipped":
            _skip_reason = "hold" if prov.get("decision") == "HOLD" else "unavailable"
        return {
            "status": _st,
            "skip_reason": _skip_reason,
            "issues": pr.get("issues", []),
            "severity": pr.get("severity", "none"),
            "latency_ms": pr.get("latency_ms"),
            "blocked": pr.get("blocked", False),
            "action": pr.get("action", ""),
            "decision": prov.get("decision"),
            "ts": snap.get("ts"),
        }
    except Exception:  # noqa: BLE001
        return {"status": "skipped", "skip_reason": "unavailable", "reason": "提取失败"}


# ──────────────────────────────────────────────────────────────
# 风控事件流：回答「为什么这段时间一单没开」
# ──────────────────────────────────────────────────────────────
@router.get("/risk-events")
def get_risk_events(
    user: User = Depends(get_current_user),
    account_id: str = "",
    limit: int = 50,
):
    """最近的拦截/熔断事件 + 按原因聚合的排行。

    这个接口存在的意义，是把系统里最容易引发客户不信任的一类现象说清楚：
    AI 明明在屏幕上喊了 BUY，却没有下单。在此之前这些原因只存在于
    后端日志里（客户看不到），于是「没交易」和「系统挂了」在客户眼中
    是同一件事。

    top_reasons 是给运营看的：如果某个账号 80% 的拦截都来自
    PER_TRADE_RISK_LIMIT，那不是 AI 不干活，是这个账号的风险参数配得太紧。
    """
    try:
        events = query_risk_events(
            user_id=user.id,
            mt5_account_id=(account_id or None),
            limit=limit,
        )
        from collections import Counter
        from app.services.risk_event_log import code_label

        counter: Counter = Counter()
        for e in events:
            for c in (e.get("codes") or []):
                counter[c] += 1
        top = [
            {"code": k, "count": v, "label": code_label(k)}
            for k, v in counter.most_common(8)
        ]
        # 给每条事件补中文标签，前端不必再维护一份映射表
        for e in events:
            e["labels"] = [code_label(c) for c in (e.get("codes") or [])]
        return {"events": events, "top_reasons": top, "total": len(events)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[风控事件] 查询失败: {e}")
        return {"events": [], "top_reasons": [], "total": 0, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# AI Key 用量统计（多 Key 聚合 + token / 费用）
# ──────────────────────────────────────────────────────────────
@router.get("/ai-usage")
def get_ai_usage(user: User = Depends(get_current_user)):
    """
    返回 KeyPool 全量统计：
    - 按 provider 分组：deepseek / hunyuan
    - 每 pool：总调用次数、今日调用、token 总量、本月成本 USD
    - 每 key 明细：调用次数 / tokens / 成本 / 脱敏 key
    """
    from app.services.key_pool import get_all_stats
    return get_all_stats()


@router.get("/model-workflow")
def get_model_workflow(user: User = Depends(get_current_user)):
    """
    返回 3 个 AI 模型的工作原理概览（自洽性解释 + 当前状态）：
    - 3 个角色：DeepSeek 激进派 / Hunyuan 稳健派 / Meta 终裁
    - 每角色当前权重 / 最近准确率 / 是否在跑
    - 最近一次辩论的"是否达成共识"
    """
    engine = _get_debate_engine()
    ds_w, hy_w = engine.meta_agent.get_weights()
    last = engine.last_debate or {}

    return {
        "roles": [
            {
                "name": "DeepSeek V4",
                "alias": "激进派",
                "role_desc": "技术分析专长 · 捕捉趋势/动量/突破",
                "weight": round(ds_w, 3),
                "recent_accuracy": round(engine.meta_agent.deepseek_perf.recent_accuracy, 3),
                "signals_total": engine.meta_agent.deepseek_perf.total_signals,
                "last_decision": (last.get("ds_final") or last.get("ds_initial") or {}).get("decision", "-"),
                "last_confidence": round(float((last.get("ds_final") or last.get("ds_initial") or {}).get("confidence", 0)), 3),
            },
            {
                "name": "腾讯混元 Hy3",
                "alias": "稳健派",
                "role_desc": "金融建模专长 · 量化风险/波动率/尾部",
                "weight": round(hy_w, 3),
                "recent_accuracy": round(engine.meta_agent.hunyuan_perf.recent_accuracy, 3),
                "signals_total": engine.meta_agent.hunyuan_perf.total_signals,
                "last_decision": (last.get("hy_final") or last.get("hy_initial") or {}).get("decision", "-"),
                "last_confidence": round(float((last.get("hy_final") or last.get("hy_initial") or {}).get("confidence", 0)), 3),
            },
            {
                "name": "Meta-Agent",
                "alias": "终裁",
                "role_desc": "动态加权裁决 · 自进化 · 极端风险强制HOLD",
                "weight": 1.0,
                "recent_accuracy": 0.5,
                "signals_total": 0,
                "last_decision": getattr(last.get("decision"), "decision", "-"),
                "last_confidence": round(float(getattr(last.get("decision"), "confidence", 0)), 3),
            },
        ],
        "flow": [
            "Step 1 · 采集市场多周期数据 (M5→H1→H4→D1) + 宏观 (DXY/VIX)",
            "Step 2 · DS + HY 独立初判 (并行, 各 2K tokens)",
            "Step 3 · DS ↔ HY 交叉辩论 (双方看到对方论据后修正, 默认 2 轮, 共识即停)",
            "Step 4 · Meta-Agent 加权裁决 (动态权重 × 体制调整 × 风险修正)",
            "Step 5 · 风控审核 (置信度/点差/回撤/单笔风险/同向持仓) → 通过则执行",
            "Step 6 · 平仓后 feedback → 更新 DS/HY 准确率 → 自进化",
        ],
        "self_consistency": {
            "model": "多 Key 轮询 + 内存 token 统计 + 30s 异步刷库",
            "decision_alignment": "两模型分歧 → Meta 降权 + HOLD 权重增加；共识 → 1.1x 加成",
            "risk_override": "extreme 风险 → 强制 HOLD（不论两模型说什么）",
            "evolution_loop": "每次平仓后自动 feedback，权重 EMA 平滑更新 (α=0.2)",
        },
        "last_consensus": getattr(last.get("decision"), "consensus", "unknown") if last else "unknown",
        "ts": datetime.now().isoformat(),
    }
