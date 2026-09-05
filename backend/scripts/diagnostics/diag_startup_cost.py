"""启动耗时归因实验（一次性诊断脚本，非生产代码）。

目的：查清生产重启时 init_db 耗时从 ~5s 暴涨到 198s 的真实原因。
候选假设：
  H1  33MB 库文件本身让 create_all 变慢
  H2  _raw_creator 的 _wprobe 主库写探测（每连接一次 CREATE/INSERT/DELETE/COMMIT）
      在 DELETE journal 模式下每次都要创建 33MB 库的 -journal 文件，
      被 Defender 实时扫描盯上 -> 高频撞锁 -> readonly
  H3  create_all 的表反射（16 张表，含 9 张诊断垃圾表）慢

方法：分段计时，各跑 N 次取均值，不改动任何生产代码。
"""
import os
import sqlite3
import statistics
import sys
import time
from urllib.parse import quote as _urlquote

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wx_prod.dat")
URI = "file:" + _urlquote(DB) + "?mode=rwc&uri=true"


def _fmt(name, samples):
    if not samples:
        print(f"  {name:<44} (无样本)")
        return
    print(
        f"  {name:<44} 均值 {statistics.mean(samples) * 1000:8.1f}ms  "
        f"最大 {max(samples) * 1000:8.1f}ms  n={len(samples)}"
    )


def bench_connect_only(n=10):
    """只建连接 + PRAGMA，不做写探测。"""
    out = []
    for _ in range(n):
        t0 = time.time()
        c = sqlite3.connect(URI, uri=True, timeout=15, check_same_thread=False)
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        c.close()
        out.append(time.time() - t0)
    return out


def bench_connect_with_wprobe(n=10):
    """完整复刻生产 _raw_creator：连接 + PRAGMA + 主库写探测 + commit。"""
    out = []
    for _ in range(n):
        t0 = time.time()
        c = sqlite3.connect(URI, uri=True, timeout=15, check_same_thread=False)
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("CREATE TABLE IF NOT EXISTS _wprobe(id INTEGER PRIMARY KEY,v INTEGER)")
        c.execute("INSERT INTO _wprobe(v) VALUES (1)")
        c.execute("DELETE FROM _wprobe WHERE v=1")
        c.commit()
        c.close()
        out.append(time.time() - t0)
    return out


def bench_readonly_probe(n=10):
    """候选替代方案：只读校验（不写主库），用 PRAGMA quick_check 之外的最轻量方式。"""
    out = []
    for _ in range(n):
        t0 = time.time()
        c = sqlite3.connect(URI, uri=True, timeout=15, check_same_thread=False)
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=NORMAL")
        # 只读校验：能打开且能读 schema 即可；mode=rwc 已保证写权限
        c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        c.close()
        out.append(time.time() - t0)
    return out


def bench_create_all(n=3):
    """测 SQLAlchemy Base.metadata.create_all 的真实耗时。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app.database import Base, WriteSession  # noqa: E402
    import app.models  # noqa: F401,E402  确保所有 ORM 注册

    out = []
    for _ in range(n):
        t0 = time.time()
        db = WriteSession()
        Base.metadata.create_all(bind=db.bind)
        db.commit()
        db.close()
        out.append(time.time() - t0)
    return out


def journal_watch():
    """观察写事务期间是否产生 -journal 文件及其体积。"""
    jf = DB + "-journal"
    c = sqlite3.connect(URI, uri=True, timeout=15, check_same_thread=False)
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("BEGIN IMMEDIATE")
    c.execute("CREATE TABLE IF NOT EXISTS _wprobe(id INTEGER PRIMARY KEY,v INTEGER)")
    c.execute("INSERT INTO _wprobe(v) VALUES (1)")
    exists = os.path.exists(jf)
    size = os.path.getsize(jf) if exists else 0
    c.rollback()
    c.close()
    return exists, size


if __name__ == "__main__":
    print(f"DB = {DB}")
    print(f"体积 = {os.path.getsize(DB) / 1024 / 1024:.1f} MB")
    print()

    ex, sz = journal_watch()
    print(f"[journal] 写事务期间产生 -journal 文件: {ex}, 体积 {sz / 1024:.1f} KB")
    print()

    print("[连接建立耗时对比]")
    _fmt("A. 纯连接 + PRAGMA（无写）", bench_connect_only(10))
    _fmt("B. 连接 + PRAGMA + 只读校验", bench_readonly_probe(10))
    _fmt("C. 连接 + PRAGMA + _wprobe 主库写(生产现状)", bench_connect_with_wprobe(10))
    print()

    print("[建表耗时]")
    _fmt("D. Base.metadata.create_all", bench_create_all(3))
