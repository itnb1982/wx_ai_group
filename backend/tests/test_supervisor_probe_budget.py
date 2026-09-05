"""supervisor 探针预算不变式回归测试（2026-08-07 事故防复发）

事故背景
--------
2026-08-07 18:26~18:35，后端连续 5 次崩溃循环、10 分钟无法自愈恢复；
而**完全相同的启动命令手动执行却一次成功**。

根因：**监管窗口短于应用自愈窗口**。
  · supervisor 判死预算 = HEALTH_GRACE(25s) + HEALTH_MAX_FAILS(4) × INTERVAL(5s) = 45s
  · 应用 init_db 自愈预算 = INIT_DB_RETRY(6) × (_raw_creator 退避 31.5s + 1.5s) ≈ 198s
  45s < 198s ⇒ uvicorn 每次都在跑完第 2 轮 DB 重试前就被 supervisor 强杀，
  应用自带的「撞 Windows Defender 扫描锁则退避重试」机制**永远没机会生效**；
  而强杀又再次改写 DB 文件、触发 Defender 重新扫描 ⇒ 自我延续的死循环。

修复：启动探针（STARTUP_GRACE）与存活探针（HEALTH_GRACE）分离。

本测试钉死的行为契约
------------------
1. 启动判死预算必须 **严格大于** 应用 init_db 自愈总预算（否则事故必然复发）；
2. 已健康后的存活探测灵敏度不得退化（仍保持 ~45s 快速重启假死进程）；
3. 连续启动失败必须指数退避（不得回退成固定高频重启）。

任何一条被改动都会让本测试失败——这是刻意的护栏，不要"顺手调小"常量。
"""
import os
import re
import sqlite3
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

