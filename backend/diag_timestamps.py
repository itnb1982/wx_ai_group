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
cur.execute(f"SELECT mt5_account_id,mt5_ticket,open_time,action,open_price FROM trades WHERE mt5_account_id IN ({q}) ORDER BY mt5_account_id,open_time",list(ACT))
rows=cur.fetchall()
by=defaultdict(list)
for r in rows: by[r["mt5_account_id"]].append(r)
# 看主号与跟号前5笔时间，判断时间戳是否同源
for uid,lab in ACT.items():
    print(f"\n[{lab}] 前5笔:")
    for r in by[uid][:5]:
        print(f"  {r['open_time']}  {r['action']:4}  ticket={r['mt5_ticket']}  op={r['open_price']}")
con.close()
