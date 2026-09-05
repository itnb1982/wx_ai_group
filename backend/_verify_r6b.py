# -*- coding: utf-8 -*-
"""第6轮补充：周期超时原文 + 按小时分布 + 扫描失败按账号 + 云端空响应"""
import re, os
from datetime import datetime
from collections import defaultdict

LOG = r'F:\WanxiangAI\backend\supervisor_uvicorn.log'
size = os.path.getsize(LOG)
start = max(0, size - 16 * 1024 * 1024)
with open(LOG, 'rb') as f:
    f.seek(start)
    raw = f.read()
lines = raw.decode('utf-8', errors='replace').split('\n')[1:]
TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

def hour(ln):
    m = TS.match(ln)
    return m.group(1)[11:13] if m else None

print('===== ① _run_cycle_with_timeout 超时原文(最近8条) =====')
hits = [ln for ln in lines if '_run_cycle_with_timeout' in ln and '| ERROR' in ln]
for ln in hits[-8:]:
    print('  ', ln.strip()[:230])
print(f'   合计 {len(hits)} 条')
byh = defaultdict(int)
for ln in hits:
    h = hour(ln)
    if h:
        byh[h] += 1
print('   按小时:', ' '.join(f'{h}时:{v}' for h, v in sorted(byh.items())))

print('\n===== ② run_cycle_for_user ERROR 原文(最近5) =====')
h2 = [ln for ln in lines if 'run_cycle_for_user' in ln and '| ERROR' in ln]
for ln in h2[-5:]:
    print('  ', ln.strip()[:230])
print(f'   合计 {len(h2)} 条')

print('\n===== ③ 持仓扫描失败 按小时 × 账号 =====')
sc = defaultdict(lambda: defaultdict(int))
for ln in lines:
    if 'get_all_positions_rescanned' in ln and ('| ERROR' in ln or '| WARNING' in ln):
        h = hour(ln)
        m = re.search(r'\] ([0-9a-f]{8}) ', ln)
        acc = m.group(1) if m else '?'
        if h:
            sc[h][acc] += 1
tot = 0
for h in sorted(sc):
    row = sc[h]
    s = sum(row.values())
    tot += s
    print(f'  {h}时 合计{s:<5} ' + ' '.join(f'{a}:{v}' for a, v in sorted(row.items(), key=lambda x: -x[1])))
print(f'  总计 {tot}')

print('\n===== ④ deepseek analyze / evaluate_exits ERROR 原文(各最近3) =====')
for anchor in ['deepseek_client:analyze', 'deepseek_client:evaluate_exits', 'debate_engine:decide']:
    hh = [ln for ln in lines if anchor in ln and '| ERROR' in ln]
    print(f'  --- {anchor}  合计 {len(hh)} ---')
    for ln in hh[-3:]:
        print('     ', ln.strip()[:210])
    bh = defaultdict(int)
    for ln in hh:
        h = hour(ln)
        if h:
            bh[h] += 1
    print('      按小时:', ' '.join(f'{h}:{v}' for h, v in sorted(bh.items())))

print('\n===== ⑤ 进程启动时间点(全部) =====')
for i, ln in enumerate(lines):
    if 'Started server process' in ln:
        for j in range(max(0, i - 6), i):
            m = TS.match(lines[j])
            if m:
                print('  ', m.group(1), '->', ln.strip()[-30:])
                break
        else:
            print('   (无时间戳) ', ln.strip()[-30:])