DATABASE_PY = os.path.join(BACKEND_DIR, "app", "database.py")
SUPERVISOR_PY = os.path.join(BACKEND_DIR, "supervisor.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _db_self_heal_budget_sec() -> float:
    """实测 init_db 在 DB 持续不可写时的最大阻塞预算（秒）。

    ★ 2026-08-08 重写：原实现用正则解析 database.py 源码反推预算。
      那是脆性设计 —— 一旦函数签名或退避写法变动（本次即为 _raw_creator
      加入 sleeper 注入、退避改为 min(...) 形式），测试就以「解析失败」告终，
      而不是以「契约被违反」告终。前者掩盖真正要守护的东西。
      现在 database.py 支持 sleeper 注入，直接**实测真实阻塞时长**：
      既不依赖源码文本形态，测的又是真行为，而且照样零真实等待。
    """
    from app import database

    slept = []

    class _Boom:
        def __init__(self):
            raise sqlite3.OperationalError("attempt to write a readonly database")

    database.init_db(session_factory=_Boom, sleeper=slept.append)
    inner = _worst_case_raw_creator_backoff()
    # init_db 每一轮都可能走满 _raw_creator 的退避，故按最坏叠加
    return sum(slept) + database.init_db.__defaults__[0] * inner


def _worst_case_raw_creator_backoff() -> float:
    """实测 _raw_creator 单次调用在持续 readonly 下的退避总时长。"""
    from app import database

    slept = []

    def always_readonly(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    orig = sqlite3.connect
    sqlite3.connect = always_readonly
    try:
        database._raw_creator(sleeper=slept.append)
    except RuntimeError:
        pass
    finally:
        sqlite3.connect = orig
    return sum(slept)


@pytest.fixture(scope="module")
def sup():
    """import supervisor 取真实常量。

    supervisor.py 的模块级仅做路径计算，实际逻辑全在 `if __name__ == "__main__"` 下，
    import 无副作用（不会拉起 uvicorn）。
    """
    import supervisor  # noqa: PLC0415
    return supervisor


class TestStartupBudgetMustCoverAppSelfHeal:
    """核心契约：supervisor 必须等得起应用自愈，否则 2026-08-07 事故必复发。"""

    def test_startup_kill_budget_exceeds_db_self_heal_budget(self, sup):
        db_budget = _db_self_heal_budget_sec()
        startup_kill_budget = (
            sup.STARTUP_GRACE + sup.HEALTH_MAX_FAILS * sup.HEALTH_CHECK_INTERVAL
        )
        assert startup_kill_budget > db_budget, (
            f"启动判死预算 {startup_kill_budget:.1f}s 未覆盖 DB 自愈预算 {db_budget:.1f}s —— "
            f"uvicorn 会在 init_db 重试跑完前被强杀，2026-08-07 永久崩溃循环将复发。"
            f"请调大 STARTUP_GRACE 或缩短 database.py 的重试预算。"
        )

    def test_startup_grace_alone_covers_most_of_self_heal(self, sup):
        """宽限期本身就应覆盖绝大部分自愈耗时，不能全靠 MAX_FAILS 兜底。"""
        db_budget = _db_self_heal_budget_sec()
        assert sup.STARTUP_GRACE >= db_budget * 0.9, (
            f"STARTUP_GRACE={sup.STARTUP_GRACE}s 相对 DB 自愈预算 {db_budget:.1f}s 余量过薄"
        )

    def test_startup_grace_strictly_longer_than_liveness_grace(self, sup):
        """启动探针必须比存活探针宽松，否则等于没分离。"""
        assert sup.STARTUP_GRACE > sup.HEALTH_GRACE, (
            "STARTUP_GRACE 必须严格大于 HEALTH_GRACE，否则启动/存活探针分离形同虚设"
        )


class TestLivenessSensitivityNotRegressed:
    """反向契约：修复启动问题不得牺牲假死检测灵敏度。"""

    def test_liveness_kill_budget_stays_fast(self, sup):
        liveness_budget = (
            sup.HEALTH_GRACE + sup.HEALTH_MAX_FAILS * sup.HEALTH_CHECK_INTERVAL
        )
        assert liveness_budget <= 60.0, (
            f"已健康后的假死判定预算 {liveness_budget:.1f}s 过长，"
            f"后端无声假死将长时间无人接管（原设计 ~45s）"
        )

    def test_health_probe_interval_reasonable(self, sup):
        assert 1.0 <= sup.HEALTH_CHECK_INTERVAL <= 10.0

    def test_max_fails_still_requires_multiple_misses(self, sup):
        """至少连续 2 次失败才判死，避免单次网络抖动误杀。"""
        assert sup.HEALTH_MAX_FAILS >= 2


class TestRestartBackoffExists:
    """连续启动失败必须指数退避，避免高频重启反复触发杀软扫描锁。"""

    def test_backoff_logic_present_in_source(self):
        src = _read(SUPERVISOR_PY)
        assert "_restart_backoff" in src, "重启退避逻辑缺失（会退化成固定高频重启）"
        assert re.search(r"_restart_backoff\s*=\s*min\(_restart_backoff\s*\*\s*2", src), (
            "重启退避未采用指数增长"
        )

    def test_backoff_resets_after_healthy(self):
        """本轮曾健康则必须复位退避，否则正常假死恢复会被越拖越慢。"""
        src = _read(SUPERVISOR_PY)
        assert re.search(
            r"if became_healthy:\s*\n\s*_restart_backoff\s*=\s*RESTART_DELAY", src
        ), "退避未在进入健康状态后复位"

    def test_backoff_has_upper_bound(self):
        src = _read(SUPERVISOR_PY)
        m = re.search(r"min\(_restart_backoff\s*\*\s*2,\s*([\d.]+)\)", src)
        assert m, "退避缺少上限，可能无限增长导致长时间不重试"
        assert float(m.group(1)) <= 120.0, "退避上限过大，故障恢复过慢"


class TestDiagnosabilityGuard:
    """_raw_creator 的原始异常必须落日志——否则线上永远只能看到笼统文案。"""

    def test_raw_creator_logs_original_exception(self):
        src = _read(DATABASE_PY)
        # 允许 _raw_creator 带参数（sleeper/attempts 注入），只锁「必须落日志 + 携带真因」
        m = re.search(r"def _raw_creator\([^)]*\).*?raise RuntimeError\(", src, re.S)
        assert m, "未找到 _raw_creator"
        body = m.group(0)
        assert "logger.warning" in body, (
            "_raw_creator 失败时未落日志，原始异常被吞将导致故障无法定位"
        )
        assert "_last_err" in body, "最终异常未携带最后一次真实原因"

    def test_database_logs_go_to_project_logger(self):
        """database.py 必须用 loguru，不能用标准 logging。

        2026-08-08 事故：database.py 当时是 logging.getLogger("db")，
        而全项目日志走 loguru —— init_db 疯狂重试 198 秒，
        日志里**一条记录都没有**。不可观测的失败等于没有失败处理。
        """
        src = _read(DATABASE_PY)
        assert "from loguru import logger" in src, (
            "database.py 未接入 loguru，DB 故障将不会出现在项目日志中"
        )
        assert not re.search(r"^logger\s*=\s*logging\.getLogger", src, re.M), (
            "database.py 仍在用标准 logging，日志会进黑洞（事故复发风险）"
        )
