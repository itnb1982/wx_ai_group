# -*- coding: utf-8 -*-
<#
万象Ai — PowerShell 侧的 Python 解释器发现（与 backend/runtime_paths.py 同策略）

为什么单独抽一份：计划任务注册脚本跑在 Python 之前，没法先 import Python 模块
来问"Python 在哪"。两边策略必须保持一致，否则会出现"手工启动能跑、开机自启跑不起来"
这种最难排查的环境错位。

优先级：WX_PYTHON 环境变量 -> 项目 .venv -> 项目 venv -> PATH -> py launcher
#>

function Find-WxPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot
    )

    # 1. 运维显式指定
    if ($env:WX_PYTHON -and (Test-Path -LiteralPath $env:WX_PYTHON)) {
        return $env:WX_PYTHON
    }

    # 2/3. 项目自带虚拟环境（优先，因为 torch/chronos 等重依赖只装在这里）
    foreach ($name in @(".venv", "venv")) {
        $cand = Join-Path $ProjectRoot "$name\Scripts\python.exe"
        if (Test-Path -LiteralPath $cand) { return $cand }
    }

    # 4. PATH
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # 5. py launcher
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $p = & py -3 -c "import sys;print(sys.executable)" 2>$null
            if ($p -and (Test-Path -LiteralPath $p)) { return $p }
        } catch { }
    }

    throw "未找到 Python 解释器。请先在项目根目录运行 bootstrap.bat，或安装 Python 3.11+ 并加入 PATH，或设置 WX_PYTHON 环境变量。"
}
