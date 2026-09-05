# -*- coding: utf-8 -*-
"""HuggingFace 惰性加载导致的 non-persistent buffer 未初始化 —— 体检与修复。

════════════════════════════════════════════════════════════════════
这是一个**静默正确性杀手**，本项目为它付出过代价，务必读完再改。
════════════════════════════════════════════════════════════════════

现象（2026-08-08 在 Time-MoE 200M 上实测）：
    · GPU 推理输出 NaN，CPU 推理不报错但结果是噪声
    · 同一份输入连续跑两次，结果不一样（不可复现）
    · 全程无任何异常、无警告，模型 loaded=True 一切"正常"

根因：
    RoPE 的 `inv_freq` 通常以 `register_buffer(..., persistent=False)` 注册。
    persistent=False 意味着它**不会被保存进 checkpoint**，设计上依赖
    `__init__` 里的公式现场计算。而 transformers 5.x 为了加速大模型加载，
    默认走 meta device + 惰性 materialize：__init__ 阶段张量只有形状没有内存，
    随后用 checkpoint 里的权重填充。checkpoint 里没有的 non-persistent buffer
    就永远得不到填充，materialize 出来的是**未初始化内存**——
    也就是显存/内存里上一个进程留下的垃圾。

    实测拿到的 inv_freq = [8.933666e+12, 0.0, 0.0, ...]
    而正确值应是 [1.0, 0.865, 0.749, ...] 这样的递减序列。
    各层数值还略有差异(8.933666e+12 / 8.93474e+12 / 8.934874e+12)，
    正是相邻未初始化内存块的指纹。

为什么必须专门体检而不能"跑通就算"：
    位置编码错乱不会让模型崩，只会让它变成一个**昂贵的随机数发生器**。
    如果带着这个 bug 去跑回测，会得到"该模型准确率 46%，不如抛硬币"的结论，
    然后据此把一个本来有用的模型踢出决策链——用错误的数据做出错误的架构决策，
    比模型本身不准严重得多。回测台的第一责任是保证被测对象是健康的。

修复后实测（合成序列，1 步预测）：
    修复前 CPU corr=0.6589 / GPU 全 NaN
    修复后 corr=0.9986，MAE 0.886 < "价格不变"基线 1.120，三次推理完全一致。
"""
from __future__ import annotations

from typing import Any


def _rope_expected(dim: int, base: float, device, dtype):
    """按 RoPE 原始定义重算 inv_freq: 1 / base^(2i/dim)。"""
    import torch
    return 1.0 / (base ** (torch.arange(0, dim, 2, dtype=torch.float32,
                                        device=device) / dim))


def inspect_buffers(model: Any, max_report: int = 12) -> dict:
    """体检所有 buffer，找出未初始化的嫌疑对象。

    判定规则（任一命中即可疑）：
      · 含 NaN / Inf
      · inv_freq 首元素不接近 1.0（RoPE 定义决定它必然 == 1.0）
      · inv_freq 不是单调递减
      · 绝对值出现 >1e6 的离谱数量级
    """
    import torch

    suspects, checked = [], 0
    for name, buf in model.named_buffers():
        if buf is None or not torch.is_tensor(buf) or buf.numel() == 0:
            continue
        checked += 1
        reason = None
        if torch.isnan(buf).any():
            reason = "含 NaN"
        elif torch.isinf(buf).any():
            reason = "含 Inf"
        elif name.endswith("inv_freq"):
            f = buf.float().flatten()
            if abs(float(f[0]) - 1.0) > 1e-3:
                reason = "inv_freq[0]=%.4g（应为 1.0）" % float(f[0])
            elif f.numel() > 1 and not bool((f[:-1] >= f[1:]).all()):
                reason = "inv_freq 非单调递减"
        elif float(buf.abs().max()) > 1e6:
            reason = "数量级异常 max=%.4g" % float(buf.abs().max())
        if reason:
            suspects.append({"name": name, "reason": reason,
                             "shape": tuple(buf.shape)})
    return {"checked": checked, "suspect_count": len(suspects),
            "suspects": suspects[:max_report], "healthy": not suspects}


def repair_rope_buffers(model: Any, device=None, dtype=None) -> dict:
    """重建所有 rotary_embedding 的 inv_freq 及其 cos/sin 缓存。

    幂等：对本来就健康的模型执行也无害（重算结果与正确值一致）。
    返回修复统计，调用方应记录日志——静默修复同样危险。
    """
    import torch

    if device is None:
        try:
            device = next(model.parameters()).device
        except StopIteration:
            device = torch.device("cpu")
    if dtype is None:
        try:
            dtype = next(model.parameters()).dtype
        except StopIteration:
            dtype = torch.float32

    cfg = getattr(model, "config", None)
    default_dim = None
    default_base = 10000.0
    if cfg is not None:
        hs = getattr(cfg, "hidden_size", None)
        nh = getattr(cfg, "num_attention_heads", None)
        if hs and nh:
            default_dim = hs // nh
        default_base = (getattr(cfg, "rope_theta", None)
                        or getattr(cfg, "rope_base", None) or 10000.0)

    repaired, failed = [], []
    for mod_name, mod in model.named_modules():
        if not hasattr(mod, "inv_freq"):
            continue
        try:
            dim = getattr(mod, "dim", None) or default_dim
            if not dim:
                # 从 buffer 长度反推：inv_freq 长度 == dim/2
                dim = int(mod.inv_freq.numel()) * 2
            base = getattr(mod, "base", None) or default_base
            inv = _rope_expected(int(dim), float(base), device, torch.float32)
            if inv.numel() != mod.inv_freq.numel():
                failed.append({"name": mod_name,
                               "why": "长度不匹配 %d vs %d"
                                      % (inv.numel(), mod.inv_freq.numel())})
                continue
            mod.inv_freq = inv
            # 旧式实现会预先缓存 cos/sin，必须一并重建，否则用的还是垃圾
            if hasattr(mod, "_set_cos_sin_cache"):
                seq = getattr(mod, "max_seq_len_cached", None) or 4096
                mod._set_cos_sin_cache(int(seq), device, dtype)
            repaired.append(mod_name)
        except Exception as e:  # noqa: BLE001
            failed.append({"name": mod_name,
                           "why": "%s: %s" % (type(e).__name__, str(e)[:80])})
    return {"repaired": len(repaired), "failed": failed,
            "names": repaired[:8]}


def load_and_repair(model, device=None, dtype=None, verbose: bool = True) -> dict:
    """加载后的标准收口动作：体检 → 修复 → 复检。

    任何用 trust_remote_code 加载的时序模型都应该过一遍这个函数。
    复检仍不健康时不静默放行，交由调用方决定是否弃用该模型。
    """
    before = inspect_buffers(model)
    fix = {"repaired": 0, "failed": []}
    if not before["healthy"]:
        fix = repair_rope_buffers(model, device, dtype)
    after = inspect_buffers(model)
    report = {"before": before, "fix": fix, "after": after,
              "ok": after["healthy"]}
    if verbose:
        if before["healthy"]:
            print("[buffer体检] 通过，检查 %d 个 buffer 无异常"
                  % before["checked"])
        else:
            print("[buffer体检] 发现 %d 个未初始化 buffer，例：%s"
                  % (before["suspect_count"],
                     "; ".join("%s(%s)" % (s["name"], s["reason"])
                               for s in before["suspects"][:3])))
            print("[buffer修复] 重建 %d 个 rotary_emb → 复检%s"
                  % (fix["repaired"], "通过" if after["healthy"]
                     else "仍异常(%d)" % after["suspect_count"]))
    return report
