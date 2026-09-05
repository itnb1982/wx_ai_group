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

for path in ["/api/dashboard/accounts", "/api/dashboard/debug-history"]:
    url = "http://127.0.0.1:8080" + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=280) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"=== {path} ERROR: {repr(e)} ===")
        continue
    print(f"\n========== {path} ==========")
    if path.endswith("accounts"):
        for d in data.get("accounts", []):
            print(f"  {d['name']:<14} login={d['login']}")
            print(f"     今日盈利={d['today_profit']}  历史盈利={d['hist_profit']}  当前浮盈={d['float_pnl']}")
            print(f"     今日订单={d['today_orders']}  历史订单={d['hist_orders']}  持仓数={d['position_count']}")
        p = data.get("portfolio", {})
        print(f"  --- 组合: 今日盈利={p.get('today_profit')} 历史盈利={p.get('hist_profit')} ---")
    else:
        for it in data.get("debug", []):
            print(f"  {it['name']:<14} 今日成交profit={it['real_profit_today']} 90天profit={it['real_profit_90d']} 今日笔数(count)={it['real_trades_today']}")
