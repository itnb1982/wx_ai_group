# -*- coding: utf-8 -*-
"""
2026-08-16 视觉 Ollama 启动器（Python 版，绕过 PowerShell 5.1 ProcessStartInfo 兼容性）

背景：start_ollama_vision.ps1 在硬件切换（用户把显示器接到核显）后，
ProcessStartInfo.FileName 属性赋值报"找不到属性"，导致 11435 视觉实例
连续 FATAL。主实例 11434 因为走 Ollama 自启动任务（不依赖 PS 启动器）能跑。

方案：用 Python subprocess.Popen 显式 env dict 启动 ollama serve，绕过 PowerShell
5.1/7+ ProcessStartInfo 兼容性问题；同时根据 nvidia-smi 自动探测 GPU 拓扑，
将来硬件再变（增减卡/驱动变化）也不会崩溃。

用法：
  python start_ollama_vision.py                      # 默认 11435, GPU 1, F:/WanxiangAI/runtime/ollama
  python start_ollama_vision.py --port 11435 --gpu 1
  python start_ollama_vision.py --port 11436 --gpu 0
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time


# 默认可移植路径（优先运行时路径，其次项目 runtime 目录）
_DEFAULT_OLLAMA = r"F:\WanxiangAI\runtime\ollama\ollama.exe"
_DEFAULT_MODELS_DIR = r"F:\WanxiangAI\runtime\ollama-models"
_CREATE_NO_WINDOW = 0x08000000  # Windows: 不弹黑窗


def _detect_gpu_count() -> int:
    """用 nvidia-smi 探测可用 NVIDIA GPU 数（>= 0，0 表示无 CUDA 环境）。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=5,
        ).strip()
        return len([line for line in out.splitlines() if line.strip()])
    except Exception:
        return 0


def _resolve_gpu(preferred: int | None) -> int:
    """根据硬件拓扑与偏好决定最终 CUDA_VISIBLE_DEVICES。

    规则（2026-08-19 用 torch.cuda.get_device_properties 实测确认：
          CUDA0=8GB RTX3060Ti, CUDA1=12GB RTX3060）：
      - 0 张 NVIDIA：返回 -1（调用方应回退 CPU 并告警）
      - 1 张 NVIDIA：只能用 0
      - 2+ 张：视觉用 0(8GB RTX3060Ti)，主实例用 1(12GB RTX3060）
    """
    n = _detect_gpu_count()
    if n == 0:
        return -1
    if n == 1:
        return 0
    if preferred is None or preferred < 0 or preferred >= n:
        return 0
    return preferred


def _wait_nvidia_ready(timeout: float = 90.0) -> bool:
    """开机自启时等 NVIDIA 驱动就绪，避免 Ollama 误判无 CUDA 而永久降级 CPU。"""
    nv = shutil.which("nvidia-smi") or r"C:\Windows\system32\nvidia-smi.exe"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                [nv, "--query-gpu=index,memory.total", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=10,
            ).strip()
            gpus = [l for l in out.splitlines() if l.strip() and "MiB" in l]
            if len(gpus) >= 1:
                print(f"[start_ollama_vision.py] nvidia-smi 就绪, 识别到 {len(gpus)} 张 GPU")
                return True
        except Exception:
            pass
        time.sleep(3)
    print("[start_ollama_vision.py] 警告: 超时未检测到 nvidia-smi, 仍尝试启动(可能降级 CPU)",
          file=sys.stderr)
    return False


