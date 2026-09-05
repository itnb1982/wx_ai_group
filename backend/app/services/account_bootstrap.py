"""
启动期 MT5 账号接入与自愈（Phase 2 / 启动韧性）

━━━ 为什么要把这段逻辑从 lifespan 里搬出来 ━━━
2026-08-08 00:09 生产事故：后端重启时 DB 撞上一次瞬时锁
（旧进程被 kill 残留 wx_prod.dat-journal + Defender 扫描 34MB 库文件），
`SessionLocal()` 抛 readonly，被 lifespan 最外层 `except Exception` 吞掉，
结果 **4 个客户账号一个都没接上，且此后永不重试** —— 而事后探测同一个库
0.00s 可写，说明那只是一次几十秒就会自愈的抖动。

旧结构三重放大了这次抖动：
  ① 取列表是单点，失败即全员失败（per-account 的 try/except 根本没机会跑）；
  ② lifespan 层没有重试，`_raw_creator` 那 31.5s 退避耗尽就彻底放弃；
  ③ 失败后无任何补偿，进程可以一路"健康"运行，却一单也下不出去。

更致命的是它**不声张**：health 返回 status=ok，DB 里 4 行还留着上次会话的
is_connected=1/ONLINE，前端照样 4 个绿灯。多租户下这 4 行是 4 个独立客户，
运维和客户都看不出问题 —— 属于赔付级的静默失败。

━━━ 本模块的四条设计承诺 ━━━
  1. **取列表必须重试**：DB 瞬时锁是 Windows + Defender 环境下的已知常态，
     不是异常事件，必须当作正常路径扛过去。
  2. **逐账号隔离**：一个客户的终端僵死/密文损坏，绝不许拖累其他客户。
  3. **状态如实落库**：连不上就写 ERROR。宁可让前端显示红灯，
     也绝不允许留着陈旧的 ONLINE 假装在线。
  4. **永不放弃**：还有账号没连上，就起后台守护持续重试；
     全部连上后守护自行退出，不空转。

外加一条铁律：**bootstrap 永不向 lifespan 抛异常**。
一旦抛出去，整个后端服务起不来，那比少接几个账号严重得多。
所有依赖（session/解密/连接器/sleep）都以参数注入，便于确定性测试。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from loguru import logger


# ────────────────────────────── 数据结构 ──────────────────────────────
@dataclass
class AccountSnapshot:
    """从 ORM 行里摘出连接真正需要的字段（凭据已解密）。

    带上 row 引用是为了连接结束后就地回写真实状态 —— 这正是防止
    "陈旧 ONLINE 骗前端" 的关键；解耦成纯数据反而会丢掉回写通道。
    """

    id: str
    login: str
    password: str
    server: str
    name: str
    terminal_path: str = ""
    row: object = None


@dataclass
class BootstrapResult:
    total: int = 0
    connected: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    load_error: Optional[str] = None

    @property
    def needs_retry(self) -> bool:
        """还有没接上的账号，或压根没读到列表 —— 两者都必须继续自愈。"""
        return bool(self.failed) or self.load_error is not None

    @property
    def all_connected(self) -> bool:
        return self.load_error is None and not self.failed

    def summary(self) -> str:
        if self.load_error:
            return f"账号列表读取失败({self.load_error[:120]})，已转入后台重试"
        return f"账号接入 {len(self.connected)}/{self.total}" + (
            f"，失败: {','.join(self.failed)}" if self.failed else ""
        )


# ────────────────────────────── 内部工具 ──────────────────────────────
def _is_transient(exc: BaseException) -> bool:
    """判断是否属于「等一会儿就会好」的抖动。

    readonly / locked / unable to open 三类全部来自 Windows 文件锁与
    Defender 扫描，都是瞬时的；其余（如表不存在）重试再多次也没用。
    """
    s = str(exc).lower()
    return any(
        k in s
        for k in ("readonly", "database is locked", "unable to open", "disk i/o", "busy")
    )


def _mark_row(row, connected: bool, message: str = "") -> None:
    """把真实连接结果就地写回 ORM 行。

    容忍 AccountStatus 枚举不可导入的场景（回退字符串），
    因为这里绝不能因为一个 import 失败而中断整个接入流程。
    """
    if row is None:
        return
    try:
        row.is_connected = bool(connected)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.models.mt5_account import AccountStatus

        row.status = AccountStatus.ONLINE if connected else AccountStatus.ERROR
    except Exception:  # noqa: BLE001
        try:
            row.status = "ONLINE" if connected else "ERROR"
        except Exception:  # noqa: BLE001
            pass
    if message:
        try:
            row.status_message = str(message)[:200]
        except Exception:  # noqa: BLE001
            pass


def load_accounts(
    session_factory: Callable,
    decryptor: Callable[[str], str],
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    sleeper: Callable[[float], None] = time.sleep,
):
    """读取全部已保存账号，DB 瞬时抖动时重试。

    返回 (session, snapshots, error)：
      · 成功 → (打开着的 session, [...], None)
      · 失败 → (None, [], "原因")

    ★ session 刻意**不在此处关闭**：调用方连接完账号后要用同一个 session
      回写真实状态。SQLite 的读事务在 .all() 之后即结束，不会长期持锁。
    """
    last_err: Optional[BaseException] = None
    for i in range(max(1, attempts)):
        session = None
        try:
            from app.models.mt5_account import MT5Account

            session = session_factory()
            rows = session.query(MT5Account).all()
            snaps: List[AccountSnapshot] = []
            for r in rows:
                # 解密失败只影响该客户，不能让整批陪葬
                try:
                    pwd = decryptor(r.password) if r.password else ""
                except BaseException as de:  # noqa: BLE001
                    logger.warning(f"[启动] 账号 {getattr(r, 'name', '?')} 凭据解密失败: {de}")
                    _mark_row(r, False, f"凭据解密失败: {de}")
                    snaps.append(
                        AccountSnapshot(
                            id=r.id, login="", password="", server="",
                            name=getattr(r, "name", "?"), terminal_path="", row=r,
                        )
                    )
                    continue
                snaps.append(
                    AccountSnapshot(
                        id=r.id,
                        login=r.account_id,
                        password=pwd,
                        server=r.server,
                        name=getattr(r, "name", "?"),
                        terminal_path=getattr(r, "terminal_path", "") or "",
                        row=r,
                    )
                )
            return session, snaps, None
        except BaseException as e:  # noqa: BLE001
            last_err = e
            try:
                if session is not None:
                    session.close()
            except BaseException:  # noqa: BLE001
                pass
            if i < attempts - 1:
                delay = min(base_delay * (2 ** i), max_delay)
                logger.warning(
                    f"[启动] 读取账号列表第 {i + 1}/{attempts} 次失败"
                    f"({type(e).__name__}: {str(e)[:160]})，{delay:.1f}s 后重试"
                )
                try:
                    sleeper(delay)
                except BaseException:  # noqa: BLE001
                    pass
                if not _is_transient(e):
                    # 非瞬时错误重试也是白费，但仍给一次机会后即止损
                    break
    return None, [], f"{type(last_err).__name__}: {last_err}" if last_err else "unknown"


def connect_accounts(
    snapshots: Sequence[AccountSnapshot],
    connector: Callable[..., bool],
    only_ids: Optional[Sequence[str]] = None,
) -> BootstrapResult:
    """逐个接入账号；任何单账号异常都被隔离，并把真实结果写回 ORM 行。"""
    res = BootstrapResult(total=len(snapshots))
    wanted = set(only_ids) if only_ids is not None else None
    for s in snapshots:
        if wanted is not None and s.id not in wanted:
            continue
        if not s.login:  # 解密阶段已判死，直接计入失败
            res.failed.append(s.id)
            continue
        try:
            ok = bool(
                connector(
                    account_id=s.id,
                    login=s.login,
                    password=s.password,
                    server=s.server,
                    name=s.name,
                    terminal_path=s.terminal_path,
                )
            )
            _mark_row(s.row, ok, "" if ok else "启动接入失败，已转入后台重试")
            (res.connected if ok else res.failed).append(s.id)
            logger.info(f"[启动] MT5账号 {s.name} 接入: {'成功' if ok else '失败'}")
        except BaseException as e:  # noqa: BLE001
            _mark_row(s.row, False, f"接入异常: {e}")
            res.failed.append(s.id)
            logger.warning(f"[启动] MT5账号 {s.name} 接入异常: {e}")
    return res


def bootstrap(
    session_factory: Callable,
    decryptor: Callable[[str], str],
    connector: Callable[..., bool],
    attempts: int = 5,
    base_delay: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> BootstrapResult:
    """启动期账号接入总入口。**任何情况下都不会抛异常。**"""
    session = None
    try:
        session, snaps, err = load_accounts(
            session_factory, decryptor, attempts=attempts,
            base_delay=base_delay, sleeper=sleeper,
        )
        if err is not None:
            return BootstrapResult(load_error=err)
        res = connect_accounts(snaps, connector)
        # 状态回写失败不影响连接结论：账号该在线还是在线，只是前端可能显示滞后
        try:
            if session is not None:
                session.commit()
        except BaseException as ce:  # noqa: BLE001
            logger.warning(f"[启动] 账号状态回写失败(不影响已建立的连接): {ce}")
        return res
    except BaseException as e:  # noqa: BLE001
        # 铁律：绝不把异常放回 lifespan —— 那会让整个后端起不来
        logger.warning(f"[启动] 账号接入流程意外中止: {type(e).__name__}: {e}")
        return BootstrapResult(load_error=f"{type(e).__name__}: {e}")
    finally:
        try:
            if session is not None:
                session.close()
        except BaseException:  # noqa: BLE001
            pass


# ────────────────────────────── 自愈守护 ──────────────────────────────
def start_reconnect_daemon(
    session_factory: Callable,
    decryptor: Callable[[str], str],
    connector: Callable[..., bool],
    pending_ids: Optional[Sequence[str]] = None,
    interval: float = 30.0,
    max_interval: float = 300.0,
    max_rounds: int = 0,
    only_if_needed: bool = True,
    on_settled: Optional[Callable[[BootstrapResult], None]] = None,
) -> Optional[threading.Thread]:
    """为未接入的账号起后台重试守护；全部接上后自行退出。

    · pending_ids=None → 首轮把读到的账号全当作待接入（启动读列表就失败时的情形）。
    · max_rounds=0     → 不限轮次（生产默认：抖动可能持续很久，但总会好）。
    · only_if_needed   → 无事可做时返回 None，不白起线程。
    """
    if only_if_needed and pending_ids is not None and not pending_ids:
        return None

    if only_if_needed and pending_ids is None:
        # 先廉价探一次：一个账号都没配置（全新部署）就不必起线程
        probe_session = None
        try:
            probe_session, snaps, err = load_accounts(
                session_factory, decryptor, attempts=1, sleeper=lambda _s: None
            )
            if err is None and not snaps:
                return None
        except BaseException:  # noqa: BLE001
            pass
        finally:
            try:
                if probe_session is not None:
                    probe_session.close()
            except BaseException:  # noqa: BLE001
                pass

    stop_evt = threading.Event()

    def _loop():
        delay = interval
        rounds = 0
        remaining = list(pending_ids) if pending_ids is not None else None
        while not stop_evt.is_set():
            rounds += 1
            session = None
            try:
                session, snaps, err = load_accounts(
                    session_factory, decryptor, attempts=2, base_delay=1.0,
                    sleeper=stop_evt.wait,
                )
                if err is None:
                    if remaining is None:
                        remaining = [s.id for s in snaps]
                    res = connect_accounts(snaps, connector, only_ids=remaining)
                    try:
                        if session is not None:
                            session.commit()
                    except BaseException:  # noqa: BLE001
                        pass
                    if res.connected:
                        logger.info(
                            f"[自愈] 账号重连成功 {len(res.connected)} 个: "
                            f"{','.join(res.connected)}"
                        )
                    remaining = [i for i in remaining if i not in set(res.connected)]
                    if not remaining:
                        logger.info(f"[自愈] 全部账号已接入（第 {rounds} 轮），守护退出")
                        if on_settled:
                            try:
                                on_settled(res)
                            except BaseException:  # noqa: BLE001
                                pass
                        return
            except BaseException as e:  # noqa: BLE001
                # 守护线程自己死掉 = 自愈通道断裂，比任何单次失败都严重
                logger.warning(f"[自愈] 重连轮次异常(继续重试): {type(e).__name__}: {e}")
            finally:
                try:
                    if session is not None:
                        session.close()
                except BaseException:  # noqa: BLE001
                    pass

            if max_rounds and rounds >= max_rounds:
                logger.warning(f"[自愈] 达到最大轮次 {max_rounds}，守护退出")
                return
            stop_evt.wait(delay)
            delay = min(delay * 1.5, max_interval)

    t = threading.Thread(target=_loop, name="account-reconnect-daemon", daemon=True)
    t.stop_event = stop_evt  # type: ignore[attr-defined]
    t.start()
    return t
