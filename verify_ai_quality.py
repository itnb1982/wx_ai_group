# -*- coding: utf-8 -*-
"""AI大脑质量交叉验证 v2：真实盈亏来自 ai_activities(close/close_partial)，
AI决策上下文来自 trades(debate_summary/meta_agent)，按 ticket 关联。
当前持仓明细单独从 trades 表按 ticket 取。"""
import sqlite3, re, json, datetime
from collections import defaultdict, Counter

DB = r"F:/WanxiangAI/data/wx_prod.dat"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); cur = c.cursor()

# ---- 1. 真实盈亏：ai_activities close/close_partial ----
cur.execute("SELECT mt5_account_id, direction, detail, created_at FROM ai_activities WHERE kind IN ('close','close_partial')")
closes = []
pnl_by_ticket = defaultdict(float)
for acc, dirc, detail, ts in cur.fetchall():
    m = re.search(r'平仓\s+(\d+)\s+(.*?)\s*盈亏\s*([−\-+]?)\s*([\d.]+)', detail or '')
    if not m:
        # 兜底：直接找 盈亏
        m2 = re.search(r'盈亏\s*([−\-+]?)\s*([\d.]+)', detail or '')
        if not m2: continue
        ticket=None; sign=m2.group(1); val=float(m2.group(2))
    else:
        ticket=m.group(1); sign=m.group(3); val=float(m.group(4))
    val = -val if sign in ('-','−') else val
    closes.append({'acc':acc,'dir':dirc,'ticket':ticket,'pnl':val,'ts':ts,'detail':detail})
    if ticket: pnl_by_ticket[ticket]+=val

# ---- 2. trade_review：DS/HY 决策对错 ----
cur.execute("SELECT subject, delta, reason FROM evolution_logs WHERE kind='trade_review'")
review={}
for subj, delta, reason in cur.fetchall():
    mt=re.search(r'#(\d+)', subj or '')
    ticket=mt.group(1) if mt else None
    dm=re.search(r'DS=([✓✗·])\((BUY|SELL|HOLD)\)', reason or '')
    hm=re.search(r'HY=([✓✗·])\((BUY|SELL|HOLD)\)', reason or '')
    pm=re.search(r'盈亏\s*([−\-+]?)\s*([\d.]+)', reason or '')
    pnl = -float(pm.group(2)) if pm and pm.group(1) in ('-','−') else (float(pm.group(2)) if pm else None)
    review[ticket]={'ds_mark':dm.group(1) if dm else None,'ds_dir':dm.group(2) if dm else None,
                    'hy_mark':hm.group(1) if hm else None,'hy_dir':hm.group(2) if hm else None,'pnl':pnl}

# ---- 3. trades 决策上下文 ----
cur.execute("SELECT mt5_ticket, mt5_account_id, action, open_price, sl, tp, mfe, mae, exit_reason, debate_summary, meta_agent_decision, meta_agent_confidence, open_time, close_time FROM trades")
trades_idx={}
for r in cur.fetchall():
    t=r[0]
    ds=r[9] or ''
    regime=re.search(r'体制[:：]\s*([^\s|]+)', ds)
    cons=re.search(r'共识[:：]\s*([^\s|]+)', ds)
    risk=re.search(r'风险[:：]\s*([^\s|()]+)', ds)
    trades_idx[t]={'acc':r[1],'action':r[2],'open_price':r[3],'sl':r[4],'tp':r[5],'mfe':r[6],'mae':r[7],
                   'exit_reason':r[8],'regime':regime.group(1) if regime else None,
                   'consensus':cons.group(1) if cons else None,'risk':risk.group(1) if risk else None,
                   'meta':r[10],'conf':r[11],'open_time':r[12],'close_time':r[13].__str__() if r[13] else None}

