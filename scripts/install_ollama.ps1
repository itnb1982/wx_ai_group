<#
.SYNOPSIS
    万象Ai — 本地 LLM 运行时（Ollama + Qwen3-8B）一键部署

.DESCRIPTION
    ╔══════════════════════════════════════════════════════════════════╗
    ║ 为什么需要这个脚本                                                ║
    ║                                                                  ║
    ║ Qwen3-8B 是系统的第四个模型（决策校对员 / 降级副驾）。它不是      ║
    ║ 交易主链路的依赖——不装也能正常交易——但装上之后：               ║
    ║   · 常态：每笔决策多一道结构校验，能拦住 JSON 畸形、止损挂反、     ║
    ║     理由与方向自相矛盾这类云端偶发的低级错误；                    ║
    ║   · 降级：云端双脑双双失联时，它是唯一还能出票的 LLM。            ║
    ║                                                                  ║
    ║ 而它的安装偏偏是整套部署里最容易卡住的一步：Ollama 的分发走       ║
    ║ GitHub Release，国内直连经常只有几十 KB/s，1.4GB 的包能拖几小时。 ║
    ║ 让客户自己去趟这个坑，等于把交付成功率交给运气。                  ║
    ╚══════════════════════════════════════════════════════════════════╝

    脚本做四件事：
      1. 已装则跳过（幂等，可反复执行）
      2. 多个下载源**并发测速**，挑最快的那个下载（而不是傻等第一个）
      3. 断点续传 + 校验，中断了再跑一次即可接着下
      4. 拉取 qwen3:8b，并做一次真实推理验证

    全程不需要管理员权限：用免安装 zip 版，解压到用户目录即可。

.PARAMETER InstallDir
    Ollama 解压目录。默认 <项目根>\runtime\ollama

.PARAMETER SkipModel
    只装 Ollama 运行时，不拉模型（适合先把 1.4GB 搞定，模型稍后再说）

.PARAMETER Version
    Ollama 版本号，默认 v0.32.6

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_ollama.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$SkipModel,
    [string]$Version = "v0.32.6"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # 关掉进度条，Invoke-WebRequest 会快一个数量级

$ROOT = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $ROOT "runtime\ollama"
}
$MODEL = "qwen3:8b"
$ZIP_NAME = "ollama-windows-amd64.zip"

