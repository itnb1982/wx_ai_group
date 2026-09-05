"""
XAU/USD万象Ai自动量化交易系统 v1.0.0 — DeepSeek V4 + 混元 Hy3 双模型AI自动交易系统
FastAPI 主入口
"""
import os
import sys
import signal
import threading
import webbrowser
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

# 日志落地到文件（便于排查启动/自动交易循环状态，不再吞进后台 stderr）
# R4 修复(2026-08-04)：文件名带 PID，避免 supervisor 重启时 Windows 文件句柄竞争
# 导致新进程 loguru 文件 sink 打开失败、日志全线静默（旧单文件 wanxiang_backend.log 仍保留为历史）。
import os as _os
# 【2026-08-08 可移植性修复】数据目录统一走 runtime_paths.data_dir()，
# 兜底为「项目根/data」而不是开发机的 F:/WanxiangAI/data。
# 原来的绝对路径兜底把开发机焊进了产品：客户装到 D 盘且没设 DATA_DIR，
# 日志就往一个不存在的盘符写，静默丢失、排障时一片空白。
import sys as _sys
from pathlib import Path as _Path
_BACKEND_DIR = _Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in _sys.path:
    _sys.path.insert(0, str(_BACKEND_DIR))
from runtime_paths import data_path as _data_path  # noqa: E402

try:
    logger.add(
        _data_path(f"wanxiang_backend_{_os.getpid()}.log"),
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        level="INFO",
        enqueue=False,
    )
except Exception:
    pass

# 确保后端包在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.version import get_build_info as _build_info
from app.database import init_db, SessionLocal, start_db_selfheal_daemon as init_db_selfheal
from app.routers import auth_router, accounts_router, keys_router, strategy_router, dashboard_router, mt5_discover_router, trading_router, emergency_router, license_router, local_model_router, ts_reference_router

# ─────────────────────────────────────────────────────────────
# 进程存活自检（根治"后端无声死亡"）：记录启动时间，供 /api/health
# 返回真实运行时长 + 关键守护线程存活信号，前端据此判断后端是真活而非假活。
# ─────────────────────────────────────────────────────────────
import time as _time
_STARTUP_TS = _time.time()          # 进程启动时间戳（单调时钟，不受系统时间回拨影响）
_LAST_CYCLE_TS = {"ts": _time.time()}  # 自动交易循环最近一次成功 tick（ trading 模块会更新）

# 已配置账号数（启动接入时确定）。health 靠它判断「该在线几个」——
# 存内存而非每次查库：health 被 supervisor 每 5s 轮询，查库既有成本更有撞锁风险，
# 而撞锁恰恰是本次事故的诱因，绝不能让健康检查自己成为新的故障源。
_ACCOUNT_STATE = {"expected": 0, "bootstrap": "未执行"}
# DB 就绪状态（供健康检查如实上报；不查库，避免 health 每 5s 撞锁）
_DB_STATE = {"ready": False, "detail": "未初始化"}

