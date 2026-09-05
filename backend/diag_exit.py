"""M1 AI出场诊断：实测 DeepSeek 对真实持仓 payload 的返回，确认为何 decisions 为空。"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.core.deepseek_client import DeepSeekClient, EXIT_DECISION_SYSTEM_PROMPT

# 用 .env fallback key 构建 client（与 app 单 key 模式一致）
import app.config as cfg
ds = DeepSeekClient(api_key=cfg.settings.DEEPSEEK_API_KEY)

# 还原 _build_payload 的 8 笔持仓结构（贴近日志里 2877213e 的真实场景）
positions = []
sample = [
    # ticket, type, open, current, sl, tp, vol, profit, mfe
    ("1001", "sell", 4067.20, 4049.29, 4067.20, 0.0, 0.05, 8.95, 9.10),
    ("1002", "sell", 4067.18, 4049.18, 4067.24, 0.0, 0.05, 9.00, 9.05),
    ("1003", "buy",  4030.10, 4049.50, 4025.00, 0.0, 0.05, 9.70, 9.80),
    ("1004", "sell", 4066.00, 4050.10, 4066.50, 0.0, 0.05, 7.90, 8.20),
    ("1005", "buy",  4031.00, 4048.90, 4026.00, 0.0, 0.05, 8.90, 9.00),
    ("1006", "sell", 4068.00, 4051.30, 4068.40, 0.0, 0.05, 8.30, 8.60),
    ("1007", "buy",  4029.50, 4049.80, 4024.50, 0.0, 0.05, 10.15, 10.20),
    ("1008", "sell", 4069.10, 4052.00, 4069.50, 0.0, 0.05, 8.55, 8.90),
]
for t, typ, op, cp, sl, tp, vol, pf, mfe in sample:
    positions.append({
        "ticket": t, "type": typ, "price_open": op, "price_current": cp,
        "sl": sl, "tp": tp, "volume": vol, "profit": pf, "mfe": mfe,
    })

payload = []
for p in positions:
    op = float(p["price_open"]); cp = float(p["price_current"])
    move = ((cp-op)/1.5) if p["type"]=="buy" else ((op-cp)/1.5)
    payload.append({
        "ticket": str(p["ticket"]), "type": p["type"], "open_price": round(op,2),
        "current_price": round(cp,2), "sl": float(p["sl"]), "tp": float(p["tp"]),
        "volume": float(p["volume"]), "profit": round(p["profit"],2),
        "mfe": round(p["mfe"],2), "move_atr": round(move,2), "holding_minutes": 35,
    })

market_context = {"regime": "ranging", "trend": "weak_down", "note": "黄金区间震荡，关注4050支撑"}

print("=== payload ===")
print(json.dumps(payload, ensure_ascii=False, indent=2))

print("\n=== 调用 evaluate_exits ===")
res = ds.evaluate_exits(payload, market_context, timeout=50)
print("RESULT:", json.dumps(res, ensure_ascii=False, indent=2))

print("\n=== RAW LLM content（直接复现 chat 调用）===")
client, key_id = ds._resolve_client(timeout=50)
user_prompt = (
    f"当前XAUUSD市场背景:\n{json.dumps(market_context, indent=2, ensure_ascii=False)}\n\n"
    f"需要决策的持仓(共{len(payload)}笔):\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
    f"请对每笔持仓给出出场决策，返回严格JSON: {{\"decisions\":[...]}}"
)
try:
    resp = ds._ds_chat(client, key_id, model=ds.model,
                       messages=[{"role":"system","content":EXIT_DECISION_SYSTEM_PROMPT},
                                 {"role":"user","content":user_prompt}],
                       temperature=0.3, max_tokens=cfg.settings.AI_MAX_TOKENS_DEBATE,
                       response_format={"type":"json_object"})
    raw = resp.choices[0].message.content
    print("RAW CONTENT:\n", raw)
except Exception as e:
    print("RAW CALL ERROR:", repr(e))
