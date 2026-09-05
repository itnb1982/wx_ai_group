@echo off
chcp 65001 >nul 2>&1
title 万象Ai 智能交易系统 - 环境初始化
setlocal EnableDelayedExpansion

REM ============================================================
REM  一键环境初始化（新机器部署第一步）
REM
REM  做五件事：
REM    1. 找到宿主机的 Python，校验版本
REM    2. 在项目内创建 .venv 虚拟环境
REM    3. 安装后端依赖
REM    4. 若有 Node 则安装前端依赖并构建
REM    5. 系统初始化：生成 .env / 建库 / 创建管理员账号
REM
REM  第 5 步为什么不能省：
REM    .env 不随交付包分发（含密钥，必须现场生成），而后端启动时
REM    SECRET_KEY 为空会直接拒绝启动；并且新库里一个账号都没有，
REM    没有第 5 步，装完依赖也只是"起不来、登不进"。
REM
REM  为什么 .venv 不能随项目一起拷贝：
REM    Windows 上 .venv\Scripts\python.exe 只是个转发器，真正的标准库和 DLL
REM    仍在创建它的那台机器的 Python 安装目录里（记录在 pyvenv.cfg 的 home= 字段）。
REM    直接拷贝虚拟环境到别的机器，它会去找一个不存在的路径然后失败。
REM    因此交付包只带源码，虚拟环境由本脚本在目标机现场生成。
REM ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

echo ============================================================
echo   万象Ai 智能交易系统 — 环境初始化
echo   项目目录: %ROOT%
echo ============================================================
echo.

REM ---------- 1. 找 Python ----------
set "PYEXE="
if defined WX_PYTHON if exist "%WX_PYTHON%" set "PYEXE=%WX_PYTHON%"
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if not defined PYEXE set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    echo [错误] 未检测到 Python。
    echo.
    echo 请先安装 Python 3.11 或更高版本：https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo [1/5] 检测到 Python: %PYEXE%
"%PYEXE%" -c "import sys;assert sys.version_info>=(3,11),'低于 3.11';print('      版本:',sys.version.split()[0])" || (
    echo [错误] Python 版本过低，需要 3.11 或更高。
    pause
    exit /b 1
)
echo.

REM ---------- 2. 创建虚拟环境 ----------
if exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [2/5] 虚拟环境已存在，跳过创建。
) else (
    echo [2/5] 创建虚拟环境 .venv ...
    "%PYEXE%" -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败。
        pause
        exit /b 1
    )
)
set "VPY=%ROOT%\.venv\Scripts\python.exe"
echo.

REM ---------- 3. 后端依赖 ----------
echo [3/5] 安装后端依赖（首次较慢，请耐心等待）...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r "%ROOT%\backend\requirements.txt"
if errorlevel 1 (
    echo [错误] 后端依赖安装失败，请检查网络连接。
    pause
    exit /b 1
)
echo       后端依赖安装完成。
echo.

REM ---------- 3.5 本地时序模型依赖（可选，重型） ----------
REM  说明：torch(CUDA) 解包后约 2.5~3GB。缺失时系统自动降级为
REM  SMC/Regime + 规则方向终审，交易不受影响，只是少了本地时序增强。
REM  跳过方式：设置环境变量 WX_SKIP_ML=1 后再运行本脚本。
if "%WX_SKIP_ML%"=="1" (
    echo [3.5/5] 已通过 WX_SKIP_ML=1 跳过本地时序模型依赖。
    goto :skip_ml
)
if not exist "%ROOT%\models\chronos-2\config.json" (
    echo [3.5/5] 未检测到 models\chronos-2 权重，跳过时序模型依赖安装。
    goto :skip_ml
)
echo [3.5/5] 安装本地时序模型依赖（约 2.5GB，耗时较长）...
echo         如需跳过，请 Ctrl+C 终止后设置 WX_SKIP_ML=1 重跑。
"%VPY%" -m pip install -r "%ROOT%\backend\requirements-ml.txt"
if errorlevel 1 (
    echo       [警告] 时序模型依赖安装失败 —— 不影响核心交易功能，
    echo              系统将自动降级为 SMC/Regime + 规则方向终审。
) else (
    echo       时序模型依赖安装完成。
)
:skip_ml
echo.

REM ---------- 4. 前端依赖与构建 ----------
echo [4/5] 检查前端构建环境...
set "NODEEXE="
if defined WX_NODE if exist "%WX_NODE%" set "NODEEXE=%WX_NODE%"
if not defined NODEEXE (
    for /f "delims=" %%P in ('where node 2^>nul') do (
        if not defined NODEEXE set "NODEEXE=%%P"
    )
)

if not defined NODEEXE (
    echo       [跳过] 未检测到 Node.js。
    echo       项目已附带预构建的前端产物 frontend\dist，可直接运行。
    echo       仅当需要修改前端源码时才需安装 Node 18+。
) else (
    echo       检测到 Node: %NODEEXE%
    if exist "%ROOT%\frontend\dist\index.html" (
        echo       已存在前端构建产物，跳过重新构建。
        echo       如需重新构建请运行: python frontend\deploy.py
    ) else (
        echo       安装前端依赖并构建...
        pushd "%ROOT%\frontend"
        call npm install
        popd
        "%VPY%" "%ROOT%\frontend\deploy.py"
    )
)
echo.

REM ---------- 5. 系统初始化（.env / 建库 / 管理员账号） ----------
echo [5/5] 系统初始化...
"%VPY%" "%ROOT%\backend\scripts\init_deployment.py"
if errorlevel 1 (
    echo.
    echo [错误] 系统初始化失败，请查看上方提示。
    echo        修复后可单独重跑本步骤（脚本可重复执行，不会覆盖已有数据）:
    echo          .venv\Scripts\python.exe backend\scripts\init_deployment.py
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   全部完成
echo ============================================================
echo.
echo   启动系统:  start_all.bat
echo   访问地址:  http://127.0.0.1:8080
echo.
echo   首次登录的邮箱与密码见上方输出；
echo   随机生成的密码同时写入了项目根目录「首次登录凭据.txt」。
echo.
echo   可选增强（本地 AI 模型，不装也能正常交易）:
echo     1. 安装 Ollama:  https://ollama.com/download
echo     2. 拉取模型:     ollama pull qwen3:8b
echo.
pause
endlocal
