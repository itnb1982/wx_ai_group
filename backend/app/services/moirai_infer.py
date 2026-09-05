# -*- coding: utf-8 -*-
"""Moirai 隔离 venv 推理脚本（由 MoiraiP 通过子进程调用）。

运行环境：{ROOT}/models/moirai_venv（独立 numpy/torch/uni2ts，与生产 .venv 完全隔离）。
用法：python moirai_infer.py <input.json> <output.json>
  input.json  = {"ctx": [float,...], "horizon": int}
  output.json = {"pred_end": float, "lo": float|null, "hi": float|null}

为何独立进程：Moirai 依赖 uni2ts，强装进生产环境会触发其依赖对 numpy 的
重编译，可能搞坏正在交易的 .venv。隔离 venv + 子进程调用，两边都干净。
"""
from __future__ import annotations

import sys
import os
import json
import numpy as np
import torch


def main():
    if len(sys.argv) < 3:
        print("usage: moirai_infer.py <input.json> <output.json>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        req = json.load(f)
    ctx = np.asarray(req["ctx"], dtype=np.float32)
    horizon = int(req.get("horizon", 4))

    # 上下文长度：取 512 与可用长度的小者（Moirai 对上下文长度有要求）
    context_length = min(512, max(16, len(ctx)))

    from uni2ts.model.moirai import MoiraiForecast, MoiraiModule

    # 优先使用本地权重，避免本机无法访问 HuggingFace Hub。
    # 本地模型目录命名可能是 moirai-moe-small 或 moirai-1.1-R-small。
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(script_dir, "..", "..", ".."))
    local_candidates = [
        os.path.join(root, "models", "moirai-1.1-R-small"),
        os.path.join(root, "models", "moirai-moe-small"),
    ]
    model_path = "Salesforce/moirai-1.1-R-small"
    for cand in local_candidates:
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "config.json")):
            model_path = cand
            break

    model = MoiraiForecast(
        module=MoiraiModule.from_pretrained(model_path),
        prediction_length=horizon,
        context_length=context_length,
        patch_size=16,
        num_samples=100,
        target_dim=1,
        feat_dynamic_real_dim=0,
        past_feat_dynamic_real_dim=0,
    )
    predictor = model.create_predictor(batch_size=1)

    # uni2ts 吃 pandas DataFrame（宽表）
    import pandas as pd
    df = pd.DataFrame({"target": ctx})
    df.index = pd.date_range("2020-01-01", periods=len(df), freq="h")
    ds = df.iloc[-context_length:]

    from gluonts.dataset.pandas import PandasDataset
    gds = PandasDataset(dict(ds))
    fcsts = list(predictor.predict(gds))
    samples = fcsts[0].samples  # (num_samples, prediction_length)
    samples = np.asarray(samples, dtype=np.float32)
    mean = float(samples.mean(axis=0)[-1])
    lo = float(np.percentile(samples, 10, axis=0)[-1])
    hi = float(np.percentile(samples, 90, axis=0)[-1])

    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump({"pred_end": mean, "lo": lo, "hi": hi}, f)


if __name__ == "__main__":
    main()
