# 为 WanxiangOllama / WanxiangOllamaVision 补「开机(Boot/AtStartup)触发器」
# 与 WanxiangAIBackend 一致，使整机重启（无需登录）后主脑/视觉模型自动拉起。
# 仅修改任务计划触发器，不重启后端、不重启 Ollama、不平仓、不影响 MT5。
$ErrorActionPreference = "Stop"
$tasks = @("WanxiangOllama", "WanxiangOllamaVision")
foreach ($tn in $tasks) {
    try {
        $t = Get-ScheduledTask -TaskName $tn -ErrorAction Stop
        $hasBoot = $t.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }
        if ($hasBoot) {
            Write-Host "[$tn] 已有开机(Boot)触发器，跳过"
        } else {
            $boot = New-ScheduledTaskTrigger -AtStartup
            $triggers = @($t.Triggers) + $boot
            Set-ScheduledTask -TaskName $tn -Trigger $triggers -ErrorAction Stop
            Write-Host "[$tn] 已添加开机(Boot)触发器 OK" -ForegroundColor Green
        }
    } catch {
        Write-Host "[$tn] 失败（请以管理员身份运行）: $_" -ForegroundColor Red
    }
}
Write-Host "--- 验证（应均为 LogonTrigger, BootTrigger）---"
foreach ($tn in $tasks) {
    try {
        $t = Get-ScheduledTask -TaskName $tn
        $types = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ", "
        Write-Host "$tn -> [$types]"
    } catch { Write-Host "$tn -> 读取失败" }
}
