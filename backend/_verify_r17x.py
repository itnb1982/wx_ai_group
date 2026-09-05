# -*- coding: utf-8 -*-
"""R17 交叉验证：余额差反推真实盈亏 / 锁竞争与持仓相关性 / 浮盈轨迹。"""
import re
import collections

LOG = "F:/WanxiangAI/backend/supervisor_uvicorn.log"
raw = open(LOG, "rb").read()
if len(raw) > 90 * 1024 * 1024:
    raw = raw[-90 * 1024 * 1024:]
txt = raw.decode("gbk", "replace")
lines = txt.split("\n")


def hh(l):
    m = re.search(r"(\d{2}):(\d{2}):(\d{2})", l[:30])
    return m.group(1) if m else "??"


print("=" * 74)
print("[1] 09:24–09:46 批次 浮动 P/L 轨迹（日志直证）")
pat = re.compile(r"#(3782705\d\d)\([SB]\)\s*P/L=([+-][\d\.]+)")
seq = collections.defaultdict(list)
for ln in lines:
    for m in pat.finditer(ln):
        t = m.group(1)
        ts = ln[11:19]
        seq[t].append((ts, float(m.group(2))))
for t, v in seq.items():
    if not v:
        continue
    vals = [x[1] for x in v]
    print(f"  #{t}  样本{len(v)}  首{v[0][0]}={vals[0]:+.2f}  "
          f"峰值={max(vals):+.2f}  末{v[-1][0]}={vals[-1]:+.2f}")
tot_last = sum(v[-1][1] for v in seq.values() if v)
tot_peak = sum(max(x[1] for x in v) for v in seq.values() if v)
print(f"  >>> 末值合计 {tot_last:+.2f}   峰值合计 {tot_peak:+.2f}")

print()
print("=" * 74)
print("[2] 08:39 批次(378186xxx) 浮动 P/L 轨迹")
pat2 = re.compile(r"#(3781867\d\d)\([SB]\)\s*P/L=([+-][\d\.]+)")
seq2 = collections.defaultdict(list)
for ln in lines:
    for m in pat2.finditer(ln):
        seq2[m.group(1)].append((ln[11:19], float(m.group(2))))
for t, v in seq2.items():
    vals = [x[1] for x in v]
    print(f"  #{t}  样本{len(v)}  峰值={max(vals):+.2f}  末{v[-1][0]}={vals[-1]:+.2f}")
print(f"  >>> 末值合计 {sum(v[-1][1] for v in seq2.values()):+.2f}")

print()
print("=" * 74)
print("[3] database is locked 按小时 × 是否有持仓（相关性检验）")
lock = collections.Counter()
mv = collections.Counter()
mp = collections.Counter()
for ln in lines:
    h = hh(ln)
    if "database is locked" in ln:
        lock[h] += 1
    if "move_sl" in ln:
        mv[h] += 1
    if "_manage_positions:" in ln:
        mp[h] += 1
print(f"  {'时':4s} {'locked':>8s} {'move_sl':>8s} {'持仓全景':>8s}")
for h in sorted(set(list(lock) + list(mv) + list(mp))):
    if h == "??":
        continue
    print(f"  {h}时 {lock[h]:8d} {mv[h]:8d} {mp[h]:8d}")

print()
print("=" * 74)
print("[4] 今日开仓原文（execute_cycle:2030）")
for ln in [x for x in lines if "execute_cycle:2030" in x]:
    print("  " + ln.strip()[:250])

print()
print("=" * 74)
print("[5] 10 时裁决 summary 全文样本（看死票与真分歧构成）")
buf = [x for x in lines if "trading:_auto_loop:626" in x and " 10:" in x[:22]]
print(f"  10时 cycle 共 {len(buf)} 条")
combo = collections.Counter()
for ln in buf:
    ds = re.search(r"'deepseek_vote':\s*'(\w+)'", ln)
    hy = re.search(r"'hunyuan_vote':\s*'(\w+)'", ln)
    cf = re.search(r"'confidence':\s*([\d\.]+)", ln)
    orders = re.search(r"orders=(\d+)", ln)
    combo[(ds.group(1) if ds else '?', hy.group(1) if hy else '?')] += 1
print("  (DS票, HY票) 组合分布:", dict(combo))
confs = [float(re.search(r"'confidence':\s*([\d\.]+)", x).group(1))
         for x in buf if re.search(r"'confidence':\s*([\d\.]+)", x)]
if confs:
    print(f"  置信度 min={min(confs):.3f} max={max(confs):.3f} avg={sum(confs)/len(confs):.3f}")
    print(f"  conf=0.000 的周期数 = {sum(1 for c in confs if c == 0)}")

print()
print("=" * 74)
print("[6] Chronos 方向 vs 混元方向（10时，真分歧 or 死票致命）")
cro = collections.Counter()
for ln in lines:
    if "adjudicate" in ln and " 10:" in ln[:22]:
        m = re.search(r"Chronos=(\w+)", ln)
        if m:
            cro[m.group(1)] += 1
print("  10时 Chronos 方向分布:", dict(cro))

print()
print("=" * 74)
print("[7] 最近一次完整 R2/R3 决策快照（含权重）")
for ln in [x for x in lines if "adjudicate:1018" in x][-5:]:
    print("  " + ln.strip()[:300])

print()
print("=" * 74)
print("[8] 空仓窗口量化")
opens = [x for x in lines if "execute_cycle:2030" in x]
if opens:
    print("  末次开仓:", opens[-1].strip()[:60])
closes = [x for x in lines if "_reconcile_positions:1106" in x]
if closes:
    print("  末次兜底平仓:", closes[-1].strip()[:60])
