"""M1 对照诊断：定位出场 LLM 空响应的根因。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.core.deepseek_client import DeepSeekClient, EXIT_DECISION_SYSTEM_PROMPT
import app.config as cfg
ds = DeepSeekClient(api_key=cfg.settings.DEEPSEEK_API_KEY)

def call(messages, rf, label):
    client, key_id = ds._resolve_client(timeout=50)
    try:
        resp = ds._ds_chat(client, key_id, model=ds.model, messages=messages,
                           temperature=0.3, max_tokens=cfg.settings.AI_MAX_TOKENS_DEBATE,
                           response_format=rf)
        c = (resp.choices[0].message.content or "").strip()
        print(f"[{label}] finish={resp.choices[0].finish_reason} len={len(c)} content={c[:300]!r}")
    except Exception as e:
        print(f"[{label}] ERROR {type(e).__name__}: {e}")

# 1) 出场提示词 + json_object（现网配置）
exit_user = "当前XAUUSD市场背景: ranging\n持仓: 1笔 sell 浮盈9$ mfe9.1\n返回严格JSON: {\"decisions\":[...]}"
call([{"role":"system","content":EXIT_DECISION_SYSTEM_PROMPT},
      {"role":"user","content":exit_user}], {"type":"json_object"}, "出场+json_object")

# 2) 出场提示词 + 无 response_format
call([{"role":"system","content":EXIT_DECISION_SYSTEM_PROMPT},
      {"role":"user","content":exit_user}], None, "出场+无rf")

# 3) 极简提示词 + json_object（基线，确认模型本身能返回内容）
call([{"role":"system","content":"你输出JSON。"},
      {"role":"user","content":"返回 {\"decisions\":[{\"ticket\":\"1\",\"action\":\"hold\"}]}"}],
     {"type":"json_object"}, "极简+json_object")

# 4) 出场提示词但改用 deepseek-v4-flash（思考模型）看是否不同
client, key_id = ds._resolve_client(timeout=50)
try:
    resp = ds._ds_chat(client, key_id, model="deepseek-v4-flash",
                       messages=[{"role":"system","content":EXIT_DECISION_SYSTEM_PROMPT},
                                 {"role":"user","content":exit_user}],
                       temperature=0.3, max_tokens=cfg.settings.AI_MAX_TOKENS_DEBATE,
                       response_format={"type":"json_object"})
    c = (resp.choices[0].message.content or "").strip()
    print(f"[出场+flash] finish={resp.choices[0].finish_reason} len={len(c)} content={c[:300]!r}")
except Exception as e:
    print(f"[出场+flash] ERROR {type(e).__name__}: {e}")
