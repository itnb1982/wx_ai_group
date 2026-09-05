"""为 liumanchun4 指定独立的 MT5 终端安装，消除与 liumanchun3 共用终端导致的账号串号"""
import sqlite3, os

DB = os.path.expanduser("~/.wanxiangai/wanxiangai.db")
NEW_PATH = r"E:\mt5_4\terminal64.exe"

con = sqlite3.connect(DB)
cur = con.cursor()

cur.execute("UPDATE mt5_accounts SET terminal_path=? WHERE name='liumanchun4'", (NEW_PATH,))
# 顺带规范化 liumanchun3 的路径分隔符
cur.execute(
    "UPDATE mt5_accounts SET terminal_path=? WHERE name='liumanchun3'",
    (r"C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe",),
)
con.commit()

cur.execute("SELECT name, account_id, terminal_path FROM mt5_accounts ORDER BY name")
print("更新后的终端映射：")
seen = {}
for name, acc, path in cur.fetchall():
    dup = "  <-- 重复!" if path.lower() in seen else ""
    seen[path.lower()] = name
    print(f"  {name:14s} {acc:>12s}  {path}{dup}")
con.close()
print("\n独立终端数:", len(seen))
