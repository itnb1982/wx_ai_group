"""
万象AI — 交易执行路由
触发 AI 双模型决策闭环（DebateEngine → 风控 → MT5 真实下单）

端点：
  POST /api/trade/execute      手动触发一轮决策 + 真实下单
  POST /api/trade/auto/start   启动后台自动循环（开市后自动决策下单）
  POST /api/trade/auto/stop    停止后台自动循环
  GET  /api/trade/status       查看自动循环状态
"""
import os
import threading
import time
import re
from datetime import datetime
from typing import Optional
# 并发执行统一走 app.core.account_lane 的常驻有界命名池（"user" / "account"），
# 不再在本文件每轮新建 ThreadPoolExecutor（N 涨会造成线程风暴）。

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db, SessionLocal
from app.models.user import User
from app.models.strategy import StrategyConfig
from app.models.mt5_account import MT5Account
from app.routers.auth import get_current_user
from app.services.trade_executor import TradeExecutor, clear_leader_exit_bus, _is_copied, _mark_copied
from app.services import signal_bus  # ★ 2026-08-19 毫秒级跟单：EARLY_COPIED 早信号去重
from app.services.market_session import get_session_state, get_session_state_fast
from app.services.mt5_service import mt5_service
from app.services.primary_selector import pick_market_primary
from app.services.ai_memory import push_evolution


# 懒加载：避免与 dashboard 的循环导入
def _get_shared_debate_engine():
    """获取 Dashboard 共享的 DebateEngine 单例（含 KeyPool）"""
    from app.routers.dashboard import _get_debate_engine
    return _get_debate_engine()

router = APIRouter(prefix="/api/trade", tags=["交易执行"])

# ── 后台自动循环状态 ──
_auto_lock = threading.Lock()
_auto_thread: Optional[threading.Thread] = None
_auto_running = False
_auto_status = {
    "running": False,
    "cycles": 0,
    "last_cycle": None,
    "last_result": None,
    "last_error": None,
}

# 主号最近一次开仓信号缓存（供跟号实时守护线程做入场补单兜底）
_LATEST_LEADER_SIGNAL: dict = {}


def _get_primary(user_id: str, db: Session) -> Optional[MT5Account]:
    """查找行情主号。

    ★ 2026-08-09：改走统一的 primary_selector。旧实现只要 is_market_primary
    标记存在就直接返回，哪怕那个账号已经掉线，导致 AI 拿不到真实行情。
    """
    return pick_market_primary(db, user_id)


def _get_trade_leader(db: Session, trading_accounts: list) -> MT5Account:
    """信号主号（交易主号）识别：
    优先取行情主号(is_market_primary)；否则取列表中首个（已过滤 connected+trading）。
    ★ 主号 = AI 决策 + 先下单；其他账号自动跟单。
    """
    for a in trading_accounts:
        if a.is_market_primary:
            return a
    return trading_accounts[0]


def get_or_default_strategy(db: Session, acct: MT5Account, user_id: str) -> StrategyConfig:
    """取账号策略；无则给一条合理的默认策略（含每账号独立风控默认值）。

    ★ 风控跟随（follow_leader）：若该号开启跟随且非主号 → 实时继承主号的风控/平仓参数，
      但**不写回本号 DB**（构造 detached 副本）。手数/本金(base_capital 等)保持本号自身，
      篮子浮盈阈值($)按"本号本金/主号本金"等比缩放，避免小号被大阈值错配。
    """
    import copy
    strategy = db.query(StrategyConfig).filter(
        StrategyConfig.mt5_account_id == acct.id,
        StrategyConfig.user_id == user_id,
    ).first()
    if not strategy:
        strategy = StrategyConfig(
            mt5_account_id=acct.id, user_id=user_id,
            base_capital=1000.0, min_confidence=0.65,
            max_lot_per_trade=0.5, max_positions=8,
            follow_leader=True,
        )

    # ── 风控跟随：继承主号 ──
    if getattr(strategy, "follow_leader", True) and not acct.is_market_primary:
        leader = db.query(MT5Account).filter(
            MT5Account.user_id == user_id,
            MT5Account.is_market_primary == True,
        ).first()
        if leader and leader.id != acct.id:
            ls = db.query(StrategyConfig).filter(
                StrategyConfig.mt5_account_id == leader.id,
                StrategyConfig.user_id == user_id,
            ).first()
            if ls:
                # 继承：风控 + 平仓相关参数（不含手数/本金/身份字段）
                # ★ 2026-08-10 信号塔统一：max_risk_per_trade_pct 移出继承——
                #   它是【手数计算基准】，必须用客户自己填的值（用户铁律：
                #   "手数严格按照策略风控每个账户客户自己填写的来执行"）。
                #   继承主号的风险%会让 3299/3301 大号的手数基准变成主号小号的值。
                inherit = [
                    "min_confidence", "max_positions",
                    "max_position_lots", "max_daily_loss_pct", "max_drawdown_pct",
                    "max_spread_points", "trade_asian", "trade_european", "trade_american",
                    "open_interval_seconds",
                    "smart_tp_enabled", "tp1_atr_mult", "tp1_close_pct", "tp2_atr_mult",
                    "tp2_close_pct", "tp3_atr_mult", "tp3_close_pct",
                    "breakeven_after_tp1", "breakeven_buffer_points", "trailing_atr_mult",
                    "trailing_activate_after_tp2", "ai_reverse_close_confidence",
                    "reversal_confirm_cycles", "enable_l3_guard", "enable_trailing_sl",
                ]
                merged = copy.copy(strategy)  # detached 副本，setattr 不会污染 DB
                for f in inherit:
                    setattr(merged, f, getattr(ls, f, None))
                # ★ 2026-08-05 修复前后端不一致：运行期与 GET 统一用主号原始值，
                #   不再按本金缩放。用户在前端设多少锁利就按多少触发（设$10即$10触发），
                #   消除"显示值≠触发值"的体感 bug。
                merged.basket_tp_amount = float(getattr(ls, "basket_tp_amount", 100.0) or 100.0)
                logger.debug(
                    f"[跟随主号] 账号 {acct.account_id[:8]} 继承主号 {leader.account_id[:8]} "
                    f"风控；basket_tp 用主号原始值 {merged.basket_tp_amount:.1f}$ (不缩放)"
                )
                return merged
    return strategy


