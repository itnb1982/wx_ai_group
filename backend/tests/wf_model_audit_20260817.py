"""
2026-08-17 · 本地时序模型方向能力 Walk-Forward 复测（科学规划依据）
=============================================================
背景：用户指出 8 模型只用 5 个、TimesFM/Time-MoE/Moirai 三个本地模型闲置。
本脚本用最近 30 天真实 XAUUSD M15 行情，对每个模型做「未来 2 小时方向预测」评测：
  - 输入：前 128 根 M15 收盘价（32 小时上下文）
  - 预测：未来 8 根（2 小时）走势 → 方向 BUY/SELL（预测均值终点 vs 当前价）
  - 实测：8 根后的真实涨跌
  - 指标：方向准确率、净点数（按方向入场持有 8 根平仓）、相对随机基线（50%）提升

评测模型（同环境对比，CPU float32）：
  - numpy 规则（现役 direction_guard 均线逻辑，基准线）
  - chronos-2（现役，生产在用的时序票）
  - timesfm-2.5（闲置）
  - time-moe 200m（闲置）
  - moirai-moe（闲置，py312 venv 单独跑——见 wf_moirai_20260817.py）

只读操作：仅 copy_rates 拉历史行情 + 本地推理，不碰交易、不碰生产库。
"""
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
MODEL_ROOT = os.path.join(os.path.dirname(ROOT), "models")   # F:\WanxiangAI\models

DATA_NPZ = os.path.join(ROOT, "data", "wf_audit_20260817.npz")
OUT_JSON = os.path.join(ROOT, "data", "wf_audit_20260817_result.json")

CONTEXT = 128     # 输入根数（32h）
HORIZON = 8       # 预测根数（2h）
STEP = 16         # 测试点间隔（每 4h 一个点，防重叠）
BARS = 2880       # 拉取 30 天 M15


