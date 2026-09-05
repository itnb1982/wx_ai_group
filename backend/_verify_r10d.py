# -*- coding: utf-8 -*-
"""第10轮 d：ASCII 锚点统计（日志中文编码混乱，禁用中文匹配）"""
import re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
CUT = "2026-08-10 22:39"
recent = [l for l in lines if l[:4] == "2026" and l[:16] >= CUT]

def cnt(pat):
    return [l for l in recent if re.search(pat, l)]

pan = cnt(r"_manage_positions:\d+")
print(f"=== 持仓全景扫描(ASCII锚点 _manage_positions) 近3h {len(pan)} 次")
ts = [l[:19] for l in pan]
print("   最早:", ts[0] if ts else "-", "| 最晚:", ts[-1] if ts else "-")
# 间隔分析
import datetime
dts = [datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S") for t in ts]
gaps = [(dts[i+1]-dts[i]).total_seconds() for i in range(len(dts)-1)]
if gaps:
    big = [(ts[i], gaps[i]/60) for i in range(len(gaps)) if gaps[i] > 300]
    print(f"   最大间隔 {max(gaps)/60:.1f} 分钟 | >5分钟的缺口 {len(big)} 处")
    for t, g in big[-6:]:
        print(f"     缺口起点 {t} 持续 {g:.1f} 分钟")

sl = cnt(r"_mirror_leader_exits:2959|SL\u2192|smart_exit:evaluate_position:276")
print(f"\n=== 实际改单 SL 上移 近3h {len(sl)} 次")
for l in sl[-6:]:
    print("   ", re.sub(r"[^\x20-\x7e]", ".", l)[:140])

se = cnt(r"smart_exit:evaluate_position")
print(f"\n=== smart_exit 评估 近3h {len(se)} 条")
sets = [l[:19] for l in se]
print("   时间点:", sets)

# 决策：ASCII 锚点
for pat, name in ((r"meta_agent:adjudicate:\d+", "MetaAgent裁决"),
                  (r"debate_engine:decide:\d+", "辩论引擎"),
                  (r"fusion_v2", "fusion_v2"),
                  (r"chronos", "chronos调用"),
                  (r"_apply_proofread|proofread", "Qwen校对"),
                  (r"deepseek_client", "DeepSeek调用"),
                  (r"open_position|_execute_entry|order_send", "开仓执行")):
    v = cnt(pat)
    lvl = collections.Counter(re.search(r"\|\s*(\w+)\s*\|", l).group(1) for l in v if re.search(r"\|\s*(\w+)\s*\|", l))
    print(f"\n=== {name} 近3h {len(v)} 行  级别={dict(lvl)}")

# 方向：从 P/L 行统计 S/B
d = collections.Counter()
for l in recent:
    for m in re.finditer(r"#\d+\(([SB])\)", l):
        d[m.group(1)] += 1
print(f"\n=== 持仓方向出现次数(S=空,B=多) === {dict(d)}")

# 唯一持仓 ticket
tk = {}
for l in recent:
    m = re.search(r"#(\d+)\(([SB])\) P/L=([+-][\d\.]+)", l)
    if m:
        tk.setdefault(m.group(1), [m.group(2), [], l[:19], l[:19]])
        tk[m.group(1)][1].append(float(m.group(3)))
        tk[m.group(1)][3] = l[:19]
print(f"\n=== 近3h 出现过的持仓 {len(tk)} 笔（方向/浮盈区间/首末时间）===")
for t, (dirc, pls, t0, t1) in sorted(tk.items(), key=lambda x: x[1][2]):
    print(f"  #{t} {dirc} 浮盈 min={min(pls):>9.2f} max={max(pls):>9.2f} last={pls[-1]:>9.2f}  {t0[11:]}~{t1[11:]}")
