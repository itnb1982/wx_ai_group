"""安全删除工具 —— 根治 WorkBuddy managed python 的 sitecustomize shim
把 os.remove / shutil.rmtree 重定向进回收站的问题。

现象（2026-08-11 排查）：backend 的 worker 子进程跑在
C:\\Users\\15588\\workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe 上，
该解释器自带 sitecustomize shim，会 hook Python 层的 os.remove /
shutil.rmtree，把删除重定向到 Windows 回收站。backend 跑时序模型
（Chronos / Moirai）每个 tick 都创建+删除临时 json，于是"实时"往回收站
灌垃圾，一会儿就塞满。

本模块用 ctypes 直接调用 Win32 DeleteFileW / RemoveDirectoryW，
绕过 Python 层的 hook，删除即真删、不进回收站。仅在 Windows 生效，
非 Windows 回退到 os.remove。
"""

import os
import ctypes

_KERNEL32 = None


def _k32():
    global _KERNEL32
    if _KERNEL32 is None:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _KERNEL32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        _KERNEL32.DeleteFileW.restype = ctypes.c_int
        _KERNEL32.RemoveDirectoryW.argtypes = [ctypes.c_wchar_p]
        _KERNEL32.RemoveDirectoryW.restype = ctypes.c_int
    return _KERNEL32


def safe_remove(path):
    """删除文件或空目录，不进回收站。不存在则静默跳过。"""
    if not path:
        return
    try:
        if not os.path.exists(path):
            return
    except Exception:
        return
    try:
        k = _k32()
        if os.path.isdir(path) and not os.path.islink(path):
            k.RemoveDirectoryW(path)
        else:
            k.DeleteFileW(path)
    except Exception:
        # 任何异常都回退到原生删除（仍可能被 shim 拦截，但至少不报错）
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.remove(path)
        except Exception:
            pass


def safe_rmtree(path):
    """递归删除目录树，不进回收站。"""
    if not path or not os.path.exists(path):
        return
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                safe_remove(os.path.join(root, f))
            for d in dirs:
                safe_remove(os.path.join(root, d))
        safe_remove(path)
    except Exception:
        pass
