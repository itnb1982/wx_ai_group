"""
策略配置路由
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db, safe_commit
from app.models.user import User
from app.models.strategy import StrategyConfig
from app.models.mt5_account import MT5Account
from app.routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["策略配置"])


class UpdateStrategyRequest(BaseModel):
    name: Optional[str] = None
    ai_mode: Optional[str] = None  # dual / single_ds / single_hy / notify_only
    decision_interval: Optional[int] = None
    max_position_lots: Optional[float] = None
    max_positions: Optional[int] = None
    max_daily_loss_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    max_spread_points: Optional[int] = None
    max_risk_per_trade_pct: Optional[float] = None
    min_confidence: Optional[float] = None
    trade_asian: Optional[bool] = None
    trade_european: Optional[bool] = None
    trade_american: Optional[bool] = None
    auto_evolution: Optional[bool] = None
    # === 智能手数自适应 ===
    base_capital: Optional[float] = None
    sizing_mode: Optional[str] = None
    volatility_factor: Optional[float] = None
    same_direction_decay: Optional[float] = None
    min_lot_per_trade: Optional[float] = None
    max_lot_per_trade: Optional[float] = None
    max_concurrent_same_direction: Optional[int] = None
    open_interval_seconds: Optional[int] = None
    capital_source: Optional[str] = None  # live(实时余额) / manual(用 base_capital)
    sizing_scale_mode: Optional[str] = None  # auto(按本金等比缩放上限) / manual(写死上限)
    # === 智能分批止盈 ===
    smart_tp_enabled: Optional[bool] = None
    tp1_atr_mult: Optional[float] = None
    tp1_close_pct: Optional[float] = None
    tp2_atr_mult: Optional[float] = None
    tp2_close_pct: Optional[float] = None
    tp3_atr_mult: Optional[float] = None
    tp3_close_pct: Optional[float] = None
    # === 追踪止损 / 保本 ===
    breakeven_after_tp1: Optional[bool] = None
    breakeven_buffer_points: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    trailing_activate_after_tp2: Optional[bool] = None
    # === AI 反向平仓门控 ===
    ai_reverse_close_confidence: Optional[float] = None
    # === 风控跟随 + 智能平仓增强 ===
    follow_leader: Optional[bool] = None
    reversal_confirm_cycles: Optional[int] = None
    basket_tp_amount: Optional[float] = None
    enable_l3_guard: Optional[bool] = None
    enable_trailing_sl: Optional[bool] = None
    # === 第⑤道防线·浮亏熔断（独立参数，与盈利锁利分开）===
    enable_hard_loss_cut: Optional[bool] = None
    hard_loss_basket_amount: Optional[float] = None
    hard_loss_per_trade_amount: Optional[float] = None
    # === 决策质量门控（加法型软门，不破坏既有风控）===
    regime_open_mode: Optional[str] = None   # off / soft / hard（体制门）
    short_guard_mode: Optional[str] = None   # off / soft / hard（空头约束）


def _strategy_to_dict(s: StrategyConfig) -> dict:
    return {
        "id": s.id,
        "mt5_account_id": s.mt5_account_id,
        "name": s.name,
        "symbol": s.symbol,
        "ai_mode": s.ai_mode,
        "decision_interval": s.decision_interval,
        "max_position_lots": s.max_position_lots,
        "max_positions": getattr(s, 'max_positions', 10),
        "max_daily_loss_pct": s.max_daily_loss_pct,
        "max_drawdown_pct": s.max_drawdown_pct,
        "max_spread_points": s.max_spread_points,
        "max_risk_per_trade_pct": s.max_risk_per_trade_pct,
        "min_confidence": s.min_confidence,
        "trade_asian": s.trade_asian,
        "trade_european": s.trade_european,
        "trade_american": s.trade_american,
        "auto_evolution": s.auto_evolution,
        # 智能手数
        "base_capital": getattr(s, 'base_capital', 1000.0),
        "sizing_mode": getattr(s, 'sizing_mode', 'smart'),
        "volatility_factor": getattr(s, 'volatility_factor', 1.0),
        "same_direction_decay": getattr(s, 'same_direction_decay', 0.5),
        "min_lot_per_trade": getattr(s, 'min_lot_per_trade', 0.01),
        "max_lot_per_trade": getattr(s, 'max_lot_per_trade', 1.0),
        "max_concurrent_same_direction": getattr(s, 'max_concurrent_same_direction', 3),
        "open_interval_seconds": getattr(s, 'open_interval_seconds', 180),
        "capital_source": getattr(s, 'capital_source', 'live'),
        "sizing_scale_mode": getattr(s, 'sizing_scale_mode', 'auto'),
        # 智能分批止盈
        "smart_tp_enabled": getattr(s, 'smart_tp_enabled', True),
        "tp1_atr_mult": getattr(s, 'tp1_atr_mult', 1.0),
        "tp1_close_pct": getattr(s, 'tp1_close_pct', 0.40),
        "tp2_atr_mult": getattr(s, 'tp2_atr_mult', 1.5),
        "tp2_close_pct": getattr(s, 'tp2_close_pct', 0.30),
        "tp3_atr_mult": getattr(s, 'tp3_atr_mult', 2.5),
        "tp3_close_pct": getattr(s, 'tp3_close_pct', 0.20),
        # 追踪止损 / 保本
        "breakeven_after_tp1": getattr(s, 'breakeven_after_tp1', True),
        "breakeven_buffer_points": getattr(s, 'breakeven_buffer_points', 0.5),
        "trailing_atr_mult": getattr(s, 'trailing_atr_mult', 1.5),
        "trailing_activate_after_tp2": getattr(s, 'trailing_activate_after_tp2', True),
        # AI 反向平仓门控（2026-08-17 用户铁律：方向翻转即止损，门槛对齐 lean 下限 0.42）
        "ai_reverse_close_confidence": getattr(s, 'ai_reverse_close_confidence', 0.42),
        # 风控跟随 + 智能平仓增强
        "follow_leader": getattr(s, 'follow_leader', True),
        "reversal_confirm_cycles": getattr(s, 'reversal_confirm_cycles', 2),
        "basket_tp_amount": getattr(s, 'basket_tp_amount', 100.0),
        "enable_l3_guard": getattr(s, 'enable_l3_guard', True),
        "enable_trailing_sl": getattr(s, 'enable_trailing_sl', True),
        # 第⑤道防线·浮亏熔断（独立参数）
        "enable_hard_loss_cut": getattr(s, 'enable_hard_loss_cut', True),
        "hard_loss_basket_amount": getattr(s, 'hard_loss_basket_amount', 50.0),
        "hard_loss_per_trade_amount": getattr(s, 'hard_loss_per_trade_amount', 30.0),
        # 决策质量门控
        "regime_open_mode": getattr(s, 'regime_open_mode', 'soft'),
        "short_guard_mode": getattr(s, 'short_guard_mode', 'soft'),
    }


@router.get("/{mt5_account_id}")
def get_strategy(mt5_account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取指定MT5账号的策略配置（策略按账号唯一，跨用户共享）"""
    strategy = db.query(StrategyConfig).filter(
        StrategyConfig.mt5_account_id == mt5_account_id,
    ).first()

    if not strategy:
        return {
            "id": None,
            "mt5_account_id": mt5_account_id,
            "name": "默认策略",
            "ai_mode": "dual",
            "decision_interval": 60,
            "max_position_lots": 1.0,
            "max_positions": 10,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 20.0,
            "max_spread_points": 50,
            "max_risk_per_trade_pct": 2.0,
            "min_confidence": 0.65,
            "trade_asian": True,
            "trade_european": True,
            "trade_american": True,
            "auto_evolution": True,
            # 智能手数
            "base_capital": 1000.0,
            "sizing_mode": "smart",
            "volatility_factor": 1.0,
            "same_direction_decay": 0.5,
            "min_lot_per_trade": 0.01,
            "max_lot_per_trade": 1.0,
            "max_concurrent_same_direction": 3,
            "open_interval_seconds": 180,
            "capital_source": "live",
            "sizing_scale_mode": "auto",
            # 智能分批止盈
            "smart_tp_enabled": True,
            "tp1_atr_mult": 1.0,
            "tp1_close_pct": 0.40,
            "tp2_atr_mult": 1.5,
            "tp2_close_pct": 0.30,
            "tp3_atr_mult": 2.5,
            "tp3_close_pct": 0.20,
            # 追踪止损 / 保本
            "breakeven_after_tp1": True,
            "breakeven_buffer_points": 0.5,
            "trailing_atr_mult": 1.5,
            "trailing_activate_after_tp2": True,
            # AI 反向平仓门控（2026-08-17 用户铁律：方向翻转即止损，对齐 lean 下限 0.42）
            "ai_reverse_close_confidence": 0.42,
            # 风控跟随 + 智能平仓增强
            "follow_leader": True,
            "reversal_confirm_cycles": 2,
            "basket_tp_amount": 100.0,
            "enable_l3_guard": True,
            "enable_trailing_sl": True,
            # 决策质量门控（默认 soft：提准非拦截）
            "regime_open_mode": "soft",
            "short_guard_mode": "soft",
        }

    # ── 风控跟随：返回合并主号后的值（只读，不写回本号 DB），让前端灰显显示真实生效值 ──
    if getattr(strategy, "follow_leader", True):
        acct = db.query(MT5Account).filter(MT5Account.id == mt5_account_id).first()
        if acct and not acct.is_market_primary:
            leader = db.query(MT5Account).filter(
                MT5Account.user_id == strategy.user_id,
                MT5Account.is_market_primary == True,
            ).first()
            if leader and leader.id != acct.id:
                ls = db.query(StrategyConfig).filter(
                    StrategyConfig.mt5_account_id == leader.id,
                    StrategyConfig.user_id == strategy.user_id,
                ).first()
                if ls:
                    d = _strategy_to_dict(strategy)
                    # ★ 仅继承风控/平仓/熔断字段；账号私有字段（本金/手数/AI模式/单笔风险%）保持本号值
                    #   ★ 2026-08-10 信号塔统一：max_risk_per_trade_pct 移出继承——手数基准必须用
                    #     客户自己填的值（否则跟号改风险%被重定向写主号行，自己永远不变，前端显示也错）。
                    _inherit = [
                        "min_confidence", "max_positions",
                        "max_position_lots", "max_daily_loss_pct", "max_drawdown_pct",
                        "max_spread_points", "trade_asian", "trade_european", "trade_american",
                        "open_interval_seconds",
                        "smart_tp_enabled", "tp1_atr_mult", "tp1_close_pct", "tp2_atr_mult",
                        "tp2_close_pct", "tp3_atr_mult", "tp3_close_pct",
                        "breakeven_after_tp1", "breakeven_buffer_points", "trailing_atr_mult",
                        "trailing_activate_after_tp2", "ai_reverse_close_confidence",
                        "reversal_confirm_cycles", "enable_l3_guard", "enable_trailing_sl",
                        "enable_hard_loss_cut", "hard_loss_basket_amount", "hard_loss_per_trade_amount",
                        "regime_open_mode", "short_guard_mode",
                    ]
                    for _f in _inherit:
                        d[_f] = getattr(ls, _f, None)
                    _lead_cap = float(getattr(ls, "base_capital", 1000) or 1000)
                    _my_cap = float(getattr(strategy, "base_capital", 1000) or 1000)
                    if _lead_cap > 0:
                        # ★ 2026-08-05 修正：去掉本金缩放，basket_tp_amount 用主号原始美元值。
                        # 用户设多少就按多少锁利（"设10刀就10刀触发"），不再因缩放显示/触发不一致。
                        d["basket_tp_amount"] = float(getattr(ls, "basket_tp_amount", 100.0) or 100.0)
                    d["_inherited"] = True
                    return d

    return _strategy_to_dict(strategy)


