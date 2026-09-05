"""
XAU/USD 万象Ai — 智能分批止盈 / 追踪止损引擎
================================================
目标：让 AI 在已有持仓上"自动管理出场"，而不是死等 TP/SL 被扫。

设计依据（≥3家交叉验证）：
1. AlgoMatrix 4-TP system: 40%@1.0ATR → 30%@1.5ATR → 20%@2.5ATR → 10%留runner
   (algomatrix.trade, 2026-08)
2. AlphaMind: breakeven after TP1, then trailing; 6-agent regime aware
   (alphamind-ai.com, 2026)
3. Pro-Scalper: breakeven trigger 10-15 pips, trail 1.5×ATR M5
   (pro-scalper.com, 2026)

智能平仓 5 道防线（按顺序检查）：
  ① 4 级分批止盈（TP1~TP3 已到 → 按比例平掉）
  ② TP1 触发后 → 移动 SL 至入场 + buffer（保本单）
  ③ TP2 触发后 → 激活追踪止损（trailing_atr_mult × ATR）
  ④ 追踪止损被扫 → 全平
  ⑤ AI 反向决策 + 置信度 > 阈值 → 全平（最快反应）

调用方：trade_executor._manage_positions() 每个 tick/每轮决策都跑一遍。
"""
from typing import Optional
from loguru import logger
import MetaTrader5 as mt5


def _cfg(strategy, key: str, default):
    if isinstance(strategy, dict):
        return strategy.get(key, default)
    # 兼容 ORM 实例（如 StrategyConfig）：否则非 dict 时函数隐式返回 None，
    # 下游 float(None) 会崩（测试用 ORM 实例验证 serde 路径时暴露）。
    return getattr(strategy, key, default)


def _scalar(x):
    """把 Chronos 分位（可能是 list/ndarray/标量）安全转为 float；失败返回 None。
    防回归：SELL 方向 smart_exit 曾因对列表调 float() 抛 TypeError 被静默吞掉，
    导致 P10 地板止盈/追踪下限/原生TP 全部失效、退化到安全网。"""
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return float(x[-1]) if x else None
    try:
        import numpy as np
        if isinstance(x, np.ndarray):
            return float(x.flat[-1]) if x.size else None
    except ImportError:
        pass
    try:
        return float(x)
    except Exception:
        return None


