import sqlite3
db = 'F:/WanxiangAI/backend/data/wx_prod.dat'
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

# 先确认主号 UUID 在 trades 里到底怎么存
print('=== trades 表中不同 mt5_account_id 取值 ===')
rows = c.execute('SELECT DISTINCT mt5_account_id FROM trades').fetchall()
for r in rows:
    print(repr(r['mt5_account_id']))

print('\n=== 主号(3540bf33)最近15笔，按开仓时间倒序 ===')
rows = c.execute("""
    SELECT mt5_ticket, action, open_price, open_time, profit, net_profit, meta_agent_decision
    FROM trades WHERE mt5_account_id='3540bf33' ORDER BY open_time DESC LIMIT 15
""").fetchall()
for r in rows:
    md = (r['meta_agent_decision'] or '')[:50].replace('\n', ' ')
    print(f"{r['open_time']} | {r['action']:4} @ {r['open_price']} | net={r['net_profit']} | {md}")

print('\n=== 重启后(>=2026-08-14 15:30)全账号新开仓 ===')
rows = c.execute("""
    SELECT mt5_account_id, action, open_price, open_time, meta_agent_decision
    FROM trades WHERE open_time >= '2026-08-14 15:30' ORDER BY open_time
""").fetchall()
print(f'新开仓数={len(rows)}')
for r in rows:
    md = (r['meta_agent_decision'] or '')[:50].replace('\n', ' ')
    print(f"{r['open_time']} | {r['mt5_account_id']} | {r['action']:4} @ {r['open_price']} | {md}")
