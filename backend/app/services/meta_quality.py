"""
Meta-Labeling · 本地信号质量陪审团（v4 方案核心）

★ 思想（López de Prado Meta-Labeling + 用户实盘观察）：
  用户实盘截图实证：Meta-Labeling·LSTM/TCN 以 76% 强烈看空，但云 DeepSeek 仍固执买多 → 亏损。
  根因：语义大脑的"意图"缺乏本地时序模型的"质量校验"。
  本模块 = 本地质量陪审团：不预测方向（方向交给 DeepSeek/SMC），只回答一个问题——
  「当前这笔信号，质量高不高、该让利润跑还是该啃头皮？」

★ 输出：
  - q          ∈ [0,1]  信号质量分
  - regime     HIGH(≥0.7)/MID(0.5~0.7)/LOW(0.35~0.5)/VERY_LOW(<0.35)
  - direction  体制方向（BUY/SELL/NEUTRAL）
  - chronos_tp_ceiling  动态止盈天花板（HIGH/MID 用 Chronos P90 末价；LOW 不用）
  - notes      判定依据（供前端"AI 工作可视化"展示，客户一看就懂）

★ 红线（提准非拦截，绝不腰斩交易数）：
  - 只有 VERY_LOW 才建议"观望"（极少触发，仅山顶接飞刀/极端矛盾）；
  - LOW 仍给信号，只是"啃头皮"（紧 ATR 快进快出），不减交易笔数；
  - 正常 Q 不影响开仓，只影响"仓位 + 止盈方式"。

★ 多账号优先：纯函数，输入 market_data（行情主号共享数据），与账号数 N 解耦。
"""
import logging

logger = logging.getLogger("meta_quality")

# regime 阈值（与 v4 方案文档一致）
HIGH_TH = 0.70
MID_TH = 0.50
LOW_TH = 0.35

# 不确定性带宽阈值（带宽>2.5% 视为高不确定性，降质）
UNCERT_HIGH = 0.025
# 价格延伸度阈值（|Z|>2.5 视为统计罕见延伸，降质防接飞刀）
EXT_Z_TH = 2.5


def _extract_close(market_data: dict):
    """从快照提取收盘价序列（优先 M15>H1>M5，需≥32 根）。

    快照结构：timeframes[tf]["closes"]=收盘价列表（市场快照已存入），
    兼容旧式 timeframes[tf]["bars"]=K线列表（含 close 字段）。
    """
    tfs = (market_data or {}).get("timeframes", {}) or {}
    for tf in ("M15", "H1", "M5"):
        d = tfs.get(tf, {}) or {}
        closes = d.get("closes")
        if not closes:
            # 兼容：原始 K 线列表
            bars = d.get("bars", []) or []
            if isinstance(bars, list):
                closes = [float(b.get("close", 0)) for b in bars if isinstance(b, dict) and b.get("close")]
        if closes and len(closes) >= 32:
            return [float(c) for c in closes]
    return None