def evaluate_position(
    *,
    position: dict,             # 来自 mt5_service.get_positions
    atr: float,                  # 当前 ATR（与开仓时不同）
    ai_decision: str,            # 'BUY' / 'SELL' / 'HOLD'
    ai_confidence: float,
    strategy,
    ai_reverse_th: Optional[float] = None,   # 显式传入则优先(支持实时读DB)，否则取 strategy 默认
    quality_regime: str = "",    # v4 Meta 质量陪审团: HIGH/MID/LOW/VERY_LOW（""=未评估，按 MID 常规）
    chronos_tp_ceiling: Optional[float] = None,  # Chronos P90 末价（BUY 盈利目标/止盈天花板）
    chronos_p10: Optional[float] = None,         # Chronos P10 末价（SELL 盈利目标/止盈地板）
    peak_move: Optional[float] = None,           # ★ 2026-08-10 历史峰值浮盈(价格偏移, 与 move 同单位)
) -> dict:
    """
    评估单个持仓是否需要部分/全平

    返回 dict:
      - action: 'hold' | 'partial_close' | 'full_close'
      - close_pct: 0~1  平仓比例（仅 partial_close）
      - new_sl: 修改后的止损（None 不改）
      - reason: 决策原因
    """
    # ── 大脑审计：记录平仓大脑接入（喂了什么）──
    try:
        from app.services.brain_audit import record as _ba_rec
        _ba_rec("smart_exit", "input", input_fields={
            "position": bool(position), "atr": atr,
            "ai_decision": ai_decision, "ai_confidence": ai_confidence,
            "quality_regime": quality_regime,
        })
    except Exception:
        pass

    pos_type = (position.get("type") or "").lower()   # 'buy' / 'sell'
    volume = float(position.get("volume", 0) or 0)
    open_price = float(position.get("price_open") or position.get("open_price") or 0)
    current_sl = float(position.get("sl") or 0)
    current_tp = float(position.get("tp") or 0)
    current_price = float(position.get("price_current") or position.get("current_price") or 0)
    profit = float(position.get("profit", 0) or 0)

    # ★ 2026-08-12 修复：部分账号 get_positions 不回传 price_current(=0) →
    #   下方「数据不足」直接 return，保本/追踪硬地板永不被计算，浮盈单在盈利窗口也
    #   不上移 SL（由赚变亏，正是用户看到的「从盈利到亏损」）。
    #   此处用 MT5 已给的 profit/open_price 反推现价兜底（XAUUSD：1 标准手=100oz，
    #   $1 波动=盈利 $100×手数 → 价移 = profit/(volume*100)）。反推值带点差误差，
    #   但保本 SL 锚定 open_price 不依赖它，仅用于「是否触发」判定，足够。
    #   仅当 price_current 缺失时生效，正常回传的账户行为完全不变（提准非拦截、纯加法）。
    if current_price <= 0 and volume > 0 and open_price > 0:
        _move = profit / (volume * 100.0)
        current_price = (open_price - _move) if pos_type == "sell" else (open_price + _move)

    if volume <= 0 or open_price <= 0 or current_price <= 0:
        return {"action": "hold", "close_pct": 0, "new_sl": None, "new_tp": None, "reason": "数据不足"}

    # ===== 读取配置 =====
    smart_tp = bool(_cfg(strategy, "smart_tp_enabled", True))
    # ★ 2026-08-17 用户铁律：平仓=防守动作，方向翻转即止损，门槛对齐 lean 放行下限 0.42
    ai_reverse_th = float(ai_reverse_th if ai_reverse_th is not None
                          else _cfg(strategy, "ai_reverse_close_confidence", 0.42))

    tp1_mult = float(_cfg(strategy, "tp1_atr_mult", 1.0))
    tp1_pct = float(_cfg(strategy, "tp1_close_pct", 0.40))
    tp2_mult = float(_cfg(strategy, "tp2_atr_mult", 1.5))
    tp2_pct = float(_cfg(strategy, "tp2_close_pct", 0.30))
    tp3_mult = float(_cfg(strategy, "tp3_atr_mult", 2.5))
    tp3_pct = float(_cfg(strategy, "tp3_close_pct", 0.20))

    be_enabled = bool(_cfg(strategy, "breakeven_after_tp1", True))
    be_buffer = float(_cfg(strategy, "breakeven_buffer_points", 0.5))

    trail_mult = float(_cfg(strategy, "trailing_atr_mult", 1.5))
    trail_after_tp2 = bool(_cfg(strategy, "trailing_activate_after_tp2", True))
    enable_trail = bool(_cfg(strategy, "enable_trailing_sl", True))

    # ── v4 Meta 质量陪审团：止盈 regime 参数化（提准非拦截，绝不减交易笔数）──
    # HIGH(≥0.7): 让利润奔跑——原生 TP 已被设为 Chronos P90 天花板（见 compute_initial_sl_tp），
    #             此处追踪 TP 上限= P90，触及即全平吃满整段（不吃不喝变喝汤）。
    # MID(0.5~0.7): 常规 4 级分批（默认行为，阈值不变）。
    # LOW(0.35~0.5): 啃头皮——分批阈值收紧(×0.6)，快进快出、更早落袋，但照常开仓（不减笔数）。
    # VERY_LOW(<0.35): 极少触发；同样只做「紧 scalp」处理，绝不拦截开仓（严守「提准非拦截」红线）。
    _q = (quality_regime or "").upper()
    _scalp = 1.0
    if _q in ("LOW", "VERY_LOW"):
        _scalp = 0.6
    if _scalp != 1.0:
        tp1_mult *= _scalp
        tp2_mult *= _scalp
        tp3_mult *= _scalp
        trail_mult *= 0.8  # 啃头皮：追踪更贴，保护薄利

    # ===== 计算盈亏倍数（以 ATR 为单位） =====
    if pos_type == "buy":
        move = current_price - open_price
    else:  # sell
        move = open_price - current_price
    move_atr = move / max(atr, 0.01)  # 距入场多少个 ATR

    # ===== ★★ 2026-08-18 用户铁律·开仓即亏认错（补「盈利即护盘」盲区）=====
    # 实盘根因（昨晚三笔大亏 -779/-155/-118）：现有「浮盈回吐锁利」只覆盖「先盈后回吐」，
    #   对「开仓即逆方向、从未进盈利区」完全失效 → 扛到 SL/AI 认错才平，单笔亏 800/300/280 点。
    #   用户："绝不等到亏损、回撤一点就跑"。本防线是「盈利即护盘」的完整镜像：
    #   持仓从未盈利(peak_move<=0) 且当前浮亏超认错阈值 → 立即全平认错，不等 SL、不等 AI 判反向。
    # ★ 防误杀双保险：① 阈值 = max(硬地板3.0, 0.3×ATR) > 噪音带(~0.29×ATR)，正常波动不触发；
    #   ② 仅「从未盈利」单触发——方向对的单通常会先盈利(peak>0)走回吐锁利，不被误杀。
    #   仅「持续/快速反向突破且从未盈利」才触发（方向真错即跑，远在 1.5×ATR 的 SL 之前）。
    _cut_pt = float(_cfg(strategy, "cut_loss_wrong_dir_pt", 3.0))
    _cut_atr_mult = float(_cfg(strategy, "cut_loss_wrong_dir_atr_mult", 0.30))
    _cut_th = max(_cut_pt, _cut_atr_mult * atr)
    _ever_profit = (peak_move is not None and peak_move > 0)
    if move < 0 and abs(move) >= _cut_th - 0.005 and not _ever_profit:
        return {
            "action": "full_close",
            "close_pct": 1.0,
            "new_sl": None,
            "new_tp": None,
            "reason": f"开仓即亏认错(浮亏{abs(move):.2f}≥{_cut_th:.2f}·从未盈利)→全平不扛",
        }

    # ===== ⑤ AI 反向 + 置信度 → 标记反转意图（由 trade_executor 做连续确认防抖后再全平）=====
    # 审计修复(2026-08-05 初稿 / 2026-08-06 回正)：
    #   ① ★ 2026-08-17 用户铁律改定：门槛 0.60/0.75 → 0.42（对齐 lean 放行下限）。
    #      「AI 已翻方向但持仓死扛」是用户实盘最大痛点（实证：AI 45% 翻多、0.65 门槛永不触发、
    #      SELL 继续扛单扩亏）。平仓是防守动作：AI 给出明确方向(≥0.42)且与持仓相反 → 立即标记
    #      反转意图，由 L2 连续确认防抖过滤假反转，不再用高置信门槛把真反转也挡掉。
    #   ② ★ 移除旧版"浮盈单保护"(profit>0则跳过反向平)——该逻辑导致：
    #      AI出BUY时已有SELL浮盈→不标记反转→BUY新开→对冲锁死。
    #      正确做法：浮盈单也走L2防抖确认链，由连续N轮同向确认来过滤假反转，
    #      而非一刀切跳过。保本/追踪由下方独立逻辑兜底，不受影响。
    if ai_decision and ai_confidence >= ai_reverse_th:
        opposite = ((pos_type == "buy" and ai_decision.upper() == "SELL") or
                    (pos_type == "sell" and ai_decision.upper() == "BUY"))
        if opposite:
            return {
                "action": "reverse_signal",
                "close_pct": 1.0,
                "new_sl": None,
                "new_tp": None,
                "reason": f"AI反向{ai_decision}({ai_confidence:.0%}≥{ai_reverse_th:.0%})→待连续确认",
            }

    # ===== ★ v4 HIGH 质量：利润奔跑到 Chronos 远端分位数即全平（吃满整段）=====
    # 用户核心诉求：「关键点就在什么时候止盈」——HIGH 质量信号让利润真正奔跑，
    # 不中途分批啃掉，等价格触及「盈利方向的远端分位数」一次性落袋。
    # ★ 方向感知修复(2026-08-07)：BUY 盈利目标=P90(上界)，SELL 盈利目标=P10(下界)。
    #   原 bug：SELL 也用 P90 作"天花板"，而 P90 是预测上界——SELL 水下时
    #   current_price<=P90 恒成立→误判"吃满"在水下全平亏损。
    #   仅当目标在「盈利方向」上才生效（buy: 目标>入场; sell: 目标<入场）。
    if _q == "HIGH":
        _target = None
        _tag = ""
        if pos_type == "buy" and chronos_tp_ceiling:
            _t = _scalar(chronos_tp_ceiling)  # P90 上界
            if _t > open_price:
                _target, _tag = _t, "P90天花板"
        elif pos_type == "sell" and chronos_p10 is not None:
            _t = _scalar(chronos_p10)  # P10 下界
            if _t < open_price:
                _target, _tag = _t, "P10地板"
        if _target is not None and (
            (pos_type == "buy" and current_price >= _target) or
            (pos_type == "sell" and current_price <= _target)
        ):
            return {
                "action": "full_close",
                "close_pct": 1.0,
                "new_sl": None,
                "new_tp": None,
                "reason": f"HIGH质量·价格触及Chronos {_tag}({_target:.2f})→全平吃满",
            }

    # ===== ★ 浮盈回吐主动锁利（★ 2026-08-10 新增：补「AI 只判反向才平」的盲区）=====
    # 用户核心诉求：「最高盈利 900 多，行情反弹两根线 AI 还不平仓锁利」。
    # 原行为：AI 判 SELL 顺势就一直持有，只有判反向(BUY)才平 → 浮盈从峰值回吐坐视不管，
    #        直到被被动 SL 扫掉（629 vs 峰值 900）。这是"AI 不捕捉利润"的根源。
    # 本逻辑：持仓曾浮盈进入"利润区"，随后从峰值回吐 → 主动全平锁利，不等 AI 判反向、
    #        不等被动 SL。利润回吐保护必须在 AI 方向未变时也能生效。
    # ★★ 2026-08-17 用户理念 P0 修正：「发现不对果断全部平仓走人，不锁 50%，宁等下次机会」★★
    #   原门槛 0.5×ATR 进利润区（当前 ATR≈15 → 7.45 点）在震荡行情永不达标：
    #   实测大仓峰值 2.81 点（+$140）回吐 0.54 点仍被判定"未进利润区"→ 浮盈坐视回吐。
    #   修正为绝对点数地板：利润区 min(0.5×ATR, 2.0点)（ATR 大时不再 7.45 点，0.5手≈$100
    #   即有保护资格）、回吐下限 max(峰值15%, 0.25点)（0.5手≈$12.5，防 M5 噪音误平）
    #   ——"涨不动就走"。
    # ★ 2026-08-17 用户理念（盈利即护盘·回撤一点就跑）：
    #   用户："只要仓位盈利了，AI就时刻盯着，回撤一点就要跑；赚到10+就准备着，
    #        几美金也可以，绝不等到亏损"。故：利润区 0.5 点（小浮盈即护盘）、
    #   回撤 ≥max(峰值5%, 0.30点)（回撤一点点就跑；0.30点=点差+缓冲防 M5 噪音亏点差）。
    _pz_pt = 0.5
    _rt_pt = 0.30
    _rt_pct = 0.05
    try:
        from app.config import settings as _s3
        _pz_pt = float(getattr(_s3, "SMART_EXIT_PROFIT_ZONE_PT", 0.5) or 0.5)
        _rt_pt = float(getattr(_s3, "SMART_EXIT_RETRACE_PT", 0.30) or 0.30)
        _rt_pct = float(getattr(_s3, "SMART_EXIT_RETRACE_PCT", 0.05) or 0.05)
    except Exception:  # noqa: BLE001
        pass
    # ★★ 2026-08-17 P0 修复：`move > 0` 条件拦截回撤到亏损的场景 ★★
    #   原写法 `peak_move > 0 and move > 0`：价格从峰值回撤到浮亏（move<0）时
    #   整块被跳过 → "回撤一点就跑，绝不等到亏损"在【已转亏】时反而失效。
    #   实锤：主号 23:36 浮盈 1.4 点 → 23:38 回撤到 -0.3 点（回撤 1.7 点≥0.30 阈值）
    #   未触发，直到 23:47 才被更高峰值的回吐锁利。改为只要曾进利润区
    #   （peak_move≥利润区地板）即评估回吐，move 为负时回吐更大、更要跑。
    if peak_move and peak_move > 0:
        _pt_start = min(atr * 0.5, _pz_pt)
        if peak_move >= _pt_start:
            _retrace = peak_move - move
            _retrace_min = max(peak_move * _rt_pct, _rt_pt)
            # 浮点容忍：0.005 价格误差（1 手 = $0.5），防 1.8 vs 1.8000000003 边界失守
            if _retrace >= _retrace_min - 0.005:
                logger.info(
                    f"[smart_exit] 🎯 浮盈回吐锁利: ticket={position.get('ticket')} "
                    f"峰值{peak_move:.2f}→当前{move:.2f} 回吐{_retrace:.2f}≥{_retrace_min:.2f} → 全平锁利"
                )
                return {
                    "action": "full_close",
                    "close_pct": 1.0,
                    "new_sl": None,
                    "new_tp": None,
                    "reason": f"浮盈回吐锁利(峰值{peak_move:.1f}→{move:.1f},回吐{_retrace:.1f}≥阈值)",
                }

    # ===== ★ 2026-08-10 新增：浮盈达标即锁(partial_close 50%) — 用户第二次诉求 =====
    # 用户原话："浮盈 $1091 早就可以平仓了" —— 仅靠"回吐才平"太晚，"看到大浮盈就该锁"。
    # 本逻辑：持仓曾进入利润区(peak ≥0.5×ATR) 且 当前浮盈 ≥0.3×ATR($475/1手)
    #        → 平 50% 锁利(留一半让趋势奔跑)—— 既早兑现利润又不锁死趋势单。
    # 与上面"浮盈回吐锁利"互补：达标即锁一半 → 回吐再多则全平。
    # ★★ 2026-08-17 用户理念 P0 修正：「发现不对果断全部平仓走人，不要锁50%，
    #   宁愿等下一次机会也不要亏损离场」——锁 50% 的"留一半继续扛"在回吐时会
    #   把已锁的一半利润也搭进去。改为「要么持有，要么全走」：趋势健康时让利润
    #   奔跑（不锁），涨不动（回吐≥15%）时上面回吐全平一次性走光。
    #   开关 SMART_EXIT_LOCK50_ENABLED（默认 False=禁用锁50%，纯持有+回吐全平）。
    _lock50_on = True
    try:
        from app.config import settings as _s2
        _lock50_on = bool(getattr(_s2, "SMART_EXIT_LOCK50_ENABLED", False))
    except Exception:  # noqa: BLE001
        _lock50_on = False
    if _lock50_on and peak_move and peak_move > 0 and move > 0:
        _pt_start = atr * 0.5
        if peak_move >= _pt_start and move >= atr * 0.30:
            # ★ 2026-08-12 修复：partial_close 必须附带基于 peak_move 的动态利润锁定 SL。
            #   原逻辑返回 new_sl=None → 首次减半后，后续只要仍处于利润区，
            #   smart_exit 反复进入本分支并被 trade_executor 防重改成 hold，
            #   永远不会再执行下方的"早期保本+动态利润锁定" → SL 停留在保本点不再跟随。
            #   修正：partial 与动态锁定共用同一套 peak_move 计算，返回时把更锁利的 SL 带上，
            #   让已减半的仓位也能随价格继续上涨而上移 SL（真 trailing）。
            be_buffer = float(_cfg(strategy, "breakeven_buffer_points", 0.5) or 0.5)
            MIN_SL_DIST = float(_cfg(strategy, "min_sl_distance", 8.0))
            be_sl_atr_mult = float(_cfg(strategy, "be_sl_atr_mult", 1.0))
            be_sl_floor = max(MIN_SL_DIST, be_sl_atr_mult * atr)
            # 2026-08-13 修复：保本 SL 距现价须 ≥ be_sl_floor，否则刚减半就被噪音扫（用户见"刚止损就反转"）。
            # SELL: SL=open+max(be_buffer, floor-move)；BUY: 仅 move≥floor+be_buffer 才安全锁利SL，否则保留宽SL。
            if pos_type == "sell":
                _x = max(be_buffer, be_sl_floor - move)
                be_sl = round(open_price + _x, 2)
            else:
                if move >= be_sl_floor + be_buffer:
                    _x = max(be_buffer, move - be_sl_floor)
                    be_sl = round(open_price + _x, 2)
                else:
                    be_sl = current_sl
            better = (current_sl == 0) or \
                     (pos_type == "buy" and be_sl > current_sl) or \
                     (pos_type == "sell" and be_sl < current_sl)
            new_sl = be_sl if better else None
            logger.info(
                f"[smart_exit] 💰 浮盈达标锁50%: ticket={position.get('ticket')} "
                f"峰值{peak_move:.2f}({peak_move/atr:.2f}×ATR) 当前{move:.2f}({move/atr:.2f}×ATR) "
                f"SL地板={new_sl} → 平50%锁利"
            )
            return {
                "action": "partial_close",
                "close_pct": 0.5,
                "new_sl": new_sl,
                "new_tp": None,
                "reason": f"浮盈达标锁50%(峰值{peak_move:.1f},当前{move:.1f},均≥0.3ATR)",
            }

    # ===== ★ 早期保本 + 动态利润锁定（调研支撑）=====
    # 黄金 XAUUSD 波动大，浮盈后必须阶梯上移 SL，而非一次保本就停。
    # 参考：Quantum Algo 50%@1R/30%@2R/20%runner；Pro-Scalper BE@10-15p trail@20-25p；
    #       Volity "Ratchet" 随浮盈扩大逐步收紧 ATR 乘数；Chandelier Exit 用 ATR 跟踪极值。
    if enable_trail:
        be_buffer = float(_cfg(strategy, "breakeven_buffer_points", 0.5) or 0.5)
        # ★★★ 2026-08-13 根因修复：保本 SL 必须躲开「噪音区」 ★★★
        # 故障现场（用户反馈）：詹启东三跟号 SELL 仓位「刚止损就行情反转跌下来」——
        #   原 BE_EARLY_ATR_MULT=0.08 → 仅 +1.2 点浮盈即把 SELL SL 移到 open+0.5(仅0.5点)，
        #   该 SL 距现价仅 ~1.7 点，落在黄金正常回调噪音(1~3点)/扫流动性(15~25点)里 →
        #   价格正常回踩即被扫，随后行情继续原方向(用户见"反转")。
        #   根因：MIN_SL_DIST 硬下限只约束「开仓初始 SL」(compute_initial_sl_tp)，不约束
        #        「开仓后移动的 SL」→ 保本移动可生成极近止损盲区。
        #   调研(pro-scalper: BE buffer 1~3pips 但 SL 至少 1.0×ATR 避开噪音；sunburstmarkets:
        #        SL≥1.0×ATR 避免正常噪音；cihtexpo: 黄金扫流动性常延伸15~25点才反转)。
        #   修法（提准非拦截·零交易笔数影响）：保本 SL 距现价须 ≥ be_sl_floor，
        #   be_sl_floor = max(MIN_SL_DIST, be_sl_atr_mult×atr)。
        #   SELL: SL=open+max(be_buffer, be_sl_floor-move) → 距现价≥floor，随 move 增大收紧到 open+buffer(真保本)。
        #   BUY : 仅 move≥floor+be_buffer 才有安全锁利SL(open+max(be_buffer,move-floor))，否则保留初始宽SL。
        MIN_SL_DIST = float(_cfg(strategy, "min_sl_distance", 8.0))
        be_sl_atr_mult = float(_cfg(strategy, "be_sl_atr_mult", 1.0))
        be_sl_floor = max(MIN_SL_DIST, be_sl_atr_mult * atr)
        _peak_move = peak_move if (peak_move is not None and peak_move > 0) else move
        _effective_move = max(move, _peak_move) if move > 0 else _peak_move
        if _effective_move > 0:
            logger.info(f"[smart_exit] pos={pos_type} move={move:.1f} floor={be_sl_floor:.1f} sl={current_sl} open={open_price}")
        if _effective_move >= be_sl_floor + be_buffer:
            # 噪音安全保本 SL（SELL/BUY 通用，保证 SL 距现价 ≥ be_sl_floor）
            if pos_type == "sell":
                _x = max(be_buffer, be_sl_floor - move)
                be_sl = round(open_price + _x, 2)
            else:
                _x = max(be_buffer, move - be_sl_floor)
                be_sl = round(open_price + _x, 2)
            reason = f"保本(+{_effective_move:.1f}点)→SL移入(open±{_x:.1f},距地板{be_sl_floor:.1f})"
            better = (current_sl == 0) or \
                     (pos_type == "buy" and be_sl > current_sl) or \
                     (pos_type == "sell" and be_sl < current_sl)
            if better:
                logger.info(f"[smart_exit] ✅ {reason}: ticket={position.get('ticket')} {pos_type} move={move:.1f} peak_move={_peak_move:.1f} → SL→{be_sl}")
                return {
                    "action": "hold",
                    "close_pct": 0,
                    "new_sl": be_sl,
                    "new_tp": None,
                    "reason": reason,
                }

    if not smart_tp:
        return {"action": "hold", "close_pct": 0, "new_sl": None, "new_tp": None, "reason": "智能分批止盈未启用"}

    # ===== ① 4 级分批止盈 =====
    new_sl = None

    # TP3 已到（move_atr ≥ tp3_mult）→ 平 20%
    if move_atr >= tp3_mult and tp3_pct > 0:
        return {
            "action": "partial_close",
            "close_pct": tp3_pct,
            "new_sl": _maybe_trail(current_sl, pos_type, current_price, atr, trail_mult, True),
            "new_tp": None,
            "reason": f"TP3触发({move_atr:.2f}≥{tp3_mult}×ATR)→平{tp3_pct:.0%}",
        }

    # TP2 已到 → 平 30%
    if move_atr >= tp2_mult and tp2_pct > 0:
        new_sl = _maybe_trail(current_sl, pos_type, current_price, atr, trail_mult, True)
        return {
            "action": "partial_close",
            "close_pct": tp2_pct,
            "new_sl": new_sl,
            "new_tp": None,
            "reason": f"TP2触发({move_atr:.2f}≥{tp2_mult}×ATR)→平{tp2_pct:.0%}",
        }

    # TP1 已到 → 平 40% + 触发保本
    if move_atr >= tp1_mult and tp1_pct > 0:
        new_sl = current_sl
        if be_enabled:
            be_buffer = float(_cfg(strategy, "breakeven_buffer_points", 0.5) or 0.5)
            MIN_SL_DIST = float(_cfg(strategy, "min_sl_distance", 8.0))
            be_sl_atr_mult = float(_cfg(strategy, "be_sl_atr_mult", 1.0))
            be_sl_floor = max(MIN_SL_DIST, be_sl_atr_mult * atr)
            # 2026-08-13 修复：保本 SL 距现价须 ≥ be_sl_floor（噪音安全）。
            if pos_type == "sell":
                _x = max(be_buffer, be_sl_floor - move)
                be_sl = round(open_price + _x, 2)
            else:
                if move >= be_sl_floor + be_buffer:
                    _x = max(be_buffer, move - be_sl_floor)
                    be_sl = round(open_price + _x, 2)
                else:
                    be_sl = current_sl
            if pos_type == "buy":
                if be_sl > current_sl:
                    new_sl = be_sl
            else:
                if current_sl == 0 or be_sl < current_sl:
                    new_sl = be_sl
        return {
            "action": "partial_close",
            "close_pct": tp1_pct,
            "new_sl": new_sl,
            "new_tp": None,
            "reason": f"TP1触发({move_atr:.2f}≥{tp1_mult}×ATR)→平{tp1_pct:.0%}+保本",
        }

    # ===== ③ TP2 已触发后，追踪止损保护利润（受 enable_trailing_sl 控制）=====
    if enable_trail and trail_after_tp2 and move_atr >= tp2_mult * 0.8:
        # 价格已接近 TP2 但还没到，也开始保护性追踪
        new_sl = _maybe_trail(current_sl, pos_type, current_price, atr, trail_mult, False)
        if new_sl is not None and new_sl != current_sl:
            return {
                "action": "hold",
                "close_pct": 0,
                "new_sl": new_sl,
                "new_tp": None,
                "reason": f"追踪保护(SL→{new_sl})",
            }

    # ===== ★ 追踪止盈（让利润奔跑）=====
    # 盈利达到 TP1 的 50% 时开始追踪：TP 随价格上移，锁住已实现利润的同时不封顶
    # 激活条件：move_atr ≥ tp1_mult×0.5 且 当前有原生 TP（>0）
    if move_atr >= tp1_mult * 0.5 and current_tp > 0:
        trailed_tp = _trail_tp(current_tp, pos_type, current_price, atr, trail_mult)
        # HIGH 质量：追踪 TP 上限/下限 = Chronos 远端分位数（BUY→P90 上界, SELL→P10 下界）
        # ★ 2026-08-16 审计终检修复：封顶钳位后须加方向护栏（BUY 只上移 / SELL 只下移）——
        #   P90/P10 逐轮重算可能下移，纯钳位后 trailed_tp 可能 < current_tp（BUY）→ L415 判
        #   != 即改单 → TP 被下调，违背"只上移不回调"（利润提前落袋）。
        if _q == "HIGH":
            if pos_type == "buy" and chronos_tp_ceiling and trailed_tp is not None:
                _ceil = _scalar(chronos_tp_ceiling)
                if trailed_tp > _ceil:
                    trailed_tp = round(_ceil, 2)
                # 护栏：绝不比当前 TP 低
                if trailed_tp < current_tp:
                    trailed_tp = None
            elif pos_type == "sell" and chronos_p10 is not None and trailed_tp is not None:
                _floor = _scalar(chronos_p10)
                if trailed_tp < _floor:  # SELL 追踪 TP 不该跌破 P10（下界），否则过度追空
                    trailed_tp = round(_floor, 2)
                # 护栏：绝不比当前 TP 高
                if trailed_tp > current_tp:
                    trailed_tp = None
        if trailed_tp is not None and trailed_tp != current_tp:
            logger.info(
                f"[smart_exit] 📈 追踪止盈: ticket={position.get('ticket')} "
                f"{pos_type} TP {current_tp}→{trailed_tp} (价={current_price}, 利润奔跑中)"
            )
            return {
                "action": "hold",
                "close_pct": 0,
                "new_sl": None,
                "new_tp": trailed_tp,
                "reason": f"追踪止盈(TP→{trailed_tp}, 利润奔跑中)",
            }

    return {
        "action": "hold",
        "close_pct": 0,
        "new_sl": None,
        "new_tp": None,
        "reason": f"持仓中({move_atr:.2f}×ATR, P/L={profit:+.2f})",
    }


