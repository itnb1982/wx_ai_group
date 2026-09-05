<# 万象Ai 后端重启（任务计划程序模式，稳健版）#>
$ErrorActionPreference = "Continue"
$LOG = "F:\WanxiangAI\backend\restart_log.txt"
$TASK = "WanxiangAIBackend"
$OLLAMA_EXE = "F:\WanxiangAI\runtime\ollama\ollama.exe"
$OLLAMA_MODELS = "F:\WanxiangAI\runtime\ollama-models"
$OLLAMA_TASK = "WanxiangOllama"

function Log($msg) {
    $t = Get-Date -Format "HH:mm:ss"
    $line = "$t $msg"
    try { $line | Out-File -FilePath $LOG -Append -Encoding utf8 } catch {}
    Write-Host $line
}

function Ensure-Ollama {
    Log "  检查 Ollama 服务..."
    $running = $false
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch {}
    # ★ 2026-08-19 强制重启（容量变量须进程启动时读取才生效）
    # 只杀端口监听进程(ollama serve)杀不掉子进程 ollama_llama_server(僵尸 runner 仍占显存)，
    # 且普通 Stop-Process 对"卡死在 NVIDIA 驱动层的僵尸"无效(用户态无法回收其 CUDA 上下文)。
    # 改用 taskkill /F /T 强制树杀 + 重试3次 + 校验是否真消失；残留则说明是驱动层卡死僵尸，
    # 只能整机重启/GPU reset 回收 → 此处明确告警，不隐瞒。
    try {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            & taskkill /F /IM ollama.exe /IM ollama_llama_server.exe /T 2>$null
            Start-Sleep -Seconds 3
            $still = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
            if (-not $still) { Log "  旧 Ollama 进程树已清理（回收僵尸显存）"; break }
            if ($attempt -eq 3) { Log "  WARN: Ollama 进程残留(疑似驱动层卡死僵尸,用户态无法杀) → 建议整机重启回收显存" }
        }
    } catch {}
    Log "  Ollama 重启中(应用新容量配置)..."
    try { [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $OLLAMA_MODELS, "User") } catch {}
    $env:OLLAMA_MODELS = $OLLAMA_MODELS
    $env:OLLAMA_CONTEXT_LENGTH = "4096"
    $env:OLLAMA_NUM_PARALLEL = "1"
    $env:OLLAMA_MAX_LOADED_MODELS = "1"
    $env:OLLAMA_KEEP_ALIVE = "24h"
    try { Start-Process -FilePath $OLLAMA_EXE -ArgumentList "serve" -WindowStyle Hidden -ErrorAction Stop } catch { Log "  启动 Ollama 失败: $_" }
    $ok = $false
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 1
        try { $rr = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 2; if ($rr.StatusCode -eq 200) { $ok = $true; break } } catch {}
    }
    if ($ok) { Log "  Ollama 启动结果: 成功" } else { Log "  Ollama 启动结果: 失败，请手动检查" }
}

# ★ 2026-08-19 治本：确保任务含「开机(Boot/AtStartup)触发器」。
#   根因：WanxiangOllama / WanxiangOllamaVision 此前只有 ONLOGON 触发器；
#   若整机重启未产生登录事件(无自动登录/锁屏恢复)，Ollama 不启动 → 后端连不上模型 → 决策退化。
#   后端 WanxiangAIBackend 因有 Boot 触发器故能自启，这里补齐两个 Ollama 任务，使其一致。
#   注意：Set-ScheduledTask 需提权；本函数失败仅记日志(非致命)，由用户在管理员 PowerShell 执行等价命令兜底。
function Ensure-BootTrigger($taskName) {
    try {
        $t = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $hasBoot = $t.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }
        if ($hasBoot) { Log "  $taskName 开机(Boot)触发器已存在" ; return }
        $boot = New-ScheduledTaskTrigger -AtStartup
        $triggers = @($t.Triggers) + $boot
        Set-ScheduledTask -TaskName $taskName -Trigger $triggers -ErrorAction Stop
        Log "  已为 $taskName 添加开机(Boot)触发器 ✓"
    } catch { Log "  确保 $taskName 开机(Boot)触发器失败(非致命,需管理员执行): $_" }
}

