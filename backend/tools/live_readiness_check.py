# -*- coding: utf-8 -*-
"""实盘准入体检（只读，绝不写库、绝不下单、绝不改任何开关）。

用途
----
回答一个问题：**现在能不能把真实账号的交易开关打开？**

客户诉求原话（2026-08-12）："两个号我是取消交易的，等系统一切正常、并能盈利、
不会开错单，我再自己打开交易。" 本脚本把这三条主观标准翻译成可自动判定的硬门槛，
避免靠感觉拍板。

三大门类（全部通过才建议开启真实账号）
    A 系统健康  —— "一切正常"
    B 方向质量  —— "不会开错单"
    C 盈利能力  —— "能盈利"

运行
----
    python backend/tools/live_readiness_check.py
    python backend/tools/live_readiness_check.py --days 3      # 盈利统计窗口(默认3天)
    python backend/tools/live_readiness_check.py --json        # 机器可读输出

安全性
------
    * DB 一律以 URI `mode=ro` 只读打开，物理上无法写入。
    * 不导入任何决策链/交易模块，不发任何 MT5 命令。
    * 不读写 mt5_accounts.is_trading_enabled，只做展示。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 路径解析
# 可移植铁律：不写死盘符，一律从本文件位置回推项目根。
_THIS = Path(__file__).resolve()
BACKEND_DIR = _THIS.parent.parent          # backend/
PROJECT_ROOT = BACKEND_DIR.parent          # <项目根>/
DB_PATH = BACKEND_DIR / "data" / "wx_prod.dat"
LOG_PATH = BACKEND_DIR / "supervisor_uvicorn.log"
HEALTH_URL = "http://localhost:8080/api/health"

# ---------------------------------------------------------------- 门槛常量
# 依据："多交易多赚钱、PF>1、不爆仓"，配合 XAUUSD 高波动特性设定。
TH = {
    "min_uptime_min": 30,      # 进程连续存活 ≥30min（证明不崩）
    "max_db_locked": 0,        # 近 30min database is locked 次数上限
    "max_traceback": 0,        # 近 30min 未捕获异常次数上限
    "max_pending": 0,          # 活跃账号 pending_verify 上限（账本零失真）
    "min_fusion_avail": 0.80,  # 融合票可用率
    "min_fusion_hit": 0.55,    # 融合票命中率均值
    "min_dir_winrate": 0.45,   # 近 N 笔方向胜率（配合盈亏比即可正收益）
    "max_one_side": 0.95,      # 近 N 笔单边占比上限（>95% 单边 ⇒ 疑似权重劫持）
    "recent_n": 20,            # 方向质量取样笔数
    "min_pf": 1.20,            # Profit Factor
    "max_consec_loss": 5,      # 最大连亏笔数
    "max_daily_dd_pct": 5.0,   # 单日最大回撤占本金百分比
    "min_profit_days": 2,      # 窗口内需盈利的天数
}

OK, BAD, WARN = "PASS", "FAIL", "WARN"


# ---------------------------------------------------------------- 基础设施
def _ro_conn() -> sqlite3.Connection:
    """只读连接。mode=ro 由 SQLite 内核保证物理不可写。"""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.execute("PRAGMA query_only=ON")   # 双保险
    return con


def _read_log_tail(minutes: int) -> list[str]:
    """读近 N 分钟带时间戳的日志行。日志为 GBK 家族编码，须 gb18030 解码。"""
    if not LOG_PATH.exists():
        return []
    cut = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    ts = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    out: list[str] = []
    # 只读末尾 ~40MB，避免整文件扫描
    size = LOG_PATH.stat().st_size
    start = max(0, size - 40 * 1024 * 1024)
    with open(LOG_PATH, "rb") as fh:
        fh.seek(start)
        if start:
            fh.readline()          # 丢弃可能被截断的半行
        for raw in fh:
            try:
                s = raw.decode("gb18030", "replace")
            except Exception:
                s = raw.decode("utf-8", "replace")
            m = ts.match(s)
            if m and m.group(1) >= cut:
                out.append(s.rstrip())
    return out


def _health() -> dict:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


class Report:
    """收集检查项，产出红绿灯汇总。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, group: str, name: str, status: str, actual: str, need: str = "") -> None:
        self.items.append({"group": group, "name": name, "status": status,
                           "actual": str(actual), "need": need})

    def group_ok(self, group: str) -> bool:
        gs = [i for i in self.items if i["group"] == group]
        return bool(gs) and all(i["status"] != BAD for i in gs)

    @property
    def all_ok(self) -> bool:
        return all(i["status"] != BAD for i in self.items) and bool(self.items)


