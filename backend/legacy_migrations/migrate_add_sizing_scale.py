"""迁移：strategy_configs 增加 sizing_scale_mode 列（风控上限按本金自适应）

ALTER 两个库：backend/data/wx_prod.dat + F:/WanxiangAI/data/wx_prod.dat
使用 URI mode=rwc 强制读写（根治 Windows 只读回退），幂等检测已存在列。
"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DBS = [
    os.path.join(HERE, "data", "wx_prod.dat"),
    r"F:\\WanxiangAI\\data\wx_prod.dat",
]
COL = "sizing_scale_mode"
COLDEF = "VARCHAR(8) NOT NULL DEFAULT 'auto'"


def main():
    for db in DBS:
        if not os.path.exists(db):
            print(f"[跳过] 库不存在: {db}")
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=rwc", uri=True)
            cur = c.cursor()
            existing = {r[1] for r in cur.execute("PRAGMA table_info(strategy_configs)")}
            if COL in existing:
                print(f"[已存在] {db} 已有 {COL}")
            else:
                cur.execute(f"ALTER TABLE strategy_configs ADD COLUMN {COL} {COLDEF}")
                c.commit()
                print(f"[OK] {db} 已加列 {COL}")
            c.close()
        except Exception as e:
            print(f"[失败] {db}: {e}")
    print("迁移完成。")


if __name__ == "__main__":
    main()
