# -*- coding: utf-8 -*-
import urllib.request, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
B="http://127.0.0.1:8080"
import ssl
def login():
    for em,pw in [("itnb@qq.com","123456"),("admin@wanxiang.ai","admin123"),("itnb","123456")]:
        try:
            d=json.dumps({"email":em,"password":pw}).encode()
            r=urllib.request.Request(B+"/api/auth/login",data=d,headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(r,timeout=15) as x:
                return json.loads(x.read())["access_token"]
        except Exception as e: pass
    return None
tk=login()
print("login:", bool(tk))
H={"Authorization":"Bearer "+tk} if tk else {}
for p in ["/api/dashboard/market-chart?tf=M15","/api/dashboard/system-health","/api/dashboard/ai-flow"]:
    try:
        r=urllib.request.Request(B+p,headers=H)
        with urllib.request.urlopen(r,timeout=25) as x:
            b=json.loads(x.read())
        s=json.dumps(b,ensure_ascii=False)
        print("\n###",p,"len",len(s))
        if "market-chart" in p and isinstance(b,dict):
            c=b.get("candles") or b.get("data") or []
            print(" keys:",list(b.keys()))
            if c: print(" last3:",json.dumps(c[-3:],ensure_ascii=False))
        else:
            print(s[:2600])
    except Exception as e:
        print("\n###",p,"ERR",e)
