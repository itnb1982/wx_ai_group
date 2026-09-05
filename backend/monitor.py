#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
万象Ai 实盘交易监控系统 — 周期巡检脚本
检查项：后端健康 / MT5连接 / 交易异常 / AI决策异常 / API Key状态
输出：结构化报告 (JSON + 人类可读摘要)
"""
import json, urllib.request, urllib.error, base64, hmac, hashlib, time, sys, os
import sqlite3, subprocess, re
from datetime import datetime, timedelta
from pathlib import Path

# ─── 配置 ───
BACKEND = "http://127.0.0.1:8080"
SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
DB_PATH = "F:/WanxiangAI/data/wx_prod.dat"
LOG_DIR = Path("F:/WanxiangAI/backend")
# 找最新日志文件
def find_latest_log():
    candidates = sorted(LOG_DIR.glob("backend_run_v*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None

def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def mk_token(uid=UID):
    h = b64(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    p = b64(json.dumps({"sub":uid,"email":"x@x.com","exp":int(time.time())+3600}).encode())
    s = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{s}"

def api(method, path, token=None, body=None):
    headers = {}
    if token: headers["Authorization"] = f"Bearer {token}"
    if body is not None: headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    url = f"{BACKEND}{path}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        ct = r.headers.get("content-type", "")
        if ct.startswith("application/json"):
            return r.status, json.loads(r.read().decode() or "null")
        return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        try: j = json.loads(e.read().decode() or "null")
        except: j = e.read().decode()[:300]
        return e.code, j
    except Exception as e:
        return 0, str(e)

def check_db_trades():
    """检查交易记录异常"""
    result = {"total_trades": 0, "recent_24h": 0, "open_positions": 0,
              "consecutive_losses": 0, "large_loss": None, "stuck_positions": [], "errors": []}
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM trades")
        result["total_trades"] = cur.fetchone()[0]

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("SELECT COUNT(*) FROM trades WHERE open_time >= ?", (cutoff,))
        result["recent_24h"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM trades WHERE result IS NULL OR result = ''")
        result["open_positions"] = cur.fetchone()[0]

        # 连续亏损检查
        cur.execute("SELECT result, net_profit, profit FROM trades WHERE result IS NOT NULL AND result != '' ORDER BY id DESC LIMIT 20")
        recent = cur.fetchall()
        consec_loss = 0
        for r in recent:
            pnl = r["net_profit"] if r["net_profit"] is not None else r["profit"]
            if r["result"] == "loss" or (pnl is not None and pnl < 0):
                consec_loss += 1
            else:
                break
        result["consecutive_losses"] = consec_loss

        # 大额亏损
        cur.execute("SELECT MAX(ABS(COALESCE(net_profit, profit, 0))) as max_loss FROM trades WHERE result = 'loss'")
        row = cur.fetchone()
        if row and row["max_loss"] and abs(row["max_loss"]) > 100:
            result["large_loss"] = row["max_loss"]

        # 卡住的持仓（开仓超过48小时未平）
        cur.execute("""SELECT mt5_ticket, symbol, action, open_time, profit, net_profit FROM trades
                       WHERE (result IS NULL OR result = '')
                       AND open_time < ?""", (cutoff,))
        for r in cur.fetchall():
            result["stuck_positions"].append(dict(r))

        # AI activity 异常
        try:
            cur.execute("SELECT COUNT(*) FROM ai_activities WHERE created_at >= ?", (cutoff,))
            result["ai_decisions_24h"] = cur.fetchone()[0]
        except:
            result["ai_decisions_24h"] = "N/A"

        # evolution logs
        try:
            cur.execute("SELECT COUNT(*) FROM evolution_logs WHERE created_at >= ?", (cutoff,))
            result["evolution_count_24h"] = cur.fetchone()[0]
        except:
            result["evolution_count_24h"] = "N/A"

        con.close()
    except Exception as e:
        result["errors"].append(f"DB查询失败: {e}")
    return result

def check_logs_anomalies():
    """检查日志中的异常"""
    log_file = find_latest_log()
    result = {"log_file": str(log_file.name) if log_file else "NONE",
              "errors": [], "warnings": [], "ai_status": {}, "total_errors": 0, "total_warnings": 0}
    if not log_file:
        result["errors"].append("找不到日志文件")
        return result

    try:
        content = log_file.read_text(encoding="utf-8", errors="replace")
        # 清理 ANSI 颜色码
        content = re.sub(r'\x1b\[[0-9;]*m', '', content)
        lines = content.split("\n")

        # 统计 ERROR 和 WARNING
        error_lines = [l for l in lines if "| ERROR" in l]
        warning_lines = [l for l in lines if "| WARNING" in l]
        result["total_errors"] = len(error_lines)
        result["total_warnings"] = len(warning_lines)

        # 最近30行的错误
        recent_errors = error_lines[-10:]
        for l in recent_errors:
            # 提取关键信息
            if "401" in l or "Authentication" in l:
                result["errors"].append(f"API认证失败: {l.strip()[:150]}")
            elif "402" in l or "quota" in l.lower() or "额度" in l:
                result["errors"].append(f"API配额耗尽: {l.strip()[:150]}")
            elif "Connection" in l or "connect" in l.lower():
                result["errors"].append(f"连接异常: {l.strip()[:150]}")
            elif "Exception" in l or "Traceback" in l:
                result["errors"].append(f"代码异常: {l.strip()[:150]}")
            else:
                result["errors"].append(l.strip()[:150])

        recent_warnings = warning_lines[-5:]
        for l in recent_warnings:
            result["warnings"].append(l.strip()[:150])

        # AI 决策状态
        ds_decisions = [l for l in lines if "DeepSeek" in l and "决策" in l]
        hy_errors = [l for l in lines if "混元" in l and "失败" in l]
        debate_results = [l for l in lines if "最终决策" in l]
        degradations = [l for l in lines if "降级" in l]

        result["ai_status"] = {
            "deepseek_decisions": len(ds_decisions),
            "hunyuan_errors": len(hy_errors),
            "debate_results": len(debate_results),
            "degradation_events": len(degradations),
            "last_deepseek": ds_decisions[-1].strip()[:120] if ds_decisions else "NONE",
            "last_debate": debate_results[-1].strip()[:120] if debate_results else "NONE",
        }

        # 检查是否长时间没有决策
        if debate_results:
            # 提取最后一条决策的时间
            last = debate_results[-1]
            time_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", last)
            if time_match:
                last_time = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S")
                gap = (datetime.now() - last_time).total_seconds() / 60
                result["ai_status"]["minutes_since_last_decision"] = round(gap, 1)
                if gap > 60:
                    result["warnings"].append(f"AI决策停滞：距上次决策已 {round(gap)} 分钟")

    except Exception as e:
        result["errors"].append(f"日志读取失败: {e}")
    return result

def check_api_keys():
    """检查 API Key 状态"""
    result = {"deepseek": {}, "hunyuan": {}}
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT provider, COUNT(*) as total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active, SUM(CASE WHEN is_valid=1 THEN 1 ELSE 0 END) as valid FROM api_keys GROUP BY provider")
        for r in cur.fetchall():
            provider = r["provider"]
            result[provider] = {"total": r["total"], "active": r["active"], "valid": r["valid"]}
        con.close()
    except Exception as e:
        result["error"] = str(e)
    return result

def run_monitor():
    """执行完整监控巡检"""
    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall_status": "OK",  # OK / WARNING / CRITICAL
        "issues": [],
    }

    tok = mk_token()

    # 1. 后端健康
    s, d = api("GET", "/api/health")
    if s != 200:
        report["backend_health"] = f"CRITICAL: {s} {d}"
        report["overall_status"] = "CRITICAL"
        report["issues"].append(f"后端不健康: HTTP {s}")
    else:
        report["backend_health"] = "OK (200)"

    # 2. MT5 连接状态 (直接用 /api/accounts/，更可靠)
    s, d = api("GET", "/api/accounts/", tok)
    if s == 200 and isinstance(d, list):
        report["mt5_accounts"] = []
        for a in d:
            acct_info = {
                "name": a.get("name"),
                "account_id": a.get("account_id"),
                "connected": a.get("is_connected"),
                "trading_enabled": a.get("is_trading_enabled"),
                "primary": a.get("is_market_primary"),
            }
            report["mt5_accounts"].append(acct_info)
            if not a.get("is_connected"):
                report["issues"].append(f"{a.get('name')} MT5断连")
                report["overall_status"] = "WARNING"
    else:
        report["issues"].append(f"无法获取账号状态: HTTP {s}")
        report["overall_status"] = "WARNING"

    # 3. 交易异常检查
    trade_check = check_db_trades()
    report["trades"] = trade_check
    if trade_check["consecutive_losses"] >= 5:
        report["issues"].append(f"连续亏损 {trade_check['consecutive_losses']} 笔，建议检查策略")
        report["overall_status"] = "WARNING"
    if trade_check["large_loss"] and abs(trade_check["large_loss"]) > 200:
        report["issues"].append(f"大额亏损: ${trade_check['large_loss']}")
        report["overall_status"] = "WARNING"
    if trade_check["stuck_positions"]:
        report["issues"].append(f"卡住的持仓: {len(trade_check['stuck_positions'])} 笔超48小时未平")
        report["overall_status"] = "WARNING"

    # 4. 日志异常检查
    log_check = check_logs_anomalies()
    report["logs"] = log_check
    if log_check["ai_status"].get("hunyuan_errors", 0) > 0:
        report["issues"].append(f"混元API错误: {log_check['ai_status']['hunyuan_errors']} 次")
    if log_check["ai_status"].get("degradation_events", 0) > 0:
        report["issues"].append(f"AI降级事件: {log_check['ai_status']['degradation_events']} 次（混元不可用，DeepSeek单模型）")
    if log_check["ai_status"].get("minutes_since_last_decision", 0) > 60:
        report["issues"].append(f"AI决策停滞: {log_check['ai_status']['minutes_since_last_decision']} 分钟")
        report["overall_status"] = "WARNING"
    if log_check["total_errors"] > 20:
        report["issues"].append(f"日志错误过多: {log_check['total_errors']} 条")
        report["overall_status"] = "WARNING"

    # 5. API Key 状态
    key_check = check_api_keys()
    report["api_keys"] = key_check

    # 6. 端口检查
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(("127.0.0.1", 8080))
        sock.close()
        if result != 0:
            report["port_8080"] = "CRITICAL: 端口未监听"
            report["overall_status"] = "CRITICAL"
            report["issues"].append("端口 8080 未监听，后端可能已崩溃")
        else:
            report["port_8080"] = "OK"
    except:
        report["port_8080"] = "UNKNOWN"

    # 汇总
    if not report["issues"]:
        report["issues"].append("无异常，系统运行正常")

    return report

if __name__ == "__main__":
    report = run_monitor()

    # 人类可读摘要
    print("=" * 60)
    print(f"  万象Ai 实盘监控巡检报告")
    print(f"  时间: {report['timestamp']}")
    print(f"  总体状态: {report['overall_status']}")
    print("=" * 60)

    print(f"\n[后端] {report['backend_health']}")
    print(f"[端口] {report['port_8080']}")

    print(f"\n[MT5 账号]")
    for a in report.get("mt5_accounts", []):
        status = "✅" if a["connected"] else "❌"
        trading = "交易开" if a.get("trading_enabled") else "交易停"
        print(f"  {status} {a['name']} | {trading}")

    t = report.get("trades", {})
    print(f"\n[交易]")
    print(f"  总交易: {t.get('total_trades', 0)} | 24h: {t.get('recent_24h', 0)} | 持仓: {t.get('open_positions', 0)}")
    print(f"  连续亏损: {t.get('consecutive_losses', 0)} | 大额亏损: {t.get('large_loss', '无')}")
    print(f"  卡住持仓: {len(t.get('stuck_positions', []))} 笔")
    print(f"  AI决策(24h): {t.get('ai_decisions_24h', 'N/A')} | 进化(24h): {t.get('evolution_count_24h', 'N/A')}")

    l = report.get("logs", {})
    ai = l.get("ai_status", {})
    print(f"\n[AI 决策]")
    print(f"  DeepSeek决策: {ai.get('deepseek_decisions', 0)} 次")
    print(f"  混元错误: {ai.get('hunyuan_errors', 0)} 次")
    print(f"  降级事件: {ai.get('degradation_events', 0)} 次")
    print(f"  距上次决策: {ai.get('minutes_since_last_decision', 'N/A')} 分钟")
    if ai.get("last_debate"):
        print(f"  最后决策: {ai['last_debate']}")

    print(f"\n[日志异常]")
    print(f"  错误: {l.get('total_errors', 0)} 条 | 警告: {l.get('total_warnings', 0)} 条")
    for e in l.get("errors", [])[-3:]:
        print(f"  ❌ {e}")
    for w in l.get("warnings", [])[-3:]:
        print(f"  ⚠️ {w}")

    print(f"\n[API Key]")
    for provider, info in report.get("api_keys", {}).items():
        if isinstance(info, dict) and info:
            print(f"  {provider}: 总{info.get('total',0)} 活跃{info.get('active',0)} 有效{info.get('valid',0)}")

    print(f"\n[问题汇总]")
    for issue in report["issues"]:
        print(f"  → {issue}")

    print("\n" + "=" * 60)

    # JSON 输出（供自动化解析）
    print("\n--- JSON ---")
    print(json.dumps(report, ensure_ascii=False, default=str))

    # 退出码：CRITICAL=2, WARNING=1, OK=0
    if report["overall_status"] == "CRITICAL":
        sys.exit(2)
    elif report["overall_status"] == "WARNING":
        sys.exit(1)
    else:
        sys.exit(0)
