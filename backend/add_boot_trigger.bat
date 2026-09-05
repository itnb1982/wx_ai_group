@echo off
chcp 65001 >nul
title 补齐 Ollama 开机自启触发器（治本：整机重启后自动拉起模型）
REM 自检管理员权限，不足则自提权(UAC)
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 需要管理员权限，正在请求 UAC 提权...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -ExecutionPolicy Bypass -File "%~dp0add_boot_trigger.ps1"
pause
