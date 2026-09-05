"""L3 契约测试：手数引擎重构前后【逐字段等价】。

守的规矩（V6 §12.4 扼杀者协议）：
    抽取权威函数属于"结构重构"，不允许改变任何一笔交易的手数。
    本测试把 v1.4.0-baseline 的旧实现动态加载，与当前实现跑同一批输入对拍。
    只要有一个字段不同就红 —— 这是"不修东坏西"的机器保证。

输入矩阵覆盖真实生产库四个账号的配置组合 + 边界值。
"""
import itertools

import pytest

from app.services.intelligent_sizing import compute_intelligent_size
from tests import legacy_loader
from tests.legacy_loader import load_module_at_ref

BASELINE_REF = "v1.4.0-baseline"
GIT_PATH = "backend/app/services/intelligent_sizing.py"

_legacy = load_module_at_ref(GIT_PATH, BASELINE_REF, alias="_legacy_sizing")

# skip reason 必须带上**真实**失败原因。原来只写"非 git 环境或 ref 不存在"，
# 结果 620 个用例静默跳过了很久都没人察觉——手数是钱，安全网空转必须刺眼。
pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        _legacy is None,
        reason=(f"⚠ 手数等价性安全网未运行！无法从 {BASELINE_REF} 加载旧实现："
                f"{legacy_loader.last_failure or '未知原因'}"),
    ),
]


class _S:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── 真实生产库四账号配置（2026-08-07 快照）+ 人造边界 ──────────
PROFILES = [
    # 百万 demo 账号（liumanchun1 / liumanchun3）
    dict(min_lot_per_trade=0.5, max_lot_per_trade=1.0, max_position_lots=1.0,
         capital_source="live", base_capital=1_000_000.0, sizing_scale_mode="auto"),
    # 真实规模账号（liumanchuan2 / liumanchun4）
    dict(min_lot_per_trade=0.01, max_lot_per_trade=0.05, max_position_lots=1.0,
         capital_source="live", base_capital=1000.0, sizing_scale_mode="auto"),
    # manual 本金模式
    dict(min_lot_per_trade=0.01, max_lot_per_trade=1.0, max_position_lots=1.0,
         capital_source="manual", base_capital=32000.0, sizing_scale_mode="auto"),
    # 关闭缩放
    dict(min_lot_per_trade=0.01, max_lot_per_trade=1.0, max_position_lots=1.0,
         capital_source="live", base_capital=1000.0, sizing_scale_mode="manual"),
    # 退化模式
    dict(min_lot_per_trade=0.01, max_lot_per_trade=1.0, max_position_lots=1.0,
         capital_source="live", base_capital=1000.0, sizing_scale_mode="auto",
         sizing_mode="fixed"),
]

BALANCES = [0.0, 2408.0, 2700.0, 989_000.0, 1_006_000.0]
ATRS = [0.0, 20.0, 55.5, 300.0]
CONFS = [0.35, 0.62, 0.95]
SAME_DIRS = [0, 2]

_CASES = list(itertools.product(
    range(len(PROFILES)), BALANCES, ATRS, CONFS, SAME_DIRS
))


@pytest.fixture
def legacy_lot_mode(monkeypatch):
    """把手数硬边界开关拨回历史值，用于验证【结构重构】本身没改变行为。

    硬边界是 P0-2 有意为之的【行为变更】，不能混进重构等价性验证，
    否则一旦真出现意外漂移会被误当成"预期内的变化"而漏掉。
    """
    from app.services import capital_authority
    monkeypatch.setattr(capital_authority, "LOT_HARD_BOUNDS_DEFAULT", False)


