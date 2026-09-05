"""
XAU/USD 万象Ai — 纯 Python 离线回测器（M1.5）
=============================================
验收 M1「AI 出场 Agent」：同一组开仓序列下，对比
  · 策略R = 原 smart_exit 规则引擎（复现 PF≈0.382 基线）
  · 策略A = AI 出场 Agent（真实 LLM 决策）
输出 PF / 胜率 / 笔数 / 盈亏比 / 捕捉率(capture rate)。

设计红线（与全系统一致）：
- MT5 仅作「行情数据源」，只调用 copy_rates_range 只读拉历史 K 线，
  不编译任何 EA、不写一行 MQL5。
- AI 只输出意图(hold/partial_close/full_close/reverse_signal + new_sl)，
  本引擎负责模拟执行；AI 严禁移除 SL（AIExitAgent._validate 已拦截）。
- 任何 LLM 超时/异常 → AIExitAgent 自动回退 smart_exit 规则引擎（出场永不卡死）。

开仓信号说明：本回测器的开仓信号是「测试激励」（简化趋势突破），
仅用于产生可比的持仓序列；真实系统开仓仍是 AI（MetaAgent 辩论）。
两种出场策略跑在同一组开仓序列上，故 PF 差异纯粹来自「出场逻辑」，
这正是 M1 的验收点。
"""
import json
from typing import Optional

# 合约规格：XAUUSD 1 手 = 100 盎司，$1 价格变动 = $100/手
CONTRACT_SIZE = 100.0


def load_rates_csv(path: str) -> list:
    """从本地 CSV 加载行情（脱机回放，完全不依赖 MT5）"""
    import csv
    bars = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bars.append({
                "time": row.get("time", ""),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": float(row.get("volume", row.get("tick_volume", 0)) or 0),
            })
    return bars


