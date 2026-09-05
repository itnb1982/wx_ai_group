"""回归比对台 · 旧实现加载器（V6 §12.3）。

原理：用 `git show <ref>:<path>` 把重构前的源码取出来，动态加载成一个
独立模块，与当前实现跑同一批输入，逐字段对拍。

这样"重构没改变行为"不再靠人眼看 diff，而是可执行、可 CI 的断言。
若目标 ref 不含该文件（例如全新文件），返回 None，由测试自行 skip。

★ 2026-08-08 审计修复（安全网静默失效）
   原实现直接调 `["git", ...]` 并把所有异常吞成 `return None`。在 PATH 中
   没有 git 的 shell（本机 PowerShell 就是）下必然 FileNotFoundError，
   结果 **620 个手数等价性用例全部静默 skip** —— 手数直接等于钱，这层
   "改代码不许改变任何一笔手数"的安全网空转了很久都没人发现。
   现在：① 主动定位 git；② 记录真实失败原因供 skip reason 展示，
   让"没跑"这件事在测试输出里刺眼可见，而不是伪装成一切正常。
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 最近一次加载失败的原因。测试把它拼进 skip reason，杜绝"静默 skip"。
last_failure: str = ""


def _find_git() -> Optional[str]:
    """定位 git 可执行文件。

    查找顺序（**全部通用**，刻意不绑定任何特定 IDE / 工具链的私有目录，
    以满足"整目录拷到别的 Windows 机器就能跑"的可移植性要求）：
      1. 环境变量 WX_GIT_EXE —— 部署方显式指定，优先级最高
      2. PATH（shutil.which）
      3. Git for Windows / Linux 的标准安装位置
    """
    env_git = os.environ.get("WX_GIT_EXE", "").strip()
    if env_git and Path(env_git).exists():
        return env_git

    found = shutil.which("git")
    if found:
        return found

    for cand in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
        "/usr/bin/git",
        "/usr/local/bin/git",
    ):
        if cand and Path(cand).exists():
            return cand
    return None


def load_module_at_ref(git_path: str, ref: str, alias: Optional[str] = None):
    """从 git 某个提交加载模块。

    Args:
        git_path: 仓库内相对路径，如 'backend/app/services/intelligent_sizing.py'
        ref:      git 引用，如 'v1.4.0-baseline' / 'HEAD~1'
        alias:    加载后的模块名（默认自动生成，避免污染 sys.modules）

    Returns:
        模块对象；失败返回 None，失败原因写入模块级 `last_failure`。
    """
    global last_failure
    last_failure = ""

    git_exe = _find_git()
    if not git_exe:
        last_failure = (
            "找不到 git 可执行文件（PATH 中无 git，标准安装位置也没有）。"
            "请把 git 加入 PATH，或设置环境变量 WX_GIT_EXE 指向 git.exe。"
            "注意：这会导致等价性安全网整体空转！"
        )
        return None

    try:
        proc = subprocess.run(
            [git_exe, "show", f"{ref}:{git_path}"],
            cwd=REPO_ROOT, capture_output=True, check=True,
        )
        src = proc.stdout
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace").strip()[:200]
        last_failure = f"git show {ref}:{git_path} 失败：{err}"
        return None
    except OSError as e:
        last_failure = f"调用 git 失败（{git_exe}）：{e}"
        return None

    name = alias or f"_legacy_{uuid.uuid4().hex[:8]}"
    tmp = Path(tempfile.gettempdir()) / f"{name}.py"
    tmp.write_bytes(src)

    spec = importlib.util.spec_from_file_location(name, tmp)
    if spec is None or spec.loader is None:
        last_failure = f"无法为 {tmp} 创建模块 spec"
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 —— 旧代码可能依赖已删除的模块
        sys.modules.pop(name, None)
        last_failure = f"执行旧模块失败：{type(e).__name__}: {e}"
        return None
    return mod
