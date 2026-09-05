"""
万象Ai XAUUSD — 市场体制检测引擎（三周期架构·海外调研驱动）

输入：MT5 多周期 OHLCV 序列 + 当前价
输出：体制标签 + 4H方向偏置 + 15m结构确认 + 5m入场时机 + 价格延伸度 + 趋势末端风险

★ 2026-08-06 架构重构（海外 ≥3 源调研交叉验证）：
  - quantum-algo.com (XAUUSD专属实测): "4H sets directional bias, 15m provides entry timing"
    只用一对 bias+entry，不是6周期同时投票。XAUUSD 各周期胜率: D1=72% / 4H=65% / 1H=58% / 15m=52% / 5m=48% / 1m=43%
  - LLM-TradeBot (多Agent生产级): 只用 5m/15m/1h 三周期数据，RegimeDetector 识别 Trending/Choppy→调权重，RiskAuditAgent才是唯一有否决权的
  - LARSA (FinRL+DeepSeek学术级): regime 动态调 Ensemble 权重(0.3/0.4/0.5)，不拦截

  核心设计（纠正旧版 6 周期平等投票的架构偏差）：
  - 4H：方向偏置(bias)——定大局方向，做软过滤（meta_agent 据此调权重），不硬拦
  - 15m：结构确认——验证 4H 偏置是否被中周期确认（bridge）
  - 5m：入场时机——短周期动量，决定实际入场节奏
  - M1 数据源已加但不参与体制判读（仅喂 AI 原始 bars 做精准入场）
  - 硬拦截回归 RiskEngine（max_positions / max_lots / drawdown），已存在

核心输出：
  regime: trend_up / trend_down / range / volatile
  direction_bias: 4H 方向 (up/down/neutral) — 供 meta_agent 调节置信权重
  structure_dir_15m: 15m 结构方向 — 验证偏置
  entry_dir_5m: 5m 入场方向 — 实际动量的最后确认
  extension_z / at_stale_top / at_stale_bottom: 趋势末端接飞刀风险（保留，供哨兵+AI参考）

设计铁律：纯行情、全局共享、多账号优先；只输出客观体制，权重调节逻辑在 meta_agent。
"""
import math
from typing import Optional
from loguru import logger


def _efficiency_ratio(closes: list, period: int = 20) -> float:
    """Kaufman 效率比：|净值位移| / 总路径长度。1=完美趋势, 0=原地震荡。"""
    n = len(closes)
    if n < period + 1:
        return 0.0
    window = closes[-period - 1:]
    net = abs(window[-1] - window[0])
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path <= 1e-9:
        return 0.0
    return net / path


