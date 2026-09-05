"""
XAU/USD万象Ai自动量化交易系统 — 6层物理风控引擎
独立于AI决策，不参与辩论，只做最终安全审批

★ P0-1 根因修复（2026-08-05）：
  原实现直接在父进程调用 MetaTrader5.mt5.*（symbol_info / positions_get /
  history_deals_get / account_info）。但本系统每个账号是独立 Worker 子进程持有
  自己的 mt5.initialize()，父进程（uvicorn）从未连接任何终端，所有 mt5.* 调用均
  返回 None —— 导致 Layer1 点差 / Layer2 持仓数 / Layer2b 同向并发 / Layer3 日亏 /
  Layer4 回撤 全部静默通过（等于风控被悄悄关掉，仅 Layer5/6 纯计算层生效）。
  现改为：所有行情/持仓/账户/历史数据一律通过 mt5_service IPC，按 account_id
  向对应 Worker 查询。若未传入 mt5_service/account_id（配置缺失），数据相关层
  一律"失败关闭"（拒绝开仓并明确告警），绝不再静默放行。
"""
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from app.services.capital_authority import effective_capital, resolve_lot_bounds


class RejectCode:
    """风控拒绝原因的结构化事件码（Phase 4 溯源）。

    为什么不能只靠中文文本？因为统计聚合要靠它。运营要回答的问题是
    「哪条风控砍单最多、砍得对不对、要不要调参」—— 一旦用中文做 key，
    某天文案改一个字，历史统计就断成两截。文案给人看，code 给机器用。
    """
    SPREAD_DATA_UNAVAILABLE = "SPREAD_DATA_UNAVAILABLE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    POSITION_DATA_UNAVAILABLE = "POSITION_DATA_UNAVAILABLE"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_POSITION_LOTS = "MAX_POSITION_LOTS"
    SAME_DIRECTION_LIMIT = "SAME_DIRECTION_LIMIT"
    DAILY_PNL_DATA_UNAVAILABLE = "DAILY_PNL_DATA_UNAVAILABLE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    EQUITY_DATA_UNAVAILABLE = "EQUITY_DATA_UNAVAILABLE"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    PER_TRADE_RISK_LIMIT = "PER_TRADE_RISK_LIMIT"
    MARKET_CLOSED_WEEKEND = "MARKET_CLOSED_WEEKEND"
    SESSION_DISABLED = "SESSION_DISABLED"
    # 非风控引擎层，但共用同一套事件码空间（执行器 / Phase 6 闸门）
    EXECUTOR_MAX_POSITIONS = "EXECUTOR_MAX_POSITIONS"
    DEGRADE_L3_CIRCUIT = "DEGRADE_L3_CIRCUIT"


class Reason(str):
    """带结构化事件码的拒绝原因。

    ★ 这是一个 str 子类，不是新类型。这样设计的唯一目的是**零破坏**：
      各层依旧 `return False, Reason("点差过大", CODE)`，调用方
      `passed, reason = eng._check_spread(...)` 照常解包、照常比较、照常
      f-string 拼接 —— 既有 8 处测试与所有调用点一行都不用改。
      唯一多出来的是 `reason.code`，给溯源用。

      备选方案是把各层改成返回三元组，但那要同时改 8 处测试；
      在一个已经跑着实盘的系统上，能不动的调用面就不要动。
    """

    __slots__ = ("code",)

    def __new__(cls, text: str, code: str = ""):
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


def reason_code(reason) -> str:
    """从原因对象取事件码；普通 str（历史代码/测试构造）返回空串。"""
    return getattr(reason, "code", "") or ""


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool = True
    risk_level: str = "low"
    reject_reasons: list = field(default_factory=list)
    max_allowed_lots: float = 1.0
    daily_pnl: float = 0.0
    current_drawdown: float = 0.0

    @property
    def reject_codes(self) -> list:
        """结构化事件码列表（与 reject_reasons 一一对应，缺码的位置给 UNKNOWN）。

        做成 property 而不是字段：避免两份数据不同步 —— 只要有人往
        reject_reasons 里 append 而忘了同步 codes，统计就会静默失真。
        """
        return [reason_code(r) or "UNKNOWN" for r in self.reject_reasons]


