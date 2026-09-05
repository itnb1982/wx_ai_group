"""
万象Ai XAUUSD — 执行质量遥测（滑点追踪·⑥）

用途：记录每笔市价单的「成交价 vs 请求价」滑点，累计滚动统计，
      暴露给 AI 上下文，让模型知道当前经纪商执行质量（是否 B-book 剥削）。
      纯内部遥测，零外部依赖（数据来自 MT5 自身 order_send 返回值）。

设计：
  - XAUUSD 1 pip = 0.1，1 point = 0.01。滑点以 pip 为主、point 为辅。
  - 模块级单例 + 环形缓冲（最近 120 笔），全局共享（滑点是经纪商级，
    对所有账号一致，天然多账号优先）。
  - 只记录成功成交；异常静默忽略，绝不阻断交易。
"""


import time
from collections import deque
from typing import Optional, Dict, Any


class ExecutionTelemetry:
    def __init__(self, cap: int = 120):
        self.samples: deque = deque(maxlen=cap)
        self.last: Optional[dict] = None

    def record(self, side: str, requested: float, filled: float):
        """side: BUY/SELL；requested=下单时请求价；filled=MT5 实际成交价(result.price)。"""
        try:
            req = float(requested)
            fill = float(filled)
        except (TypeError, ValueError):
            return
        if req <= 0 or fill <= 0:
            return
        diff = abs(fill - req)
        pip = diff / 0.1          # XAUUSD 1 pip = 0.1
        pts = diff / 0.01         # 1 point = 0.01
        e = {
            "side": side,
            "req": round(req, 3),
            "fill": round(fill, 3),
            "pip": round(pip, 3),
            "pts": round(pts, 2),
            "ts": time.time(),
        }
        self.samples.append(e)
        self.last = e

    def summary(self) -> Dict[str, Any]:
        if not self.samples:
            return {"available": False}
        pips = [s["pip"] for s in self.samples]
        avg = sum(pips) / len(pips)
        return {
            "available": True,
            "count": len(pips),
            "last_pip": self.last["pip"],
            "last_side": self.last["side"],
            "avg_pip": round(avg, 3),
            "max_pip": round(max(pips), 3),
            "assessment": _assess(avg),
        }


def _assess(avg_pip: float) -> str:
    if avg_pip < 0.5:
        return "滑点极低(经纪商执行质量好)"
    if avg_pip < 1.5:
        return "滑点正常"
    if avg_pip < 3.0:
        return "滑点偏高(关注B-book风险)"
    return "滑点严重(疑似经纪商滑点剥削)"


_engine = ExecutionTelemetry()


def get_telemetry() -> ExecutionTelemetry:
    return _engine


def record_fill(side: str, requested: float, filled: float):
    """供 trade_executor 调用，记录一笔成交滑点。异常静默。"""
    try:
        _engine.record(side, requested, filled)
    except Exception:
        pass
