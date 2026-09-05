"""
万象Ai XAUUSD — SMC/ICT 机构订单流特征引擎（2026 前沿方案·大脑换血核心）

输入：MT5 多周期 OHLCV 序列（来自 market_analyzer._build_from_raw 的 raw['timeframes']）
输出：结构化机构足迹（Order Block / FVG / BOS-CHoCH / Liquidity Sweep / 溢价折扣区）

调研支撑（2026-08-05 海外交叉验证，≥3 独立源）：
  - Informatica 学术期刊(2026-02)：SMC/ICT 确定性原语提取（OB/FVG/BOS/流动性扫）
  - HuggingFace XAUUSD-ML-Trader：SMC 是该集成的「最盈利策略」，86 特征之一
  - algomatrix.trade / quantum-algo：黄金上 SMC 显著优于 RSI/MACD（胜率 41%→75%）
  - Liquidity Hunters / Trading Club AI(2026)：SMC 结构层 + 多因子融合实战

设计铁律：
  - 纯行情特征，全局共享（行情主号拉取），不绑定任何账号 → 天然多账号优先
  - 输出结构精简（每周期最多保留 4 个 OB/FVG），避免喂给 LLM 爆 token
  - 不写死交易规则，只把「机构足迹」客观呈现给 AI，由 AI 综合判断
"""
from typing import Optional


# ─────────────────────────── 基础指标 ───────────────────────────
def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < 2:
        return 0.0
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    # ★ 2026-08-15 审计P2修复：统一 **Wilder 递推**（与 market_analyzer._calc_single_atr /
    #   regime_detect._atr 同口径）——原简单平均导致同一 ATR(14) 三套值，结构锚 min_gap
    #   与 regime extension_z 各自为政，融合语义漂移。
    prev = sum(trs[:period]) / period
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
    return prev


def _sma(vals: list, period: int) -> float:
    if len(vals) < period or period <= 0:
        return vals[-1] if vals else 0.0
    return sum(vals[-period:]) / period


