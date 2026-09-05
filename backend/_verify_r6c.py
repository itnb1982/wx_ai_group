# -*- coding: utf-8 -*-
"""第6轮补充C：行情快照(Chronos/裁决/ATR/regime) + 今日锁利事件 + 未平单实盘对账"""
import re, os, json, urllib.request
from datetime import datetime

LOG = r'F:\WanxiangAI\backend\supervisor_uvicorn.log'
size = os.path.getsize(LOG)
with open(LOG, 'rb') as f:
    f.seek(max(0, size - 10 * 1024 * 1024))
    raw = f.read()
lines = raw.decode('utf-8', errors='replace').split('\n')[1:]

print('===== ① Chronos 预测原始行(最近6) =====')
ch = [ln for ln in lines if 'chronos' in ln.lower() and ('P90' in ln or 'P10' in ln or 'p90' in ln)]
for ln in ch[-6:]:
    print('  ', ln.strip()[:220])
print(f'   合计 {len(ch)}')

print('\n===== ② MetaAgent 裁决(最近6) =====')
mt = [ln for ln in lines if 'meta_agent:adjudicate' in ln]
for ln in mt[-6:]:
    print('  ', ln.strip()[:220])

print('\n===== ③ ATR / regime 行(最近5) =====')
at = [ln for ln in lines if ('ATR' in ln or 'regime' in ln.lower()) and 'INFO' in ln]
for ln in at[-5:]:
    print('  ', ln.strip()[:220])

print('\n===== ④ 今日 SL 上移/锁利 事件计数 =====')
for anchor in ['modify_position', 'l3_tp_lock', 'set_sl', 'update_sl', 'trailing', '_lock']:
    n = sum(1 for ln in lines if anchor in ln)
    print(f'   {anchor:<18} {n}')

print('\n===== ⑤ 未平单 实盘 SL/TP 对账（强制新登录）=====')
base = 'http://127.0.0.1:8080'
body = json.dumps({"email": "1558895@qq.com", "password": "Tzhl@708090"}).encode()
r = urllib.request.urlopen(urllib.request.Request(
    base + '/api/auth/login', data=body, headers={'Content-Type': 'application/json'}), timeout=15)
tok = json.loads(r.read())['access_token']
h = {'Authorization': 'Bearer ' + tok}
d = json.loads(urllib.request.urlopen(urllib.request.Request(
    base + '/api/dashboard/accounts', headers=h), timeout=30).read())
print('   cache_age_sec =', d.get('cache_age_sec'))
zero_tp = []
n = 0
for a in d['accounts']:
    for p in a['positions']:
        n += 1
        if not p.get('tp'):
            zero_tp.append(p['ticket'])
print(f'   实盘未平单 {n} 笔，TP 为 0 的: {zero_tp if zero_tp else "无"}')
print('   portfolio:', d['portfolio'])