class _DecisionShim:
    """轻量决策对象：仅含 _manage_positions / smart_exit 需要的 decision/confidence 字段，
    用于把主号的决策传递给跟号做持仓管理（不触发跟号自身的 AI 辩论）。"""

    def __init__(self, decision: str, confidence: float):
        self.decision = decision      # BUY / SELL / HOLD
        self.confidence = confidence


def run_cycle_for_user(user_id: str) -> dict:
    """信号主号领导制：主号先 AI 决策+下单，其他账号按自身风控复制主号订单。
    ★ 多账号支持：1~N 个账号。N=1 时主号即唯一，无跟单。
    ★ 每账号风控独立：手数按各账号 base_capital；笔数/手数/同向并发/风险% 各账号独立审核。
    """
    db = SessionLocal()
    try:
        # 查询该用户所有已连接且启用交易的账号
        trading_accounts = db.query(MT5Account).filter(
            MT5Account.user_id == user_id,
            MT5Account.is_connected == True,
            MT5Account.is_trading_enabled == True,
        ).all()

        if not trading_accounts:
            return {
                "timestamp": datetime.now().isoformat(),
                "decision": None,
                "orders": [],
                "errors": ["无已连接且启用交易的账号"],
            }

        shared_engine = _get_shared_debate_engine()
        all_orders = []
        all_errors = []
        last_decision = None

        # ── 1) 主号(信号主号)先决策 + 下单 ──
        leader = _get_trade_leader(db, trading_accounts)
        leader_strategy = get_or_default_strategy(db, leader, user_id)
        leader_exec = TradeExecutor(
            account_id=leader.id,
            strategy=leader_strategy,
            user_id=user_id,
            db=db,
            engine=shared_engine,
        )
        leader_exec._is_leader = True   # ★ 信号塔：仅主号运行 AI 出场并广播动作，跟号只镜像

        # ★★ 2026-08-19 毫秒级跟单：跟号并发复制函数（提前定义，供早信号回调与 2a 共用）。
        #   每个 copy_order 仍走自身风控与独立 DB 会话；COPIED/EARLY_COPIED 双去重防双开。
        #   注意：早信号回调触发时 leader_signal（execute_cycle 返回后才有）可能未定义，
        #   故支持 _sig 显式传入（早信号路径传早信号，2a 路径缺省用 leader_signal）。
        def _run_follower_copy(_pair, _sig=None):
            _acct, _fstrategy = _pair
            _fdb = SessionLocal()
            try:
                _fexec = TradeExecutor(
                    account_id=_acct.id, strategy=_fstrategy,
                    user_id=user_id, db=_fdb, engine=shared_engine)
                _fexec._follow_leader = True
                _fexec._is_leader = False
                # 早信号已分发的跟号，2a 兜底直接跳过（防双开）
                if signal_bus.EARLY_COPIED.is_active(_acct.id):
                    return {"order": None, "errors": [], "skipped": "early"}
                _sig = _sig if _sig is not None else leader_signal
                if _sig:
                    return _fexec.copy_order(_sig)
                return {"order": None, "errors": []}
            finally:
                _fdb.close()

        # ★★ 2026-08-19 毫秒级跟单：主号 place_order 前把"早信号"回调到这里，
        #   挂号立即并行发单（与主号成交重叠），不再等主号成交（旧串行 +0.55~1.3s）。
        #   挂号手数/风控仍走各自 copy_order 独立审核；主号失败时挂号裸奔仓由
        #   [跟号对账兜底] 机制强制平掉（安全兜底已有）。
        def _early_dispatch(signal: dict) -> None:
            try:
                _dir = str(signal.get("direction", "")).upper()
                if _dir not in ("BUY", "SELL"):
                    return
                _pairs = []
                for _acct in trading_accounts:
                    if _acct.id == leader.id:
                        continue
                    try:
                        _fs = get_or_default_strategy(db, _acct, user_id)
                        if bool(getattr(_fs, "follow_leader", True)):
                            _pairs.append((_acct, _fs))
                    except Exception:
                        continue
                if not _pairs:
                    return

                def _bg_copy():
                    try:
                        from app.core.account_lane import get_lane_pool
                        _copy_results = get_lane_pool().map_accounts(
                            lambda _p: _run_follower_copy(_p, signal), _pairs)
                        for _r in _copy_results or []:
                            if _r.get("ok") and (_r.get("result") or {}).get("order"):
                                try:
                                    signal_bus.EARLY_COPIED.mark(_r["result"]["order"].get("account_id", ""))
                                except Exception:
                                    pass
                    except Exception as _pe:
                        logger.warning(f"[毫秒跟单] 早信号并行分发失败,回退串行: {_pe}")
                        try:
                            for _pair in _pairs:
                                _r = _run_follower_copy(_pair, signal)
                                if _r.get("order"):
                                    signal_bus.EARLY_COPIED.mark(str(_r["order"].get("account_id", "")))
                        except Exception as _se:
                            logger.warning(f"[毫秒跟单] 早信号串行兜底失败: {_se}")

                import threading as _th
                _th.Thread(target=_bg_copy, daemon=True, name="early-copy").start()
            except Exception as _de:
                logger.warning(f"[毫秒跟单] 早信号分发器异常(不影响主号下单): {_de}")

        leader_exec._early_copy_cb = _early_dispatch

        # ★ L3双保险：每轮cycle开始时立即检查篮子浮盈（2s守护线程的补充）
        #   防止守护线程因异常/重启间隙漏检；XAUUSD数秒可波动$2-5，60s cycle间隔内必须兜底。
        try:
            leader_exec._fast_l3_lock()
        except Exception as _l3e:
            logger.warning(f"[自动交易] 主号L3双保险检查异常: {_l3e}")

        logger.info(f"[自动交易] 主号 {leader.name}({leader.account_id[:8]}...) 决策+下单")
        leader_res = leader_exec.execute_cycle()
        leader_decision = leader_res.get("decision")
        leader_signal = leader_res.get("signal")   # 主号下单后产出的可复制信号

        # ★ 进化时间线保活：每轮 cycle 都写一条轻量进化记录（含 HOLD），
        #   解决"长时间不开单/不平仓时进化时间线停更"的问题。
        #   仅在平仓 feedback 时写的 weight_update/trade_review 是"重"事件；
        #   这里的 cycle_record 是"轻"事件——让客户看到 AI 每轮都在工作。
        _action = (leader_decision or {}).get("action", "HOLD") if isinstance(leader_decision, dict) else "HOLD"
        _conf = float((leader_decision or {}).get("confidence", 0) or 0) if isinstance(leader_decision, dict) else 0
        _ds_vote = (leader_decision or {}).get("deepseek_vote", "-") if isinstance(leader_decision, dict) else "-"
        _hy_vote = (leader_decision or {}).get("hunyuan_vote", "-") if isinstance(leader_decision, dict) else "-"
        push_evolution({
            "kind": "cycle_record",
            "subject": f"裁决{_action}",
            "before": f"DS={_ds_vote} HY={_hy_vote}",
            "after": f"置信{_conf:.0%}",
            "delta": "",
            "reason": f"主号{leader.name} 第#{_auto_status.get('cycles', 0)+1}轮",
        })
        # 缓存主号最新开仓信号，供跟号实时守护线程补单（主周期若因超时/风控瞬时拒绝漏跟，10s 内补齐）
        _LATEST_LEADER_SIGNAL[user_id] = {"signal": leader_signal, "ts": time.time()}
        all_orders.extend(leader_res.get("orders", []))
        all_errors.extend(leader_res.get("errors", []))
        if leader_decision:
            last_decision = leader_decision

        # 主号决策包装为 shim，供跟号做持仓管理（信号塔镜像主号出场）。
        # ★ 关键修复：无论主号本轮是否新开仓(HOLD/只管理持仓)，跟号每轮都必须跑镜像，
        #   否则主号在"非开仓轮"触发的 L3护盾/反转/分批/移损不会被跟号跟随
        #   （这正是之前"副号平仓不跟主号"断层的根因：L3全平发生在 HOLD 轮，leader_shim=None 导致跳过）。
        leader_shim = _DecisionShim(
            (leader_decision or {}).get("action", "HOLD"),
            float((leader_decision or {}).get("confidence", 0) or 0),
        )

        # ── 2) 跟号 / 独立账号：按各自模式处理 ──
        # ★ 2026-08-07 优化「单用户周期过长→180s超时跳轮/trade_stale」：
        #   主号 + 每个独立账号(follow_leader=False)各跑一次完整 AI 辩论(≈40s)，原串行
        #   累加(主号+2独立≈120s+)。现把独立账号的 execute_cycle 并发执行（彼此无依赖），
        #   单用户周期≈主号 + 最慢独立号(≈80s)，交易频率显著提升（贴合"多交易多赚钱"）。
        #   并发安全：每个独立账号线程自建 DB 会话（SQLAlchemy Session 非线程安全），
        #   且 decide() 共享缓存已加锁（见 debate_engine._CACHE_LOCK）。
        _independents = []  # [(acct, f_strategy)] 待并发执行的独立账号
        _followers = []     # [(acct, f_strategy)] 跟随主号账号，待并发复制开仓
        for acct in trading_accounts:
            if acct.id == leader.id:
                continue  # 主号已在第 1 步处理
            try:
                f_strategy = get_or_default_strategy(db, acct, user_id)
                follow_leader = bool(getattr(f_strategy, "follow_leader", True))
                if follow_leader:
                    _followers.append((acct, f_strategy))
                else:
                    # 独立账号：收集后并发执行（见下方线程池）
                    _independents.append((acct, f_strategy))
            except Exception as e:
                import traceback as _tb
                logger.error(f"[自动交易] 账号 {acct.name} 预处理异常: {e}\n{_tb.format_exc()}")
                all_errors.append(f"{acct.name}: {e}")

        # ── 2a) 跟号并发复制主号开仓信号（毫秒级同步）
        # ★★ 2026-08-18 修复主号/跟单开仓不同步（用户多次强调）：
        #   原实现顺序调用每个跟号的 copy_order，串行累加 200-800ms 错峰抖动 →
        #   日志实测同一信号 2877213e 与 b3db40fd 相差 2.5s，成交价滑点 $0.01-0.30。
        #   改为线程池并发执行：所有跟号几乎同时收到信号并调用 MT5，延迟从"累加"
        #   降为"max(单个跟单耗时)"；每个 copy_order 仍走自身风控与独立 DB 会话。
        # ★★ 2026-08-19 毫秒级跟单升级：_run_follower_copy 已提前定义（见第 1 步），
        #   主号 place_order 前的早信号已并行分发挂号（EARLY_COPIED 去重）；
        #   此处 2a 作为兜底：早信号未触发/主号成交后需回填 ticket 时补跟。
        if _followers:
            try:
                from app.core.account_lane import get_lane_pool, set_active_accounts
                set_active_accounts(len(trading_accounts))
                _copy_results = get_lane_pool().map_accounts(_run_follower_copy, _followers)
            except Exception as _pe:
                logger.error(f"[自动交易] 跟单车道池不可用，回退串行: {_pe}")
                _copy_results = []
                for _pair in _followers:
                    try:
                        _copy_results.append({"ok": True, "result": _run_follower_copy(_pair), "error": None})
                    except Exception as _se:
                        _copy_results.append({"ok": False, "result": None, "error": str(_se)})

            for _r in _copy_results:
                if _r.get("ok"):
                    _res = _r.get("result") or {}
                    if _res.get("order"):
                        all_orders.append(_res["order"])
                    all_errors.extend(_res.get("errors", []))
                else:
                    logger.error(f"[自动交易] 跟号并发复制异常: {_r.get('error')}")
                    all_errors.append(str(_r.get("error")))

        # ── 2b) 跟号对账 + 持仓管理（平仓镜像、移损等，不阻塞开仓复制）
        #   原顺序：复制 → 对账 → 管理。现复制已并发完成；对账/管理仍顺序执行，
        #   避免并发平仓镜像引入部分平/移损竞态。
        for acct, f_strategy in _followers:
            try:
                fdb = SessionLocal()
                try:
                    f_exec = TradeExecutor(
                        account_id=acct.id, strategy=f_strategy,
                        user_id=user_id, db=fdb, engine=shared_engine)
                    f_exec._follow_leader = True
                    f_exec._is_leader = False
                    f_exec._reconcile_positions()
                    f_exec._manage_positions(leader_shim)
                finally:
                    fdb.close()
            except Exception as e:
                import traceback as _tb
                logger.error(f"[自动交易] 跟号 {acct.name} 管理异常: {e}\n{_tb.format_exc()}")
                all_errors.append(f"{acct.name}: {e}")

        # 并发执行独立账号（各自新建 DB 会话，避免跨线程共享 Session）
        # ★ 2026-08-08 Phase 3：改用进程级常驻有界池（app.core.account_lane）。
        #   原实现 `ThreadPoolExecutor(max_workers=len(_independents))` 每轮现建现销：
        #     - N=50 客户时每轮创建/销毁 50 条线程 = 线程风暴，而 MT5 IPC 本身串行，
        #       超发线程只是排队争抢，纯开销
        #     - 线程数随客户数线性膨胀，违背铁律「N 是变量，1 或 50+ 都要稳」
        #   常驻池上界与 N 解耦（默认 min(cpu*2, 32)），且单账号走直通不过池。
        if _independents:
            def _run_independent(_pair):
                _acct, _fstrategy = _pair
                _idb = SessionLocal()
                try:
                    _iexec = TradeExecutor(
                        account_id=_acct.id, strategy=_fstrategy,
                        user_id=user_id, db=_idb, engine=shared_engine)
                    _iexec._is_leader = True
                    _iexec._follow_leader = False
                    _iexec._reconcile_positions()
                    logger.info(f"[自动交易]  独立账号 {_acct.name} 运行自身 AI 决策循环(并发)")
                    return _iexec.execute_cycle()
                finally:
                    _idb.close()

            # 广播本轮并发规模：下单点据此计算错峰抖动窗口（N=1 零延迟）
            try:
                from app.core.account_lane import get_lane_pool, set_active_accounts
                set_active_accounts(len(trading_accounts))
                _lane_results = get_lane_pool().map_accounts(_run_independent, _independents)
            except Exception as _pe:
                logger.error(f"[自动交易] 车道池不可用，回退串行: {_pe}")
                _lane_results = []
                for _pair in _independents:
                    try:
                        _lane_results.append({"ok": True, "result": _run_independent(_pair), "error": None})
                    except Exception as _se:
                        _lane_results.append({"ok": False, "result": None, "error": str(_se)})

            for _r in _lane_results:
                if _r.get("ok"):
                    _ires = _r.get("result") or {}
                    all_orders.extend(_ires.get("orders", []))
                    all_errors.extend(_ires.get("errors", []))
                else:
                    logger.error(f"[自动交易] 独立账号并发执行异常: {_r.get('error')}")
                    all_errors.append(str(_r.get("error")))

        # ★ 2026-08-12 数据完整性补强：已连接但【未启用交易】的账号也必须补账。
        #   根因：上方 trading_accounts 过滤 `is_trading_enabled == True`，
        #   导致人工停牌的账号（如真实账号被关掉交易开关保护资金）完全不进对账链 →
        #   其历史 pending_verify 永久不回填、真实盈亏永久丢失、账本失真。
        #   本段【只做 pending 回填】：纯读 MT5 历史成交 + 回写自己的历史行，
        #   不开仓、不管理持仓、不碰任何资金操作 → 与"停止交易"的语义完全不冲突。
        #   失败全吞，绝不影响主交易链路。
        try:
            _paused = db.query(MT5Account).filter(
                MT5Account.user_id == user_id,
                MT5Account.is_connected == True,
                MT5Account.is_trading_enabled == False,
            ).all()
            for _pa in _paused:
                try:
                    _pstrat = get_or_default_strategy(db, _pa, user_id)
                    _pexec = TradeExecutor(
                        account_id=_pa.id, strategy=_pstrat,
                        user_id=user_id, db=db, engine=shared_engine)
                    _pexec._rescan_pending_verify()
                except Exception as _pe:
                    logger.warning(f"[停牌补账] {_pa.name} pending 回填跳过: {_pe}")
        except Exception as _pe2:
            logger.warning(f"[停牌补账] 扫描跳过: {_pe2}")

        # 注：不再周期末清空信号塔总线——改由 TTL(180s) + 跟号幂等去重管理，
        # 避免清总线把高频监控线程在周期间发布的平仓动作冲掉导致跟号漏跟。
        return {
            "timestamp": datetime.now().isoformat(),
            "leader": {"id": leader.id, "name": leader.name, "login": leader.account_id},
            "decision": last_decision,
            "orders": all_orders,
            "errors": all_errors,
        }
    except Exception as e:
        return {
            "timestamp": datetime.now().isoformat(),
            "decision": None,
            "orders": [],
            "errors": [str(e)],
        }
    finally:
        db.close()