def touch_cycle():
    """由自动交易循环每轮调用，标记"核心交易链路还活着"."""
    _LAST_CYCLE_TS["ts"] = _time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    # 启动时
    logger.info("=" * 60)
    logger.info(f"  {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"  {settings.APP_DESCRIPTION}")
    logger.info("=" * 60)

    # ★ 2026-08-08 重写：初始化数据库 —— 启动路径快速认输，自愈交给后台。
    #
    # 事故背景：旧 init_db(max_retry=6) 配合 _raw_creator 的 31.5s 退避，
    # 在 DB 撞 readonly 时死等 198s（实测 198.055s），加上账号接入的同步重试
    # 共 265s，越过 supervisor 的 260s 判死线 → 强杀 → 重启 → 再 265s → 死循环。
    # 而强杀又留下 hot journal，让下一轮更容易撞 readonly，自我强化。
    #
    # 现在：几秒内认输并放行启动，DB 未就绪则起后台守护持续自愈；
    # 健康检查如实报 degraded（supervisor 不把 degraded 计入失败，不会误杀）。
    _db_ready = init_db()
    _DB_STATE["ready"] = _db_ready
    if not _db_ready:
        _DB_STATE["detail"] = "启动期建表未成功，已转入后台自愈"
        init_db_selfheal(
            only_if_needed=False,  # 启动期刚失败过，无需再探一次
            interval=10.0, max_interval=120.0,
            on_ready=lambda: _DB_STATE.update({"ready": True, "detail": "后台自愈完成"}),
        )
    logger.info(f"  数据库: {settings.get_database_url()}（就绪={_db_ready}）")

    # ★ 2026-08-08/09 重写：自动接入所有已保存的 MT5 账号。
    #
    # 事故背景：原实现把「取账号列表」和「逐个连接」写在同一个 try 里，
    # 重启瞬间 DB 撞一次 readonly（旧进程残留 journal + Defender 扫 34MB 库），
    # SessionLocal() 一抛，for 循环一次都没进 —— **4 个客户账号全部静默失联，
    # 且此后永不重试**。而事后探测同一个库 0.00s 可写，本是几十秒就会自愈的抖动。
    # 更糟的是它不声张：health 仍报 ok，DB 里 4 行还留着上次会话的 ONLINE，
    # 前端 4 个绿灯 —— 多租户下这是 4 个真实客户全天不交易的赔付级事故。
    #
    # 2026-08-09 二次加固：connector 内部含 90s MT5 终端启动/Worker 初始化超时，
    # 4 账号串行最坏 360s。若放在 lifespan 同步执行，/api/health 在这 6 分钟内完全
    # 不可达 → supervisor 启动探针 240s 判死并强杀 → 永远起不来。
    # 因此把 bootstrap 整体移入后台守护线程，lifespan 立即返回，health 立刻可用。
    import threading

    def _bootstrap_bg():
        try:
            from app.services import account_bootstrap as _ab
            from app.services.mt5_service import mt5_service
            from app.utils.crypto import decrypt as _decrypt

            def _connector(account_id, login, password, server, name, terminal_path):
                return mt5_service.add_account(
                    account_id=account_id, login=login, password=password,
                    server=server, name=name, terminal_path=terminal_path,
                )

            _boot = _ab.bootstrap(
                session_factory=SessionLocal, decryptor=_decrypt, connector=_connector,
                attempts=2, base_delay=1.0,
            )
            _ACCOUNT_STATE["expected"] = _boot.total
            _ACCOUNT_STATE["bootstrap"] = _boot.summary()
            logger.info(f"[启动] {_boot.summary()}")

            if _boot.needs_retry:
                _pending = None if _boot.load_error else list(_boot.failed)
                _ab.start_reconnect_daemon(
                    session_factory=SessionLocal, decryptor=_decrypt, connector=_connector,
                    pending_ids=_pending, interval=20.0, max_interval=180.0,
                    on_settled=lambda r: _ACCOUNT_STATE.update(
                        {"expected": max(_ACCOUNT_STATE.get("expected", 0), r.total),
                         "bootstrap": "后台自愈完成：" + r.summary()}
                    ),
                )
                logger.warning("[启动] 存在未接入账号，已启动后台重连守护（20s 起，退避至 180s）")
        except Exception as _e:
            logger.warning(f"[启动] MT5 账号接入流程异常: {_e}")

    threading.Thread(target=_bootstrap_bg, name="bootstrap-mt5", daemon=True).start()
    _ACCOUNT_STATE["bootstrap"] = "后台初始化中..."

    # ★ 2026-08-14：视觉模型第四票生产者线程（后台低频渲染 H4/M15 图表→送视觉模型(CPU)→缓存 VisionVote）。
    #   决策链只同步读缓存票，零延迟；启动失败仅降级为「无视觉票」，不影响主流程。
    try:
        from app.services.vision_service import get_service as get_vision_svc
        if getattr(settings, "VISION_VOTE_ENABLED", True):
            get_vision_svc().start()
            logger.info("  视觉模型第四票生产者已调度（后台守护线程）")
    except Exception as _ve:  # noqa: BLE001
        logger.warning(f"[启动] 视觉模型服务启动失败（降级：无视觉票）: {_ve}")

    # 确保数据目录存在
    Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)

    # ★ 2026-08-12 云模型总开关：启动时从 runtime_config 表读取主开关到内存 settings。
    #   客户可在【AI Key 管理 / 系统管理】自助切换（开=云+本地混跑，关=纯本地融合），
    #   无 DB 记录则回退 .env 默认值并落库。热切换、无需重启。
    try:
        from app.services.cloud_switch import init_cloud_switch
        init_cloud_switch()
        logger.info("  云模型总开关已从 DB 加载")
    except Exception as _e:
        logger.warning(f"[启动] 云模型开关加载异常（不影响交易）: {_e}")

    # 启动期自愈：清理 ai_activities 无界增长（保留窗口，task #294）。
    # 即便运行时分摊清理失效，重启也会立刻收敛到最新 MAX_AI_ACTIVITIES 条。
    try:
        from app.services.trade_executor import prune_ai_activities
        _pruned = prune_ai_activities()
        if _pruned:
            logger.info(f"[启动] ai_activities 自愈清理 {_pruned} 条旧活动")
    except Exception as _pe:
        logger.warning(f"[启动] ai_activities 清理跳过（不影响启动）: {_pe}")

    # 生成 SECRET_KEY（如果未设置）
    if not settings.SECRET_KEY:
        import secrets
        settings.SECRET_KEY = secrets.token_hex(32)
        logger.info(f"  已生成 SECRET_KEY")

    logger.info(f"  服务地址: http://{settings.HOST}:{settings.PORT}")
    logger.info(f"  交易品种: {settings.SYMBOL}")
    logger.info("=" * 60)

    # AI 工作剧场：写入进化初始化事件 + 启动信号扫描后台器（持续产生 AI 活动流）
    try:
        from app.services.ai_scanner import write_init_evolution, start_scanner
        write_init_evolution()
        start_scanner(interval=8)
    except Exception as _e:
        logger.warning(f"[启动] AI 扫描器启动异常: {_e}")

    # 启动自动交易循环（_auto_loop）：开市后自动 AI 决策 + 下单闭环
    # 关键：之前需要手动 POST /api/trade/auto/start 才能启动，结果用户看不到 AI 在工作。
    # 现在启动即自动开启，避免"仪表盘看不出 AI 在工作"的误导。
    try:
        from app.routers.trading import _start_auto_internal
        _start_auto_internal()
    except Exception as _e:
        logger.warning(f"[启动] 自动交易循环启动异常: {_e}")

    # 启动利润锁利高频守护线程（仅主号，每2s检查篮子浮盈，达标即全平并广播信号塔；零AI）
    # ★ 毫秒级要求：XAUUSD可在数秒内波动$2-5，15s轮询会导致浮盈$10→$12→回亏的全过程被漏掉。
    #   2s间隔 + 主循环双保险 + 反转即时平仓 = 准实时响应。
    try:
        from app.routers.trading import _l3_profit_lock_monitor_loop
        threading.Thread(target=_l3_profit_lock_monitor_loop, kwargs={"interval": 2.0}, daemon=True).start()
        logger.info("  利润锁利高频守护线程已启动（2s·毫秒级）")
    except Exception as _e:
        logger.warning(f"[启动] 利润锁利守护线程启动异常: {_e}")

    # 启动副号实时跟单守护线程（每10s拉信号塔总线立即镜像主号平仓/移损 + 入场补单兜底）
    # 核心修复：副号平仓从「等下一个主周期(~100s)」压到 ≤10s，金融产品不可有延时。
    try:
        from app.routers.trading import _follower_mirror_loop
        threading.Thread(target=_follower_mirror_loop, kwargs={"interval": 2.0}, daemon=True).start()
        logger.info("  副号实时跟单守护线程已启动（10s）")
    except Exception as _e:
        logger.warning(f"[启动] 副号实时跟单守护线程启动异常: {_e}")

    # 启动自触发一次 AI 辩论决策（后台线程，不阻塞启动），让「辩论擂台」冷启动即有内容
    try:
        import threading as _threading
        from app.routers.dashboard import trigger_initial_decision
        _threading.Thread(target=trigger_initial_decision, args=(1,), daemon=True).start()
    except Exception as _e:
        logger.warning(f"[启动] 自触发决策异常: {_e}")

    # ★ Phase 9.1（2026-08-08）：启动期预热本地 Qwen3-8B 校对员。
    #   首笔 proofread 若现加载 ~5GB 权重会超过常规 12s 超时 → 被当 skipped 跳过，
    #   断路器失效。后台预热一次把权重常驻显存（keep_alive=30m 维持），
    #   交易时段每笔决策都会调用，自然保持温热；休市无调用 30min 后自动释放。
    #   失败不阻断启动：校对员是增值断路器，模型挂了系统照常交易（fail-open）。
    try:
        from app.services.local_llm_service import get_local_llm

        def _warm_local_llm():
            try:
                get_local_llm().warm()
            except Exception as _we:  # noqa: BLE001
                logger.warning(f"[启动] 本地模型预热异常（不影响主流程）: {_we}")

        threading.Thread(target=_warm_local_llm, daemon=True).start()
        logger.info("  本地校对员预热线程已启动（后台）")
    except Exception as _e:
        logger.warning(f"[启动] 本地模型预热启动异常: {_e}")

    # 启动外部行情数据后台定时刷新（DXY/VIX/相关性），接口只读缓存 → 作战图 0 延迟
    try:
        from app.services.market_data import market_data_provider
        market_data_provider.refresh_loop(interval=60)
        market_data_provider.correlation_sampling_loop(interval=5)
        logger.info("  外部行情缓存刷新线程已启动（DXY/VIX 60s，相关性采样 5s）")
    except Exception as _e:
        logger.warning(f"[启动] 外部行情缓存线程启动异常: {_e}")

    # 启动本地时序模型「信号源参考」服务（仅前端观测，未接入交易决策链）。
    # 显式放在交易链路全部启动之后，并独立线程异步加载模型，绝不阻塞启动、
    # 绝不影响自动交易循环。其输出只供「多模型信号源参考面板」展示。
    try:
        from app.services.ts_reference_service import get_service

        get_service().ensure_started()
        logger.info("  本地时序参考服务已启动（后台刷新，仅供前端参考面板观测，未接入交易）")
    except Exception as _e:
        logger.warning(f"[启动] 时序参考服务启动异常: {_e}")

    # 启动 KeyPool 异步刷库线程（每 30s 把内存 token 统计写回 DB api_keys 表）
    try:
        from app.services.key_pool import start_flush_loop
        start_flush_loop(interval=30.0)
    except Exception as _e:
        logger.warning(f"[启动] KeyPool 刷库线程启动异常: {_e}")

    # ★ 2026-08-09 根治 health 阻塞：启动 MT5 状态缓存后台刷新线程。
    # health 每 5s 被拉一次，若同步对每个 worker 发 IPC ping 会阻塞 uvicorn 单 worker，
    # 导致 health 响应 10s+ 触发前端断连红条。此后台线程定期刷新状态到内存缓存，
    # health 只读缓存，响应回到毫秒级。
    try:
        from app.services.mt5_service import start_status_refresh
        start_status_refresh()
        logger.info("  MT5 状态缓存后台刷新已启动（health 将读取缓存）")
    except Exception as _e:
        logger.warning(f"[启动] MT5 状态缓存刷新启动异常: {_e}")

    # ★ 2026-08-06 根治：丢弃主进程启动期建立的 DB 引擎连接，避免残留只读句柄
    #   导致本进程内后续写操作间歇 readonly / 文件被本进程持续占用。之后请求走
    #   NullPool 全新连接，彻底绕开启动期异常路径。
    try:
        from app.database import dispose_all_engines
        dispose_all_engines()
    except Exception as _e:
        logger.warning(f"[启动] dispose_all_engines 异常: {_e}")

    yield

    # 关闭时
    try:
        from app.services.ai_scanner import stop_scanner
        stop_scanner()
    except Exception:
        pass
    from app.services.mt5_service import mt5_service
    mt5_service.shutdown_all()
    logger.info("  已关闭所有 MT5 连接")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.APP_DESCRIPTION,
    lifespan=lifespan,
)

