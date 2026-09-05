@echo off
chcp 65001 >nul
title Start Vision Ollama (gpu0=8GB Ti) for WanxiangAI
REM ★ 2026-08-19 根治"任务计划拉起视觉失败"：绕过 .ps1 链路，直跑 Python 启动器。
REM   实证：.ps1(bat→powershell→python) 链路下 serve 150s 不监听；schtasks 直跑
REM   python 启动器 1s 即就绪(serve 脱离 Job 持久存活)。bat 由任务计划 Action 调用，
REM   改为直跑后任务计划拉起即走成功链路。
"F:\WanxiangAI\.venv\Scripts\python.exe" "%~dp0scripts\start_ollama_vision.py" --gpu 0