# ---------------------------------------------------------------- A 系统健康
def check_system(rep: Report) -> None:
    G = "A 系统健康"
    h = _health()
    if "_error" in h:
        rep.add(G, "后端进程", BAD, f"接口不可达 {h['_error']}", "/api/health 200")
        return

    up_min = float(h.get("uptime_sec") or 0) / 60
    rep.add(G, "进程连续存活", OK if up_min >= TH["min_uptime_min"] else BAD,
            f"{up_min:.1f} 分钟 (pid={h.get('pid')})", f"≥{TH['min_uptime_min']} 分钟")

    mt5 = h.get("mt5_connected")
    # mt5_connected 可能是 "6/6" 或 数字/布尔，做兼容解析
    mt5_ok, mt5_txt = False, str(mt5)
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", str(mt5).strip())
    if m:
        mt5_ok = m.group(1) == m.group(2) and int(m.group(2)) > 0
    else:
        mt5_ok = bool(mt5)
    rep.add(G, "MT5 账号在线", OK if mt5_ok else BAD, mt5_txt, "全部在线")

    rep.add(G, "自动交易主循环", OK if h.get("auto_loop_running") else BAD,
            str(h.get("auto_loop_running")), "running")

    # DB 模式（WAL 是并发写不锁死的前提）
    try:
        con = _ro_conn()
        jm = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
    except Exception as e:
        jm = f"读取失败 {e}"
    rep.add(G, "SQLite 日志模式", OK if str(jm).lower() == "wal" else BAD, jm, "wal")

    # 近 30min 的锁冲突 / 未捕获异常
    logs = _read_log_tail(30)
    locked = sum(1 for s in logs if "database is locked" in s or "写引擎连接创建失败" in s)
    tb = sum(1 for s in logs if "Traceback" in s or "CRITICAL" in s)
    rep.add(G, "DB 锁冲突(近30min)", OK if locked <= TH["max_db_locked"] else BAD,
            f"{locked} 次", f"≤{TH['max_db_locked']}")
    rep.add(G, "未捕获异常(近30min)", OK if tb <= TH["max_traceback"] else BAD,
            f"{tb} 次", f"≤{TH['max_traceback']}")

    # 账本失真：只统计【已启用交易】账号，停牌账号的历史 pending 不作为阻塞项
    try:
        con = _ro_conn()
        pv = con.execute("""
            SELECT COUNT(*) FROM trades t JOIN mt5_accounts a ON a.id = t.mt5_account_id
            WHERE t.result = 'pending_verify' AND a.is_trading_enabled = 1
        """).fetchone()[0]
        pv_paused = con.execute("""
            SELECT COUNT(*) FROM trades t JOIN mt5_accounts a ON a.id = t.mt5_account_id
            WHERE t.result = 'pending_verify' AND a.is_trading_enabled = 0
        """).fetchone()[0]
        con.close()
    except Exception as e:
        pv, pv_paused = -1, -1
        print(f"[warn] pending 统计失败: {e}", file=sys.stderr)
    rep.add(G, "账本失真(活跃账号待回填)", OK if 0 <= pv <= TH["max_pending"] else BAD,
            f"{pv} 笔", f"≤{TH['max_pending']}")
    if pv_paused > 0:
        rep.add(G, "停牌账号历史待回填", WARN, f"{pv_paused} 笔",
                "不阻塞开启（停牌期无新单）")


