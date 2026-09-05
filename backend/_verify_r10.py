# -*- coding: utf-8 -*-
"""第10轮：修正统计口径 —— mt5_closed_external 不再整类排除。
   只排除「真零值伪单」(profit==0 且 close_price==open_price)，保留真实 SL 击穿单。"""
import sqlite3, sys, io, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB = r"F:/WanxiangAI/backend/data/wx_prod.dat"
ACC = ('2877213e-e79f-4ac4-93cd-4db64730bc04','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
       '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3','3540bf33-ee40-4169-8099-7c9616406d99')
NAME = {'2877213e-e79f-4ac4-93cd-4db64730bc04':'liumanchun1(1610093299)',
        'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd':'liumanchuan2(1610097175)',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3':'liumanchun3(1610093301)',
        '3540bf33-ee40-4169-8099-7c9616406d99':'liumanchun4(1610098464)'}
ph = ','.join('?'*4)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

def perf(rows):
    n=len(rows); w=[r for r in rows if r['profit']>0]
    gp=sum(r['profit'] for r in w); gl=-sum(r['profit'] for r in rows if r['profit']<0)
    return n, (len(w)/n*100 if n else 0), gp, gl, gp-gl, (gp/gl if gl>0 else 0)

# 全量已平单（保留真实盈亏的外部平仓）
c.execute(f"""SELECT * FROM trades WHERE mt5_account_id IN ({ph})
              AND close_time IS NOT NULL""", ACC)
allrows=[dict(r) for r in c.fetchall()]
def is_fake_zero(r):
    return (r.get('exit_reason')=='mt5_closed_external' and (r.get('profit') or 0)==0
            and abs((r.get('close_price') or 0)-(r.get('open_price') or 0))<1e-9)
real=[r for r in allrows if not is_fake_zero(r)]
fake=[r for r in allrows if is_fake_zero(r)]

print("=== 口径对照 ===")
print(f"  全部已平单 {len(allrows)} | 真零值伪单(剔除) {len(fake)} | 有效样本 {len(real)}")

print("\n=== 按日绩效【新口径：含真实盈亏的外部平仓】===")
print(f"{'日期':<12}{'笔数':>6}{'胜率':>8}{'毛盈':>13}{'毛亏':>13}{'净利':>13}{'PF':>8}")
byday={}
for r in real:
    d=(r['close_time'] or '')[:10]
    byday.setdefault(d,[]).append(r)
tot=0
for d in sorted(byday):
    n,wr,gp,gl,net,pf = perf(byday[d]); tot+=net
    print(f"{d:<12}{n:>6}{wr:>7.1f}%{gp:>13.2f}{gl:>13.2f}{net:>13.2f}{pf:>8.3f}")
print(f"{'累计':<12}{'':>6}{'':>8}{'':>13}{'':>13}{tot:>13.2f}")

print("\n=== 旧口径对照（整类排除 mt5_closed_external）===")
old=[r for r in allrows if r.get('exit_reason')!='mt5_closed_external']
byday2={}
for r in old:
    byday2.setdefault((r['close_time'] or '')[:10],[]).append(r)
for d in sorted(byday2)[-3:]:
    n,wr,gp,gl,net,pf = perf(byday2[d])
    print(f"  {d} 旧口径 n={n} 胜率={wr:.1f}% 净={net:.2f} PF={pf:.3f}")

# 近 24 小时
now=datetime.datetime.now()
cut=(now-datetime.timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
cut3=(now-datetime.timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')
r24=[r for r in real if (r['close_time'] or '')>=cut]
r3 =[r for r in real if (r['close_time'] or '')>=cut3]
for label,rr in (("近24h",r24),("近3h",r3)):
    n,wr,gp,gl,net,pf = perf(rr)
    print(f"\n=== {label} 汇总 === n={n} 胜率={wr:.1f}% 毛盈={gp:.2f} 毛亏={gl:.2f} 净={net:.2f} PF={pf:.3f}")

print("\n=== 近3h 出场原因归因 ===")
by={}
for r in r3:
    k=(r.get('exit_reason') or '?')[:40]
    by.setdefault(k,[]).append(r)
for k,v in sorted(by.items(), key=lambda x: sum(t['profit'] for t in x[1])):
    n,wr,gp,gl,net,pf=perf(v)
    print(f"  {k:<42}{n:>4}笔 胜率{wr:>5.1f}% 净={net:>11.2f} 均单={net/n:>9.2f}")

print("\n=== 近3h 按账号 ===")
for a in ACC:
    v=[r for r in r3 if r['mt5_account_id']==a]
    if not v: continue
    n,wr,gp,gl,net,pf=perf(v)
    print(f"  {NAME[a]:<26} n={n:>3} 胜率={wr:>5.1f}% 净={net:>11.2f}")

print("\n=== 近3h 1.0手大单明细（方向/入场/出场/SL）===")
big=[r for r in r3 if (r.get('volume') or 0)>=0.5]
big.sort(key=lambda r: r['close_time'] or '')
for r in big:
    hit = "SL击穿" if abs((r.get('close_price') or 0)-(r.get('sl') or -1))<0.05 else "其他"
    print(f"  {r['close_time']} {NAME.get(r['mt5_account_id'],'')[:12]:<12} #{r.get('mt5_ticket')} "
          f"{r.get('action')} v={r.get('volume')} in={r.get('open_price')} out={r.get('close_price')} "
          f"sl={r.get('sl')} pnl={r.get('profit'):.2f} [{hit}] {(r.get('exit_reason') or '')[:22]}")

print("\n=== 未平单 ===")
c.execute(f"SELECT * FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL", ACC)
op=[dict(r) for r in c.fetchall()]
print(f"  未平单 {len(op)} 笔")
for r in op:
    print(f"   {r.get('open_time')} {NAME.get(r['mt5_account_id'],'')[:12]} #{r.get('mt5_ticket')} "
          f"{r.get('action')} v={r.get('volume')} in={r.get('open_price')} sl={r.get('sl')} tp={r.get('tp')}")

print("\n=== 近3h 开仓方向分布（按 open_time）===")
c.execute(f"""SELECT action, COUNT(*) n, SUM(volume) v FROM trades
              WHERE mt5_account_id IN ({ph}) AND open_time>=? GROUP BY action""", (*ACC, cut3))
for r in c.fetchall():
    print(f"   {r['action']}: {r['n']} 笔, 总手数 {r['v']}")
con.close()
