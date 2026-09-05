"""全模块 API 审计：按前端各功能模块逐一实测接口，输出非 200 与异常响应。

覆盖用户截图箭头指向的所有大模块 → 小模块。
"""
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
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", time.time() - t0


def login():
    for path in ("/api/auth/login", "/api/auth/token", "/api/users/login"):
        st, raw, _ = req("POST", path, body={"email": EMAIL, "password": PASSWORD})
        if st == 200:
            try:
                d = json.loads(raw)
                tok = d.get("access_token") or d.get("token")
                if tok:
                    print(f"[登录] {path} OK")
                    return tok
            except Exception:
                pass
        st2, raw2, _ = req("POST", path, body={"username": EMAIL, "password": PASSWORD})
        if st2 == 200:
            try:
                d = json.loads(raw2)
                tok = d.get("access_token") or d.get("token")
                if tok:
                    print(f"[登录] {path} (username) OK")
                    return tok
            except Exception:
                pass
    print("[登录] 失败，将以匿名方式审计")
    return None


# 模块 → [(method, path)]
MODULES = {
    "健康/基础": [
        ("GET", "/api/health"),
        ("GET", "/api/version"),
    ],
    "仪表盘": [
        ("GET", "/api/dashboard/accounts"),
        ("GET", "/api/dashboard/ai-flow"),
        ("GET", "/api/dashboard/market-chart?tf=M15"),
        ("GET", "/api/dashboard/market-session"),
        ("GET", "/api/dashboard/system-health"),
        ("GET", "/api/dashboard/risk-events?limit=40"),
        ("GET", "/api/dashboard/stats"),
        ("GET", "/api/dashboard/equity-curve"),
        ("GET", "/api/dashboard/positions"),
        ("GET", "/api/dashboard/recent-trades"),
    ],
    "账户管理": [
        ("GET", "/api/accounts/"),
        ("GET", "/api/accounts/status/all"),
        ("GET", "/api/mt5/discover"),
    ],
    "策略与风控": [
        ("GET", "/api/strategy/"),
        ("GET", "/api/strategy/configs"),
        ("GET", "/api/risk/status"),
        ("GET", "/api/risk/config"),
        ("GET", "/api/emergency/status"),
    ],
    "交易": [
        ("GET", "/api/trading/status"),
        ("GET", "/api/trading/history?limit=20"),
        ("GET", "/api/trading/positions"),
    ],
    "AI Key 管理": [
        ("GET", "/api/keys/"),
        ("GET", "/api/keys/status"),
    ],
    "本地模型/信号源参考": [
        ("GET", "/api/local-model/status"),
        ("GET", "/api/ts-reference/status"),
        ("GET", "/api/ts-reference/models"),
        ("GET", "/api/ts-reference/forecast"),
    ],
    "系统管理": [
        ("GET", "/api/system/info"),
        ("GET", "/api/system/logs?limit=20"),
        ("GET", "/api/settings/"),
        ("GET", "/api/license/status"),
    ],
    "AI 决策/复盘": [
        ("GET", "/api/ai/decisions?limit=10"),
        ("GET", "/api/review/summary"),
    ],
}


def main():
    tok = login()
    print()
    bad = []
    slow = []
    missing = []
    for mod, items in MODULES.items():
        print(f"══ {mod} " + "═" * (56 - len(mod)))
        for method, path in items:
            st, raw, dt = req(method, path, tok)
            flag = "OK "
            if st == 404:
                flag = "404"
                missing.append((mod, path))
            elif st == -1 or st >= 500:
                flag = "ERR"
                bad.append((mod, path, st, raw[:400]))
            elif st in (401, 403):
                flag = "AUTH"
            if dt > 3.0:
                slow.append((mod, path, dt))
            snippet = raw.replace("\n", " ")[:110]
            print(f"  [{flag}] {st:>4} {dt:5.2f}s  {path}")
            if flag in ("ERR",):
                print(f"        └─ {snippet}")
        print()

    print("=" * 66)
    print(f"严重错误(5xx/连接失败): {len(bad)}")
    for mod, path, st, raw in bad:
        print(f"  ✗ [{mod}] {path}  -> {st}")
        print(f"     {raw[:300]}")
    print(f"\n路由不存在(404，可能前端在调): {len(missing)}")
    for mod, path in missing:
        print(f"  ? [{mod}] {path}")
    print(f"\n响应慢(>3s): {len(slow)}")
    for mod, path, dt in slow:
        print(f"  ⏱ [{mod}] {path}  {dt:.2f}s")


if __name__ == "__main__":
    main()
