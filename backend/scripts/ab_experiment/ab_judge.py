#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A/B 实验·判定脚本（周一实盘 walk-forward 用）

功能：
  - 读取 ab_results.json，对比「基线窗口(关)」与「处理窗口(开)」的真实交易表现。
  - 按铁律判定是否通过（任一不满足即回退辩论环）：
      ① 笔数 ≥ 基线 80%（不腰斩）；
      ② 净利润不下降（容许 -5% 噪声，低于即判下降）；
      ③ 盈利因子 PF > 1（系统必须正期望）。
  - 输出 PASS（保留辩论环）或 ROLLBACK（关闭辩论环 + 回退）结论。

用法：
  python ab_judge.py                          # 默认对比 W1_OFF vs W2_ON
  python ab_judge.py --base W1_OFF --treat W2_ON
  python ab_judge.py --min-trade-ratio 0.8 --max-profit-drop 0.05

依赖：仅 Python 标准库。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(HERE, "ab_results.json")


def load(base, treat):
    if not os.path.exists(RESULTS_PATH):
        print(f"[错误] 未找到 {RESULTS_PATH}，请先跑 ab_monitor.py 采集两个窗口。")
        sys.exit(1)
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        results = json.load(f)
    if base not in results:
        print(f"[错误] 基线窗口 '{base}' 未采集。已有：{list(results.keys())}")
        sys.exit(1)
    if treat not in results:
        print(f"[错误] 处理窗口 '{treat}' 未采集。已有：{list(results.keys())}")
        sys.exit(1)
    return results[base], results[treat]


def verdict(base, treat, min_ratio, max_drop):
    b, t = base["trades"], treat["trades"]
    checks = []

    # ① 笔数不腰斩
    bc, tc = b["count"], t["count"]
    ratio = (tc / bc) if bc else (1.0 if tc else 0.0)
    c1 = ratio >= min_ratio
    checks.append(("笔数不腰斩", f"{tc} vs {bc} (比 {ratio*100:.0f}%)", "PASS" if c1 else "FAIL", c1))

    # ② 净利润不下降
    bn, tn = b["net_profit"], t["net_profit"]
    drop = ((bn - tn) / bn) if bn else 0.0
    c2 = tn >= bn * (1 - max_drop)
    checks.append(("净利润不降", f"${tn:.0f} vs ${bn:.0f} (降幅 {drop*100:.1f}%)", "PASS" if c2 else "FAIL", c2))

    # ③ PF > 1
    pf = t["profit_factor"]
    pf_ok = (pf is not None) and (pf > 1.0)
    checks.append(("盈利因子 PF>1", f"PF={pf}", "PASS" if pf_ok else "FAIL", pf_ok))

    passed = all(c[3] for c in checks)
    return passed, checks, (bc, tc, bn, tn, pf)


def main():
    ap = argparse.ArgumentParser(description="A/B 实验·判定")
    ap.add_argument("--base", default="W1_OFF", help="基线窗口 label")
    ap.add_argument("--treat", default="W2_ON", help="处理窗口 label")
    ap.add_argument("--min-trade-ratio", type=float, default=0.8, help="笔数下限比例（默认 0.8）")
    ap.add_argument("--max-profit-drop", type=float, default=0.05, help="净利润允许最大降幅（默认 0.05=5%）")
    args = ap.parse_args()

    base, treat = load(args.base, args.treat)
    passed, checks, _ = verdict(base, treat, args.min_trade_ratio, args.max_profit_drop)

    print("=" * 60)
    print(f" A/B 判定：基线[{args.base}] vs 处理[{args.treat}]")
    print(f" 基线窗口 {base['start']}~{base['end']} | 处理窗口 {treat['start']}~{treat['end']}")
    print("=" * 60)
    for name, val, st, _ in checks:
        print(f"  [{st}] {name:12s}: {val}")
    print("-" * 60)
    if passed:
        print("  ✅ 结论：PASS —— 辩论环提升/不削弱表现，保留开启。")
        print("         后续可继续观察或纳入常态化；如需进一步验证可延长窗口。")
    else:
        print("  ❌ 结论：ROLLBACK —— 辩论环未通过铁律判定，立即关闭。")
        print("         执行：python ab_control.py --off  → 双击 restart_task_backend.bat 重载。")
        print("         回退后辩论环不再介入决策链（零代码改动即恢复基线行为）。")
    print("=" * 60)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
