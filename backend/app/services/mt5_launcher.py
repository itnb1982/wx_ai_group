"""
MT5 终端启动器
------------------------------------------------------------
解决 MT5 Python API 下单返回 retcode=10027 (AutoTrading disabled by client) 的问题。

原因：
    MetaTrader5 的 Python 包只能「附着」到一个已经运行的终端实例，
    无法改变该终端的「算法交易（Algo Trading）」开关状态。
    该开关默认关闭，且持久化在加密的 config/settings.ini 中，无法直接编辑。

官方解法：
    MT5 支持通过命令行启动配置文件设置该开关：
        terminal64.exe /config:<绝对路径.ini>
    其中 [Experts] 节的 AllowLiveTrading=1 即对应工具栏 Algo Trading 按钮。
    配置文件必须以 UTF-16 (Unicode) 保存。

因此：在 Worker 调用 mt5.initialize() 之前，若目标终端尚未运行，
      则由本模块用带 AllowLiveTrading=1 的配置文件把它拉起来。
"""

import os
import subprocess
import threading
import time
import logging
from typing import Sequence

from app.services.proc_utils import find_pids_by_exe, list_pids_by_name, kill_pids
from runtime_paths import data_dir

logger = logging.getLogger(__name__)

# ── 本进程启动过的终端记录 {规范化路径: PID} ──────────────────────────
# 为什么需要它（2026-08-09 三次修复）：
#   当进程无权读取其他会话进程的可执行路径时（OpenProcess err=5），
#   find_pids_by_exe 会返回 resolved=False。上一版在这种情况下"只要有
#   任意 terminal64.exe 在跑就判定已运行"，在**多账号**下是灾难：
#   账号1 的终端起来后，账号 2/3/4 全都误判成"我的终端已在运行"。
#   而本记录是按路径精确登记的，不依赖任何权限，可作为可靠兜底。
_LAUNCHED: dict = {}
_LAUNCHED_LOCK = threading.Lock()


def _norm_path(p: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(p))
    except Exception:
        return os.path.normcase(p or "")


def _remember_launch(terminal_path: str, pid: int) -> None:
    with _LAUNCHED_LOCK:
        _LAUNCHED[_norm_path(terminal_path)] = int(pid)


def _launched_pid_alive(terminal_path: str) -> bool:
    """本进程是否启动过该路径的终端，且那个 PID 现在还活着。"""
    with _LAUNCHED_LOCK:
        pid = _LAUNCHED.get(_norm_path(terminal_path))
    if not pid:
        return False
    # ★ 2026-08-15 审计P3修复：原 `pid in list_pids_by_name("terminal64.exe")` 仅按进程名
    #   匹配——PID 被 OS 回收复用时（旧终端死后分配给其他 terminal64/进程）会误判存活 →
    #   不重新拉起终端。改按【路径精确】匹配（与全项目 2026-08-09 铁律一致），
    #   PID 复用/他路径终端均不会误判。
    pids, resolved = find_pids_by_exe("terminal64.exe", _norm_path(terminal_path))
    return resolved and pid in pids

# 启动配置文件存放目录
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".wanxiangai", "mt5_config")

# WebView2 数据目录（解决 LocalSystem/Session0 下 MT5 启动时 Edge WebView
# 无法写入 systemprofile 的问题）。按账号 tag 隔离，避免多个 MT5 实例共享同一
# WebView 数据目录引发冲突。基座使用 runtime_paths.data_dir() 而不是
# os.path.expanduser("~")，保证整目录拷到别机也能落到可写位置。
_WEBVIEW2_DIR = os.path.join(str(data_dir()), "webview2")

# MT5 启动配置模板（UTF-16 保存）
_CONFIG_TEMPLATE = """; WanxiangAI 自动生成 —— 请勿手工编辑
; 作用：启动 MT5 时强制打开算法交易开关，使 Python API 可以下单
[Common]
Login={login}
Password={password}
Server={server}
KeepPrivate=1
NewsEnable=0

[Experts]
AllowLiveTrading=1
AllowDllImport=1
Enabled=1
Account=0
Profile=0
"""


