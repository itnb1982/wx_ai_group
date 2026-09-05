import sqlite3, os

db = os.path.expanduser("~/.wanxiangai/wanxiangai.db")
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("PRAGMA table_info(mt5_accounts)")
cols = [r[1] for r in cur.fetchall()]
print("COLS:", cols)
cur.execute("SELECT * FROM mt5_accounts")
for row in cur.fetchall():
    d = dict(zip(cols, row))
    print("-" * 60)
    for k in ("id", "name", "account_id", "server", "terminal_path", "is_trading_enabled", "role"):
        if k in d:
            print(f"  {k} = {d[k]}")
con.close()