def _rsi(closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


# ─────────────────────────── 摆动点 ───────────────────────────
def _swing_points(highs: list, lows: list, left: int = 3, right: int = 3) -> list:
    """返回摆动点列表：{'idx','type':'high'/'low','price','time'}"""
    swings = []
    n = len(highs)
    for i in range(left, n - right):
        # 摆动高
        if highs[i] >= max(highs[i - left:i] + highs[i + 1:i + right + 1]):
            swings.append({"idx": i, "type": "high", "price": highs[i]})
        # 摆动低
        elif lows[i] <= min(lows[i - left:i] + lows[i + 1:i + right + 1]):
            swings.append({"idx": i, "type": "low", "price": lows[i]})
    return swings


# ─────────────────────────── FVG（公允价值缺口） ───────────────────────────
def _detect_fvg(bars: list, max_keep: int = 4) -> list:
    """
    三K线失衡检测：
      bullish FVG：low[i] > high[i-2]，缺口区间 [high[i-2], low[i]]
      bearish FVG：high[i] < low[i-2]，缺口区间 [low[i-2], high[i]]
    新鲜度随年龄(距当前bar数) + 距价格距离衰减。
    """
    out = []
    n = len(bars)
    for i in range(2, n):
        h2, l2 = bars[i - 2]["high"], bars[i - 2]["low"]
        hi, li = bars[i]["high"], bars[i]["low"]
        age = (n - 1) - i
        if li > h2:  # bullish
            top, bottom = li, h2
            kind = "bullish"
        elif hi < l2:  # bearish
            top, bottom = l2, hi
            kind = "bearish"
        else:
            continue
        gap = top - bottom
        if gap <= 0:
            continue
        fresh = __import__("math").exp(-0.05 * age)
        out.append({
            "type": kind,
            "top": round(top, 2),
            "bottom": round(bottom, 2),
            "gap": round(gap, 2),
            "freshness": round(fresh, 3),
            "created_at": bars[i].get("time", ""),
        })
    # 保留最新鲜的
    out.sort(key=lambda x: x["freshness"], reverse=True)
    return out[:max_keep]


# ─────────────────────────── Order Block（订单块） ───────────────────────────
def _detect_ob(bars: list, atr: float, max_keep: int = 4) -> list:
    """
    OB = 位移(displacement)前最后一根反向K线。
    displacement 判定：实体幅度 >= tau*ATR（tau=1.5，论文口径）。
      bullish OB：位移向上前最后一根阴线（close<open），区间为 [open, low]
      bearish OB：位移向下前最后一根阳线（close>open），区间为 [high, open]
    """
    import math
    out = []
    n = len(bars)
    tau = 1.5
    thresh = tau * atr if atr > 0 else 0
    for i in range(1, n):
        body = abs(bars[i]["close"] - bars[i]["open"])
        if thresh > 0 and body < thresh:
            continue
        direction = "up" if bars[i]["close"] > bars[i]["open"] else "down"
        ob_idx = i - 1
        if ob_idx < 0:
            continue
        ob = bars[ob_idx]
        age = (n - 1) - ob_idx
        if direction == "up" and ob["close"] < ob["open"]:
            zone_top, zone_bottom = ob["open"], ob["low"]
            kind = "bullish"
        elif direction == "down" and ob["close"] > ob["open"]:
            zone_top, zone_bottom = ob["high"], ob["open"]
            kind = "bearish"
        else:
            continue
        # 强度：位移越大越强；新鲜度随年龄衰减
        strength = min(1.0, body / (thresh * 2 + 1e-9))
        fresh = math.exp(-0.04 * age)
        out.append({
            "type": kind,
            "zone_top": round(zone_top, 2),
            "zone_bottom": round(zone_bottom, 2),
            "strength": round(strength, 3),
            "freshness": round(fresh, 3),
            "created_at": ob.get("time", ""),
        })
    out.sort(key=lambda x: (x["freshness"] + x["strength"]) / 2, reverse=True)
    return out[:max_keep]


# ─────────────────────────── Liquidity Sweep（流动性扫荡） ───────────────────────────
def _detect_sweep(bars: list, swings: list, max_keep: int = 4) -> list:
    """
    影线刺穿摆动高低点但实体收回 = 扫流动性（止损猎杀）后反转。
      up sweep：high[i] > 最近摆动高 且 close[i] <= 摆动高
      down sweep：low[i] < 最近摆动低 且 close[i] >= 摆动低
    """
    out = []
    n = len(bars)
    # 预存摆动高低点序列
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    for i in range(1, n):
        hi, lo, c = bars[i]["high"], bars[i]["low"], bars[i]["close"]
        # 找该bar之前最近的摆动点
        last_h = max((s["price"] for s in highs if s["idx"] < i), default=None)
        last_l = min((s["price"] for s in lows if s["idx"] < i), default=None)
        if last_h is not None and hi > last_h and c <= last_h:
            out.append({"type": "up", "level": round(last_h, 2),
                        "at": bars[i].get("time", ""), "close": round(c, 2)})
        elif last_l is not None and lo < last_l and c >= last_l:
            out.append({"type": "down", "level": round(last_l, 2),
                        "at": bars[i].get("time", ""), "close": round(c, 2)})
    # 保留最近
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:max_keep]


# ─────────────────────────── Premium / Discount（溢价折扣区） ───────────────────────────
def _premium_discount(bars: list) -> dict:
    """基于近期 swing range 的 OTE 区（HuggingFace / Informatica 口径）。
    discount 区（偏多入场）=[H-0.79(H-L), H-0.62(H-L)]；
    premium 区（偏空入场）=[L+0.62(H-L), L+0.79(H-L)]。
    """
    recent = bars[-60:]
    if not recent:
        return {"available": False}
    H = max(b["high"] for b in recent)
    L = min(b["low"] for b in recent)
    rng = H - L
    if rng <= 0:
        return {"available": False}
    ote_buy_bottom = H - 0.79 * rng
    ote_buy_top = H - 0.62 * rng
    ote_sell_bottom = L + 0.62 * rng
    ote_sell_top = L + 0.79 * rng
    cur = bars[-1]["close"]
    if cur >= ote_sell_bottom:
        zone = "premium"      # 高位区，禁追多
    elif cur <= ote_buy_top:
        zone = "discount"     # 低位区，禁追空
    else:
        zone = "neutral"
    return {
        "available": True,
        "swing_high": round(H, 2),
        "swing_low": round(L, 2),
        "ote_buy": [round(ote_buy_bottom, 2), round(ote_buy_top, 2)],
        "ote_sell": [round(ote_sell_bottom, 2), round(ote_sell_top, 2)],
        "current_zone": zone,
    }


