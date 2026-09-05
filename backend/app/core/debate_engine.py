"""
XAU/USD万象Ai自动量化交易系统 — 双模型辩论引擎
编排 DeepSeek V4 ↔ 混元 Hy3 的多空辩论流程

流程：
1. 双模型独立分析（并行）
2. 交叉辩论（看到对方论据后修正）
3. Meta-Agent 加权裁决
4. 风控审核
"""

import time
import threading
import copy
from datetime import datetime
from loguru import logger
from concurrent.futures import ThreadPoolExecutor
from app.core.deepseek_client import DeepSeekClient
from app.core.hunyuan_client import HunyuanClient
from app.core.market_analyzer import MarketAnalyzer
from app.core.meta_agent import MetaAgent, DebateDecision
from app.services.memory_bank import MemoryBank
from app.services.mt5_service import mt5_service
from app.services.cloud_switch import effective_cloud_enabled


# ★ 2026-08-07 修复"AI 瞎子"根因：按账号 ID 隔离决策缓存。
#   原模块级全局缓存使同循环内只有第 1 个账号的持仓/成交被注入 AI prompt，
#   后续账号（独立账号）直接复用决策 → AI 根本不知道这些账号的持仓状态。
#   现缓存 key = account_id：主号、独立账号各自独立跑完整辩论，
#   AI 必须看到本账号真实账本后再投票；同账号 45s 内复用（省 token）。
#   跟号不跑辩论，直接镜像主号，不受此缓存影响。
_SHARED_DECISION_CACHE: dict = {}
# ★ 2026-08-07 线程安全：独立账号并发跑辩论后，多线程会同时读写此缓存，
#   裸 dict 的 get+set 是 read-modify-write 竞态。加锁保护（CPython GIL 下单个
#   dict 操作虽原子，但读-改-写组合非原子，并发会导致缓存写入丢失/脏读）。
_CACHE_LOCK = threading.Lock()


def _get_health_monitor():
    """取降级监视器（Phase 6）。任何异常返回 None → 调用方按「无降级」旧行为跑。

    刻意用惰性 import + 全吞异常：降级车道是**附加**能力，它自己出问题
    绝不能把交易主链路带崩。这也让本模块在监视器缺席时仍可独立运行。
    """
    try:
        from app.services.platform_health_monitor import degrade_enabled, get_monitor

        if not degrade_enabled():
            return None
        return get_monitor()
    except Exception:
        return None


def _filter_directional_lessons(lessons: list) -> list:
    """过滤掉带方向偏置的教训，只保留风控/仓位/出场类经验。

    2026-08-06 根因修复：memory_bank 中累积了如"SELL方向亏损""上行区间做空挨打"等
    方向性 lesson，被注入 AI prompt 后造成 BUY-only 偏置。今后只注入与方向无关的
    风控教训（低置信、持仓时长、出场节奏等）。
    """
    blocked = [
        "sell方向", "buy方向", "做空", "做多", "系统性挨打", "系统性亏损",
        "上行区间", "下行区间", "只准", "禁止开", "严禁开",
    ]
    out = []
    for lesson in lessons:
        low = str(lesson).lower()
        if any(b in low for b in blocked):
            logger.debug(f"[辩论引擎] 过滤方向性 lesson: {lesson[:80]}")
            continue
        out.append(lesson)
    return out