def _l3_profit_lock_monitor_loop(interval: float = 2.0):
    """利润锁利高频守护线程：覆盖所有交易账号，每 interval 秒检查篮子浮盈，达标即全平并广播信号塔。

    ★ 2026-08-07 修复"只处理一个账号"：原逻辑只监控 is_market_primary 主号，
      独立账号/跟号的篮子浮盈只能等 ~100s 主周期才处理，数秒波动内浮盈回吐。
      现所有 connected+trading 账号都跑 2s 级 L3 快监：
      - 主号/独立账号触发后广播，驱动跟号镜像；
      - 跟号触发后只平自己（不广播），避免与主号广播重复。

    ★ 毫秒级要求（2026-08-05 升级）：XAUUSD 可在数秒内波动 $2-5，
      原 15s 轮询会导致浮盈 $10→$12→回亏的全过程被漏掉（用户实盘亏损 $60+ 教训）。
      现 2s 间隔 + 主循环 cycle 双保险 + 反转即时平仓 = 准实时响应。
    纯机械、零 AI 调用。"""
    while True:
        try:
            db = SessionLocal()
            try:
                accounts = db.query(MT5Account).filter(
                    MT5Account.is_connected == True,
                    MT5Account.is_trading_enabled == True,
                ).all()
                if accounts:
                    engine = _get_shared_debate_engine()
                    for acc in accounts:
                        try:
                            strat = get_or_default_strategy(db, acc, acc.user_id)
                            follow_leader = bool(getattr(strat, "follow_leader", True))
                            exec = TradeExecutor(
                                account_id=acc.id, strategy=strat,
                                user_id=acc.user_id, db=db, engine=engine)
                            # 主号/独立账号广播；跟号只平自己不广播
                            exec._is_leader = (acc.is_market_primary == True) or (not follow_leader)
                            exec._follow_leader = follow_leader
                            exec._fast_l3_lock()
                            # ★ 2026-08-15：per-position 追踪止损下沉到 2s 级（内部按 follow_leader 自动跳过跟号）
                            exec._fast_leader_trailing()
                        except Exception as _e:
                            # ★ 2026-08-11 logger.exception 输出堆栈，定位 str 异常真实源头
                            logger.exception(f"[L3快监] 账号 {acc.id} 异常: {_e}")
            finally:
                db.close()
        except Exception as _e:
            logger.warning(f"[L3快监] 循环异常: {_e}")
        time.sleep(interval)


