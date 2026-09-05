@echo off
rem ============================================================
rem  WanxiangAI backend service restart (auto UAC elevation)
rem
rem  Pure ASCII on purpose:
rem    .bat must NOT carry a BOM, and non-ASCII text here gets
rem    mangled by the console code page. All Chinese output
rem    lives in restart_service_admin.ps1 (UTF-8 with BOM).
rem
rem  What the .ps1 does:
rem    1) verify admin      2) stop service and wait for STOPPED
rem    3) kill leftover terminal64.exe / port 8080 squatters
rem    4) start service     5) poll /api/health until ready
rem ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0restart_service_admin.ps1' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_service_admin.ps1"
