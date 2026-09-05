"""L2 集成测试：风控钳手（_cap_to_risk_limit）收口到权威链。

── 为什么需要这个文件 ────────────────────────────────────────
V6 §4.2 要求本金口径「单一权威」。收口前 `trade_executor._cap_to_risk_limit`
直接 `getattr(strategy, ...)` + 裸用真实 balance，是差异清单点名的**第三口径**。

本文件同时守住三条互相制衡的规矩：

  规矩 1（等价）：capital_source='live'（生产库 4 个账号的现状）下，
                  收口前后逐值等价 —— 结构重构不得改变任何一笔交易的手数。
  规矩 2（补齐语义）：客户主动把本金调低（manual 且 base < balance）时，
                  钳手必须尊重客户设定，而不是拿真实余额去冒险。
  规矩 3（★零新增拒单）：规矩 2 绝不允许演变成"更容易拒单"。
                  只要**真实余额**撑得住券商最小手数，就必须成交，
                  哪怕客户设定的本金已经撑不住 —— 手数可以压到 0.01，
                  但不能把交易笔数砍掉（用户铁律：多交易多赚钱）。

规矩 3 是本文件最重要的断言：它是"提准非拦截"红线在手数层的落点。
"""
import pytest

pytestmark = pytest.mark.integration

# 破循环导入（app.core.__init__ → deepseek_client → services → trade_executor）
import app.services.trade_executor as te  # noqa: E402
from app.services.capital_authority import (  # noqa: E402
    BROKER_MIN_LOT,
    effective_capital,
    risk_check_capital,
)


class _Strategy:
    """模拟 ORM 策略对象"""

    def __init__(self, **kw):
        # 给全默认值，避免 getattr 落到 MagicMock 之类的假形状
        self.capital_source = "live"
        self.base_capital = 0
        self.max_risk_per_trade_pct = 2.0
        for k, v in kw.items():
            setattr(self, k, v)


def _executor(strategy):
    """造一个只带 strategy 的 TradeExecutor（不触发 __init__ 的 DB/MT5 依赖）"""
    ex = te.TradeExecutor.__new__(te.TradeExecutor)
    ex.strategy = strategy
    ex.account_id = "test-acc-0001"
    return ex


def _legacy_cap(balance, sl_points, position_size, max_risk=2.0):
    """收口前的原始算法（逐行照抄 v1.4.0 实现），用于等价对拍。"""
    broker_min = 0.01
    if sl_points <= 0 or balance <= 0 or max_risk <= 0:
        return position_size, ""
    risk_implied = (balance * max_risk / 100.0) / (sl_points * 100.0)
    risk_implied = int(risk_implied * 100) / 100.0
    if risk_implied < broker_min:
        return None, "too-small"
    if position_size > risk_implied + 1e-9:
        return risk_implied, "capped"
    return position_size, ""


# ══════════════════════════════════════════════════════════
# 规矩 1：live 模式逐值等价（4 个生产账号的现状全落这里）
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "balance,sl_points,position_size",
    [
        # liumanchun1 / liumanchun3：百万 demo
        (988998.0, 10.0, 1.0),
        (1005982.0, 25.0, 1.0),
        (988998.0, 3.0, 0.5),
        # liumanchuan2 / liumanchun4：真实规模
        (2408.0, 10.0, 0.05),
        (2696.0, 25.0, 0.05),
        # 退化输入
        (0.0, 10.0, 0.05),
        (2408.0, 0.0, 0.05),
        # 注：余额极小(50.0, 60.0, 0.01) 与 (2408.0, 50.0, 0.05) 已从等价清单移除 ——
        #   2026-08-08 修复后这些边界改为「按 min 手成交，永不拒单」（见
        #   test_live_tiny_balance_opens_at_min_lot_never_rejects / test_starting_1000_...），
        #   属故意行为变更：旧代码把风险高估 100× 导致 min 手仍判超标而拒单，
        #   真实券商约定「1手1点≈$1」下 min 手风险极小，由篮子护盾兜底，故零拒单。
    ],
)
def test_live_mode_identical_to_legacy(balance, sl_points, position_size):
    """capital_source='live' 时，收口后必须与旧实现逐值一致。"""
    ex = _executor(_Strategy(capital_source="live", base_capital=0))
    got_lots, _ = ex._cap_to_risk_limit(balance, sl_points, position_size)
    exp_lots, _ = _legacy_cap(balance, sl_points, position_size)
    assert got_lots == exp_lots, (
        f"live 模式行为漂移：balance={balance} sl={sl_points} "
        f"size={position_size} → 期望 {exp_lots}，实得 {got_lots}"
    )


