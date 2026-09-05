"""
XAU/USD 万象Ai — 智能手数自适应引擎
=========================================
目标：让 AI 根据【本金额度 + 行情趋势强度 + 同方向持仓数 + 信号置信度】动态计算手数，
      而不是死板的"按余额百分比"。

设计依据（≥3家交叉验证）：
1. PMTS institutional framework: volatility-adjusted sizing
   (titansystempro.com, 2026-04-20)
   → position size scales inversely with ATR; high-vol 缩手, low-vol 加码
2. AlphaMind AI: signal-strength modulated sizing
   (alphamind-ai.com, 2026)
   → strong signal → modestly upsize; weak signal → 0.5% instead of 1%
3. Tradewink + TheLedgerMind: portfolio heat management
   → same-direction correlation = same bet, decay each subsequent entry

公式（取上述三家交集 + 本地化）：
  base_risk$   = balance × max_risk_per_trade_pct / 100
  size_raw     = base_risk$ / (atr × 100)            # 黄金 1手 1点 = $100
  size_raw    *= volatility_factor                    # 强趋势加码
  size_raw    *= signal_strength_mult                # 信号强度调制（非线性：低置信强惩罚、高置信不缩）
  size_raw    *= (1.0 - same_direction_decay)^N      # 同向第N单衰减
  size_final   = clamp(size_raw, min_lot, max_lot)

sizing_mode = 'fixed' 时退化为原"余额百分比"算法（向后兼容）。
"""
from typing import Optional
from datetime import datetime
from loguru import logger

from app.services.capital_authority import effective_capital, resolve_lot_bounds


def _session_quality_mult() -> float:
    """
    时段质量乘数 — 基于全球调研（≥3源交叉验证）：
    1. ratioxtrade.com: London-NY overlap (13:00-17:00 UTC) delivers cleanest XAUUSD breakouts
    2. pro-scalper.com: Asian range breakout + London open = highest probability gold entries
    3. algomatrix.trade: London+NY (07:00-20:00 UTC) produces 3-4 clean signals vs Asian noise

    时段划分（GMT+8 本机时间）：
      21:00-01:00  伦敦-纽约重叠（黄金4小时） → ×1.25  加码25%
      15:00-21:00  伦敦单盘                    → ×1.10  加码10%
      01:00-03:00  纽约尾盘                    → ×1.05  微加
      03:00-07:00  凌晨清淡                    → ×0.90  缩手10%
      07:00-15:00  亚盘                        → ×1.00  默认
    """
    h = datetime.now().hour
    if 21 <= h or h < 1:      # 21:00-01:00 伦敦-纽约重叠
        return 1.25
    elif 15 <= h < 21:         # 15:00-21:00 伦敦单盘
        return 1.10
    elif 1 <= h < 3:           # 01:00-03:00 纽约尾盘
        return 1.05
    elif 3 <= h < 7:           # 03:00-07:00 凌晨清淡
        return 0.90
    else:                      # 07:00-15:00 亚盘
        return 1.00


def _cfg(strategy, key: str, default):
    """统一读取策略配置（dict 或 ORM）"""
    if isinstance(strategy, dict):
        return strategy.get(key, default)
    return getattr(strategy, key, default) or default


def _degrade_mult() -> float:
    """★ Phase 6 降级手数系数（L0=1.0 / L1=0.7 / L2=0.4 / L3=0.0）。

    放在 sizing 这个**唯一手数权威入口**里，而不是散在各下单点：
    只要经过本函数算出的手数，就一定已经按当前平台能力缩过手，
    杜绝「某条下单路径忘了乘系数」这类必然会发生的遗漏。

    监控自身故障时返回 1.0（不因为健康检查挂了就误砍全体客户手数）。
    """
    try:
        from app.services.platform_health_monitor import degrade_enabled, lot_multiplier

        if not degrade_enabled():
            return 1.0
        m = float(lot_multiplier())
        return m if 0.0 <= m <= 1.0 else 1.0
    except Exception:
        return 1.0


