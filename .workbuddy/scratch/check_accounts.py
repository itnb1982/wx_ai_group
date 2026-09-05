"""连续多轮查询各账号实时信息，检测共用终端导致的账号串号问题"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8081"


def req(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


st, tok = req("POST", "/api/auth/login", {"email": "probe@wx.local", "password": "Probe@123456"})
token = tok.get("access_token") or tok.get("token")

for rd in range(3):
    st, data = req("GET", "/api/accounts/status", token=token)
    print(f"--- 第 {rd + 1} 轮 (status={st}) ---")
    items = data if isinstance(data, list) else data.get("accounts", data.get("data", []))
    if isinstance(items, list):
        for a in items:
            print(f"  {str(a.get('name')):14s} login={a.get('account_id')} "
                  f"connected={a.get('is_connected')} balance={a.get('balance')} equity={a.get('equity')}")
    else:
        print(json.dumps(data, ensure_ascii=False)[:800])
    time.sleep(3)
