@echo off
chcp 65001 >nul 2>&1
title WanxiangAI Backend Service Installer (Admin required)
cd /d F:\WanxiangAI\backend

echo ============================================
echo   WanxiangAI Backend -> Windows Service
echo   No window / Auto start / Auto restart
echo ============================================
echo.

REM 1) Install service via pywin32 framework
"F:\WanxiangAI\.venv\Scripts\python.exe" "F:\WanxiangAI\backend\windows_service.py" install
if errorlevel 1 (
    echo [ERROR] Service install failed. Make sure this window is Run as Administrator.
    pause
    exit /b 1
)

REM 2) Configure failure recovery and auto start
sc failure "WanxiangAIBackend" reset= 0 actions= restart/3000/restart/5000/restart/10000
sc config "WanxiangAIBackend" start= auto

REM 3) Kill any existing temp backend so the service can take port 8080
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:8080" ^| findstr LISTENING') do (
    taskkill /PID %%a /T /F >nul 2>&1
    echo [OK] Stopped temp backend PID=%%a
)
timeout /t 5 /nobreak >nul

REM 4) Start service
net start WanxiangAIBackend

REM 5) Remove old startup-folder entries to avoid double-start
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WanxiangAI_Start.bat" (
    del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WanxiangAI_Start.bat"
    echo [OK] Removed old startup bat
)
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WanxiangAI_Start.vbs" (
    del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WanxiangAI_Start.vbs"
    echo [OK] Removed old startup vbs
)

echo.
echo ============================================
echo   Done. Backend is now running as a service.
echo   Check services.msc -> WanxiangAIBackend
echo ============================================
pause
