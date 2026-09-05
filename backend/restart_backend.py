"""安全重启 uvicorn：杀掉监听 8080 的 uvicorn 进程树，supervisor 会自动拉起新进程加载新代码。
不杀 supervisor 主进程（保持监管），仅让其子进程 uvicorn 退出后重生。"""
import subprocess
import time
import sys

# 中文 Windows 的 stdout 默认是 GBK，脚本里任何非 GBK 字符（emoji、✓ 等）
# 都会在 print 时抛 UnicodeEncodeError，让一个本来成功的重启以退出码 1 收场，
# 进而误导调用方以为重启失败。先把编码钉死，再谈其它。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = "8080"


def find_listen_pids(port):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, encoding="gbk", errors="ignore", timeout=15).stdout
    except Exception as e:
        print(f"[重启] netstat 失败: {e}")
        return set()
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTEN" in line:
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    return pids


def kill_tree(pid) -> bool:
    """终止进程树。返回 True 表示确实杀成功。

    后端以任务计划程序（更高完整性级别）运行时，普通权限终端执行 taskkill
    会全量返回「拒绝访问」。这种情况必须当失败处理——否则会误报重启成功，
    让人以为新代码已生效，实际跑的还是旧进程（2026-08-09 踩过一次）。
    """
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           capture_output=True, encoding="gbk", errors="ignore", timeout=15)
        msg = (r.stdout or "").strip() or (r.stderr or "").strip()
        print(f"[重启] taskkill {pid}: {msg}")
        denied = ("拒绝访问" in msg) or ("Access is denied" in msg)
        return (r.returncode == 0) and not denied
    except Exception as e:
        print(f"[重启] kill {pid} 异常: {e}")
        return False


print("[重启] 查找监听", PORT, "的进程...")
pids = find_listen_pids(PORT)
print(f"[重启] 找到 PID: {pids}")
if not pids:
    print("[重启] 未找到监听进程，可能已停止")
    sys.exit(0)

killed_any = False
for pid in pids:
    if kill_tree(pid):
        killed_any = True

if not killed_any:
    print()
    print("[重启] ❌ 全部终止请求被拒绝——后端正以更高权限运行"
          "（任务计划程序 / 管理员），当前终端无权终止它。")
    print("[重启]    请右键「以管理员身份运行」 backend\\restart_task_backend.bat")
    sys.exit(2)

print("[重启] 已请求终止 uvicorn，supervisor 将自动重生新进程（加载新代码）...")
print("[重启] 等待 supervisor 冷启动（约 30s）...")
time.sleep(30)

# 验证：PID 必须换新。只看「端口有人监听」是不够的——
# 旧进程没被杀掉时端口同样在监听，会把「根本没重启」误报成「已重生」。
new_pids = find_listen_pids(PORT)
print(f"[重启] 重启后监听 PID: {new_pids}")
if not new_pids:
    print("[重启] ⚠️ 端口仍未监听，请检查 supervisor_uvicorn.log")
    sys.exit(1)
if new_pids == pids:
    print(f"[重启] ❌ PID 未变化（仍为 {new_pids}），旧进程未被终止，新代码没有加载。")
    print("[重启]    请右键「以管理员身份运行」 backend\\restart_task_backend.bat")
    sys.exit(2)
print(f"[重启] ✅ uvicorn 已重生（{pids} → {new_pids}），新代码已加载")
