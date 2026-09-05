@echo off
chcp 65001 >nul
echo ========================================
echo   以管理员身份重启后端服务
echo ========================================
echo.

echo 正在启动 PowerShell（管理员权限）...
powershell -ExecutionPolicy Bypass -File "%~dp0restart_backend.ps1"

echo.
echo ========================================
echo   重启完成！
echo   请访问 http://127.0.0.1:8080
echo ========================================
pause
