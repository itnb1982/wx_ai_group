# -*- coding: utf-8 -*-
"""
辩论环（TradingAgents 式 · 加法增强 · 提准非拦截）
============================================================

设计宗旨（对齐万象Ai 交易系统铁律）：
  ★ 提准非拦截：辩论环【只缩放置信】，绝不改方向、绝不硬 HOLD、绝不砍笔数。
    它只对「已被多视角明确质疑的弱信号」做乘性软缩权（∈[FLOOR, 1.0]），
    使最差的边际单自然低于开仓门槛 → 提升整体方向准确率；强共识单照常 → 不腰斩笔数。
  ★ 不回问 / 不新增云模型调用：直接消费裁决阶段【已有的】多路信号
    （DeepSeek / 混元 / Chronos / 融合票 / 视觉 / 副驾 / SMC订单流 / 体制 / 风险），
    把它们当作 TradingAgents 的「牛熊研究员 + 风控多视角审议团」的各方观点，做对抗式综合。
    —— 不启动新的 LLM agent（避免新增 token 成本 / 推理延迟 / 非确定性 / 违背 8B 角色铁律）。
  ★ 完全可灰度：DEBATE_RING_ENABLED 默认 False；全部参数 getattr 化。
  ★ 永不崩溃：任何异常 → 返回 (原置信, 降级标记)，绝不阻断决策链。

TradingAgents 辩论环移植说明（为何用「已有信号」而非「新开 N 个 LLM agent」）：
  TradingAgents(Tauric Research, arXiv:2412.20138) 的核心是「多角色分析师对抗辩论 →
  主持人综合 → 风险委员会多视角审议 → 谨慎决策」。本系统已有 DS/HY(双脑) + Chronos/融合票(时序)
  + 视觉(结构) + 副驾(确认) + SMC(订单流) 共 6~7 路独立视角，天然构成「研究团」。
  把它们按「支持终裁方向(牛) / 反对(熊) / 中立」分类并综合分歧度，
  正是 TradingAgents 辩论环的对抗式核心价值（分歧→谨慎），且零额外成本、完全可复现 A/B。
  后续如需「真·LLM 牛熊对抗提示」可作为可选项（DEBATE_RING_LLM_DELIB 子开关）进一步增强，本版不实现以免引入非确定性。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("DebateRing")


def _view_of(panel_dir: str, final_decision: str) -> int:
    """把某路信号的方向票映射为对终裁的视角：+1 支持 / -1 反对 / 0 中立或未知。"""
    d = (panel_dir or "").upper()
    if d in ("BUY", "SELL"):
        return 1 if d == final_decision else -1
    return 0  # HOLD / NEUTRAL / 空 → 中立


def run_debate_ring(final_decision: str, final_confidence: float,
                    ctx: dict, settings=None, shadow: bool = False) -> tuple:
    """
    运行辩论环，返回 (缩放后置信, 详情dict)。

    Args:
        final_decision: 当前终裁方向（BUY / SELL / HOLD）。
        final_confidence: 当前终裁置信（0~1）。
        ctx: 多视角信号上下文（见 adjudicate 注入点构造）。
        settings: 配置对象（用 getattr 安全读取，缺省全关）。
        shadow: ★ 2026-08-17 shadow 模式——忽略 DEBATE_RING_ENABLED 开关强制计算，
                用于 walk-forward A/B 基线采集（"若开启会缩到多少"），调用方负责不应用结果。

    Returns:
        (scaled_confidence, detail) —— 任何异常均返回 (final_confidence, {"error": ...}) 即无影响。
    """
    detail = {"enabled": False, "applied": False, "shadow": bool(shadow)}
    try:
        if not shadow and not getattr(settings, "DEBATE_RING_ENABLED", False):
            detail["enabled"] = False
            return float(final_confidence), detail
        detail["enabled"] = True

        # HOLD 无需缩放（没有要开的交易）
        if final_decision not in ("BUY", "SELL"):
            detail["skipped"] = "HOLD"
            return float(final_confidence), detail

        floor = float(getattr(settings, "DEBATE_RING_FLOOR", 0.80))
        disagree_pen = float(getattr(settings, "DEBATE_RING_DISAGREEMENT_PENALTY", 0.15))
        risk_pen = float(getattr(settings, "DEBATE_RING_RISK_PENALTY", 0.05))
        max_pen = float(getattr(settings, "DEBATE_RING_MAX_PENALTY", 0.20))

        # ── 牛熊研究员：把各路信号按对终裁的视角分类 ──
        panels = [
            ("DS", _view_of(ctx.get("ds_final"), final_decision)),
            ("HY", _view_of(ctx.get("hy_final"), final_decision)),
            ("Chronos", _view_of(ctx.get("chronos_dir"), final_decision)),
            ("融合票", _view_of(ctx.get("ts_fusion_dir"), final_decision)),
            ("视觉", _view_of(ctx.get("vision_dir"), final_decision)),
            ("副驾", _view_of(ctx.get("copilot_dir"), final_decision)),
            ("SMC订单流", _smc_view(ctx.get("smc_bias"), final_decision)),
        ]
        bull = sum(1 for _, v in panels if v == 1)
        bear = sum(1 for _, v in panels if v == -1)
        neutral = sum(1 for _, v in panels if v == 0)
        detail["bull"] = bull
        detail["bear"] = bear
        detail["neutral"] = neutral

        # ── 主持人综合①：分歧度惩罚 ──
        directional = bull + bear
        if directional == 0:
            disagree_part = 0.0
        elif bull == 0 and bear > 0:
            disagree_part = disagree_pen                      # 全部有方向票都反对 → 满额
        else:
            disagree_part = disagree_pen * (bear / directional)  # 反对占比越高惩罚越重
        detail["disagree_penalty"] = round(disagree_part, 4)

        # ── 风控多视角审议团：风险偏高额外谨慎 ──
        risk_flags = []
        _risk_level = str(ctx.get("risk_level", ""))
        if _risk_level == "extreme":
            # 极端风险已被其他门 HOLD，这里仅记录
            risk_flags.append("extreme")
        elif _risk_level == "high":
            risk_flags.append("high")
            risk_part = risk_pen
        else:
            risk_part = 0.0
        # 高波动体制（未被判 extreme 但仍需谨慎）
        if str(ctx.get("market_regime", "")) in ("volatile", "高波动", "极端"):
            if "high" not in risk_flags:
                risk_flags.append("volatile")
                risk_part = max(risk_part, risk_pen * 0.5)
        detail["risk_flags"] = risk_flags
        detail["risk_penalty"] = round(risk_part, 4)

        # ── 主持人综合②：总惩罚封顶 → 乘性缩权 ──
        total_pen = min(max_pen, disagree_part + risk_part)
        scale = max(floor, 1.0 - total_pen)
        detail["scale"] = round(scale, 4)
        detail["total_penalty"] = round(total_pen, 4)

        new_conf = float(final_confidence) * scale
        detail["applied"] = new_conf < float(final_confidence) - 1e-9
        detail["original_confidence"] = round(float(final_confidence), 4)
        detail["scaled_confidence"] = round(new_conf, 4)
        return new_conf, detail
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[辩论环] 异常降级(无影响): {e}")
        detail["error"] = str(e)
        return float(final_confidence), detail


def _smc_view(smc_bias: str, final_decision: str) -> int:
    """SMC 机构订单流偏置 → 对终裁的视角。bullish 支持 BUY/反对 SELL；bearish 反之。"""
    b = str(smc_bias or "").lower()
    if b == "bullish":
        return 1 if final_decision == "BUY" else (-1 if final_decision == "SELL" else 0)
    if b == "bearish":
        return 1 if final_decision == "SELL" else (-1 if final_decision == "BUY" else 0)
    return 0
