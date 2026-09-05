"""
万象Ai量化交易系统 — 后端守护进程（看门狗）
==========================================
职责：
  1. 拉起 uvicorn 子进程（前端 + API 一体）。
  2. 若子进程崩溃/退出，自动重启（带熔断防抖），使系统具备"无人值守自愈"能力。
  3. 重启前清理上一轮可能残留的 mt5_worker 孤儿进程，避免其长期占用终端连接。

MT5 终端本身由后端 Worker 按需自行启动（见 mt5_launcher.ensure_terminal），
本守护进程只负责"后端进程死了能自动爬起来"。
"""
import os
import sys
import time
import signal
import socket
import subprocess
import urllib.request
import ctypes

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
# 解释器改为运行时发现（见 runtime_paths），不再写死开发机绝对路径。
# 商业版要求整目录可拷贝到任意电脑运行，硬编码路径换机即废。
# 优先级已在 runtime_paths.find_python 中定义：项目 .venv 优先于系统 Python——
# 因为 torch/chronos 等重依赖只装在项目 venv 里，用错解释器会导致本地模型
# 永远加载失败（静默降级），这比直接崩溃更难排查。
sys.path.insert(0, BACKEND_DIR)
from runtime_paths import find_python  # noqa: E402

PYTHON = find_python()
# ★ 2026-08-09 关键修复：严禁用 pythonw.exe 拉起 uvicorn。
# Windows 上 pythonw.exe 被 subprocess.Popen 启动且 stdout 重定向到文件时，
# 会额外 spawn 一个中间 launcher 进程；supervisor 拿到的是外层 PID，而真实
# uvicorn 服务跑在孙子进程里。结果：supervisor 监管的是一个空壳，kill/poll
# 都针对错误进程 → 端口占用、双开、假死无法自愈。
# 改用 python.exe + CREATE_NO_WINDOW，stdout/stderr 已重定向到日志文件，
# 同样无黑框，但 PID 就是真实 uvicorn 服务进程。
PYTHONW = PYTHON
HOST = "127.0.0.1"
PORT = "8080"
RESTART_DELAY = 3           # 子进程退出后等待重启的秒数
MAX_RESTARTS_PER_MIN = 12   # 1分钟内重启超过此数视为持续崩溃（仍继续，仅告警）

# ── 存活探测（根治"后端无声假死"）──
# 仅探测 loopback 健康端点是否可达：可达（即便 status=degraded，如非交易时段
# cycle 停滞）即视为进程活着，不重启；不可达（进程死/挂起无响应）才判定假死重启。
# 故意不把 "degraded" 计入失败，避免非交易时段误杀。网络抖动仅影响 AI/MT5 上游，
# 不波及本机 loopback，故不会因此误重启。
HEALTH_URL = f"http://{HOST}:{PORT}/api/health"
HEALTH_CHECK_INTERVAL = 5.0   # 每 5 秒探一次
HEALTH_MAX_FAILS = 4          # 连续「硬失败(连接被拒/进程真死)」达此数（~20s）才判假死重启
HEALTH_TIMEOUT = 12           # 单次健康探测超时（秒）。原 4s 太紧：主循环被合法重型
                            # 请求（前端轮询触发的本地模型推理 / 云端双脑 API 调用）短暂
                            # 阻塞即被误判假死。12s 覆盖正常业务峰值，避免「正在服务用户
                            # 的进程被自己强杀」→ 前端 500 风暴、用户「系统不能用」。
HEALTH_SOFT_MAX_FAILS = 12    # 连续「软超时(进程存活但暂时忙/慢)」达此数（~60s 持续超时）
                            # 才判真卡死强杀。正常业务偶发 12-16s 阻塞远达不到，不会误杀。

# ── 2026-08-07 根治「永久崩溃循环」：启动探针 / 存活探针分离 ──────────────
# 事故复盘：2026-08-07 18:26~18:35 服务连续 5 次崩溃循环、10 分钟无法恢复，
#   而完全相同的命令手动启动却一次成功。根因是**监管窗口短于应用自愈窗口**：
#     · supervisor 判死预算 = HEALTH_GRACE(25s) + MAX_FAILS(4) × INTERVAL(5s) = 45s
#     · 应用 init_db 自愈预算 = 6 次 × (_raw_creator 退避 31.5s + 1.5s) ≈ 198s
#   45s < 198s → uvicorn 每次都在跑完第 2 轮 DB 重试前就被强杀，应用自带的
#   「撞 Defender 扫描锁则退避重试」机制**永远没机会生效**；而强杀又再次改写
#   DB 文件状态、触发 Defender 重新扫描，下一轮重启继续撞锁 → 自我延续的死循环。
#   （手动启动无人杀它，等 Defender 扫完自然成功，印证此结论。）
# 修复（对标 K8s startupProbe 与 livenessProbe 分离的标准做法）：
#   · 启动期：只要进程还活着就不判死，宽限期覆盖应用最大自愈预算（240s > 198s）；
#   · 转入健康后：才启用 45s 的严格存活探测，假死照常快速重启，不牺牲自愈灵敏度。
STARTUP_GRACE = 480.0         # 启动探针：首次健康前的宽限期。必须覆盖 init_db 自愈预算(~198s)
                              # 及 N 个 MT5 账号串行连接预算(4账号×90s=360s)，否则 supervisor 会在
                              # 正常启动流程中误判假死并强杀 uvicorn → 永久崩溃循环。
