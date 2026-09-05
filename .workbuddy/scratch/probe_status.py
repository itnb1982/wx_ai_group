import json, urllib.request, urllib.error

BASE = "http://127.0.0.1:8081"

def post(path, data, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def get(path, token):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

# 1) 登录 probe
st, body = post("/api/auth/login", {"email": "probe@wx.local", "password": "Probe@123456"})
if st != 200:
    print("登录失败:", st, body)
    raise SystemExit
token = body["access_token"]
print("LOGIN OK, token前20位:", token[:20], "...")

# 2) 自动循环状态
st, body = get("/api/trade/status", token)
print("\n=== /api/trade/status ===")
print(json.dumps(body, ensure_ascii=False, indent=2)[:1600])

# 3) 账户列表
st, body = get("/api/accounts", token)
print("\n=== /api/accounts ===")
if isinstance(body, list):
    for a in body:
        print(f"  {a.get('name')} | login={a.get('login')} | status={a.get('status')} | equity={a.get('equity')} | server={a.get('server')}")
    print(f"  共 {len(body)} 个账号")
else:
    print(json.dumps(body, ensure_ascii=False)[:800])
