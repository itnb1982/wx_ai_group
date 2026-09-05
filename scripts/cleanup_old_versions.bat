@echo off
chcp 65001 >nul
echo ========================================
echo   万象Ai - 清理旧版本调试文件
echo   保留最新核心代码和运行脚本
echo   总架构师自动清理工具
echo ========================================
echo.

set "PROJECT_DIR=F:\WanxiangAI"

echo [1/3] 扫描需要清理的文件...
echo.

set "ROOT_COUNT=0"
set "BACKEND_COUNT=0"

:: 统计根目录文件
for %%f in (
    analyze_positions.py
    audit_examples.py
    audit_leader.py
    audit_live.py
    audit_trades.py
    build_report.py
    check_diag.py
    cleanup_mt5.py
    cleanup_mt5_v2.py
    cleanup_mt5_v3.py
    diag_debug.py
    migrate_max_positions.py
    probe_api.py
    probe_cboe.py
    probe_ext_test.py
    probe_fred.py
    probe_stooq.py
    probe_stooq2.py
    test_adjudicate.py
    test_direct_launch.py
    test_e2e_market.py
    test_e2e_v2.py
    test_l2_exit.py
    test_liumanchun4_after.py
    test_liumanchun4_diag.py
    test_mt5_dual.py
    verify_accounts.py
    verify_ai_quality.py
    verify_leader.py
    verify_p0.py
    AI自主交易技术调研_20260805.md
    交易模块问题清单与AI自主交易改进方向_20260805.md
    仪表盘交易流程设计.md
    仪表盘交易流程设计_v3.md
    优化方案_AI交易质量提升_20260804.md
    优化方案_AI交易质量提升_v2_AI驱动.md
    外部指标API技术调研.md
    审计报告_20260804.md
    审计报告_v1.0_20260802.md
    新机安装教程.md
    架构_API调用与缩放方案.md
    验证报告_双向事件_20260805.md
) do (
    if exist "%PROJECT_DIR%\%%f" set /a ROOT_COUNT+=1
)

:: 统计 backend 目录文件（排除核心文件）
for %%f in (
    _patch_tmp.py
    _poll_health.py
    _r19_parse.py
    _verify_daily.py
    _verify_db.py
    _verify_gap.py
    _verify_hourly.py
    _verify_levels.py
    _verify_pos.py
    _verify_log.py
    _verify_r10.py
    _verify_r10b.py
    _verify_r10c.py
    _verify_r10d.py
    _verify_r11.py
    _verify_r11db.py
    _verify_r11log.py
    _verify_r11m.py
    _verify_r11x.py
    _verify_r13.py
    _verify_r13b.py
    _verify_r13c.py
    _verify_r17.py
    _verify_r17db.py
    _verify_r17log.py
    _verify_r17x.py
    _verify_r18.py
    _verify_r18_log.py
    _verify_r18_log2.py
    _verify_r19.py
    _verify_r19db.py
    _verify_r19log.py
    _verify_r19m.py
    _verify_r19mkt.py
    _verify_r19pos.py
    _verify_r5.py
    _verify_r6.py
    _verify_r6b.py
    _verify_r6c.py
    _verify_r7.py
    _verify_r7b.py
    _verify_r9.py
    _verify_r9b.py
    audit2.py
    audit3_iso.py
    audit4_guard.py
    audit_all_apis.py
    diag_exit.py
    diag_exit2.py
    diag_exit3.py
    diag_final.py
    diag_timestamps.py
    emergency_console.py
    explore_accounts.py
    explore_accounts2.py
    explore_trades_schema.py
    gen_audit_chart.py
    launch_supervisor.py
    ollama_pull_daemon.py
    pre_restart_flatten.py
    query_leader_baseline.py
    query_recent.py
    research_active_accounts.py
    research_meta_contra.py
    research_recent.py
    restart_backend.py
    run_backtest.py
    run_guard.py
    test_asian_gate.py
    test_pm_standalone.py
    test_smc_fix.py
    test_vision_pipeline.py
    verify_fixes.py
    verify_smc_bias.py
    windows_service.py
    盯盘监控.py
) do (
    if exist "%%f" set /a BACKEND_COUNT+=1
)

echo   根目录待清理: %ROOT_COUNT% 个文件
echo   backend 待清理: %BACKEND_COUNT% 个文件
echo.

echo [2/3] 正在清理根目录文件...
echo.

