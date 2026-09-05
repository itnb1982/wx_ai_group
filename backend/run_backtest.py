"""
离线回测 CLI（M1.5）— 纯 Python，MT5 仅作行情数据源，不编译 EA。

用法:
  # 用 MT5 实时拉最近 14 天 M15 行情，对比 规则基线 vs AI 出场
  python run_backtest.py --mode both --days 14 --tf M15

  # 脱机回放（完全不连 MT5），仅验证规则基线框架
  python run_backtest.py --mode rule --csv rates.csv

  # 只看被动等 SL/TP 的基线（最差情况）
  python run_backtest.py --mode hold --csv rates.csv

说明:
  - both = 同开仓序列下跑 规则引擎 + AI出场Agent，输出 PF/笔数/净盈亏对比
  - AI 模式需配置 DeepSeek API Key（backend/.env），无 key 自动降级为仅规则基线
  - MT5 仅在 --csv 不传时用于 copy_rates_range 只读拉行情
"""
import sys
import os
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.backtest_engine import run_backtest, load_rates_csv, fetch_rates_mt5

_LABELS = {"rule": "规则引擎(基线)", "ai": "AI出场Agent", "hold": "被动SL/TP"}


def _print(res: dict):
    print(f"\n── {res['label']} ──")
    for k, v in res.items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser(description="万象Ai 离线回测器（不编译EA）")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M15")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--mode", default="both", choices=["rule", "ai", "hold", "both"])
    ap.add_argument("--decision-every", type=int, default=4,
                    help="AI 模式决策间隔(根K线)，越大越省LLM调用")
    ap.add_argument("--csv", default="", help="本地行情CSV路径(脱机回放)")
    ap.add_argument("--max-bars", type=int, default=0, help="限制回测根数(小样本调试)")
    args = ap.parse_args()

    # ── 加载行情 ──
    if args.csv:
        bars = load_rates_csv(args.csv)
        print(f"[行情] 从 CSV 加载 {len(bars)} 根 {args.symbol} {args.tf}")
    else:
        print(f"[行情] 从 MT5 只读拉取最近 {args.days} 天 {args.symbol} {args.tf} ...")
        bars = fetch_rates_mt5(args.symbol, args.tf, args.days)
        print(f"[行情] 拉取 {len(bars)} 根")
    if args.max_bars > 0:
        bars = bars[-args.max_bars:]
        print(f"[行情] 限制为最近 {len(bars)} 根")

    # ── 选定模式 ──
    modes = []
    if args.mode in ("rule", "both"):
        modes.append("rule")
    if args.mode in ("ai", "both"):
        modes.append("ai")
    if args.mode == "hold":
        modes.append("hold")

    # ── 构造 AI Agent（若需要）──
    ai_agent = None
    if "ai" in modes:
        try:
            from app.core.deepseek_client import DeepSeekClient
            from app.services.ai_exit import AIExitAgent
            ds = DeepSeekClient()
            ai_agent = AIExitAgent(ds, account_id="BACKTEST")
            print("[AI] 出场 Agent 已加载（真实 LLM 决策）")
        except Exception as e:
            print(f"[WARN] AI 模式不可用（LLM 未配置: {e}）；仅跑规则基线")
            modes = [m for m in modes if m != "ai"]

    # ── 运行 ──
    results = {}
    for m in modes:
        res = run_backtest(
            bars, m, ai_agent=ai_agent,
            decision_every=args.decision_every, label=_LABELS[m])
        results[m] = res
        _print(res)

    # ── 对比 ──
    if "rule" in results and "ai" in results:
        r, a = results["rule"], results["ai"]
        print("\n========== 对比：规则基线(R) vs AI出场(A) ==========")
        print(f"  PF      : {r['pf']}   →   {a['pf']}   "
              f"{'✅ AI更优' if _gt(a['pf'], r['pf']) else '⚠️ 未超越'}")
        print(f"  笔数    : {r['trades_closed']}   →   {a['trades_closed']}   "
              f"{'✅ 未腰斩' if a['trades_closed'] >= r['trades_closed'] * 0.5 else '❌ 腰斩'}")
        print(f"  胜率    : {r['win_rate']}   →   {a['win_rate']}")
        print(f"  净盈亏  : {r['net_pnl']}   →   {a['net_pnl']}")
        print(f"  捕捉率  : {r['avg_capture_rate']}   →   {a['avg_capture_rate']}")
        print("=======================================================")

    with open("backtest_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n结果已写入 backtest_result.json")


def _gt(a, b) -> bool:
    try:
        return float(a) > float(b)
    except Exception:
        return False


if __name__ == "__main__":
    main()