def _write_config(login: str, password: str, server: str, tag: str) -> str:
    """生成 UTF-16 编码的启动配置文件，返回其绝对路径"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, f"{tag}.ini")
    content = _CONFIG_TEMPLATE.format(login=login, password=password, server=server)
    # MT5 要求启动配置文件为 Unicode(UTF-16 LE with BOM)
    with open(path, "w", encoding="utf-16") as f:
        f.write(content)
    return path


def is_terminal_running(terminal_path: str) -> bool:
    """判断指定路径的 terminal64.exe 是否已在运行。

    2026-08-09 二次根因修复（这是「系统瘫痪」的真凶之一）
    ──────────────────────────────────────────────
    上一版为绕开被禁用的 psutil，改用 `wmic ... get ExecutablePath` 匹配路径。
    但实测本机 wmic 的 ExecutablePath 字段**全局失效**（查调用者自己的
    python.exe 都返回空），于是本函数**永远返回 False**：
      → ensure_terminal 每次都判定「终端未运行」→ 对一个已在运行的终端重复冷启动
      → 两个实例抢同一数据目录 → mt5.initialize() 报 IPC send failed (-10001)
      → 4 个账号全部连不上，且每次都白等 90s 冷启动超时。

    现改用 Win32 API QueryFullProcessImageNameW 直接取路径（proc_utils）。

    2026-08-09 三次修复：拿不到路径时**不能**再"只要有任意 terminal64.exe
    就算已运行"。那在多账号下会让账号 2/3/4 集体误判成"我的终端已在运行"，
    真正该起的终端永远起不来。改为按"本进程启动记录"精确兜底。
    """
    if not terminal_path:
        return False

    pids, resolved = find_pids_by_exe("terminal64.exe", terminal_path)
    if resolved:
        return bool(pids)

    # resolved=False：无 terminal64.exe，或有但读不到路径（权限不足）
    if not list_pids_by_name("terminal64.exe"):
        return False  # 一个都没有，明确未运行

    # 有同名进程但无法归属 → 只认本进程亲手启动、且 PID 仍存活的那一个。
    # 绝不因为"别的账号有终端在跑"就认为自己的终端也在跑。
    if _launched_pid_alive(terminal_path):
        return True

    logger.warning(
        "[MT5启动器] 存在 terminal64.exe 但无法解析其可执行路径"
        "（当前进程权限不足以 OpenProcess 其他会话的进程），"
        f"且本进程未启动过该终端，判定为未运行: {terminal_path}"
    )
    return False


def ensure_terminal(
    terminal_path: str,
    login: str = "",
    password: str = "",
    server: str = "",
    tag: str = "default",
    wait: float = 25.0,
) -> dict:
    """
    确保目标终端已运行，且算法交易开关处于开启状态。

    如果终端未运行 -> 用 /config: 启动（AllowLiveTrading=1）。
    如果终端已运行 -> 探测 trade_allowed；若算法交易被关闭则自动杀掉并用正确配置重启。
                      （MT5 升级后会重置此开关，这是 retcode=10027 的主因。）

    返回: {"started": bool, "already_running": bool, "config": str, "error": str|None}
    """
    result = {"started": False, "already_running": False, "config": "", "error": None}

    if not terminal_path or not os.path.exists(terminal_path):
        result["error"] = f"终端路径不存在: {terminal_path}"
        return result

    # ── 阶段1：终端未运行 → 直接用 /config: 启动 ──
    if not is_terminal_running(terminal_path):
        return _start_terminal(terminal_path, login, password, server, tag, wait)

    # ── 阶段2：终端已运行 → 探测算法交易开关 ──
    # ★ 2026-08-09：_check_algo_trading 内部走 mt5.initialize(path=...)，
    #   而 MetaTrader5 包在目标终端**未运行时会自行把它拉起来**（且不带
    #   /config，AllowLiveTrading 是关的）。这等于绕开启动器偷偷开了第二个
    #   实例，正是抢数据目录、触发 IPC send failed 的第二个源头。
    #   因此只有"能精确确认该终端在跑"时才做探测；无法确认就跳过探测，
    #   直接按已运行处理，把开关校验留给 Worker 侧。
    _pids, _resolved = find_pids_by_exe("terminal64.exe", terminal_path)
    if not (_resolved and _pids):
        result["already_running"] = True
        _show_terminal_window(tag)
        logger.info(f"[MT5启动器] {tag} 终端已在运行（无法精确校验算法交易开关，跳过探测以免误启第二实例）")
        return result

    trade_ok = _check_algo_trading(terminal_path)
    if trade_ok:
        result["already_running"] = True
        _show_terminal_window(tag)  # 点连接/重启时把本地客户端窗口调到前台
        logger.info(f"[MT5启动器] {tag} 终端已在运行且算法交易已开启 ✓")
        return result

    # ── 阶段3：终端在跑但算法交易被关（MT5 升级/重置）→ 杀掉重启 ──
    logger.warning(
        f"[MT5启动器] {tag} 终端进程存活但算法交易已被关闭！"
        f"（可能 MT5 升级重置了设置）正在杀掉并用正确配置重启..."
    )
    _kill_terminal(terminal_path)
    time.sleep(2.0)  # 等进程彻底退出
    return _start_terminal(terminal_path, login, password, server, tag, wait)


def _start_terminal(
    terminal_path: str, login: str, password: str, server: str, tag: str, wait: float,
) -> dict:
    """用 /config:AllowLiveTrading=1 启动终端"""
    result = {"started": False, "already_running": False, "config": "", "error": None}
    try:
        cfg = _write_config(login, password, server, tag)
        result["config"] = cfg

        # ★ 2026-08-09：LocalSystem/Session0 下 MT5 内置 Edge WebView 默认把用户数据
        # 目录放在 systemprofile，因权限/隔离问题无法创建，会弹窗报错并拖死启动。
        # 通过环境变量把 WebView2 数据目录重定向到当前用户可写位置（服务身份下即
        # LocalSystem 的 %USERPROFILE%，目录由本模块预创建）。
        webview2_folder = os.path.join(_WEBVIEW2_DIR, tag)
        os.makedirs(webview2_folder, exist_ok=True)
        env = os.environ.copy()
        env["WEBVIEW2_USER_DATA_FOLDER"] = webview2_folder

        proc = subprocess.Popen(
            [terminal_path, f"/config:{cfg}"],
            cwd=os.path.dirname(terminal_path),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env,
        )
        # 登记 PID：权限不足读不到进程路径时，这是判断"我的终端还在不在"的唯一可靠依据
        _remember_launch(terminal_path, proc.pid)
        deadline = time.time() + wait
        while time.time() < deadline:
            if is_terminal_running(terminal_path):
                result["started"] = True
                break
            time.sleep(1.0)
        if not result["started"]:
            result["error"] = "终端启动超时"
        else:
            time.sleep(6.0)  # 等待登录与行情同步
            _show_terminal_window(tag)  # 冷启动时也把客户端窗口调到前台
    except Exception as e:
        result["error"] = f"启动终端失败: {e}"
    logger.info(f"[MT5启动器] {tag} 新启动 -> {result}")
    return result


def _check_algo_trading(terminal_path: str) -> bool:
    """
    探测指定终端的算法交易开关是否开启。
    通过 mt5.initialize(path=...) 附着后读 terminal_info().trade_allowed。
    """
    try:
        import MetaTrader5 as mt5
        ok = mt5.initialize(path=terminal_path)
        if not ok:
            return False
        try:
            ti = mt5.terminal_info()
            allowed = getattr(ti, "trade_allowed", False) if ti else False
            logger.debug(f"[MT5启动器] trade_allowed={allowed} for {terminal_path}")
            return bool(allowed)
        finally:
            mt5.shutdown()
    except Exception as e:
        logger.debug(f"[MT5启动器] 探测算法交易失败: {e}")
        return False


def _kill_terminal(terminal_path: str, wait_exit: float = 10.0) -> None:
    """杀掉指定路径的 terminal64.exe 进程，并等待其真正退出。

    避免旧进程残留变成「无响应」僵尸、与新启动的实例重叠（这会导致
    ensure_terminal 误判「已在运行」并附着到死进程，表现为「点连接调取不出客户端」）。

    2026-08-09 二次根因修复：原实现用 `wmic where ExecutablePath='...'` 查 PID，
    而本机 wmic 的 ExecutablePath 字段全局失效 → 永远查不到 PID → **永远杀不掉**，
    僵尸终端不断堆积，正是「点连接调不出客户端」的元凶。
    现改用 Win32 API 解析路径（proc_utils），仍严格按路径匹配，绝不误杀其他账号终端。
    """
    pids, resolved = find_pids_by_exe("terminal64.exe", terminal_path)
    if not resolved:
        # 无法确认归属时绝不盲杀：多账号共存下误杀等于把别人的仓位连接掐断。
        logger.warning(
            f"[MT5启动器] 无法解析 terminal64.exe 的可执行路径，跳过清理以免误杀其他账号终端: {terminal_path}"
        )
        return

    killed = kill_pids(pids)

    # 等待进程真正退出，避免旧实例残留成僵尸
    deadline = time.time() + wait_exit
    while time.time() < deadline:
        still, ok = find_pids_by_exe("terminal64.exe", terminal_path)
        if ok and not still:
            break
        time.sleep(0.5)

    if killed:
        logger.info(f"[MT5启动器] 已杀掉终端进程(PID): {killed}")


def cleanup_orphan_terminals(terminal_paths: Sequence[str]) -> dict:
    """★ 2026-08-15 第三批#7：重载时清理上一后端实例残留的孤儿 MT5 终端。

    背景（实测痛点）：
        后端重载（restart_task_backend）杀掉 python 进程树后，旧实例亲手拉起的
        terminal64.exe 可能因「会话隔离 / 权限不足读不到进程路径」未被级联杀净，
        残留在原路径继续运行。新实例启动后 ensure_terminal 按「路径精确」判定该终端
        仍在运行 → 直接附着到这个僵死/持锁的旧终端，表现为连接卡死、行情/下单无响应。

    本函数在启动接入账号前，对【已知 WanxiangAI 终端路径】做一次路径精确的孤儿清理：
        凡在规范化路径上运行、但本进程尚未登记(_LAUNCHED 在新进程里为空)的终端，
        一律视为上一实例残留 → 精确杀掉，让新实例干净冷启动。

    安全边界（沿用 2026-08-09 铁律）：
        仅匹配 find_pids_by_exe 路径精确相等(resolved=True) 的 terminal64.exe，
        绝不因「存在任意 terminal64.exe」就盲杀，绝不误杀其他账号/其他 MT5 客户端。
    """
    result = {"cleaned": 0, "skipped": 0, "paths": []}
    seen: set = set()
    for tp in (terminal_paths or []):
        norm = _norm_path(tp)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result["paths"].append(norm)
        if not os.path.exists(tp):
            result["skipped"] += 1
            continue
        # 路径精确匹配：只有能确认路径归属时才清理，否则交回 ensure_terminal 处理
        pids, resolved = find_pids_by_exe("terminal64.exe", tp)
        if resolved and pids:
            logger.warning(
                f"[MT5启动器] 探测到路径 {norm} 上有残留终端(PID={pids})，"
                f"判定为上一实例孤儿，执行路径精确清理"
            )
            _kill_terminal(tp)
            result["cleaned"] += 1
        else:
            result["skipped"] += 1
    if result["cleaned"]:
        logger.info(f"[MT5启动器] 孤儿终端清理完成：清理 {result['cleaned']} 个，跳过 {result['skipped']} 个")
    return result


def _show_terminal_window(tag: str) -> None:
    """把指定账号的 MT5 客户端窗口恢复到前台（若被最小化）。tag 通常为 login 串。

    用途：点击「连接」或 Worker 重启时，把本地客户端窗口调到用户眼前，
          对应「点连接调取本地客户端」的体验。失败（如服务无桌面会话）静默忽略。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        target_tag = str(tag or "")
        if not target_tag:
            return

        def _cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if target_tag in buf.value:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE 恢复最小化
                        try:
                            user32.SetForegroundWindow(hwnd)
                        except Exception:
                            pass
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception as e:
        logger.debug(f"[MT5启动器] 显示终端窗口失败(忽略): {e}")
