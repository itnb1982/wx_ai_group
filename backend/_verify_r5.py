# -*- coding: utf-8 -*-
"""第5轮定向验证：TP清零bug / 扫描失败归零 / 盈亏单位错配 / Chronos贡献"""
import sqlite3, re, io, os, sys, collections

DB = r"F:/WanxiangAI/backend/data/wx_prod.dat"
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
ACCS = ('2877213e-e79f-4ac4-93cd-4db64730bc04','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3','3540bf33-ee40-4169-8099-7c9616406d99')
NAME = {'2877213e-e79f-4ac4-93cd-4db64730bc04':'liumanchun1','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd':'liumanchuan2',
        '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3':'liumanchun3','3540bf33-ee40-4169-8099-7c9616406d99':'liumanchun4'}

out = []
def P(s=''):
    out.append(str(s)); print(s)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()
ph = ','.join('?'*len(ACCS))

# ---------- 1. 盈亏单位错配检测（今日全部已平单） ----------
P("=== [1] 盈亏单位错配检测（今日已平·1.0手及以上重点）===")
cur.execute(f"""SELECT mt5_account_id, mt5_ticket, action, volume, open_price, close_price, profit, exit_reason
                FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
                AND date(close_time)='2026-08-10' AND IFNULL(exit_reason,'')!='mt5_closed_external'
                ORDER BY volume DESC""", ACCS)
rows = cur.fetchall()
bad = []; tot_lost = 0.0
for acc, tk, act, vol, op, cp, pf, rsn in rows:
    if not op or not cp or not vol:
        continue
    sign = 1 if (act or '').lower() == 'buy' else -1
    pts = (cp - op) * sign
    expected = pts * vol * 100.0
    if abs(expected) < 1e-6:
        continue
    ratio = (pf or 0) / expected
    if 0.005 < abs(ratio) < 0.05:   # 约 1/100
        bad.append((NAME.get(acc, acc[:8]), tk, vol, pts, pf, expected, rsn))
        tot_lost += (expected - (pf or 0))
if bad:
    P(f"  发现 {len(bad)} 笔单位错配（profit 少乘 100 倍合约乘数）：")
    for n, tk, vol, pts, pf, exp, rsn in bad:
        P(f"   {n:<13}#{tk} {vol}手 {pts:+.2f}点  DB记={pf:+.2f}  应为={exp:+.2f}  少记={exp-pf:+.2f}  reason={rsn}")
    P(f"  → 今日合计少记金额: {tot_lost:+.2f}")
else:
    P("  未发现单位错配")

# ---------- 2. 今日按账号 DB口径 vs 修正口径 ----------
P()
P("=== [2] 今日按账号：DB原始 vs 单位修正后 ===")
per = collections.defaultdict(lambda: [0, 0.0, 0.0])
for acc, tk, act, vol, op, cp, pf, rsn in rows:
    n = NAME.get(acc, acc[:8])
    per[n][0] += 1
    per[n][1] += (pf or 0)
    fixed = pf or 0
    if op and cp and vol:
        sign = 1 if (act or '').lower() == 'buy' else -1
        exp = (cp - op) * sign * vol * 100.0
        if abs(exp) > 1e-6 and 0.005 < abs((pf or 0) / exp) < 0.05:
            fixed = exp
    per[n][2] += fixed
P(f"  {'账号':<14}{'笔数':>5}{'DB净额':>13}{'修正净额':>13}")
for n, (c, raw, fx) in sorted(per.items()):
    P(f"  {n:<14}{c:>5}{raw:>13.2f}{fx:>13.2f}")

# ---------- 3. Chronos 出场贡献（全历史） ----------
P()
P("=== [3] Chronos P90 天花板出场贡献（全历史）===")
cur.execute(f"""SELECT exit_reason, COUNT(*), SUM(profit), SUM(CASE WHEN profit>0 THEN 1 ELSE 0 END)
                FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
                AND exit_reason LIKE '%Chronos%' GROUP BY exit_reason""", ACCS)
tn = tp_ = tw = 0
for rsn, c, s, w in cur.fetchall():
    P(f"  {rsn[:52]:<54} n={c:<4} 净={s:>10.2f} 胜率={100.0*w/c:>5.1f}%")
    tn += c; tp_ += (s or 0); tw += (w or 0)
if tn:
    P(f"  合计 n={tn} 净={tp_:+.2f} 胜率={100.0*tw/tn:.1f}%")

# ---------- 4. 未平单 DB vs 实盘 SL 漂移 ----------
P()
P("=== [4] 未平单 DB 记录（对比实盘接口检查 SL/TP 是否回写）===")
cur.execute(f"""SELECT mt5_account_id, mt5_ticket, action, volume, open_price, sl, tp, open_time
                FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL""", ACCS)
for acc, tk, act, vol, op, sl, tp, ot in cur.fetchall():
    P(f"  {NAME.get(acc,acc[:8]):<13}#{tk} {act} {vol}手 in={op} DB_sl={sl} DB_tp={tp} open={ot}")

con.close()

# ---------- 5. 日志：持仓扫描失败按小时（含19/20时） ----------
P()
P("=== [5] 持仓扫描失败 按小时（近尾部日志）===")
size = os.path.getsize(LOG)
span = min(size, 9 * 1024 * 1024)
with open(LOG, 'rb') as f:
    f.seek(size - span)
    data = f.read().decode('utf-8', 'ignore')
lines = data.split('\n')
hour_scan = collections.Counter()
hour_tpzero = collections.Counter()
modify_lines = []
for ln in lines:
    m = re.match(r'(\d{4}-\d{2}-\d{2} (\d{2})):', ln)
    if not m:
        continue
    h = m.group(2)
    if 'get_all_positions_rescanned' in ln or ('positions' in ln and 'ERROR' in ln and 'scan' in ln.lower()):
        hour_scan[h] += 1
    if 'modify_position' in ln or 'modify' in ln.lower() and 'sl=' in ln:
        if len(modify_lines) < 14:
            modify_lines.append(ln[:230])
for h in sorted(hour_scan):
    P(f"   {h}时: {hour_scan[h]}")
P(f"   合计 {sum(hour_scan.values())}")

P()
P("=== [6] SL 修改相关日志（查 TP 是否被一并传 0）===")
for ln in modify_lines:
    P("   " + ln)

with io.open(r"F:/WanxiangAI/backend/_r5_out.txt", "w", encoding="utf-8") as f:
    f.write('\n'.join(out))
