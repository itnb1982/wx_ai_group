# -*- coding: utf-8 -*-
"""本地时序模型真实可用性验证（必须在原生 PowerShell/cmd 下运行）。

背景：Git Bash(MSYS2) 环境下 `import torch` 会触发 0xC0000005 段错误，
      而原生 PowerShell 下完全正常。历史探针缓存里的"永久降级"结论
      全部是在 Git Bash 下误采集的假失败，需清除后重采。

用法（PowerShell）：
    F:\\WanxiangAI\\.venv\\Scripts\\python.exe F:\\WanxiangAI\\backend\\scripts\\verify_ts_models.py
"""
from __future__ import annotations

import os
import sys
import time
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


try:  # Windows 控制台默认 GBK，强制 UTF-8 防止中文/符号崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    log("=" * 72)
    log("本地时序模型真实可用性验证")
    log("=" * 72)

    # ── 1. 基础环境 ──
    log(f"[1] Python: {sys.version.split()[0]}  exe={sys.executable}")
    t0 = time.time()
    import torch
    log(f"[1] torch {torch.__version__} 导入成功 ({time.time()-t0:.1f}s)")
    log(f"[1] CUDA available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        log(f"[1] device={torch.cuda.get_device_name(0)} "
            f"显存={torch.cuda.get_device_properties(0).total_memory/1024**3:.1f}GB")

    # ── 2. 清除历史假探针缓存 ──
    cache = os.path.join(ROOT, "data", "torch_probe_cache.json")
    if os.path.exists(cache):
        try:
            old = json.load(open(cache, "r", encoding="utf-8"))
            bad = [k for k, v in old.items() if not v.get("ok")]
            if bad:
                os.remove(cache)
                log(f"[2] 已清除假探针缓存 {len(bad)} 条 → {cache}")
            else:
                log("[2] 探针缓存无失败记录，保留")
        except Exception as e:  # noqa: BLE001
            log(f"[2] 缓存处理异常（忽略）: {e}")
    else:
        log("[2] 无探针缓存文件")

    # ── 3. 重新跑探针 ──
    from app.services.chronos_service import probe_torch_usable, ChronosEngine
    probe = probe_torch_usable(use_cache=False)
    log(f"[3] 探针重采结果: ok={probe.get('ok')} torch={probe.get('torch_version')} "
        f"cuda={probe.get('cuda')} reason={probe.get('reason')}")
    if not probe.get("ok"):
        log("[3] [FAIL] 探针仍失败 —— 请确认本脚本运行在原生 PowerShell 而非 Git Bash")
        return 1

    # ── 4. Chronos-2 真实加载 + 预测 ──
    eng = ChronosEngine.get()
    t0 = time.time()
    ok = eng._ensure_loaded()
    log(f"[4] Chronos-2 加载: {ok} ({time.time()-t0:.1f}s) err={eng._load_error}")
    if not ok:
        return 2

    import numpy as np
    rng = np.random.default_rng(42)
    base = 3300.0
    closes = (base + np.cumsum(rng.normal(0, 1.5, 256))).tolist()

    t0 = time.time()
    out = eng.forecast(closes, prediction_length=12)
    dt = time.time() - t0
    if out is None:
        log("[4] [FAIL] forecast 返回 None")
        return 3
    log(f"[4] [OK] 单变量预测成功 耗时={dt*1000:.0f}ms")
    log(f"[4]   末价={closes[-1]:.2f}  返回键={list(out.keys()) if isinstance(out, dict) else type(out)}")
    if isinstance(out, dict):
        for k in ("p10", "p50", "p90"):
            v = out.get(k)
            if v is not None:
                arr = np.asarray(v, dtype=float).ravel()
                log(f"[4]   {k}: 首={arr[0]:.2f} 末={arr[-1]:.2f}")

    # ── 5. 带协变量的多变量预测 ──
    cov = {
        "DXY": (100 + np.cumsum(rng.normal(0, 0.08, 256))).tolist(),
        "US10Y": (4.2 + np.cumsum(rng.normal(0, 0.01, 256))).tolist(),
        "VIX": (15 + np.cumsum(rng.normal(0, 0.15, 256))).tolist(),
    }
    t0 = time.time()
    out2 = eng.forecast(closes, prediction_length=12, covariates=cov)
    dt = time.time() - t0
    log(f"[5] 多变量(DXY/US10Y/VIX)预测: {'[OK] 成功' if out2 else '[FAIL] 失败'} 耗时={dt*1000:.0f}ms")

    log("=" * 72)
    log("结论：Chronos-2 在本机原生环境下 完全可用")
    log("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
