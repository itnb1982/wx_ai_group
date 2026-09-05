import sqlite3, sys

DB = r"F:\\WanxiangAI\\data\wx_prod.dat"

def main():
    try:
        conn = sqlite3.connect(DB, timeout=30)
        cur = conn.cursor()
        # 检查列是否已存在
        cur.execute("PRAGMA table_info(strategy_configs)")
        cols = {row[1] for row in cur.fetchall()}
        if "max_positions" in cols:
            print("OK: max_positions 已存在，无需迁移")
        else:
            cur.execute("ALTER TABLE strategy_configs ADD COLUMN max_positions INTEGER NOT NULL DEFAULT 10")
            conn.commit()
            print("OK: 已添加 max_positions 列 (默认 10)")
        # 验证
        cur.execute("PRAGMA table_info(strategy_configs)")
        cols = {row[1] for row in cur.fetchall()}
        print("当前 strategy_configs 列:", sorted(cols))
        conn.close()
    except Exception as e:
        print("ERR:", repr(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
