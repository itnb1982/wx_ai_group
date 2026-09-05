"""
迁移脚本：strategy_configs 表新增 3 列（决策质量门控 + 手数本金来源）
- capital_source   VARCHAR(12) DEFAULT 'live'   (账户私有，不继承)
- regime_open_mode VARCHAR(8)  DEFAULT 'soft'   (风险过滤，继承主号)
- short_guard_mode VARCHAR(8)  DEFAULT 'soft'   (风险过滤，继承主号)

使用 URI mode=rwc 强制读写（根治 Windows 下只读回退），并对每个候选库幂等执行。
"""
import sqlite3
from urllib.parse import quote as _urlquote

CANDIDATES = [
    r"F:/WanxiangAI/backend/data/wx_prod.dat",
    r"F:/WanxiangAI/data/wx_prod.dat",
]

NEW_COLS = [
    ("capital_source", "VARCHAR(12)", "live"),
    ("regime_open_mode", "VARCHAR(8)", "soft"),
    ("short_guard_mode", "VARCHAR(8)", "soft"),
]


def migrate_one(path: str):
    import os
    if not os.path.exists(path):
        print(f"[跳过] 不存在: {path}")
        return
    uri = "file:" + _urlquote(path) + "?mode=rwc&uri=true"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "strategy_configs" not in tables:
            print(f"[跳过] 该库无 strategy_configs 表: {path}")
            conn.close()
            return
        cur.execute("PRAGMA table_info(strategy_configs)")
        existing = {row[1] for row in cur.fetchall()}
        added = []
        for col, ctype, default in NEW_COLS:
            if col in existing:
                print(f"  [已存在] {col} @ {path}")
                continue
            cur.execute(f"ALTER TABLE strategy_configs ADD COLUMN {col} {ctype} DEFAULT '{default}'")
            added.append(col)
        conn.commit()
        conn.close()
        if added:
            print(f"[完成] 已新增列 {added} @ {path}")
        else:
            print(f"[完成] 无需改动 @ {path}")
    except Exception as e:
        print(f"[失败] {path}: {e}")


if __name__ == "__main__":
    for p in CANDIDATES:
        migrate_one(p)