def _follower_mirror_loop(interval: float = 2.0):
    """副号实时跟单守护线程：每 interval 秒拉取信号塔总线上主号的出场动作并立即镜像。

    ★ 这是用户硬要求『副号所有动作必须跟主号实时同步，金融产品不能有延时』的核心修复：
      原跟号仅在主周期(~100s)读总线 → 主号在周期间(高频锁利线程15s/AI出场)平仓时，跟号要等下一个周期才平，
      延迟约 1 分钟。本线程把跟号平仓延迟压到 ≤interval(10s)，与主号高频平仓基本同步。
      零 AI 调用；主号 AI 出场仍仅主号喂进化（_record_close 内 _is_leader 门控）。
      同时做入场补单兜底：若主周期因超时/风控瞬时拒绝漏跟，10s 内用缓存的主号信号补齐。"""
    while True:
        try:
            db = SessionLocal()
            try:
                active_user_ids = [
                    row[0] for row in db.query(MT5Account.user_id)
                    .filter(MT5Account.is_connected == True,
                            MT5Account.is_trading_enabled == True)
                    .distinct().all()
                ]
                engine = _get_shared_debate_engine()
                for uid in active_user_ids:
                    accs = db.query(MT5Account).filter(
                        MT5Account.user_id == uid,
                        MT5Account.is_connected == True,
                        MT5Account.is_trading_enabled == True,
                    ).all()
                    if not accs:
                        continue
                    leader = _get_trade_leader(db, accs)
                    # 入场补单：主号最新开仓信号（新鲜才补，避免历史信号反复触发）
                    lsig = _LATEST_LEADER_SIGNAL.get(uid)
                    fresh_sig = None
                    if lsig and (time.time() - lsig.get("ts", 0)) < 120:
                        fresh_sig = lsig.get("signal")
                    for acct in accs:
                        if acct.id == leader.id:
                            continue  # 主号不跟自己
                        try:
                            # ★ 毫秒级可靠性：每账号独立session，避免一个毒杀全死
                            fdb = SessionLocal()
                            try:
                                fstrat = get_or_default_strategy(fdb, acct, uid)
                                fexec = TradeExecutor(
                                    account_id=acct.id, strategy=fstrat,
                                    user_id=uid, db=fdb, engine=engine)
                                positions = mt5_service.get_positions(acct.id, "XAUUSD") or []
                                # ★ 2026-08-05 独立风控：独立账号(follow_leader=False)自管，
                                #   不镜像主号、不复制主号单；仅跟随主号的账号才镜像+补单。
                                is_follower = getattr(fstrat, "follow_leader", True)
                                # ★ 2026-08-17 方案C毫秒级跟单（快监顺序修正）：
                                #   入场补单必须排在镜像出场/对账之前——copy_order 是毫秒级 IPC，
                                #   而 _mirror_leader_exits/_reconcile_against_leader 含持仓查询与账本
                                #   清理（慢操作，且 Worker 断连时会超时拖慢整个循环）。先补新单
                                #   再镜像/对账，主号开仓信号一到，跟号下一轮快监(2s)立即复制。
                                #   实测根因：2877213e 延迟 9.5s、b3db40fd 41.5s 均因顺序在慢操作之后。
                                if is_follower and fresh_sig and fresh_sig.get("ticket"):
                                    lt = str(fresh_sig.get("ticket"))
                                    # ★ 2026-08-06 修复重复跟单：用进程级 _is_copied 替代不可靠的 comment 正则
                                    if not _is_copied(acct.id, lt):
                                        try:
                                            r = fexec.copy_order(fresh_sig)
                                            if r.get("order"):
                                                _mark_copied(acct.id, lt)
                                                logger.info(
                                                    f"[跟号快监] {acct.account_id[:8]} 补单主号#{lt} "
                                                    f"{fresh_sig.get('direction')}")
                                        except Exception as _ce:
                                            logger.warning(f"[跟号快监] {acct.account_id[:8]} 补单异常: {_ce}")
                                    else:
                                        logger.debug(
                                            f"[跟号快监] {acct.account_id[:8]} 主号#{lt} 已复制，跳过"
                                        )
                                if is_follower:
                                    # 镜像主号出场动作（信号塔），确保平仓同步
                                    if positions:
                                        fexec._mirror_leader_exits(positions, None)
                                    # ★ 2026-08-06 修复主副仓不同步：
                                    #   跟号不再自己跑 _fast_l3_lock（篮子/单笔浮亏熔断会提前平仓导致与主号不同步）。
                                    #   改为对照主号持仓清理孤儿单，作为广播漏跟的最终兜底。
                                    fexec._reconcile_against_leader()
                            finally:
                                fdb.close()
                        except Exception as _e:
                            logger.warning(f"[跟号快监] {acct.account_id[:8]} 异常: {_e}")
            finally:
                db.close()
        except Exception as _e:
            logger.warning(f"[跟号快监] 循环异常: {_e}")
        time.sleep(interval)


