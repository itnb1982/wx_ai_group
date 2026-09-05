# -*- coding: utf-8 -*-
"""本地时序模型「信号源参考」服务 —— 完全独立于交易决策链。

══════════════════════════════════════════════════════════════════════
★ 架构红线（用户铁律「提准非拦截」「参考面板·未接入系统」）：
   本服务仅供前端「多模型信号源参考面板」观测模型能力，
   其产出【绝不】进入任何开仓/平仓/风控决策。
   反向守卫见 tests/test_ts_reference_decoupled.py：本模块不得 import 任何
   决策链模块（debate_engine / meta_agent / trade_executor / risk_engine /
   smart_exit / numpy_direction_guard / chronos_service）。
══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import numpy as np

SYMBOL = "XAUUSD"
TF = "H1"
CTX = 256
HORIZON = 4
REFRESH_SEC = 300  # 5 分钟刷新一次：参考面板是「观测」，不需要秒级实时，降低系统负载
HIT_WINDOW = 30          # 滚动命中率窗口（最近 30 次刷新）

# 各模型展示配色（前端也会用同一套，保持一致）
MODEL_COLORS = {
    "Chronos-2(120M)": "var(--green)",
    "TimesFM-2.5(200M)": "var(--blue)",
    "Time-MoE(200M)": "var(--purple)",
    "Moirai(447M)": "var(--gold)",
}

# 四模型在系统中的角色说明（架构透明化，让用户一眼看懂谁干什么）
# ★ 2026-08-11 全盘可视化：fusion_v2 已落地，4 个本地时序模型聚合为「融合票(权重0.22)」
#   接入 meta_agent 决策链第三票方向源（三道安全门保障，绝非裸接）。措辞从"仅参考未接入"
#   更新为"已聚合接入"，与决策链真实状态一致，避免客户看到两套矛盾的架构描述。
ROLE_NOTE = (
    "云端双脑(DeepSeek+混元) = 方向锚与可解释性；"
    "本地 Chronos-2 = 风险区间/动态止盈；"
    "NumPy 规则终审器 = 强冲突才 HOLD 的安全阀；"
    "下方 4 个本地时序模型已聚合为「融合票(权重0.22)」接入 meta_agent 决策链第三票方向源"
    "（三道安全门：合成行情禁用 / snapshot 过期 / 可用模型<2 回退单 Chronos）。"
)


class TSReferenceService:
    def __init__(self):
        self._lock = threading.Lock()
        self._predictors = []       # [(name, instance), ...]
        self._loaded = False
        self._started = False
        self._thread = None
        self._snapshot = {
            "status": "未启动",
            "live": False,
            "symbol": SYMBOL,
            "tf": TF,
            "horizon": HORIZON,
            "last_price": None,
            "updated_at": 0,
            "models": [],
            "hit_window": HIT_WINDOW,
            "note": ROLE_NOTE,
            "decoupled": True,
        }
        self._prev_preds = {}       # name -> {"dir":, "price":}
        self._hits = {}             # name -> [bool, ...] 滚动
        self._pending = {}          # name -> 未就绪原因(str)，面板展示为「待安装」卡片

    # ── 生命周期 ──────────────────────────────────────────────
    def ensure_started(self):
        """幂等启动。首次启动时把「模型加载」也丢进后台线程，避免阻塞 uvicorn 启动。"""
        with self._lock:
            if self._started:
                return
            self._started = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        self._thread = t

    def _loop(self):
        # 首轮：加载模型（可能耗时十几秒，放在线程里不阻塞启动）
        try:
            self._load_models()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._snapshot = dict(self._snapshot)
                self._snapshot["status"] = "模型加载失败"
                self._snapshot["error"] = str(e)[:200]
        # 之后：周期性刷新快照
        while True:
            try:
                self._refresh()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._snapshot = dict(self._snapshot)
                    self._snapshot["status"] = "刷新异常"
                    self._snapshot["error"] = str(e)[:200]
            time.sleep(REFRESH_SEC)

    def _load_models(self):
        from app.services.ts_reference_models import (
            ChronosP, TimesFMP, TimeMoEP, MoiraiP, load_live_rates,
        )
        self._pending = {}
        loaded = []
        for cls in (ChronosP, TimesFMP, TimeMoEP):
            try:
                p = cls()
                if p.ready():
                    loaded.append(p)
                else:
                    self._pending[p.name] = "权重未找到或加载失败"
            except Exception:  # noqa: BLE001
                pass
        # Moirai：独立 venv，单独给明确未就绪原因（而非每轮必败子进程）
        try:
            mp = MoiraiP()
            if mp.ready():
                loaded.append(mp)
            else:
                self._pending[mp.name] = mp.unready_reason()
        except Exception as e:  # noqa: BLE001
            self._pending["Moirai(447M)"] = str(e)[:120]

        with self._lock:
            self._predictors = [(p.name, p) for p in loaded]
            self._loaded = True
            self._snapshot = dict(self._snapshot)
            self._snapshot["status"] = "模型加载中" if not loaded else "运行中"

    # ── 核心刷新 ──────────────────────────────────────────────
    def _refresh(self):
        from app.services.ts_reference_models import load_live_rates

        with self._lock:
            predictors = list(self._predictors)
        if not predictors:
            # 还没加载完或全失败：先用空快照占位
            with self._lock:
                self._snapshot = dict(self._snapshot)
                self._snapshot["status"] = "模型加载中"
                self._snapshot["updated_at"] = time.time()
            return

        closes, highs, lows, live = load_live_rates(SYMBOL, TF, CTX + HORIZON + 50)
        ctx = closes[-CTX:]
        last_price = float(closes[-1])

        models_out = []
        cur_preds = {}

        def _run_one(name_p):
            name, p = name_p
            try:
                return p.predict_detail(ctx, HORIZON)
            except Exception as e:  # noqa: BLE001
                return e

        # 每个预测器最多给 75 秒（略大于 Moirai 子进程 60s timeout 的保险余量），
        # 避免单个模型挂起拖住整轮刷新或 uvicorn 健康探测。
        with ThreadPoolExecutor(max_workers=len(predictors) or 1) as exe:
            futures = {exe.submit(_run_one, np): np[0] for np in predictors}
            for fut, name in futures.items():
                try:
                    d = fut.result(timeout=75)
                    if isinstance(d, Exception):
                        raise d
                    cur_preds[name] = {"dir": d.direction, "price": last_price}
                    # 命中率：与上一轮预测时价格相比，方向是否应验
                    prev = self._prev_preds.get(name)
                    if prev is not None and prev.get("dir") in ("BUY", "SELL"):
                        moved = last_price - prev["price"]
                        correct = ((prev["dir"] == "BUY" and moved > 0) or
                                   (prev["dir"] == "SELL" and moved < 0))
                        self._hits.setdefault(name, []).append(bool(correct))
                        if len(self._hits[name]) > HIT_WINDOW:
                            self._hits[name].pop(0)
                    models_out.append({
                        "name": name,
                        "direction": d.direction,
                        "score": round(float(d.score), 4),
                        "confidence": round(float(d.confidence), 4),
                        "pred_end": round(float(d.pred_end), 2) if d.pred_end else None,
                        "last_price": round(d.last_price, 2),
                        "lo": round(float(d.lo), 2) if d.lo else None,
                        "hi": round(float(d.hi), 2) if d.hi else None,
                        "available": True,
                        "color": MODEL_COLORS.get(name),
                    })
                except FutureTimeout:
                    models_out.append({
                        "name": name,
                        "direction": "TIMEOUT",
                        "available": False,
                        "error": "本轮推理超时（模型加载或推理阻塞）",
                        "color": MODEL_COLORS.get(name),
                        "score": 0.0, "confidence": 0.0,
                        "pred_end": None, "last_price": None,
                        "lo": None, "hi": None,
                    })
                except Exception as e:  # noqa: BLE001
                    models_out.append({
                        "name": name,
                        "direction": "ERROR",
                        "available": False,
                        "error": str(e)[:160],
                        "color": MODEL_COLORS.get(name),
                        "score": 0.0,
                        "confidence": 0.0,
                        "pred_end": None,
                        "last_price": None,
                        "lo": None,
                        "hi": None,
                    })

        # 挂命中率
        for m in models_out:
            h = self._hits.get(m["name"], [])
            m["hits"] = len(h)
            m["hit_rate"] = round(sum(h) / len(h), 3) if h else None

        # 未就绪模型（如 Moirai 待独立 venv）也展示为「待安装」卡片，保持透明
        with self._lock:
            pending = dict(self._pending)
        for name, reason in pending.items():
            models_out.append({
                "name": name,
                "direction": "N/A",
                "available": False,
                "error": reason,
                "color": MODEL_COLORS.get(name),
                "score": 0.0, "confidence": 0.0,
                "pred_end": None, "last_price": None, "lo": None, "hi": None,
            })

        with self._lock:
            self._prev_preds = cur_preds
            self._snapshot = {
                "status": "运行中",
                "live": live,
                "symbol": SYMBOL,
                "tf": TF,
                "horizon": HORIZON,
                "last_price": round(last_price, 2),
                "updated_at": time.time(),
                "models": models_out,
                "hit_window": HIT_WINDOW,
                "note": ROLE_NOTE,
                "decoupled": True,
            }

    # ── 对外只读接口 ──────────────────────────────────────────
    def get_snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def selftest_model(self, name: str) -> dict:
        """单个模型自检： ready() + 一次快速推理，返回是否可用及时延。

        会优先复用已加载的预测器实例；若未加载则按名称尝试实例化。
        推理使用合成行情，不依赖 MT5 在线，保证休市/离线也能自检。
        """
        import time

        from app.services.ts_reference_models import (
            ChronosP, MoiraiP, TimeMoEP, TimesFMP,
        )

        start = time.time()
        result = {
            "name": name,
            "available": False,
            "ready": False,
            "inference_ok": False,
            "latency_ms": 0,
            "error": None,
        }

        # 1) 找已加载的实例
        p = None
        pending_reason = None
        with self._lock:
            for n, pred in self._predictors:
                if n == name:
                    p = pred
                    break
            pending_reason = self._pending.get(name)

        # 2) 未加载则按名称实例化（不影响主服务已有实例）
        if p is None:
            mapping = {
                "Chronos-2(120M)": ChronosP,
                "TimesFM-2.5(200M)": TimesFMP,
                "Time-MoE(200M)": TimeMoEP,
                "Moirai(447M)": MoiraiP,
            }
            cls = mapping.get(name)
            if cls is None:
                result["error"] = f"未知模型: {name}"
                result["latency_ms"] = round((time.time() - start) * 1000)
                return result
            try:
                p = cls()
            except Exception as e:  # noqa: BLE001
                result["error"] = f"实例化失败: {type(e).__name__}: {str(e)[:120]}"
                result["latency_ms"] = round((time.time() - start) * 1000)
                return result

        # 3) ready 检查
        try:
            if not p.ready():
                result["error"] = pending_reason or "模型未就绪（权重/venv 未准备好）"
                result["latency_ms"] = round((time.time() - start) * 1000)
                return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"ready() 异常: {type(e).__name__}: {str(e)[:120]}"
            result["latency_ms"] = round((time.time() - start) * 1000)
            return result

        result["ready"] = True

        # 4) 快速推理：合成行情，不依赖 MT5
        try:
            last_price = self._snapshot.get("last_price") or 4300.0
            t = np.linspace(0, 4 * np.pi, CTX)
            noise = np.random.normal(0, 0.5, CTX)
            ctx = last_price + 5 * np.sin(t) + noise

            with ThreadPoolExecutor(max_workers=1) as exe:
                fut = exe.submit(p.predict_detail, ctx, HORIZON)
                d = fut.result(timeout=35)
            result["inference_ok"] = True
            result["direction"] = d.direction
            result["pred_end"] = round(float(d.pred_end), 2) if d.pred_end else None
            result["confidence"] = round(float(d.confidence), 4) if d.confidence else None
        except FutureTimeout:
            result["error"] = "推理超时（模型加载或推理阻塞超过 35 秒）"
        except Exception as e:  # noqa: BLE001
            result["error"] = f"推理异常: {type(e).__name__}: {str(e)[:160]}"

        result["available"] = result["ready"] and result["inference_ok"]
        result["latency_ms"] = round((time.time() - start) * 1000)
        return result


_service = None


def get_service() -> TSReferenceService:
    global _service
    if _service is None:
        _service = TSReferenceService()
    return _service