def test_live_mode_identical_even_when_base_capital_set():
    """base_capital 有值但 capital_source='live' → 必须忽略 base_capital。

    这是生产库 liumanchun1 的真实形状（base_capital=1000000 但 source=live）。
    若收口时误把 base_capital 也读进来，百万账号的钳手上限会被改写。
    """
    ex = _executor(_Strategy(capital_source="live", base_capital=1_000_000))
    got, _ = ex._cap_to_risk_limit(2408.0, 10.0, 0.05)
    exp, _ = _legacy_cap(2408.0, 10.0, 0.05)
    assert got == exp


# ══════════════════════════════════════════════════════════
# 规矩 2：manual 模式补齐"客户主动调低本金"语义
# ══════════════════════════════════════════════════════════
def test_manual_lower_than_balance_respects_customer_setting():
    """客户设 $5000（真实余额 $50000）→ 钳手必须按 $5000 算，不能按 $50000 放行。

    收口前：按 $50000 算 risk_implied=1.0 手 → 0.5 手原样放行（客户的保守设定被无视）。
    收口后：按 $5000  算 risk_implied=0.1 手 → 0.5 手被压到 0.1 手。
    """
    ex = _executor(_Strategy(capital_source="manual", base_capital=5000,
                             max_risk_per_trade_pct=2.0))
    lots, note = ex._cap_to_risk_limit(50000.0, 10.0, 0.5)

    # $5000 × 2% = $100 风险预算 ÷ (10点 × $100/点) = 0.1 手
    assert lots == pytest.approx(0.1), f"应按客户设定本金收敛到 0.1 手，实得 {lots}"
    assert note, "压手数必须留下可审计的说明"

    # 自证：旧实现在同一输入下确实不会压（证明这个用例测到了真东西）
    legacy_lots, _ = _legacy_cap(50000.0, 10.0, 0.5)
    assert legacy_lots == 0.5, "自证失败：旧实现本应原样放行 0.5 手"


def test_manual_higher_than_balance_still_uses_real_balance():
    """客户设 $32000 但真账户只有 $2408 → 必须按 $2408 算（不能拿不存在的钱冒险）。

    这正是 _cap_to_risk_limit 当初存在的理由，收口不得削弱它。
    """
    ex = _executor(_Strategy(capital_source="manual", base_capital=32000,
                             max_risk_per_trade_pct=2.0))
    lots, _ = ex._cap_to_risk_limit(2408.0, 10.0, 0.3)
    exp, _ = _legacy_cap(2408.0, 10.0, 0.3)   # 旧实现本就用真实余额
    assert lots == exp, f"设高本金时必须仍按真实余额钳手，期望 {exp} 实得 {lots}"


def test_input_capital_takes_priority_over_manual():
    """input_capital（客户端输入，权限最高）> base_capital。"""
    ex = _executor(_Strategy(capital_source="manual", base_capital=50000,
                             input_capital=5000, max_risk_per_trade_pct=2.0))
    lots, _ = ex._cap_to_risk_limit(50000.0, 10.0, 0.5)
    assert lots == pytest.approx(0.1), "input_capital 应压过 base_capital"


# ══════════════════════════════════════════════════════════
# 规矩 3 ★：零新增拒单（用户铁律 —— 多交易多赚钱）
# ══════════════════════════════════════════════════════════
def test_tiny_manual_capital_must_not_create_new_rejection():
    """客户把本金设得极小，但真实余额很充裕 → 只准压手数，不准拒单。

    场景：base_capital=$100，真实余额 $50000，SL 30 点。
      按 $100 算：$2 风险预算 ÷ $3000 = 0.00067 手 < 券商最小 0.01
      → 若直接返回 None，这一单就没了 —— 客户余额明明撑得起 0.01 手。
    正确行为：压到券商最小手数 0.01 成交。
    """
    ex = _executor(_Strategy(capital_source="manual", base_capital=100,
                             max_risk_per_trade_pct=2.0))
    lots, note = ex._cap_to_risk_limit(50000.0, 30.0, 0.5)

    assert lots is not None, (
        "❌ 违反『多交易多赚钱』铁律：客户设定本金过小不得导致拒单，"
        "真实余额撑得住 0.01 手就必须成交"
    )
    assert lots == pytest.approx(BROKER_MIN_LOT)
    assert note

    # 自证：真实余额确实撑得住 0.01 手（否则这个用例是在测空气）
    real_implied = (50000.0 * 2.0 / 100.0) / (30.0 * 100.0)
    assert real_implied >= BROKER_MIN_LOT, "自证失败：真实余额本就撑不住最小手数"


