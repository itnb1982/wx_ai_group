"""
XAU/USD万象Ai自动量化交易系统 — MT5 连接服务（多进程版）
每个账号独立 Worker 子进程，各自维持独立的 mt5.initialize()
"""
import os
import time
import uuid
import threading
import multiprocessing
from multiprocessing import Process
from multiprocessing.connection import Connection, Pipe
from datetime import datetime
from typing import Optional, Dict, Tuple
from loguru import logger

from app.services.mt5_worker import worker_main


# ── 进程启动方式（Windows 必须用 spawn） ──
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass  # 已经设置过


# ── 通信超时配置 ──
WORKER_START_TIMEOUT = 20      # 终端已在运行时的 Worker 启动超时
WORKER_COLDSTART_TIMEOUT = 150  # 需要先拉起终端时的超时（首次启动要同步服务器与行情）
CMD_TIMEOUT = 10               # 普通命令超时

# ── 命令锁：保证 send+recv 原子性，避免多线程并发调用【同一管道】时响应错乱 ──
# 2026-08-09 性能根因修复：原先是一把全局锁，把 N 个账号的所有 MT5 调用全部串行化。
# 实测 /api/dashboard/accounts 需 20.7s（4 账号 × 每账号 4 次调用，其中 90 天历史
# 成交是重查询），前端 3s 轮询直接超时 → 全屏红条。
# 不同账号跑在各自独立的 Worker 子进程 + 独立管道上，本就互不干扰，
# 只需「同一账号内串行」。改为按 account_id 分锁后，N 个账号可真正并行。
_cmd_lock = threading.Lock()  # 兜底锁（未提供 lock_key 时使用）
_cmd_locks: Dict[str, threading.Lock] = {}
_cmd_locks_guard = threading.Lock()


def _get_cmd_lock(key: str) -> threading.Lock:
    """取某账号专属的命令锁（懒创建）。"""
    with _cmd_locks_guard:
        lk = _cmd_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _cmd_locks[key] = lk
        return lk

# ── 重连/重生冷却：MT5 会话短暂抖动时避免每周期都杀掉并重生 Worker ──
# （与 Worker 自身秒级自愈防抖配合，双保险且互不打架）
RESPAWN_COOLDOWN_SEC = 45.0
_RESPAWN_COOLDOWN_TS: Dict[str, float] = {}

# ── 自动重连失败退避（2026-08-09 修复雪崩）──
# 背景：_reconnect 失败后原先没有任何冷却，而 /api/dashboard/* 每 3s 轮询、
# 每账号还要查余额/持仓/净值等多个命令，实测日志被刷成 每3s × 4账号 × 4条，
# supervisor_uvicorn.log 涨到 37 万行。更危险的是：凭证登记修好之后，
# 没有退避就会变成"每 3s 尝试 spawn 4 个 MT5 终端进程"——冷启动单次 90s，
# 进程会瞬间堆叠把机器压垮。故加指数退避：20s → 40s → 80s → … → 上限 300s。
RECONNECT_BACKOFF_BASE = 20.0
RECONNECT_BACKOFF_MAX = 300.0
_RECONNECT_NEXT_TS: Dict[str, float] = {}
_RECONNECT_FAILS: Dict[str, int] = {}

# ── 熔断：单账号连续重连失败达到上限后停止自动重连，标记为 OFFLINE ──
# 背景（2026-08-12 根治）：某账号终端不兼容（如 Netting 模式）会导致 Worker
# 初始化永久失败 → 进程退出 → 被自动重连反复拉起 → 无限崩溃循环，刷屏日志、
# 占用 _lock、拖垮 health 判定、甚至阻塞删除接口。故加熔断：连续失败 N 次后
# 标记 OFFLINE，停止重连并记录原因，需人工排查后手动"连接"才重试。
# ★ 2026-08-15 调整：原 5 次过低——重启后跟号终端握手慢/瞬时失败易在窗口内
#   打满 5 次→永久熔断OFFLINE→主号平它跟不上(主号平挂号没平)。提到 12 次，
#   配合下方 _spawn 多次 poll 重试，给慢终端充足恢复窗口；真正的终端不兼容
#   (Netting模式等)仍会打满并熔断，不误伤。
MAX_RECONNECT_ATTEMPTS = 12
_OFFLINE: Dict[str, str] = {}  # account_id -> 熔断原因（进程生命周期内有效）

# ── 每账号 spawn 锁：避免对同一账号并发拉起 Worker，且让 90s MT5 初始化阻塞
#    不再占用全局 _lock（2026-08-12 根治：旧实现 _spawn 全程持 _lock，导致
#    崩溃账号的 90s 初始化卡住其他账号的删除/连接操作，表现为"删除按钮点不动"）──
_SPAWN_LOCKS: Dict[str, threading.Lock] = {}
_SPAWN_LOCKS_GUARD = threading.Lock()


def _get_spawn_lock(account_id: str) -> threading.Lock:
    with _SPAWN_LOCKS_GUARD:
        lk = _SPAWN_LOCKS.get(account_id)
        if lk is None:
            lk = threading.Lock()
            _SPAWN_LOCKS[account_id] = lk
        return lk

# ── 账号状态缓存（供 /api/health 高频轻量读取，避免每 5s 同步 IPC 阻塞事件循环） ──
# 2026-08-09：health 被 supervisor/前端每 5s 轮询，原实现直接对每个 worker 发 ping，
# 4 账号串行 × 3s 超时 → health 响应 10-14s，超过前端超时阈值导致全屏红条。
# 改为后台线程定期刷新，health 只读本缓存。
_STATUS_CACHE: list = []
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_THREAD: Optional[threading.Thread] = None
_STATUS_REFRESH_INTERVAL = 10.0  # 秒


def _refresh_status_cache():
    """后台刷新 MT5 账号连接状态缓存。"""
    try:
        st = mt5_service.get_all_accounts_status() if mt5_service else []
    except Exception:
        st = []
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[:] = st


def _status_refresh_loop():
    """后台线程入口：首次立即刷新，之后按间隔刷新。"""
    _refresh_status_cache()
    while True:
        time.sleep(_STATUS_REFRESH_INTERVAL)
        _refresh_status_cache()


