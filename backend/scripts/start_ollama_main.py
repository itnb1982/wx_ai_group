# -*- coding: utf-8 -*-
"""
2026-08-19 主脑 Ollama 启动器（Python 版，与视觉实例 start_ollama_vision.py 一致）

背景：原 start_ollama.ps1 用 PowerShell Start-Process 启动 ollama serve，
在任务计划（WanxiangOllama, ONBOOT）会话结束时子进程被回收，导致整机重启后
主脑 11434 起不来、qwen 离线、决策退化（之前误判为"自动重启失效"）。
改用 Python subprocess.Popen（creationflags=_CREATE_NO_WINDOW）让 ollama 进程
脱离任务会话独立存活（视觉实例已验证此方式在任务计划下持久）。

绑定：gpu1 (CUDA_VISIBLE_DEVICES=1) = 12GB RTX 3060 / port 11434 / qwen3:8b
（注：本机 CUDA 序号与 nvidia-smi 序号不一致——CUDA0=8GB RTX3060Ti, CUDA1=12GB RTX3060，
 故主脑必须绑 CUDA1 才能吃满 12GB 显存；视觉实例绑 CUDA0）
容量治理：NUM_PARALLEL=1 + MAX_LOADED_MODELS=1 + CONTEXT_LENGTH=4096
（单并发单模型常驻，KV cache 不按并行数倍增；上下文锁 4096，业务注释自认
 4096 完全容得下、显存增量 0MB）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time


_DEFAULT_OLLAMA = r"F:\WanxiangAI\runtime\ollama\ollama.exe"
_DEFAULT_MODELS_DIR = r"F:\WanxiangAI\runtime\ollama-models"
_CREATE_NO_WINDOW = 0x08000000  # Windows: 不弹黑窗


def _wait_nvidia_ready(timeout: float = 90.0) -> bool:
    """开机自启时 NVIDIA 驱动可能尚未就绪，Ollama 会误判无 CUDA 而永久降级到 CPU
    （2026-08-19 实测：新装 12GB 卡后整机重启，Ollama 在驱动就绪前启动→两张卡全 CPU）。
    启动 ollama serve 前轮询 nvidia-smi，确认驱动已识别到 GPU 再继续，规避该竞态。"""
    nv = shutil.which("nvidia-smi") or r"C:\Windows\system32\nvidia-smi.exe"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                [nv, "--query-gpu=index,memory.total", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL, text=True, timeout=10,
            ).strip()
            gpus = [l for l in out.splitlines() if l.strip() and "MiB" in l]
            if len(gpus) >= 1:
                print(f"[start_ollama_main.py] nvidia-smi 就绪, 识别到 {len(gpus)} 张 GPU")
                return True
        except Exception:
            pass
        time.sleep(3)
    print("[start_ollama_main.py] 警告: 超时未检测到 nvidia-smi, 仍尝试启动(可能降级 CPU)",
          file=sys.stderr)
    return False


def _kill_ollama_on_port(port: int) -> None:
    """配置变更/重启后，旧 ollama serve 可能因进程分离而残留并占用本端口（且可能跑在 CPU）。
    启动前按端口找到占用进程并整树强杀，确保新实例能绑定端口且不被旧实例干扰。
    本启动器经任务计划以高权限运行，故 taskkill 可成功（沙箱直接杀会被拒绝访问）。"""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", pid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
                    print(f"[start_ollama_main.py] 已整树强杀占用端口 {port} 的旧 ollama PID={pid}")
                except Exception as e:
                    print(f"[start_ollama_main.py] 结束端口 {port} 旧进程 PID={pid} 失败: {e}",
                          file=sys.stderr)
    except Exception:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="主脑 Ollama 启动器（Python 版）")
    p.add_argument("--ollama", default=_DEFAULT_OLLAMA, help="ollama.exe 路径")
    p.add_argument("--port", type=int, default=11434, help="OLLAMA_HOST 端口")
    p.add_argument("--models-dir", default=_DEFAULT_MODELS_DIR, help="OLLAMA_MODELS 模型目录")
    p.add_argument("--gpu", type=int, default=1, help="CUDA_VISIBLE_DEVICES GPU 索引(1=12GB RTX 3060)")
    args = p.parse_args()

    if not os.path.isfile(args.ollama):
        print(f"FATAL: ollama.exe not found at {args.ollama}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(args.gpu)
    env["OLLAMA_HOST"] = f"127.0.0.1:{args.port}"
    env["OLLAMA_MODELS"] = args.models_dir
    env["OLLAMA_NUM_PARALLEL"] = "1"
    env["OLLAMA_MAX_LOADED_MODELS"] = "1"
    env["OLLAMA_KEEP_ALIVE"] = "24h"
    env["OLLAMA_CONTEXT_LENGTH"] = "4096"
    cwd = os.path.dirname(args.ollama)
    # 先清掉占用本端口的旧 ollama（配置变更/重启残留），再等驱动就绪
    _kill_ollama_on_port(args.port)
    # 开机自启时等驱动就绪，避免 Ollama 误判无 CUDA 而永久降级 CPU
    _wait_nvidia_ready()
    print(f"[start_ollama_main.py] starting {args.ollama} serve (gpu{args.gpu}, port {args.port})")
    # detach：脱离调用方（任务计划）会话，独立存活
    subprocess.Popen(
        [args.ollama, "serve"],
        cwd=cwd,
        env=env,
        creationflags=_CREATE_NO_WINDOW,
    )
    print(f"[start_ollama_main.py] ollama serve launched on port {args.port}")
    # 确保主脑模型已装（已装则秒回，未装则拉取，最多等 10 分钟）
    try:
        subprocess.run([args.ollama, "pull", "qwen3:8b"], env=env, cwd=cwd, timeout=600, check=False)
        print("[start_ollama_main.py] qwen3:8b ensured")
    except Exception as e:
        print(f"WARN: pull qwen3:8b failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
