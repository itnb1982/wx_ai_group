"""跨资产协变量获取（Chronos-2 多变量预测的 past_covariates）。

★ 目标：把 DXY（美元指数代理）/ US10Y（美债10年收益率）/ VIX（恐慌指数）
   作为外生协变量喂给 Chronos-2，利用它们与 XAUUSD 的强相关性提升预测精度——
   这正是 Chronos-2 相对 Bolt 的核心 edge（Bolt 纯单变量、吃不到跨资产）。

★ 数据源（2026-08-07 实测本沙箱可达、稳定、免费、无 API key）：
   - DXY 代理  : Frankfurter API（api.frankfurter.app，ECB 官方汇率，按 DXY 官方权重
                 对 USD 兑一篮子货币做加权几何平均 → 美元强度指数，锚定 ~100 区间）。
   - VIX       : CBOE 官方日线 CSV（cdn.cboe.com，公开 CDN，9245 行日线历史）。
   - US10Y     : 美国财政部官方日线收益率曲线 CSV（home.treasury.gov，10 Yr 列，无 key）。
   （旧 stooq 源经实测全 404，已废弃；Yahoo Finance 本沙箱被反爬 403，不可用。）

★ 对齐方式（日线 → M15 intraday 阶梯重采样）：
   外部协变量是日线频，XAU 上下文是 M15 频。把最近 need_days 个日值各自
   重复铺满其对应 intraday 段（每个交易日宏观 regime 日内恒定，符合经济学直觉），
   既保留日频趋势形状，又正确对齐到 target_len。这是低频频外生回归量的标准做法。

★ 获取策略（纯旁路、绝不阻塞交易，提准非拦截）：
   1. 优先：从 market_data 已订阅的跨资产 M15 closes 直接取（最准、零延迟）；
   2. 回退：上述三个日线源（TTL 缓存 + 磁盘兜底）；
   3. 全部缺失返回 None → Chronos-2 自动单变量降级。

★ 与账号数 N 解耦：输入 market_data（行情主号共享数据），纯函数。
"""
import os
import time
import logging
from datetime import date, timedelta

logger = logging.getLogger("covariates")

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "models", "cov_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 与 XAUUSD 的理论相关性（仅用于 notes，不影响方向）
CORR = {"DXY": -0.8, "US10Y": -0.7, "VIX": 0.6}

# DXY 官方权重（美元兑一篮子货币）与参考基准汇率（锚定指数 ~100 量级）
_DXY_WEIGHTS = {"EUR": 0.576, "JPY": 0.136, "GBP": 0.119,
                "CAD": 0.091, "SEK": 0.042, "CHF": 0.036}
_DXY_REF = {"EUR": 0.92, "JPY": 141.0, "GBP": 0.79, "CAD": 1.34, "SEK": 10.4, "CHF": 0.85}

_CBOE_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_FRANKFURTER_URL = "https://api.frankfurter.app/{d0}..{d1}?from=USD&to=EUR,GBP,JPY,CAD,SEK,CHF"
_TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                 "daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve"
                 "&field_tdr_date_value=2026&page&_format=csv")

_SERIES_TTL = 21600  # 6 小时刷新一次日线序列缓存
_BARS_PER_DAY = 96   # XAU M15 每日约 96 根（23h 交易）

