# -*- coding: utf-8 -*-
"""第11轮 日志侧：真实浮盈轨迹 / 账号映射 / 重启 / 全平 / AI评估缺口 / 锁利 / ERROR"""
import re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
print(f"日志总行数: {len(lines)}")

CUT = "2026-08-10 23:44"
recent = [l for l in lines if l[:4] == "2026" and l[:16] >= CUT]
print(f"近3h(自 {CUT}) 行数: {len(recent)}")

# 1) 日志级别
lv = collections.Counter()
for l in recent:
    m = re.search(r"\|\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|", l)
    if m: lv[m.group(1)] += 1
print("\n=== 近3h 日志级别 ===", dict(lv))

# 2) ERROR TOP
errs = collections.Counter()
for l in recent:
    if "| ERROR" in l:
        m = re.search(r"\|\s*([\w\.]+:[\w_]+:\d+)\s*-\s*(.{0,90})", l)
        errs[(m.group(1) if m else "?", (m.group(2) if m else l[-90:]).strip())] += 1
print("\n=== 近3h ERROR TOP8 ===")
for (mod, msg), n in errs.most_common(8):
    print(f"  {n:>4}x {mod}  {msg[:78]}")

# 3) 重启点
print("\n=== 启动/重启事件 ===")
for l in recent:
    if re.search(r"(startup|Application startup complete|Uvicorn running|Started server process)", l, re.I):
        print("   ", l[:150])

# 4) 人工全平 / 紧急
print("\n=== emergency / flatten 事件 ===")
for l in recent:
    if "emergency" in l and ("flatten" in l or "halt" in l.lower()):
        print("   ", l[:190])

# 5) 真实浮盈轨迹（ASCII 锚点）
pl = collections.defaultdict(list)
for l in recent:
    m = re.search(r"#(\d+)\(([SB])\) P/L=([+-][\d\.]+)", l)
    if m: pl[m.group(1)].append((l[:19], float(m.group(3))))

TARGET = ["378015562", "378015564", "378015556", "378015563",
          "377977945", "377977947", "377977951", "377977943",
          "377945135", "377945141", "377945228", "377945037",
          "377895261", "377895283", "377895259", "377895268"]
DBVAL = {"378015562": -1759.00, "378015564": -2457.00, "378015556": -34.90, "378015563": -32.48,
         "377977945": -2474.00, "377977947": -49.52, "377977951": -2477.00, "377977943": -49.56,
         "377945135": -2586.00, "377945141": -50.50, "377945228": -2564.00, "377945037": -50.74,
         "377895261": -2525.00, "377895283": -2520.00, "377895259": -50.42, "377895268": -29.98}
print("\n=== 【关键】平仓前真实浮盈(日志) vs DB回写 ===")
print(f"{'ticket':<12}{'样本':>5}{'峰值':>11}{'谷值':>11}{'最后浮盈':>12}{'最后时刻':>21}{'DB回写':>11}{'偏差':>11}")
tr = td = 0.0
for t in TARGET:
    v = DBVAL[t]
    if pl.get(t):
        seq = [x[1] for x in pl[t]]
        ts, last = pl[t][-1]
        tr += last; td += v
        print(f"{t:<12}{len(seq):>5}{max(seq):>11.2f}{min(seq):>11.2f}{last:>12.2f}{ts:>21}{v:>11.2f}{last-v:>11.2f}")
    else:
        print(f"{t:<12}{0:>5}{'-':>11}{'-':>11}{'(无日志)':>12}{'':>21}{v:>11.2f}")
print(f"{'合计可比对':<12}{'':>5}{'':>11}{'':>11}{tr:>12.2f}{'':>21}{td:>11.2f}{tr-td:>11.2f}")

# 6) 价格最高点（判断 SL 4388 是否真被打到）
hi = []
for l in recent:
    for m in re.finditer(r"(?:bid|Bid|BID)[=: ]+([\d]{4}\.\d{2})", l):
        hi.append((l[:19], float(m.group(1))))
if hi:
    mx = max(hi, key=lambda x: x[1])
    print(f"\n=== 近3h 日志中 bid 最高 = {mx[1]} @ {mx[0]} | 样本 {len(hi)}")
    late = [x for x in hi if x[0] >= "2026-08-11 02:20"]
    if late:
        print(f"    02:20 后 bid 区间 = {min(x[1] for x in late)} ~ {max(x[1] for x in late)}")

# 7) 动态止损 / 锁利
mv = [l for l in recent if "move_sl" in l or "SL->" in l or "modify_sl" in l or "trailing" in l.lower()]
print(f"\n=== 动态止损/锁利动作 近3h {len(mv)} 条 ===")
for l in mv[-10:]:
    print("   ", l[:165])

# 8) AI 出场评估
se = [l for l in recent if "smart_exit" in l]
print(f"\n=== smart_exit 近3h {len(se)} 条 ===")
for l in se[-8:]:
    print("   ", l[:170])
ts_se = sorted(set(l[:19] for l in se))
print("  评估时间戳(去重):", ts_se[-14:])

# 9) 持仓全景刷新（ASCII 锚点）
pan = [l[:19] for l in recent if re.search(r"_manage_positions:\d+", l)]
print(f"\n=== _manage_positions 近3h {len(pan)} 次, 最后5: {pan[-5:]}")

# 10) 开仓 / 决策
ex = [l for l in recent if re.search(r"(execute_trade|order_send|开仓成功)", l)]
print(f"\n=== 执行类日志 近3h {len(ex)} 条, 最后6 ===")
for l in ex[-6:]:
    print("   ", l[:170])

# 11) 账号余额锚点（用于 DB vs 真实对账）
bal = [l for l in recent if re.search(r"balance[=: ]", l, re.I)]
print(f"\n=== balance 相关行 {len(bal)} 条, 最后6 ===")
for l in bal[-6:]:
    print("   ", l[:180])

# 12) 外部平仓/对账锚点
rec = [l for l in recent if "reconcile" in l or "mt5_closed_external" in l]
print(f"\n=== reconcile / 外部平仓 近3h {len(rec)} 条, 最后10 ===")
for l in rec[-10:]:
    print("   ", l[:185])