:: 删除根目录调试/测试脚本
for %%f in (
    analyze_positions.py
    audit_examples.py
    audit_leader.py
    audit_live.py
    audit_trades.py
    build_report.py
    check_diag.py
    cleanup_mt5.py
    cleanup_mt5_v2.py
    cleanup_mt5_v3.py
    diag_debug.py
    migrate_max_positions.py
    probe_api.py
    probe_cboe.py
    probe_ext_test.py
    probe_fred.py
    probe_stooq.py
    probe_stooq2.py
    test_adjudicate.py
    test_direct_launch.py
    test_e2e_market.py
    test_e2e_v2.py
    test_l2_exit.py
    test_liumanchun4_after.py
    test_liumanchun4_diag.py
    test_mt5_dual.py
    verify_accounts.py
    verify_ai_quality.py
    verify_leader.py
    verify_p0.py
) do (
    if exist "%PROJECT_DIR%\%%f" (
        del /F /Q "%PROJECT_DIR%\%%f" >nul
        echo   [根] 删除: %%f
    )
)

:: 删除根目录旧版本文档
for %%f in (
    AI自主交易技术调研_20260805.md
    交易模块问题清单与AI自主交易改进方向_20260805.md
    仪表盘交易流程设计.md
    仪表盘交易流程设计_v3.md
    优化方案_AI交易质量提升_20260804.md
    优化方案_AI交易质量提升_v2_AI驱动.md
    外部指标API技术调研.md
    审计报告_20260804.md
    审计报告_v1.0_20260802.md
    新机安装教程.md
    架构_API调用与缩放方案.md
    验证报告_双向事件_20260805.md
) do (
    if exist "%PROJECT_DIR%\%%f" (
        del /F /Q "%PROJECT_DIR%\%%f" >nul
        echo   [根] 删除: %%f
    )
)

echo.
echo [3/3] 正在清理 backend 目录旧文件...
echo.

:: 删除 backend 目录调试/验证脚本
set "DEL_COUNT=0"
for %%f in (
    _patch_tmp.py
    _poll_health.py
    _r19_parse.py
    _verify_daily.py
    _verify_db.py
    _verify_gap.py
    _verify_hourly.py
    _verify_levels.py
    _verify_pos.py
    _verify_log.py
    _verify_r10.py
    _verify_r10b.py
    _verify_r10c.py
    _verify_r10d.py
    _verify_r11.py
    _verify_r11db.py
    _verify_r11log.py
    _verify_r11m.py
    _verify_r11x.py
    _verify_r13.py
    _verify_r13b.py
    _verify_r13c.py
    _verify_r17.py
    _verify_r17db.py
    _verify_r17log.py
    _verify_r17x.py
    _verify_r18.py
    _verify_r18_log.py
    _verify_r18_log2.py
    _verify_r19.py
    _verify_r19db.py
    _verify_r19log.py
    _verify_r19m.py
    _verify_r19mkt.py
    _verify_r19pos.py
    _verify_r5.py
    _verify_r6.py
    _verify_r6b.py
    _verify_r6c.py
    _verify_r7.py
    _verify_r7b.py
    _verify_r9.py
    _verify_r9b.py
    audit2.py
    audit3_iso.py
    audit4_guard.py
    audit_all_apis.py
    diag_exit.py
    diag_exit2.py
    diag_exit3.py
    diag_final.py
    diag_timestamps.py
    emergency_console.py
    explore_accounts.py
    explore_accounts2.py
    explore_trades_schema.py
    gen_audit_chart.py
    launch_supervisor.py
    ollama_pull_daemon.py
    pre_restart_flatten.py
    query_leader_baseline.py
    query_recent.py
    research_active_accounts.py
    research_meta_contra.py
    research_recent.py
    restart_backend.py
    run_backtest.py
    run_guard.py
    test_asian_gate.py
    test_pm_standalone.py
    test_smc_fix.py
    test_vision_pipeline.py
    verify_fixes.py
    verify_smc_bias.py
    windows_service.py
    盯盘监控.py
) do (
    if exist "%%f" (
        del /F /Q "%%f" >nul
        set /a DEL_COUNT+=1
    )
)
echo   [后端] 已删除 %DEL_COUNT% 个旧文件

echo.
echo ========================================
echo   清理完成！
echo ========================================
echo.
echo 已保留的核心文件：
echo   - backend/app/ （主应用代码）
echo   - backend/supervisor.py （服务管理）
echo   - backend/monitor.py （监控模块）
echo   - backend/start_backend_now.py （启动脚本）
echo   - backend/restart_as_admin.py （重启工具）
echo   - frontend/ （前端代码）
echo   - scripts/ （管理脚本）
echo   - docs/ （文档）
echo.
echo 当前最新版本：65e0301
echo Git 仓库：https://github.com/itnb1982/wx_ai_group
echo.
