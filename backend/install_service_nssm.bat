@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

cd /d F:\WanxiangAI\backend

REM Paths
set "SERVICE_NAME=WanxiangAIBackend"
set "NSSM=F:\WanxiangAI\backend\nssm.exe"
set "PYTHON=F:\WanxiangAI\.venv\Scripts\python.exe"
set "SUPERVISOR=F:\WanxiangAI\backend\supervisor.py"
set "LOG_DIR=F:\WanxiangAI\backend\logs"
set "LOCK_FILE=F:\WanxiangAI\backend\.supervisor.lock"

echo [WanxiangAI] Installing backend as Windows Service via nssm...
echo [WanxiangAI] Service name: %SERVICE_NAME%

REM Make sure log directory exists
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Stop and remove old service if exists
echo [WanxiangAI] Removing old service (if any)...
sc stop %SERVICE_NAME% >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1
sc delete %SERVICE_NAME% >nul 2>&1
if %ERRORLEVEL%==0 (
    echo [WanxiangAI] Old service removed.
) else (
    echo [WanxiangAI] No old service found or already removed.
)

REM Kill any leftover backend python processes (including launch_supervisor / supervisor / uvicorn)
echo [WanxiangAI] Killing leftover backend python processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and ($_.CommandLine -like '*WanxiangAI*' -or $_.CommandLine -like '*supervisor.py*' -or $_.CommandLine -like '*launch_supervisor*' -or $_.CommandLine -like '*uvicorn*') } | ForEach-Object { Write-Host '  Killing PID' $_.ProcessId; try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"
timeout /t 3 /nobreak >nul 2>&1

REM Remove stale supervisor lock
echo [WanxiangAI] Removing stale lock file...
if exist "%LOCK_FILE%" del /F "%LOCK_FILE%" >nul 2>&1

REM Release port 8080
echo [WanxiangAI] Releasing port 8080...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:8080" ^| findstr "LISTENING"') do (
    echo [WanxiangAI] Killing PID %%a
    taskkill /PID %%a /T /F >nul 2>&1
)
timeout /t 3 /nobreak >nul 2>&1

REM Install new service with nssm
echo [WanxiangAI] Installing service with nssm...
"%NSSM%" install %SERVICE_NAME% "%PYTHON%" "%SUPERVISOR%"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] nssm install failed. Please run this batch as Administrator.
    pause
    exit /b 1
)

REM Configure service
"%NSSM%" set %SERVICE_NAME% DisplayName "WanxiangAI Trading Backend"
"%NSSM%" set %SERVICE_NAME% Description "WanxiangAI XAUUSD AI trading system backend (Supervisor + uvicorn + MT5 workers)"
"%NSSM%" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%NSSM%" set %SERVICE_NAME% AppDirectory F:\WanxiangAI\backend
"%NSSM%" set %SERVICE_NAME% AppStdout "%LOG_DIR%\nssm_stdout.log"
"%NSSM%" set %SERVICE_NAME% AppStderr "%LOG_DIR%\nssm_stderr.log"
"%NSSM%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM%" set %SERVICE_NAME% AppRotateBytes 52428800
"%NSSM%" set %SERVICE_NAME% AppEnvironmentExtra "PYTHONPATH=F:\WanxiangAI\backend"

REM Configure failure recovery: restart on crash
sc failure %SERVICE_NAME% reset= 0 actions= restart/3000/restart/5000/restart/10000

echo [WanxiangAI] Starting service...
sc start %SERVICE_NAME%
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Failed to start service. Check logs in %LOG_DIR%\nssm_stderr.log
    pause
    exit /b 1
)

echo.
echo [WanxiangAI] Done. Backend is now running as a Windows Service.
echo [WanxiangAI] Verify with: sc query %SERVICE_NAME%
echo [WanxiangAI] Remove old startup shortcuts manually if any.
echo.
pause