# ---------------------------------------------------------------- B 方向质量
def check_direction(rep: Report) -> None:
    G = "B 方向质量"
    logs = _read_log_tail(60)

    # 融合票可用率：出现"融合票不可用"即为一次不可用
    avail_hit = sum(1 for s in logs if "模型融合票" in s)
    avail_miss = sum(1 for s in logs if "融合票不可用" in s)
    tot = avail_hit + avail_miss
    ratio = (avail_hit / tot) if tot else 0.0
    rep.add(G, "融合票可用率(近1h)",
            OK if tot and ratio >= TH["min_fusion_avail"] else (BAD if tot else WARN),
            f"{ratio:.0%} ({avail_hit}/{tot})" if tot else "无样本",
            f"≥{TH['min_fusion_avail']:.0%}")

    # 融合票命中率：日志形如 "命中均=67%"
    hits = [int(x) for x in re.findall(r"命中均?=(\d+)%", "\n".join(logs))]
    if hits:
        avg = sum(hits) / len(hits) / 100
        rep.add(G, "融合票命中率均值", OK if avg >= TH["min_fusion_hit"] else BAD,
                f"{avg:.0%} (n={len(hits)})", f"≥{TH['min_fusion_hit']:.0%}")
    else:
        rep.add(G, "融合票命中率均值", WARN, "无样本(需运行≥10min)",
                f"≥{TH['min_fusion_hit']:.0%}")

    # 实盘方向胜率 + 单边占比（只看已启用交易的账号）
    try:
        con = _ro_conn()
        rows = con.execute(f"""
            SELECT t.action, COALESCE(t.net_profit, t.profit, 0) AS pnl
            FROM trades t JOIN mt5_accounts a ON a.id = t.mt5_account_id
            WHERE a.is_trading_enabled = 1 AND t.close_time IS NOT NULL
              AND t.result IN ('win','loss','breakeven','partial')
            ORDER BY t.close_time DESC LIMIT {TH['recent_n']}
        """).fetchall()
        con.close()
    except Exception as e:
        rep.add(G, "实盘方向胜率", WARN, f"查询失败 {e}", "")
        return

    if not rows:
        rep.add(G, "实盘方向胜率", WARN, "无已平仓样本", f"≥{TH['min_dir_winrate']:.0%}")
        return

    wins = sum(1 for a, p in rows if p > 0)
    wr = wins / len(rows)
    rep.add(G, f"实盘方向胜率(近{len(rows)}笔)", OK if wr >= TH["min_dir_winrate"] else BAD,
            f"{wr:.0%} ({wins}/{len(rows)})", f"≥{TH['min_dir_winrate']:.0%}")

    buys = sum(1 for a, _ in rows if str(a).lower() == "buy")
    side = max(buys, len(rows) - buys) / len(rows)
    rep.add(G, "单边集中度(劫持哨兵)", OK if side <= TH["max_one_side"] else BAD,
            f"{side:.0%} ({'BUY' if buys >= len(rows) - buys else 'SELL'} 占比)",
            f"≤{TH['max_one_side']:.0%}")


# ---------------------------------------------------------------- C 盈利能力
def check_profit(rep: Report, days: int) -> None:
    G = "C 盈利能力"
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        con = _ro_conn()
        rows = con.execute(f"""
            SELECT substr(t.close_time,1,10) AS d,
                   COALESCE(t.net_profit, t.profit, 0) AS pnl,
                   t.close_time
            FROM trades t JOIN mt5_accounts a ON a.id = t.mt5_account_id
            WHERE a.is_trading_enabled = 1 AND t.close_time >= '{since}'
              AND t.result IN ('win','loss','breakeven','partial')
            ORDER BY t.close_time ASC
        """).fetchall()
        bal = con.execute("""
            SELECT COALESCE(SUM(balance),0) FROM mt5_accounts WHERE is_trading_enabled = 1
        """).fetchone()[0] or 0
        con.close()
    except Exception as e:
        rep.add(G, "盈亏统计", BAD, f"查询失败 {e}", "")
        return

    if not rows:
        rep.add(G, "盈亏统计", WARN, f"近{days}天无已平仓样本", "需先跑出交易")
        return

    # 按日汇总
    daily: dict[str, float] = {}
    for d, pnl, _ in rows:
        daily[d] = daily.get(d, 0.0) + float(pnl or 0)
    profit_days = sum(1 for v in daily.values() if v > 0)
    rep.add(G, f"盈利天数(近{days}天)",
            OK if profit_days >= TH["min_profit_days"] else BAD,
            f"{profit_days}/{len(daily)} 天盈利 " +
            " ".join(f"{k[5:]}:{v:+.0f}" for k, v in sorted(daily.items())),
            f"≥{TH['min_profit_days']} 天")

    # Profit Factor
    gross_win = sum(float(p) for _, p, _ in rows if float(p or 0) > 0)
    gross_loss = abs(sum(float(p) for _, p, _ in rows if float(p or 0) < 0))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    rep.add(G, "Profit Factor", OK if pf >= TH["min_pf"] else BAD,
            f"{pf:.2f} (赢{gross_win:+.0f} / 亏-{gross_loss:.0f})", f"≥{TH['min_pf']}")

    net = gross_win - gross_loss
    rep.add(G, f"净盈亏(近{days}天)", OK if net > 0 else BAD,
            f"{net:+.2f} ({len(rows)} 笔)", "> 0")

    # 最大连亏
    cur = mx = 0
    for _, p, _ in rows:
        if float(p or 0) < 0:
            cur += 1
            mx = max(mx, cur)
        else:
            cur = 0
    rep.add(G, "最大连亏", OK if mx <= TH["max_consec_loss"] else BAD,
            f"{mx} 笔", f"≤{TH['max_consec_loss']}")

    # 单日最大亏损占本金比
    worst = min(daily.values()) if daily else 0.0
    dd_pct = (abs(worst) / bal * 100) if (bal and worst < 0) else 0.0
    rep.add(G, "单日最大回撤占本金", OK if dd_pct <= TH["max_daily_dd_pct"] else BAD,
            f"{dd_pct:.2f}% (最差日 {worst:+.0f} / 本金 {bal:.0f})",
            f"≤{TH['max_daily_dd_pct']}%")


