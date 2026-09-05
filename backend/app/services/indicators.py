"""
万象AI — 技术指标计算（纯 Python，无第三方依赖）
输入：bars = [{open, high, low, close, volume, time}, ...]（末位=最新）
输出：各指标当前值 + 均线/布林序列（供前端叠加绘制）+ 趋势判读

指标与 AI 喂给 prompt 的参数同源（ATR14/ADX14/RSI14/EMA20/BOLL/MACD），
面板显示与实际决策一致。
"""
from typing import Dict, Any, List


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = []
    prev = values[0]
    for i, v in enumerate(values):
        prev = v if i == 0 else (v * k + prev * (1 - k))
        out.append(prev)
    return out


def _sma(values: List[float], period: int) -> List[float]:
    out = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def compute_indicators(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "atr": None, "adx": None, "rsi": None,
        "ema20": None, "ema20_dir": "→",
        "boll_upper": None, "boll_mid": None, "boll_lower": None,
        "macd": None, "macd_signal": None, "macd_hist": None,
        "trend": "中性", "ema20_series": [], "boll_upper_series": [], "boll_lower_series": [],
    }
    if not bars or len(bars) < 20:
        return result

    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]

    # ── EMA20 ──
    ema20 = _ema(closes, 20)
    result["ema20"] = round(ema20[-1], 2)
    result["ema20_series"] = [round(x, 2) for x in ema20]
    if len(ema20) >= 3:
        diff = ema20[-1] - ema20[-3]
        if diff > 0:
            result["ema20_dir"] = "↑"
        elif diff < 0:
            result["ema20_dir"] = "↓"
        else:
            result["ema20_dir"] = "→"

    # ── ATR(14) ──
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) >= 14:
        atr = sum(trs[-14:]) / 14
        result["atr"] = round(atr, 2)

    # ── RSI(14) Wilder ──
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    if len(gains) >= 14:
        avg_g = sum(gains[:14]) / 14
        avg_l = sum(losses[:14]) / 14
        for i in range(14, len(gains)):
            avg_g = (avg_g * 13 + gains[i]) / 14
            avg_l = (avg_l * 13 + losses[i]) / 14
        if avg_l == 0:
            rsi = 100.0
        else:
            rs = avg_g / avg_l
            rsi = 100 - 100 / (1 + rs)
        result["rsi"] = round(rsi, 1)

    # ── ADX(14) Wilder ──
    if len(bars) >= 30:
        pdm, mdm, atr_adx = [], [], []
        for i in range(1, len(bars)):
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            pdm.append(max(up, 0) if up > dn else 0)
            mdm.append(max(dn, 0) if dn > up else 0)
            h, l, pc = highs[i], lows[i], closes[i - 1]
            atr_adx.append(max(h - l, abs(h - pc), abs(l - pc)))
        # 初始平滑
        tr0 = sum(atr_adx[:14]) / 14
        pd0 = sum(pdm[:14]) / 14
        md0 = sum(mdm[:14]) / 14
        dx_list = []
        prev_tr, prev_pd, prev_md = tr0, pd0, md0
        for i in range(14, len(atr_adx)):
            prev_tr = (prev_tr * 13 + atr_adx[i]) / 14
            prev_pd = (prev_pd * 13 + pdm[i]) / 14
            prev_md = (prev_md * 13 + mdm[i]) / 14
            if prev_tr > 0:
                pdi = 100 * prev_pd / prev_tr
                mdi = 100 * prev_md / prev_tr
                dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
            else:
                dx = 0
            dx_list.append(dx)
        if dx_list:
            adx = sum(dx_list[-14:]) / len(dx_list[-14:])
            result["adx"] = round(adx, 1)

    # ── Bollinger(20,2) ──
    sma20 = _sma(closes, 20)
    mid_series, up_series, low_series = [], [], []
    for i in range(len(closes)):
        if sma20[i] is None:
            mid_series.append(None); up_series.append(None); low_series.append(None)
        else:
            window = closes[i + 1 - 20:i + 1]
            sd = (sum((x - sma20[i]) ** 2 for x in window) / 20) ** 0.5
            mid_series.append(round(sma20[i], 2))
            up_series.append(round(sma20[i] + 2 * sd, 2))
            low_series.append(round(sma20[i] - 2 * sd, 2))
    result["boll_mid"] = mid_series[-1]
    result["boll_upper"] = up_series[-1]
    result["boll_lower"] = low_series[-1]
    result["boll_mid_series"] = mid_series
    result["boll_upper_series"] = up_series
    result["boll_lower_series"] = low_series

    # ── MACD(12,26,9) ──
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(closes))]
    signal = _ema(macd_line, 9)
    macd_val = macd_line[-1]
    sig_val = signal[-1]
    result["macd"] = round(macd_val, 2)
    result["macd_signal"] = round(sig_val, 2)
    result["macd_hist"] = round(macd_val - sig_val, 2)

    # ── 趋势判读（简化，给客户看的结论，不参与决策）──
    trend = "中性"
    if result["ema20_dir"] == "↑" and (result["adx"] or 0) >= 25:
        trend = "偏多（趋势可信）"
    elif result["ema20_dir"] == "↓" and (result["adx"] or 0) >= 25:
        trend = "偏空（趋势可信）"
    elif (result["adx"] or 99) < 20:
        trend = "震荡（趋势弱）"
    elif result["ema20_dir"] == "↑":
        trend = "偏多（趋势待确认）"
    elif result["ema20_dir"] == "↓":
        trend = "偏空（趋势待确认）"
    result["trend"] = trend

    return result
