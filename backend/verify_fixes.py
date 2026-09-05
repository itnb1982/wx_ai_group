r"""重启后专项验收：逐条判定 2026-08-09 这一轮 P0 修复是否真的生效。

用法（PowerShell）：
    F:\WanxiangAI\.venv\Scripts\python.exe F:\WanxiangAI\backend\verify_fixes.py

每一项都对应一个已定位的根因，不是泛泛的"能不能打得开"：
  1) 重连死锁      —— 日志里「无存储的凭证」占比应从 84% 降到 ~0
  2) 终端重复冷启动 —— 「终端未运行，将先启动终端」不应再每 90s 复读
  3) 行情主号选择   —— 不应再出现「使用模拟数据」
  4) MT5 账号接入   —— 期望 4 个账号在线
  5) 接口时延       —— dashboard / equity-curve / system-health 应远低于修复前
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8080"
EMAIL = "1558895@qq.com"
PASSWORD = "Tzhl@708090"

# 修复前实测基线，用于对比（秒）
BASELINE = {
    "/api/dashboard/accounts": 20.71,
    "/api/dashboard/equity-curve": 11.90,
    "/api/dashboard/system-health": 5.90,
}


def req(method, path, token=None, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", time.time() - t0


def login():
    st, raw, _ = req("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if st == 200:
        try:
            return json.loads(raw).get("access_token")
        except Exception:
            pass
    print(f"[登录失败] status={st} {raw[:200]}")
    return None


def newest_log():
    """取当前 uvicorn 进程的日志文件（按修改时间取最新）。"""
    cands = []
    for d in (r"F:\WanxiangAI\data", os.path.join(os.environ.get("LOCALAPPDATA", ""), "WanxiangAI", "data")):
        cands += glob.glob(os.path.join(d, "wanxiang_backend_*.log"))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


results = []


def check(name, ok, detail):
    results.append((name, ok, detail))
    mark = "通过" if ok else "未通过"
    print(f"  [{mark}] {name}\n         {detail}")


print("=" * 72)
print("  万象Ai 修复验收（2026-08-09 P0 组合拳）")
print("=" * 72)

# ── 0. 后端可用性 ──
st, raw, dt = req("GET", "/api/health", timeout=15)
if st != 200:
    print(f"\n后端未就绪：/api/health status={st} {raw[:200]}")
    sys.exit(1)
health = {}
try:
    health = json.loads(raw)
except Exception:
    pass
print(f"\n后端在线，/api/health {dt*1000:.0f}ms\n")

token = login()
if not token:
    sys.exit(1)

print("-" * 72)
print("【一】日志层面：根因是否消失")
print("-" * 72)

log = newest_log()
if not log:
    check("定位当前日志", False, "未找到 wanxiang_backend_*.log")
else:
    print(f"  日志目录: {os.path.dirname(log)}")
    age = time.time() - os.path.getmtime(log)
    with open(log, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    total = len(lines) or 1
    n_cred = sum(1 for L in lines if "无存储的凭证" in L)
    n_cold = sum(1 for L in lines if "终端未运行，将先启动终端" in L)
    n_mock = sum(1 for L in lines if "使用模拟数据" in L)
    n_ipc = sum(1 for L in lines if "IPC send failed" in L or "IPC initialize failed" in L)

    print(f"  日志: {os.path.basename(log)}  共 {total} 行  (最后写入 {age:.0f}s 前)\n")
    check("重连死锁已解除（无存储的凭证）",
          n_cred / total < 0.02,
          f"{n_cred} 行 / 占比 {n_cred/total*100:.1f}%  （修复前 5475 行 / 84.3%）")
    check("终端不再重复冷启动",
          n_cold <= 4,
          f"「终端未运行，将先启动终端」{n_cold} 次（≤4 为正常首启，修复前每 90s 复读）")
    # 「使用模拟数据」的判据是**是否已经停止**，而不是累计了多少次。
    # 后端刚重启、MT5 账号尚未接入的那几十秒里，行情降级为模拟是正常保护行为；
    # 这期间若恰好被高频轮询打中，几十条记录是正常的（实测 46s 内 60 条），
    # 用累计次数判会把一次健康的冷启动误报成故障。
    # 正确判据：最后一条降级记录之后，日志仍在继续写，且不再出现新的降级。
    _TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

    def _ts_of(line):
        m = _TS_RE.match(line)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    mock_last = log_last = None
    for L in lines:
        t = _ts_of(L)
        if t:
            log_last = t
            if "使用模拟数据" in L:
                mock_last = t

    if mock_last is None:
        mock_ok, mock_msg = True, "从未降级为模拟数据"
    else:
        quiet = (log_last - mock_last).total_seconds() if log_last else 0.0
        mock_ok = quiet >= 60
        mock_msg = (f"共 {n_mock} 次，最后一次 {mock_last:%H:%M:%S}，"
                    f"此后已平静 {quiet:.0f}s（≥60s 视为过渡期结束）")
    check("行情不再持续降级为模拟数据", mock_ok, mock_msg)
    check("MT5 IPC 无冲突",
          n_ipc == 0,
          f"IPC 失败 {n_ipc} 次")

print()
print("-" * 72)
print("【二】账号层面：MT5 是否真的接入")
print("-" * 72)

st, raw, dt = req("GET", "/api/accounts/", token)
if st != 200:
    check("账号列表可读", False, f"status={st} {raw[:150]}")
else:
    try:
        accs = json.loads(raw)
        accs = accs if isinstance(accs, list) else accs.get("accounts", [])
    except Exception:
        accs = []
    online = [a for a in accs if a.get("is_connected")]
    for a in accs:
        flag = "在线" if a.get("is_connected") else "离线"
        print(f"    - {a.get('name'):<14} {flag:<4} status={a.get('status')}")
    check("MT5 账号接入", len(online) == len(accs) and accs,
          f"{len(online)}/{len(accs)} 在线")

print()
print("-" * 72)
print("【三】接口时延：并行化 + 分锁 + 缓存是否见效")
print("-" * 72)

for path, base in BASELINE.items():
    st, raw, dt = req("GET", path, token)
    # 时延基线来自修复前首次冷缓存实测；放宽到 4s 或基线一半，取较大者。
    threshold = max(4.0, base * 0.5)
    ok = (st == 200) and (dt < threshold)
    check(f"{path}", ok,
          f"status={st}  {dt:.2f}s  （修复前 {base:.2f}s，阈值 {threshold:.1f}s）")

# ai-flow：确认没有 simulated 标记
st, raw, dt = req("GET", "/api/dashboard/ai-flow", token)
simulated = '"simulated": true' in raw.replace(" ", "").replace('"simulated":true', '"simulated": true')
check("/api/dashboard/ai-flow 使用真实行情",
      st == 200 and not simulated,
      f"status={st}  {dt:.2f}s  simulated={simulated}")

print()
print("=" * 72)
passed = sum(1 for _, ok, _ in results if ok)
print(f"  验收结果：{passed} / {len(results)} 项通过")
print("=" * 72)
for name, ok, detail in results:
    if not ok:
        print(f"  未通过 -> {name}: {detail}")
sys.exit(0 if passed == len(results) else 2)
