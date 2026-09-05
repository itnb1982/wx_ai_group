"""Alembic 运行环境 —— 万象Ai 数据库结构变更的唯一入口。

设计要点（V6 工程安全网 12.5）：
  1. 库地址【只】从 app.config.settings 解析，绝不在 ini 里写死 —— 杜绝
     「开发机迁移误打生产库」。
  2. 升级前【自动备份】生产库文件（SQLite 单文件，成本极低），失败即中止。
     金融系统的结构变更必须可秒级回滚。
  3. render_as_batch=True —— SQLite 不支持 ALTER COLUMN/DROP COLUMN，
     必须走 batch 模式（建影子表→拷数据→改名）。这正是历史上
     「加字段只能手写 ALTER、还经常漏」的根因。
  4. compare_type=True（捕获类型漂移），但 compare_server_default=False。
     实测：打开 server_default 比对会对 strategy_configs 等表刷出 40+ 条误报
     —— 模型侧用 Python 的 default=（写入时填值），生产库侧是历史 ALTER 留下的
     SQL DEFAULT，二者语义等价却被判为差异。在 SQLite 的 batch 模式下，这些
     假差异会被翻译成「建影子表→全量拷数据→改名」的整表重建，对 30MB+ 的
     生产库是实打实的风险。真正的结构漂移（缺列/多列/类型变）不依赖它，
     由 compare_type 与列比对覆盖（已用独立 schema diff 脚本验证零漏报）。
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── 让 alembic 能 import 到 app 包（backend/ 加入 sys.path）──
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.config import settings          # noqa: E402
from app.database import Base            # noqa: E402
import app.models                        # noqa: F401,E402  必须 import 才能注册全部表

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ── 注入真实库地址（唯一来源）──
_DB_URL = settings.get_database_url()
config.set_main_option("sqlalchemy.url", _DB_URL)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """autogenerate 对象过滤器：非业务表一律忽略。

    生产库里存在【不由 ORM 管理】的运行时表：
      · _wprobe —— database._raw_creator 每次建连都要用的「主库真实可写性探测表」
        （Windows 下 sqlite 会静默回退只读，靠它兜底）。若不过滤，autogenerate
        会因为模型里没有它而生成 op.drop_table('_wprobe')，迁移一跑就把活跃
        探测表删了。
      · __diag_* / _t* / _w* —— 历史排障留下的一次性诊断表。
    规则：下划线开头的表全部视为运行时/诊断表，不纳入迁移管理。
    """
    if type_ == "table" and name and name.startswith("_"):
        return False
    return True


def _db_file_path(url: str) -> Path | None:
    """从 sqlite URL 提取物理文件路径；非 sqlite 返回 None。"""
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///")[-1] if "///" in url else url.replace("sqlite:", "")
    raw = raw.split("?")[0].strip()
    return Path(raw) if raw and raw != ":memory:" else None


def _is_mutating_command() -> bool:
    """当前 alembic 命令是否会真正写库。

    只读命令（current/history/heads/show、以及 revision --autogenerate）也会走
    run_migrations_online() 建连接。若不区分，每次查状态都要复制 30MB+ 生产库，
    既慢又把备份目录塞满真正需要的那份被淹没。
    """
    cmd_opts = getattr(config, "cmd_opts", None)
    cmd = getattr(cmd_opts, "cmd", None) if cmd_opts is not None else None
    name = getattr(cmd[0], "__name__", "") if cmd else ""
    return name in ("upgrade", "downgrade", "stamp")


def _backup_before_migrate() -> None:
    """升级前自动备份数据库文件。备份失败 → 直接抛错中止迁移。

    宁可不迁移，也不允许在没有退路的情况下改生产库结构。
    可用 WX_SKIP_MIGRATE_BACKUP=1 跳过（仅限一次性建空库/CI 场景）。
    """
    if not _is_mutating_command():
        return
    if os.getenv("WX_SKIP_MIGRATE_BACKUP") == "1":
        print("[alembic] 已按 WX_SKIP_MIGRATE_BACKUP=1 跳过备份")
        return

    db_file = _db_file_path(_DB_URL)
    if db_file is None or not db_file.exists():
        print("[alembic] 目标库尚不存在（首次建库），跳过备份")
        return

    backup_dir = _BACKEND_DIR / "backups" / "schema_migrations"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"{db_file.stem}_{stamp}{db_file.suffix}"

    shutil.copy2(db_file, dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"[alembic] 迁移前已备份: {dst}  ({size_mb:.2f} MB)")

    if dst.stat().st_size != db_file.stat().st_size:
        raise RuntimeError(f"备份体积与源库不一致，中止迁移。src={db_file} dst={dst}")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。用于人工审阅将要执行的 DDL。"""
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=False,   # 见文件头注释 4
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：真正对库执行迁移（先备份）。"""
    _backup_before_migrate()

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,          # SQLite 改字段必需
            compare_type=True,
            compare_server_default=False,   # 见文件头注释 4
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
