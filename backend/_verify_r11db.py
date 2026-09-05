# -*- coding: utf-8 -*-
"""第11轮 DB：只读统计（真实4账号），区分真零值伪单 / SL伪造单 / 可信单"""
import sqlite3, sys, io, datetime, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB = r"F:/WanxiangAI/backend/data/wx_prod.dat"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

ACC = {
    '2877213e-e79f-4ac4-93cd-4db64730bc04': 'A(2877213e)',
    'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd': 'B(b3db40fd)',
    '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3': 'C(8ecb1ff9)',
    '3540bf33-ee40-4169-8099-7c9616406d99': 'D(3540bf33)',
}
IN = "(" + ",".join(f"'{k}'" for k in ACC) + ")"

print("=" * 78)
print("【A. 当前未平单 close_time IS NULL】")
cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,sl,tp,profit,
                       open_time,exit_reason,mfe,mae
                FROM trades WHERE mt5_account_id IN {IN} AND close_time IS NULL
                ORDER BY open_time DESC LIMIT 40""")
rows = cur.fetchall()
print(f"未平单数 = {len(rows)}")
for r in rows:
    print(f"  {ACC[r['mt5_account_id']]} #{r['mt5_ticket']} {r['action']} vol={r['volume']} "
          f"open={r['open_price']} sl={r['sl']} tp={r['tp']} open_time={r['open_time']}")

print("\n" + "=" * 78)
print("【B. 近3小时已平单明细（判伪造）】")
cut = (datetime.datetime.now() - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,close_price,sl,tp,
                       profit,exit_reason,open_time,close_time,mfe,mae
                FROM trades WHERE mt5_account_id IN {IN} AND close_time >= ?
                ORDER BY close_time DESC""", (cut,))
rows = cur.fetchall()
print(f"近3h 已平单 = {len(rows)}  (cut={cut})")

zero_fake = []   # profit==0 且 close==open
sl_fake = []     # close_price == sl 精确相等（第10轮发现的伪造形态）
good = []
for r in rows:
    p = r['profit'] or 0.0
    op, cp, sl = r['open_price'], r['close_price'], r['sl']
    if abs(p) < 1e-9 and cp is not None and op is not None and abs(cp - op) < 1e-9:
        zero_fake.append(r)
    elif sl and cp and abs(cp - sl) < 1e-6:
        sl_fake.append(r)
    else:
        good.append(r)

print(f"  真零值伪单(close==open,profit=0) : {len(zero_fake)}")
print(f"  SL伪造嫌疑单(close_price==sl)     : {len(sl_fake)}  合计profit={sum(r['profit'] or 0 for r in sl_fake):.2f}")
print(f"  可信单                            : {len(good)}  合计profit={sum(r['profit'] or 0 for r in good):.2f}")

print("\n-- SL伪造嫌疑单明细(最多20) --")
for r in sl_fake[:20]:
    print(f"  {r['close_time']} {ACC[r['mt5_account_id']]} #{r['mt5_ticket']} {r['action']} "
          f"vol={r['volume']} open={r['open_price']} close={r['close_price']} sl={r['sl']} "
          f"profit={r['profit']:.2f} exit={r['exit_reason']}")

print("\n-- 可信单明细(最多30) --")
for r in good[:30]:
    print(f"  {r['close_time']} {ACC[r['mt5_account_id']]} #{r['mt5_ticket']} {r['action']} "
          f"vol={r['volume']} open={r['open_price']} close={r['close_price']} "
          f"profit={(r['profit'] or 0):.2f} exit={r['exit_reason']} mfe={r['mfe']} mae={r['mae']}")

print("\n" + "=" * 78)
print("【C. 今日(08-11) 按出场原因 —— 仅可信单口径】")
cur.execute(f"""SELECT exit_reason, COUNT(*) n, SUM(profit) s,
                       SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END) w,
                       SUM(CASE WHEN profit>0 THEN profit ELSE 0 END) gp,
                       SUM(CASE WHEN profit<0 THEN -profit ELSE 0 END) gl
                FROM trades WHERE mt5_account_id IN {IN}
                  AND date(close_time)='2026-08-11'
                  AND NOT (profit=0 AND close_price=open_price)
                  AND NOT (sl IS NOT NULL AND sl>0 AND abs(close_price-sl)<1e-6)
                GROUP BY exit_reason ORDER BY s DESC""")