def _trend_strength_dir(closes: list, highs: list = None, lows: list = None) -> str:
    """★ 2026-08-17 重写：真正的 SMC/ICT 结构方向判定（HH/HL/LH/LL）。

    原实现（2026-08-06 初版）是纯收盘价涨跌计数（ups≥downs+2 → up），docstring 却写
    "基于连续 HH/HL 判断"——名不符实，且被实盘打脸（用户 2026-08-17 19:22~19:40 事故）：
      M15 高点 4405.81→4404.67→4404.89→4400.65 逐级下移（供给区 LH 三重拒绝），
      但收盘价 18:15 大阳线后连涨 → 旧实现判 up → MarketAnalyzer structure_dir_15m=up
      → 注入决策链 → DS 翻 BUY、AI 出场把正确的 SELL 平掉 → 刚平完行情下跌。
    正确结构语义：价格在供给区下方反弹但高点无法上破 = LH（更低高点）= 空头结构，
    绝不能因为"收盘价涨了几天"判 up（区间上沿追多=接飞刀）。

    新算法（SMC 摆动点结构，海外≥3源交叉验证的行业标准）：
      1. 从 high/low（缺省回退 close）提取最近 2 个摆动高点和 2 个摆动低点
         （摆动点 = 比左右相邻各 1 根更极端的局部极值）
      2. 比较相邻摆动点：高点更高=HH / 更低=LH；低点更高=HL / 更低=LL
      3. 方向裁决：
           HH+HL → up（上升结构）；LH+LL → down（下降结构）
           HH+LL（扩张/突破）→ 看收盘位置：站上最近高点=up，跌破最近低点=down，否则 neutral
           LH+HL（收敛三角）→ neutral（结构未决，禁拍方向）
      4. 摆动点不足（横盘无摆动）→ neutral
    设计铁律：只输出客观结构，不做任何交易拦截（提准非拦截：让信号算得准，不砍信号）。
    """
    n = len(closes)
    if n < 10:
        return "neutral"
    # 用 high/low 判结构（更接近真实摆动）；缺省回退 close（旧调用方兼容）
    highs = highs if highs else closes
    lows = lows if lows else closes
    recent_h = highs[-10:]
    recent_l = lows[-10:]

    def _swings(seq):
        """提取局部极值摆动点（左右各比 1 根更极端）。返回 (values, is_high) 列表。"""
        pts = []
        for i in range(1, len(seq) - 1):
            if seq[i] > seq[i - 1] and seq[i] > seq[i + 1]:
                pts.append((seq[i], True))
            elif seq[i] < seq[i - 1] and seq[i] < seq[i + 1]:
                pts.append((seq[i], False))
        return pts

    _pts = _swings(recent_h) + _swings(recent_l)
    # 只保留最近若干摆动点：高点序列与低点序列分开
    hh_pts = [v for v, is_h in _swings(recent_h) if is_h]
    ll_pts = [v for v, is_l in _swings(recent_l) if is_l]
    if len(hh_pts) >= 2 and len(ll_pts) >= 2:
        h1, h2 = hh_pts[-2], hh_pts[-1]   # 相邻两个摆动高点
        l1, l2 = ll_pts[-2], ll_pts[-1]   # 相邻两个摆动低点
        hh = h2 > h1
        ll_ = l2 < l1
        hl = l2 > l1
        lh = h2 < h1
        if hh and hl:
            return "up"
        if lh and ll_:
            return "down"
        # 扩张（HH+LL）→ 看收盘站上/跌破哪边
        if hh and ll_:
            last_c = closes[-1]
            if last_c > h2:
                return "up"
            if last_c < l2:
                return "down"
            return "neutral"
        # 收敛（LH+HL）→ 结构未决
        return "neutral"
    if len(hh_pts) >= 2:
        # 只有高点结构可判（如单边下跌无摆动低点）
        return "down" if hh_pts[-1] < hh_pts[-2] else "up"
    if len(ll_pts) >= 2:
        return "up" if ll_pts[-1] > ll_pts[-2] else "down"
    # ★ 2026-08-17 兜底：摆动点不足 = 单调走势（无回撤的真趋势，如线性上涨）。
    #   此时没有结构反转信号，回退收盘价方向（原逻辑），避免把强趋势误判为 range。
    #   注意：这里只处理"单调方向"，供给区 LH 等结构场景已在上方分支捕获。
    _ups = sum(1 for i in range(1, len(closes[-10:])) if closes[-i] > closes[-i - 1])
    _downs = sum(1 for i in range(1, len(closes[-10:])) if closes[-i] < closes[-i - 1])
    if _ups >= _downs + 2:
        return "up"
    if _downs >= _ups + 2:
        return "down"
    return "neutral"


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < 2:
        return 0.0
    trs = []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    # ★ 2026-08-15 审计P2修复：与 market_analyzer._calc_single_atr 统一用 **Wilder 递推**。
    #   原简单平均 `sum(trs[-period:])/period` 与 smc_features 同病 → 同一 ATR(14) 三套值，
    #   regime 的 extension_z 与结构锚 min_gap 各自为政，融合语义漂移。
    prev = sum(trs[:period]) / period
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
    return prev


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
    return 100.0 - (100.0 / (1.0 + ag / al))