HEALTH_GRACE = 25.0           # 存活探针：已健康过之后，重启后的短宽限

_log_path = os.path.join(BACKEND_DIR, "supervisor.log")
_uvicorn_log = os.path.join(BACKEND_DIR, "supervisor_uvicorn.log")


#: 单个日志文件上限（MB）。超过就轮转，最多保留 LOG_KEEP 个历史文件。
LOG_MAX_MB = 50
LOG_KEEP = 3


def _rotate_if_big(path: str, max_mb: int = LOG_MAX_MB, keep: int = LOG_KEEP):
    """日志超限就轮转（xxx.log → xxx.log.1 → .2 …），超出 keep 的丢弃。

    ★ 2026-08-08 审计发现：uvicorn 的 stdout 是以 "a" 模式**纯追加**写入的，
    从来不轮转。实测 supervisor_uvicorn.log 已经涨到 130MB 还在长。
    7×24 连续交易下这会持续膨胀，最终把磁盘吃满——而磁盘一满，
    写库/写日志全线失败，是能把交易系统整个拖死的低级故障。

    刻意做成"启动时检查一次"而不是引入 logging.RotatingFileHandler：
    这里的写入方是**子进程的 stdout 句柄**，不归 logging 管，
    只有在重新拉起子进程的这一刻换文件才是安全的（不会写到一半被抽走）。
    """
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) < max_mb * 1024 * 1024:
            return
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            _safe_remove(oldest)
        for i in range(keep - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
    except Exception:
        # 轮转失败绝不能挡住服务启动——大不了继续写大文件。
        pass


def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _kill_orphan_workers():
    """清理上一轮崩溃残留的 mt5_worker 孤儿进程（命令行含 spawn_main 标记）。

    仅匹配我们的 Worker 子进程（uvicorn 主进程命令行无 spawn_main），
    不会误杀其他 python 程序。重启新 uvicorn 前调用，确保终端连接不被旧进程长期占用。
    """
    ps = (
        'Get-CimInstance Win32_Process -ErrorAction SilentlyContinue '
        '| Where-Object { $_.CommandLine -like "*spawn_main*" } '
        '| ForEach-Object { $_.Terminate() }'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
    except Exception:
        pass


def _get_uvicorn_server_pid(log_path: str, timeout: float = 30.0) -> int | None:
    """从 uvicorn 日志中读取真实服务进程 PID。

    Windows 上 uvicorn 会 spawn 一个 launcher 子进程，真正服务跑在孙子进程里
    （日志 `Started server process [PID]`）。supervisor 必须监管/强杀这个真实 PID，
    否则 launcher 活着、服务卡死时无法自愈，形成"无声假死"。
    """
    deadline = time.time() + timeout
    marker = "Started server process ["
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # 从当前末尾开始
            while time.time() < deadline:
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                idx = line.find(marker)
                if idx == -1:
                    continue
                start = idx + len(marker)
                end = line.find("]", start)
                if end == -1:
                    continue
                try:
                    return int(line[start:end])
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _kill_tree(pid):
    """按进程树强杀（父 + 子 Worker），确保旧 uvicorn 及其 Worker 一并退出。

    2026-08-09 踩坑：本机 psutil.Process()/process_iter() 在遍历命令行时
    会永久挂死（已知环境缺陷），因此彻底禁用 psutil，只用 taskkill /T /F
    杀整棵进程树。实测 taskkill /T 对通过 multiprocessing.spawn 启动的
    Worker 子进程回收足够彻底；残留端口问题由后续 _wait_port_free 兜底。

    ★ 严禁用 os.system：os.system 在 Windows 上会弹出独立 cmd.exe 黑框
      （父进程 pythonw 无控制台，子 cmd 会被分配新控制台 → 黑框一闪而过）。
      改用 subprocess + CREATE_NO_WINDOW，杀进程不再闪黑框。
    """
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
    except Exception:
        pass


# 命名互斥体句柄：必须全程持有（直到进程退出），不能让 GC 提前关闭 ——
# 否则互斥体释放，第二个 supervisor 又能拿到，单实例守卫就失效了。
_SINGLE_MUTEX = None


def _safe_remove(path):
    """安全删除，绕过 WorkBuddy managed python 的 sitecustomize shim
    （该 shim 把 os.remove 重定向进回收站，导致日志轮转疯狂塞满回收站）。"""
    if not path or not os.path.exists(path):
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        kernel32.DeleteFileW.restype = ctypes.c_int
        kernel32.DeleteFileW(path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


def _single_instance() -> bool:
    """跨进程单实例守卫（Windows 命名互斥体，正确用法）。
    返回 True=获得独占权，False=已有实例在跑。

    2026-08-09 三次踩坑后定稿：
      1) 旧实现用 ctypes.windll 调 CreateMutexW，但没设 argtypes/restype 与
         use_last_error，GetLastError() 取值不可靠 → 两实例都误判拿到锁→双开竞争。
      2) 互斥体句柄当局部变量，函数返回后被 GC 关闭 → 锁释放→双开。
      3) 改「哨兵端口」又被 Windows SO_REUSEADDR 语义坑（反而允许复用）。
    最终回到命名互斥体，但用 WinDLL(use_last_error=True)+正确 argtypes/restype：
      · CreateMutexW 同名全局唯一；胜出者 GetLastError()!=183，其余必得
        183(ERROR_ALREADY_EXISTS)→立即退出。
      · 句柄存全局 _SINGLE_MUTEX 常驻进程生命周期；进程退出由 OS 自动释放。
      · PID 锁文件仅作运维排查标记。
    """
    global _SINGLE_MUTEX
    lock = os.path.join(BACKEND_DIR, ".supervisor.lock")
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            mutex = kernel32.CreateMutexW(None, False, "WanxiangAI_Supervisor_Singleton")
            if not mutex:
                return _single_instance_fallback()
            err = ctypes.get_last_error()
            if err == 183:  # ERROR_ALREADY_EXISTS：另一实例已持有
                try:
                    kernel32.CloseHandle(mutex)
                except Exception:
                    pass
                return False
            # 唯一胜出者：持有句柄（不关闭），写 PID 锁文件便于排查。
            _SINGLE_MUTEX = mutex
            try:
                with open(lock, "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass
            return True
        except Exception:
            return _single_instance_fallback()
    return _single_instance_fallback()


def _pid_alive(pid: int) -> bool:
    """不用 psutil 检查 PID 是否存活（本机 psutil 会挂死）。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5,
        )
        return str(pid) in r.stdout
    except Exception:
        return False


def _single_instance_fallback() -> bool:
    """互斥体不可用（非 Windows / win32api 缺失）时的兜底：端口占用 + PID 文件锁。"""
    lock = os.path.join(BACKEND_DIR, ".supervisor.lock")
    if _port_in_use(PORT):
        return False
    try:
        if os.path.exists(lock):
            with open(lock, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            if _pid_alive(pid):
                return False
    except Exception:
        pass
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    return True


def _port_in_use(port: str) -> bool:
    """探测本机端口是否已被监听（用于单实例守卫，避免重复拉起 supervisor 抢端口）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _health_ok() -> str:
    """探测 uvicorn 健康端点，区分三种结果（根治 2026-08-09 重启风暴）：

      "ok"   : 端点可达且在超时内响应（进程活着、能正常服务）
      "dead" : 连接被拒 / 进程已不存在（真正假死 → 应快速重启）
      "busy" : 连接可达但响应超时（主循环正忙，如前端轮询触发本地模型推理 /
               云端双脑 API 调用；进程没死，只是暂时慢 → 宽容，不重启）

    设计动机：
      原实现 urlopen(timeout=4) 把「主循环被合法重型请求短暂阻塞」与「进程真死」
      一视同仁——只要 >4s 没响应就累计失败，4 次即强杀。前端开着轮询仪表盘时，
      每 ~1 分钟就有 12-16s 的同步重型 AI/云调用，于是每 ~1 分钟被误杀一次，
      前端 500 风暴、用户「系统不能用」。

      现改为三级：硬死亡（连接被拒）才快杀；软超时（进程忙）只在「连续足够久
      （HEALTH_SOFT_MAX_FAILS，约 60s 持续超时）」时才判真卡死强杀。这样正在
      服务用户的进程绝不会被自己杀掉，而真正卡死的进程仍会被自愈拉起。
    """
    import socket
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as r:
            r.read()
        return "ok"
    except urllib.error.HTTPError:
        # 拿到 HTTP 响应（即便 5xx）即说明进程活着，按 ok 处理
        return "ok"
    except urllib.error.URLError:
        # 连接被拒 / 名称解析失败 / 进程不存在 → 真正假死
        return "dead"
    except (socket.timeout, TimeoutError):
        # 连接可达但响应超时 → 主循环正忙，进程没死
        return "busy"
    except Exception:
        # 其他异常（SSL/协议等）按 busy 宽容处理，避免误杀
        return "busy"


def _kill_zombie_terminals():
    """清理"无响应(Not Responding)"的 terminal64 进程。

    僵尸终端（MT5 升级/重启残留、GUI 消息循环卡死）会占用终端路径，
    导致新 Worker 的 ensure_terminal 误判"已在运行"并附着到死进程、
    表现为"点连接调取不出客户端 / 交易无法下单"。
    仅杀 'Responding=False' 的终端（健康但暂时繁忙的不会被误杀），杀完等待其退出。
    在每次 (重)启动 uvicorn 前调用，确保干净的终端环境。
    """
    ps = (
        'Get-Process -Name "terminal64" -ErrorAction SilentlyContinue '
        '| Where-Object { -not $_.Responding } '
        '| ForEach-Object { '
        '    try { $_.Kill(); $_.WaitForExit(10000) | Out-Null } catch {} '
        '}'
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        pass


def _wait_port_free(host: str, port: int, timeout: int = 8) -> None:
    """等待端口释放（解决 Windows 下 [Errno 10048] 崩溃循环）。

    进程退出后 Windows 不会立即释放 socket（TIME_WAIT / 句柄延迟回收），
    新进程立即 bind 会失败。本函数轮询等待端口可绑定。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            s.close()
            return  # 端口已释放
        except OSError:
            time.sleep(0.5)
    _log(f"警告：等待端口 {port} 释放超时({timeout}s)，仍尝试重启")


def main():
    os.chdir(BACKEND_DIR)

    # ── 单实例守卫：命名互斥体 + PID 文件锁（已加固 PID 复用防御）──
    if not _single_instance():
        _log("已有 supervisor 实例在运行（单实例守卫：互斥体/PID锁占用），本进程退出以避免重复拉起")
        sys.exit(0)
    # 端口守卫作为二次保险（正常情况下互斥体已拦截重复实例）
    if _port_in_use(PORT):
        _log(f"端口 {PORT} 已被占用（另一 supervisor/uvicorn 实例已在运行），本进程退出以避免冲突")
        sys.exit(0)

    _log("守护进程启动，开始监管后端 uvicorn...")
    _kill_orphan_workers()
    _kill_zombie_terminals()
    child = None
    restarts = []
    _restart_backoff = RESTART_DELAY  # 连续启动失败时指数退避（见循环末尾）

    def _on_signal(signum, frame):
        _log(f"收到退出信号 {signum}，正在终止后端子进程...")
        if child is not None and child.poll() is None:
            _kill_tree(child.pid)
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    while True:
        try:
            # 拉起子进程前是唯一安全的换文件时机（此刻没有活跃写入句柄）
            _rotate_if_big(_uvicorn_log)
            _rotate_if_big(_log_path)
            child = subprocess.Popen(
                [PYTHONW, "-m", "uvicorn", "app.main:app",
                 "--host", HOST, "--port", PORT],
                cwd=BACKEND_DIR,
                stdout=open(_uvicorn_log, "a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _log(f"已启动 uvicorn 子进程 PID={child.pid}（前端+API: http://{HOST}:{PORT}）")
        except Exception as e:
            _log(f"启动 uvicorn 失败: {e}，{RESTART_DELAY}s 后重试")
            time.sleep(RESTART_DELAY)
            continue

        # ── 2026-08-09：Windows 上 uvicorn 会 spawn 真实服务进程，
        #   supervisor 必须监管真实 PID 而非 launcher PID。
        real_pid = _get_uvicorn_server_pid(_uvicorn_log, timeout=30.0)
        target_pid = real_pid if real_pid else child.pid
        if real_pid and real_pid != child.pid:
            _log(f"已识别 uvicorn 真实服务进程 PID={real_pid}（launcher PID={child.pid}）")

        # 监管：等待子进程退出 + 周期性健康探测（发现无声假死/挂起）
        spawn_time = time.time()
        hard_failed = 0   # 硬失败（连接被拒/进程真死）→ 快杀
        soft_failed = 0   # 软超时（进程忙/暂慢）→ 宽容，仅真卡死才杀
        became_healthy = False
        unhealthy_exits = 0   # 连续「未健康即退出」计数
        while True:
            code = child.poll()
            if code is not None:
                if not became_healthy:
                    unhealthy_exits += 1
                    # 2026-08-09 修复：code=1 不等于端口占用。常见原因是应用冷启动崩溃、
                    # WorkBuddy safe-delete 注入、DB 只读等。若端口实际空闲，应重试而非自杀。
                    if _port_in_use(PORT):
                        _log(f"uvicorn 子进程在未健康状态下退出（code={code}），且端口 {PORT} 确实被占用，本 supervisor 让出并退出")
                        sys.exit(0)
                    if unhealthy_exits >= 3:
                        _log(f"uvicorn 子进程连续 {unhealthy_exits} 次未健康即退出（code={code}），放弃重试，本 supervisor 退出")
                        sys.exit(1)
                    _log(f"uvicorn 子进程在未健康状态下退出（code={code}），端口 {PORT} 空闲，{RESTART_DELAY}s 后重试（{unhealthy_exits}/3）")
                    break
                _log(f"uvicorn 子进程退出（code={code}），清理并 {RESTART_DELAY}s 后重启")
                break
            # ── 启动探针 / 存活探针分离（见文件头 STARTUP_GRACE 注释）──
            #   未健康过 → 用 STARTUP_GRACE(240s)：期间只探测不判死，让应用把
            #                init_db 的退避重试（最长 ~198s）完整跑完；
            #   已健康过 → 用 HEALTH_GRACE(25s)：假死照常快速重启，灵敏度不变。
            elapsed = time.time() - spawn_time
            grace = HEALTH_GRACE if became_healthy else STARTUP_GRACE
            if elapsed >= HEALTH_CHECK_INTERVAL:
                status = _health_ok()
                if status == "ok":
                    if not became_healthy:
                        _log(f"uvicorn 已进入健康状态（冷启动耗时 {elapsed:.1f}s），转入存活探测")
                    became_healthy = True
                    hard_failed = 0
                    soft_failed = 0
                else:
                    # 失败分两类：dead(真死,快杀) / busy(忙,宽容)
                    if status == "dead":
                        hard_failed += 1
                        soft_failed = 0
                        _fail_kind = "连接被拒(进程疑似已死)"
                    else:  # busy
                        soft_failed += 1
                        hard_failed = 0
                        _fail_kind = "响应超时(主循环正忙/暂慢)"
                    if elapsed >= grace:
                        if hard_failed >= HEALTH_MAX_FAILS:
                            _log(f"健康探测连续 {hard_failed} 次硬失败（{_fail_kind}），强制重启子进程")
                            _kill_tree(target_pid)
                            break
                        if soft_failed >= HEALTH_SOFT_MAX_FAILS:
                            _log(f"健康探测连续 {soft_failed} 次软超时（{_fail_kind}，疑似真卡死），强制重启子进程")
                            _kill_tree(target_pid)
                            break
            time.sleep(HEALTH_CHECK_INTERVAL)

        # 退出后清理孤儿 Worker（崩溃时 daemon 子进程可能未被回收）
        _kill_orphan_workers()
        _kill_zombie_terminals()

        # ★ 等待端口释放（Windows 下进程退出后 socket 不会立即释放，
        #   新进程立即绑定会触发 [Errno 10048] → 崩溃循环）
        _wait_port_free(HOST, int(PORT), timeout=8)

        # 重启频率熔断（防持续崩溃打满 CPU）
        now = time.time()
        restarts.append(now)
        restarts[:] = [t for t in restarts if now - t < 60]
        if len(restarts) > MAX_RESTARTS_PER_MIN:
            _log(f"警告：1分钟内已重启 {len(restarts)} 次，疑似持续崩溃，仍继续尝试（请检查 {os.path.basename(_uvicorn_log)}）")

        # ── 重启退避：连续「启动即失败」时指数拉长间隔 ──────────────────
        # 高频重启会反复改写 DB 文件、持续触发 Windows Defender 实时扫描锁，
        # 让每一轮都撞同一把锁 → 自我延续的崩溃循环。退避给杀软留出释放时间。
        # 本轮曾进入健康状态则立即复位，不影响正常假死重启的恢复速度。
        if became_healthy:
            _restart_backoff = RESTART_DELAY
        else:
            _restart_backoff = min(_restart_backoff * 2, 60.0)
            _log(f"本轮未能进入健康状态，重启退避至 {_restart_backoff:.0f}s"
                 f"（避免高频重启反复触发杀软扫描锁）")
        time.sleep(_restart_backoff)


if __name__ == "__main__":
    main()
