"""
万象Ai XAUUSD — 订单流 / CVD 模块（②）

目标：给 AI 提供「买盘是否枯竭、卖压是否在放大」的订单流信号，
      补上方案里缺失的「订单流代理」维度（类别②）。

四源设计（稳健优先 + 诚实标注，全部纯上下文增强、绝不进决策闸门）：
  1) Binance XAU/USD 永续（用户指定源·【真 CVD】）：
     - 拉 fapi/spot klines（含 taker_buy_base_asset_volume）→ 算每根主动买卖量
       delta = 2*taker_buy - total_volume（带符号）；CVD = 累计。
     - 这是本系统唯一「真逐笔成交 CVD」（Binance 黄金永续真实 taker 买卖量）。
     - 多种子域名与主备，任一可达即用；全不可达则优雅降级 available=False。
  2) CME 黄金期货 GC1! 量能压力代理（补充源·【非真 CVD，诚实标注】）：
     - 调研结论（≥3源交叉验证）：CME 官方逐笔 Trades 需 WebSocket+ILA 付费协议；
       阿里云 CME API 仅给聚合 volume 无买卖拆分；API Ninjas/Omkar 历史 OHLCV 需付费 key。
       → CME 逐笔真实 CVD 免费不可得。
     - 因此本源用免 key 的 Stooq gc.f 1m / Yahoo GC=F 1m（OHLCV）算
       「成交量加权方向压力」= Σ sign(close-open)*volume，作为 CME 黄金期货量能代理，
       补充确认 Binance 真 CVD 的方向，绝非逐笔买卖 delta。is_real_cvd=False。
  3) MT5 本地蜡烛方向量能代理（本沙箱/无外网时仍真实运作·【非真 CVD，诚实标注】）：
     - 用 MT5 已拉取的 M1 分笔（close vs open 方向 × volume）算本地方向量能代理。
  4) MT5 copy_ticks_range tick 级代理（用户指定·【非真 CVD，诚实标注】）：
     - 用 mt5.copy_ticks_range 拉 XAUUSD tick，按 MqlTick flags + 价格跳动方向
       算 tick-volume 方向累加（tick 级微观失衡）。FX 现货无逐笔买卖 flag
       （last/volume_real 恒空、TICK_FLAG_BUY/SELL 不置位）→ 仅方向累加、无量纲，
       诚实标注为 tick 级方向压力代理，非真 CVD。
     - 接法照搬 ts_reference_models.load_live_rates 安全模式：懒导入、幂等 initialize、
       绝不 shutdown、全程降级；API 进程连不到终端时回退到 M1 棒方向代理。

合并快照：binance 可达 → 主 source="binance" 且附 cme/mt5_proxy/mt5_tick；
          不可达 → 任一可达源作主，available_sources 列出实际贡献源。
无论如何 AI 都拿到订单流信号 → 符合「确保每一个都真实运作」；全部仅上下文，非闸门。

设计铁律：纯行情、全局共享、多账号优先；网络/终端失败绝不阻断决策（降级）。
"""

import json
import ssl
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Dict, Any


# ── Binance 多端点（任一可达即用）──
# fapi=永续官方；data-api.binance.vision=公开镜像(含 taker_buy)；api.binance.com=现货(含 taker_buy)
_BINANCE_ENDPOINTS = [
    "https://fapi.binance.com/fapi/v1/klines?symbol=XAUUSDT&interval=1m&limit=60",
    "https://data-api.binance.vision/api/v3/klines?symbol=XAUUSDT&interval=1m&limit=60",
    "https://api.binance.com/api/v3/klines?symbol=XAUUSDT&interval=1m&limit=60",
]
_REFRESH_SEC = 60
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_cache: Dict[str, Any] = {"data": None, "ts": 0.0, "ok": False}
_lock = threading.Lock()


