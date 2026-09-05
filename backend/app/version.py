"""版本单一权威源读取器（V6 Phase 7.1）。

为什么需要这个模块：
    版本号曾散落三处且互相矛盾（后端 1.0.0 / package.json 0.1.0 / 登录页 v1.0.0），
    客户报障时说不清跑的是哪一版。现在只认项目根 `VERSION` 文件一处，
    后端读它、前端构建注入它、/api/health 吐它、CHANGELOG 记它。

查找优先级（越靠前越权威）：
    1. 环境变量 WX_VERSION      —— 安装器/容器分发时注入，无需带源码树
    2. <项目根>/VERSION          —— 开发与本机部署的正常路径
    3. <backend>/VERSION         —— 打包时只拷 backend 目录的场景
    4. 兜底 "0.0.0-unknown"      —— 绝不允许因读不到版本号而让后端起不来

SemVer 判定口诀（写给未来的自己）：
    客户要重新学 / 重新配 → MAJOR
    客户看到新东西       → MINOR
    客户没感觉但更稳     → PATCH
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

_FALLBACK_VERSION = "0.0.0-unknown"

# backend/app/version.py → parents[0]=app, [1]=backend, [2]=项目根
_APP_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _APP_DIR.parent
_PROJECT_ROOT = _BACKEND_DIR.parent


def _candidate_paths() -> List[Path]:
    """返回 VERSION 文件的候选路径（按优先级）。

    独立成函数是为了让测试能 monkeypatch 成空列表，验证兜底分支不抛异常。
    """
    return [_PROJECT_ROOT / "VERSION", _BACKEND_DIR / "VERSION"]


@lru_cache(maxsize=1)
def get_version() -> str:
    """返回当前系统版本号。永不抛异常。"""
    env_v = (os.environ.get("WX_VERSION") or "").strip()
    if env_v:
        return env_v

    for p in _candidate_paths():
        try:
            if p.is_file():
                raw = p.read_text(encoding="utf-8").strip()
                if raw:
                    return raw
        except Exception:
            # 读文件失败（权限/编码/被杀软锁）不能拖垮启动，继续试下一个
            continue

    return _FALLBACK_VERSION


@lru_cache(maxsize=1)
def get_git_commit() -> str:
    """返回短 commit id，取不到返回 "unknown"。

    「关于」弹窗要显示它——客户报障时贴一眼就能定位到确切代码快照，
    比版本号更精确（同一版本可能有热修）。
    """
    env_c = (os.environ.get("WX_GIT_COMMIT") or "").strip()
    if env_c:
        return env_c
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            c = (out.stdout or "").strip()
            if c:
                return c
    except Exception:
        pass
    return "unknown"


@lru_cache(maxsize=1)
def get_build_time() -> str:
    """构建时间（ISO8601）。

    优先用安装器注入的 WX_BUILD_TIME；开发环境退化为 VERSION 文件的修改时间，
    再不行才用进程启动时刻——保证「关于」弹窗永远有值可显示。
    """
    env_t = (os.environ.get("WX_BUILD_TIME") or "").strip()
    if env_t:
        return env_t
    for p in _candidate_paths():
        try:
            if p.is_file():
                ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                return ts.astimezone().isoformat(timespec="seconds")
        except Exception:
            continue
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_build_info() -> Dict[str, str]:
    """「关于」弹窗与 /api/health 共用的版本信息包。"""
    return {
        "version": get_version(),
        "build_time": get_build_time(),
        "git_commit": get_git_commit(),
    }


__all__ = ["get_version", "get_git_commit", "get_build_time", "get_build_info"]
