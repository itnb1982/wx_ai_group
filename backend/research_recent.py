import sqlite3
from collections import defaultdict
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
cur = con.cursor()
ACT = {
 "3540bf33-ee40-4169-8099-7c9616406d99":"主号1610098464",
 "2877213e-e79f-4ac4-93cd-4db64730bc04":"跟号1610093299",
 "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd":"跟号1610097175",
}
q=",".join("?"*3)
WIN=300.0
def parse(t):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f","%Y-%m-%d %H:%M:%S"):
        try: return __import__("datetime").datetime.strptime(t,fmt)
        except: pass
    return None

def load(uid):
    cur.execute("""SELECT mt5_ticket,open_time,action,open_price,close_price,volume,
                   profit,net_profit,result,exit_reason,meta_agent_decision,
                   deepseek_decision,hunyuan_decision,chronos_vote,
                   deepseek_reasoning,hunyuan_reasoning
                   FROM trades WHERE mt5_account_id=? AND open_time>='2026-08-13'
                   ORDER BY open_time""",(uid,))
    return cur.fetchall()

lead=load("3540bf33-ee40-4169-8099-7c9616406d99")
f1=load("2877213e-e79f-4ac4-93cd-4db64730bc04")
f2=load("b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd")
print(f"近两日: 主号={len(lead)} 跟号3299={len(f1)} 跟号7175={len(f2)}")

def npf(r): return r["net_profit"] if r["net_profit"] is not None else (r["profit"] or 0)

def match(lead_trade, foll):
    lt=parse(lead_trade["open_time"]); la=lead_trade["action"]
    best=None;bd=1e9
    for r in foll:
        ft=parse(r["open_time"])
        if not ft: continue
        d=abs((ft-lt).total_seconds())
        if d<=WIN and r["action"]==la and d<bd:
            bd=d;best=r
    return best

m1=m2=0; unmatched=[]
for r in lead:
    a=match(r,f1); b=match(r,f2)
    if a: m1+=1
    if b: m2+=1
    if not a or not b: unmatched.append((r,a,b))
print(f"主号近两日同向匹配: 跟号3299={m1}/{len(lead)}  跟号7175={m2}/{len(lead)}")
print(f"未双向匹配的主号单={len(unmatched)}")
# 未匹配原因分类
from collections import Counter
c=Counter()
for r,a,b in unmatched:
    if not a and not b: c["两跟号均无"]+=1
    elif not a: c["缺3299"]+=1
    elif not b: c["缺7175"]+=1
print("未匹配分布:",dict(c))

print("\n########## 主号近两日亏损单（错方向研究） ##########")
ll=[r for r in lead if npf(r)<0]
print(f"主号近两日亏损单={len(ll)} 笔, 总亏损=${sum(npf(r) for r in ll):.2f}\n")
for r in sorted(ll,key=lambda x:x["open_time"]):
    ds=(r["deepseek_reasoning"] or "")[:120].replace("\n"," ")
    print(f"{r['open_time'][:16]} {r['action']:4} @{r['open_price']}->{r['close_price']} npf=${npf(r):.2f} exit={r['exit_reason']} meta={r['meta_agent_decision']}")
    print(f"   DS={r['deepseek_decision']} HY={r['hunyuan_decision']} Ch={r['chronos_vote']} | {ds}")
con.close()