def compute_intelligent_size(
    *,
    balance: float,
    atr: float,
    signal_confidence: float,
    same_direction_count: int,
    strategy,
    ai_target_risk_pct: float = None,
    adx: float = None,
) -> dict:
    """
    计算单笔手数 + 决策详情

    ★ 2026-08-10 v6：新增 `adx` 参数（实时 H1 ADX 趋势强度），
      作为独立"趋势强弱乘数"调制手数 —— 用户需求：底线(min_lot)~红线(max_lot)
      之间按趋势强弱自动开单手数（强趋势加码、震荡缩手），而不是恒定一个值。

    返回 dict:
      - lots: 最终手数（已 clamp）
      - raw_lots: 计算前原始值
      - reason: 决策原因（用于审计+前端展示）
      - components: 各乘数明细

    ★ 2026-08-07 v5：ai_target_risk_pct 由 AI 自主决策传入（position_intent=reduce 时砍半），
      覆盖策略固定 max_risk_per_trade_pct，实现"AI 主动收缩总敞口"。None=沿用策略固定值。
      有效性区间 [0.1, 10]%，越界则忽略（防 AI 抽风把风险拉爆）。
    """
    # ===== 读取配置 =====
    sizing_mode = _cfg(strategy, "sizing_mode", "smart")
    max_risk_pct = float(_cfg(strategy, "max_risk_per_trade_pct", 2.0))
    # AI 自主风险占比覆盖（仅当合理区间）
    _ai_risk = None
    if ai_target_risk_pct is not None:
        try:
            _ai_risk = float(ai_target_risk_pct)
            if not (0.1 <= _ai_risk <= 10.0):
                _ai_risk = None
        except (TypeError, ValueError):
            _ai_risk = None
    if _ai_risk is not None:
        max_risk_pct = _ai_risk
    base_capital = float(_cfg(strategy, "base_capital", 0) or 0)
    capital_source = _cfg(strategy, "capital_source", "live")
    vol_factor = float(_cfg(strategy, "volatility_factor", 1.0))
    decay = float(_cfg(strategy, "same_direction_decay", 0.5))
    min_lot = float(_cfg(strategy, "min_lot_per_trade", 0.01))
    max_lot = float(_cfg(strategy, "max_lot_per_trade", 1.0))
    max_pos_lots = float(_cfg(strategy, "max_position_lots", 1.0))

    # ★ 手数本金来源（账户私有·不继承主号）—— 权威链见 capital_authority.py：
    #   input_capital（客户输入，权限最高） > base_capital(manual) > balance(live 默认)
    #   向后兼容：未配置 capital_source（旧库/旧 dict）一律按 'live' 处理。
    #   ⚠ 禁止在此处内联判断本金来源，必须走 effective_capital()（V6 §4.2 单一权威）
    _cap = effective_capital(strategy, balance=balance)
    effective_balance = _cap.value
    capital_source_used = _cap.label

    # ★ 风控上限按本金自适应（国际调研精髓 ≥3源交叉验证: investmentkit / tradewink /
    #    passcmt / arrowalgo / whatsriskmanagement 一致结论）：
    #   Fixed Fractional 手数已随余额自动缩放（手数=balance×risk%÷SL距离$），但 max_lot /
    #   max_position_lots 上限写死会令大本金账号无辜拒单（单笔就触顶总手数上限）。
    #   auto 模式下上限随本金等比缩放(封顶防极端)，小本金自动收紧(更保守防爆仓)。
    #   组合总风险 = N笔 × balance × risk%，由 risk% 锁定；放大上限不增破产风险
    #   （不靠写死上限防爆仓，靠风险百分比铁律），仅防单笔过大滑点才封顶。
    #   ⚠ 禁止在此处内联缩放，必须走 resolve_lot_bounds()（V6 §4.3 单一权威）
    _bounds = resolve_lot_bounds(strategy, effective_balance)
    sizing_scale_mode = _bounds.mode
    min_lot = _bounds.min_lot
    max_lot = _bounds.max_lot
    max_pos_lots = _bounds.max_position_lots

    components = {
        "balance": balance,
        "base_capital": base_capital,
        "capital_source": capital_source_used,
        "effective_balance": effective_balance,
        "max_risk_pct": max_risk_pct,
        "atr": atr,
        "signal_confidence": signal_confidence,
        "same_direction_count": same_direction_count,
        "volatility_factor": vol_factor,
        "same_direction_decay": decay,
        "sizing_mode": sizing_mode,
    }

    # ★ Phase 6：平台降级手数系数（在最后统一施加，见文件末 _apply_degrade 注释）
    deg = _degrade_mult()
    components["degrade_mult"] = deg

    # ===== 退化模式（兼容旧逻辑） =====
    if sizing_mode == "fixed" or atr <= 0 or not (effective_balance > 0):
        risk_amount = effective_balance * (max_risk_pct / 100.0)
        sl_points = max(atr, 20.0)
        # ★ 2026-08-15 审计P1修复：原 `* 1.0` 缺 ×100 换算（与 smart 分支
        #   `/(sl_points*100)` 同口径：ATR 美元数值 ×100 转点数，1 手 1 点=$1）。
        #   缺失导致 fixed 模式手数放大 100 倍、恒打满 max_lot，风险预算失效。
        lots = risk_amount / (sl_points * 100.0)
        lots = max(min(lots, max_pos_lots, max_lot), min_lot)
        lots, _deg_note = _apply_degrade(lots, deg, min_lot)
        return {
            "lots": round(lots, 2),
            "raw_lots": round(lots, 2),
            "reason": "固定模式（fallback）" + _deg_note,
            "components": components,
        }

    # ===== 智能模式 =====
    risk_amount = effective_balance * (max_risk_pct / 100.0)

    # 2) ATR-based size (黄金 1手 1点 = $100)
    sl_points = max(atr, 1.0)
    base_lots = risk_amount / (sl_points * 100.0)

    # 3) 趋势强度系数（volatility_factor 来自配置：>1 强趋势加码, <1 弱趋势缩手）
    #    默认 1.0，区间 [0.3, 2.0]
    vol_mult = max(0.3, min(vol_factor, 2.0))

    # 3.5) 时段质量系数 — 伦敦-纽约重叠时段自动加码（调研支撑，加法型不砍信号）
    session_mult = _session_quality_mult()

    # 4) 信号强度系数（非线性，提准非拦截）
    #   实证(4账号1211笔平仓)：conf<0.6 单笔均亏 -1.5~-1.9（最重亏损区间），≥0.7 反而小亏；
    #   故对低置信强惩罚、高置信不缩，逼 AI 在不确定时自动"缩手"而非硬拦截开单。
    c = max(0.0, min(signal_confidence, 1.0))
    if c >= 0.7:    sig_mult = 1.0
    elif c >= 0.65: sig_mult = 0.8
    elif c >= 0.6:  sig_mult = 0.55
    elif c >= 0.55: sig_mult = 0.35
    else:           sig_mult = 0.2

    # 5) 同方向持仓衰减：第1单×1.0, 第2单×(1-decay), 第3单×(1-decay)^2 ...
    if same_direction_count <= 0:
        dir_mult = 1.0
    else:
        dir_mult = (1.0 - max(0.0, min(decay, 0.9))) ** same_direction_count

    # ★ 2026-08-10 v6 趋势强弱乘数（实时 H1 ADX → 手数自适应）
    #   用户需求：底线~红线之间按趋势强弱自动开单手数（强趋势加码、震荡缩手）。
    #   映射（ADX 14 通用分档，参考 stockcharts/Wilder 标准）：
    #     ADX ≥ 30  强趋势 → ×1.25（顺势加码）
    #     ADX ≤ 20  震荡/无趋势 → ×0.75（缩手防假突破）
    #     20 < ADX < 30 线性 0.75→1.25
    #   取不到 ADX（数据不足）→ ×1.0（不干预，向后兼容）。
    try:
        _adx_v = float(adx) if adx is not None else 0.0
    except (TypeError, ValueError):
        _adx_v = 0.0
    if _adx_v <= 0:
        adx_mult = 1.0
    elif _adx_v >= 30:
        adx_mult = 1.25
    elif _adx_v <= 20:
        adx_mult = 0.75
    else:
        adx_mult = 0.75 + 0.50 * ((_adx_v - 20.0) / 10.0)
    adx_mult = round(adx_mult, 3)

    # ===== 汇总 =====
    raw_lots = base_lots * vol_mult * session_mult * sig_mult * dir_mult * adx_mult

    # ★ 钳制到 [min_lot, max_lot]，且不超过 max_position_lots
    #   改进：当 raw_lots 超过 max_lot 时，不再一律顶到上限，
    #   而是按信号置信度在 [min_lot, max_lot] 区间内分配：
    #     高置信(≥0.7) → max_lot（信号强，给满额）
    #     中等置信(0.55~0.7) → 线性插值（信号一般，给中间值）
    #     低置信(<0.55) → min_lot（信号弱，给起步量）
    #   这样 min_lot 成为真正的"起步手数"，max_lot 是"强信号上限"
    ceiling = min(max_pos_lots, max_lot)
    if raw_lots <= ceiling:
        # ★ 2026-08-15 P2-5 修复：小本金账号 raw_lots 恒被钳到 min_lot，失去「本金等比缩放」意义
        #   （即无论本金多少都给起步量）。此为可接受的保守保护，但须可观测——
        #   故补告警日志，便于审计/调参时识别「被钳到 min_lot」的账号。
        if raw_lots < min_lot:
            logger.warning(
                f"[sizing] 账号本金偏小：raw_lots={raw_lots:.4f} < min_lot={min_lot}，"
                f"已钳到 min_lot（失去本金等比缩放，属预期保守保护）"
            )
        final_lots = max(raw_lots, min_lot)
    else:
        # raw_lots 超限：按置信度在 [min_lot, ceiling] 内分配
        c = max(0.0, min(signal_confidence, 1.0))
        if c >= 0.70:
            final_lots = ceiling
        elif c >= 0.55:
            ratio = (c - 0.55) / 0.15  # 0~1 映射
            final_lots = min_lot + (ceiling - min_lot) * ratio
        else:
            final_lots = min_lot

    # ★ Phase 6：降级系数必须在 clamp **之后**施加。
    #   反例（曾差点写错）：若只在 raw_lots 上乘，大本金账号（如 $989k demo）
    #   raw_lots 恒超 ceiling → 走「按置信度分配」分支 → 结果只取决于置信度，
    #   降级系数被完全吃掉，L2 下照样满仓。放在最后是唯一不会被旁路的位置。
    final_lots, _deg_note = _apply_degrade(final_lots, deg, min_lot)

    components.update({
        "base_lots": round(base_lots, 4),
        "vol_mult": round(vol_mult, 3),
        "session_mult": round(session_mult, 3),
        "sig_mult": round(sig_mult, 3),
        "dir_mult": round(dir_mult, 3),
        "adx_mult": adx_mult,
        "adx": _adx_v,
        "raw_lots": round(raw_lots, 4),
    })

    _session_label = {1.25: "伦敦-纽约重叠", 1.10: "伦敦单盘", 1.05: "纽约尾盘", 0.90: "凌晨清淡", 1.00: "亚盘"}.get(session_mult, "未知")
    reason = (
        f"智能手数: 基础 {base_lots:.2f}手 × "
        f"趋势×{vol_mult:.2f} × 时段×{session_mult:.2f}({_session_label}) × 信号×{sig_mult:.2f} × "
        f"同向×{dir_mult:.2f} × ADX{_adx_v:.0f}×{adx_mult:.2f} = {raw_lots:.2f}手 → 钳制 {final_lots:.2f}手" + _deg_note
    )

    return {
        "lots": round(final_lots, 2),
        "raw_lots": round(raw_lots, 2),
        "reason": reason,
        "components": components,
    }


