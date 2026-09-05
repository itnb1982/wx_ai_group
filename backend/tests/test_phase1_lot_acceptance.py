"""L2 验收测试：V6 Phase 1 —— 「4 账号手数计算结果可预期，与前端输入一致」。

这是 Phase 1 的**验收标准原文**，本文件把它变成可执行断言。

── 被验收的链路（端到端）────────────────────────────────────
    前端设定 (min_lot / max_lot / max_position_lots / risk%)
        → capital_authority.effective_capital     本金裁定
        → capital_authority.resolve_lot_bounds    手数硬边界（V6 §4.3）
        → intelligent_sizing.compute_intelligent_size   算手数
        → trade_executor._cap_to_risk_limit        风控反钳
        → 最终下单手数

── 四条验收铁律 ──────────────────────────────────────────
    A. 可预期：同样输入必得同样手数（无隐藏随机/时间依赖）
    B. 与前端一致：最终手数恒落在客户设定的 [min_lot, max_lot] 内，
       auto 缩放**不得**把客户设的天花板顶穿（2026-08-07 生产事故：设 1 手成交 11 手）
    C. 不拒单：常规行情参数下整条链路不得返回 None（用户铁律：多交易多赚钱）
    D. 不爆仓：最终手数的实际风险不得超过 max_risk_per_trade_pct
       —— 否则 risk_engine Layer 5 会拒单，等于白算一场

注：`_session_quality_mult()` 读系统时间，本文件一律锁死，
    否则同一份配置在亚盘和伦敦盘会算出不同手数，验收就无从谈起。
"""
import pytest

pytestmark = pytest.mark.integration

import app.services.trade_executor as te  # noqa: E402  (破循环导入)
from app.services import intelligent_sizing as isz  # noqa: E402
from app.services.capital_authority import resolve_lot_bounds  # noqa: E402
from app.services.intelligent_sizing import compute_intelligent_size  # noqa: E402


class _Strategy:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ══════════════════════════════════════════════════════════
# 生产库真实快照（2026-08-07，capital_source 四个账号全为 live）
# ══════════════════════════════════════════════════════════
ACCOUNTS = {
    # 百万 demo —— 曾经的事故现场：设 max_lot=1.0，实际成交 10~11 手
    "liumanchun1": dict(
        balance=988998.0,
        cfg=dict(capital_source="live", base_capital=1_000_000,
                 max_risk_per_trade_pct=2.0, min_lot_per_trade=0.5,
                 max_lot_per_trade=1.0, max_position_lots=1.0,
                 sizing_scale_mode="auto", sizing_mode="smart",
                 volatility_factor=1.0, same_direction_decay=0.5),
    ),
    "liumanchun3": dict(
        balance=1_005_982.0,
        cfg=dict(capital_source="live", base_capital=1_000_000,
                 max_risk_per_trade_pct=2.0, min_lot_per_trade=0.5,
                 max_lot_per_trade=1.0, max_position_lots=1.0,
                 sizing_scale_mode="auto", sizing_mode="smart",
                 volatility_factor=1.0, same_direction_decay=0.5),
    ),
    # 真实规模账号
    "liumanchuan2": dict(
        balance=2408.0,
        cfg=dict(capital_source="live", base_capital=1000,
                 max_risk_per_trade_pct=2.0, min_lot_per_trade=0.01,
                 max_lot_per_trade=0.05, max_position_lots=1.0,
                 sizing_scale_mode="auto", sizing_mode="smart",
                 volatility_factor=1.0, same_direction_decay=0.5),
    ),
    "liumanchun4": dict(
        balance=2696.0,
        cfg=dict(capital_source="live", base_capital=1000,
                 max_risk_per_trade_pct=2.0, min_lot_per_trade=0.01,
                 max_lot_per_trade=0.05, max_position_lots=1.0,
                 sizing_scale_mode="auto", sizing_mode="smart",
                 volatility_factor=1.0, same_direction_decay=0.5),
    ),
}

# 常规行情矩阵：ATR(=SL点数) × 置信度
MARKET = [(10.0, 0.75), (25.0, 0.70), (25.0, 0.60), (40.0, 0.85), (6.0, 0.55)]


@pytest.fixture(autouse=True)
def _freeze_session(monkeypatch):
    """锁死时段系数 —— 验收要求"可预期"，就不能让手数随开盘时段漂移。"""
    monkeypatch.setattr(isz, "_session_quality_mult", lambda: 1.0)


def _pipeline(name, atr, conf, same_dir=0):
    """跑完整链路，返回 (最终手数或None, sizing手数, 策略对象, 余额)"""
    prof = ACCOUNTS[name]
    strat = _Strategy(**prof["cfg"])
    bal = prof["balance"]

    sized = compute_intelligent_size(
        balance=bal, atr=atr, signal_confidence=conf,
        same_direction_count=same_dir, strategy=strat,
    )["lots"]

    ex = te.TradeExecutor.__new__(te.TradeExecutor)
    ex.strategy = strat
    ex.account_id = name
    final, _note = ex._cap_to_risk_limit(bal, atr, sized)
    return final, sized, strat, bal