def _maybe_trail(current_sl, pos_type, current_price, atr, trail_mult, force) -> Optional[float]:
    """
    计算追踪止损（只往有利方向移动 SL，不回调）

    force=True: 强制按当前价更新（用于 TP 触发后）
    force=False: 仅当新 SL 比当前 SL 更优时更新
    """
    if atr <= 0 or trail_mult <= 0:
        return None

    trail_distance = atr * trail_mult
    if pos_type == "buy":
        new_sl = round(current_price - trail_distance, 2)
        if force or new_sl > current_sl:
            return new_sl
    else:  # sell
        new_sl = round(current_price + trail_distance, 2)
        if force or (current_sl == 0 or new_sl < current_sl):
            return new_sl
    return None


def _trail_tp(current_tp, pos_type, current_price, atr, trail_mult) -> Optional[float]:
    """
    ★ 追踪止盈（2026-08-05 新增）—— 让利润真正奔跑
    ★★ 2026-08-16 审计P0-1修复：旧实现方向反了——BUY 写成 current_price - trail_distance
       （TP 压到现价下方 → 要么 broker 拒绝 Invalid stops 静默失效，要么瞬间市价平仓），
       SELL 同理反向。正确：BUY TP 应随价格上涨而上移（TP = price + distance，只上移不回调）；
       SELL TP 应随价格下跌而下移（TP = price - distance，只下移不回调）。

    与 _maybe_trail（追踪止损）对称设计：
      - 追踪止损：锁住下方利润（防由赢转亏）
      - 追踪止盈：抬高上方天花板（让利润奔跑）
      - 两者配合 → "保底 + 不封顶"的完整出场框架
    """
    if atr <= 0 or trail_mult <= 0 or current_tp <= 0:
        return None

    # 追踪止盈距离稍短于追踪止损（更敏感地上移）
    trail_distance = atr * trail_mult * 0.8
    min_gap = atr * 0.3  # TP 与当前价最小间距（避免太近被噪音触发）

    if pos_type == "buy":
        # BUY：TP 高于现价才有效 → 上移 + 保持最小间距
        new_tp = round(current_price + trail_distance, 2)
        if new_tp > current_tp and new_tp > current_price + min_gap:
            return new_tp
    else:  # sell
        # SELL：TP 低于现价才有效 → 下移 + 保持最小间距
        new_tp = round(current_price - trail_distance, 2)
        if (current_tp == 0 or new_tp < current_tp) and new_tp < current_price - min_gap:
            return new_tp
    return None


