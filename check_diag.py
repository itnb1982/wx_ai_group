"""调用后端 /api/trade/_diag 与 /_probe，验证 10027 是否解除"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8081"
USER = "probe@wx.local"
PWD = "Probe@123456"


def req(method, path, data=None, token=None):
    url = BASE + path
    body = None
    headers = {"Content-Type": "application/json"}
    if data is not None:
        body = json.dumps(data).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 登录
st, tok = req("POST", "/api/auth/login", {"email": USER, "password": PWD})
if st != 200:
    st, tok = req("POST", "/api/auth/login", {"username": USER, "password": PWD})
print("[login]", st)
if st != 200:
    print(tok)
    raise SystemExit(1)
token = tok.get("access_token") or tok.get("token")

st, diag = req("GET", "/api/trade/_diag", token=token)
print("\n[_diag]", st)
if isinstance(diag, list):
    for a in diag:
        t = a.get("terminal", {})
        print(f"  {a.get('name'):14s} login={a.get('login')} "
              f"trade_allowed={t.get('trade_allowed')} "
              f"tradeapi_disabled={t.get('tradeapi_disabled')} "
              f"connected={t.get('connected')} data_path={str(t.get('data_path'))[-12:]}")
else:
    print(json.dumps(diag, ensure_ascii=False, indent=2)[:2000])

st, probe = req("POST", "/api/trade/_probe", token=token)
print("\n[_probe]", st)
print(json.dumps(probe, ensure_ascii=False, indent=2)[:2000])
