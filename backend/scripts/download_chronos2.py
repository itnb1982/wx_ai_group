"""下载 Chronos-2 120M 本地权重（modelscope 优先，hf-mirror 回退）。
目标目录：<项目根>/models/chronos-2
Chronos-2 支持多变量 + 协变量（past/future/real/categorical），上下文 8192，Apache-2.0。
"""
import os
import sys
import time
from pathlib import Path

# 按脚本自身位置推导，客户机装在任何盘符都能正确落地
TARGET = str(Path(__file__).resolve().parents[2] / "models" / "chronos-2")
SRC = "amazon/chronos-2"
os.makedirs(TARGET, exist_ok=True)


def try_modelscope():
    from modelscope import snapshot_download
    return snapshot_download(SRC, local_dir=TARGET)


def try_hf_mirror():
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    from huggingface_hub import snapshot_download
    return snapshot_download(SRC, local_dir=TARGET)


for fn in (try_modelscope, try_hf_mirror):
    for attempt in range(3):
        try:
            p = fn()
            print("DOWNLOAD_OK", p)
            sys.exit(0)
        except Exception as e:
            print("ATTEMPT_FAIL", attempt, repr(e)[:240])
            time.sleep(5)

print("ALL_FAILED")
sys.exit(1)