def _prep_bars(tfs_raw: dict, tf: str, min_bars: int = 25) -> tuple:
    """提取指定周期的 OHLCV 序列。返回 (closes, highs, lows) 或 (None,None,None)。"""
    d = (tfs_raw or {}).get(tf, {}) or {}
    bars = d.get("bars") or []
    # ★ 2026-08-17 防御：部分上游把 "bars" 放成 int（K 线数量）而非列表——
    #   生产 market_analyzer 传原始 K 线（列表），但摘要结构（meta_agent 侧）bars 是 int。
    #   int 会炸 len()。此时若 closes 列表可用则直接用它，否则返回 None。
    if not isinstance(bars, (list, tuple)):
        closes = d.get("closes") or []
        if isinstance(closes, (list, tuple)) and len(closes) >= min_bars:
            highs = d.get("highs") or closes
            lows = d.get("lows") or closes
            return list(closes), list(highs), list(lows)
        return None, None, None
    if len(bars) < min_bars:
        return None, None, None
    # ★ 2026-08-15 审计P2修复：b.get() 防御取值——原 `b["close"]` 缺 key 抛 KeyError，
    #   被 market_analyzer 外层 try 吞掉后 SMC/Regime 整段静默降级 neutral（功能隐性丢失）。
    closes = [b.get("close") for b in bars if isinstance(b, dict)]
    highs = [b.get("high") for b in bars if isinstance(b, dict)]
    lows = [b.get("low") for b in bars if isinstance(b, dict)]
    closes = [x for x in closes if x is not None]
    highs = [x for x in highs if x is not None]
    lows = [x for x in lows if x is not None]
    if len(closes) < min_bars:
        return None, None, None
    return closes, highs, lows


