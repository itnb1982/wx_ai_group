"""验证：出场 token 预算提到 4096 后能否返回有效 decisions。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.core.deepseek_client import DeepSeekClient
import app.config as cfg
cfg.settings.AI_MAX_TOKENS_DEBATE = 4096   # 模拟修复
ds = DeepSeekClient(api_key=cfg.settings.DEEPSEEK_API_KEY)

payload = []
for t, typ, op, cp, sl in [("1001","sell",4067.20,4049.29,4067.20),
                           ("1002","buy",4030.10,4049.50,4025.00),
                           ("1003","sell",4066.00,4050.10,4066.50),
                           ("1004","buy",4031.00,4048.90,4026.00)]:
    profit = round((cp-op)*0.05*100,2) if typ=="buy" else round((op-cp)*0.05*100,2)
    payload.append({"ticket":t,"type":typ,"open_price":op,"current_price":cp,"sl":sl,"tp":0.0,
                    "volume":0.05,"profit":profit,"mfe":9.0,"move_atr":12.0,"holding_minutes":35})
mc = {"regime":"ranging","trend":"weak_down"}
res = ds.evaluate_exits(payload, mc, timeout=50)
print("RESULT:", json.dumps(res, ensure_ascii=False, indent=2)[:1500])
