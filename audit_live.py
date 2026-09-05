import hmac, hashlib, base64, json, time, urllib.request

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
USER_ID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")

header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
now = int(time.time())
payload = b64url(json.dumps({"sub": USER_ID, "exp": now + 3600, "iat": now}).encode())
sig = b64url(hmac.new(SECRET.encode(), header + b"." + payload, "sha256").digest())
token = (header + b"." + payload + b"." + sig).decode()

ACC = ["2877213e-e79f-4ac4-93cd-4db64730bc04",
       "3540bf33-ee40-4169-8099-7c9616406d99",
       "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3",
       "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd"]

def get(path):
    url = "http://127.0.0.1:8080" + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"__error__": repr(e)}

print("===== 账户今日/组合汇总 =====")
d = get("/api/dashboard/accounts")
if isinstance(d, dict) and "accounts" in d:
    for a in d["accounts"]:
        print(f"  {a.get('name','?'):<14} 今日盈亏={a.get('today_profit')} 浮盈={a.get('float_pnl')} "
              f"持仓数={a.get('position_count')} 今日订单={a.get('today_orders')} 余额={a.get('balance')}")
    p = d.get("portfolio", {})
    print(f"  --- 组合: 今日盈亏={p.get('today_profit')} 浮盈={p.get('float_pnl')} ---")
else:
    print("  accounts接口返回:", str(d)[:300])

print("\n===== 各账号实时持仓 =====")
for aid in ACC:
    pos = get(f"/api/accounts/{aid}/positions")
    if isinstance(pos, dict) and "__error__" in pos:
        print(f"  {aid[:8]}: 错误 {pos['__error__']}")
        continue
    print(f"\n  --- 账号 {aid[:8]} 持仓数={len(pos) if isinstance(pos,list) else '?'}")
    if isinstance(pos, list):
        for p in pos:
            print(f"    ticket={p.get('ticket')} {str(p.get('type')).upper():4s} vol={p.get('volume')} "
                  f"open={p.get('open_price')} cur={p.get('current_price')} sl={p.get('sl')} tp={p.get('tp')} "
                  f"pnl={p.get('pnl') or p.get('profit')} cmt={str(p.get('comment'))[:30]}")
