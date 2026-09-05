#requires -RunAsAdministrator
<#
  万象Ai 后端运行身份迁移脚本（2026-08-09）
  ──────────────────────────────────────────────
  背景：以 Windows 服务 + LocalSystem 身份运行后端时，MT5 终端被启动在
  Session 0（非交互式会话）。MT5 内置的 Edge WebView 会尝试往
  C:\Windows\System32\config\systemprofile\AppData\Roaming\MetaQuotes\Terminal\...
  写数据，受 Session 0 权限/隔离限制，弹窗报错并拖死/失败，导致 2/3/4 号
  账号 90s 超时、IPC send failed。

  方案：把后端从 Windows 服务迁移为「任务计划程序」任务，在用户登录会话
  （当前登录用户 15588）中运行。MT5 终端随之在用户会话启动，数据目录落在
  C:\Users\15588\AppData\Roaming\MetaQuotes\Terminal，与日常桌面 MT5 一致，
  彻底避开 Session 0 / systemprofile 问题。

  注意：
  1. 本脚本必须以管理员运行。
  2. 迁移后后端随当前用户登录自动启动；用户注销/重启未登录期间不运行。
  3. 迁移前会自动备份原服务配置到 F:\WanxiangAI\data\svc_backup.txt。
#>

$ErrorActionPreference = "Stop"
$SVC = "WanxiangAIBackend"
$PY = "F:\WanxiangAI\.venv\Scripts\python.exe"
$SUP = "F:\WanxiangAI\backend\supervisor.py"
$WD = "F:\WanxiangAI\backend"
$USER = "15588"
$TASK = "WanxiangAIBackend"

function Wait-ForHealth {
    param([int]$MaxSec = 180)
    $deadline = (Get-Date).AddSeconds($MaxSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/health" -UseBasicParsing -TimeoutSec 8
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 3
    }
    return $false
}

function Show-Step {
    param([string]$msg)
    Write-Host "`n[$(Get-Date -Format HH:mm:ss)] $msg" -ForegroundColor Cyan
}

Show-Step "步骤 1/7：确认管理员权限"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "必须以管理员运行本脚本！" -ForegroundColor Red
    exit 1
}

Show-Step "步骤 2/7：备份原服务配置"
$svcInfo = sc.exe qc $SVC 2>&1
$svcInfo | Out-File -FilePath "F:\WanxiangAI\data\svc_backup.txt" -Encoding utf8
Write-Host "已备份到 F:\WanxiangAI\data\svc_backup.txt" -ForegroundColor Green

Show-Step "步骤 3/7：停止并删除 Windows 服务 $SVC"
sc.exe stop $SVC | Out-Null
Start-Sleep -Seconds 3
$max = 30
while ((sc.exe query $SVC | Select-String "RUNNING") -and $max -gt 0) {
    Start-Sleep -Seconds 2
    $max--
}
sc.exe delete $SVC | Out-Null
Start-Sleep -Seconds 2
Write-Host "服务已删除" -ForegroundColor Green

Show-Step "步骤 4/7：清理残留 terminal64.exe / python 进程"
Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "*WanxiangAI*" -or $_.CommandLine -like "*supervisor.py*"
} | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "残留进程已清理" -ForegroundColor Green

Show-Step "步骤 5/7：创建任务计划程序任务（用户登录时启动）"
$action = New-ScheduledTaskAction -Execute $PY -Argument $SUP -WorkingDirectory $WD
# 触发器：用户登录时 + 系统启动时（如果用户已登录则启动）
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $USER
$triggerBoot = New-ScheduledTaskTrigger -AtStartup
# 设置：最高权限、隐藏窗口、不终止空闲、失败重启
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable:$false -ExecutionTimeLimit (New-TimeSpan -Days 365)
# 主体：以当前用户运行，并勾选"使用最高权限运行"
$principalObj = New-ScheduledTaskPrincipal -UserId $USER -LogonType Interactive -RunLevel Highest
# 如果任务已存在则删除
Unregister-ScheduledTask -TaskName $TASK -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TASK -Action $action -Trigger $triggerLogon,$triggerBoot `
    -Settings $settings -Principal $principalObj -Description "万象Ai 后端守护进程（用户会话运行，避开 Session 0）" | Out-Null
Write-Host "任务 $TASK 已创建" -ForegroundColor Green

Show-Step "步骤 6/7：立即启动任务"
Start-ScheduledTask -TaskName $TASK
Write-Host "任务已启动" -ForegroundColor Green

Show-Step "步骤 7/7：等待后端就绪（最多 180 秒）"
if (Wait-ForHealth -MaxSec 180) {
    Write-Host "`n==============================================" -ForegroundColor Green
    Write-Host "  迁移成功，后端已就绪" -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "`n后端现在以当前用户 ($USER) 会话运行。" -ForegroundColor Gray
    Write-Host "MT5 终端将随后端在用户会话中启动，不再受 Session 0 限制。" -ForegroundColor Gray
    Write-Host "请回到对话，让助手执行 verify_fixes.py 验收。" -ForegroundColor Gray
} else {
    Write-Host "`n==============================================" -ForegroundColor Red
    Write-Host "  后端未在 180 秒内就绪，请把本窗口内容告知助手" -ForegroundColor Red
    Write-Host "==============================================" -ForegroundColor Red
    Get-ScheduledTaskInfo -TaskName $TASK | Out-String | Write-Host
}

Write-Host "`n按回车关闭窗口..." -ForegroundColor Gray
[void][System.Console]::ReadLine()
