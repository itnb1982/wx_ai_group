# -*- coding: utf-8 -*-
"""第13轮 B：真实开仓锚点 + 裁决置信度修正 + 拦截规则归因 + 开市后决策链"""
import re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

LOG = r"F:\WanxiangAI\backend\supervisor_uvicorn.log"
TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
CUT = "2026-08-11 00"

A = {
    'exec_open':   re.compile(r'trade_executor:execute_cycle:2030'),
    'copy_order':  re.compile(r'trade_executor:copy_order:2356'),
    'calc_lot':    re.compile(r'_calc_position_size:883'),
    'cycle_sum':   re.compile(r'trading:_auto_loop:626'),
    'adjudicate':  re.compile(r'meta_agent:adjudicate:1018'),
    'R3_rule':     re.compile(r'meta_agent:adjudicate:605'),
}
ORDERS_RE = re.compile(r"orders=(\d+)")
ACT_RE    = re.compile(r"'action':\s*'(\w+)'")
CONF_RE   = re.compile(r"'confidence':\s*([\d\.]+)")
DSV_RE    = re.compile(r"'deepseek_vote':\s*'(\w+)'")
HYV_RE    = re.compile(r"'hunyuan_vote':\s*'(\w+)'")

cnt = collections.defaultdict(lambda: collections.Counter())
cycle_rows = []
adj_lines = []
r3_lines = []
lot_clamp = []

def hour_of(l):
    m = TS.match(l); return m.group(1)[:13] if m else None
def ts_of(l):
    m = TS.match(l); return m.group(1) if m else None

with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        h = hour_of(line)
        if not h or h < CUT: continue
        for k, rx in A.items():
            if rx.search(line):
                cnt[k][h] += 1
        if A['cycle_sum'].search(line):
            o = ORDERS_RE.search(line); a = ACT_RE.search(line)
            c = CONF_RE.search(line); d = DSV_RE.search(line); y = HYV_RE.search(line)
            cycle_rows.append((ts_of(line), a.group(1) if a else '?',
                               float(c.group(1)) if c else 0.0,
                               int(o.group(1)) if o else 0,
                               d.group(1) if d else '-', y.group(1) if y else '-'))
        if A['adjudicate'].search(line):
            adj_lines.append(line.rstrip())
        if A['R3_rule'].search(line):
            r3_lines.append(line.rstrip())
        if A['calc_lot'].search(line):
            lot_clamp.append(line.rstrip())

print("=" * 78)
print("【A】真实执行锚点按小时")
print("=" * 78)
hours = sorted({h for c in cnt.values() for h in c})
print("锚点".ljust(16) + "".join(x[-2:].rjust(7) for x in hours) + "   合计")
for k in A:
    if not cnt[k]: continue
    print(k.ljust(16) + "".join(str(cnt[k].get(h, 0)).rjust(7) for h in hours)
          + str(sum(cnt[k].values())).rjust(8))

print()
print("=" * 78)
print("【B】自动交易 cycle 汇总（action / conf / orders / DS票 / HY票）")
print("=" * 78)
bh = collections.defaultdict(lambda: [0, 0, collections.Counter(), 0.0])
for t, a, c, o, d, y in cycle_rows:
    h = t[:13]
    bh[h][0] += 1; bh[h][1] += o; bh[h][2][a] += 1; bh[h][3] += c
print("小时".ljust(7) + "cycle数".rjust(8) + "orders合计".rjust(11) + "均conf".rjust(9) + "  action分布")
for h in sorted(bh):
    n, o, ac, cs = bh[h]
    print(h[-2:].ljust(7) + str(n).rjust(8) + str(o).rjust(11)
          + f"{cs/n:.3f}".rjust(9) + "  " + str(dict(ac)))

print()
print("--- 近 25 条 cycle 明细 ---")
for t, a, c, o, d, y in cycle_rows[-25:]:
    flag = "  <== 开仓" if o > 0 else ""
    print(f"  {t[-8:]}  action={a:<5} conf={c:.3f}  orders={o}  DS={d:<5} HY={y:<5}{flag}")

print()
print("=" * 78)
print("【C】DS 票取值分布（判断 402 时投的什么票）")
print("=" * 78)
dsc = collections.Counter(d for *_, d, y in cycle_rows)
hyc = collections.Counter(y for *_, d, y in cycle_rows)
print("  DeepSeek 票:", dict(dsc))
print("  混元    票:", dict(hyc))

print()
print("=" * 78)
print("【D】R3 拦截规则原文（最近 6 条）")
print("=" * 78)
for l in r3_lines[-6:]:
    print("  " + l[:230])

print()
print("=" * 78)
print("【E】adjudicate:1018 原文（最近 6 条，看真实置信度）")
print("=" * 78)
for l in adj_lines[-6:]:
    print("  " + l[:250])

print()
print("=" * 78)
print("【F】手数计算/钳制（最近 5 条，判风控是否放行）")
print("=" * 78)
for l in lot_clamp[-5:]:
    print("  " + l[:230])
