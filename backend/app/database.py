"""
XAU/USD万象Ai自动量化交易系统 — 数据库连接与Session管理
使用 SQLite (无需额外安装数据库服务)
"""
import time
import threading
import sqlite3
from typing import Optional
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import settings

# ★ 2026-08-08 事故修复：原来这里是 logging.getLogger("db")，而全项目日志走 loguru。
#   后果：生产重启时 init_db 疯狂重试 198 秒，日志里**一条记录都没有** ——
#   排障时只能靠猜（是只读？锁定？路径不对？权限？）。
#   不可观测的失败等于没有失败处理。统一到 loguru，任何 DB 异常必须留痕。
from loguru import logger

_DATABASE_URL = settings.get_database_url()
# 提取原生文件路径（供 raw sqlite3 引擎用）
_DB_FILE = _DATABASE_URL.replace("sqlite:///", "").replace("sqlite:", "").strip()


def _on_connect(dbapi_conn, conn_record):
    """每个新连接建立时统一设置 SQLite 调优参数。

    2026-08-12 修正：恢复 WAL 模式（写引擎已在 2026-08-07 启用 WAL，此处必须对称，
      否则主引擎连接以 DELETE 模式频繁把库切回 DELETE，与写引擎 WAL 互相打架 → 持续锁竞争）。
      实际本系统为「1 主 uvicorn + 6 个 MT5 Worker 子进程」并发写同一 SQLite 文件，
      存在真实多进程并发写，WAL 的「读写不互斥」正是对症解法。
    """
    try:
        cur = dbapi_conn.cursor()
        # 与写引擎完全一致的 WAL 模式：读写互不阻塞，根治 database is locked
        cur.execute("PRAGMA journal_mode=WAL")
        # 持锁等待上限提到 30s（与写引擎同值），避免瞬时并发写雪崩
        cur.execute("PRAGMA busy_timeout=30000")
        # WAL 下限制 -wal 文件大小，避免极端情况下 WAL 无限膨胀
        cur.execute("PRAGMA journal_size_limit=67108864")
        # NORMAL：保证持久性，减少不必要 fsync（性能）
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"DB 连接初始化 PRAGMA 失败（可忽略，下次连接重试）: {e}")


# ── 连接创建器：原生 sqlite3 + 可写性探测 + 正确读写共享模式 ──
# 主引擎与写引擎共用此 creator，确保任何连接建立时即校验可写，
# 只读连接（启动期撞 Windows/Defender 瞬锁时可能建成）根本不会进入连接池。
# ── 连接重试预算（2026-08-08 事故后收敛）─────────────────────────────
# 事故值：6 次 × 0.5×2^n = 0.5+1+2+4+8+16 = 31.5s。
# 这个数字本身看着还行，但它会被 init_db 的重试次数**再乘一遍**（×6 = 198s），
# 直接把启动时间推过 supervisor 的判死线，形成重启死循环。
# 收敛依据：readonly 的现实成因是 hot journal 残留与杀软瞬时扫描锁，
# 均在数秒内自行释放；等 31.5s 既救不回真故障，又拖死启动。
# 真正的长期自愈交给后台守护（start_db_selfheal_daemon），不占用启动路径。
_CREATOR_ATTEMPTS = 4
_CREATOR_BASE_DELAY = 0.4
_CREATOR_MAX_DELAY = 2.0

# 模块级睡眠钩子：让「启动总预算」可被端到端实测。
# 为什么不直接拿 time.sleep 当默认参数：默认参数在**函数定义时**求值，
# 测试 monkeypatch 模块属性对它无效 —— 那样写出的预算测试是假绿的。
# 实测教训：反向验证时把重试次数改回事故值 6 次，预算测试竟然照样通过，
# 因为它只覆盖到 init_db 自己的 1.5s，漏掉了 _raw_creator 的 31.5s 大头。
_SLEEPER = time.sleep