# ══════════════════════════════════════════════════════════
# 铁律 A：可预期（确定性）
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", list(ACCOUNTS))
@pytest.mark.parametrize("atr,conf", MARKET)
def test_A_deterministic(name, atr, conf):
    """同输入必得同输出 —— 跑 3 遍结果必须一致。"""
    runs = {_pipeline(name, atr, conf)[0] for _ in range(3)}
    assert len(runs) == 1, f"{name} 手数不确定，同输入得到多种结果：{runs}"


# ══════════════════════════════════════════════════════════
# 铁律 B：与前端输入一致（硬边界不可突破）★核心
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", list(ACCOUNTS))
@pytest.mark.parametrize("atr,conf", MARKET)
def test_B_never_exceeds_frontend_max_lot(name, atr, conf):
    """最终手数不得超过客户在前端设的 max_lot_per_trade。

    这是 2026-08-07 生产事故的回归守卫：
      liumanchun1 设 1.0 手 → 实际成交 11.13 手（$989k÷$1000=989x 被截到 50x 放大）。
    """
    final, sized, strat, _ = _pipeline(name, atr, conf)
    ceiling = strat.max_lot_per_trade
    assert final is not None
    assert final <= ceiling + 1e-9, (
        f"❌ {name} 突破前端设定天花板：设 {ceiling} 手，实算 {final} 手"
        f"（sizing={sized}, atr={atr}, conf={conf}）"
    )


@pytest.mark.parametrize("name", list(ACCOUNTS))
@pytest.mark.parametrize("atr,conf", MARKET)
def test_B2_never_below_broker_min(name, atr, conf):
    """最终手数不得跌破券商最小手数（否则等于隐性拒单）。"""
    final, _, _, _ = _pipeline(name, atr, conf)
    assert final is not None and final >= 0.01 - 1e-9, f"{name} 手数塌到 {final}"


def test_B3_hard_bounds_is_what_actually_stops_the_11_lot_accident():
    """★自证：证明"硬边界"这道防线真的在拦，而不是碰巧没超。

    做法：把 enforce_hard_bounds 关掉（历史行为）跑同一份配置，
    百万账号手数应当立刻飙到远超 1.0 手 —— 复刻生产事故。
    """
    prof = ACCOUNTS["liumanchun1"]
    strat = _Strategy(**prof["cfg"])
    bal = prof["balance"]

    # 硬边界打开（当前默认）→ 上限就是客户设的 1.0
    on = resolve_lot_bounds(strat, bal)
    # 硬边界关闭（历史行为）→ 上限被 auto 缩放放大
    off = resolve_lot_bounds(strat, bal, enforce_hard_bounds=False)

    assert on.max_lot == pytest.approx(1.0)
    assert off.max_lot == pytest.approx(50.0), (
        "自证失败：关掉硬边界后上限本应被放大到 50 倍，"
        "说明这条用例没测到真正的放大路径"
    )
    assert off.max_lot > on.max_lot * 10, "自证失败：两种模式差异不足，用例无意义"


def test_B4_small_account_not_penalized_by_hard_bounds():
    """小账号不受硬边界影响 —— 硬边界只压大账号，不许误伤小账号。"""
    prof = ACCOUNTS["liumanchuan2"]
    strat = _Strategy(**prof["cfg"])
    on = resolve_lot_bounds(strat, prof["balance"])
    off = resolve_lot_bounds(strat, prof["balance"], enforce_hard_bounds=False)
    # $2408 → scale 2.4x，关掉硬边界会放大到 0.12；打开则收回客户设的 0.05
    assert on.max_lot == pytest.approx(0.05)
    assert off.max_lot > on.max_lot   # 自证：小账号确实也走了缩放路径
    # 但下限不受影响，起步手数照旧
    assert on.min_lot == pytest.approx(0.01)


# ══════════════════════════════════════════════════════════
# 铁律 C：不拒单（多交易多赚钱）
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", list(ACCOUNTS))
@pytest.mark.parametrize("atr,conf", MARKET)
@pytest.mark.parametrize("same_dir", [0, 1, 2])
def test_C_never_rejects_under_normal_market(name, atr, conf, same_dir):
    """常规行情 + 同向持仓衰减下，手数链路一律不得拒单。

    钳手只有在"真实余额连 0.01 手都撑不住"时才允许返回 None，
    四个生产账号都远不到那个地步。
    """
    final, sized, _, bal = _pipeline(name, atr, conf, same_dir)
    assert final is not None, (
        f"❌ {name} 被手数链路拒单（余额${bal:.0f}, atr={atr}, conf={conf}, "
        f"同向={same_dir}, sizing={sized}）—— 违反『多交易多赚钱』铁律"
    )


