"""
前端构建 + 部署校验一体脚本（V6 Phase 7.1 收尾）

╔══════════════════════════════════════════════════════════════════════╗
║ 这个脚本存在的理由（一段血泪史）：                                     ║
║                                                                      ║
║   历史做法是 `vite build` 输出到 dist，再手工拷成 dist_vN，            ║
║   再改 backend/.env 的 FRONTEND_DIST_DIR，再重启后端。                 ║
║   四步里漏任何一步，结果都是「代码明明改了，页面纹丝不动」，            ║
║   然后花半小时怀疑是缓存、是 React、是后端——其实是没部署。             ║
║   最终积出 28 个 dist_vN 目录（已归档到 backups/dist_archive/）。      ║
║                                                                      ║
║   现在：build 直接写 dist，后端直接读 dist。构建即部署，一步到位。      ║
╚══════════════════════════════════════════════════════════════════════╝

用法：
    python deploy.py            # 构建 + 清理孤儿产物 + 校验
    python deploy.py --no-build # 只清理 + 校验（用于排查现网产物）

为什么不用 vite 的 emptyOutDir 自动清理：
    Windows 上 Defender / uvicorn 可能正持有 dist 里的文件句柄，
    vite 清空目录时会直接抛 EBUSY 让整个构建失败。
    改成"构建完再按 index.html 的实际引用做减法"，
    删不掉的文件只是留着占点空间，绝不会让构建挂掉。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
INDEX = DIST / "index.html"

# 引用形如 /assets/index-XXXX.js 或 assets/index-XXXX.css
_REF = re.compile(r'(?:src|href)="/?((?:assets/)[^"]+)"')


def _node_exe() -> str:
    """
    找 node。统一走 runtime_paths 的发现逻辑（环境变量 → PATH → 常见安装位置），
    不再保留任何开发机专属的绝对路径回退——那种回退换台机器就是死路，
    而且会把"没装 Node"这种一句话能说清的问题伪装成诡异的构建失败。
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT.parent / "backend"))
    try:
        from runtime_paths import find_node
        return find_node(required=False) or "node"
    except Exception:
        from shutil import which
        return which("node") or "node"


def run_build() -> bool:
    """调 vite 构建。用 node 直接跑 vite.js，绕开 npm/npx 的 shell 差异。"""
    vite = ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        print(f"[构建] 找不到 vite: {vite}")
        return False
    print("[构建] vite build ...")
    r = subprocess.run(
        [_node_exe(), str(vite), "build"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        errors="replace",  # Windows 中文控制台按 GBK 解码会炸，强制替换
    )
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip()[-1500:])
    if r.returncode != 0:
        print(f"[构建] ✗ 失败 exit={r.returncode}")
        return False
    print("[构建] ✓ 完成")
    return True


def referenced_assets() -> set[str]:
    if not INDEX.exists():
        return set()
    html = INDEX.read_text(encoding="utf-8", errors="replace")
    # 剥离 ?v= 版本戳，只取真实磁盘路径，避免误判「缺失被引用产物」
    return {m.replace("\\", "/").split("?")[0] for m in _REF.findall(html)}


def prune(refs: set[str]) -> tuple[int, int]:
    """删除 dist/assets 下没有被 index.html 引用的文件。"""
    assets = DIST / "assets"
    if not assets.exists():
        return 0, 0
    removed = failed = 0
    for f in assets.iterdir():
        if not f.is_file():
            continue
        rel = f"assets/{f.name}"
        if rel in refs:
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            # 被占用就留着——占空间不致命，构建失败才致命
            failed += 1
    return removed, failed


def verify(refs: set[str]) -> bool:
    """index.html 引用的每个文件都必须真实存在，否则页面会白屏。"""
    ok = True
    for rel in sorted(refs):
        if not (DIST / rel).exists():
            print(f"[校验] ✗ 缺失被引用的产物: {rel}")
            ok = False
    return ok


# 给 /assets/index-xxxx.{js,css} 引用追加 ?v=<构建时间戳>，强制浏览器视为新资源。
# 放在 prune/verify 之后注入，避免带 query 的引用干扰「引用完整性」校验。
_STAMP_RE = re.compile(r'(/assets/index-[A-Za-z0-9_-]+\.)(?:js|css)(?=[\"\'])')


def stamp_index_html() -> bool:
    if not INDEX.exists():
        return False
    html = INDEX.read_text(encoding="utf-8", errors="replace")
    version = str(int(INDEX.stat().st_mtime))
    new = _STAMP_RE.sub(lambda m: f"{m.group(1)}js?v={version}" if m.group(0).endswith("js") else f"{m.group(1)}css?v={version}", html)
    if new != html:
        INDEX.write_text(new, encoding="utf-8")
        print(f"[版本戳] 已给 index.html 资源引用注入 ?v={version}（破浏览器顽固缓存）")
    return True


def main() -> int:
    no_build = "--no-build" in sys.argv

    if not no_build and not run_build():
        return 1

    refs = referenced_assets()
    if not refs:
        print("[校验] ✗ index.html 里没解析到任何 assets 引用，构建可能不完整")
        return 1

    removed, failed = prune(refs)
    print(f"[清理] 删除孤儿产物 {removed} 个" + (f"，{failed} 个被占用跳过" if failed else ""))

    if not verify(refs):
        return 1

    stamp_index_html()

    print("[校验] ✓ 引用完整：" + "  ".join(sorted(refs)))
    print()
    print("─" * 62)
    print("  已部署到 frontend/dist（backend/.env: FRONTEND_DIST_DIR=dist）")
    print("  后端 StaticFiles 按请求读盘，无需重启即可生效。")
    print("  ★ 请在浏览器按 Ctrl+Shift+R 硬刷，否则看到的仍是旧 bundle。")
    print("─" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
