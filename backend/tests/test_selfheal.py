"""MT5 断线自愈逻辑单测（全程 mock，不触碰真实 MT5 终端）。

原为 backend/test_selfheal.py 脚本式验证（无 def test_，从未被 pytest 收集）。
Phase -1 收编要点：
  1. 原脚本直接改写 sys.modules["MetaTrader5"] 且不还原 —— 会污染同一进程内
     后续所有测试。现改为 fixture，用后必还原。
  2. 拆为独立用例，失败时能精确定位是「健康判定」还是「重连触发词」出问题。

覆盖两条关键失效链：
  - Worker 侧：会话断开后 _ensure_connected 必须触发且仅触发一次重连
  - 父进程侧：_is_respawn_error 必须能识别 IPC send failed / -10001 / 终端断开
"""
import sys
import types

import pytest


@pytest.fixture
def mt5_mock(monkeypatch):
    """用假的 MetaTrader5 顶替真库，测试结束自动还原。

    ★ 关键（收编时踩过的坑）：不能用 monkeypatch.setitem(sys.modules, "MetaTrader5", fake)。
      mt5_worker.py 顶部是 `import MetaTrader5 as mt5`，模块级名字在【首次导入时】就绑死了
      真库对象。单跑本文件时恰好是首次导入所以能过；全量 pytest 时 mt5_worker 已被别的
      测试提前导入，改 sys.modules 对已绑定的 `w.mt5` 毫无影响 → _mt5_healthy() 读真终端
      返回 False，测试假失败。
      正解：直接替换 mt5_worker 模块内的 `mt5` 绑定，与导入顺序完全解耦。
    """
    state = {"connected": True}

    class _TerminalInfo:
        def __init__(self, connected):
            self.connected = connected

    fake = types.ModuleType("MetaTrader5")
    fake.terminal_info = lambda: _TerminalInfo(state["connected"])
    fake.account_info = lambda: (object() if state["connected"] else None)

    def _initialize(**kwargs):
        state["connected"] = True
        return True

    fake.initialize = _initialize
    fake.shutdown = lambda: None

    import app.services.mt5_worker as w

    monkeypatch.setattr(w, "mt5", fake)
    # 冷却计时器归零，避免上一条用例残留的时间戳把本次重连吞掉
    monkeypatch.setattr(w, "_last_reconnect_try", 0.0, raising=False)
    yield state  # 测试内可通过 state["connected"] 控制断线


@pytest.mark.unit
def test_healthy_when_terminal_connected(mt5_mock):
    """终端在线 → _mt5_healthy() 为 True。"""
    import app.services.mt5_worker as w

    assert w._mt5_healthy() is True


@pytest.mark.unit
def test_disconnect_triggers_exactly_one_reconnect(mt5_mock, monkeypatch):
    """会话断开 → _ensure_connected 必须触发重连，且恰好一次（不得风暴重连）。"""
    import app.services.mt5_worker as w

    mt5_mock["connected"] = False

    calls = {"n": 0}

    def fake_reconnect(params):
        calls["n"] += 1
        mt5_mock["connected"] = True   # 模拟重连成功
        return True

    monkeypatch.setattr(w, "_reconnect_mt5", fake_reconnect)

    params = {"login": 1, "password": "x", "server": "s", "path": "p"}
    ok = w._ensure_connected(params)

    assert ok is True, "断开后重连应返回 True"
    assert calls["n"] == 1, f"应调用一次 _reconnect_mt5，实际 {calls['n']} 次"

    # 恢复后再次自检：会话已健康 → 绝不能再发起第二次重连（防重连风暴打挂终端）
    assert w._ensure_connected(params) is True
    assert calls["n"] == 1, f"恢复后不应重复重连，实际累计 {calls['n']} 次"


@pytest.mark.unit
def test_healthy_session_does_not_reconnect(mt5_mock, monkeypatch):
    """已健康时不得触发多余重连（避免无谓抖动）。"""
    import app.services.mt5_worker as w

    mt5_mock["connected"] = True

    calls = {"n": 0}

    def should_not_be_called(params):
        calls["n"] += 1
        return True

    monkeypatch.setattr(w, "_reconnect_mt5", should_not_be_called)

    assert w._mt5_healthy() is True
    assert w._ensure_connected({"login": 1}) is True
    assert calls["n"] == 0, "会话健康时不应发起重连"


@pytest.mark.unit
@pytest.mark.parametrize(
    "message,expected",
    [
        ("无法获取账户信息: (-10001, 'IPC send failed')", True),
        ("Worker 连接断开: handle is closed", True),
        ("MT5 终端断开，正在自动重连恢复", True),
        ("一些无关的业务错误", False),
    ],
)
def test_respawn_error_detection(mt5_mock, message, expected):
    """父进程重连触发词必须覆盖真实断线特征，且不误伤普通业务错误。

    漏判 → Worker 死了不重生，该账号静默停摆；
    误判 → 普通业务错误也重启 Worker，造成无谓抖动。
    """
    import app.services.mt5_service  # noqa: F401  确保子模块已加载

    ms = sys.modules["app.services.mt5_service"]  # 取模块本身（避开同名单例遮蔽）
    assert ms._is_respawn_error(message) is expected
