"""
Phase 6 降级车道 — 接入层集成测试（混沌演练的自动化版本）
=============================================================
单元测试证明「监视器算得对」，本文件证明「算出来的结论真的落到了行为上」。
两者缺一不可 —— 历史上最常见的失败模式不是判定错，而是判定对了但没人用。

覆盖：
  A. 手数系数真的缩了手（含大本金账号 ceiling 分支不被旁路 —— 曾差点漏掉）
  B. L3 熔断闸门装在正确的位置（只挡开仓，不挡持仓管理）
  C. 辩论引擎正确上报云端健康 / 不把「模型审慎 HOLD」误判成「API 宕机」
  D. 混沌演练：断 DS→L1、断双云+无本地→L3、断双云+有本地→L2
"""
import re
from pathlib import Path

import pytest

from app.services.intelligent_sizing import _apply_degrade, compute_intelligent_size
from app.services.platform_health_monitor import (
    COMPONENTS,
    FAIL_STREAK_TO_DOWN,
    DegradeLevel,
    get_monitor,
    reset_monitor,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_monitor():
    reset_monitor()
    m = get_monitor()
    for c in COMPONENTS:
        m.report_ok(c)
    yield m
    reset_monitor()


def _down(m, name):
    for _ in range(FAIL_STREAK_TO_DOWN):
        m.report_fail(name, "chaos")


def _strategy(**over):
    base = {
        "sizing_mode": "smart",
        "max_risk_per_trade_pct": 2.0,
        "capital_source": "live",
        "volatility_factor": 1.0,
        "same_direction_decay": 0.5,
        "min_lot_per_trade": 0.01,
        "max_lot_per_trade": 1.0,
        "max_position_lots": 5.0,
        "lot_bounds_mode": "manual",
    }
    base.update(over)
    return base


def _size(balance, conf=0.8, strategy=None):
    return compute_intelligent_size(
        balance=balance,
        atr=20.0,
        signal_confidence=conf,
        same_direction_count=0,
        strategy=strategy or _strategy(),
    )


# ============================================================
#  A. 手数系数真的落地
# ============================================================
class TestSizingHonoursDegrade:
    def test_l0_unchanged(self, clean_monitor):
        r = _size(10_000)
        assert clean_monitor.level() == DegradeLevel.L0
        assert r["components"]["degrade_mult"] == 1.0
        assert "降级" not in r["reason"]

    def test_l1_cuts_to_70pct(self, clean_monitor):
        base = _size(10_000)["lots"]
        _down(clean_monitor, "deepseek")
        assert clean_monitor.level() == DegradeLevel.L1
        cut = _size(10_000)["lots"]
        assert cut < base
        assert cut == pytest.approx(round(base * 0.7, 2), abs=0.011)

    def test_l2_cuts_to_40pct(self, clean_monitor):
        base = _size(10_000)["lots"]
        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        assert clean_monitor.level() == DegradeLevel.L2
        cut = _size(10_000)["lots"]
        assert cut == pytest.approx(round(base * 0.4, 2), abs=0.011)

    def test_l3_returns_zero_lots(self, clean_monitor):
        _down(clean_monitor, "market_data")
        assert clean_monitor.level() == DegradeLevel.L3
        r = _size(10_000)
        assert r["lots"] == 0.0
        assert "熔断" in r["reason"]

    def test_large_account_still_gets_cut(self, clean_monitor):
        """★ 回归守卫：$989k demo 账号 raw_lots 恒超 ceiling → 走「按置信度分配」
        分支。若降级系数只乘在 raw_lots 上，这里会被完全旁路、L2 照样满仓。
        """
        # ceiling 取 5.0：$989k 账号 raw_lots≈9.9 必然超顶 → 走「按置信度分配」分支
        strat = _strategy(max_lot_per_trade=5.0, max_position_lots=5.0)
        r0 = _size(989_000, conf=0.9, strategy=strat)
        base = r0["lots"]
        assert r0["raw_lots"] > 5.0, "前提：大账号 raw_lots 必须超 ceiling，否则本用例失去意义"
        assert base == pytest.approx(5.0, abs=1e-6), "前提：高置信超顶应取满 ceiling"
        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        cut = _size(989_000, conf=0.9, strategy=strat)["lots"]
        assert cut == pytest.approx(2.0, abs=0.01), \
            f"大账号 L2 手数应为 ceiling×0.4=2.0，实际 {cut} → 降级被 ceiling 分支旁路"

    def test_fixed_mode_also_cut(self, clean_monitor):
        strat = _strategy(sizing_mode="fixed")
        base = _size(10_000, strategy=strat)["lots"]
        _down(clean_monitor, "deepseek")
        cut = _size(10_000, strategy=strat)["lots"]
        assert cut <= base

    def test_min_lot_is_a_floor_not_breakable(self, clean_monitor):
        """降级不得把手数压到最小交易单位以下（0.005 手是下不出去的）。"""
        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        r = _size(200, conf=0.5)  # 极小账号
        assert r["lots"] >= 0.01

    def test_kill_switch_restores_full_size(self, clean_monitor, monkeypatch):
        _down(clean_monitor, "deepseek")
        cut = _size(10_000)["lots"]
        monkeypatch.setenv("WX_DEGRADE_DISABLED", "1")
        full = _size(10_000)["lots"]
        assert full > cut, "总开关必须能一键退回无降级行为"

    def test_apply_degrade_unit(self):
        assert _apply_degrade(1.0, 1.0, 0.01)[0] == 1.0
        assert _apply_degrade(1.0, 0.0, 0.01)[0] == 0.0
        assert _apply_degrade(1.0, 0.4, 0.01)[0] == pytest.approx(0.4)
        assert _apply_degrade(0.01, 0.4, 0.01)[0] == 0.01
        assert _apply_degrade(1.0, "bad", 0.01)[0] == 1.0  # type: ignore[arg-type]


# ============================================================
#  B. 熔断闸门装在正确位置（铁律一的结构性守卫）
# ============================================================
class TestGatePlacement:
    @staticmethod
    def _src() -> str:
        import app.services.trade_executor as te

        return Path(te.__file__).read_text(encoding="utf-8")

    def test_gate_exists(self):
        assert "allow_new_entry" in self._src(), "执行器未接入 L3 熔断闸门"

    def test_gate_is_after_position_management(self):
        """★ 核心结构守卫：闸门必须在 _manage_positions 之后。

        若被挪到方法开头，L3 会连持仓保护一起停掉 —— 那是在系统能力最弱的
        时刻放弃守护客户已有仓位，比不降级更危险。
        """
        src = self._src()
        gate = src.index("allow_new_entry as _allow_entry")
        manage = src.index("# Step 2: 管理现有持仓")
        close_opp = src.rindex("self._close_opposite_for_decision(ai_decision)", 0, gate)
        assert manage < gate, "熔断闸门跑到了持仓管理之前 → 会连止损保护一起停掉"
        assert close_opp < gate, "熔断闸门跑到了反向平仓之前 → 会挡住平仓路径"

    def test_gate_is_before_order_placement(self):
        src = self._src()
        gate = src.index("allow_new_entry as _allow_entry")
        order = src.index("place_order", gate)
        assert gate < order, "闸门必须在下单之前，否则挡不住开仓"

    def test_gate_failure_is_fail_open(self):
        """闸门自身异常 → 放行（监控故障不该变成隐形停机）。"""
        src = self._src()
        seg = src[src.index("Phase 6 降级熔断闸门"):]
        seg = seg[: seg.index("Step 3: 风控审核")]
        assert "except Exception" in seg
        assert "降级闸门检查跳过" in seg

    def test_no_close_call_in_gate_block(self):
        """闸门代码块内不得出现任何平仓调用（铁律一）。

        注意：必须先剥掉注释再扫描 —— 闸门上方的说明注释里就写着
        `_close_opposite_for_decision()`（那是在解释闸门为何装在它后面），
        连注释一起扫会被自己的文档误伤。
        """
        src = self._src()
        seg = src[src.index("Phase 6 降级熔断闸门"):]
        seg = seg[: seg.index("Step 3: 风控审核")]
        code_only = "\n".join(
            line.split("#", 1)[0] for line in seg.splitlines()
        )
        for bad in ("close_position", "close_all", "_close_", "order_close"):
            assert bad not in code_only, f"熔断闸门里出现平仓调用 {bad} → 违反铁律一"


# ============================================================
#  C. 辩论引擎的健康上报
# ============================================================
class TestDebateEngineReporting:
    @staticmethod
    def _src() -> str:
        import app.core.debate_engine as de

        return Path(de.__file__).read_text(encoding="utf-8")

    def test_reports_all_four_components(self):
        src = self._src()
        for comp in ("market_data", "chronos", "deepseek", "hunyuan", "local_llm"):
            assert f'"{comp}"' in src, f"辩论引擎未上报 {comp} 健康"

    def test_skipped_call_is_not_reported_as_failure(self):
        """★ 跳过调用 ≠ 调用失败。

        若把「因熔断而跳过」也上报为失败，就会自我强化：
        跳过→上报失败→更判失联→继续跳过，系统永远出不来。
        """
        src = self._src()
        assert "if _ds_allowed:" in src and "if _hy_allowed:" in src, \
            "上报必须以「本轮真的发起过调用」为前提"

    def test_deepseek_failure_marked(self):
        """DS 失败分支必须带 _api_failed，否则无法与「模型审慎 HOLD」区分。"""
        import app.core.deepseek_client as dc

        src = Path(dc.__file__).read_text(encoding="utf-8")
        holds = re.findall(r'\{"decision": "HOLD", "confidence": 0\.0[^}]*\}', src)
        assert holds, "未找到 DeepSeek 的 HOLD 失败返回"
        for h in holds:
            assert "_api_failed" in h, f"DeepSeek 失败分支缺 _api_failed 标记: {h[:80]}"

    def test_symmetric_single_model_branches(self):
        """DS 挂和 HY 挂必须对称处理，否则 DS 挂时每轮白等一次超时。"""
        src = self._src()
        assert "elif ds_api_failed:" in src, "缺少 DeepSeek 失联时的混元单模型分支"

    def test_degraded_path_never_closes_positions(self):
        """降级决策路径不得产出任何平仓动作。"""
        src = self._src()
        seg = src[src.index("def _degraded_decide"):]
        seg = seg[: seg.index("def get_last_context")]
        for bad in ("close_position", "close_all", "CLOSE", "平掉", "全平"):
            assert bad not in seg, f"降级路径出现平仓语义 {bad} → 违反铁律一"

    def test_degraded_path_uses_copilot_gate(self):
        """本地副驾必须走 copilot_gate（三道锁），不得直接采信 8B 的方向。"""
        src = self._src()
        seg = src[src.index("def _degraded_decide"):]
        seg = seg[: seg.index("def get_last_context")]
        assert "copilot_gate" in seg, "副驾未过三道锁 → 违反铁律二"
        assert "chronos_dir" in seg, "副驾未要求 Chronos 同向"


# ============================================================
#  D. 混沌演练（自动化）
# ============================================================
class TestChaosDrills:
    def test_drill_1_kill_deepseek(self, clean_monitor):
        """演练①：DeepSeek 断线 → L1，仍交易，手数 70%。"""
        _down(clean_monitor, "deepseek")
        s = clean_monitor.snapshot()
        assert s.level == DegradeLevel.L1
        assert s.allow_new_entry is True
        assert s.lot_multiplier == pytest.approx(0.7)
        assert "DeepSeek" in s.reason

    def test_drill_2_kill_both_clouds_no_local(self, clean_monitor):
        """演练②：双云断 + 本机无 Ollama（当前真实环境）→ L3，停开仓不平仓。"""
        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        _down(clean_monitor, "local_llm")
        s = clean_monitor.snapshot()
        assert s.level == DegradeLevel.L3
        assert s.allow_new_entry is False
        assert s.lot_multiplier == 0.0
        assert "持仓" in s.detail, "L3 文案必须明确告知持仓不受影响"

    def test_drill_3_kill_both_clouds_with_local(self, clean_monitor):
        """演练③：双云断但本地 Qwen3 在线 → L2 副驾，手数 40% + 强制 Chronos 同向。"""
        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        s = clean_monitor.snapshot()
        assert s.level == DegradeLevel.L2
        assert s.allow_new_entry is True
        assert s.require_local_confirm is True
        assert s.lot_multiplier == pytest.approx(0.4)

    def test_drill_4_dirty_market_data(self, clean_monitor):
        """演练④：行情脏污/断线 → L3（垃圾进垃圾出，输入不可信就别下注）。"""
        _down(clean_monitor, "market_data")
        s = clean_monitor.snapshot()
        assert s.level == DegradeLevel.L3
        assert "行情" in s.reason

    def test_drill_5_full_recovery_path(self, clean_monitor):
        """演练⑤：从 L3 一路恢复到 L0（验证系统能自己爬回来，不需人工重启）。"""
        import time

        _down(clean_monitor, "deepseek")
        _down(clean_monitor, "hunyuan")
        _down(clean_monitor, "local_llm")
        _down(clean_monitor, "market_data")
        assert clean_monitor.level() == DegradeLevel.L3

        clean_monitor._last_down_ts = time.time() - 999
        for _ in range(5):
            for c in COMPONENTS:
                clean_monitor.report_ok(c)
        assert clean_monitor.level() == DegradeLevel.L0, "系统必须能自愈回全能力"
        assert clean_monitor.lot_multiplier() == 1.0

    def test_drill_6_degrade_never_triggers_close(self, clean_monitor):
        """演练⑥：全档位遍历，确认没有任何一档会产出平仓指令。"""
        for lv in (DegradeLevel.L0, DegradeLevel.L1, DegradeLevel.L2, DegradeLevel.L3):
            clean_monitor.set_manual_level(lv, "演练")
            s = clean_monitor.snapshot()
            d = s.as_dict()
            for k in d:
                assert "close" not in k.lower(), f"{lv.name} 快照含平仓字段 {k}"
            assert isinstance(s.allow_new_entry, bool)
        clean_monitor.set_manual_level(None)
