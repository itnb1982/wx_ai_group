"""Chronos-Bolt-base 技术可行性验证：加载 + XAU 风格序列分位数预测 + 延迟/显存。
仅验证推理管线、显存占用、输出分位数形态，不接入真实行情（真实质量后续 shadow 模式验证）。
"""
import time, torch, numpy as np
from chronos import ChronosPipeline

LOG = "F:/WanxiangAI/chronos_verify.log"
def log(s):
    with open(LOG, "a", encoding="utf8") as f:
        f.write(s + "\n")
    print(s)

log("=== Chronos-Bolt-base 验证 START ===")
log(f"torch={torch.__version__} CUDA={torch.cuda.is_available()} 设备={torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")

# 1) 加载模型
t0 = time.time()
pipe = ChronosPipeline.from_pretrained(
    "amazon/chronos-bolt-base",
    device_map="cuda",
    torch_dtype=torch.bfloat16,
)
load_s = time.time() - t0
alloc_gb = torch.cuda.memory_allocated() / 1e9
log(f"模型加载耗时={load_s:.1f}s | 常驻显存={alloc_gb:.2f}GB")

# 2) 合成 XAU 风格序列（约 4000 水平随机游走，长度 512 步 = 上下文窗口上限）
np.random.seed(7)
prices = 4000 + np.cumsum(np.random.randn(512) * 2.0)
context = torch.tensor(prices, dtype=torch.float32)
log(f"上下文长度={len(context)} | 末价={context[-1]:.2f}")

# 3) 推理：预测未来 24 步，20 个样本 -> 分位数
t0 = time.time()
with torch.no_grad():
    pred = pipe.predict(context=context, prediction_length=24, num_samples=20)
infer_s = time.time() - t0
peak_gb = torch.cuda.max_memory_allocated() / 1e9
log(f"推理耗时={infer_s:.2f}s (24步/20样本) | 峰值显存={peak_gb:.2f}GB")
log(f"预测张量 shape={tuple(pred.shape)}")

# 4) 取 P10/P50/P90
p10 = torch.quantile(pred[0], 0.10, dim=0)
p50 = torch.quantile(pred[0], 0.50, dim=0)
p90 = torch.quantile(pred[0], 0.90, dim=0)
last = context[-1].item()
log(f"末价={last:.2f}")
log(f"P50 未来1/6/12/24步={p50[0]:.2f}/{p50[5]:.2f}/{p50[11]:.2f}/{p50[23]:.2f}")
log(f"P90 未来24步={p90[23]:.2f} | P10 未来24步={p10[23]:.2f}")
spread = (p90[23] - p10[23]) / last * 100
log(f"24步预测带宽度(P90-P10)/末价={spread:.2f}%")
log("=== Chronos-Bolt-base 验证 END ===")
