"""本地验证：Chronos-Bolt-base 从本地目录加载 + 对合成 XAU 序列推理。"""
import sys, os, time
sys.path.insert(0, "F:/WanxiangAI/backend")

import torch
import numpy as np

print(f"torch={torch.__version__} CUDA={torch.cuda.is_available()}", flush=True)

# 1) raw 直接验证 ChronosBoltPipeline 本地加载
from chronos import ChronosBoltPipeline
t0 = time.time()
try:
    pipe = ChronosBoltPipeline.from_pretrained(
        "F:/WanxiangAI/models/chronos-bolt-base",
        device_map="cuda" if torch.cuda.is_available() else "cpu",
        dtype=torch.bfloat16,
    )
    print(f"[raw] 加载成功 {time.time()-t0:.1f}s | 常驻显存 {torch.cuda.memory_allocated()/1e9:.2f}GB", flush=True)
except Exception as e:
    print(f"[raw] 加载失败: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

# 2) 推理：合成 XAU 序列（512 步）
np.random.seed(7)
prices = 4000 + np.cumsum(np.random.randn(512) * 2.0)
ctx = torch.tensor(prices, dtype=torch.float32)
t0 = time.time()
with torch.no_grad():
    pred = pipe.predict(inputs=ctx, prediction_length=24)  # (1, 9, 24) 分位数 [0.1..0.9]
print(f"[raw] 推理 {time.time()-t0:.2f}s | shape={tuple(pred.shape)} | 峰值显存 {torch.cuda.max_memory_allocated()/1e9:.2f}GB", flush=True)

p50 = pred[0][4]; p90 = pred[0][8]; p10 = pred[0][0]
last = float(ctx[-1])
print(f"[raw] 末价={last:.2f} | P50末={float(p50[-1]):.2f} P90末={float(p90[-1]):.2f} P10末={float(p10[-1]):.2f}", flush=True)

# 3) 验证 chronos_service 封装（单例 + 降级逻辑 + 分位数解析）
from app.services.chronos_service import ChronosEngine
eng = ChronosEngine.get()
res = eng.forecast(prices.tolist(), prediction_length=24)
if res is None:
    print("[svc] forecast 返回 None（异常）", flush=True)
    sys.exit(1)
print(f"[svc] direction={res['direction']} 末价={res['last_price']:.2f} "
      f"P50末={res['p50_final']:.2f} P90末={res['p90_final']:.2f} 带宽={res['uncertainty']:.4f}", flush=True)
print("[svc] status:", eng.status, flush=True)
print("=== 本地验证 END ===", flush=True)
