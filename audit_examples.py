import sqlite3, json
from datetime import datetime
DB = "F:/WanxiangAI/data/wx_prod.dat"
LID = "2877213e-e79f-4ac4-93cd-4db64730bc04"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
cur = c.cursor()
rows = list(cur.execute(
    "SELECT * FROM trades WHERE mt5_account_id=? AND result NOT LIKE 'pending%' ORDER BY profit ASC",
    (LID,)))
def f2(x): return round(float(x),2) if x is not None else 0.0

print("===== 最惨 12 笔亏损单（含决策轨迹）=====")
for r in rows[:12]:
    print(f"ticket={r['mt5_ticket']} {r['action']} 开{r['open_price']}->平{r['close_price']} "
          f"SL{r['sl']} 盈亏={f2(r['profit'])} 终裁={r['meta_agent_decision']}({f2(r['meta_agent_confidence'])}) "
          f"DS={r['deepseek_decision']}({f2(r['deepseek_confidence'])}) HY={r['hunyuan_decision']}({f2(r['hunyuan_confidence'])}) "
          f"风控通={r['risk_check_passed']} 出场={r['result']}")

print("\n===== 代表性：被L2反转平掉且亏损的单（前8）=====")
rev_loss = [r for r in rows if 'AI反向' in (r['result'] or '') and f2(r['profit'])<0]
for r in rev_loss[:8]:
    print(f"ticket={r['mt5_ticket']} {r['action']} 盈亏={f2(r['profit'])} 终裁={r['meta_agent_decision']}({f2(r['meta_agent_confidence'])}) 出场={r['result']}")

print("\n===== 开仓置信度分布（占总开仓比）=====")
all_rows = list(cur.execute("SELECT meta_agent_confidence FROM trades WHERE mt5_account_id=? AND result NOT LIKE 'pending%'",(LID,)))
import collections
buckets = collections.Counter()
for r in all_rows:
    mc=f2(r['meta_agent_confidence'])
    b = '<0.55' if mc<0.55 else '0.55-0.6' if mc<0.6 else '0.6-0.65' if mc<0.65 else '0.65-0.7' if mc<0.7 else '>=0.7'
    buckets[b]+=1
tot=len(all_rows)
for b in ['<0.55','0.55-0.6','0.6-0.65','0.65-0.7','>=0.7']:
    print(f"  {b:10s}: {buckets[b]:4d}  ({buckets[b]/tot*100:.1f}%)")
print(f"  开仓<0.65 占比: {(buckets['<0.55']+buckets['0.55-0.6']+buckets['0.6-0.65'])/tot*100:.1f}%")

print("\n===== 反转平仓是否看盈亏？grep 验证 =====")
print("  smart_evaluate_position 在返回 reverse_signal 前未判断 profit 字段（代码已确认：88-96行纯方向+置信）")
c.close()
