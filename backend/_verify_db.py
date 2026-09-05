"""只读查询 trades 表（file:...?mode=ro，绝不写库）。

口径铁律（首轮踩坑记录）：
  - 必须用 profit，net_profit 恒为 0
  - 统计 PF/胜率时须排除 exit_reason='mt5_closed_external' 的零值单
"""
import sqlite3
from collections import Counter

DB = "F:/WanxiangAI/backend/data/wx_prod.dat"
ACCS = (
    "2877213e-e79f-4ac4-93cd-4db64730bc04",
    "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd",
    "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3",
    "3540bf33-ee40-4169-8099-7c9616406d99",
)
NAME = {
    "2877213e": "liumanchun1(1610093299)",
    "b3db40fd": "liumanchuan2(1610097175)",
    "8ecb1ff9": "liumanchun3(1610093301)",
    "3540bf33": "liumanchun4(1610098464)",
}

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row
cur = con.cursor()
ph = ",".join("?" * len(ACCS))


def pnl(r):
    """铁律：用 profit，不用 net_profit（后者恒 0）。"""
    v = r["profit"]
    return v if v is not None else 0.0


print("真实4账号 trades 总数:",
      cur.execute(f"SELECT COUNT(*) c FROM trades WHERE mt5_account_id IN ({ph})", ACCS).fetchone()["c"])

print("\n-- 已平/未平 --")
for row in cur.execute(
    f"""SELECT CASE WHEN close_time IS NULL THEN '未平' ELSE '已平' END st, COUNT(*) c
        FROM trades WHERE mt5_account_id IN ({ph}) GROUP BY st""", ACCS):
    print(f"  {row['st']}: {row['c']}")

print("\n-- 今日(08-10)已平单 全量 --")
q4 = f"""SELECT mt5_account_id, mt5_ticket, action, volume, open_price, close_price,
         profit, exit_reason, open_time, close_time, sl, tp, mfe, mae, result,
         meta_agent_confidence, q_score, chronos_vote
         FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
         AND close_time >= '2026-08-10' ORDER BY close_time DESC"""
t = cur.execute(q4, ACCS).fetchall()
print(f"  今日已平 {len(t)} 单")
for x in t:
    print(f"   {str(x['close_time'])[:19]} {NAME.get(x['mt5_account_id'][:8],'?')} #{x['mt5_ticket']} "
          f"{x['action']} v={x['volume']} in={x['open_price']} out={x['close_price']} "
          f"pnl={pnl(x):.2f} reason={x['exit_reason']} sl={x['sl']} tp={x['tp']} "
          f"mfe={x['mfe']} mae={x['mae']} q={x['q_score']} conf={x['meta_agent_confidence']}")

real_t = [x for x in t if x["exit_reason"] != "mt5_closed_external"]
print(f"\n  [今日·剔除外部平仓零值单] n={len(real_t)}")
if real_t:
    w = [pnl(p) for p in real_t if pnl(p) > 0]
    l = [pnl(p) for p in real_t if pnl(p) <= 0]
    gp, gl = sum(w), abs(sum(l))
    print(f"   盈={len(w)} 亏={len(l)} 毛盈={gp:.2f} 毛亏={gl:.2f} 净={gp-gl:.2f} "
          f"PF={(gp/gl if gl else 0):.3f} 胜率={len(w)/len(real_t)*100:.1f}%")
    print("   出场原因:", dict(Counter(p["exit_reason"] for p in real_t)))

print("\n-- 最近200已平单盈亏分布（剔除外部平仓）--")
q2 = f"""SELECT profit, net_profit, exit_reason, mt5_account_id, close_time, mfe, mae, action
         FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
         AND (exit_reason IS NULL OR exit_reason <> 'mt5_closed_external')
         ORDER BY close_time DESC LIMIT 200"""
