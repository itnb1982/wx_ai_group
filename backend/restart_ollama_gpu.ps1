# 重启 Ollama 双实例(以管理员权限运行)
# 用途: 清掉因进程分离而残留的高权限 ollama 孤儿(可能跑在 CPU), 并以新 CUDA 配置重启
#   - 主脑 WanxiangOllama  -> CUDA_VISIBLE_DEVICES=1 = 12GB RTX 3060 (qwen3:8b)
#   - 视觉 WanxiangOllamaVision -> CUDA_VISIBLE_DEVICES=0 = 8GB RTX 3060 Ti (qwen2.5vl:7b)
# 不动 MT5、不动后端、不平仓。
$ErrorActionPreference = 'Continue'

Write-Host "=== [1/3] 整树强杀所有 ollama 进程(清孤儿 CPU 实例) ==="
taskkill.exe /F /IM ollama.exe /IM ollama_llama_server.exe /T 2>&1 | Out-Host
Start-Sleep -Seconds 4

Write-Host ""
Write-Host "=== [2/3] 先结束任务, 等旧实例释放端口, 再以新配置拉起 ==="
schtasks.exe /End /TN "WanxiangOllama" 2>&1 | Out-Host
schtasks.exe /End /TN "WanxiangOllamaVision" 2>&1 | Out-Host
Write-Host "  等待 8s 让旧实例彻底释放 11434/11435..."
Start-Sleep -Seconds 8

# 若还有分离残留占用端口, 再按端口整树杀一次(覆盖高权限孤儿)
function Kill-PortListener([int]$port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        $pid = $conn.OwningProcess
        if ($pid) {
            Write-Host "  端口 $port 仍被 PID=$pid 占用, 整树强杀..."
            taskkill.exe /F /T /PID $pid 2>&1 | Out-Host
        }
    } catch {}
}
Kill-PortListener 11434
Kill-PortListener 11435
Start-Sleep -Seconds 3

schtasks.exe /Run /TN "WanxiangOllama" 2>&1 | Out-Host
schtasks.exe /Run /TN "WanxiangOllamaVision" 2>&1 | Out-Host

Write-Host ""
Write-Host "=== [3/3] 等待实例绑定端口(20s) ==="
Start-Sleep -Seconds 20
netstat.exe -ano 2>$null | Select-String ":11434|:11435" | Where-Object { $_ -match "LISTENING" } | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "完成。下一步请在 WorkBuddy 会话里让 AI 触发一次模型预热并验证 ollama ps = 100% GPU。"
Write-Host "预期: 主脑 qwen3:8b 在 12GB 卡(CUDA1), 视觉 qwen2.5vl:7b 在 8GB Ti(CUDA0)。"
