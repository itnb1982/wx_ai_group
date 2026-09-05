"""最终验证：生产配置下 evaluate_exits 对 8 笔持仓返回有效 decisions。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.core.deepseek_client import DeepSeekClient
import app.config as cfg
print(f"[配置] AI_MAX_TOKENS_ANALYSIS={cfg.settings.AI_MAX_TOKENS_ANALYSIS} "
      f"AI_MAX_TOKENS_DEBATE={cfg.settings.AI_MAX_TOKENS_DEBATE} model={cfg.settings.DEEPSEEK_MODEL}")
ds = DeepSeekClient(api_key=cfg.settings.DEEPSEEK_API_KEY)
# 还原真实 _build_payload 的 8 笔结构
payload = []
rows = [("1001","sell",4067.20,4049.29,4067.20),("1002","sell",4067.18,4049.18,4067.24),
        ("1003","buy",4030.10,4049.50,4025.00),("1004","sell",4066.00,4050.10,4066.50),
        ("1005","buy",4031.00,4048.90,4026.00),("1006","sell",4068.00,4051.30,4068.40),
        ("1007","buy",4029.50,4049.80,4024.50),("1008","sell",4069.10,4052.00,4069.50)]
for t,typ,op,cp,sl in rows:
    profit = round((cp-op)*0.05*100,2) if typ=="buy" else round((op-cp)*0.05*100,2)
    payload.append({"ticket":t,"type":typ,"open_price":op,"current_price":cp,"sl":sl,"tp":0.0,
                    "volume":0.05,"profit":profit,"mfe":9.0,"move_atr":12.0,"holding_minutes":35})
mc = {"regime":"ranging","trend":"weak_down"}
res = ds.evaluate_exits(payload, mc, timeout=50)
dec = res.get("decisions", [])
print(f"RESULT error={res.get('error')} decisions={len(dec)}")
for d in dec:
    print(f"  ticket={d.get('ticket')} action={d.get('action')} new_sl={d.get('new_sl')} reason={str(d.get('reason'))[:40]}")
