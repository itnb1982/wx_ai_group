"""
决策快照 — DebateDecision → 溯源用结构化字典（单一权威序列化点）

★ Phase 4（2026-08-08）：为什么需要这个模块？

  溯源链此前断在三个地方，而且断法各不相同：
    1) 落库断链：trades 表只存了 6 个字段，Chronos 三票/Q 分/分位全塞进
       debate_summary 一段自由文本；
    2) 接口断链：/dashboard/decision 与 /ai-flow 各自手写一份 dict，字段各挑各的，
       chronos_vote / q_score / p10 / p50 一个都没透传；
    3) **命名断链**（最隐蔽）：前端 MetaQualityPanel 消费的是 `q` / `chronos_dir`，
       后端 DebateDecision 里叫 `q_score` / `chronos_vote`。同一个东西两个名字，
       于是谁也不敢直接对接，只能各算各的 —— 这正是 /ai-flow 旁路重算 meta_quality
       的根源，也导致「前端看到的 Q」和「下单时用的 Q」来自两次不同推理。

  所以序列化必须收口到一个函数。任何要把决策交给外部（DB / API / 日志）的地方
  都从这里取，字段名以本模块为准，不允许再各写一份。

★ 语义契约（有测试钉死，改动前先读 tests/test_decision_provenance.py）：
  `chronos_weight == 0` 表示「Chronos 没参与加权」（服务没跑起来/预测不可用），
  和「Chronos 投了 HOLD（建议观望）」是**两件完全不同的事**，票面却都是 HOLD。
  前端必须能区分：前者要显示"本地模型降级"，后者要显示"本地模型建议观望"。
  为此本模块显式输出 `chronos_available` 布尔量，不让前端自己去猜。
"""
from __future__ import annotations

import json
from typing import Any, Optional


