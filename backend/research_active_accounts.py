import sqlite3, json
from collections import defaultdict
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

ACT = {
    "3540bf33-ee40-4169-8099-7c9616406d99": "主号1610098464",
    "2877213e-e79f-4ac4-93cd-4db64730bc04": "跟号1610093299",
    "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd": "跟号1610097175",
}
qmarks = ",".join("?"*len(ACT))
cur.execute(f"""SELECT mt5_account_id, mt5_ticket, open_time, close_time, action,
                       open_price, close_price, volume, profit, net_profit, result,
                       exit_reason, meta_agent_decision, deepseek_decision, hunyuan_decision,
                       chronos_vote, deepseek_reasoning, hunyuan_reasoning
                FROM trades WHERE mt5_account_id IN ({qmarks})
                ORDER BY mt5_account_id, open_time""", list(ACT.keys()))
rows = cur.fetchall()

def npf(r):
    return r["net_profit"] if r["net_profit"] is not None else (r["profit"] or 0)

by_acc = defaultdict(list)
for r in rows:
    by_acc[r["mt5_account_id"]].append(r)

print("########## 一、各账号总览 ##########")
for uid, label in ACT.items():
    rs = by_acc[uid]
    wins = sum(1 for r in rs if npf(r) > 0)
    loss = sum(1 for r in rs if npf(r) < 0)
    tot = sum(npf(r) for r in rs)
    print(f"[{label}] {uid[:8]} 笔数={len(rs)} 赢={wins} 亏={loss} 净盈亏=${tot:.2f}")

print("\n########## 二、主副一致性核对（按 open_time+action 分组） ##########")
# 以主号为基准，看每笔是否有对应跟号同向
lead = by_acc["3540bf33-ee40-4169-8099-7c9616406d99"]
f1 = {(r["open_time"], r["action"]): r for r in by_acc["2877213e-e79f-4ac4-93cd-4db64730bc04"]}
f2 = {(r["open_time"], r["action"]): r for r in by_acc["b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd"]}
matched1 = matched2 = 0
for r in lead:
    key = (r["open_time"], r["action"])
    if key in f1: matched1 += 1
    if key in f2: matched2 += 1
print(f"主号笔数={len(lead)}  跟号3299同向匹配={matched1}  跟号7175同向匹配={matched2}")
unmatched = [r for r in lead if (r["open_time"], r["action"]) not in f1 or (r["open_time"], r["action"]) not in f2]
if unmatched:
    print("主号未在跟号找到同向的订单（可能为外部平仓/未验证）：")
    for r in unmatched[:20]:
        print(f"  t={r['open_time']} {r['action']} ticket={r['mt5_ticket']} npf={npf(r):.2f} exit={r['exit_reason']}")

print("\n########## 三、主号亏损单明细（错方向研究重点） ##########")
lead_loss = [r for r in lead if npf(r) < 0]
lead_loss.sort(key=lambda r: r["open_time"])
print(f"主号亏损单共 {len(lead_loss)} 笔\n")
for r in lead_loss:
    print(f"--- {r['open_time']} | {r['action']} @ {r['open_price']} -> {r['close_price']} | npf=${npf(r):.2f} | exit={r['exit_reason']} | meta={r['meta_agent_decision']}({r['meta_agent_confidence'] if 'meta_agent_confidence' in r.keys() else '?'})")
    ds = (r['deepseek_reasoning'] or '')[:160].replace('\n',' ')
    hy = (r['hunyuan_reasoning'] or '')[:160].replace('\n',' ')
    print(f"    DS决策={r['deepseek_decision']}  HY决策={r['hunyuan_decision']}  Chronos={r['chronos_vote']}")
    print(f"    DS理由: {ds}")
    print(f"    HY理由: {hy}")
con.close()
