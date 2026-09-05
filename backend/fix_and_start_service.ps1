#Requires -RunAsAdministrator
<#
修复 nssm 服务启动冲突：停止服务 -> 清理残留 supervisor/uvicorn -> 删除锁 -> 启动服务
#>
$ErrorActionPreference = "Continue"

$svc = "WanxiangAIBackend"
$lock = "F:\WanxiangAI\backend\.supervisor.lock"
$nssm = "F:\WanxiangAI\backend\nssm.exe"

Write-Host "[WanxiangAI] Stopping service $svc ..."
& sc.exe stop $svc 2>&1 | Out-Null
Start-Sleep -Seconds 3

Write-Host "[WanxiangAI] Killing leftover WanxiangAI python processes ..."
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and (
        $_.CommandLine -like '*WanxiangAI*' -or
        $_.CommandLine -like '*launch_supervisor*' -or
        $_.CommandLine -like '*uvicorn*' -or
        $_.CommandLine -like '*supervisor*'
    )
}
foreach ($p in $procs) {
    Write-Host "  Killing PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(80, $p.CommandLine.Length)))"
    & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null
}
Start-Sleep -Seconds 3

if (Test-Path $lock) {
    Write-Host "[WanxiangAI] Removing stale lock file $lock"
    Remove-Item $lock -Force
}

Write-Host "[WanxiangAI] Checking port 8080 ..."
$portUser = netstat -ano 2>$null | findstr "127.0.0.1:8080" | findstr "LISTENING"
if ($portUser) {
    Write-Host "  Port still occupied: $portUser"
    $portUser -split '\s+' | Where-Object { $_ -match '^\d+$' } | ForEach-Object {
        Write-Host "  Killing PID $_"
        & taskkill /PID $_ /T /F 2>&1 | Out-Null
    }
    Start-Sleep -Seconds 2
}

Write-Host "[WanxiangAI] Starting service $svc ..."
& sc.exe start $svc 2>&1 | Out-Null

Write-Host "[WanxiangAI] Waiting 45 seconds for uvicorn cold start ..."
for ($i = 0; $i -lt 9; $i++) {
    Start-Sleep -Seconds 5
    $state = (& sc.exe query $svc 2>&1 | Select-String "STATE") -replace '\s+', ' '
    Write-Host "  [$($i*5)s] $state"
}

Write-Host ""
Write-Host "[WanxiangAI] Final service state:"
& sc.exe query $svc 2>&1 | Select-String -Pattern "SERVICE_NAME|STATE|PID" | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "[WanxiangAI] Health check:"
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/health" -TimeoutSec 5
    Write-Host "  OK: $($r | ConvertTo-Json -Compress)"
} catch {
    Write-Host "  FAILED: $_"
}
