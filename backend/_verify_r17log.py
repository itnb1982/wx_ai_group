"""R17 只读日志分析。铁律：GBK 解码；故障类锚点必须叠加 ERROR/WARNING 级别过滤。"""
import re
from collections import Counter, defaultdict

LOG = "F:/WanxiangAI/backend/supervisor_uvicorn.log"
raw = open(LOG, "rb").read()
# 只取最后 14MB，覆盖今日
raw = raw[-14_000_000:]
txt = raw.decode("gbk", "replace")
lines = txt.split("\n")
print(f"[载入] {len(lines)} 行")

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def hour(l):
    m = TS.match(l)
    return m.group(1)[11:13] if m else None


def ts(l):
    m = TS.match(l)
    return m.group(1) if m else None


# 只保留今日
today = [l for l in lines if l.startswith("2026-08-11")]
print(f"[今日] {len(today)} 行")

print("=" * 72)
print("[A] DeepSeek 402 / 恢复情况（按小时）")
h402 = Counter()
hok = Counter()
first_ok_after = None
last402 = None
for l in today:
    if "402" in l and "Insufficient" in l:
        h = hour(l)
        if h:
            h402[h] += 1
        last402 = ts(l)
for h in sorted(set(list(h402))):
    print(f"   {h}时: 402={h402[h]}")
print(f"   今日 402 合计={sum(h402.values())}  末次={last402}")

print("=" * 72)
print("[B] 裁决方向分布（adjudicate:1018）按小时  ※置信度取 c[0]")
adj = re.compile(r"meta_agent:adjudicate:1018")
conf_re = re.compile(r"[:：](\d\.\d{2})")
dirs = defaultdict(Counter)
confs = defaultdict(list)
for l in today:
    if adj.search(l):
        h = hour(l)
        for d in ("SELL", "BUY", "HOLD"):
            if re.search(r"\b" + d + r"\b", l):
                dirs[h][d] += 1
                break
        c = conf_re.findall(l)
        if c:
            confs[h].append(float(c[0]))
for h in sorted(dirs):
    cc = confs[h]
    tot = sum(dirs[h].values())
    hp = dirs[h]["HOLD"] / tot * 100 if tot else 0
    print(f"   {h}时: {dict(dirs[h])} 共{tot} HOLD占{hp:5.1f}% 均置信={sum(cc)/len(cc) if cc else 0:.3f}")

print("=" * 72)
print("[C] cycle 汇总锚点 _auto_loop:626（orders / confidence / errors）")
cyc = [l for l in today if "_auto_loop:626" in l]
print(f"   今日 cycle 汇总行 {len(cyc)}")
ho = defaultdict(lambda: [0, 0])  # [周期数, orders合计]
for l in cyc:
    h = hour(l)
    m = re.search(r"orders=(\d+)", l)
    ho[h][0] += 1
    if m:
        ho[h][1] += int(m.group(1))
for h in sorted(ho):
    print(f"   {h}时: 周期={ho[h][0]:3d} orders合计={ho[h][1]}")
print("   —— 最近 6 条 cycle 汇总原文 ——")
for l in cyc[-6:]:
    print("    ", l.strip()[:340])

print("=" * 72)
print("[D] database is locked（按小时）")
hl = Counter()
for l in today:
    if "database is locked" in l:
        h = hour(l)
        if h:
            hl[h] += 1
for h in sorted(hl):
    print(f"   {h}时: {hl[h]}")
print(f"   今日合计={sum(hl.values())}")

print("=" * 72)
print("[E] 关键动作锚点计数（今日）")
anchors = {
    "开仓execute_cycle:2030": "trade_executor:execute_cycle:2030",
    "跟单copy_order": "trade_executor:copy_order",
    "SL兜底伪造:1106": "_reconcile_positions:1106",
    "smart_exit评估": "smart_exit:evaluate_position",
    "持仓管理_manage_positions": "_manage_positions:",
    "人工全平": "_flatten_all_sync",
    "镜像退出": "_mirror_leader_exits",
    "modify_sl_tp": "modify_sl_tp",
    "扫描失败rescanned": "get_all_positions_rescanned",
    "cycle超时": "_run_cycle_with_timeout",
}
for k, a in anchors.items():
    c = sum(1 for l in today if a in l)
    print(f"   {k:28s}: {c}")

print("=" * 72)
print("[F] 中文关键标记（GBK 已解码）")
for k in ("[智能止损]", "[追踪止盈]", "[持仓全景]", "[跟号镜像]", "清孤儿单", "休市"):
    c = sum(1 for l in today if k in l)
    print(f"   {k:14s}: {c}")

print("=" * 72)
print("[G] 13:00 之后全部关键事件时间线（开仓/平仓/止损/镜像/对账）")
keys = ("execute_cycle:2030", "copy_order", "_reconcile_positions", "[智能止损]",
        "[追踪止盈]", "清孤儿单", "modify_sl_tp", "_mirror_leader_exits")
for l in today:
    h = hour(l)
    if h and h >= "13" and any(k in l for k in keys):
        print("   ", l.strip()[:300])

print("=" * 72)
print("[H] ERROR 级别 TOP 模块（今日）")
errs = Counter()
for l in today:
    if "| ERROR" in l or " ERROR " in l:
        m = re.search(r"([a-z_]+:[a-z_]+:\d+)", l)
        errs[m.group(1) if m else "other"] += 1
for k, v in errs.most_common(12):
    print(f"   {k:46s} {v}")
print(f"   今日 ERROR 合计={sum(errs.values())}")