# ─────────────────────────── BOS / CHoCH 方向 ───────────────────────────
def _last_bos_direction(swings: list, closes: list) -> Optional[str]:
    """中长期结构方向（bullish=buy偏向, bearish=sell偏向）。

    修复(2026-08-05)：旧版只看「最后一段摆动」，震荡上涨里的最后一段回调
    会被误判为 bearish，与 Regime 的 strong uptrend 直接打架，导致 AI 在
    强趋势里被「全局偏空」误导而永远等回调。现改为看最近 80 根摆动点的
    净方向（高点是否抬高 / 低点是否抬高），避免单根回调翻转整体偏向。
    """
    if not swings:
        return None
    recent = [s for s in swings if s["idx"] >= len(closes) - 80]
    recent = sorted(recent, key=lambda s: s["idx"])
    if len(recent) < 4:
        return None
    highs = [s["price"] for s in recent if s["type"] == "high"]
    lows = [s["price"] for s in recent if s["type"] == "low"]
    bull = 0
    bear = 0
    if len(highs) >= 2:
        if highs[-1] > highs[0]:
            bull += 1
        elif highs[-1] < highs[0]:
            bear += 1
    if len(lows) >= 2:
        if lows[-1] > lows[0]:   # 低点抬高 = 上涨结构
            bull += 1
        elif lows[-1] < lows[0]:  # 低点降低 = 下跌结构
            bear += 1
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return None


# ─────────────────────────── 主入口 ───────────────────────────
def compute_smc(tfs_raw: dict) -> dict:
    """
    tfs_raw: {tf_name: {"bars": [{open,high,low,close,volume,time}]}}
    返回 {"per_tf": {...}, "global_bias": "bullish"/"bearish"/"neutral", "tf_count": N}
    """
    per_tf = {}
    weighted_bias = 0.0
    total_weight = 0.0
    # 高周期对全局偏向权重更高：H4/H1 主导，M15/M5 确认，M1 仅参考。
    tf_weights = {"H4": 4.0, "H1": 3.0, "M15": 1.5, "M5": 0.8, "M1": 0.3}
    for tf, data in tfs_raw.items():
        bars = data.get("bars") or []
        if len(bars) < 30:
            per_tf[tf] = {"available": False}
            continue
        closes = [b.get("close") for b in bars if isinstance(b, dict)]
        highs = [b.get("high") for b in bars if isinstance(b, dict)]
        lows = [b.get("low") for b in bars if isinstance(b, dict)]
        closes = [x for x in closes if x is not None]
        highs = [x for x in highs if x is not None]
        lows = [x for x in lows if x is not None]
        if len(closes) < 30 or len(highs) < 30 or len(lows) < 30:
            per_tf[tf] = {"available": False}
            continue
        atr = _atr(highs, lows, closes, 14)
        swings = _swing_points(highs, lows, left=3, right=3)
        fvgs = _detect_fvg(bars)
        obs = _detect_ob(bars, atr)
        sweeps = _detect_sweep(bars, swings)
        pd = _premium_discount(bars)
        bias = _last_bos_direction(swings, closes) or "neutral"
        w = tf_weights.get(tf, 0.5)
        if bias == "bullish":
            weighted_bias += w
        elif bias == "bearish":
            weighted_bias -= w
        total_weight += w
        per_tf[tf] = {
            "available": True,
            "bias": bias,
            "order_blocks": obs,
            "fvgs": fvgs,
            "liquidity_sweeps": sweeps,
            "premium_discount": pd,
            "atr": round(atr, 2),
            "rsi": round(_rsi(closes, 14), 1),
        }

    # 需要显著优势才给出全局偏向（避免噪声平局）
    if total_weight > 0:
        score = weighted_bias / total_weight
    else:
        score = 0.0

    # ★ 2026-08-12 审计修复：此前 SMC 只吃 OHLCV 价格结构，未参考宏观镜像(DXY/VIX)。
    # DXY 与黄金强负相关：DXY 走强→黄金偏空→对 bullish 评分打折、bearish 加权，
    # 防止下跌市里反弹波段误报 bullish（正是「全开 BUY 全亏」的根因之一）。
    try:
        from app.services.market_data import market_data_provider
        _ext = market_data_provider.get_external_snapshot()
        _dxy = (_ext.get("dxy") or {})
        _corr = (_ext.get("correlation") or {})
        _dxy_chg = float(_corr.get("dxy_change_1d") or _dxy.get("change_pct") or 0.0)
        if _dxy_chg > 0.1:       # DXY 当日走强 → 黄金偏空语境
            score -= 0.15
        elif _dxy_chg < -0.1:    # DXY 当日走弱 → 黄金偏多语境
            score += 0.15
    except Exception:
        pass

    if score >= 0.35:
        global_bias = "bullish"
    elif score <= -0.35:
        global_bias = "bearish"
    else:
        global_bias = "neutral"

    return {"per_tf": per_tf, "global_bias": global_bias, "tf_count": len(per_tf), "bias_score": round(score, 3)}


