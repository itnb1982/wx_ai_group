import urllib.request, json
base='http://127.0.0.1:8080'
body=json.dumps({"email":"1558895@qq.com","password":"Tzhl@708090"}).encode()
r=urllib.request.urlopen(urllib.request.Request(base+'/api/auth/login',data=body,headers={'Content-Type':'application/json'}),timeout=15)
tok=json.loads(r.read())['access_token']
h={'Authorization':'Bearer '+tok}
d=json.loads(urllib.request.urlopen(urllib.request.Request(base+'/api/dashboard/accounts',headers=h),timeout=25).read())
print('cache_age_sec=', d.get('cache_age_sec'))
tot=0
for a in d['accounts']:
    print(f"{a['name']:14s} {a['login']} eq={a['equity']:.2f} today={a['today_profit']:.2f} float={a['float_pnl']:.2f} pos={a['position_count']}")
    for p in a['positions']:
        op=p['open_price']; cp=p['current_price']; sl=p['sl']; ty=p['type']
        risk=(sl-op) if ty=='sell' else (op-sl)
        gain=(op-cp) if ty=='sell' else (cp-op)
        lock=(op-sl) if ty=='sell' else (sl-op)
        eff = (lock/gain*100) if gain>0 else None
        print(f"    #{p['ticket']} {ty} {p['volume']} in={op} now={cp} sl={sl} tp={p['tp']} pnl={p['profit']:.2f} 浮盈点={gain:.2f} SL锁定点={lock:.2f} 锁利效率={('%.0f%%'%eff) if eff is not None else 'n/a'} 硬损点={risk:.2f} 持仓{p['holding_minutes']}min")
        tot+=p['profit']
print('合计浮动=',round(tot,2))
pf=d['portfolio']; print('portfolio today=',pf['today_profit'],'positions=',pf['total_positions'],'online=',pf['online'],'equity=',pf['total_equity'])