def _f(v: Any) -> Optional[float]:
    """安全转 float。None/空/非数一律返回 None（不要把缺失伪装成 0.0）。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / inf 一律当缺失：它们会让 JSON 变成非法字面量，前端 JSON.parse 直接炸
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _s(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return default


def build_decision_snapshot(decision: Any, degrade_level: str = "") -> dict:
    """把 DebateDecision（或任何鸭子类型对象）摊成溯源快照。

    对入参极度宽容：全程 getattr 带默认值，任何字段缺失都不抛。
    原因很实际 —— 调用点在下单主链路上，快照是"附加信息"，
    绝不能因为某个字段没赋值就把开仓搞失败。
    """
    if decision is None:
        return {}

    g = lambda name, dflt=None: getattr(decision, name, dflt)  # noqa: E731

    chronos_weight = _f(g("chronos_weight", 0.0)) or 0.0
    p10 = _f(g("chronos_p10"))
    p50 = _f(g("chronos_p50"))
    # tp_ceiling 语义上就是 P90（预测上界），历史命名沿用至今。
    # 这里同时给出两个键：p90 供前端画分位带，chronos_tp_ceiling 保持向后兼容。
    p90 = _f(g("chronos_tp_ceiling"))

    snap = {
        # ── 最终裁决 ──
        "decision": _s(g("decision", "HOLD"), "HOLD"),
        "confidence": _f(g("confidence", 0.0)) or 0.0,
        "risk_level": _s(g("risk_level", "")),
        "consensus": _s(g("consensus", "")),
        "plain_summary": _s(g("plain_summary", "")),
        # ── 三票（前端辩论擂台按这个渲染，不再只画两条气泡）──
        "votes": {
            "deepseek": {
                "vote": _s(g("deepseek_vote", "HOLD"), "HOLD"),
                "weight": _f(g("deepseek_weight", 0.0)) or 0.0,
                # ★ 2026-08-15 审计P2修复：补真实置信（此前只存权重，PF/准确率归因失真）
                "confidence": _f(g("deepseek_confidence")),
            },
            "hunyuan": {
                "vote": _s(g("hunyuan_vote", "HOLD"), "HOLD"),
                "weight": _f(g("hunyuan_weight", 0.0)) or 0.0,
                "confidence": _f(g("hunyuan_confidence")),
            },
            "chronos": {
                "vote": _s(g("chronos_vote", "HOLD"), "HOLD"),
                "weight": chronos_weight,
                # ★ 见模块头「语义契约」：靠这个布尔区分"降级"与"观望"
                "available": chronos_weight > 0.0,
            },
            # ★ 2026-08-15 审计P2修复：融合票（第四票）此前完全未进快照 → 无法回放
            #   fusion_v2 的 4 模型聚合（Chronos/TimesFM/Moirai/NumPy 加权）。补全
            #   dir/weight/conf/agree/hit_avg/models，使历史单可忠实重放融合决策。
            "fusion": {
                "vote": _s(g("ts_fusion_dir", "HOLD"), "HOLD"),
                "weight": _f(g("ts_fusion_weight", 0.0)) or 0.0,
                "confidence": _f(g("ts_fusion_conf", 0.0)) or 0.0,
                "agree": bool(g("ts_fusion_agree", False)),
                "hit_avg": _f(g("ts_fusion_hit_avg", 0.0)) or 0.0,
                "models": int(g("ts_fusion_models") or 0),
                "available": (_f(g("ts_fusion_weight", 0.0)) or 0.0) > 0.0,
                "note": _s(g("ts_fusion_note", ""), ""),
            },
            # ★ 2026-08-14 视觉模型第四票（加法增强，非闸门）：H4/M15/M5 三帧 K线结构识别。
            #   available 用 weight>0 区分"参与"与"不可用"；note 透出 H4/M15/M5 单周期方向。
            "vision": {
                "vote": _s(g("vision_dir", "HOLD"), "HOLD"),
                "weight": _f(g("vision_weight", 0.0)) or 0.0,
                "confidence": _f(g("vision_conf", 0.0)) or 0.0,
                "h4": _s(g("vision_h4_dir", "HOLD"), "HOLD"),
                "m15": _s(g("vision_m15_dir", "HOLD"), "HOLD"),
                "m5": _s(g("vision_m5_dir", "HOLD"), "HOLD"),
                "m5_conf": _f(g("vision_m5_conf", 0.0)) or 0.0,
                "agree": bool(g("vision_agree", False)),
                "available": (_f(g("vision_weight", 0.0)) or 0.0) > 0.0,
                "note": _s(g("vision_note", ""), ""),
            },
            # ★ 2026-08-14 Qwen3-8B 常态确认型副驾第五票（加法增强，非闸门）。
            #   available 用 weight>0 区分"参与"与"不可用"；note 透出放行原因。
            "copilot": {
                "vote": _s(g("copilot_dir", "HOLD"), "HOLD"),
                "weight": _f(g("copilot_weight", 0.0)) or 0.0,
                "confidence": _f(g("copilot_conf", 0.0)) or 0.0,
                "agree": bool(g("copilot_agree", False)),
                "available": (_f(g("copilot_weight", 0.0)) or 0.0) > 0.0,
                "note": _s(g("copilot_note", ""), ""),
            },
        },
        "chronos_agree": bool(g("chronos_agree", False)),
        # ★ 2026-08-15 审计P2修复：补校准置信（诚实展示层）与门触发统计（可回放拦截率）。
        "calibrated_confidence": _f(g("calibrated_confidence")),
        "gate_stats": dict(g("gate_stats") or {}),
        # ── 本地质量陪审团 ──
        "quality": {
            "q_score": _f(g("q_score")),
            "regime": _s(g("quality_regime", "")),
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "chronos_tp_ceiling": p90,   # 向后兼容旧前端字段名
        },
        # ── AI 自主仓位意图（v5）──
        "position": {
            "intent": _s(g("position_intent", "open"), "open"),
            "target_risk_pct": _f(g("target_risk_pct")),
            "portfolio_state": _s(g("portfolio_state", "")),
        },
        # ── 篮子级 AI 持仓管理（2026-08-17 · 用户铁律：开完仓核心任务=维护持仓）──
        #   DS/HY position_action 融合 + 连续 2 轮确认 → 执行层消费 close_all/trim。
        #   前端「AI 工作剧场」据此展示"AI 在护仓"（hold/trim/close_all + 确认态）。
        "basket": {
            "action": _s(g("basket_action", "hold"), "hold"),
            "confidence": _f(g("basket_action_conf", 0.0)) or 0.0,
            "reason": _s(g("basket_action_reason", "")),
            "confirmed": bool(g("basket_action_confirmed", False)),
            "confirm_note": _s(g("basket_action_confirm_note", "")),
        },
        # ── 进场价位对齐（2026-08-14 根治「AI 想 4329 开空、执行 4315 市价开」）──
        #   entry.price = AI 期望入场价（双脑 JSON entry_price 优先，回退解析 reasoning）；
        #   entry.style = "market"(立即市价) / "limit"(等回到 zone 再点火)。
        #   审计可直接对照「AI 想要的价格」与「实际成交 open_price」，验证是否还丢价位。
        "entry": {
            "price": _f(g("entry_price")),
            "style": _s(g("entry_style", "market"), "market"),
        },
        # ── 本地校对员（Qwen3-8B）：这单开出去前有没有被本地模型核对过 ──
        #   status 三态必须原样透出，前端据此区分：
        #     skipped → 灰色「未校对」（本地模型没跑），
        #     clean   → 绿色「已核对」，
        #     issues  → 金色「有疑点」（仅提示，未拦截）。
        #   合并 skipped 与 clean 会让"模型挂了"长得像"一切正常"。
        "proofread": {
            "status": _s(g("proofread_status", "skipped"), "skipped"),
            "issues": list(g("proofread_issues") or []),
            "severity": _s(g("proofread_severity", "none"), "none"),
            "latency_ms": _f(g("proofread_latency_ms")),
            # ★ Phase 9.1 闭环断路器：标记这单是否被本地校对员按住（而非 AI 自己观望）
            "blocked": bool(g("proofread_blocked", False)),
            "block_reason": _s(g("block_reason", "")),
            # ★ 2026-08-11：措施文案（"做了什么"），前端展开展示
            "action": _s(g("proofread_action", "")),
        },
        # ── 方向终审器：统计信号是否与云端方向强冲突 ──
        #   model=numpy 表示当前是纯规则兜底；未来可=chronos-2/timesfm-2.5 等。
        "direction_guard": {
            "blocked": bool(g("direction_guard_blocked", False)),
            "conflict": _s(g("direction_guard_conflict", "none"), "none"),
            "score": _f(g("direction_guard_score")),
            "reason": _s(g("direction_guard_reason", "")),
            "model": _s(g("direction_guard_model", "numpy"), "numpy"),
            # ★ 2026-08-15 第三批#4 纯加法：规则③回放特征（缺省 None，不污染旧单）。
            #   审计可直接用 price_to_ma_z / z_avg_5 / rsi14 重算
            #   「趋势反向 + |Z|>Z_MINOR 才 major」是否对历史单成立，无需重启前向计数。
            "price_to_ma_z": _f(g("direction_guard_price_to_ma_z")),
            "z_avg_5": _f(g("direction_guard_z_avg_5")),
            "rsi14": _f(g("direction_guard_rsi14")),
        },
        # ── 平台降级档位：这单是在什么系统状态下开出去的 ──
        "degrade_level": _s(degrade_level or current_degrade_level()),
        # ★ 2026-08-17 辩论环 shadow 埋点：开关关闭时也计算"若开启会缩权到多少"，
        #   落库供 1-2 周后与真实基线 A/B 对比（无痛开启，不需要二次部署）。
        "debate_ring_shadow": dict(g("debate_ring_shadow") or {}),
    }
    return snap


def current_degrade_level() -> str:
    """取当前平台降级档位。监控不可用时返回空串而不是假装 L0。

    这里刻意不 import 顶层：降级车道是 Phase 6 的附加能力，
    溯源模块不该因为它出问题而连累（同 debate_engine 的处理方式）。
    """
    try:
        from app.services.platform_health_monitor import current_level

        return str(current_level().name)
    except Exception:  # noqa: BLE001
        return ""


def snapshot_to_json(snap: dict) -> Optional[str]:
    """快照落库用。序列化失败返回 None —— 宁可少存一份快照，也不能让写库崩。"""
    if not snap:
        return None
    try:
        return json.dumps(snap, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return None


def flat_columns(snap: dict) -> dict:
    """抽出要平铺进 trades 表的三个可筛列（其余进 decision_snapshot JSON）。"""
    if not snap:
        return {"chronos_vote": None, "q_score": None, "degrade_level": None}
    votes = snap.get("votes") or {}
    chronos = votes.get("chronos") or {}
    quality = snap.get("quality") or {}
    return {
        "chronos_vote": chronos.get("vote") or None,
        "q_score": quality.get("q_score"),
        "degrade_level": snap.get("degrade_level") or None,
    }