@pytest.mark.parametrize("pi,bal,atr,conf,nsame", _CASES)
def test_sizing_output_identical_to_baseline(legacy_lot_mode, pi, bal, atr, conf, nsame):
    kw = dict(balance=bal, atr=atr, signal_confidence=conf,
              same_direction_count=nsame)
    new = compute_intelligent_size(strategy=_S(**PROFILES[pi]), **kw)
    old = _legacy.compute_intelligent_size(strategy=_S(**PROFILES[pi]), **kw)

    # 手数是钱，必须逐分不差
    assert new["lots"] == old["lots"], (
        f"手数漂移! profile={pi} bal={bal} atr={atr} conf={conf} "
        f"新={new['lots']} 旧={old['lots']}"
    )
    assert new["raw_lots"] == old["raw_lots"], f"raw_lots 漂移 profile={pi}"

    # 结构字段一致（防止漏字段导致下游 KeyError）
    assert set(new.keys()) == set(old.keys()), (
        f"返回字段集合变了: 多={set(new) - set(old)} 少={set(old) - set(new)}"
    )

    # 数值型组件逐个比对（reason 等文案允许不同）
    on, oo = new.get("components", {}), old.get("components", {})
    for k in oo:
        if isinstance(oo[k], (int, float)):
            assert k in on and on[k] == pytest.approx(oo[k]), (
                f"components.{k} 漂移: 新={on.get(k)} 旧={oo[k]} (profile={pi})"
            )


# ══════════════════════════════════════════════════════════════
# P0-2 行为变更留档：手数硬边界生效后，与 baseline 的差异必须
#      ①只发生在"超过后台设定上限"的场景 ②方向只能是变小
# ══════════════════════════════════════════════════════════════
_MILLION = dict(min_lot_per_trade=0.5, max_lot_per_trade=1.0, max_position_lots=1.0,
                capital_source="live", base_capital=1_000_000.0,
                sizing_scale_mode="auto")


@pytest.mark.parametrize("bal", [988_998.0, 1_005_982.0])
@pytest.mark.parametrize("atr", [20.0, 55.5, 300.0])
@pytest.mark.parametrize("conf", [0.35, 0.62, 0.95])
def test_hard_bounds_only_shrinks_never_grows(bal, atr, conf):
    """百万账号任何输入下，新手数 ≤ 旧手数，且绝不超过后台设定的 1.0 手。"""
    kw = dict(balance=bal, atr=atr, signal_confidence=conf, same_direction_count=0)
    new = compute_intelligent_size(strategy=_S(**_MILLION), **kw)
    old = _legacy.compute_intelligent_size(strategy=_S(**_MILLION), **kw)

    assert new["lots"] <= old["lots"] + 1e-9, (
        f"硬边界竟然放大了手数! 新={new['lots']} 旧={old['lots']}"
    )
    assert new["lots"] <= _MILLION["max_lot_per_trade"] + 1e-9, (
        f"击穿后台设定上限! 设定={_MILLION['max_lot_per_trade']} 实际={new['lots']}"
    )


def test_hard_bounds_does_not_block_any_trade():
    """核心铁律护栏：硬边界只限幅、不拒单 —— 手数必须仍 > 0。"""
    for bal in (988_998.0, 2408.0, 2696.0):
        for atr in (20.0, 55.5, 300.0):
            r = compute_intelligent_size(
                strategy=_S(**_MILLION), balance=bal, atr=atr,
                signal_confidence=0.62, same_direction_count=0)
            assert r["lots"] > 0, f"硬边界把交易拒掉了! bal={bal} atr={atr}"


def test_small_account_unaffected_by_hard_bounds():
    """真实规模小账号（$2408 / 上限 0.05）行为不应有任何变化。"""
    small = dict(min_lot_per_trade=0.01, max_lot_per_trade=0.05,
                 max_position_lots=1.0, capital_source="live",
                 base_capital=1000.0, sizing_scale_mode="auto")
    for atr in (20.0, 55.5, 300.0):
        kw = dict(balance=2408.0, atr=atr, signal_confidence=0.62,
                  same_direction_count=0)
        new = compute_intelligent_size(strategy=_S(**small), **kw)
        old = _legacy.compute_intelligent_size(strategy=_S(**small), **kw)
        assert new["lots"] == old["lots"], (
            f"小账号被误伤! atr={atr} 新={new['lots']} 旧={old['lots']}"
        )
