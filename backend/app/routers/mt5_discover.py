"""
MT5 终端自动发现路由 — 扫描本机所有 MetaTrader 5 安装
"""
import os
import glob
import winreg
from fastapi import APIRouter, Depends
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/mt5", tags=["MT5终端发现"])

# 已知的常见安装路径
KNOWN_PATHS = [
    r"F:\mt52\terminal64.exe",  # 已知的回测机
    r"D:\mt5\terminal64.exe",   # 已知的 Demo 终端
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
]

# 需要扫描的根目录前缀
SCAN_ROOTS = [
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "D:\\",
    "E:\\",
    "F:\\",
]


def _scan_directory(root: str, max_depth: int = 3) -> list:
    """递归扫描目录，搜索 terminal64.exe"""
    found = []
    if not os.path.isdir(root):
        return found
    try:
        for root_dir, dirs, files in os.walk(root):
            depth = root_dir[len(root):].count(os.sep)
            if depth > max_depth:
                dirs.clear()  # 不再深入
                continue
            if "terminal64.exe" in files:
                full_path = os.path.join(root_dir, "terminal64.exe")
                found.append(full_path)
    except PermissionError:
        pass
    return found


def discover_mt5_terminals() -> list[dict]:
    """扫描本机所有 MT5 终端，返回列表"""
    terminals = {}
    seen_paths = set()

    # 1. 先检查已知路径（最优先，因为它们是用户实际使用的）
    for known in KNOWN_PATHS:
        norm = os.path.normpath(known)
        if norm in seen_paths:
            continue
        if os.path.isfile(norm):
            seen_paths.add(norm)
            parent = os.path.dirname(norm)
            terminals[norm] = {
                "path": norm,
                "name": _derive_name(norm),
                "source": "已知路径",
                "version": _get_mt5_version(norm),
            }

    # 2. 注册表查询
    try:
        for hive, key_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\MetaQuotes\MetaTrader 5"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\MetaQuotes\MetaTrader 5"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\MetaQuotes\MetaTrader 5"),
        ]:
            try:
                key = winreg.OpenKey(hive, key_path)
                try:
                    install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                    exe_path = os.path.normpath(os.path.join(install_path, "terminal64.exe"))
                    if exe_path not in seen_paths and os.path.isfile(exe_path):
                        seen_paths.add(exe_path)
                        terminals[exe_path] = {
                            "path": exe_path,
                            "name": _derive_name(exe_path),
                            "source": "注册表",
                            "version": _get_mt5_version(exe_path),
                        }
                except (FileNotFoundError, OSError):
                    pass
                finally:
                    winreg.CloseKey(key)
            except (FileNotFoundError, OSError):
                pass
    except Exception:
        pass

    # 3. 多级快速扫描（覆盖 3 层深度，不依赖目录名关键词）
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        # 组合多种模式，确保不同安装路径都能被扫到
        patterns = [
            os.path.join(root, "terminal64.exe"),               # 根目录
            os.path.join(root, "*", "terminal64.exe"),            # 1层
            os.path.join(root, "*", "*", "terminal64.exe"),      # 2层
            os.path.join(root, "*", "*", "*", "terminal64.exe"),# 3层
            os.path.join(root, "*MetaTrader*", "terminal64.exe"),              # 1层含关键词
            os.path.join(root, "*", "*MetaTrader*", "terminal64.exe"),         # 2层含关键词
            os.path.join(root, "*", "*", "*MetaTrader*", "terminal64.exe"),   # 3层含关键词
        ]
        for pattern in patterns:
            for match in glob.glob(pattern, recursive=False):
                norm = os.path.normpath(match)
                if norm not in seen_paths and os.path.isfile(norm):
                    seen_paths.add(norm)
                    terminals[norm] = {
                        "path": norm,
                        "name": _derive_name(norm),
                        "source": "文件扫描",
                        "version": _get_mt5_version(norm),
                    }

    # 4. 深度扫描（兜底，始终执行，限制深度防卡死）
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        found = _scan_directory(root, max_depth=3)
        for f in found:
            norm = os.path.normpath(f)
            if norm not in seen_paths:
                seen_paths.add(norm)
                terminals[norm] = {
                    "path": norm,
                    "name": _derive_name(norm),
                    "source": "深度扫描",
                    "version": _get_mt5_version(norm),
                }

    return list(terminals.values())


def _derive_name(exe_path: str) -> str:
    """从路径推导友好名称"""
    parent = os.path.basename(os.path.dirname(exe_path))
    drive = os.path.splitdrive(exe_path)[0]

    # 特殊路径用固定名称
    norm = os.path.normpath(exe_path)
    if "mt52" in norm.lower():
        return "MT5 回测机 (F:)  — ICMarkets"
    if "d:\\mt5" in norm.lower() or "d:/mt5" in norm.lower():
        return "MT5 Demo (D:) — 模拟盘"

    if "meta" in parent.lower():
        return f"MT5 ({drive}) — {parent}"
    return f"MT5 ({drive}) — {parent}"


def _get_mt5_version(exe_path: str) -> str:
    """读取 EXE 文件版本信息"""
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-Command",
             f"(Get-Item '{exe_path}').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_terminal_by_name(terminal_name: str) -> str | None:
    """根据名称反查终端路径"""
    terminals = discover_mt5_terminals()
    for t in terminals:
        if t["name"] == terminal_name or t["path"] == terminal_name:
            return t["path"]
    return None


@router.get("/discover")
async def discover_terminals(user=Depends(get_current_user)):
    """扫描本机所有 MT5 终端安装。

    2026-08-09：文件扫描可能耗时数秒，改为 async + to_thread offload，
    避免阻塞事件循环导致 health 超时红条。
    """
    import asyncio
    terminals = await asyncio.to_thread(discover_mt5_terminals)
    return {"terminals": terminals, "count": len(terminals)}
