import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:8081"

def post(p, d, t=None):
    h = {"Content-Type": "application/json"}
    if t:
        h["Authorization"] = f"Bearer {t}"
    r = urllib.request.Request(BASE + p, data=json.dumps(d).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def get(p, t):
    r = urllib.request.Request(BASE + p, headers={"Authorization": f"Bearer {t}"})
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

# 1) 登录
s, b = post("/api/auth/login", {"email": "probe@wx.local", "password": "Probe@123456"})
tok = b["access_token"]
print("LOGIN OK")

# 2) 列出账号，拿到 id
s, accts = get("/api/accounts/", tok)
print(f"\n=== 账号列表 ({len(accts) if isinstance(accts,list) else '?'}) ===")
if isinstance(accts, list):
    for a in accts:
        print(f"  {a['name']} | id={a['id']} | trading_enabled={a.get('is_trading_enabled')}")

# 3) 逐个开启交易开关
print("\n=== 开启交易开关 (is_trading_enabled=True) ===")
for a in (accts if isinstance(accts, list) else []):
    s, b = post(f"/api/accounts/{a['id']}/toggle-trading", {"enabled": True}, tok)
    print(f"  {a['name']}: {b}")

# 4) 下单通路探针（绕过AI/风控，直接打MT5；周末应返回经纪商 market_closed）
print("\n=== 下单通路探针 /api/trade/_probe ===")
s, b = post("/api/trade/_probe", {}, tok)
print(json.dumps(b, ensure_ascii=False, indent=2))

# 5) 重启自动循环
print("\n=== 重启自动循环 /api/trade/auto/start ===")
s, b = post("/api/trade/auto/start", {}, tok)
print(json.dumps(b, ensure_ascii=False))

# 6) 查询循环状态
s, b = get("/api/trade/status", tok)
print("\n=== 自动循环状态 ===")
print(json.dumps(b, ensure_ascii=False, indent=2))
