# -*- coding: utf-8 -*-
"""
万象AI 后端守护启动器（根治"后端无声死亡"）

设计：双层自愈
  外层：Windows 任务计划程序（schtasks）开机自启 + 失败重启（见 install_guard.ps1）
  内层：本脚本作为"看门狗"，拉起 uvicorn 子进程；
        - 子进程退出码非 0 / 异常退出 → 立刻重启（最多 1 秒间隔，带指数退避上限）
        - 捕获 SIGTERM 时优雅退出，不再拉起（交给外层决定是否重启）
        - 记录每次崩溃/重启到日志，便于审计

绝不出现"进程半死不活挂着"的状态：子进程一旦退出，要么立刻重启，要么看门狗退出。
"""
import os
import sys
import time
import subprocess
import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from runtime_paths import find_python, PROJECT_ROOT  # noqa: E402

# 解释器与日志路径均改为运行时推导，不写死盘符/用户名：
# 商业版整目录换机部署时，硬编码路径会让守护进程静默失效。
PY = find_python()
UVICORN_MODULE = "app.main:app"
HOST = "0.0.0.0"
PORT = "8080"

# 看门狗自身日志（与 uvicorn 分开，确保崩溃也能查到）
_LOG_DIR = PROJECT_ROOT / "data"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = str(_LOG_DIR / "wanxiang_guard.log")

_stop = {"flag": False}

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][GUARD] {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)

def _handle_term(signum, frame):
    log("收到 SIGTERM，停止拉起新进程，退出看门狗")
    _stop["flag"] = True

signal.signal(signal.SIGTERM, _handle_term)
signal.signal(signal.SIGINT, _handle_term)

def _port_in_use(port: int) -> bool:
    """检查端口是否已被占用，用于避免与新版 supervisor 双重监管。"""
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


def main():
    log("=== 万象AI 后端守护启动 ===")

    # 2026-08-09：新版 supervisor 已接管监管；若其已在运行，旧 guard 不应再
    # 拉起第二套 uvicorn，否则双重监管会互相抢端口/杀进程，导致"原地打转"。
    if _port_in_use(int(PORT)):
        log(f"端口 {PORT} 已被占用，新版 supervisor 正在运行，本旧 guard 直接退出")
        return

    env = os.environ.copy()
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    attempts = 0
    while not _stop["flag"]:
        attempts += 1
        # 指数退避（防止崩溃-重启风暴把 CPU 打满）：1s,2s,4s,8s,封顶 15s
        backoff = min(15, max(1, 2 ** (attempts - 1)))
        log(f"启动 uvicorn (第{attempts}次尝试) PORT={PORT}")
        # 2026-08-09 关键修复：必须加 CREATE_NO_WINDOW，否则用户桌面会弹出
        # 标题为 "F:\\...\\python.exe" 的黑色控制台窗口。
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", UVICORN_MODULE,
             "--host", HOST, "--port", PORT, "--workers", "1"],
            cwd=HERE, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            ),
        )
        log(f"uvicorn 子进程 PID={proc.pid} 已启动")
        rc = proc.wait()  # 阻塞直到子进程退出
        if _stop["flag"]:
            log("看门狗已停止，不再拉起")
            break
        # 每次重启前再次检查：若端口已被别人占用（supervisor 已拉起新实例），
        # 本旧 guard 让出，避免和 supervisor 互相厮杀。
        if _port_in_use(int(PORT)):
            log(f"端口 {PORT} 已被占用，新版 supervisor 已接管，本旧 guard 退出")
            break
        log(f"uvicorn 子进程异常退出 (returncode={rc})，{backoff}s 后重启")
        attempts = min(attempts + 1, 1000)
        time.sleep(backoff)
    log("=== 万象AI 后端守护退出 ===")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("KeyboardInterrupt，退出")
    sys.exit(0)
