# -*- coding: utf-8 -*-
"""只读：按日志级别拆分关键模块计数 + 重启时间点。避免把 INFO 误报成故障。"""
import collections
import re
from datetime import datetime

LOG = r"F:\WanxiangAI\backend\supervisor_uvicorn.log"
TAIL = 12 * 1024 * 1024

with open(LOG, "rb") as f:
    size = f.seek(0, 2)
    f.seek(max(0, size - TAIL))
    lines = f.read().decode("utf-8", "ignore").split("\n")[1:]

MODULES = {
    "debate_engine:decide": "辩论引擎决策",
    "get_all_positions_rescanned": "持仓全景扫描",
    "_run_cycle_with_timeout": "单轮决策周期",
    "smart_exit:evaluate_position": "smart_exit评估",
    "meta_agent:adjudicate": "MetaAgent裁决",
    "deepseek_client": "DeepSeek客户端",
    "hunyuan": "混元客户端",
    "chronos": "Chronos",
}
LEVELS = ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")

print("=== 关键模块 × 日志级别（尾部12MB）===")
print("%-22s %s" % ("模块", "  ".join("%-8s" % l for l in LEVELS)))
for key, name in MODULES.items():
    c = collections.Counter()
    for ln in lines:
        if key in ln:
            for l in LEVELS:
                if ("| %s" % l) in ln[:40]:
                    c[l] += 1
                    break
    if sum(c.values()):
        print("%-22s %s   总=%d" % (name, "  ".join("%-8d" % c.get(l, 0) for l in LEVELS), sum(c.values())))

print("\n=== 重启时间点 ===")
for i, ln in enumerate(lines):
    if "Started server process" in ln:
        pid = re.search(r"\[(\d+)\]", ln)
        ts = None
        for j in range(i, max(0, i - 60), -1):
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", lines[j])
            if m:
                ts = m.group(1)
                break
        print("   pid=%s  临近时间戳=%s" % (pid.group(1) if pid else "?", ts))

print("\n=== 近2小时 ERROR/CRITICAL topN ===")
cnt = collections.Counter()
for ln in lines:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ln)
    if not m:
        continue
    t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    if t < datetime(2026, 8, 10, 15, 50, 0):
        continue
    if "| ERROR" in ln[:40] or "| CRITICAL" in ln[:40]:
        mm = re.search(r"\| (?:ERROR|CRITICAL)\s+\| ([\w\.]+:[\w_]+):(\d+)", ln)
        cnt[mm.group(1) if mm else "other"] += 1
for k, v in cnt.most_common(12):
    print("   %-58s %d" % (k, v))
