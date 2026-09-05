import sqlite3
db = 'F:/WanxiangAI/backend/data/wx_prod.dat'
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

LEADER = '3540bf33%'
print('=== 主号历史 action 分布与盈亏 ===')
rows = c.execute("""
    SELECT action, COUNT(*) n,
           ROUND(SUM(COALESCE(net_profit,profit,0)),2) net
    FROM trades WHERE mt5_account_id LIKE ? GROUP BY action
""", (LEADER,)).fetchall()
for r in rows:
    print(f"  {r['action']:5} 笔数={r['n']:4}  净盈亏=${r['net']}")

print('\n=== 主号全部订单(按时间倒序，最近25笔) ===')
rows = c.execute("""
    SELECT mt5_ticket, action, open_price, open_time,
           ROUND(COALESCE(net_profit,profit,0),2) net,
           substr(meta_agent_decision,1,55) md
    FROM trades WHERE mt5_account_id LIKE ? ORDER BY open_time DESC LIMIT 25
""", (LEADER,)).fetchall()
for r in rows:
    md = (r['md'] or '').replace('\n',' ')
    print(f"{r['open_time']} | {r['action']:4}@{r['open_price']} | net=${r['net']} | {md}")

print('\n=== 重启后(>=2026-08-14 15:30)全账号新开仓 ===')
rows = c.execute("""
    SELECT mt5_account_id, action, open_price, open_time
    FROM trades WHERE open_time >= '2026-08-14 15:30' ORDER BY open_time
""").fetchall()
print(f'新开仓数={len(rows)}')
for r in rows:
    print(f"  {r['open_time']} | {r['mt5_account_id'][:8]} | {r['action']}@{r['open_price']}")