def _run_cycle_with_timeout(user_id: str, timeout: int = 180) -> dict:
    """带超时墙的单轮决策：防止 AI 辩论/下单调用卡死导致整个 auto_loop 假死。

    实现：守护线程执行 run_cycle_for_user，主线程 join 超时则返回「本轮超时」占位结果，
    确保 auto_loop 无论如何都能进入下一轮（cycles 持续推进、前端状态可见）。
    """
    _box: list = []
    _err: list = []

    def _worker():
        try:
            _box.append(run_cycle_for_user(user_id))
        except Exception as e:
            _err.append(str(e))

    _t = threading.Thread(target=_worker, daemon=True)
    _t.start()
    _t.join(timeout=timeout)
    if _box:
        return _box[0]
    if _t.is_alive():
        logger.error(f"[自动交易] 单轮决策超时({timeout}s)，强制跳过本轮 user={user_id[:8]}，避免引擎假死")
        return {
            "timestamp": datetime.now().isoformat(),
            "decision": None,
            "orders": [],
            "errors": [f"cycle_timeout({timeout}s)"],
        }
    # 线程已结束但异常（极少）：返回错误占位
    return {
        "timestamp": datetime.now().isoformat(),
        "decision": None,
        "orders": [],
        "errors": _err or ["unknown_cycle_error"],
    }


