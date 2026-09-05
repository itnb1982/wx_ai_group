# -*- coding: utf-8 -*-
"""
视觉模型下载智能守护（自愈版）
- 目标：qwen2.5vl:3b（约2GB，3b体量可靠，7b在本机网络持续卡94%故弃用）
- 判死：监控 ollama-models/blobs 目录总体积，连续 STALL_SEC 秒零增长 => 杀全部 ollama 进程并重拉
- 健壮性：subprocess 输出用 errors='replace' 读，避免中文 Windows GBK 字节导致 UnicodeDecodeError 崩溃
- 循环直到 /api/tags 出现目标模型
"""
import os
import time
import json
import subprocess
import urllib.request

MODEL = "qwen2.5vl:3b"
MODELS_DIR = r"F:\WanxiangAI\runtime\ollama-models"
BLOBS = os.path.join(MODELS_DIR, "blobs")
OLLAMA = r"F:\WanxiangAI\runtime\ollama\ollama.exe"
TAGS = "http://127.0.0.1:11434/api/tags"
STALL_SEC = 180          # 连续 180s 零增长判定卡死
MAX_RUN = 4 * 3600       # 最多跑 4 小时
LOG = r"F:\WanxiangAI\backend\ollama_pull.log"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def tags_has() -> bool:
    try:
        with urllib.request.urlopen(TAGS, timeout=5) as r:
            d = json.load(r)
        return any(m.get("name") == MODEL for m in d.get("models", []))
    except Exception:
        return False


def blobs_size() -> int:
    try:
        total = 0
        for fn in os.listdir(BLOBS):
            if "sha256" in fn:
                total += os.path.getsize(os.path.join(BLOBS, fn))
        return total
    except Exception:
        return -1


def kill_ollama():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"],
                       capture_output=True, timeout=20)
    except Exception as e:
        log(f"kill_ollama err: {e}")


def start_pull():
    env = dict(os.environ)
    env["OLLAMA_MODELS"] = MODELS_DIR
    # errors='replace' 防止中文 Windows 控制台 GBK 字节触发 UnicodeDecodeError 崩溃
    p = subprocess.Popen(
        [OLLAMA, "pull", MODEL],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", env=env,
    )
    return p


def main():
    log(f"=== 下载守护启动：目标 {MODEL} ===")
    pull = None
    last_size = -1
    last_grow = time.time()
    t0 = time.time()

    while time.time() - t0 < MAX_RUN:
        if tags_has():
            log("✅ 模型已就位，守护退出")
            return

        sz = blobs_size()
        if sz > last_size + 1024 * 1024:   # 增长 >1MB 视为有进展
            last_size = sz
            last_grow = time.time()

        # 确保有一个 pull 在跑
        if pull is None or pull.poll() is not None:
            if pull is not None and pull.poll() is not None:
                log("pull 进程已退出，准备重拉")
            kill_ollama()
            time.sleep(2)
            pull = start_pull()
            log(f">>> 启动 ollama pull {MODEL}（blobs当前 {sz/1e9:.2f}GB）")
            time.sleep(8)
            continue

        # 卡死判定
        stalled = (time.time() - last_grow) > STALL_SEC
        if stalled:
            log(f"⚠️ 卡死检测：blobs={sz/1e9:.2f}GB，距上次增长已 {(time.time()-last_grow):.0f}s -> 杀进程重拉")
            try:
                pull.kill()
            except Exception:
                pass
            kill_ollama()
            last_size = -1
            last_grow = time.time()
            pull = None
            time.sleep(3)
            continue

        time.sleep(20)

    log("⏰ 超过最大运行时间，守护退出（模型仍未就位，需人工介入）")


if __name__ == "__main__":
    main()
