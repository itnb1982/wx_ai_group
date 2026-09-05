"""
万象Ai 智能交易系统 — 交付包构建
================================================================
【为什么必须用白名单，而不是"排除几个目录"】
项目根目录当前躺着 90 多个开发残留：一次性诊断脚本、40 多个 backend_run_vNN.log、
内部审计报告、调研文档、运行时状态 json、两个残留 .db……
用黑名单去排它们，永远会漏；而漏掉的每一个都可能是事故：

  * backend/.env            —— DeepSeek / 混元的真实 API Key + JWT SECRET_KEY
  * backend/data/*.dat      —— 当前客户的账号、密码哈希、全部交易记录
  * backend/memory_bank.json—— AI 记忆体，含历史成交明细
  * docs/*.html             —— 内部架构与审计报告
  * audit_trades_out.txt    —— 实盘盈亏明细

所以规则反过来：**只有明确列进白名单的东西才能进包**。
新增模块时如果忘了登记，后果是"客户少一个文件"（部署时立刻报错、当场发现），
而不是"客户多拿到我们的密钥"（悄无声息、无法挽回）。两种失败的代价不对称，
白名单选择的是代价更小的那一侧。

【两种模式】
  full     （默认）完整源码，供自己换机部署 / 内部备份
  customer         仅运行必需：不含前端源码、测试、内部文档

【用法】
    python backend/scripts/make_release.py
    python backend/scripts/make_release.py --mode customer
    python backend/scripts/make_release.py --with-models        # 附带 1.2GB Chronos 权重
    python backend/scripts/make_release.py --out D:/发货 --no-zip
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent


# ═══════════════════════════════════════════════════════════════
#  白名单
# ═══════════════════════════════════════════════════════════════
# 运行系统必需 —— 少任何一样客户都跑不起来
CORE_DIRS = [
    "backend/app",
    "backend/alembic",
    "backend/scripts",
    "frontend/dist",          # 预构建前端产物：客户机不需要装 Node
]
CORE_FILES = [
    "VERSION",
    "CHANGELOG.md",
    "bootstrap.bat",          # 一键建环境 + 初始化
    "start_all.bat",          # 一键启动
    ".gitignore",
    "backend/.env.example",   # .env 由 init_deployment.py 现场生成
    "backend/alembic.ini",
    "backend/requirements.txt",
    "backend/requirements-optional.txt",
    "backend/mt5_requirements.txt",
    "backend/runtime_paths.py",
    "backend/supervisor.py",
    "backend/run_guard.py",
    "backend/restart_backend.py",
    "backend/emergency_console.py",
    # 运维脚本（Windows 计划任务 / Defender 排除 / 解释器发现）
    "backend/find_python.ps1",
    "backend/install_autostart.ps1",
    "backend/install_guard.ps1",
    "backend/add_defender_exclusions.ps1",
    # 可选增强：本地 Ollama 模型安装
    "scripts/install_ollama.ps1",
]

# 仅 full 模式：源码与自检能力
DEV_DIRS = [
    "backend/tests",
    "frontend/src",
    "frontend/public",
    "docs",
    "installer",
]
DEV_FILES = [
    "backend/pytest.ini",
    "backend/requirements-dev.txt",
    "backend/wanxiangai.spec",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.js",
    "frontend/index.html",
    "frontend/deploy.py",
    "build.bat",
    "scripts/download_chronos.py",
    "scripts/chronos_verify.py",
    "scripts/chronos_local_verify.py",
]

MODEL_DIRS = ["models"]

# 白名单目录内部仍要剔除的垃圾
EXCLUDE_PATTERNS = [
    "__pycache__", "*.pyc", "*.pyo", "*.pyd",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "*.log", "*.db", "*.dat", "*.sqlite", "*.sqlite3",
    ".env", ".env.*",
    "*.timestamp-*.mjs",        # vite 的临时配置产物
    "node_modules",
    "*.bak", "*.tmp", "*.orig",
    "首次登录凭据.txt",
]
# .env.example 是模板不是机密，必须放行（上面的 .env.* 会误伤它）
EXCLUDE_ALLOW = {".env.example"}


def _excluded(rel: Path) -> bool:
    for part in rel.parts:
        if part in EXCLUDE_ALLOW:
            continue
        for pat in EXCLUDE_PATTERNS:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


# ═══════════════════════════════════════════════════════════════
#  收集与复制
# ═══════════════════════════════════════════════════════════════
def collect(mode: str, with_models: bool) -> tuple[list[Path], list[str]]:
    """返回 (要打包的文件相对路径列表, 缺失项警告)。"""
    dirs = list(CORE_DIRS)
    files = list(CORE_FILES)
    if mode == "full":
        dirs += DEV_DIRS
        files += DEV_FILES
    if with_models:
        dirs += MODEL_DIRS

    picked: list[Path] = []
    warns: list[str] = []

    for d in dirs:
        src = ROOT / d
        if not src.is_dir():
            warns.append(f"目录不存在，已跳过：{d}")
            continue
        for dp, dn, fn in os.walk(src):
            dn[:] = [x for x in dn if not _excluded(Path(x))]
            for f in fn:
                p = Path(dp) / f
                rel = p.relative_to(ROOT)
                if _excluded(rel):
                    continue
                picked.append(rel)

    for f in files:
        src = ROOT / f
        if not src.is_file():
            warns.append(f"文件不存在，已跳过：{f}")
            continue
        picked.append(Path(f))

    return sorted(set(picked)), warns


def copy_tree(rels: list[Path], dest: Path) -> int:
    total = 0
    for rel in rels:
        s = ROOT / rel
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        total += s.stat().st_size
    return total


# ═══════════════════════════════════════════════════════════════
#  打包后自检 —— 最后一道闸门
# ═══════════════════════════════════════════════════════════════
# 机密特征。宁可误报也不能漏报：这里漏一个，泄露的是真金白银的 API 额度，
# 或者客户的交易账号。
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "疑似 API Key（sk- 开头）"),
    (re.compile(r"SECRET_KEY\s*=\s*[0-9a-fA-F]{32,}"), "JWT SECRET_KEY 实值"),
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/](?:WanxiangAI|Users)"), "开发机绝对路径"),
]
SCAN_SUFFIX = {".py", ".bat", ".ps1", ".env", ".example", ".txt", ".json",
               ".js", ".jsx", ".ini", ".cfg", ".md", ".xml", ".yml", ".yaml"}
# 打包产物本来就是压缩过的一整坨，逐字符扫没有意义且极慢
SCAN_SKIP_DIRS = {"dist", "models", "node_modules"}

REQUIRED_IN_PACKAGE = [
    "bootstrap.bat",
    "start_all.bat",
    "backend/.env.example",
    "backend/scripts/init_deployment.py",
    "backend/runtime_paths.py",
    "backend/requirements.txt",
    "frontend/dist/index.html",
    "VERSION",
]
FORBIDDEN_SUFFIX = {".db", ".dat", ".sqlite", ".sqlite3", ".log"}


def _strip_py_docstrings(src: str) -> str:
    """把文档字符串整体置空，保留行号对齐（便于报错定位）。"""
    import ast  # noqa: PLC0415

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                doc_lines.add(ln)
    return "\n".join(
        "" if i in doc_lines else line
        for i, line in enumerate(src.splitlines(), 1)
    )


def verify(dest: Path) -> tuple[list[str], list[str]]:
    """返回 (致命问题, 提示)。致命问题存在时拒绝出包。"""
    fatal: list[str] = []
    notes: list[str] = []

    for req in REQUIRED_IN_PACKAGE:
        if not (dest / req).exists():
            fatal.append(f"[缺失] 交付包里没有 {req} —— 客户无法完成部署")

    for dp, dn, fn in os.walk(dest):
        dn[:] = [x for x in dn if x not in {"__pycache__"}]
        rel_dir = Path(dp).relative_to(dest)
        top = rel_dir.parts[0] if rel_dir.parts else ""
        for f in fn:
            p = Path(dp) / f
            rel = p.relative_to(dest)
            if p.suffix.lower() in FORBIDDEN_SUFFIX:
                fatal.append(f"[数据泄露] 包内含 {rel} —— 运行时数据/日志不得交付")
                continue
            if f == ".env":
                fatal.append(f"[密钥泄露] 包内含 {rel} —— 含真实 API Key 与 SECRET_KEY")
                continue
            if any(part in SCAN_SKIP_DIRS for part in rel.parts):
                continue
            if p.suffix.lower() not in SCAN_SUFFIX:
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            # .py 要先剥掉文档字符串。本项目大量代码在 docstring 里
            # 解释"过去为什么写死 F:/WanxiangAI、现在为什么不能再写"——
            # 把这些说明当成违规报出来，真问题会被淹没在噪音里。
            if p.suffix.lower() == ".py":
                txt = _strip_py_docstrings(txt)
            for i, line in enumerate(txt.splitlines(), 1):
                stripped = line.strip()
                # 注释里出现"不要写 F:/..."这类说明是正当的
                if stripped.startswith(("#", "//", "REM", "::", "*", "<!--")):
                    continue
                for pat, why in SECRET_PATTERNS:
                    m = pat.search(line)
                    if m:
                        fatal.append(
                            f"[{why}] {rel}:{i}  {m.group(0)[:40]}"
                        )
    return fatal, notes


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════
def human(n: int) -> str:
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.0f} KB"


def read_version() -> str:
    vf = ROOT / "VERSION"
    if vf.exists():
        return vf.read_text(encoding="utf-8").strip() or "0.0.0"
    return "0.0.0"


def main() -> int:
    ap = argparse.ArgumentParser(description="构建万象Ai交付包")
    ap.add_argument("--mode", choices=["full", "customer"], default="full",
                    help="full=完整源码（自己换机）；customer=仅运行必需（发货）")
    ap.add_argument("--out", default=str(ROOT / "release"),
                    help="输出目录，默认 <项目根>/release")
    ap.add_argument("--with-models", action="store_true",
                    help="附带 models/ 本地模型权重（约 1.2GB）")
    ap.add_argument("--no-zip", action="store_true", help="只生成目录，不压缩")
    ap.add_argument("--force", action="store_true", help="自检不通过也强行出包（危险）")
    args = ap.parse_args()

    ver = read_version()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    name = f"WanxiangAI_v{ver}_{args.mode}_{stamp}"
    out_root = Path(args.out)
    dest = out_root / name

    print("=" * 66)
    print("  万象Ai 交付包构建")
    print("=" * 66)
    print(f"  版本   : {ver}")
    print(f"  模式   : {args.mode}" + ("（含模型权重）" if args.with_models else ""))
    print(f"  输出   : {dest}")
    print()

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    print("[1/4] 按白名单收集文件...")
    rels, warns = collect(args.mode, args.with_models)
    for w in warns:
        print(f"      [提示] {w}")
    print(f"      命中 {len(rels)} 个文件")

    print("[2/4] 复制到交付目录...")
    total = copy_tree(rels, dest)
    print(f"      已复制 {human(total)}")

    print("[3/4] 出包前自检（机密扫描 + 完整性校验）...")
    fatal, _ = verify(dest)
    if fatal:
        print()
        for x in fatal[:30]:
            print(f"      {x}")
        if len(fatal) > 30:
            print(f"      ... 另有 {len(fatal)-30} 项")
        print()
        if not args.force:
            shutil.rmtree(dest, ignore_errors=True)
            print("[FAIL] 自检未通过，已删除半成品交付目录。")
            print("       请修复上述问题后重新打包（如确认为误报，可加 --force）。")
            return 1
        print("      [警告] --force 已指定，忽略自检失败继续出包")
    else:
        print("      通过：无密钥、无运行时数据、必需文件齐备")

    # 交付说明
    (dest / "部署说明.txt").write_text(
        f"""万象Ai 智能交易系统 — 部署说明
