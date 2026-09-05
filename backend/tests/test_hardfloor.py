"""保本/追踪「硬地板」合并规则单测（纯函数级）。

原为 backend/test_hardfloor.py（已是 pytest 格式，但因 testpaths=tests 从未被收集）。
Phase -1 收编：移入 tests/、去掉手动 sys.path 注入（conftest 已统一处理）、补 unit 标记。

核心不变式：规则引擎算出的保本/追踪 SL 是**地板**，M1 模型不得把它撤回或调劣，
只有当 M1 给出**更锁利**的 SL 时才允许覆盖。防止模型把已保本的单又放回风险区。
"""
import pytest

from app.services.trade_executor import _merge_hard_floor_sl


def _assert_sl(name: str, got, exp):
    if got is None or exp is None:
        assert got is exp, f"{name}: got={got} exp={exp}"
        return
    assert abs(got - exp) < 1e-9, f"{name}: got={got} exp={exp}"


@pytest.mark.unit
def test_buy_rule_be_floor_not_overridden_by_m1_none():
    """buy 已达保本条件、M1 返回 hold（无 new_sl）→ 必须落规则引擎的保本 SL。"""
    got = _merge_hard_floor_sl(
        pos_type="buy", current_sl=1985.0, rule_new_sl=2000.5, m1_new_sl=None
    )
    _assert_sl("buy 保本地板不被 M1(None) 覆盖", got, 2000.5)


@pytest.mark.unit
def test_buy_m1_better_trail_overrides_rule():
    """buy：M1 追踪 SL 更靠近现价（更锁利）→ 采用 M1。"""
    got = _merge_hard_floor_sl(
        pos_type="buy", current_sl=1985.0, rule_new_sl=2000.5, m1_new_sl=2010.0
    )
    _assert_sl("buy M1 更优追踪覆盖规则", got, 2010.0)


@pytest.mark.unit
def test_buy_no_sl_from_either_side_means_hold():
    """buy：双方都没给 new_sl → 返回 None（不发 modify），与原逻辑一致。"""
    got = _merge_hard_floor_sl(
        pos_type="buy", current_sl=1985.0, rule_new_sl=None, m1_new_sl=None
    )
    _assert_sl("buy 双方无 SL → 不动", got, None)


@pytest.mark.unit
def test_sell_rule_be_floor():
    """sell：保本 SL 更小即更锁利 → 采用规则引擎。"""
    got = _merge_hard_floor_sl(
        pos_type="sell", current_sl=2015.0, rule_new_sl=1999.5, m1_new_sl=None
    )
    _assert_sl("sell 保本地板", got, 1999.5)


@pytest.mark.unit
def test_sell_m1_better_trail():
    """sell：M1 给出 1990 < 规则 1999.5，更锁利 → 采用 M1。"""
    got = _merge_hard_floor_sl(
        pos_type="sell", current_sl=2015.0, rule_new_sl=1999.5, m1_new_sl=1990.0
    )
    _assert_sl("sell M1 更优追踪覆盖", got, 1990.0)


@pytest.mark.unit
def test_not_worse_than_current():
    """合并结果与当前 SL 相同 → 返回 None，避免无意义的 modify 请求。"""
    got = _merge_hard_floor_sl(
        pos_type="buy", current_sl=2000.5, rule_new_sl=2000.5, m1_new_sl=None
    )
    _assert_sl("等于当前 SL → 不动", got, None)


@pytest.mark.unit
def test_current_sl_zero_rule_fills():
    """当前 SL=0（异常/裸单）但规则引擎算出保本 SL → 必须补上，不能放任裸奔。"""
    got = _merge_hard_floor_sl(
        pos_type="buy", current_sl=0.0, rule_new_sl=2000.5, m1_new_sl=None
    )
    _assert_sl("当前 SL=0 → 用规则引擎保本", got, 2000.5)


@pytest.mark.unit
def test_unknown_position_type_returns_none():
    """未知持仓类型 → None（失败关闭，不猜方向）。"""
    got = _merge_hard_floor_sl(
        pos_type="weird", current_sl=1985.0, rule_new_sl=2000.5, m1_new_sl=None
    )
    _assert_sl("未知类型 → None", got, None)