def compute_initial_sl_tp(
    *,
    side: str,        # 'BUY' / 'SELL'
    entry_price: float,
    atr: float,
    strategy,
    quality_regime: str = "",        # v4 Meta 质量陪审团: HIGH/MID/LOW/VERY_LOW
    chronos_tp_ceiling: Optional[float] = None,  # Chronos P90 末价（BUY 盈利目标）
    chronos_p10: Optional[float] = None,         # Chronos P10 末价（SELL 盈利目标）
    structure_sl: Optional[float] = None,        # ★ 2026-08-13 结构锚定 SL（摆动失效位）
    structure_tp: Optional[float] = None,        # ★ 2026-08-13 结构锚定 TP（下一结构目标）
) -> dict:
    """
    计算开仓时的初始 SL/TP

    ★ 修复(2026-08-05)：原生 TP 改为 TP3（远端安全网），不再设 TP1。
    
    原缺陷：MT5 原生 TP=TP1(entry+1.0ATR) 是硬天花板，价格一触及经纪商就全平，
           smart_exit 的 TP2/TP3 分批+runner 永远没机会执行 → "喝汤不吃肉"。
    
    新设计：
      - 原生 TP = TP3(entry + tp3_mult×ATR) 作为极端安全网（系统崩溃兜底）
      - TP1/TP2 由 smart_exit.evaluate_position 通过市价部分平仓执行
      - 追踪止盈由 _trail_tp 每周期上移 MT5 原生 TP（让利润奔跑）

    ★ v4 Meta 质量陪审团（2026-08-06 接入）：止盈 regime 决定初始 SL/TP（提准非拦截）
      - HIGH(≥0.7): 原生 TP = Chronos P90 天花板，让利润真正奔跑到远端分位数才落袋；
      - MID(0.5~0.7): 常规——原生 TP = TP3 安全网；
      - LOW(0.35~0.5)/VERY_LOW(<0.35): 啃头皮——收紧 SL(1.0×ATR) 与 TP 安全网(≈TP1)，
        快进快出，但照常开仓（绝不减交易笔数）。
    """
    smart_tp = bool(_cfg(strategy, "smart_tp_enabled", True))
    tp1_mult = float(_cfg(strategy, "tp1_atr_mult", 1.0))
    tp3_mult = float(_cfg(strategy, "tp3_atr_mult", 2.5))
    is_buy = side.upper() == "BUY"

    # ★★★ 2026-08-13 根因修复：ATR 硬下限 + SL 最小距离硬下限 ★★★
    # 故障现场：主号 #381546204 SELL 0.01 @4406.39 的 sl=4407.39（仅 100 点），
    #   而同主号同分钟其他单 SL 均 2892 点 —— 唯独这笔开仓瞬间 atr 被取到异常极小值
    #   （~1.0 而非正常 20+），sl_dollar=1.5×atr 算出 100 点窄 SL → 金价波动 1 点即
    #   broker 实时秒扫，AI 周期级锁利(peak_move 回吐≥30%平)永远赶不上 → 浮盈回吐到
    #   保本被扫（exit_reason=mt5_closed_external_unverified）。即「AI 大脑失明」真因。
    # 修法（纯加法护栏，符合「提准非拦截」）：
    #   ① atr 硬下限：低于 MIN_ATR 视为异常，强制用兜底值，杜绝窄 SL 源；
    #   ② SL 最小距离硬下限：sl_dollar 无论如何 ≥ MIN_SL_DIST，斩断 100 点秒扫。
    # 黄金日波动大，MIN_ATR=10（价格单位≈1000 点）、MIN_SL_DIST=8.0（≈800 点）为安全下限，
    #   可被 config 覆盖。此护栏不改开仓方向/风控逻辑，交易笔数/净利/PF 不受影响。
    MIN_ATR = float(_cfg(strategy, "min_atr_floor", 10.0))
    MIN_SL_DIST = float(_cfg(strategy, "min_sl_distance", 8.0))
    if atr is None or atr <= 0 or atr < MIN_ATR:
        atr = max(MIN_ATR, float(atr or 0)) if (atr and atr > 0) else MIN_ATR
    _sl_mult = 1.0 if (quality_regime or "").upper() in ("LOW", "VERY_LOW") else 1.5
    sl_dollar = max(_sl_mult * atr, MIN_SL_DIST)

    if not smart_tp:
        sl_dollar = max(1.5 * atr, MIN_SL_DIST)
        if is_buy:
            return {"sl": round(entry_price - sl_dollar, 2), "tp": round(entry_price + 3.0 * atr, 2)}
        else:
            return {"sl": round(entry_price + sl_dollar, 2), "tp": round(entry_price - 3.0 * atr, 2)}

    # smart_tp 模式：原生 TP 设为远端安全网，分批由 evaluate_position 接管
    sl_dollar = max(1.5 * atr, MIN_SL_DIST)
    _q = (quality_regime or "").upper()

    if _q == "HIGH":
        # 让利润奔跑：原生 TP = 盈利方向远端分位数（BUY→P90 上界, SELL→P10 下界）
        # 仅在盈利方向采用，否则回退 TP3 安全网
        if is_buy and chronos_tp_ceiling is not None and _scalar(chronos_tp_ceiling) > entry_price:
            _tp = round(_scalar(chronos_tp_ceiling), 2)
            logger.info(f"[smart_exit] HIGH质量开仓: 原生TP=P90天花板{_tp:.2f}(让利润奔跑)")
        elif (not is_buy) and chronos_p10 is not None and _scalar(chronos_p10) < entry_price:
            _tp = round(_scalar(chronos_p10), 2)
            logger.info(f"[smart_exit] HIGH质量开仓: 原生TP=P10地板{_tp:.2f}(让利润奔跑)")
        else:
            _tp = round(entry_price + (tp3_mult if is_buy else -tp3_mult) * atr, 2)
    elif _q in ("LOW", "VERY_LOW"):
        # 啃头皮：收紧 SL 与 TP 安全网（快进快出，但照常开仓）
        sl_dollar = max(1.0 * atr, MIN_SL_DIST)
        _tp = round(entry_price + (tp1_mult if is_buy else -tp1_mult) * atr, 2)
        logger.info(f"[smart_exit] {_q}质量开仓: 收紧SL/TP(啃头皮 scalp)")
    else:
        # MID / 默认：原生 TP = TP3 远端安全网
        _tp = round(entry_price + (tp3_mult if is_buy else -tp3_mult) * atr, 2)

    # ★★★ 2026-08-13 结构锚定 SL/TP（加法·ATR 兜底·零行为变化）★★★
    # 把 SL 从「统计距 entry±1.5×ATR」升级为「结构失效位」：
    #   BUY → SL 挂在当前价下方最近摆动低点（跌破=多头结构破→止损，不扛假突破）
    #   SELL → SL 挂在当前价上方最近摆动高点（涨破=空头结构破→止损）
    # 边界护栏（防异常/防爆仓）：
    #   ① 结构距 < MIN_SL_DIST → 太贴近，弃用回退 ATR（避免 2026-08-13 窄 SL 秒扫）
    #   ② 结构距 > MAX_SL_ATR_MULT×ATR → 结构过远（极端趋势），钳到上限（防手数过小/风险失真）
    #   ③ 方向校验：BUY 的 SL 必须 < entry；SELL 必须 > entry，否则弃用
    # 无结构锚(available=False/推不出) → 全程不触发，行为与旧版完全一致。
    _max_sl_mult = float(_cfg(strategy, "max_sl_atr_mult", 6.0))
    _max_sl = max(MIN_SL_DIST, _max_sl_mult * atr)
    _struct_sl = None
    if structure_sl is not None and structure_sl > 0:
        _sd = abs(entry_price - structure_sl)
        _dir_ok = (is_buy and structure_sl < entry_price) or (not is_buy and structure_sl > entry_price)
        if _dir_ok and MIN_SL_DIST <= _sd <= _max_sl:
            _struct_sl = round(structure_sl, 2)
            logger.info(
                f"[smart_exit] 结构锚定SL: 用结构位{_struct_sl:.2f}替代ATR距"
                f"(距{_sd:.0f}点∈[{MIN_SL_DIST:.0f},{_max_sl:.0f}])"
            )
        else:
            logger.debug(
                f"[smart_exit] 结构SL弃用(方向{_dir_ok}/距{_sd:.0f}∉[{MIN_SL_DIST:.0f},{_max_sl:.0f}])→ATR兜底"
            )
    # 结构 TP 延伸（只拉远不拉近·避免砍赢家）：结构目标比 ATR/Chronos 默认更远才采用
    if structure_tp is not None and structure_tp > 0:
        if is_buy and structure_tp > _tp and structure_tp > entry_price + MIN_SL_DIST:
            _tp = round(structure_tp, 2)
            logger.info(f"[smart_exit] 结构锚定TP: 延伸到结构目标{_tp:.2f}(更远·让利润奔跑)")
        elif (not is_buy) and structure_tp < _tp and structure_tp < entry_price - MIN_SL_DIST:
            _tp = round(structure_tp, 2)
            logger.info(f"[smart_exit] 结构锚定TP: 延伸到结构目标{_tp:.2f}(更远·让利润奔跑)")

    if is_buy:
        return {
            "sl": _struct_sl if _struct_sl is not None else round(entry_price - sl_dollar, 2),
            "tp": _tp,   # ← 远端安全网（HIGH=P90 / MID=TP3 / LOW=TP1）/ 结构延伸
        }
    else:
        return {
            "sl": _struct_sl if _struct_sl is not None else round(entry_price + sl_dollar, 2),
            "tp": _tp,
        }
