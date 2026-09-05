"""
L2 反转平仓 + 浮盈保护 离线单测（不依赖 LLM / MT5）
验证审计修复：
  R1: ai_reverse_close_confidence 默认 0.75（低于门槛不反转）
  R2: 浮盈单(profit>0)绝不被 AI 反向信号平掉（落回追踪/TP）
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/backend")
from app.services.smart_exit import evaluate_position

STRAT = {
    "smart_tp_enabled": True,
    "ai_reverse_close_confidence": 0.75,
    "tp1_atr_mult": 1.0, "tp1_close_pct": 0.40,
    "tp2_atr_mult": 1.5, "tp2_close_pct": 0.30,
    "tp3_atr_mult": 2.5, "tp3_close_pct": 0.20,
    "breakeven_after_tp1": True, "breakeven_buffer_points": 0.5,
    "trailing_atr_mult": 1.5, "trailing_activate_after_tp2": True,
    "enable_trailing_sl": True,
}

def mk_pos(ptype, open_p, cur_p, sl=0.0, tp=0.0, vol=0.01, profit=None):
    if profit is None:
        profit = (cur_p - open_p) * vol * 100 if ptype == "buy" else (open_p - cur_p) * vol * 100
    return {"type": ptype, "price_open": open_p, "price_current": cur_p,
            "sl": sl, "tp": tp, "volume": vol, "profit": profit}

def run(ptype, open_p, cur_p, ai_dec, ai_conf, **kw):
    pos = mk_pos(ptype, open_p, cur_p, **kw)
    return evaluate_position(position=pos, atr=15.0, ai_decision=ai_dec,
                              ai_confidence=ai_conf, strategy=STRAT)

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name}")

print("=== L2 反转平仓 / 浮盈保护 单测 ===")
# 1) 浮盈买多 + AI说SELL(0.80) → 不应反向平(应 hold/保本)
r = run("buy", 4000, 4010, "SELL", 0.80, profit=6.6)
check("浮盈单不被反向平(盈利+6.6, 反向0.80)", r["action"] != "reverse_signal")

# 2) 浮盈卖空 + AI说BUY(0.80) → 不应反向平
r = run("sell", 4000, 3990, "BUY", 0.80, profit=6.6)
check("浮盈空单不被反向平(盈利+6.6, 反向0.80)", r["action"] != "reverse_signal")

# 3) 亏损买多 + AI说SELL(0.80) → 应反向平仓(砍亏单)
r = run("buy", 4000, 3990, "SELL", 0.80, profit=-6.6)
check("亏损单反向平(亏-6.6, 反向0.80, ≥0.75)", r["action"] == "reverse_signal")

# 4) 亏损买多 + AI说SELL(0.70) → 低于门槛0.75, 不应反向平
r = run("buy", 4000, 3990, "SELL", 0.70, profit=-6.6)
check("亏损单低于门槛不反(0.70<0.75)", r["action"] != "reverse_signal")

# 5) 浮盈买多但反向信号刚好0.75门槛 → 浮盈仍保护
r = run("buy", 4000, 4010, "SELL", 0.75, profit=6.6)
check("浮盈单在门槛0.75仍不被反", r["action"] != "reverse_signal")

# 6) 亏损买多反向0.75 → 刚好达到门槛, 反转
r = run("buy", 4000, 3990, "SELL", 0.75, profit=-6.6)
check("亏损单恰好0.75门槛反转", r["action"] == "reverse_signal")

# 7) 同向信号(买多+AI说BUY) → 不反转, 走正常逻辑
r = run("buy", 4000, 4010, "BUY", 0.90, profit=6.6)
check("同向信号不反转", r["action"] != "reverse_signal")

# 8) 浮盈买多触发早期保本(移动SL入) → 即便被反向信号, 仍返回保本hold而非reverse
r = run("buy", 4000, 4012, "SELL", 0.80, profit=8.0, sl=0.0)
# move=12 >= 0.3*15=4.5 → 早期保本 new_sl=4000.5
check("浮盈单落回早期保本(new_sl 下移)", r.get("new_sl") is not None and r["action"] != "reverse_signal")

print(f"\n结果: 通过 {passed} / 失败 {failed}")
sys.exit(1 if failed else 0)
