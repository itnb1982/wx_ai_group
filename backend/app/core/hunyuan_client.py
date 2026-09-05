"""
XAU/USD万象Ai自动量化交易系统 — 腾讯混元 Hy3 API 客户端
接口: TokenHub 平台 (OpenAI 兼容)
模型: hy3 — 256k上下文，擅长金融建模与推理
支持 KeyPool 多 Key 轮询 + token 用量统计
"""
import json
import re
import threading
from typing import Optional
from openai import OpenAI
from loguru import logger
from app.config import settings
from app.services.key_pool import KeyPool, KeyPoolItem


# 全局并发信号量：与 DeepSeek 同理，限制同时在途调用数，规避高并发空响应。
_HY_SEM = threading.Semaphore(3)  # 与 DeepSeek 同档并发上限；混元空响应问题较轻


def _fmt_recent_trades(market_data: dict) -> str:
    """把最近真实成交格式化为可读文本，注入决策提示词，让 AI 从自己盈亏里学（治越跑越笨）。"""
    _rows = (market_data or {}).get("recent_closed_trades") or []
    if not _rows:
        return "（暂无历史成交记录）"
    _lines = []
    for t in _rows:
        _win = "盈" if float(t.get("pnl", 0) or 0) >= 0 else "亏"
        _lines.append(
            f"  - #{t.get('ticket')} {t.get('dir')} 开{t.get('open')} 平{t.get('close')} "
            f"{_win}{t.get('pnl')}$ 出场={t.get('reason')} @ {t.get('when')}"
        )
    return "\n".join(_lines)


def _fmt_my_positions(market_data: dict) -> str:
    """把当前真实持仓格式化为可读文本，注入开仓决策提示词（根治 AI 失明）。
    ★ 2026-08-06 增强：前置「持仓篮总览」，让 AI 一眼看到净暴露与总浮亏，
    避免逐笔罗列时看不出重仓方向（用户反馈：AI 不看持仓→逆势狂开亏损单）。
    """
    _pos = (market_data or {}).get("my_open_positions") or []
    if not _pos:
        return "（无持仓，当前空仓）"
    buy_lots = sum(float(p.get("volume", 0) or 0) for p in _pos
                   if str(p.get("direction", "")).upper().startswith("BUY"))
    sell_lots = sum(float(p.get("volume", 0) or 0) for p in _pos
                    if str(p.get("direction", "")).upper().startswith("SELL"))
    net = buy_lots - sell_lots
    total_pnl = sum(float(p.get("floating_pnl", 0) or 0) for p in _pos)
    _net_desc = (f"BUY多{abs(net):.2f}手" if net > 0
                 else (f"SELL多{abs(net):.2f}手" if net < 0 else "中性"))
    _summary = (
        f"【持仓篮总览】笔数={len(_pos)} | 总BUY={buy_lots:.2f}手 总SELL={sell_lots:.2f}手 "
        f"| 净暴露={_net_desc} | 浮动总盈亏={total_pnl:+.2f}$"
    )
    _lines = [_summary]
    for p in _pos:
        _age = p.get("age_min", -1)
        _age_s = f"{_age}分钟" if _age >= 0 else "未知"
        _lines.append(
            f"  - {p.get('direction')} #{p.get('ticket')} {p.get('volume')}手 "
            f"开{p.get('entry')} 现{p.get('current')} 浮{p.get('floating_pnl')}$ "
            f"持仓{_age_s} SL={p.get('sl')} TP={p.get('tp')}"
        )
    return "\n".join(_lines)


