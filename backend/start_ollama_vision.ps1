#Requires -Version 5.1
param([switch]$Force)
<#
  WanxiangAI - Vision-model dedicated Ollama instance (bound to gpu1 / port 11435)
  Dual 3060 Ti plan (2026-08-14 / model upgrade 2026-08-16):
    gpu0 -> main instance (11434) runs qwen3:8b (normal-mode co-pilot)
    gpu1 -> this instance (11435) runs qwen2.5vl:7b (vision 4th vote, GPU, 3b->7b)
    CPU  -> Chronos-2 + time-series arena 4 models

  2026-08-16 ROBUSTNESS FIX (hardware topology change):
    Old impl used PowerShell 5.1 ProcessStartInfo directly. After user switched
    display to iGPU, FileName property assignment failed ("cannot find property"),
    so 11435 vision instance kept FATAL and could not self-heal.
    New impl delegates to scripts/start_ollama_vision.py (Python subprocess.Popen
    with explicit env dict) - stable across PS versions and hardware topology,
    auto-detects NVIDIA GPUs via nvidia-smi.

  Idempotent: if 11435 is already listening, exit immediately (no port fight).
  -Force: kill any existing 11435 listener and restart (used by backend self-heal).
  NOTE: comments are ASCII-only to avoid PowerShell -File GBK/UTF-8 parsing bugs.
#>
$ErrorActionPreference = "Continue"
$modelDir = "F:\WanxiangAI\runtime\ollama-models"
$visionPort = 11435
# 2026-08-16: $PSScriptRoot may be empty under some invocation contexts
# (PowerShell tool -NonInteractive / some scheduled-task contexts), which made
# Join-Path fall back to a relative path (log lost / script path broken).
# Add an absolute-path fallback (this script is fixed at F:\WanxiangAI\backend).
$_backendRoot = if ($PSScriptRoot) { $PSScriptRoot } else { "F:\WanxiangAI\backend" }
$logFile = Join-Path $_backendRoot "ollama_vision_startup.log"
function Log($m) { "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')] $m" | Out-File -Append -FilePath $logFile -Encoding UTF8 }

