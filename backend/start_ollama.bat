@echo off
chcp 65001 >nul
title Start Ollama for WanxiangAI
powershell -ExecutionPolicy Bypass -File "%~dp0start_ollama.ps1" -Force
