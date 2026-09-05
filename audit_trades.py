import sqlite3, json
from datetime import datetime

DB = "F:/WanxiangAI/data/wx_prod.dat"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# 1) schema
print("===== trades 表结构 =====")
for r in c.execute("PRAGMA table_info(trades)"):
    print(f"  {r['name']:<28} {r['type']}")

# 2) 账号清单
print("\n===== 账号清单(按成交数) =====")
accs = list(c.execute(
    "SELECT mt5_account_id, COUNT(*) n, MIN(open_time) first_t, MAX(close_time) last_t "
    "FROM trades GROUP BY mt5_account_id ORDER BY n DESC"))
acc_ids = [a['mt5_account_id'] for a in accs]
for a in accs:
    print(f"  {a['mt5_account_id']}  n={a['n']}  {a['first_t']} ~ {a['last_t']}")

def f2(x):
    try: return round(float(x), 2)
    except: return 0.0

def dur_min(o, cl):
    try: return (datetime.fromisoformat(cl) - datetime.fromisoformat(o)).total_seconds()/60
    except: return 0.0

def cat(res):
    res = res or ''
    if 'AI反向' in res: return 'AI反转(L2)'
    if 'L3' in res: return 'L3篮子锁利'
    if '跟号' in res: return '跟号镜像'
    if 'TP1' in res or 'partial' in res: return 'TP1分批(L1)'
    if 'AI出场' in res: return 'M1AI出场'
    if 'SL' in res or '止损' in res: return '止损'
    if 'TP' in res: return '止盈'
    if 'manual' in res.lower(): return '手动'
    return '其他:' + res[:18]

for LID in acc_ids:
    rows = list(c.execute(
        "SELECT * FROM trades WHERE mt5_account_id=? AND result NOT LIKE 'pending%' "
        "ORDER BY open_time", (LID,)))
    if not rows:
        print(f"\n##### 账号 {LID}: 无已平仓样本")
        continue
    wins = losses = be = 0
    total = 0.0; pnls = []
    cats = {}; conf_b = {'<0.55': [], '0.55-0.6': [], '0.6-0.65': [], '0.65-0.7': [], '>=0.7': []}
    dur_b = {'<5m': [], '5-30m': [], '30-120m': [], '>120m': []}
    dirs = {}; loss_list = []
    for r in rows:
        p = f2(r['profit']); total += p; pnls.append(p)
        if p > 0: wins += 1
        elif p < 0: losses += 1; loss_list.append(r)
        else: be += 1
        cc = cat(r['result'])
        d = cats.setdefault(cc, {'n':0,'pnl':0.0,'w':0,'l':0}); d['n']+=1; d['pnl']+=p
        if p>0: d['w']+=1
        elif p<0: d['l']+=1
        mc = f2(r['meta_agent_confidence'])
        b = '<0.55' if mc<0.55 else '0.55-0.6' if mc<0.6 else '0.6-0.65' if mc<0.65 else '0.65-0.7' if mc<0.7 else '>=0.7'
        conf_b[b].append(p)
        dm = dur_min(r['open_time'], r['close_time'])
        db = '<5m' if dm<5 else '5-30m' if dm<30 else '30-120m' if dm<120 else '>120m'
        dur_b[db].append(p)
        act = r['action'] or '?'
        dd = dirs.setdefault(act, {'n':0,'pnl':0.0,'w':0,'l':0}); dd['n']+=1; dd['pnl']+=p
        if p>0: dd['w']+=1
        elif p<0: dd['l']+=1
    gw = sum(p for p in pnls if p>0); gl = abs(sum(p for p in pnls if p<0))
    pf = gw/gl if gl>0 else float('inf'); wr = wins/len(rows)*100
    pnls_s = sorted(pnls)
    print(f"\n##### 账号 {LID} (样本={len(rows)}) #####")
    print(f"  胜率={wr:.1f}%  总盈亏={f2(total)}  PF={pf:.3f}  均笔={f2(total/len(rows))}")
    print(f"  盈利={wins} 亏损={losses} 平={be}  最大盈={pnls_s[-1]} 最大亏={pnls_s[0]}")
    print("  -- 按出场原因 --")
    for k,v in sorted(cats.items(), key=lambda x:-x[1]['pnl']):
        w2 = v['w']/(v['w']+v['l'])*100 if (v['w']+v['l']) else 0
        print(f"    {k:14s} n={v['n']:4d} 盈亏={f2(v['pnl']):8.2f} 胜率={w2:5.1f}%")
    print("  -- 按终裁置信度 --")
    for k,v in conf_b.items():
        if v: print(f"    {k:9s} n={len(v):4d} 盈亏={f2(sum(v)):8.2f} 均={f2(sum(v)/len(v)):.2f}")
    print("  -- 按时长 --")
    for k,v in dur_b.items():
        if v: print(f"    {k:7s} n={len(v):4d} 盈亏={f2(sum(v)):8.2f} 均={f2(sum(v)/len(v)):.2f}")
    print("  -- 按方向 --")
    for k,v in dirs.items():
        w2 = v['w']/v['n']*100 if v['n'] else 0
        print(f"    {k:5s} n={v['n']:4d} 盈亏={f2(v['pnl']):8.2f} 胜率={w2:.1f}%")
    # 不合理信号
    low_loss = [f2(r['profit']) for r in loss_list if f2(r['meta_agent_confidence'])<0.6]
    short_loss = [f2(r['profit']) for r in loss_list if dur_min(r['open_time'],r['close_time'])<10]
    rev_loss = [f2(r['profit']) for r in loss_list if 'AI反向' in (r['result'] or '')]
    revers = [r for r in rows if 'AI反向' in (r['result'] or '')]
    rev_pnl = sum(f2(r['profit']) for r in revers)
    print(f"  >> 不合理信号: 低置信(<0.6)开仓亏损={len(low_loss)}笔 合计={f2(sum(low_loss))} | "
          f"持仓<10m即亏={len(short_loss)}笔 合计={f2(sum(short_loss))} | "
          f"AI反转平仓={len(revers)}笔 合计={f2(rev_pnl)}")
    # Top10 亏损单明细
    top = sorted(loss_list, key=lambda r: f2(r['profit']))[:10]
    print("  >> Top10 亏损单(开→平 方向 置信 盈亏 原因):")
    for r in top:
        print(f"    {r['open_time']}->{r['close_time']} {r['action']:4s} conf={f2(r['meta_agent_confidence'])} "
              f"pnl={f2(r['profit'])} {cat(r['result'])} | {(r['result'] or '')[:40]}")

c.close()
