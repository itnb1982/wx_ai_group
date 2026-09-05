# -*- coding: utf-8 -*-
"""R19 DB 只读分析：SL伪造检测 / 盈亏分布 / 出场归因 / 特征填充 / 未平单SL位置"""
import sqlite3, sys, collections
sys.stdout.reconfigure(encoding="utf-8")

DB = "file:F:/WanxiangAI/backend/data/wx_prod.dat?mode=ro"
ACC = {"2877213e-e79f-4ac4-93cd-4db64730bc04":"liumanchun1",
       "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd":"liumanchuan2",
       "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3":"liumanchun3",
       "3540bf33-ee40-4169-8099-7c9616406d99":"liumanchun4"}
ai = ",".join(f"'{a}'" for a in ACC)
con = sqlite3.connect(DB, uri=True, timeout=30); cur = con.cursor()

def fake(cp, sl):
    """SL伪造判据：close_price 与 sl 精确相等（零滑点，物理不可能）"""
    return cp is not None and sl is not None and abs(cp - sl) < 1e-9

print("="*90)
print("[1] 当前未平单（DB口径） — SL 位置与硬损距离")
rows = cur.execute(f"""SELECT mt5_ticket,mt5_account_id,action,volume,open_price,sl,tp,
       open_time,mfe,mae,chronos_vote,q_score,meta_agent_confidence,exit_reason
   FROM trades WHERE mt5_account_id IN ({ai}) AND close_time IS NULL
   ORDER BY open_time""").fetchall()
print(f"  未平单数 = {len(rows)}")
for r in rows:
    tk,aid,act,vol,op,sl,tp,ot,mfe,mae,cv,qs,mac,er = r
    slpts = (sl-op) if act and act.lower()=="sell" else (op-sl if sl else None)
    print(f"  #{tk} {ACC[aid]:13s} {act:4s} vol={vol:<5} open={op} sl={sl} tp={tp} "
          f"SL距开仓={slpts:+.2f}pt open={ot} mfe={mfe} mae={mae} chronos={cv} q={qs} meta={mac}")

print("="*90)
print("[2] 今日(08-11)已平单 — 伪造检测 + 盈亏分布")
today = cur.execute(f"""SELECT mt5_ticket,mt5_account_id,action,volume,open_price,close_price,sl,tp,
       profit,net_profit,result,exit_reason,open_time,close_time,mfe,mae
   FROM trades WHERE mt5_account_id IN ({ai}) AND close_time>='2026-08-11 00:00:00'
   ORDER BY close_time""").fetchall()
fk = [t for t in today if fake(t[5], t[6])]
zero = [t for t in today if not fake(t[5],t[6]) and (t[8] or 0)==0 and t[5]==t[4]]
ok  = [t for t in today if t not in fk and t not in zero]
print(f"  今日已平 {len(today)} 笔： SL伪造(close≡sl) {len(fk)} 笔 / 人工全平零值(close≡open) {len(zero)} 笔 / 可信 {len(ok)} 笔")
print(f"  DB全量 profit 合计 = {sum((t[8] or 0) for t in today):+.2f}  (含伪造，不可用)")
print(f"  伪造单 profit 合计 = {sum((t[8] or 0) for t in fk):+.2f}  (虚假亏损)")
print(f"  可信单 profit 合计 = {sum((t[8] or 0) for t in ok):+.2f}")
print("  -- 伪造单明细（含公式复算校验 (sl-open)*vol*100）--")
for t in fk:
    tk,aid,act,vol,op,cp,sl,tp,pf = t[0],t[1],t[2],t[3],t[4],t[5],t[6],t[7],t[8]
    est = (sl-op)*vol*100 if act.lower()=="sell" else (sl-op)*vol*100
    print(f"    #{tk} {ACC[aid]:13s} {act:4s} vol={vol:<5} open={op} close={cp} sl={sl} "
          f"DBprofit={pf:+.2f} 复算={est:+.2f} 误差={abs((pf or 0)-est):.2f} 平仓={t[13]} reason={t[11]}")
print("  -- 可信单明细 --")
for t in ok:
    print(f"    #{t[0]} {ACC[t[1]]:13s} {t[2]:4s} vol={t[3]:<5} open={t[4]} close={t[5]} "
          f"profit={(t[8] or 0):+.2f} reason={t[11]} 平仓={t[13]} mfe={t[14]} mae={t[15]}")