# ---- 4. 合并：每笔平仓订单 = 真实盈亏 + 决策上下文 ----
merged=[]
unmatched_pnl=0.0
for cl in closes:
    tk=cl['ticket']
    t=trades_idx.get(tk,{})
    rv=review.get(tk,{})
    merged.append({
        'ticket':tk,'acc':cl['acc'],'dir':cl['dir'],'pnl':cl['pnl'],'ts':cl['ts'],
        'action':t.get('action'),'regime':t.get('regime'),'consensus':t.get('consensus'),
        'risk':t.get('risk'),'meta':t.get('meta'),'conf':t.get('conf'),
        'mfe':t.get('mfe'),'mae':t.get('mae'),'exit_reason':t.get('exit_reason'),
        'ds_mark':rv.get('ds_mark'),'ds_dir':rv.get('ds_dir'),
        'hy_mark':rv.get('hy_mark'),'hy_dir':rv.get('hy_dir'),
    })

def is_win(x): return x['pnl']>0
def is_loss(x): return x['pnl']<0
N=len(merged)
wins=[x for x in merged if is_win(x)]; losses=[x for x in merged if is_loss(x)]
total=sum(x['pnl'] for x in merged)
gw=sum(x['pnl'] for x in wins); gl=abs(sum(x['pnl'] for x in losses))
pf=gw/gl if gl else float('inf')
wr=len(wins)/N*100
# 权益曲线/回撤
mc=sorted([x for x in merged if x['ts']], key=lambda x:x['ts'])
eq=0.0;pk=0.0;mdd=0.0
for x in mc:
    eq+=x['pnl']; pk=max(pk,eq); mdd=min(mdd,eq-pk)
cl_c=0;maxcl=0
for x in mc:
    if is_loss(x): cl_c+=1; maxcl=max(maxcl,cl_c)
    else: cl_c=0

# 每账号
amap={}
cur.execute("SELECT id,name,balance FROM mt5_accounts")
for rid,name,bal in cur.fetchall(): amap[rid]=(name,bal)
per_acct={}
agg=defaultdict(list)
for x in merged: agg[x['acc']].append(x)
for acc,xs in agg.items():
    w=[x for x in xs if is_win(x)]; l=[x for x in xs if is_loss(x)]
    g=sum(x['pnl'] for x in w); gl2=abs(sum(x['pnl'] for x in l))
    per_acct[acc]={'name':amap.get(acc,(acc,0))[0],'balance':amap.get(acc,(acc,0))[1],
        'n':len(xs),'wr':len(w)/len(xs)*100,'net':sum(x['pnl'] for x in xs),
        'pf':(g/gl2 if gl2 else float('inf')),'avg_win':g/len(w) if w else 0,'avg_loss':gl2/len(l) if l else 0}

# 体制x方向
def buck(f):
    d=defaultdict(lambda:{'n':0,'w':0,'net':0.0,'gw':0.0,'gl':0.0})
    for x in merged:
        k=f(x)
        if k is None: continue
        b=d[k]; b['n']+=1
        if is_win(x): b['w']+=1; b['gw']+=x['pnl']
        elif is_loss(x): b['gl']+=abs(x['pnl'])
        b['net']+=x['pnl']
    for k,b in d.items():
        b['wr']=b['w']/b['n']*100 if b['n'] else 0
        b['pf']=b['gw']/b['gl'] if b['gl'] else float('inf')
    return d
regime_dir=buck(lambda x:(x['regime'],x['action']))
regime_all=buck(lambda x:x['regime'])
cons_all=buck(lambda x:x['consensus'])
meta_all=buck(lambda x:x['meta'])
# DS 决策对错 vs 盈亏
ds_corr=defaultdict(lambda:{'n':0,'w':0,'net':0.0})
for x in merged:
    if not x['ds_mark']: continue
    k=x['ds_mark']  # ✓/✗/·
    d=ds_corr[k]; d['n']+=1
    if is_win(x): d['w']+=1
    d['net']+=x['pnl']
for k,d in ds_corr.items(): d['wr']=d['w']/d['n']*100 if d['n'] else 0

