# -*- coding: utf-8 -*-
"""R19 持仓强刷 + 硬损距离核算（只读）"""
import json, base64, hmac, hashlib, time, urllib.request, sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8")
BASE="http://127.0.0.1:8080"
SECRET="5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt():
    h=b64u(json.dumps({"alg":"HS256","typ":"JWT"}).encode()); n=int(time.time())
    p=b64u(json.dumps({"sub":"6f50aea4-7879-4d6d-8046-9b9d9f1989a3","email":"1558895@qq.com","exp":n+3600,"iat":n}).encode())
    s=hmac.new(SECRET.encode(),f"{h}.{p}".encode(),hashlib.sha256).digest()
    return f"{h}.{p}.{b64u(s)}"
def get(u):
    r=urllib.request.Request(u); r.add_header("Authorization",f"Bearer {jwt()}")
    with urllib.request.urlopen(r,timeout=15) as x: return json.loads(x.read().decode())

# 取两次（stale-while-revalidate：首调可能陈旧）
a1=get(f"{BASE}/api/dashboard/accounts"); time.sleep(1.5)
a2=get(f"{BASE}/api/dashboard/accounts")
print("="*95)
print(f"首调 cache_age={a1.get('cache_age_sec')}  强刷 cache_age={a2.get('cache_age_sec')}")
print(f"首调 portfolio: pos={a1['portfolio']['total_positions']} eq={a1['portfolio']['total_equity']} today={a1['portfolio']['today_profit']}")
print(f"强刷 portfolio: pos={a2['portfolio']['total_positions']} eq={a2['portfolio']['total_equity']} today={a2['portfolio']['today_profit']}")
print(f"快照差(equity) = {a2['portfolio']['total_equity']-a1['portfolio']['total_equity']:+.2f}")

print("="*95)
print("[强刷真实持仓 + 硬损距离]  (SL距现价 = 还能亏多少点)")
tot_float=0.0; tot_lot=0.0; rows=[]
for acc in a2["accounts"]:
    if not acc.get("is_trading"): continue
    for p in acc["positions"]:
        typ=p["type"]; op=p["open_price"]; cur=p["current_price"]; sl=p["sl"]; tp=p["tp"]
        # SELL: 亏损方向=价涨；SL 在上方
        if typ=="sell":
            sl_dist = sl-cur          # 距SL还有多少点（正=未触）
            sl_from_open = sl-op
            pts_now = op-cur          # 当前盈亏点数(正=盈)
        else:
            sl_dist = cur-sl
            sl_from_open = op-sl
            pts_now = cur-op
        risk_usd = (sl_dist)*p["volume"]*100
        rows.append((acc["name"],p["ticket"],typ,p["volume"],op,cur,sl,tp,p["profit"],
                     pts_now,sl_dist,sl_from_open,risk_usd,p["holding_minutes"]))
        tot_float+=p["profit"]; tot_lot+=p["volume"]
rows.sort(key=lambda r:r[8])
print(f"{'账号':<13}{'ticket':<11}{'向':<5}{'手数':<6}{'开仓':<9}{'现价':<9}{'SL':<9}{'TP':<9}{'浮盈$':>9}{'点数':>7}{'距SL点':>8}{'SL宽':>7}{'剩余风险$':>11}{'持仓min':>8}")
for r in rows:
    print(f"{r[0]:<13}{r[1]:<11}{r[2]:<5}{r[3]:<6}{r[4]:<9}{r[5]:<9}{r[6]:<9}{r[7]:<9}{r[8]:>9.2f}{r[9]:>7.2f}{r[10]:>8.2f}{r[11]:>7.2f}{r[12]:>11.2f}{r[13]:>8}")
print(f"\n合计: {len(rows)} 笔 / {tot_lot:.2f} 手 / 浮盈 {tot_float:+.2f} / 组合权益 {a2['portfolio']['total_equity']:.2f}")
print(f"浮亏占权益 = {abs(tot_float)/a2['portfolio']['total_equity']*100:.4f}%")
print("按账号 today_profit(MT5 broker日口径):")
for acc in a2["accounts"]:
    if acc.get("is_trading"):
        print(f"  {acc['name']:<13} bal={acc['balance']:>12.2f} eq={acc['equity']:>12.2f} today={acc['today_profit']:>9.2f} float={acc['float_pnl']:>8.2f} pos={acc['position_count']} today_orders={acc['today_orders']}")

# DB 未平对照
con=sqlite3.connect("file:F:/WanxiangAI/backend/data/wx_prod.dat?mode=ro",uri=True,timeout=30)
ai="'2877213e-e79f-4ac4-93cd-4db64730bc04','b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd','8ecb1ff9-aa09-4057-9f0e-a87434a29bf3','3540bf33-ee40-4169-8099-7c9616406d99'"
dbopen=con.execute(f"SELECT mt5_ticket,volume,sl,tp FROM trades WHERE mt5_account_id IN ({ai}) AND close_time IS NULL").fetchall()
con.close()
print(f"\n三源对照: 首调API={a1['portfolio']['total_positions']}  强刷API={len(rows)}  DB未平={len(dbopen)}")
api_tk={r[1] for r in rows}; db_tk={t[0] for t in dbopen}
print(f"  仅DB有(平仓回写滞后/孤儿) = {sorted(db_tk-api_tk)}")
print(f"  仅API有(开仓入库滞后)     = {sorted(api_tk-db_tk)}")
print("  DB sl/tp vs 实盘 sl/tp 漂移（DB为开仓快照，永不更新）:")
dbmap={t[0]:(t[2],t[3]) for t in dbopen}
for r in rows:
    if r[1] in dbmap:
        dsl,dtp=dbmap[r[1]]
        print(f"    #{r[1]}: DB sl={dsl} tp={dtp} | 实盘 sl={r[6]} tp={r[7]} | SL漂移={r[6]-dsl:+.2f} TP{'=0!!' if r[7]==0 else '正常'}")
