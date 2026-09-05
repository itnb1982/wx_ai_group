"""添加 terminal_path 列到已有数据库"""
import sqlite3
import os

DB_PATH = os.path.expanduser("~/.wanxiangai/wanxiangai.db")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# 检查列是否已存在
cursor.execute("PRAGMA table_info(mt5_accounts)")
columns = [col[1] for col in cursor.fetchall()]

if "terminal_path" not in columns:
    cursor.execute("ALTER TABLE mt5_accounts ADD COLUMN terminal_path VARCHAR(500) DEFAULT '';")
    conn.commit()
    print("✅ terminal_path 列已添加")
else:
    print("⚠️ terminal_path 列已存在，跳过")

conn.close()
