"""R18 只读日志扫描：GBK 解码 + ASCII 锚点，按时间窗统计。
聚焦：重启时间点、smart_exit 评估、move_sl、SL伪造(_reconcile:1106)、
DeepSeek 402、database is locked、持仓全景、出场评估、_auto_loop:626 周期汇总。
"""
import re
from datetime import datetime

LOG = "F:/WanxiangAI/backend/supervisor_uvicorn.log"
# 当前轮次时间窗：2026-08-11 14:57 倒推 ~90 分钟
NOW = datetime(2026, 8, 11, 14, 57, 29)
WIN_START = datetime(2026, 8, 11, 13, 0, 0)   # 覆盖 R17 之后到本轮
WIN_END = NOW

# 锚点定义（全 ASCII）
ANCHORS = {
    "restart": re.compile(r"Application startup complete|Started server process|Uvicorn running"),
    "smart_exit_eval": re.compile(r"smart_exit:evaluate_position"),
    "panorama": re.compile(r"\[持仓全景\]"),
    "move_sl": re.compile(r"智能止损\] SL->|move_sl|SL->"),
    "reconcile": re.compile(r"_reconcile_positions:1106"),
    "ds402": re.compile(r"402 Insufficient Balance|Insufficient Balance"),
    "db_locked": re.compile(r"database is locked"),
    "evaluate_exits": re.compile(r"evaluate_exits"),
    "auto_loop626": re.compile(r"_auto_loop:626"),
    "cycle_timeout": re.compile(r"_run_cycle_with_timeout|run_cycle_for_user"),
    "flatten": re.compile(r"emergency:_flatten_all_sync"),
    "tracking_tp": re.compile(r"追踪止盈"),
    "mirror": re.compile(r"跟号镜像|mirror"),
}


def parse_ts(line):
    # 日志行形如 2026-08-11 14:48:01,xxx | ...
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


counts = {k: 0 for k in ANCHORS}
recent_ts = {k: [] for k in ANCHORS}
restarts = []
last_smart_exit_ts = None
last_panorama_ts = None

with open(LOG, "rb") as f:
    # 只扫最后 3MB 足够覆盖窗口
    f.seek(0, 2)
    size = f.tell()
    seek = max(0, size - 3_000_000)
    f.seek(seek)
    buf = f.read().decode("gbk", "replace")

for line in buf.splitlines():
    ts = parse_ts(line)
    if ts is None:
        continue
    if ts < WIN_START or ts > WIN_END:
        continue
    for k, rx in ANCHORS.items():
        if rx.search(line):
            counts[k] += 1
            recent_ts[k].append(ts.strftime("%H:%M:%S"))
            if k == "restart":
                restarts.append(ts.strftime("%m-%d %H:%M:%S"))
            if k == "smart_exit_eval":
                last_smart_exit_ts = ts
            if k == "panorama":
                last_panorama_ts = ts

print("=== 日志扫描窗口", WIN_START, "~", WIN_END, "===")
print("锚点命中计数：")
for k in ANCHORS:
    print(f"  {k:18s}: {counts[k]}")
print()
print("重启时间点(本窗口):", restarts if restarts else "无")
print("末次 smart_exit 评估:", last_smart_exit_ts.strftime("%H:%M:%S") if last_smart_exit_ts else "无")
print("末次 持仓全景:", last_panorama_ts.strftime("%H:%M:%S") if last_panorama_ts else "无")
if last_smart_exit_ts:
    gap = (NOW - last_smart_exit_ts).total_seconds()
    print(f"距末次评估间隔: {gap:.0f}s ({gap/60:.1f}min)")

# database is locked 按小时分布
print()
print("=== database is locked 按小时 (本窗口) ===")
with open(LOG, "rb") as f:
    f.seek(0, 2)
    size = f.tell()
    f.seek(max(0, size - 3_000_000))
    buf = f.read().decode("gbk", "replace")
lh = {}
for line in buf.splitlines():
    ts = parse_ts(line)
    if ts is None or ts < WIN_START or ts > WIN_END:
        continue
    if "database is locked" in line:
        lh[ts.strftime("%H")] = lh.get(ts.strftime("%H"), 0) + 1
for h in sorted(lh):
    print(f"  {h}时: {lh[h]}")
