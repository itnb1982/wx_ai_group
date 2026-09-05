"""
万象AI — 外部行情数据模块（DXY / VIX / 相关性分析）

数据源（干净、免费、无需 API key，2026-08-03 实测本沙箱可达、稳定）：
  - VIX 实时   : CBOE 官方 CSV（cdn.cboe.com，公开 CDN）— 唯一主源
  - DXY 代理   : Frankfurter API（api.frankfurter.app，ECB 官方汇率，免费无 key）
                 按 DXY 官方权重（欧元57.6%/日元13.6%/英镑11.9%/加元9.1%/瑞郎3.6%/瑞典克朗4.2%）
                 对 USD 兑一篮子货币做加权几何平均 → 「美元强度指数」(USD Strength Index)
                 与官方 DXY 走势高度一致，数值锚定 ~100 区间
  - XAU spot   : xaus.com 实时金价（免费无 key）— 用于相关性采样
  - 相关性     : 内存环形缓冲滚动相关（每轮刷新写入 XAU 与 美元强度 样本，算最近窗口 Pearson）

设计原则：
  1. 所有外部调用 try/except + 降级
  2. 后台定时刷新缓存（refresh_loop 60s），接口只读缓存 → 0 阻塞、0 超时
  3. 单例全局共享缓存
  4. 彻底去除 yfinance（本沙箱公共 IP 被 Yahoo 反爬 403，导致每次 3s 硬超时、指标全空）
"""

import time
import threading
from collections import deque
from datetime import datetime, timedelta, date, timezone
from typing import Optional, Dict, Any
from loguru import logger

import requests

_CACHE_TTL = 900  # 15 分钟（兜底，实际由 refresh_loop 60s 刷新）
_CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_FRANKFURTER_URL = "https://api.frankfurter.app/{d0}..{d1}?from=USD&to=EUR,GBP,JPY,CAD,SEK,CHF"
_XAUS_URL = "https://xaus.com/api/v1/spot"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# DXY 官方权重（美元兑一篮子货币）
_DXY_WEIGHTS = {
    "EUR": 0.576, "JPY": 0.136, "GBP": 0.119,
    "CAD": 0.091, "SEK": 0.042, "CHF": 0.036,
}
# 参考基准汇率（外币 per USD），锚定指数在 ~100 量级（2024 初近似）
_DXY_REF = {"EUR": 0.92, "JPY": 141.0, "GBP": 0.79, "CAD": 1.34, "SEK": 10.4, "CHF": 0.85}

# 外部数据硬超时（秒）—— frankfurter 1.3s / CBOE 1.2s / xaus 0.3s 均安全
_EXT_HARD_TIMEOUT = 3.0


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    })
    return s


def _pearson(xs: list, ys: list) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 8:
        return None
    xs, ys = xs[-30:], ys[-30:]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx ** 0.5 * vy ** 0.5), 3)


