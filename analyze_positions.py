import hmac, hashlib, base64, json, time, urllib.request

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
USER_ID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")

header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
now = int(time.time())
payload = b64url(json.dumps({"sub": USER_ID, "exp": now + 3600, "iat": now}).encode())
sig = b64url(hmac.new(SECRET.encode(), header + b"." + payload, hashlib.sha256).digest())
token = (header + b"." + payload + b"." + sig).decode()

url = "http://127.0.0.1:8080/api/dashboard/accounts"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
with urllib.request.urlopen(req, timeout=280) as r:
    data = json.loads(r.read().decode())

print("=" * 80)
print("各账号当前持仓明细 + 风控体检")
print("=" * 80)
for d in data.get("accounts", []):
    name = d["name"]
    positions = d.get("positions", [])
    float_total = d.get("float_pnl", 0)
    print(f"\n### {name} (login={d['login']})  当前浮盈合计={float_total}  持仓数={len(positions)}")
    if not positions:
        print("   (无持仓)")
        continue
    worst = min(positions, key=lambda p: p.get("profit") or 0)
    longest = max(positions, key=lambda p: p.get("holding_minutes") or 0)
    no_sl = [p for p in positions if not (p.get("sl") or 0)]
    big = [p for p in positions if (p.get("volume") or 0) >= 0.5]
    for p in positions:
        sl_tag = "无SL" if not (p.get("sl") or 0) else f"SL={p['sl']}"
        tp_tag = "无TP" if not (p.get("tp") or 0) else f"TP={p['tp']}"
        print(f"   ticket={p.get('ticket')} {'买' if p.get('type')==0 else '卖'} "
              f"手数={p.get('volume')} 浮亏={round(p.get('profit') or 0,2)} "
              f"持仓={p.get('holding_minutes')}分 {sl_tag} {tp_tag}")
    print(f"   >> 最差单笔浮亏={round(worst.get('profit') or 0,2)}  最长持仓={longest.get('holding_minutes')}分")
    print(f"   >> 无止损单={len(no_sl)}/{len(positions)}  重仓(>=0.5手)={len(big)}")
