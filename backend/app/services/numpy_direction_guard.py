"""
纯 NumPy 实现的"方向终审器"（Numpy Direction Guard）。

设计目标：
1. 不依赖 PyTorch / Transformers，在当前沙箱可立即运行；
2. 把用户实证有效的规则（价格延伸度 + RSI 极端 + 布林带外轨 + 趋势强度）
   变成确定性方向冲突检测；
3. 只做"冲突 → HOLD"，不替代云端双脑的方向判断，贴合"提准非拦截"铁律。

输入：
- closes: 最近 N 根收盘价序列（list/np.ndarray，至少 50 根为佳）
- current_price: 当前真实价格
- proposed_direction: 云端决策方向，"BUY" / "SELL" / "HOLD"

输出：
- DirectionGuardResult（dataclass），包含：
  - direction_score: [-1, 1]，综合看多/看空强度
  - conflict_level: none / minor / major
  - reason: 人类可读理由
  - features: 原始特征字典（方便审计、前端展示）

规则来源（均来自项目记忆 2026-07-21 用户确认的方法论）：
- 黄金价格距基准 MA > 2.5 × ATR = 统计罕见延伸、回归概率骤升；
- H4 RSI 极端阈值 72/28（非 70/30，黄金波动大）；
- 布林带收盘破外轨 H1 后 73% 概率 3-5 根回归；
- CTA 用价格距均值 Z > 1.5 判趋势拥挤/衰竭。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union

import numpy as np


@dataclass
class DirectionGuardResult:
    """方向终审结果。"""

    direction_score: float = 0.0  # -1(极空) ~ +1(极多)
    conflict_level: str = "none"  # none / minor / major
    reason: str = ""
    features: Dict[str, float] = field(default_factory=dict)
    suggested_direction: str = "HOLD"  # 当 conflict_level=major 时强制 HOLD；minor 保持原方向但告警
    model: str = "numpy"  # 产生该结果的模型标识，用于 TSArena 多模型聚合


class NumpyDirectionGuard:
    """基于价格序列统计的轻量方向终审器。"""

    # 默认窗口：H1 用 50 根 ≈ 2 个交易日；M5 用 200 根 ≈ 16 小时。
    # 为适应不同周期，默认按输入长度自适应，但可外部覆盖。
    DEFAULT_MA_WINDOW = 50
    DEFAULT_ATR_WINDOW = 14
    DEFAULT_RSI_WINDOW = 14

    # 阈值：均来自用户实证规则，可调。
    Z_MAJOR = 2.5       # 价格偏离 MA 超过 2.5σ → 罕见延伸
    Z_MINOR = 1.5       # 趋势拥挤/衰竭警戒
    RSI_OVERBOUGHT = 72
    RSI_OVERSOLD = 28
    BB_PENETRATE_MAJOR = 0.95  # 收盘在布林带外轨之外的占比阈值

    def __init__(
        self,
        ma_window: int = DEFAULT_MA_WINDOW,
        atr_window: int = DEFAULT_ATR_WINDOW,
        rsi_window: int = DEFAULT_RSI_WINDOW,
    ):
        self.ma_window = ma_window
        self.atr_window = atr_window
        self.rsi_window = rsi_window

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def review(
        self,
        closes: Sequence[float],
        current_price: float,
        proposed_direction: str,
    ) -> DirectionGuardResult:
        """
        对云端提出的方向做终审。

        返回值说明：
        - conflict_level = "none"：无统计冲突，放行；
        - conflict_level = "minor"：有轻微冲突（如延伸但未极端），放行但前端告警；
        - conflict_level = "major"：强统计冲突，建议 HOLD（由调用方决定是否降级）。
        """
        if proposed_direction in (None, "HOLD", ""):
            return DirectionGuardResult(
                direction_score=0.0,
                conflict_level="none",
                reason="原本 HOLD，无需终审",
                suggested_direction="HOLD",
            )

        closes_arr = np.asarray(closes, dtype=np.float64)
        if len(closes_arr) < max(self.ma_window, self.rsi_window, self.atr_window) + 5:
            return DirectionGuardResult(
                direction_score=0.0,
                conflict_level="none",
                reason="历史数据不足，跳过方向终审",
                suggested_direction=proposed_direction,
            )

        feats = self._compute_features(closes_arr, current_price)
        score, conflict, reason = self._score_and_conflict(
            proposed_direction, feats
        )

        return DirectionGuardResult(
            direction_score=round(score, 4),
            conflict_level=conflict,
            reason=reason,
            features={k: round(v, 4) for k, v in feats.items()},
            suggested_direction=(
                "HOLD" if conflict == "major" else proposed_direction
            ),
        )

    # ------------------------------------------------------------------
    # 特征计算
    # ------------------------------------------------------------------
    def _compute_features(
        self, closes: np.ndarray, current_price: float
    ) -> Dict[str, float]:
        """计算方向终审所需的统计特征。"""
        feats: Dict[str, float] = {}

        ma = self._sma(closes, self.ma_window)
        std = self._rolling_std(closes, self.ma_window)
        # 只有 close 时，用滚动标准差作为波动单位（比 close-to-close ATR 更稳健，
        # 避免强趋势段 close-to-close 波动被低估导致误判）。
        volatility_unit = std[-1] if std[-1] > 0 else 1e-6

        feats["ma50"] = float(ma[-1])
        feats["volatility_unit"] = float(volatility_unit)
        feats["price_to_ma_z"] = float((current_price - ma[-1]) / volatility_unit)
        # atr14 仅作参考；后续规则统一用 price_to_ma_z（基于 std）。
        atr = self._atr(closes, self.atr_window)
        feats["atr14"] = float(atr[-1]) if not np.isnan(atr[-1]) else 0.0

        rsi = self._rsi(closes, self.rsi_window)
        feats["rsi14"] = float(rsi[-1])

        bb_upper, bb_lower, bb_width = self._bollinger_bands(closes, 20, 2.0)
        feats["bb_upper"] = float(bb_upper[-1])
        feats["bb_lower"] = float(bb_lower[-1])
        feats["bb_width"] = float(bb_width[-1])
        feats["bb_position"] = self._bb_position(current_price, bb_upper[-1], bb_lower[-1])

        # 趋势强度：线性回归斜率（已做价格归一化）
        trend_score = self._trend_strength(closes[-self.ma_window:])
        feats["trend_slope"] = float(trend_score)

        # 近期延伸度均值（看是否持续超买/超卖）
        feats["z_avg_5"] = float(np.mean(
            (closes[-5:] - ma[-5:]) / np.where(std[-5:] > 0, std[-5:], 1)
        ))

        return feats

    # ------------------------------------------------------------------
    # 冲突判定
    # ------------------------------------------------------------------
    def _score_and_conflict(
        self, proposed: str, feats: Dict[str, float]
    ) -> tuple[float, str, str]:
        """
        根据特征计算方向分与冲突等级。

        核心逻辑：
        - 方向分综合了 trend_slope / price-to-ma / bb_position；
        - 当云端方向与统计信号强冲突，且价格处于极端延伸位 → major；
        - 单一信号冲突或轻微延伸 → minor；
        - 正常贴合 → none。
        """
        z = feats["price_to_ma_z"]
        rsi = feats["rsi14"]
        trend = feats["trend_slope"]
        bb_pos = feats["bb_position"]
        # 短期平均延伸度，用于识别末端冲刺
        z_avg_5 = feats.get("z_avg_5", 0.0)

        # 方向分：把多个因素压到 [-1, 1]
        # z 和 trend 同向时互相加强；RSI 在两端提供动量确认。
        score = np.tanh(
            0.5 * z + 0.4 * trend + 0.2 * np.sin(np.pi * (rsi - 50) / 100)
        )
        score = float(score)

        reasons: List[str] = []
        conflict = "none"

        proposed_buy = proposed.upper() == "BUY"
        proposed_sell = proposed.upper() == "SELL"

        # 1. 极端延伸 + RSI 极端 → 末端接飞刀
        #   要求：当前偏离 AND 近 5 根平均也偏离（确认是末端冲刺，不是单根毛刺）
        if z > self.Z_MAJOR and z_avg_5 > self.Z_MINOR and rsi > self.RSI_OVERBOUGHT:
            if proposed_buy:
                conflict = "major"
                reasons.append(
                    f"价格高于MA {z:.2f}σ且RSI {rsi:.1f} > {self.RSI_OVERBOUGHT}，"
                    f"处于多头极端延伸区，BUY 疑似接飞刀"
                )
            else:
                conflict = max_conflict(conflict, "minor")
                reasons.append(
                    f"多头极端延伸但提议 SELL，统计冲突轻微"
                )

        if z < -self.Z_MAJOR and z_avg_5 < -self.Z_MINOR and rsi < self.RSI_OVERSOLD:
            if proposed_sell:
                conflict = "major"
                reasons.append(
                    f"价格低于MA {abs(z):.2f}σ且RSI {rsi:.1f} < {self.RSI_OVERSOLD}，"
                    f"处于空头极端延伸区，SELL 疑似接飞刀"
                )
            else:
                conflict = max_conflict(conflict, "minor")
                reasons.append(
                    f"空头极端延伸但提议 BUY，统计冲突轻微"
                )

        # 2. 布林带外轨突破（连续在外侧）
        if bb_pos > 1.0:
            if proposed_buy:
                conflict = "major"
                reasons.append("价格突破布林带上轨，短期回归概率高，BUY 冲突")
            else:
                conflict = max_conflict(conflict, "minor")
        elif bb_pos < -1.0:
            if proposed_sell:
                conflict = "major"
                reasons.append("价格跌破布林带下轨，短期回归概率高，SELL 冲突")
            else:
                conflict = max_conflict(conflict, "minor")

        # 3. 趋势强度与方向明显相反
        #    关键修正（2026-08-15 第二批#6）：原始裸「trend<-0.3 即 major」会把
        #    「健康回调中的买入 / 上升趋势中回踩下轨买入」也判为 major → 触发软降权(conf×0.6)
        #    砍掉有效利润。调研(pro-scalper H4 RSI 72/28、goldpriceaction OU 均值回归、
        #    MEXC Z-Score 三源交叉验证)结论：趋势反向 ≠ 拦截；只有当价格同时处于
        #    「末端延伸」(|Z| > Z_MINOR=1.5) 时才属接飞刀(major)；否则属健康回调，
        #    仅 minor（仅记录·不干预=放行），契合铁律"提准非拦截"。
        if proposed_buy and trend < -0.3:
            if abs(z) > self.Z_MINOR:
                conflict = max_conflict(conflict, "major")
                reasons.append(
                    f"短期趋势斜率为负({trend:.2f})且价格偏离MA达{abs(z):.2f}σ(末端延伸)，"
                    f"BUY 疑似接飞刀"
                )
            else:
                conflict = max_conflict(conflict, "minor")
                reasons.append(
                    f"短期趋势斜率为负({trend:.2f})但价格未延伸(|Z|={abs(z):.2f}≤{self.Z_MINOR})，"
                    f"属健康回调，仅记录不干预"
                )
        elif proposed_sell and trend > 0.3:
            if abs(z) > self.Z_MINOR:
                conflict = max_conflict(conflict, "major")
                reasons.append(
                    f"短期趋势斜率为正({trend:.2f})且价格偏离MA达{abs(z):.2f}σ(末端延伸)，"
                    f"SELL 疑似接飞刀"
                )
            else:
                conflict = max_conflict(conflict, "minor")
                reasons.append(
                    f"短期趋势斜率为正({trend:.2f})但价格未延伸(|Z|={abs(z):.2f}≤{self.Z_MINOR})，"
                    f"属健康回调，仅记录不干预"
                )

        # 4. 轻微趋势拥挤（z 1.5~2.5）
        if abs(z) > self.Z_MINOR and conflict == "none":
            conflict = "minor"
            reasons.append(f"价格偏离MA达 {z:.2f}σ，趋势拥挤，方向风险上升")

        if not reasons:
            return score, "none", "统计方向终审通过"

        return score, conflict, "; ".join(reasons)

    # ------------------------------------------------------------------
    # 基础指标（纯 NumPy）
    # ------------------------------------------------------------------
    @staticmethod
    def _sma(x: np.ndarray, window: int) -> np.ndarray:
        """简单移动平均（cumsum 差分实现）。"""
        if len(x) < window:
            return np.full_like(x, np.nan)
        cumsum = np.cumsum(np.concatenate(([0.0], x)))
        return (cumsum[window:] - cumsum[:-window]) / window

    @staticmethod
    def _rolling_std(x: np.ndarray, window: int) -> np.ndarray:
        """滚动标准差。"""
        if len(x) < window:
            return np.full_like(x, np.nan)
        # 为了和 ma 对齐长度，前面补 nan
        ma = np.convolve(x, np.ones(window) / window, mode="valid")
        # 计算每个窗口的标准差
        std = np.array(
            [np.std(x[i : i + window], ddof=1) for i in range(len(x) - window + 1)]
        )
        return np.concatenate((np.full(window - 1, np.nan), std))

    @staticmethod
    def _atr(high_low_close: np.ndarray, window: int) -> np.ndarray:
        """
        简化 ATR：只有 close 序列时，用 |close - prev_close| 近似 TR。
        注意：首根没有前一根，不 prepend（避免把首根 TR 填为 0 导致 ATR 被严重低估）。
        """
        if len(high_low_close) < 2:
            return np.full_like(high_low_close, np.nan)
        tr = np.abs(np.diff(high_low_close))
        if len(tr) < window:
            return np.concatenate((np.full(len(high_low_close) - len(tr), np.nan), tr))
        atr = np.convolve(tr, np.ones(window) / window, mode="valid")
        # 前面补 nan，让长度和输入一致；因为 tr 比输入短 1，所以补 window 个 nan。
        return np.concatenate((np.full(window, np.nan), atr))

    @staticmethod
    def _rsi(x: np.ndarray, window: int) -> np.ndarray:
        """相对强弱指数（RSI），Wilder 平滑。"""
        delta = np.diff(x)
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)

        # 用卷积做初始简单平均，长度与 tr 对齐
        avg_gain = np.convolve(gain, np.ones(window) / window, mode="valid")
        avg_loss = np.convolve(loss, np.ones(window) / window, mode="valid")

        eps = 1e-9
        rs = avg_gain / (avg_loss + eps)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        # 前面补 window 个 nan（因为 diff 少 1 根，卷积 valid 再少 window-1 根）
        return np.concatenate((np.full(window, np.nan), rsi))

    @staticmethod
    def _bollinger_bands(
        x: np.ndarray, window: int = 20, num_std: float = 2.0
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """布林带。"""
        ma = np.convolve(x, np.ones(window) / window, mode="valid")
        std = np.array(
            [np.std(x[i : i + window], ddof=1) for i in range(len(x) - window + 1)]
        )
        upper = ma + num_std * std
        lower = ma - num_std * std
        width = (upper - lower) / ma

        pad = window - 1
        upper = np.concatenate((np.full(pad, np.nan), upper))
        lower = np.concatenate((np.full(pad, np.nan), lower))
        width = np.concatenate((np.full(pad, np.nan), width))
        return upper, lower, width

    @staticmethod
    def _bb_position(price: float, upper: float, lower: float) -> float:
        """
        价格在布林带中的位置：
        0 = 中轨；+1 = 上轨；-1 = 下轨；>1 = 突破上轨；<-1 = 跌破下轨。
        """
        if np.isnan(upper) or np.isnan(lower) or upper == lower:
            return 0.0
        return (2 * price - upper - lower) / (upper - lower)

    @staticmethod
    def _trend_strength(x: np.ndarray) -> float:
        """
        线性回归斜率（归一化到 [-1, 1] 量级）。
        用 x 与 time index 的相关系数 * 标准化斜率。
        """
        if len(x) < 5 or np.std(x) < 1e-9:
            return 0.0
        t = np.arange(len(x), dtype=np.float64)
        # 标准化后求斜率
        x_norm = (x - np.mean(x)) / np.std(x)
        t_norm = (t - np.mean(t)) / np.std(t)
        slope = np.corrcoef(t_norm, x_norm)[0, 1]
        return float(np.clip(slope, -1.0, 1.0))


def max_conflict(a: str, b: str) -> str:
    """取两个冲突等级中更高的一个。"""
    order = {"none": 0, "minor": 1, "major": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b