def _safe_json_loads(content: str, fallback_decision: str = "HOLD") -> dict:
    """
    容错 JSON 解析：去掉 <think> 块 / markdown 围栏 / 抽取第一个平衡 {...}。
    解析成功但缺 reasoning 时，用 decision+confidence+key_factors 自动补全可读摘要；
    失败时把原文/错误原因作为 reasoning 兜底，永不返回无意义占位符。
    """

    def _polish(data: dict) -> Optional[dict]:
        """补全/规范化字段，确保 reasoning 永远有可读内容。"""
        if not isinstance(data, dict):
            return None
        decision = str(data.get("decision") or fallback_decision).upper()
        if decision not in ("BUY", "SELL", "HOLD"):
            decision = fallback_decision
        confidence = float(data.get("confidence") or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        reasoning = (data.get("reasoning") or "").strip()
        # 反驳轮模型返回的是 revised_reasoning，统一映射到 reasoning 以便下游展示
        if not reasoning:
            reasoning = (data.get("revised_reasoning") or "").strip()
        key_factors = data.get("key_factors") or []
        if not reasoning:
            kf_txt = "；".join(key_factors) if isinstance(key_factors, list) and key_factors else "未给出"
            reasoning = f"模型给出 {decision}（置信{confidence:.0%}）但未提供文字推理；关键因子：{kf_txt}"
            data["_missing_reasoning"] = True
        data["decision"] = decision
        data["confidence"] = confidence
        data["reasoning"] = reasoning
        return data

    if not content or not content.strip():
        return {"decision": fallback_decision, "confidence": 0.0,
                "reasoning": f"模型返回空内容，未产生推理（fallback={fallback_decision}）",
                "_parse_error": True}
    try:
        data = json.loads(content)
        polished = _polish(data)
        if polished:
            return polished
    except Exception:
        pass
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if cleaned and cleaned != content:
        try:
            data = json.loads(cleaned)
            polished = _polish(data)
            if polished:
                return polished
        except Exception:
            pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1))
            polished = _polish(data)
            if polished:
                return polished
        except Exception:
            pass
    depth, start = 0, None
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if start is None:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                snippet = cleaned[start:i + 1]
                try:
                    data = json.loads(snippet)
                    polished = _polish(data)
                    if polished:
                        return polished
                except Exception:
                    start, depth = None, 0
                    continue
    snippet = (content or "").strip()[:240]
    return {"decision": fallback_decision, "confidence": 0.0,
            "reasoning": snippet or f"模型输出无法解析为 JSON（fallback={fallback_decision}）",
            "_parse_error": True}


def _extract_usage(response, pool: Optional[KeyPool], key_id: Optional[str]):
    """从 OpenAI 兼容响应中提取 usage，写回 pool 内存统计"""
    if pool is None or key_id is None:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    try:
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        if pt == 0 and ct == 0:
            return
        pool.record(key_id, pt, ct)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[混元Hy3] usage 提取失败: {e}")