def _kill_ollama_on_port(port: int) -> None:
    """启动前按端口整树强杀占用本端口的旧 ollama（分离残留/CPU 实例），确保新实例可绑定。"""
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
                    print(f"[start_ollama_vision.py] 已整树强杀占用端口 {port} 的旧 ollama PID={pid}")
                except Exception as e:
                    print(f"[start_ollama_vision.py] 结束端口 {port} 旧进程 PID={pid} 失败: {e}",
                          file=sys.stderr)
    except Exception:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="视觉 Ollama 启动器（Python 版）")
    p.add_argument("--ollama", default=_DEFAULT_OLLAMA,
                   help="ollama.exe 路径")
    p.add_argument("--port", type=int, default=11435,
                   help="OLLAMA_HOST 端口")
    p.add_argument("--models-dir", default=_DEFAULT_MODELS_DIR,
                   help="OLLAMA_MODELS 模型目录")
    p.add_argument("--gpu", type=int, default=0,
                   help="CUDA_VISIBLE_DEVICES GPU 索引（写死 0=8GB RTX3060Ti，不依赖自动探测，"
                        "避免重启瞬间探测失败回退继承系统 CUDA_VISIBLE_DEVICES=1 而挤到 12GB 卡）")
    args = p.parse_args()

    if not os.path.isfile(args.ollama):
        print(f"FATAL: ollama.exe not found at {args.ollama}", file=sys.stderr)
        return 1

    gpu = _resolve_gpu(args.gpu)
    if gpu < 0:
        print("FATAL: 无 NVIDIA GPU 可用，视觉实例只能 CPU 回退",
              file=sys.stderr)
        # 仍允许启动（CPU 推理），但警告
        cuda_visible = ""
    else:
        cuda_visible = str(gpu)
        print(f"[start_ollama_vision.py] detected NVIDIA GPUs={_detect_gpu_count()}, "
              f"using CUDA_VISIBLE_DEVICES={cuda_visible}")

    env = os.environ.copy()
    if cuda_visible:
        env["CUDA_VISIBLE_DEVICES"] = cuda_visible
    # ★ 2026-08-19 硬核修复：Ollama 的 Vulkan 后端无视 CUDA_VISIBLE_DEVICES，
    #   能看到全部 GPU。模型加载时调度器选了显存更大的 12GB 卡(Vulkan2)而非
    #   本实例绑定的 8GB Ti(CUDA0)，导致两模型挤爆 12GB 卡、8GB Ti 空置。
    #   禁用 Vulkan 后设备列表只剩 CUDA0=8GB Ti，视觉模型必然落到 8GB 卡。
    env["OLLAMA_VULKAN"] = "0"
    env["OLLAMA_HOST"] = f"127.0.0.1:{args.port}"
    env["OLLAMA_MODELS"] = args.models_dir

    cwd = os.path.dirname(args.ollama)
    # 先清掉占用本端口的旧 ollama（配置变更/重启残留），再等驱动就绪
    _kill_ollama_on_port(args.port)
    # 开机自启时等驱动就绪，避免 Ollama 误判无 CUDA 而永久降级 CPU
    _wait_nvidia_ready()
    print(f"[start_ollama_vision.py] starting {args.ollama} serve in {cwd}")
    # ★ 2026-08-19 根治"任务计划拉起失败看不到报错"：serve 的 stdout/stderr 原本
    #   继承调用方（任务计划管道），输出块缓冲丢失 → 150s 超时后无从诊断。
    #   现重定向到 backend/ollama_vision_serve.log，下次失败可直接读 serve 真实日志。
    _serve_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ollama_vision_serve.log")
    _serve_log_path = os.path.normpath(_serve_log_path)
    try:
        _serve_handle = open(_serve_log_path, "a", encoding="utf-8", errors="replace")
    except Exception:
        _serve_handle = None
    subprocess.Popen(
        [args.ollama, "serve"],
        cwd=cwd,
        env=env,
        stdout=_serve_handle if _serve_handle is not None else subprocess.DEVNULL,
        stderr=_serve_handle if _serve_handle is not None else subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
        # 不等退出，detach 长期运行
    )
    if _serve_handle is not None:
        _serve_handle.flush()
    print(f"[start_ollama_vision.py] ollama serve launched on port {args.port}")
    # ★ 2026-08-19 关键修复：任务计划(Job)会在 .ps1 退出时回收子进程。
    #   主脑 start_ollama_main.py 靠 Popen 后 pull 保持存活 10 分钟，serve 才有
    #   足够时间完成初始化并脱离 Job；视觉此前 Popen 后立即 return，.ps1 轮询
    #   59s 超时就退出 → serve 被 Job 回收 → 11435 永远起不来(16:16 成功只因
    #   当时 serve 恰好 2s 内监听完成)。这里同样保持进程存活：轮询等待端口就绪
    #   (最多 120s)，确认 serve 正常监听后才退出，与主脑一致。
    import socket
    deadline = time.time() + 120.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", args.port), timeout=2):
                print(f"[start_ollama_vision.py] vision Ollama ready on port {args.port}")
                break
        except OSError:
            time.sleep(3)
    else:
        print(f"[start_ollama_vision.py] WARN: 120s 内端口 {args.port} 未就绪, 仍返回(可能被 Job 回收)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