function Register-OllamaTask {
    $bat = "F:\WanxiangAI\backend\start_ollama.bat"
    $exists = $false
    try { $exists = [bool](schtasks /Query /TN $OLLAMA_TASK 2>$null) } catch {}
    if (-not $exists) {
        try { schtasks /Create /TN $OLLAMA_TASK /TR "`"$bat`"" /SC ONLOGON /RL HIGHEST /F 2>&1 | Out-Null; Log "  已注册 Ollama 登录自启任务" } catch { Log "  注册 Ollama 任务失败(非致命): $_" }
    } else { Log "  Ollama 自启任务已存在" }
    Ensure-BootTrigger $OLLAMA_TASK
}

$VISION_OLLAMA_TASK = "WanxiangOllamaVision"
function Register-VisionOllamaTask {
    $bat = "F:\WanxiangAI\backend\start_ollama_vision.bat"
    $exists = $false
    try { $exists = [bool](schtasks /Query /TN $VISION_OLLAMA_TASK 2>$null) } catch {}
    if (-not $exists) {
        try { schtasks /Create /TN $VISION_OLLAMA_TASK /TR "`"$bat`"" /SC ONLOGON /RL HIGHEST /F 2>&1 | Out-Null; Log "  已注册视觉 Ollama(11435/gpu1) 登录自启任务" } catch { Log "  注册视觉 Ollama 任务失败(非致命): $_" }
    } else { Log "  视觉 Ollama(11435/gpu1) 自启任务已存在" }
    Ensure-BootTrigger $VISION_OLLAMA_TASK
}

function Ensure-VisionOllama {
    Log "  检查视觉 Ollama(11435/gpu1)..."
    $running = $false
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:11435/api/tags" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $running = $true }
    } catch {}
    # ★ 2026-08-19 强制重启：旧进程树已在 Ensure-Ollama 阶段按名整树清理(含僵尸 llama_server)，
    #   此处不再杀端口进程(避免误杀刚启动的 11434 / 避免 Get-NetTCPConnection 卡死)，直接带新配置拉起 11435。
    Log "  视觉 Ollama(11435) 重启中(应用新容量配置)..."
    try { [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $OLLAMA_MODELS, "User") } catch {}
    $env:OLLAMA_MODELS = $OLLAMA_MODELS
    # ★ 2026-08-19 根治：原 Start-Process powershell → .ps1 链路在任务计划/重启脚本环境下
    #   serve 150s 不监听（实证 20:17:37/20:18:15 两次 spawn 全失败）；schtasks 直跑 python
    #   启动器 1s 就绪且 serve 脱离 Job 持久存活。此处改直跑 python 启动器（与 bat 一致）。
    try { Start-Process -FilePath "F:\WanxiangAI\.venv\Scripts\python.exe" -ArgumentList "`"F:\WanxiangAI\backend\scripts\start_ollama_vision.py`" --gpu 0" -WindowStyle Hidden -ErrorAction Stop } catch { Log "  启动视觉 Ollama 失败: $_" }
    $ok = $false
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 1
        try { $rr = Invoke-WebRequest -Uri "http://127.0.0.1:11435/api/tags" -UseBasicParsing -TimeoutSec 2; if ($rr.StatusCode -eq 200) { $ok = $true; break } } catch {}
    }
    if ($ok) { Log "  视觉 Ollama(11435/gpu1) 启动结果: 成功" } else { Log "  视觉 Ollama(11435) 启动结果: 失败，请手动检查 start_ollama_vision.ps1" }
}

function Port-Open($port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        return ($null -ne $c)
    } catch { return $false }
}

Log "===== restart_task_backend 开始 ====="

# ★ 2026-08-19 稳定性根治：锁主脑 num_ctx=4096（跳过显存探测自适应扩8192的KV爆炸）。
#   设为用户级持久环境变量 → schtasks 拉起的后端进程读得到，且重启后仍生效。
try { [Environment]::SetEnvironmentVariable("WX_LOCAL_LLM_NUM_CTX", "4096", "User") } catch {}
Log "  已锁定 WX_LOCAL_LLM_NUM_CTX=4096 (User)"

Log "[0/4] 确保 Ollama 本地模型服务常驻..."
Register-OllamaTask
Ensure-Ollama
# ★ 2026-08-14 双卡规划：视觉 Ollama 实例独立绑 gpu1(11435)
Register-VisionOllamaTask
Ensure-VisionOllama

# ★★ 2026-08-10 预平仓锁利：杀 python 树会级联强杀 MT5 终端(terminal64)，
#   MT5 客户端默认 auto-reverse 所有持仓 → 重启瞬间损失全部未实现浮盈
#   （实测 20:32:48 一次损失 $1030）。先调后端 /api/emergency/flatten 主动平仓锁利，
#   再杀进程，避免被 MT5 auto-reverse 掉浮盈。失败不阻塞重启（回退 auto-reverse）。
Log "[0.5/4] 重启前主动平仓锁利（防 MT5 auto-reverse 损失浮盈）..."
try {
    $prePy = "F:\WanxiangAI\.venv\Scripts\python.exe"
    $preScript = "F:\WanxiangAI\backend\pre_restart_flatten.py"
    if (Test-Path $prePy) {
        & $prePy $preScript 2>&1 | ForEach-Object { Log "    $_" }
    } else {
        Log "  python 环境缺失，跳过预平仓（回退 MT5 auto-reverse）"
    }
} catch { Log "  预平仓异常（继续，回退 auto-reverse）: $_" }

Log "[1/4] 终止旧后端进程（WanxiangAI 相关 python）..."
$procs = $null
try { $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue } catch { Log "  Get-CimInstance 失败: $_" }
$kill = @()
if ($procs) {
    $kill = @($procs | Where-Object {
        ($_.ExecutablePath -like "*WanxiangAI*") -or
        ($_.CommandLine -like "*supervisor.py*") -or
        ($_.CommandLine -like "*uvicorn*")
    })
}
if ($kill.Count -eq 0) { Log "  Get-CimInstance 未匹配到旧后端进程" }
else {
    foreach ($p in $kill) {
        Log "    终止 PID=$($p.ProcessId) $($p.Name)"
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { Log "    Stop-Process 失败: $_" }
        try { cmd /c "taskkill /T /F /PID $($p.ProcessId)" 2>$null | Out-Null } catch {}
    }
}
# Fallback：若 8080 仍被占，用 netstat 定位 PID 并强杀（专治 uvicorn 孤儿）
if (Port-Open 8080) {
    Log "  8080 仍被占用，尝试 netstat 定位并强杀..."
    try {
        $listeners = @(@(netstat -ano | findstr ":8080" | findstr "LISTEN") | Where-Object { $_ -ne '' })
        foreach ($line in $listeners) {
            $parts = $line -split '\s+'
            $pid8080 = $parts[$parts.Length - 1]
            if ($pid8080 -match '^\d+$') {
                Log "    taskkill /T /F /PID $pid8080"
                cmd /c "taskkill /T /F /PID $pid8080" 2>$null | Out-Null
            }
        }
    } catch { Log "  netstat fallback 失败: $_" }
}

Log "  等待 8080 端口释放（最多 30 秒）..."
$released = $false
for ($i = 0; $i -lt 30; $i++) {
    if (-not (Port-Open 8080)) { $released = $true; break }
    Start-Sleep -Seconds 1
}
Log "  8080 已释放: $released"

Log "[2/4] 重启任务 $TASK..."
try { Stop-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 2
try { Start-ScheduledTask -TaskName $TASK } catch { Log "  Start-ScheduledTask 失败: $_" }
Start-Sleep -Seconds 5

Log "[3/4] 等待后端就绪（最多 180 秒）..."
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}
if ($ready) { Log "[4/4] 后端已就绪 ✓" } else { Log "[4/4] 后端未在 180 秒内就绪，请检查 restart_log.txt" }

Log "===== 完成 ====="
Read-Host "按回车关闭窗口"
