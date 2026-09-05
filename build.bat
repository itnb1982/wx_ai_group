@echo off
chcp 65001 >nul
echo ============================================
echo   XAU/USD万象Ai自动量化交易系统 v1.0 — 一键构建脚本
echo   DeepSeek V4 + 混元 Hy3 双模型交易系统
echo ============================================
echo.

set PROJECT_DIR=%~dp0

echo [1/3] 安装后端依赖...
cd /d "%PROJECT_DIR%backend"
pip install -r requirements.txt >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 后端依赖安装失败
    pause
    exit /b 1
)
echo ✅ 后端依赖就绪

echo.
echo [2/3] 打包后端为 EXE...
cd /d "%PROJECT_DIR%backend"
pyinstaller --clean --noconfirm wanxiangai.spec
if %errorlevel% neq 0 (
    echo ❌ PyInstaller打包失败
    pause
    exit /b 1
)
echo ✅ EXE打包成功

echo.
echo [3/3] 生成安装包...
if not exist "%PROJECT_DIR%dist" mkdir "%PROJECT_DIR%dist"
if not exist "%PROJECT_DIR%dist\installer" mkdir "%PROJECT_DIR%dist\installer"

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    cd /d "%PROJECT_DIR%installer"
    %ISCC% setup.iss
    if %errorlevel% equ 0 (
        echo ✅ 安装包生成成功
    ) else (
        echo ⚠️ Inno Setup编译失败，请检查setup.iss
    )
) else (
    echo ⚠️ 未找到Inno Setup，跳过安装包生成
    echo   请安装Inno Setup 6: https://jrsoftware.org/isinfo.php
)

echo.
echo ============================================
echo   构建完成!
echo   EXE文件: %PROJECT_DIR%backend\dist\WanxiangAI.exe
echo   安装包:  %PROJECT_DIR%dist\installer\
echo   前端文件: %PROJECT_DIR%frontend\index.html
echo ============================================
echo.
echo 运行方式:
echo   1. 直接运行 EXE: backend\dist\WanxiangAI.exe
echo   2. 开发模式: cd backend ^&^& python -m uvicorn app.main:app --reload
echo   3. 浏览器访问: http://127.0.0.1:8080
echo ============================================
pause
