@echo off
chcp 65001 >nul
echo ========================================
echo  万象Ai 后端服务重启
echo ========================================
echo.

echo [1/3] 停止旧服务 (PID 11640)...
taskkill /F /PID 11640 2>nul
if %errorlevel% neq 0 (
    echo ! 无法终止进程，可能需要管理员权限
    echo    请右键 VS Code → "以管理员身份运行" 后重试
    pause
    exit /b 1
)
echo ✓ 旧服务已停止

echo.
echo [2/3] 等待 3 秒...
timeout /t 3 /nobreak >nul

echo.
echo [3/3] 启动新服务...
cd /d "%~dp0"
start "" "supervisor_uvicorn.bat"

echo ✓ 服务正在启动...
echo.
timeout /t 5 /nobreak >nul

echo 检查服务状态...
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo ✓ 后端服务已成功启动！
    echo.
    echo 访问地址: http://127.0.0.1:8080
) else (
    echo ! 服务可能启动失败，请查看日志
)

echo.
echo ========================================
echo  完成！
echo ========================================
pause
