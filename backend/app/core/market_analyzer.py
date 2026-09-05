"""
XAU/USD万象Ai自动量化交易系统 — 市场数据采集分析器
采集 XAUUSD 多时间框架技术指标 + DXY/VIX 外部数据，为AI决策提供完整市场视图

数据来源：
  - XAUUSD 技术指标: 行情主号 Worker (MT5 原始 OHLCV → 本地计算)
  - DXY/VIX/相关性: market_data_provider (yfinance → 本地计算)
"""

from datetime import datetime, timedelta
from typing import Optional, List
from loguru import logger
import time

# ── 从 services 导入（延迟导入以避免循环依赖） ──
from app.services.market_data import market_data_provider

# ── 订单流CVD 源状态缓存（供仪表盘呈现「真CVD(Binance) / 代理(MT5)」）──
_ORDERFLOW_STATUS_CACHE = {"data": None, "ts": 0.0}
_ORDERFLOW_STATUS_TTL = 2.0  # 秒，与主轴循环同频


def _cache_orderflow_status(of):
    """把最近一次订单流快照的源状态缓存下来，供 /dashboard/system-health 读取。"""
    if not isinstance(of, dict):
        return
    src = of.get("source")
    sub = of.get(src) if src else None
    is_real = bool(sub.get("is_real_cvd")) if isinstance(sub, dict) else False
    _ORDERFLOW_STATUS_CACHE["data"] = {
        "available": bool(of.get("available")),
        "source": src,
        "is_real_cvd": is_real,
        "reading": of.get("reading"),
        "buy_pressure_dry": of.get("buy_pressure_dry"),
        "sell_pressure_high": of.get("sell_pressure_high"),
        "available_sources": of.get("available_sources"),
    }
    _ORDERFLOW_STATUS_CACHE["ts"] = time.time()


def get_orderflow_status():
    """仪表盘用：返回订单流CVD 是否为真实源（Binance）还是代理（MT5），供客户可见。"""
    data = _ORDERFLOW_STATUS_CACHE["data"]
    if data is None:
        return {"available": False, "live": False, "source": None, "is_real_cvd": False}
    age = time.time() - _ORDERFLOW_STATUS_CACHE["ts"]
    return {**data, "live": age <= _ORDERFLOW_STATUS_TTL * 3}