def _apply_degrade(lots: float, deg: float, min_lot: float):
    """施加降级手数系数，返回 (手数, 说明后缀)。

    三条判定：
      * deg >= 1.0  → 原样返回（L0 全能力，零开销、零行为改变）
      * deg <= 0.0  → 返回 0.0 手（L3 熔断）。与 allow_new_entry() 构成双保险：
                      即便某条下单路径忘了查熔断，手数为 0 也开不出单。
      * 0 < deg < 1 → 打折后仍不得低于 min_lot（min_lot 是交易最小单位，
                      不是可以突破的软限制；小账号在降级下可能已到底，属正常）。
    """
    try:
        d = float(deg)
    except (TypeError, ValueError):
        return lots, ""
    if d >= 1.0:
        return lots, ""
    if d <= 0.0:
        return 0.0, " → ⛔L3熔断：平台降级，本轮不开新仓"
    scaled = max(lots * d, float(min_lot))
    return scaled, f" → 降级×{d:.2f} = {scaled:.2f}手"


def count_same_direction_positions(positions: list, direction: str) -> int:
    """
    统计同方向持仓笔数

    positions: list of dict (来自 mt5_service.get_positions)
        每条 pos 至少含 'type' 字段（'buy' / 'sell'）
    direction: 准备开仓的方向 'BUY' / 'SELL'
    """
    want = "buy" if direction.upper() == "BUY" else "sell"
    # ★ 2026-08-17 防御（铁律：持仓元素非 dict 守卫）——曾现 'list' object has no attribute 'get'
    #   （copy_order 跟单复制失败刷屏），此处对非 dict 元素一律跳过，绝不让异常形态打断手数计算。
    return sum(1 for p in positions
               if isinstance(p, dict) and (p.get("type") or "").lower() == want)
