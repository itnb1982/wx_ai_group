import subprocess, time, sys

# 启动前检查
print('=== 启动前 ===')
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True)
print(r.stdout.decode('gbk', errors='replace'))

# 直接用 Popen 启动 STARTRADER 52
print('\n=== 启动 STARTRADER 52 terminal64.exe ===')
p = subprocess.Popen([r'C:\Program Files\STARTRADER Financial MetaTrader 52\terminal64.exe'])
print(f'Popen PID={p.pid}')

# 等待 15 秒让终端充分启动
print('等待 15 秒...')
time.sleep(15)

# 检查所有 terminal64 进程
print('\n=== 启动后 ===')
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True)
print(r.stdout.decode('gbk', errors='replace'))

# 获取所有进程的路径
r2 = subprocess.run(['wmic', 'process', 'where', "name='terminal64.exe'",
                     'get', 'ProcessId,ExecutablePath', '/value'],
                    capture_output=True)
out = r2.stdout.decode('utf-8', errors='replace')
print('\n所有 terminal64 进程路径:')
for line in out.split('\n'):
    line = line.strip()
    if line:
        print(f'  {line}')

# 清理：终止我们启动的进程
print(f'\n终止 PID={p.pid}...')
p.terminate()
try:
    p.wait(timeout=5)
except:
    p.kill()
print('完成')