class MarketAnalyzer:
    """市场数据采集与技术指标计算

    不再直接调用 mt5.initialize()。
    改为接收 MT5Service + 行情主号 ID，通过 Worker Pipe 获取原始 OHLCV 数据。
    """

    SYMBOL = "XAUUSD"

    def __init__(self, mt5_service=None, market_primary_id: str = ""):
        """
        Args:
            mt5_service: MT5Service 实例（多进程管理）
            market_primary_id: 行情主号的 account_id DB UUID
        """
        self._mt5 = mt5_service
        self._primary_id = market_primary_id
        self._cached_snapshot: Optional[dict] = None
        self._cache_time: Optional[datetime] = None

    def set_primary(self, mt5_service, primary_id: str):
        """运行时设置/切换行情主号"""
        self._mt5 = mt5_service
        self._primary_id = primary_id
        logger.info(f"[MarketAnalyzer] 行情主号已设置为: {primary_id[:8]}...")

    # ════════════════════════════════════════════════════════════════
    #  主入口
    # ════════════════════════════════════════════════════════════════

    def get_market_snapshot(self) -> dict:
        """
        获取完整市场快照 — AI 辩论入口

        流程:
          1. 从行情主号 Worker 获取 XAUUSD 原始 OHLCV
          2. 本地计算全部技术指标
          3. 从外部 API 获取 DXY/VIX/相关性
          4. 两者融合返回
        """
        raw = self._fetch_raw_market_data()
        external = self._fetch_external_data()

        if raw is None or "error" in raw:
            logger.warning("[MarketAnalyzer] XAUUSD 行情数据不可用，使用模拟数据")
            snapshot = self._get_mock_snapshot()
            # ★ P1-2 根因修复：模拟快照必须打标。否则 decide() 会拿随机噪声
            #   当真实行情去跑 AI 辩论并开仓（等于在噪声上下注）。下游（debate_engine）
            #   据此 HOLD，禁止实盘在"无真实行情"时开新仓。
            snapshot["simulated"] = True
            snapshot["data_quality"] = "simulated"
            # ★ 2026-08-16 管理后台审计修复：market_data 是唯一缺上报的组件——
            #   行情失败/模拟时如实上报失败（降级监视器据此判 L3 行情失联）。
            try:
                from app.services.platform_health_monitor import report_fail
                report_fail("market_data", "XAUUSD 行情不可用/模拟")
            except Exception:
                pass
        else:
            snapshot = self._build_from_raw(raw)
            # ★ 2026-08-16 管理后台审计修复：真实行情成功 → 上报 OK（monitor 组件复活）
            try:
                from app.services.platform_health_monitor import report_ok
                report_ok("market_data")
            except Exception:
                pass

        # 融合外部数据
        snapshot["external"] = external

        # ★ 2026-08-13 新闻/舆情层（结构性补短板）：注入 XAUUSD 相关公开 RSS 聚合情绪。
        #   "Blank beats wrong"：无新鲜新闻 → news.has_news=False，下游不注入信号。
        #   舆情仅做「提准」（高影响事件下逆舆情方向需更高置信），绝不 blanket 拦截。
        try:
            snapshot["news"] = self._fetch_news_context()
        except Exception as _ne:
            logger.warning(f"[MarketAnalyzer] 新闻层注入跳过: {_ne}")
            snapshot["news"] = {"has_news": False, "error": str(_ne)[:120]}

        # 注入时段质量信息（调研支撑：London-NY overlap = cleanest gold breakouts）
        snapshot["session_info"] = self._get_session_info()

        self._cached_snapshot = snapshot
        self._cache_time = datetime.now()
        return snapshot

    def _get_session_info(self) -> dict:
        """
        当前交易时段质量信息 — 供 AI 决策参考
        调研依据：
        1. ratioxtrade.com: London-NY overlap (13:00-17:00 UTC) = cleanest XAUUSD breakouts
        2. pro-scalper.com: Asian range + London open = highest probability entries
        3. algomatrix.trade: London+NY produces 3-4 clean signals vs Asian noise
        """
        now = datetime.now()
        h = now.hour
        weekday = now.weekday()

        if weekday >= 5:
            return {"name": "周末休市", "quality": "none", "suggestion": "不交易"}

        if 21 <= h or h < 1:    # 21:00-01:00 GMT+8
            session = {"name": "伦敦-纽约重叠", "quality": "excellent", "suggestion": "黄金交易时段，波动大、突破质量最高，适合趋势跟踪"}
        elif 15 <= h < 21:       # 15:00-21:00
            session = {"name": "伦敦单盘", "quality": "good", "suggestion": "欧洲时段，趋势启动期，适合入场"}
        elif 1 <= h < 3:         # 01:00-03:00
            session = {"name": "纽约尾盘", "quality": "fair", "suggestion": "美盘后期，波动递减，注意收盘前波动"}
        elif 3 <= h < 7:         # 03:00-07:00
            session = {"name": "凌晨清淡", "quality": "poor", "suggestion": "流动性低，点差可能扩大，谨慎交易"}
        else:                    # 07:00-15:00
            session = {"name": "亚盘", "quality": "moderate", "suggestion": "亚洲时段，区间震荡为主，适合均值回归策略"}

        session["hour"] = h
        session["weekday"] = weekday
        return session

    # ════════════════════════════════════════════════════════════════
    #  数据获取
    # ════════════════════════════════════════════════════════════════

    def _fetch_raw_market_data(self) -> Optional[dict]:
        """通过 MT5Service 从行情主号 Worker 获取原始 OHLCV"""
        if self._mt5 is None or not self._primary_id:
            logger.warning("[MarketAnalyzer] 未配置行情主号，尝试降级…")
            return None

        try:
            raw = self._mt5.get_market_data(self._primary_id, self.SYMBOL)
            return raw
        except Exception as e:
            logger.error(f"[MarketAnalyzer] 行情数据获取异常: {e}")
            return None

    def _get_current_price(self) -> dict:
        """获取当前 tick 价格（bid/ask/spread），供交易执行器下单时使用"""
        if self._mt5 is None or not self._primary_id:
            logger.warning("[MarketAnalyzer] 未配置行情主号，无法获取实时价格")
            # ★ 2026-08-15 审计P1修复：全 0 报价必须打降级标记（与 mock 快照一致），
            #   下游看到 simulated=True 应拒绝下单，杜绝 0 价成交风险。
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "spread": 0.0,
                    "simulated": True, "reason": "行情主号未配置"}
        try:
            raw = self._mt5.get_market_data(self._primary_id, self.SYMBOL)
            if raw and "current" in raw:
                c = raw.get("current", {})
                return {
                    "bid": float(c.get("bid", 0) or 0),
                    "ask": float(c.get("ask", 0) or 0),
                    "last": float(c.get("last", 0) or 0),
                    "spread": float(c.get("spread", 0) or 0),
                    "simulated": False,
                }
        except Exception as e:
            logger.error(f"[MarketAnalyzer] 获取实时价格失败: {e}")
        # ★ 2026-08-15 审计P1修复：异常/无数据同样打降级标记
        return {"bid": 0.0, "ask": 0.0, "last": 0.0, "spread": 0.0,
                "simulated": True, "reason": "行情主号无数据或异常"}

    def _fetch_external_data(self) -> dict:
        """获取 DXY/VIX/相关性

        ★ 2026-08-13 透明化修复：DXY/VIX 是**日级滞后**数据（VIX=CBOE 昨日收盘 CSV、
        DXY=前一日 ECB 汇率代理），并非日内实时。原代码直接喂给云模型 prompt 且无任何
        标注，模型会把"昨天"当成"现在"做宏观判断。此处显式注入 data_lag_note +
        granularity，让 DeepSeek/混元在看到 external 段时知道这是滞后数据、不可当即时信号。
        """
        try:
            ext = market_data_provider.get_external_snapshot()
        except Exception as e:
            logger.warning(f"[MarketAnalyzer] 外部数据获取失败: {e}")
            ext = {"error": str(e), "dxy": None, "vix": None, "correlation": None}
        if isinstance(ext, dict):
            ext.setdefault(
                "data_lag_note",
                "DXY/VIX 为日级滞后数据（VIX=CBOE 昨日收盘、DXY=前一日美元指数），"
                "非日内实时；相关性样本不足时取长期统计常态 -0.65。请勿当作即时宏观信号使用。",
            )
            ext.setdefault("granularity", "daily")
        return ext

    def _fetch_news_context(self) -> dict:
        """获取 XAUUSD 新闻/舆情上下文（2026-08-13 新增）

        调 news_service 聚合公开 RSS（多源）→ 词典情绪分 → gold_sentiment_score[-1,1]
        + 高影响事件标记。无新鲜新闻时返回 has_news=False（不注入信号，避免编造）。
        """
        try:
            from app.services.news_service import get_news_context
            return get_news_context()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MarketAnalyzer] 新闻上下文获取失败: {e}")
            return {"has_news": False, "error": str(e)[:120]}

    # ════════════════════════════════════════════════════════════════
    #  从原始 Worker 数据构建指标
    # ════════════════════════════════════════════════════════════════

    def _build_from_raw(self, raw: dict) -> dict:
        """将 Worker 返回的原始 OHLCV 数据转为完整技术指标快照"""
        current = raw.get("current", {})
        tfs_raw = raw.get("timeframes", {})

        snapshot = {
            "timestamp": raw.get("timestamp", datetime.now().isoformat()),
            "symbol": raw.get("symbol", self.SYMBOL),
            "current_price": {
                "bid": current.get("bid", 0),
                "ask": current.get("ask", 0),
                "last": current.get("last", 0),
            },
            "spread": current.get("spread", 0),
            "timeframes": {},
            "volatility_metrics": self._calc_volatility_metrics_from_raw(tfs_raw),
            "key_levels": self._find_key_levels_from_raw(tfs_raw, current),
        }

        # 逐时间框架计算指标
        for tf_name, tf_data in tfs_raw.items():
            bars = tf_data.get("bars", [])
            if len(bars) < 20:
                snapshot["timeframes"][tf_name] = {"error": "数据不足"}
                continue

            closes = [b.get("close", 0) for b in bars]
            highs = [b.get("high", 0) for b in bars]
            lows = [b.get("low", 0) for b in bars]
            volumes = [b.get("volume", 0) for b in bars]

            snapshot["timeframes"][tf_name] = {
                "bars": len(bars),
                # ★ v4 Meta 质量陪审团/Chronos 需要真实收盘价序列（≥32 根）。
                # 此处把已算好的 closes 一并存入快照；该键位于 timeframes 内，
                # 会被 _compress_for_prompt 裁掉，不会膨胀发给 DeepSeek 的 prompt。
                "closes": closes,
                "latest": {
                    "open": bars[-1].get("open", 0),
                    "high": bars[-1].get("high", 0),
                    "low": bars[-1].get("low", 0),
                    "close": bars[-1].get("close", 0),
                    "volume": bars[-1].get("volume", 0),
                },
                "ma": {
                    "MA20": self._sma(closes, 20),
                    "MA50": self._sma(closes, 50),
                    "MA200": self._sma(closes, 200) if len(closes) >= 200 else None,
                },
                "rsi": self._rsi(closes, 14),
                "macd": self._macd(closes),
                "bollinger": self._bollinger_bands(closes, 20, 2),
                "atr": self._atr(highs, lows, closes, 14),
                "trend": self._trend_strength(closes),
                "volume_ratio": self._volume_ratio(volumes),
            }

        # ★ 2026-08-05 大脑换血：注入 SMC 机构足迹 + 市场体制（根治山顶开BUY）
        # 调研支撑：HuggingFace XAUUSD-ML-Trader(SMC最盈利) / Informatica期刊(确定性SMC原语)
        #          / Tradewink+RegimeRisk(HMM+ER三层体制, 趋势末端硬约束)
        # SMC 与 Regime 均基于行情主号共享数据，全局有效 → 天然多账号优先（N账号无关）
        try:
            from app.services.smc_features import compute_smc, derive_structure_anchors
            from app.services.regime_detect import detect_regime, detect_structure_break
            snapshot["smc_features"] = compute_smc(tfs_raw)
            _cp = float(current.get("last") or current.get("bid") or 0)
            snapshot["regime"] = detect_regime(tfs_raw, _cp)
            # ★ 2026-08-17 结构突破事件（BOS/CHoCH，SMC/ICT 趋势启动识别）：
            #   纯信息加法注入决策链（提准非拦截）——AI 读到"趋势延续突破/反转预警"，
            #   顺势方向有结构背书，逆结构方向需自行辩护。调研≥3源交叉验证后落地。
            try:
                snapshot["structure_break"] = detect_structure_break(tfs_raw)
                _sb = snapshot["structure_break"]
                if _sb.get("advice_zh"):
                    logger.info(f"[MarketAnalyzer] 结构突破: {_sb['advice_zh']} "
                                f"(htf_aligned={_sb.get('htf_aligned')})")
            except Exception as _sbe:
                logger.warning(f"[MarketAnalyzer] 结构突破检测失败(降级): {_sbe}")
                snapshot["structure_break"] = {"m15": None, "m5": None,
                                               "htf_aligned": False, "advice_zh": None}
            # ★ 2026-08-13 结构锚点：把结构失效位/目标位推出来存进 snapshot，
            #   供 trade_executor 开仓时把 SL/TP 锚定到结构位（ATR 兜底）。
            snapshot["structure_anchors"] = derive_structure_anchors(tfs_raw, _cp)
            # ★ 2026-08-06 三周期趋势写回：把 regime 的三周期方向写回各周期 trend_dir，
            #   供 AI 统一判读（4H=偏置, 15m=结构, 5m=入场）
            _rg = snapshot["regime"]
            _trend_map = {
                "H4": _rg.get("direction_bias", "neutral"),
                "M15": _rg.get("structure_dir_15m", "neutral"),
                "M5": _rg.get("entry_dir_5m", "neutral"),
            }
            for _tf, _dir in _trend_map.items():
                if _tf in snapshot.get("timeframes", {}):
                    snapshot["timeframes"][_tf]["trend_dir"] = _dir
            _smc = snapshot["smc_features"].get("global_bias", "neutral")
            _rg_label = _rg.get("label_zh", "未知")
            _ext = _rg.get("extension_z", 0)
            _top = " [⚠趋势末端-山顶风险]" if _rg.get("at_stale_top") else ""
            _bias = f" 4H偏置={_rg.get('direction_bias','?')}"
            logger.info(f"[MarketAnalyzer] SMC全局偏向={_smc} | 体制={_rg_label}{_bias} | 延伸Z={_ext}{_top}")
        except Exception as _e:
            logger.warning(f"[MarketAnalyzer] SMC/Regime 注入失败(降级传统指标): {_e}")
            snapshot["smc_features"] = {"per_tf": {}, "global_bias": "neutral", "tf_count": 0}
            snapshot["structure_anchors"] = {"available": False}
            snapshot["regime"] = {"regime": "unknown", "label_zh": "计算失败",
                                  "direction_bias": "neutral",
                                  "structure_dir_15m": "neutral", "entry_dir_5m": "neutral"}

        # ★ 2026-08-06 补强：原始价格结构（最近 N 根 K 线实体/影线/连续同向/摆动高低点趋势）
        #   对应你列的「让 AI 自己看高低点、收盘价序列、低点是否下移」。蒸馏指标只给结论，
        #   这里给证据，AI 既能看 SMC/Regime 结论也能自己读原始结构，避免被错误蒸馏误导。
        try:
            snapshot["price_structure"] = self._build_price_structure(tfs_raw)
        except Exception as _e:
            logger.warning(f"[MarketAnalyzer] 价格结构注入失败: {_e}")
            snapshot["price_structure"] = {}

        # ★ 2026-08-06 补强②：订单流 / CVD（买盘枯竭 / 卖压放大信号）
        #   双源：Binance 永续(用户指定) 可达时用，不可达降级；MT5 本地分笔 CVD 代理常驻，
        #   保证 AI 永远有订单流信号（本沙箱外网受限时仍真实运作）。
        try:
            from app.services.orderflow_cvd import get_orderflow_snapshot
            snapshot["orderflow"] = get_orderflow_snapshot(tfs_raw)
            _of = snapshot["orderflow"]
            _cache_orderflow_status(_of)
            if _of.get("available"):
                logger.info(
                    f"[MarketAnalyzer] 订单流CVD 源={_of.get('source')} 读={_of.get('reading')} "
                    f"买枯={_of.get('buy_pressure_dry')} 卖压={_of.get('sell_pressure_high')}"
                )
        except Exception as _e:
            logger.warning(f"[MarketAnalyzer] 订单流CVD注入失败(降级): {_e}")
            snapshot["orderflow"] = {"available": False}

        # ★ 2026-08-06 补强⑥：执行质量滑点遥测（经纪商执行质量，零外部依赖）
        try:
            from app.services.execution_telemetry import get_telemetry
            snapshot["execution"] = get_telemetry().summary()
        except Exception as _e:
            logger.warning(f"[MarketAnalyzer] 执行滑点遥测注入失败(降级): {_e}")
            snapshot["execution"] = {"available": False}

        # ★★ 2026-08-16 审计P1-1修复：空数据 fail-open——
        #   Worker 在 copy_rates 拉空时仍返回 ok=True + 全空 timeframes（mt5_worker L548-553），
        #   此分支 raw 无 error → 走 _build_from_raw 且不打 simulated → 下游 debate_engine 的
        #   simulated 拦截不触发 → AI 拿空数据辩论可能开仓（冷启动 90s 窗口/瞬时失败）。
        #   修法：全部 TF 数据不足时显式打 simulated=True + data_quality="stale"，与
        #   get_market_snapshot 的模拟快照同构（下游 debate_engine 据此 HOLD 禁开仓）。
        _tf_err_count = 0
        _tf_count = len(snapshot.get("timeframes") or {})
        for _tf_name, _tf_val in (snapshot.get("timeframes") or {}).items():
            if isinstance(_tf_val, dict) and _tf_val.get("error"):
                _tf_err_count += 1
        if _tf_count > 0 and _tf_err_count >= _tf_count:
            snapshot["simulated"] = True
            snapshot["data_quality"] = "stale"
            logger.warning(
                f"[MarketAnalyzer] 全部 {_tf_count} 个周期数据不足 → 标记 simulated(stale)，"
                f"下游将禁止实盘开仓"
            )

        return snapshot

    def _calc_volatility_metrics_from_raw(self, tfs_raw: dict) -> dict:
        """从原始数据计算波动率指标"""
        h1_data = tfs_raw.get("H1", {})
        d1_data = tfs_raw.get("D1", {})

        h1_bars = h1_data.get("bars", [])
        d1_bars = d1_data.get("bars", [])

        if len(h1_bars) < 20 or len(d1_bars) < 20:
            return {"error": "波动率数据不足", "atr_14": 0, "volatility_regime": "未知"}

        h1_highs = [b.get("high", 0) for b in h1_bars]
        h1_lows = [b.get("low", 0) for b in h1_bars]
        h1_closes = [b.get("close", 0) for b in h1_bars]
        d1_highs = [b.get("high", 0) for b in d1_bars]
        d1_lows = [b.get("low", 0) for b in d1_bars]
        d1_closes = [b.get("close", 0) for b in d1_bars]

        # 每日真实波幅
        d1_tr = []
        for i in range(1, len(d1_bars)):
            tr = max(
                d1_highs[i] - d1_lows[i],
                abs(d1_highs[i] - d1_closes[i - 1]),
                abs(d1_lows[i] - d1_closes[i - 1]),
            )
            d1_tr.append(tr)

        h1_atr = self._atr(h1_highs, h1_lows, h1_closes, 14)
        d1_atr = self._atr(d1_highs, d1_lows, d1_closes, 14)

        if d1_tr:
            avg_atr_20 = sum(d1_tr[-20:]) / min(20, len(d1_tr))
        else:
            avg_atr_20 = d1_atr or 0

        # 波动率分区
        regime = "正常"
        if d1_atr and avg_atr_20:
            ratio = d1_atr / avg_atr_20
            if ratio > 1.5:
                regime = "极端"
            elif ratio > 1.2:
                regime = "高波动"
            elif ratio < 0.7:
                regime = "低波动"

        # 布林带宽度（H1 基准）
        bb_width = 0
        if len(h1_closes) >= 20:
            bb = self._bollinger_bands(h1_closes, 20, 2)
            bb_width = bb.get("width", 0)

        # ★ 2026-08-10 趋势强弱指标：ADX(14) H1（喂给智能手数引擎做趋势自适应加/减码）
        try:
            from app.services.indicators import compute_indicators
            _adx = (compute_indicators(h1_bars) or {}).get("adx")
        except Exception:
            _adx = None

        return {
            "h1_atr": round(h1_atr, 2) if h1_atr else 0,
            "d1_atr": round(d1_atr, 2) if d1_atr else 0,
            "h1_adx": round(_adx, 1) if _adx else 0,
            "avg_d1_atr_20": round(avg_atr_20, 2),
            "volatility_regime": regime,
            "atr_ratio": round(d1_atr / avg_atr_20, 2) if d1_atr and avg_atr_20 else 1.0,
            "bollinger_width": round(bb_width, 2),
            "daily_range_pct": round(d1_atr / d1_closes[-1] * 100, 2) if d1_atr and d1_closes else 0,
        }

    def _find_key_levels_from_raw(self, tfs_raw: dict, current: dict) -> dict:
        """从原始数据找关键价位"""
        d1_data = tfs_raw.get("D1", {})
        d1_bars = d1_data.get("bars", [])

        if len(d1_bars) < 20:
            return {"error": "数据不足"}

        d1_highs = [b.get("high", 0) for b in d1_bars]
        d1_lows = [b.get("low", 0) for b in d1_bars]
        current_price = current.get("bid", 0)

        recent_high = max(d1_highs[-20:])
        recent_low = min(d1_lows[-20:])
        all_time_high = max(d1_highs)
        all_time_low = min(d1_lows)

        pivot = (recent_high + recent_low + (current_price or recent_low)) / 3

        levels = {
            "current_price": current_price,
            "recent_high_20d": recent_high,
            "recent_low_20d": recent_low,
            "all_time_high_60d": all_time_high,
            "all_time_low_60d": all_time_low,
            "pivot": round(pivot, 2),
            "resistance": [recent_high, all_time_high],
            "support": [recent_low, all_time_low],
        }

        if current_price:
            levels["distance_to_support"] = round(current_price - recent_low, 2)
            levels["distance_to_resistance"] = round(recent_high - current_price, 2)

        return levels

    # ════════════════════════════════════════════════════════════════
    #  模拟数据（MT5 不可用时降级）
    # ════════════════════════════════════════════════════════════════

    def _get_mock_snapshot(self) -> dict:
        """模拟市场数据 — 所有数据源都不可用时的降级方案"""
        import random
        base = 2650.0 + random.uniform(-15, 15)
        regime = random.choice(["正常", "低波动", "高波动"])
        return {
            "timestamp": datetime.now().isoformat(),
            "symbol": self.SYMBOL,
            "current_price": {"bid": round(base, 2), "ask": round(base + 0.3, 2), "last": round(base, 2)},
            "spread": 0.3,
            "timeframes": {
                "M5": {"trend": "up", "rsi": round(random.uniform(55, 68), 1), "macd": "bullish", "ema20": round(base - 2, 2), "ema50": round(base - 8, 2)},
                "M15": {"trend": "up", "rsi": round(random.uniform(58, 65), 1), "macd": "bullish", "ema20": round(base - 3, 2), "ema50": round(base - 10, 2)},
                "H1": {"trend": random.choice(["up", "sideways"]), "rsi": round(random.uniform(52, 62), 1), "macd": random.choice(["bullish", "neutral"]), "ema20": round(base - 5, 2), "ema50": round(base - 12, 2)},
                "H4": {"trend": "up", "rsi": round(random.uniform(60, 72), 1), "macd": "bullish", "ema20": round(base - 8, 2), "ema50": round(base - 20, 2)},
                "D1": {"trend": "up", "rsi": round(random.uniform(58, 70), 1), "macd": "bullish", "ema20": round(base - 15, 2), "ema50": round(base - 30, 2)},
            },
            "volatility_metrics": {
                "atr_14": round(random.uniform(15, 22), 2),
                "bollinger_width": round(random.uniform(1.8, 2.8), 2),
                "daily_range_pct": round(random.uniform(0.6, 1.2), 2),
                "regime": regime,
                "volatility_regime": regime,
            },
            "key_levels": {
                "resistance": [round(base + 20, 0), round(base + 35, 0), round(base + 50, 0)],
                "support": [round(base - 20, 0), round(base - 35, 0), round(base - 50, 0)],
                "pivot": round(base, 0),
            },
        }

    # ════════════════════════════════════════════════════════════════
    #  技术指标计算（与旧版完全一致）
    # ════════════════════════════════════════════════════════════════

    def _sma(self, data: list, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return round(sum(data[-period:]) / period, 2)

    def _rsi(self, closes: list, period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        window = closes[-(period + 1):]
        gains, losses = [], []
        for i in range(1, len(window)):
            diff = window[i] - window[i - 1]
            gains.append(diff if diff > 0 else 0)
            losses.append(abs(diff) if diff < 0 else 0)
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)

    def _macd(self, closes: list) -> dict:
        if len(closes) < 26:
            return {}
        # ★ 2026-08-15 审计P2修复：原 `signal=None, histogram=macd_line` 是假 MACD——
        #   金叉死叉/动能类判断全部失效。改为先算 macd 序列，再对其做 EMA9 得真 signal，
        #   histogram = macd - signal（closes 通常 ≤100 根，O(n²) 成本可忽略）。
        macd_series = []
        for i in range(26, len(closes) + 1):
            seg = closes[:i]
            e12 = self._ema(seg, 12)
            e26 = self._ema(seg, 26)
            if e12 is not None and e26 is not None:
                macd_series.append(e12 - e26)
        if len(macd_series) < 9:
            return {}
        macd_line = round(macd_series[-1], 4)
        k = 2.0 / (9 + 1)
        ema9 = macd_series[0]
        for m in macd_series[1:]:
            ema9 = m * k + ema9 * (1 - k)
        signal = round(ema9, 4)
        return {"macd": macd_line, "signal": signal, "histogram": round(macd_line - signal, 4)}

    def _ema(self, data: list, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return round(ema, 4)

    def _bollinger_bands(self, closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
        if len(closes) < period:
            return {}
        sma = self._sma(closes, period)
        if sma is None:
            return {}
        recent = closes[-period:]
        variance = sum((x - sma) ** 2 for x in recent) / period
        std = variance ** 0.5
        return {
            "upper": round(sma + std_dev * std, 2),
            "middle": round(sma, 2),
            "lower": round(sma - std_dev * std, 2),
            "width": round((2 * std_dev * std) / sma * 100, 2) if sma else 0,
            "position": round((closes[-1] - sma) / (std_dev * std) * 100, 1) if std > 0 else 0,
        }

    def _atr(self, highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
        return self._calc_single_atr(highs, lows, closes, period)

    def _calc_single_atr(self, highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        tr_values = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_values.append(tr)
        if not tr_values:
            return None
        atr = sum(tr_values[:period]) / period
        for i in range(period, len(tr_values)):
            atr = (atr * (period - 1) + tr_values[i]) / period
        return round(atr, 2)

    def _build_price_structure(self, tfs_raw: dict, tfs=("H4", "H1", "M15"), n: int = 12) -> dict:
        """★ 2026-08-06 补强：最近 N 根原始 K 线结构喂给 AI（高低点/收盘价序列/连续同向/
        摆动高低点趋势）。蒸馏指标只给结论，这里给证据，多账号无关（行情主号共享数据）。"""
        out = {}
        for tf in tfs:
            bars = (tfs_raw.get(tf, {}) or {}).get("bars", [])
            if len(bars) < n:
                continue
            recent = bars[-n:]
            seq = []
            for b in recent:
                o = float(b.get("open", 0)); h = float(b.get("high", 0))
                l = float(b.get("low", 0)); c = float(b.get("close", 0))
                rng = (h - l) if (h - l) != 0 else 1e-9
                bull = c >= o
                seq.append({
                    "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
                    "bull": bull,
                    "body%": round((c - o) / rng * 100, 1),
                    "up_sh%": round((h - max(o, c)) / rng * 100, 1),
                    "dn_sh%": round((min(o, c) - l) / rng * 100, 1),
                })
            bull_streak = 0; bear_streak = 0
            for b in reversed(seq):
                if b["bull"]: bull_streak += 1
                else: break
            for b in reversed(seq):
                if not b["bull"]: bear_streak += 1
                else: break
            lows = [b["l"] for b in seq]
            highs = [b["h"] for b in seq]
            swing_low = "上升(低点抬升)" if lows[-1] > lows[0] else ("下降(低点下移)" if lows[-1] < lows[0] else "持平")
            swing_high = "上升" if highs[-1] > highs[0] else ("下降" if highs[-1] < highs[0] else "持平")
            out[tf] = {
                "bars": seq,
                "bull_streak": bull_streak,
                "bear_streak": bear_streak,
                "swing_low_trend": swing_low,
                "swing_high_trend": swing_high,
                "last_close": round(seq[-1]["c"], 2),
            }
        return out

    def _trend_strength(self, closes: list) -> str:
        """★ 2026-08-06 修正：改用价格行为(近10根方向) + 近端/远端斜率，
        根除滞后 SMA20/50 在趋势反转初期误报 strong_uptrend 的顽疾。
        旧逻辑：下跌初期 SMA20 仍 > SMA50 → 误报 strong_uptrend → BUY 获体制背书狂开亏损单。
        现逻辑：近10根方向定调(up/down/neutral) + 近5根斜率定 strong，多周期均适用、灵敏。
        """
        if len(closes) < 20:
            return "unknown"
        recent = closes[-10:]
        ups = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        downs = len(recent) - 1 - ups
        slope5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] != 0 else 0
        slope20 = (closes[-1] - closes[-20]) / closes[-20] * 100 if closes[-20] != 0 else 0
        dir_up = ups >= downs + 2
        dir_down = downs >= ups + 2
        if dir_up and slope5 > 0.15:
            return "strong_uptrend" if slope20 > 1 else "uptrend"
        if dir_up:
            return "uptrend"
        if dir_down and slope5 < -0.15:
            return "strong_downtrend" if slope20 < -1 else "downtrend"
        if dir_down:
            return "downtrend"
        return "ranging"

    def _volume_ratio(self, volumes: list) -> float:
        if len(volumes) < 10:
            return 1.0
        recent_avg = sum(volumes[-5:]) / 5
        longer_avg = sum(volumes[-10:]) / 10
        if longer_avg == 0:
            return 1.0
        return round(recent_avg / longer_avg, 2)
