# -*- coding: utf-8 -*-
"""从 ModelScope（魔搭）下载本地时序模型权重到 models/ 目录。

为什么用魔搭而不是 HuggingFace：本机网络实测 huggingface.co 与
hf-mirror.com 均不可达，魔搭可正常访问。权重下载后随项目分发，
客户机离线即可加载（不再联网）。

用法（原生 PowerShell / cmd，**不要用 Git Bash**）：
    .venv\\Scripts\\python.exe backend\\scripts\\download_ts_models.py [模型名...]
不带参数则下载全部。
"""
from __future__ import annotations

import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_DIR = os.path.join(ROOT, "models")

# 本地目录名 -> 魔搭仓库 ID
TARGETS = {
    "timesfm-2.5-200m": "google/timesfm-2.5-200m-transformers",
    "moirai-moe-small": "Salesforce/moirai-moe-1.0-R-small",
}


def main() -> int:
    want = sys.argv[1:] or list(TARGETS.keys())
    os.makedirs(MODEL_DIR, exist_ok=True)
    from modelscope import snapshot_download

    rc = 0
    for name in want:
        repo = TARGETS.get(name)
        if not repo:
            print(f"[SKIP] 未知模型名: {name}")
            continue
        dest = os.path.join(MODEL_DIR, name)
        if os.path.isdir(dest) and os.listdir(dest):
            print(f"[SKIP] 已存在: {dest}")
            continue
        print(f"[DL] {name}  <-  {repo}")
        t0 = time.time()
        try:
            path = snapshot_download(repo, local_dir=dest)
            size = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(path)
                for f in fs
            )
            print(f"[OK] {name} 完成 {size/1024**2:.0f}MB 耗时{time.time()-t0:.0f}s -> {path}")
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