def _get_session_with_timeout(timeout_sec: float = 15.0) -> dict:
    """带超时的 session_state 获取：MT5 IPC 卡住时自动降级到静态时钟。

    ★ 根治 auto_loop 卡死：优先用纯静态快速时钟（get_session_state_fast，
      不调 MT5 IPC，绝不会卡死），XAUUSD 开盘判断用 GMT+3 静态推算足够准确。
       仅当静态计算异常时，才尝试 MT5 全量版（带线程超时，失败立即回退静态）。
       这样无论 MT5 Worker 是否就绪，auto_loop 都不会被 IPC 阻塞卡死。
    """
    # 1) 优先纯静态时钟（秒回，无 IPC 阻塞风险）
    try:
        return get_session_state_fast()
    except Exception as e:
        logger.warning(f"[自动交易] get_session_state_fast 异常，转全量: {e}")
    # 2) 全量 MT5 版（带超时保护，防止 IPC 卡死）
    result_box: list = []  # [session_dict]

    def _worker():
        try:
            result_box.append(get_session_state())
        except Exception as e:
            logger.warning(f"[自动交易] get_session_state 异常: {e}")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if result_box:
        return result_box[0]
    # 超时降级：用静态时钟（不调 MT5）
    logger.warning(f"[自动交易] get_session_state 超时({timeout_sec}s)，降级为静态时钟")
    return get_session_state_fast()


