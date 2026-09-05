# -*- coding: utf-8 -*-
import sqlite3, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
DB = "file:F:/WanxiangAI/backend/data/wx_prod.dat?mode=ro"
ACC = ('2877213e-e79f-4ac4-93cd-4db64730bc04','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
       '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3','3540bf33-ee40-4169-8099-7c9616406d99')
NAME = {'2877213e-e79f-4ac4-93cd-4db64730bc04':'liumanchun1','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd':'liumanchuan2',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3':'liumanchun3','3540bf33-ee40-4169-8099-7c9616406d99':'liumanchun4'}
c = sqlite3.connect(DB, uri=True); cur = c.cursor()
IN = "(" + ",".join("?"*4) + ")"

print("== 未平单 (close_time IS NULL) ==")
cur.execute(f"SELECT mt5_account_id,mt5_ticket,action,volume,open_price,sl,tp,open_time FROM trades WHERE mt5_account_id IN {IN} AND close_time IS NULL", ACC)
r = cur.fetchall()
print("  count:", len(r))
for x in r: print("   ", NAME.get(x[0]), x[1:])

print()
print("== 近 1 小时 (>= 2026-08-10 23:23) 已平单 按出场原因 ==")
cur.execute(f"""SELECT exit_reason, COUNT(*), ROUND(SUM(profit),2), ROUND(AVG(profit),2),
 SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END), SUM(CASE WHEN profit<0 THEN 1 ELSE 0 END),
 SUM(CASE WHEN profit=0 THEN 1 ELSE 0 END)
 FROM trades WHERE mt5_account_id IN {IN} AND close_time>='2026-08-10 23:23' GROUP BY exit_reason ORDER BY 3""", ACC)
for x in cur.fetchall(): print("   ", x)

print()
print("== 交易日 08-10 全量(>= 08-10 00:00) 按出场原因 ==")
cur.execute(f"""SELECT exit_reason, COUNT(*), ROUND(SUM(profit),2), ROUND(AVG(profit),2),
 SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END), SUM(CASE WHEN profit<0 THEN 1 ELSE 0 END),
 SUM(CASE WHEN profit=0 THEN 1 ELSE 0 END)
 FROM trades WHERE mt5_account_id IN {IN} AND close_time>='2026-08-10 00:00' GROUP BY exit_reason ORDER BY 3""", ACC)
tot=[0,0.0]
for x in cur.fetchall():
    print("   ", x); tot[0]+=x[1]; tot[1]+=x[2]

print()
print("== 08-10 有效单(剔除 profit=0) 总体 ==")
cur.execute(f"""SELECT COUNT(*), ROUND(SUM(profit),2),
 SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END), SUM(CASE WHEN profit<0 THEN 1 ELSE 0 END),
 ROUND(SUM(CASE WHEN profit>0 THEN profit ELSE 0 END),2), ROUND(-SUM(CASE WHEN profit<0 THEN profit ELSE 0 END),2)
 FROM trades WHERE mt5_account_id IN {IN} AND close_time>='2026-08-10 00:00' AND profit!=0""", ACC)
n,net,w,l,gp,gl = cur.fetchone()
print(f"   有效 {n} 笔 净 {net} 胜率 {round(w*100/max(n,1),1)}% 毛利 {gp} 毛亏 {gl} PF {round(gp/gl,3) if gl else 'inf'}")

print()
print("== 08-10 按账号 ==")
cur.execute(f"""SELECT mt5_account_id, COUNT(*), ROUND(SUM(profit),2) FROM trades
 WHERE mt5_account_id IN {IN} AND close_time>='2026-08-10 00:00' GROUP BY 1""", ACC)
for x in cur.fetchall(): print("   ", NAME.get(x[0]), x[1], x[2])

print()
print("== 最后 20 笔(全量,含时间) ==")
cur.execute(f"""SELECT close_time,mt5_account_id,mt5_ticket,action,volume,open_price,close_price,profit,exit_reason,sl,tp
 FROM trades WHERE mt5_account_id IN {IN} ORDER BY close_time DESC LIMIT 20""", ACC)
for x in cur.fetchall():
    print(f"   {x[0]} {NAME.get(x[1])} #{x[2]} {x[3]} v={x[4]} in={x[5]} out={x[6]} pnl={x[7]} sl={x[9]} tp={x[10]} r={x[8]}")

print()
print("== 08-10 大单(volume>=0.5) 出场统计 ==")
cur.execute(f"""SELECT exit_reason, COUNT(*), ROUND(SUM(profit),2) FROM trades
 WHERE mt5_account_id IN {IN} AND close_time>='2026-08-10 00:00' AND volume>=0.5 GROUP BY 1 ORDER BY 3""", ACC)
for x in cur.fetchall(): print("   ", x)
c.close()
