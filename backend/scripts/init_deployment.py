"""
万象Ai 智能交易系统 — 首次部署初始化（幂等）
================================================================
【这个脚本存在的理由】
2026-08-08 交付审计发现：把整个项目目录拷到一台干净的 Windows 电脑上，
即便依赖全部装好，系统依然**起不来、也登不进去**。三重断点：

  1) `.env` 被 .gitignore 排除 —— 客户机上根本没有这个文件。
     于是 SECRET_KEY 为空，auth.py 顶部的启动守卫直接
     `raise RuntimeError("SECRET_KEY 未配置：拒绝启动")`，服务当场夭折。

  2) 就算手工补一个 `.env`，里面的 DATA_DIR / DATABASE_URL 曾写死 F 盘绝对路径，
     客户机没有 F 盘，后端连不上库。（该问题已在配置层根治，此处不再依赖 .env 给路径。）

  3) 就算路径全通，数据库里**一个账号都没有**。
     `init_db()` 只建表不建账号；`/api/auth/register` 只能建普通用户
     （`is_admin` 恒为 False，全仓没有任何代码会把它置真）。
     结果：系统跑起来了，但没有任何人能以管理员身份登录。

本脚本把这三件事一次性做完，且**可以反复执行**——
重跑不会覆盖已有配置、不会重置密码、不会动任何已存在的数据。

【使用】
    python backend/scripts/init_deployment.py
    python backend/scripts/init_deployment.py --email you@corp.com --password 'Xxx@123456'
    python backend/scripts/init_deployment.py --reset-password      # 仅重置管理员密码

【设计约束】
  * 步骤 1（生成 .env）必须在 import 任何 app.* 模块**之前**完成。
    因为 `app.config` 在被 import 的瞬间就实例化 Settings 并读取 .env，
    先 import 再写 .env 等于白写。所以这一段只用标准库。
  * 任何一步失败都要说清「哪一步、为什么、怎么修」，不做静默兜底。
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import string
import sys
from pathlib import Path

# 控制台编码兜底：Windows 默认 GBK，遇到无法编码的字符会抛 UnicodeEncodeError
# 把整个脚本崩掉。errors='replace' 保留控制台原编码，只是把个别字符换成 '?'，
# 不至于因为一个装饰性符号让部署失败。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# 本文件位于 <项目根>/backend/scripts/init_deployment.py
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"
ENV_EXAMPLE = BACKEND_DIR / ".env.example"
CRED_FILE = PROJECT_ROOT / "首次登录凭据.txt"

# 让 `import app.xxx` 可用（脚本可能从任意工作目录被调用）
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ═══════════════════════════════════════════════════════════════
#  小工具
# ═══════════════════════════════════════════════════════════════
def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, title: str) -> None:
    say("")
    say(f"[{n}/4] {title}")
    say("-" * 60)


def gen_password(length: int = 16) -> str:
    """生成便于人工转录的强密码：去掉 0/O/1/l/I 这类易混字符。"""
    alphabet = (
        string.ascii_uppercase.replace("O", "").replace("I", "")
        + string.ascii_lowercase.replace("l", "")
        + "23456789"
    )
    core = "".join(secrets.choice(alphabet) for _ in range(length - 2))
    # 保证至少含一个特殊字符与一个数字，满足常见口令强度校验
    return core + secrets.choice("@#$%") + secrets.choice("23456789")


# ═══════════════════════════════════════════════════════════════
#  步骤 1：确保 .env 存在且 SECRET_KEY 已生成
# ═══════════════════════════════════════════════════════════════
def ensure_env() -> tuple[bool, bool]:
    """返回 (是否新建了 .env, 是否新生成了 SECRET_KEY)。仅用标准库。"""
    created = False
    if not ENV_FILE.exists():
        if not ENV_EXAMPLE.exists():
            raise SystemExit(
                f"[FAIL] 既没有 {ENV_FILE.name} 也没有 {ENV_EXAMPLE.name}。\n"
                f"       期望位置：{BACKEND_DIR}\n"
                f"       说明交付包不完整，请重新获取完整项目目录。"
            )
        ENV_FILE.write_text(
            ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        created = True
        say(f"[OK] 已从 .env.example 生成 {ENV_FILE}")
    else:
        say(f"[--] {ENV_FILE.name} 已存在，保留现有配置（不覆盖）")

    text = ENV_FILE.read_text(encoding="utf-8")
    m = re.search(r"^SECRET_KEY\s*=\s*(.*)$", text, flags=re.M)
    current = (m.group(1).strip() if m else "")
    if current:
        say("[--] SECRET_KEY 已配置，保持不变")
        return created, False

    new_key = secrets.token_hex(32)  # 64 位十六进制
    if m:
        text = text[: m.start()] + f"SECRET_KEY={new_key}" + text[m.end():]
    else:
        text = text.rstrip("\n") + f"\n\n# 首次部署自动生成\nSECRET_KEY={new_key}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    say("[OK] 已生成随机 SECRET_KEY（64 位十六进制）")
    say("     注意：更换该值会让所有已签发的登录令牌立即失效。")
    return created, True


# ═══════════════════════════════════════════════════════════════
#  步骤 2：数据库建表 / 迁移
# ═══════════════════════════════════════════════════════════════
def ensure_database() -> str:
    """建表或升级到最新版本，返回数据库文件路径。

    为什么不无脑 `alembic upgrade head`：
    基线迁移 17ac6904264d 只覆盖了 7 张表，而 ORM 现在的表远不止这些
    （迁移链是从一个已存在的库 autogenerate 出来的）。对一个全新的空库
    直接 upgrade，会建出一个**缺表的残库**，故障要等到运行时才暴露。

    所以分两种情况：
      * 全新库（没有 alembic_version 表）→ create_all 建到当前 ORM 状态，
        再 stamp head 打上版本标记（后续增量迁移正常衔接）
      * 已纳管的库              → upgrade head 走正常增量迁移
    """
    from sqlalchemy import inspect  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.database import Base, engine, init_db  # noqa: PLC0415
    import app.models  # noqa: F401,PLC0415  # 触发全部模型注册到 Base.metadata

    url = settings.get_database_url()
    db_file = url.replace("sqlite:///", "").replace("sqlite:", "").strip()
    say(f"     数据库：{db_file}")

    insp = inspect(engine)
    existing = set(insp.get_table_names())
    is_fresh = "alembic_version" not in existing

    if not init_db():
        raise SystemExit(
            "[FAIL] 建表失败。常见原因：\n"
            "       1) 数据库文件被占用（后端/其他进程仍在运行）→ 先停掉服务\n"
            "       2) 目录只读或杀毒软件加锁 → 给项目目录加 Defender 排除\n"
            "       3) 磁盘空间不足"
        )
    after = set(inspect(engine).get_table_names())
    created_tables = sorted(after - existing - {"alembic_version"})
    if created_tables:
        say(f"[OK] 新建 {len(created_tables)} 张表：{', '.join(created_tables[:8])}"
            + (" ..." if len(created_tables) > 8 else ""))
    else:
        say(f"[--] 表结构已是最新（共 {len(after)} 张表），无需新建")

    _run_alembic(fresh=is_fresh)
    return db_file


def _run_alembic(*, fresh: bool) -> None:
    ini = BACKEND_DIR / "alembic.ini"
    if not ini.exists():
        say("[!!] 未找到 alembic.ini，跳过版本标记（不影响首次运行，但后续迁移需手工处理）")
        return
    try:
        from alembic import command  # noqa: PLC0415
        from alembic.config import Config  # noqa: PLC0415
    except ImportError:
        say("[!!] 未安装 alembic，跳过版本标记。请执行：pip install alembic")
        return

    cfg = Config(str(ini))
    # alembic.ini 里的 script_location 通常是相对路径，必须锚定到 backend 目录，
    # 否则从别的工作目录调用本脚本时会找不到迁移目录。
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    try:
        if fresh:
            command.stamp(cfg, "head")
            say("[OK] 全新库：已标记为最新迁移版本（stamp head）")
        else:
            command.upgrade(cfg, "head")
            say("[OK] 已应用增量迁移（upgrade head）")
    except Exception as e:  # noqa: BLE001
        say(f"[!!] Alembic 执行失败：{e}")
        say("     表结构已由 create_all 建好，系统可以运行；"
            "但后续增量迁移前请先手工处理此问题。")


# ═══════════════════════════════════════════════════════════════
#  步骤 3：管理员账号（幂等）
# ═══════════════════════════════════════════════════════════════
def ensure_admin(email: str, password: str | None, reset: bool) -> tuple[str, str | None]:
    """确保存在一个可登录的管理员。返回 (email, 明文密码或 None)。

    幂等语义（很重要，别改）：
      * 账号已存在 → **绝不重置密码**，只补齐 is_admin / is_active。
        重跑部署脚本把客户改过的密码打回默认值，是事故不是便利。
      * 显式 --reset-password 才重置。
    """
    from passlib.context import CryptContext  # noqa: PLC0415

    from app.database import WriteSession  # noqa: PLC0415
    from app.models.user import SubscriptionTier, User  # noqa: PLC0415

    # 必须与 app/routers/auth.py 的方案完全一致，否则哈希对不上，登录必失败
    pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

    db = WriteSession()
    try:
        user = db.query(User).filter(User.email == email).first()
        plain: str | None = None

        if user is None:
            plain = password or gen_password()
            username = _unique_username(db, User, email)
            user = User(
                email=email,
                username=username,
                hashed_password=pwd_context.hash(plain),
                is_active=True,
                is_admin=True,
                subscription_tier=SubscriptionTier.ENTERPRISE,
            )
            db.add(user)
            db.commit()
            say(f"[OK] 已创建管理员账号：{email}（用户名 {username}）")
        else:
            changed = []
            if not user.is_admin:
                user.is_admin = True
                changed.append("提升为管理员")
            if not user.is_active:
                user.is_active = True
                changed.append("重新启用")
            if reset:
                plain = password or gen_password()
                user.hashed_password = pwd_context.hash(plain)
                changed.append("重置密码")
            if changed:
                db.commit()
                say(f"[OK] 账号 {email} 已更新：{'、'.join(changed)}")
            else:
                say(f"[--] 管理员 {email} 已存在且状态正常，未做任何修改")

        total = db.query(User).count()
        admins = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        say(f"     当前用户总数 {total}，其中管理员 {admins}")
        return email, plain
    finally:
        db.close()


def _unique_username(db, User, email: str) -> str:  # noqa: N803
    base = (email.split("@")[0] or "admin")[:80]
    name = base
    i = 1
    while db.query(User).filter(User.username == name).first() is not None:
        i += 1
        name = f"{base}{i}"
    return name


def write_credentials(email: str, plain: str) -> None:
    """把随机生成的密码落盘，避免控制台被刷走后再也找不回来。"""
    CRED_FILE.write_text(
        "万象Ai 智能交易系统 — 首次登录凭据\n"
        "=========================================\n"
        f"登录邮箱：{email}\n"
        f"初始密码：{plain}\n\n"
        "* 该密码为首次部署随机生成，请登录后立即修改。\n"
        "* 修改密码后请删除本文件。\n"
        "* 请勿将本文件随项目目录一起转发给他人。\n",
        encoding="utf-8",
    )
    say(f"[OK] 凭据已写入：{CRED_FILE}")


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="万象Ai 首次部署初始化（可重复执行）"
    )
    ap.add_argument("--email", default=os.environ.get("WX_ADMIN_EMAIL", "admin@wanxiang.ai"),
                    help="管理员登录邮箱（默认 admin@wanxiang.ai）")
    ap.add_argument("--password", default=os.environ.get("WX_ADMIN_PASSWORD"),
                    help="管理员密码；不指定则随机生成并写入「首次登录凭据.txt」")
    ap.add_argument("--reset-password", action="store_true",
                    help="账号已存在时也强制重置密码")
    args = ap.parse_args()

    say("=" * 60)
    say("  万象Ai 智能交易系统 — 首次部署初始化")
    say("=" * 60)
    say(f"  项目根目录：{PROJECT_ROOT}")

    step(1, "环境配置 .env")
    ensure_env()

    # ★ 顺序不可调换：.env 就绪后才能 import app.*（config 在 import 时读 .env）
    step(2, "数据库表结构")
    db_file = ensure_database()

    step(3, "管理员账号")
    email, plain = ensure_admin(args.email, args.password, args.reset_password)
    if plain:
        write_credentials(email, plain)

    step(4, "运行时自检")
    try:
        import runtime_paths  # noqa: PLC0415
        say(runtime_paths.describe())
    except Exception as e:  # noqa: BLE001
        say(f"[!!] 运行时自检跳过：{e}")

    say("")
    say("=" * 60)
    say("  初始化完成")
    say("=" * 60)
    say(f"  数据库    : {db_file}")
    say(f"  登录邮箱  : {email}")
    if plain:
        say(f"  初始密码  : {plain}   <-- 请登录后立即修改")
    else:
        say("  登录密码  : 沿用已有密码（本次未修改）")
    say("")
    say("  下一步：双击 start_all.bat 启动系统，浏览器访问 http://127.0.0.1:8080/")
    say("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        say("")
        say(f"[FAIL] 初始化中断：{type(exc).__name__}: {exc}")
        say("       请把以上完整输出发给技术支持。")
        sys.exit(1)