@router.put("/{mt5_account_id}")
def update_strategy(mt5_account_id: str, req: UpdateStrategyRequest,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """更新策略配置（按账号唯一：找到就更新，找不到就建）"""
    # ── D1/D2/D3：枚举 / 范围 / 分批比例总和 校验，杜绝脏数据导致交易异常 ──
    if req.sizing_mode is not None and req.sizing_mode not in ("smart", "fixed"):
        raise HTTPException(status_code=400, detail="sizing_mode 仅支持 smart / fixed")
    if req.ai_mode is not None and req.ai_mode not in ("dual", "single_ds", "single_hy", "notify_only"):
        raise HTTPException(status_code=400, detail="ai_mode 仅支持 dual / single_ds / single_hy / notify_only")
    if req.base_capital is not None and req.base_capital <= 0:
        raise HTTPException(status_code=400, detail="基础本金必须为正数")
    if req.max_position_lots is not None and req.max_position_lots <= 0:
        raise HTTPException(status_code=400, detail="最大持仓手数必须为正数")
    if req.max_positions is not None and req.max_positions < 1:
        raise HTTPException(status_code=400, detail="最大持仓笔数至少为 1")
    if req.open_interval_seconds is not None and req.open_interval_seconds < 0:
        raise HTTPException(status_code=400, detail="开仓间隔不能为负")
    if req.max_risk_per_trade_pct is not None and not (0 < req.max_risk_per_trade_pct <= 100):
        raise HTTPException(status_code=400, detail="单笔风险百分比应在 0~100 之间")
    if req.min_confidence is not None and not (0 <= req.min_confidence <= 1):
        raise HTTPException(status_code=400, detail="最小置信度应在 0~1 之间")
    if req.volatility_factor is not None and req.volatility_factor <= 0:
        raise HTTPException(status_code=400, detail="波动率系数必须为正数")
    if req.max_daily_loss_pct is not None and req.max_daily_loss_pct < 0:
        raise HTTPException(status_code=400, detail="最大日亏损比例不能为负")
    if req.max_drawdown_pct is not None and req.max_drawdown_pct < 0:
        raise HTTPException(status_code=400, detail="最大回撤比例不能为负")
    if req.tp1_close_pct is not None and not (0 < req.tp1_close_pct < 1):
        raise HTTPException(status_code=400, detail="TP1 平仓比例应在 0~1 之间")
    if req.tp2_close_pct is not None and not (0 < req.tp2_close_pct < 1):
        raise HTTPException(status_code=400, detail="TP2 平仓比例应在 0~1 之间")
    if req.tp3_close_pct is not None and not (0 < req.tp3_close_pct < 1):
        raise HTTPException(status_code=400, detail="TP3 平仓比例应在 0~1 之间")
    # ★ 决策质量门控枚举校验（加法型，杜绝脏值）
    if req.capital_source is not None and req.capital_source not in ("live", "manual"):
        raise HTTPException(status_code=400, detail="capital_source 仅支持 live / manual")
    if req.regime_open_mode is not None and req.regime_open_mode not in ("off", "soft", "hard"):
        raise HTTPException(status_code=400, detail="regime_open_mode 仅支持 off / soft / hard")
    if req.short_guard_mode is not None and req.short_guard_mode not in ("off", "soft", "hard"):
        raise HTTPException(status_code=400, detail="short_guard_mode 仅支持 off / soft / hard")
    if req.sizing_scale_mode is not None and req.sizing_scale_mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="sizing_scale_mode 仅支持 auto / manual")

    # 取现有策略用于补全"缺失字段"，再校验分批平仓比例总和 ≤ 100%
    existing = db.query(StrategyConfig).filter(
        StrategyConfig.mt5_account_id == mt5_account_id,
    ).first()
    _t1 = req.tp1_close_pct if req.tp1_close_pct is not None else (existing.tp1_close_pct if existing else 0.40)
    _t2 = req.tp2_close_pct if req.tp2_close_pct is not None else (existing.tp2_close_pct if existing else 0.30)
    _t3 = req.tp3_close_pct if req.tp3_close_pct is not None else (existing.tp3_close_pct if existing else 0.20)
    if _t1 + _t2 + _t3 > 1.0:
        raise HTTPException(status_code=400, detail="TP1+TP2+TP3 平仓比例之和不能超过 100%")

    strategy = existing
    if strategy is None:
        # 没有就建一条（策略按账号唯一，不绑死 user，便于跨用户编辑）
        strategy = StrategyConfig(
            user_id=user.id,
            mt5_account_id=mt5_account_id,
            name="默认策略",
        )
        db.add(strategy)

    # ── 风控跟随：跟号修改继承字段时，实际写入主号行 ──
    # L3护盾/L2反转在主号(行情主号=篮子主)运行，跟号只是镜像。若在跟号行改 basket_tp_amount，
    # 主号仍用旧值 → "设了10却按主号值触发" 的体感 bug（2026-08-05 用户复盘）。故重定向到主号行。
    #
    # ★ 账号私有字段（本金/手数/AI模式等）不参与继承，每账号独立可改：
    #   base_capital / sizing_mode / volatility_factor / same_direction_decay /
    #   min_lot_per_trade / max_lot_per_trade / max_concurrent_same_direction /
    #   name / ai_mode / decision_interval / auto_evolution / follow_leader / capital_source / sizing_scale_mode
    #   ★ 2026-08-10 信号塔统一：max_risk_per_trade_pct 移出继承（保存时跟号不再重定向写主号行）
    _inherit_fields = [
        "ai_reverse_close_confidence", "reversal_confirm_cycles",
        "basket_tp_amount", "enable_l3_guard", "enable_trailing_sl",
        "min_confidence", "max_positions",
        "max_position_lots", "max_daily_loss_pct", "max_drawdown_pct",
        "max_spread_points", "trade_asian", "trade_european", "trade_american",
        "open_interval_seconds",
        "smart_tp_enabled", "tp1_atr_mult", "tp1_close_pct", "tp2_atr_mult",
        "tp2_close_pct", "tp3_atr_mult", "tp3_close_pct",
        "breakeven_after_tp1", "breakeven_buffer_points", "trailing_atr_mult",
        "trailing_activate_after_tp2",
        "enable_hard_loss_cut", "hard_loss_basket_amount", "hard_loss_per_trade_amount",
        "regime_open_mode", "short_guard_mode",
    ]
    _acct = db.query(MT5Account).filter(MT5Account.id == mt5_account_id).first()
    _is_leader_acct = bool(_acct and _acct.is_market_primary)
    _req_follow = req.model_dump(exclude_none=True).get(
        "follow_leader", getattr(strategy, "follow_leader", True)
    )
    _target_strategy = strategy
    if (not _is_leader_acct) and _req_follow:
        _leader = None
        if _acct is not None:
            _leader = db.query(MT5Account).filter(
                MT5Account.user_id == _acct.user_id,
                MT5Account.is_market_primary == True,
            ).first()
        if _leader is not None and _leader.id != mt5_account_id:
            _ls = db.query(StrategyConfig).filter(
                StrategyConfig.mt5_account_id == _leader.id,
            ).first()
            if _ls is not None:
                _target_strategy = _ls
                logger.info(
                    f"[策略保存] 账号 {mt5_account_id[:8]} 跟随主号，继承字段改写入主号 {_leader.id[:8]}"
                )

    # 把所有字段赋值包进 apply，供 safe_commit 在瞬锁回滚后幂等重放（杜绝"200 不落库"）
    _fields = dict(req.model_dump(exclude_none=True))
    _target_is_leader = _target_strategy is strategy

    def _apply():
        for field, value in _fields.items():
            if not _target_is_leader and field in _inherit_fields:
                setattr(_target_strategy, field, value)
            else:
                setattr(strategy, field, value)

    _apply()

    logger.info(
        f"[策略保存] {mt5_account_id[:8]} 待写入字段: "
        f"{ {k: getattr(strategy, k, None) for k in _fields.keys()} }"
    )
    try:
        # 健壮提交：自动消化 Defender 间歇写锁（readonly / database is locked）
        safe_commit(db, apply=_apply)
    except Exception as _commit_err:
        raise HTTPException(status_code=500, detail=f"策略保存失败: {_commit_err}")
    db.refresh(strategy)
    logger.info(
        f"[策略保存] {mt5_account_id[:8]} commit 成功，basket_tp={getattr(strategy,'basket_tp_amount',None)}"
    )

    return {"ok": True, "id": strategy.id, "mt5_account_id": mt5_account_id,
            "saved_fields": {k: getattr(strategy, k, None) for k in _fields.keys()}}
