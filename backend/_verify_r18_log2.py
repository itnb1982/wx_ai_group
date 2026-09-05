"""R18 细化日志扫描：时间分布 + 重启后持仓是否被评估 + SL伪造时间 + DS402小时分布。"""
import re
from datetime import datetime

LOG = "F:/WanxiangAI/backend/supervisor_uvicorn.log"
NOW = datetime(2026, 8, 11, 14, 57, 29)
WIN = (datetime(2026, 8, 11, 13, 0, 0), NOW)


def parse_ts(line):
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None


def load():
    with open(LOG, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 3_000_000))
        return f.read().decode("gbk", "replace")


buf = load()
lines = buf.splitlines()

# 1. smart_exit_eval 时间分布
print("=== smart_exit:evaluate_position 命中时间 ===")
se = [parse_ts(l) for l in lines if "smart_exit:evaluate_position" in l]
se = [t for t in se if t and WIN[0] <= t <= WIN[1]]
for t in se:
    print("  ", t.strftime("%H:%M:%S"))

# 2. 开仓时间 (execute_cycle 主号 / copy_order 跟单)
print("\n=== 开仓动作时间 (execute_cycle/copy_order) ===")
for l in lines:
    t = parse_ts(l)
    if not t or not (WIN[0] <= t <= WIN[1]):
        continue
    if "execute_cycle:2030" in l or "copy_order:2356" in l or "开仓" in l or "ticket=" in l:
        if "trade_executor" in l:
            print("  ", t.strftime("%H:%M:%S"), l.strip()[:120])

# 3. reconcile:1106 时间 + 紧邻平仓
print("\n=== _reconcile_positions:1106 (SL伪造兜底) 时间 ===")
rec = [parse_ts(l) for l in lines if "_reconcile_positions:1106" in l]
rec = [t for t in rec if t and WIN[0] <= t <= WIN[1]]
for t in rec:
    print("  ", t.strftime("%H:%M:%S"))

# 4. DS402 按小时
print("\n=== DeepSeek 402 按小时 (本窗口+前后) ===")
dsh = {}
for l in lines:
    t = parse_ts(l)
    if not t or not (datetime(2026, 8, 11, 8, 0) <= t <= WIN[1]):
        continue
    if "402 Insufficient Balance" in l or "Insufficient Balance" in l:
        dsh[t.strftime("%H")] = dsh.get(t.strftime("%H"), 0) + 1
for h in sorted(dsh):
    print(f"  {h}时: {dsh[h]}")

# 5. 人工全平时间
print("\n=== 人工一键全平 (emergency:_flatten_all_sync) 时间 ===")
for l in lines:
    t = parse_ts(l)
    if not t or not (WIN[0] <= t <= WIN[1]):
        continue
    if "emergency:_flatten_all_sync" in l:
        print("  ", t.strftime("%H:%M:%S"), l.strip()[:110])

# 6. 重启反推：health uptime 588s -> started ~14:47:50. 看 14:47-14:49 区间日志密集度
print("\n=== 14:46-14:50 区间关键行 (判断重启) ===")
for l in lines:
    t = parse_ts(l)
    if not t or not (datetime(2026, 8, 11, 14, 46) <= t <= datetime(2026, 8, 11, 14, 50)):
        continue
    if any(k in l for k in ("flatten", "startup", "shutdown", "Traceback", "ERROR", "重启", "reload", "reconcile", "execute_cycle", "copy_order")):
        print("  ", t.strftime("%H:%M:%S"), l.strip()[:110])
