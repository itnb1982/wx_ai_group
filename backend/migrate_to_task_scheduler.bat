@echo off
chcp 65001 >nul
title 万象Ai 后端运行身份迁移
echo ============================================
echo  正在迁移后端运行身份：服务(LocalSystem) -> 任务计划程序(当前用户会话)
echo  目的：让 MT5 终端在用户会话中启动，彻底避开 Session 0 / systemprofile 限制
echo ============================================
pause
echo 请求管理员权限...
PowerShell -ExecutionPolicy Bypass -File "%~dp0migrate_to_task_scheduler.ps1"
