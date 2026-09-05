# -*- coding: utf-8 -*-
"""第13轮 C：SL兜底伪造核查 + 特征填充率 + 今日出场归因 + 空仓窗口量化"""
import sqlite3, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB = r"F:/WanxiangAI/backend/data/wx_prod.dat"
ACC = ('2877213e-e79f-4ac4-93cd-4db64730bc04','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
       '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3','3540bf33-ee40-4169-8099-7c9616406d99')
NAME = {'2877213e-e79f-4ac4-93cd-4db64730bc04':'liumanchun1',
        'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd':'liumanchuan2',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3':'liumanchun3',
        '3540bf33-ee40-4169-8099-7c9616406d99':'liumanchun4'}

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()
ph = ",".join("?" * len(ACC))

print("=" * 78)
print("【1】今日(08-11) SL兜底伪造核查：close_price ≡ sl")
print("=" * 78)
cur.execute(f"""SELECT mt5_ticket,mt5_account_id,action,volume,open_price,close_price,sl,tp,
                profit,close_time,exit_reason
                FROM trades WHERE mt5_account_id IN ({ph})
                AND close_time IS NOT NULL AND date(close_time)='2026-08-11'
                ORDER BY close_time""", ACC)
rows = cur.fetchall()
fake, real = [], []
for r in rows:
    cp, sl = r['close_price'], r['sl']
    if cp is not None and sl is not None and abs(cp - sl) < 1e-9 and sl != 0:
        fake.append(r)
    else:
        real.append(r)
print(f"  今日已平 {len(rows)} 笔 | close≡sl 伪造嫌疑 {len(fake)} 笔 | 正常 {len(real)} 笔")
fs = sum(r['profit'] or 0 for r in fake)
rs = sum(r['profit'] or 0 for r in real)
print(f"  伪造单 DB 记账合计 = {fs:+.2f}")
print(f"  正常单 DB 记账合计 = {rs:+.2f}")
print(f"  DB 全日合计       = {fs+rs:+.2f}   (MT5 真值 today_profit = -1730.76)")
print("  --- 伪造单明细 ---")
for r in fake:
    print(f"   {str(r['close_time'])[11:19]} #{r['mt5_ticket']} {NAME.get(r['mt5_account_id'],'?'):<12}"
          f" {r['action']:<4} v={r['volume']:<5} in={r['open_price']:<9} close={r['close_price']:<9}"
          f" sl={r['sl']:<9} profit={r['profit']:+.2f}")

print()
print("=" * 78)
print("【2】今日特征填充率（本地 RL 训练可用性）")
print("=" * 78)
cur.execute("PRAGMA table_info(trades)")
cols = {c[1] for c in cur.fetchall()}
feats = [c for c in ['mfe','mae','chronos_vote','q_score','meta_agent_confidence',
                     'decision_snapshot','regime','atr_at_entry'] if c in cols]
n = len(rows)
for f in feats:
    cur.execute(f"""SELECT COUNT(*) FROM trades WHERE mt5_account_id IN ({ph})
                    AND close_time IS NOT NULL AND date(close_time)='2026-08-11'
                    AND {f} IS NOT NULL AND {f}!='' AND {f}!=0""", ACC)
    k = cur.fetchone()[0]
    print(f"  {f:<24} {k}/{n} = {k/n*100 if n else 0:.1f}%")

print()
print("=" * 78)
print("【3】今日出场原因归因（剔除伪造单后）")
print("=" * 78)
agg = collections.defaultdict(lambda: [0, 0, 0.0])
for r in real:
    k = (r['exit_reason'] or '-')[:40]
    agg[k][0] += 1
    if (r['profit'] or 0) > 0: agg[k][1] += 1
    agg[k][2] += r['profit'] or 0
for k, (c, w, p) in sorted(agg.items(), key=lambda x: -x[1][2]):
    print(f"  {k:<42} {c:>3}笔 胜率{w/c*100:>5.1f}% 净={p:>10.2f} 均单={p/c:>9.2f}")

print()
print("=" * 78)
print("【4】末次开仓 / 末次平仓 时间（量化空仓窗口）")
print("=" * 78)
cur.execute(f"""SELECT MAX(open_time),MAX(close_time) FROM trades
                WHERE mt5_account_id IN ({ph})""", ACC)
o, c = cur.fetchone()
print(f"  末次开仓 open_time  = {o}")
print(f"  末次平仓 close_time = {c}")

cur.execute(f"""SELECT strftime('%H',open_time) h, COUNT(*) FROM trades
                WHERE mt5_account_id IN ({ph}) AND date(open_time)='2026-08-11'
                GROUP BY h ORDER BY h""", ACC)
print("  今日开仓节奏（按小时）:", {k: v for k, v in cur.fetchall()})

print()
print("=" * 78)
print("【5】当前未平单")
print("=" * 78)
cur.execute(f"""SELECT mt5_ticket,mt5_account_id,action,volume,open_price,sl,tp,open_time
                FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL""", ACC)
u = cur.fetchall()
print(f"  DB 未平单 {len(u)} 笔")
for r in u:
    print(f"   #{r['mt5_ticket']} {NAME.get(r['mt5_account_id'],'?')} {r['action']} v={r['volume']}"
          f" in={r['open_price']} sl={r['sl']} tp={r['tp']} @{r['open_time']}")
con.close()