# ══════════════════════════════════════════════════════════
# 铁律 D：不爆仓（最终手数必须能过 risk_engine Layer 5）
# ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", list(ACCOUNTS))
@pytest.mark.parametrize("atr,conf", MARKET)
def test_D_final_lot_passes_layer5_risk_check(name, atr, conf):
    """最终手数的实际风险 ≤ max_risk_per_trade_pct。

    否则 risk_engine Layer 5（同样按真实余额算）会拒单 —— 手数白算一场。
    这条同时解释了钳手为何用 floor 而非 round：
      sizing 的 round 可能把 0.0193 手抬到 0.02（风险 2.08% > 2%）→ Layer 5 拒单；
      钳手 floor 到 0.01 才是**唯一能真正成交**的手数。floor 不是保守，是可成交性。
    """
    final, _, strat, bal = _pipeline(name, atr, conf)
    assert final is not None
    risk_amount = atr * final * 100.0          # 黄金每手每点 $100
    risk_pct = risk_amount / bal * 100.0
    assert risk_pct <= strat.max_risk_per_trade_pct + 1e-6, (
        f"❌ {name} 最终手数 {final} 风险 {risk_pct:.3f}% "
        f"超过上限 {strat.max_risk_per_trade_pct}% → risk_engine Layer 5 会拒单"
    )


def test_D2_floor_vs_round_gap_is_intentional():
    """★自证：证明上一条不是空话 —— sizing 的 round 确实会越线，靠钳手兜回来。"""
    prof = ACCOUNTS["liumanchuan2"]
    strat = _Strategy(**prof["cfg"])
    bal, atr = prof["balance"], 25.0

    sized = compute_intelligent_size(
        balance=bal, atr=atr, signal_confidence=0.75,
        same_direction_count=0, strategy=strat,
    )["lots"]
    # sizing 结果若直接下单，风险率：
    naive_risk = atr * sized * 100.0 / bal * 100.0

    ex = te.TradeExecutor.__new__(te.TradeExecutor)
    ex.strategy = strat
    ex.account_id = "liumanchuan2"
    final, _ = ex._cap_to_risk_limit(bal, atr, sized)
    final_risk = atr * final * 100.0 / bal * 100.0

    assert naive_risk > 2.0, (
        f"自证失败：sizing 结果风险 {naive_risk:.3f}% 本就没越线，"
        f"这条用例没证明钳手的价值"
    )
    assert final_risk <= 2.0, f"钳手没能把风险压回合规：{final_risk:.3f}%"


# ══════════════════════════════════════════════════════════
# 多账号铁律：账号数是变量，不得写死 4
# ══════════════════════════════════════════════════════════
def test_scales_to_any_account_count():
    """N=1 与 N=10+ 都必须走同一套逻辑（模拟 10 个异构账号）。

    ⚠ 起步本金取 $2000：低于此值时，券商最小手数 0.01 在常态止损
      （生产日志实测 SL 26~33 点）下风险已超 2%，钳手会拒单。
      这是一个**已确认的独立缺陷**（产品宣称支持 $1000 起本金），
      单独立项调研修复，不在本文件的验收范围内 —— 本文件只验收
      "手数在支持区间内可预期"，不掩盖也不顺手修那个问题。
    """
    finals = []
    for i in range(10):
        bal = 2000.0 * (i + 1)              # $2000 ~ $20000
        strat = _Strategy(
            capital_source="live", base_capital=0, max_risk_per_trade_pct=2.0,
            min_lot_per_trade=0.01, max_lot_per_trade=0.5, max_position_lots=1.0,
            sizing_scale_mode="auto", sizing_mode="smart",
            volatility_factor=1.0, same_direction_decay=0.5,
        )
        sized = compute_intelligent_size(
            balance=bal, atr=20.0, signal_confidence=0.75,
            same_direction_count=0, strategy=strat,
        )["lots"]
        ex = te.TradeExecutor.__new__(te.TradeExecutor)
        ex.strategy = strat
        ex.account_id = f"acc{i}"
        final, _ = ex._cap_to_risk_limit(bal, 20.0, sized)
        finals.append((bal, final))

    for bal, final in finals:
        assert final is not None, f"余额${bal:.0f} 账号被拒单"
        assert 0.01 - 1e-9 <= final <= 0.5 + 1e-9, f"余额${bal:.0f} 手数越界：{final}"

    # 手数应随本金单调不减（自适应的基本性质）
    lots = [f for _, f in finals]
    assert lots == sorted(lots), f"手数未随本金单调递增：{lots}"
