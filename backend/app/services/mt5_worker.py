"""
MT5 Worker 子进程 — 每个账号独立进程，维持独立的 mt5.initialize()

协议（JSON，通过 multiprocessing.Connection）：
  主→Worker: {"cmd": "...", "args": {...}}
  Worker→主: {"ok": true, "data": {...}} 或 {"ok": false, "error": "..."}

支持命令:
  - ping               → 心跳检测
  - get_account_info   → 返回账户信息
  - get_positions      → 返回持仓列表（args: symbol）
  - get_market_data    → 返回原始行情数据（args: symbol）→ 供 MarketAnalyzer 使用
  - place_order        → 下单（args: symbol, order_type, volume, price, sl, tp, comment）
  - close_position     → 平仓（args: ticket, volume）
  - shutdown           → 断开 MT5 并退出
"""

import sys
import os
import json
import traceback
import logging
from datetime import datetime
import time
from multiprocessing.connection import Connection

import MetaTrader5 as mt5

logger = logging.getLogger("mt5_worker")

# ── 2026-08-16 下单幂等键（_safe_send 重连重发去重）────────────────────────
# 背景：主进程 _safe_send 对「Worker 已成交但响应前断管」会重连重发原命令，
#   若无幂等键 → 重复开仓（双倍敞口）/ 重复平仓（多余调用）。
#   req_id 由调用方生成随命令传递；本 Worker 将已执行 req_id 持久化到
#   data/exec_req_ids.json（进程重启不丢），收到同 req_id 命令 → 返回上次结果
#   摘要（幂等语义：不重复执行）。限 _EXEC_REQ_MAX 条 LRU 防膨胀。
_EXEC_REQ_MEM: dict = {}
_EXEC_REQ_FILE: str = ""
_EXEC_REQ_MAX = 500


def _exec_req_file_path(account_id: str = "") -> str:
    # ★ 2026-08-17 P1修复（审计）：原单文件被所有 Worker 进程共享写、无锁，
    #   后写覆盖先写 → Worker 重启后幂等去重失效（断管重发双倍开平仓）。
    #   改为按账号分文件（account_id=MT5 login），各 Worker 只写自己的文件。
    _fname = f"exec_req_ids_{account_id}.json" if account_id else "exec_req_ids.json"
    try:
        from runtime_paths import data_dir
        return str(data_dir(create=True) / _fname)
    except Exception:
        _d = os.environ.get("DATA_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data")
        try:
            os.makedirs(_d, exist_ok=True)
        except Exception:
            pass
        return os.path.join(_d, _fname)


def _load_exec_reqs(account_id: str = "") -> None:
    global _EXEC_REQ_FILE, _EXEC_REQ_MEM
    _EXEC_REQ_FILE = _exec_req_file_path(account_id)
    try:
        if os.path.exists(_EXEC_REQ_FILE):
            with open(_EXEC_REQ_FILE, "r", encoding="utf-8") as f:
                _raw = json.load(f)
            if isinstance(_raw, dict):
                _EXEC_REQ_MEM = _raw
    except Exception:
        _EXEC_REQ_MEM = {}


def _check_dup_req(req_id: str):
    """返回已执行记录的 ticket（None=未执行过）。"""
    if not req_id:
        return None
    _r = _EXEC_REQ_MEM.get(req_id)
    return _r.get("ticket") if isinstance(_r, dict) else None


def _mark_exec_req(req_id: str, cmd: str, ticket, **extra) -> None:
    """记录已执行请求（幂等键）。extra 可带成交快照（volume/close_price/profit/net_profit），
    duplicate 重发时回放首次成交的真实数据——★ 2026-08-16 审计P0-3修复：
    旧实现只存 {ts,cmd,ticket}，partial 平仓成交后回执丢失（断管）→ 重发 duplicate 返回
    全 0 → 记账跳过 → 已实现盈亏永久丢失。现在存快照，duplicate 能回放真实盈亏。"""
    if not req_id:
        return
    _rec = {"ts": time.time(), "cmd": cmd, "ticket": ticket}
    if extra:
        _rec.update(extra)
    _EXEC_REQ_MEM[req_id] = _rec
    if len(_EXEC_REQ_MEM) > _EXEC_REQ_MAX:
        _old = sorted(_EXEC_REQ_MEM, key=lambda k: (_EXEC_REQ_MEM[k] or {}).get("ts", 0))
        for _k in _old[: len(_EXEC_REQ_MEM) - _EXEC_REQ_MAX]:
            _EXEC_REQ_MEM.pop(_k, None)
    try:
        if _EXEC_REQ_FILE:
            with open(_EXEC_REQ_FILE, "w", encoding="utf-8") as f:
                json.dump(_EXEC_REQ_MEM, f, ensure_ascii=False)
    except Exception:
        pass


def _dup_exec_record(req_id: str):
    """返回已执行请求的完整记录 dict（None=未执行过）。★ P0-3 修复用：duplicate 回放成交快照。"""
    if not req_id:
        return None
    _r = _EXEC_REQ_MEM.get(req_id)
    return _r if isinstance(_r, dict) else None


def _symbol_select_safe(symbol: str) -> bool:
    """安全选择品种，失败只记日志不中断"""
    try:
        return mt5.symbol_select(symbol, True)
    except Exception:
        return False


def _sanitize_comment(comment) -> str:
    """
    净化下单 comment，规避 MT5 终端 'Invalid comment argument' 拒单：
      - 强制转字符串
      - 去除控制字符/不可打印字符
      - 截断到 20 字符（MT5 comment 有效上限约 29，留足余量杜绝超长拒单）
    防御性兜底：无论上游传什么 comment，最终都给终端一个安全字符串。
    """
    if comment is None:
        return "WanxiangAI"
    s = str(comment)
    # 仅保留可打印 ASCII（字母/数字/常见标点），其余丢弃
    s = "".join(ch for ch in s if 32 <= ord(ch) < 127)
    s = s.strip()
    if not s:
        s = "WanxiangAI"
    return s[:20]


def _build_init_kwargs(connect_params: dict) -> dict:
    """从 connect_params 构造 mt5.initialize 入参（login/password/server/path）。

    抽成独立函数，供 worker_main 初始化与 _reconnect_mt5 重连共用，避免重复拼接。
    ★ P0-2 根因修复：此前该函数实现仅作为 _sanitize_comment 中 return 之后的死代码存在，
      导致 _reconnect_mt5 在真正需要重连时抛 NameError，MT5 会话断裂后无法自愈，
      交易系统假死。现提为独立函数。
    """
    init_kwargs = {
        "login": int(connect_params["login"]),
        "password": connect_params["password"],
        "server": connect_params["server"],
    }
    if connect_params.get("path"):
        init_kwargs["path"] = connect_params["path"]
    return init_kwargs


def _reconnect_mt5(connect_params: dict) -> bool:
    """
    交易服务器掉线后重建 MT5 会话（shutdown → initialize → login）。
    返回是否重连成功。这是应对 demo 终端交易连接不稳定（order_send 返回 None）的
    自愈手段，避免 Worker 长期空转无法下单。

    扩展（2026-08-05 运维）：若 mt5.initialize 失败且配置了终端路径，说明终端进程
    本身可能已异常退出（崩溃/被杀），此时先调用 ensure_terminal 把终端重新拉起来，
    再重试 initialize —— 实现 Worker 级"终端异常退出"秒级自愈，不必等父进程 45s
    重生 Worker（更快恢复交易，且不断开既有会话上下文）。
    """
    def _do_init():
        try:
            return mt5.initialize(**_build_init_kwargs(connect_params))
        except Exception as _e:
            logger.warning(f"[Worker] 重连 initialize 异常: {_e}")
            return False

    try:
        mt5.shutdown()
    except Exception:
        pass
    time.sleep(1.0)
    ok = _do_init()
    if not ok and connect_params.get("path"):
        # 终端进程可能已死（无法附着），重启终端后再试一次
        logger.warning("[Worker] initialize 失败，尝试先重启终端进程再重连...")
        try:
            from app.services.mt5_launcher import ensure_terminal
            ensure_terminal(
                terminal_path=connect_params["path"],
                login=str(connect_params.get("login", "")),
                password=str(connect_params.get("password", "")),
                server=str(connect_params.get("server", "")),
                tag=str(connect_params.get("login", "")),
            )
        except Exception as _le:
            logger.warning(f"[Worker] 重启终端失败（忽略，下个周期再试）: {_le}")
        time.sleep(2.0)
        ok = _do_init()
    if not ok:
        logger.warning(f"[Worker] 重连 initialize 失败: {mt5.last_error()}")
        return False
    # 校验交易会话确实可用
    info = mt5.account_info()
    if info is None:
        logger.warning(f"[Worker] 重连后 account_info 仍为空: {mt5.last_error()}")
        return False
    logger.info(f"[Worker] 重连成功 login={info.login} balance={info.balance}")
    return True


