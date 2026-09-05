import sqlite3, json

DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con = sqlite3.connect(DB)
cur = con.cursor()

# 列出所有表
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print("=== TABLES ===")
print(tables)

# 找账号相关表
for t in tables:
    if 'account' in t.lower() or 'mt5' in t.lower():
        print(f"\n=== SCHEMA: {t} ===")
        cur.execute(f"PRAGMA table_info({t})")
        for col in cur.fetchall():
            print(col)
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print("ROWS:", cur.fetchone()[0])

con.close()
