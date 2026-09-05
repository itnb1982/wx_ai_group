"""R17 只读 DB 分析：SL 伪造检测 / 出场归因 / 特征填充 / 盈亏分布。"""
import sqlite3
from collections import Counter, defaultdict

DB = "F:/WanxiangAI/backend/data/wx_prod.dat"
ACCS = (
    "2877213e-e79f-4ac4-93cd-4db64730bc04",
    "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd",
    "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3",
    "3540bf33-ee40-4169-8099-7c9616406d99",
)
NAME = {
    "2877213e": "lmc1(跟)",
    "b3db40fd": "lmc2(跟)",
    "8ecb1ff9": "lmc3(跟)",
    "3540bf33": "lmc4(主)",
}
TODAY = "2026-08-11"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
con.row_factory = sqlite3.Row
cur = con.cursor()
ph = ",".join("?" * len(ACCS))


def nm(a):
    return NAME.get(a[:8], a[:8])


print("=" * 70)
print("[1] DB 未平单 (close_time IS NULL)")
rows = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,sl,tp,profit,open_time,exit_reason
    FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL ORDER BY open_time DESC""", ACCS).fetchall()
print(f"  共 {len(rows)} 笔")
for x in rows:
    print(f"   {str(x['open_time'])[:19]} {nm(x['mt5_account_id'])} #{x['mt5_ticket']} {x['action']} "
          f"v={x['volume']} in={x['open_price']} sl={x['sl']} tp={x['tp']} pnl={x['profit']}")

print("=" * 70)
print(f"[2] 今日({TODAY}) 已平单全量 + SL伪造检测 (close_price ≡ sl)")
t = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,close_price,profit,
    exit_reason,open_time,close_time,sl,tp,mfe,mae,result,meta_agent_confidence,q_score,chronos_vote
    FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
    AND close_time >= '{TODAY}' ORDER BY close_time""", ACCS).fetchall()
print(f"  今日已平 {len(t)} 笔")
forged, clean = [], []
for x in t:
    cp, sl, op, vol = x["close_price"], x["sl"], x["open_price"], x["volume"]
    is_forged = False
    tagbits = []
    if cp is not None and sl is not None and abs(cp - sl) < 1e-6:
        is_forged = True
        tagbits.append("close≡sl")
    if cp is not None and op is not None and abs(cp - op) < 1e-6:
        is_forged = True
        tagbits.append("close≡open")
    # 复算公式核对
    est = None
    if None not in (cp, op, vol):
        sign = 1 if str(x["action"]).lower().startswith("b") else -1
        est = (cp - op) * vol * 100 * sign
    tag = ("  <<伪造:" + "+".join(tagbits) + ">>") if is_forged else ""
    (forged if is_forged else clean).append(x)
    print(f"   {str(x['close_time'])[:19]} {nm(x['mt5_account_id'])} #{x['mt5_ticket']} {x['action']} "
          f"v={vol} in={op} out={cp} sl={sl} tp={x['tp']} pnl={x['profit']} "
          f"复算={est if est is None else round(est,2)} reason={x['exit_reason']}{tag}")

sf = sum((x["profit"] or 0) for x in forged)
sc = sum((x["profit"] or 0) for x in clean)
print(f"\n  伪造单 {len(forged)} 笔 合计 {sf:.2f}  |  正常单 {len(clean)} 笔 合计 {sc:.2f}")
print(f"  DB今日全量 {sf+sc:.2f}   （伪造占比 {len(forged)/len(t)*100 if t else 0:.1f}%）")

print("=" * 70)
print("[3] 今日出场归因（分伪造/正常）")
agg = defaultdict(lambda: [0, 0.0, 0])
for x in t:
    cp, sl, op = x["close_price"], x["sl"], x["open_price"]
    isf = (cp is not None and sl is not None and abs(cp - sl) < 1e-6) or \
          (cp is not None and op is not None and abs(cp - op) < 1e-6)
    key = (x["exit_reason"] or "None") + ("[伪造]" if isf else "")
    a = agg[key]
    a[0] += 1
    a[1] += x["profit"] or 0
    if (x["profit"] or 0) > 0:
        a[2] += 1
for k, v in sorted(agg.items(), key=lambda i: i[1][1]):
    print(f"   {k:38s} n={v[0]:3d} 净={v[1]:10.2f} 胜率={v[2]/v[0]*100:5.1f}% 均单={v[1]/v[0]:8.2f}")

print("=" * 70)
print("[4] 今日按账号（剔伪造）")
for a in ACCS:
    sub = [x for x in clean if x["mt5_account_id"] == a]
    if not sub:
        print(f"   {nm(a)}: 无正常单")
        continue
    w = [x["profit"] or 0 for x in sub if (x["profit"] or 0) > 0]
    l = [x["profit"] or 0 for x in sub if (x["profit"] or 0) <= 0]
    gp, gl = sum(w), abs(sum(l))
    print(f"   {nm(a)}: n={len(sub)} 胜={len(w)} 净={gp-gl:9.2f} PF={(gp/gl if gl else 0):6.3f} 胜率={len(w)/len(sub)*100:5.1f}%")

print("=" * 70)
print("[5] 今日特征填充率（已平单）")
tot = len(t)
for col in ("mfe", "mae", "chronos_vote", "q_score", "meta_agent_confidence"):
    c = sum(1 for x in t if x[col] is not None and x[col] != 0)
    print(f"   {col:24s}: {c}/{tot} = {c/tot*100 if tot else 0:.1f}%")

print("=" * 70)
print("[6] 最近平仓 15 笔（跨日，看最新形态）")
r = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,close_price,profit,
    exit_reason,close_time,sl,tp FROM trades WHERE mt5_account_id IN ({ph})
    AND close_time IS NOT NULL ORDER BY close_time DESC LIMIT 15""", ACCS).fetchall()
for x in r:
    cp, sl = x["close_price"], x["sl"]
    f = "  <<close≡sl>>" if (cp is not None and sl is not None and abs(cp - sl) < 1e-6) else ""
    print(f"   {str(x['close_time'])[:19]} {nm(x['mt5_account_id'])} #{x['mt5_ticket']} {x['action']} "
          f"v={x['volume']} out={cp} sl={sl} tp={x['tp']} pnl={x['profit']} r={x['exit_reason']}{f}")

print("=" * 70)
print("[7] 今日开仓节奏（按小时，按账号）")
o = cur.execute(f"""SELECT mt5_account_id, substr(open_time,12,2) hh, COUNT(*) c
    FROM trades WHERE mt5_account_id IN ({ph}) AND open_time >= '{TODAY}'
    GROUP BY mt5_account_id, hh ORDER BY hh""", ACCS).fetchall()
hh = defaultdict(dict)
for x in o:
    hh[x["hh"]][nm(x["mt5_account_id"])] = x["c"]
for k in sorted(hh):
    print(f"   {k}时: {dict(sorted(hh[k].items()))}  合计={sum(hh[k].values())}")

con.close()
