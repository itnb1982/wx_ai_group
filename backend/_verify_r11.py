# -*- coding: utf-8 -*-
"""第11轮：API 采集（health / local-model / accounts强刷 / market-chart / system-health）"""
import json, sys, io, time, urllib.error, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

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
    st, raw = req("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if st == 200:
        d = json.loads(raw)
        return d.get("access_token") or d.get("token")
    return None


def jget(path, tok):
    st, raw = req("GET", path, token=tok)
    if st != 200:
        return {"__err": st, "__raw": raw[:200]}
    try:
        return json.loads(raw)
    except Exception:
        return {"__err": "parse", "__raw": raw[:200]}


# ---------- 1. health ----------
h = jget("/api/health", None)
print("=" * 70)
print("【1. /api/health】")
for k in ("status", "pid", "uptime_sec", "mt5_connected", "trade_stale", "auto_running",
          "l3_guard_alive", "follower_alive", "degrade_level"):
    if k in h:
        print(f"  {k:<18}= {h[k]}")
print(f"  emergency         = {h.get('emergency')}")
mt5c = h.get("mt5_connected")
print(f"  [P0判据] mt5_connected = {mt5c}")

tok = login()
print(f"  login_ok = {bool(tok)}")

# ---------- 2. local-model ----------
lm = jget("/api/local-model/status", tok)
print("\n" + "=" * 70)
print("【2. /api/local-model/status】")
print(json.dumps(lm, ensure_ascii=False, indent=1)[:4000])

# ---------- 3. accounts 强刷（调两次） ----------
a1 = jget("/api/dashboard/accounts", tok)
time.sleep(1.5)
a2 = jget("/api/dashboard/accounts", tok)
print("\n" + "=" * 70)
print("【3. /api/dashboard/accounts 强刷】")
acc = a2 if isinstance(a2, dict) else a1
print("cache_age_sec:", acc.get("cache_age_sec"), "| 第一次:", a1.get("cache_age_sec"))
rows = acc.get("accounts") or acc.get("data") or []
if isinstance(rows, dict):
    rows = rows.get("accounts", [])
tot_pos = 0
for r in rows:
    poss = r.get("positions") or []
    tot_pos += len(poss)
    print(f"\n-- login={r.get('login')} name={r.get('name') or r.get('alias')} "
          f"balance={r.get('balance')} equity={r.get('equity')} "
          f"today_profit={r.get('today_profit')} pos_cnt={r.get('position_count')} 实际={len(poss)}")
    for p in poss:
        print(f"     #{p.get('ticket')} {p.get('type')} vol={p.get('volume')} "
              f"open={p.get('price_open')} cur={p.get('price_current')} "
              f"sl={p.get('sl')} tp={p.get('tp')} P/L={p.get('profit')} time={p.get('time')}")
print(f"\n合计持仓 = {tot_pos}")

# ---------- 4. market-chart ----------
mc = jget("/api/dashboard/market-chart?tf=M15", tok)
print("\n" + "=" * 70)
print("【4. /api/dashboard/market-chart?tf=M15】")
for k in ("current", "indicators", "macro", "trend", "ai_defense", "regime"):
    if k in mc:
        print(f"  {k}: {json.dumps(mc[k], ensure_ascii=False)[:900]}")

# ---------- 5. system-health ----------
sh = jget("/api/dashboard/system-health", tok)
print("\n" + "=" * 70)
print("【5. /api/dashboard/system-health】")
print(json.dumps(sh, ensure_ascii=False)[:2500])
