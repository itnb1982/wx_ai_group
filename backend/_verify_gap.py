# -*- coding: utf-8 -*-
"""只读诊断：①smart_exit 评估间隔缺口 ②行情为模拟数据来源 ③重启时点。
纯观测脚本，不修改任何交易代码/配置。
"""
import io
import os
import re
from datetime import datetime

LOG = r"F:\WanxiangAI\backend\supervisor_uvicorn.log"
TAIL = 12 * 1024 * 1024

size = os.path.getsize(LOG)
start = max(0, size - TAIL)
with open(LOG, "rb") as f:
    f.seek(start)
    raw = f.read()
text = raw.decode("utf-8", errors="replace")
lines = text.split("\n")[1:]
print("分析尾部 %.1f MB / 共 %d 行" % (len(raw) / 1024 / 1024, len(lines)))

TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def ts_of(ln):
    m = TS.match(ln)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------- ① smart_exit 评估时间序列 + 最大缺口 ----------
ev = []
for ln in lines:
    if "smart_exit:evaluate_position" in ln:
        t = ts_of(ln)
        if t:
            ev.append(t)
ev.sort()
print("\n===== ① smart_exit 评估间隔 =====")
print("评估总次数(尾部窗口): %d" % len(ev))
if ev:
    print("首次: %s   末次: %s" % (ev[0], ev[-1]))
    gaps = []
    for i in range(1, len(ev)):
        gaps.append(((ev[i] - ev[i - 1]).total_seconds(), ev[i - 1], ev[i]))
    gaps.sort(key=lambda x: -x[0])
    print("最大 8 个缺口:")
    for g, a, b in gaps[:8]:
        print("   %6.0f 秒 (%4.1f 分)  %s -> %s" % (g, g / 60.0, a.strftime("%H:%M:%S"), b.strftime("%H:%M:%S")))

# ---------- ② 重启标记 ----------
print("\n===== ② 进程启动/重启标记 =====")
pat_boot = ("Started server process", "Application startup", "Uvicorn running", "supervisor")
hits = []
for ln in lines:
    for p in pat_boot:
        if p in ln:
            t = ts_of(ln)
            hits.append((t, ln.strip()[:150]))
            break
for t, ln in hits[-12:]:
    print("   " + ln)

# ---------- ③ 行情为模拟数据 的来源行 ----------
print("\n===== ③ 强制HOLD/模拟行情 原始样本 =====")
kw = [
    "is_mock", "mock", "simulate", "simulated", "SIM_DATA", "fallback_quote",
]
samples = []
for ln in lines:
    low = ln.lower()
    if "hold" in low and ("mock" in low or "simulat" in low):
        samples.append(ln.strip())
print("命中行数: %d" % len(samples))
for s in samples[-6:]:
    print("   " + s[:230])

# ---------- ④ 最近的持仓扫描失败原始行 ----------
print("\n===== ④ 持仓扫描失败 原始样本(最近5条) =====")
scan = [ln.strip() for ln in lines if "get_all_positions_rescanned" in ln]
print("命中行数: %d" % len(scan))
for s in scan[-5:]:
    print("   " + s[:230])

# ---------- ⑤ 17:25 之后的关键活动（重启前后） ----------
print("\n===== ⑤ 17:25 之后关键活动时间线(抽样) =====")
anchors = (
    "smart_exit:evaluate_position",
    "meta_agent:adjudicate",
    "Started server process",
    "Application startup",
    "get_all_positions_rescanned",
)
tl = []
for ln in lines:
    t = ts_of(ln)
    if not t or t < datetime(2026, 8, 10, 17, 25, 0):
        continue
    for a in anchors:
        if a in ln:
            tl.append((t, a))
            break
buckets = {}
for t, a in tl:
    key = t.strftime("%H:%M")
    buckets.setdefault(key, {}).setdefault(a, 0)
    buckets[key][a] += 1
for k in sorted(buckets):
    parts = ", ".join("%s=%d" % (a.split(":")[-1] or a, n) for a, n in sorted(buckets[k].items()))
    print("   %s  %s" % (k, parts))
