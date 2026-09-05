@echo off
chcp 65001 >nul
echo ========================================
echo   万象Ai - Git 代码版本管理
echo ========================================
echo.

cd /d "%~dp0.."

echo [1/5] 检查 Git 状态...
git status --short > nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 当前目录不是 Git 仓库！
    pause
    exit /b 1
)

echo.
echo [2/5] 拉取远程最新代码...
git pull origin master
if %errorlevel% neq 0 (
    echo [警告] 拉取代码失败，可能是网络问题或本地有未提交的更改
    echo        请检查网络连接或手动处理冲突
    pause
    exit /b 1
)

echo.
echo [3/5] 检查是否有未提交的更改...
git status --short > temp_status.tmp
if %errorlevel% equ 0 (
    findstr /C:" M" temp_status.tmp > nul
    if %errorlevel% equ 0 (
        echo [提示] 发现未提交的更改，正在暂存...
        git stash save "自动暂存于 %date% %time%" > nul 2>&1
        echo [成功] 已暂存更改
    ) else (
        echo [信息] 无未提交的更改
    )
)
del temp_status.tmp > nul 2>&1

echo.
echo [4/5] 确认当前版本...
git log --oneline -1

echo.
echo [5/5] 同步完成！
echo.
echo ========================================
echo   代码已同步到最新版本
echo ========================================
echo.
echo 提示：如需提交代码，请使用 Git 管理脚本或手动操作
pause