# ---------------------------------------------------------------- 账号总览
def account_overview() -> list[dict]:
    try:
        con = _ro_conn()
        rows = con.execute("""
            SELECT name, account_type, is_connected, is_trading_enabled, balance, equity
            FROM mt5_accounts ORDER BY account_type DESC, name
        """).fetchall()
        con.close()
    except Exception:
        return []
    return [{"name": r[0], "type": r[1], "connected": bool(r[2]),
             "trading": bool(r[3]), "balance": r[4], "equity": r[5]} for r in rows]


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="实盘准入体检（只读）")
    ap.add_argument("--days", type=int, default=3, help="盈利统计窗口天数(默认3)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    rep = Report()
    check_system(rep)
    check_direction(rep)
    check_profit(rep, args.days)
    accounts = account_overview()

    verdict = "可以开启真实账号交易" if rep.all_ok else "尚未达标，暂不建议开启"

    if args.json:
        print(json.dumps({"verdict": verdict, "all_ok": rep.all_ok,
                          "items": rep.items, "accounts": accounts},
                         ensure_ascii=False, indent=2))
        return 0 if rep.all_ok else 1

    icon = {OK: "[PASS]", BAD: "[FAIL]", WARN: "[WARN]"}
    W = 68
    print("=" * W)
    print(" 万象Ai 实盘准入体检 ".center(W - 6, " "))
    print(f" {datetime.now():%Y-%m-%d %H:%M:%S}".center(W - 6, " "))
    print("=" * W)

    cur_group = None
    for it in rep.items:
        if it["group"] != cur_group:
            cur_group = it["group"]
            gk = OK if rep.group_ok(cur_group) else BAD
            print(f"\n{icon[gk]} {cur_group}")
            print("-" * W)
        need = f"  [门槛 {it['need']}]" if it["need"] else ""
        print(f"  {icon[it['status']]:<7} {it['name']:<24} {it['actual']}{need}")

    print("\n" + "-" * W)
    print("账号总览（本脚本只读，不改任何开关）")
    for a in accounts:
        tag = "运行中" if a["trading"] else "已停牌"
        print(f"  {tag}  {a['name']:<13} {a['type']:<5} "
              f"conn={int(a['connected'])} 余额={a['balance']} 净值={a['equity']}")

    print("=" * W)
    print(f"结论：{verdict}")
    if not rep.all_ok:
        fails = [i['name'] for i in rep.items if i['status'] == BAD]
        print(f"未达标项（{len(fails)}）：{', '.join(fails)}")
        print("\n开启方式（达标后由你手动操作，系统不会自动开）：")
        print("  前端 → 账号管理 → 目标真实账号 → 打开「启用交易」开关")
        print("  建议：先只开 1 个号、手数 0.01，观察 3 个交易日再开第 2 个。")
    print("=" * W)
    return 0 if rep.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