pr = cur.execute(q2, ACCS).fetchall()
wins = [pnl(p) for p in pr if pnl(p) > 0]
loss = [pnl(p) for p in pr if pnl(p) <= 0]
print(f"  样本={len(pr)} 盈={len(wins)} 亏={len(loss)}")
if pr:
    gp, gl = sum(wins), abs(sum(loss))
    print(f"  毛盈={gp:.2f} 毛亏={gl:.2f} 净={gp-gl:.2f} PF={(gp/gl if gl else 0):.3f} 胜率={len(wins)/len(pr)*100:.1f}%")
    if wins:
        print(f"  平均盈={sum(wins)/len(wins):.2f} 最大盈={max(wins):.2f}")
    if loss:
        print(f"  平均亏={sum(loss)/len(loss):.2f} 最大亏={min(loss):.2f}")
    print("  出场原因:", dict(Counter(p["exit_reason"] for p in pr)))
    print("  方向分布:", dict(Counter(p["action"] for p in pr)))
    print("  时间跨度:", str(pr[-1]["close_time"])[:19], "→", str(pr[0]["close_time"])[:19])

print("\n-- 按账号统计（最近200已平·剔除外部）--")
for a in ACCS:
    sub = [p for p in pr if p["mt5_account_id"] == a]
    if not sub:
        continue
    w = [pnl(p) for p in sub if pnl(p) > 0]
    l = [pnl(p) for p in sub if pnl(p) <= 0]
    gp, gl = sum(w), abs(sum(l))
    print(f"  {NAME[a[:8]]}: n={len(sub)} 胜={len(w)} 净={gp-gl:.2f} PF={(gp/gl if gl else 0):.3f}")

print("\n-- DB 未平单 --")
q3 = f"""SELECT mt5_account_id, mt5_ticket, action, volume, open_price, sl, tp,
         profit, open_time, exit_reason FROM trades
         WHERE mt5_account_id IN ({ph}) AND close_time IS NULL ORDER BY open_time DESC LIMIT 30"""
for x in cur.execute(q3, ACCS).fetchall():
    print(f"  {str(x['open_time'])[:19]} {NAME.get(x['mt5_account_id'][:8],'?')} #{x['mt5_ticket']} "
          f"{x['action']} v={x['volume']} in={x['open_price']} sl={x['sl']} tp={x['tp']} pnl={x['profit']}")

print("\n-- 外部平仓零值单占比（全历史）--")
tot = cur.execute(f"SELECT COUNT(*) c FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL", ACCS).fetchone()["c"]
ext = cur.execute(f"SELECT COUNT(*) c FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL AND exit_reason='mt5_closed_external'", ACCS).fetchone()["c"]
print(f"  已平总数={tot} 外部平仓={ext} 占比={ext/tot*100 if tot else 0:.1f}%")

print("\n-- 全历史（剔除外部平仓）--")
qa = f"""SELECT profit FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
         AND (exit_reason IS NULL OR exit_reason <> 'mt5_closed_external')"""
allr = cur.execute(qa, ACCS).fetchall()
aw = [r["profit"] or 0 for r in allr if (r["profit"] or 0) > 0]
al = [r["profit"] or 0 for r in allr if (r["profit"] or 0) <= 0]
gp, gl = sum(aw), abs(sum(al))
print(f"  n={len(allr)} 胜={len(aw)} 净={gp-gl:.2f} PF={(gp/gl if gl else 0):.3f} 胜率={len(aw)/len(allr)*100 if allr else 0:.1f}%")

print("\n-- acc_exit_0001 假数据条数 --")
print("  ", cur.execute("SELECT COUNT(*) c FROM trades WHERE mt5_account_id='acc_exit_0001'").fetchone()["c"])

print("\n-- 训练特征填充率（全历史真实4账号已平单）--")
for col in ("mfe", "mae", "chronos_vote", "q_score", "meta_agent_confidence"):
    try:
        c = cur.execute(
            f"SELECT COUNT(*) c FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL "
            f"AND {col} IS NOT NULL AND {col} <> 0", ACCS).fetchone()["c"]
        print(f"  {col}: {c}/{tot} = {c/tot*100 if tot else 0:.1f}%")
    except Exception as e:
        print(f"  {col}: ERR {e}")

con.close()