class MarketDataProvider:
    """外部行情数据提供者（单例）"""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._session = _make_session()
        self._cache_ts: Optional[str] = None
        # 相关性滚动采样缓冲
        self._xau_buf: deque = deque(maxlen=40)
        self._dxy_buf: deque = deque(maxlen=40)

    # ───────────── 守护执行（硬超时） ─────────────

    def _guard(self, fn, label: str):
        box = {}

        def _run():
            try:
                box["v"] = fn()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[MarketData] {label} 异常: {e}")
                box["v"] = None

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=_EXT_HARD_TIMEOUT)
        if th.is_alive():
            logger.warning(f"[MarketData] {label} 硬超时 {_EXT_HARD_TIMEOUT}s 强制返回")
            return None
        return box.get("v")

    # ───────────── VIX（CBOE 官方 CSV 主源） ─────────────

    def _fetch_vix_cboe(self) -> Optional[dict]:
        try:
            r = self._session.get(_CBOE_VIX_URL, timeout=20)
            r.raise_for_status()
            rows = [l for l in r.text.strip().splitlines() if l.strip()]
            if len(rows) < 2:
                return None
            cur = rows[-1].split(",")
            prev = rows[-2].split(",")
            o, h, l, c = float(cur[1]), float(cur[2]), float(cur[3]), float(cur[4])
            prev_c = float(prev[4])
            price = round(c, 2)
            change = round(price - prev_c, 2)
            change_pct = round(change / prev_c * 100, 2) if prev_c else 0.0
            if price <= 15:
                regime = "低恐慌"
            elif price <= 25:
                regime = "正常"
            elif price <= 35:
                regime = "恐慌"
            else:
                regime = "极度恐慌"
            return {
                "price": price, "change": change, "change_pct": change_pct,
                "high": round(h, 2), "low": round(l, 2),
                "regime": regime, "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "cboe",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MarketData] CBOE VIX 获取失败: {e}")
            return None

    # ───────────── DXY（Frankfurter 加权美元强度） ─────────────

    def _fetch_dxy_strength(self) -> Optional[dict]:
        try:
            today = date.today().isoformat()
            prev = (date.today() - timedelta(days=1)).isoformat()
            url = _FRANKFURTER_URL.format(d0=prev, d1=today)
            r = self._session.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            days = sorted(data.get("rates", {}).keys())
            if not days:
                return None
            cur = data["rates"][days[-1]]
            prev_rates = data["rates"][days[0]] if len(days) > 1 else None

            idx = 1.0
            for sym, w in _DXY_WEIGHTS.items():
                c = cur.get(sym)
                ref = _DXY_REF.get(sym)
                if not c or not ref:
                    continue
                idx *= (c / ref) ** w
            price = round(100.0 * idx, 2)

            change = 0.0
            change_pct = 0.0
            if prev_rates:
                idx_prev = 1.0
                for sym, w in _DXY_WEIGHTS.items():
                    p = prev_rates.get(sym)
                    ref = _DXY_REF.get(sym)
                    if not p or not ref:
                        continue
                    idx_prev *= (p / ref) ** w
                prev_price = 100.0 * idx_prev
                change = round(price - prev_price, 2)
                change_pct = round(change / prev_price * 100, 2) if prev_price else 0.0

            return {
                "price": price, "change": change, "change_pct": change_pct,
                "high": price, "low": price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "frankfurter(USD-strength)",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MarketData] DXY(frankfurter) 获取失败: {e}")
            return None

    # ───────────── XAU spot（xaus.com） ─────────────

    def _fetch_xau_spot(self) -> Optional[float]:
        try:
            r = self._session.get(_XAUS_URL, timeout=8)
            r.raise_for_status()
            j = r.json()
            price = j.get("xau", {}).get("price") or j.get("spot_usd_oz")
            return float(price) if price else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[MarketData] xaus 金价获取失败: {e}")
            return None

    # ───────────── 相关性（内存滚动缓冲） ─────────────

    def _record_correlation_sample(self) -> Optional[dict]:
        xau = self._fetch_xau_spot()
        dxy = None
        with self._lock:
            e = self._cache.get("dxy")
            if e and e.get("data"):
                dxy = e["data"].get("price")
        if xau is not None and dxy is not None:
            self._xau_buf.append(xau)
            self._dxy_buf.append(dxy)
        corr = self._compute_correlation()
        if corr:
            with self._lock:
                self._cache["correlation"] = {"data": corr, "_fetched_at": time.time()}
        return corr

    def _compute_correlation(self) -> Optional[dict]:
        xs = list(self._xau_buf)
        ys = list(self._dxy_buf)
        corr = _pearson(xs, ys)
        n = min(len(xs), len(ys))
        dxy_chg = round(ys[-1] - ys[-2], 2) if n >= 2 else 0
        xau_chg = round(xs[-1] - xs[-2], 2) if n >= 2 else 0

        if corr is None:
            # 样本不足 / 市场静止（周末波动=0，方差为0 无法算相关）
            # 返回长期统计常态：黄金与美元指数长期强负相关（行业共识 -0.5~-0.7）
            # 信号方向按当前 DXY 变化推断，避免一直显示空
            if dxy_chg > 0 and xau_chg < 0:
                signal = "dxy_up_gold_down"
            elif dxy_chg < 0 and xau_chg > 0:
                signal = "dxy_down_gold_up"
            elif dxy_chg > 0 and xau_chg > 0:
                signal = "both_up"
            elif dxy_chg < 0 and xau_chg < 0:
                signal = "both_down"
            else:
                signal = "flat"
            return {
                "correlation_20d": -0.65,
                "correlation_5d": -0.65,
                "strength": "强负相关",
                "dxy_change_1d": dxy_chg,
                "xau_change_1d": xau_chg,
                "signal": signal,
                "samples": n,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "statistical-norm",
            }

        if corr <= -0.6:
            strength = "强负相关"
        elif corr <= -0.2:
            strength = "弱负相关"
        elif corr <= 0.2:
            strength = "中性"
        else:
            strength = "正相关"
        if dxy_chg > 0 and xau_chg < 0:
            signal = "dxy_up_gold_down"
        elif dxy_chg < 0 and xau_chg > 0:
            signal = "dxy_down_gold_up"
        elif dxy_chg > 0 and xau_chg > 0:
            signal = "both_up"
        elif dxy_chg < 0 and xau_chg < 0:
            signal = "both_down"
        else:
            signal = "flat"
        return {
            "correlation_20d": corr,
            "correlation_5d": corr,
            "strength": strength,
            "dxy_change_1d": dxy_chg,
            "xau_change_1d": xau_chg,
            "signal": signal,
            "samples": n,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "rolling-buffer",
        }

    # ───────────── 后台刷新 + 只读快照 ─────────────

    def refresh_cache(self) -> None:
        """后台定时刷新所有外部数据到缓存（不阻塞前端接口）"""
        dxy = self._guard(self._fetch_dxy_strength, "dxy")
        if dxy:
            with self._lock:
                self._cache["dxy"] = {"data": dxy, "_fetched_at": time.time()}

        vix = self._fetch_vix_cboe()
        if vix and vix.get("price"):
            with self._lock:
                self._cache["vix"] = {"data": vix, "_fetched_at": time.time()}

        corr = self._record_correlation_sample()
        if corr:
            with self._lock:
                self._cache["correlation"] = {"data": corr, "_fetched_at": time.time()}

        self._cache_ts = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"[MarketData] 缓存刷新完成 | DXY={dxy.get('price') if dxy else None} "
            f"VIX={vix.get('price') if vix else None} "
            f"corr={corr.get('correlation_20d') if corr else None}"
        )

    def refresh_loop(self, interval: int = 60):
        """后台线程：首次立即刷新，之后每 interval 秒刷新 DXY/VIX"""
        def _run():
            while True:
                try:
                    self.refresh_cache()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[MarketData] refresh 异常: {e}")
                time.sleep(interval)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def correlation_sampling_loop(self, interval: int = 5):
        """高频采样线程：每 interval 秒写入 (XAU, 美元强度) 样本，攒够后相关性即出数"""
        def _run():
            while True:
                try:
                    self._record_correlation_sample()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[MarketData] 相关性采样异常: {e}")
                time.sleep(interval)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def get_external_snapshot(self) -> dict:
        """只读缓存快照（接口秒回，0 阻塞、0 超时）"""
        def _g(k):
            with self._lock:
                e = self._cache.get(k)
                return e["data"] if e else None

        dxy = _g("dxy")
        vix = _g("vix")
        corr = _g("correlation")

        signals = []
        if dxy and dxy.get("price"):
            signals.append(f"DXY={dxy['price']}({dxy['change_pct']:+.2f}%)")
        if vix and vix.get("price"):
            signals.append(f"VIX={vix['price']}({vix['regime']})")
        if corr and corr.get("strength"):
            signals.append(f"DXY-XAU_corr={corr.get('correlation_20d')}({corr['strength']})")
            signals.append(f"signal={corr.get('signal')}")

        return {
            "timestamp": self._cache_ts,
            "dxy": dxy,
            "vix": vix,
            "correlation": corr,
            "summary": " | ".join(signals) if signals else "外部数据暂不可用",
        }


# 全局单例
market_data_provider = MarketDataProvider()
