"""
2026-08-17 · Moirai-MoE 方向能力 Walk-Forward 复测（py312 venv 隔离版）
=================================================================
与 wf_model_audit_20260817.py 同一套评测协议，读取同一份数据 npz。
运行环境：models/moirai_venv_py312（uni2ts + numpy 1.26，与生产隔离）
按生产 moirai_infer.py 的调用方式：MoiraiForecast + create_predictor + gluonts PandasDataset
"""
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_ROOT = os.path.join(os.path.dirname(ROOT), "models")
DATA_NPZ = os.path.join(ROOT, "data", "wf_audit_20260817.npz")
OUT_JSON = os.path.join(ROOT, "data", "wf_audit_moirai_20260817_result.json")

CONTEXT = 128
HORIZON = 8
STEP = 16


def build_test_points(closes):
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
    if pred_dir == 0:
        return 0.0
    return (fut - cur) * 100 * pred_dir


def main():
    t0 = time.time()
    if not os.path.exists(DATA_NPZ):
        print("[moirai] 数据文件不存在，先跑 wf_model_audit_20260817.py")
        return
    d = np.load(DATA_NPZ, allow_pickle=True)
    closes = d["closes"].tolist()
    print(f"[moirai] 数据 {len(closes)} 根")
    pts = build_test_points(closes)
    print(f"[moirai] 测试点 {len(pts)}")

    from uni2ts.model.moirai import MoiraiForecast, MoiraiModule

    cands = [
        os.path.join(MODEL_ROOT, "moirai-moe-small"),
        os.path.join(MODEL_ROOT, "moirai-1.1-R-small"),
    ]
    model_path = None
    for c in cands:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "config.json")):
            model_path = c
            break
    if model_path is None:
        model_path = "Salesforce/moirai-1.1-R-small"
    print(f"[moirai] 模型: {model_path}")

    model = MoiraiForecast(
        module=MoiraiModule.from_pretrained(model_path),
        prediction_length=HORIZON,
        context_length=CONTEXT,
        patch_size=16,
        num_samples=20,
        target_dim=1,
        feat_dynamic_real_dim=0,
        past_feat_dynamic_real_dim=0,
    )
    predictor = model.create_predictor(batch_size=8)

    import pandas as pd
    from gluonts.dataset.pandas import PandasDataset

    hits = 0
    total_pips = 0.0
    for ctx, label, cur, fut in pts:
        try:
            df = pd.DataFrame({"target": np.asarray(ctx, dtype=np.float32)})
            df.index = pd.date_range("2020-01-01", periods=len(df), freq="h")
            gds = PandasDataset(dict(df.iloc[-CONTEXT:]))
            fcsts = list(predictor.predict(gds))
            samples = np.asarray(fcsts[0].samples, dtype=np.float32)  # (num_samples, HORIZON)
            mean_fc = float(samples.mean(axis=0)[-1])
        except Exception as e:  # noqa: BLE001
            print(f"[moirai] 预测异常: {e}")
            continue
        pred = 1 if mean_fc > cur else (-1 if mean_fc < cur else 0)
        if pred == 0:
            continue
        if np.sign(pred) == np.sign(label):
            hits += 1
        total_pips += pips_gain(cur, fut, pred)

    res = {
        "moirai-moe(闲置)": {
            "hits": hits, "total": len(pts),
            "acc": hits / max(len(pts), 1), "pips": round(total_pips, 1),
        },
        "_meta": {
            "model_path": model_path,
            "context": CONTEXT, "horizon": HORIZON, "step": STEP,
            "elapsed_sec": round(time.time() - t0, 1),
        },
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"[moirai] 完成: {res['moirai-moe(闲置)']}（{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
