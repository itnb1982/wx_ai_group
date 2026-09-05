# -*- coding: utf-8 -*-
"""本地时序模型「信号源参考面板」API。

仅向外暴露只读快照。本路由绝不触发任何下单 / 风控 / 决策动作——
它就是个「模型能力观测窗」，供用户开盘对照实时行情看准不准。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from loguru import logger

from app.models.user import User
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/ts-reference", tags=["信号源参考"])


@router.get("/snapshot")
def snapshot(user: User = Depends(get_current_user)):
    """多模型信号源参考快照（只读，未接入交易系统）。"""
    from app.services.ts_reference_service import get_service

    svc = get_service()
    svc.ensure_started()
    snap = svc.get_snapshot()
    # ★ 2026-08-11 全盘可视化：把融合票也一并推给参考面板，
    #   让「融合票算式 + 实时权重条」与 4 模型卡片同源同刻，无需前端再并发 ai-flow。
    #   只读聚合，不触发任何推理（融合票复用参考面板 snapshot，零额外显存）。
    try:
        from app.services.fusion_service import get_service as get_fusion

        fv = get_fusion().get_fusion_vote()
        snap["fusion_vote"] = {
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
        logger.warning(f"[ts-reference] 融合票聚合失败: {e}")
        snap["fusion_vote"] = {"available": False, "note": "融合票不可用"}
    return snap


@router.get("/{model_name}/selftest")
def selftest_model(model_name: str, user: User = Depends(get_current_user)):
    """单个模型自检：检查权重/venv 是否就绪，并跑一次快速推理。

    模型名需 URL 编码，例如 Chronos-2(120M) -> Chronos-2%28120M%29。
    """
    from app.services.ts_reference_service import get_service

    svc = get_service()
    svc.ensure_started()
    return svc.selftest_model(model_name)
