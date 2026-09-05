# -*- coding: utf-8 -*-
"""第10轮 c：日志侧核验 —— 真实浮盈轨迹 vs DB 回写值、锁利动作、AI 评估缺口、ERROR 级别统计"""
import re, sys, io, collections, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
print(f"日志行数: {len(lines)}")

CUT = "2026-08-10 22:39"   # 近3h
recent = [l for l in lines if l[:16] >= CUT and l[:4] == "2026"]
print(f"近3h 行数: {len(recent)}")

# 1) 各级别计数
lv = collections.Counter()
for l in recent:
    m = re.search(r"\|\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\|", l)
    if m: lv[m.group(1)] += 1
print("\n=== 近3h 日志级别 ===", dict(lv))

# 2) ERROR topN
errs = collections.Counter()
for l in recent:
    if "| ERROR" in l:
        m = re.search(r"\|\s*([\w\.]+:[\w_]+:\d+)\s*-\s*(.{0,90})", l)
        errs[(m.group(1) if m else "?", (m.group(2) if m else l[-90:]).strip())] += 1
print("\n=== 近3h ERROR TOP ===")
for (mod, msg), n in errs.most_common(8):
    print(f"  {n:>4}x {mod}  {msg[:80]}")

# 3) 锁利/移动止损动作
mv = [l for l in recent if "SL→" in l or "move_sl" in l]
mv_real = [l for l in recent if "SL→" in l]
print(f"\n=== 动态止损 === 近3h SL→实际改单 {len(mv_real)} 次 | move_sl 动作广播 {len(mv)} 次")
for l in mv_real[-8:]:
    print("   ", l[:150])

# 4) AI 出场评估缺口
se = [l for l in recent if "smart_exit" in l]
print(f"\n=== AI 出场评估 === 近3h smart_exit 行 {len(se)} 条")
for l in se[-5:]:
    print("   ", l[:160])

# 5) 持仓全景刷新节奏
pan = [l[:19] for l in recent if "[持仓全景]" in l]
print(f"\n=== 持仓全景 === 近3h 刷新 {len(pan)} 次, 最后 5 次: {pan[-5:]}")

# 6) 真实浮盈轨迹（P/L=）对比 DB 回写
pl = collections.defaultdict(list)
for l in recent:
    m = re.search(r"#(\d+)\(([SB])\) P/L=([+-][\d\.]+)", l)
    if m: pl[m.group(1)].append((l[:19], float(m.group(3))))
DBVAL = {"377945135": -2586.00, "377945228": -2564.00, "377977945": -2474.00,
         "377977951": -2477.00, "377895261": -2525.00, "377895283": -2520.00,
         "377889405": -2540.00, "377889412": -2541.00}
print("\n=== 【关键】平仓前真实浮盈 vs DB 回写盈亏 ===")
print(f"{'ticket':<12}{'最后浮盈(真实)':>16}{'最后时刻':>22}{'DB回写':>12}{'偏差':>12}")
tot_real = tot_db = 0.0
for t, v in DBVAL.items():
    if pl.get(t):
        ts, last = pl[t][-1]
        tot_real += last; tot_db += v
        print(f"{t:<12}{last:>16.2f}{ts:>22}{v:>12.2f}{last - v:>12.2f}")
    else:
        print(f"{t:<12}{'(无浮盈日志)':>16}{'':>22}{v:>12.2f}")
print(f"{'合计(可比对)':<12}{tot_real:>16.2f}{'':>22}{tot_db:>12.2f}{tot_real - tot_db:>12.2f}")

# 7) 开仓方向
op = collections.Counter()
for l in recent:
    m = re.search(r"(开仓成功|下单成功|已开仓).{0,60}?(BUY|SELL|买入|卖出)", l)
    if m: op[m.group(2)] += 1
print("\n=== 近3h 开仓日志方向 ===", dict(op))

# 8) 决策方向分布
dec = collections.Counter()
for l in recent:
    m = re.search(r"最终决策[：:]\s*(BUY|SELL|HOLD)", l) or re.search(r"裁决.{0,10}(BUY|SELL|HOLD)", l)
    if m: dec[m.group(1)] += 1
print("=== 近3h 决策方向 ===", dict(dec))
