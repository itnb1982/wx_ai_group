"""
XAU/USD万象Ai自动量化交易系统 — DeepSeek V4 API 客户端
支持 Non-Think / Think High / Think Max 三级推理
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


# 全局并发信号量：deepseek-v4-pro 推理模型在高并发(同发>~10)下返回空 content(empty_response)，
# 限制同时的在途调用数即可彻底消除（实测 信号量=3 → 12 路并发 100% 成功；取 4 留余量）。
_DS_SEM = threading.Semaphore(3)  # 主号辩论(1并发)+出场后台(1并发)峰值=2；留 1 槽余量防慢API占满级联；并发空响应由重试+粘滞缓存兜底


# =====================================================================
# AI 出场决策官 系统提示词（M1：替代 smart_exit 写死规则引擎）
# 设计铁律：AI 只输出"出场意图"，机械层执行；严禁移除 SL。
# =====================================================================
EXIT_DECISION_SYSTEM_PROMPT = """你是世界顶级黄金(XAUUSD)交易系统的「AI出场决策官」。系统已用AI开仓，现在由你决定每笔持仓如何出场（平仓/移动止损/反手）。你的目标：让利润奔跑、在结构转弱时锁定利润、在结构证伪时果断离场——而不是机械地一冒头就保本、把盈利切碎。

你可以对每笔持仓输出以下意图之一：
- "hold"：继续持有，不做任何操作
- "partial_close"：平掉一部分(close_pct 0.05~0.95)，保留剩余仓位让利润继续奔跑
- "full_close"：全部平仓离场
- "reverse_signal"：结构已明确反转，建议反手（需高置信；系统会做连续确认防抖，不会立即执行）

移动止损：用 action="hold" 并给出 new_sl（保护性止损价）。new_sl 必须位于市价"内侧"：
- 多单(buy)：new_sl 必须 < 当前价 且 > 0（只能下移保本/追踪，绝不能设为0或≥当前价，否则等于删除止损或立即止损）
- 空单(sell)：new_sl 必须 > 当前价（只能上移）
严禁移除止损（不要输出 new_sl=0）。

返回严格JSON：
{
  "decisions": [
    {
      "ticket": "持仓单号(字符串)",
      "action": "hold" | "partial_close" | "full_close" | "reverse_signal",
      "close_pct": 0.0,            // 仅 partial_close 用，0.05~0.95
      "new_sl": null,              // 仅 hold 用（移动止损），其余填 null
      "reason": "简短中文理由(≤80字)：基于趋势/动量/结构/盈亏比"
    }
  ]
}

决策原则（AI原生，不要写死阈值）：
1. 盈利单：只要趋势/动量仍对其有利，就让利润奔跑；可用 trailing(new_sl) 锁定部分利润，但不要轻易 full_close 或大幅 partial_close 把盈利切碎。
   ★ 硬性红线：绝不要仅仅因为"当前 AI 信号与开仓方向相反"就对【盈利单】(profit>0) 做 full_close 或 reverse_signal——那只是噪音鞭锯，会把赢家切成亏损。盈利单只允许：① 达到 TP / 追踪止损被扫；② 结构三重确认反转(mfe 大幅回吐 + 关键位破位 + regime 切换同时成立)才考虑离场，且优先用 trailing(new_sl) 锁定利润而非直接全平。
