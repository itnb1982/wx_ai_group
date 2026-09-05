"""逐个加载 torch/lib 下的 DLL，定位 `import torch` 时 ACCESS_VIOLATION 的元凶。

背景（2026-08-08 事故）：
  supervisor 用 venv 解释器拉起 uvicorn 后，进程每次都在
  chronos_service._ensure_loaded → `import torch` 处
  抛 Windows fatal exception: access violation（退出码 0xC0000005）→ 无限重启。
  裸解释器没装 torch，Chronos 直接降级（可用=False），所以"直连不崩"是假象。

用法（每个 DLL 起独立子进程，崩了也不影响主流程）：
  <venv-python> scripts/diagnostics/diag_torch_dll.py
"""

import os
import subprocess
import sys

CHILD_CODE = r"""
import ctypes, sys, os
p = sys.argv[1]
os.add_dll_directory(os.path.dirname(p))
ctypes.CDLL(p)
print("OK")
"""


def main() -> int:
    # ★ 绝不能在本进程 import torch：access violation 是进程级崩溃，
    #   try/except 抓不住，整个诊断脚本会静默消失（本脚本第一版就这么翻车的）。
    #   改为纯路径推断定位 torch/lib。
    lib_dir = os.path.join(
        os.path.dirname(os.path.dirname(sys.executable)),
        "Lib", "site-packages", "torch", "lib",
    )

    if not os.path.isdir(lib_dir):
        print(f"未找到 torch/lib 目录: {lib_dir}")
        return 2

    dlls = sorted(f for f in os.listdir(lib_dir) if f.lower().endswith(".dll"))
    print(f"torch/lib = {lib_dir}")
    print(f"共 {len(dlls)} 个 DLL，逐个在独立子进程中加载...\n")

    bad = []
    for name in dlls:
        full = os.path.join(lib_dir, name)
        try:
            r = subprocess.run(
                [sys.executable, "-c", CHILD_CODE, full],
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            print(f"  [超时] {name}")
            bad.append((name, "timeout"))
            continue

        if r.returncode == 0:
            continue

        # 0xC0000005 在 Python 里回显为 3221225477
        tag = "ACCESS_VIOLATION" if r.returncode == 3221225477 else f"rc={r.returncode}"
        err = (r.stderr or "").strip().splitlines()
        detail = err[-1] if err else ""
        print(f"  [失败] {name}  {tag}  {detail}")
        bad.append((name, tag))

    print()
    if bad:
        print(f"共 {len(bad)} 个 DLL 加载失败：")
        for name, tag in bad:
            print(f"  - {name}  ({tag})")
    else:
        print("所有 DLL 单独加载均成功 —— 崩溃点不在单个 DLL，")
        print("更可能是 torch/_C.pyd 初始化阶段（CUDA 上下文 / 符号冲突）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
