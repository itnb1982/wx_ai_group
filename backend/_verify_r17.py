"""R17 采集器：接口层（只读）。
固化标准动作：
  - accounts 取两次（stale-while-revalidate 是设计行为，首调必陈旧）
  - 同时采集 market-chart / system-health / market-session
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
EMAIL = "1558895@qq.com"
PASSWORD = "Tzhl@708090"
TIMEOUT = 30


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
    st, raw = req("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if st == 200:
        try:
            return json.loads(raw).get("access_token")
        except Exception:
            return None
    return None


out = {}
st, raw = req("GET", "/api/health")
out["health"] = {"status": st, "body": raw}

tok = login()
out["login_ok"] = bool(tok)

# 快照差法：先取缓存值
st, raw = req("GET", "/api/dashboard/accounts", token=tok)
out["accounts_1"] = {"status": st, "body": raw}
# 强刷取真值
st, raw = req("GET", "/api/dashboard/accounts", token=tok)
out["accounts_2"] = {"status": st, "body": raw}

for key, path in (
    ("local_model", "/api/local-model/status"),
    ("market_chart", "/api/dashboard/market-chart?tf=M15"),
    ("system_health", "/api/dashboard/system-health"),
    ("market_session", "/api/dashboard/market-session"),
):
    st, raw = req("GET", path, token=tok)
    out[key] = {"status": st, "body": raw}

sys.stdout.write(json.dumps(out, ensure_ascii=False))