tot_n = tot_s = tot_w = tot_gp = tot_gl = 0
print(f"{'出场原因':<26}{'笔数':>6}{'净盈亏':>13}{'胜率':>9}{'均单':>11}")
for r in cur.fetchall():
    n, s, w = r['n'], r['s'] or 0, r['w'] or 0
    tot_n += n; tot_s += s; tot_w += w; tot_gp += r['gp'] or 0; tot_gl += r['gl'] or 0
    print(f"{str(r['exit_reason']):<26}{n:>6}{s:>13.2f}{w/n*100:>8.1f}%{s/n:>11.2f}")
pf = tot_gp / tot_gl if tot_gl > 0 else float('inf')
print(f"{'合计':<26}{tot_n:>6}{tot_s:>13.2f}{(tot_w/tot_n*100 if tot_n else 0):>8.1f}%  PF={pf:.3f}")

print("\n" + "=" * 78)
print("【D. 今日(08-11) 全量口径 vs 剔除伪单口径 对比】")
for label, extra in (("全量", ""),
                     ("剔真零值", " AND NOT (profit=0 AND close_price=open_price)"),
                     ("再剔SL伪造", " AND NOT (profit=0 AND close_price=open_price) AND NOT (sl IS NOT NULL AND sl>0 AND abs(close_price-sl)<1e-6)")):
    cur.execute(f"""SELECT COUNT(*) n, SUM(profit) s,
                           SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END) w
                    FROM trades WHERE mt5_account_id IN {IN}
                      AND date(close_time)='2026-08-11' {extra}""")
    r = cur.fetchone()
    n = r['n'] or 0
    print(f"  {label:<12} 笔数={n:>4}  净={r['s'] or 0:>12.2f}  胜率={(r['w']/n*100 if n else 0):>5.1f}%")

print("\n" + "=" * 78)
print("【E. 今日按账号（可信单）】")
cur.execute(f"""SELECT mt5_account_id a, COUNT(*) n, SUM(profit) s,
                       SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END) w
                FROM trades WHERE mt5_account_id IN {IN}
                  AND date(close_time)='2026-08-11'
                  AND NOT (profit=0 AND close_price=open_price)
                  AND NOT (sl IS NOT NULL AND sl>0 AND abs(close_price-sl)<1e-6)
                GROUP BY a""")
for r in cur.fetchall():
    n = r['n']
    print(f"  {ACC[r['a']]:<16} 笔数={n:>4} 净={r['s'] or 0:>11.2f} 胜率={(r['w']/n*100 if n else 0):>5.1f}%")

print("\n" + "=" * 78)
print("【F. 今日开仓节奏（按小时）】")
cur.execute(f"""SELECT substr(open_time,12,2) h, COUNT(*) n
                FROM trades WHERE mt5_account_id IN {IN}
                  AND date(open_time)='2026-08-11' GROUP BY h ORDER BY h""")
print("  " + " | ".join(f"{r['h']}时:{r['n']}" for r in cur.fetchall()))

print("\n" + "=" * 78)
print("【G. 今日特征填充率】")
cur.execute(f"""SELECT COUNT(*) t,
                 SUM(CASE WHEN mfe IS NOT NULL AND mfe<>0 THEN 1 ELSE 0 END) mfe,
                 SUM(CASE WHEN mae IS NOT NULL AND mae<>0 THEN 1 ELSE 0 END) mae,
                 SUM(CASE WHEN chronos_vote IS NOT NULL AND chronos_vote<>'' THEN 1 ELSE 0 END) cv,
                 SUM(CASE WHEN meta_agent_confidence IS NOT NULL THEN 1 ELSE 0 END) mac
                FROM trades WHERE mt5_account_id IN {IN} AND date(close_time)='2026-08-11'""")
r = cur.fetchone()
t = r['t'] or 1
print(f"  今日已平 {r['t']} 笔 | mfe {r['mfe']/t*100:.1f}% | mae {r['mae']/t*100:.1f}% | "
      f"chronos_vote {r['cv']/t*100:.1f}% | meta_conf {r['mac']/t*100:.1f}%")

con.close()
