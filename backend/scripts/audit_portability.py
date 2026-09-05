"""
可移植性审计 —— 回答一个问题：把整个目录拷到另一台干净的 Windows 电脑上，
它还能不能装起来、跑起来？

╔══════════════════════════════════════════════════════════════════════╗
║ 为什么要写成脚本而不是"人工检查一遍"                                  ║
║                                                                      ║
║ 可移植性问题有个恶劣特性：**在开发机上永远看不见**。                  ║
║   - 硬编码 F:/WanxiangAI 在开发机上是对的                            ║
║   - 漏登记的 requests 在开发机上早就装好了                           ║
║   - 绝对路径的解释器在开发机上确实存在                                ║
║ 所有这些问题都要等到客户那台机器上才爆炸，而那时你不在现场。          ║
║                                                                      ║
║ 人工检查会遗漏，因为你的眼睛已经习惯了这些路径。脚本不会。            ║
╚══════════════════════════════════════════════════════════════════════╝

检查项：
  A. 硬编码绝对路径（盘符开头 / 用户目录 / 开发机专属）
  B. 依赖完整性（代码 import 的三方包 vs requirements 声明）
  C. 交付物污染（不该拷贝的东西：.venv / __pycache__ / 数据库 / 日志）
  D. WorkBuddy 运行时残留（客户机上不存在的沙箱专属路径）
  E. 脚本编码（中文 Windows 的 BOM 陷阱）
  F. 配置文件里的绝对路径（.env —— A 段扫不到的盲区）
  G. 首次部署链完整性（没有它，装完依赖也是"起不来、登不进"）

用法：
    python scripts/audit_portability.py            # 全量
    python scripts/audit_portability.py --strict   # 有问题时退出码非 0（供 CI 用）
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent

# ── 扫描范围 ───────────────────────────────────────────────────────
# 只扫真正会被交付并在运行期执行的代码。
# audit_* / 一次性迁移脚本 / 测试 不在运行路径上，
# 它们里面出现开发机路径不影响客户部署，报出来只会淹没真问题。
PROD_DIRS = [BACKEND / "app", BACKEND / "runtime_paths.py", BACKEND / "supervisor.py"]
SKIP_DIR_NAMES = {
    "__pycache__", ".git", "node_modules", ".venv", "venv", "dist",
    "legacy_migrations", "tests", ".ptmp", "backups",
}


def _iter_py(paths: list[Path]):
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in SKIP_DIR_NAMES or part.startswith(".ptmp")
                       for part in f.parts):
                    continue
                yield f


def _strip_comments(src: str) -> str:
    """去掉注释与文档字符串。

    注释里出现 "F:/WanxiangAI" 通常是在**解释我们为什么不再用它**
    （就像本次修复留下的那些说明）。把它们当违规报出来，
    会让审计输出充满噪音，真问题反而被埋掉。
    """
    out = []
    try:
        tree = ast.parse(src)
        doc_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    doc_lines.add(ln)
    except SyntaxError:
        doc_lines = set()
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines:
            out.append("")
            continue
        s = line.split("#", 1)[0] if "#" in line else line
        out.append(s)
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════
#  A. 硬编码绝对路径
# ═══════════════════════════════════════════════════════════════════
# 允许清单：这些绝对路径是「配置」而非「硬编码」——
# MT5 终端扫描目标本来就该是本机路径，客户改配置即可。
ABS_ALLOW = (
    re.compile(r"[Cc]:[\\/]Program Files"),        # 系统标准位置
    re.compile(r"mt5_discover"),                    # MT5 终端扫描（设计如此）
)
ABS_PATTERNS = [
    (re.compile(r"['\"][A-Za-z]:[\\/]{1,2}[^'\"]{2,}['\"]"), "绝对盘符路径"),
    (re.compile(r"[Cc]:[\\/]{1,2}Users[\\/]{1,2}\w+"), "开发机用户目录"),
]


def audit_abs_paths() -> list[str]:
    hits = []
    for f in _iter_py(PROD_DIRS):
        if "mt5_discover" in f.name:
            continue  # 扫描盘符是该模块的正当职责
        src = _strip_comments(f.read_text(encoding="utf-8", errors="replace"))
        for i, line in enumerate(src.splitlines(), 1):
            for pat, kind in ABS_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                if any(a.search(line) for a in ABS_ALLOW):
                    continue
                rel = f.relative_to(ROOT)
                hits.append(f"[{kind}] {rel}:{i}  {m.group(0)[:70]}")
    return hits


# ═══════════════════════════════════════════════════════════════════
#  B. 依赖完整性
# ═══════════════════════════════════════════════════════════════════
# import 名 → pip 包名（两者不一致的情况）
IMPORT_TO_PKG = {
    "jwt": "PyJWT", "dotenv": "python-dotenv", "yaml": "PyYAML",
    "MetaTrader5": "MetaTrader5", "dateutil": "python-dateutil",
    "sklearn": "scikit-learn", "PIL": "Pillow", "cv2": "opencv-python",
    "multipart": "python-multipart", "jose": "python-jose",
    "chronos": "chronos-forecasting", "google": "google-generativeai",
}
# 标准库（3.11+）。不用 sys.stdlib_module_names 是为了让本脚本
# 在更老的解释器上也能跑——审计工具本身不该挑运行环境。
STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "typing_extensions", "__future__",
}


def _top_level_imports(f: Path) -> set[str]:
    names = set()
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:      # 相对导入，是自己人
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _declared_packages() -> set[str]:
    pkgs = set()
    for name in ("requirements.txt", "requirements-optional.txt", "requirements-dev.txt"):
        p = BACKEND / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            pkg = re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip()
            if pkg:
                pkgs.add(pkg.lower().replace("_", "-"))
    return pkgs


def audit_dependencies() -> list[str]:
    declared = _declared_packages()
    # 项目自身的顶层模块（同目录可直接 import 的），不是三方依赖
    local = {p.stem for p in BACKEND.glob("*.py")} | {
        d.name for d in BACKEND.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    }
    missing: dict[str, set[str]] = {}
    for f in _iter_py(PROD_DIRS):
        for mod in _top_level_imports(f):
            if mod in STDLIB or mod in local or mod.startswith("_"):
                continue
            pkg = IMPORT_TO_PKG.get(mod, mod).lower().replace("_", "-")
            if pkg not in declared:
                missing.setdefault(f"{mod} (→ pip 包 {IMPORT_TO_PKG.get(mod, mod)})",
                                   set()).add(str(f.relative_to(ROOT)))
    return [f"[未声明依赖] {k}  ← {sorted(v)[0]}" + (f" 等 {len(v)} 处" if len(v) > 1 else "")
            for k, v in sorted(missing.items())]


# ═══════════════════════════════════════════════════════════════════
#  C. 交付物污染
# ═══════════════════════════════════════════════════════════════════
def audit_delivery() -> list[str]:
    """检查会被误拷、且拷过去必然出问题的东西。"""
    notes = []
    venv = ROOT / ".venv"
    if venv.exists():
        cfg = venv / "pyvenv.cfg"
        home = ""
        if cfg.exists():
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("home"):
                    home = line.split("=", 1)[-1].strip()
        notes.append(
            "[交付提醒] .venv 存在且不可随目录拷贝 —— "
            f"pyvenv.cfg 的 home 指向创建机的 {home or '<未知>'}，"
            "换机后解释器转发器立即失效。交付前必须排除 .venv，由 bootstrap.bat 现场重建。"
        )
    if not (ROOT / "bootstrap.bat").exists():
        notes.append("[缺失] 根目录没有 bootstrap.bat —— 新机器无法一键建环境")
    db = BACKEND / "data" / "wx_prod.dat"
    if db.exists():
        mb = db.stat().st_size / 1024 / 1024
        notes.append(
            f"[交付提醒] 生产库 backend/data/wx_prod.dat 存在（{mb:.1f} MB）—— "
            "交付给新客户前必须清空或替换，否则会把当前客户的账号与交易记录一并送出去（数据泄露）"
        )
    return notes


# ═══════════════════════════════════════════════════════════════════
#  D. WorkBuddy / 开发沙箱残留
# ═══════════════════════════════════════════════════════════════════
WB_PAT = re.compile(r"WorkBuddy|workbuddy|\.codebuddy|CODEBUDDY", re.I)


def audit_workbuddy() -> list[str]:
    hits = []
    scan = list(PROD_DIRS) + [
        p for p in ROOT.glob("*.bat")
    ] + [p for p in ROOT.glob("*.ps1")] + [p for p in BACKEND.glob("*.py")]
    seen = set()
    for f in scan:
        if not f.is_file() or f in seen:
            continue
        seen.add(f)
        if f.suffix in (".bat", ".ps1", ".py"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(txt.splitlines(), 1):
                if WB_PAT.search(line) and not line.strip().startswith(("#", "::", "REM", "//")):
                    hits.append(f"[沙箱残留] {f.relative_to(ROOT)}:{i}  {line.strip()[:80]}")
    for d in PROD_DIRS:
        if d.is_dir():
            for f in _iter_py([d]):
                txt = f.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(txt.splitlines(), 1):
                    if WB_PAT.search(line) and not line.strip().startswith("#"):
                        hits.append(f"[沙箱残留] {f.relative_to(ROOT)}:{i}  {line.strip()[:80]}")
    return sorted(set(hits))


# ═══════════════════════════════════════════════════════════════════
#  E. 脚本编码（中文 Windows 专属杀手）
# ═══════════════════════════════════════════════════════════════════
# 2026-08-08 实测踩坑：find_python.ps1 —— 上一轮专门为可移植性写的脚本 ——
# 自己在中文 Windows 上跑不起来。
#
# 原因：PowerShell 5.1（Win10/11 自带的那个）读 .ps1 时，
# 若文件没有 BOM，就按**系统 ANSI 代码页**解码，中文环境即 GBK。
# UTF-8 编码的中文被当 GBK 读 → 字符串里混进乱码字节 → 引号配对错乱
# → 整个脚本报语法错误。而这在英文系统上完全正常，开发时根本发现不了。
#
# 两类文件的规则**正好相反**，很容易记混：
#   .ps1 → 含中文时【必须】有 UTF-8 BOM（PowerShell 靠 BOM 识别 UTF-8）
#   .bat → 【绝不能】有 BOM（cmd 会把 BOM 字节当成命令的一部分，
#           首行 @echo off 直接报错），含中文时靠 `chcp 65001` 切码页
def audit_script_encoding() -> list[str]:
    hits = []
    for f in list(ROOT.rglob("*.ps1")) + list(ROOT.rglob("*.bat")):
        if any(p in SKIP_DIR_NAMES or p.startswith(".ptmp") for p in f.parts):
            continue
        raw = f.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            txt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            txt = raw.decode("gbk", errors="replace")
        cjk = any("\u4e00" <= c <= "\u9fff" for c in txt)
        rel = f.relative_to(ROOT)
        if f.suffix.lower() == ".ps1":
            if cjk and not bom:
                hits.append(
                    f"[编码] {rel}  含中文但缺 UTF-8 BOM —— "
                    f"中文 Windows 的 PowerShell 5.1 会按 GBK 解码并报语法错误"
                )
        else:  # .bat
            if bom:
                hits.append(f"[编码] {rel}  .bat 带了 BOM —— cmd 会因此在首行报错")
            elif cjk and "chcp" not in txt.lower():
                is_utf8 = True
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    is_utf8 = False
                if is_utf8:
                    hits.append(f"[编码] {rel}  UTF-8 中文但没有 chcp 65001 —— 控制台会显示乱码")
    return hits


# ═══════════════════════════════════════════════════════════════════
#  F. 配置文件里的绝对路径
# ═══════════════════════════════════════════════════════════════════
# 2026-08-08 补：A 段只扫 .py，结果漏掉了最致命的一处 —— `.env`。
#
# 当时 .env 里写着：
#     DATA_DIR=F:/WanxiangAI/data
#     DATABASE_URL=sqlite:///F:/WanxiangAI/backend/data/wx_prod.dat
# 代码层的硬编码全清干净了，审计 A~E 全绿，但只要客户机没有 F 盘，
# 后端连不上库，照样开箱即废。配置文件恰恰是最容易被"顺手写死"的地方，
# 因为它不参与编译、没有 lint、改起来毫无阻力。
#
# 规则：.env 类文件的**值**里不允许出现绝对盘符。注释行不算
# （注释里写"不要写 F:/..."是正当的说明）。
# (?<![A-Za-z]) 很关键：没有它，"https://..." 里的 "s:/" 会被当成盘符误报。
# 盘符字母的左边只可能是行首、空格、引号或斜杠（如 sqlite:///F:/...），绝不会是字母。
_ENV_ABS = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*(?<![A-Za-z])[A-Za-z]:[\\/].*)$"
)


def audit_config_abs_paths() -> list[str]:
    hits = []
    candidates = [BACKEND / ".env", BACKEND / ".env.example"]
    candidates += [p for p in ROOT.glob("*.env")]
    candidates += [p for p in ROOT.glob(".env*")]
    seen = set()
    for f in candidates:
        if not f.is_file() or f in seen:
            continue
        seen.add(f)
        for i, line in enumerate(
            f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            m = _ENV_ABS.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            hits.append(
                f"[配置绝对路径] {f.relative_to(ROOT)}:{i}  {key}={val[:60]} "
                f"—— 客户机不一定有这个盘，应留空由代码按项目位置推导"
            )
    return hits


# ═══════════════════════════════════════════════════════════════════
#  G. 首次部署链完整性
# ═══════════════════════════════════════════════════════════════════
# 交付包在客户机上"装完依赖依然用不了"的三个断点，逐一守住：
#   1) .env 不随包分发（含密钥）→ 必须有脚本现场生成，否则 SECRET_KEY 为空、
#      auth.py 启动守卫直接 raise，服务起不来
#   2) 新库里没有任何账号，register 只能建普通用户（is_admin 恒 False）
#      → 必须有种子管理员，否则登不进系统
#   3) 上面两件事必须挂在 bootstrap.bat 里，否则等于没有
def audit_first_deploy() -> list[str]:
    hits = []
    init_py = BACKEND / "scripts" / "init_deployment.py"
    if not init_py.exists():
        hits.append(
            "[部署链断裂] 缺少 backend/scripts/init_deployment.py —— "
            "新机器上没有任何东西会生成 .env、建库、创建管理员账号"
        )
    else:
        src = init_py.read_text(encoding="utf-8", errors="replace")
        for token, why in (
            ("SECRET_KEY", "不生成 SECRET_KEY，后端启动守卫会直接拒绝启动"),
            ("is_admin", "不创建管理员，客户装完也登不进系统"),
        ):
            if token not in src:
                hits.append(f"[部署链残缺] init_deployment.py 未处理 {token} —— {why}")

    bs = ROOT / "bootstrap.bat"
    if bs.exists():
        txt = bs.read_text(encoding="utf-8", errors="replace")
        if "init_deployment" not in txt:
            hits.append(
                "[部署链断裂] bootstrap.bat 没有调用 init_deployment.py —— "
                "客户按文档执行 bootstrap 后系统仍然无法启动"
            )

    ex = BACKEND / ".env.example"
    if not ex.exists():
        hits.append("[部署链断裂] 缺少 backend/.env.example —— 现场无法生成 .env 模板")

    # 随机初始密码会落盘，绝不能进版本库
    gi = ROOT / ".gitignore"
    if gi.exists():
        gitxt = gi.read_text(encoding="utf-8", errors="replace")
        if "首次登录凭据" not in gitxt:
            hits.append(
                "[凭据泄露风险] .gitignore 未忽略「首次登录凭据.txt」—— "
                "初始管理员明文密码可能被提交进版本库"
            )
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="发现问题时返回非 0 退出码")
    args = ap.parse_args()

    sections = [
        ("A. 硬编码绝对路径", audit_abs_paths(), True),
        ("B. 依赖完整性", audit_dependencies(), True),
        ("C. 交付物检查", audit_delivery(), False),   # 提醒性质，不算失败
        ("D. 开发沙箱残留", audit_workbuddy(), True),
        ("E. 脚本编码（中文 Windows）", audit_script_encoding(), True),
        ("F. 配置文件绝对路径（.env）", audit_config_abs_paths(), True),
        ("G. 首次部署链完整性", audit_first_deploy(), True),
    ]

    print("=" * 68)
    print(" 万象Ai 可移植性审计 —— 「拷到别的电脑还能跑吗」")
    print(f" 项目根：{ROOT}")
    print("=" * 68)

    fatal = 0
    for title, items, is_error in sections:
        mark = "✓" if not items else ("✗" if is_error else "!")
        print(f"\n{mark} {title}  （{len(items)} 项）")
        if not items:
            print("    无问题")
        for it in items:
            print(f"    {it}")
        if items and is_error:
            fatal += len(items)

    print("\n" + "=" * 68)
    if fatal:
        print(f"✗ 发现 {fatal} 项会影响换机部署的问题，请修复后再交付")
    else:
        print("✓ 未发现阻断换机部署的问题")
    print("=" * 68)
    return 1 if (fatal and args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
