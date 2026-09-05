# -*- coding: utf-8 -*-
"""Chronos-2 分位数「校准度」实测 —— 回答一个被长期混淆的问题。

背景（为什么必须单独测这个）：
    竞技场回测显示 Chronos-2 的**方向准确率**只有 48.75%（不如抛硬币），
    但人肉盯盘的直观感受却是「Chronos 90% 的时候是对的」。
    这两个结论看似矛盾，实则很可能在测两件完全不同的事：

      · 方向准确率 = P50 末值相对现价的涨跌符号，是否与未来真实涨跌同号。
        这是**点预测**，对随机游走资产而言天然接近 50%。

      · 区间校准度 = 未来真实价格是否落在 [P10, P90] 预测带内。
        这是**区间预测**，理想值 80%。肉眼看图时"预测带把行情包住了"
        给人的感觉就是"它预测得很准"——但这是覆盖率，不是方向。

    如果 Chronos 的区间校准良好而方向近随机，那么它的正确岗位是
    **风险区间估计器**（止盈天花板 / 不确定性度量 / 仓位缩放），
    而**不是方向终审**。把它放错岗位，既埋没长处又放大短处。

判据：
    区间覆盖率显著低于 80% → 过度自信（带子太窄），天花板会被频繁击穿；
    显著高于 80%           → 过度保守（带子太宽），天花板形同虚设；
    接近 80%               → 校准良好，可放心用于止盈天花板与仓位缩放。

用法（原生 PowerShell / cmd，切勿用 Git Bash）：
    .venv\\Scripts\\python.exe backend\\scripts\\chronos_calibration.py --tf M15 --bars 6000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(BACKEND)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
MODELS = os.path.join(ROOT, "models")

import numpy as np  # noqa: E402


class _Tee:
    def __init__(self, stream, path):
        self.stream = stream
        self.f = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:  # noqa: BLE001
            pass
        self.f.write(s)
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass
        self.f.flush()


def load_rates(symbol: str, tf: str, bars: int):
    import MetaTrader5 as mt5

    TF = {"M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
          "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
          "H4": mt5.TIMEFRAME_H4}
    if not mt5.initialize():
        raise RuntimeError(f"MT5 初始化失败: {mt5.last_error()}")
    try:
        for s in [symbol, symbol + "m", "XAUUSD", "GOLD"]:
            r = mt5.copy_rates_from_pos(s, TF[tf], 0, bars)
            if r is not None and len(r) > 100:
                closes = np.asarray([float(x["close"]) for x in r], dtype=float)
                print(f"[数据] symbol={s} tf={tf} 根数={len(closes)} "
                      f"区间 {closes[0]:.2f} → {closes[-1]:.2f}")
                return closes
        raise RuntimeError("取不到 K 线")
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M15")
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--horizon", type=int, default=8)
    ap.add_argument("--ctx", type=int, default=256)
    ap.add_argument("--step", type=int, default=6)
    ap.add_argument("--out", default=os.path.join(BACKEND, "data", "chronos_calibration.json"))
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    sys.stdout = _Tee(sys.stdout, os.path.splitext(a.out)[0] + ".log")

    closes = load_rates(a.symbol, a.tf, a.bars)

    import torch
    from chronos import Chronos2Pipeline

    d = os.path.join(MODELS, "chronos-2")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    pipe = Chronos2Pipeline.from_pretrained(d, device_map=dev, dtype=torch.bfloat16)
    print(f"[加载] Chronos-2 就绪 device={dev}")

    idxs = list(range(a.ctx, len(closes) - a.horizon, a.step))
    H = a.horizon
    print(f"[校准] 样本={len(idxs)} horizon={H} ctx={a.ctx}")

    # 逐步统计：cover[h] = 第 h 步真值落在 [P10,P90] 内的次数
    cover = np.zeros(H, dtype=int)
    below = np.zeros(H, dtype=int)   # 真值低于 P10（带子偏高）
    above = np.zeros(H, dtype=int)   # 真值高于 P90（带子偏低）
    ae_model = np.zeros(H)           # P50 绝对误差
    ae_naive = np.zeros(H)           # 「价格不变」基线绝对误差
    band_w = np.zeros(H)             # 平均带宽
    dir_ok = 0
    dir_n = 0
    n = 0

    t0 = time.time()
    for k, i in enumerate(idxs):
        ctx = closes[i - a.ctx:i]
        truth = closes[i:i + H]
        last = float(ctx[-1])
        try:
            with torch.no_grad():
                q, _ = pipe.predict_quantiles(
                    inputs=[{"target": np.asarray(ctx, dtype=np.float32)}],
                    prediction_length=H,
                    quantile_levels=[0.1, 0.5, 0.9])
            arr = q[0].float().cpu().numpy()
            if arr.ndim == 3:
                arr = arr[0]
        except Exception as e:  # noqa: BLE001
            if k == 0:
                print(f"[异常] {type(e).__name__}: {str(e)[:160]}")
            continue

        p10, p50, p90 = arr[:, 0], arr[:, 1], arr[:, 2]
        for h in range(H):
            t = truth[h]
            if t < p10[h]:
                below[h] += 1
            elif t > p90[h]:
                above[h] += 1
            else:
                cover[h] += 1
            ae_model[h] += abs(p50[h] - t)
            ae_naive[h] += abs(last - t)
            band_w[h] += (p90[h] - p10[h])
        # 方向（末步）作对照
        if abs(p50[-1] - last) > 1e-9:
            dir_n += 1
            if np.sign(p50[-1] - last) == np.sign(truth[-1] - last):
                dir_ok += 1
        n += 1
        if k and k % 100 == 0:
            el = time.time() - t0
            print(f"  进度 {k}/{len(idxs)} 已用{el:.0f}s 预计剩余{el/k*(len(idxs)-k):.0f}s")

    if n == 0:
        print("[错误] 无有效样本")
        return 1

    print("\n" + "=" * 88)
    print(f"Chronos-2 分位数校准报告  |  {a.symbol} {a.tf}  |  样本 {n}")
    print("=" * 88)
    print(f"{'步':>4}{'区间覆盖率%':>13}{'低于P10%':>11}{'高于P90%':>11}"
          f"{'P50误差':>10}{'不变基线误差':>14}{'带宽':>9}")
    print("-" * 88)
    steps = []
    for h in range(H):
        cov = cover[h] / n * 100
        blw = below[h] / n * 100
        abv = above[h] / n * 100
        m = ae_model[h] / n
        nv = ae_naive[h] / n
        bw = band_w[h] / n
        steps.append({"步": h + 1, "覆盖率": round(cov, 2), "低于P10": round(blw, 2),
                      "高于P90": round(abv, 2), "P50平均误差": round(m, 3),
                      "不变基线误差": round(nv, 3), "平均带宽": round(bw, 3)})
        print(f"{h+1:>4}{cov:>13.2f}{blw:>11.2f}{abv:>11.2f}{m:>10.3f}{nv:>14.3f}{bw:>9.3f}")
    print("=" * 88)

    avg_cov = float(np.mean([s["覆盖率"] for s in steps]))
    skill = 1 - (ae_model.sum() / max(ae_naive.sum(), 1e-9))
    dir_acc = dir_ok / dir_n * 100 if dir_n else 0.0

    print(f"\n【结论】")
    print(f"  平均区间覆盖率 = {avg_cov:.2f}%  (理想 80%)")
    if avg_cov < 70:
        verdict = "过度自信：预测带太窄，止盈天花板会被频繁击穿，需放宽或降权"
    elif avg_cov > 90:
        verdict = "过度保守：预测带太宽，天花板形同虚设，参考价值有限"
    else:
        verdict = "校准良好：可安全用于止盈天花板与不确定性度量"
    print(f"  判定：{verdict}")
    print(f"  P50 相对「价格不变」基线的技能分 = {skill*100:+.2f}%  (>0 才说明点预测有增量价值)")
    print(f"  末步方向准确率 = {dir_acc:.2f}%  (与区间覆盖率是两回事，勿混为一谈)")

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"生成时间": time.strftime("%Y-%m-%d %H:%M:%S"), "参数": vars(a),
                   "样本数": n, "逐步": steps, "平均区间覆盖率": round(avg_cov, 2),
                   "判定": verdict, "P50技能分%": round(skill * 100, 2),
                   "末步方向准确率%": round(dir_acc, 2)},
                  f, ensure_ascii=False, indent=2)
    print(f"[报告] 已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
