"""保本/追踪硬地板 —— 端到端集成测试（mock MT5/DB，验证 _manage_positions 实际行为）。

原为 backend/test_hardfloor_integration.py 脚本式运行（从未被 pytest 收集）。
Phase -1 收编要点：原脚本用 `te.mt5_service = mock` 直接改模块全局且不还原，
会污染同进程内后续测试 —— 现改用 monkeypatch，测试结束自动还原。

核心验证（一条会直接亏钱的失效链）：
  当 M1 出场 Agent 接管持仓、返回 action=hold 且 new_sl=None（命中缓存/粘滞，
  常见情形）时，规则引擎算出的「早期保本 SL」仍必须通过硬地板合并被真正下发到
  MT5（modify_sl_tp 被调用）。否则浮盈单失去确定性保本 → 由赚变亏。
"""
import types
from unittest.mock import MagicMock

import pytest

import app.services.trade_executor as te
from app.services.trade_executor import TradeExecutor


@pytest.fixture
def executor_and_mt5(monkeypatch):
    """构造轻量 TradeExecutor（mock 掉 db / 引擎 / mt5_service）。

    ★ 收编时踩过的坑：持仓读取入口已重构。_manage_positions 现在走
      get_all_positions_rescanned() → mt5_service.get_all_positions(account_id)（多轮
      重扫描并集，修 MT5 竞态漏单），不再是旧的 get_positions()。旧测试仍 mock
      get_positions，导致 MagicMock 的 get_all_positions 返回不可迭代对象 → 实际拿到
      空持仓 → 函数在 "全持仓=0" 处直接 return → modify_sl_tp 从未被调用 →
      call_args 为 None → TypeError。故此处必须 mock get_all_positions。
    """
    engine = MagicMock()
    engine.market.get_market_snapshot.return_value = {
        "volatility_metrics": {"h1_atr": 15.0, "d1_atr": 15.0},
        "current_price": 2005.0,
    }
    engine.deepseek = MagicMock()

    strategy = types.SimpleNamespace()  # 所有 smart_exit 配置走默认值

    mock_mt5 = MagicMock()
    monkeypatch.setattr(te, "mt5_service", mock_mt5)
    # 重扫描默认 3 轮、每轮 sleep 0.4s，纯 mock 场景没必要真等（用例从 ~0.8s 降到毫秒级）
    monkeypatch.setattr(te.time, "sleep", lambda *_a, **_k: None)

    executor = TradeExecutor(
        account_id="acc_test",
        strategy=strategy,
        user_id="user_test",
        db=MagicMock(),
        engine=engine,
    )

    # L3 篮子护盾与本测试无关：浮盈达标会直接全平 return，干扰硬地板断言 → 关闭
    def _fake_fresh(field, default=None):
        if field == "enable_l3_guard":
            return False
        return default

    executor._fresh_strat = _fake_fresh
    executor.exit_agent = MagicMock()
    # 信号塔分流：仅主号走完整 AI 出场 + 硬地板，跟号走镜像分支。此处验证主号路径。
    executor._is_leader = True

    return executor, mock_mt5


def _sl_calls(mock_mt5):
    """只取「改 SL」的那些调用（同一 mock 也承接改 TP 调用，不能盲取 call_args 末条）。"""
    return [c for c in mock_mt5.modify_sl_tp.call_args_list if c.kwargs.get("sl") is not None]


@pytest.mark.integration
def test_m1_hold_still_applies_breakeven(executor_and_mt5):
    """M1 返回 hold 且无 new_sl + 浮盈达标 → 保本地板仍必须被下发。

    ★ 2026-08-17 契约更新：2026-08-13 防噪音区修复后，保本 SL 须 move ≥
      be_sl_floor=max(MIN_SL_DIST=8, 1.0×ATR=15)+buffer 0.5 = 15.5 才上移
      （旧 0.3×ATR 即保本会生成距现价仅几点的 SL，被黄金正常噪音扫掉——
      用户实锤"刚止损就反转"）。期望 SL = open + (move - floor) = 2000+5 = 2005。
    """
    executor, mock_mt5 = executor_and_mt5

    # 浮盈 buy 单：open=2000, current=2020（move=20 ≥ 15.5）→ 保本 SL=2005
    mock_mt5.get_all_positions.return_value = [{
        "ticket": 12345,
        "type": "buy",
        "volume": 0.1,
        "price_open": 2000.0,
        "price_current": 2020.0,
        "sl": 1985.0,          # 低于开仓价 → 应被移入保本
        "tp": 2030.0,
        "profit": 200.0,
    }]
    executor.exit_agent.evaluate.return_value = {
        "12345": {"action": "hold", "close_pct": 0, "new_sl": None, "reason": "M1 cache hold"}
    }

    executor._manage_positions(types.SimpleNamespace(decision="BUY", confidence=0.6))

    calls = _sl_calls(mock_mt5)
    assert calls, (
        "硬地板失效：M1 返回 hold 时未下发 SL —— 浮盈单失去确定性保本，会由赚变亏"
    )
    ticket = calls[-1].args[1]
    applied_sl = calls[-1].kwargs["sl"]
    assert ticket == 12345, f"ticket 不匹配: {ticket}"
    assert abs(applied_sl - 2005.0) < 1e-6, f"保本地板 SL 错误: got={applied_sl} exp=2005.0"


@pytest.mark.integration
def test_m1_better_trail_overrides_rule(executor_and_mt5):
    """M1 给出更锁利的追踪 SL(2010 > 规则 2000.5) → 采用 M1，不削弱 AI 追踪能力。"""
    executor, mock_mt5 = executor_and_mt5

    mock_mt5.get_all_positions.return_value = [{
        "ticket": 999, "type": "buy", "volume": 0.1,
        "price_open": 2000.0, "price_current": 2020.0,
        "sl": 1985.0, "tp": 2050.0, "profit": 200.0,
    }]
    executor.exit_agent.evaluate.return_value = {
        "999": {"action": "hold", "close_pct": 0, "new_sl": 2010.0, "reason": "M1 trail"}
    }

    executor._manage_positions(types.SimpleNamespace(decision="BUY", confidence=0.6))

    calls = _sl_calls(mock_mt5)
    assert calls, "M1 给出追踪 SL 却未下发"
    applied_sl = calls[-1].kwargs["sl"]
    assert abs(applied_sl - 2010.0) < 1e-6, f"M1 更优 SL 未被采用: got={applied_sl}"


@pytest.mark.integration
def test_no_positions_no_crash(executor_and_mt5):
    """无持仓 → 安全返回，不得发出任何 modify / close 请求。"""
    executor, mock_mt5 = executor_and_mt5

    mock_mt5.get_all_positions.return_value = []
    executor.exit_agent.evaluate.return_value = {}

    executor._manage_positions(types.SimpleNamespace(decision="HOLD", confidence=0.0))

    assert not mock_mt5.modify_sl_tp.called
    assert not mock_mt5.close_position.called
