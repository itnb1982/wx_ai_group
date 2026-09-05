"""
万象Ai — 本金与手数边界【单一权威源】(Capital & Lot Authority)
================================================================
V6 架构 §4.2「权威链」的唯一实现点。任何模块需要知道
「这个账号的有效本金是多少 / 手数硬边界是多少」，都必须调用本模块，
禁止再各自 getattr(strategy, 'base_capital') 自行判断。

── 为什么必须存在（重构前的病）──────────────────────────────
重构前同一套判断散落三处且口径不一致：
  1. intelligent_sizing.compute_intelligent_size()  L115  manual/live 二分支
  2. risk_engine.RiskEngine._effective_balance()    L68   manual/live 二分支（复制品）
  3. trade_executor._cap_to_risk_limit()            L531  直接用真实 balance（第三口径！）
后果：手数按 base_capital=$32000 算出 0.3 手，反钳按真实 $2408 又压回 0.01 手，
      两套本金打架 → 客户看到的手数与后台设置对不上。

── 权威链（优先级从高到低，V6 §4.2 铁律）──────────────────────
  1. input_capital   客户/运维在客户端明确输入的本金   ← 权限最高
  2. base_capital    后台策略手填参考本金 (capital_source='manual')
  3. balance         MT5 实时账户余额 (capital_source='live'，默认)
"客户输入的本金参数，权限比仓位自带金额大" —— 用户原话。

── 兼容承诺（扼杀者模式 Strangler Fig）─────────────────────────
当 input_capital 缺省/为 0 时，本模块行为与重构前 100% 等价，
因此可以先原地替换三处调用而不改变任何交易行为，
待回归比对台验证通过后，再打开 input_capital 与硬边界开关。
"""
from dataclasses import dataclass
from typing import Any, Optional

# ── 常量（与重构前保持完全一致，不得随手改动）──────────────────
REF_CAPITAL = 1000.0      # 手数上限缩放的参考本金基准（$1000 = 1x）
LOT_SCALE_CAP = 50.0      # 缩放倍数封顶，防极端本金把单笔手数放到滑点灾难区
BROKER_MIN_LOT = 0.01     # 券商最小手数

# ── V6 §4.3 手数硬边界铁律 ─────────────────────────────────────
# True  = 后台设定的 max_lot / max_position_lots 是【不可突破的天花板】，
#         auto 缩放只能在其下取值（小本金收紧允许，放大禁止）。
# False = 历史行为（auto 可把上限放大至 50 倍）。仅用于紧急回退。
#
# 【为什么必须为 True — 2026-08-07 生产库实证】
#   liumanchun1  后台设定 max_lot=1.0 手 → 实际成交最大 11.13 手（超限 50 笔）
#   liumanchun3  后台设定 max_lot=1.0 手 → 实际成交最大 10.62 手（超限 45 笔）
#   根因：$989k 余额 ÷ $1000 基准 = 989x，被 LOT_SCALE_CAP 截到 50x，
#         于是 1.0 手上限被静默放大成 50 手，风控形同虚设。
#   这对商业客户是事故级问题：客户设 1 手，系统敢开 11 手。
#   用户铁律原文：「手数管理也要以输入的手数权限最大，
#                  最低/最高手数硬边界不可突破，中间由行情决定」。
#
# 【为什么不违反"多交易多赚钱"铁律】
#   硬边界只压手数、不拒单：超限的 95 笔会被压到 1.0 手照常成交，
#   交易笔数一笔不减，仅收敛单笔敞口。属"限幅"而非"过滤"。
LOT_HARD_BOUNDS_DEFAULT = True


# ══════════════════════════════════════════════════════════════
# 结果对象
# ══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CapitalDecision:
    """本金裁定结果（不可变，便于原样写进决策溯源日志）"""
    value: float          # 最终生效本金（$）
    source: str           # 'input' | 'manual' | 'live'
    label: str            # 人类可读来源，用于日志与前端溯源面板
    balance: float        # 参与裁定的 MT5 真实余额（始终记录，供审计）

    @property
    def is_synthetic(self) -> bool:
        """是否使用了"非真实余额"的本金（input/manual）。

        为 True 时，风控反钳必须额外用真实余额兜底，
        否则会出现"按 $32000 算手数、真账户只有 $2408"的爆仓风险。
        """
        return self.source in ("input", "manual")


@dataclass(frozen=True)
class LotBounds:
    """手数边界裁定结果"""
    min_lot: float            # 单笔最小手数（生效值）
    max_lot: float            # 单笔最大手数（生效值，auto 模式下已缩放）
    max_position_lots: float  # 同方向持仓总手数上限（生效值）
    raw_max_lot: float        # 用户后台设定的原始单笔上限（硬边界原值）
    raw_max_position_lots: float
    scale: float              # 实际生效的缩放倍数
    mode: str                 # 'auto' | 'manual'


# ══════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════
def _cfg(strategy: Any, key: str, default):
    """统一读取策略配置（支持 dict / ORM 对象 / None）。

    注意：沿用 intelligent_sizing._cfg 的语义 —— 取到假值(0/''/None)也回退 default，
    这是历史行为，改动会影响"用户把 base_capital 设成 0"的场景，故原样保留。
    """
    if strategy is None:
        return default
    if isinstance(strategy, dict):
        v = strategy.get(key, default)
        return v if v not in (None, "") else default
    return getattr(strategy, key, default) or default