Log "=== start_ollama_vision.ps1 invoked (pid $pid) ==="
try {
    $env:OLLAMA_MODELS = $modelDir
    $env:OLLAMA_HOST = "127.0.0.1:$visionPort"
    # 2026-08-19 修正：不再硬编码 CUDA_VISIBLE_DEVICES，交给 Python 启动器按当前硬件拓扑选择。
    # torch 实测本机：CUDA0=8GB RTX3060Ti, CUDA1=12GB RTX3060。
    # 视觉实例应落在 8GB Ti(CUDA0)，主脑实例落在 12GB(CUDA1)。
    # 单并发+单模型常驻，控制显存不泄漏。
    $env:OLLAMA_NUM_PARALLEL = "1"
    $env:OLLAMA_MAX_LOADED_MODELS = "1"
    $env:OLLAMA_KEEP_ALIVE = "24h"
    $env:OLLAMA_CONTEXT_LENGTH = "4096"
    Log "env set: OLLAMA_HOST=$env:OLLAMA_HOST (CUDA绑定由 Python 启动器自动选择)"

    function Test-VisionPort {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$visionPort/api/tags" -UseBasicParsing -TimeoutSec 2
            return ($r.StatusCode -eq 200)
        } catch { return $false }
    }

    if (Test-VisionPort) {
        if (-not $Force) {
            Log "11435 already listening -> idempotent exit 0"
            Write-Host "[start_ollama_vision] 11435 already listening, vision Ollama is running on gpu1."
            exit 0
        }
        Log "11435 listening but -Force set -> killing existing listener first"
        try {
            $conn = Get-NetTCPConnection -LocalPort $visionPort -State Listen -ErrorAction Stop
            $oldPid = $conn.OwningProcess
            if ($oldPid) {
                Log "killing old 11435 listener PID=$oldPid"
                Stop-Process -Id $oldPid -Force -ErrorAction Stop
                Start-Sleep -Seconds 2
            }
        } catch {
            Log "WARN: failed to kill old 11435 listener: $_"
        }
    }
    Log "11435 not listening, starting serve"

    # 2026-08-16 ROBUSTNESS FIX: delegate to Python launcher
    # (subprocess.Popen + env dict, cross-version, auto GPU topology detect).
    $py = "F:\WanxiangAI\.venv\Scripts\python.exe"
    $backendDir = if ($PSScriptRoot) { $PSScriptRoot } else { "F:\WanxiangAI\backend" }
    $script = Join-Path $backendDir "scripts\start_ollama_vision.py"
    if (-not (Test-Path $py)) {
        Log "ERROR: Python launcher python.exe not found: $py"
        Write-Error "[start_ollama_vision] python.exe not found."
        exit 1
    }
    if (-not (Test-Path $script)) {
        Log "ERROR: Python launcher script not found: $script"
        Write-Error "[start_ollama_vision] start_ollama_vision.py not found."
        exit 1
    }
    try {
        # Invoke Python launcher directly (blocking until it spawns ollama serve
        # then returns). Avoid Start-Process to dodge PS 5.1 arg-binding issues.
        # 显式传 --gpu 0（8GB RTX3060Ti），写死不依赖自动探测，杜绝重启瞬间
        # 探测失败回退继承系统 CUDA_VISIBLE_DEVICES=1 而把视觉实例挤到 12GB 卡。
        & $py $script --gpu 0 2>&1 | ForEach-Object { Log "  py: $_" }
        Log "Python launcher executed (ollama serve spawned)"
    } catch {
        Log "FATAL: Python launcher failed: $_"
        Write-Error "[start_ollama_vision] Python launcher failed: $_"
        exit 1
    }
    $env:OLLAMA_HOST = "127.0.0.1:$visionPort"

    # Old ProcessStartInfo path retired (kept for traceability):
    #   $psi = New-Object System.Diagnostics.ProcessStartInfo
    #   $psi.FileName = $ollama
    #   $psi.Arguments = "serve"
    #   $psi.WorkingDirectory = (Split-Path $ollama)
    #   $psi.UseShellExecute = $false
    #   $psi.CreateNoWindow = $true
    #   $psi.EnvironmentVariables["OLLAMA_MODELS"] = $modelDir
    #   $psi.EnvironmentVariables["OLLAMA_HOST"] = "127.0.0.1:$visionPort"
    #   $psi.EnvironmentVariables["CUDA_VISIBLE_DEVICES"] = "1"
    #   [void][System.Diagnostics.Process]::Start($psi)
    #   Log "Start-Process ollama serve issued (ProcessStartInfo CUDA_VISIBLE_DEVICES=1)"

    $deadline = (Get-Date).AddSeconds(150)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-VisionPort) { $ready = $true; break }
    }
    if (-not $ready) {
        Log "ERROR: timed out waiting for vision Ollama on 11435"
        Write-Error "[start_ollama_vision] timed out waiting for vision Ollama."
        exit 1
    }
    Log "vision Ollama ready on 11435/gpu1"
    Write-Host "[start_ollama_vision] vision Ollama (11435/gpu1) is ready."
    try {
        # Keep model in sync with backend config VISION_MODEL (currently qwen2.5vl:7b).
        # Explicitly target this instance's port to avoid using the 11434 main instance.
        $env:OLLAMA_HOST = "127.0.0.1:$visionPort"
        & "F:\WanxiangAI\runtime\ollama\ollama.exe" pull qwen2.5vl:7b 2>&1 | ForEach-Object { Log "  pull: $_" }
        Log "qwen2.5vl:7b pull done"
    } catch {
        Log "WARN: pull qwen2.5vl:7b failed (maybe already present): $_"
    }
    exit 0
} catch {
    Log "FATAL: $_"
    Write-Error "[start_ollama_vision] fatal: $_"
    exit 1
}