def _raw_creator(sleeper=None, attempts: int = _CREATOR_ATTEMPTS,
                 base_delay: float = _CREATOR_BASE_DELAY):
    """为写引擎创建原生 sqlite3 连接（永远可写）。

    2026-08-06 根因定论 + 根治：
      现象：uvicorn 进程内以普通 sqlite3.connect(路径) 打开文件库时，Windows 下
      SQLite 会因共享模式回退为「只读打开」(attempt to write a readonly database)，
      而独立进程写同一文件却 100% 成功——故根因在「进程内连接建立方式」而非文件锁/
      Defender/句柄泄漏（均已逐一排除）。
      旧"可写性探测"写的是 TEMP 临时表，根本不碰主库文件 → 探测通过≠主库可写，
      属误判。
      根治：改用 SQLite URI 强制读写模式（mode=rwc）。mode=rwc 在无法获得写权限时
      会**直接报错**（而非回退只读），从而被 NullPool 重试消化；同时探测改用**主库
      真实临时表**（写主库文件，不污染业务表），确保探测通过即主库真可写。
    """
    from urllib.parse import quote as _urlquote
    # Windows 绝对路径含冒号/空格，必须 urlquote 后拼成合法 sqlite URI
    _uri = "file:" + _urlquote(_DB_FILE) + "?mode=rwc&uri=true"
    # 2026-08-07 可诊断性加固：此前重试全失败只抛一句笼统文案，原始异常被完全吞掉，
    # 线上「服务起不来」无法定位（只读？锁定？路径不存在？权限？全靠猜）。
    # 现改为：每次失败即刻落日志（带 URI + 原始异常类型与内容），最终异常携带最后一次真因。
    _last_err = None
    for _try in range(attempts):
        conn = None
        try:
            conn = sqlite3.connect(_uri, uri=True, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            # WAL 下限制 -wal 文件大小，避免极端情况下 WAL 无限膨胀
            conn.execute("PRAGMA journal_size_limit=67108864")
            # 真实可写性探测：在主库建临时探测表并写入（提交即删，不污染业务表）
            conn.execute("CREATE TABLE IF NOT EXISTS _wprobe(id INTEGER PRIMARY KEY,v INTEGER)")
            conn.execute("INSERT INTO _wprobe(v) VALUES (1)")
            conn.execute("DELETE FROM _wprobe WHERE v=1")
            conn.commit()
            return conn
        except Exception as e:  # noqa: BLE001
            _last_err = e
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                f"_raw_creator 第 {_try + 1}/{attempts} 次失败: "
                f"{type(e).__name__}: {e} | uri={_uri}"
            )
            s = str(e).lower()
            if "readonly" in s or "database is locked" in s or "unable to open" in s:
                # 最后一次失败不再空等：直接落到下方 RuntimeError，
                # 把「继续等」的决策权交给调用方（启动路径要快，后台守护才慢慢磨）。
                if _try < attempts - 1:
                    _sleep = sleeper if sleeper is not None else _SLEEPER
                    _sleep(min(base_delay * (2 ** _try), _CREATOR_MAX_DELAY))
                continue
            raise
    raise RuntimeError(
        f"写引擎连接创建失败：DB 持续只读/锁定/无法打开 | uri={_uri} | "
        f"最后真因: {type(_last_err).__name__}: {_last_err}"
    )