def _cfg_raw(strategy: Any, key: str, default=None):
    """读取配置【不做假值回退】—— 布尔开关必须走这里。

    _cfg 沿用历史的 `or default` 语义，会把 False/0 当成"没配置"而回退默认值，
    导致「客户显式关掉某开关」失效。布尔字段一律用本函数。
    """
    if strategy is None:
        return default
    if isinstance(strategy, dict):
        return strategy.get(key, default)
    return getattr(strategy, key, default)


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════
# 权威 API 1：本金
# ══════════════════════════════════════════════════════════════
def effective_capital(
    strategy: Any,
    balance: float = 0.0,
    input_capital: Optional[float] = None,
) -> CapitalDecision:
    """裁定该账号的有效本金 —— 全系统唯一入口。

    Args:
        strategy:      策略配置（ORM 或 dict，可为 None）
        balance:       MT5 实时账户余额
        input_capital: 客户端输入本金。显式传入且 >0 时权限最高；
                       若为 None，会尝试从 strategy.input_capital 读取
                       （字段尚未落库时自动回退，行为等价于重构前）。

    Returns:
        CapitalDecision
    """
    bal = max(_to_float(balance, 0.0), 0.0)

    # ── 第 1 顺位：客户端输入本金（权限最高）────────────────
    ic = input_capital
    if ic is None:
        ic = _cfg(strategy, "input_capital", 0)   # 字段未落库时恒为 0 → 等价旧行为
    ic = _to_float(ic, 0.0)
    if ic > 0:
        return CapitalDecision(
            value=ic, source="input",
            label="input(客户输入本金)", balance=bal,
        )

    # ── 第 2 顺位：后台手填参考本金 ─────────────────────────
    capital_source = str(_cfg(strategy, "capital_source", "live") or "live").lower()
    base_capital = _to_float(_cfg(strategy, "base_capital", 0), 0.0)
    if capital_source == "manual" and base_capital > 0:
        return CapitalDecision(
            value=base_capital, source="manual",
            label="manual(base_capital)", balance=bal,
        )

    # ── 第 3 顺位：MT5 实时余额（默认）──────────────────────
    return CapitalDecision(
        value=bal, source="live",
        label="live(balance)", balance=bal,
    )


# ══════════════════════════════════════════════════════════════
# 权威 API 2：手数边界
# ══════════════════════════════════════════════════════════════
def resolve_lot_bounds(
    strategy: Any,
    effective_balance: float,
    enforce_hard_bounds: Optional[bool] = None,
) -> LotBounds:
    """裁定该账号的手数边界 —— 全系统唯一入口。

    auto 模式（默认，等价重构前）：
        上限 = 后台设定值 × min(有效本金 / $1000, 50)
        —— 大本金账号自动放开（根治"单笔就触顶总手数上限"无辜拒单）；
        —— 小本金自动收紧（更保守防爆仓）；封顶 50x 防极端滑点。

    manual 模式：直接用后台写死上限。

    Args:
        enforce_hard_bounds: V6 §4.3「手数硬边界不可突破」开关。
            None（默认）= 按优先级取值：策略字段 enforce_lot_hard_bounds
                          > 模块常量 LOT_HARD_BOUNDS_DEFAULT（当前 True）
            True  = 后台 max_lot 是天花板，缩放只能向下不能向上
            False = 历史行为（auto 可放大 50 倍），仅供紧急回退与等价对拍
    """
    raw_min = _to_float(_cfg(strategy, "min_lot_per_trade", 0.01), 0.01)
    raw_max = _to_float(_cfg(strategy, "max_lot_per_trade", 1.0), 1.0)
    raw_pos = _to_float(_cfg(strategy, "max_position_lots", 1.0), 1.0)
    mode = str(_cfg(strategy, "sizing_scale_mode", "auto") or "auto").lower()

    if enforce_hard_bounds is None:
        # 策略级可覆盖（字段尚未落库时 _cfg 返回默认值 → 走全局常量）
        _flag = _cfg_raw(strategy, "enforce_lot_hard_bounds", None)
        enforce_hard_bounds = (
            LOT_HARD_BOUNDS_DEFAULT if _flag is None else bool(_flag)
        )

    eb = _to_float(effective_balance, 0.0)
    scale = (eb / REF_CAPITAL) if eb > 0 else 1.0

    if mode == "auto":
        k = min(scale, LOT_SCALE_CAP)
        eff_max = raw_max * k
        eff_pos = raw_pos * k
        if enforce_hard_bounds:
            # 硬边界模式：缩放不得越过用户设定的天花板
            eff_max = min(eff_max, raw_max)
            eff_pos = min(eff_pos, raw_pos)
    else:
        k = 1.0
        eff_max, eff_pos = raw_max, raw_pos

    return LotBounds(
        min_lot=max(raw_min, BROKER_MIN_LOT),
        max_lot=eff_max,
        max_position_lots=eff_pos,
        raw_max_lot=raw_max,
        raw_max_position_lots=raw_pos,
        scale=k,
        mode=mode,
    )


# ══════════════════════════════════════════════════════════════
# 权威 API 3：风控反钳的本金口径
# ══════════════════════════════════════════════════════════════
def risk_check_capital(decision: CapitalDecision) -> float:
    """风控校验（反钳手数、日损上限）应当使用的本金。

    铁律：**风控永远以能真实亏掉的钱为准**。
    - source='live'            → 就是余额本身
    - source='input'/'manual'  → 取 min(设定本金, 真实余额)
      设高了不能让客户真去冒不存在的钱的风险；
      设低了（客户想保守）则尊重客户的低设定。

    这正是重构前 trade_executor._cap_to_risk_limit 直接用 balance 的意图，
    但那里丢掉了"客户主动调低"的语义，此处补齐。
    """
    if decision.source == "live":
        return decision.balance
    if decision.balance <= 0:
        # 余额拿不到（IPC 故障/未连接）→ 退回设定本金，交由上层降级逻辑处理
        return decision.value
    return min(decision.value, decision.balance)