======================================
版本：v{ver}    打包时间：{datetime.now():%Y-%m-%d %H:%M}
模式：{args.mode}{"（含本地模型权重）" if args.with_models else ""}

【部署步骤】
  1. 把整个目录拷到目标电脑任意位置（路径不限，不要求特定盘符）
  2. 确认已安装 Python 3.11 或更高版本，安装时勾选 "Add Python to PATH"
  3. 双击 bootstrap.bat，等待自动完成：
       建虚拟环境 -> 装依赖 -> 生成配置 -> 建数据库 -> 创建管理员账号
  4. 记下屏幕上显示的登录邮箱与初始密码
     （同时写入本目录下的「首次登录凭据.txt」）
  5. 双击 start_all.bat 启动，浏览器打开 http://127.0.0.1:8080

【必须手工填写的配置】
  编辑 backend\\.env，填入你自己的模型 API Key：
      DEEPSEEK_API_KEY=sk-...
      HUNYUAN_API_KEY=sk-...
  不填也能启动，但 AI 决策不可用。

【本地模型（可选，不装也能正常交易）】
  * Chronos-2 时序模型：把 models\\ 目录一并拷过来即可{"（本包已包含）" if args.with_models else "（本包未包含，需单独拷贝）"}
  * Qwen3-8B 本地大模型：安装 Ollama 后执行 ollama pull qwen3:8b