def detect_regime(tfs_raw: dict, current_price: float = 0.0) -> dict:
    """
    三周期架构（行业最佳实践·海外≥3源交叉验证）：

    4H = 方向偏置 (direction_bias)
      定大局方向：up 则偏向 BUY，down 则偏向 SELL，neutral 无偏向。
      供 meta_agent 据此调节置信权重（趋势市顺势+权，震荡市均值回归+权），
      **不硬拦**。

    15m = 结构确认 (structure_dir)
      验证 4H 偏置是否被中周期确认。
      4H+15m 同向 = 强趋势；4H偏但15m反向 = 潜在反转→区间。

    5m = 入场时机 (entry_dir)
      短周期动量方向，决定实际入场节奏。
      5m+15m 同向 = 入场确认充分；反向 = 等待更好的入场点。
    """
    # ── 4H：方向偏置（Foundation） ──
    # ★ 2026-08-06 紧急修复：4H 偏置不能只看收盘价计数（滞后），
    #   必须以 H1 价格相对于 MA20 的位置（extension_z）做硬校验。
    #   当价格已跌破均值（z<0）仍判 up，会在下跌中持续给 BUY 加成→逆势巨亏。
    #   当价格涨破均值（z>0）仍判 down，同理给 SELL 加成。
    #   这里先用 H1 数据算 extension_z，若 z 与初步偏置反向，则降权为 neutral。
    c4h, h4h, l4h = _prep_bars(tfs_raw, "H4", 15)
    c_h1, h_h1, l_h1 = _prep_bars(tfs_raw, "H1", 25)
    _ext_z_for_bias = None
    if c_h1 and len(c_h1) >= 20:
        _ma20_h1 = sum(c_h1[-20:]) / 20
        _atr_h1 = _atr(h_h1, l_h1, c_h1, 14)
        if _atr_h1 > 0 and current_price > 0:
            _ext_z_for_bias = (current_price - _ma20_h1) / (2.0 * _atr_h1)

    if c4h and h4h and l4h:
        # ★ 4H 偏置只看最近 5 根（约 20 小时），对日内转折更灵敏；
        #   旧 10 根窗口记忆前期涨势，价格下跌后仍误判为 up。
        # ★ 2026-08-17 结构修复：传入 high/low 序列，用真实摆动点（HH/HL/LH/LL）判方向。
        _raw_bias = (_trend_strength_dir(c4h[-5:], h4h[-5:], l4h[-5:])
                     if len(c4h) >= 5 else _trend_strength_dir(c4h, h4h, l4h))

        # 硬校验 1：价格相对 H1 MA20 的位置。
        # 阈值从 ±0.3 收紧到 ±0.1：只要价格轻微反向突破均值，偏置即失效。
        if _raw_bias == "up" and _ext_z_for_bias is not None and _ext_z_for_bias < -0.1:
            bias_4h = "neutral"
            logger.info(f"[RegimeDetect] 4H初步up但价格跌破H1均值(z={_ext_z_for_bias:.2f})，偏置降权为neutral")
        elif _raw_bias == "down" and _ext_z_for_bias is not None and _ext_z_for_bias > 0.1:
            bias_4h = "neutral"
            logger.info(f"[RegimeDetect] 4H初步down但价格涨破H1均值(z={_ext_z_for_bias:.2f})，偏置降权为neutral")
        else:
            # 硬校验 2：用 H4 高低点结构做二次校验。
            # 若价格已跌破最近 5 根 H4 低点，则不应再判 up（至少 neutral）。
            _atr4h = _atr(h4h, l4h, c4h, 5)
            _cur = c4h[-1]
            _recent_highs = h4h[-5:] if len(h4h) >= 5 else h4h
            _recent_lows = l4h[-5:] if len(l4h) >= 5 else l4h
            _hh = max(_recent_highs) if _recent_highs else _cur
            _ll = min(_recent_lows) if _recent_lows else _cur
            if _raw_bias == "up" and _cur < _ll - 0.3 * _atr4h:
                bias_4h = "down"
            elif _raw_bias == "down" and _cur > _hh + 0.3 * _atr4h:
                bias_4h = "up"
            else:
                bias_4h = _raw_bias
    else:
        bias_4h = "neutral"

    # ── 15m：结构确认（Bridge） ──
    c15, h15, l15 = _prep_bars(tfs_raw, "M15", 25)
    dir_15 = _trend_strength_dir(c15, h15, l15) if c15 else "neutral"
    er_15 = _efficiency_ratio(c15, 20) if c15 else 0.0

    # ── 5m：入场时机（Entry） ──
    c5, h5, l5 = _prep_bars(tfs_raw, "M5", 15)
    # 5m 取更短窗口（近12根）判短期结构，更灵敏（2026-08-17 同步传 high/low）
    dir_5 = (_trend_strength_dir(c5[-12:], h5[-12:], l5[-12:])
             if c5 and h5 and l5 and len(c5) >= 12 else "neutral")

    # ── 备选 H1（用于计算延伸度/ATR/RSI，保持与旧版一致） ──
    c_h1, h_h1, l_h1 = _prep_bars(tfs_raw, "H1", 25)
    if c_h1 is None:
        # 退路：用 M15 代替
        c_h1, h_h1, l_h1 = c15, h15, l15

    if c_h1 is None:
        return {
            "regime": "unknown", "confidence": 0.0, "label_zh": "数据不足",
            "direction_bias": bias_4h,
            "structure_dir_15m": dir_15, "entry_dir_5m": dir_5,
            "extension_z": 0.0, "at_stale_top": False, "at_stale_bottom": False,
            "mean_reversion_risk": 0.0, "rsi_h1": 50.0, "efficiency_ratio": 0.0,
            "advice_zh": "行情数据不足，谨慎",
        }

    # ── H1 指标（用于延伸度/波动率） ──
    atr_h1 = _atr(h_h1, l_h1, c_h1, 14)
    rsi_h1 = _rsi(c_h1, 14)
    ma20 = (sum(c_h1[-20:]) / 20) if len(c_h1) >= 20 else c_h1[-1]
    er_h1 = _efficiency_ratio(c_h1, 20)

    # ── 综合体制判定（4H + 15m 共识） ──
    # 4H 与 15m 同向 = 强趋势；同向 neutral = 明确震荡
    # 4H 偏但 15m 反向 = 潜在反转 → 区间（等确认），但 M15 结构/动能强劲时
    #   按「短周期转向优先于长周期滞后」判为趋势（与 DeepSeek 提示词一致）。
    def _m15_strong():
        """M15 趋势是否足够强：ER 够高 或 连续同向结构确认。"""
        if dir_15 == "neutral":
            return False
        _er15 = er_15 if er_15 is not None else 0.0
        if _er15 >= 0.25:
            return True
        # 结构确认：最近 4 根 K 线低点逐低（跌）或高点逐高（涨）
        if c15 and len(c15) >= 5 and l15 and h15:
            if dir_15 == "down":
                return all(l15[-i] < l15[-i - 1] for i in range(1, min(4, len(l15))))
            if dir_15 == "up":
                return all(h15[-i] > h15[-i - 1] for i in range(1, min(4, len(h15))))
        return False

    if bias_4h == "up" and dir_15 == "up":
        regime = "trend_up"
    elif bias_4h == "down" and dir_15 == "down":
        regime = "trend_down"
    elif bias_4h == "neutral" and dir_15 == "neutral":
        # 波动率辅助：高波动 = volatile，低波动 = range
        vol_ratio = (atr_h1 / current_price) if current_price > 0 and atr_h1 > 0 else 0.0
        regime = "volatile" if vol_ratio > 0.012 else "range"
    elif bias_4h == "up" and dir_15 == "down":
        # 4H 涨但 15m 跌：若 M15 结构/动能强劲，按短周期转向优先判下跌趋势
        regime = "trend_down" if _m15_strong() else "range"
    elif bias_4h == "down" and dir_15 == "up":
        regime = "trend_up" if _m15_strong() else "range"
    elif bias_4h in ("up", "down") and dir_15 == "neutral":
        # 4H 偏向、15m 无方向 → 偏弱趋势，仍按 4H 判
        regime = "trend_up" if bias_4h == "up" else "trend_down"
    elif bias_4h == "neutral" and dir_15 in ("up", "down"):
        # 4H 无方向、15m 有方向 → 中周期趋势，按 15m 判
        # ★ 2026-08-18 修复：降低 ER 门槛(0.45→0.25)并加结构确认，
        #   避免把 M15 清晰趋势误判为 range（尤其是 4H 仍残留旧趋势时）。
        regime = ("trend_up" if dir_15 == "up" else "trend_down") if _m15_strong() else "range"
    else:
        regime = "range"

    # ── 价格延伸度 Z（H1）：(price - MA20) / (2*ATR) ──
    if atr_h1 > 0 and current_price > 0:
        extension_z = (current_price - ma20) / (2.0 * atr_h1)
    else:
        extension_z = 0.0

    # ── 趋势末端（接飞刀）判定（H1 基准，保留供哨兵+AI参考） ──
    at_stale_top = (regime == "trend_up" and extension_z > 2.0 and rsi_h1 > 72)
    at_stale_bottom = (regime == "trend_down" and extension_z < -2.0 and rsi_h1 < 28)
    at_stale_top = at_stale_top and extension_z > 2.5
    at_stale_bottom = at_stale_bottom and extension_z < -2.5

    # ── 均值回归风险 ──
    if regime == "trend_up":
        mrr = min(1.0, max(0.0, extension_z / 3.0))
    elif regime == "trend_down":
        mrr = min(1.0, max(0.0, -extension_z / 3.0))
    else:
        mrr = 0.0

    # ── 置信度：方向一致(4H+15m同向) + ER高 → 高 ──
    _tf_agree = 1.0 if ((bias_4h == dir_15 and bias_4h != "neutral") or (bias_4h == "neutral" and dir_15 == "neutral")) else 0.6
    confidence = 0.4 + 0.3 * _tf_agree + 0.15 * min(er_h1, 1.0) + 0.05 * min(er_15, 1.0)
    confidence = min(0.98, confidence)

    label_map = {
        "trend_up": "强势上涨趋势",
        "trend_down": "强势下跌趋势",
        "range": "区间震荡",
        "volatile": "高波动无序",
        "unknown": "未知",
    }
    advice = "正常跟随趋势"
    if at_stale_top:
        advice = "⚠️ 趋势末端/高位延伸区：禁止追BUY、警惕反转，优先等待回调至机构需求区"
    elif at_stale_bottom:
        advice = "⚠️ 趋势末端/低位超卖区：禁止追SELL、警惕逼空反转"
    elif regime == "range":
        advice = "区间震荡：避免追突破，等区间边界机构反应"
    elif regime == "volatile":
        advice = "高波动：缩仓，等结构确认"

    return {
        "regime": regime,
        "confidence": round(confidence, 3),
        "label_zh": label_map.get(regime, regime),
        # ★ 新增：三周期架构输出
        "direction_bias": bias_4h,
        "structure_dir_15m": dir_15,
        "entry_dir_5m": dir_5,
        # 保留：趋势末端接飞刀
        "extension_z": round(extension_z, 3),
        "at_stale_top": bool(at_stale_top),
        "at_stale_bottom": bool(at_stale_bottom),
        "mean_reversion_risk": round(mrr, 3),
        "rsi_h1": round(rsi_h1, 1),
        "efficiency_ratio": round(er_h1, 3),
        "advice_zh": advice,
    }


