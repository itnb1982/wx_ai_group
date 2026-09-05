# -*- coding: utf-8 -*-
"""R19 行情快照 + unverified批次倒推 + 锁利/timeout按小时（复用 JWT）"""
import json, base64, hmac, hashlib, time, urllib.request, sys, re, collections
sys.stdout.reconfigure(encoding="utf-8")
BASE="http://127.0.0.1:8080"
SECRET="5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt():
    h=b64u(json.dumps({"alg":"HS256","typ":"JWT"}).encode()); n=int(time.time())
    p=b64u(json.dumps({"sub":"6f50aea4-7879-4d6d-8046-9b9d9f1989a3","email":"1558895@qq.com","exp":n+3600,"iat":n}).encode())
    s=hmac.new(SECRET.encode(),f"{h}.{p}".encode(),hashlib.sha256).digest()
    return f"{h}.{p}.{b64u(s)}"
def get(u):
    r=urllib.request.Request(u); r.add_header("Authorization",f"Bearer {jwt()}")
    with urllib.request.urlopen(r,timeout=12) as x: return json.loads(x.read().decode())

print("="*80); print("[行情快照 /api/dashboard/market-chart?tf=M15]")
try:
    d=get(f"{BASE}/api/dashboard/market-chart?tf=M15")
    for k in ("current","indicators","macro","trend","ai_defense","regime"):
        if k in d: print(f"  {k} = {json.dumps(d[k],ensure_ascii=False)[:500]}")
    print("  top-keys:", list(d.keys()))
except Exception as e:
    print("  market-chart ERR:", e)

print("="*80); print("[系统健康 /api/dashboard/system-health]")
try:
    d=get(f"{BASE}/api/dashboard/system-health")
    print("  ", json.dumps(d,ensure_ascii=False)[:700])
except Exception as e:
    print("  system-health ERR:", e)
