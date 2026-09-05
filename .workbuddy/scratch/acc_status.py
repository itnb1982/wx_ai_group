import json, urllib.request, urllib.error
BASE="http://127.0.0.1:8081"
def post(p,d,t=None):
    h={"Content-Type":"application/json"}
    if t: h["Authorization"]=f"Bearer {t}"
    r=urllib.request.Request(BASE+p,data=json.dumps(d).encode(),headers=h,method="POST")
    try:
        with urllib.request.urlopen(r,timeout=20) as x: return x.status,json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code,json.loads(e.read().decode())
def get(p,t):
    r=urllib.request.Request(BASE+p,headers={"Authorization":f"Bearer {t}"})
    try:
        with urllib.request.urlopen(r,timeout=20) as x: return x.status,json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code,json.loads(e.read().decode())
s,b=post("/api/auth/login",{"email":"probe@wx.local","password":"Probe@123456"})
tok=b["access_token"]
print("=== /api/accounts/status (worker连通性) ===")
s,b=get("/api/accounts/status",tok)
print(json.dumps(b,ensure_ascii=False,indent=2))
print("\n=== /api/accounts/ (账号列表) ===")
s,b=get("/api/accounts/",tok)
if isinstance(b,list):
    for a in b:
        print(f"  {a.get('name')} | login={a.get('account_id')} | status={a.get('status')} | connected={a.get('is_connected')} | equity={a.get('equity')} | trading_enabled={a.get('is_trading_enabled')}")
else:
    print(json.dumps(b,ensure_ascii=False)[:500])