def _evaluate_core(market_data: dict, chronos: dict | None, used_cov: list,
                   cross: dict | None = None) -> dict:
    """共享评分逻辑（同步/异步两个入口都走这里）。"""
    # ── 2) SMC / Regime 体制感知 ──
    regime = (market_data or {}).get("regime", {}) or {}
    _reg = regime.get("regime", "unknown") if isinstance(regime, dict) else "unknown"
    bias = str((regime.get("direction_bias", "neutral") if isinstance(regime, dict) else "neutral") or "neutral").lower()
    ext_z = float((regime.get("extension_z", 0.0) if isinstance(regime, dict) else 0.0) or 0.0)
    at_top = bool(regime.get("at_stale_top", False) if isinstance(regime, dict) else False)
    at_bottom = bool(regime.get("at_stale_bottom", False) if isinstance(regime, dict) else False)

    notes = []
    q = 0.50  # 中性基准

    # ── 3) 体制基础分 ──
    trend_dir = "NEUTRAL"
    if _reg in ("trend_up", "strong_uptrend"):
        q += 0.15; trend_dir = "BUY"
    elif _reg in ("trend_down", "strong_downtrend"):
        q += 0.15; trend_dir = "SELL"
    elif _reg == "range":
        q += 0.00; trend_dir = "NEUTRAL"
    else:  # volatile / unknown
        q -= 0.10; trend_dir = "NEUTRAL"
        notes.append("体制震荡/未知→降质")

    # ── 4) Chronos 分位数方向融合 ──
    chronos_dir = (chronos or {}).get("direction", "NEUTRAL")
    chronos_unc = (chronos or {}).get("uncertainty", None)
    if chronos is not None:
        if chronos_dir == trend_dir and trend_dir != "NEUTRAL":
            q += 0.15
            notes.append(f"Chronos分位数方向({chronos_dir})与体制同向→加分")
        elif chronos_dir != "NEUTRAL" and trend_dir != "NEUTRAL" and chronos_dir != trend_dir:
            q -= 0.20
            notes.append(f"Chronos方向({chronos_dir})与体制({trend_dir})反向→显著降质")
        if chronos_unc is not None and chronos_unc > UNCERT_HIGH:
            q -= 0.10
            notes.append(f"Chronos不确定性高({chronos_unc:.1%})→降质")

    # ── 5) 位置风险（山顶/谷底/延伸）──
    if at_top:
        q -= 0.20
        notes.append("价格处历史山顶→接飞刀风险高")
    if at_bottom:
        q += 0.05
        notes.append("价格处历史谷底→反弹质量偏高")
    if abs(ext_z) > EXT_Z_TH:
        q -= 0.10
        notes.append(f"价格严重延伸(Z={ext_z:.1f})→降质防接飞刀")

    # ── 5.5) TimesFM 风险区间交叉验证（2026-08-17 · 模型科学规划）──
    #   方向预测复测证明时序模型不优于随机（TimesFM acc50.0%），但独立区间
    #   估计是有效信息：两模型区间一致 → 不确定性低 → 质量分微升（提准）；
    #   显著分歧 → 不确定性高 → 质量分降 + 止盈收紧（降损）。加法，非拦截。
    cross_div = None
    cross_agreement = None
    if cross is not None:
        cross_div = cross.get("divergence")
        cross_agreement = cross.get("agreement")
        if cross_agreement == "high":
            q += 0.05
            notes.append("双时序模型区间一致(TimesFM/Chronos)→质量提升")
        elif cross_agreement == "low":
            q -= 0.10
            notes.append("双时序模型区间显著分歧→不确定性高→降质")
        elif cross_agreement == "mid":
            notes.append("双时序模型区间中等分歧→不确定性中")

    q = max(0.0, min(1.0, q))

    # ── 6) regime 映射 ──
    if q >= HIGH_TH:
        qregime = "HIGH"
    elif q >= MID_TH:
        qregime = "MID"
    elif q >= LOW_TH:
        qregime = "LOW"
    else:
        qregime = "VERY_LOW"

    # ── 7) 动态止盈天花板（仅 HIGH/MID 启用，让利润奔跑到 P90）──
    tp_ceiling = None
    if chronos is not None and qregime in ("HIGH", "MID"):
        tp_ceiling = chronos.get("p90_final")
        # ★ 交叉验证分歧大 → 止盈收紧（取两模型较保守的一端，防过度乐观追价）
        if cross_agreement == "low" and cross is not None and tp_ceiling is not None:
            _t_p90 = cross.get("t_p90")
            if _t_p90 is not None:
                if _t_p90 < tp_ceiling:
                    tp_ceiling = _t_p90
                notes.append("区间分歧→止盈天花板收紧至TimesFM端")

    # 把 Chronos 未来分位数序列透传给前端，用于 Sparkline 动画与可观测性
    ret = {
        "q": round(q, 3),
        "regime": qregime,
        "direction": trend_dir,
        "chronos_dir": chronos_dir,
        "uncertainty": chronos_unc,
        "chronos_tp_ceiling": tp_ceiling,
        "chronos_p50_final": chronos.get("p50_final") if chronos else None,
        "chronos_available": chronos is not None,
        "covariates": used_cov,
        "cross_ts": {
            "available": cross is not None,
            "divergence": cross_div,
            "agreement": cross_agreement,
            "note": (cross or {}).get("note"),
        } if cross is not None or cross_div is not None else None,
        "notes": notes,
    }
    if chronos is not None:
        ret.update({
            "p10": chronos.get("p10"),
            "p50": chronos.get("p50"),
            "p90": chronos.get("p90"),
            # ★ 收口：交易逻辑侧只需标量末价（与 p90_final 一致），避免 SELL 方向
            #   smart_exit 对列表调 float() 抛 TypeError 被静默吞掉（见 smart_exit 修复）。
            "p10_final": chronos.get("p10_final"),
            "p90_final": chronos.get("p90_final"),
            "last_price": chronos.get("last_price"),
        })
    return ret