def _fetch_binance_once() -> Optional[Dict[str, Any]]:
    """拉一笔 Binance klines 并算 CVD。失败返回 None。"""
    last_err = ""
    for url in _BINANCE_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5, context=_CTX) as resp:
                rows = json.loads(resp.read().decode())
            if not rows or len(rows) < 10:
                continue
            cvd = 0.0
            deltas = []
            closes = []
            for k in rows:
                vol = float(k[5])
                tbv = float(k[9])  # taker_buy_base_asset_volume
                delta = tbv - (vol - tbv)  # = 2*tbv - vol，带符号主动买卖量
                cvd += delta
                deltas.append(delta)
                closes.append(float(k[4]))
            recent = deltas[-5:]
            delta_recent = sum(recent)
            cvd_first_half = sum(deltas[: len(deltas) // 2])
            cvd_slope = cvd - cvd_first_half
            price_chg = closes[-1] - closes[0]
            # 判定
            buy_pressure_dry = (delta_recent < -1.0) or (price_chg > 0 and cvd_slope < -1.0)
            sell_pressure_high = (delta_recent < -2.0) or (price_chg < 0 and cvd_slope < -2.0)
            if delta_recent > 1.0:
                reading = "买盘主动(主动买入占优)"
            elif delta_recent < -1.0:
                reading = "卖压主动(主动卖出占优)"
            else:
                reading = "买卖均衡"
            return {
                "available": True,
                "source": "binance",
                "symbol": "XAUUSDT",
                "candles": len(rows),
                "cvd": round(cvd, 2),
                "cvd_slope": round(cvd_slope, 2),
                "delta_recent": round(delta_recent, 2),
                "price_change": round(price_chg, 2),
                "reading": reading,
                "is_real_cvd": True,
                "buy_pressure_dry": bool(buy_pressure_dry),
                "sell_pressure_high": bool(sell_pressure_high),
            }
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:60]
            continue
    return None


def _refresh_loop():
    while True:
        try:
            d = _fetch_binance_once()
            with _lock:
                if d is not None:
                    _cache["data"] = d
                    _cache["ok"] = True
                else:
                    _cache["ok"] = False
                _cache["ts"] = time.time()
        except Exception:
            with _lock:
                _cache["ok"] = False
                _cache["ts"] = time.time()
        time.sleep(_REFRESH_SEC)


def _ensure_refresh_started():
    if not getattr(_refresh_loop, "_started", False):
        _refresh_loop._started = True
        t = threading.Thread(target=_refresh_loop, daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════════════
#  ② CME 黄金期货 GC1! 量能压力代理（非真 CVD，诚实标注）
#  源：Stooq gc.f 1m（免 key，可靠）为主；Yahoo GC=F 1m 为辅。
#  计算：Σ sign(close-open)*volume = 成交量加权方向压力（量能代理，非逐笔买卖 delta）
# ═══════════════════════════════════════════════════════════════════════
_CME_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0, "ok": False}
_CME_LOCK = threading.Lock()
_CME_REFRESH_SEC = 60

_CME_STOOQ_URL = "https://stooq.com/q/d/l/?s=gc.f&i=1m"
_CME_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d"


def _fetch_cme_stooq() -> Optional[list]:
    """Stooq gc.f 1m CSV → [(open,high,low,close,volume), ...]。失败返回 None。"""
    try:
        req = urllib.request.Request(_CME_STOOQ_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=_CTX) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) < 10:
            return None
        rows = []
        for ln in lines[1:]:  # 跳过表头 Date,Time,Open,High,Low,Close,Volume
            parts = ln.split(",")
            if len(parts) < 7:
                continue
            try:
                o = float(parts[2]); h = float(parts[3]); l = float(parts[4])
                c = float(parts[5]); v = float(parts[6])
            except ValueError:
                continue
            rows.append((o, h, l, c, v))
        return rows if len(rows) >= 10 else None
    except Exception:  # noqa: BLE001
        return None