def start_status_refresh() -> None:
    """启动 MT5 状态缓存后台刷新线程（lifespan 中调用一次）。"""
    global _STATUS_THREAD
    if _STATUS_THREAD is not None and _STATUS_THREAD.is_alive():
        return
    t = threading.Thread(target=_status_refresh_loop, name="mt5-status-refresh", daemon=True)
    t.start()
    _STATUS_THREAD = t


def get_cached_accounts_status() -> list:
    """读取缓存的 MT5 账号状态（供 /api/health 使用，毫秒级）。"""
    with _STATUS_CACHE_LOCK:
        return list(_STATUS_CACHE)


def _is_respawn_error(err: str) -> bool:
    """判断错误是否属于需要重连/重生 Worker 的类别。

    2026-08-04 扩充：此前仅匹配 '连接断开'/'响应超时'（管道级），
    MT5 升级重启后终端会话死、Worker 仍活着时返回的是
    'IPC send failed'(-10001)/'无法获取账户信息'/'MT5 终端断开' 等，
    不匹配导致系统假死约12分钟。现一并纳入兜底重连。
    """
    if not err:
        return False
    keys = ("连接断开", "响应超时", "Worker 连接断开", "Worker 失联",
            "IPC send failed", "(-10001)", "10001", "MT5 终端断开",
            "MT5 初始化失败", "无法获取账户信息", "not connected", "terminal")
    return any(k in err for k in keys)


def _send_cmd(conn: Connection, cmd: dict, timeout: float = CMD_TIMEOUT,
              lock_key: str = "") -> dict:
    """向 Worker 发送命令并等待响应（按账号加锁防止并发竞态）。

    lock_key 传 account_id 时使用该账号专属锁，多账号可并行；
    留空则退回全局锁（仅兼容未传参的历史调用点）。

    ★ 2026-08-17 P0 修复（22:11-22:13 全服务卡死）：锁获取必须带超时。
      原实现 `with lock:` 无限等待——当某账号锁被长任务（_reconnect 最长
      150s / 管道错乱的 worker）持有时，所有调用方排队死等，配合 async 端点
      同步调用可把事件循环/线程池整体堵死。改为 acquire(timeout) 快速失败，
      调用方走"Worker 忙/响应超时"路径降级，绝不无限阻塞。
    """
    _lk = _get_cmd_lock(lock_key) if lock_key else _cmd_lock
    if not _lk.acquire(timeout=max(5.0, timeout + 2.0)):
        return {"ok": False, "error": f"Worker 命令锁等待超时（busy, cmd={cmd.get('cmd')}）"}
    try:
        try:
            conn.send(cmd)
            if conn.poll(timeout):
                return conn.recv()
            else:
                return {"ok": False, "error": "Worker 响应超时"}
        except (EOFError, ConnectionResetError, OSError) as e:
            return {"ok": False, "error": f"Worker 连接断开: {str(e)}"}
    finally:
        _lk.release()


