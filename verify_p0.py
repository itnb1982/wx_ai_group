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

def get(path):
    req = urllib.request.Request("http://127.0.0.1:8080" + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

print("=== 1) market-chart (AI 作战布防) ===")
try:
    c = get("/api/dashboard/market-chart?tf=M15")
    ad = c.get("ai_defense")
    print("  ai_defense 存在:", bool(ad))
    if ad:
        print("    net_bias      :", ad.get("net_bias"))
        print("    bias_strength :", ad.get("bias_strength"))
        print("    total/sell/buy:", ad.get("total"), ad.get("sell_count"), ad.get("buy_count"))
        print("    avg_entry     :", ad.get("avg_entry"))
        print("    avg_sl/avg_tp :", ad.get("avg_sl"), ad.get("avg_tp"))
        print("    ai_read       :", ad.get("ai_read"))
    print("  bars 数量       :", len(c.get("bars", [])))
except Exception as e:
    print("  ERROR:", repr(e))

print("\n=== 2) equity-curve (组合累计盈利) ===")
try:
    eq = get("/api/dashboard/equity-curve?days=30")
    s = eq.get("series", [])
    print("  series 长度   :", len(s))
    print("  total_cum     :", eq.get("total_cum"))
    print("  total_daily   :", eq.get("total_daily"))
    if s:
        print("  首点:", s[0])
        print("  末点:", s[-1])
except Exception as e:
    print("  ERROR:", repr(e))

print("\n=== 3) accounts (账户网格) ===")
try:
    a = get("/api/dashboard/accounts")
    pf = a.get("portfolio", {})
    print("  portfolio.today_profit:", pf.get("today_profit"))
    print("  portfolio.hist_profit :", pf.get("hist_profit"))
    print("  accounts 数           :", len(a.get("accounts", [])))
except Exception as e:
    print("  ERROR:", repr(e))

print("\nDONE")
