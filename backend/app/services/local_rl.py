"""
万象Ai XAUUSD — 本地进化引擎（真在线学习·区别于经验回注反模式）

为什么不是「经验回注」：ATLAS(ACL2026) 论文实证——把历史教训硬塞进 prompt 的
reflection 式做法无法系统提升，且制造方向偏置。正确做法是：
  ① 在线学习（持续从每笔真实盈亏更新特征权重）
  ② 动态提示/权重优化（Adaptive-OPRO）
  ③ 漂移检测（ADWIN）自动适应体制变化
本引擎实现 ①+③ 的轻量版：基于每笔成交持续更新「情境→期望盈亏」映射。
2026-08-11 升级：在「软提示文本」之外，新增 get_confidence_modifier() 把该映射
变成决策层的「硬置信修正器」（对齐 SOTA 折扣上下文老虎机），形成真闭环。

设计铁律：
  - 纯 Python，无重依赖；失败静默降级，绝不阻塞主交易链路
  - 全局共享（行情特征），多账号优先
  - 从空开始，随实盘累积自动变聪明（真进化，非静态文本）
"""
from collections import deque
import math
import logging
from typing import Optional

logger = logging.getLogger("wanxiang.evolution")

try:
    from app.config import settings as _settings
except Exception:  # 独立运行/循环导入保护
    _settings = None


# 情境标签（从 market_data 的 regime/smc 派生）
def _extract_tags(market_data: dict) -> list:
    regime = (market_data or {}).get("regime") or {}
    smc = (market_data or {}).get("smc_features") or {}
    tags = []
    r = regime.get("regime")
    if r:
        tags.append(f"regime:{r}")
    if regime.get("at_stale_top"):
        tags.append("stale_top")
    if regime.get("at_stale_bottom"):
        tags.append("stale_bottom")
    bias = smc.get("global_bias")
    if bias and bias != "neutral":
        tags.append(f"smc:{bias}")
    ext = float(regime.get("extension_z", 0) or 0)
    if ext > 2:
        tags.append("ext_high")
    elif ext < -2:
        tags.append("ext_low")
    return tags


