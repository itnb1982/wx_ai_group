"""
万象Ai 智能交易系统 — 运行时路径发现（可移植性基石）
=====================================================
【为什么需要这个模块】
商业版要求：整个项目目录拷贝到任意一台 Windows 电脑上都能正常部署运行。
历史上 supervisor / run_guard / start_all.bat / deploy.py 里写死了开发机专属的
解释器绝对路径（形如 C:\\Users\\<某个人>\\...\\python.exe）。这类路径一旦离开
开发机就 100% 失效——进程根本拉不起来，整套系统直接废掉。

【设计原则】
1. **零第三方依赖**：只用标准库。本模块会被 supervisor 这类"比 venv 更早执行"
   的顶层脚本导入，那时任何 pip 包都可能还不存在。
2. **发现而非假设**：按优先级探测候选路径，第一个可用的即采纳。
3. **失败要响亮**：找不到时抛出带修复指引的异常，而不是静默回退到一个
   注定失败的路径——静默失败会让运维排查成本高一个数量级。

【解释器优先级】
  1. 环境变量 WX_PYTHON            —— 运维显式指定，最高优先级，便于特殊部署
  2. <项目根>/.venv                —— 项目自带虚拟环境（推荐的标准部署形态）
  3. <项目根>/venv                 —— 兼容另一种常见命名
  4. sys.executable                —— 当前正在运行本脚本的解释器
  5. PATH 中的 python / python3    —— 系统级安装
  6. Windows py launcher (py -3)   —— 官方安装包默认提供

Node 同理（WX_NODE → PATH → 常见安装位置）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which

# 本文件位于 <项目根>/backend/runtime_paths.py
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

_IS_WIN = os.name == "nt"
# venv 内解释器的相对位置在 Windows 与 POSIX 上不同
_VENV_REL = ("Scripts", "python.exe") if _IS_WIN else ("bin", "python")
_NODE_NAME = "node.exe" if _IS_WIN else "node"


class RuntimeNotFound(RuntimeError):
    """找不到必需的运行时。异常信息里必须带可操作的修复指引。"""


# ═══════════════════════════════════════════════════════════════
#  数据目录
# ═══════════════════════════════════════════════════════════════
def data_dir(*, create: bool = True) -> Path:
    """运行期数据目录（日志、状态文件、临时快照）。

    优先级：环境变量 DATA_DIR > <项目根>/data

    ── 为什么必须有这个函数 ─────────────────────────────────
    2026-08-08 审计发现全项目有 6 处写着
        os.environ.get("DATA_DIR", "F:/WanxiangAI/data")
    的兜底。在开发机上这个兜底永远命中得很自然，于是没人发现问题；
    但客户把目录拷到 D 盘或另一台机器，DATA_DIR 又没设，
    程序就会去写一个**根本不存在的 F 盘路径**——
    轻则日志静默丢失，重则状态文件写不进去导致反转确认逻辑失灵。

    兜底路径写成绝对路径，本质上是把「我的开发机」焊死进了产品。
    正确的兜底永远是「相对于我自己所在的位置」。
    """
    raw = os.environ.get("DATA_DIR")
    d = Path(raw) if raw else (PROJECT_ROOT / "data")
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            # 目录建不出来（只读盘/权限）时不炸——调用方通常是日志初始化，
            # 为了写日志把进程搞崩是本末倒置。
            pass
    return d


def data_path(*parts: str, create_dir: bool = True) -> str:
    """拼一个数据目录下的文件路径，返回字符串（大多数调用方要 str）。"""
    return str(data_dir(create=create_dir).joinpath(*parts))


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir.joinpath(*_VENV_REL)


def project_venv_dir() -> Path | None:
    """返回项目自带 venv 目录（若存在且内含解释器）。"""
    for name in (".venv", "venv"):
        d = PROJECT_ROOT / name
        if _venv_python(d).is_file():
            return d
    return None


def _usable(p: str | os.PathLike | None) -> str | None:
    """校验候选解释器确实存在且可执行。"""
    if not p:
        return None
    path = Path(p)
    return str(path) if path.is_file() else None


def find_python(*, required: bool = True) -> str | None:
    """
    定位可用的 Python 解释器。

    注意 sys.executable 的排序：它排在项目 venv **之后**。
    原因是 supervisor 常被系统级 Python 拉起，但真正装了 torch/chronos 等重依赖的
    是项目 venv；若优先用 sys.executable，会出现"进程能起来但本地模型永远加载失败"
    的隐性降级——这类问题比直接崩溃更难发现。
    """
    # 1. 运维显式指定
    if p := _usable(os.environ.get("WX_PYTHON")):
        return p

    # 2/3. 项目自带 venv
    if (d := project_venv_dir()) is not None:
        if p := _usable(_venv_python(d)):
            return p

    # 4. 当前解释器（排除 embedded / frozen 场景下的非常规值）
    if not getattr(sys, "frozen", False):
        if p := _usable(sys.executable):
            return p

    # 5. PATH
    for name in ("python", "python3"):
        if p := _usable(which(name)):
            return p

    # 6. Windows py launcher —— 询问它 3.x 的真实路径
    if _IS_WIN and which("py"):
        try:
            r = subprocess.run(
                ["py", "-3", "-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=10, errors="replace",
            )
            if r.returncode == 0:
                if p := _usable(r.stdout.strip()):
                    return p
        except Exception:
            # py launcher 存在但不可用不是致命错误，继续往下走到统一的报错出口
            pass

    if required:
        raise RuntimeNotFound(
            "未找到可用的 Python 解释器。\n"
            "修复方式（任选其一）：\n"
            "  1) 在项目根目录运行 bootstrap.bat 自动创建 .venv（推荐）\n"
            "  2) 安装 Python 3.11+ 并勾选 Add to PATH\n"
            "  3) 设置环境变量 WX_PYTHON 指向解释器完整路径"
        )
    return None


def find_node(*, required: bool = False) -> str | None:
    """定位 Node（仅前端构建需要，运行期不需要，故默认不强制）。"""
    if p := _usable(os.environ.get("WX_NODE")):
        return p
    if p := _usable(which("node")):
        return p

    if _IS_WIN:
        # 官方安装包与常见包管理器的默认落点
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / _NODE_NAME,
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs" / _NODE_NAME,
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / _NODE_NAME,
        ]
        for c in candidates:
            if p := _usable(c):
                return p

    if required:
        raise RuntimeNotFound(
            "未找到 Node.js（前端构建需要）。\n"
            "修复方式：安装 Node 18+ 并加入 PATH，或设置环境变量 WX_NODE 指向 node 可执行文件。"
        )
    return None


def describe() -> str:
    """诊断输出，供部署自检脚本调用。"""
    py = find_python(required=False)
    node = find_node(required=False)
    venv = project_venv_dir()
    lines = [
        f"项目根目录 : {PROJECT_ROOT}",
        f"项目 venv  : {venv or '（未创建，建议运行 bootstrap.bat）'}",
        f"Python     : {py or '✗ 未找到'}",
        f"Node       : {node or '（未找到，仅影响前端构建）'}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
