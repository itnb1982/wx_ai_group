@echo off
chcp 65001 >nul 2>&1
title WanxiangAI Trading System Launcher
setlocal EnableDelayedExpansion

REM ============================================================
REM  启动器 —— 全部路径基于本脚本位置推导（%~dp0），不写死盘符。
REM  商业版要求：整个项目目录拷到任意电脑都能直接跑。
REM ============================================================
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%\backend"

echo ================================================
echo  WanxiangAI Gold Quant Trading System
echo  (Supervisor mode - auto-restart on crash)
echo ================================================
echo.

REM ---- 解释器发现：环境变量 -^> 项目 venv -^> PATH -^> py launcher ----
set "PYEXE="
if defined WX_PYTHON if exist "%WX_PYTHON%" set "PYEXE=%WX_PYTHON%"
if not defined PYEXE if exist "%ROOT%\.venv\Scripts\python.exe" set "PYEXE=%ROOT%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT%\venv\Scripts\python.exe" set "PYEXE=%ROOT%\venv\Scripts\python.exe"
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)

if not defined PYEXE (
    echo [ERROR] 未找到 Python 解释器。
    echo.
    echo 请先在项目根目录运行 bootstrap.bat 完成环境初始化，
    echo 或安装 Python 3.11+ 并勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo Using Python : %PYEXE%
echo Project root : %ROOT%
echo   URL: http://127.0.0.1:8080
echo   The supervisor keeps the system alive and
echo   auto-restarts it if the backend crashes.
echo   MT5 terminals are started automatically by workers.
echo.
echo You may close this window; the supervisor keeps
echo running in the background.
echo.

start "WanxiangAI-Supervisor" "%PYEXE%" supervisor.py

echo Launcher done. Check the supervisor window for status.
echo.
pause
endlocal