function Write-Step($msg)  { Write-Host "`n=== $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Err2($msg)  { Write-Host "  [X]  $msg" -ForegroundColor Red }

# ═══════════════════════════════════════════════════════════════════
#  0. 已装检测
# ═══════════════════════════════════════════════════════════════════
function Get-OllamaExe {
    # 找顺序：本脚本的安装目录 → PATH → 官方安装器的默认落点
    $candidates = @(
        (Join-Path $InstallDir "ollama.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        "$env:ProgramFiles\Ollama\ollama.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor DarkCyan
Write-Host " 万象Ai — 本地 LLM 运行时部署（Ollama + $MODEL）" -ForegroundColor White
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor DarkCyan

Write-Step "检查现有安装"
$exe = Get-OllamaExe
if ($exe) {
    Write-Ok "已检测到 Ollama：$exe"
} else {
    # ═══════════════════════════════════════════════════════════════
    #  1. 多源并发测速
    # ═══════════════════════════════════════════════════════════════
    # 为什么要测速而不是按固定顺序试：镜像站的可用性是**按小时变化**的。
    # 今天最快的源明天可能挂掉。写死顺序 = 把运气写进部署脚本。
    # 每个源只下 3MB 做样本，全部并发，5 秒内就能选出赢家。
    $gh = "https://github.com/ollama/ollama/releases/download/$Version/$ZIP_NAME"
    $sources = @(
        @{ Name = "ghfast.top";     Url = "https://ghfast.top/$gh" },
        @{ Name = "gh-proxy.com";   Url = "https://gh-proxy.com/$gh" },
        @{ Name = "ghproxy.net";    Url = "https://ghproxy.net/$gh" },
        @{ Name = "moeyy.xyz";      Url = "https://github.moeyy.xyz/$gh" },
        @{ Name = "GitHub 直连";     Url = $gh }
    )

    Write-Step "并发测速（每源取 3MB 样本，约 8 秒）"
    $jobs = foreach ($s in $sources) {
        Start-Job -ScriptBlock {
            param($url, $name)
            $sw = [Diagnostics.Stopwatch]::StartNew()
            try {
                $req = [Net.HttpWebRequest]::Create($url)
                $req.Timeout = 8000
                $req.ReadWriteTimeout = 8000
                $req.AddRange(0, 3145727)          # 前 3MB
                $resp = $req.GetResponse()
                $stream = $resp.GetResponseStream()
                $buf = New-Object byte[] 65536
                $total = 0
                while ($sw.ElapsedMilliseconds -lt 8000) {
                    $n = $stream.Read($buf, 0, $buf.Length)
                    if ($n -le 0) { break }
                    $total += $n
                }
                $stream.Close(); $resp.Close()
                $sw.Stop()
                $kbps = if ($sw.Elapsed.TotalSeconds -gt 0) { $total / 1024 / $sw.Elapsed.TotalSeconds } else { 0 }
                [PSCustomObject]@{ Name = $name; Url = $url; KBps = [math]::Round($kbps, 1); Bytes = $total }
            } catch {
                [PSCustomObject]@{ Name = $name; Url = $url; KBps = 0; Bytes = 0 }
            }
        } -ArgumentList $s.Url, $s.Name
    }

    $null = Wait-Job -Job $jobs -Timeout 25
    $results = @()
    foreach ($j in $jobs) {
        try { $results += Receive-Job -Job $j -ErrorAction SilentlyContinue } catch {}
    }
    Remove-Job -Job $jobs -Force -ErrorAction SilentlyContinue

    $results = $results | Where-Object { $_ -ne $null } | Sort-Object -Property KBps -Descending
    foreach ($r in $results) {
        $tag = if ($r.KBps -gt 0) { "{0,8:N1} KB/s" -f $r.KBps } else { "     不可用" }
        Write-Host ("    {0,-14} {1}" -f $r.Name, $tag)
    }

    $best = $results | Where-Object { $_.KBps -gt 0 } | Select-Object -First 1
    if (-not $best) {
        Write-Err2 "所有下载源均不可用。"
        Write-Host @"

  这通常是网络环境问题，不是脚本问题。三条出路：

  1) 换个时段重试（镜像站白天负载高，夜间通常快很多）
  2) 手动下载后放到指定位置，再重跑本脚本：
       下载 $ZIP_NAME
       放到 $InstallDir\$ZIP_NAME
  3) 直接跳过——Ollama 是【增强项】不是【依赖项】。
     不装它，系统依旧正常交易，只是：
       · 少一道决策校对
       · 云端双脑同时失联时没有本地副驾兜底（降级面板会明确提示"副驾缺位"）
     系统管理页会一直显示"未启用"并附带安装指引，随时可以补装。

"@ -ForegroundColor Gray
        exit 2
    }

    Write-Ok "选用 $($best.Name)（$($best.KBps) KB/s）"
    $est = [math]::Round(1390 / ($best.KBps / 1024) / 60, 1)
    Write-Host "    预计耗时约 $est 分钟（1.4GB）" -ForegroundColor DarkGray
    if ($best.KBps -lt 200) {
        Write-Warn2 "速度偏低。可以让它挂着慢慢下——支持断点续传，中断后重跑本脚本会接着下。"
    }

    # ═══════════════════════════════════════════════════════════════
    #  2. 断点续传下载
    # ═══════════════════════════════════════════════════════════════
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $zipPath = Join-Path $InstallDir $ZIP_NAME

    Write-Step "下载 Ollama $Version"
    # 优先用系统自带 curl.exe：它的断点续传（-C -）比 Invoke-WebRequest 可靠得多，
    # 且不会把整个文件读进内存（1.4GB 走内存对小配置机器是灾难）。
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --retry 10 --retry-delay 5 --retry-all-errors -C - `
            --speed-limit 1024 --speed-time 120 `
            -o $zipPath $best.Url
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 33) {
            # 33 = 服务器不支持断点续传；此时删掉重下
            if ($LASTEXITCODE -eq 33) { Remove-Item $zipPath -Force -ErrorAction SilentlyContinue }
            Write-Err2 "下载失败（curl 退出码 $LASTEXITCODE）。重跑本脚本可断点续传。"
            exit 3
        }
    } else {
        Invoke-WebRequest -Uri $best.Url -OutFile $zipPath -UseBasicParsing
    }

    if (-not (Test-Path $zipPath)) { Write-Err2 "下载后文件不存在"; exit 3 }
    $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    if ($sizeMB -lt 500) {
        Write-Err2 "文件只有 $sizeMB MB，明显不完整（应约 1390MB）。重跑本脚本继续下载。"
        exit 3
    }
    Write-Ok "下载完成（$sizeMB MB）"

    # ═══════════════════════════════════════════════════════════════
    #  3. 解压
    # ═══════════════════════════════════════════════════════════════
    Write-Step "解压到 $InstallDir"
    try {
        Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
    } catch {
        Write-Err2 "解压失败：$($_.Exception.Message)"
        Write-Warn2 "通常意味着包没下完整。删掉 $zipPath 后重跑本脚本。"
        exit 4
    }
    $exe = Join-Path $InstallDir "ollama.exe"
    if (-not (Test-Path $exe)) { Write-Err2 "解压后找不到 ollama.exe"; exit 4 }
    Write-Ok "解压完成"
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
}