# ─────────────────────────── 结构锚点推导（供开仓 SL/TP 锚定结构位）───────────────────────────
def derive_structure_anchors(tfs_raw: dict, current_price: float = 0.0) -> dict:
    """
    ★ 2026-08-13 新增（用户「多结构感知落地到开单/平仓」需求）：
    从多周期 OHLCV 推导「结构止损/止盈锚点」，让 compute_initial_sl_tp 把 SL 挂在
    结构失效位（摆动低点/高点）、TP 挂到下一结构目标（流动性池/摆动点），而非
    统计距 entry±1.5×ATR。这正是用户两张 H4 图方法论的落地：
      - 真突破（图1）= 结构不破 → SL 在结构外，给行情奔跑空间，可跟；
      - 假突破/多头陷阱（图2）= 结构破 → 价格跌破 SL 锚 → 立即止损，不扛。

    返回（价格单位=美元报价，如 XAUUSD 4400.xx）：
      sl_anchor_buy : 做多失效位（当前价下方最近摆动低点，跌破=多头结构破）
      sl_anchor_sell: 做空失效位（当前价上方最近摆动高点，涨破=空头结构破）
      tp_anchor_buy : 做多目标位（当前价上方最近摆动高点/流动性池）
      tp_anchor_sell: 做空目标位（当前价下方最近摆动低点/流动性池）
      available     : 是否成功推出可用锚
      source_tf     : 取自哪个周期（H1 优先，M15 兜底）

    设计铁律：纯行情、无副作用、零行为变化兜底——
      推不出有效锚（数据不足/无摆动点）→ available=False，compute_initial_sl_tp 回退 ATR。
    """
    out = {
        "sl_anchor_buy": None, "sl_anchor_sell": None,
        "tp_anchor_buy": None, "tp_anchor_sell": None,
        "available": False, "source_tf": "",
    }
    if current_price <= 0:
        return out
    # 优先 H1（中周期结构最稳），无则 M15，再 H4 兜底
    for tf in ("H1", "M15", "H4"):
        d = (tfs_raw or {}).get(tf, {}) or {}
        bars = d.get("bars") or []
        if len(bars) < 30:
            continue
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        atr = _atr(highs, lows, closes, 14)
        swings = _swing_points(highs, lows, left=3, right=3)
        if not swings:
            continue
        # 只取贴近当前的近 60 根摆动点（结构时效：远摆动点已失效）
        _cut = len(bars) - 60
        sw_lows = sorted(s["price"] for s in swings if s["type"] == "low" and s["idx"] >= _cut)
        sw_highs = sorted(s["price"] for s in swings if s["type"] == "high" and s["idx"] >= _cut)
        # 最小结构间距：锚点至少离当前价 0.3×ATR，避免锚到贴身噪声摆动
        _min_gap = max(2.0, 0.3 * atr)
        # BUY 失效位：当前价下方最近摆动低点（max=最接近当前）
        _buy_sl = [p for p in sw_lows if current_price - p >= _min_gap]
        if _buy_sl:
            out["sl_anchor_buy"] = round(max(_buy_sl), 2)
        # SELL 失效位：当前价上方最近摆动高点（min=最接近当前）
        _sell_sl = [p for p in sw_highs if p - current_price >= _min_gap]
        if _sell_sl:
            out["sl_anchor_sell"] = round(min(_sell_sl), 2)
        # BUY 目标位：当前价上方摆动高点（下一阻力=流动性池）。
        # ★ P1-#3 修复：与 SELL 锚点(max(_sell_tp))对称，取【最远】摆动高点，
        #   使结构 TP 通常 > ATR 默认 TP，让 smart_exit 守卫放行延伸（原 min 取最近高点
        #   常 < ATR TP → 守卫 structure_tp>_tp 永不成立 → BUY 结构 TP 一半失效死代码）。
        _buy_tp = [p for p in sw_highs if p - current_price >= _min_gap]
        if _buy_tp:
            out["tp_anchor_buy"] = round(max(_buy_tp), 2)
        # SELL 目标位：当前价下方最近摆动低点（下一支撑=流动性池）
        _sell_tp = [p for p in sw_lows if current_price - p >= _min_gap]
        if _sell_tp:
            out["tp_anchor_sell"] = round(max(_sell_tp), 2)
        out["source_tf"] = tf
        out["available"] = any(out[k] is not None for k in
                               ("sl_anchor_buy", "sl_anchor_sell", "tp_anchor_buy", "tp_anchor_sell"))
        break  # H1 命中即用，避免更低周期噪声
    return out