# name -> {"vals": [...], "updated": t}
_series_cache: dict = {}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url: str, timeout: int = 25):
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Accept": "*/*", "Connection": "keep-alive"})
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


# ───────────── 日线序列获取（三个已验证可用源） ─────────────

def _fetch_dxy_series(days: int = 400):
    """Frankfurter 日线范围 → 加权美元强度指数序列（oldest→newest）。"""
    today = date.today()
    start = today - timedelta(days=days)
    url = _FRANKFURTER_URL.format(d0=start.isoformat(), d1=today.isoformat())
    txt = _http_get(url, timeout=15)
    import json
    data = json.loads(txt)
    daylist = sorted(data.get("rates", {}).keys())
    series = []
    for d in daylist:
        rates = data["rates"][d]
        idx = 1.0
        for sym, w in _DXY_WEIGHTS.items():
            c = rates.get(sym)
            ref = _DXY_REF.get(sym)
            if c and ref:
                idx *= (c / ref) ** w
        series.append(round(100.0 * idx, 4))
    return series


def _fetch_vix_series(days: int = 400):
    """CBOE 日线 CSV → Close 序列（oldest→newest）。"""
    txt = _http_get(_CBOE_VIX_URL, timeout=25)
    rows = [l for l in txt.strip().splitlines() if l.strip()][1:]  # 跳表头
    vals = []
    for row in rows[-days:]:
        parts = row.split(",")
        if len(parts) >= 5:
            try:
                vals.append(float(parts[4]))  # CLOSE
            except ValueError:
                pass
    return vals


def _fetch_us10y_series(days: int = 400):
    """美国财政部日线收益率曲线 CSV → 10 Yr 序列（oldest→newest）。"""
    txt = _http_get(_TREASURY_URL, timeout=25)
    rows = [l for l in txt.strip().splitlines() if l.strip()]
    if len(rows) < 2:
        return []
    hdr = rows[0].split(",")
    idx = None
    for i, h in enumerate(hdr):
        if h.strip().strip('"') == "10 Yr":
            idx = i
            break
    if idx is None:
        return []
    pts = []
    for row in rows[1:]:
        parts = row.split(",")
        if len(parts) <= idx:
            continue
        d = parts[0].strip().strip('"')
        try:
            v = float(parts[idx])
        except ValueError:
            continue
        try:
            mm, dd, yy = d.split("/")
            dt = date(int(yy), int(mm), int(dd))
        except Exception:  # noqa: BLE001
            continue
        if v > 0:
            pts.append((dt, v))
    pts.sort(key=lambda x: x[0])
    return [v for _, v in pts][-days:]


_FETCHERS = {"DXY": _fetch_dxy_series, "VIX": _fetch_vix_series, "US10Y": _fetch_us10y_series}


# ───────────── 缓存 + 降级 ─────────────

def _load_series(name: str, days: int = 400):
    """带 TTL + 磁盘缓存的日线序列获取，返回 list[float] 或 None。"""
    now = time.time()
    c = _series_cache.get(name)
    if c and now - c["updated"] < _SERIES_TTL and len(c["vals"]) >= 8:
        return c["vals"]
    # 磁盘缓存
    p = os.path.join(CACHE_DIR, name + ".series.csv")
    try:
        if os.path.exists(p) and now - os.path.getmtime(p) < _SERIES_TTL:
            with open(p) as f:
                vals = [float(x) for x in f.read().split() if x]
            if len(vals) >= 8:
                _series_cache[name] = {"vals": vals, "updated": now}
                return vals
    except (OSError, ValueError):
        pass
    # 实时获取
    try:
        vals = _FETCHERS[name](days)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cov] {name} 日线获取失败: {e}")
        vals = None
    if vals and len(vals) >= 8:
        _series_cache[name] = {"vals": vals, "updated": now}
        try:
            with open(p, "w") as f:
                f.write("\n".join(str(x) for x in vals))
        except OSError:
            pass
        return vals
    # 已过期磁盘兜底
    try:
        with open(p) as f:
            vals = [float(x) for x in f.read().split() if x]
        if vals:
            return vals
    except (OSError, ValueError):
        pass
    return None


def _resample_daily_to_intraday(daily, target_len: int, bars_per_day: int = _BARS_PER_DAY):
    """把日线序列阶梯重采样到 target_len 的 intraday 协变量。

    取最近 need_days 个日值，各自重复铺满对应 intraday 段；
    日值不足则前向填充最早值。返回长度=target_len 的 list[float]。
    """
    if not daily:
        return None
    need_days = max(1, -(-target_len // bars_per_day))  # ceil
    if len(daily) >= need_days:
        chosen = daily[-need_days:]
    else:
        chosen = [daily[0]] * (need_days - len(daily)) + daily
    out = []
    per = target_len // len(chosen)
    rem = target_len - per * len(chosen)
    for i, v in enumerate(chosen):
        cnt = per + (1 if i >= len(chosen) - rem else 0)
        out.extend([v] * cnt)
    return out[:target_len]


# ───────────── 对外主接口 ─────────────

def get_cross_asset_covariates(target_len: int, market_data: dict = None) -> dict:
    """返回对齐后的协变量 dict {name: list[float](长度=target_len)} 或 None。

    - 优先 market_data 已订阅的跨资产 M15 closes；
    - 不足则用 DXY/VIX/US10Y 三个日线源（阶梯重采样）补充；
    - 全部缺失返回 None（调用方单变量降级）。
    """
    if target_len <= 0:
        return None
    cov: dict = {}

    # 1) 优先 market_data 已订阅跨资产
    tfs = (market_data or {}).get("timeframes", {}) or {}
    for name in ("DXY", "US10Y", "VIX"):
        md = tfs.get(name) or {}
        closes = md.get("closes")
        if not closes:
            bars = md.get("bars", []) or []
            if isinstance(bars, list):
                closes = [float(b.get("close", 0)) for b in bars
                          if isinstance(b, dict) and b.get("close")]
        if closes and len(closes) >= 32:
            cov[name] = [float(c) for c in closes]

    # 2) 日线源补充缺失项
    for name in ("DXY", "US10Y", "VIX"):
        if name not in cov:
            s = _load_series(name)
            if s:
                rs = _resample_daily_to_intraday(s, target_len)
                if rs:
                    cov[name] = rs

    if not cov:
        return None

    # 3) 对齐长度：取最近 target_len 个，不足前向填充
    aligned = {}
    for name, series in cov.items():
        if len(series) >= target_len:
            aligned[name] = series[-target_len:]
        else:
            aligned[name] = [series[0]] * (target_len - len(series)) + series
    return aligned
