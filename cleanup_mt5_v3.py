import subprocess, sys, os, locale

# 设置正确的编码
os.environ['PYTHONIOENCODING'] = 'utf-8'

# 用 tasklist 获取进程列表（用 bytes 模式避免编码问题）
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True)
output = r.stdout.decode('gbk', errors='replace')
lines = output.strip().split('\n')[1:]  # 跳过表头

pids = []
for line in lines:
    parts = line.split('","')
    if len(parts) >= 2:
        try:
            pid = int(parts[1].strip('"'))
            pids.append(pid)
        except:
            pass

print(f'发现 {len(pids)} 个 terminal64 进程')

# 用 wmic 获取路径和内存（bytes模式）
from collections import defaultdict
by_path = defaultdict(list)

for pid in pids:
    r2 = subprocess.run(['wmic', 'process', 'where', f'ProcessId={pid}',
                        'get', 'ExecutablePath,WorkingSetSize', '/value'],
                       capture_output=True)
    out = r2.stdout.decode('utf-8', errors='replace')
    path = None
    mem = 0
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('ExecutablePath='):
            path = line.split('=', 1)[1].strip()
        elif line.startswith('WorkingSetSize='):
            try:
                mem = int(line.split('=', 1)[1].strip())
            except:
                pass
    if path:
        by_path[path].append((pid, mem))
        print(f'  PID={pid} path={path[:65]} mem={mem//1024//1024}MB')

# 保留每个路径内存最大的一个，杀掉其余
for path, items in by_path.items():
    items.sort(key=lambda x: x[1], reverse=True)
    keep_pid = items[0][0]
    print(f'\n路径: {path[:65]}')
    print(f'  保留 PID={keep_pid}')
    for pid, mem in items[1:]:
        print(f'  杀掉 PID={pid} ... ', end='', flush=True)
        kr = subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        msg = kr.stdout.decode('gbk', errors='replace').strip() if kr.stdout else ''
        print('OK' if kr.returncode == 0 else f'FAIL({kr.returncode}): {msg[:50]}')

# 验证
print('\n清理后剩余:')
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True)
out = r.stdout.decode('gbk', errors='replace')
print(out)