# ── 主引擎：SQLAlchemy ORM（用于查询、复杂事务、FastAPI 依赖注入）───────────
# 2026-08-06 根治：主引擎也改用 _raw_creator（URI mode=rwc 强读写 + 真实可写性探测）。
# 根因：普通 sqlite3.connect(路径) 在 Windows 下可能以「只读」方式打开（回退而非报错），
# 导致所有写报 readonly database；改用 URI mode=rwc 强制读写，无法获得写权限时直接报错，
# 被 NullPool 重试消化。统一走 _raw_creator 后，任何连接建立时即校验主库真可写。
engine = create_engine(
    "sqlite:///",
    creator=_raw_creator,
    echo=settings.DEBUG,
    pool_pre_ping=False,
    poolclass=NullPool,
)
# 每个新连接（NullPool 下即每次写）都应用调优（_raw_creator 已含 journal/busy_timeout）
event.listen(engine, "connect", _on_connect)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ── 写引擎：原生 sqlite3（2026-08-05 新增，绕过 SQLAlchemy 只读问题）─────────
# 实测证明：uvicorn 长驻进程下 SQLAlchemy NullPool 间歇报"readonly database"
# 而同一进程/同 DB 的原生 sqlite3.connect() 30 秒 15 次写入全部成功（0 失败）
# 故所有关键写入（key_pool / 反向对账落库 / 扫描器持久化）走此路径。
write_engine = create_engine(
    "sqlite:///",
    creator=_raw_creator,
    poolclass=NullPool,
    echo=False,
    pool_pre_ping=False,
)
WriteSession = sessionmaker(autocommit=False, autoflush=False, bind=write_engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖注入: 获取数据库会话。

    2026-08-06 修正：统一走 write_engine（原生 sqlite3 creator）。
      原因：SessionLocal 绑定的 SQLAlchemy engine 在 Windows/Defender 扫描下
      间歇报 "attempt to write a readonly database"，导致策略保存/设主号等
      写接口返回 HTTP 500。write_engine 使用原生 sqlite3 连接 + 15s busy_timeout，
      实测在同等环境下写入成功率显著提高；读操作亦完全兼容。
    """
    db = WriteSession()
    try:
        yield db
    finally:
        db.close()


# 瞬态写锁错误（Windows/Defender 实时扫描会间歇把 .dat 文件标记为只读或加锁）
_TRANSIENT_WRITE_ERRS = ("readonly", "database is locked")


def safe_commit(db, apply=None, max_retries: int = 6, base_delay: float = 0.5):
    """健壮提交：消化 Windows/Defender 间歇写锁（readonly / database is locked）。

    这是根治"策略保存/设主号偶尔 500 或返回 200 但不落库"的关键。

    设计要点：
    - apply: 可选无参函数，每次提交尝试前重放所有字段赋值。
      因为瞬锁失败后必须 rollback，rollback 会丢弃 session 的待提交改动，
      不重放就会"返回 200 但没落库"。把赋值包进 apply 即可幂等重放。
    - 指数退避（0.5,1,2,4,8s…）覆盖 Defender 突发扫描窗口（常持续数秒）。
    - 非瞬锁错误（约束冲突等）立即原样抛出，不掩盖真问题。
    返回 True 表示提交成功；彻底失败则抛出异常。
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            if apply is not None:
                apply()
            db.commit()
            return True
        except Exception as e:  # noqa: BLE001
            s = str(e).lower()
            if any(k in s for k in _TRANSIENT_WRITE_ERRS) and attempt < max_retries - 1:
                # 回滚待提交改动（rollback 后 apply 可幂等重放，避免"200 不落库"）
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                # 注意：此处不 close 整个 session —— 否则会破坏 get_current_user
                # 绑定的 User 对象与依赖链（曾导致 "Instance is not bound to a Session"）。
                # 连接可写性由 _raw_creator 的可写性探测保证：新连接建立时即校验可写，
                # 被污染成只读的连接根本不会进入连接池，故同一 session 内重试即可成功。
                time.sleep(base_delay * (2 ** attempt))
                last_err = e
                continue
            # 非瞬锁错误（约束冲突等）：原样抛出，不掩盖真问题
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass
    raise last_err


def init_db(max_retry: int = 2, retry_interval: float = 1.0,
            session_factory=None, sleeper=None, metadata=None):
    """初始化数据库表 —— 启动路径，必须快。

    ★ 2026-08-08 重写（生产重启死循环事故）
    ────────────────────────────────────────────────────────────────
    旧实现 max_retry=6 / retry_interval=1.5，配合当时 _raw_creator 的 31.5s
    退避，最坏耗时 6 × (31.5 + 1.5) = **198s** —— 与事故日志实测的
    198.055s 分毫不差。加上账号接入的同步重试，总启动 265s，越过 supervisor
    的 260s 判死线，被强杀重启，如此往复形成死循环；而强杀又留下 hot journal，
    让下一轮更容易撞 readonly，自我强化。

    设计原则的转变：
      旧：在启动路径上死等，直到 DB 可写为止（把可用性赌在一次抖动上）
      新：**启动路径快速认输，长期自愈交给后台守护**
          —— 服务先起来（健康检查如实报 degraded），DB 好了再自动补齐。
          这正是 readiness / liveness 分离的标准做法。

    契约：永不抛异常。抛了 lifespan 就断，整个服务起不来。

    参数（依赖注入，仅为可测性；生产调用保持无参）：
      session_factory: 会话工厂，默认 WriteSession
      sleeper:         睡眠函数，默认 time.sleep
      metadata:        元数据对象，默认 Base.metadata
    """
    factory = session_factory or WriteSession
    meta = metadata if metadata is not None else Base.metadata
    _sleep = sleeper if sleeper is not None else _SLEEPER
    last_err = None
    for attempt in range(1, max_retry + 1):
        db = None
        try:
            # 用写引擎建表（与请求内写路径同源，避免 SessionLocal 的 engine 残留只读连接）
            db = factory()
            meta.create_all(bind=db.bind)
            db.commit()
            if attempt > 1:
                logger.info(f"init_db 第 {attempt} 次重试成功")
            return True
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"init_db 第 {attempt}/{max_retry} 次失败: {type(e).__name__}: {e}")
            # 最后一次失败不再空等 —— 立刻把控制权还给启动流程
            if attempt < max_retry:
                _sleep(retry_interval)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
    logger.error(
        f"init_db 启动期未能建表（将转入后台自愈，服务以 degraded 状态先行启动）: "
        f"{type(last_err).__name__}: {last_err}"
    )
    return False


