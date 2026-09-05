"""
健康检查必须说真话：账号失联时不许再返回 status=ok（Phase 2 / 可观测性）

═══ 事故现场原样（2026-08-08 00:09，pid 27444）═══
    GET /api/health →
    {"status":"ok", "mt5_connected":0, "auto_loop_running":true, "trade_stale":false}

进程活着、主循环 44.2s 一轮转得好好的，于是 status 判定为 ok。
可 `mt5_connected: 0` —— 4 个客户账号一个都没接上，一单也下不出去。

旧判定只看两件事：
    trade_stale = auto_running and cycle_gap > 180
    status = "ok" if (uptime > 0 and not trade_stale) else "degraded"
**完全不看有几个账号该在线、实际在线几个。**
循环空转（没有账号可交易，自然每轮都很快"完成"）反而让 trade_stale 更漂亮。

配合 DB 里那 4 行陈旧的 is_connected=1/ONLINE（前端 4 个绿灯），
系统在三个地方同时撒谎。多租户下这 4 行是 4 个独立客户全天不交易 ——
监控绿灯 + 业务全停，是最危险的静默失败形态。

═══ 为什么敢让它变 degraded（不会引发重启风暴）═══
supervisor.py:228 明确写着「status=degraded 不算失败，不重启」，
它只探端点可达性。这点是本修复成立的前提：否则会变成
"账号连不上 → 判 degraded → 重启进程 → 还是连不上"的自我延续死循环，
而重启根本治不好 MT5 连接（治它的是 account_bootstrap 的后台自愈守护）。

═══ 判定口径：少一个都算降级 ═══
不用"全断才算降级"，因为多租户下少的那一个就是某位真实客户全天不交易。
3/4 在线对平台是小事，对第 4 位客户是 100% 的事故。
"""
import sys

import pytest

import app.main as m

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_state():
    """health 读的是模块级 _ACCOUNT_STATE / _DB_STATE，用完必须还原，避免污染其他用例。"""
    saved = dict(m._ACCOUNT_STATE)
    saved_db = dict(m._DB_STATE)
    yield
    m._ACCOUNT_STATE.clear()
    m._ACCOUNT_STATE.update(saved)
    m._DB_STATE.clear()
    m._DB_STATE.update(saved_db)


def _health(monkeypatch, *, expected, connected, auto_running=True, cycle_gap=10.0,
            db_ready=True):
    """把 health 依赖的外部量固定住，只考察它的判定逻辑。

    db_ready 默认 True：本文件考察的是「账号失联能否被如实上报」，
    DB 就绪与否是另一条独立判据（见 test_startup_budget.py），
    此处固定为健康以免干扰账号维度的断言。
    """
    m._ACCOUNT_STATE["expected"] = expected
    m._DB_STATE["ready"] = db_ready

    class _FakeMT5:
        @staticmethod
        def get_all_accounts_status():
            return [{"connected": True} for _ in range(connected)]

        @staticmethod
        def get_account_health_summary():
            # 配合 health_check 新逻辑（main.py:536 改用 get_account_health_summary）：
            # degraded 仅当「应交易且未熔断」账号有掉线。
            # 本测试场景固定所有「应交易」账号均已在线（connected 个全在线），
            # trading_connected 取 min(connected, expected) 以兼容 connected>expected
            # 的「运行期新增账号」场景；与 trading_expected 相等时不判降级。
            _trad_conn = min(connected, expected) if expected > 0 else connected
            return {
                "trading_expected": expected,
                "trading_connected": _trad_conn,
                "offline": [],
                "non_trading_offline": [],
            }

    # 注意：不能用 `import app.services.mt5_service as m`——app/services/__init__.py
    # 把同名的**实例**绑到了包属性上，那样拿到的是实例而非模块，patch 会打空。
    # 必须走 sys.modules 取真模块（health 内部是 from ... import mt5_service）。
    monkeypatch.setattr(sys.modules["app.services.mt5_service"], "mt5_service", _FakeMT5)

    import app.routers.trading as tr
    monkeypatch.setattr(tr, "_auto_running", auto_running, raising=False)
    from datetime import datetime, timedelta
    monkeypatch.setattr(
        tr, "_auto_status",
        {"cycles": 5, "last_cycle": (datetime.now() - timedelta(seconds=cycle_gap)).isoformat()},
        raising=False,
    )
    return m.health_check()


