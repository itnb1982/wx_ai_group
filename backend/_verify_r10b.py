# -*- coding: utf-8 -*-
import json, sys, io, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, ".")
from _verify_hourly import req, login  # noqa

tok = login()
print("login_ok:", bool(tok))

def g(p):
    st, raw = req("GET", p, token=tok)
    if st != 200:
        return {"__error__": f"HTTP {st}: {raw[:200]}"}
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw__": raw[:500]}

mc = g("/api/dashboard/market-chart?tf=M15")
if "__error__" in mc:
    print("market-chart:", mc)
else:
    print("--keys--", list(mc.keys()))
    for k in ("current", "indicators", "macro", "trend", "ai_defense", "chronos", "prediction", "summary"):
        if k in mc:
            print(f"--{k}--", json.dumps(mc[k], ensure_ascii=False)[:1500])

sh = g("/api/dashboard/system-health")
print("\n--system-health--", json.dumps(sh, ensure_ascii=False)[:2500])
