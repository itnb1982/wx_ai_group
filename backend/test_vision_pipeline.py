# -*- coding: utf-8 -*-
"""视觉模型管线端到端测试（渲染 + 聚合 + 真实模型调用）。
用法：
  venv\\Scripts\\python.exe test_vision_pipeline.py          # 仅渲染+聚合
  venv\\Scripts\\python.exe test_vision_pipeline.py --vision  # 额外调用真实视觉模型（需已 pull）
"""
import os
import sys
import csv
import json
import random

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.services.vision_chart import render_chart
from app.services.vision_service import VisionService, _extract_json, _clean_decision, _clean_conf


def load_m15_csv(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("tick_volume", 0) or 0),
                    "time": r.get("time_utc") or r.get("time", ""),
                })
            except (KeyError, ValueError):
                continue
    return rows


def synth_h4(n=120, drift=0.6):
    bars = []
    price = 4300.0
    for i in range(n):
        # 带正漂移 + 噪声 + 偶发回调，制造可读的 HH/HL 结构
        step = drift + random.uniform(-2.2, 2.2)
        if i % 17 == 0:
            step -= random.uniform(6, 12)  # 回调
        o = price
        c = max(1.0, price + step)
        h = max(o, c) + random.uniform(0, 3)
        l = min(o, c) - random.uniform(0, 3)
        bars.append({"open": o, "high": h, "low": l, "close": c,
                     "volume": random.uniform(50, 200), "time": f"bar{i}"})
        price = c
    return bars


def main():
    random.seed(7)
    m15 = load_m15_csv(os.path.join(HERE, "audit_20260814_full_prices.csv"))
    h4 = synth_h4()

    print(f"[1] 渲染 M15 图表（真实数据，{len(m15)} 根）...")
    png_m15 = render_chart(m15, "XAUUSD M15 (real 2026-08-13)")
    print(f"    M15 PNG: {'OK ' + str(len(png_m15)) + ' bytes' if png_m15 else 'FAIL'}")
    if png_m15:
        with open(os.path.join(HERE, "vision_test_m15.png"), "wb") as f:
            f.write(png_m15)

    print(f"[2] 渲染 H4 图表（合成结构，{len(h4)} 根）...")
    png_h4 = render_chart(h4, "XAUUSD H4 (synth)")
    print(f"    H4 PNG: {'OK ' + str(len(png_h4)) + ' bytes' if png_h4 else 'FAIL'}")
    if png_h4:
        with open(os.path.join(HERE, "vision_test_h4.png"), "wb") as f:
            f.write(png_h4)

    print("[3] 聚合逻辑验证（模拟模型返回）...")
    svc = VisionService()
    cases = [
        ({"h4": {"decision": "BUY", "confidence": 0.8}, "m15": {"decision": "BUY", "confidence": 0.7}}, "同向看多"),
        ({"h4": {"decision": "SELL", "confidence": 0.8}, "m15": {"decision": "SELL", "confidence": 0.6}}, "同向看空"),
        ({"h4": {"decision": "BUY", "confidence": 0.7}, "m15": {"decision": "SELL", "confidence": 0.7}}, "分歧→降权"),
        ({"h4": {"decision": "BUY", "confidence": 0.6}, "m15": {"decision": "HOLD", "confidence": 0.0}}, "仅H4有方向"),
        ({"h4": {"decision": "HOLD", "confidence": 0.0}, "m15": {"decision": "HOLD", "confidence": 0.0}}, "双观望→HOLD"),
    ]
    for obj, tag in cases:
        v = svc._aggregate(obj)
        print(f"    {tag:10s} -> dir={v.direction} conf={v.confidence:.2f} scale={v.weight_scale:.2f} "
              f"agree={v.agree} note={v.note}")

    if "--vision" in sys.argv:
        print("[4] 真实视觉模型调用（需已 pull qwen2.5vl:7b）...")
        v = svc._call_vision(png_h4, png_m15)
        print("    原始返回:", json.dumps(v, ensure_ascii=False)[:400] if v else "None（模型未就绪/不可用）")
        if v:
            agg = svc._aggregate(v)
            print("    聚合票:", json.dumps(agg.as_dict(), ensure_ascii=False))
    else:
        print("[4] 跳过真实模型调用（加 --vision 启用，需模型已拉取完成）")

    print("DONE")


if __name__ == "__main__":
    main()
