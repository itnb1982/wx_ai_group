import sqlite3, json, statistics
from datetime import datetime
DB = "F:/WanxiangAI/data/wx_prod.dat"
LID = "2877213e-e79f-4ac4-93cd-4db64730bc04"

c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
cur = c.cursor()
rows = list(cur.execute(
    "SELECT * FROM trades WHERE mt5_account_id=? AND result NOT LIKE 'pending%' ORDER BY open_time",
    (LID,)))

def f2(x):
    return round(float(x), 2) if x is not None else 0.0

def dur_min(o, cl):
    try:
        return (datetime.fromisoformat(cl) - datetime.fromisoformat(o)).total_seconds() / 60
    except Exception:
        return 0.0

def cat(r):
    res = r['result'] or ''
    if 'AI反向' in res: return 'AI反转平仓(L2)'
    if 'L3' in res: return 'L3篮子锁利'
    if '跟号镜像' in res: return '跟号镜像'
    if 'TP1' in res or 'partial' in res: return 'TP1分批止盈(L1)'
    if 'AI出场' in res: return 'M1AI出场'
    if 'SL' in res or '止损' in res: return '止损'
    if 'TP' in res: return '止盈'
    if 'manual' in res.lower(): return '手动'
    return '其他:' + res[:20]

cats = {}
wins = losses = breakeven = 0
total = 0.0
conf_buckets = {'0-0.55': [], '0.55-0.6': [], '0.6-0.65': [], '0.65-0.7': [], '0.7+': []}
dur_buckets = {'<5m': [], '5-30m': [], '30-120m': [], '120m+': []}
action_stat = {}
loss_list = []
pnls_all = []
for r in rows:
    p = f2(r['profit']); total += p; pnls_all.append(p)
    if p > 0: wins += 1; action_stat.setdefault(r['action'], [0, 0.0, 0])[2] += 1
    elif p < 0: losses += 1; loss_list.append(r)
    else: breakeven += 1
    cc = cat(r)
    cats.setdefault(cc, {'n': 0, 'pnl': 0.0, 'w': 0, 'l': 0})
    cats[cc]['n'] += 1; cats[cc]['pnl'] += p
    if p > 0: cats[cc]['w'] += 1
    elif p < 0: cats[cc]['l'] += 1
    mc = f2(r['meta_agent_confidence'])
    b = ('0-0.55' if mc < 0.55 else '0.55-0.6' if mc < 0.6 else '0.6-0.65' if mc < 0.65
         else '0.65-0.7' if mc < 0.7 else '0.7+')
    conf_buckets[b].append(p)
    dm = dur_min(r['open_time'], r['close_time'])
    db = '<5m' if dm < 5 else '5-30m' if dm < 30 else '30-120m' if dm < 120 else '120m+'
    dur_buckets[db].append(p)
    action_stat.setdefault(r['action'], [0, 0.0, 0])[0] += 1
    action_stat[r['action']][1] += p

gross_win = sum(p for p in pnls_all if p > 0)
gross_loss = abs(sum(p for p in pnls_all if p < 0))
pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
wr = wins / len(rows) * 100
print("===== 主号(2877213e/liumanchun1) 已平仓统计 =====")
print(f"样本数={len(rows)} 盈利={wins} 亏损={losses} 平={breakeven} 胜率={wr:.1f}%")
print(f"总盈亏={f2(total)}  毛盈利={f2(gross_win)} 毛亏损={f2(gross_loss)} PF={pf:.3f}")
print(f"平均每笔={f2(total/len(rows))} 平均盈利={f2(gross_win/wins) if wins else 0} "
      f"平均亏损={f2(-gross_loss/losses) if losses else 0}")
pnls_sorted = sorted(pnls_all)
print(f"最大单笔盈利={pnls_sorted[-1]} 最大单笔亏损={pnls_sorted[0]}")
print("\n===== 按出场原因分组 =====")
for k, v in sorted(cats.items(), key=lambda x: -x[1]['pnl']):
    wr2 = v['w'] / (v['w'] + v['l']) * 100 if (v['w'] + v['l']) > 0 else 0
    print(f"  {k:18s} n={v['n']:4d} 盈亏={f2(v['pnl']):8.2f} 胜率={wr2:5.1f}% 均={f2(v['pnl']/v['n']):.2f}")
print("\n===== 按终裁置信度分组 =====")
for k, v in conf_buckets.items():
    if v: print(f"  {k:10s} n={len(v):4d} 总盈亏={f2(sum(v)):8.2f} 均={f2(sum(v)/len(v)):.2f}")
print("\n===== 按持仓时长分组 =====")
for k, v in dur_buckets.items():
    if v: print(f"  {k:8s} n={len(v):4d} 总盈亏={f2(sum(v)):8.2f} 均={f2(sum(v)/len(v)):.2f}")
print("\n===== 按方向 =====")
for k, v in action_stat.items():
    wr2 = v[2] / v[0] * 100 if v[0] else 0
    print(f"  {k:5s} n={v[0]:4d} 盈亏={f2(v[1]):8.2f} 胜率={wr2:.1f}%")
rev_loss = [f2(r['profit']) for r in loss_list if 'AI反向' in (r['result'] or '')]
rev_win = [f2(r['profit']) for r in rows if 'AI反向' in (r['result'] or '') and f2(r['profit']) > 0]
rev_all = [f2(r['profit']) for r in rows if 'AI反向' in (r['result'] or '')]
print(f"\n===== AI反转平仓 专门 =====")
print(f"  AI反转平仓 总笔数={len(rev_all)} 总盈亏={f2(sum(rev_all))} 盈利({len(rev_win)}) 亏损({len(rev_loss)})")
print(f"  AI反转亏损单合计={f2(sum(rev_loss))}  盈利单合计={f2(sum(rev_win))}")

# 可规避亏损识别：低置信开仓 + 亏损
low_conf_loss = [f2(r['profit']) for r in loss_list if f2(r['meta_agent_confidence']) < 0.6]
print(f"\n===== 可规避性 =====")
print(f"  低置信(<0.6)开仓的亏损单: {len(low_conf_loss)} 笔, 合计={f2(sum(low_conf_loss))}")
short_loss = [f2(r['profit']) for r in loss_list if dur_min(r['open_time'], r['close_time']) < 10]
print(f"  持仓<10分钟即亏损平仓: {len(short_loss)} 笔, 合计={f2(sum(short_loss))}")
# 反转平仓里亏损且持仓短(被洗)
rev_short_loss = [f2(r['profit']) for r in loss_list
                  if 'AI反向' in (r['result'] or '') and dur_min(r['open_time'], r['close_time']) < 20]
print(f"  AI反转+持仓<20分钟亏损: {len(rev_short_loss)} 笔, 合计={f2(sum(rev_short_loss))}")
c.close()