def _run_chronos_forecast(market_data: dict):
    """抽离 Chronos 同步调用，供同步/异步两个入口复用。"""
    close = _extract_close(market_data)
    if close is None or len(close) < 32:
        return None, []
    from app.services.chronos_service import ChronosEngine
    from app.services.covariates import get_cross_asset_covariates
    cov = get_cross_asset_covariates(len(close), market_data)
    used_cov = list(cov.keys()) if cov else []
    if used_cov:
        logger.debug(f"[MetaQuality] 注入跨资产协变量: {used_cov}")
    chronos = ChronosEngine.get().forecast(close, prediction_length=24, num_samples=20, covariates=cov)
    return chronos, used_cov


def _run_cross_validate(market_data: dict, chronos: dict | None):
    """TimesFM 交叉验证（静默降级，超时/失败返回 None）。"""
    if chronos is None:
        return None
    try:
        close = _extract_close(market_data)
        if close is None or len(close) < 32:
            return None
        from app.services.ts_cross_validate import cross_validate
        return cross_validate(close, chronos)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[MetaQuality] TimesFM 交叉验证降级: {e}")
        return None


async def _run_cross_validate_async(market_data: dict, chronos: dict | None):
    """异步入口：交叉验证推理 offload 到线程池（内部已有超时保护）。"""
    if chronos is None:
        return None
    try:
        import asyncio
        return await asyncio.to_thread(_run_cross_validate, market_data, chronos)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[MetaQuality] TimesFM 交叉验证异步降级: {e}")
        return None


def evaluate_meta_quality(market_data: dict) -> dict:
    """
    融合 Chronos 分位数 + SMC/Regime → 质量分 Q + 止盈 regime。
    总是返回 dict（即使 Chronos 不可用也返回基于 SMC/Regime 的降级评估）。
    供交易循环等后台线程同步调用。
    """
    chronos, used_cov = None, []
    try:
        chronos, used_cov = _run_chronos_forecast(market_data)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[MetaQuality] Chronos 不可用，降级: {e}")
    cross = _run_cross_validate(market_data, chronos)
    return _evaluate_core(market_data, chronos, used_cov, cross)


async def evaluate_meta_quality_async(market_data: dict) -> dict:
    """异步入口：Chronos 推理 offload 到线程池，不阻塞 uvicorn 事件循环。

    2026-08-09 修复：dashboard /ai-flow 此前同步跑 Chronos，堵住事件循环，
    导致 /api/health 心跳 3s 超时 → 前端全屏红条。
    """
    chronos, used_cov = None, []
    try:
        close = _extract_close(market_data)
        if close is not None and len(close) >= 32:
            from app.services.chronos_service import ChronosEngine
            from app.services.covariates import get_cross_asset_covariates
            cov = get_cross_asset_covariates(len(close), market_data)
            used_cov = list(cov.keys()) if cov else []
            if used_cov:
                logger.debug(f"[MetaQuality] 注入跨资产协变量: {used_cov}")
            chronos = await ChronosEngine.get().forecast_async(
                close, prediction_length=24, num_samples=20, covariates=cov
            )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[MetaQuality] Chronos 不可用，降级: {e}")
    cross = await _run_cross_validate_async(market_data, chronos)
    return _evaluate_core(market_data, chronos, used_cov, cross)


def quality_regime_from_q(q: float) -> str:
    """单纯由 Q 值映射 regime（供测试/其他模块复用）。"""
    if q >= HIGH_TH:
        return "HIGH"
    if q >= MID_TH:
        return "MID"
    if q >= LOW_TH:
        return "LOW"
    return "VERY_LOW"
