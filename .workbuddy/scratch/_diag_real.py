import json, urllib.request, time
import jwt

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UID = "6af9b283-2572-4034-b4ee-d0dbf2fc2584"
tok = jwt.encode({"sub": UID, "exp": int(time.time())+3600}, SECRET, algorithm="HS256")

def call(m, p, body=None):
    h = {"Content-Type":"application/json","Authorization":"Bearer "+tok}
    r = urllib.request.Request("http://127.0.0.1:8081"+p,
        data=json.dumps(body).encode() if body else None, headers=h, method=m)
    try:
        return json.loads(urllib.request.urlopen(r, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:300]}

print("=== /api/accounts/ ===")
print(json.dumps(call("GET","/api/accounts/"), ensure_ascii=False)[:800])

print("\n=== /api/keys/ ===")
ks = call("GET","/api/keys/")
print(json.dumps(ks, ensure_ascii=False)[:800])

print("\n=== /api/dashboard/market-session ===")
print(json.dumps(call("GET","/api/dashboard/market-session"), ensure_ascii=False)[:500])

print("\n=== /api/dashboard/market-chart?tf=H1 ===")
mc = call("GET","/api/dashboard/market-chart?tf=H1")
if isinstance(mc, dict):
    bars = mc.get("bars", [])
    print("symbol:", mc.get("symbol"), "bars数:", len(bars), "server_time:", mc.get("server_time"))
    print("current:", mc.get("current"))
    print("前2根bars:", json.dumps(bars[:2], ensure_ascii=False) if bars else "无")
    print("indicators keys:", list((mc.get("indicators") or {}).keys()) if mc.get("indicators") else None)
else:
    print(mc)

print("\n=== /api/mt5-discover/discover ===")
print(json.dumps(call("GET","/api/mt5-discover/discover"), ensure_ascii=False)[:600])