class DebateEngine:
    """双模型辩论引擎 — 支持 KeyPool 多 Key 调度 + token 统计"""

    def __init__(
        self,
        deepseek_key: str = "",
        deepseek_secret: str = "",
        hunyuan_key: str = "",
        hunyuan_secret: str = "",
        mt5_service=None,
        market_primary_id: str = "",
        deepseek_pool=None,
        hunyuan_pool=None,
    ):
        # 兼容旧接口（.env fallback key）+ 新接口（KeyPool 调度）
        self.deepseek = DeepSeekClient(api_key=deepseek_key, pool=deepseek_pool)
        self.hunyuan = HunyuanClient(api_key=hunyuan_key, pool=hunyuan_pool)
        self.market = MarketAnalyzer(mt5_service=mt5_service, market_primary_id=market_primary_id)
        self.meta_agent = MetaAgent()
        self._mt5 = mt5_service
        self._primary_id = market_primary_id
        # 最近一次完整决策快照（debate 双方原始论据 + Meta 裁决）
        # 用于「辩论擂台」面板展示真实 AI 推理过程
        self.last_debate = None  # dict
        self._history: list = []  # 最近 50 次辩论快照
        # ★ 2026-08-07：决策缓存已按账号 ID 隔离（见文件顶部）。
        #   同账号复用、不同账号独立辩论，AI 必须看到本账号持仓/成交后再投票。

    def set_market_primary(self, mt5_service, primary_id: str):
        """运行时切换行情主号"""
        self._mt5 = mt5_service
        self._primary_id = primary_id
        self.market.set_primary(mt5_service, primary_id)

    def decide(self, debate_rounds: int = 2, account_id: str = None) -> DebateDecision:
        """
        执行完整的双模型辩论决策流程

        返回: DebateDecision (含最终决策 + 置信度 + 权重详情)
        account_id: 主号账户ID，用于把「当前真实持仓」注入决策上下文，
                    根治 AI「下完单就失明」——让 AI 看见自己的账本再投票。
        """
        # ★ 2026-08-12 局部防御性导入：避免模块级循环导入导致 settings 未绑定，
        #   同时兼容早期 app/core/config.py 已废弃的旧路径。
        try:
            from app.core.config import settings
        except Exception:  # noqa: BLE001
            from app.config import settings

        # Step 0: 采集市场数据
        logger.info("[辩论引擎] 0. 采集市场数据...")
        market_data = self.market.get_market_snapshot()
        # ── 大脑审计：记录双脑接入（喂了什么）──
        try:
            from app.services.brain_audit import start_cycle, record as _ba_rec
            start_cycle()
        except Exception:
            pass
        # ★ Phase 6：行情健康上报。行情是决策链的输入端，它脏了后面全是垃圾，
        #   故「行情失联」直接判 L3 熔断（见 platform_health_monitor 判定矩阵）。
        _hm0 = _get_health_monitor()
        if _hm0:
            _md_ok = ("error" not in market_data) and not market_data.get("simulated")
            _hm0.report(
                "market_data", _md_ok,
                str(market_data.get("error") or ("模拟数据" if market_data.get("simulated") else ""))[:120],
            )
        if "error" in market_data:
            logger.error(f"[辩论引擎] 市场数据采集失败: {market_data['error']}")
            return DebateDecision(
                decision="HOLD",
                confidence=0.0,
                deepseek_weight=0.5,
                hunyuan_weight=0.5,
                deepseek_vote="HOLD",
                hunyuan_vote="HOLD",
                reasoning_summary=f"数据采集失败: {market_data.get('error')}",
                risk_level="extreme",
            )
        # ★ P1-2 根因修复：真实行情不可用时 get_market_snapshot 会退化为随机模拟快照。
        #   若让 AI 据此辩论决策，等于在噪声上下注开仓。实盘必须 HOLD，不开新仓
        #   （仍由执行器继续管理已有持仓的止损/止盈）。
        if market_data.get("simulated"):
            logger.error(
                "[辩论引擎] 行情为模拟数据（真实行情不可用），实盘禁止据此开仓 → HOLD"
            )
            return DebateDecision(
                decision="HOLD",
                confidence=0.0,
                deepseek_weight=0.5,
                hunyuan_weight=0.5,
                deepseek_vote="HOLD",
                hunyuan_vote="HOLD",
                reasoning_summary="行情数据为模拟值（真实行情不可用），实盘禁止据此开仓",
                risk_level="extreme",
            )

        # ★ 2026-08-07：按账号 ID 隔离决策缓存。
        #   同账号 45s 内复用决策（省 token），不同账号各自独立跑辩论，
        #   确保 AI 看到本账号持仓/成交后再投票。
        _now = time.time()
        with _CACHE_LOCK:
            _cached = _SHARED_DECISION_CACHE.get(account_id)
        if _cached is not None and (_now - _cached.get("t", 0)) < 45:
            logger.info(f"[辩论引擎] 复用账号{account_id[:8]}决策缓存(跳过重复辩论，省token)")
            # ★ 2026-08-15 复检P2修复：命中返回同样深拷贝——写入侧已 deepcopy（L682），
            #   但命中返回共享引用，多调用方（尤其执行器 L2041 改写 proofread_blocked）
            #   仍会污染缓存对象，45s 内下次命中返回被污染对象（快照误标「被拦截」）。
            return copy.deepcopy(_cached["decision"])

        # Step 0.5: 注入历史经验教训（memory_bank → 开仓决策 prompt）
        # 2026-08-06 修正：只注入风控/仓位/出场类经验，严禁注入方向性偏置。
        # 原 lesson 中"SELL方向亏损""上行区间做空挨打"等被 AI 当成方向规则，
        # 导致 BUY-only 行为。过滤后保留：低置信亏损、持仓时长、出场节奏等。
        try:
            _bank = MemoryBank()
            _lessons = _filter_directional_lessons(_bank.top_lessons(5))
            if _lessons:
                market_data["empirical_lessons"] = _lessons
                logger.info(f"[辩论引擎] 已注入 {len(_lessons)} 条风控经验到开仓决策")
        except Exception as _e:
            logger.warning(f"[辩论引擎] memory_bank 注入跳过: {_e}")

        # Step 0.6: 反向对账·注入当前真实持仓（根治 AI 失明，FINRS 论文核心：
        # 方向决策必须基于「当前仓位状态」）。从 MT5 拉主号实时持仓，写入
        # market_data['my_open_positions']，让双 AI 投票前先看见自己的账本——
        # 避免「看不见已有单→重复同向下单/逆势加仓」的失明循环。
        if account_id:
            try:
                _pos = mt5_service.get_positions(account_id, "XAUUSD") or []
                _now = time.time()
                _open = []
                for p in _pos:
                    _ep = float(p.get("price_open") or p.get("open_price") or 0)
                    _cp = float(p.get("price_current") or p.get("current_price") or 0)
                    _vol = float(p.get("volume") or 0)
                    _dir = "BUY" if str(p.get("type", "")).upper() in ("BUY", "0") else "SELL"
                    # 浮盈亏（XAUUSD 1手≈100/点，粗略用价格差×volume×100）
                    _pnl = 0.0
                    if _ep and _cp:
                        _pnl = (_cp - _ep) * _vol * 100 if _dir == "BUY" else (_ep - _cp) * _vol * 100
                    # ★ 2026-08-07 修复"AI 瞎子"复发根因：MT5 返回的 time_open 可能是
                    #   ISO 字符串('2026-08-07T17:21:12')而非 epoch 时间戳，float() 会抛
                    #   ValueError → 整个持仓注入 try 被吞 → AI 看不见自己持仓（重新变瞎子）。
                    #   现同时兼容 epoch(int/float) 与 ISO 字符串两种格式。
                    _opent_raw = p.get("time_open") or p.get("open_time") or 0
                    try:
                        if isinstance(_opent_raw, str):
                            _opent = datetime.fromisoformat(_opent_raw.replace("Z", "")).timestamp()
                        else:
                            _opent = float(_opent_raw)
                    except Exception:
                        _opent = 0.0
                    _age = int((_now - _opent) / 60) if _opent else -1
                    _open.append({
                        "ticket": str(p.get("ticket", "")),
                        "direction": _dir,
                        "volume": _vol,
                        "entry": _ep,
                        "current": _cp,
                        "floating_pnl": round(_pnl, 2),
                        "age_min": _age,
                        "sl": float(p.get("sl") or 0),
                        "tp": float(p.get("tp") or 0),
                    })
                market_data["my_open_positions"] = _open
                # ★ 2026-08-17 P1修复：注入 account_id 供 meta_agent 篮子处置
                #   防抖确认按账号隔离（否则主号+独立号共用 "primary" 确认状态串扰）
                market_data["account_id"] = str(account_id)
                if _open:
                    logger.info(
                        f"[辩论引擎] 已注入主号{account_id[:8]}当前持仓 {len(_open)} 笔到决策上下文: "
                        + ", ".join(f"{o['direction']}#{o['ticket']}(浮{o['floating_pnl']})" for o in _open)
                    )
                # ★ 2026-08-07 v5：全局仓位快照（让 AI 在决策时看见"整体仓位多重"，
                #   支撑自主仓位管理——缩手/加仓不再盲开）。adjudicate 据其算 position_intent。
                market_data["portfolio_state"] = {
                    "total_positions": len(_open),
                    "total_lots": round(sum(o["volume"] for o in _open), 2),
                    "total_floating_pnl": round(sum(o["floating_pnl"] for o in _open), 2),
                    "max_single_loss": round(min((o["floating_pnl"] for o in _open), default=0.0), 2),
                }
            except Exception as _pe:
                logger.warning(f"[辩论引擎] 持仓注入跳过: {_pe}")

        # Step 0.7: 注入最近真实成交（根治「AI 越跑越笨」）
        # 审计发现：DB 的 trades 历史成交表此前从不被任何模块回读，AI 只拿到
        # 聚合权重 + 泛化教训文本，永远看不到「过去哪笔、什么开仓价、亏多少」的
        # 逐笔语境，故方向错误长期不改、盈利无变化。现把最近 N 笔已平成交喂给双 AI，
        # 让「复盘真实战绩」成为决策输入 → 真正从自己的盈亏里学。
        if account_id:
            try:
                _hist = []
                # ★ 优先读进程内存成交缓冲（永远可写，不依赖被 Defender 锁死的 SQLite）。
                # 这样即使 DB 只读，AI 仍能看到自己最近的真实盈亏 → 持续学习（根治"越跑越笨"）。
                from app.services.ai_memory import get_trades as _mem_get_trades
                _buf = _mem_get_trades(60)
                _closed = [e for e in _buf if str(e.get("kind", "")) in ("close", "close_partial", "sl")]
                for e in _closed[-15:]:
                    _dir = str(e.get("direction") or "").upper()
                    if _dir not in ("BUY", "SELL"):
                        _dir = "HOLD"
                    _ts = str(e.get("ts") or "")
                    _when = _ts[:16].replace("T", " ") if _ts else ""
                    _hist.append({
                        "ticket": str(e.get("ticket") or ""),
                        "dir": _dir,
                        "open": round(float(e.get("open_price") or 0), 2),
                        "close": round(float(e.get("close_price") or 0), 2),
                        "pnl": round(float(e.get("pnl") or 0), 2),
                        "reason": str(e.get("reason") or ""),
                        "when": _when,
                    })
                # 内存无数据（进程刚重启/尚无平仓）→ 回退读 DB（Defender 解锁时可用）
                if not _hist:
                    try:
                        from app.database import SessionLocal
                        from app.models.trade import Trade
                        _db = SessionLocal()
                        try:
                            _rows = (
                                _db.query(Trade)
                                .filter(
                                    Trade.mt5_account_id == account_id,
                                    Trade.close_time.isnot(None),
                                )
                                .order_by(Trade.close_time.desc())
                                .limit(15)
                                .all()
                            )
                            for _t in _rows:
                                _act = str(_t.action or "").lower()
                                if _act.startswith("buy"):
                                    _dir = "BUY"
                                elif _act.startswith("sell"):
                                    _dir = "SELL"
                                else:
                                    _dir = str(_t.meta_agent_decision or "HOLD")
                                _hist.append({
                                    "ticket": str(_t.mt5_ticket or ""),
                                    "dir": _dir,
                                    "open": round(float(_t.open_price or 0), 2),
                                    "close": round(float(_t.close_price or 0), 2),
                                    # ★ #9 含佣口径：优先 net_profit（真实结算），缺失回退 profit
                                    "pnl": round(float(_t.net_profit if _t.net_profit is not None else _t.profit or 0), 2),
                                    "reason": str(_t.exit_reason or _t.result or ""),
                                    "when": _t.close_time.strftime("%m-%d %H:%M") if _t.close_time else "",
                                })
                        finally:
                            _db.close()
                    except Exception as _dbe:
                        logger.debug(f"[辩论引擎] DB成交回退读取失败(忽略): {_dbe}")
                if _hist:
                    market_data["recent_closed_trades"] = _hist
                    _wp = sum(h["pnl"] for h in _hist)
                    logger.info(
                        f"[辩论引擎] 已注入最近 {len(_hist)} 笔真实成交(内存优先,近 {_hist[0]['when']}) "
                        f"合计盈亏 {_wp:+.2f}$ 到决策上下文"
                    )
                else:
                    logger.info("[辩论引擎] 暂无已平仓成交可注入（内存+DB 均为空）")
            except Exception as _te:
                logger.warning(f"[辩论引擎] 历史成交注入跳过: {_te}")

        # Step 0.8: 反转哨兵（第3辩论角色）— 趋势末端反转制衡，根治「山顶开BUY」
        # 全局共享（行情主号数据），对所有账号一致生效 → 天然多账号优先
        try:
            from app.core.reversal_sentinel import evaluate as _sentinel_eval
            _sentinel = _sentinel_eval(market_data)
            market_data["reversal_sentinel"] = _sentinel
            if _sentinel.get("signal") != "NONE":
                logger.info(
                    f"[辩论引擎] 反转哨兵:{_sentinel['signal']}(置信{_sentinel['confidence']:.0%}) "
                    f"证据={_sentinel.get('evidence')}"
                )
        except Exception as _se:
            logger.warning(f"[辩论引擎] 反转哨兵跳过: {_se}")
            market_data["reversal_sentinel"] = {"signal": "NONE", "confidence": 0.0}

        # Step 0.85: Meta 质量陪审团（v4 核心·本地时序模型制衡语义大脑）
        # 用户实盘实证：云 DeepSeek 曾固执买多(LSTM/TCN 强烈看空)→亏损。
        # 本步用本地 Chronos 分位数 + SMC/Regime 融合出「信号质量分 Q + 止盈 regime」，
        # 回答「这笔信号质量高不高、该让利润跑还是啃头皮」，不预测方向（方向交给 DeepSeek/SMC）。
        # 只做提准（HIGH/MID 给动态 TP 天花板 / LOW 紧 ATR 啃头皮），绝不拦截开仓 → 符合「提准非拦截」。
        # 全局共享（行情主号数据），对所有账号一致生效 → 天然多账号优先。
        try:
            from app.services.meta_quality import evaluate_meta_quality
            _mq = evaluate_meta_quality(market_data)
            market_data["meta_quality"] = _mq
            # ★ Phase 6：Chronos 健康上报。它是增强项不是必需项——单独挂掉
            #   不降级（云端双脑照常决策），但 L2 副驾模式下它是必需的背书方。
            if _hm0:
                _hm0.report("chronos", bool(_mq.get("chronos_available")), "Chronos 不可用")
            if _mq.get("regime"):
                logger.info(
                    f"[辩论引擎] Meta质量陪审团: regime={_mq['regime']} Q={_mq.get('q')} "
                    f"Chronos方向={_mq.get('chronos_dir')}(可用={_mq.get('chronos_available')}) "
                    f"TP天花板={_mq.get('chronos_tp_ceiling')}"
                )
        except Exception as _me:
            logger.warning(f"[辩论引擎] Meta质量陪审团跳过: {_me}")
            market_data["meta_quality"] = {}
            if _hm0:
                _hm0.report("chronos", False, str(_me)[:120])

        # ── 大脑审计：注入全部完成后，记录云脑真实看到的完整输入 ──
        # 原记录点在 Step 0 注入前（market_data 还是毛坯），导致
        # my_open_positions/portfolio_state/recent_closed_trades/reversal_sentinel/
        # empirical_lessons/meta_quality 尚未注入即被记录 → completeness 假阴性、
        # 审计误判"云脑没看到持仓"。现延后到 Step 0.5~0.85 全部注入之后。
        try:
            _ba_rec("deepseek", "input", input_fields=market_data)
            _ba_rec("hunyuan", "input", input_fields=market_data)
        except Exception:
            pass

        # Step 0.75: 本地进化引擎（真在线学习，区别于经验回注反模式）
        # ATLAS(ACL2026)实证：reflection式教训回注无效且制造偏置；正确做法是
        # 基于每笔真实盈亏持续更新「情境→期望盈亏」映射。零接入点风险（从成交缓冲同步）。
        try:
            from app.services.ai_memory import get_trades as _mem_get_trades
            from app.services.local_rl import get_engine
            _eng = get_engine()
            _buf_all = _mem_get_trades(300)
            _eng.sync_from_buffer(
                [e for e in _buf_all if str(e.get("kind", "")) in ("close", "close_partial", "sl")]
            )
            market_data["evolution_advice"] = _eng.get_advice(market_data)
            _drift = _eng.drift_alert()
            if _drift:
                logger.warning(f"[辩论引擎] 进化引擎漂移预警: {_drift}")
        except Exception as _ee:
            logger.debug(f"[辩论引擎] 进化引擎跳过: {_ee}")
            market_data["evolution_advice"] = []

        # Step 1: 双模型独立分析（并发执行，延迟从 2×T 降到 ~max(T1,T2)）
        logger.info("[辩论引擎] 1. 双模型独立分析（并发）...")
        # 实时交易使用快速模型（use_deep_think=False）：
        # 深度思考模型固有响应分钟级，无法满足 60s 一轮的实时决策时延要求，
        # 决策质量由「双模型 + 多轮辩论 + Meta 加权裁决」共同保证，不依赖单模型慢思考。
        #
        # ★ Phase 6 降级车道：已判定失联的云在 L2/L3 下不再每轮硬撞
        #   （60s 一轮里两次 30s 超时会把整轮吃光），但监视器保留 half-open
        #   探活窗口（每 180s 放行一次），确保云恢复后系统能自愈回 L0。
        _hm = _get_health_monitor()
        _cloud_enabled = effective_cloud_enabled()
        _ds_allowed = _cloud_enabled and (_hm.allow_cloud_call("deepseek") if _hm else True)
        _hy_allowed = _cloud_enabled and (_hm.allow_cloud_call("hunyuan") if _hm else True)

        def _skipped(name: str, reason: str = "处于降级熔断期") -> dict:
            return {
                "decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                "_skipped": True,
                "reasoning": f"（{name} {reason}，本轮跳过调用）",
            }

        if not _cloud_enabled:
            logger.info("[辩论引擎] 云模型总开关 ENABLE_CLOUD_MODELS=False，跳过 DeepSeek/混元调用")

        if _ds_allowed and _hy_allowed:
            with ThreadPoolExecutor(max_workers=2) as ex:
                ds_fut = ex.submit(self.deepseek.analyze, market_data, False)
                hy_fut = ex.submit(self.hunyuan.analyze, market_data)
                deepseek_analysis = ds_fut.result()
                hunyuan_analysis = hy_fut.result()
        else:
            # 只调还活着的那朵（或都不调）——串行即可，最多一个调用
            if not _cloud_enabled:
                # ★ 云模型总开关关闭：云票被「禁用」而非「调用失败」。
                #   禁用绝不能带 _api_failed，否则下方 line 480 的
                #   `ds_api_failed and hy_api_failed` 会把「禁用」误判成
                #   「双云全失败」，提前 return 进 _degraded_decide 降级车道，
                #   从而跳过 Meta-Agent 的「关云→纯本地融合票裁决」路径
                #   （meta_agent.adjudicate 已正确实现：云票权重归零、
                #   由本地时序融合第四票主导方向）。实测该误判会导致
                #   关云后永远 HOLD / 置信度 0 / 彻底不开仓。
                _disabled = {
                    "decision": "HOLD",
                    "confidence": 0.0,
                    "_disabled": True,
                    "reasoning": "云模型总开关已关闭，云票禁用（转纯本地融合决策）",
                }
                deepseek_analysis = _disabled
                hunyuan_analysis = _disabled
            else:
                # 降级熔断期：真实调用失败的那朵才带 _api_failed（触发降级车道）
                _skip_reason = "处于降级熔断期"
                deepseek_analysis = (
                    self.deepseek.analyze(market_data, False) if _ds_allowed
                    else _skipped("DeepSeek", _skip_reason)
                )
                hunyuan_analysis = (
                    self.hunyuan.analyze(market_data) if _hy_allowed
                    else _skipped("混元", _skip_reason)
                )

        # 检测 API 是否失败（配额耗尽/Key 失效/网络中断等）
        ds_api_failed = bool(deepseek_analysis.get("_api_failed", False))
        hy_api_failed = bool(hunyuan_analysis.get("_api_failed", False))

        # ★ 向降级监视器上报（只在真实发起过调用时上报，跳过的不算数据点——
        #   否则「跳过 → 上报失败 → 更判失联」会自我强化，永远出不来）
        if _hm:
            if _ds_allowed:
                _hm.report("deepseek", not ds_api_failed,
                           str(deepseek_analysis.get("reasoning", ""))[:120])
            if _hy_allowed:
                _hm.report("hunyuan", not hy_api_failed,
                           str(hunyuan_analysis.get("reasoning", ""))[:120])

        if hy_api_failed and not ds_api_failed:
            logger.warning("[辩论引擎] 混元 API 不可用，降级为 DeepSeek 单模型模式")
        elif ds_api_failed and not hy_api_failed:
            # ★ 2026-08-11 本地副驾补位（L1.5）：DS 单云失败 → 不再白等变单脑，
            #   而是让本地 Qwen3-8B 以「副驾」身份补 DS 的票（带 copilot_gate 三道锁：
            #   Chronos 同向 + 置信门槛 + 手数系数降权），保住「双脑」结构。
            #   云消耗实证（8/11）：DS 402 欠费 465 次、单日 600+ 次云端调用，
            #   单脑运行 8.5h 决策质量塌方 → 本地兜底是刚需不是可选项。
            _local_vote = None
            _local_gate = None
            try:
                # ★ 2026-08-15 审计P2修复：L1 副驾补位同样受 LOCAL_COPILOT_VOTE_ENABLED 约束。
                #   #11 只盖了 meta_agent 第五票(L816) 与 L2 关键路径(_degraded_decide)，漏了
                #   本路径 → 开关关闭时 DS 失联仍会被 8B 顶全权重票，开关语义不完整。
                if not bool(getattr(settings, "LOCAL_COPILOT_VOTE_ENABLED", True)):
                    logger.debug("[辩论引擎] 副驾开关已关闭 → L1 补位跳过（与第五票/L2 一致）")
                else:
                    from app.services.local_llm_service import copilot as _local_copilot
                    from app.services.local_llm_service import copilot_gate as _local_gate_fn
                    from app.services.local_llm_service import is_available as _local_ok
                    if _local_ok():
                        _local_vote = _local_copilot(market_data)
                        _cdir = (market_data.get("meta_quality") or {}).get("chronos_dir")
                        _local_gate = _local_gate_fn(_local_vote, _cdir)
            except Exception as _le:
                logger.warning(f"[辩论引擎] 本地副驾补位异常: {_le}")
                _local_vote = None
            if _local_vote is not None and _local_gate and _local_gate.get("allow"):
                deepseek_analysis = {
                    "decision": _local_gate["decision"],
                    "confidence": _local_gate["confidence"],
                    "reasoning": f"[本地Qwen3副驾补位·DS失联] {getattr(_local_vote, 'reason', '')}",
                    "_api_failed": True,          # 保持降级标记（DS 确实不可用）
                    "_local_copilot": True,       # 票源标记：来自本地副驾，不计入 DS 准确率
                    "agree_with_opponent": False,
                }
                logger.info(
                    f"[辩论引擎] ✅ DS 失联 → 本地副驾补位成功: "
                    f"{_local_gate['decision']}({_local_gate['confidence']:.0%})"
                )
            else:
                logger.warning("[辩论引擎] DeepSeek API 不可用 → 混元单模型模式（本地副驾不可用/未过锁）")

        # ★ 双云全失 → 交给 Phase 6 降级车道（L2 本地副驾 / L3 熔断）
        if ds_api_failed and hy_api_failed:
            return self._degraded_decide(market_data, account_id)

        logger.info(
            f"[辩论引擎] 初判: DS={deepseek_analysis.get('decision')}({deepseek_analysis.get('confidence',0):.0%}) "
            f"HY={hunyuan_analysis.get('decision')}({hunyuan_analysis.get('confidence',0):.0%})"
            + (" [混元API失败]" if hy_api_failed else "")
            + (" [DeepSeekAPI失败]" if ds_api_failed else "")
        )

        # Step 2: 多轮交叉辩论（每轮双模型反驳并发执行）
        ds_final = deepseek_analysis
        hy_final = hunyuan_analysis

        # ★ 云模型总开关关闭时，没有双脑可辩论，直接跳过辩论轮，把 HOLD 空票交给 Meta-Agent。
        #   Meta-Agent 会以本地时序融合票为实际方向权威进行裁决。
        _cloud_enabled = effective_cloud_enabled()
        if not _cloud_enabled:
            logger.info("[辩论引擎] 2. 云模型已关闭，跳过交叉辩论")
            deepseek_rebuttal = deepseek_analysis
            hunyuan_rebuttal = hunyuan_analysis
            round_num = 0  # 关云无辩论轮次；下方 snapshot 的 rounds 字段需有值（防 UnboundLocalError）
        else:
            for round_num in range(1, debate_rounds + 1):
                logger.info(f"[辩论引擎] 2.{round_num} 辩论第{round_num}轮...")

                if hy_api_failed:
                    # 混元不可用：DeepSeek 单模型自我修正（不调 HY API）
                    deepseek_rebuttal = self.deepseek.debate_rebuttal(
                        opponent_analysis=hy_final,
                        my_analysis=ds_final,
                        market_data=market_data,
                    )
                    hunyuan_rebuttal = hy_final
                elif ds_api_failed:
                    # ★ 对称分支：DeepSeek 不可用 → 混元单模型自我修正（不调 DS API）。
                    #   原实现只处理了「混元挂」，DeepSeek 挂时仍会去撞死接口，
                    #   在 L1 下每轮白等一次超时。
                    hunyuan_rebuttal = self.hunyuan.debate_rebuttal(
                        opponent_analysis=ds_final,
                        my_analysis=hy_final,
                        market_data=market_data,
                    )
                    deepseek_rebuttal = ds_final
                else:
                    with ThreadPoolExecutor(max_workers=2) as ex:
                        ds_fut = ex.submit(
                            self.deepseek.debate_rebuttal,
                            opponent_analysis=hy_final,
                            my_analysis=ds_final,
                            market_data=market_data,
                        )
                        hy_fut = ex.submit(
                            self.hunyuan.debate_rebuttal,
                            opponent_analysis=ds_final,
                            my_analysis=hy_final,
                            market_data=market_data,
                        )
                        deepseek_rebuttal = ds_fut.result()
                        hunyuan_rebuttal = hy_fut.result()

                # 更新立场
                ds_final = deepseek_rebuttal
                hy_final = hunyuan_rebuttal

                ds_agree = ds_final.get("agree_with_opponent", False)
                hy_agree = hy_final.get("agree_with_opponent", False)

                logger.info(
                    f"[辩论引擎] 辩论R{round_num}: DS修正→{ds_final.get('decision')} 同意对方:{ds_agree} | "
                    f"HY修正→{hy_final.get('decision')} 同意对方:{hy_agree}"
                )

                # 如果双方达成共识，提前结束辩论
                if ds_agree and hy_agree:
                    logger.info("[辩论引擎] 双方达成共识，提前结束辩论")
                    break

        # Step 3: Meta-Agent 加权裁决
        logger.info("[辩论引擎] 3. Meta-Agent加权裁决...")
        # ── 大脑审计：记录双脑输出（输出了什么）──
        try:
            from app.services.brain_audit import record as _ba_rec
            _ba_rec("deepseek", "output", output=deepseek_analysis, adopted=1, consumer="meta_agent")
            _ba_rec("hunyuan", "output", output=hunyuan_analysis, adopted=1, consumer="meta_agent")
        except Exception:
            pass
        decision = self.meta_agent.adjudicate(
            deepseek_analysis=deepseek_analysis,
            hunyuan_analysis=hunyuan_analysis,
            deepseek_rebuttal=ds_final,
            hunyuan_rebuttal=hy_final,
            market_data=market_data,
        )

        # Step 3.5: 本地校对员（Qwen3-8B）—— L0 常态职责
        # ------------------------------------------------------------------
        # 这是本地 8B 在**正常模式**下唯一被允许的工作：拿着确定性清单核对
        # 云端产出的决策（字段完整性 / 自相矛盾 / 止损方向 / 幻觉价格）。
        # 它不投票、不改方向；只有「代码侧算术审计」认定的结构性缺陷
        # （止损/止盈挂反、价格幻觉）才会把本笔降级为 HOLD，
        # 8B 自己的主观疑点一律只记录告警。
        #
        # 为什么值得做：云端大模型偶发的结构性错误（比如 BUY 却把止损挂在
        # 入场价上方）单看置信度是看不出来的，而这类错误一旦成交就是真金白银
        # 的损失。让一个 5GB 的本地模型做"对照检查"，成本几乎为零。
        #
        # 为什么绝不让它拦单：拦单就等于给了 8B 否决权，而 8B 的金融判断
        # 接近随机（Fin-Bias, ACL2026），拿它当门神会砍掉本该赚钱的交易，
        # 直接违背"提准非拦截"的最高红线。
        self._apply_proofread(decision, market_data)

        # Step 3.6: 方向软警示器（NumPy 统计特征 / 未来可替换为 Chronos/TimesFM 等）
        # ------------------------------------------------------------------
        # 用户实证：M5-H4 全 BUY 但行情在跌时，本地时序信号（Chronos）更贴近
        # 真实方向；云端方向来自滞后指标包装。因此引入"统计上下文"：
        #   · 统计信号与云端方向强冲突且价格处于极端延伸位 → 【软降权】(置信×0.6)，
        #     不强行改写方向（提准非拦截：保留交易量=保留利润，硬投票会砍 98% 行情）；
        #   · 仅当 AI 自身置信本就极低(<0.25)才视同 HOLD，规避极端延伸位硬接飞刀。
        # 当前工作机沙箱无法运行 PyTorch，先用纯 NumPy 规则版兜底；等部署机跑
        # 通真实时序模型后，可在同一接口下无缝替换，不影响交易链其它模块。
        # 方向终审权始终在 MetaAgent 加权软投票（融合票第四票），本步只提供软上下文。
        self._apply_direction_guard(decision, market_data)

        # Step 4: 记录完整决策过程
        self._last_deepseek_analysis = deepseek_analysis
        self._last_hunyuan_analysis = hunyuan_analysis
        self._last_market_data = market_data

        # 缓存完整辩论快照（供前端"辩论擂台"展示）
        from datetime import datetime
        snapshot = {
            "ts": datetime.utcnow().isoformat(),
            "ds_initial": deepseek_analysis,
            "hy_initial": hunyuan_analysis,
            "ds_final": ds_final,
            "hy_final": hy_final,
            "decision": decision,
            "rounds": round_num,
            "rounds_total": debate_rounds,
        }
        self.last_debate = snapshot
        self._history.append(snapshot)
        if len(self._history) > 50:
            self._history = self._history[-50:]

        logger.info(
            f"[辩论引擎] ✅ 最终决策: {decision.decision} "
            f"置信度:{decision.confidence:.2f} "
            f"风险:{decision.risk_level}"
        )
        # ★ 2026-08-07：按账号 ID 写入隔离缓存（加锁防并发竞态）。
        # ★ 2026-08-15 审计P2修复：存**深拷贝**——执行器下单前结构闸门会写
        #   ai_decision.proofread_blocked/block_reason（L2041-2042）。若缓存存对象引用，
        #   45s 内同账号复用缓存会返回被上一轮改写过的对象 → 下笔快照/前端误标「被拦截」
        #   （脏缓存）。deepcopy 后执行器改的是副本，缓存保持干净。
        with _CACHE_LOCK:
            _SHARED_DECISION_CACHE[account_id] = {"t": time.time(), "decision": copy.deepcopy(decision)}
        return decision

    # ============================================================
    #  Phase 9 本地校对员（L0 常态）
    # ============================================================
    def _apply_proofread(self, decision, market_data: dict) -> None:
        """让本地 Qwen3-8B 校对云端决策，结果写回 decision 的 proofread_* 字段。

        契约（调用方可以依赖）：
          * **永不抛异常**——校对是增值项，不能因为它挂了就断了交易链。
          * **永不修改 confidence**，也**永不翻转方向**（BUY 不会变 SELL）。
          * 唯一允许的干预：`code_severity == "major"` 时把本笔降级为 HOLD，
            即「这一枪先别开」。仅限**代码侧算术审计**认定的结构性缺陷
            （止损挂反 / 止盈挂反 / 价格幻觉），这类单成交即亏或无效，
            拦掉是纯收益，不属于「靠过滤刷胜率」。
          * **LLM 的主观判断永不拦单**。8B 的金融判断接近随机
            （Fin-Bias, ACL2026），给它否决权 = 砍掉本该赚钱的交易，
            直接违背「提准非拦截」最高红线。它的疑点只记录告警。
          * 本地模型不可用时保持 `status="skipped"`，绝不伪装成 "clean"。

        ★ 2026-08-08 审计修复：此前这里读的是合并后的 `res.severity`（代码侧与
          LLM 取高者），意味着 8B 报一个主观 major 就能把单子拦掉；叠加
          「LLM 输出格式不规范 + 有疑点 → 自动判 major」那条，等于**格式抖动
          都能砍单**。已改为只认 `res.code_severity`。

        HOLD 决策不校对：没有方向、没有止损，清单里 4 条有 3 条不适用，
        花 2 秒去查一个不会下单的决策纯属浪费主循环时间。
        """
        try:
            if decision is None or getattr(decision, "decision", "HOLD") == "HOLD":
                return

            from app.services.local_llm_service import proofread as _proofread

            # ★ 修复：market_analyzer 在生产环境把 current_price 返回为
            #   {"bid","ask","last"} 字典（market_analyzer.py:168），与下方方向终审器
            #   :702-706 解包逻辑保持一致，先把字典拆出标量 last 价，避免把 dict 当
            #   标量传入 local_llm_service._structural_audit 触发 dict>int TypeError（被
            #   :666 的 try 静默吞掉），导致云端决策阶段的本地校对员/结构审计断路器失效。
            _cp_raw = (market_data or {}).get("current_price")
            _cp_scalar = (
                _cp_raw.get("last") if isinstance(_cp_raw, dict) else None
            ) or (market_data or {}).get("price") or (market_data or {}).get("close")
            snap = {"current_price": _cp_scalar, "orderflow": (market_data or {}).get("orderflow")}
            payload = {
                "decision": decision.decision,
                "confidence": decision.confidence,
                "entry_price": snap.get("current_price"),
                "stop_loss": getattr(decision, "stop_loss", None),
                "take_profit": getattr(decision, "take_profit", None),
                "reason": getattr(decision, "reasoning_summary", "")[:500],
            }
            res = _proofread(payload, snap)

            hm = _get_health_monitor()
            if res is None:
                # 没查成。保持 skipped，并把"本地模型不可用"如实上报健康监控——
                # 校对失败不影响交易，但必须让运维看见，否则会以为它一直在工作。
                if hm:
                    hm.report("local_llm", False, "校对员未返回结果")
                return

            decision.proofread_status = "clean" if res.ok else "issues"
            decision.proofread_issues = list(res.issues or [])
            decision.proofread_severity = res.severity
            decision.proofread_latency_ms = round(res.latency_ms, 1)
            if hm:
                hm.report("local_llm", True, "")

            if not res.ok:
                logger.warning(
                    f"[本地校对员] ⚠ 发现 {len(res.issues)} 处问题"
                    f"(severity={res.severity}): {'; '.join(res.issues)[:300]} "
                    f"—— 仅记录告警，不干预交易决策"
                )
                # ★ Phase 9.1 闭环断路器（2026-08-08）：
                #   sev=major 的结构性错（SL/TP 挂反、价格幻觉、理由与方向自相矛盾）
                #   视为「这单结构上有问题，成交即亏/无效」。此时**不改方向、不投票**，
                #   只把本笔决策降级为 HOLD —— 即「这枪先别开」，等下一轮干净决策。
                #   这是断路器不是否决权：不把 BUY 翻成 SELL，也不影响云端方向判断，
                #   只是拦住这一笔明显有结构缺陷的单子，符合「提准非拦截」红线。
                # ★ 只认 code_severity：纯算术、100% 可复现、无主观成分。
                #   LLM 的 major 到不了这里（它最多让 res.severity 变高，
                #   而 res.severity 只用于展示和告警分级）。
                if (res.code_severity == "major"
                        and getattr(decision, "decision", "HOLD") != "HOLD"):
                    decision.decision = "HOLD"
                    decision.proofread_blocked = True
                    decision.block_reason = (
                        "本地校对员(结构审计)拦截结构性缺陷：" +
                        "；".join(res.code_issues or res.issues)[:200]
                    )
                    logger.warning(
                        f"[本地校对员] 🛑 本笔决策降级为 HOLD（断路器触发）：{decision.block_reason}"
                    )
            else:
                logger.info(
                    f"[本地校对员] ✓ 决策核对通过 ({res.latency_ms:.0f}ms)"
                )

            # ★ 措施文案（"做了什么"）：由 status/severity/blocked 确定性派生，
            #   不依赖 LLM 主观输出，供前端展开"系统对该疑点采取了什么措施"。
            _st = getattr(decision, "proofread_status", "skipped")
            _sev = getattr(decision, "proofread_severity", "none")
            _blk = getattr(decision, "proofread_blocked", False)
            if _blk:
                decision.proofread_action = (
                    "🛑 已触发结构审计断路器：本笔决策降级为 HOLD，不予开仓"
                    "（不改方向、不投票，只拦结构自杀单）"
                )
            elif _st == "issues" and _sev == "major":
                decision.proofread_action = (
                    "⚠ 重大结构性疑点（止损/止盈方向错误或价格幻觉），"
                    "已记录告警，建议人工复核后再放行"
                )
            elif _st == "issues" and _sev == "minor":
                decision.proofread_action = (
                    "ℹ 轻微疑点（如盈亏比偏低/止损偏近），已记录告警，不干预交易"
                )
            elif _st == "clean":
                decision.proofread_action = (
                    "✓ 结构核对通过：止损/止盈方向正确、无价格幻觉、理由与方向一致"
                )
            elif _st == "skipped":
                decision.proofread_action = (
                    "本地校对员未参与（模型不可用/未启用），本笔未经本地核对"
                )
            else:
                decision.proofread_action = "已核对"
        except Exception as e:
            # 锦上添花，绝不能成为交易链上的新故障源——但也不能静默吞掉让故障
            # 长期潜伏。改为 warning 级可见 + 上报健康监控，让运维能看见。
            logger.warning(f"[本地校对员] 跳过（异常已吞，不影响交易）: {e}")
            hm = _get_health_monitor()
            if hm:
                hm.report("local_llm", False, f"校对员异常: {e}")

    # ============================================================
    #  Phase 10 方向终审器（NumPy 规则版 / 未来替换为真实时序模型）
    # ============================================================
    def _apply_direction_guard(self, decision: DebateDecision, market_data: dict) -> None:
        """用纯 NumPy 统计特征对云端方向做【软警示】，而非硬否决。

        契约（2026-08-11 修订 · AI 模型思维 / 提准非拦截）：
          * **永不抛异常**——和校对员一样，是新增强项不是故障源。
          * **永不单方面改写方向**：major 冲突只做【软降权】(置信×0.6) 并标记记录；
            minor 只记录不干预。方向终审权始终在 MetaAgent 加权软投票（融合票第四票）。
          * 仅当 AI 自身置信本就极低(<0.25)时才视同 HOLD，规避极端延伸位硬接飞刀。
          * 这是「统计上下文 / 软贡献」，不是「信号源 / 硬门」。

        未来替换：把 `NumpyDirectionGuard` 换成 `TSArena` 中的真实时序模型预测，
        本方法其余逻辑（冲突 → 软降权、minor → 记录）保持不变。
        """
        try:
            if decision is None or getattr(decision, "decision", "HOLD") == "HOLD":
                return

            # 从 market_data 取收盘价序列：优先 H1（2 天≈48 根），不足则用 M15。
            tfs = (market_data or {}).get("timeframes", {})
            closes = None
            for tf in ("H1", "M15", "M5"):
                if tf in tfs and isinstance(tfs[tf], dict) and "closes" in tfs[tf]:
                    cand = tfs[tf]["closes"]
                    if isinstance(cand, (list, tuple)) and len(cand) >= 50:
                        closes = list(cand)
                        break
            # 次选：外部直接传入的 closes（便于单测/回测）
            if not closes:
                closes = (market_data or {}).get("recent_closes") or (market_data or {}).get("closes")

            current_price_raw = (market_data or {}).get("current_price")
            current_price = (
                (current_price_raw.get("last") if isinstance(current_price_raw, dict) else None)
                or (market_data or {}).get("price")
                or (market_data or {}).get("close")
            )
            if not closes or current_price is None:
                return

            from app.services.numpy_direction_guard import NumpyDirectionGuard

            guard = NumpyDirectionGuard()
            res = guard.review(closes, float(current_price), decision.decision)

            decision.direction_guard_score = res.direction_score
            decision.direction_guard_conflict = res.conflict_level
            decision.direction_guard_reason = res.reason
            decision.direction_guard_model = "numpy"
            # ★ 2026-08-15 第三批#4 纯加法：把规则③判定依赖的 3 个原始特征透传进决策对象，
            #   由 decision_snapshot 收口落库，使历史单可忠实回放规则③（不干预实时决策）。
            _dg_feats = getattr(res, "features", None) or {}
            decision.direction_guard_price_to_ma_z = _dg_feats.get("price_to_ma_z")
            decision.direction_guard_z_avg_5 = _dg_feats.get("z_avg_5")
            decision.direction_guard_rsi14 = _dg_feats.get("rsi14")

            if res.conflict_level == "major":
                # ★ 2026-08-11 改为「软警示」而非「硬否决」（AI 模型思维 / 提准非拦截）：
                # 不再单方面把「云端双脑 + 融合票(第四票)」的方向改写成 HOLD —— 那是 EA
                # 写死方向门，违背纯AI铁律#2；且硬投票会砍掉 98% 有效行情→腰斩利润
                # （见 v2.7.06 教训与 SOL 实盘案例）。改为：仅记录强冲突，把 numpy 统计
                # 分作为【软降权】提示，方向仍交由 MetaAgent 加权软投票（融合票已是第四票）
                # 与双脑共同裁决。仅当 AI 自身置信本就极低(<0.25)时才视同 HOLD，
                # 避免极端延伸位硬接飞刀这一真正危险场景。
                logger.warning(
                    f"[方向终审器] ⚠ {decision.decision} 与统计信号强冲突（软警示·不否决）：{res.reason[:120]}"
                )
                decision.direction_guard_blocked = False
                _ai_conf = getattr(decision, "confidence", 1.0) or 0.0
                if _ai_conf < 0.25:
                    decision.decision = "HOLD"
                else:
                    # 软降权：冲突折算成置信折损，让融合/双脑在边缘时自然收敛到 HOLD，
                    # 但不强行改写已经达成的方向共识（保留交易量 = 保留利润）。
                    decision.confidence = max(0.15, _ai_conf * 0.6)
            elif res.conflict_level == "minor":
                logger.info(
                    f"[方向终审器] ⚠ {decision.decision} 轻微统计冲突（仅记录·不干预）: {res.reason[:120]}"
                )
        except Exception as e:
            # 方向终审不能成为交易链故障源。
            logger.debug(f"[方向终审器] 跳过（异常已吞）: {e}")

    # ============================================================
    #  Phase 6 降级车道：双云全失后的决策路径
    # ============================================================
    def _degraded_decide(self, market_data: dict, account_id: str = None) -> DebateDecision:
        """云端双脑全部失联时的决策（L2 本地副驾 / L3 熔断）。

        ★ 铁律：本方法**只可能产出 HOLD 或一个被三道锁验过的方向**，
          绝不会产出「平仓」指令。降级期间已有持仓一律交回
          SL/TP/SmartExit/护盾篮子处理 —— 只关水龙头，不抽桶里的水。

        ★ 铁律：本地 8B 的方向票必须叠 Chronos 同向 + 置信门槛（copilot_gate），
          单靠 8B 判方向接近抛硬币（Fin-Bias, ACL2026）。
        """
        base = dict(
            deepseek_weight=0.0,
            hunyuan_weight=0.0,
            deepseek_vote="HOLD",
            hunyuan_vote="HOLD",
        )
        mq = market_data.get("meta_quality") or {}
        chronos_dir = mq.get("chronos_dir")
        chronos_available = bool(mq.get("chronos_available"))
        q_score = mq.get("q")

        def _mk(decision: str, conf: float, summary: str, risk: str,
                plain: str, cvote: str = "HOLD", cweight: float = 0.0) -> DebateDecision:
            d = DebateDecision(
                decision=decision,
                confidence=conf,
                reasoning_summary=summary,
                risk_level=risk,
                plain_summary=plain,
                consensus="degraded",
                chronos_vote=cvote,
                chronos_weight=cweight,
                q_score=q_score,
                **base,
            )
            with _CACHE_LOCK:
                _SHARED_DECISION_CACHE[account_id] = {"t": time.time(), "decision": d}
            return d

        hm = _get_health_monitor()

        # ★ #11 一致性：副驾开关须同时约束「常态5th票」(meta_agent L816) 与「L2 关键路径」(本方法)。
        #   开关关闭时，L2 降级车道同样不可用 qwen3:8b → 视为无决策能力 → 落 L3 熔断（停新开仓）。
        #   默认 True(config LOCAL_COPILOT_VOTE_ENABLED)，故常态行为不变；仅显式关闭才退 L3。
        try:
            from app.core.config import settings as _cfg
        except Exception:  # noqa: BLE001
            from app.config import settings as _cfg
        _copilot_enabled = bool(getattr(_cfg, "LOCAL_COPILOT_VOTE_ENABLED", True))

        # ★ 2026-08-15 审计自纠：import 必须带保护——降级车道是安全网，绝不能因
        #   local_llm_service 导入失败（依赖缺失/循环导入）崩掉整条 L2/L3 决策路径。
        #   import 失败视同副驾不可用 → _local_ok 恒 False → vote=None → 落 L3 熔断。
        try:
            from app.services.local_llm_service import copilot as _local_copilot
            from app.services.local_llm_service import copilot_gate as _gate
            from app.services.local_llm_service import is_available as _local_ok
        except Exception as _ie:  # noqa: BLE001
            logger.warning(f"[辩论引擎·降级] 副驾模块导入失败，视为不可用: {_ie}")
            _local_copilot = None
            _gate = None
            _local_ok = lambda: False

        vote = None
        if _copilot_enabled:
            try:
                local_ok = _local_ok()
                if hm:
                    hm.report("local_llm", local_ok,
                              "" if local_ok else "Ollama/qwen3 不可用")
                if local_ok:
                    vote = _local_copilot(market_data)
                    if hm:
                        hm.report("local_llm", vote is not None, "副驾未返回有效票")
            except Exception as _le:
                logger.warning(f"[辩论引擎·降级] 本地副驾调用异常: {_le}")
                if hm:
                    hm.report("local_llm", False, str(_le)[:120])
                vote = None
        else:
            logger.warning("[辩论引擎·降级] 副驾开关(LOCAL_COPILOT_VOTE_ENABLED)已关闭 → L2 无决策能力，转 L3 熔断")
            if hm:
                hm.report("local_llm", False, "副驾开关已关闭")

        if vote is None:
            # ---- L3：无任何可信决策能力 → 停发新开仓（持仓不动）----
            logger.error(
                "[辩论引擎·降级] L3 熔断：云端双脑失联且本地副驾不可用 → "
                "本轮不开新仓（已有持仓仍由 SL/TP/智能平仓守护）"
            )
            return _mk(
                "HOLD", 0.0,
                "L3 熔断：云端双脑失联且本地 Qwen3 副驾不可用，停发新开仓信号。"
                "已有持仓不受影响，继续由止损/止盈/智能平仓守护。",
                "extreme",
                "AI 大脑全部离线，系统已停止开新单；你手上的单子仍有止损止盈在守着。",
            )

        # ---- L2：本地副驾 + Chronos 双确认 ----
        gate = _gate(vote, chronos_dir if chronos_available else None)
        cweight = 0.5 if chronos_available else 0.0
        cvote = str(chronos_dir or "HOLD").upper() if chronos_available else "HOLD"
        if cvote not in ("BUY", "SELL", "HOLD"):
            cvote = "HOLD"

        if not gate["allow"]:
            logger.warning(f"[辩论引擎·降级] L2 副驾未放行: {gate['reason']}")
            return _mk(
                "HOLD", float(gate.get("confidence") or 0.0),
                f"L2 本地副驾模式：{gate['reason']}（云端双脑失联）。",
                "high",
                f"云端 AI 掉线，本地小模型接管但把握不足（{gate['reason']}），本轮观望。",
                cvote=cvote, cweight=cweight,
            )

        logger.warning(
            f"[辩论引擎·降级] L2 副驾放行: {gate['decision']} "
            f"置信{gate['confidence']:.2f} + Chronos {cvote} 同向（手数将降至 40%）"
        )
        return _mk(
            gate["decision"], float(gate["confidence"]),
            f"L2 本地副驾模式：Qwen3-8B 判 {gate['decision']}"
            f"(置信 {gate['confidence']:.2f}) 且 Chronos 时序同向；"
            f"云端双脑失联期间手数自动降至 40%。",
            "high",
            f"云端 AI 掉线，本地模型和时序模型都看{'涨' if gate['decision'] == 'BUY' else '跌'}，"
            f"谨慎小仓位试探（仓位已自动砍到四成）。",
            cvote=cvote, cweight=cweight,
        )

    def get_last_context(self) -> dict:
        """获取上一次决策的完整上下文（用于交易记录）"""
        return {
            "deepseek_analysis": getattr(self, "_last_deepseek_analysis", {}),
            "hunyuan_analysis": getattr(self, "_last_hunyuan_analysis", {}),
            "market_data": getattr(self, "_last_market_data", {}),
        }
