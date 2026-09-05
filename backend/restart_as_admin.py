"""
以管理员权限重启后端服务
"""
import subprocess
import sys
import os
import time

# 获取当前目录
current_dir = os.path.dirname(os.path.abspath(__file__))

# 终止旧的 supervisor 进程（PID 11640）
print("正在终止旧的后端服务 (PID 11640)...")
try:
    subprocess.run(
        ["taskkill", "/F", "/PID", "11640"],
        check=True,
        capture_output=True
    )
    print("✓ 旧服务已停止")
except subprocess.CalledProcessError as e:
    print(f"✗ 停止失败: {e}")
    print("提示：请右键 VS Code → 以管理员身份运行，然后重试")
    sys.exit(1)

# 等待进程完全退出
time.sleep(2)

# 重新启动 supervisor
print("正在重新启动后端服务...")
uvicorn_script = os.path.join(current_dir, "supervisor_uvicorn.bat")
if os.path.exists(uvicorn_script):
    subprocess.Popen(
        ["cmd", "/C", uvicorn_script],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    print("✓ 后端服务正在启动...")
    time.sleep(5)
    
    # 检查是否启动成功
    import socket
    if socket.socket().connect_ex(("127.0.0.1", 8080)) == 0:
        print("✓ 后端服务已成功启动，监听端口 8080")
    else:
        print("✗ 后端服务可能启动失败，请检查日志")
else:
    print(f"✗ 找不到启动脚本: {uvicorn_script}")
    sys.exit(1)