# ── 连接自愈状态（每个 Worker 是独立 spawn 进程，模块全局量各进程独占，安全）──
_RECONNECT_COOLDOWN = 8.0   # 断线后最小重连间隔（秒），防抖避免反复 initialize 打挂终端
_last_reconnect_try = 0.0


def _mt5_healthy() -> bool:
    """快速判定 MT5 会话是否可用：终端已连 + 账户信息可取。
    MT5 升级/重启后会话断裂，terminal_info().connected=False 或 account_info()=None，
    此时 mt5.* 调用会返回 None / 抛 (-10001, 'IPC send failed')。"""
    try:
        ti = mt5.terminal_info()
        if ti is None or not getattr(ti, "connected", False):
            return False
        if mt5.account_info() is None:
            return False
        return True
    except Exception:
        return False


def _ensure_connected(connect_params: dict) -> bool:
    """命令前自检：MT5 会话断了就立即重连（冷却防抖）。返回当前是否已连。

    根因修复（2026-08-04）：此前 MT5 升级重启终端后，Worker 进程仍活着但内部
    MT5 会话已死，只读指令返回 'IPC send failed'(-10001) 却未触发任何重连，
    导致整个交易系统假死约 12 分钟。现改为每个命令前主动体检，断开即秒级自愈。
    """
    global _last_reconnect_try
    if _mt5_healthy():
        return True
    now = time.time()
    if (now - _last_reconnect_try) < _RECONNECT_COOLDOWN:
        return False
    _last_reconnect_try = now
    logger.warning("[Worker] 检测到 MT5 会话断开，尝试自愈重连...")
    ok = _reconnect_mt5(connect_params)
    if ok:
        logger.info("[Worker] 自愈重连成功，恢复交易")
    else:
        logger.warning("[Worker] 自愈重连失败（终端可能仍在升级/未就绪），下个周期重试")
    return ok


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    """返回 year 年 month 月第 n 个 weekday（0=周一..6=周日）的 date"""
    from datetime import date, timedelta
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    first = d + timedelta(days=offset)
    return first + timedelta(days=7 * (n - 1))


def _star_trader_offset(dt_utc) -> int:
    """
    星迈(MT5)服务器 UTC 偏移：
      冬令 GMT+2 / 夏令 GMT+3（跟随美国 DST：3月第2周日 02:00 UTC ~ 11月第1周日 02:00 UTC）
    入参 dt_utc 为无时区 datetime（视为 UTC）。
    """
    from datetime import datetime
    year = dt_utc.year
    dst_start = _nth_weekday(year, 3, 6, 2)   # 3月第2周日
    dst_end = _nth_weekday(year, 11, 6, 1)    # 11月第1周日
    start_dt = datetime(year, dst_start.month, dst_start.day, 2, 0)
    end_dt = datetime(year, dst_end.month, dst_end.day, 2, 0)
    if start_dt <= dt_utc < end_dt:
        return 3
    return 2


def _mt5_position_open_time(p_time) -> str:
    """★ 时区修正（2026-08-10 根治「持仓 open_time 比真实开仓快 3h → holding_minutes 为负」显示 bug）：

    MT5 的 position.time 是星迈**服务器墙钟秒数**(UTC+offset)，并非纯 POSIX UTC；
    若直接 ``datetime.fromtimestamp(p.time)`` 按本机 UTC+8 解释，会凭空多出 offset 小时，
    导致前端持仓时长(holding_minutes = now - open_time)算出负数、显示成「负持仓时长」。

    这里先减服务器偏移还原为绝对 UTC 秒数，再按本机时区展示，
    与 trades 表 ``datetime.now()`` 记录的 open_time 保持同一基准。
    """
    off = _star_trader_offset(datetime.utcnow())
    abs_utc = int(p_time) - int(off) * 3600
    return datetime.fromtimestamp(abs_utc).isoformat()