【常见问题】
  * 提示 SECRET_KEY 未配置 -> 说明 bootstrap.bat 的第 5 步没跑成功，
    单独执行：.venv\\Scripts\\python.exe backend\\scripts\\init_deployment.py
  * 数据库报 readonly -> 给项目目录加杀毒软件排除：
    管理员 PowerShell 执行 backend\\add_defender_exclusions.ps1
  * 前端页面是旧的 -> 浏览器按 Ctrl+Shift+R 强制刷新
""",
        encoding="utf-8",
    )

    print("[4/4] 打包...")
    if args.no_zip:
        print(f"      已跳过压缩（--no-zip）")
        zip_path = None
    else:
        zip_path = out_root / f"{name}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for dp, _, fn in os.walk(dest):
                for f in fn:
                    p = Path(dp) / f
                    z.write(p, p.relative_to(dest.parent))
        print(f"      {zip_path.name}  {human(zip_path.stat().st_size)}")

    print()
    print("=" * 66)
    print("  完成")
    print("=" * 66)
    print(f"  目录 : {dest}")
    if zip_path:
        print(f"  压缩 : {zip_path}")
    if not args.with_models:
        print()
        print("  提示：未附带 models/（约 1.2GB 的 Chronos-2 权重）。")
        print("        不带也能正常交易，本地时序增强会自动降级；")
        print("        需要时把 models/ 目录整体拷到交付目录同级即可。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
