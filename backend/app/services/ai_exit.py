"""
XAU/USD 万象Ai — AI 出场决策 Agent（M1）
=======================================
替代 smart_exit.py 的写死规则引擎，由 LLM 对每笔持仓推理出场意图。
机械层(trade_executor._manage_positions)只负责执行；risk_engine / L3护盾 / L2反转防抖为硬护栏。

设计铁律（红线）：
- AI 只输出"意图"(hold / partial_close / full_close / reverse_signal + 可选 new_sl)，机械层执行。
- 严禁 AI 移除 SL（new_sl=0 或置于错向 → 直接拒绝并回退规则引擎）。
- 任何 LLM 超时 / 异常 / 校验失败 → 整笔回退 smart_exit 规则引擎（出场永不卡死）。
- 单账号出场决策硬墙：EXIT_BUDGET 秒，超时不候直接回退。
- 每账号批量决策（一次 LLM 调用覆盖该账号全部持仓），控制延迟与成本。
"""
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from loguru import logger
from app.services.smart_exit import evaluate_position as smart_evaluate_position


# 每账号出场决策硬墙（秒）：超过此时间未返回则整批回退规则引擎
# 16s 仍在主循环 120s 超时墙内（4 账号最坏 64s 不超限），给 DeepSeek 并发延迟留余量
EXIT_BUDGET = 16.0
# 后台 LLM 调用超时墙（秒）：异步不阻塞主循环，可给足时间熬过 DeepSeek 并发排队
# （辩论 analyze 用默认 120s 能成功，出场若只给 16s 会被掐断 → 后台放宽到 50s）
EXIT_LLM_TIMEOUT = 50.0

# ★ 全局出场评估速率限制：4 账号错开，避免同分钟并发挤爆 DeepSeek 导致挂死
# 两次出场 LLM 评估至少间隔 12s，保证 DeepSeek 一次只服务 1 个请求（成功率大幅提升）
_EXIT_EVAL_LOCK = threading.Lock()
_LAST_EXIT_EVAL = 0.0
_EXIT_MIN_INTERVAL = 12.0
# 进程级 MFE/MAE 跟踪：最大有利偏移(MFE)与最大不利偏移(MAE)，为 M2 Reflexion 捕捉率学习
# 与平仓复盘提供数据。★ 2026-08-06 审计修复：此前仅跟踪 MFE，MAE 完全缺失；且二者均
# 未回写 trades 表（盘中仅存进程内存，重启即丢）→ MFE回灌经验库空转、仪表盘 MFE 为假。
_EXIT_MFE: dict = {}
_EXIT_MAE: dict = {}

_ALLOWED_ACTIONS = {"hold", "partial_close", "full_close", "reverse_signal"}


def _track_mfe(account_id: str, ticket, current_profit: float) -> float:
    """跟踪每笔持仓历史最大有利偏移(MFE)，为捕捉率学习铺垫。
    ★ 2026-08-06 修复：原逻辑 `prev = _EXIT_MFE.get(key, current_profit)` 取默认=当前值，
       `current_profit > prev` 恒为假 → 键从未写入 → MFE 永远返回 0/默认，
       M2 反思与 mfe_promote 一直吃 0（MFE回灌空转的真正根因）。改为首次必存值。"""
    key = (account_id, str(ticket))
    prev = _EXIT_MFE.get(key)
    if prev is None or current_profit > prev:
        _EXIT_MFE[key] = current_profit
    return _EXIT_MFE.get(key, current_profit)


def _track_mae(account_id: str, ticket, current_profit: float) -> float:
    """跟踪每笔持仓历史最大不利偏移(MAE，= 最差的浮亏)：衡量入场质量/是否接飞刀。
    current_profit 取持仓期间的最小值即最大不利偏移。"""
    key = (account_id, str(ticket))
    prev = _EXIT_MAE.get(key)
    if prev is None or current_profit < prev:
        _EXIT_MAE[key] = current_profit
    return _EXIT_MAE.get(key, current_profit)


def _holding_minutes(pos: dict) -> int:
    t = pos.get("time")
    if not t:
        return 0
    try:
        return int((time.time() - float(t)) / 60)
    except Exception:
        return 0