def worker_main(conn: Connection, connect_params: dict):
    """
    Worker 进程入口。
    
    connect_params 包含:
      - login: int       MT5 账号
      - password: str    明文密码
      - server: str      服务器
      - path: str        terminal64.exe 路径（可选）
    """
    try:
        # ── 0. 确保终端已启动且算法交易开关为开 ──
        # MetaTrader5 Python 包只能附着到已运行的终端，无法改其 Algo Trading 开关；
        # 该开关关闭时 order_send 会返回 retcode=10027。
        # 因此若终端未运行，这里用 /config: 启动配置（AllowLiveTrading=1）把它拉起来。
        if connect_params.get("path"):
            try:
                from app.services.mt5_launcher import ensure_terminal
                ensure_terminal(
                    terminal_path=connect_params["path"],
                    login=str(connect_params["login"]),
                    password=connect_params["password"],
                    server=connect_params["server"],
                    tag=str(connect_params["login"]),
                )
            except Exception:
                pass  # 启动器失败不阻断，交给 initialize 自行尝试

        # ── 1. 建立 MT5 连接（带重试，给 terminal 完成登录/同步留出时间） ──
        init_kwargs = {
            "login": int(connect_params["login"]),
            "password": connect_params["password"],
            "server": connect_params["server"],
        }
        if connect_params.get("path"):
            init_kwargs["path"] = connect_params["path"]

        # 2026-08-09：刚启动的 terminal 可能还在登录/同步行情，mt5.initialize 立刻
        # 调用会返回 (-10001) IPC send failed。这里循环重试最多 90s，而不是立即失败。
        init_deadline = time.time() + 90.0
        result = False
        last_error = None
        while time.time() < init_deadline:
            try:
                result = mt5.initialize(**init_kwargs)
            except Exception as _e:
                result = False
                last_error = (0, str(_e))
            if result:
                break
            last_error = mt5.last_error()
            # 可恢复错误才重试；永久错误（如密码错）立即失败
            # ★ 2026-08-16 根因修复（liumanchuan2 启动 55 次循环）：原集合仅
            #   {-10001,-10003,-3}，孤儿终端清理后冷启动期间 initialize 会返回
            #   其他瞬时错误码（-2 内部/-1 未知/0 空错误）→ 立即 break → Worker
            #   7 秒即退 → _spawn 判失败 → 重连循环直到终端就绪（实测 20 分钟 55 次）。
            #   现扩为「非凭据类全部重试」：仅 -4 无法连服务器 / -5 账户未找到 /
            #   -6 无效账户 这类重试无意义的凭据错误才立即失败。
            recoverable = {
                -10001,  # IPC send failed
                -10003,  # IPC initialize failed
                -3,      # 终端无响应类（部分版本）
                -2,      # 内部错误（冷启动期间终端未就绪常见）
                -1,      # 未知错误
                0,       # 空错误（last_error 未填充，冷启动常见）
            }
            err_code = last_error[0] if isinstance(last_error, (list, tuple)) else 0
            if err_code in (-4, -5, -6):
                # 凭据/账户类永久错误：重试无意义，立即失败（避免 90s 空转）
                break
            if err_code not in recoverable:
                break
            logger.warning(
                f"[Worker] MT5 初始化失败 {last_error}，5s 后重试 "
                f"(剩余 {init_deadline - time.time():.0f}s)"
            )
            time.sleep(5.0)

        if not result:
            error = last_error or mt5.last_error()
            error_msg = f"({error[0]}) {error[1]}" if error else "未知错误"
            conn.send({"ok": False, "error": f"MT5 初始化失败: {error_msg}"})
            conn.close()
            return

        # ── 2. 发送就绪信号 ──
        info = mt5.account_info()
        ready_data = {
            "login": info.login if info else connect_params["login"],
            "balance": info.balance if info else 0,
            "equity": info.equity if info else 0,
            "currency": info.currency if info else "",
        }
        conn.send({"ok": True, "data": ready_data, "event": "connected"})

        # ── 2.5 加载下单幂等键集合（进程重启不丢，防重连重发双倍执行）──
        _load_exec_reqs(str(connect_params.get("login", "")))

        # ── 3. 命令循环 ──
        while True:
            # 轮询等待命令（0.5 秒间隔，防止 CPU 空转）
            # ★ 2026-08-17 P0修复（审计·今日 Worker 反复断连根因）：
            #   conn.poll 在 try 外 + recv 只捕 EOFError——Windows 下管道句柄失效
            #   抛 OSError(WinError 6)，冒泡到外层 except → finally 清理 → 进程退出，
            #   主进程 _send_cmd 捕获 EOF → 判"连接断开"→ 45s 后重启 Worker
            #   → 所有 Worker 周期性重启（实盘观察的反复断连）。
            #   现 poll/recv 统一捕 (EOFError, OSError)：EOF=真断退出；OSError=瞬时
            #   句柄抖动，记日志后 continue（不退出，等下一轮 poll 自愈）。
            try:
                if not conn.poll(0.5):
                    continue
            except (EOFError, OSError) as _pe:
                if isinstance(_pe, EOFError):
                    break  # 管道已关闭，退出
                logger.warning(f"[Worker] poll 瞬时 OSError(忽略): {_pe}")
                continue

            try:
                cmd = conn.recv()
            except EOFError:
                break  # 管道已关闭，退出
            except OSError as _oe:
                logger.warning(f"[Worker] recv 瞬时 OSError(忽略，自愈): {_oe}")
                continue

            if not isinstance(cmd, dict):
                conn.send({"ok": False, "error": "无效命令格式"})
                continue

            cmd_type = cmd.get("cmd", "")

            # ── 断线自愈守卫：MT5 升级/重启/掉线后，命令前先确认会话可用 ──
            # 根因：此前 MT5 升级重启终端，Worker 进程仍活着但内部 MT5 会话已死，
            # 只读指令返回 'IPC send failed'(-10001) 却未触发任何重连，导致系统假死约12分钟。
            # 现改为每个命令前主动体检，断开即秒级自愈重连（带8s冷却防抖）。
            if cmd_type == "shutdown":
                conn.send({"ok": True, "data": {"shutdown": True}})
                break
            if not _ensure_connected(connect_params):
                conn.send({"ok": False, "error": "MT5 终端断开，正在自动重连恢复（升级/重启后会话断裂）"})
                continue

            # ----- ping -----
            # ★★ 2026-08-11 P0 修复：主循环包裹总 try/except
            #   根因：之前此 if/elif 链任意分支抛异常 → worker 进程崩溃退出
            #   → service 侧 IPC 拿到 BrokenPipe/EOF → 下次 IPC 偶尔返字符串
            #   → 触发 service 层 '_pd.get("deal")' 等抛 'str' object has no attribute 'get'。
            #   修复：包裹 try，任何命令异常转 {ok: False, error: ...} 返给 service。
            try:
                if cmd_type == "ping":
                    conn.send({"ok": True, "data": {"pong": True}})

                # ----- get_account_info -----
                elif cmd_type == "get_account_info":
                    info = mt5.account_info()
                    if info is None:
                        err = mt5.last_error()
                        conn.send({"ok": False, "error": f"无法获取账户信息: {err}"})
                    else:
                        conn.send({
                            "ok": True,
                            "data": {
                                "login": info.login,
                                "balance": info.balance,
                                "equity": info.equity,
                                "margin": info.margin,
                                "margin_free": info.margin_free,
                                "margin_level": info.margin_level,
                                "profit": info.profit,
                                "currency": info.currency,
                            }
                        })

                # ----- get_positions -----
                elif cmd_type == "get_positions":
                    symbol = cmd.get("args", {}).get("symbol", "XAUUSD")
                    # ★ 2026-08-06 修复"智能平仓只处理单订单"根因A：支持全持仓查询。
                    #   symbol=None 时 positions_get() 不带参数 → 返回该账号【全部品种】持仓，
                    #   解决"只按 XAUUSD 过滤导致其他品种/全持仓不可见"的盲区。
                    if symbol is None:
                        positions = mt5.positions_get()
                    else:
                        positions = mt5.positions_get(symbol=symbol)
                    if positions is None:
                        # ★★ 2026-08-11 P0 修复：None = MT5 会话未就绪/查询失败，
                        #   绝不是"真空仓"！原实现静默转 [] 返回 ok=True → 对账把
                        #   本地全部 open 单误判为"外部平仓"→ 幽灵 pending_verify
                        #   （实证：15:47 开 SELL，15:50 worker 冷启动时被判外部平仓）。
                        #   正确语义：查询失败必须返回 ok=False，让调用方走 fail-safe。
                        _pe = "unknown"
                        try:
                            _pe = mt5.last_error()
                        except Exception:
                            pass
                        logger.warning(f"[Worker] positions_get 返回 None（MT5 会话未就绪？last_error={_pe}）→ 返回失败，绝不冒充空仓")
                        conn.send({"ok": False, "error": f"positions_get 返回 None: {_pe}"})
                        continue
                    data = []
                    for p in positions:
                        data.append({
                            "ticket": p.ticket,
                            "symbol": p.symbol,
                            "type": "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                            "volume": p.volume,
                            "open_price": p.price_open,
                            "current_price": p.price_current,
                            "sl": p.sl,
                            "tp": p.tp,
                            "profit": p.profit,
                            "swap": p.swap,
                            "comment": p.comment,
                            "open_time": _mt5_position_open_time(p.time),
                        })
                    conn.send({"ok": True, "data": data})

                # ----- get_market_data -----
                elif cmd_type == "get_market_data":
                    symbol = cmd.get("args", {}).get("symbol", "XAUUSD")

                    if not _symbol_select_safe(symbol):
                        conn.send({"ok": False, "error": f"无法选择品种 {symbol}"})
                        continue

                    tick = mt5.symbol_info_tick(symbol)
                    sym_info = mt5.symbol_info(symbol)

                    if tick is None:
                        conn.send({"ok": False, "error": f"无法获取 {symbol} 报价"})
                        continue

                    # 当前报价
                    spread = 0
                    if sym_info:
                        spread = round((tick.ask - tick.bid) / sym_info.point, 1)

                    market_data = {
                        "symbol": symbol,
                        "timestamp": datetime.now().isoformat(),
                        "current": {
                            "bid": tick.bid,
                            "ask": tick.ask,
                            "last": tick.last,
                            "spread": spread,
                        },
                        "timeframes": {},
                        "symbol_info": {
                            "digits": sym_info.digits if sym_info else 0,
                            "point": sym_info.point if sym_info else 0.01,
                            "volume_step": sym_info.volume_step if sym_info else 0.01,
                            "trade_stops_level": sym_info.trade_stops_level if sym_info else 0,
                            "trade_freeze_level": sym_info.trade_freeze_level if sym_info else 0,
                        } if sym_info else {},
                    }

                    # 采集各时间框架 OHLCV
                    TF_MAP = {
                        "M1": mt5.TIMEFRAME_M1,
                        "M5": mt5.TIMEFRAME_M5,
                        "M15": mt5.TIMEFRAME_M15,
                        "M30": mt5.TIMEFRAME_M30,
                        "H1": mt5.TIMEFRAME_H1,
                        "H4": mt5.TIMEFRAME_H4,
                        "D1": mt5.TIMEFRAME_D1,
                    }
                    for tf_name, tf_enum in TF_MAP.items():
                        rates = None
                        for _attempt in range(3):
                            try:
                                rates = mt5.copy_rates_from_pos(symbol, tf_enum, 0, 200)
                            except Exception:
                                rates = None
                            if rates is not None and len(rates) > 0:
                                break
                            time.sleep(0.4)
                        if rates is not None and len(rates) > 0:
                            bars = []
                            # ★ 2026-08-16 审计P1-9修复：MT5 copy_rates 的 time 是服务器墙钟秒数
                            #   （星迈 GMT+2/+3），旧代码 fromtimestamp 按本机 GMT+8 解释 → K线
                            #   时间戳错 5h → 跨周期对齐/最新K线判断/视觉图时间轴全部错位。
                            #   与 _mt5_position_open_time（position.time 减 offset 还原）统一。
                            _bar_off = _star_trader_offset(datetime.utcnow())
                            for r in rates:
                                # MT5 新版包返回普通结构化 ndarray（元素为 numpy.void），
                                # 必须用字符串键访问；并用原生 float/int 包装以便 JSON 序列化
                                bars.append({
                                    "open": float(r["open"]),
                                    "high": float(r["high"]),
                                    "low": float(r["low"]),
                                    "close": float(r["close"]),
                                    "volume": float(r["tick_volume"]),
                                    "time": datetime.fromtimestamp(int(r["time"]) - int(_bar_off) * 3600).isoformat(),
                                })
                            market_data["timeframes"][tf_name] = {
                                "count": len(bars),
                                "bars": bars,
                            }
                        else:
                            market_data["timeframes"][tf_name] = {
                                "count": 0,
                                "bars": [],
                                "error": "数据不足",
                            }

                    conn.send({"ok": True, "data": market_data})

                # ----- get_server_info -----
                elif cmd_type == "get_server_info":
                    # 返回 MT5 服务器当前墙钟时间 + 品种交易时段（权威时区来源）
                    # 关键：tick.time 是 UTC 纪元秒；星迈服务器墙钟 = UTC + 偏移
                    #   冬令 GMT+2 / 夏令 GMT+3(跟随美国 DST: 3月第2周日~11月第1周日)
                    # 不能用 fromtimestamp()，那会得到本机本地时间(GMT+8)而非服务器墙钟
                    # ★ 2026-08-16 管理后台审计修复：整段防御性 try——
                    #   REAL 账号（詹启东/詹启东3）的 symbol_info_session_trade 间歇异常
                    #   （周末休市数据缺失），原未捕获会冒泡产生脏响应（service 侧收到
                    #   非 dict 形态 → 健康面板误报离线）。异常时发结构化错误，永不脏输出。
                    try:
                        import time as _time
                        from datetime import timezone as _tz, timedelta as _td
                        symbol = cmd.get("args", {}).get("symbol", "XAUUSD")

                        utc_now = datetime.utcnow()
                        server_offset = _star_trader_offset(utc_now)  # +2 或 +3
                        server_dt = utc_now.replace(tzinfo=_tz.utc) + _td(hours=server_offset)
                        server_dt = server_dt.replace(tzinfo=None)

                        # 采集 7 天交易时段（MT5: SUNDAY=0..SATURDAY=6，分钟数，已是服务器时间）
                        sessions = {}
                        if _symbol_select_safe(symbol):
                            for d in range(7):
                                try:
                                    sess = mt5.symbol_info_session_trade(symbol, d)
                                except Exception:
                                    sess = None
                                if sess:
                                    sessions[str(d)] = [[int(s[0]), int(s[1])] for s in sess]
                                else:
                                    sessions[str(d)] = []
                        # 未连上品种时给空，交由上层静态兜底

                        conn.send({
                            "ok": True,
                            "data": {
                                "server_time": server_dt.isoformat(),
                                "server_offset": server_offset,
                                "sessions": sessions,
                                "timezone_note": f"MT5 服务器墙钟（冬令 GMT+2 / 夏令 GMT+3，当前 GMT+{server_offset}）",
                            },
                        })
                    except Exception as _sie:
                        logger.warning(f"[Worker] get_server_info 异常(已防御): {_sie}")
                        conn.send({"ok": False, "error": f"get_server_info 内部异常: {str(_sie)[:120]}"})

                # ----- get_terminal_info -----
                elif cmd_type == "get_terminal_info":
                    info = mt5.terminal_info()
                    if info is None:
                        conn.send({"ok": False, "error": "无法获取终端信息"})
                        continue
                    d = info._asdict()
                    conn.send({
                        "ok": True,
                        "data": {
                            "name": d.get("name"),
                            "path": d.get("path"),
                            "data_path": d.get("data_path"),
                            "connected": d.get("connected"),
                            "trade_allowed": d.get("trade_allowed"),
                            "trade_allowed_expert": d.get("trade_allowed_expert"),
                            "algo_allowed": d.get("algo_allowed"),
                            "dll_allowed": d.get("dll_allowed"),
                            "tradeapi_disabled": d.get("tradeapi_disabled"),
                        },
                    })

                # ----- get_history_deals -----
                elif cmd_type == "get_history_deals":
                    args = cmd.get("args", {})
                    # 区间由调用方指定（service 层已按 server 时区切好今日/历史）
                    date_from = args.get("date_from")
                    date_end = args.get("date_end")
                    if date_from is None:
                        from datetime import timedelta
                        date_from = datetime.now() - timedelta(days=90)
                    if date_end is None:
                        date_end = datetime.now()
                    if isinstance(date_from, str):
                        date_from = datetime.fromisoformat(date_from)
                    if isinstance(date_end, str):
                        date_end = datetime.fromisoformat(date_end)
                    # 历史成交需在终端连上后从券商服务器同步，刚拉起时可能短暂为空。
                    # 若返回 None 或空，重试最多 2 次（每次等 1.5s），避免把"未同步完"误判成"无交易"。
                    deals = mt5.history_deals_get(date_from, date_end)
                    _retry = 0
                    while (deals is None or len(deals) == 0) and _retry < 2:
                        _retry += 1
                        import time as _t
                        _t.sleep(1.5)
                        deals = mt5.history_deals_get(date_from, date_end)
                    if deals is None:
                        conn.send({"ok": True, "data": {"deals": [], "total_profit": 0.0, "count": 0, "close_count": 0, "raw_count": -1}})
                        continue
                    result_deals = []
                    total_profit = 0.0
                    close_count = 0  # 平仓笔数（=已平仓交易数，与盈利同源、语义一致）
                    for d in deals:
                        dd = d._asdict()
                        dtype = dd.get("type")
                        # 只统计真实交易（买/卖），排除入金出金(BALANCE=2)、赠金、
                        # 利息、手续费、修正等资金操作——否则会把入金本金误算成盈利
                        if dtype not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                            continue
                        entry = int(dd.get("entry", 0) or 0)
                        profit = float(dd.get("profit", 0) or 0)
                        commission = float(dd.get("commission", 0) or 0)
                        swap = float(dd.get("swap", 0) or 0)
                        net = profit + commission + swap
                        total_profit += net
                        # entry: 0=建仓 1=平仓出场 2=反手(同时平仓+开仓) 3=强平
                        # 平仓出场/反手/强平 都算一笔已平仓交易
                        if entry in (1, 2, 3):
                            close_count += 1
                        result_deals.append({
                            "ticket": dd.get("ticket"),
                            "order": dd.get("order"),
                            "symbol": dd.get("symbol"),
                            "type": dd.get("type"),
                            "volume": dd.get("volume"),
                            "price": dd.get("price"),
                            "profit": profit,
                            "commission": commission,
                            "swap": swap,
                            "net_profit": round(net, 2),
                            "time": str(dd.get("time", "")),
                            "entry": dd.get("entry"),  # 0=建仓, 1=平仓出场, 2=反手, 3=强平
                            "position_id": dd.get("position_id"),
                            "magic": dd.get("magic"),   # AI 下单标记（20260802），用于区分 AI vs 手工
                            "comment": dd.get("comment"),
                        })
                    conn.send({
                        "ok": True,
                        "data": {
                            "deals": result_deals,
                            "total_profit": round(total_profit, 2),
                            "count": len(result_deals),
                            "close_count": close_count,
                            "raw_count": len(deals) if deals is not None else -1,
                        },
                    })

                # ----- get_history_orders -----
                # ★ 盈利统计主数据源：MT5终端"历史"标签页显示的是 Orders（订单），
                #   而非 Deals（成交）。模拟盘两者可能不同步——orders 更准、更及时。
                elif cmd_type == "get_history_orders":
                    args = cmd.get("args", {})
                    date_from = args.get("date_from")
                    date_end = args.get("date_end")
                    if date_from is None:
                        from datetime import timedelta as _td
                        date_from = datetime.now() - _td(days=90)
                    if date_end is None:
                        date_end = datetime.now()
                    if isinstance(date_from, str):
                        date_from = datetime.fromisoformat(date_from)
                    if isinstance(date_end, str):
                        date_end = datetime.fromisoformat(date_end)

                    orders = mt5.history_orders_get(date_from, date_end)
                    _retry = 0
                    while (orders is None or len(orders) == 0) and _retry < 2:
                        _retry += 1
                        import time as _t2
                        _t2.sleep(1.5)
                        orders = mt5.history_orders_get(date_from, date_end)
                    if orders is None:
                        conn.send({"ok": True, "data": {"orders": [], "total_profit": 0.0, "count": 0, "raw_count": -1}})
                        continue

                    result_orders = []
                    total_profit = 0.0
                    for o in orders:
                        oo = o._asdict()
                        otype = int(oo.get("type", -1))
                        ostate = int(oo.get("state", -1))
                        # 只统计已完成的交易订单（BUY/SELL），排除 BALANCE/修正等
                        # state=3(FILLED)=已完全成交（对已平仓订单意味着完整执行完毕）
                        if otype not in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL):
                            continue
                        # 只统计已完成的订单（有平仓时间=已完全处理）
                        profit = float(oo.get("profit", 0) or 0)
                        commission = float(oo.get("commission", 0) or 0)
                        swap = float(oo.get("swap", 0) or 0)
                        net = profit + commission + swap
                        total_profit += net
                        result_orders.append({
                            "ticket": int(oo.get("ticket", 0)),
                            "symbol": oo.get("symbol", ""),
                            "type": otype,
                            "volume_initial": float(oo.get("volume_initial", 0) or 0),
                            "price_open": float(oo.get("price_open", 0) or 0),
                            "price_close": float(oo.get("price_close", 0) or 0),
                            "profit": profit,
                            "commission": commission,
                            "swap": swap,
                            "net_profit": round(net, 2),
                            "time_setup": str(oo.get("time_setup", "")),
                            "time_done": str(oo.get("time_done", "")),
                            "state": ostate,
                            "comment": oo.get("comment", ""),
                        })
                    conn.send({
                        "ok": True,
                        "data": {
                            "orders": result_orders,
                            "total_profit": round(total_profit, 2),
                            "count": len(result_orders),
                            "raw_count": len(orders),
                        },
                    })

                # ----- get_recent_deals (诊断用：最近N笔原始deals) -----
                elif cmd_type == "get_recent_deals":
                    _DEAL_TYPE_NAME = {
                        0: "BUY", 1: "SELL", 2: "BALANCE", 3: "CREDIT",
                        4: "CHARGE", 5: "CORRECTION", 6: "BONUS", 7: "COMMISSION",
                    }
                    limit = cmd.get("args", {}).get("limit", 20)
                    from datetime import timedelta
                    # 拉最近30天，取最后N笔
                    deals = mt5.history_deals_get(datetime.now() - timedelta(days=30), datetime.now())
                    recent = []
                    if deals is not None:
                        for d in list(deals)[-limit:]:
                            dd = d._asdict()
                            recent.append({
                                "time": str(dd.get("time", "")),
                                "type": int(dd.get("type", -1)),
                                "type_name": _DEAL_TYPE_NAME.get(int(dd.get("type", -1)), f"UNKNOWN({dd.get('type')})"),
                                "entry": int(dd.get("entry", -1)),
                                "symbol": dd.get("symbol", ""),
                                "volume": float(dd.get("volume", 0) or 0),
                                "price": float(dd.get("price", 0) or 0),
                                "profit": float(dd.get("profit", 0) or 0),
                                "commission": float(dd.get("commission", 0) or 0),
                                "swap": float(dd.get("swap", 0) or 0),
                                # ★★ 2026-08-10 修复：补 position_id（对应持仓 ticket）——
                                #   此前缺失导致对账路径无法按 ticket 匹配外部平仓 deal，
                                #   mt5_closed_external 盈亏恒 0、开平价相同（盈亏丢失）。
                                "position_id": int(dd.get("position_id", 0) or 0),
                            })
                    conn.send({"ok": True, "data": {"recent": recent, "total_raw": len(deals) if deals is not None else 0}})
                    continue

                # ----- get_deal_by_position (按持仓ticket精准拉成交，2026-08-11) -----
                #   对账路径的核心修复：get_recent_deals(limit=500) 在 MT5 断连/重启后
                #   deals 历史可能部分丢失 → position_id 匹配不上 → profit=0 假 breakeven。
                #   这里用 mt5.history_deals_get(position=ticket) 直接按持仓号拉，
                #   不依赖缓存窗口，且只取【平仓 deal】（entry=1/3 带盈亏）。
                elif cmd_type == "get_deal_by_position":
                    args = cmd.get("args", {})
                    ticket = int(args.get("ticket", 0) or 0)
                    # ★ P1-#4：透传开仓价/方向，REAL 券商 deal.price=0 时反推真实平仓价
                    _deal_open_price = float(args.get("open_price") or 0) or 0
                    _deal_action = str(args.get("action") or "").upper()
                    from datetime import timedelta as _td2
                    import time as _t2
                    close_time = args.get("close_time")
                    # ★★★ 2026-08-12 P0 根治（第2版·时区错位，前 4 轮误判的真根因）★★★
                    #   history_deals_get(date_from, date_to) 的入参必须是【MT5 服务器墙钟】
                    #   （星迈 = UTC+2 冬令 / UTC+3 夏令），而 trades.close_time 由
                    #   datetime.now() 写入，是【本机本地时间 GMT+8】。
                    #   直接拿本地时间当窗口边界 → 偏差 (8-3)=5 小时 ≫ ±20min 窗口
                    #   → 查询【必然返回空集】。
                    #   而空集既不满足 `deals is None` 也不满足 `len(deals)>0`，
                    #   导致原有三条诊断日志全部被绕过 → 表现为「完全静默」，
                    #   排障时无法区分「函数未被调用」与「调用了但查不到」（实测误判 3 轮、
                    #   pending_verify 永久挂账 34 笔）。
                    #   反证：宽窗口(3天)当初能返回数据，正因 3 天足够大、5 小时偏移仍在窗口内，
                    #   只是无过滤宽窗口被截断成最早一批老 ID（377/376 开头）而匹配不上。
                    #   修复：naive 本地 close_time → epoch(绝对UTC) → +server_offset → 服务器墙钟。
                    _srv_off = _star_trader_offset(datetime.utcnow())
                    _df = None
                    _dt = None
                    _win_desc = ""
                    if close_time:
                        try:
                            if isinstance(close_time, (int, float)):
                                _epoch = float(close_time)
                            elif isinstance(close_time, str):
                                # naive 本地时间字符串 → timestamp() 按本机时区正确转 epoch
                                _epoch = datetime.fromisoformat(
                                    close_time.strip().replace("Z", "")[:19]).timestamp()
                            else:
                                _epoch = close_time.timestamp()   # datetime 对象（本地 naive）
                            # epoch(绝对 UTC) → MT5 服务器墙钟（不写死本机时区，可移植）
                            _ct_srv = datetime.utcfromtimestamp(_epoch + _srv_off * 3600)
                            # ±45min：覆盖 DST 边界误判与对账发现延迟，仍远小于会被截断的窗口
                            _df = _ct_srv - _td2(minutes=45)
                            _dt = _ct_srv + _td2(minutes=45)
                            _win_desc = f"srv窄窗{_df:%m-%d %H:%M}~{_dt:%H:%M}(GMT+{_srv_off})"
                        except Exception as _te:
                            _df = None
                            _dt = None
                            logger.warning(
                                f"[get_deal_by_position] ticket={ticket} close_time 解析失败"
                                f"({type(_te).__name__}: {_te}) → 转兜底窗口")
                    if _df is None:
                        _now_srv = datetime.utcnow() + _td2(hours=_srv_off)
                        _df = _now_srv - _td2(minutes=60)
                        _dt = _now_srv + _td2(minutes=5)
                        _win_desc = f"srv兜底窗{_df:%m-%d %H:%M}~{_dt:%H:%M}(GMT+{_srv_off})"
                    # ---- L1：position 过滤 + 服务器时间窄窗口（最精准） ----
                    _path = "L1"
                    try:
                        deals = mt5.history_deals_get(_df, _dt, position=ticket)
                    except Exception as _he:
                        logger.warning(
                            f"[get_deal_by_position] ticket={ticket} L1 position 过滤异常"
                            f"({type(_he).__name__}: {_he})")
                        deals = None
                    # ---- L2：position 过滤 + 宽窗口(近7天)，免疫时区/偏移误算 ----
                    #   关键：带 position= 过滤时结果集只有该单的 1~3 笔，【不存在截断问题】。
                    #   截断只发生在「无过滤的宽窗口」（返回上万笔被裁成最早 N 笔）。
                    #   故此级既宽容又精准，是时区换算万一出错时的强兜底。
                    if deals is None or len(deals) == 0:
                        _path = "L2"
                        try:
                            _srv_now = datetime.utcnow() + _td2(hours=_srv_off)
                            deals = mt5.history_deals_get(
                                _srv_now - _td2(days=7), _srv_now + _td2(days=1), position=ticket)
                        except Exception as _he2:
                            logger.warning(
                                f"[get_deal_by_position] ticket={ticket} L2 宽窗口 position 异常"
                                f"({type(_he2).__name__}: {_he2})")
                            deals = None
                    # ---- L3：无过滤 + 服务器时间窄窗口，手动按 position_id 匹配 ----
                    if deals is None or len(deals) == 0:
                        _path = "L3"
                        try:
                            deals = mt5.history_deals_get(_df, _dt)
                        except Exception:
                            deals = None
                    if deals is None:
                        logger.warning(
                            f"[get_deal_by_position] ticket={ticket} 历史成交同步失败(deals=None) "
                            f"path={_path} {_win_desc}")
                    elif len(deals) == 0:
                        # ★ 补上原先完全静默的空集分支：必须留痕。
                        #   否则排障无法区分「未调用」与「调用了但查不到」（本次误判 3 轮的直接原因）。
                        logger.warning(
                            f"[get_deal_by_position] ticket={ticket} 三级查询均空集 "
                            f"path={_path} {_win_desc} → 保持 pending_verify")
                    _out_deal = None
                    _found = 0
                    _seen_ids = []
                    if deals is not None:
                        for d in deals:
                            _found += 1
                            dd = d._asdict()
                            _pid = int(dd.get("position_id") or 0)
                            _pby = int(dd.get("position_by_id") or 0)
                            if _pid != ticket and _pby != ticket:
                                if len(_seen_ids) < 5:
                                    _seen_ids.append(f"pid={_pid}/pby={_pby}")
                                continue
                            _entry = int(dd.get("entry", -1))
                            _profit = float(dd.get("profit", 0) or 0)
                            # 平仓 deal（entry=1 平仓出场 / 3 强平）优先；反手(2)也带盈亏
                            if _entry in (1, 3) or (_entry == 2 and _profit != 0):
                                _deal_price = float(dd.get("price", 0) or 0)
                                # ★ P1-#4 REAL 券商平仓价 price=0 缺回退：REAL 账户 deal.price
                                #   返回 0（DEMO 正常），但 profit 真实。用 open_price + 盈亏/手数
                                #   反推真实平仓价，避免下游把 close_price 记 0（价格失真污染归因/可视化）。
                                if _deal_price <= 0 and _profit != 0 and _deal_open_price > 0:
                                    _deal_vol = float(dd.get("volume", 0) or 0)
                                    if _deal_vol > 0:
                                        _deal_move = _profit / (_deal_vol * 100.0)
                                        _deal_price = round(
                                            (_deal_open_price + _deal_move) if _deal_action == "BUY"
                                            else (_deal_open_price - _deal_move), 2)
                                _out_deal = {
                                    "position_id": ticket,
                                    "entry": _entry,
                                    "price": _deal_price,
                                    "profit": round(_profit, 2),
                                    # ★ 2026-08-15 复检P1修复：合并 net_profit（含佣/隔夜），
                                    #   与 recent-deals 路径口径一致——原缺该字段，对账兜底/
                                    #   pending_verify 回填取 `net_profit or profit` 回退裸 profit，
                                    #   佣金全部丢失（铁律①在两条路径仍被凿穿）。
                                    "net_profit": round(
                                        _profit + float(dd.get("commission", 0) or 0)
                                        + float(dd.get("swap", 0) or 0), 2),
                                    "commission": float(dd.get("commission", 0) or 0),
                                    "swap": float(dd.get("swap", 0) or 0),
                                    "volume": float(dd.get("volume", 0) or 0),
                                    "time": str(dd.get("time", "")),
                                    "found": _found,
                                }
                    if _out_deal is None and deals is not None and len(deals) > 0:
                        logger.warning(
                            f"[get_deal_by_position] ticket={ticket} path={_path} 拉到 {len(deals)} 笔成交但"
                            f"未匹配平仓deal（position_id/by_id 样本: {_seen_ids}）{_win_desc}")
                    elif _out_deal is not None:
                        # ★ 成功留痕：确认三级查询哪一级命中，便于验证时区修复是否生效
                        logger.info(
                            f"[get_deal_by_position] ticket={ticket} path={_path} 命中平仓deal "
                            f"price={_out_deal['price']} profit={_out_deal['profit']} {_win_desc}")
                    conn.send({
                        "ok": True,
                        "data": {"ticket": ticket, "deal": _out_deal, "found": _found}
                    })
                    continue

                # ----- get_recent_orders (诊断用：最近N笔原始orders) -----
                elif cmd_type == "get_recent_orders":
                    _ORDER_TYPE_NAME = {
                        0: "BUY", 1: "SELL", 2: "BUYLIMIT", 3: "SELLLIMIT",
                        4: "BUYSTOP", 5: "SELLSTOP", 6: "BALANCE", 7: "CREDIT",
                    }
                    limit = cmd.get("args", {}).get("limit", 20)
                    from datetime import timedelta as _td
                    orders = mt5.history_orders_get(datetime.now() - _td(days=30), datetime.now())
                    recent = []
                    if orders is not None:
                        for o in list(orders)[-limit:]:
                            oo = o._asdict()
                            recent.append({
                                "time_setup": str(oo.get("time_setup", "")),
                                "time_done": str(oo.get("time_done", "")),
                                "order": int(oo.get("ticket", 0)),
                                "type": int(oo.get("type", -1)),
                                "type_name": _ORDER_TYPE_NAME.get(int(oo.get("type", -1)), f"ORD_{oo.get('type')}"),
                                "state": int(oo.get("state", -1)),
                                "symbol": oo.get("symbol", ""),
                                "volume_initial": float(oo.get("volume_initial", 0) or 0),
                                "price_open": float(oo.get("price_open", 0) or 0),
                                "price_close": float(oo.get("price_close", 0) or 0),
                                "profit": float(oo.get("profit", 0) or 0),
                                "sl": float(oo.get("sl", 0) or 0),
                                "tp": float(oo.get("tp", 0) or 0),
                                "comment": oo.get("comment", ""),
                            })
                    conn.send({"ok": True, "data": {"recent": recent, "total_raw": len(orders) if orders is not None else 0}})
                    continue

                # ----- place_order -----
                elif cmd_type == "place_order":
                    args = cmd.get("args", {})
                    # ★ 2026-08-16 下单幂等键：同 req_id 已执行过 → 不重复开仓，
                    #   直接返回上次结果摘要（调用方 _safe_send 重连重发场景）。
                    _req = str(cmd.get("req_id") or "")
                    _dup_ticket = _check_dup_req(_req)
                    if _dup_ticket is not None:
                        logger.info(
                            f"[Worker] 幂等去重：place_order req_id={_req[:12]}… "
                            f"已执行(ticket={_dup_ticket}) → 直接返回，不重复开仓"
                        )
                        # 回填真实成交价/手数（调用方首次写账需要真实值，避免 0 污染账本）
                        _dup_price = 0.0
                        _dup_vol = 0.0
                        try:
                            _pp = mt5.positions_get(ticket=int(_dup_ticket))
                            if _pp and len(_pp) > 0:
                                _dup_price = float(_pp[0].price_open or 0)
                                _dup_vol = float(_pp[0].volume or 0)
                        except Exception:
                            pass
                        conn.send({
                            "ok": True,
                            "data": {"duplicate": True, "ticket": _dup_ticket,
                                     "volume": _dup_vol, "price": _dup_price,
                                     "type": args.get("order_type", "BUY")},
                        })
                        continue
                    symbol = args.get("symbol", "XAUUSD")
                    order_type = args.get("order_type", "BUY")
                    volume = float(args.get("volume", 0.01))
                    sl = float(args.get("sl", 0))
                    tp = float(args.get("tp", 0))
                    comment = _sanitize_comment(args.get("comment", "WanxiangAI"))

                    # 选择品种
                    if not _symbol_select_safe(symbol):
                        conn.send({"ok": False, "error": f"无法选择品种 {symbol}"})
                        continue

                    symbol_info = mt5.symbol_info(symbol)
                    if symbol_info is None:
                        conn.send({"ok": False, "error": f"品种 {symbol} 信息获取失败"})
                        continue

                    # 手数归一化
                    step = symbol_info.volume_step
                    volume = round(volume / step) * step
                    volume = max(symbol_info.volume_min, min(volume, symbol_info.volume_max))

                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        conn.send({"ok": False, "error": f"无法获取 {symbol} 报价"})
                        continue

                    is_buy = order_type.upper() == "BUY"
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": volume,
                        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                        "price": tick.ask if is_buy else tick.bid,
                        "sl": sl,
                        "tp": tp,
                        "deviation": 20,
                        "magic": 20260802,
                        "comment": comment,
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }

                    result = mt5.order_send(request)
                    if result is None:
                        # 交易服务器连接可能已掉线（demo 终端常见）：重连 MT5 会话后重试一次
                        try:
                            _le = mt5.last_error()
                        except Exception:
                            _le = "unknown"
                        logger.warning(f"[Worker] order_send 返回 None，last_error={_le}，尝试重连后重试 symbol={symbol} vol={volume}")
                        if _reconnect_mt5(connect_params):
                            time.sleep(0.5)
                            result = mt5.order_send(request)
                        if result is None:
                            try:
                                _le2 = mt5.last_error()
                            except Exception:
                                _le2 = "unknown"
                            logger.warning(f"[Worker] 重连后仍 order_send 返回 None，last_error={_le2}")
                            conn.send({"ok": False, "error": f"MT5 order_send 返回空（重连后仍失败）last_error={_le2}"})
                            continue
                    elif result.retcode != mt5.TRADE_RETCODE_DONE:
                        conn.send({
                            "ok": False,
                            "error": f"下单失败: retcode={result.retcode}, {result.comment}",
                            "data": {"retcode": result.retcode}
                        })
                    else:
                        # ★ 2026-08-11 修复（P0 真实账号假巨亏）：部分 REAL 券商
                        #   order_send 返回的 result.price 恒为 0，导致跟单开仓价记 0、
                        #   平仓盈亏被算成 -4394 假巨亏。改用 positions_get(ticket).price_open
                        #   取真实开仓价回填；DEMO/正常券商仍走 result.price。
                        _fill_price = float(result.price or 0)
                        if _fill_price <= 0:
                            try:
                                _p = mt5.positions_get(ticket=result.order)
                                if _p and len(_p) > 0:
                                    _fill_price = float(_p[0].price_open or 0)
                            except Exception:
                                pass
                        # ★ 2026-08-16 下单幂等键：成交后立即记录 req_id（防重连重发双倍开仓）
                        _mark_exec_req(_req, "place_order", result.order)
                        conn.send({
                            "ok": True,
                            "data": {
                                "ticket": result.order,
                                "volume": result.volume,
                                "price": _fill_price,
                                "type": order_type,
                            }
                        })

                # ----- close_position -----
                elif cmd_type == "close_position":
                    args = cmd.get("args", {})
                    # ★ 2026-08-16 下单幂等键：同 req_id 已执行过 → 不重复平仓
                    _req = str(cmd.get("req_id") or "")
                    _dup_ticket = _check_dup_req(_req)
                    if _dup_ticket is not None:
                        logger.info(
                            f"[Worker] 幂等去重：close_position req_id={_req[:12]}… "
                            f"已执行(ticket={_dup_ticket}) → 直接返回，不重复平仓"
                        )
                        # ★ 2026-08-16 审计P0-3修复：duplicate 回放首次成交快照，
                        #   而非全 0 —— 否则 partial 平仓成交后回执丢失 → 记账跳过 →
                        #   已实现盈亏永久丢失且 _PARTIAL_DONE 已占坑不可自愈。
                        _rec = _dup_exec_record(_req) or {}
                        conn.send({
                            "ok": True,
                            "data": {
                                "duplicate": True,
                                "ticket": _dup_ticket,
                                "volume": float(_rec.get("volume") or 0.0),
                                "close_price": float(_rec.get("close_price") or 0.0),
                                "profit": float(_rec.get("profit") or 0.0),
                                "net_profit": float(_rec.get("net_profit") or 0.0),
                            },
                        })
                        continue
                    ticket = int(args.get("ticket", 0))
                    vol = float(args.get("volume", 0))

                    position = mt5.positions_get(ticket=ticket)
                    if position is None or len(position) == 0:
                        conn.send({"ok": False, "error": f"持仓 {ticket} 不存在"})
                        continue

                    pos = position[0]
                    close_volume = vol if vol > 0 else pos.volume
                    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

                    tick = mt5.symbol_info_tick(pos.symbol)
                    if tick is None:
                        conn.send({"ok": False, "error": f"无法获取 {pos.symbol} 报价"})
                        continue

                    close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": pos.symbol,
                        "volume": close_volume,
                        "type": close_type,
                        "position": ticket,
                        "price": close_price,
                        "deviation": 20,
                        "magic": 20260802,
                        "comment": "WanxiangAI_Close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }

                    result = mt5.order_send(request)
                    if result is None:
                        # 交易服务器连接可能已掉线（demo 终端常见）：重连 MT5 会话后重试一次
                        try:
                            _le = mt5.last_error()
                        except Exception:
                            _le = "unknown"
                        logger.warning(
                            f"[Worker] 平仓 order_send 返回 None，last_error={_le}，"
                            f"尝试重连后重试 ticket={ticket}"
                        )
                        if _reconnect_mt5(connect_params):
                            time.sleep(0.5)
                            result = mt5.order_send(request)
                        if result is None:
                            try:
                                _le2 = mt5.last_error()
                            except Exception:
                                _le2 = "unknown"
                            logger.warning(f"[Worker] 平仓重连后仍 order_send 返回 None，last_error={_le2}")
                            conn.send({"ok": False, "error": f"MT5 平仓 order_send 返回空（重连后仍失败）last_error={_le2}"})
                            continue
                    if result.retcode != mt5.TRADE_RETCODE_DONE:
                        conn.send({
                            "ok": False,
                            "error": f"平仓失败: {result.comment}",
                            "data": {"retcode": result.retcode}
                        })
                    else:
                        # ★ 2026-08-10 修复（所有仓位通用，非单笔）：原返回 pos.profit=平仓前整仓浮盈，
                        #   partial 平仓(如平0.5手)时虚高；且缺 volume → trades.volume 永远停在开仓值。
                        #   改为返回【实际成交手数】+【本次真实已实现盈亏】。
                        #   黄金 1 手 = 100 oz，1 美元价格移动 = $100/手：
                        #     BUY:  (close - open) × filled_vol × 100
                        #     SELL: (open - close) × filled_vol × 100
                        _filled_vol = float(result.volume or close_volume)
                        # ★ 2026-08-11 修复（P0 真实账号假巨亏）：REAL 券商 result.price 可能=0，
                        #   平仓价回退到平仓方向市价(tick)；开仓价始终用 pos.price_open（真实）。
                        _close_px = float(result.price or 0)
                        if _close_px <= 0:
                            _close_px = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                        if pos.type == mt5.ORDER_TYPE_BUY:
                            _real_pnl = (_close_px - pos.price_open) * _filled_vol * 100.0
                        else:
                            _real_pnl = (pos.price_open - _close_px) * _filled_vol * 100.0
                        # ★ 2026-08-15 审计P2修复：「net_profit 含佣优先」铁律名存实亡——
                        #   原返回无 net_profit，_record_close/_reconcile 的 `net_profit or profit`
                        #   恒取不含佣 profit，佣金/隔夜从不计入已实现盈亏。平仓后拉该 position
                        #   的 deals 汇总 commission+swap，net_profit = 价差盈亏 + 佣 + 隔夜。
                        #   （拉取失败时回退净额=价差盈亏，保持原行为不破坏。）
                        _net_pnl = _real_pnl
                        try:
                            # ★ 2026-08-15 复检P1修复：只取【本次平仓 deal】的佣金/隔夜——
                            #   原 history_deals_get(position) 返回整仓全部 deals（含开仓+此前
                            #   partial），第 2 次起 partial 把已计入的佣金/隔夜重复累加
                            #   （TP1/TP2/TP3 分批是常态路径 → trades/trade_exits 净利系统性失真）。
                            #   优先用 order_send 返回的 result.deal 精确锁定本次成交；
                            #   缺失时回退取该 position 全部 deals 中最后一个 OUT(1/3) deal。
                            _deal_tk = int(getattr(result, "deal", 0) or 0)
                            _d1 = None
                            if _deal_tk:
                                _d1 = mt5.history_deals_get(ticket=_deal_tk)
                            if not _d1:
                                _alld = mt5.history_deals_get(position=int(ticket)) or []
                                _outs = [d for d in _alld if getattr(d, "entry", -1) in (1, 3)]
                                _d1 = [_outs[-1]] if _outs else []
                            if _d1:
                                _comm = sum(float(getattr(d, "commission", 0) or 0) for d in _d1)
                                _swap = sum(float(getattr(d, "swap", 0) or 0) for d in _d1)
                                _net_pnl = _real_pnl + _comm + _swap
                        except Exception:
                            pass
                        # ★ 2026-08-16 下单幂等键：成交后先记录 req_id 再回执
                        #   （先记录后回执 → 崩溃窗口内重发被去重，绝不重复执行）
                        # ★ 2026-08-16 审计P0-3修复：同时保存首次成交快照，
                        #   回执丢失后 duplicate 重发能回放真实 volume/price/profit/net_profit
                        #   （旧实现只存 ticket，duplicate 返回全 0 → partial 盈亏永久丢失）。
                        _mark_exec_req(
                            _req, "close_position", ticket,
                            volume=_filled_vol,
                            close_price=_close_px,
                            profit=round(_real_pnl, 2),
                            net_profit=round(_net_pnl, 2),
                        )
                        conn.send({
                            "ok": True,
                            "data": {
                                "ticket": ticket,
                                "volume": _filled_vol,
                                "close_price": _close_px,
                                "profit": round(_real_pnl, 2),
                                "net_profit": round(_net_pnl, 2),
                            }
                        })

                # ----- modify_sl_tp（追踪止损 / 保本单）-----
                elif cmd_type == "modify_sl_tp":
                    args = cmd.get("args", {})
                    ticket = int(args.get("ticket", 0))
                    new_sl = float(args.get("sl", 0))
                    new_tp = float(args.get("tp", 0))

                    position = mt5.positions_get(ticket=ticket)
                    if position is None or len(position) == 0:
                        conn.send({"ok": False, "error": f"持仓 {ticket} 不存在"})
                        continue

                    pos = position[0]
                    # ★★ 2026-08-17 P0 修复：TRADE_ACTION_SLTP 必须带 type（持仓方向）★★
                    #   实测：开仓 request 带 type 时 SL 正常落位（#385622370 SL=4418.49），
                    #   而本 modify request 缺 type → MT5 服务器对 SLTP 修改静默忽略
                    #   （order_send 返回 retcode=DONE 但 SL 未生效）→ 跟号 SL 补设 5 轮假成功、
                    #   smart_exit 追踪止损从未真正移动过 SL（全时段日志零成功）。
                    #   修复：补 type = 持仓方向对应 ORDER_TYPE；同时补 type_filling 保守兜底。
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "symbol": pos.symbol,
                        "type": mt5.ORDER_TYPE_BUY if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL,
                        "type_filling": mt5.ORDER_FILLING_IOC,
                    }
                    if new_sl > 0:
                        request["sl"] = new_sl
                    if new_tp > 0:
                        request["tp"] = new_tp

                    result = mt5.order_send(request)
                    if result is None:
                        conn.send({"ok": False, "error": "MT5 order_send 返回空"})
                        continue
                    if result.retcode != mt5.TRADE_RETCODE_DONE:
                        conn.send({"ok": False, "error": f"改单失败 retcode={result.retcode} {result.comment}"})
                        continue
                    conn.send({
                        "ok": True,
                        "data": {
                            "ticket": ticket,
                            "sl": new_sl,
                            "tp": new_tp,
                        }
                    })

                # ----- shutdown -----
                elif cmd_type == "shutdown":
                    conn.send({"ok": True, "data": {"shutdown": True}})
                    break

                # ----- get_tick -----
                elif cmd_type == "get_tick":
                    # 轻量报价查询（仅 bid/ask/spread），供风控 Layer1 点差检查使用，
                    # 避免为一次风控校验拉取 200 根×6 周期完整 OHLCV。
                    symbol = cmd.get("args", {}).get("symbol", "XAUUSD")
                    if not _symbol_select_safe(symbol):
                        conn.send({"ok": False, "error": f"无法选择品种 {symbol}"})
                        continue
                    tick = mt5.symbol_info_tick(symbol)
                    sym_info = mt5.symbol_info(symbol)
                    if tick is None:
                        conn.send({"ok": False, "error": f"无法获取 {symbol} 报价"})
                        continue
                    spread = 0.0
                    if sym_info:
                        spread = round((tick.ask - tick.bid) / sym_info.point, 1)
                    conn.send({
                        "ok": True,
                        "data": {
                            "symbol": symbol,
                            "bid": tick.bid,
                            "ask": tick.ask,
                            "spread": spread,
                        },
                    })

                else:
                    conn.send({"ok": False, "error": f"未知命令: {cmd_type}"})
            except Exception as _cmd_e:
                # ★ 2026-08-11 P0 修复：每个命令 try 兜底，绝不让 worker 退出
                try:
                    conn.send({'ok': False, 'error': '命令 ' + str(cmd_type) + ' 异常: ' + type(_cmd_e).__name__ + ': ' + str(_cmd_e)})
                except Exception:
                    pass
                logger.warning(f'[Worker] 命令 {cmd_type} 异常已兜底不退出: {_cmd_e}')

    except Exception as e:
        try:
            conn.send({"ok": False, "error": f"Worker 异常: {str(e)}"})
        except Exception:
            pass
    finally:
        # ── 4. 清理 ──
        try:
            mt5.shutdown()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
