import sqlite3, json, re
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
cur=con.cursor()
# 取近期主号、meta逆共识(SELL->BUY)的几笔, 抽 decision_snapshot
cur.execute("""SELECT mt5_ticket,open_time,meta_agent_decision,deepseek_decision,
  hunyuan_decision,chronos_vote,decision_snapshot FROM trades
  WHERE mt5_account_id='3540bf33-ee40-4169-8099-7c9616406d99'
  AND open_time>='2026-08-13' ORDER BY open_time""")
rows=cur.fetchall()
def cons(ds,hy,ch):
    v=[x for x in [ds,hy,ch] if x and str(x).upper() not in ("HOLD","NONE","","NULL")]
    if not v: return "ABSTAIN"
    from collections import Counter
    return Counter(str(x).upper() for x in v).most_common(1)[0][0]
shown=0
for r in rows:
    c=cons(r["deepseek_decision"],r["hunyuan_decision"],r["chronos_vote"])
    if c=="SELL" and (r["meta_agent_decision"] or "").upper()=="BUY":
        snap=r["decision_snapshot"]
        smc="(无快照)"
        if snap:
            try:
                d=json.loads(snap) if isinstance(snap,str) else snap
                # 递归找 global_bias / bullish
                txt=json.dumps(d)
                m=re.search(r'global_bias["\':\s]+([a-zA-Z]+)', txt)
                smc=m.group(1) if m else "(未找到global_bias)"
            except Exception as e:
                smc=f"(解析失败:{e})"
        print(f"ticket={r['mt5_ticket']} {r['open_time'][:16]} meta={r['meta_agent_decision']} 共识=SELL smc_bias={smc}")
        shown+=1
        if shown>=8: break
con.close()
