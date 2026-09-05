@echo off
chcp 65001 >nul
:: 自提权: 若未以管理员运行, 则请求 UAC 后以管理员重启本脚本
net session >nul 2>&1
if %errorLevel% == 0 (
  powershell -ExecutionPolicy Bypass -File "%~dp0restart_ollama_gpu.ps1"
) else (
  powershell -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
)
exit /b
