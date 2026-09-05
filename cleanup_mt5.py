import subprocess, sys

# 获取所有 terminal64 进程
result = subprocess.run(['wmic', 'process', 'where', "name='terminal64.exe'", 'get', 'ProcessId,ExecutablePath', '/value'],
                       capture_output=True, text=True)
lines = result.stdout.strip().split('\n')

procs = []
current = {}
for line in lines:
    line = line.strip()
    if not line:
        if current:
            procs.append(current)
            current = {}
        continue
    if '=' in line:
        k, v = line.split('=', 1)
        current[k] = v

if current:
    procs.append(current)

print(f'发现 {len(procs)} 个 terminal64 进程')

# 按路径分组，保留每个路径内存最大的一个
from collections import defaultdict
by_path = defaultdict(list)

for p in procs:
    pid = int(p.get('ProcessId', 0))
    path = p.get('ExecutablePath', '')
    if pid and path:
        # 获取内存
        try:
            r = subprocess.run(['wmic', 'process', 'where', f"ProcessId={pid}", 'get', 'WorkingSetSize', '/value'],
                              capture_output=True, text=True)
            mem = 0
            for l in r.stdout.split('\n'):
                if 'WorkingSetSize=' in l:
                    mem = int(l.split('=', 1)[1].strip())
                    break
        except:
            mem = 0
        by_path[path].append((pid, mem))

# 决定杀哪些：保留每个路径内存最大的，杀掉其他的
to_kill = []
keep = []
for path, items in by_path.items():
    items.sort(key=lambda x: x[1], reverse=True)
    keep.append(items[0])
    to_kill.extend(items[1:])

print(f'\n保留 {len(keep)} 个:')
for pid, mem in keep:
    print(f'  PID={pid} mem={mem//1024//1024}MB')

print(f'\n杀掉 {len(to_kill)} 个重复实例:')
for pid, mem in to_kill:
    print(f'  PID={pid} mem={mem//1024//1024}MB')
    subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)

print('\n清理完成')

# 再次列出
result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq terminal64.exe'], capture_output=True, text=True)
print(result.stdout)
