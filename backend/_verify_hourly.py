"""每小时持续验证采集器（只读，不修改任何交易代码/配置/下单）。"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
EMAIL = "1558895@qq.com"
PASSWORD = "Tzhl@708090"
TIMEOUT = 25


def req(method, path, token=None, body=None, timeout=TIMEOUT):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def login():
    for path in ("/api/auth/login", "/api/auth/token", "/api/users/login"):
        for body in ({"email": EMAIL, "password": PASSWORD},
                     {"username": EMAIL, "password": PASSWORD}):
            st, raw = req("POST", path, body=body)
            if st == 200:
                try:
                    d = json.loads(raw)
                    tok = d.get("access_token") or d.get("token")
                    if tok:
                        return tok
                except Exception:
                    pass
    return None


out = {}
st, raw = req("GET", "/api/health")
out["health"] = {"status": st, "body": raw}

tok = login()
out["login_ok"] = bool(tok)

for key, path in (("local_model", "/api/local-model/status"),
                  ("accounts", "/api/dashboard/accounts")):
    st, raw = req("GET", path, token=tok)
    out[key] = {"status": st, "body": raw}

sys.stdout.write(json.dumps(out, ensure_ascii=False))
