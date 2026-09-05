@echo off
chcp 65001 >nul 2>&1
echo [WanxiangAI] Running service repair and start script...
powershell -ExecutionPolicy Bypass -File "F:\WanxiangAI\backend\fix_and_start_service.ps1"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Script failed. Run this batch as Administrator.
    pause
    exit /b 1
)
echo.
echo [WanxiangAI] Done.
pause