def load_data():
    """优先读缓存，否则从 MT5 拉取（只读）。"""
    if os.path.exists(DATA_NPZ):
        d = np.load(DATA_NPZ, allow_pickle=True)
        return d["closes"].tolist(), d.get("times") if "times" in d.files else None
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise RuntimeError(f"MT5 初始化失败: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M15, 0, BARS)
    if rates is None or len(rates) == 0:
        mt5.shutdown()
        raise RuntimeError(f"copy_rates 失败: {mt5.last_error()}")
    closes = [float(r[4]) for r in rates]  # close
    times = [int(r[0]) for r in rates]
    mt5.shutdown()
    np.savez(DATA_NPZ, closes=np.asarray(closes), times=np.asarray(times))
    return closes, times


def build_test_points(closes):
    """构造 (context, label) 测试集：label=+1 未来8根涨 / -1 跌（按 8 根末价）。"""
    pts = []
    for i in range(CONTEXT, len(closes) - HORIZON, STEP):
        ctx = closes[i - CONTEXT:i]
        cur = closes[i - 1]
        fut = closes[i + HORIZON - 1]
        label = 1 if fut > cur else (-1 if fut < cur else 0)
        if label == 0:
            continue
        pts.append((ctx, label, cur, fut))
    return pts


def pips_gain(cur, fut, pred_dir):
    """按预测方向入场、8 根后平仓的净点数（XAUUSD 1.0 = 100 点）。"""
    if pred_dir == 0:
        return 0.0
    return (fut - cur) * 100 * pred_dir


def eval_numpy(closes, pts):
    """现役 NumpyDirectionGuard 的均线方向规则（近似）。"""
    hits = 0
    total_pips = 0.0
    for ctx, label, cur, fut in pts:
        arr = np.asarray(ctx)
        ma_fast = arr[-8:].mean()
        ma_slow = arr.mean()
        pred = 1 if ma_fast > ma_slow else (-1 if ma_fast < ma_slow else 0)
        if pred == 0:
            continue
        if np.sign(pred) == np.sign(label):
            hits += 1
        total_pips += pips_gain(cur, fut, pred)
    return {"hits": hits, "total": len(pts), "acc": hits / max(len(pts), 1),
            "pips": round(total_pips, 1)}


def eval_chronos2(closes, pts):
    # 复用生产共享加载（chronos_shared 处理版本兼容 + 子进程探针隔离）
    from app.services.chronos_shared import predict_quantiles_safe
    import numpy as np
    hits = 0
    total_pips = 0.0
    for ctx, label, cur, fut in pts:
        try:
            quantiles, _mean = predict_quantiles_safe(
                inputs=[{"target": np.asarray(ctx, dtype=np.float32)}],
                prediction_length=HORIZON,
                quantile_levels=[0.10, 0.50, 0.90],
            )
            qt = quantiles[0]  # (n_variates, horizon, 3)
            if qt.shape[0] == 1:
                vt = qt[0]
            else:
                last_vals = qt[:, -1, 1].cpu().numpy().astype(float)
                vt = qt[int(np.argmax(np.abs(last_vals)))]
            mean_fc = float(np.asarray(vt[:, 1].cpu().numpy())[-1])  # P50 末值
        except Exception as e:  # noqa: BLE001
            print(f"[wf] chronos 预测异常: {e}")
            continue
        pred = 1 if mean_fc > cur else (-1 if mean_fc < cur else 0)
        if pred == 0:
            continue
        if np.sign(pred) == np.sign(label):
            hits += 1
        total_pips += pips_gain(cur, fut, pred)
    return {"hits": hits, "total": len(pts), "acc": hits / max(len(pts), 1),
            "pips": round(total_pips, 1)}


def eval_timesfm(closes, pts):
    import torch
    from transformers import TimesFm2_5ModelForPrediction
    model_dir = os.path.join(MODEL_ROOT, "timesfm-2.5-200m")
    model = TimesFm2_5ModelForPrediction.from_pretrained(
        model_dir, torch_dtype=torch.float32, device_map="cpu")
    model.eval()
    hits = 0
    total_pips = 0.0
    for ctx, label, cur, fut in pts:
        arr = torch.tensor(np.asarray(ctx, dtype=np.float32)).unsqueeze(0)
        with torch.no_grad():
            out = model(past_values=arr, forecast_context_len=CONTEXT)
        mean_fc = float(out.mean_predictions[0].numpy()[:HORIZON].mean())
        pred = 1 if mean_fc > cur else (-1 if mean_fc < cur else 0)
        if pred == 0:
            continue
        if np.sign(pred) == np.sign(label):
            hits += 1
        total_pips += pips_gain(cur, fut, pred)
    return {"hits": hits, "total": len(pts), "acc": hits / max(len(pts), 1),
            "pips": round(total_pips, 1)}


def eval_timemoe(closes, pts):
    """Time-MoE 200m：transformers 5.14 接口不兼容（generate DynamicCache.seen_tokens 移除 +
    forward 需 token 化输入），本环境无法完成评测 → 返回状态标记。"""
    return {"hits": 0, "total": 0, "acc": 0.0, "pips": 0.0,
            "status": "SKIP: transformers5.14 接口不兼容（DynamicCache/inputs_embeds），需适配后复测"}


def main():
    t0 = time.time()
    print("[wf] 加载数据...")
    closes, _ = load_data()
    print(f"[wf] 数据 {len(closes)} 根 M15")
    pts = build_test_points(closes)
    print(f"[wf] 测试点 {len(pts)} 个（每点 context={CONTEXT} horizon={HORIZON}）")

    results = {}
    results["numpy规则(现役)"] = eval_numpy(closes, pts)
    print(f"[wf] numpy: {results['numpy规则(现役)']}")
    results["chronos-2(现役)"] = eval_chronos2(closes, pts)
    print(f"[wf] chronos-2: {results['chronos-2(现役)']}")
    results["timesfm-2.5(闲置)"] = eval_timesfm(closes, pts)
    print(f"[wf] timesfm-2.5: {results['timesfm-2.5(闲置)']}")
    results["time-moe(闲置)"] = eval_timemoe(closes, pts)
    print(f"[wf] time-moe: {results['time-moe(闲置)']}")

    results["_meta"] = {
        "context": CONTEXT, "horizon": HORIZON, "step": STEP,
        "bars": len(closes), "points": len(pts),
        "elapsed_sec": round(time.time() - t0, 1),
        "data_range": f"{closes[0]:.2f} ~ {closes[-1]:.2f}",
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[wf] 结果已写 {OUT_JSON}（{time.time()-t0:.0f}s）")
    for k, v in results.items():
        if not k.startswith("_"):
            print(f"  {k}: acc={v['acc']:.3f} pips={v['pips']:+.1f}")


if __name__ == "__main__":
    main()