def _auto_loop():
    """后台自动决策循环：市场开放时按间隔触发决策 + 下单"""
    global _auto_running, _auto_status
    logger.info("[自动交易] _auto_loop 线程进入主循环")
    while True:
        with _auto_lock:
            if not _auto_running:
                logger.info("[自动交易] _auto_running 关闭，退出循环")
                break
        try:
            logger.debug("[自动交易] 开始获取市场时钟状态...")
            session = _get_session_with_timeout(timeout_sec=15.0)
            logger.debug(f"[自动交易] 市场时钟: is_open={session.get('is_open')} phase={session.get('phase_label')} source={session.get('source')}")
            if not session.get("is_open"):
                # 休市：跳过决策（零无效 API 调用），仅记录状态
                _auto_status["last_result"] = {
                    "skipped": True,
                    "reason": "market_closed",
                    "session": session.get("phase_label"),
                }
                logger.info(f"[自动交易] 休市中（{session.get('phase_label')}），跳过本轮")
                interval = 60
            else:
                # 只遍历"拥有已连接 MT5 主号"的用户，避免被无账号的辅助账号
                # （诊断/审计等）污染 last_result。同一物理交易者会通过主号聚合。
                db = SessionLocal()
                try:
                    active_user_ids = [
                        row[0] for row in db.query(MT5Account.user_id)
                        .filter(MT5Account.is_connected == True)
                        .distinct().all()
                    ]
                finally:
                    db.close()
                logger.info(f"[自动交易] 活跃交易者 {len(active_user_ids)} 人: {[uid[:8] for uid in active_user_ids]}")
                last_res = None
                # ★ 2026-08-07 优化「决策超时/周期过长」：原 4 账号串行累加 → 整轮周期≈4×单辩论
                #   (290s+)，远超 60s 间隔 → 跳轮、trade_stale、单用户180s超时。现改为并发执行
                #   各用户周期（每用户仍各自带180s超时墙），整轮周期≈最慢单用户(≈100s)，
                #   交易频率显著提升（贴合"多交易多赚钱"铁律）。不同用户对应不同账号，并发不
                #   会导致同账号重复下单；同用户内部仍串行。
                # ★ 2026-08-08 Phase 3：用户级也走常驻有界池（"user" 车道）。
                #   多租户 SaaS 下「一个交易账号 = 一个独立客户」，active_user_ids
                #   会随客户数增长；原 max_workers=len(users) 无上界，50 客户 → 50 线程，
                #   而每条还会再挂一条 180s 超时守护线程 = 100 线程。
                #   注意必须用与账号级**不同的命名池**：用户级任务内部会派发账号级任务，
                #   共用一个池会在池打满时形成嵌套死锁（见 account_lane 注释）。
                _cycle_timeout = 180
                try:
                    from app.core.account_lane import get_lane_pool as _get_lane_pool
                    _user_results = _get_lane_pool("user").map_accounts(
                        lambda _uid: (_uid, _run_cycle_with_timeout(_uid, _cycle_timeout)),
                        active_user_ids,
                    )
                except Exception as _pe:
                    logger.error(f"[自动交易] 用户车道池不可用，回退串行: {_pe}")
                    _user_results = []
                    for _uid in active_user_ids:
                        try:
                            _user_results.append(
                                {"ok": True, "result": (_uid, _run_cycle_with_timeout(_uid, _cycle_timeout))})
                        except Exception as _se:
                            _user_results.append({"ok": False, "result": None, "error": str(_se), "item": _uid})

                for _r in _user_results:
                    if _r.get("ok"):
                        _uid, res = _r["result"]
                    else:
                        _uid = str(_r.get("item") or "unknown")
                        res = {"timestamp": datetime.now().isoformat(), "decision": None,
                               "orders": [], "errors": [str(_r.get("error"))]}
                    last_res = res
                    _auto_status["cycles"] += 1
                    logger.info(
                        f"[自动交易] cycle#{_auto_status['cycles']} "
                        f"user={_uid[:8]} decision={res.get('decision')} "
                        f"orders={len(res.get('orders', []))} "
                        f"errors={res.get('errors', [])}"
                    )
                _auto_status["last_result"] = last_res or {
                    "timestamp": datetime.now().isoformat(),
                    "decision": None, "orders": [], "errors": ["无活跃交易者"],
                }
                # ★ 2026-08-11 自适应决策间隔（云消耗优化，实测 DS 单日 600+ 次调用烧穿余额）：
                #   保持"多交易多赚钱"铁律——只在【全部用户 HOLD + 零订单】的平静期拉长间隔，
                #   一旦有方向决策或有开单动作立即回到 30s，绝不因降频错过入场时机。
                #   ★ 2026-08-17 持仓感知（用户核心哲学：有仓管仓、无仓找机会）：
                #     任一账号有持仓 → 压缩到 30s（管仓优先，smart_exit/持仓管家/篮子/
                #     视觉看护每 30s 一巡，跟上行情变化；L3 锁利 2s 高频线程兜底不变）。
                #     全空仓 + 全 HOLD → 60s 平静（省云消耗，专注等信号）。
                _calm = True
                _had_action = False
                _has_pos = False
                for _r in _user_results:
                    _res = _r.get("result", (None, None))[1] if isinstance(_r.get("result"), tuple) else None
                    if not _res:
                        continue
                    # ★ 2026-08-17 修复：decision 是 dict({'action':..})，str(dict).upper()
                    #   永远不等于 "BUY"/"SELL" → _calm 恒 True → 方向信号来了也不加速（30s 失效）。
                    #   正确取 action 键。兼容旧字符串形态（兜底）。
                    _dec_raw = _res.get("decision")
                    if isinstance(_dec_raw, dict):
                        _dec = str(_dec_raw.get("action") or "HOLD").upper()
                    else:
                        _dec = str(_dec_raw or "HOLD").upper()
                    if _dec in ("BUY", "SELL"):
                        _calm = False
                    if _res.get("orders"):
                        _had_action = True
                    if _res.get("has_positions"):
                        _has_pos = True
                if _had_action:
                    interval = 30
                elif _has_pos:
                    # ★ 持仓感知：有仓时管仓优先，推理间隔跟上行情（用户诉求 2026-08-17）
                    interval = 30
                elif _calm:
                    interval = 60
                else:
                    interval = 30
                logger.debug(
                    f"[自动交易] 决策间隔={interval}s (calm={_calm} action={_had_action} pos={_has_pos})"
                )
        except Exception as e:
            logger.error(f"[自动交易] cycle 异常: {type(e).__name__}: {e}")
            _auto_status["last_error"] = str(e)
            interval = 30
        _auto_status["last_cycle"] = datetime.now().isoformat()
        # 跨进程持久化最后 tick 时间戳（供 health 接口读取，规避导入实例/
        # 多 worker 导致的 _auto_status 不可见问题）。文件写在 DATA_DIR。
        try:
            # ★ 可移植性(2026-08-08)：兜底路径必须相对项目根，禁止写死开发机盘符。
            #   写入端与 main.py 的读取端共用 runtime_paths.data_path，
            #   否则会出现"写 F 盘、读项目目录"→ 心跳文件永远读不到 → 健康检查
            #   误报自动交易停摆的幽灵故障。
            from runtime_paths import data_path as _dp
            with open(_dp("last_cycle_ts.txt"), "w") as _lf:
                _lf.write(str(int(datetime.now().timestamp())))
        except Exception:
            pass
        # 可中断的间隔等待
        for _ in range(interval):
            with _auto_lock:
                if not _auto_running:
                    break
            time.sleep(1)


