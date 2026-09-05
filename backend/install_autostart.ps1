# -*- coding: utf-8 -*-
<#
万象AI 后端进程守护 —— 开机自启安装（supervisor 版）
让机器重启后交易系统自动恢复：AtStartup(开机无需登录) + AtLogOn(登录保底)。
自愈链：本任务(失败自动重启) -> supervisor.py(看门狗) -> uvicorn -> mt5_worker。
单实例守卫在 supervisor.py 内（端口占用则退出），故双重触发不会双开抢端口。
安装（建议管理员，否则自动降级为登录时自启）：
  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
卸载：schtasks /Delete /TN "WanxiangAI_Supervisor_AutoStart" /F
#>
$ErrorActionPreference = "Stop"

$TASK_NAME = "WanxiangAI_Supervisor_AutoStart"

# 路径基于脚本自身位置推导，见 install_guard.ps1 中的同款说明。
$BACKEND = $PSScriptRoot
$ROOT = Split-Path -Parent $BACKEND
$SUP = Join-Path $BACKEND "supervisor.py"

. (Join-Path $BACKEND "find_python.ps1")
$PY = Find-WxPython -ProjectRoot $ROOT
Write-Host "使用解释器: $PY"

# 删除同名旧任务（若存在）
$old = schtasks /Query /TN $TASK_NAME 2>$null
if ($old) {
    Write-Host "检测到旧任务，先删除..."
    schtasks /Delete /TN $TASK_NAME /F | Out-Null
}

$action = New-ScheduledTaskAction -Execute $PY -Argument "-u `"$SUP`"" -WorkingDirectory $BACKEND
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew

# 先尝试"开机自启 + 无需登录"（需管理员；非管理员会抛错并降级）
$registered = $false
try {
    $t1 = New-ScheduledTaskTrigger -AtStartup
    $t2 = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger @($t1, $t2) -Settings $settings -Description "万象AI 后端守护：开机自启(无需登录)+崩溃自愈" -Force | Out-Null
    Write-Host "OK: 任务已注册 = 开机自启(无需登录) + 登录时保底"
    $registered = $true
} catch {
    Write-Host "WARN: 完整注册失败（可能非管理员）: $($_.Exception.Message)"
    Write-Host "      回退为仅『登录时自启』（无需管理员）..."
    try {
        $t = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger @($t) -Settings $settings -Description "万象AI 后端守护：登录时自启+崩溃自愈" -Force | Out-Null
        Write-Host "OK: 任务已注册 = 登录时自启（开机无登录自启需管理员重跑本脚本）"
        $registered = $true
    } catch {
        Write-Host "ERROR: 注册失败: $($_.Exception.Message)"
    }
}

if ($registered) {
    Write-Host ""
    Write-Host "查看: schtasks /Query /TN $TASK_NAME"
    Write-Host "停止: schtasks /End /TN $TASK_NAME"
    Write-Host "卸载: schtasks /Delete /TN $TASK_NAME /F"
}