# ─────────────────────────────────────────────────────────────
# 进程自杀保护（根治"后端无声死亡"）：捕获未处理异常/信号，
# 记录致命日志后立即退出——交给外层进程守护（任务计划/supervisor）自动拉起。
# 绝不让进程"半死不活"地挂着（静默、不服务、不停机），那比崩溃更危险。
# ─────────────────────────────────────────────────────────────
import sys as _sys
def _fatal_crash_hook(exc_type, exc_val, exc_tb):
    # 非崩溃信号：让正常退出/Ctrl+C/协程取消走原生流程，禁止强杀。
    if exc_type in (KeyboardInterrupt, SystemExit):
        return
    try:
        import asyncio as _asyncio
        if exc_type is _asyncio.CancelledError:
            return
    except Exception:
        pass
    try:
        logger.error(f"[致命崩溃] 未捕获异常 → 进程即将退出，等待守护拉起: "
                     f"{exc_type.__name__}: {exc_val}")
    except Exception:
        pass
    # 不调用 sys.exit（会进入正常退出流程），直接 os._exit 强制退出，
    # 确保守护进程能在最短时间内检测到并重启。
    # 仅对真·未捕获异常生效（KeyboardInterrupt/SystemExit/协程取消已排除）。
    import os as _os2
    _os2._exit(1)