# ═══════════════════════════════════════════════════════════════════
#  4. 启动服务
# ═══════════════════════════════════════════════════════════════════
Write-Step "启动 Ollama 服务"
function Test-OllamaUp {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

if (Test-OllamaUp) {
    Write-Ok "服务已在运行（127.0.0.1:11434）"
} else {
    # OLLAMA_MODELS 指到项目内，避免模型散落在 C 盘用户目录——
    # 换机迁移时能跟着项目一起搬，省掉重新下载 5GB。
    $modelsDir = Join-Path $ROOT "runtime\ollama-models"
    New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
    [Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $modelsDir, "User")
    $env:OLLAMA_MODELS = $modelsDir

    Start-Process -FilePath $exe -ArgumentList "serve" -WindowStyle Hidden
    $ok = $false
    foreach ($i in 1..20) {
        Start-Sleep -Seconds 1
        if (Test-OllamaUp) { $ok = $true; break }
    }
    if ($ok) { Write-Ok "服务已启动，模型目录：$modelsDir" }
    else { Write-Err2 "服务启动超时。手动执行：`"$exe`" serve"; exit 5 }
}

# ═══════════════════════════════════════════════════════════════════
#  5. 拉取模型
# ═══════════════════════════════════════════════════════════════════
if ($SkipModel) {
    Write-Warn2 "已跳过模型拉取（-SkipModel）。稍后执行：`"$exe`" pull $MODEL"
} else {
    Write-Step "拉取模型 $MODEL（约 5GB，Q4_K_M 量化）"
    $tags = ""
    try { $tags = (Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5 -UseBasicParsing).Content } catch {}
    if ($tags -match [regex]::Escape($MODEL)) {
        Write-Ok "模型已存在，跳过"
    } else {
        Write-Host "    Ollama 自带断点续传，中断后重跑本脚本即可接着下。" -ForegroundColor DarkGray
        & $exe pull $MODEL
        if ($LASTEXITCODE -ne 0) {
            Write-Err2 "模型拉取失败（退出码 $LASTEXITCODE）"
            Write-Warn2 "重跑本脚本可续传。或改用更小的 qwen3:4b：`"$exe`" pull qwen3:4b"
            exit 6
        }
        Write-Ok "模型就绪"
    }
}

# ═══════════════════════════════════════════════════════════════════
#  6. 真实推理验证
# ═══════════════════════════════════════════════════════════════════
# 只验证"服务活着"是不够的——服务活着但模型加载不进显存（显存不足）
# 是很常见的失败模式，而且只有真正推理时才暴露。
if (-not $SkipModel) {
    Write-Step "端到端验证（真实推理一次）"
    $body = @{
        model  = $MODEL
        prompt = "/no_think 回复 OK 两个字符即可。"
        stream = $false
        options = @{ temperature = 0.3; num_ctx = 4096 }
    } | ConvertTo-Json -Depth 5

    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/generate" `
            -Method Post -Body $body -ContentType "application/json" -TimeoutSec 180
        $sw.Stop()
        Write-Ok "推理成功（$([math]::Round($sw.Elapsed.TotalSeconds,1))s）：$($resp.response -replace '\s+',' ')"
    } catch {
        Write-Err2 "推理失败：$($_.Exception.Message)"
        Write-Warn2 "常见原因是显存不足（8GB 显卡需保证约 5.5GB 空闲）。"
        Write-Warn2 "可改用更小的模型：`"$exe`" pull qwen3:4b，并设置 WX_LOCAL_LLM_MODEL=qwen3:4b"
        exit 7
    }
}

Write-Host "`n══════════════════════════════════════════════════════════" -ForegroundColor DarkCyan
Write-Ok "本地 LLM 运行时部署完成"
Write-Host @"

  后端会自动探测 127.0.0.1:11434，无需改任何配置。
  打开系统管理页即可看到 Qwen3-8B 从「未启用」变为「在岗」。

  相关环境变量（都是可选的）：
    WX_LOCAL_LLM_DISABLED=1        临时停用本地 LLM
    WX_LOCAL_LLM_MODEL=qwen3:4b    换用更小的模型
    OLLAMA_MODELS                  模型存放目录（已设为项目内）

"@ -ForegroundColor Gray