# ★ M1 修复：AIExitAgent 按 account_id 进程级单例。
#   根因：trade_executor 每交易周期(60s) new 一个 TradeExecutor（含新 AIExitAgent），
#   上一轮后台评估写入的 self._cache（决策缓存）随旧实例被 GC 回收 →
#   下一轮新实例无缓存 → 永远“无新鲜缓存”→ 每轮重新后台评估、粘滞缓存永失效、
#   偶发空响应即整轮回退规则引擎。改为按 account_id 复用同一实例，决策缓存跨周期持久。
_EXIT_AGENTS: dict = {}
_EXIT_AGENTS_LOCK = threading.Lock()


def get_exit_agent(deepseek_client, account_id: str) -> "AIExitAgent":
    """按 account_id 返回持久化单例（首次创建，之后复用），保证决策缓存跨交易周期存活。"""
    key = account_id or "_default_"
    with _EXIT_AGENTS_LOCK:
        ag = _EXIT_AGENTS.get(key)
        if ag is None:
            ag = AIExitAgent(deepseek_client, account_id=key)
            _EXIT_AGENTS[key] = ag
        else:
            # 客户端随周期重建，刷新引用以确保用最新 KeyPool（DeepSeekClient 本身近乎无状态）
            ag.ds = deepseek_client
        return ag


def _build_payload(positions, atr, account_id: str) -> list:
    """把 MT5 持仓字典列表转换为喂给 LLM 的精简 payload"""
    payload = []
    for p in positions:
        pos_type = (p.get("type") or "").lower()
        open_price = float(p.get("price_open") or p.get("open_price") or 0)
        current_price = float(p.get("price_current") or p.get("current_price") or 0)
        if open_price > 0 and current_price > 0 and atr > 0:
            move_atr = ((current_price - open_price) / atr
                        if pos_type == "buy" else (open_price - current_price) / atr)
        else:
            move_atr = 0.0
        profit = float(p.get("profit", 0) or 0)
        mfe = _track_mfe(account_id, p.get("ticket"), profit)
        mae = _track_mae(account_id, p.get("ticket"), profit)
        payload.append({
            "ticket": str(p.get("ticket")),
            "type": pos_type,
            "open_price": round(open_price, 2),
            "current_price": round(current_price, 2),
            "sl": float(p.get("sl") or 0),
            "tp": float(p.get("tp") or 0),
            "volume": float(p.get("volume", 0) or 0),
            "profit": round(profit, 2),
            "mfe": round(mfe, 2),
            "mae": round(mae, 2),
            "move_atr": round(move_atr, 2),
            "holding_minutes": _holding_minutes(p),
        })
    return payload


def _validate(decision: dict, pos: dict, atr: float):
    """
    校验 AI 出场决策的安全性，拒绝会爆仓/乱来的输出。
    返回校验后的决策 dict，或 None（交由调用方回退规则引擎）。
    """
    action = (decision.get("action") or "hold").lower()
    if action not in _ALLOWED_ACTIONS:
        logger.debug(f"[AI出场] 拒绝未知 action={action} → 回退")
        return None

    pos_type = (pos.get("type") or "").lower()
    current_price = float(pos.get("current_price") or pos.get("price_current") or 0)

    close_pct = float(decision.get("close_pct", 0) or 0)
    if action == "partial_close":
        if not (0.05 <= close_pct <= 0.95):
            logger.debug(f"[AI出场] 拒绝非法 close_pct={close_pct} → 回退")
            return None
    else:
        close_pct = 0.0

    # 红线：new_sl 必须保留且置于市价"内侧"，严禁移除或错向
    new_sl = decision.get("new_sl")
    if new_sl is not None and new_sl not in (0, 0.0, "0", ""):
        new_sl = float(new_sl)
        if current_price <= 0:
            return None
        if pos_type == "buy" and not (0 < new_sl < current_price):
            logger.debug(f"[AI出场] 拒绝错向/失效 SL(new_sl={new_sl}≥当前价) → 回退")
            return None
        if pos_type == "sell" and not (current_price < new_sl):
            logger.debug(f"[AI出场] 拒绝错向/失效 SL(new_sl={new_sl}≤当前价) → 回退")
            return None
    else:
        new_sl = None

    reason = str(decision.get("reason", ""))[:200]
    return {
        "action": action,
        "close_pct": close_pct,
        "new_sl": new_sl,
        "reason": f"[AI出场] {reason}",
        "ai_driven": True,
    }


