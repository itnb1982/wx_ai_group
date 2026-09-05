import json, urllib.request, urllib.error, base64, hmac, hashlib, time

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
UIDB = "6af9b283-2572-4034-b4ee-d0dbf2fc2584"

def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def mk(uid):
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": uid, "email": "x@x.com", "exp": int(time.time()) + 3600}).encode())
    s = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{s}"

def call(method, url, token=None, body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        ct = r.headers.get("content-type", "")
        if ct.startswith("application/json"):
            try:
                return r.status, json.loads(r.read().decode() or "null")
            except Exception:
                return r.status, None
        return r.status, r.read().decode()[:120]
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode() or "null")
        except Exception:
            j = e.read().decode()[:120]
        return e.code, j

B = "http://127.0.0.1:8080"
tok = mk(UID)
tokB = mk(UIDB)
acct = "2877213e-e79f-4ac4-93cd-4db64730bc04"

print("== 1 health ==")
print("  ", call("GET", f"{B}/api/health")[0])

print("== 2 system-health (P0) ==")
s, d = call("GET", f"{B}/api/dashboard/system-health", tok)
print("  ", s, "accts=", len(d.get("accounts", [])) if isinstance(d, dict) else d)

print("== 3 accounts list ==")
s, d = call("GET", f"{B}/api/accounts/", tok)
print("  ", s, "n=", len(d) if isinstance(d, list) else d,
      [(a["name"], a["is_market_primary"]) for a in d] if isinstance(d, list) else "")

print("== 4 market-chart 隔离 userB(无账号) ==")
s, d = call("GET", f"{B}/api/dashboard/market-chart", tokB)
print("  ", s, type(d).__name__, (d.get("error") or "primary=" + str(d.get("primary_account")) or "ok") if isinstance(d, dict) else str(d)[:100])

print("== 5 market-chart userA(有账号) ==")
s, d = call("GET", f"{B}/api/dashboard/market-chart", tok)
print("  ", s, (d.get("error") or "primary=" + str(d.get("primary_account")) or "ok") if isinstance(d, dict) else str(d)[:100])

print("== 6 login 错密码 ==")
s, d = call("POST", f"{B}/api/auth/login", None, {"email": "probe@wx.local", "password": "wrong"})
print("  ", s)

print("== 7 strategy 负本金 ==")
s, d = call("PUT", f"{B}/api/strategy/{acct}", tok, {"base_capital": -5})
print("  ", s, d.get("detail") if isinstance(d, dict) else d)

print("== 8 strategy 非法 sizing_mode ==")
s, d = call("PUT", f"{B}/api/strategy/{acct}", tok, {"sizing_mode": "xxx"})
print("  ", s, d.get("detail") if isinstance(d, dict) else d)

print("== 9 trading 端点存在性 (GET 无效, 不真下单) ==")
for ep in ["/api/trading/state", "/api/trading/run-cycle", "/api/trading/manual-order"]:
    s, d = call("GET", f"{B}{ep}", tok)
    print(f"  GET {ep} ->", s, (str(d)[:80] if d else ""))

print("== 10 账户 positions/status ==")
s, d = call("GET", f"{B}/api/accounts/{acct}/positions", tok)
print("  positions", s, (len(d) if isinstance(d, list) else d))
s, d = call("GET", f"{B}/api/accounts/{acct}/status", tok)
print("  status", s, (d if isinstance(d, dict) else d))
