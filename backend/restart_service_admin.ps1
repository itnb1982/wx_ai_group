$ErrorActionPreference = "Continue"
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$SVC = "WanxiangAIBackend"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  万象Ai 后端服务重启（加载本轮修复代码）" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# ── 0. 确认管理员 ──
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[错误] 未以管理员身份运行。请右键选择「以管理员身份运行」。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
Write-Host "[1/5] 管理员权限确认 OK" -ForegroundColor Green

# ── 0.5 预平仓锁利（★ 2026-08-10 新增）──
#   杀 MT5 终端(terminal64) 时 MT5 默认 auto-reverse 全部持仓 → 重启损失未实现浮盈
#   （实测一次 $1030）。先调后端 /api/emergency/flatten 主动平仓，再杀终端。
Write-Host "[1.5/5] 重启前主动平仓锁利（防 MT5 auto-reverse 损失浮盈）..." -ForegroundColor Yellow
try {
    $prePy = "F:\WanxiangAI\.venv\Scripts\python.exe"
    $preScript = "F:\WanxiangAI\backend\pre_restart_flatten.py"
    if (Test-Path $prePy) {
        & $prePy $preScript 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
    } else {
        Write-Host "      python 环境缺失，跳过预平仓（回退 MT5 auto-reverse）" -ForegroundColor DarkYellow
    }
} catch {
    Write-Host "      预平仓异常（继续，回退 auto-reverse）: $_" -ForegroundColor DarkYellow
}

# ── 1. 停服务 ──
Write-Host "[2/5] 正在停止服务 $SVC ..." -ForegroundColor Yellow
sc.exe stop $SVC | Out-Null
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    # ★ 2026-08-11 修复 PS 5.1/7 兼容：sc.exe query 可能返回无 STATE 行（服务 STOPPING/未注册），
    #   原 .ToString() 在 Select-String 返回 $null 时报 InvokeMethodOnNull（截图红字根因）。
    try {
        $raw = sc.exe query $SVC 2>&1
        $STATE_LINE = ($raw | Select-String "STATE" | Select-Object -First 1)
        if ($null -ne $STATE_LINE -and $STATE_LINE.ToString() -match "STOPPED") { break }
        if ($LASTEXITCODE -ne 0) { break }   # 服务名无效等 → 视为已停
        # 成功返回但无 STATE 行（罕见中间态）→ 继续等
    } catch {
        break
    }
    Start-Sleep -Seconds 2
}
Write-Host "      服务已停止" -ForegroundColor Green

# ── 2. 清理残留 uvicorn / 僵尸 MT5 终端 / 卡死 python ──
Write-Host "[3/5] 清理残留进程..." -ForegroundColor Yellow
$killedT = 0
Get-Process -Name "terminal64" -ErrorAction SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $killedT++ } catch {}
}
Write-Host "      已清理 MT5 终端进程: $killedT 个" -ForegroundColor Green

$killedP = 0
Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    # 只杀 Services 会话（Session 0）里的 python，避免误伤用户桌面其他 Python
    $_.SessionId -eq 0
} | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $killedP++ } catch {}
}
Write-Host "      已清理 Services 会话残留 python: $killedP 个" -ForegroundColor Green

# 端口 8080 若仍被占用，连同占用者一起清掉
$occ = (netstat -ano | Select-String ":8080\s.*LISTENING")
if ($occ) {
    foreach ($line in $occ) {
        $p = ($line.ToString() -split '\s+')[-1]
        if ($p -match '^\d+$') {
            try { Stop-Process -Id ([int]$p) -Force -ErrorAction Stop; Write-Host "      已清理占用 8080 的 PID $p" -ForegroundColor Green } catch {}
        }
    }
}

# ── 2.5 预创建 WebView2 数据目录，避免 LocalSystem 下 MT5 弹窗报错 ──
$wvDir = "F:\WanxiangAI\data\webview2"
if (-not (Test-Path $wvDir)) {
    New-Item -ItemType Directory -Path $wvDir -Force | Out-Null
}
# 给 LocalSystem / Administrators / Users 写权限（即使目录已存在也刷新）
try {
    icacls $wvDir /grant "SYSTEM:(OI)(CI)F" /T /Q | Out-Null
    icacls $wvDir /grant "Administrators:(OI)(CI)F" /T /Q | Out-Null
    icacls $wvDir /grant "Users:(OI)(CI)F" /T /Q | Out-Null
    Write-Host "      WebView2 数据目录已就绪: $wvDir" -ForegroundColor Green
} catch {
    Write-Host "      WebView2 目录权限设置失败（可继续）: $_" -ForegroundColor DarkYellow
}

# ── 3. 起服务 ──
Write-Host "[4/5] 正在启动服务 $SVC ..." -ForegroundColor Yellow
sc.exe start $SVC | Out-Null
Start-Sleep -Seconds 5

# ── 4. 等待健康检查通过 ──
# 2026-08-09：MT5 账号接入已改为 lifespan 返回后后台异步执行，health 本身很快
# 可用；但为了让用户看到"服务真的活了"，这里仍然等 health 200。最大 300s。
Write-Host "[5/5] 等待后端就绪（最多 300 秒）..." -ForegroundColor Yellow
$ok = $false
$deadline = (Get-Date).AddSeconds(300)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/health" -UseBasicParsing -TimeoutSec 8
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}

Write-Host ""
if ($ok) {
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "  重启成功，后端已就绪" -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "MT5 各账号会在随后 1-3 分钟内陆续自动接入。" -ForegroundColor Gray
    Write-Host "可回到对话，让助手复测全模块接口。" -ForegroundColor Gray
} else {
    Write-Host "==============================================" -ForegroundColor Red
    Write-Host "  后端未在 300 秒内就绪，请把这个窗口的内容告知助手" -ForegroundColor Red
    Write-Host "==============================================" -ForegroundColor Red
    sc.exe query $SVC
}
Write-Host ""
Read-Host "按回车关闭窗口"
