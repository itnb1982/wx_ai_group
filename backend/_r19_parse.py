# -*- coding: utf-8 -*-
"""解析 _r19_raw.json，输出结构化摘要（只读）。"""
import json, io, sys
sys.stdout.reconfigure(encoding="utf-8")
d = json.load(open("_r19_raw.json", encoding="utf-8"))

h = d["health"]
print("="*80)
print("[HEALTH]")
for k in ("status","pid","uptime_sec","mt5_connected","accounts_expected","accounts_bootstrap",
          "trade_stale","last_cycle_sec","auto_loop_running","db_ready","l3_guard_alive","follower_alive"):
    print(f"  {k} = {h.get(k)}")
print(f"  emergency = {h.get('emergency')}")

m = d["model"]
print("="*80)
print("[MODEL]")
c = m["chronos"]
print(f"  Chronos: ok={c['calls_ok']} fail={c['calls_fail']} device={c['device']} "
      f"last_ok_ago={c['last_ok_ago_s']}s lat={c['last_latency_ms']}ms mv={c['last_multivariate']} cov={c['last_covariates']}")
q = m["qwen"]
print(f"  Qwen: avail={q['available']} ok={q['calls_ok']} fail={q['calls_fail']} "
      f"last_lat={q['last_latency_ms']}ms p50={q['latency_p50_ms']} p95={q['latency_p95_ms']} "
      f"last_err={q['last_error']!r} act_ago={q['last_activity_ago_s']}s roles={q['roles']['proofreader']}")
for name in ("deepseek","hunyuan"):
    cc = m["cloud"][name]
    print(f"  {name}: ready={cc['ready']} down={cc.get('down')} stale={cc.get('stale')} "
          f"total_ok={cc.get('total_ok')} last_error={cc['last_error']!r}")
print(f"  summary = {m['summary']}")
if "degrade" in m: print(f"  degrade = {m['degrade']}")
print(f"  model_keys = {list(m.keys())}")

print("="*80)
print("[ACCOUNTS]")
for tag in ("first","refreshed"):
    a = d["accounts"][tag]
    print(f"--- {tag} cache_age={a.get('cache_age_sec')} portfolio={a.get('portfolio')}")
    for acc in a["accounts"]:
        if not acc.get("is_trading"): 
            print(f"    (skip non-trading) {acc['name']} login={acc['login']} pos={acc['position_count']}")
            continue
        print(f"    {acc['name']} login={acc['login']} primary={acc['is_primary']} bal={acc['balance']} eq={acc['equity']} "
              f"today={acc['today_profit']} float={acc['float_pnl']} pos={acc['position_count']} orders_today={acc['today_orders']}")
        for p in acc["positions"]:
            print(f"       #{p['ticket']} {p['type']} vol={p['volume']} open={p['open_price']} cur={p['current_price']} "
                  f"sl={p['sl']} tp={p['tp']} pnl={p['profit']} hold={p['holding_minutes']}min open_time={p['open_time']}")

print("="*80)
print("[DB cols]")
print(" ", d["db"]["cols"])
