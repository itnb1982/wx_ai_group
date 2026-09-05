# -*- coding: utf-8 -*-
"""第13轮只读日志分析：开市后决策链 / 裁决塌陷 / 开仓执行 / 出场评估 / SL兜底伪造"""
import re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

LOG = r"F:\WanxiangAI\backend\supervisor_uvicorn.log"

TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

def hour_of(line):
    m = TS.match(line)
    return m.group(1)[:13] if m else None

def ts_of(line):
    m = TS.match(line)
    return m.group(1) if m else None

# ASCII anchors only (中文日志已 GBK 乱码)
ANCHORS = {
    'adjudicate':        re.compile(r'meta_agent:adjudicate:1018'),
    'adjudicate605':     re.compile(r'meta_agent:adjudicate:605'),
    'fusion_v2':         re.compile(r'fusion_v2'),
    'execute_trade':     re.compile(r'execute_trade'),
    'order_send':        re.compile(r'order_send'),
    'intent_open':       re.compile(r'intent=open'),
    'smart_exit':        re.compile(r'smart_exit:evaluate_position'),
    'manage_pos':        re.compile(r'_manage_positions:\d+'),
    'move_sl':           re.compile(r'move_sl'),
    'reconcile1106':     re.compile(r'_reconcile_positions:1106'),
    'reconcile':         re.compile(r'reconcile'),
    'flatten_all':       re.compile(r'emergency:_flatten_all_sync'),
    'cycle_timeout':     re.compile(r'_run_cycle_with_timeout'),
    'scan_none':         re.compile(r'get_all_positions_rescanned'),
    'auto_loop_closed':  re.compile(r'_auto_loop:573'),
    'proofread':         re.compile(r'proofread|qwen', re.I),
    'ds402':             re.compile(r'402'),
    'copy_order':        re.compile(r'copy_order'),
    'mirror_exit':       re.compile(r'_mirror_leader_exits'),
    'R3':                re.compile(r'R3'),
}

DIR_RE  = re.compile(r'meta_agent:adjudicate:1018.*?\b(BUY|SELL|HOLD)\b')
CONF_RE = re.compile(r'[:：](\d\.\d{2})')
FUSION_RE = re.compile(r'=([A-Z]{3,4})\(w=[\d\.]+\|conf=(\d+)%')
PL_RE = re.compile(r'#(\d+)\(([SB])\) P/L=([+-][\d\.]+)')

counts = collections.defaultdict(lambda: collections.Counter())   # anchor -> hour -> n
adj_by_hour = collections.defaultdict(lambda: collections.Counter())
adj_conf = collections.defaultdict(list)
fus_by_hour = collections.defaultdict(lambda: collections.Counter())
fus_conf = collections.defaultdict(list)
levels = collections.Counter()
err_lines = []
recent_pl = collections.defaultdict(list)
last_seen = {}
first_seen = {}

# 只看最近 6 小时（从 00 时起）
CUT = "2026-08-11 00"

with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
    for line in f:
        h = hour_of(line)
        if not h or h < CUT:
            continue
        t = ts_of(line)
        for k, rx in ANCHORS.items():
            if rx.search(line):
                counts[k][h] += 1
                last_seen[k] = t
                if k not in first_seen:
                    first_seen[k] = t
        m = DIR_RE.search(line)
        if m:
            adj_by_hour[h][m.group(1)] += 1
            c = CONF_RE.findall(line)
            if c:
                try: adj_conf[h].append(float(c[-1]))
                except: pass
        for fm in FUSION_RE.finditer(line):
            fus_by_hour[h][fm.group(1)] += 1
            fus_conf[h].append(int(fm.group(2)))
        if ' ERROR ' in line or '| ERROR' in line:
            levels['ERROR'] += 1
            err_lines.append(line.rstrip()[:220])
        elif ' WARNING ' in line or '| WARNING' in line:
            levels['WARNING'] += 1
        for pm in PL_RE.finditer(line):
            recent_pl[pm.group(1)].append((t, pm.group(2), float(pm.group(3))))

print("=" * 78)
print("【1】关键锚点按小时计数（00时起）")
print("=" * 78)
hours = sorted({h for c in counts.values() for h in c})
hdr = "锚点".ljust(20) + "".join(x[-2:].rjust(7) for x in hours) + "   合计"
print(hdr)
for k in ANCHORS:
    if not counts[k]:
        continue
    row = k.ljust(20) + "".join(str(counts[k].get(h, 0)).rjust(7) for h in hours)
    row += str(sum(counts[k].values())).rjust(8)
    print(row)

print()
print("=" * 78)
print("【2】MetaAgent 裁决方向分布 × 小时（判系统是否被冻结）")
print("=" * 78)
print("小时".ljust(8) + "BUY".rjust(6) + "SELL".rjust(7) + "HOLD".rjust(7) + "均置信".rjust(9) + "  HOLD占比")
for h in sorted(adj_by_hour):
    c = adj_by_hour[h]
    tot = sum(c.values())
    avg = sum(adj_conf[h]) / len(adj_conf[h]) if adj_conf[h] else 0
    hp = c['HOLD'] / tot * 100 if tot else 0
    print(h[-2:].ljust(8) + str(c['BUY']).rjust(6) + str(c['SELL']).rjust(7)
          + str(c['HOLD']).rjust(7) + f"{avg:.3f}".rjust(9) + f"   {hp:.1f}%")

print()
print("=" * 78)
print("【3】fusion_v2 融合层方向 × 小时（对照裁决层，看是否被否决）")
print("=" * 78)
for h in sorted(fus_by_hour):
    c = fus_by_hour[h]
    avg = sum(fus_conf[h]) / len(fus_conf[h]) if fus_conf[h] else 0
    print(h[-2:].ljust(6) + str(dict(c)).ljust(46) + f"均conf={avg:.0f}%")

print()
print("=" * 78)
print("【4】首次/末次出现时间")
print("=" * 78)
for k in sorted(last_seen):
    print(f"{k:<20} first={first_seen.get(k,'-')}  last={last_seen[k]}")

print()
print("=" * 78)
print(f"【5】日志级别：ERROR={levels['ERROR']}  WARNING={levels['WARNING']}")
print("=" * 78)
em = collections.Counter()
for e in err_lines:
    mm = re.search(r'(\w+:\w+:\d+)', e)
    em[mm.group(1) if mm else 'other'] += 1
for k, v in em.most_common(12):
    print(f"  {v:>5}  {k}")
print("--- 最近 8 条 ERROR ---")
for e in err_lines[-8:]:
    print("  " + e)

print()
print("=" * 78)
print("【6】持仓浮动 P/L 轨迹（近期，最多 12 单）")
print("=" * 78)
for tk, seq in list(recent_pl.items())[-12:]:
    vals = [v for _, _, v in seq]
    print(f"  #{tk} {seq[0][1]}  n={len(seq)}  首={vals[0]:+.2f} 末={vals[-1]:+.2f} "
          f"峰={max(vals):+.2f} 谷={min(vals):+.2f}  [{seq[0][0][-8:]} → {seq[-1][0][-8:]}]")
