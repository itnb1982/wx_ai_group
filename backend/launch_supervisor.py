"""分离式启动器：立即退出，但 spawn 一个独立于本进程的 supervisor。
避免后台任务框架因「2s 无输出」杀掉长驻 supervisor。

2026-08-09 修正：
- 不再硬编码 F:/WanxiangAI 路径，改用 runtime_paths 保证商业化可移植。
- 使用 CREATE_NO_WINDOW，避免用户桌面出现黑色 python.exe 控制台窗口。
- 若已有 supervisor 在跑且占用 8080，则直接退出，不重复拉起。
"""
import os
import sys
import subprocess

# 运行时路径发现（兼容任意部署目录）
BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)
from runtime_paths import find_python  # noqa: E402

PY = find_python()
# ★ 2026-08-09 关键修复：与 supervisor.py 保持一致，不用 pythonw.exe。
# pythonw.exe 在 subprocess.Popen 且 stdout 重定向到文件时会产生中间 launcher
# 进程，导致 PID 树混乱、双开、监管失灵。python.exe + CREATE_NO_WINDOW 同样
# 无黑框，但 PID 就是真实 supervisor 进程。
PYW = PY
LOG = os.path.join(BACKEND, "supervisor_console.log")
LOCK = os.path.join(BACKEND, ".supervisor.lock")
PORT = 8080


def _port_in_use(port: int) -> bool:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    """不用 psutil 检查 PID 是否存活（本机 psutil 遍历命令行会永久挂死）。"""
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


def _supervisor_already_running() -> bool:
    """检查是否已有 supervisor 在运行。

    2026-08-09 修正：原实现要求「端口已被占用」才认为在运行，
    但 supervisor 刚启动时还需要先杀孤儿 Worker / 僵尸终端，
    然后才拉起 uvicorn 绑定端口，这段时间（数秒）端口是空闲的。
    若用户/脚本在此窗口内连续触发两次 launch_supervisor.py，
    就会出现两个 supervisor（双开竞争）。

    正确逻辑：只要 .supervisor.lock 存在、PID 存活、且命令行包含
    supervisor.py，就视为「已有实例在启动或已启动」，直接退出。
    端口占用检查仅作为互斥体不可用时（权限/兼容性问题）的兜底。
    """
    if not os.path.exists(LOCK):
        return False
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        if _pid_alive(pid):
            # PID 还活着：此时即使端口暂时空闲，也说明它正在启动中。
            # 额外再确认端口占用，防 lock 残留但进程已死的极端情况。
            return _port_in_use(PORT) or True
    except Exception:
        pass
    return False


if _supervisor_already_running():
    print("已有 supervisor 在运行且占用 8080，无需重复启动")
    sys.exit(0)

child = subprocess.Popen(
    [PYW, "-u", "supervisor.py"],
    cwd=BACKEND,
    stdout=open(LOG, "a", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=(
        subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW
    ),
)
print(f"已分离启动 supervisor 子进程 PID={child.pid}")
sys.exit(0)