# ═══════════════════ 缺陷组：旧逻辑会返回 ok 的那些情形 ═══════════════════
def test_all_accounts_offline_must_not_report_ok(monkeypatch):
    """事故现场复刻：4 个账号配着、0 个在线,绝不允许再报 ok。"""
    r = _health(monkeypatch, expected=4, connected=0)
    assert r["mt5_connected"] == 0
    assert r["accounts_expected"] == 4
    assert r["accounts_degraded"] is True
    assert r["status"] == "degraded", (
        "4 个客户账号全部失联却报 ok —— 这正是让事故潜伏 6 分钟无人察觉的原因"
    )


def test_partial_outage_is_still_degraded(monkeypatch):
    """3/4 在线：对平台是小事,对第 4 位客户是 100% 的事故,必须降级。"""
    r = _health(monkeypatch, expected=4, connected=3)
    assert r["accounts_degraded"] is True
    assert r["status"] == "degraded"


def test_healthy_loop_cannot_mask_account_outage(monkeypatch):
    """主循环转得再欢也不能掩盖账号失联(空转反而让 cycle_gap 更漂亮)。"""
    r = _health(monkeypatch, expected=2, connected=0, auto_running=True, cycle_gap=1.0)
    assert r["trade_stale"] is False, "前提：循环本身是健康的"
    assert r["status"] == "degraded", "循环健康不等于业务健康"


# ═══════════════════ 护栏组：不得制造新的误报 ═══════════════════
def test_all_connected_reports_ok(monkeypatch):
    r = _health(monkeypatch, expected=4, connected=4)
    assert r["accounts_degraded"] is False
    assert r["status"] == "ok"


def test_fresh_deploy_without_accounts_is_ok(monkeypatch):
    """全新部署还没配账号 —— 属正常状态,不能吓唬运维。"""
    r = _health(monkeypatch, expected=0, connected=0)
    assert r["accounts_degraded"] is False
    assert r["status"] == "ok"


def test_more_connected_than_expected_is_not_degraded(monkeypatch):
    """运行期新增账号会让实际数暂时超过启动快照,不该误判降级。"""
    r = _health(monkeypatch, expected=2, connected=3)
    assert r["accounts_degraded"] is False
    assert r["status"] == "ok"


def test_health_exposes_bootstrap_conclusion(monkeypatch):
    """接入结论要直接可读,运维不必去翻日志猜 mt5_connected=0 的含义。"""
    m._ACCOUNT_STATE["bootstrap"] = "账号接入 3/4，失败: id2"
    r = _health(monkeypatch, expected=4, connected=3)
    assert r["accounts_bootstrap"] == "账号接入 3/4，失败: id2"


def test_health_never_raises_when_mt5_unavailable(monkeypatch):
    """MT5 模块整个炸掉时,health 仍须能返回 —— 否则 supervisor 会误判假死重启。"""
    class _Boom:
        @staticmethod
        def get_all_accounts_status():
            raise RuntimeError("MT5 模块炸了")

    monkeypatch.setattr(sys.modules["app.services.mt5_service"], "mt5_service", _Boom)
    m._ACCOUNT_STATE["expected"] = 4

    r = m.health_check()
    assert r["mt5_connected"] == 0
    assert r["status"] == "degraded", "拿不到连接数时应保守判降级，而不是乐观报 ok"


# ═══════════════ DB 就绪维度（2026-08-08 启动路径改造后新增）═══════════════
def test_db_not_ready_must_not_report_ok(monkeypatch):
    """启动路径改为「快速认输 + 后台自愈」后，服务会在 DB 还没建好表时先起来。

    这是刻意设计（避免 198s 阻塞触发 supervisor 强杀死循环），
    但**先起来 ≠ 可用**：此时必须如实报 degraded，
    否则就是把「账号撒谎」换成了「DB 撒谎」，本质没变。
    """
    r = _health(monkeypatch, expected=0, connected=0, db_ready=False)
    assert r["db_ready"] is False
    assert r["status"] == "degraded", "DB 未就绪却报 ok —— 又一次静默失败"


def test_db_ready_restores_ok(monkeypatch):
    """后台自愈把 DB 救回来之后，status 必须能回到 ok（否则永远绿不了）。"""
    r = _health(monkeypatch, expected=0, connected=0, db_ready=True)
    assert r["db_ready"] is True
    assert r["status"] == "ok"


def test_health_exposes_db_detail(monkeypatch):
    """DB 自愈过程要可读，运维不必翻日志猜 db_ready=False 卡在哪。"""
    m._DB_STATE["detail"] = "启动期建表未成功，已转入后台自愈"
    r = _health(monkeypatch, expected=0, connected=0, db_ready=False)
    assert r["db_detail"] == "启动期建表未成功，已转入后台自愈"
