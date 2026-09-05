# -*- coding: utf-8 -*-
"""R19 日志只读扫描（GBK 解码，ASCII 锚点），确认：AI 持仓评估活跃度 / 强制平仓无评估 /
重启 / database is locked / cycle timeout / 人工全平"""
import re, sys, collections, datetime
sys.stdout.reconfigure(encoding="utf-8")
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
now = datetime.datetime.now()
try:
    data = open(LOG, "rb").read().decode("gbk", "replace")
except Exception as e:
    print("LOG ERR", e); raise SystemExit
lines = data.splitlines()
N = len(lines)
print(f"日志总行数 = {N}")

def count(anchor, level=None):
    c = 0
    for ln in lines:
        if anchor in ln and (level is None or f"| {level} |" in ln or f"|{level}|" in ln):
            c += 1
    return c

def recent(anchor, minutes=30):
    """返回最近 N 分钟内命中的时间戳列表（粗解析 [HH:MM:SS] 或 ISO）"""
    pat = re.compile(r"(\d{2}):(\d{2}):(\d{2})")
    out = []
    for ln in lines:
        if anchor in ln:
            m = pat.search(ln)
            if m:
                out.append(m.group(0))
    return out

print("-"*80)
print("[A] AI 持仓评估活跃度（最近命中时间戳）")
for a in ("smart_exit:evaluate_position", "持仓全景", "evaluate_exits", "_manage_positions", "辩论引擎", "meta_agent:adjudicate"):
    hits = recent(a, 60)
    print(f"  {a:30s} 末次命中 = {hits[-1] if hits else '无'}  总命中={len(hits)}")

print("-"*80)
print("[B] 人工全平 / flatten")
f1 = count("emergency:_flatten_all_sync")
print(f"  emergency:_flatten_all_sync = {f1}")
# 取末次
flat = [l for l in lines if "emergency:_flatten_all_sync" in l]
print(f"  末次: {flat[-1][:160] if flat else '无'}")

print("-"*80)
print("[C] 重启 / 启动")
starts = count("Application startup complete") + count("Started server process")
print(f"  startup 标记 = {starts}（本日志失效，改用 health.uptime 反推）")

print("-"*80)
print("[D] database is locked（按小时粗分，取时间戳）")
locks = [l for l in lines if "database is locked" in l]
print(f"  总命中 = {len(locks)}")
hrs = collections.Counter()
for l in locks:
    m = re.search(r"(\d{2}):(\d{2}):(\d{2})", l)
    if m: hrs[m.group(1)] += 1
if hrs: print("  按小时:", dict(sorted(hrs.items())))

print("-"*80)
print("[E] cycle timeout")
to = count("_run_cycle_with_timeout") + count("run_cycle_for_user")
print(f"  cycle timeout 锚点 = {to}")

print("-"*80)
print("[F] DeepSeek 402")
ds402 = count("402")  # 粗略
ds402line = [l for l in lines if "402" in l and ("Insufficient" in l or "Balance" in l)]
print(f"  DeepSeek 402 行数 = {len(ds402line)}  末次: {ds402line[-1][:120] if ds402line else '无'}")

print("-"*80)
print("[G] 14:53-15:52 强制平仓无评估检查（unverified 窗口）")
# 找该时段的 mt5_closed_external_unverified / 人工全平 / 有无同期评估
win = [l for l in lines if re.search(r"(14:5[3-9]|15:[0-5]\d)", l)]
ev = collections.Counter()
for l in win:
    for k in ("flatten","持仓全景","smart_exit","reconcile","挂单","order","move_sl","SL"):
        if k in l: ev[k]+=1
print(f"  该窗口日志行数 = {len(win)}  事件计数 = {dict(ev)}")

print("-"*80)
print("[H] 最新 5 个有 HH:MM:SS 的评估/持仓类日志（看实时状态）")
seen = []
pat = re.compile(r"(\d{2}):(\d{2}):(\d{2}).{0,40}(持仓全景|smart_exit|evaluate_position|辩论|MetaAgent|融合|fusion)")
for l in lines[-3000:]:
    m = pat.search(l)
    if m: seen.append(m.group(0))
for s in seen[-8:]:
    print("  ", s)
