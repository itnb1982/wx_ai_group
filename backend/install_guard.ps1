# -*- coding: utf-8 -*-
<#
万象AI 后端进程守护 安装脚本（根治"后端无声死亡"）

双层自愈：
  1. 本任务计划：开机自启 + 失败重启（最多每1分钟重跑一次，连续失败也持续拉起）
  2. run_guard.py 内层看门狗：uvicorn 子进程崩了 1s 内重启

安装（管理员 PowerShell）：
  powershell -ExecutionPolicy Bypass -File install_guard.ps1

卸载：
  schtasks /Delete /TN "WanxiangAI_Backend_Guard" /F
#>
$ErrorActionPreference = "Stop"

$TASK_NAME = "WanxiangAI_Backend_Guard"

# 路径全部基于脚本自身位置推导，不写死盘符与用户名——
# 商业版整目录换机部署时，硬编码路径会让计划任务注册成功但永远启动失败。
$BACKEND = $PSScriptRoot
$ROOT = Split-Path -Parent $BACKEND
$GUARD = Join-Path $BACKEND "run_guard.py"

. (Join-Path $BACKEND "find_python.ps1")
$PY = Find-WxPython -ProjectRoot $ROOT
Write-Host "使用解释器: $PY"

# 先删除旧任务（若存在）
$old = schtasks /Query /TN $TASK_NAME 2>$null
if ($old) {
    Write-Host "检测到旧任务，先删除..."
    schtasks /Delete /TN $TASK_NAME /F | Out-Null
}

# 构造动作：python run_guard.py
$action = New-ScheduledTaskAction `
    -Execute $PY `
    -Argument "-u `"$GUARD`"" `
    -WorkingDirectory $BACKEND

# 触发器：登录时 + 每1分钟重跑（崩溃后最多1分钟自愈）
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration ([TimeSpan]::MaxValue))
)

# 设置：以当前用户运行、最高权限、不限于电源、失败重启
# 注意：PowerShell 的反引号续行符后面必须紧跟换行。
# 曾在此处写成 "`   # 注释" —— 反引号转义了空格而非换行，整段直接语法错误，
# 导致这个安装脚本长期无法执行。续行符后一律不得跟任何字符。
# ExecutionTimeLimit=Zero 表示不限运行时间（守护进程需常驻）。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# 注册任务
Register-ScheduledTask `
    -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "万象AI 后端守护：开机自启 + 崩溃自愈（uvicorn 挂了1分钟内自动拉起）" `
    -Force | Out-Null

# 立即运行一次
schtasks /Run /TN $TASK_NAME | Out-Null
Write-Host "✅ 守护任务 [$TASK_NAME] 已安装并立即启动"
Write-Host "   - 开机自启：已设置（AtLogOn）"
Write-Host "   - 崩溃自愈：每1分钟检测，失败自动重启"
Write-Host "   - 内层看门狗：uvicorn 子进程崩溃 1s 内重启"
Write-Host ""
Write-Host "查看状态：schtasks /Query /TN $TASK_NAME"
Write-Host "停止任务：schtasks /End /TN $TASK_NAME"
Write-Host "卸载任务：schtasks /Delete /TN $TASK_NAME /F"