2. 只有当结构明确转弱（关键支撑/阻力被反向突破、动量背离、regime 切换、开仓逻辑被证伪）才考虑平仓或反手。
3. 亏损单：若开仓逻辑已失效且未触发经纪商SL，应果断 full_close；但不要因短期噪音恐慌平仓。
4. 利用"最大有利偏移(mfe)"判断：若价格曾大幅有利(mfe很大)后回落，应把止损上移锁定利润，而非回吐。
5. 方向偏见自检：BUY 与 SELL 完全对称。多单出现明确看跌反转结构应优先考虑 reverse_signal；空单出现明确看涨反转结构也应优先考虑 reverse_signal。不要死扛已被证伪的方向。
6. 价格延伸度自检（防"高位接飞刀/低位接刀"）：market_context 中的 price_extension_z 表示当前价距布林中轨的标准化偏离（正=已延伸过高/高位区，负=已超卖/低位区）。若持仓处显著正延伸且趋势动能转弱：盈利单优先用 trailing(new_sl) 或 partial_close 锁定利润，绝不追高加仓；亏损单警惕反转、结构证伪即果断 full_close。处显著负延伸且出现止跌结构：亏损空单不宜恐慌杀跌，警惕逼空反转。
7. 不确定时返回 hold（宁可观望，不要乱动）。
8. 额外上下文（已随 market_context 提供，务必综合参考）：reversal_sentinel(反转哨兵，显著顶/底背离时警惕趋势末端反转)、meta_quality(本地时序质量陪审团：HIGH=让利润奔跑/MID=正常/LOW=啃头皮快出)、evolution_advice(演化出的出场哲学)、recent_closed_trades(你近期真实盈亏复盘)、portfolio_state(全局仓位快照)。不要只看单笔价格，要结合这些信号判断是否让利润奔跑还是锁利。"""


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


def _safe_json_loads(content: str, fallback_decision: str = "HOLD") -> dict:
    """
    容错 JSON 解析：兼容思考模型带 <think> 块、markdown ```json 围栏、
    流式残留、注释等非标准 JSON 输出。
    解析成功但缺 reasoning 时，用 decision+confidence+key_factors 自动补全可读摘要；
    失败时把原始文本/错误原因作为 reasoning 兜底，避免抛出技术错误给用户。
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

    # 1) 直解析
    try:
        data = json.loads(content)
        polished = _polish(data)
        if polished:
            return polished
    except Exception:
        pass

    # 2) 去掉 <think>...</think> 思考块（部分模型会拼接到 content 前面）
    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if cleaned and cleaned != content:
        try:
            data = json.loads(cleaned)
            polished = _polish(data)
            if polished:
                return polished
        except Exception:
            pass

    # 3) 抽取 markdown ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1))
            polished = _polish(data)
            if polished:
                return polished
        except Exception:
            pass

    # 4) 找第一个平衡的 {...} 块
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

    # 5) 全部失败 → 兜底：把原始内容作为 reasoning 文本，前 240 字
    snippet = (content or "").strip()[:240]
    return {"decision": fallback_decision, "confidence": 0.0,
            "reasoning": snippet or f"模型输出无法解析为 JSON（fallback={fallback_decision}）",
            "_parse_error": True}


def _extract_usage(response, pool: Optional[KeyPool], key_id: Optional[str]):
    """
    从 OpenAI 兼容响应中提取 usage，并写回 pool 内存统计。
    兼容字段：prompt_tokens / completion_tokens / total_tokens（OpenAI / DeepSeek 标准字段）。
    """
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
        logger.debug(f"[DeepSeek] usage 提取失败: {e}")


class DeepSeekClient:
    """DeepSeek V4 客户端 — 支持 KeyPool 多 Key 调度 + token 统计"""

    def __init__(self, api_key: Optional[str] = None, pool: Optional[KeyPool] = None):
        # 兼容两种调用方式：
        # 1) 旧：传 api_key 单 key 模式（用于 .env fallback）
        # 2) 新：传 pool 多 key 模式（DB 中多个 DeepSeek key 时）
        self.pool = pool
        self._fallback_key = api_key or settings.DEEPSEEK_API_KEY
        self._current_key_id: Optional[str] = None
        self.client: Optional[OpenAI] = None
        self.model = settings.DEEPSEEK_MODEL
        self.think_model = settings.DEEPSEEK_THINK_MODEL
        # 单 key 模式：构造一次 client
        if self.pool is None and self._fallback_key:
            self.client = OpenAI(
                api_key=self._fallback_key,
                base_url=settings.DEEPSEEK_BASE_URL,
                timeout=120,
            )
            self._current_key_id = "_fallback"

    def _resolve_client(self, timeout: float = 120.0) -> tuple[Optional[OpenAI], Optional[str]]:
        """根据 pool 状态选择 key，返回 (client, key_id)。
        timeout 仅对本次新建 client 生效；默认 120s（开仓/辩论用），
        出场等实时决策可传更短的值（如 12s）做硬墙，避免拖累主循环。
        """
        if self.pool and not self.pool.is_empty():
            item: KeyPoolItem = self.pool.pick()
            if item is not None:
                client = OpenAI(
                    api_key=item.api_key,
                    base_url=settings.DEEPSEEK_BASE_URL,
                    timeout=timeout,
                )
                return client, item.key_id
        # pool 为空或没传 pool → fallback
        if self._fallback_key:
            # 仅默认超时(120)复用缓存 client；其余按指定超时新建（避免出场短墙被缓存120s client拖累）
            if timeout == 120.0 and self.client is not None:
                return self.client, "_fallback"
            client = OpenAI(
                api_key=self._fallback_key,
                base_url=settings.DEEPSEEK_BASE_URL,
                timeout=timeout,
            )
            if timeout == 120.0:
                self.client = client
            return client, "_fallback"
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
        """
        401 认证失败时：①从内存 pool 移除该 Key ②同步标记 DB is_valid=0, is_active=0。
        移除后 pool 可能变空 → _resolve_client 自动回退 .env Key。
        """
        if not key_id or key_id == "_fallback" or not self.pool:
            return
        logger.warning(f"[DeepSeek] Key {key_id} 认证失败，自动下线: {error_msg[:120]}")
        self.pool.deactivate(key_id)
        # 同步标记 DB（持久化，重启后也不再用）
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
                    logger.info(f"[DeepSeek] DB Key {db_id} 已标记 is_valid=0, is_active=0")
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[DeepSeek] DB 下线标记失败: {e}")

    def _ds_chat(self, client, key_id, **kwargs):
        """带全局并发信号量 + 空内容重试的 DeepSeek 调用。

        根因修复：deepseek-v4-pro 推理模型在高并发下返回空 content(empty_response)，
        导致辩论/出场决策静默回退规则引擎。限制同时在途调用数(信号量)可彻底消除；
        空内容(非 length 截断)再重试一次兜底。
        """
        last = None
        for _ in range(3):  # 初调 + 2 次重试：空响应兜底
            kwargs.setdefault("timeout", 120)   # ★ 显式超时：慢API快速失败，避免挂起占用信号量槽(默认600s会卡死整轮)
            with _DS_SEM:
                resp = client.chat.completions.create(**kwargs)
            ch = resp.choices[0] if getattr(resp, "choices", None) else None
            content = (ch.message.content if ch else None) or ""
            fr = getattr(ch, "finish_reason", None) if ch else None
            if content.strip() or fr == "length":
                return resp
            last = resp  # 空内容且非 length → 重试一次
        return last

    def _select_analysis_model(self, market_data: dict, use_deep_think: bool = True) -> str:
        """
        2026-08-11 模型路由：全程强制 flash（用户硬性要求禁用 pro 降本）。
        无论简单/复杂/分歧场景，一律返回 self.model（flash）。use_deep_think 参数保留以兼容
        调用方，但不再触发任何 pro 升级——flash 对 XAUUSD 技术分析已足够。
        """
        # 默认主力：flash（廉价、快）
        selected = self.model

        # 调用方明确要求 deep think 时，再判断是否真的需要 pro
        if use_deep_think:
            _complex = False
            _regime = market_data.get("regime", {})
            _sentinel = market_data.get("reversal_sentinel", {})
            _key_levels = market_data.get("key_levels", {})
            _price = market_data.get("current_price") or 0
            try:
                _price = float(_price)
            except Exception:
                _price = 0.0
            _ext_z = float(_regime.get("extension_z", 0) or 0)
            _vol = str(_regime.get("volatility_regime", "")).lower()
            _trend = str(_regime.get("trend_regime", "")).lower()

            # 1) 价格处于极端延伸或末端风险区
            if abs(_ext_z) >= 2.2 or _sentinel.get("signal") not in (None, "NONE", "none", ""):
                _complex = True
            # 2) 高波动/极端体制
            if _vol in ("高波动", "极端", "high", "extreme"):
                _complex = True
            # 3) 当前价贴近关键支撑/阻力（±$2 以内，黄金 1 点≈$1）
            for lvl in (_key_levels.get("support", []) or []) + (_key_levels.get("resistance", []) or []):
                try:
                    if abs(float(lvl) - _price) <= 2.0:
                        _complex = True
                        break
                except Exception:
                    continue
            # 4) 已有持仓且浮亏较大（需要深度决策止损/反手）
            _positions = market_data.get("my_open_positions") or []
            for p in _positions:
                try:
                    if float(p.get("floating_pnl") or 0) <= -80:
                        _complex = True
                        break
                except Exception:
                    continue

            if _complex:
                # ★ 2026-08-11 用户要求：禁用 pro，复杂场景也强制 flash 降本。
                selected = self.model
                logger.info(f"[DeepSeek] 复杂场景仍用 flash(禁用pro): ext_z={_ext_z:.2f} sentinel={_sentinel.get('signal')} vol={_vol}")

        return selected

    def _compress_for_prompt(self, md: dict) -> dict:
        """根治 DeepSeek 截断（国际调研精髓，≥3源交叉验证）:
        - kodiq.ai: 截断三大主因之一是「上下文窗口溢出」——输入占满 → 输出无空间;
          解法 = clear the desk，只保留必要部分，不要把全部数据塞进每个请求。
        - dreaming.press: 结构化输出截断应 discard-and-retry（已实现），而非拼接片段。
        - ai-tldr.dev: 每个 LLM 调用应记录 input/output token 与 finish_reason（可观测）。
        这里压缩注入提示的 *输入* 体积: 保留全部决策周期(M1/M5/M15/M30/H1/H4/D1)近N根OHLC(每周期截断控制token)，
        截断 price_structure 长序列; smc/regime/哨兵/进化/持仓/成交 保留(核心决策信息)。
        M1 仅喂原始 bars 供精准入场判断，不参与体制/门控。
        """
        if not isinstance(md, dict):
            return md
        out = dict(md)
        tf = md.get("timeframes")
        if isinstance(tf, dict):
            tf_out = {}
            # ★ 2026-08-06 保留全部6周期（落实第一天「多周期趋势指标给AI」方案）；
            #   每周期仅截最近N根控制token，不再砍掉M5/M30短周期（否则AI看不到短期下跌）。
            # ★ 2026-08-17 盯盘修复（P1·DS 推理链截断 54 次/开市3小时）：
            #   早盘实测输入 token 达 13113（超限→finish_reason=length→reasoning 被砍）。
            #   收紧每周期保留根数（M1 20→12、H1/H4 24→18 等），预计砍 30%+ 输入体积；
            #   仍保留全部 7 周期（不砍周期数，只减每周期冗余历史，不违反多周期方案）。
            _KEEP_BARS = {"M1": 12, "M5": 12, "M15": 14, "M30": 16, "H1": 18, "H4": 18, "D1": 8}
            for k, _keep in _KEEP_BARS.items():
                d = tf.get(k)
                if not isinstance(d, dict):
                    continue
                # ★★ 2026-08-17 P0 修复：market_analyzer snapshot 的 timeframes 里
                #   "bars" 是数量(int)、真实 K 线在 "closes"(列表)；原实现读 bars →
                #   tf_out 只输出 {"bars": 数量}，rsi/macd/trend/atr 等指标全丢，
                #   DS 只能靠 price_structure 的 12 根 K 线（多周期指标失明）。
                #   修复：透出指标摘要 + closes 尾部（数量可控不膨胀 token）。
                _closes = d.get("closes") or []
                if isinstance(_closes, list) and _closes:
                    _closes_tail = [round(float(c), 2) for c in _closes][-_keep:]
                else:
                    _closes_tail = []
                tf_out[k] = {
                    "closes_tail": _closes_tail,
                    "rsi": d.get("rsi"),
                    "macd": d.get("macd"),
                    "trend": d.get("trend"),
                    "atr": d.get("atr"),
                    "ma20": (d.get("ma") or {}).get("MA20") if isinstance(d.get("ma"), dict) else None,
                    "latest": d.get("latest"),
                }
            out["timeframes"] = tf_out
        ps = md.get("price_structure")
        if isinstance(ps, dict):
            ps_out = {}
            for k, v in ps.items():
                if isinstance(v, list) and len(v) > 30:
                    ps_out[k] = v[-30:]
                else:
                    ps_out[k] = v
            out["price_structure"] = ps_out
        return out

    def analyze(
        self,
        market_data: dict,
        use_deep_think: bool = True,
    ) -> dict:
        """
        技术分析 — DeepSeek V4 专长
        返回: {decision, confidence, reasoning, key_factors}
        """
        from app.services.news_service import format_prompt_block as _fmt_news_block
        client, key_id = self._resolve_client()
        if client is None:
            # ★ Phase 6：`_api_failed` 标记（与 hunyuan_client 对称）。
            #   必须把「API 故障」和「模型审慎给 HOLD」区分开——前者是故障要触发降级，
            #   后者是正常决策。只看 decision=="HOLD" 无法区分，会把审慎误判成宕机。
            return {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                    "reasoning": "（未配置 DeepSeek Key）"}

        model = self._select_analysis_model(market_data, use_deep_think)

        # ★ 根治截断：压缩注入上下文(国际调研: kodiq.ai 上下文溢出=截断主因②; 只留必要数据)
        market_data = self._compress_for_prompt(market_data)

        system_prompt = """你是世界顶级黄金(XAUUSD)机构级交易分析师，融合「智能资金概念(SMC/ICT)」与「市场体制感知(Regime)」框架。

你必须基于提供的数据（含机构订单流结构与体制）做出概率判断。返回严格的JSON格式：
{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "简洁的机构级推理（中文，不超过200字）",
    "key_factors": ["因素1", "因素2", "因素3"],
    "entry_price": 建议入场价(nullable),
    "stop_loss": 建议止损价(nullable),
    "take_profit": 建议止盈价(nullable),
    "position_action": {"action": "hold" | "trim" | "close_all", "confidence": 0.0-1.0, "reason": "简短理由"}
}

【持仓管理指令·核心任务】(2026-08-17 篮子级 AI 持仓管理)
你已看到「持仓篮总览 + 逐笔持仓」(my_open_positions)。有持仓时，管理好持仓与找机会同等重要：
- 若持仓合计浮盈可观但行情开始不利（动能衰竭/结构破位/浮盈从峰值明显回吐）→ close_all 或 trim，主动锁利，绝不坐等回吐到亏损；
- 若持仓方向仍被结构/体制支持且浮盈健康 → hold，让利润奔跑；
- 行情反转结构明确 → close_all，不恋战；
- 没有持仓或判断不清 → position_action.action = "hold"。
position_action 是对【全部当前持仓】的篮子级建议：trim=每笔减仓一半，close_all=全平。缺失/非法解析一律按 hold 处理。

分析框架（AI原生综合，不写死规则，BUY/SELL 方向完全对称）：
1. 以机构订单流结构为决策骨架：Order Block(机构建仓区)、FVG(公允价值缺口)、BOS/CHoCH(结构突破)、Liquidity Sweep(流动性扫荡)。价格回踩机构需求区(FVG/OB)且结构确认才做多；回踩供给区且结构确认才做空。优先在「结构+流动性」共振区交易。
2. 尊重市场体制(Regime)：趋势市跟随结构顺势交易、震荡市不追突破、高波动缩仓。牛市结构支持 BUY，熊市结构支持 SELL，无预设立场。严禁把"顺势做多"或"顺势做空"作为默认假设。
3. 【三周期协同】已为你提供各周期的 OHLC K 线与趋势方向(trend_dir)：
   - 4H = 方向偏置(bias)：定大局方向，回踩偏置方向更安全；
   - 15m = 结构确认：验证 4H 偏置是否被中周期证实，同向=趋势可靠、反向=潜在反转需观望；
   - 5m = 入场时机：短周期动量决定实际入场节奏，5m+15m 同向=入场确认充分；
   - M1 = 仅供精准入场参考（噪声大，不可用于方向判断）。
   【短周期转向优先于长周期滞后】——当 5m/15m 已转跌而 4H 仍在涨时，应判定为下跌初期、优先 SELL / 回避 BUY。
4. 价格延伸度自检：仅当 extension_z>2.5 且伴随 SMC 结构证伪（反向流动性扫荡/CHoCH）才视为"接飞刀"需克制；延伸度在 1.5~2.5 的正常高位/低位区间，顺势单照常交易。
5. 不预测，只做概率判断；仅在完全无方向感（置信度<0.3）时才返回HOLD，其余正常表达BUY/SELL判断。
6. 方向无偏：不得因历史统计（如"某方向曾经亏损"）而系统性地偏向 BUY 或 SELL；每次判断只基于当前价格行为、SMC 结构与市场体制。
7. 你还会收到「原始价格结构(最近N根K线序列)」与「跨资产/宏观环境(DXY/VIX/相关性)」作为独立证据，须自行判读、相互印证，不被单一指标结论带偏。
8. 【价格行为硬约束】当 M5/M15 连续 3 根以上 K 线同向创近期新高/新低，且当前无明确反向流动性扫荡或 CHoCH 结构确认时，必须优先跟随短周期实际动量方向（创新低→SELL，创新高→BUY）。禁止仅因 H4 存在未测试的 FVG/OB 支撑/阻力就逆势抄底/摸顶。
9. 【本地模型制衡·强制解释（v4 关键机制）】你还会收到「本地 Meta 质量陪审团」结论（由独立的本地时序模型 Chronos 给出，只看价格行为、不依赖你的语义推理，是防止"接飞刀"的关键制衡）。若其方向与你的 BUY/SELL 判断严重冲突（例如陪审团 Chronos 强烈看空而你判 BUY，或强烈看多而你判 SELL），你必须在 reasoning 中专门解释：为何你的判断优先于本地时序模型？否则你的方向判定视为缺乏独立证据支撑、可信度打折。本地模型不受你"意图"影响，当它强烈反向时，宁可先观望也不要盲目逆本地模型开单。"""

        user_prompt = f"""当前XAUUSD市场数据:

时间框架数据:
{json.dumps(market_data.get('timeframes', {}), indent=2, ensure_ascii=False)}

近期关键价位:
{json.dumps(market_data.get('key_levels', {}), indent=2, ensure_ascii=False)}

【机构订单流结构 SMC（2026前沿·机构足迹，决策骨架）】:
{json.dumps(market_data.get('smc_features', {}), ensure_ascii=False, default=str)}

【市场体制 Regime（趋势/震荡/末端风险，最高优先级参考）】:
{json.dumps(market_data.get('regime', {}), ensure_ascii=False, default=str)}

【反转哨兵警示（趋势末端反转制衡，若 signal≠NONE 须高度警惕接飞刀）】:
{json.dumps(market_data.get('reversal_sentinel', {}), ensure_ascii=False, default=str)}

【本地进化洞察（基于本系统真实盈亏的在线学习，数据驱动软参考）】:
{json.dumps(market_data.get('evolution_advice', []), ensure_ascii=False, default=str)}

【本地 Meta 质量陪审团（Chronos 时序模型 + SMC/Regime 融合，独立制衡你的方向判断；详见上面第9条强制解释规则）】:
{json.dumps(market_data.get('meta_quality', {}), ensure_ascii=False, default=str)}

市场状态:
- 点差: {market_data.get('spread', 'N/A')}
- 当前价格: {market_data.get('current_price', 'N/A')}
- 波动率: {json.dumps(market_data.get('volatility_metrics', {}), ensure_ascii=False)}

【原始价格结构（最近N根K线实体/影线/连续同向/摆动高低点趋势，AI 自行读证据）】:
{json.dumps(market_data.get('price_structure', {}), ensure_ascii=False, default=str)}

【结构突破 BOS/CHoCH（SMC/ICT 趋势启动识别，2026-08-17 调研落地，重要决策信息）】:
{json.dumps(market_data.get('structure_break', {}), ensure_ascii=False, default=str)}
判读规则（海外 SMC/ICT ≥3源交叉验证的行业标准）:
- BOS(Break of Structure)=收盘突破摆动点且结构延续(HH/LL)→趋势确认信号；方向与 BOS 一致=顺结构，与 BOS 相反=逆结构做单须额外举证。
- CHoCH(Change of Character)=逆势首破→反转预警，原方向结构可能失效。
- 只认收盘价（wick 影线穿透不算突破）。
- htf_aligned=true 且 displacement≥0.8×ATR = 高周期一致+突破有力 = 强趋势启动信号，应优先顺势方向，不要在此时逆势。
- BOS 是方向过滤器不是追价触发器：突破后等回踩结构位再介入，不追突破蜡烛。

【跨资产 / 宏观环境（DXY 美元强度、VIX 恐慌指数、DXY-XAU 相关性；外部数据已抓取但此前未进 AI，本次补强）】:
{json.dumps(market_data.get('external', {}), ensure_ascii=False, default=str)}

{_fmt_news_block(market_data)}

【订单流 / CVD（买盘是否枯竭、卖压是否放大；Binance永续+MT5本地代理双源，2026-08-06 补强②）】:
{json.dumps(market_data.get('orderflow', {}), ensure_ascii=False, default=str)}

【执行质量滑点（经纪商实际成交滑点，警惕 B-book 滑点剥削；2026-08-06 补强⑥）】:
{json.dumps(market_data.get('execution', {}), ensure_ascii=False, default=str)}

【我方当前真实持仓（来自MT5，决策前必须参考，避免重复同向下单或逆势加仓）】:
{_fmt_my_positions(market_data)}

【我方最近真实成交复盘（来自MT5实盘，AI必须从自己的盈亏中学习，避免重复犯同方向/同形态的错误）】:
{_fmt_recent_trades(market_data)}

请给出你的技术分析判断。若我已持有同方向仓位且浮盈，勿盲目加仓；若已持有反方向仓位且被套，需明确判断是否该止损/反手。"""

        # 注入历史经验教训（从 debate_engine 经 memory_bank 加载）
        _lessons = market_data.get("empirical_lessons")
        if _lessons:
            user_prompt += f"""

【历史盈亏统计参考（仅用于仓位/风控/出场参考，不得用于方向偏置）】:
{json.dumps(_lessons, indent=2, ensure_ascii=False, default=str)}

以上为本系统历史盈亏统计的参考语境。请结合当前 SMC 机构结构与市场体制综合判断，
不得因"某方向历史上亏损多"就系统性地回避该方向；历史教训只影响仓位大小、止损松紧与出场节奏，
不影响 BUY/SELL/HOLD 的方向选择。"""

        try:
            response = self._ds_chat(
                client, key_id,
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=settings.AI_TEMPERATURE,
                max_tokens=settings.AI_MAX_TOKENS_ANALYSIS,
                response_format={"type": "json_object"},
            )
            _extract_usage(response, self.pool, key_id)
            # ★ 内容守卫：暴露真实错误原因，不再静默吞掉
            if not response.choices:
                logger.error(f"[DeepSeek] 返回空 choices model={model} (key={key_id})")
                return {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                        "reasoning": f"模型返回空响应(model={model})，请检查Key/网关/模型名"}
            content = response.choices[0].message.content
            if not content or not content.strip():
                fr = getattr(response.choices[0], "finish_reason", None)
                # ★ 2026-08-11 强制降费：禁止 length 截断时翻倍到 8192。
                #   原因：deepseek-v4-flash 推理链被截断后升级 token 会导致单次调用费用指数级增长；
                #   当前已把 AI_MAX_TOKENS_ANALYSIS 从 4096 限制到 2048，若仍截断，说明模型输出过长，
                #   应直接要求模型精简推理而非烧钱换完整 JSON。保持原 token 重试一次即停。
                if fr == "length":
                    logger.warning(
                        f"[DeepSeek] finish_reason=length（推理链截断），保持 token={settings.AI_MAX_TOKENS_ANALYSIS} "
                        f"重试一次，禁止升级到 8192 (key={key_id})"
                    )
                    try:
                        response2 = self._ds_chat(
                            client, key_id,
                            model=model,
                            messages=[
                                {"role": "system", "content": system_prompt + "\n【强制约束】你的 reasoning 必须严格控制在 100 字以内，只留核心结论；禁止展开长篇推理，否则 JSON 将被截断而无法使用。"},
                                {"role": "user", "content": user_prompt},
                            ],
                            temperature=settings.AI_TEMPERATURE,
                            max_tokens=settings.AI_MAX_TOKENS_ANALYSIS,
                            response_format={"type": "json_object"},
                        )
                        _extract_usage(response2, self.pool, key_id)
                        content = response2.choices[0].message.content if response2.choices else None
                        fr = getattr(response2.choices[0], "finish_reason", None) if response2.choices else None
                    except Exception as e2:
                        logger.error(f"[DeepSeek] 保持 token 重试异常: {e2}")
                if not content or not content.strip():
                    logger.error(f"[DeepSeek] 返回空内容 model={model} finish_reason={fr} (key={key_id})")
                    return {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                            "reasoning": f"模型返回空内容(finish_reason={fr}, model={model})，可能Key无效或模型不存在"}
            try:
                result = _safe_json_loads(content)
            except Exception as e:
                logger.error(f"[DeepSeek] 解析失败: {e}")
                # ★ Phase 6：解析失败 = 本次调用没拿到可用结论，等同该云不可用，
                #   必须打 `_api_failed`，否则会被下游当成「模型审慎 HOLD」而不计入
                #   失败连击 → 云已经废了却永远降不了级。
                result = {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                          "reasoning": f"API错误: {str(e)[:160]}"}
            # 可观测(ai-tldr.dev: 每次LLM调用应记录 in/out token + finish_reason，截断率超阈值即告警)
            try:
                _u = getattr(response, "usage", None)
                if _u is not None:
                    logger.info(f"[DeepSeek] token用量 in={getattr(_u, 'prompt_tokens', 0)} "
                                f"out={getattr(_u, 'completion_tokens', 0)} "
                                f"finish={getattr(response.choices[0], 'finish_reason', None) if response.choices else '?'}")
            except Exception:
                pass

            logger.info(f"[DeepSeek] 决策: {result.get('decision')} 置信度: {result.get('confidence')} (key={key_id})")
            logger.info(f"[DeepSeek] 推理: {result.get('reasoning')} | 关键因素: {result.get('key_factors')}")
            return result
        except Exception as e:
            # 401 认证失败 → 自动下线该 Key + 重试一次（回退 .env）
            if self._is_auth_error(e) and key_id and key_id != "_fallback":
                self._deactivate_key(key_id, str(e))
                client2, key_id2 = self._resolve_client()
                if client2 is not None:
                    logger.info(f"[DeepSeek] 401 后重试 (key={key_id2})")
                    try:
                        response = self._ds_chat(
                            client2, key_id2,
                            model=model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            temperature=settings.AI_TEMPERATURE,
                            max_tokens=settings.AI_MAX_TOKENS_ANALYSIS,
                            response_format={"type": "json_object"},
                        )
                        _extract_usage(response, self.pool, key_id2)
                        if not response.choices or not (response.choices[0].message.content or "").strip():
                            fr2 = getattr(response.choices[0], "finish_reason", None) if response.choices else None
                            logger.error(f"[DeepSeek] 重试仍空内容 finish_reason={fr2} (key={key_id2})")
                            return {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                                    "reasoning": f"重试后模型仍返回空内容(finish_reason={fr2})"}
                        result = _safe_json_loads(response.choices[0].message.content)
                        logger.info(f"[DeepSeek] 重试成功: {result.get('decision')} 置信度: {result.get('confidence')} (key={key_id2})")
                        return result
                    except Exception as e2:
                        logger.error(f"[DeepSeek] 重试仍失败: {e2}")
            logger.error(f"[DeepSeek] 分析失败: {e}")
            return {"decision": "HOLD", "confidence": 0.0, "_api_failed": True,
                    "reasoning": f"API错误: {str(e)[:160]}"}

    def evaluate_exits(self, positions_payload: list, market_context: dict, timeout: float = 12.0) -> dict:
        """
        AI 出场决策评估（M1）：对一批持仓一次性输出出场意图，避免逐笔调 LLM。
        使用快速非思考模型(self.model) + 短超时硬墙，失败返回 {"error":..., "decisions":[]} 由调用方回退规则引擎。
        返回: {"decisions": [{"ticket","action","close_pct","new_sl","reason"}, ...]}
        """
        client, key_id = self._resolve_client(timeout=timeout)
        if client is None:
            return {"error": "no_deepseek_key", "decisions": []}
        model = self.model  # 快速非思考模型

        user_prompt = (
            f"当前XAUUSD市场背景:\n"
            f"{json.dumps(market_context, indent=2, ensure_ascii=False)}\n\n"
            f"需要决策的持仓(共{len(positions_payload)}笔):\n"
            f"{json.dumps(positions_payload, indent=2, ensure_ascii=False)}\n\n"
            f"请对每笔持仓给出出场决策，返回严格JSON: {{\"decisions\":[...]}}"
        )
        last_err = None
        for attempt in range(2):  # 首次 + 1 次重试(自动换下一个 key，绕过偶发延迟)
            client, key_id = self._resolve_client(timeout=timeout)
            if client is None:
                return {"error": "no_deepseek_key", "decisions": []}
            try:
                response = self._ds_chat(
                    client, key_id,
                    model=model,
                    messages=[
                        {"role": "system", "content": EXIT_DECISION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    # ★ 2026-08-11 强制降费：出场评估统一使用 AI_MAX_TOKENS_ANALYSIS(2048)，
                    #   不再保底 4096。多持仓出场应精简 reasoning，靠结构化 JSON 承载决策。
                    max_tokens=settings.AI_MAX_TOKENS_ANALYSIS,
                    response_format={"type": "json_object"},
                )
                _extract_usage(response, self.pool, key_id)
                content = response.choices[0].message.content if response.choices else None
                if not content or not content.strip():
                    fr = getattr(response.choices[0], "finish_reason", None) if response.choices else None
                    # ★ 2026-08-11 强制降费：禁止 length 截断时翻倍到 8192。
                    if fr == "length":
                        logger.warning(
                            f"[DeepSeek] 出场决策 finish_reason=length（截断），保持 token={settings.AI_MAX_TOKENS_ANALYSIS} "
                            f"重试一次，禁止升级到 8192 (key={key_id})"
                        )
                        try:
                            response2 = self._ds_chat(
                                client, key_id, model=model,
                                messages=[
                                    {"role": "system", "content": EXIT_DECISION_SYSTEM_PROMPT + "\n【强制约束】每笔持仓的 reasoning 必须控制在 30 字以内，只输出结论；禁止长篇分析，否则 JSON 将被截断。"},
                                    {"role": "user", "content": user_prompt},
                                ],
                                temperature=0.3, max_tokens=settings.AI_MAX_TOKENS_ANALYSIS,
                                response_format={"type": "json_object"},
                            )
                            _extract_usage(response2, self.pool, key_id)
                            content = response2.choices[0].message.content if response2.choices else None
                            fr = getattr(response2.choices[0], "finish_reason", None) if response2.choices else None
                        except Exception as e2:
                            logger.error(f"[DeepSeek] 出场决策保持token重试异常: {e2}")
                    if not content or not content.strip():
                        logger.error(f"[DeepSeek] 出场决策返回空内容 model={model} finish_reason={fr} (key={key_id})")
                        return {"error": "empty_response", "decisions": []}
                result = _safe_json_loads(content, fallback_decision="HOLD")
                decisions = result.get("decisions", [])
                if not isinstance(decisions, list):
                    return {"error": "bad_decisions_format", "decisions": []}
                return {"decisions": decisions}
            except Exception as e:
                last_err = e
                # 超时/连接类异常 → 换 key 重试一次（不立即回退规则引擎）
                is_timeout = ("timed out" in str(e).lower()
                              or "timeout" in type(e).__name__.lower()
                              or "connection" in str(e).lower())
                if is_timeout and attempt == 0:
                    logger.warning(f"[DeepSeek] 出场决策超时, 换key重试(attempt {attempt + 1})")
                    continue
                logger.warning(f"[DeepSeek] 出场决策评估失败(账户回退规则引擎): {e}")
                return {"error": str(e), "decisions": []}
        return {"error": str(last_err), "decisions": []}

    def debate_rebuttal(self, opponent_analysis: dict, my_analysis: dict, market_data: dict) -> dict:
        """
        辩论反驳 — 看到对方论据后重新评估
        """
        from app.services.news_service import format_prompt_block as _fmt_news_block
        client, key_id = self._resolve_client()
        if client is None:
            return {"decision": my_analysis.get("decision"),
                    "confidence": my_analysis.get("confidence", 0.5),
                    "agree_with_opponent": False}

        system_prompt = """你是世界顶级黄金交易分析师。你看到了另一位分析师的判断。

你的任务是审视对方的论据，看是否有足以推翻你原始判断的硬伤（如忽略了关键支撑/阻力、误判了趋势方向、忽略了重要的背离信号）。如果没有这种硬伤，保持你的立场不变。

返回严格JSON:
{
    "decision": "BUY" | "SELL" | "HOLD",
    "confidence": 0.0-1.0,
    "agree_with_opponent": true | false,
    "rebuttal_points": ["反驳点1", "反驳点2"],
    "revised_reasoning": "修正后的推理（中文，不超过150字）",
    "entry_price": 建议入场价(nullable，维持原方向时用你更优的价位；翻转为新方向时给新方向的入场价；无明确价位填null)
}
原则：
1. 只在对方面摆出你确实忽略的硬伤时才同意，否则坚持自己的判断
2. 对方如果也是 HOLD（观望）——这不构成反驳理由，坚持你的方向判断
3. 技术分析本质是概率判断，不必因为对方"更安全"而放弃自己的独立判断
4. BUY 与 SELL 完全对称：不得因历史盈亏统计而预设任何方向偏好
5. 【强制反调·魔鬼代言人（2026-08-13 强化牛熊对抗辩论）】你必须在反驳中至少列出 1 条与你最终方向相反的、最强的反向风险（为何这可能是顺势陷阱 / 假突破 / 趋势末端接飞刀 / 被新闻舆情反向打脸）。若你维持原方向，必须明确逐条驳倒该反向风险；若证据已变弱，应敢于降为 HOLD 甚至翻转。不要被对方或你自己的初始判断锚定，须重新独立评估。"""

        # ★ 2026-08-13 审计修复(B1)：第二轮辩论是 meta_agent 优先采用的最终决策来源，
        #   原实现只喂 H1+成交+新闻，丢弃了 my_open_positions/regime/sentinel/meta_quality/smc
        #   等关键上下文 → 最终方向在「看不见自己账本」下拍板，直接击穿「根治 AI 失明」保证。
        #   现与第一轮 analyze 对称补全上下文（重点补 my_open_positions 持仓篮）。
        user_prompt = f"""我的初始判断: {json.dumps(my_analysis, ensure_ascii=False)}

对方分析师的判断: {json.dumps(opponent_analysis, ensure_ascii=False)}

时间框架数据(H1):
{json.dumps(market_data.get('timeframes', {}).get('H1', {}), ensure_ascii=False)}

近期关键价位:
{json.dumps(market_data.get('key_levels', {}), ensure_ascii=False)}

【机构订单流结构 SMC】:
{json.dumps(market_data.get('smc_features', {}), ensure_ascii=False, default=str)}

【市场体制 Regime】:
{json.dumps(market_data.get('regime', {}), ensure_ascii=False, default=str)}

【反转哨兵警示】:
{json.dumps(market_data.get('reversal_sentinel', {}), ensure_ascii=False, default=str)}

【本地 Meta 质量陪审团（Chronos 时序模型制衡方向判断）】:
{json.dumps(market_data.get('meta_quality', {}), ensure_ascii=False, default=str)}

市场状态:
- 点差: {market_data.get('spread', 'N/A')}
- 当前价格: {market_data.get('current_price', 'N/A')}
- 波动率: {json.dumps(market_data.get('volatility_metrics', {}), ensure_ascii=False)}

【原始价格结构】:
{json.dumps(market_data.get('price_structure', {}), ensure_ascii=False, default=str)}

【跨资产 / 宏观环境（DXY / VIX / 相关性）】:
{json.dumps(market_data.get('external', {}), ensure_ascii=False, default=str)}

【订单流 / CVD】:
{json.dumps(market_data.get('orderflow', {}), ensure_ascii=False, default=str)}

【我方当前真实持仓（来自MT5，辩论时仍须参考，避免重复同向下单或逆势加仓）】:
{_fmt_my_positions(market_data)}

【我方最近真实成交复盘（来自实盘，必须参考，避免重复犯错）】:
{_fmt_recent_trades(market_data)}

{_fmt_news_block(market_data)}

请给出你的辩论反驳。"""

        # 反驳阶段也注入历史教训（仅用于风控/仓位校准，不得用于方向偏置）
        _lessons = market_data.get("empirical_lessons")
        if _lessons:
            user_prompt += f"""

【实盘统计参考（仅用于风控/仓位校准，不得用于方向偏置）】:
{json.dumps(_lessons, indent=2, ensure_ascii=False, default=str)}

以上统计仅作风险参考，不得因"某方向历史上亏损多"就拒绝在该方向出现明确结构信号时翻转。"""

        # 模型路由：★ 2026-08-11 用户硬性要求全程 flash、禁用 pro（降本）。
        # 原「方向冲突→升级 pro 深度仲裁」逻辑已移除，分歧场景也用 flash。
        _debate_model = self.model

        try:
            response = self._ds_chat(
                client, key_id,
                model=_debate_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=settings.AI_MAX_TOKENS_DEBATE,
                response_format={"type": "json_object"},
            )
            _extract_usage(response, self.pool, key_id)
            try:
                result = _safe_json_loads(response.choices[0].message.content,
                                        fallback_decision=my_analysis.get("decision") or "HOLD")
                # ★ 修复：safe_json_loads fallback 给 confidence=0.0，但应该用初始置信度
                if result.get("confidence", 0) <= 0 and my_analysis.get("confidence", 0) > 0:
                    result["confidence"] = my_analysis["confidence"]
                return result
            except Exception as e:
                logger.error(f"[DeepSeek] 辩论解析失败: {e}")
                return {"decision": my_analysis.get("decision"),
                        "confidence": my_analysis.get("confidence", 0.5),
                        "agree_with_opponent": False,
                        "revised_reasoning": f"（辩论轮解析失败）{str(e)[:120]}"}
        except Exception as e:
            # 401 认证失败 → 自动下线该 Key + 重试一次（回退 .env）
            if self._is_auth_error(e) and key_id and key_id != "_fallback":
                self._deactivate_key(key_id, str(e))
                client2, key_id2 = self._resolve_client()
                if client2 is not None:
                    logger.info(f"[DeepSeek] 辩论 401 后重试 (key={key_id2})")
                    try:
                        response = self._ds_chat(
                            client2, key_id2,
                            model=self.model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            temperature=0.4,
                            max_tokens=settings.AI_MAX_TOKENS_DEBATE,
                            response_format={"type": "json_object"},
                        )
                        _extract_usage(response, self.pool, key_id2)
                        result = _safe_json_loads(
                            response.choices[0].message.content,
                            fallback_decision=my_analysis.get("decision") or "HOLD",
                        )
                        logger.info(f"[DeepSeek] 辩论重试成功 (key={key_id2})")
                        return result
                    except Exception as e2:
                        logger.error(f"[DeepSeek] 辩论重试仍失败: {e2}")
            logger.error(f"[DeepSeek] 辩论失败: {e}")
            return {"decision": my_analysis.get("decision"), "confidence": my_analysis.get("confidence", 0.5), "agree_with_opponent": False}