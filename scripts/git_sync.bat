@echo off
chcp 65001 >nul
echo ========================================
echo   万象Ai - Git 代码同步脚本
echo ========================================
echo.

cd /d "%~dp0.."

echo [1/3] 检查 Git 状态...
git status --short > nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 当前目录不是 Git 仓库！
    pause
    exit /b 1
)

echo [2/3] 拉取远程最新代码...
git pull origin master
if %errorlevel% neq 0 (
    echo [警告] 拉取代码失败，可能是网络问题或本地有未提交的更改
    echo        请检查网络连接或手动处理冲突
    pause
    exit /b 1
)

echo [3/3] 同步完成！
echo.
echo ========================================
echo   代码已同步到最新版本
echo ========================================
pause