def fetch_rates_mt5(symbol: str, tf_name: str, days: int) -> list:
    """从 MT5 只读拉历史行情（MT5 仅作数据源，不编译 EA）"""
    import MetaTrader5 as mt5
    from datetime import datetime, timedelta
    if not mt5.initialize():
        raise RuntimeError("MT5 初始化失败，请确认终端已启动并登录；或改用 --csv 脱机回放")
    tf_map = {
        "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
    }
    tf = tf_map.get(tf_name, mt5.TIMEFRAME_M15)
    end = datetime.now()
    start = end - timedelta(days=days)
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError("MT5 行情拉取为空")
    bars = []
    for r in rates:
        bars.append({
            "time": datetime.fromtimestamp(int(r["time"])).isoformat(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": float(r["tick_volume"]),
        })
    return bars


def compute_atr_series(bars: list, period: int = 14) -> list:
    atr = [0.0] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        trs.append(tr)
        if i < period:
            atr[i] = sum(trs[:i + 1]) / (i + 1)
        else:
            atr[i] = sum(trs[i - period + 1:i + 1]) / period
    return atr


def generate_signals(bars: list, atr_series: list, sma_period: int = 20,
                     cooldown: int = 8) -> list:
    """
    简化趋势突破开仓信号（仅作测试激励，产生可比持仓序列）。
    返回 [(bar_idx, side), ...]，side ∈ 'BUY'/'SELL'。
    """
    signals = []
    sma = [0.0] * len(bars)
    for i in range(len(bars)):
        if i < sma_period:
            sma[i] = sum(b["close"] for b in bars[:i + 1]) / (i + 1)
        else:
            sma[i] = sum(b["close"] for b in bars[i - sma_period + 1:i + 1]) / sma_period
    last_sig = -cooldown - 1
    for i in range(sma_period, len(bars)):
        c = bars[i]["close"]
        atr = atr_series[i]
        # 趋势突破 + ATR 过滤（避免亚盘噪声）
        if atr <= 0:
            continue
        if c > sma[i] and (c - sma[i]) > 0.5 * atr and (i - last_sig) >= cooldown:
            signals.append((i, "BUY")); last_sig = i
        elif c < sma[i] and (sma[i] - c) > 0.5 * atr and (i - last_sig) >= cooldown:
            signals.append((i, "SELL")); last_sig = i
    return signals


def _build_market_context(bar: dict, atr: float, equity: float, avg_atr: float) -> dict:
    regime = "volatile" if (atr > avg_atr * 1.3 and avg_atr > 0) else "calm"
    return {
        "symbol": "XAUUSD",
        "bid": round(bar["close"], 2),
        "ask": round(bar["close"] + 0.5, 2),
        "spread": 0.5,
        "atr": round(atr, 2),
        "regime": regime,
        "trend": "up" if bar["close"] >= bar["open"] else "down",
        "support": round(bar["low"], 2),
        "resistance": round(bar["high"], 2),
        "news_in_5min": False,
        "account_equity": round(equity, 2),
        "daily_pnl_pct": 0.0,
    }


def _realized_pnl(side: str, open_price: float, exit_price: float, volume: float) -> float:
    diff = (exit_price - open_price) if side == "BUY" else (open_price - exit_price)
    return diff * volume * CONTRACT_SIZE


def run_backtest(bars: list, mode: str, strategy=None, ai_agent=None,
                 decision_every: int = 4, max_positions: int = 3,
                 initial_equity: float = 10000.0, label: str = "strategy") -> dict:
    """
    mode: 'rule' = 原 smart_exit 规则引擎； 'ai' = AI 出场 Agent；
          'hold' = 仅等经纪商 SL/TP（被动出场基线）
    返回指标 dict（供对比）。
    """
    from app.services.smart_exit import evaluate_position as smart_eval, compute_initial_sl_tp

    atr_series = compute_atr_series(bars, 14)
    avg_atr = sum(a for a in atr_series if a > 0) / max(1, len([a for a in atr_series if a > 0]))
    signals = generate_signals(bars, atr_series)

    positions = []   # 活跃持仓
    trades = []      # 已平仓记录
    equity = initial_equity
    ticket_seq = 1

    for i, bar in enumerate(bars):
        atr = atr_series[i] or avg_atr or 1.0
        # ── 1. 开仓信号 ──
        for (si, side) in signals:
            if si == i and len(positions) < max_positions:
                entry = bar["close"]
                sltp = compute_initial_sl_tp(side=side, entry_price=entry, atr=atr, strategy=strategy)
                pos = {
                    "ticket": f"BT{ticket_seq}", "side": side.lower(),
                    "price_open": entry, "sl": sltp["sl"], "tp": sltp["tp"],
                    "volume": 0.1, "remaining": 0.1,
                    "open_idx": i, "time": bar["time"],
                    "price_current": entry, "profit": 0.0,
                    "max_favorable": 0.0,
                }
                ticket_seq += 1
                positions.append(pos)

        if not positions:
            continue

        # ── 1.5 跟踪 MFE（用本根 high/low，先于决策/平仓，保证捕捉率准确）──
        for p in positions:
            if p["side"] == "buy":
                fav = (bar["high"] - p["price_open"]) * p["remaining"] * CONTRACT_SIZE
            else:
                fav = (p["price_open"] - bar["low"]) * p["remaining"] * CONTRACT_SIZE
            if fav > p["max_favorable"]:
                p["max_favorable"] = fav

        # ── 2. 出场决策 ──
        ctx = _build_market_context(bar, atr, equity, avg_atr)
        decisions = {}
        if mode == "hold":
            for p in positions:
                decisions[p["ticket"]] = {"action": "hold", "close_pct": 0, "new_sl": None, "reason": "被动SL/TP"}
        elif mode == "rule":
            for p in positions:
                decisions[p["ticket"]] = smart_eval(
                    position=p, atr=atr, ai_decision="HOLD", ai_confidence=0, strategy=strategy)
        elif mode == "ai":
            if ai_agent is not None and (i % decision_every == 0):
                raw = ai_agent.evaluate(
                    positions=[p for p in positions], atr=atr, strategy=strategy,
                    market_context=ctx, ai_decision="HOLD", ai_confidence=0.0)
                decisions = raw if raw else {}
            # 非决策周期：维持持仓（不重复调 LLM，省成本）
            for p in positions:
                if p["ticket"] not in decisions:
                    decisions[p["ticket"]] = {"action": "hold", "close_pct": 0, "new_sl": None, "reason": "非决策周期"}

        # ── 3. 执行决策 ──
        for p in list(positions):
            dec = decisions.get(p["ticket"])
            if not dec:
                continue
            action = (dec.get("action") or "hold").lower()
            cur = bar["close"]
            p["price_current"] = cur
            if action in ("full_close", "reverse_signal"):
                pnl = _realized_pnl(p["side"], p["price_open"], cur, p["remaining"])
                p["remaining"] = 0.0
                equity += pnl
                trades.append(_mk_trade(p, cur, pnl, dec.get("reason", "")))
                positions.remove(p)
            elif action == "partial_close":
                cp = max(0.05, min(0.95, float(dec.get("close_pct", 0) or 0)))
                vol = p["remaining"] * cp
                pnl = _realized_pnl(p["side"], p["price_open"], cur, vol)
                p["remaining"] -= vol
                equity += pnl
                trades.append(_mk_trade(p, cur, pnl, dec.get("reason", ""), partial=True, vol=vol))
                if p["remaining"] <= 1e-6:
                    positions.remove(p)
            # new_sl 更新（AI/规则都可能给）
            new_sl = dec.get("new_sl")
            if new_sl is not None and new_sl not in (0, 0.0):
                # 红线：SL 必须置于市价内侧（buy: new_sl<cur；sell: new_sl>cur）
                if (p["side"] == "buy" and new_sl < cur) or (p["side"] == "sell" and new_sl > cur):
                    p["sl"] = float(new_sl)

        # ── 4. 检查 SL/TP 触及（用 high/low 模拟经纪商执行）──
        for p in list(positions):
            hi, lo = bar["high"], bar["low"]
            touched = None
            if p["side"] == "buy":
                if p["sl"] > 0 and lo <= p["sl"]:
                    touched = p["sl"]
                elif p["tp"] > 0 and hi >= p["tp"]:
                    touched = p["tp"]
            else:
                if p["sl"] > 0 and hi >= p["sl"]:
                    touched = p["sl"]
                elif p["tp"] > 0 and lo <= p["tp"]:
                    touched = p["tp"]
            if touched is not None:
                pnl = _realized_pnl(p["side"], p["price_open"], touched, p["remaining"])
                p["remaining"] = 0.0
                equity += pnl
                trades.append(_mk_trade(p, touched, pnl, "SL/TP触发"))
                positions.remove(p)

    return _calc_metrics(trades, label, initial_equity, equity)


def _mk_trade(pos, exit_price, pnl, reason, partial=False, vol=None) -> dict:
    return {
        "ticket": pos["ticket"], "side": pos["side"], "open": pos["price_open"],
        "exit": exit_price, "volume": vol if vol else pos["volume"],
        "pnl": round(pnl, 2), "reason": reason, "partial": partial,
        "max_favorable": round(pos["max_favorable"], 2),
    }


def _calc_metrics(trades: list, label: str, init_eq: float, final_eq: float) -> dict:
    closed = [t for t in trades if not t["partial"]]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    n = len(closed)
    win_rate = (len(wins) / n) if n else 0.0
    pf = (gp / gl) if gl > 0 else float("inf")
    avg_win = (gp / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else float("inf")
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    # 捕捉率 = 实盈 / 最大有利偏移（仅统计曾盈利的平仓；保本单锁利超 MFE 的归一到满）
    cap = [min(t["pnl"] / t["max_favorable"], 1.0) for t in closed
           if t["max_favorable"] > 0 and t["pnl"] > 0]
    avg_capture = sum(cap) / len(cap) if cap else 0.0
    return {
        "label": label,
        "trades_closed": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "pf": round(pf, 3) if pf != float("inf") else "inf",
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff, 3) if payoff != float("inf") else "inf",
        "expectancy": round(expectancy, 2),
        "avg_capture_rate": round(avg_capture, 3),
        "net_pnl": round(sum(t["pnl"] for t in trades), 2),
        "init_equity": init_eq, "final_equity": round(final_eq, 2),
    }
