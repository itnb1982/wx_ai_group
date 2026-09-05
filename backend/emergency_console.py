#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
万象AI — 紧急处置控制台（Phase 0 / V6）

命令行版的"红色大按钮"。存在的理由：后端 Web 服务挂了、数据库锁死了、
浏览器打不开了——这些时候人依然必须能把机器停下来。

╔══════════════════════════════════════════════════════════════════════╗
║ 能力分层（诚实说明，不吹牛）：                                        ║
║                                                                      ║
║   【必定可用】封盘 / 解除 / 查看状态                                  ║
║     纯本地文件操作，不需要后端、不需要数据库、不需要网络。            ║
║     即使整个后端进程已经死了，封盘依然生效——因为状态写在磁盘上，      ║
║     后端下次启动时第一件事就是读它。                                  ║
║                                                                      ║
║   【尽力而为】一键全平                                                ║
║     平仓必须通过 MT5 连接，而 MT5 连接活在后端进程里。                ║
║     后端还活着 → 调 API 平仓；后端已死 → 本工具无能为力，            ║
║     会明确告诉你"请立即打开 MT5 终端手工平仓"，不假装成功。           ║
╚══════════════════════════════════════════════════════════════════════╝

用法：
  python emergency_console.py status
  python emergency_console.py halt   --reason "盘面异常"
  python emergency_console.py halt   --level HALT_ALL --scope <account_id> --reason "该号连亏"
  python emergency_console.py resume --scope global
  python emergency_console.py flatten --yes --reason "爆仓风险"   （需后端存活 + token）