class HunyuanClient:
    """腾讯混元 Hy3 客户端 — 支持 KeyPool 多 Key 调度 + token 统计"""

    def __init__(self, api_key: Optional[str] = None, pool: Optional[KeyPool] = None):
        self.pool = pool
        self._fallback_key = api_key or settings.HUNYUAN_API_KEY
        self._current_key_id: Optional[str] = None
        self.client: Optional[OpenAI] = None
        self.model = settings.HUNYUAN_MODEL
        # 单 key fallback
        if self.pool is None and self._fallback_key:
            self.client = OpenAI(
                api_key=self._fallback_key,
                base_url=settings.HUNYUAN_BASE_URL,
            )
            self._current_key_id = "_fallback"

    def _resolve_client(self) -> tuple[Optional[OpenAI], Optional[str]]:
        if self.pool and not self.pool.is_empty():
            item: KeyPoolItem = self.pool.pick()
            if item is not None:
                client = OpenAI(
                    api_key=item.api_key,
                    base_url=settings.HUNYUAN_BASE_URL,
                )
                return client, item.key_id
        if self._fallback_key:
            if self.client is None:
                self.client = OpenAI(
                    api_key=self._fallback_key,
                    base_url=settings.HUNYUAN_BASE_URL,
                )
            return self.client, "_fallback"
        return None, None

    # ─────────────── 401 自动下线 ───────────────
    @staticmethod
    def _is_auth_error(e: Exception) -> bool:
        """判断是否为 401 认证失败（Key 失效/无效）"""
        s = str(e)
        return (
            "401" in s
            or "Authentication" in s
            or ("invalid" in s.lower() and "key" in s.lower())
        )

    def _deactivate_key(self, key_id: str, error_msg: str):
        """401 时从 pool 移除该 Key + 同步标记 DB is_valid=0, is_active=0"""
        if not key_id or key_id == "_fallback" or not self.pool:
            return
        logger.warning(f"[混元Hy3] Key {key_id} 认证失败，自动下线: {error_msg[:120]}")
        self.pool.deactivate(key_id)
        try:
            from app.database import SessionLocal
            from app.models.api_key import APIKey
            db = SessionLocal()
            try:
                db_id = key_id.split(":", 1)[-1] if ":" in key_id else key_id
                rec = db.query(APIKey).filter(APIKey.id == db_id).first()
                if rec:
                    rec.is_valid = False
                    rec.is_active = False
                    db.commit()
                    logger.info(f"[混元Hy3] DB Key {db_id} 已标记 is_valid=0, is_active=0")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[混元Hy3] DB 下线标记失败: {e}")

    def _call(self, messages: list, temperature: float = 0.3, max_tokens: int = 2048,
              response_format: Optional[dict] = None) -> str:
        """同步调用混元 API，返回 text。401 时自动下线 Key + 重试一次。"""
        for attempt in range(2):  # 最多 2 次（首次 + 1 次重试）
            client, key_id = self._resolve_client()
            if client is None:
                return ""
            kwargs = dict(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if response_format:
                kwargs["response_format"] = response_format
            kwargs.setdefault("timeout", 90)   # ★ 显式超时：慢API快速失败，避免挂起占用信号量槽
            try:
                with _HY_SEM:
                    response = client.chat.completions.create(**kwargs)
                _extract_usage(response, self.pool, key_id)
                self._last_key_id = key_id
                content = response.choices[0].message.content if response.choices else ""
                if content and content.strip():
                    return content
                # 空内容(并发限流导致) → 重试一次
                logger.warning(f"[混元Hy3] 空内容返回，重试(attempt {attempt})")
                continue
            except Exception as e:
                if self._is_auth_error(e) and key_id and key_id != "_fallback" and attempt == 0:
                    self._deactivate_key(key_id, str(e))
                    logger.info(f"[混元Hy3] 401 后重试 (pool 剩余 {self.pool.size() if self.pool else 0})")
                    continue  # 重试（pool 变空 → 回退 .env）
                raise  # 非认证错误或重试后仍失败，向上抛出
        return ""

    async def _call_async(self, messages: list, temperature: float = 0.3, max_tokens: int = 2048,
                          response_format: Optional[dict] = None) -> str:
        """异步调用混元 API"""
        import asyncio
        client, key_id = self._resolve_client()
        if client is None:
            return ""
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_format:
            kwargs["response_format"] = response_format
        kwargs.setdefault("timeout", 90)   # ★ 显式超时

        loop = asyncio.get_running_loop()

        def _do():
            with _HY_SEM:
                return client.chat.completions.create(**kwargs)

        response = await loop.run_in_executor(None, _do)
        _extract_usage(response, self.pool, key_id)
        return response.choices[0].message.content

    def analyze(self, market_data: dict) -> dict:
        """
        金融建模分析 — 混元 Hy3 专长
        擅长: 波动率建模、风险量化、事件驱动、低幻觉
        返回: {decision, confidence, reasoning, risk_assessment}
        """
        from app.services.news_service import format_prompt_block as _fmt_news_block
        system_prompt = """你是世界顶级黄金(XAUUSD)量化分析师，专精金融建模、机构订单流(SMC/ICT)解读与风险管理。

你会看到与另一位技术分析师完全对称的市场数据，包括：多周期价格结构、机构订单流特征(SMC)、市场体制(Regime)、反转哨兵信号、本地进化洞察。你必须独立地基于这些证据做出交易决策。

返回严格的JSON格式：
{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "金融建模分析推理（中文，不超过200字）",
    "entry_price": 建议入场价(数字,nullable；若你认为应等反弹/回踩到某个更好价位再动手，填该价位，例如想等反弹到4325再空就填4325；否则填当前价或null),
    "risk_assessment": {
        "volatility_regime": "低波动" | "正常" | "高波动" | "极端",
        "risk_score": 0-10,
        "position_sizing_suggestion": "建议仓位比例（如0.5%）",
        "black_swan_probability": 0.0-1.0
    },
    "key_factors": ["因素1", "因素2", "因素3"],
    "position_action": {"action": "hold" | "trim" | "close_all", "confidence": 0.0-1.0, "reason": "简短理由"}
}

【持仓管理指令·核心任务】(2026-08-17 篮子级 AI 持仓管理)
你已看到「持仓篮总览 + 逐笔持仓」(my_open_positions)。有持仓时，管理好持仓与找机会同等重要：
- 持仓合计浮盈可观但行情开始不利（动能衰竭/结构破位/浮盈从峰值明显回吐）→ close_all 或 trim，主动锁利，绝不坐等回吐到亏损；
- 持仓方向仍被结构/体制支持且浮盈健康 → hold，让利润奔跑；
- 行情反转结构明确 → close_all，不恋战；
- 没有持仓或判断不清 → position_action.action = "hold"。
position_action 是对【全部当前持仓】的篮子级建议：trim=每笔减仓一半，close_all=全平。缺失/非法解析一律按 hold 处理。

分析原则：
1. 重点关注波动率状态、尾部风险与机构订单流结构的共振/背离
2. 量化风险回报比，不追涨杀跌
3. 考虑宏观经济事件对黄金的影响
4. 低幻觉：确实无方向感（置信度<0.3）才说HOLD，其余正常表达判断
5. 置信度<0.3时必须返回HOLD
6. BUY 与 SELL 完全对称：不得因历史盈亏统计而系统性地偏向任何方向
7. 你会同时收到「原始价格结构(最近N根K线序列)」与「跨资产/宏观环境(DXY/VIX/相关性)」，须作为独立证据自行判读
8. 当机构订单流结构(SMC: Order Block / FVG / BOS-CHoCH)与市场体制(Regime)共振给出明确方向时，必须输出 BUY 或 SELL，不要默认 HOLD；但【价格行为硬约束】优先：当 M5/M15 连续 3 根以上 K 线同向创近期新高/新低，且当前无明确反向流动性扫荡或 CHoCH 结构确认时，必须优先跟随短周期实际动量方向（创新低→SELL，创新高→BUY），禁止仅因长周期 FVG/OB 未测试就逆势抄底/摸顶。HOLD 仅用于"完全无结构信号、多空证据对等"的真混沌行情。"""

        user_prompt = f"""当前XAUUSD市场数据:

多时间框架: {json.dumps(market_data.get('timeframes', {}), indent=2, ensure_ascii=False)}
波动率指标: {json.dumps(market_data.get('volatility_metrics', {}), indent=2, ensure_ascii=False)}
关键价位: {json.dumps(market_data.get('key_levels', {}), indent=2, ensure_ascii=False)}
当前点差: {market_data.get('spread', 'N/A')}
当前价格: {market_data.get('current_price', 'N/A')}

【原始价格结构（最近N根K线实体/影线/连续同向/摆动高低点趋势，AI 自行读证据）】:
{json.dumps(market_data.get('price_structure', {}), ensure_ascii=False, default=str)}

【跨资产 / 宏观环境（DXY / VIX / DXY-XAU 相关性；外部数据已抓取但此前未进 AI，本次补强）】:
{json.dumps(market_data.get('external', {}), ensure_ascii=False, default=str)}

{_fmt_news_block(market_data)}

【订单流 / CVD（买盘是否枯竭、卖压是否放大；Binance永续+MT5本地代理双源，2026-08-06 补强②）】:
{json.dumps(market_data.get('orderflow', {}), ensure_ascii=False, default=str)}

【执行质量滑点（经纪商实际成交滑点，警惕 B-book 滑点剥削；2026-08-06 补强⑥）】:
{json.dumps(market_data.get('execution', {}), ensure_ascii=False, default=str)}

【机构订单流结构 SMC（决策骨架，与技术分析师输入对称）】:
{json.dumps(market_data.get('smc_features', {}), ensure_ascii=False, default=str)}

【市场体制 Regime（趋势/震荡/末端风险，最高优先级参考）】:
{json.dumps(market_data.get('regime', {}), ensure_ascii=False, default=str)}

【反转哨兵警示（趋势末端反转制衡）】:
{json.dumps(market_data.get('reversal_sentinel', {}), ensure_ascii=False, default=str)}

【本地进化洞察（基于本系统真实盈亏的在线学习，数据驱动软参考）】:
{json.dumps(market_data.get('evolution_advice', []), ensure_ascii=False, default=str)}

【本地 Meta 质量陪审团（Chronos 时序模型 + SMC/Regime 融合，独立制衡你的方向判断；详见系统提示第9条强制解释规则）】:
{json.dumps(market_data.get('meta_quality', {}), ensure_ascii=False, default=str)}

【我方当前真实持仓（来自MT5，决策前必须参考，避免重复同向下单或逆势加仓）】:
{_fmt_my_positions(market_data)}

【我方最近真实成交复盘（来自MT5实盘，AI必须从自己的盈亏中学习，避免重复犯同方向/同形态的错误）】:
{_fmt_recent_trades(market_data)}

请给出你的金融建模分析和风险评估。若我已持有同方向仓位且浮盈，勿盲目加仓；若已持有反方向仓位且被套，需明确判断是否该止损/反手。"""

        # 注入历史经验教训（从 debate_engine 经 memory_bank 加载）
        _lessons = market_data.get("empirical_lessons")
        if _lessons:
            user_prompt += f"""

【历史交易统计参考（仅用于仓位/风控校准，不得用于方向偏置）】:
{json.dumps(_lessons, indent=2, ensure_ascii=False, default=str)}

以上统计仅作风险参考，不得因"某方向历史上亏损多"就系统性地回避该方向；
历史教训只影响仓位大小、止损松紧与出场节奏，不影响 BUY/SELL/HOLD 的方向选择。"""

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            content = self._call(messages, temperature=settings.AI_TEMPERATURE, max_tokens=settings.AI_MAX_TOKENS_ANALYSIS)

            # ★ 2026-08-15 审计P1修复：空响应必须视同失败（与 DeepSeek 行为对齐）。
            #   原实现空串进 _safe_json_loads("") → HOLD/conf=0 且无 _api_failed →
            #   debate_engine 判 hy_api_failed=False 并上报健康 True → 双脑名义单脑运行、永不降级。
            if not content or not str(content).strip():
                logger.warning("[混元Hy3] 空内容响应 → 标记 API 失败（与DS对齐，避免被当合法HOLD且永不降级）")
                return {
                    "decision": "HOLD",
                    "confidence": 0.0,
                    "reasoning": "API错误: 空内容响应（并发限流/服务异常）",
                    "risk_assessment": {"risk_score": 5, "black_swan_probability": 0.3},
                    "_api_failed": True,
                }

            try:
                analysis = _safe_json_loads(content)
            except Exception as e:
                logger.error(f"[混元Hy3] 解析失败: {e}")
                analysis = {"decision": "HOLD", "confidence": 0.0,
                            "reasoning": f"API错误: {str(e)[:160]}",
                            "risk_assessment": {"risk_score": 10, "black_swan_probability": 1.0}}
            logger.info(f"[混元Hy3] 决策: {analysis.get('decision')} 置信度: {analysis.get('confidence')} 风险评分: {analysis.get('risk_assessment', {}).get('risk_score')}")
            return analysis
        except Exception as e:
            logger.error(f"[混元Hy3] 分析失败: {e}")
            return {
                "decision": "HOLD",
                "confidence": 0.0,
                "reasoning": f"API错误: {str(e)[:160]}",
                "risk_assessment": {"risk_score": 5, "black_swan_probability": 0.3},
                "_api_failed": True,
            }

    def debate_rebuttal(self, opponent_analysis: dict, my_analysis: dict, market_data: dict) -> dict:
        """
        辩论反驳 — 从金融建模视角评估技术分析
        """
        from app.services.news_service import format_prompt_block as _fmt_news_block
        system_prompt = """你是世界顶级量化分析师。你看到了技术分析师的判断，并拥有与其对称的机构订单流(SMC)、市场体制(Regime)、反转哨兵、进化洞察数据。

从金融建模与机构结构视角评估：技术信号是否被波动率/风险特征/订单流结构支撑？

返回严格JSON:
{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "agree_with_opponent": true | false,
    "rebuttal_points": ["反驳点1", "反驳点2"],
    "revised_reasoning": "修正后的推理（中文，不超过150字）",
    "risk_adjustment": "对技术信号的量化风险修正"
}
原则：
1. 技术信号好但风险极高→降置信度
2. 技术信号弱但风险极低→可适度跟进
3. 不为了辩论而反对，客观量化
4. BUY 与 SELL 完全对称：不得因历史盈亏统计而预设任何方向偏好
5. 【强制反调·魔鬼代言人（2026-08-13 强化牛熊对抗辩论）】你必须在反驳中至少列出 1 条与你最终方向相反的、最强的反向风险（为何这可能是顺势陷阱 / 假突破 / 趋势末端接飞刀 / 被新闻舆情反向打脸）。若你维持原方向，必须明确逐条驳倒该反向风险；若证据已变弱，应敢于降为 HOLD 甚至翻转。不要被对方或你自己的初始判断锚定，须重新独立评估。"""

        user_prompt = f"""我的初始判断: {json.dumps(my_analysis, ensure_ascii=False)}

技术分析师的判断: {json.dumps(opponent_analysis, ensure_ascii=False)}

波动率数据: {json.dumps(market_data.get('volatility_metrics', {}), indent=2, ensure_ascii=False)}

【原始价格结构（最近N根K线序列）】:
{json.dumps(market_data.get('price_structure', {}), ensure_ascii=False, default=str)}

【跨资产 / 宏观环境（DXY / VIX / 相关性）】:
{json.dumps(market_data.get('external', {}), ensure_ascii=False, default=str)}

【订单流 / CVD（买盘是否枯竭、卖压是否放大；Binance永续+MT5本地代理双源，2026-08-06 补强②）】:
{json.dumps(market_data.get('orderflow', {}), ensure_ascii=False, default=str)}

【执行质量滑点（经纪商实际成交滑点，警惕 B-book 滑点剥削；2026-08-06 补强⑥）】:
{json.dumps(market_data.get('execution', {}), ensure_ascii=False, default=str)}

【机构订单流结构 SMC（与初始判断输入对称）】:
{json.dumps(market_data.get('smc_features', {}), ensure_ascii=False, default=str)}

【市场体制 Regime】:
{json.dumps(market_data.get('regime', {}), ensure_ascii=False, default=str)}

【反转哨兵警示】:
{json.dumps(market_data.get('reversal_sentinel', {}), ensure_ascii=False, default=str)}

【本地进化洞察】:
{json.dumps(market_data.get('evolution_advice', []), ensure_ascii=False, default=str)}

【我方当前真实持仓（来自MT5，辩论时仍须参考，避免重复同向下单或逆势加仓）】:
{_fmt_my_positions(market_data)}

【我方最近真实成交复盘（来自实盘，必须参考，避免重复犯错）】:
{_fmt_recent_trades(market_data)}

{_fmt_news_block(market_data)}

请给出你的量化风险评估。"""

        # 反驳阶段也注入历史教训（仅用于风控/仓位校准，不得用于方向偏置）
        _lessons = market_data.get("empirical_lessons")
        if _lessons:
            user_prompt += f"""

【实盘统计参考（仅用于风控/仓位校准，不得用于方向偏置）】:
{json.dumps(_lessons, indent=2, ensure_ascii=False, default=str)}

以上统计仅作风险参考，不得因"某方向历史上亏损多"就拒绝在该方向出现明确结构信号时翻转。"""

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            content = self._call(messages, temperature=0.4, max_tokens=settings.AI_MAX_TOKENS_DEBATE)
            # ★ 2026-08-15 审计P1修复：辩论轮空响应同样标记失败，不静默沿用初始判断
            if not content or not str(content).strip():
                logger.warning("[混元Hy3] 辩论轮空内容响应 → 标记 API 失败（与DS对齐）")
                return {
                    "decision": "HOLD",
                    "confidence": float(my_analysis.get("confidence") or 0.0),
                    "agree_with_opponent": False,
                    "revised_reasoning": "（辩论轮空内容响应）",
                    "risk_adjustment": "",
                    "_api_failed": True,
                }
            try:
                result = _safe_json_loads(content,
                                        fallback_decision=my_analysis.get("decision") or "HOLD")
                # ★ 修复：safe_json_loads fallback 给 confidence=0.0，但应该用初始置信度
                if result.get("confidence", 0) <= 0 and my_analysis.get("confidence", 0) > 0:
                    result["confidence"] = my_analysis["confidence"]
                return result
            except Exception as e:
                logger.error(f"[混元Hy3] 辩论解析失败: {e}")
                return {"decision": my_analysis.get("decision"),
                        "confidence": my_analysis.get("confidence", 0.5),
                        "agree_with_opponent": False,
                        "revised_reasoning": f"（辩论轮解析失败）{str(e)[:120]}"}
        except Exception as e:
            logger.error(f"[混元Hy3] 辩论失败: {e}")
            return {
                "decision": my_analysis.get("decision"),
                "confidence": my_analysis.get("confidence", 0.5),
                "agree_with_opponent": False,
            }