# 提前砍：exit_reason 含 AI反向/AI出场 且 mfe>0 但净小/亏
prem=[x for x in merged if x['exit_reason'] and ('AI反向' in x['exit_reason'] or 'AI出场' in x['exit_reason']) and (x['mfe'] or 0)>0 and x['pnl']<=0]
# L3锁利 / 跟号 属于规则止盈，非砍仓
rule_close=[x for x in merged if x['exit_reason'] and ('L3' in x['exit_reason'] or '跟号' in x['exit_reason'])]
# 日期切片（按平仓时间）
pre=[x for x in mc if x['ts'] and x['ts'][:10]<'2026-08-05']
post=[x for x in mc if x['ts'] and x['ts'][:10]>='2026-08-05']
def st(xs):
    if not xs: return None
    w=[x for x in xs if is_win(x)]; l=[x for x in xs if is_loss(x)]
    g=sum(x['pnl'] for x in w); gl2=abs(sum(x['pnl'] for x in l))
    return {'n':len(xs),'wr':len(w)/len(xs)*100,'net':sum(x['pnl'] for x in xs),'pf':(g/gl2 if gl2 else float('inf'))}
c.close()

# 出场原因分布(来自 trades 表 result 字段)
exit_c = Counter()
cur2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); cu2=cur2.cursor()
cu2.execute("SELECT result FROM trades")
for (r,) in cu2.fetchall():
    if r: exit_c[r]+=1
cur2.close()

def fj(d): return {k:(round(v,2) if isinstance(v,float) else v) for k,v in d.items()}
out={
 'N':N,'total':round(total,2),'wr':round(wr,2),'pf':round(pf,3) if pf!=float('inf') else None,
 'gw':round(gw,2),'gl':round(gl,2),'avg_win':round(gw/len(wins),2) if wins else 0,'avg_loss':round(gl/len(losses),2) if losses else 0,
 'mdd':round(mdd,2),'maxcl':maxcl,
 'per_acct':{k:fj(v) for k,v in per_acct.items()},
 'regime_dir':{f"{k[0]}|{k[1]}":fj(v) for k,v in regime_dir.items()},
 'regime_all':{k:fj(v) for k,v in regime_all.items()},
 'cons_all':{k:fj(v) for k,v in cons_all.items()},
 'meta_all':{k:fj(v) for k,v in meta_all.items()},
 'ds_corr':{k:fj(v) for k,v in ds_corr.items()},
 'prem_n':len(prem),'rule_close_n':len(rule_close),
 'pre':st(pre),'post':st(post),
 'exit_c':dict(exit_c),
 'ts_range':[mc[0]['ts'],mc[-1]['ts']] if mc else None,
}
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open('aiq_clean.json','w',encoding='utf-8'), ensure_ascii=False, indent=1)
print("\n==== 概要 ====")
print(f"已平仓订单(真实盈亏) {N} 笔 | 总盈亏 {total:,.2f} | 胜率 {wr:.1f}% | PF {pf:.2f} | 均赢 {gw/len(wins):.2f}/均亏 {gl/len(losses):.2f} | 回撤 {mdd:,.2f} | 最大连亏 {maxcl}")
print("DS决策标记胜率:", {k:f"{v['wr']:.1f}%(n={v['n']})" for k,v in ds_corr.items()})
print("体制x方向 (净盈亏排序):")
for k,v in sorted(regime_dir.items(), key=lambda x:-x[1]['net']):
    print(f"  {str(k[0]):12}|{str(k[1]):4} n={v['n']:4} 胜率={v['wr']:5.1f}% PF={v['pf']:.2f} 净={v['net']:9.1f}")
print(f"\n提前砍(AI反向/AI出场且mfe>0净≤0): {len(prem)} | 规则止盈(L3/跟号): {len(rule_close)}")
print("升级前(<8/5):", st(pre))
print("升级后(>=8/5):", st(post))