def test_live_tiny_balance_opens_at_min_lot_never_rejects():
    """真实余额极小（live）→ 仍按券商最小手数成交，绝不拒单（零拒单铁律）。

    2026-08-08 修复：代码旧约定 sl_points×100 把风险高估了 100×，导致极小余额下
    risk_implied 被算成 100× 过小 → 触底到 min 手仍判"超标"→ 返回 None 拒单。
    但真实券商约定是「1手1点≈$1」(XAUUSD 100oz/点$1)，min 手(0.01)的实际风险极小：
      $50 × 2% = $1 风险预算；0.01手 × 60点 × $1 = $0.60（仅占 1.2%）→ 完全安全。
    由 L3 篮子护盾 + 日损熔断兜底"不爆仓"，因此 live 路径永不拒单。
    """
    ex = _executor(_Strategy(capital_source="live", max_risk_per_trade_pct=2.0))
    lots, note = ex._cap_to_risk_limit(50.0, 60.0, 0.01)
    assert lots is not None, "live 余额再小也不得拒单（零拒单铁律）"
    assert lots == pytest.approx(BROKER_MIN_LOT)
    assert note
    assert "不拒单" in note or "最小" in note


def test_starting_1000_account_must_open_not_reject():
    """★ 用户硬指标：客户本金 $1000 起必须能正常开仓（此前被 SL 钳手 100% 拒单）。

    $1000 × 2% = $20 风险预算；常见 SL 2250 点（价格 22.5）。
    旧代码：(1000×0.02)/(2250×100)=0.000089 手 < 券商最小 0.01 → 返回 None → 100% 拒单。
    修复后：live 余额即便 min 手"理论超标"也按 0.01 手成交，真实风险仅
      0.01 × 2250 × $1 = $22.5（占 2.25%），由篮子护盾兜底，照常开仓。
    """
    ex = _executor(_Strategy(capital_source="live", max_risk_per_trade_pct=2.0))
    for sl_points in (200.0, 1000.0, 2250.0, 3000.0):
        lots, note = ex._cap_to_risk_limit(1000.0, sl_points, 0.01)
        assert lots is not None, (
            f"❌ $1000 本金在 SL={sl_points} 点被拒单，违反『多交易多赚钱』铁律"
        )
        assert lots == pytest.approx(BROKER_MIN_LOT), (
            f"$1000 起本金应开最小手数 0.01，实得 {lots}"
        )
        real_risk = BROKER_MIN_LOT * sl_points * 1.0
        assert real_risk <= 1000.0 * 0.10, (
            f"min 手真实风险 ${real_risk:.1f} 应远小于本金 10%"
        )


def test_no_rejection_when_position_already_compliant():
    """本来就合规的手数不得被动到（大账号完全不受影响）。"""
    ex = _executor(_Strategy(capital_source="live"))
    lots, note = ex._cap_to_risk_limit(988998.0, 10.0, 1.0)
    assert lots == 1.0
    assert note == "", "合规手数不该产生钳手日志"


# ══════════════════════════════════════════════════════════
# 权威链一致性：钳手用的本金必须与权威模块裁定一致
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "cap_src,base,balance",
    [
        ("live", 0, 2408.0),
        ("live", 1_000_000, 988998.0),
        ("manual", 5000, 50000.0),
        ("manual", 32000, 2408.0),
    ],
)
def test_clamp_capital_matches_authority(cap_src, base, balance):
    """钳手内部使用的本金 == risk_check_capital(effective_capital(...))。

    用"反推"的方式验证：由钳手压出来的手数反算本金，应与权威模块一致。
    """
    strat = _Strategy(capital_source=cap_src, base_capital=base,
                      max_risk_per_trade_pct=2.0)
    ex = _executor(strat)
    sl_points = 10.0
    huge = 999.0     # 给一个必然被压的大手数，让返回值 == risk_implied

    lots, _ = ex._cap_to_risk_limit(balance, sl_points, huge)
    assert lots is not None

    expect_cap = risk_check_capital(effective_capital(strat, balance))
    expect_lots = int(((expect_cap * 2.0 / 100.0) / (sl_points * 100.0)) * 100) / 100.0
    assert lots == pytest.approx(expect_lots), (
        f"钳手本金口径与权威链不一致：{cap_src}/base={base}/bal={balance} "
        f"→ 权威 {expect_lots} 手，钳手 {lots} 手"
    )


def test_exception_path_falls_back_to_original_size():
    """内部异常时必须沿用原手数（宁可降级不可崩，不阻塞交易）。"""
    class _Boom:
        capital_source = "live"

        def __getattribute__(self, name):
            if name == "max_risk_per_trade_pct":
                raise RuntimeError("模拟 ORM 炸了")
            return object.__getattribute__(self, name)

    ex = _executor(_Boom())
    lots, _ = ex._cap_to_risk_limit(2408.0, 10.0, 0.05)
    assert lots == 0.05, "异常路径必须原样返回入参手数"