def start_db_selfheal_daemon(session_factory=None, interval: float = 15.0,
                             max_interval: float = 120.0, max_rounds: int = 0,
                             only_if_needed: bool = True, sleeper=time.sleep,
                             metadata=None, on_ready=None) -> Optional[threading.Thread]:
    """DB 未就绪时，在后台持续把它救回来。

    这是「启动期快速认输」的**必要配套**：只快速失败而没有后台补偿，
    等于把「卡死几分钟」换成「永久瘫痪」—— 那更糟。

    行为：
      - only_if_needed=True 时先探一次，DB 本来就好则不起线程（不留常驻空转）
      - 指数退避重试（interval → max_interval），成功即回调 on_ready 并自行退出
      - max_rounds=0 表示不限轮次（生产默认，直到救回为止）
      - daemon 线程：不阻塞进程退出
      - 自身任何异常都不得让线程静默消失

    返回：守护线程；无需自愈时返回 None。
    """
    factory = session_factory or WriteSession
    meta = metadata if metadata is not None else Base.metadata

    def _try_once() -> bool:
        db = None
        try:
            db = factory()
            meta.create_all(bind=db.bind)
            db.commit()
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    if only_if_needed and _try_once():
        logger.info("[db] 数据库已就绪，无需启动自愈守护")
        return None

    def _loop():
        delay = interval
        rounds = 0
        while True:
            rounds += 1
            try:
                if _try_once():
                    logger.info(f"[db] 后台自愈成功（第 {rounds} 轮），数据库已就绪")
                    if on_ready is not None:
                        try:
                            on_ready()
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"[db] 自愈回调异常（不影响自愈结果）: {e}")
                    return
                logger.warning(f"[db] 后台自愈第 {rounds} 轮未成功，{delay:.0f}s 后重试")
            except Exception as e:  # noqa: BLE001
                # 守护线程绝不能因意外异常静默死掉
                logger.warning(f"[db] 自愈守护第 {rounds} 轮异常: {type(e).__name__}: {e}")
            if max_rounds and rounds >= max_rounds:
                logger.error(f"[db] 后台自愈达到最大轮次 {max_rounds}，停止重试")
                return
            sleeper(delay)
            delay = min(delay * 2, max_interval)

    t = threading.Thread(target=_loop, name="db-selfheal", daemon=True)
    t.start()
    logger.warning(f"[db] 数据库未就绪，已启动后台自愈守护（{interval:.0f}s 起，退避至 {max_interval:.0f}s）")
    return t


def dispose_all_engines():
    """启动流程末尾强制释放主进程级 DB 引擎连接池。

    2026-08-06 说明：readonly 根因已定位为「uvicorn 进程内普通 sqlite3.connect(路径)
    在 Windows 上回退为只读打开」，已由 _raw_creator 改用 URI mode=rwc 强制读写根治。
    本函数保留为防御性收尾——丢弃启动期建立的任何连接池，使请求期走 NullPool 全新
    连接（且每条连接都经过可写性探测）。Windows 下 psutil 返回的 fd 为 -1，无法跨进程
    关闭句柄，故不再做 os.close hack（无效）。
    """
    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    try:
        write_engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    import gc
    gc.collect()
    logger.info("[db] 已 dispose 主进程 DB 引擎连接池")
