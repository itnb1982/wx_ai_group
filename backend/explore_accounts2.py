import sqlite3
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()
cur.execute("""SELECT id, name, account_id, account_type, status, is_trading_enabled,
                      is_market_primary, balance, equity, server
               FROM mt5_accounts ORDER BY is_market_primary DESC, account_id""")
for r in cur.fetchall():
    print("UUID=%s" % r["id"])
    print("  账号号(account_id)=%s  名称=%s" % (r["account_id"], r["name"]))
    print("  类型=%s  状态=%s  交易开关=%s  主号=%s" % (
        r["account_type"], r["status"], r["is_trading_enabled"], r["is_market_primary"]))
    print("  余额=%.2f  净值=%.2f  服务器=%s" % (r["balance"], r["equity"], r["server"]))
    print()

# 确认 1610098464 的 UUID
cur.execute("SELECT id, name, is_market_primary, account_type, is_trading_enabled FROM mt5_accounts WHERE account_id='1610098464'")
row = cur.fetchone()
print(">>> 1610098464 映射:", dict(row) if row else "未找到")

# strategy_configs 是否有独立行
cur.execute("SELECT account_id, config_json FROM strategy_configs")
print("\n=== strategy_configs 行数:", cur.fetchall().__len__(), "===")
con.close()
