#Requires -Version 5.1
param([switch]$Force)
<#
  启动主脑 Ollama 实例（绑 gpu1/CUDA1 / port 11434，跑 qwen3:8b）。

  2026-08-19 改为委托 Python 启动器（scripts/start_ollama_main.py），
  与视觉实例一致：subprocess.Popen + detach，任务计划(ONBOOT)下进程独立存活，
  根治「整机重启后主脑 11434 起不来 → qwen 离线 → 决策退化」的问题。

  默认幂等：若 11434 已监听则直接退出；带 -Force 时先杀掉旧实例再启动，
  确保配置变更（如 CUDA 绑定）后真正生效。
#>
$ErrorActionPreference = "Stop"
$port = 11434

function Test-MainPort {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/tags" -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (Test-MainPort) {
    if (-not $Force) {
        Write-Host "[start_ollama] 11434 已监听，Ollama 已在运行。"
        exit 0
    }
    Write-Host "[start_ollama] -Force 已指定，结束旧 11434 监听后再启动..."
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        $oldPid = $conn.OwningProcess
        if ($oldPid) {
            Stop-Process -Id $oldPid -Force -ErrorAction Stop
            Start-Sleep -Seconds 2
        }
    } catch {
        Write-Warning "[start_ollama] 结束旧实例失败(继续启动): $_"
    }
}

$py = "F:\WanxiangAI\.venv\Scripts\python.exe"
$backendDir = if ($PSScriptRoot) { $PSScriptRoot } else { "F:\WanxiangAI\backend" }
$script = Join-Path $backendDir "scripts\start_ollama_main.py"
if (-not (Test-Path $py)) { Write-Error "[start_ollama] python.exe not found: $py"; exit 1 }
if (-not (Test-Path $script)) { Write-Error "[start_ollama] start_ollama_main.py not found: $script"; exit 1 }

try {
    # 直接调用 Python 启动器（阻塞直到它 spawn ollama serve 并 pull 完模型后返回）。
    # 不走 Start-Process，避免 PS 5.1 参数绑定问题 + 任务计划会话结束回收子进程。
    & $py $script 2>&1 | ForEach-Object { Write-Host "  py: $_" }
    Write-Host "[start_ollama] Python launcher executed (ollama serve spawned)"
} catch {
    Write-Error "[start_ollama] Python launcher failed: $_"
    exit 1
}

$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-MainPort) { $ready = $true; break }
}
if (-not $ready) {
    Write-Error "[start_ollama] timed out waiting for Ollama on 11434"
    exit 1
}
Write-Host "[start_ollama] Ollama ready on 11434/gpu0"
exit 0