class RiskEngine:
    """
    6层物理安全网
    Layer 1: 点差检查
    Layer 2: 最大持仓限制
    Layer 3: 日亏损上限
    Layer 4: 回撤熔断
    Layer 5: 单笔风险上限
    Layer 6: 交易时段检查

    所有依赖实时行情/持仓/账户数据的层（1/2/2b/3/4）均通过 mt5_service IPC 查询，
    不再在父进程直接调用 mt5.*（父进程未连接终端）。
    """

    def __init__(self, strategy_config=None, mt5_service=None, account_id: str = ""):
        # 兼容 dict 和 StrategyConfig 对象
        if strategy_config is None:
            self._config = {}
        elif isinstance(strategy_config, dict):
            self._config = strategy_config
        else:
            self._config = {
                k: v for k, v in strategy_config.__dict__.items()
                if not k.startswith('_')
            }
        # ★ P0-1：持有 IPC 服务与账号上下文，所有实时数据走这里查询
        self._mt5_service = mt5_service
        self._account_id = account_id or ""

    def _get(self, key: str, default=None):
        """统一获取配置值"""
        if hasattr(self, '_config') and isinstance(self._config, dict):
            return self._config.get(key, default)
        return default

    # ===== 本金 / 手数上限：一律委托 capital_authority 单一权威 =====
    #   ⚠ 此处曾经复制过一份 manual/live 判断与缩放公式，与 intelligent_sizing
    #     各自演化导致口径漂移。V6 §4.2/§4.3 起统一收口，禁止再写内联判断。
    def _effective_balance(self, account_balance: float = 0.0) -> float:
        """本金来源解析（账户私有·不继承主号）。权威链见 capital_authority.py。"""
        return effective_capital(self._config_view(), balance=account_balance).value

    def _scaled_max_position_lots(self, effective_balance: float) -> float:
        """持仓总手数上限（auto 模式随本金等比缩放，封顶 50x）。"""
        return resolve_lot_bounds(
            self._config_view(), effective_balance
        ).max_position_lots

    def _config_view(self) -> dict:
        """把风控引擎自身的配置字典暴露给权威模块（保持 dict 语义一致）。"""
        cfg = getattr(self, "_config", None)
        return cfg if isinstance(cfg, dict) else {}

    # ========== IPC 数据获取（按 account_id 向对应 Worker 查询） ==========

    def _fetch_positions(self, symbol: str):
        """返回持仓列表(dict) 或 None（不可用）。None 必须触发失败关闭。

        ★ 2026-08-07 Phase 1：改用 get_positions_checked。
        原实现调 get_positions()，它在 Worker 掉线/超时时同样返回 []，
        于是本函数**永远返回不了 None**，上层三处 `if positions is None:
        暂缓开仓` 全是死代码 —— 持仓数据一缺失就被当成"零持仓"，
        最大笔数/最大手数/同向并发三道门集体放行，直接导致超仓。
        这与本模块顶部 P0-1 记的"风控被悄悄关掉"是同一个教训的第二次复发。
        """
        if self._mt5_service is None or not self._account_id:
            return None
        try:
            _fn = getattr(self._mt5_service, "get_positions_checked", None)
            if _fn is None:      # 兼容未升级的注入对象（测试替身/旧版本）
                return self._mt5_service.get_positions(self._account_id, symbol) or []
            ok, positions = _fn(self._account_id, symbol)
            if not ok:
                logger.warning(
                    f"[风控] 持仓数据源不可用(account={self._account_id}) → 失败关闭，暂缓开仓")
                return None
            return positions or []
        except Exception as e:
            logger.warning(f"[风控] 获取持仓失败(account={self._account_id}): {e}")
            return None

    def _fetch_account_info(self):
        """返回账户信息 dict 或 None（不可用）。"""
        if self._mt5_service is None or not self._account_id:
            return None
        try:
            info = self._mt5_service.get_account_info(self._account_id)
            if isinstance(info, dict) and "error" not in info:
                return info
            return None
        except Exception as e:
            logger.warning(f"[风控] 获取账户信息失败(account={self._account_id}): {e}")
            return None

    def _fetch_daily_pnl(self):
        """返回今日净盈亏(float) 或 None（不可用）。基于历史成交(net_profit)求和。"""
        if self._mt5_service is None or not self._account_id:
            return None
        try:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            now = datetime.now()
            # ★ 2026-08-07 Phase 1：同 _fetch_positions —— get_history_deals()
            #   失败时返回 {"deals": [], ...}，与"今天没成交"同形，会让本函数
            #   算出 0.0 而非 None，日亏损熔断的失败关闭因此永不触发。
            _fn = getattr(self._mt5_service, "get_history_deals_checked", None)
            if _fn is not None:
                ok, data = _fn(self._account_id, today, now)
                if not ok:
                    logger.warning(
                        f"[风控] 日盈亏数据源不可用(account={self._account_id}) → 失败关闭，暂缓开仓")
                    return None
            else:                # 兼容未升级的注入对象（测试替身/旧版本）
                data = self._mt5_service.get_history_deals(self._account_id, today, now)
            if not isinstance(data, dict):
                return None
            deals = data.get("deals") or []
            total = 0.0
            for d in deals:
                net = d.get("net_profit")
                if net is not None:
                    total += float(net)
            return total
        except Exception as e:
            logger.warning(f"[风控] 获取历史成交失败(account={self._account_id}): {e}")
            return None

    def _fetch_spread(self, symbol: str):
        """返回点差(float) 或 None（不可用）。"""
        if self._mt5_service is None or not self._account_id:
            return None
        try:
            tick = self._mt5_service.get_tick(self._account_id, symbol)
            if isinstance(tick, dict) and "error" not in tick:
                return tick.get("spread")
            return None
        except Exception as e:
            logger.warning(f"[风控] 获取点差失败(account={self._account_id}): {e}")
            return None

    # ========== 6层安全检查 ==========

    def check_trade_allowed(
        self,
        symbol: str = "XAUUSD",
        volume: float = 0.01,
        entry_price: float = 0,
        stop_loss: float = 0,
        account_balance: float = 0,
        direction: str = "",
    ) -> RiskCheckResult:
        """
        全面的风控检查
        direction: 准备开仓方向 'BUY' / 'SELL'（用于同方向并发上限判断）
        """
        result = RiskCheckResult()

        # 重新计算日盈亏（用于结果展示/日志）
        daily_pnl = self._fetch_daily_pnl()
        result.daily_pnl = daily_pnl if daily_pnl is not None else 0.0

        # Layer 1: 点差检查
        spread_ok, spread_msg = self._check_spread(symbol)
        if not spread_ok:
            result.reject_reasons.append(spread_msg)

        # Layer 2: 最大持仓笔数（硬上限）+ 最大持仓手数
        pos_ok, pos_msg = self._check_position_limits(symbol, volume, account_balance)
        if not pos_ok:
            result.reject_reasons.append(pos_msg)
        result.max_allowed_lots = self._scaled_max_position_lots(self._effective_balance(account_balance))

        # Layer 2b: 同方向并发上限（防止同方向金字塔爆仓）
        if direction:
            max_concurrent = int(self._get("max_concurrent_same_direction", 3) or 3)
            dir_ok, dir_msg = self.check_same_direction(direction, max_concurrent)
            if not dir_ok:
                result.reject_reasons.append(dir_msg)

        # Layer 3: 日亏损上限
        loss_ok, loss_msg = self._check_daily_loss(account_balance)
        if not loss_ok:
            result.reject_reasons.append(loss_msg)

        # Layer 4: 回撤熔断
        dd = self._get_current_drawdown(account_balance)
        result.current_drawdown = dd if dd is not None else 0.0
        dd_ok, dd_msg = self._check_drawdown(account_balance, dd)
        if not dd_ok:
            result.reject_reasons.append(dd_msg)

        # Layer 5: 单笔风险
        risk_ok, risk_msg = self._check_per_trade_risk(volume, entry_price, stop_loss, account_balance)
        if not risk_ok:
            result.reject_reasons.append(risk_msg)

        # Layer 6: 交易时段
        time_ok, time_msg = self._check_trading_hours()
        if not time_ok:
            result.reject_reasons.append(time_msg)

        # 综合判断
        if result.reject_reasons:
            result.passed = False
            result.risk_level = "extreme" if len(result.reject_reasons) >= 3 else "high"
            logger.warning(f"[风控] ❌ 拒绝交易: {'; '.join(result.reject_reasons)}")
        else:
            result.passed = True
            logger.info("[风控] ✅ 风控检查全部通过")

        return result

    def _check_spread(self, symbol: str) -> tuple:
        """Layer 1: 点差检查（经 IPC 取实时点差）"""
        max_spread = self._get("max_spread_points", 50)
        spread = self._fetch_spread(symbol)
        if spread is None:
            return False, Reason("点差数据不可用（风控数据源缺失），暂缓开仓",
                                 RejectCode.SPREAD_DATA_UNAVAILABLE)
        if spread > max_spread:
            return False, Reason(f"点差过大({spread:.1f}>{max_spread})",
                                 RejectCode.SPREAD_TOO_WIDE)
        return True, ""

    def _check_position_limits(self, symbol: str, new_volume: float, account_balance: float = 0.0) -> tuple:
        """Layer 2: 最大持仓笔数（硬上限）+ 最大持仓手数（经 IPC 取实时持仓）

        ★ 修复：原先把"总持仓数"误当"同向持仓数"来卡 max_concurrent_same_direction，
           现改为：①总笔数 ≤ max_positions ②总手数 ≤ max_position_lots。
           同方向并发上限移至 Layer 2b（check_same_direction）单独判断。
        ★ 风控上限按本金自适应（与 intelligent_sizing.py 同口径，国际调研精髓 ≥3源交叉验证）：
           max_position_lots 在 auto 模式下随 effective_balance 等比缩放（封顶防极端），
           根治大本金账号"单笔就触顶总手数上限"的无辜拒单（超最大持仓手数 633 次）。
        """
        max_positions = int(self._get("max_positions", 10) or 10)
        # 与 intelligent_sizing.py 完全一致的本金自适应上限
        effective_balance = self._effective_balance(account_balance)
        max_lots = self._scaled_max_position_lots(effective_balance)
        positions = self._fetch_positions(symbol)
        if positions is None:
            return False, Reason("持仓数据不可用（风控数据源缺失），暂缓开仓",
                                 RejectCode.POSITION_DATA_UNAVAILABLE)

        # ① 最大持仓笔数（独立于其他账号的硬上限）
        current_count = len(positions)
        if current_count >= max_positions:
            return False, Reason(f"已达最大持仓笔数({current_count}>={max_positions})",
                                 RejectCode.MAX_POSITIONS)

        # ② 最大持仓手数（累计，本金自适应上限）
        current_volume = sum(float(p.get("volume", 0) or 0) for p in positions)
        if current_volume + new_volume > max_lots:
            return False, Reason(
                f"超最大持仓手数({current_volume:.2f}+{new_volume:.2f}>{max_lots:.2f})",
                RejectCode.MAX_POSITION_LOTS)

        return True, ""

    def check_same_direction(self, side: str, max_concurrent: int = 3) -> tuple:
        """检查某方向是否已达并发上限（经 IPC 取实时持仓）"""
        side = (side or "").lower()
        positions = self._fetch_positions("XAUUSD")
        if positions is None:
            return False, Reason("持仓数据不可用（风控数据源缺失），暂缓开仓",
                                 RejectCode.POSITION_DATA_UNAVAILABLE)
        same = [p for p in positions if (p.get("type") or "").lower() == side]
        if len(same) >= max_concurrent:
            return False, Reason(
                f"{side.upper()} 同向持仓已 {len(same)} 单 (上限 {max_concurrent})",
                RejectCode.SAME_DIRECTION_LIMIT)
        return True, ""

    def _check_daily_loss(self, balance: float) -> tuple:
        """Layer 3: 日亏损上限（经 IPC 取今日净盈亏）"""
        max_loss_pct = self._get("max_daily_loss_pct", 5.0)
        daily_pnl = self._fetch_daily_pnl()
        if daily_pnl is None:
            return False, Reason("日盈亏数据不可用（风控数据源缺失），暂缓开仓",
                                 RejectCode.DAILY_PNL_DATA_UNAVAILABLE)
        if balance > 0 and daily_pnl < 0:
            loss_pct = abs(daily_pnl) / balance * 100
            if loss_pct > max_loss_pct:
                return False, Reason(f"日亏损超限({loss_pct:.1f}%>{max_loss_pct}%)",
                                     RejectCode.DAILY_LOSS_LIMIT)
        return True, ""

    def _check_drawdown(self, balance: float, dd: Optional[float] = None) -> tuple:
        """Layer 4: 回撤熔断（经 IPC 取账户权益）"""
        max_dd_pct = self._get("max_drawdown_pct", 20.0)
        if dd is None:
            dd = self._get_current_drawdown(balance)
        if dd is None:
            return False, Reason("账户权益数据不可用（风控数据源缺失），暂缓开仓",
                                 RejectCode.EQUITY_DATA_UNAVAILABLE)
        if dd > max_dd_pct:
            return False, Reason(f"回撤熔断({dd:.1f}%>{max_dd_pct}%)",
                                 RejectCode.DRAWDOWN_HALT)
        return True, ""

    def _check_per_trade_risk(self, volume: float, entry: float, sl: float, balance: float) -> tuple:
        """Layer 5: 单笔风险上限（纯计算，不依赖实时数据）"""
        max_risk_pct = self._get("max_risk_per_trade_pct", 2.0)
        if balance > 0 and sl > 0 and entry > 0 and volume > 0:
            risk_amount = abs(entry - sl) * volume * 100  # 黄金每手每点$100
            risk_pct = risk_amount / balance * 100
            if risk_pct > max_risk_pct:
                return False, Reason(f"单笔风险超限({risk_pct:.1f}%>{max_risk_pct}%)",
                                     RejectCode.PER_TRADE_RISK_LIMIT)
        return True, ""

    def _check_trading_hours(self) -> tuple:
        """Layer 6: 交易时段检查（纯日期计算，不依赖实时数据）"""
        now = datetime.now()
        hour = now.hour

        asian = self._get("trade_asian", True)
        european = self._get("trade_european", True)
        american = self._get("trade_american", True)

        # 周六日全天休息
        if now.weekday() >= 5:
            return False, Reason("周末休市", RejectCode.MARKET_CLOSED_WEEKEND)

        # 检查当前时段
        is_asian = 7 <= hour < 15
        is_european = 15 <= hour < 20
        is_american = 20 <= hour or hour < 3

        if is_asian and not asian:
            return False, Reason("亚盘时段已关闭", RejectCode.SESSION_DISABLED)
        if is_european and not european:
            return False, Reason("欧盘时段已关闭", RejectCode.SESSION_DISABLED)
        if is_american and not american:
            return False, Reason("美盘时段已关闭", RejectCode.SESSION_DISABLED)

        return True, ""

    # ========== 辅助计算 ==========

    def _get_current_drawdown(self, balance: float):
        """获取当前回撤百分比（经 IPC 取账户权益），无数据返回 None。"""
        info = self._fetch_account_info()
        if info is None:
            return None
        equity = info.get("equity")
        if equity is None or balance <= 0:
            return 0.0
        return (balance - equity) / balance * 100