def detect_structure_break(tfs_raw: dict) -> dict:
    """★ 2026-08-17 新增：SMC/ICT 结构突破事件检测（BOS / CHoCH）。

    调研依据（≥3 独立出处交叉验证，2026-08-17 海外调研）：
      - TradingView BOS/CHOCH Demand&Supply（开源 Pine）：Bullish BOS = 收盘突破最近
        swing high 且该高点高于前一个（HH 延续=趋势确认）；CHoCH = 突破的是更低的高点
        （LH，逆势首破=反转预警）。只认收盘价，wick 影线穿透不算。
      - backtrex.com（BOS in SMC/ICT）：BOS=延续、CHoCH=反转；区间内 BOS 不可靠；
        多周期一致性是硬规则（H4/D1 定义 bias → H1/M15 确认 → M5 时机）。
      - liquidityhunters.cl / forexmt4indicators（BOS vs CHoCH 专文）：CHoCH 是警报、
        BOS 是确认；HH/HL=up、LH/LL=down。
      - coinxsight / kaigai-fx（突破确认）：收盘突破 + ADX>20 + 多周期对齐；
        "不追突破蜡烛、等回踩"。

    设计：纯信息加法（提准非拦截）——只产出客观结构事件供 AI 决策参考，不拦截任何单。
    返回 dict：
      m15: {bos/choch/broke_high/broke_low/last_swing_high/last_swing_low/displacement}
      m5 : 同上
      htf_aligned: bool —— M15 BOS 方向与 4H/H1 偏置一致（SMC 多周期确认硬规则）
      advice_zh   : 一句话结论（注入决策链/前端）
    """
    def _break_on_bars(closes, highs, lows):
        """在单周期 K 线上检测结构突破。返回 dict 或 None。"""
        if not closes or len(closes) < 10:
            return None
        recent_h = (highs or closes)[-10:]
        recent_l = (lows or closes)[-10:]

        def _swings(seq):
            pts = []
            for i in range(1, len(seq) - 1):
                if seq[i] > seq[i - 1] and seq[i] > seq[i + 1]:
                    pts.append((seq[i], True))
                elif seq[i] < seq[i - 1] and seq[i] < seq[i + 1]:
                    pts.append((seq[i], False))
            return pts

        hp = [v for v, is_h in _swings(recent_h) if is_h]
        lp = [v for v, is_l in _swings(recent_l) if is_l]
        # ★ 2026-08-17 强趋势回退：单调走势提取不到摆动点（每根都比前一根极端）时，
        #   用最近 N 根 high 最大值 / low 最小值作参考摆动点——SMC 实践中"最后明显高点/
        #   低点"即可作为结构参考，否则强趋势中的首次反向突破会漏检 CHoCH。
        #   注意必须排除当前 K 线（其 high/low 是突破瞬间自身的极值，close 永远无法突破）。
        if not hp:
            _pre_h = recent_h[:-1]
            if _pre_h:
                hp = [max(float(x) for x in _pre_h)]
        if not lp:
            _pre_l = recent_l[:-1]
            if _pre_l:
                lp = [min(float(x) for x in _pre_l)]
        out = {"bos": None, "choch": None, "broke_high": False, "broke_low": False,
               "last_swing_high": float(hp[-1]) if hp else None,
               "last_swing_low": float(lp[-1]) if lp else None,
               "displacement": 0.0}
        last_c = float(closes[-1])
        # 前序结构（不含当前突破 K 线）：下跌(LH/LL)→CHoCH 反转；上涨/区间→BOS 延续/启动
        _prior = "neutral"
        if len(closes) >= 11:
            _prior = _trend_strength_dir(closes[:-1], (highs or closes)[:-1],
                                         (lows or closes)[:-1])
        # 突破力度（ATR 归一化 displacement——coinxsight/kaigai 的"突破必须有力"）
        _atr_est = 0.0
        try:
            _ws = [float(highs[i] - lows[i]) for i in range(max(0, len(highs) - 14), len(highs))
                   if highs[i] is not None and lows[i] is not None]
            if _ws:
                _atr_est = sum(_ws) / len(_ws)
        except Exception:
            pass
        if hp and last_c > float(hp[-1]):
            out["broke_high"] = True
            _hh = len(hp) >= 2 and float(hp[-1]) > float(hp[-2])
            if _hh or _prior != "down":
                out["bos"] = "up"      # HH 延续 或 区间突破(上沿) = 趋势延续/启动
            else:
                out["choch"] = "up"    # 前序下跌中首破 = 反转预警
            if _atr_est > 0:
                out["displacement"] = round((last_c - float(hp[-1])) / _atr_est, 2)
        if lp and last_c < float(lp[-1]):
            out["broke_low"] = True
            _ll = len(lp) >= 2 and float(lp[-1]) < float(lp[-2])
            if _ll or _prior != "up":
                out["bos"] = "down"
            else:
                out["choch"] = "down"
            if _atr_est > 0:
                out["displacement"] = max(out["displacement"],
                                          round((float(lp[-1]) - last_c) / _atr_est, 2))
        return out

    c15, h15, l15 = _prep_bars(tfs_raw, "M15", 12)
    c5, h5, l5 = _prep_bars(tfs_raw, "M5", 12)
    m15 = _break_on_bars(c15, h15, l15) if c15 else None
    m5 = _break_on_bars(c5, h5, l5) if c5 else None

    # ── 多周期一致性（SMC 硬规则：HTF bias + LTF 确认） ──
    htf_aligned = False
    try:
        c4h, h4h, l4h = _prep_bars(tfs_raw, "H4", 15)
        c_h1, h_h1, l_h1 = _prep_bars(tfs_raw, "H1", 20)
        _b4 = _trend_strength_dir(c4h, h4h, l4h) if c4h else "neutral"
        _b1 = _trend_strength_dir(c_h1, h_h1, l_h1) if c_h1 else "neutral"
        _m15_bos = (m15 or {}).get("bos")
        if _m15_bos in ("up", "down"):
            # 4H 同向 = 强一致；4H neutral 但 H1 同向 = 弱一致（4H 无 bias 不阻挡）
            htf_aligned = (_m15_bos == _b4) or (_b4 == "neutral" and _m15_bos == _b1)
    except Exception:
        htf_aligned = False

    # ── 一句话结论（注入决策链供 AI 判读） ──
    advice_zh = None
    _m = m15 or {}
    if _m.get("bos") in ("up", "down"):
        _dir = "上升" if _m["bos"] == "up" else "下降"
        _conf = "高周期一致·趋势启动信号" if htf_aligned else "高周期未确认·谨慎"
        advice_zh = f"M15 出现{_dir}趋势延续突破(bullish BOS→{_m['bos']})，{_conf}"
        if htf_aligned and _m.get("displacement", 0) >= 0.8:
            advice_zh += "，突破有力(displacement≥0.8×ATR)"
    elif _m.get("choch") in ("up", "down"):
        _dir = "上升" if _m["choch"] == "up" else "下降"
        advice_zh = f"M15 出现{_dir}逆势首破(CHoCH，{_m['choch']})→ 结构可能反转，预警"

    return {
        "m15": m15, "m5": m5,
        "htf_aligned": bool(htf_aligned),
        "advice_zh": advice_zh,
    }
