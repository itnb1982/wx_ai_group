import sqlite3
from collections import Counter
DB = r"F:\WanxiangAI\backend\data\wx_prod.dat"
con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
cur=con.cursor()
cur.execute("""SELECT open_time,action,open_price,close_price,profit,net_profit,
  meta_agent_decision,deepseek_decision,hunyuan_decision,chronos_vote,exit_reason
  FROM trades WHERE mt5_account_id='3540bf33-ee40-4169-8099-7c9616406d99'
  AND open_time>='2026-08-13' ORDER BY open_time""")
rows=cur.fetchall()
def npf(r): return r["net_profit"] if r["net_profit"] is not None else (r["profit"] or 0)

def consensus(ds,hy,ch):
    v=[x for x in [ds,hy,ch] if x and x.upper() not in ("HOLD","NONE","","NULL")]
    if not v: return "ABSTAIN"
    c=Counter(x.upper() for x in v)
    return c.most_common(1)[0][0]

contra=0; agree=0; contra_win=0; contra_loss=0; contra_n=0
agree_win=0; agree_n=0
for r in rows:
    ds=(r["deepseek_decision"] or ""); hy=(r["hunyuan_decision"] or ""); ch=(r["chronos_vote"] or "")
    cons=consensus(ds,hy,ch)
    meta=(r["meta_agent_decision"] or "").upper()
    act=r["action"].upper()
    if cons in ("BUY","SELL"):
        if meta!=cons:
            contra+=1
            if npf(r)>0: contra_win+=1
            else: contra_loss+=1
            contra_n+=1
        else:
            agree+=1
            if npf(r)>0: agree_win+=1
            agree_n+=1
print(f"近两日主号 {len(rows)} 笔")
print(f"共识明确(BUY/SELL)的笔数={contra_n+agree_n}")
print(f"  meta 与共识【一致】={agree}  (赢={agree_win}, 胜率={agree_win/max(agree_n,1)*100:.0f}%)")
print(f"  meta 与共识【背离】={contra}  (赢={contra_win}, 亏={contra_loss}, 胜率={contra_win/max(contra_n,1)*100:.0f}%)")

# 哪类背离
print("\n背离明细(meta逆共识):")
for r in rows:
    ds=(r["deepseek_decision"] or ""); hy=(r["hunyuan_decision"] or ""); ch=(r["chronos_vote"] or "")
    cons=consensus(ds,hy,ch); meta=(r["meta_agent_decision"] or "").upper()
    if cons in ("BUY","SELL") and meta!=cons:
        print(f"  {r['open_time'][:16]} 共识={cons}(DS={ds}/HY={hy}/Ch={ch}) -> meta={meta} action={r['action']} npf=${npf(r):.2f} {r['exit_reason']}")
con.close()
