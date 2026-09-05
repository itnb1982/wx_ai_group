# 万象Ai — 根治 Windows Defender 锁文件导致进化权重/记忆库落盘失败
# ============================================================
# 【必须以管理员身份运行 PowerShell 执行本脚本】
#   右键 PowerShell → 以管理员身份运行 → 执行：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   & "<项目根>\backend\add_defender_exclusions.ps1"
#
# 作用：将以下路径加入 Windows Defender 排除项，Defender 不再实时扫描这些目录的写入，
#       彻底消除 WinError 5 (PermissionError) 落盘失败，确保 AI 进化状态/记忆持久化成功。
#       配合 meta_agent / memory_bank 的原子写 + 重试降级（双保险），重启不再丢进化。
# 路径说明（全部由脚本自身位置推导，不写死任何盘符）：
#   <项目根>              → 后端代码 + JSON 状态文件
#                           （meta_agent_state.json / memory_bank.json 在此目录下）
#   <项目根>\data         → 运行期数据与日志
#   <项目根>\backend\data → SQLite 生产库（wx_prod.dat）
#
# 2026-08-07 修正（事故复盘产出）：
#   ① 原写法 "F:\\WanxiangAI" 在 PowerShell 中 **不是转义**（PS 用反引号 ` 转义，不是反斜杠），
#      字符串字面值就是带两个反斜杠的 F:\\WanxiangAI，排除项很可能从未真正生效。改单反斜杠。
#   ② 补入生产库目录 backend\data —— wx_prod.dat(32MB) 才是 Defender 扫描锁的首要受害者。
#      2026-08-07 18:26~18:35 后端连续崩溃循环 10 分钟无法自愈，即因该库被扫描锁反复占用。
#
# 2026-08-08 可移植性修正：
#   路径原先写死 F:\WanxiangAI。客户机装在 D:\ 或 C:\Program Files 下时，
#   这个脚本会给三个**不存在的目录**加排除 —— 命令本身还会"成功"，
#   于是运维以为已经处理过了，实际生产库仍在被 Defender 扫描锁。
#   这类"看起来生效、其实没生效"的失败最难排查，必须从根上消除。
$ROOT = Split-Path -Parent $PSScriptRoot   # <项目根>（本脚本位于 <项目根>\backend\）
$paths = @(
    $ROOT,
    (Join-Path $ROOT "data"),
    (Join-Path $ROOT "backend\data")
)

Write-Host "项目根目录: $ROOT`n"

if (-not ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "未以管理员身份运行 —— Add-MpPreference 将失败。请右键 PowerShell → 以管理员身份运行。"
}

foreach ($p in $paths) {
    try {
        Add-MpPreference -ExclusionPath $p -ErrorAction Stop
        Write-Host "[OK] 已排除: $p"
    }
    catch {
        Write-Warning "[跳过] 排除失败(可能已存在或无管理员权限): $p -> $_"
    }
}

Write-Host "`n=== 当前生效的排除路径（需管理员权限才能读取）==="
try { (Get-MpPreference).ExclusionPath | ForEach-Object { Write-Host "  $_" } }
catch { Write-Warning "无法读取排除列表（需管理员权限）" }

Write-Host "`n完成。请重启后端(supervisor)使持久化排除+重试生效。"
Write-Host "验证：重启后观察日志 [MetaAgent] 权重已持久化 / [记忆库] 不再出现 落盘失败；"
Write-Host "      且 supervisor_uvicorn.log 不再出现 '_raw_creator 第 N/6 次失败'。"
