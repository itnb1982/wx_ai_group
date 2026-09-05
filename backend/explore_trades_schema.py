import sqlite3
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

# trades 表结构
cur.execute("PRAGMA table_info(trades)")
cols = [c[1] for c in cur.fetchall()]
print("=== trades 列 ===")
print(cols)

# 三个活跃账号 UUID
ACT = {
    "3540bf33-ee40-4169-8099-7c9616406d99": "主号1610098464",
    "2877213e-e79f-4ac4-93cd-4db64730bc04": "跟号1610093299",
    "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd": "跟号1610097175",
}
qmarks = ",".join("?"*len(ACT))
cur.execute(f"""SELECT mt5_account_id, mt5_ticket, open_time, close_time, direction,
                       open_price, close_price, volume, profit, net_profit, result, exit_reason
                FROM trades WHERE mt5_account_id IN ({qmarks})
                ORDER BY open_time""", list(ACT.keys()))
rows = cur.fetchall()
print("\n=== 三活跃账号总成交数:", len(rows), "===")

from collections import Counter, defaultdict
by_acc = defaultdict(list)
for r in rows:
    by_acc[r["mt5_account_id"]].append(r)

for uid, label in ACT.items():
    rs = by_acc[uid]
    wins = sum(1 for r in rs if (r["net_profit"] or r["profit"] or 0) > 0)
    loss = sum(1 for r in rs if (r["net_profit"] or r["profit"] or 0) < 0)
    flat = len(rs) - wins - loss
    print(f"\n[{label}] {uid[:8]}  笔数={len(rs)} 赢={wins} 亏={loss} 平={flat}")
con.close()
