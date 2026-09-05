"""pytest 全局夹具。

关键职责：把 backend/ 加入 sys.path，使测试能 `from app.services... import ...`，
且不依赖当前工作目录，pytest 从项目任意位置调用都能跑。
"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ══════════════════════════════════════════════════════════════════
# ★ 测试数据库隔离（2026-08-10 P0 修复）
#
# 背景：test_exit_query_integrity.py 等集成测试真实实例化 TradeExecutor
#   （account_id="acc_exit_0001"），其 _safe_db_write() 走独立 SessionLocal()
#   写库 —— 绕过了 fixture 里的 db=MagicMock()，直接写进生产库
#   backend/data/wx_prod.dat（ticket=123456 / price=2005.0 假数据当天
#   就混入生产 trades 表）。此前已清 164 条，跑一轮回归又写入 7 条。
#
# 修复：conftest 在本模块 import 阶段（任何 `from app...` 之前）强制
#   DATABASE_URL 指向临时测试库。app/database.py 在 import 时读取
#   settings.get_database_url() 求值，此时 env 已生效 → 全部 SQLAlchemy
#   引擎（含写引擎）落点 = 临时库，生产库零接触。
#
# 注意：必须放在 sys.path 之后、任何 app 模块 import 之前。
#   os.environ 设置对已 import 的模块无效（database.py 在模块级缓存路径）。
# ══════════════════════════════════════════════════════════════════
_TEST_DB = os.environ.get("WX_TEST_DB")  # 允许外部覆盖（CI 等场景）
if not _TEST_DB:
    _TEST_DB = os.path.join(tempfile.gettempdir(), "wx_pytest_%d.dat" % os.getpid())
os.environ["DATABASE_URL"] = "sqlite:///%s" % _TEST_DB.replace("\\", "/")
# 注意：DATA_DIR 不隔离（2026-08-10 实测 test_chronos_probe_isolation 依赖
#   真实 DATA_DIR 下的 torch_probe_cache.json 预置文件，重定向会误伤）。
#   写库污染的根因是 DATABASE_URL，隔离它即可；日志/缓存写真实目录无害。


def pytest_sessionstart(session):  # noqa: ARG001
    """测试会话开始：在隔离测试库上建全量表。

    ★ 2026-08-17 修复：此前只有 DATABASE_URL 隔离、从未 create_all →
    测试库只有 _wprobe 表，`no such table: trades` 让 emergency_integration
    （_safe_db_write 真实写 trades 表）等集成测试全部挂掉。
    """
    from app.database import engine
    from app.models import Base
    import app.models.trade  # noqa: F401  确保模型注册进 Base.metadata
    import app.models.trade_exit  # noqa: F401
    import app.models.runtime_config  # noqa: F401

    Base.metadata.create_all(engine)



def pytest_unconfigure(config):  # noqa: ARG001
    """会话结束时摘掉 loguru 的所有 sink。

    不这么做的后果（2026-08-08 实测）：loguru 在 import 时就绑定了当时的
    sys.stderr，而 pytest 会在收尾阶段把自己捕获的 stderr 关掉。之后任何
    一条日志（例如 MetaAgent 退出时持久化权重打的 DEBUG）都会撞上
    `ValueError: I/O operation on closed file`，刷出几十行 traceback，
    **把 pytest 自己的 "N passed" 汇总行整个冲掉**——跑完测试看不到结果，
    还以为是崩了。

    这不是测试逻辑问题，纯粹是日志器和捕获器的生命周期没对齐。
    在这里主动断开即可，对生产运行没有任何影响。

    ── 另一个容易与之混淆的现象（2026-08-08 踩过）─────────────
    如果跑测试时看不到 "N passed" 汇总行，先别怀疑本函数：
    pytest.ini 的 addopts 里**已经有 -q**，命令行再传一个 -q 会叠成 -qq，
    而 -qq 会直接静音汇总行。正确跑法是 `python -m pytest`（不加 -q）。
    两者症状一样（看不到汇总），成因完全不同，别修错地方。
    """
    try:
        from loguru import logger

        logger.remove()
    except Exception:  # noqa: BLE001
        pass