def _fetch_cme_yahoo() -> Optional[list]:
    """Yahoo GC=F 1m → [(open,high,low,close,volume), ...]。失败返回 None。"""
    try:
        req = urllib.request.Request(_CME_YAHOO_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=_CTX) as resp:
            obj = json.loads(resp.read().decode())
        res = obj.get("chart", {}).get("result")
        if not res or not res[0]:
            return None
        q = res[0].get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close", []); volumes = q.get("volume", [])
        opens = q.get("open", []); highs = q.get("high", []); lows = q.get("low", [])
        rows = []
        for i in range(min(len(closes), len(volumes))):
            if closes[i] is None or volumes[i] is None:
                continue
            rows.append((
                float(opens[i] or closes[i]), float(highs[i] or closes[i]),
                float(lows[i] or closes[i]), float(closes[i]), float(volumes[i] or 0),
            ))
        return rows if len(rows) >= 10 else None
    except Exception:  # noqa: BLE001
        return None


def _build_cme_proxy(rows: list, method: str) -> Dict[str, Any]:
    """从 OHLCV 行算 CME 黄金期货量能压力代理（非真 CVD）。"""
    vol_pressure = 0.0
    deltas = []
    closes = []
    vols = []
    for (o, h, l, c, v) in rows:
        direction = 1.0 if c >= o else -1.0
        signed = direction * v  # 成交量加权方向压力（量能代理）
        vol_pressure += signed
        deltas.append(signed)
        closes.append(c)
        vols.append(v)
    if not deltas:
        return None
    recent = deltas[-5:]
    delta_recent = sum(recent)
    vol_pressure_slope = vol_pressure - sum(deltas[: len(deltas) // 2])
    price_chg = closes[-1] - closes[0]
    total_vol = sum(vols)
    # 判定：量能压力方向与价格方向背离 → 压力信号
    buy_pressure_dry = (delta_recent < 0 and price_chg > 0)   # 涨价但量能转负=无量上涨
    sell_pressure_high = (delta_recent < 0 and price_chg < 0)  # 跌价且量能负=抛压重
    if delta_recent > 0:
        reading = "CME量能偏多(期货量能确认上涨)"
    elif delta_recent < 0:
        reading = "CME量能偏空(期货量能确认下跌)"
    else:
        reading = "CME量能均衡"
    return {
        "available": True,
        "source": "cme",
        "symbol": "GC1!(COMEX Gold Futures)",
        "method": method,
        "is_real_cvd": False,
        "note": "CME 逐笔真实 CVD 需付费行情(CME WebSocket+ILA)；本源仅用 GC1! OHLCV 量能压力，非逐笔买卖 delta",
        "bars": len(rows),
        "total_volume": round(total_vol, 2),
        "cvd_proxy": round(vol_pressure, 2),
        "cvd_slope": round(vol_pressure_slope, 2),
        "delta_recent": round(delta_recent, 2),
        "price_change": round(price_chg, 2),
        "reading": reading,
        "buy_pressure_dry": bool(buy_pressure_dry),
        "sell_pressure_high": bool(sell_pressure_high),
    }


def _compute_cme_proxy() -> Optional[Dict[str, Any]]:
    """拉 CME GC1! OHLCV 量能代理；带 60s 缓存；全源失败降级 None。"""
    now = time.time()
    with _CME_LOCK:
        if _CME_CACHE["ok"] and (now - _CME_CACHE["ts"]) < _CME_REFRESH_SEC:
            return _CME_CACHE["data"]
    rows = _fetch_cme_stooq()
    method = "Stooq gc.f 1m OHLCV"
    if rows is None:
        rows = _fetch_cme_yahoo()
        method = "Yahoo GC=F 1m OHLCV"
    result = _build_cme_proxy(rows, method) if rows else None
    with _CME_LOCK:
        _CME_CACHE["data"] = result
        _CME_CACHE["ts"] = now
        _CME_CACHE["ok"] = result is not None
    return result


# ═══════════════════════════════════════════════════════════════════════
#  ③ MT5 本地蜡烛方向量能代理（非真 CVD，诚实标注）
# ═══════════════════════════════════════════════════════════════════════
def _compute_mt5_proxy(tfs_raw: dict) -> Optional[Dict[str, Any]]:
    """用 MT5 本地 K 线算【方向量能代理】（无需外网，本沙箱真实运作）。

    ★ 诚实标注：本函数仅为「蜡烛涨跌方向 × 成交量」的方向量能代理，
    并非 tick 级逐笔订单流（真 CVD）。REAL/沙箱环境均无 tick 级成交明细，命名须如实，
    避免下游误当订单流强度参与方向终审/拦截。仅作视觉辅助信号。
    """
    tf = None
    for cand in ("M1", "M5", "M15"):
        bars = (tfs_raw.get(cand, {}) or {}).get("bars", [])
        if len(bars) >= 20:
            tf = cand
            break
    if tf is None:
        return None
    bars = tfs_raw[tf]["bars"]
    cvd = 0.0
    deltas = []
    closes = []
    for b in bars:
        o = float(b.get("open", 0) or 0)
        c = float(b.get("close", 0) or 0)
        v = float(b.get("volume", 0) or 0)
        # ★ 仅为「蜡烛涨跌方向 × 成交量」的方向量能代理，非 tick 级订单流。
        directional_volume = (1 if c >= o else -1) * v
        cvd += directional_volume
        deltas.append(directional_volume)
        closes.append(c)
    recent = deltas[-5:]
    delta_recent = sum(recent)
    cvd_slope = cvd - sum(deltas[: len(deltas) // 2])
    price_chg = closes[-1] - closes[0]
    buy_pressure_dry = (delta_recent < 0 and price_chg > 0)  # 上涨但量能转负=无量上涨
    sell_pressure_high = (delta_recent < 0 and price_chg < 0)  # 下跌且量能负=抛压重
    if delta_recent > 0:
        reading = "买盘主动(本地量能确认上涨)"
    elif delta_recent < 0:
        reading = "卖压主动(本地量能确认下跌)"
    else:
        reading = "量能均衡"
    return {
        "available": True,
        "source": "mt5_proxy",
        "tf": tf,
        "bars": len(bars),
        "cvd": round(cvd, 2),
        "cvd_slope": round(cvd_slope, 2),
        "delta_recent": round(delta_recent, 2),
        "price_change": round(price_chg, 2),
        "is_real_cvd": False,
        "reading": reading,
        "buy_pressure_dry": bool(buy_pressure_dry),
        "sell_pressure_high": bool(sell_pressure_high),
    }


# ═══════════════════════════════════════════════════════════════════════
#  ④ MT5 copy_ticks_range tick 级代理（非真 CVD，诚实标注）
#  接法照搬 ts_reference_models.load_live_rates 安全模式：
#  懒导入 MetaTrader5、幂等 initialize、绝不 shutdown、全程降级。
#  FX 现货无逐笔买卖 flag → 仅按价格跳动方向 × tick-volume 累加（tick 级方向压力）。
#  API 进程连不到终端时回退到 M1 棒方向代理（method 标注）。
# ═══════════════════════════════════════════════════════════════════════
_MT5_TICK_CACHE: Dict[str, Any] = {"data": None, "ts": 0.0}
_MT5_TICK_REFRESH_SEC = 30
_MT5_SYMBOLS = ["XAUUSD", "XAUUSDm", "XAUUSD.", "GOLD"]


def _compute_mt5_tick_proxy(tfs_raw: dict) -> Optional[Dict[str, Any]]:
    """用 mt5.copy_ticks_range 算 tick 级方向压力代理（非真 CVD）。"""
    now = time.time()
    if (_MT5_TICK_CACHE["data"] is not None) and (now - _MT5_TICK_CACHE["ts"]) < _MT5_TICK_REFRESH_SEC:
        return _MT5_TICK_CACHE["data"]

    ticks = None
    method = "copy_ticks_range"
    try:
        import MetaTrader5 as mt5  # 懒导入：无 wheel/无终端时静默降级
        if not mt5.initialize():  # 幂等附着运行中终端；失败→None（绝不 shutdown）
            raise RuntimeError("mt5.initialize 失败")
        from_dt = datetime.now() - timedelta(minutes=15)
        to_dt = datetime.now()
        for sym in _MT5_SYMBOLS:
            try:
                t = mt5.copy_ticks_range(sym, from_dt, to_dt, mt5.COPY_TICKS_ALL)
            except Exception:  # noqa: BLE001
                t = None
            if t is not None and len(t) > 50:
                ticks = t
                break
    except Exception:  # noqa: BLE001
        ticks = None

    result = None
    if ticks is not None and len(ticks) > 50:
        # 逐 tick 价格跳动方向 × tick-volume 累加（tick 级微观失衡）
        # FX 现货 last/volume_real 恒空、TICK_FLAG_BUY/SELL 不置位 → 用 ask 跳动方向
        # ★ 2026-08-17 开市盯盘修复：numpy 2.x 下 `"ask" in ticks.dtype` 触发
        #   `VoidDType is not iterable`（dtype.__contains__ 退化），且此段在 try 块外
        #   → 异常冒泡 → 整个 CVD 信号降级不可用（09:00 起 733 次报错）。
        #   改用 dtype.names 元组检查（跨 numpy 1.x/2.x 兼容）。
        _dtype_names = tuple(getattr(getattr(ticks, "dtype", None), "names", None) or ())
        try:
            if "ask" in _dtype_names:
                asks = [float(x["ask"]) for x in ticks]
            else:
                asks = [float(x["bid"]) for x in ticks]
            tvols = [float(x["volume"]) for x in ticks]
        except Exception:  # noqa: BLE001
            # 字段缺失/转换失败：回退 M1 bars 方向代理（下方 result is None 分支）
            ticks = None
            asks = tvols = None

        if asks is not None and tvols is not None:
            imbalance = 0.0
            deltas = []
            for i in range(1, len(asks)):
                d = asks[i] - asks[i - 1]
                if d == 0:
                    continue
                sign = 1.0 if d > 0 else -1.0
                tv = tvols[i] if (tvols and tvols[i] > 0) else 1.0  # tick-volume 无量纲时按 tick 计数
                step = sign * tv
                imbalance += step
                deltas.append(step)
            if deltas:
                delta_recent = sum(deltas[-20:])
                slope = imbalance - sum(deltas[: len(deltas) // 2])
                # ★ 2026-08-15 审计P1修复：原实现两布尔恒等（都取 delta_recent<0）→
                #   按 OR 汇总时「买盘枯竭」与「卖压放大」必同时触发，语义失真。
                #   现在区分：买盘枯竭=近期净卖+整体斜率走弱；卖压放大=近期净卖+强度显著高于平均。
                _d20 = deltas[-20:] or [0.0]
                _d20_std = (sum(x * x for x in _d20) / len(_d20)) ** 0.5 or 1.0
                buy_pressure_dry = delta_recent < 0 and slope < 0
                sell_pressure_high = delta_recent < 0 and abs(delta_recent) > _d20_std * 1.5
                reading = "tick买压" if delta_recent > 0 else ("tick卖压" if delta_recent < 0 else "tick均衡")
                result = {
                    "available": True,
                    "source": "mt5_tick",
                    "method": method,
                    "is_real_cvd": False,
                    "note": "FX现货无逐笔买卖flag；仅按价格跳动方向×tick-volume累加，非真CVD",
                    "ticks": len(asks),
                    "cvd_proxy": round(imbalance, 2),
                    "cvd_slope": round(slope, 2),
                    "delta_recent": round(delta_recent, 2),
                    "reading": reading,
                    "buy_pressure_dry": bool(buy_pressure_dry),
                    "sell_pressure_high": bool(sell_pressure_high),
                }

    # copy_ticks_range 不可用（API 进程无终端）→ 回退 M1 棒方向代理
    if result is None:
        bars = None
        for cand in ("M1", "M5"):
            b = (tfs_raw.get(cand, {}) or {}).get("bars", [])
            if len(b) >= 20:
                bars = b
                break
        if bars:
            imb = 0.0
            deltas = []
            for b in bars:
                o = float(b.get("open", 0) or 0)
                c = float(b.get("close", 0) or 0)
                v = float(b.get("volume", 0) or 0) or 1.0
                s = (1.0 if c >= o else -1.0) * v
                imb += s
                deltas.append(s)
            delta_recent = sum(deltas[-5:])
            # ★ 2026-08-15 审计P1修复：与 tick 路径同构，两布尔不再恒等
            _d5 = deltas[-5:] or [0.0]
            _d5_std = (sum(x * x for x in _d5) / len(_d5)) ** 0.5 or 1.0
            _bars_slope = imb - sum(deltas[: len(deltas) // 2])
            result = {
                "available": True,
                "source": "mt5_tick",
                "method": "m1_bars_fallback",
                "is_real_cvd": False,
                "note": "API进程无MT5终端，copy_ticks_range不可用；回退M1棒方向代理(非真CVD)",
                "bars": len(bars),
                "cvd_proxy": round(imb, 2),
                "cvd_slope": round(_bars_slope, 2),
                "delta_recent": round(delta_recent, 2),
                "reading": ("tick买压" if delta_recent > 0 else ("tick卖压" if delta_recent < 0 else "tick均衡")),
                "buy_pressure_dry": bool(delta_recent < 0 and _bars_slope < 0),
                "sell_pressure_high": bool(delta_recent < 0 and abs(delta_recent) > _d5_std * 1.5),
            }

    _MT5_TICK_CACHE["data"] = result
    _MT5_TICK_CACHE["ts"] = now
    return result


def get_orderflow_snapshot(tfs_raw: dict) -> Dict[str, Any]:
    """合并四源快照，供 AI 注入。全部纯上下文增强，非闸门。

    返回字段保持向后兼容：source/reading/buy_pressure_dry/sell_pressure_high
    仍由主源(cvd 真源优先)提供；新增 available_sources / cme / mt5_tick 明细。
    """
    _ensure_refresh_started()
    with _lock:
        binance = _cache["data"] if _cache.get("ok") else None
    proxy = _compute_mt5_proxy(tfs_raw or {})
    cme = _compute_cme_proxy()
    mt5_tick = _compute_mt5_tick_proxy(tfs_raw or {})

    sources = []
    if binance:
        sources.append("binance")
    if proxy:
        sources.append("mt5_proxy")
    if cme:
        sources.append("cme")
    if mt5_tick:
        sources.append("mt5_tick")
    if not sources:
        return {"available": False}

    # 主标签：优先真 CVD（binance），否则任一可达源
    if binance:
        src = "binance"
    elif cme:
        src = "cme"
    elif mt5_tick:
        src = "mt5_tick"
    elif proxy:
        src = "mt5_proxy"
    else:
        src = "none"

    # 压力信号：任一源报枯竭/抛压即汇总（OR），绝不单源拦截
    all_src = [s for s in (binance, cme, mt5_tick, proxy) if s]
    buy_dry = any((s or {}).get("buy_pressure_dry") for s in all_src)
    sell_high = any((s or {}).get("sell_pressure_high") for s in all_src)
    reading = ""
    for s in (binance, cme, mt5_tick, proxy):
        if s and s.get("reading"):
            reading = s["reading"]
            break

    return {
        "available": True,
        "source": src,
        "available_sources": sources,
        "binance": binance,
        "mt5_proxy": proxy,
        "cme": cme,
        "mt5_tick": mt5_tick,
        "reading": reading,
        "buy_pressure_dry": bool(buy_dry),
        "sell_pressure_high": bool(sell_high),
    }
