@echo off
chcp 65001 >nul
title WanxiangAI Backend Restart
:: Check admin rights; self-elevate via UAC if not
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -Command "Start-Process '%~f0' -Verb runAs"
    exit /b
)
echo Restarting backend to load latest fixes. Please wait...
echo.
powershell -ExecutionPolicy Bypass -File "%~dp0restart_task_backend.ps1"
echo.
echo ===== Script finished =====
echo If the management page is still abnormal, send backend/restart_log.txt to me.
echo.
pause