class AIExitAgent:
    """AI 出场决策 Agent：每周期对该账号全部持仓做批量 LLM 决策。

    ★ 关键设计（修复 100% 超时 + 主循环 120s 卡死 + 后台线程挂死）：
    - 出场 LLM 评估**异步解耦**于 60s 交易主循环，evaluate() 绝不阻塞下单/辩论。
    - evaluate() 立即返回：有新鲜缓存(≤CACHE_TTL)的持仓用 AI 决策；
      无缓存/过期的持仓本轮回退规则引擎，并触发一次后台 LLM 评估，
      结果写入缓存供下个周期使用（最多延迟 1 个周期生效）。
    - 后台 LLM 调用包 **Python 级看门狗超时**(ThreadPoolExecutor)：
      OpenAI SDK 的客户端超时在守护线程内可能不生效，故用 fut.result(timeout) 兜底，
      即使 SDK 挂死也能在 EXIT_LLM_TIMEOUT+8s 内放弃并解锁，绝不永久卡死。
    - 任何 LLM 超时/异常只影响缓存更新，不影响主循环（出场永不卡死）。
    - L2 反转防抖 / L3 篮子护盾仍在主循环每轮同步跑（硬护栏不受异步影响）。
    """

    def __init__(self, deepseek_client, account_id: str = ""):
        self.ds = deepseek_client
        self.account_id = account_id
        self._cache: dict = {}          # ticket -> (timestamp, decision, context)
        self._lock = threading.Lock()
        self._busy = False              # 后台评估进行中（带看门狗解锁）
        self._busy_since = 0.0
        self._reverse_streak: dict = {}  # ★ 2026-08-19 P0-2：ticket -> 本地8B reverse_signal 连续轮数（防抖）
        # ★ 2026-08-19 定稿P0-2：L2 出场后端解析。云端弃用后原绑 deepseek_client 持续
        #   error → L2 静默失效；关云时切本地 qwen3:8b（evaluate_exits_local 同构输出）。
        self._backend = "local"
        try:
            from app.services.cloud_switch import effective_cloud_enabled
            self._backend = "cloud" if effective_cloud_enabled() else "local"
        except Exception:
            self._backend = "local"
        try:
            from app.config import settings as _s
            if not bool(getattr(_s, "EXIT_LOCAL_BACKEND_ENABLED", True)):
                self._backend = "cloud"   # 强制回退云端路径（应急对照）
        except Exception:
            pass
        # ★ 2026-08-10：黄金 M15 波动快，缓存太久会导致"由赢转亏"时仍沿用旧决策。
        #   缩短 TTL 60s / 粘滞 90s；另加"利润变化>0.3×ATR"强制刷新。
        self._cache_ttl = 60.0          # 缓存有效期（秒）
        self._sticky_max = 90.0         # 粘滞沿用上限（秒）

    def evaluate(self, positions, atr, strategy, market_context,
                 ai_decision: str, ai_confidence: float) -> dict:
        """
        非阻塞：返回 {ticket: 决策dict}。
        无新鲜缓存的 ticket 不入 dict → 调用方回退 smart_exit 规则引擎（本轮回退，下轮生效）。
        """
        global _LAST_EXIT_EVAL   # 模块级速率限制时间戳，必须声明 global 才能赋值
        if not positions:
            return {}
        now = time.time()
        # 防卡死：后台任务卡超过 200s 强制解锁（看门狗150s之上留余量；
        # 正常评估≤120s，不会误触；仅防 SDK 线程内超时完全失效的极端永久挂死）。
        with self._lock:
            if self._busy and (now - self._busy_since) > 200:
                self._busy = False
        out: dict = {}
        need_refresh = []
        for p in positions:
            t = str(p.get("ticket"))
            cached = self._cache.get(t)
            # 计算当前持仓利润/延伸度，用于判断缓存是否严重过时
            pos_type = (p.get("type") or "").lower()
            open_price = float(p.get("price_open") or p.get("open_price") or 0)
            current_price = float(p.get("price_current") or p.get("current_price") or 0)
            profit = float(p.get("profit", 0) or 0)
            cached_profit = profit
            if cached and len(cached) > 2:
                cached_profit = cached[2].get("profit", profit)
            # 利润变化超过 0.3×ATR（至少 $2）时强制刷新，避免旧"让利润奔跑"决策害我们回吐
            profit_delta_threshold = max(2.0, 0.3 * atr)
            force_refresh = cached and abs(profit - cached_profit) > profit_delta_threshold

            if cached and not force_refresh and (now - cached[0]) < self._cache_ttl:
                valid = _validate(cached[1], p, atr)   # 每次用当前价重新校验红线
                if valid is not None:
                    out[t] = valid
                # 校验失败 → 本轮回退规则引擎（不入 out）
            elif cached and not force_refresh and (now - cached[0]) < self._sticky_max:
                # ★ M1 加固：缓存过期但在粘滞窗口内 → 沿用上一有效决策(置信衰减)，
                #   保持 AI 驱动出场，不再回退机械规则（应对 DeepSeek 空响应/并发抖动）。
                valid = _validate(cached[1], p, atr)
                if valid is not None:
                    stick = dict(valid)
                    stick["reason"] = "[AI出场-沿用缓存·置信衰减] " + str(valid.get("reason", ""))
                    out[t] = stick
                    logger.info(f"[AI出场] {self.account_id[:8]} ticket={t} 粘滞沿用缓存({(now-cached[0]):.0f}s前)，避免回退规则")
                # 粘滞校验失败 → 本轮回退规则引擎
            else:
                if force_refresh:
                    logger.info(f"[AI出场] {self.account_id[:8]} ticket={t} 利润变化{abs(profit-cached_profit):.2f}$>{profit_delta_threshold:.2f}$→强制刷新缓存")
                need_refresh.append(p)

        with self._lock:
            can_trigger = (not self._busy) and bool(need_refresh)
            if can_trigger:
                # 全局速率限制：4 账号错开，避免同分钟并发挤爆 DeepSeek 导致挂死
                with _EXIT_EVAL_LOCK:
                    if (now - _LAST_EXIT_EVAL) < _EXIT_MIN_INTERVAL:
                        can_trigger = False
                    else:
                        _LAST_EXIT_EVAL = now
                if can_trigger:
                    self._busy = True
                    self._busy_since = now
        if can_trigger:
            try:
                # 非守护线程：守护线程内 OpenAI SDK 的超时墙不触发（会无限挂死），
                # 非守护线程的超时机制可靠，看门狗能正常回收卡死的评估
                logger.info(f"[AI出场][诊断] {self.account_id[:8]} 启动后台评估线程(持仓{len(need_refresh)}笔, 距上次全局评估{now-_LAST_EXIT_EVAL:.0f}s)")
                threading.Thread(
                    target=self._bg_evaluate,
                    args=(need_refresh, atr, strategy, market_context, ai_decision, ai_confidence),
                    daemon=False).start()
            except Exception:
                with self._lock:
                    self._busy = False
        else:
            logger.info(f"[AI出场][诊断] {self.account_id[:8]} 本轮回退: 待评估{len(need_refresh)}笔 busy={self._busy}")

        if out:
            logger.info(f"[AI出场] {self.account_id[:8]} 用缓存AI决策 {len(out)}/{len(positions)} 笔"
                        + (f" (其余{len(need_refresh)}笔本轮回退规则引擎)" if need_refresh else ""))
        else:
            logger.info(f"[AI出场][诊断] {self.account_id[:8]} 无新鲜缓存→本轮回退规则引擎")
        return out

    def _bg_evaluate(self, positions, atr, strategy, market_context,
                     ai_decision: str, ai_confidence: float):
        """后台线程：调 LLM 评估，结果写入缓存（带 Python 级看门狗，不阻塞主循环）"""
        logger.info(f"[AI出场][诊断] {self.account_id[:8]} 后台线程开始, 持仓{len(positions)}笔")
        try:
            payload = _build_payload(positions, atr, self.account_id)
            raw = None
            try:
                # ★ 看门狗：即使 SDK 客户端超时在守护线程内失效，fut.result 也能可靠触发。
                #   出场评估是异步非阻塞操作（不卡主循环），故看门狗须 ≥ LLM 客户端超时(120s)，
                #   否则 4096 token 调用在辩论抢占 DeepSeek 信号量时偏慢会被提前掐断、浪费一个周期。
                #   放宽到 150s：给足 LLM 完成时间，确保首轮即缓存成功（实测 29s 常态、极端≤120s）。
                _WATCHDOG = 150.0
                with ThreadPoolExecutor(max_workers=1) as ex:
                    if self._backend == "local":
                        # ★ 2026-08-19 定稿P0-2：云端弃用→本地 qwen3:8b 出场评估（同构输出）
                        from app.services.local_llm_service import get_local_llm
                        fut = ex.submit(get_local_llm().evaluate_exits_local, payload, market_context)
                    else:
                        fut = ex.submit(self.ds.evaluate_exits, payload, market_context, EXIT_LLM_TIMEOUT)
                    raw = fut.result(timeout=_WATCHDOG)
            except FuturesTimeout:
                logger.warning(f"[AI出场] {self.account_id[:8]} 后台评估看门狗超时"
                               f"({_WATCHDOG:.0f}s)→下轮重试")
                raw = None
            except Exception as e:
                logger.warning(f"[AI出场] {self.account_id[:8]} 后台评估异常: {e}")
                raw = None

            if raw and not raw.get("error") and raw.get("decisions"):
                by_ticket = {str(p.get("ticket")): p for p in positions}
                stored = 0
                for d in raw["decisions"]:
                    t = str(d.get("ticket", ""))
                    pos = by_ticket.get(t)
                    if pos is None:
                        continue
                    valid = _validate(d, pos, atr)
                    if valid is not None:
                        # ★ 2026-08-19 定稿P0-2：本地 8B reverse_signal 需置信≥0.60 且连续 N 轮
                        #   同向确认才升级为 full_close（防抖，防单轮误判砍仓）。未达轮数本轮缓存 hold。
                        if valid["action"] == "reverse_signal" and self._backend == "local":
                            _req = 2
                            try:
                                from app.config import settings as _s
                                _req = int(getattr(_s, "EXIT_REVERSE_STREAK_REQUIRED", 2) or 2)
                            except Exception:
                                _req = 2
                            _conf = float(d.get("confidence", 0) or 0)
                            if _conf >= 0.60:
                                n = int(self._reverse_streak.get(t, 0)) + 1
                                if n >= _req:
                                    valid["action"] = "full_close"
                                    valid["reason"] = f"{valid['reason']} [本地8B反向{_req}轮确认→全平]"
                                    self._reverse_streak[t] = 0
                                    logger.info(f"[AI出场] {self.account_id[:8]} ticket={t} 本地8B反向{_req}轮确认→full_close")
                                else:
                                    self._reverse_streak[t] = n
                                    valid["action"] = "hold"
                                    valid["reason"] = f"{valid['reason']} [反向第{n}轮, 需{_req}轮连续]"
                            else:
                                self._reverse_streak.pop(t, None)
                                valid["action"] = "hold"
                                valid["reason"] = f"{valid['reason']} [反向置信{_conf:.2f}<0.60 不计数]"
                        elif valid["action"] != "hold":
                            self._reverse_streak.pop(t, None)
                        # 缓存时附带利润/延伸度快照，供后续利润变化强制刷新判断
                        pos_type = (pos.get("type") or "").lower()
                        open_price = float(pos.get("price_open") or pos.get("open_price") or 0)
                        current_price = float(pos.get("price_current") or pos.get("current_price") or 0)
                        _profit = float(pos.get("profit", 0) or 0)
                        if open_price > 0 and current_price > 0 and atr > 0:
                            _move = (current_price - open_price) if pos_type == "buy" else (open_price - current_price)
                            _move_atr = _move / max(atr, 0.01)
                        else:
                            _move_atr = 0.0
                        self._cache[t] = (time.time(), valid, {"profit": _profit, "move_atr": round(_move_atr, 2)})
                        stored += 1
                logger.info(f"[AI出场] {self.account_id[:8]} 后台评估完成，缓存 {stored}/{len(positions)} 笔")
                if stored == 0:
                    logger.warning(f"[AI出场][诊断] {self.account_id[:8]} LLM返回{len(raw.get('decisions',[]))}条但全部校验失败(检查schema/红线)")
            else:
                logger.warning(f"[AI出场][诊断] {self.account_id[:8]} 后台评估无有效决策"
                               f"(error={raw.get('error') if raw else 'none'}, decisions={len(raw.get('decisions',[])) if raw else 0})")
        finally:
            with self._lock:
                self._busy = False
            logger.info(f"[AI出场][诊断] {self.account_id[:8]} 后台线程结束, busy=False")