class EvolutionEngine:
    def __init__(self, cap: int = 3000):
        # key: "BUY@tag" -> {pnl累计, 笔数, 盈利笔数}
        self.stats: dict = {}
        self.history: deque = deque(maxlen=cap)
        self._seen_keys: set = set()

    def _key(self, direction: str, tag: str) -> str:
        return f"{direction.upper()}@{tag}"

    def record(self, direction: str, pnl: float, tags: list):
        _gamma = 0.97
        if _settings is not None:
            try:
                _gamma = float(getattr(_settings, "EVOLUTION_GAMMA", 0.97) or 0.97)
            except Exception:
                _gamma = 0.97
        d = direction.upper()
        for t in tags:
            k = self._key(d, t)
            s = self.stats.setdefault(k, {"pnl": 0.0, "cnt": 0, "wins": 0, "pnlw": 0.0, "cntw": 0.0})
            s["pnl"] += pnl
            s["cnt"] += 1
            s["pnlw"] = s["pnlw"] * _gamma + pnl
            s["cntw"] = s["cntw"] * _gamma + 1.0
            if pnl > 0:
                s["wins"] += 1
        # 整体基准
        k0 = self._key(d, "ALL")
        s0 = self.stats.setdefault(k0, {"pnl": 0.0, "cnt": 0, "wins": 0, "pnlw": 0.0, "cntw": 0.0})
        s0["pnl"] += pnl
        s0["cnt"] += 1
        s0["pnlw"] = s0["pnlw"] * _gamma + pnl
        s0["cntw"] = s0["cntw"] * _gamma + 1.0
        if pnl > 0:
            s0["wins"] += 1

    def sync_from_buffer(self, trades: list):
        """从 ai_memory 成交缓冲同步（零接入点风险；缓冲无历史tag，用当前context近似）"""
        for e in trades:
            tk = e.get("ticket")
            kind = e.get("kind")
            key = (tk, kind)
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            direction = str(e.get("direction") or e.get("decision") or "").upper()
            if direction not in ("BUY", "SELL"):
                continue
            try:
                pnl = float(e.get("pnl") or 0)
            except Exception:
                pnl = 0.0
            self.history.append((direction, pnl))
            # 缓冲无历史context，仅更新整体基准（完美特征级需 trade_executor 接入点，路线已规划）
            _gamma = 0.97
            if _settings is not None:
                try:
                    _gamma = float(getattr(_settings, "EVOLUTION_GAMMA", 0.97) or 0.97)
                except Exception:
                    _gamma = 0.97
            k0 = self._key(direction, "ALL")
            s0 = self.stats.setdefault(k0, {"pnl": 0.0, "cnt": 0, "wins": 0, "pnlw": 0.0, "cntw": 0.0})
            s0["pnl"] += pnl
            s0["cnt"] += 1
            s0["pnlw"] = s0["pnlw"] * _gamma + pnl
            s0["cntw"] = s0["cntw"] * _gamma + 1.0
            if pnl > 0:
                s0["wins"] += 1
        # 控制 seen 集合大小
        if len(self._seen_keys) > 5000:
            self._seen_keys = set(list(self._seen_keys)[-2000:])

    def _expected(self, direction: str, tag: str) -> Optional[float]:
        s = self.stats.get(self._key(direction, tag))
        if not s or s["cnt"] < 3:
            return None
        return s["pnl"] / s["cnt"]

    def _winrate(self, direction: str, tag: str) -> Optional[float]:
        s = self.stats.get(self._key(direction, tag))
        if not s or s["cnt"] < 3:
            return None
        return s["wins"] / s["cnt"]

    def get_advice(self, market_data: dict) -> list:
        """返回数据驱动的进化洞察（软参考，非硬约束）"""
        tags = _extract_tags(market_data)
        if not tags:
            return []
        advice = []
        for d in ("BUY", "SELL"):
            # 优先看最具体 tag，回退 ALL
            exp = None
            wr = None
            for t in tags:
                e = self._expected(d, t)
                if e is not None:
                    exp = e
                    wr = self._winrate(d, t)
                    ctx = t
                    break
            if exp is None:
                e0 = self._expected(d, "ALL")
                if e0 is not None:
                    exp = e0
                    wr = self._winrate(d, "ALL")
                    ctx = "全局"
            if exp is not None:
                verdict = "正期望(可参与)" if exp > 0 else "负期望(规避)"
                advice.append(
                    f"{d}在[{ctx}]历史期望盈亏{exp:+.1f}$, 胜率{wr:.0%} → {verdict}"
                )
        return advice

    def _smoothed_expected(self, direction: str, tag: str, gamma: float = 0.97):
        """折扣期望（非平稳）+ 收缩（防小样本过拟合）。

        无数据或衰减样本数不足→None；否则返回向中性(0)收缩后的均值。
        收缩公式：exp = (cntw*raw) / (cntw + prior_n)，prior_n=5 控制收缩强度。
        """
        s = self.stats.get(self._key(direction, tag))
        if not s:
            return None
        cntw = s.get("cntw", 0.0)
        if cntw < 1e-6:
            return None
        _min_n = 5.0
        if _settings is not None:
            try:
                _min_n = float(getattr(_settings, "EVOLUTION_MIN_SAMPLE", 5.0) or 5.0)
            except Exception:
                _min_n = 5.0
        if cntw < _min_n:
            return None
        _prior_n = 5.0
        raw = s["pnlw"] / cntw
        return (cntw * raw) / (cntw + _prior_n)

    def get_confidence_modifier(self, direction: str, market_data: dict) -> float:
        """真闭环·硬置信修正器（SOTA 对齐：折扣上下文老虎机 + 收缩）。

        把「情境→期望盈亏」映射变成对 final_confidence 的乘子：
          context = tag 组合(regime/smc/ext)，arm = BUY/SELL，reward = pnl；
          平滑期望归一化后过 tanh 压缩到 [-1,1] → 乘子 = 1 + clip(signal, -max_pen, max_bon)。
        安全护栏：指数衰减(防模型陈旧) + 收缩(防小样本过拟合) + 乘子上下限
        (防"永不交易"/爆量)。未达最小样本→乘子≈1.0（tanh(≈0)=0）。
        返回 1.0 表示不干预。
        """
        try:
            d = direction.upper()
            if d not in ("BUY", "SELL"):
                return 1.0
            if _settings is not None and not bool(getattr(_settings, "EVOLUTION_MODIFIER_ENABLED", True)):
                return 1.0
            _gamma = float(getattr(_settings, "EVOLUTION_GAMMA", 0.97) or 0.97)
            _learn = float(getattr(_settings, "EVOLUTION_LEARN_RATE", 0.5) or 0.5)
            _scale = float(getattr(_settings, "EVOLUTION_SCALE", 30.0) or 30.0)
            _max_pen = float(getattr(_settings, "EVOLUTION_MAX_PENALTY", 0.5) or 0.5)
            _max_bon = float(getattr(_settings, "EVOLUTION_MAX_BONUS", 0.5) or 0.5)
            tags = _extract_tags(market_data)
            _exp = None
            _ctx = "全局"
            for t in tags:
                _e = self._smoothed_expected(d, t, _gamma)
                if _e is not None:
                    _exp = _e
                    _ctx = t
                    break
            if _exp is None:
                _exp = self._smoothed_expected(d, "ALL", _gamma)
                _ctx = "全局"
            if _exp is None:
                return 1.0
            _signal = math.tanh(_learn * _exp / max(_scale, 1e-6))
            _mult = 1.0 + max(-_max_pen, min(_max_bon, _signal))
            logger.info(
                f"[真进化·置信修正] {d}@{_ctx} 平滑期望{_exp:+.2f}$ → 乘子{_mult:.3f}"
            )
            return _mult
        except Exception as _e:
            logger.debug(f"[真进化·置信修正] 异常忽略: {_e}")
            return 1.0

    def drift_alert(self) -> Optional[str]:
        """简化漂移检测：近期整体胜率显著下降则提示再适应"""
        n = min(50, len(self.history))
        if n < 20:
            return None
        recent = list(self.history)[-n:]
        win = sum(1 for _, p in recent if p > 0)
        wr = win / n
        if wr < 0.35:
            return f"近期{n}笔胜率{wr:.0%}显著偏低，建议降低仓位/暂停顺势单"
        return None


_engine: Optional[EvolutionEngine] = None


def get_engine() -> EvolutionEngine:
    global _engine
    if _engine is None:
        _engine = EvolutionEngine()
    return _engine
