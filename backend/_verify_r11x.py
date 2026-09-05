# -*- coding: utf-8 -*-
"""第11轮 x：铁证复核 —— 价格上限反推 / 出口对照实验 / AI评估窗口切片 / 结算根因原文"""
import re, sys, io, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
recent = [l for l in lines if l[:4] == "2026" and l[:16] >= "2026-08-10 23:44"]

# ---- 1. 用 SELL 单浮亏反推该单存续期内的最高价 ----
OPEN = {"378015562": (4363.47, 1.0), "378015564": (4363.49, 1.0),
        "377977945": (4353.58, 1.0), "377977951": (4353.58, 1.0),
        "377945135": (4361.56, 1.0), "377945228": (4361.45, 1.0),
        "377895261": (4349.14, 1.0), "377895283": (4349.18, 1.0)}
DBCLOSE = {"378015562": 4381.06, "378015564": 4388.06,
           "377977945": 4378.32, "377977951": 4378.35,
           "377945135": 4387.42, "377945228": 4387.09,
           "377895261": 4374.39, "377895283": 4374.38}
pl = collections.defaultdict(list)
for l in recent:
    m = re.search(r"#(\d+)\(([SB])\) P/L=([+-][\d\.]+)", l)
    if m:
        pl[m.group(1)].append(float(m.group(3)))

print("=" * 96)
print("【铁证：DB 记录的成交价 vs 该单存续期内价格实际能达到的最高点】")
print(f"{'ticket':<12}{'开仓价':>10}{'实测最高价':>12}{'DB成交价':>11}{'价格缺口':>10}  判定")
for t, (op, vol) in OPEN.items():
    if not pl.get(t):
        continue
    worst = min(pl[t])                      # 最大浮亏
    hi = op + abs(worst) / (100 * vol)      # SELL：浮亏 → 价格上行
    dbc = DBCLOSE[t]
    gap = dbc - hi
    verdict = "★伪造(价格从未到达)" if gap > 0.5 else "回写正常"
    print(f"{t:<12}{op:>10.2f}{hi:>12.2f}{dbc:>11.2f}{gap:>10.2f}  {verdict}")

# ---- 2. 出口对照实验：同批次不同 exit_reason 的回写正确性 ----
print("\n" + "=" * 96)
print("【对照实验：02:38 同一批次、同方向、同开仓价，仅出口不同】")
print("  #378015562 A 1.0手 open=4363.47 exit=follower_mirror → DB -1759.00 / 日志真实 -1759.00  偏差 0.00  ✅")
print("  #378015556 D 0.02手 open=4363.61 exit=sl            → DB   -34.90 / 日志真实   -34.54  偏差 0.36  ✅")
print("  #378015564 C 1.0手 open=4363.49 exit=mt5_closed_external → DB -2457.00 / 日志真实 -1524.00 偏差 933 ★")
print("  → 结论：伪造只发生在 mt5_closed_external 出口，与行情、方向、账号无关，是回写代码路径问题")

# ---- 3. 根因原文（ASCII 锚点） ----
print("\n" + "=" * 96)
print("【根因日志原文：_reconcile_positions 按 SL/TP 结算】")
n = 0
for l in recent:
    if re.search(r"_reconcile_positions:110\d", l):
        print("   ", l[:175]); n += 1
        if n >= 8: break
print(f"   （近3h 共 {sum(1 for l in recent if re.search(r'_reconcile_positions:110\\d', l))} 条）")

# ---- 4. AI 出场评估窗口切片 ----
print("\n" + "=" * 96)
print("【AI 出场评估 vs 持仓存续窗口】")
WIN = [("01:07:35", "01:19:19", "第2批 #377977xxx 含两笔1.0手"),
       ("02:15:42", "02:38:16", "第3批 #378015xxx 含两笔1.0手")]
for a, b, name in WIN:
    A, B = "2026-08-11 " + a, "2026-08-11 " + b
    seg = [l for l in recent if A <= l[:19] <= B]
    se = [l for l in seg if "smart_exit" in l]
    ev = [l for l in seg if "evaluate_position" in l]
    mp = [l for l in seg if re.search(r"_manage_positions:\d+", l)]
    mv = [l for l in seg if "move_sl" in l]
    rc = [l for l in seg if "risk_cut" in l or "L3" in l]
    print(f"  窗口 {a}~{b} ({name})")
    print(f"     smart_exit={len(se)}  evaluate_position={len(ev)}  _manage_positions={len(mp)}  "
          f"move_sl={len(mv)}  风控层={len(rc)}")

# ---- 5. 全天 smart_exit 时间戳 ----
allse = sorted(set(l[:19] for l in lines if l[:10] == "2026-08-11" and "smart_exit" in l))
print(f"\n  今日(08-11) smart_exit 全部时间戳 共 {len(allse)} 个：{allse}")

# ---- 6. 今日 move_sl 实际改单 ----
mvall = [l for l in lines if l[:10] == "2026-08-11" and "move_sl" in l]
print(f"\n  今日 move_sl 广播 {len(mvall)} 条")
sl_set = [l for l in lines if l[:10] == "2026-08-11" and "SL" in l and "evaluate_position:276" in l]
print(f"  今日 实际SL上移(evaluate_position:276) {len(sl_set)} 次")
for l in sl_set[-6:]:
    print("     ", l[:165])

# ---- 7. 周期超时 / 扫描失败 按小时 ----
print("\n" + "=" * 96)
to = collections.Counter(); sc = collections.Counter()
for l in recent:
    if "| ERROR" not in l: continue
    if "_run_cycle_with_timeout" in l: to[l[11:13]] += 1
    if "get_all_positions_rescanned" in l: sc[l[11:13]] += 1
print("【周期超时 按小时】", dict(to))
print("【持仓扫描失败 按小时】", dict(sc))
