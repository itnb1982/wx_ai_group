"""从 modelscope 下载 Chronos-Bolt-base 权重到本地（huggingface.co 被墙，用国内镜像）。
下载后 chronos_service 用本地路径加载，避免运行时依赖被墙的 HF。
修正：modelscope 1.39.1 的 snapshot_download 无 local_dir_use_symlink 参数。
"""
import os, sys, traceback
from modelscope.hub.snapshot_download import snapshot_download

LOG = "F:/WanxiangAI/download_chronos.log"
def log(s):
    with open(LOG, "a", encoding="utf8") as f:
        f.write(s + "\n")
    print(s)

TARGET = "F:/WanxiangAI/models/chronos-bolt-base"
CACHE = "F:/WanxiangAI/models/.cache"
os.makedirs(TARGET, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)

candidates = [
    "Amazon/chronos-bolt-base",
    "amazon/chronos-bolt-base",
    "Amazon/chronos-bolt-small",
    "amazon/chronos-bolt-small",
    "Amazon/chronos-bolt-tiny",
]

ok = False
for mid in candidates:
    try:
        log(f"尝试下载: {mid} ...")
        path = snapshot_download(mid, cache_dir=CACHE, local_dir=TARGET)
        log(f"SUCCESS {mid} -> {path}")
        ok = True
        break
    except Exception as e:
        log(f"FAIL {mid}: {type(e).__name__}: {str(e)[:300]}")
        log(traceback.format_exc()[:500])

if not ok:
    log("所有候选 ID 均失败，需手动在 modelscope 搜索正确 ID")
    sys.exit(2)
log("DONE")
