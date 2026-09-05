# -*- coding: utf-8 -*-
import json, urllib.request, time
BASE="http://127.0.0.1:8080"
for i in range(1,5):
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=8) as r:
            d=json.loads(r.read().decode())
        print(f"poll{i} status={d['status']} mt5={d['mt5_connected']} stale={d['trade_stale']} "
              f"last_cycle={round(d['last_cycle_sec'],1)} uptime={round(d['uptime_sec'],0)} "
              f"cycle_running={d['auto_loop_running']}")
    except Exception as e:
        print(f"poll{i} ERR {e}")
    if i<4: time.sleep(20)
