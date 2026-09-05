# -*- coding: utf-8 -*-
"""第6轮只读分析：周期拖慢归因 + 评估间隔按小时趋势 + 缓存陈旧度"""
import re, os, io
from datetime import datetime
from collections import defaultdict

LOG = r'F:\WanxiangAI\backend\supervisor_uvicorn.log'
TAIL_MB = 16

size = os.path.getsize(LOG)
start = max(0, size - TAIL_MB * 1024 * 1024)
with open(LOG, 'rb') as f:
    f.seek(start)
    raw = f.read()
text = raw.decode('utf-8', errors='replace')
lines = text.split('\n')[1:]
print(f'分析尾部 {len(raw)/1024/1024:.1f} MB / {len(lines)} 行\n')

TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

def ts_of(ln):
    m = TS.match(ln)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None

# ---------- ① evaluate_position 间隔 按小时统计 ----------
ev = []
for ln in lines:
    if 'smart_exit:evaluate_position' in ln:
        t = ts_of(ln)
        if t:
            ev.append(t)
ev.sort()
print('===== ① 持仓评估(evaluate_position) 按小时：次数 / 平均间隔 / 最大间隔 =====')
byh = defaultdict(list)
for i in range(1, len(ev)):
    gap = (ev[i] - ev[i-1]).total_seconds()
    byh[ev[i].strftime('%H')].append(gap)
print(f"{'小时':<6}{'次数':>6}{'平均间隔s':>12}{'最大间隔s':>12}")
for h in sorted(byh):
    g = byh[h]
    print(f'{h+"时":<6}{len(g):>6}{sum(g)/len(g):>12.0f}{max(g):>12.0f}')

# ---------- ② 自动交易周期耗时 ----------
print('\n===== ② 自动交易周期耗时样本(最近20条含"耗时/秒"的周期行) =====')
cyc = [ln for ln in lines if ('auto_trade' in ln or 'trading_loop' in ln or '_run_cycle' in ln)
       and ('耗时' in ln or 'elapsed' in ln or 'cost' in ln)]
for ln in cyc[-20:]:
    print('  ', ln.strip()[:190])
if not cyc:
    print('   (无匹配，改用 ASCII 锚点)')
    cyc2 = [ln for ln in lines if 'cycle' in ln.lower() and ('sec' in ln.lower() or 's]' in ln)]
    for ln in cyc2[-15:]:
        print('  ', ln.strip()[:190])

# ---------- ③ 超时 / 跳轮 ----------
print('\n===== ③ 超时 / 跳轮 / 单用户超时 按小时 =====')
pat = {
    '单用户超时': ['单用户', 'per_user_timeout', 'user timeout'],
    '跳轮': ['跳轮', 'skip cycle', 'skip_cycle'],
    'TimeoutError': ['TimeoutError', 'asyncio.exceptions.TimeoutError'],
}
cnt = defaultdict(lambda: defaultdict(int))
for ln in lines:
    t = ts_of(ln)
    if not t:
        continue
    for k, keys in pat.items():
        if any(x in ln for x in keys):
            cnt[k][t.strftime('%H')] += 1
for k in pat:
    if cnt[k]:
        tot = sum(cnt[k].values())
        detail = ' '.join(f'{h}时:{v}' for h, v in sorted(cnt[k].items()))
        print(f'  {k:<14} 合计{tot:<6} {detail}')
    else:
        print(f'  {k:<14} 0')

# ---------- ④ 慢环节：按耗时数字排序 ----------
print('\n===== ④ 尾部 ERROR 级别 TOP 模块 =====')
err = defaultdict(int)
for ln in lines:
    if '| ERROR' in ln:
        m = re.search(r'\| ERROR\s+\| ([\w\.]+:[\w_]+):(\d+)', ln)
        if m:
            err[f'{m.group(1)}:{m.group(2)}'] += 1
for k, v in sorted(err.items(), key=lambda x: -x[1])[:12]:
    print(f'  {v:>6}  {k}')

# ---------- ⑤ 最近持仓评估原始行 ----------
print('\n===== ⑤ 最近 12 条持仓评估原始时间戳 =====')
for t in ev[-12:]:
    print('  ', t.strftime('%H:%M:%S'))

# ---------- ⑥ 锁利 / SL 上移 事件 ----------
print('\n===== ⑥ 近期 SL 上移 / 锁利 事件(最近10) =====')
lock = [ln for ln in lines if ('modify_position' in ln or 'l3_tp_lock' in ln or 'breakeven' in ln
                               or 'trail' in ln.lower())]
for ln in lock[-10:]:
    print('  ', ln.strip()[:200])
print(f'   (窗口内锁利相关行数: {len(lock)})')