token 来源（flatten 才需要）：环境变量 WX_TOKEN，或 --token 参数。
"""
import os
import sys
import json
import argparse
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

STATE_FILE = os.path.join(_HERE, "emergency_state.json")
DEFAULT_API = os.environ.get("WX_API", "http://127.0.0.1:8080")

# ── 优先用正式模块；导入失败则降级为裸文件操作 ──
# 为什么要这个降级：本工具是最后一道防线，不能因为某个依赖装坏了就一起罢工。
_native = None
try:
    from app.services import emergency as _native   # noqa
except Exception as _e:                              # pragma: no cover - 降级路径
    print(f"[提示] 未能加载 app.services.emergency（{_e}），降级为直接读写状态文件。")


LEVELS = ("HALT_NEW", "HALT_ALL")


# ─────────────────── 降级实现（不依赖任何项目模块） ───────────────────

def _fallback_read() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1,
                "global": {"level": "NORMAL", "reason": "", "at": "", "by": ""},
                "accounts": {}, "flatten_requests": []}


def _fallback_write(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _fallback_halt(level, scope, reason, by):
    st = _fallback_read()
    entry = {"level": level, "reason": reason,
             "at": datetime.now().isoformat(timespec="seconds"), "by": by}
    if scope == "global":
        st["global"] = entry
    else:
        st.setdefault("accounts", {})[scope] = entry
    _fallback_write(st)
    return st


def _fallback_resume(scope, by):
    st = _fallback_read()
    if scope == "global":
        st["global"] = {"level": "NORMAL", "reason": "",
                        "at": datetime.now().isoformat(timespec="seconds"), "by": by}
    else:
        st.setdefault("accounts", {}).pop(scope, None)
    _fallback_write(st)
    return st


# ─────────────────────────── 命令实现 ───────────────────────────

def cmd_status(args):
    if _native:
        s = _native.summary()
        print(f"状态文件 : {s['state_file']}")
        print(f"全局档位 : {s['global_level']}")
        if s["global_level"] != "NORMAL":
            print(f"           原因={s['global_reason'] or '未填写'} "
                  f"| 操作人={s['global_by']} | 时间={s['global_at']}")
        if s["halted_accounts"]:
            print("账号级停止:")
            for aid, lv in s["halted_accounts"].items():
                print(f"  · {aid}  → {lv}")
        else:
            print("账号级停止: 无")
        print(f"是否有任何停止: {'★ 是' if s['any_halt'] else '否'}")
        if s["recent_flatten"]:
            print("最近全平记录:")
            for r in s["recent_flatten"]:
                print(f"  · {r['at']} {r['scope']} by {r['by']} → {r.get('result')}")
    else:
        st = _fallback_read()
        print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


# 紧急时刻人是手抖的，不能要求准确敲出 HALT_NEW 全称大写。
# 别名表覆盖常见口语/简写/数字，一律归一到正式档位。
_LEVEL_ALIASES = {
    "NEW": "HALT_NEW", "HALT_NEW": "HALT_NEW", "1": "HALT_NEW",
    "HALTNEW": "HALT_NEW", "STOP_NEW": "HALT_NEW", "N": "HALT_NEW",
    "ALL": "HALT_ALL", "HALT_ALL": "HALT_ALL", "2": "HALT_ALL",
    "HALTALL": "HALT_ALL", "STOP_ALL": "HALT_ALL", "A": "HALT_ALL",
}


def _normalize_level(raw: str) -> str:
    """把用户输入归一成正式档位；无法识别返回空串。"""
    key = (raw or "HALT_NEW").strip().upper().replace("-", "_").replace(" ", "")
    return _LEVEL_ALIASES.get(key, "")


def cmd_halt(args):
    level = _normalize_level(args.level)
    if not level:
        print(f"[错误] 无法识别的 level: {args.level!r}")
        print(f"       可用: {' / '.join(LEVELS)}（也接受简写 new / all）")
        print("[注意] 本次未执行任何停止操作，系统仍在正常交易！")
        return 2

    by = args.by or f"console@{os.environ.get('USERNAME', 'local')}"
    if _native:
        _native.halt(level, scope=args.scope, reason=args.reason, by=by)
    else:
        _fallback_halt(level, args.scope, args.reason, by)

    print(f"★ 已封盘：{level} | 范围={args.scope} | 原因={args.reason or '未填写'}")
    if level == "HALT_NEW":
        print("  说明：只封新仓；已有持仓的止损/止盈继续保护（不会让持仓裸奔）。")
    else:
        print("  说明：连自动平仓也已停止，现有持仓完全交由人工处置。")
    print("  生效时机：后端主循环下一次检查时（≤1 秒内读到），无需重启。")
    print("  注意：这不会平掉已有持仓。要清仓请用 flatten 或直接在 MT5 终端操作。")
    return 0


def cmd_resume(args):
    by = args.by or f"console@{os.environ.get('USERNAME', 'local')}"
    if _native:
        _native.resume(scope=args.scope, by=by)
    else:
        _fallback_resume(args.scope, by)
    print(f"已解除停止 | 范围={args.scope}")
    if args.scope == "global":
        print("  提醒：账号级停止不会被连带解除，需要单独 resume --scope <account_id>。")
    return 0


def cmd_flatten(args):
    if not args.yes:
        print("[拒绝] 一键全平会立刻平掉真实持仓并锁定亏损/利润。")
        print("       确认无误请重新执行并加上 --yes")
        return 2

    token = args.token or os.environ.get("WX_TOKEN", "")
    if not token:
        print("[缺少凭据] 全平需要后端 API 鉴权，未提供 token。")
        print("  · 环境变量 WX_TOKEN=<你的登录token>  或  --token <token>")
        print("  · 若此刻拿不到 token：先执行 halt 封住入口（不需要 token），")
        print("    再打开 MT5 终端手工平仓——这样至少不会有新仓继续开出来。")
        return 2

    try:
        import requests
    except Exception:
        print("[环境缺失] 未安装 requests，无法调用后端 API。")
        print("  请直接打开 MT5 终端手工平仓，并执行 halt 封住入口。")
        return 3

    url = f"{args.api.rstrip('/')}/api/emergency/flatten"
    try:
        resp = requests.post(
            url,
            json={"scope": args.scope, "reason": args.reason, "halt_after": True},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    except Exception as e:
        # ★ 关键：后端不可达时绝不假装成功
        print(f"[后端不可达] {e}")
        print("  本工具无法代替 MT5 平仓（MT5 连接活在后端进程里）。")
        print("  → 请立即打开 MT5 终端手工平掉所有持仓。")
        print("  → 同时执行： python emergency_console.py halt --reason '后端失联人工封盘'")
        print("    这样后端一旦恢复也不会立刻开新仓。")
        return 4

    if resp.status_code != 200:
        print(f"[失败] HTTP {resp.status_code}: {resp.text[:300]}")
        print("  → 请打开 MT5 终端人工核实持仓。")
        return 5

    data = resp.json()
    print(f"请求ID    : {data.get('request_id')}")
    print(f"已平笔数  : {data.get('total_closed')}")
    print(f"残留笔数  : {data.get('total_remaining')}")
    print(f"是否清空  : {'是' if data.get('fully_flat') else '★ 否'}")
    print(f"是否已封盘: {'是' if data.get('halted') else '否'}")
    for r in data.get("results", []):
        print(f"  · {r['account_id'][:8]} 平{r['closed_count']}笔 "
              f"残留{r['remaining_count']}笔 失败{len(r.get('failed') or [])}笔")
    if not data.get("fully_flat"):
        print("\n★★ 未完全清空！请立即打开 MT5 终端人工核实并平掉残留持仓。")
        return 6
    return 0


def main():
    p = argparse.ArgumentParser(
        description="万象AI 紧急处置控制台（封盘不依赖后端；全平需后端存活）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看当前处置状态")

    ph = sub.add_parser("halt", help="封盘（不依赖后端，必定可用）")
    ph.add_argument("--level", default="HALT_NEW",
                    help="HALT_NEW/new=只封新仓(默认) / HALT_ALL/all=连自动平仓也停")
    ph.add_argument("--scope", default="global", help="global 或具体 account_id")
    ph.add_argument("--reason", default="", help="停止原因（事后复盘用，强烈建议填）")
    ph.add_argument("--by", default="", help="操作人标识")

    pr = sub.add_parser("resume", help="解除封盘")
    pr.add_argument("--scope", default="global")
    pr.add_argument("--by", default="")

    pf = sub.add_parser("flatten", help="一键全平（需后端存活 + token）")
    pf.add_argument("--scope", default="global")
    pf.add_argument("--reason", default="")
    pf.add_argument("--yes", action="store_true", help="危险操作确认")
    pf.add_argument("--token", default="")
    pf.add_argument("--api", default=DEFAULT_API)

    args = p.parse_args()
    return {
        "status": cmd_status,
        "halt": cmd_halt,
        "resume": cmd_resume,
        "flatten": cmd_flatten,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
