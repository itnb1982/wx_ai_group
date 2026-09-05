#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B 实验·盯盘采集脚本（周一实盘 walk-forward 用）

功能：
  - 读取生产库 trades 表，统计「某交易日/周窗口」内已平仓单的：
      交易笔数、净盈利(net_profit)、毛盈利、毛亏损、盈利因子 PF、胜率。
  - 附带读取 /api/health 的 gate_stats + debate_ring_enabled（确认实盘加载模式 + 各门触发率）。
  - 把结果写入 ab_results.json（按 --label 覆盖）并追加到 ab_history.jsonl（时间序列）。

口径对齐既有审计脚本 audit_mechanism_20260813.py：直接用 trades.net_profit 求和；
净盈亏读取用 (net_profit or profit or 0) 防御空值（历史教训）。

用法：
  python ab_monitor.py --label W1_OFF --start 2026-08-17 --end 2026-08-19
  python ab_monitor.py --label W2_ON  --start 2026-08-24 --end 2026-08-26
  （--end 缺省=今天；交易时段内可反复运行，每次覆盖同 label 的结果）

依赖：仅 Python 标准库（sqlite3 / json / urllib / argparse / datetime / os）。
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(HERE, "ab_results.json")
HISTORY_PATH = os.path.join(HERE, "ab_history.jsonl")
HEALTH_URL = "http://127.0.0.1:8080/api/health"

# 候选生产库路径（按优先级探测，避免硬编码单一盘符导致拷机失败）
CANDIDATE_DBS = [
    os.environ.get("DATA_DIR", "") + "/wx_prod.dat",
    "F:/WanxiangAI/backend/data/wx_prod.dat",
    "F:/WanxiangAI/data/wx_prod.dat",
    "C:/WanxiangAI/backend/data/wx_prod.dat",
]


def find_db():
    for p in CANDIDATE_DBS:
        p = os.path.normpath(p)
        if p and os.path.exists(p):
            return p
    return None


def fetch_health():
    try:
        req = urllib.request.Request(HEALTH_URL, timeout=5)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def collect(db_path, start, end):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    # 窗口：close_time 落在 [start 00:00, end 23:59:59.999]
    lo = f"{start} 00:00:00"
    hi = f"{end} 23:59:59.999"
    rows = cur.execute(
        """
        SELECT action, net_profit, profit, result, close_time
        FROM trades
        WHERE close_time IS NOT NULL
          AND close_time >= ? AND close_time <= ?
        ORDER BY close_time ASC
        """,
        (lo, hi),
    ).fetchall()
    con.close()

    count = 0
    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    losses = 0
    for r in rows:
        np_ = r["net_profit"]
        if np_ is None:
            np_ = r["profit"] or 0.0
        np_ = float(np_)
        count += 1
        if np_ > 0:
            gross_profit += np_
            wins += 1
        elif np_ < 0:
            gross_loss += abs(np_)
            losses += 1
    net = gross_profit - gross_loss
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = (wins / count) if count else 0.0
    return {
        "count": count,
        "net_profit": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else None,
        "win_rate": round(win_rate, 4),
        "wins": wins,
        "losses": losses,
    }


def main():
    ap = argparse.ArgumentParser(description="A/B 实验·盯盘采集")
    ap.add_argument("--label", required=True, help="窗口标签，如 W1_OFF / W2_ON")
    ap.add_argument("--start", required=True, help="窗口起始日 YYYY-MM-DD")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="窗口结束日 YYYY-MM-DD（缺省=今天）")
    ap.add_argument("--db", default=None, help="生产库路径（缺省自动探测）")
    args = ap.parse_args()

    db = args.db or find_db()
    if not db:
        print("[错误] 未找到生产库 wx_prod.dat，请用 --db 指定。探测路径：")
        for p in CANDIDATE_DBS:
            print("  -", os.path.normpath(p))
        sys.exit(1)

    stats = collect(db, args.start, args.end)
    health = fetch_health()
    live_mode = health.get("debate_ring_enabled")
    gate = health.get("gate_stats", {})

    snap = {
        "label": args.label,
        "start": args.start,
        "end": args.end,
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": db,
        "live_debate_ring_enabled": live_mode,
        "gate_stats": gate,
        "trades": stats,
    }

    # 覆盖写入 ab_results.json 的对应 label
    results = {}
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH, "r", encoding="utf-8") as f:
                results = json.load(f)
        except Exception:
            results = {}
    results[args.label] = snap
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 追加时间序列
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    t = stats
    print(f"=== A/B 采集 [{args.label}] {args.start}~{args.end} ===")
    print(f"生产库      : {db}")
    print(f"实盘辩论环  : {live_mode}  (控制脚本目标应与之一致)")
    print(f"已平仓笔数  : {t['count']}  (胜 {t['wins']} / 负 {t['losses']})")
    print(f"净盈利      : ${t['net_profit']:.2f}")
    print(f"毛盈利/亏损 : ${t['gross_profit']:.2f} / ${t['gross_loss']:.2f}")
    print(f"盈利因子 PF : {t['profit_factor'] if t['profit_factor'] is not None else '∞(无亏损)'}")
    print(f"胜率        : {t['win_rate']*100:.1f}%")
    gs = gate.get("gates", {})
    if gs:
        print(f"gate_stats  : 总决策 {gate.get('total_decisions',0)} | "
              f"辩论环缩权触发 {gs.get('辩论环缩权', 0)} | HOLD {gate.get('holds',0)}")
    print(f"\n结果已写：{RESULTS_PATH}（label={args.label} 覆盖）/ 时间序列 {HISTORY_PATH}")


if __name__ == "__main__":
    main()