@router.post("/execute")
def execute_trade(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动触发一轮 AI 决策 + 真实下单"""
    return run_cycle_for_user(user.id)


class ManualOrderRequest(BaseModel):
    """手动下单请求（经风控校验）"""
    symbol: str = "XAUUSD"
    order_type: str  # BUY / SELL（必填）
    volume: float = 0.01
    sl: float = 0.0
    tp: float = 0.0
    comment: str = "WXAI_MANUAL"
    # ★ Phase 0：人工封盘期间仍允许手动下单（紧急停止是夺回控制权，不是把人的手铐上），
    #   但必须显式置 true 二次确认，避免"忘了自己停过机器"的误操作。
    override_halt: bool = False


@router.post("/order")
def manual_order(req: ManualOrderRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动下单：经 6 层风控校验后，向 MT5 发送一笔市价单"""
    primary = _get_primary(user.id, db)
    if not primary:
        raise HTTPException(400, "无已连接的行情主号")
    # E1 一致性护栏：手动下单同样受「停交易」开关约束（execute_cycle 已拦截自动/手动 execute，此处补齐直连下单入口）
    if not primary.is_trading_enabled:
        raise HTTPException(400, "该账号已停用交易（前端'停交易'），手动下单被拒绝")

    # ★ E0：人工封盘期的手动下单 —— 放行但要求二次确认。
    #   语义边界见 app/routers/emergency.py 头部说明：
    #   紧急停止约束的是"系统自动行为"，人在界面上的显式操作不该被锁死，
    #   否则人在最危急时反而什么补救动作都做不了。
    from app.services import emergency as _em
    _ok, _why = _em.allow_open(primary.id)
    if not _ok and not req.override_halt:
        raise HTTPException(
            409,
            f"{_why}。手动下单在封盘期仍然可用，但需显式确认："
            f"请重发请求并带上 override_halt=true。",
        )
    if not _ok and req.override_halt:
        logger.warning(
            f"[紧急处置] 封盘期人工强制下单 | 账号={primary.id[:8]} | 用户={user.id[:8]} "
            f"| {req.order_type} {req.volume}手 | 封盘原因={_why}"
        )
    if req.order_type.upper() not in ("BUY", "SELL"):
        raise HTTPException(400, "order_type 仅支持 BUY / SELL")

    # 风控校验（含周末/时段 Layer 6）
    from app.services.risk_engine import RiskEngine
    strategy = db.query(StrategyConfig).filter(
        StrategyConfig.mt5_account_id == primary.id,
        StrategyConfig.user_id == user.id,
    ).first() or StrategyConfig(mt5_account_id=primary.id, user_id=user.id)
    # P0-1：手动下单同样需经 IPC 风控（传入 mt5_service + 账号上下文）
    risk = RiskEngine(strategy, mt5_service=mt5_service, account_id=primary.id)
    acct = mt5_service.get_account_info(primary.id)
    balance = acct.get("balance", 0) if isinstance(acct, dict) and "error" not in acct else 0
    risk_res = risk.check_trade_allowed(
        symbol=req.symbol, volume=req.volume,
        entry_price=0, stop_loss=req.sl, account_balance=balance,
    )
    if not risk_res.passed:
        return {"ok": False, "stage": "risk", "rejected": risk_res.reject_reasons}

    order = mt5_service.place_order(
        account_id=primary.id,
        symbol=req.symbol,
        order_type=req.order_type.upper(),
        volume=req.volume,
        sl=req.sl,
        tp=req.tp,
        comment=req.comment,
    )
    return {"ok": "error" not in order, "order": order}


@router.post("/_probe")
def probe_order(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    下单通路探针（诊断用）：绕过 AI 与风控，直接向 MT5 发送一笔最小市价单，
    用于验证「下单指令是否真正到达经纪商」。
    安全约束：仅当市场休市时执行（返回经纪商 market_closed 回码即证明通路正常）；
    市价开放时拒绝，避免误下真实单。
    """
    primary = _get_primary(user.id, db)
    if not primary:
        raise HTTPException(400, "无已连接的行情主号")
    if not primary.is_trading_enabled:
        raise HTTPException(400, "该账号已停用交易（前端'停交易'），探针下单被拒绝")
    session = get_session_state()
    if session.get("is_open"):
        return {"ok": False, "stage": "guard",
                "msg": "市价开放中，探针不下达真实单；请改用 /api/trade/order 手动下单"}
    order = mt5_service.place_order(
        account_id=primary.id,
        symbol="XAUUSD",
        order_type="BUY",
        volume=0.01,
        sl=0.0,
        tp=0.0,
        comment="WXAI_PROBE",
    )
    ti = mt5_service.get_terminal_info(primary.id)
    return {"ok": "error" not in order, "probe": order, "terminal": ti}


@router.get("/_diag")
def diag(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """诊断：返回所有已连接账号的 MT5 终端信息（算法交易开关 / data_path）"""
    accs = db.query(MT5Account).filter(
        MT5Account.user_id == user.id,
        MT5Account.is_connected == True,
    ).all()
    out = []
    for a in accs:
        ti = mt5_service.get_terminal_info(a.id)
        out.append({"name": a.name, "login": a.account_id, "terminal": ti})
    return out


@router.post("/auto/start")
def start_auto(user: User = Depends(get_current_user)):
    """启动后台自动交易循环（开市后自动决策下单）"""
    return _start_auto_internal()


def _start_auto_internal() -> dict:
    """无依赖启动版本（lifespan / 监控脚本调用）"""
    global _auto_running, _auto_thread
    with _auto_lock:
        if _auto_running:
            return {"ok": True, "msg": "自动交易循环已在运行"}
        _auto_running = True
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    _auto_status["running"] = True
    logger.info("[自动交易] 循环已启动（lifespan 或 API）")
    return {"ok": True, "msg": "自动交易循环已启动"}


@router.post("/auto/stop")
def stop_auto(user: User = Depends(get_current_user)):
    """停止后台自动交易循环"""
    global _auto_running
    with _auto_lock:
        _auto_running = False
    _auto_status["running"] = False
    return {"ok": True, "msg": "自动交易循环已停止"}


@router.get("/status")
def trade_status(user: User = Depends(get_current_user)):
    """查看自动循环状态"""
    return _auto_status