print("="*90)
print("[3] 今日出场原因归因（分伪造/可信）")
agg = collections.defaultdict(lambda: [0,0.0,0])   # n, sum, win
for t in today:
    key = (t[11] or "None") + ("  [伪造]" if fake(t[5],t[6]) else "")
    p = t[8] or 0
    agg[key][0]+=1; agg[key][1]+=p
    if p>0: agg[key][2]+=1
for k,v in sorted(agg.items(), key=lambda x:x[1][1]):
    wr = v[2]/v[0]*100 if v[0] else 0
    print(f"  {k:38s} n={v[0]:3d} 合计={v[1]:+11.2f} 均单={v[1]/v[0]:+9.2f} 胜率={wr:5.1f}%")

print("="*90)
print("[4] 今日按账号（剔伪造 + 剔零值）")
per = collections.defaultdict(lambda: [0,0.0,0,0.0,0.0])
for t in ok:
    p = t[8] or 0; a=ACC[t[1]]
    per[a][0]+=1; per[a][1]+=p
    if p>0: per[a][2]+=1; per[a][3]+=p
    else: per[a][4]+=abs(p)
for a,v in per.items():
    pf = v[3]/v[4] if v[4]>0 else float("inf")
    print(f"  {a:13s} n={v[0]:3d} 净={v[1]:+9.2f} 胜率={v[2]/v[0]*100 if v[0] else 0:5.1f}% PF={pf:.3f}")

print("="*90)
print("[5] 特征填充率（今日口径 / 全历史）")
for label, cond in (("今日", "close_time>='2026-08-11 00:00:00'"), ("全历史", "1=1")):
    tot = cur.execute(f"SELECT COUNT(*) FROM trades WHERE mt5_account_id IN ({ai}) AND {cond}").fetchone()[0]
    line = [f"  {label} n={tot}: "]
    for c in ("mfe","mae","chronos_vote","q_score","meta_agent_confidence","decision_snapshot"):
        n = cur.execute(f"SELECT COUNT(*) FROM trades WHERE mt5_account_id IN ({ai}) AND {cond} AND {c} IS NOT NULL AND {c}!=0 AND {c}!=''").fetchone()[0]
        line.append(f"{c}={n/tot*100 if tot else 0:.1f}%")
    print(" ".join(line))

print("="*90)
print("[6] 最近30笔已平单盈亏分布（全部账号，含标记）")
rec = cur.execute(f"""SELECT mt5_ticket,mt5_account_id,action,volume,open_price,close_price,sl,profit,exit_reason,close_time
   FROM trades WHERE mt5_account_id IN ({ai}) AND close_time IS NOT NULL
   ORDER BY close_time DESC LIMIT 30""").fetchall()
for t in rec:
    flag = "[伪造]" if fake(t[5],t[6]) else ("[零值]" if (t[7] or 0)==0 and t[5]==t[4] else "")
    print(f"  {t[9]} #{t[0]} {ACC[t[1]]:13s} {t[2]:4s} v={t[3]:<5} p={(t[7] or 0):+9.2f} {t[8] or '':28s} {flag}")

print("="*90)
print("[7] 全历史 & 近48h 汇总（剔伪造）")
allrows = cur.execute(f"""SELECT profit,close_price,sl,open_price,close_time FROM trades
   WHERE mt5_account_id IN ({ai}) AND close_time IS NOT NULL""").fetchall()
clean = [r for r in allrows if not fake(r[1],r[2])]
gp = sum(r[0] for r in clean if (r[0] or 0)>0); gl = sum(abs(r[0]) for r in clean if (r[0] or 0)<0)
print(f"  全历史 {len(allrows)} 笔，剔伪造后 {len(clean)} 笔，净={sum((r[0] or 0) for r in clean):+.2f} "
      f"PF={gp/gl if gl else 0:.3f} 胜率={sum(1 for r in clean if (r[0] or 0)>0)/len(clean)*100:.1f}%")
n_fake = len(allrows)-len(clean)
print(f"  全历史伪造单 {n_fake} 笔，虚假亏损合计 = {sum((r[0] or 0) for r in allrows if fake(r[1],r[2])):+.2f}")
con.close()
