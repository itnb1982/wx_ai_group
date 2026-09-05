@echo off
chcp 65001 >nul 2>&1
setlocal

cd /d F:\WanxiangAI\backend

set "SERVICE_NAME=WanxiangAIBackend"
set "NSSM=F:\WanxiangAI\backend\nssm.exe"

echo [WanxiangAI] Stopping service %SERVICE_NAME%...
sc stop %SERVICE_NAME% >nul 2>&1
timeout /t 3 /nobreak >nul 2>&1

echo [WanxiangAI] Removing service %SERVICE_NAME%...
"%NSSM%" remove %SERVICE_NAME% confirm >nul 2>&1
sc delete %SERVICE_NAME% >nul 2>&1

echo [WanxiangAI] Service removed. Port 8080 should be free.
pause
