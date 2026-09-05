# -*- coding: utf-8 -*-
"""只读：按日/按出场原因统计真实4账号绩效。不修改任何交易数据。"""
import sqlite3
import collections

DB = "F:/WanxiangAI/backend/data/wx_prod.dat"
ACCS = (
    '2877213e-e79f-4ac4-93cd-4db64730bc04',
    'b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd',
    '8ecb1ff9-aa09-4057-9f0e-a87434a29bf3',
    '3540bf33-ee40-4169-8099-7c9616406d99',
)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()
ph = ",".join("?" * len(ACCS))

rows = cur.execute(
    f"""SELECT date(close_time), profit, exit_reason, action
        FROM trades
        WHERE mt5_account_id IN ({ph})
          AND close_time IS NOT NULL
          AND IFNULL(exit_reason,'') <> 'mt5_closed_external'
        ORDER BY close_time""",
    ACCS,
).fetchall()

byday = collections.defaultdict(lambda: {"n": 0, "w": 0, "gp": 0.0, "gl": 0.0})
for d, pnl, _, _ in rows:
    pnl = pnl or 0.0
    b = byday[d]
    b["n"] += 1
    if pnl > 0:
        b["w"] += 1
        b["gp"] += pnl
    else:
        b["gl"] += -pnl

print("=== 按日绩效（真实4账号·剔除外部平仓零值单）===")
print(f"{'日期':12s} {'笔数':>5s} {'胜率':>7s} {'毛盈':>11s} {'毛亏':>11s} {'净利':>12s} {'PF':>7s}")
cum = 0.0
for d in sorted(byday):
    b = byday[d]
    net = b["gp"] - b["gl"]
    cum += net
    pf = (b["gp"] / b["gl"]) if b["gl"] > 0 else 99.0
    wr = b["w"] / b["n"] * 100
    print(f"{d:12s} {b['n']:>5d} {wr:>6.1f}% {b['gp']:>11.2f} {b['gl']:>11.2f} {net:>12.2f} {pf:>7.3f}")
print(f"{'累计':12s} {len(rows):>5d} {'':>7s} {'':>11s} {'':>11s} {cum:>12.2f}")

# 出场原因绩效（近300单）
print("\n=== 出场原因绩效（最近300已平单）===")
rec = cur.execute(
    f"""SELECT exit_reason, profit FROM trades
        WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
          AND IFNULL(exit_reason,'') <> 'mt5_closed_external'
        ORDER BY close_time DESC LIMIT 300""",
    ACCS,
).fetchall()
br = collections.defaultdict(lambda: {"n": 0, "w": 0, "s": 0.0})
for r, pnl in rec:
    r = (r or "?")[:38]
    pnl = pnl or 0.0
    br[r]["n"] += 1
    br[r]["s"] += pnl
    if pnl > 0:
        br[r]["w"] += 1
print(f"{'出场原因':40s} {'笔数':>5s} {'胜率':>7s} {'净利':>12s} {'均单':>9s}")
for r, v in sorted(br.items(), key=lambda x: x[1]["s"]):
    print(f"{r:40s} {v['n']:>5d} {v['w']/v['n']*100:>6.1f}% {v['s']:>12.2f} {v['s']/v['n']:>9.2f}")

# 今日按账号
print("\n=== 今日(08-10)按账号 ===")
t = cur.execute(
    f"""SELECT mt5_account_id, profit FROM trades
        WHERE mt5_account_id IN ({ph}) AND date(close_time)='2026-08-10'
          AND IFNULL(exit_reason,'') <> 'mt5_closed_external'""",
    ACCS,
).fetchall()
nm = {ACCS[0]: "liumanchun1", ACCS[1]: "liumanchuan2", ACCS[2]: "liumanchun3", ACCS[3]: "liumanchun4"}
ba = collections.defaultdict(lambda: {"n": 0, "w": 0, "gp": 0.0, "gl": 0.0})
for a, pnl in t:
    pnl = pnl or 0.0
    x = ba[a]
    x["n"] += 1
    if pnl > 0:
        x["w"] += 1
        x["gp"] += pnl
    else:
        x["gl"] += -pnl
for a, v in ba.items():
    pf = v["gp"] / v["gl"] if v["gl"] > 0 else 99.0
    print(f"  {nm.get(a,a[:8]):14s} n={v['n']:>3d} 胜率={v['w']/v['n']*100:>5.1f}% 净={v['gp']-v['gl']:>9.2f} PF={pf:.3f}")

con.close()
