# -*- coding: utf-8 -*-
"""第7轮只读核查：最近1小时平仓明细 / 空仓成因 / 锁利全天口径 / 未平回写滞后"""
import sqlite3, os, io, sys, re, json, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DB = r"F:/WanxiangAI/backend/data/wx_prod.dat"
ACCS = ('2877213e-e79f-4ac4-93cd-4db64730bc04',
        'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3',
        '3540bf33-ee40-4169-8099-7c9616406d99')
NAMES = {'2877213e-e79f-4ac4-93cd-4db64730bc04': 'liumanchun1',
         'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd': 'liumanchuan2',
         '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3': 'liumanchun3',
         '3540bf33-ee40-4169-8099-7c9616406d99': 'liumanchun4'}

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()
ph = ','.join('?' * len(ACCS))

print("===== ① 最近1小时(21:10之后)平仓明细 =====")
cur.execute(f"""SELECT close_time, mt5_account_id, mt5_ticket, action, volume,
                       open_price, close_price, profit, exit_reason, sl, tp, open_time
                FROM trades WHERE mt5_account_id IN ({ph})
                  AND close_time IS NOT NULL AND close_time >= '2026-08-10 21:10:00'
                ORDER BY close_time""", ACCS)
rows = cur.fetchall()
tot = 0.0
extern = 0
for r in rows:
    ct, aid, tk, act, vol, op, cp, pf, rs, sl, tp, ot = r
    pf = pf or 0.0
    if rs == 'mt5_closed_external':
        extern += 1
    else:
        tot += pf
    # 单位错配检测
    exp = (cp - op) * (vol or 0) * 100 * (1 if act == 'buy' else -1) if (cp and op) else 0
    flag = ''
    if exp and pf and abs(exp) > 50 and abs(pf) > 0.01 and 0.005 < abs(pf / exp) < 0.02:
        flag = f'  <<单位错配? 应≈{exp:.2f}'
    print(f"  {ct} {NAMES.get(aid,'?'):12s} #{tk} {act:4s} v={vol} {op}->{cp} pnl={pf:.2f} [{rs}]{flag}")
print(f"  小计 {len(rows)} 笔，其中外部平仓零值 {extern} 笔；非外部合计盈亏 {tot:.2f}")

print("\n===== ② 今日全天 锁利/SL上移 类出场（DB口径，全天） =====")
cur.execute(f"""SELECT exit_reason, COUNT(*), SUM(profit) FROM trades
                WHERE mt5_account_id IN ({ph}) AND close_time >= '2026-08-10 00:00:00'
                  AND (exit_reason LIKE '%lock%' OR exit_reason LIKE '%l3%'
                       OR exit_reason LIKE '%breakeven%' OR exit_reason='tp')
                GROUP BY exit_reason ORDER BY 3 DESC""", ACCS)
for rs, n, s in cur.fetchall():
    print(f"  {rs:50s} n={n:3d} 净={s:.2f}")

print("\n===== ③ DB 未平单 vs 实盘 =====")
cur.execute(f"""SELECT open_time, mt5_account_id, mt5_ticket, action, volume, open_price,
                       sl, tp, profit, exit_reason, close_time
                FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL
                ORDER BY open_time""", ACCS)
for r in cur.fetchall():
    print(f"  开于{r[0]} {NAMES.get(r[1],'?'):12s} #{r[2]} {r[3]} v={r[4]} in={r[5]} sl={r[6]} tp={r[7]} profit={r[8]} reason={r[9]}")

print("\n===== ④ 今日开仓节奏（按小时） =====")
cur.execute(f"""SELECT substr(open_time,12,2), COUNT(*) FROM trades
                WHERE mt5_account_id IN ({ph}) AND open_time >= '2026-08-10 00:00:00'
                GROUP BY 1 ORDER BY 1""", ACCS)
print("  " + "  ".join(f"{h}时:{n}" for h, n in cur.fetchall()))

print("\n===== ⑤ 今日 mfe/mae/chronos_vote 填充率（今日口径） =====")
cur.execute(f"""SELECT COUNT(*), SUM(CASE WHEN mfe IS NOT NULL AND mfe<>0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN mae IS NOT NULL AND mae<>0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN chronos_vote IS NOT NULL THEN 1 ELSE 0 END)
                FROM trades WHERE mt5_account_id IN ({ph})
                  AND close_time >= '2026-08-10 00:00:00'""", ACCS)
n, a, b, c = cur.fetchone()
if n:
    print(f"  今日已平 {n} 笔: mfe {a}({a/n*100:.1f}%) mae {b}({b/n*100:.1f}%) chronos_vote {c}({c/n*100:.1f}%)")
con.close()

print("\n===== ⑥ 日志：最近空仓期的开仓意图/拒单锚点 =====")
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
size = os.path.getsize(LOG)
with open(LOG, 'rb') as f:
    f.seek(max(0, size - 8 * 1024 * 1024))
    tail = f.read().decode('utf-8', errors='replace').splitlines()
print(f"  尾部 {len(tail)} 行")

anchors = {
    'adjudicate:951': '最终裁决',
    'insufficient': '置信不足',
    'below_threshold': '低于阈值',
    'open_position': '开仓调用',
    'place_order': '下单',
    'HOLD': 'HOLD',
    'smart_exit:evaluate_position': '持仓评估',
    'get_all_positions_rescanned': '扫描失败',
}
for k, label in anchors.items():
    hits = [l for l in tail if k in l]
    recent = [l for l in hits if l[11:16] >= '21:30']
    print(f"  {label:10s}({k}): 尾部 {len(hits)} 条 / 21:30后 {len(recent)} 条")

print("\n  --- 最近8条最终裁决 ---")
dec = [l for l in tail if 'adjudicate:951' in l][-8:]
for l in dec:
    m = re.search(r'(\d{2}:\d{2}:\d{2})', l)
    # 提取 ASCII 可读部分：方向 + 置信度数字
    d = 'BUY' if 'BUY' in l else ('SELL' if 'SELL' in l else '?')
    cf = re.search(r'(\d\.\d{2})', l)
    print(f"    {m.group(1) if m else '?'} {d} conf={cf.group(1) if cf else '?'}")

print("\n  --- 最近6条 fusion_v2 合票 ---")
for l in [l for l in tail if 'fusion_v2' in l][-6:]:
    m = re.search(r'(\d{2}:\d{2}:\d{2})', l)
    d = re.search(r'=(BUY|SELL|HOLD)\(', l)
    w = re.search(r'w=([\d.]+)', l)
    c = re.search(r'conf=(\d+)%', l)
    print(f"    {m.group(1) if m else '?'} {d.group(1) if d else '?'} w={w.group(1) if w else '?'} conf={c.group(1) if c else '?'}%")

print("\n  --- 最近5条 Chronos 方向冲突(adjudicate:678) ---")
for l in [l for l in tail if 'adjudicate:678' in l][-5:]:
    m = re.search(r'(\d{2}:\d{2}:\d{2})', l)
    nums = re.findall(r'(\d+)%', l)
    print(f"    {m.group(1) if m else '?'} 置信 {'->'.join(nums) if nums else '?'}")

print("\n  --- 22时 扫描失败/超时 时间点 ---")
for l in tail:
    if l[11:13] == '22' and ('get_all_positions_rescanned' in l or '_run_cycle_with_timeout' in l) and 'ERROR' in l:
        m = re.search(r'(\d{2}:\d{2}:\d{2})', l)
        tag = 'scan_fail' if 'rescanned' in l else 'cycle_timeout'
        print(f"    {m.group(1) if m else '?'} {tag}")
