"""
进程查询工具（Windows）—— 替代失效的 wmic ExecutablePath 与被禁用的 psutil。

2026-08-09 根因修复背景
────────────────────────
本机存在两个致命限制，叠加后让「按可执行文件路径识别进程」彻底失效：

1. psutil 被项目铁律禁用：本机 psutil.process_iter() 会永久挂死。
2. wmic 的 ExecutablePath 字段在本机**全局失效**——实测
   `wmic process where "name='python.exe'" get ExecutablePath` 对调用者
   自己的进程都返回空值，并非权限问题，而是 WMI 该字段本身取不到。

后果（这正是「系统瘫痪」的真凶之一）：
  mt5_launcher.is_terminal_running() 靠 wmic 匹配路径 → 永远匹配不上 → 永远返回 False
  → ensure_terminal 每次都判定「终端未运行」→ 重复冷启动一个已在运行的终端
  → 两个实例抢同一数据目录 → mt5.initialize() 报 IPC send failed (-10001)
  同时 _kill_terminal() 也靠 wmic 查 PID → 永远查不到 → 僵尸终端永远杀不掉、越堆越多。

本模块改用 Win32 API `QueryFullProcessImageNameW` 直接取路径：
  - 纯 ctypes，无第三方依赖，不受 WMI 仓库状态影响
  - 单次系统调用，微秒级，不存在 psutil 那种遍历挂死风险
  - 以 PROCESS_QUERY_LIMITED_INFORMATION 打开，权限要求最低

权限说明：非管理员进程无法 OpenProcess 到 SYSTEM/其他会话的进程（err=5）。
后端以服务(SYSTEM)或管理员身份运行时可正常取得。取不到时调用方须走保守降级，
详见 exe_paths_by_name() 的 resolved 标志。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _WIN32_OK = True
except Exception:  # pragma: no cover - 非 Windows 或 DLL 异常
    _kernel32 = None
    _WIN32_OK = False


def exe_path_of(pid: int) -> Optional[str]:
    """取指定 PID 的完整可执行文件路径；失败（权限不足/进程已退出）返回 None。"""
    if not _WIN32_OK or pid <= 0:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    except Exception:
        return None
    finally:
        try:
            _kernel32.CloseHandle(handle)
        except Exception:
            pass


def list_pids_by_name(image_name: str) -> List[int]:
    """用 tasklist 列出指定映像名的所有 PID（tasklist 在本机稳定可用）。"""
    pids: List[int] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        ).stdout or ""
    except Exception:
        return pids
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("信息:") or line.lower().startswith("info:"):
            continue
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == image_name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def exe_paths_by_name(image_name: str) -> Tuple[Dict[int, Optional[str]], bool]:
    """列出某映像名的 {pid: exe_path}，并返回是否「至少解析出一个路径」。

    返回 (mapping, resolved)：
      resolved=False 表示进程存在但一个路径都取不到（典型为非管理员查 SYSTEM 进程），
      此时调用方**不得**据此判定「不是目标进程」，必须走保守降级，
      否则会重蹈 wmic 失效时「重复启动终端 → IPC send failed」的覆辙。
    """
    mapping: Dict[int, Optional[str]] = {}
    for pid in list_pids_by_name(image_name):
        mapping[pid] = exe_path_of(pid)
    resolved = any(v for v in mapping.values())
    return mapping, resolved


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(path or "")


def find_pids_by_exe(image_name: str, exe_path: str) -> Tuple[List[int], bool]:
    """查出映像名匹配且可执行路径等于 exe_path 的所有 PID。

    返回 (pids, resolved)。resolved 含义同 exe_paths_by_name：
    为 False 时 pids 恒为空，但**不代表进程不存在**，只代表无法确认归属。
    """
    target = _norm(exe_path)
    mapping, resolved = exe_paths_by_name(image_name)
    if not resolved:
        return [], False
    hits = [pid for pid, p in mapping.items() if p and _norm(p) == target]
    return hits, True


def kill_pids(pids: List[int]) -> List[int]:
    """taskkill /T /F 杀掉给定 PID，返回实际发起过 kill 的 PID 列表。

    注意：全项目禁用 os.system（会弹独立 cmd 黑框），统一走 subprocess + CREATE_NO_WINDOW。
    """
    killed: List[int] = []
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            killed.append(int(pid))
        except Exception:
            continue
    return killed
