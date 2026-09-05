import json, urllib.request, urllib.error, base64, hmac, hashlib, time

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UIDA = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
UIDB = "6af9b283-2572-4034-b4ee-d0dbf2fc2584"

def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def mk(uid):
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": uid, "email": "x@x.com", "exp": int(time.time()) + 3600}).encode())
    s = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{s}"

def get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")

B = "http://127.0.0.1:8080"
tokA = mk(UIDA)
tokB = mk(UIDB)

# 精确隔离验证：userB 的响应绝不能出现 userA 的主号 login 1610093299
sA, dA = get(f"{B}/api/dashboard/market-chart?tf=H1", tokA)
sB, dB = get(f"{B}/api/dashboard/market-chart?tf=H1", tokB)
print("userA status:", sA, "| has mt5 block:", "mt5" in dA, "| mt5 login:", dA.get("mt5", {}).get("login") if isinstance(dA, dict) else dA)
print("userB status:", sB, "| is error:", dB.get("error") if isinstance(dB, dict) else dB)
# 泄漏检查
ja = json.dumps(dA)
jb = json.dumps(dB)
print("LEAK CHECK userB contains 1610093299?", "1610093299" in jb)
print("LEAK CHECK userB contains userA name liumanchun1?", "liumanchun1" in jb)
print("userA chart has bars?", isinstance(dA, dict) and len(dA.get("bars", [])) > 0)