class MT5Service:
    """MT5 多账号管理服务（多进程版）"""

    def __init__(self):
        # {account_id: (Process, Connection)}
        self._workers: Dict[str, Tuple[Process, Connection]] = {}
        # {account_id: {"terminal_path": str, "name": str}} — 用于终端独占校验
        self._meta: Dict[str, Dict[str, str]] = {}
        # 用可重入锁：自动重连(_reconnect)会再次进入 _spawn 并加锁，普通 Lock 会死锁
        self._lock = threading.RLock()

    # ★ 2026-08-15 #7：每进程每终端路径只做一次孤儿清理（重载时清掉上一实例残留）。
    #   用集合做去重，避免重连风暴中每次 _spawn 都重复杀终端（首次接入即清，之后跳过）。
    _orphan_cleanup_done: set = set()

    # ──────────── 账号管理 ────────────

    def add_account(
        self,
        account_id: str,
        login: str,
        password: str,
        server: str,
        name: str = "",
        terminal_path: str = "",
    ) -> bool:
        """添加并连接 MT5 账号（启动独立 Worker 子进程）"""
        with self._lock:
            # ── 终端独占校验 ──
            # 一个 MT5 终端实例同一时刻只能登录一个账号。
            # 若两个账号共用同一个 terminal64.exe，后连接者会把先连接者的登录顶掉，
            # 导致读到错误的余额、甚至把订单打进别人的账户。
            if terminal_path:
                norm = os.path.normcase(os.path.abspath(terminal_path))
                for other_id, meta in self._meta.items():
                    if other_id == account_id or other_id not in self._workers:
                        continue
                    other_path = meta.get("terminal_path") or ""
                    if other_path and os.path.normcase(os.path.abspath(other_path)) == norm:
                        logger.error(
                            f"[MT5:{name or account_id}] 连接被拒绝：终端路径与账号 "
                            f"{meta.get('name') or other_id} 重复 -> {terminal_path}。"
                            f"一个 MT5 终端同时只能登录一个账号，请为每个账号准备独立的安装目录。"
                        )
                        return False

            # 用户显式发起的连接（前端"连接"按钮 / 启动期 bootstrap）：
            # 清零自动重连退避与熔断标记，让这次请求立即执行，不被旧状态挡住
            # （熔断账号只有用户手动"连接"才会被清掉并重试）。
            _RECONNECT_FAILS.pop(account_id, None)
            _RECONNECT_NEXT_TS.pop(account_id, None)
            _OFFLINE.pop(account_id, None)

            # 启动/重启 Worker（含凭证存储，供后续自动重连）
            return self._spawn(account_id, login, password, server, name, terminal_path)

    # ──────────── Worker 启动 / 自动重连 ────────────

    def _spawn(self, account_id: str, login: str, password: str,
               server: str, name: str, terminal_path: str) -> bool:
        """启动（或重启）某账号的 Worker 子进程，并存储凭证供自动重连。

        ★ 2026-08-12 根治：阻塞性的 MT5 初始化等待（最长 WORKER_COLDSTART_TIMEOUT 秒）
        不再持有全局 _lock。旧实现 _spawn 全程持 _lock，崩溃账号的 90s 初始化会卡住
        其他账号的删除/连接操作（表现为"删除按钮点不动"）。现改为：仅用每账号 spawn
        锁防并发重复拉起，关键注册/清理用 _lock 且仅持极短临界区，90s 等待移到锁外。
        """
        # 熔断后（非用户显式连接）不再自动拉起，避免无限崩溃循环
        if _OFFLINE.get(account_id):
            logger.warning(f"[MT5:{name or account_id}] 已熔断，跳过自动重连（需人工排查后手动连接）")
            return False

        # 每账号 spawn 锁：防止对同一个崩溃账号并发重复拉起 Worker
        spawn_lk = _get_spawn_lock(account_id)
        if not spawn_lk.acquire(blocking=False):
            logger.debug(f"[MT5:{name or account_id}] 已在拉起中，跳过重复 _spawn")
            return False
        try:
            # ★ 2026-08-15 #7 孤儿 MT5 终端清理（每进程每路径只清一次）：
            #   重载后上一实例拉起的 terminal64.exe 可能因会话/权限隔离未被级联杀净，
            #   残留在原路径运行。新实例若直接 ensure_terminal 会附着到僵死/持锁旧终端
            #   → 连接卡死。接入前对已知路径做路径精确清理，让新实例干净冷启动。
            if terminal_path:
                _tp_norm = os.path.normcase(os.path.abspath(terminal_path))
                if _tp_norm not in MT5Service._orphan_cleanup_done:
                    MT5Service._orphan_cleanup_done.add(_tp_norm)
                    try:
                        from app.services.mt5_launcher import cleanup_orphan_terminals

                        _oc = cleanup_orphan_terminals([terminal_path])
                        if _oc.get("cleaned"):
                            logger.warning(
                                f"[MT5:{name or account_id}] 已清理上一实例残留孤儿终端"
                                f"({_oc['cleaned']}个): {terminal_path}"
                            )
                    except Exception as _oe:
                        logger.debug(f"[MT5:{name or account_id}] 孤儿终端清理异常(忽略): {_oe}")
            # ── 极短临界区：杀旧 Worker + 登记凭证 + 起进程（不等待初始化）──
            with self._lock:
                old = self._workers.get(account_id)
                if old is not None:
                    self._kill_worker(account_id, old)
                self._meta[account_id] = {
                    "terminal_path": terminal_path or "",
                    "name": name or account_id,
                    "login": login,
                    "password": password,
                    "server": server,
                }
                parent_conn, child_conn = Pipe(duplex=True)
                connect_params = {
                    "login": login,
                    "password": password,
                    "server": server,
                    "path": terminal_path,
                }
                p = Process(
                    target=worker_main,
                    args=(child_conn, connect_params),
                    name=f"mt5-worker-{name or account_id}",
                    daemon=True,
                )
                p.start()
                # 占位登记，标记"拉起中"；后续成功则转正，失败则移除
                self._workers[account_id] = (p, parent_conn)

            # ── 阻塞等待移到全局锁之外 ──
            try:
                from app.services.mt5_launcher import is_terminal_running
                cold = bool(terminal_path) and not is_terminal_running(terminal_path)
            except Exception:
                cold = False
            start_timeout = WORKER_COLDSTART_TIMEOUT
            if cold:
                logger.info(f"[MT5:{name}] 终端未运行，将先启动终端（超时 {start_timeout}s）")
            else:
                logger.info(f"[MT5:{name}] 终端已在运行，等待 Worker 附着（超时 {start_timeout}s）")

            # ★ 2026-08-15 根治重启后跟号 Worker 死循环：原单次 poll(start_timeout) 在终端
            #   握手慢/瞬时失败时直接判超时→失败→重连退避→5次熔断OFFLINE(主号平挂号没平)。
            #   改为在 start_timeout 窗口内每 5s poll 一次并容忍瞬时失败，给慢终端充足时间；
            #   只要窗口内任一刻连接成功即转正，大幅降低重启后短暂失联被误熔断的概率。
            deadline = time.time() + start_timeout
            _connected = False
            _resp_err = f"Worker 启动超时({start_timeout}s)"
            _resp = None
            while time.time() < deadline:
                _wait = min(5.0, max(0.1, deadline - time.time()))
                if parent_conn.poll(_wait):
                    _resp = parent_conn.recv()
                    if _resp.get("ok") and _resp.get("event") == "connected":
                        _connected = True
                        break
                    else:
                        _resp_err = _resp.get("error", "Worker 启动失败")
                        break
                time.sleep(0.5)
            if _connected and _resp is not None:
                data = _resp.get("data", {})
                logger.info(
                    f"[MT5:{name}] Worker 就绪 - "
                    f"login={data.get('login')} 余额=${data.get('balance', 0):.2f}"
                )
                with self._lock:
                    self._workers[account_id] = (p, parent_conn)
                    # 连接成功：清空重连退避与熔断标记，下次真断线能立即快速重连
                    _RECONNECT_FAILS.pop(account_id, None)
                    _RECONNECT_NEXT_TS.pop(account_id, None)
                    _OFFLINE.pop(account_id, None)
                return True
            else:
                logger.error(f"[MT5:{name}] {_resp_err}")
                with self._lock:
                    self._workers.pop(account_id, None)  # 移除占位；保留 _meta 供退避后重试
                self._safe_terminate(p, parent_conn)
                return False
        finally:
            spawn_lk.release()

    def _safe_terminate(self, p: Process, conn: Connection) -> None:
        """安全终止 Worker 进程并关闭管道（不抛异常）。"""
        try:
            if p.is_alive():
                p.terminate()
            p.join(timeout=3)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def _reconnect(self, account_id: str) -> bool:
        """Worker 失联后，用已存储的凭证自动重连一次（带指数退避 + 熔断）。"""
        if _OFFLINE.get(account_id):
            # 已熔断：不再自动重连，避免崩溃账号无限循环拖垮系统
            return False
        meta = self._meta.get(account_id)
        if not meta or not meta.get("login"):
            # ★ 2026-08-17 P0修复：凭证缺失时回退从 DB 读（原实现直接放弃 → Worker 崩溃后
            #   永久无法自动重连，跟号持仓管理/镜像出场失明，如 b3db40fd 反复断连后
            #   "无存储的凭证" 卡死）。DB 存的是 DPAPI 加密密码，需 decrypt 后 _spawn。
            try:
                from app.database import SessionLocal
                from app.models.mt5_account import MT5Account as _MT5Account
                from app.utils.crypto import decrypt as _decrypt_pwd

                _s = SessionLocal()
                try:
                    _acc = _s.query(_MT5Account).filter(_MT5Account.id == account_id).first()
                finally:
                    _s.close()
                if _acc and getattr(_acc, "account_id", None):
                    _pwd = ""
                    try:
                        _pwd = _decrypt_pwd(_acc.password)
                    except Exception as _pe:
                        logger.warning(f"[MT5] 解密 {account_id} 密码失败: {_pe}")
                    self._meta[account_id] = {
                        "terminal_path": getattr(_acc, "terminal_path", "") or "",
                        "name": getattr(_acc, "name", "") or account_id,
                        "login": str(getattr(_acc, "account_id", "") or ""),
                        "password": _pwd,
                        "server": getattr(_acc, "server", "") or "",
                    }
                    meta = self._meta[account_id]
                    logger.warning(
                        f"[MT5:{account_id}] _meta 凭证缺失，已从 DB 回退读取（login={meta.get('login')}）"
                    )
                else:
                    logger.debug(f"[MT5] 无法重连 {account_id}：DB 也无凭证")
                    return False
            except Exception as _e:  # noqa: BLE001
                logger.debug(f"[MT5] 无法重连 {account_id}：凭证回退 DB 失败 ({_e})")
                return False
        if not meta or not meta.get("login"):
            logger.debug(f"[MT5] 无法重连 {account_id}：无存储的凭证")
            return False

        name = meta.get("name", account_id)
        now = time.time()
        next_ok = _RECONNECT_NEXT_TS.get(account_id, 0.0)
        if now < next_ok:
            # 退避窗口内静默跳过：spawn 会拉起 MT5 终端（冷启动 90s），
            # 绝不能被高频接口调用带着反复触发。
            logger.debug(f"[MT5:{name}] 重连退避中，剩余 {next_ok - now:.0f}s")
            return False

        logger.warning(f"[MT5:{name}] Worker 失联，尝试自动重连...")
        ok = self._spawn(
            account_id,
            meta.get("login", ""),
            meta.get("password", ""),
            meta.get("server", ""),
            name,
            meta.get("terminal_path", ""),
        )
        if not ok:
            n = _RECONNECT_FAILS.get(account_id, 0) + 1
            _RECONNECT_FAILS[account_id] = n
            if n >= MAX_RECONNECT_ATTEMPTS:
                # ★ 2026-08-12 根治：达到上限 → 熔断，停止自动重连并落库 OFFLINE
                self._mark_offline(
                    account_id,
                    f"连续 {n} 次自动重连失败（疑似终端不兼容/凭据失效），已熔断并停止重试；"
                    f"请排查终端({meta.get('terminal_path') or '默认'})与凭据后手动连接",
                )
                return False
            delay = min(RECONNECT_BACKOFF_BASE * (2 ** (n - 1)), RECONNECT_BACKOFF_MAX)
            _RECONNECT_NEXT_TS[account_id] = time.time() + delay
            logger.warning(
                f"[MT5:{name}] 第 {n} 次自动重连失败，{delay:.0f}s 后重试"
                f"（累计 {MAX_RECONNECT_ATTEMPTS} 次后熔断）"
            )
        return ok

    def _mark_offline(self, account_id: str, reason: str) -> None:
        """熔断：标记账号 OFFLINE，停止自动重连，并如实落库。"""
        name = (self._meta.get(account_id) or {}).get("name", account_id)
        logger.error(f"[MT5:{name}] 触发熔断 OFFLINE：{reason}")
        _OFFLINE[account_id] = reason
        with self._lock:
            entry = self._workers.pop(account_id, None)
            self._meta.pop(account_id, None)
            _RECONNECT_FAILS.pop(account_id, None)
            _RECONNECT_NEXT_TS.pop(account_id, None)
            _RESPAWN_COOLDOWN_TS.pop(account_id, None)
        if entry is not None:
            self._kill_worker(account_id, entry)
        # 落库：status=OFFLINE 让前端/health 一眼看到，is_connected=0
        try:
            from app.database import SessionLocal
            from sqlalchemy import text as _text
            s = SessionLocal()
            s.execute(
                _text(
                    "UPDATE mt5_accounts SET status='OFFLINE', status_message=:r, is_connected=0 WHERE id=:i"
                ),
                {"r": reason[:500], "i": account_id},
            )
            s.commit()
            s.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MT5:{name}] 熔断状态落库失败(不影响内存熔断): {e}")

    def is_account_offline(self, account_id: str) -> bool:
        """该账号是否已熔断（供重连守护跳过）。"""
        return bool(_OFFLINE.get(account_id))

    def get_offline_accounts(self) -> list:
        """返回已熔断账号列表 [{"id", "name", "reason"}]。"""
        out = []
        for k in _OFFLINE:
            out.append({
                "id": k,
                "name": (self._meta.get(k) or {}).get("name", k),
                "reason": _OFFLINE[k],
            })
        return out

    def get_account_health_summary(self) -> dict:
        """供 /api/health 使用的账号健康汇总（毫秒级，DB 读取带 60s 缓存）。

        ★ 2026-08-12 根治：degraded 只统计「应交易(is_trading_enabled=1)且未熔断」的账号，
        单账号（尤其 is_trading_enabled=0 或已熔断崩溃账号）离线不再拖垮整体判定；
        具体离线账号通过 offline / non_trading_offline 呈现，前端可单独告警而不触发断连。
        """
        now = time.time()
        cache = self.__dict__.setdefault(
            "_health_cache", {"ts": 0.0, "all_ids": set(), "trading_ids": set()}
        )
        if now - cache["ts"] > 60:
            try:
                from app.database import SessionLocal
                from sqlalchemy import text as _text
                s = SessionLocal()
                rows = s.execute(_text("SELECT id, is_trading_enabled FROM mt5_accounts")).fetchall()
                s.close()
                cache["all_ids"] = {r[0] for r in rows}
                cache["trading_ids"] = {r[0] for r in rows if r[1]}
                cache["ts"] = now
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[MT5] 健康汇总读库失败(沿用旧缓存): {e}")
        all_ids = cache["all_ids"]
        trading_ids = cache["trading_ids"]
        offline_ids = set(_OFFLINE.keys())
        # 复用后台状态缓存（仅含当前有 Worker 的账号），避免 health 内再发 IPC ping
        connected_ids = {
            s["account_id"] for s in get_cached_accounts_status() if s.get("connected")
        }
        trading_expected = len([a for a in trading_ids if a not in offline_ids])
        trading_connected = len(
            [a for a in connected_ids if a in trading_ids and a not in offline_ids]
        )
        offline_list = [
            {"id": k, "name": (self._meta.get(k) or {}).get("name", k), "reason": _OFFLINE[k]}
            for k in offline_ids
        ]
        non_trading_offline = [
            a for a in all_ids
            if a not in trading_ids and a not in offline_ids and a not in connected_ids
        ]
        non_trading_offline_list = [
            {"id": a, "name": (self._meta.get(a) or {}).get("name", a)}
            for a in non_trading_offline
        ]
        return {
            "trading_expected": trading_expected,
            "trading_connected": trading_connected,
            "all_ids": list(all_ids),
            "connected_ids": list(connected_ids),
            "offline": offline_list,
            "non_trading_offline": non_trading_offline_list,
        }

    def _safe_send(self, account_id: str, cmd: dict, timeout: float = CMD_TIMEOUT) -> dict:
        """发送命令：若 Worker 未连接 / 管道断开 / MT5 会话断裂，自动重连后重试。

        timeout 可选：历史成交等重查询需要更长等待，但同样要享受重连重试保护
        （原先它们绕过本方法裸调 _send_cmd，一次超时就直接失败）。

        ★ 2026-08-16 下单幂等键：对非幂等命令（开仓/平仓）自动注入 req_id(UUID)，
        重连重发复用同一 req_id → Worker 侧按 data/exec_req_ids.json 去重，
        杜绝「Worker 已成交但响应前断管 → 重发 → 双倍敞口」。只读命令不注入（零开销）。
        """
        if cmd.get("cmd") in ("place_order", "close_position") and not cmd.get("req_id"):
            cmd["req_id"] = uuid.uuid4().hex
        conn = self._get_conn(account_id)
        if conn is None:
            if not self._reconnect(account_id):
                return {"ok": False, "error": "账号未连接且重连失败"}
            conn = self._get_conn(account_id)
            if conn is None:
                return {"ok": False, "error": "账号未连接"}
        resp = _send_cmd(conn, cmd, timeout=timeout, lock_key=account_id)
        # 防御 Worker 返回非 dict（极少数异常 traceback 场景），避免调用方 .get 崩溃
        if not isinstance(resp, dict):
            logger.error(
                f"[MT5:{account_id}] Worker 返回非预期类型 {type(resp).__name__}，"
                f"已防御性转换为错误响应。cmd={cmd.get('cmd')}"
            )
            resp = {"ok": False, "error": f"Worker 返回非预期类型: {type(resp).__name__}"}
        err = resp.get("error", "")
        # Worker 自身已做秒级自愈；此处作为兜底：对管道级断开与 MT5 会话断裂特征
        # 触发重连/重生 Worker，并加 45s 冷却防抖避免抖动时反复杀进程。
        if not resp.get("ok") and _is_respawn_error(err):
            now = time.time()
            last = _RESPAWN_COOLDOWN_TS.get(account_id, 0)
            if (now - last) >= RESPAWN_COOLDOWN_SEC:
                _RESPAWN_COOLDOWN_TS[account_id] = now
                logger.warning(
                    f"[MT5:{account_id}] 命令失败（{err}），尝试重连/重生 Worker"
                )
                if self._reconnect(account_id):
                    conn = self._get_conn(account_id)
                    if conn is not None:
                        resp = _send_cmd(conn, cmd, timeout=timeout, lock_key=account_id)
                        # ★ 2026-08-15 复检P3修复：重连后重发的响应同样要 isinstance 防御
                        #   （与首响应 L526 同款）——Worker 异常形态时可漏过，调用方 .get 崩溃。
                        if not isinstance(resp, dict):
                            logger.error(
                                f"[MT5:{account_id}] 重连重发后 Worker 返回非预期类型 "
                                f"{type(resp).__name__}，已防御转换为错误响应"
                            )
                            resp = {"ok": False, "error": f"Worker 返回非预期类型: {type(resp).__name__}"}
            else:
                logger.debug(
                    f"[MT5:{account_id}] 命令失败（{err}），重连冷却中({RESPAWN_COOLDOWN_SEC:.0f}s)"
                )
        return resp

    def remove_account(self, account_id: str):
        """移除 MT5 账号（关闭 Worker 子进程）"""
        with self._lock:
            entry = self._workers.pop(account_id, None)
            self._meta.pop(account_id, None)
            # 一并清理重连/熔断状态，避免账号删了又以同 id 重建时被旧状态卡住
            _RECONNECT_FAILS.pop(account_id, None)
            _RECONNECT_NEXT_TS.pop(account_id, None)
            _RESPAWN_COOLDOWN_TS.pop(account_id, None)
            _OFFLINE.pop(account_id, None)
            # ★ 2026-08-15 审计P3修复：一并清理命令锁/启动锁（key 可能带 :cmd 后缀），
            #   避免账号增删多次后锁 dict 无限膨胀（长跑内存增长）。
            with _cmd_locks_guard:
                for _k in [k for k in list(_cmd_locks)
                           if k == account_id or k.startswith(account_id + ":")]:
                    _cmd_locks.pop(_k, None)
            with _SPAWN_LOCKS_GUARD:
                _SPAWN_LOCKS.pop(account_id, None)
            if entry:
                self._kill_worker(account_id, entry)

    def _kill_worker(self, account_id: str, entry: Tuple[Process, Connection]):
        """安全关闭 Worker 进程（全程有界，绝不因管道卡死而阻塞调用方）。

        ★ 2026-08-12 根治：旧实现 conn.send 直接在主线程调用，若 Worker 已死但管道句柄
        未释放，send 可能无限阻塞 → 删除接口等调用方被卡 30s 超时。现改为：send 放进
        守护线程并限时 join(2s)，超时即视为管道已废，直接走 terminate 路径。
        """
        p, conn = entry
        try:
            def _send_shutdown():
                try:
                    conn.send({"cmd": "shutdown"})
                except Exception:
                    pass
            _st = threading.Thread(target=_send_shutdown, daemon=True)
            _st.start()
            _st.join(timeout=2.0)
            if _st.is_alive():
                # send 卡死：管道已废，放弃等待确认
                logger.warning(f"[MT5] Worker {account_id} shutdown 发送超时（管道疑似卡死），直接终止")
            else:
                try:
                    if conn.poll(2.0):
                        conn.recv()  # 接收确认
                except Exception:
                    pass
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        # 等待进程退出（有界）
        p.join(timeout=3)
        if p.is_alive():
            logger.warning(f"[MT5] Worker {account_id} 未响应 shutdown，强制终止")
            p.terminate()
            p.join(timeout=2)

    def shutdown_all(self):
        """关闭所有 Worker"""
        with self._lock:
            for account_id, entry in list(self._workers.items()):
                self._kill_worker(account_id, entry)
            self._workers.clear()

    # ──────────── 数据获取 ────────────

    def _get_conn(self, account_id: str) -> Connection | None:
        """获取账号对应的 Worker 管道（线程安全）"""
        entry = self._workers.get(account_id)
        if entry is None:
            return None
        _, conn = entry
        return conn

    def alive_account_ids(self) -> set:
        """返回 Worker 进程真实存活的账号 ID 集合。

        ★ 2026-08-09 新增：DB 的 is_connected 是心跳异步落库的结果，Worker 已死
        但字段还留着 1 的窗口期真实存在。行情主号选择若信了这个字段，就会把
        AI 的行情源指向一个死账号，最终静默降级成模拟数据下单。
        这里直接看进程对象，是唯一不会说谎的依据。
        """
        alive = set()
        for aid, entry in list(self._workers.items()):
            try:
                proc, _conn = entry
                if proc is not None and proc.is_alive():
                    alive.add(aid)
            except Exception:
                continue
        return alive

    def get_account_info(self, account_id: str) -> dict:
        """获取单个账号信息"""
        resp = self._safe_send(account_id, {"cmd": "get_account_info"})
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：原 resp["data"] 直取下标，ok=True 但缺 data 键即抛 KeyError→500。
            #   改为 .get 防御，缺键返回 {} 而非崩溃。
            # ★ 2026-08-17 防御：data 非 dict（异常形态如 list）→ 返回 error，
            #   否则调用方 account_info.get("balance") 炸 'list' object has no attribute 'get'
            #   （copy_order 跟单复制失败刷屏的可疑炸点之一，与 place_order 同款防御）。
            _data = resp.get("data")
            if isinstance(_data, dict):
                return _data
            return {"error": f"账户信息 data 异常类型: {type(_data).__name__}"}
        else:
            return {"error": resp.get("error", "未知错误")}

    def _query_positions(self, account_id: str, symbol) -> tuple:
        """持仓查询的唯一底层实现，返回 (查询是否成功, 持仓列表)。

        ★ 2026-08-07 Phase 1：这是 checked 系列的共同内核。
        Worker 回 ok 但 data 为 None 时算成功且按空仓处理（不抛异常）。

        ★ 2026-08-10 根治「有持仓却显示无持仓」显示 bug：
        Worker 掉线 / 管道断 / 命令超时都会让本次查询失败，原实现
        直接返回 []，与「账号真空仓」完全无法区分 —— 前端拿到 []
        就渲染成「当前无持仓」，把真实持仓误显成空仓（偶发、难复现、
        极误导客户，且不影响实盘交易）。
        修复：维护每账号「最后已知持仓」(last-known-good) 缓存，查询
        成功即刷新；查询失败时回退缓存（缓存非空则展示真实持仓）,
        仅当该账号从未成功查询过才返回 []。
        注意隔离：破坏性动作（改账本 / 平仓）一律走 get_*_checked，
        那里读到 ok=False 会 fail-safe，不会使用这里的兜底数据，因此
        本兜底【只服务于展示】，绝不污染平仓 / 对账决策。
        """
        cache = self.__dict__.setdefault("_last_positions_cache", {})
        key = (account_id, symbol)
        resp = self._safe_send(account_id, {"cmd": "get_positions", "args": {"symbol": symbol}})
        if resp.get("ok"):
            # ★ 2026-08-11 P0 修复：data 必须是 list；worker 偶尔返回字符串时
            #   "str or []" 会保留 str（字符串是真值），直接传给调用方会触发
            #   `p.get("ticket")` 抛 'str' object has no attribute 'get'（traceback 实测）。
            #   修：isinstance 检查，非 list 视为"拿不到数据"，按失败处理 → 走缓存。
            _raw = resp.get("data")
            if isinstance(_raw, list):
                data = _raw
                cache[key] = data
                return True, data
            if _raw is None:
                # ★ 2026-08-17 契约修复：Worker 明确回 ok 且 data=None = 成功空仓
                #   （测试契约：get_positions_checked("acc") == (True, [])）。
                #   此前把 None 与异常形态(字符串)混为一谈按失败处理 → 反向对账
                #   读到 ok=False 会 fail-safe 拒开仓，空仓期白白错过开仓窗口。
                data = []
                cache[key] = data
                return True, data
            logger.warning(
                f"[MT5:{account_id}] get_positions data 异常类型 {type(_raw).__name__},"
                f"按失败处理回退缓存"
            )
            cached = cache.get(key)
            if cached is not None:
                return False, cached
            return False, []  # noqa
        # 查询失败：回退最近一次成功结果，避免把「查询失败」误显为「无持仓」
        cached = cache.get(key)
        if cached is not None:
            return False, cached
        return False, []

    def get_positions_checked(self, account_id: str, symbol: str = "XAUUSD") -> tuple:
        """★ 2026-08-07 Phase 1：可分辨失败的持仓查询，返回 (ok, positions)。

        为什么必须存在：`get_positions()` 在 Worker 掉线 / 管道断开 / 命令超时时
        与"账号确实一手没有"一样返回 []。只读场景无所谓，但**破坏性动作**
        （改账本、平仓）若拿这个空列表当事实依据，后果是灾难性的：
          · 反向对账把全部真实持仓写成 closed → AI 失明 → 重复开仓超仓；
          · 主副对账把跟号每一笔都判成孤儿单 → 主号抖一下就清空跟号仓位。
        凡是要据结果**动手**的调用方，一律用本方法，并对 ok=False 走 fail-safe。
        """
        return self._query_positions(account_id, symbol)

    def get_all_positions_checked(self, account_id: str) -> tuple:
        """全品种版的 checked 查询，契约同 get_positions_checked。"""
        return self._query_positions(account_id, None)

    def get_positions(self, account_id: str, symbol: str = "XAUUSD") -> list:
        """获取指定账号持仓（默认按 XAUUSD 过滤，兼容旧调用）。

        ⚠ 失败与空仓不可分辨（都返回 []）。只适用于展示/只读场景。
        要据结果改账本或平仓，请改用 get_positions_checked()。
        """
        return self._query_positions(account_id, symbol)[1]

    def get_all_positions(self, account_id: str) -> list:
        """★ 2026-08-06：获取指定账号【全部品种】持仓。

        修复"智能平仓只处理单订单"根因A：持仓管理/风控/篮子护盾必须能拿到
        账号下全部持仓，而非只按 XAUUSD 过滤。worker 端 symbol=None → positions_get() 全量。

        ⚠ 同样无法分辨失败与空仓，破坏性动作请用 get_all_positions_checked()。
        """
        return self._query_positions(account_id, None)[1]

    def place_order(self, account_id: str, **kwargs) -> dict:
        """在指定账号下单"""
        resp = self._safe_send(account_id, {"cmd": "place_order", "args": kwargs})
        if resp.get("ok"):
            _data = resp.get("data")
            return _data if isinstance(_data, dict) else {"error": f"data 异常类型: {type(_data).__name__}"}
        return {"error": resp.get("error", "下单失败"), "ticket": None}

    def close_position(self, account_id: str, ticket: int, volume: float = 0) -> dict:
        """在指定账号平仓"""
        resp = self._safe_send(account_id, {
            "cmd": "close_position",
            "args": {"ticket": ticket, "volume": volume}
        })
        if resp.get("ok"):
            _data = resp.get("data")
            return _data if isinstance(_data, dict) else {"error": f"data 异常类型: {type(_data).__name__}"}
        return {"error": resp.get("error", "平仓失败")}

    def modify_sl_tp(self, account_id: str, ticket: int, sl: float = 0, tp: float = 0) -> dict:
        """修改指定持仓的止损/止盈（追踪止损/保本单用）"""
        resp = self._safe_send(account_id, {
            "cmd": "modify_sl_tp",
            "args": {"ticket": ticket, "sl": sl, "tp": tp}
        })
        if resp.get("ok"):
            _data = resp.get("data")
            return _data if isinstance(_data, dict) else {"error": f"data 异常类型: {type(_data).__name__}"}
        return {"error": resp.get("error", "改单失败")}

    def get_deal_by_position(self, account_id: str, ticket: int, close_time=None, open_price=None, action=None) -> dict:
        """按持仓 ticket 精准拉平仓成交（2026-08-11 对账修复）。

        不依赖 get_recent_deals 的缓存窗口（MT5 断连/重启后历史可能丢失），
        直接 history_deals_get 拉该持仓所有 deal，
        返回其中【平仓 deal】（entry=1/3 带真实盈亏）。
        close_time：该笔平仓时间（datetime/ISO/epoch），用于构造窄窗口精准查询，
                    避免 history_deals_get 宽窗口被 MT5 截断导致匹配不到最新成交。
        返回 {"ticket", "deal": {...}|None, "found"}；失败返回 {"error"}。
        """
        _args = {"ticket": int(ticket)}
        if close_time is not None:
            _args["close_time"] = close_time
        # ★ P1-#4：透传开仓价/方向，供 worker 端对 REAL 券商 deal.price=0 做反推回退
        if open_price is not None:
            _args["open_price"] = float(open_price)
        if action is not None:
            _args["action"] = str(action)
        resp = self._safe_send(account_id, {
            "cmd": "get_deal_by_position",
            "args": _args
        })
        if resp.get("ok"):
            _data = resp.get("data")
            return _data if isinstance(_data, dict) else {"error": f"data 异常类型: {type(_data).__name__}"}
        return {"error": resp.get("error", "按持仓查成交失败")}

    def get_history_deals(self, account_id: str, date_from=None, date_end=None) -> dict:
        """获取指定账号历史成交（仅真实交易，含已实现净盈亏）。

        date_from/date_end 为 datetime 或 ISO 字符串；不传则用最近 90 天。
        """
        return self._query_history_deals(account_id, date_from, date_end)[1]

    def _query_history_deals(self, account_id: str, date_from=None, date_end=None) -> tuple:
        """历史成交查询的唯一底层实现，返回 (查询是否成功, data)。

        ★ 2026-08-07 Phase 1：改走 _safe_send（原先裸调 _send_cmd），
        使这条重查询也享受"管道断开/会话断裂 → 重连重试"的兜底，
        减少偶发超时造成的误判。
        """
        _empty = {"deals": [], "total_profit": 0.0, "count": 0}
        args = {}
        if date_from is not None:
            args["date_from"] = date_from.isoformat() if hasattr(date_from, "isoformat") else date_from
        if date_end is not None:
            args["date_end"] = date_end.isoformat() if hasattr(date_end, "isoformat") else date_end
        resp = self._safe_send(
            account_id, {"cmd": "get_history_deals", "args": args}, timeout=15)
        if resp.get("ok"):
            _d = resp.get("data") or dict(_empty)
            # ★ 2026-08-15 复检P1修复：worker 在 deals=None（查询失败/冷启动未同步）时回
            #   ok=True + raw_count=-1（把失败伪装成"确实无成交"）。此处显式识别
            #   raw_count==-1 → 判失败返回 (False, ...)，让日亏损熔断的失败关闭真正
            #   fail-closed（否则 daily_pnl=0.0 非 None，账号亏穿熔断线时 MT5 抖一下即失效）。
            if isinstance(_d, dict) and _d.get("raw_count") == -1:
                logger.warning(
                    f"[MT5:{account_id}] 历史成交查询失败(worker raw_count=-1) → 判失败关闭"
                )
                return False, dict(_empty)
            return True, _d
        return False, dict(_empty)

    def get_history_deals_checked(self, account_id: str, date_from=None, date_end=None) -> tuple:
        """★ 2026-08-07 Phase 1：可分辨失败的历史成交查询，返回 (ok, data)。

        为什么必须存在：get_history_deals() 失败时返回 {"deals": [], ...}，
        与"今天确实没有成交"同形。风控的日亏损熔断据此算出 daily_pnl=0.0，
        于是 `if daily_pnl is None: 暂缓开仓` 这条失败关闭永远不触发——
        已经亏到熔断线了，MT5 抖一下，熔断当场失效。
        """
        return self._query_history_deals(account_id, date_from, date_end)

    def get_history_orders(self, account_id: str, date_from=None, date_end=None) -> dict:
        """获取指定账号历史订单（订单级别，MT5终端"历史"标签页数据源）。

        Orders = 订单（用户在终端看到的每笔交易记录）
        Deals  = 成交（订单执行后的底层成交，模拟盘可能与orders不同步）
        盈利统计优先用 orders（更准、更及时，与终端显示一致）。
        """
        conn = self._get_conn(account_id)
        if conn is None:
            return {"orders": [], "total_profit": 0.0, "count": 0}
        args = {}
        if date_from is not None:
            args["date_from"] = date_from.isoformat() if hasattr(date_from, "isoformat") else date_from
        if date_end is not None:
            args["date_end"] = date_end.isoformat() if hasattr(date_end, "isoformat") else date_end
        resp = _send_cmd(conn, {"cmd": "get_history_orders", "args": args}, timeout=15, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，ok=True 但缺 data 键即 KeyError→500，改 .get 防御
            return resp.get("data", {"orders": [], "total_profit": 0.0, "count": 0})
        return {"orders": [], "total_profit": 0.0, "count": 0}

    def get_recent_deals(self, account_id: str, limit: int = 20) -> dict:
        """诊断用：获取最近N笔原始deals（含时间/类型/盈利，不过滤）"""
        conn = self._get_conn(account_id)
        if conn is None:
            return {"recent": [], "total_raw": 0}
        resp = _send_cmd(conn, {"cmd": "get_recent_deals", "args": {"limit": limit}}, timeout=15, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            return resp.get("data", {"recent": [], "total_raw": 0})
        return {"recent": [], "total_raw": 0}

    def get_recent_orders(self, account_id: str, limit: int = 20) -> dict:
        """诊断用：获取最近N笔原始orders（订单级别，与deals对比）"""
        conn = self._get_conn(account_id)
        if conn is None:
            return {"recent": [], "total_raw": 0}
        resp = _send_cmd(conn, {"cmd": "get_recent_orders", "args": {"limit": limit}}, timeout=15, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            return resp.get("data", {"recent": [], "total_raw": 0})
        return {"recent": [], "total_raw": 0}

    # ──────────── 行情数据 ────────────

    def get_server_info(self, account_id: str, symbol: str = "XAUUSD") -> dict:
        """从指定 Worker 获取 MT5 服务器时间 + 品种交易时段（星迈时区权威来源）"""
        conn = self._get_conn(account_id)
        if conn is None:
            return {"error": "行情主号未连接"}
        resp = _send_cmd(conn, {"cmd": "get_server_info", "args": {"symbol": symbol}}, timeout=10, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            _d = resp.get("data")
            # ★ 2026-08-15 修复：data 必须 dict 形态（异常时 worker 可能回 list/None），
            #   否则消费端 .get("server_time") 直接 AttributeError 崩掉整个健康接口。
            if not isinstance(_d, dict):
                logger.warning(
                    f"[MT5:{account_id}] get_server_info data 形态异常 {type(_d).__name__}，按失败处理"
                )
                return {"error": "服务器信息返回形态异常"}
            return _d
        return {"error": resp.get("error", "服务器信息获取失败")}

    def get_market_data(self, account_id: str, symbol: str = "XAUUSD") -> dict:
        """从指定 Worker 获取原始行情数据（OHLCV 柱）"""
        conn = self._get_conn(account_id)
        if conn is None:
            return {"error": "行情主号未连接"}
        resp = _send_cmd(conn, {"cmd": "get_market_data", "args": {"symbol": symbol}}, timeout=30, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            return resp.get("data", {"error": resp.get("error", "行情数据获取失败")})
        return {"error": resp.get("error", "行情数据获取失败")}

    # ──────────── 状态查询 ────────────

    def get_tick(self, account_id: str, symbol: str = "XAUUSD") -> dict:
        """从指定 Worker 获取轻量实时报价（bid/ask/spread），供风控点差检查。

        比 get_market_data 更廉价：不拉取 OHLCV 历史柱。
        """
        conn = self._get_conn(account_id)
        if conn is None:
            return {"error": "账号未连接"}
        resp = _send_cmd(conn, {"cmd": "get_tick", "args": {"symbol": symbol}}, timeout=10, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            return resp.get("data", {"error": resp.get("error", "报价获取失败")})
        return {"error": resp.get("error", "报价获取失败")}

    def get_terminal_info(self, account_id: str) -> dict:
        """获取指定 Worker 对应 MT5 终端信息（含算法交易开关状态）"""
        conn = self._get_conn(account_id)
        if conn is None:
            return {"error": "账号未连接"}
        resp = _send_cmd(conn, {"cmd": "get_terminal_info"}, lock_key=account_id)
        if resp.get("ok"):
            # ★ 2026-08-15 审计P1修复：与 L621 同款，.get 防御缺 data 键
            return resp.get("data", {"error": resp.get("error", "未知错误")})
        return {"error": resp.get("error", "未知错误")}

    def get_all_accounts_status(self, account_ids: Optional[set] = None) -> list:
        """获取账号连接状态（可按账号集过滤；默认全部）。

        ★ 2026-08-15 审计P0修复：多租户下 /api/accounts/status 必须按当前 user
          的账号集过滤，禁止把全平台账号连接态泄漏给任意登录用户。
        """
        results = []
        for account_id, (p, conn) in list(self._workers.items()):
            if account_ids is not None and account_id not in account_ids:
                continue
            alive = p.is_alive()
            resp = _send_cmd(conn, {"cmd": "ping"}, timeout=3, lock_key=account_id)
            results.append({
                "account_id": account_id,
                "connected": alive and resp.get("ok", False),
                "process_alive": alive,
                "last_error": resp.get("error", "") if not resp.get("ok") else "",
            })
        return results


# 全局单例
mt5_service = MT5Service()
