import subprocess
import sys

# 启动后端服务
print("正在启动后端服务...")
proc = subprocess.Popen(
    ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--reload"],
    cwd="F:/WanxiangAI/backend",
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# 等待启动
print("等待后端启动...")
import time
time.sleep(5)

# 检查是否启动成功
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
result = sock.connect_ex(('127.0.0.1', 8080))
sock.close()

if result == 0:
    print("✅ 后端服务启动成功！")
    print("请访问 http://127.0.0.1:8080")
else:
    print("❌ 后端服务启动失败")
    stdout, stderr = proc.communicate()
    print("STDOUT:", stdout)
    print("STDERR:", stderr)
