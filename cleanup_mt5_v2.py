import subprocess, re, sys
from collections import defaultdict

# 用 tasklist 获取进程列表
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True, text=True)
lines = r.stdout.strip().split('\n')[1:]  # 跳过表头

pids = []
for line in lines:
    parts = line.split('","')
    if len(parts) >= 2:
        try:
            pid = int(parts[1].strip('"'))
            pids.append(pid)
        except:
            pass

print(f'tasklist 发现 {len(pids)} 个 terminal64 进程: {pids}')

# 用 wmic 获取每个进程的路径和内存
by_path = defaultdict(list)
for pid in pids:
    r = subprocess.run(['wmic', 'process', 'where', f'ProcessId={pid}',
                        'get', 'ExecutablePath,WorkingSetSize', '/value'],
                       capture_output=True, text=True)
    path = None
    mem = 0
    for line in r.stdout.split('\n'):
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
        print(f'  PID={pid} path={path[:70]} mem={mem//1024//1024}MB')

# 保留每个路径内存最大的一个，杀掉其余
print(f'\n按路径分组:')
for path, items in by_path.items():
    items.sort(key=lambda x: x[1], reverse=True)
    print(f'  {path[:70]}: {len(items)} 个')
    keep_pid, keep_mem = items[0]
    print(f'    保留 PID={keep_pid} ({keep_mem//1024//1024}MB)')
    for pid, mem in items[1:]:
        print(f'    杀掉 PID={pid} ({mem//1024//1024}MB)...', end='')
        kr = subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True, text=True)
        print(' OK' if kr.returncode == 0 else f' FAIL: {kr.stderr.strip()}')

# 验证
print('\n清理后:')
r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe', '/FO', 'CSV'],
                   capture_output=True, text=True)
print(r.stdout)
