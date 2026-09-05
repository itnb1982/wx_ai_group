# -*- coding: utf-8 -*-
"""第7轮补充：重启窗口(21:28-21:40) 日志原文 + 现价 + 批量外部平仓因果链"""
import os, io, sys, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

LOG = r"F:/WanxiangAI/backend/supervisor_uvicorn.log"
size = os.path.getsize(LOG)
with open(LOG, 'rb') as f:
    f.seek(max(0, size - 20 * 1024 * 1024))
    tail = f.read().decode('utf-8', errors='replace').splitlines()

print(f"尾部 {len(tail)} 行\n")

print("===== ① 21:28-21:40 关键事件（重启窗口） =====")
KEY = ['Started server process', 'Shutting down', 'Application startup',
       'mt5_closed_external', 'close_position', 'reconcile', 'orphan',
       'get_all_positions_rescanned', '_manage_positions', 'l3_guard',
       'follower', 'Waiting for application shutdown']
for l in tail:
    ts = l[11:19]
    if '21:28:00' <= ts <= '21:40:00':
        if any(k in l for k in KEY):
            # 只打印 ASCII 可辨识部分
            lvl = 'ERR ' if '| ERROR' in l else ('WARN' if '| WARNING' in l else 'INFO')
            hit = [k for k in KEY if k in l]
            print(f"  {ts} [{lvl}] {'/'.join(hit)}")

print("\n===== ② 平仓动作锚点全局计数（尾部窗口） =====")
for k in ['mt5_closed_external', 'close_position', 'reconcile', 'orphan',
          '_manage_positions', 'l3_guard_loop', 'follower_sync']:
    hits = [l for l in tail if k in l]
    late = [l for l in hits if l[11:19] >= '21:30:00']
    print(f"  {k:26s} 合计 {len(hits):5d} / 21:30后 {len(late)}")

print("\n===== ③ 现价推算：最近含 4 位数价格的行情/裁决行 =====")
prices = []
for l in tail[-4000:]:
    if 'debate_engine:decide:349' in l or 'chronos' in l.lower():
        for m in re.finditer(r'\b(43\d{2}\.\d{1,3})\b', l):
            prices.append((l[11:19], float(m.group(1))))
for ts, p in prices[-12:]:
    print(f"  {ts}  {p}")

print("\n===== ④ 最近 10 条 TP天花板 / regime =====")
for l in [x for x in tail if 'debate_engine:decide:349' in x][-10:]:
    ts = l[11:19]
    rg = re.search(r'regime=(\w+)', l)
    q = re.search(r'Q=([\d.]+)', l)
    tp = re.search(r'=(\d{4}\.\d+)', l)
    d = 'BUY' if 'BUY' in l else ('SELL' if 'SELL' in l else 'NEUTRAL')
    print(f"  {ts} regime={rg.group(1) if rg else '?':6s} Q={q.group(1) if q else '?'} Chronos={d:7s} TP天花板={tp.group(1) if tp else '?'}")

print("\n===== ⑤ 最近 6 条 MetaAgent 裁决原始(数字部分) =====")
for l in [x for x in tail if 'adjudicate:951' in x][-6:]:
    ts = l[11:19]
    d = 'BUY' if 'BUY' in l else ('SELL' if 'SELL' in l else '?')
    nums = re.findall(r'[:：](\d\.\d{2})', l)
    print(f"  {ts} {d:5s} 置信/权重 = {nums}")

print("\n===== ⑥ 21:30 后 持仓评估(evaluate_position) 时间戳全量 =====")
ev = [l[11:19] for l in tail if 'smart_exit:evaluate_position' in l and l[11:19] >= '21:30:00']
print(f"  共 {len(ev)} 次: {', '.join(ev)}")

print("\n===== ⑦ 22时 开仓相关锚点 =====")
for k in ['execute_trade', 'order_send', 'trade_executor:open', 'MetaAgent', 'intent=open']:
    hits = [l for l in tail if k in l and l[11:13] == '22']
    print(f"  {k:24s} 22时 {len(hits)} 条")