_sys.excepthook = _fatal_crash_hook

def _sigterm_handler(_sig, _frame):
    logger.warning("[退出] 收到 SIGTERM，正常关闭（守护会按策略决定是否拉起）")
    _sys.exit(0)

import signal as _signal
try:
    _signal.signal(_signal.SIGTERM, _sigterm_handler)
except Exception:
    pass

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# SPA 入口强校验：所有 text/html 响应禁止浏览器长缓存。
# 否则用户缓存旧 index.html → 引用已被 deploy.py 清理的旧 JS 文件名 → 404 白屏。
# 哈希化资源（/assets/index-*.js|css，media_type 非 text/html）不命中，可放心长缓存。
class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    """SPA 入口防缓存：三重缓存头。

    注意：HTML 内资源引用（/assets/index-*.js|css）的版本戳由 deploy.py
    在构建时注入（产物级、一次性），本中间件只负责禁止浏览器长缓存 HTML 入口。
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # 注意：BaseHTTPMiddleware 包裹层下 FileResponse 的 .media_type 常为 None，
        # 必须直接读 content-type 响应头判断，否则 StaticFiles 返回的 HTML 漏判。
        _ct = response.headers.get("content-type", "") or (response.media_type or "")
        if not _ct.startswith("text/html"):
            return response
        # 三重保险：HTTP/1.1 no-cache + HTTP/1.0 Pragma + Expires: 0
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # 破坏 heuristic 缓存：去掉 ETag/Last-Modified，避免浏览器用 304 复用旧 HTML
        for _h in ("etag", "last-modified"):
            if _h in response.headers:
                del response.headers[_h]
        return response


app.add_middleware(NoCacheHTMLMiddleware)

# 注册路由
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(keys_router)
app.include_router(strategy_router)
app.include_router(dashboard_router)
app.include_router(mt5_discover_router)
app.include_router(trading_router)
app.include_router(emergency_router)
app.include_router(license_router)
app.include_router(local_model_router)
app.include_router(ts_reference_router)


@app.get("/api/diag_write")
def diag_write():
    """健康检查扩展：返回 DB 写引擎基本状态（轻量，不写库）。

    2026-08-06 根治 readonly 后保留的精简探针：仅确认进程内能否以读写方式
    打开主库文件 + 当前 journal 模式，供运维快速排查，不再做写操作。
    """
    import os as _os
    from app.database import _DB_FILE, write_engine
    return {
        "pid": _os.getpid(),
        "db_file": _DB_FILE,
        "db_file_w_ok": _os.access(_DB_FILE, _os.W_OK) if _os.path.exists(_DB_FILE) else None,
        "write_engine_url": str(write_engine.url),
    }




@app.get("/api/health")
def _health_route():
    """进程健康端点（根治 #610 回归）。

    2026-08-15 #610 曾误将 /api/health 改成只返回 gate_stats，导致前端 5s 心跳
    拿不到 pid/uptime/status/auto_loop_running 而误报断连/全屏红条。

    此处恢复为完整进程健康：health_check() 已内联 gate_stats（L622）与
    debate_ring_enabled（L625），前端心跳与 A/B 脚本采集两不误。
    """
    return health_check()


def _safe_gate_stats():
    """只读暴露裁决层方向门触发统计（A/B 实验采集用，供 health_check 内联）。

    异常即降级返回空统计，绝不因统计采集拖垮健康端点。
    """
    try:
        from app.core.meta_agent import get_gate_stats_snapshot
        return get_gate_stats_snapshot()
    except Exception as _e:
        logger.debug(f"[health] 读取 gate_stats 失败: {_e}")
        return {"total_decisions": 0, "holds": 0, "hold_rate": 0.0,
                "traded": 0, "trade_rate": 0.0, "gates": {}}


def health_check():
    """健康检查（根治"后端无声死亡"）

    返回真实进程存活信号，不止是静态 ok：
      - uptime_sec   : 进程已运行时长（秒），>0 即进程是活的
      - l3_guard_alive / follower_alive : 关键守护线程是否还活着
      - mt5_connected : 当前已连接的 MT5 账号数
      - last_cycle_sec : 自动交易循环最后 tick 距现在的秒数（过大说明交易链路僵死）
      - auto_loop_running : 自动交易主循环是否在跑
    前端心跳器每 5s 拉一次，任一异常即全屏红警。
    """
    now = _time.time()
    uptime = now - _STARTUP_TS
    # 最后 tick 时间从持久化文件读取（trading 每轮 cycle 写入 DATA_DIR/last_cycle_ts.txt），
    # 规避导入实例/多 worker 导致的内存变量不可见问题。文件缺失则回退到启动时间。
    _cycle_last_ts = _STARTUP_TS
    try:
        with open(_data_path("last_cycle_ts.txt"), "r") as _lf:
            _cycle_last_ts = float(_lf.read().strip())
    except Exception:
        pass
    cycle_gap = now - _cycle_last_ts
    # 守护线程存活探测：用 threading.enumerate 找已知名线程
    thread_names = {t.name for t in __import__("threading").enumerate()}
    l3_alive = any("l3" in n.lower() or "lock" in n.lower() for n in thread_names)
    follower_alive = any("follower" in n.lower() or "mirror" in n.lower() for n in thread_names)
    # MT5 连接数：2026-08-09 改为读取内存缓存，避免同步 IPC 阻塞 health 端点。
    # 真实状态由 lifespan 启动的后台线程定期刷新，缓存刷新间隔见 mt5_service。
    mt5_connected = 0
    auto_running = False
    try:
        from app.services.mt5_service import get_cached_accounts_status
        try:
            _statuses = get_cached_accounts_status()
            mt5_connected = sum(1 for s in _statuses if s.get("connected"))
        except Exception:
            mt5_connected = 0
        # auto_loop 运行状态从 trading 模块读取（同进程可见，最可靠）
        try:
            from app.routers.trading import _auto_running as _ar, _auto_status as _as
            auto_running = bool(_ar)
            cycles_done = int((_as or {}).get("cycles", 0) or 0)
            # ★ 2026-08-06 修正：cycle_gap 优先用进程内 _auto_status["last_cycle"]
            #   (同进程内存，绝对可靠)；last_cycle_ts.txt 受 Defender 锁定时写不进去，
            #   会长期停留旧时间戳，使 cycle_gap 虚高，却因下方误判显示 trade_stale=false
            #   的假健康（曾掩盖真实交易停滞）。内存值优先，文件值仅作兜底。
            _last_cycle_iso = (_as or {}).get("last_cycle")
            _gap_mem = None
            if _last_cycle_iso:
                try:
                    _gap_mem = now - datetime.fromisoformat(_last_cycle_iso).timestamp()
                except Exception:
                    _gap_mem = None
            if _gap_mem is not None:
                cycle_gap = _gap_mem
        except Exception:
            auto_running = False
            cycles_done = 0
    except Exception:
        pass
    # 判定交易链路是否僵死：
    # 优先信任进程内 auto_loop 真实状态——只要循环在跑且本轮进程已跑过周期，
    # 即视为健康；仅当进程内循环异常或本轮尚无数周期时，才用跨进程心跳文件
    # (last_cycle_ts.txt) 交叉验证（文件写受 Defender 影响时避免误报 degraded）。
    # 判定交易链路是否僵死：以「循环最近一次真正完成 tick 距现在」为准。
    # 只要 auto_loop 在跑且 cycle_gap ≤ 180s 即健康；否则 degraded。
    # 移除原先 "cycles_done>0 即 healthy" 的误判——那会让循环卡死却显示正常。
    trade_stale = bool(auto_running) and cycle_gap > 180

    # ★ 2026-08-08：账号失联必须让 status 说真话。
    #   事故当天 health 返回 {"status":"ok","mt5_connected":0} —— 进程活着、循环在转，
    #   于是判定为"健康"，可 4 个客户账号一个都没接上，一单也下不出去。
    #   监控绿灯 + 业务全停，是最危险的静默失败形态。
    #   判定用「已配置数 vs 实际连接数」：少一个都算 degraded（多租户下少的那个
    #   就是某位客户全天不交易）。
    #   刻意【不怕】触发重启：supervisor 只探端点可达性，明确不把 degraded 计入
    #   失败（见 supervisor.py:228）—— 这点很关键，否则会变成"连不上→重启→
    #   还连不上"的重启风暴，而重启根本治不好 MT5 连接。
    # ★ 2026-08-12 根治③：账号健康区分「后端存活」与「某账号 degraded」。
    #   旧逻辑用 _ACCOUNT_STATE["expected"]（含 is_trading_enabled=0 的关停账号、
    #   以及已熔断崩溃的账号）做全量 mt5_connected < accounts_expected 判定 →
    #   单账号（甚至被刻意关停/熔断的账号）掉线就拖垮整个系统报 degraded，
    #   前端 5s 心跳误报断连、全屏红条。
    #   新逻辑改用 mt5_service.get_account_health_summary()：
    #   - degraded 仅当「应交易(is_trading_enabled=1) 且未熔断」的账号有掉线；
    #   - 关停账号(is_trading_enabled=0) 进 non_trading_offline，熔断崩溃账号进 offline，
    #     二者只供前端单独告警，绝不让整体 status=degraded，避免误伤正常交易的其他客户。
    #   显示用 accounts_expected(全量)/mt5_connected 保持稳定，决策用 trading_* 系列。
    _offline_list = []
    _non_trading_offline_list = []
    try:
        from app.services.mt5_service import mt5_service as _mt5s
        _hs = _mt5s.get_account_health_summary()
        trading_expected = int(_hs.get("trading_expected", 0) or 0)
        trading_connected = int(_hs.get("trading_connected", 0) or 0)
        _offline_list = _hs.get("offline", []) or []
        _non_trading_offline_list = _hs.get("non_trading_offline", []) or []
    except Exception as _he:
        # 兜底：汇总失败时退化为「全量计数 == 已连接」做保守判定，不放大 degraded。
        logger.debug(f"[health] 账号健康汇总失败，回退保守判定: {_he}")
        trading_expected = int(_ACCOUNT_STATE.get("expected", 0) or 0)
        trading_connected = mt5_connected
    accounts_expected = int(_ACCOUNT_STATE.get("expected", 0) or 0)  # 显示用：全量已配置账号
    accounts_degraded = trading_expected > 0 and trading_connected < trading_expected

    # ★ 2026-08-08：DB 未就绪同样必须说真话。
    #   启动路径改为「快速认输 + 后台自愈」后，服务会在 DB 还没建好表时就先起来，
    #   这是刻意设计（避免 198s 阻塞触发 supervisor 强杀死循环）。
    #   但先起来 ≠ 可用：此时必须如实报 degraded，否则就是用另一种方式撒谎。
    db_ready = bool(_DB_STATE.get("ready", False))

    if uptime <= 0 or trade_stale or accounts_degraded or not db_ready:
        status = "degraded"
    else:
        status = "ok"

    # ★ Phase 0：人工封盘状态挂进健康检查，运维一眼能看出"交易停了是人停的还是坏了"。
    #   刻意【不改】status 取值：封盘是人为的已知状态、不是故障，
    #   把它算成 degraded 会让 supervisor 误判为异常而去重启进程。
    try:
        from app.services import emergency as _em
        _halt = _em.summary()
        _halt_info = {
            "any_halt": _halt["any_halt"],
            "global_level": _halt["global_level"],
            "halted_accounts": _halt["halted_accounts"],
        }
    except Exception as _he:
        _halt_info = {"any_halt": None, "error": str(_he)}

    return {
        "status": status,
        "emergency": _halt_info,
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        # ★ Phase 7.1：构建指纹随健康检查一起下发。
        #   前端拿它做「版本不一致黄条」——浏览器缓存住旧 bundle 时，
        #   __APP_VERSION__(构建期常量) 与这里的 version(运行期真值) 会对不上，
        #   前端据此提示用户硬刷，根治"改完没生效"的反复扯皮。
        "build": _build_info(),
        "uptime_sec": round(uptime, 1),
        "pid": _os.getpid(),
        "l3_guard_alive": l3_alive,
        "follower_alive": follower_alive,
        "mt5_connected": mt5_connected,
        # 已配置账号数 + 接入结论：让运维一眼看出"该在线几个、实际在线几个"，
        # 而不必自己去猜 mt5_connected=0 到底是没配账号还是全部失联。
        "accounts_expected": accounts_expected,
        "accounts_degraded": accounts_degraded,
        # ★ 2026-08-12 根治③：补充「应交易账号」与「离线账号」明细，
        #   前端据此单独告警而非整系统断连。
        "trading_expected": trading_expected,
        "trading_connected": trading_connected,
        "offline_accounts": _offline_list,
        "non_trading_offline_accounts": _non_trading_offline_list,
        "accounts_bootstrap": _ACCOUNT_STATE.get("bootstrap", ""),
        # DB 就绪状态：启动期快速认输后靠后台自愈补齐，运维需要看得见这个过程
        "db_ready": db_ready,
        "db_detail": _DB_STATE.get("detail", ""),
        "auto_loop_running": auto_running,
        "last_cycle_sec": round(cycle_gap, 1),
        "trade_stale": trade_stale,
        # ★ 2026-08-15 A/B 实验：暴露裁决层各方向门触发率（含辩论环缩权），
        #   供实盘 walk-forward 监控脚本采集。纯只读、零运行时影响、不在前端展示。
        "gate_stats": _safe_gate_stats(),
        # ★ 2026-08-15 A/B 实验：暴露辩论环实盘加载态（控制脚本翻转开关后，
        #   监控脚本据此确认后端确实加载了目标模式）。纯只读、零运行时影响。
        "debate_ring_enabled": bool(getattr(settings, "DEBATE_RING_ENABLED", False)),
    }


@app.get("/api/info")
def system_info():
    """系统信息"""
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "symbol": settings.SYMBOL,
        "ai_mode": "dual",  # DeepSeek V4 + 混元 Hy3
        "decision_interval": settings.AI_DECISION_INTERVAL,
        "debate_rounds": settings.AI_DEBATE_ROUNDS,
    }


# 前端静态文件（从 backend/app/main.py 往上到 WanxiangAI 再进 frontend/dist）
# 通过 FRONTEND_DIST_DIR 切换目录（默认 dist；如被 Defender 锁可临时切 dist_new）
# 优先从 settings 读取（.env 支持），其次从 OS 环境变量读取
_default_frontend_dir = Path(__file__).parent.parent.parent / "frontend" / "dist"
_alt_frontend_dir = Path(__file__).parent.parent.parent / "frontend" / "dist_new"
_frontend_dir_name = (settings.FRONTEND_DIST_DIR or os.environ.get("FRONTEND_DIST_DIR", "dist")).strip()
_frontend_dir = Path(__file__).parent.parent.parent / "frontend" / _frontend_dir_name
if not _frontend_dir.exists() and _alt_frontend_dir.exists():
    _frontend_dir = _alt_frontend_dir
if not _frontend_dir.exists() and _default_frontend_dir.exists():
    _frontend_dir = _default_frontend_dir
frontend_path = _frontend_dir

if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
    logger.info(f"  前端静态文件: {frontend_path}")
else:
    logger.warning(f"  前端文件未找到: {frontend_path}")


# 根路径 fallback：直接读取 index.html 内容返回
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_index():
    """返回前端面板（SPA 入口强制 no-cache）

    ★ 关键：index.html 入口禁止浏览器长缓存，否则用户缓存了旧 index.html →
    其引用的旧 JS 文件名（如 index-CdZiCwzz.js）已被 deploy.py 清理 → 请求 404 → 白屏。
    哈希化资源（/assets/index-*.js|css）文件名随内容变，URL 即版本，可放心长缓存，
    由 StaticFiles 自行处理，不受影响。
    """
    index_html = frontend_path / "index.html"
    if index_html.exists():
        _content = index_html.read_text(encoding="utf-8")
    else:
        _content = '<html><body><h2>XAU/USD万象Ai自动量化交易系统</h2><p>后端服务运行中</p><p><a href="/docs">API文档</a></p></body></html>'
    return HTMLResponse(
        content=_content,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ========== 直接运行入口 ==========
if __name__ == "__main__":
    import uvicorn

    def open_browser():
        """延迟打开浏览器"""
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://{settings.HOST}:{settings.PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level="info",
    )
