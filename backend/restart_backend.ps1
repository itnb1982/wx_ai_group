# 以管理员身份重启后端服务
$pid = Get-Process -Id 11640 -ErrorAction SilentlyContinue

if ($pid) {
    Write-Host "正在终止后端进程 (PID: 11640)..."
    Stop-Process -Id 11640 -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    
    # 确认进程已终止
    $pid = Get-Process -Id 11640 -ErrorAction SilentlyContinue
    if ($pid) {
        Write-Host "进程仍在运行，尝试强制终止..."
        taskkill /F /PID 11640
        Start-Sleep -Seconds 2
    }
}

Write-Host "正在启动后端服务..."
cd F:\WanxiangAI\backend
.\start_backend.bat

Write-Host "后端服务已启动，等待 5 秒后测试..."
Start-Sleep -Seconds 5

# 测试后端
$response = Invoke-WebRequest -Uri "http://127.0.0.1:8080/" -Method GET -UseBasicParsing
Write-Host "后端响应状态: $($response.StatusCode)"
Write-Host "响应长度: $($response.Content.Length) 字符"
