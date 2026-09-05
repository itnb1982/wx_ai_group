# -*- coding: utf-8 -*-
"""本地时序模型「信号源参考」预测器集合 —— 仅供前端参考面板观测。

══════════════════════════════════════════════════════════════════════
★ 架构红线（用户铁律「提准非拦截」「参考面板·未接入系统」）：
   本模块与 ts_reference_service 绝不 import 以下任何决策链模块：
     app.core.debate_engine / app.core.meta_agent
     app.routers.trading / app.services.trade_executor
     app.services.risk_engine / app.services.smart_exit
     app.services.numpy_direction_guard / app.services.chronos_service
    （唯一例外：中立项 app.services.chronos_shared 是允许的共享 Chronos-2 加载器，
      非决策链、非参考面板，仅供两者复用同一份权重，消除双实例。）
   它只读取行情（MT5 或缓存）并跑推理，产出「仅供参考」的方向分。
   任何把这些分数接回交易闭环的代码，都是违反架构红线的 bug。
   （反向守卫见 tests/test_ts_reference_decoupled.py）
══════════════════════════════════════════════════════════════════════

所有预测器统一接口：
   ready()        -> bool      是否能加载权重（失败返回 False，绝不抛）
   predict(ctx,horizon) -> float   方向分 -1..+1（>0 看多，<0 看空，≈0 无观点）
   predict_detail(ctx,horizon) -> PredDetail   含方向/置信/预测价/区间

注意：参考服务强制 CPU 推理，避免与决策链里常驻 GPU 的 Chronos-2 抢显存。
"""
from __future__ import annotations

import os
import sys
import json
import time
import tempfile
from safe_fs import safe_remove  # 绕过 WorkBuddy shim：删除不进回收站
# 2026-08-12 Chronos 双实例合并：复用中立项 chronos_shared 的共享 Chronos-2 实例（CPU/float32）
from app.services.chronos_shared import get_chronos2_pipeline, predict_quantiles_safe
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 路径：本文件位于 backend/app/services/ts_reference_models.py
_BACKEND = Path(__file__).resolve().parents[2]      # .../backend
_ROOT = _BACKEND.parent                              # .../万象Ai(F:)
MODELS = os.path.join(_ROOT, "models")
_DATA = os.path.join(_BACKEND, "data")
os.makedirs(_DATA, exist_ok=True)
_RATES_CACHE = os.path.join(_DATA, "ts_ref_rates_cache.npz")

# 方向分阈值：|分| < 此值视为 HOLD（与竞技场回测台保持一致，保证口径统一）
DIRECTION_THRESHOLD = 0.15


@dataclass
class PredDetail:
    score: float
    direction: str            # BUY / SELL / HOLD
    confidence: float         # |score|，0..1
    pred_end: float | None    # 模型预测的未来 horizon 根末端价格
    last_price: float
    lo: float | None = None   # 预测下界（如 Chronos P10）
    hi: float | None = None   # 预测上界（如 Chronos P90）


def direction_from_score(score: float, thr: float = DIRECTION_THRESHOLD) -> str:
    if score > thr:
        return "BUY"
    if score < -thr:
        return "SELL"
    return "HOLD"


def _norm_score(pred_end: float, last: float, ctx: np.ndarray, horizon: int) -> float:
    """把「预测末端价 vs 现价」折成 -1..1 的方向分，按近端波动归一。"""
    vol = float(np.std(np.diff(ctx[-100:]))) or 1e-9
    return float(np.tanh((pred_end - last) / (vol * np.sqrt(horizon) + 1e-9)))


# ═══════════════════════════════════════════════════════════════════════
#  预测器基类
# ═══════════════════════════════════════════════════════════════════════
class Predictor:
    name = "base"

    def ready(self) -> bool:
        return False

    def predict(self, ctx: np.ndarray, horizon: int) -> float:
        try:
            return self.predict_detail(ctx, horizon).score
        except Exception:  # noqa: BLE001
            return 0.0

    def predict_detail(self, ctx: np.ndarray, horizon: int) -> PredDetail:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════════
#  Chronos-2（120M，已真接入决策链作风险区间；此处作「参考」独立复算）
# ═══════════════════════════════════════════════════════════════════════
class ChronosP(Predictor):
    name = "Chronos-2(120M)"

    def __init__(self):
        self.pipe = None

    def ready(self) -> bool:
        # 2026-08-12 合并：复用进程内唯一的 chronos_shared 共享实例（CPU/float32），
        # 不再自行加载第二份 Chronos-2。共享器内部已含子进程探针隔离与 CPU 强制。
        try:
            self.pipe = get_chronos2_pipeline()
            return self.pipe is not None
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] 不可用: {type(e).__name__}: {e}")
            return False

    def predict_detail(self, ctx, horizon):
        # 合并后走 chronos_shared 线程安全封装（持有共享锁，避免与决策链并发访问同一 pipeline）
        out = predict_quantiles_safe(
            inputs=[{"target": np.asarray(ctx, dtype=np.float32)}],
            prediction_length=horizon,
            quantile_levels=[0.1, 0.5, 0.9])
        if out is None:
            raise RuntimeError("chronos_shared 推理不可用")
        q, _ = out
        arr = q[0].float().cpu().numpy()      # (n_targets, horizon, n_q)
        if arr.ndim == 3:
            arr = arr[0]
        last = float(ctx[-1])
        p10 = float(arr[-1, 0])
        p50 = float(arr[-1, 1])
        p90 = float(arr[-1, 2])
        score = _norm_score(p50, last, ctx, horizon)
        return PredDetail(
            score=score, direction=direction_from_score(score),
            confidence=abs(score), pred_end=p50, last_price=last,
            lo=p10, hi=p90)


# ═══════════════════════════════════════════════════════════════════════
#  TimesFM-2.5（200M）
# ═══════════════════════════════════════════════════════════════════════
class TimesFMP(Predictor):
    name = "TimesFM-2.5(200M)"

    def __init__(self):
        self.m = None
        self.torch = None
        self.dev = "cpu"

    def ready(self) -> bool:
        try:
            import torch
            from transformers import TimesFm2_5ModelForPrediction
            d = os.path.join(MODELS, "timesfm-2.5-200m")
            if not os.path.isdir(d):
                return False
            self.torch = torch
            self.dev = "cpu"
            self.m = TimesFm2_5ModelForPrediction.from_pretrained(d).to(self.dev).eval()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] 不可用: {type(e).__name__}: {str(e)[:160]}")
            return False

    def predict_detail(self, ctx, horizon):
        torch = self.torch
        with torch.no_grad():
            x = torch.tensor(ctx, dtype=torch.float32, device=self.dev).unsqueeze(0)
            out = self.m(past_values=x)
            arr = out.mean_predictions.float().cpu().numpy().ravel()
        p_end = float(arr[min(horizon, len(arr)) - 1])
        last = float(ctx[-1])
        score = _norm_score(p_end, last, ctx, horizon)
        return PredDetail(
            score=score, direction=direction_from_score(score),
            confidence=abs(score), pred_end=p_end, last_price=last)


# ═══════════════════════════════════════════════════════════════════════
#  Time-MoE（200M，稀疏混合专家）
#
#  坑位备忘（与竞技场回测台一致，勿重蹈）：
#   1) 直接调 forward 一次出多步，别用 model.generate()（remote code 在 5.x 连环炸）。
#   2) 必须强制 fp32（bf16 在四位数价格上丢方向，静默）。
#   3) 加载后必须做 buffer 体检（RoPE inv_freq 是 non-persistent buffer，
#      5.x 惰性加载不填值 → 输出 NaN/噪声）。见 hf_lazy_buffer_fix。
#   4) 选头逻辑：传 >= horizon 的最小可用头，否则退化为 1 步头静默失准。
# ═══════════════════════════════════════════════════════════════════════
class TimeMoEP(Predictor):
    name = "Time-MoE(200M)"
    HEADS = (1, 8, 32, 64)

    def __init__(self):
        self.m = None
        self.torch = None
        self.dev = "cpu"

    def ready(self) -> bool:
        try:
            import torch
            from transformers import AutoModelForCausalLM
            d = os.path.join(MODELS, "timemoe-200m")
            if not os.path.exists(os.path.join(d, "config.json")):
                return False
            try:
                sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
                from app.services.timemoe_compat import apply_timemoe_compat
                apply_timemoe_compat()
            except Exception:  # noqa: BLE001
                pass
            self.torch = torch
            self.dev = "cpu"
            m = AutoModelForCausalLM.from_pretrained(d, trust_remote_code=True)
            m = m.to(device=self.dev, dtype=torch.float32).eval()
            from app.services.hf_lazy_buffer_fix import load_and_repair
            rep = load_and_repair(m, self.dev, torch.float32)
            if not rep["ok"]:
                print(f"[{self.name}] buffer 修复后仍异常，弃用")
                return False
            self.m = m
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] 不可用: {type(e).__name__}: {str(e)[:160]}")
            return False

    def _pick_head(self, horizon: int) -> int:
        for h in self.HEADS:
            if h >= horizon:
                return h
        return self.HEADS[-1]

    def predict_detail(self, ctx, horizon):
        torch = self.torch
        head = self._pick_head(horizon)
        with torch.no_grad():
            x = torch.tensor(ctx, dtype=torch.float32, device=self.dev).unsqueeze(0)
            mean = x.mean(dim=-1, keepdim=True)
            std = x.std(dim=-1, keepdim=True).clamp_min(1e-6)
            out = self.m(input_ids=(x - mean) / std,
                         max_horizon_length=head, use_cache=False)
            p = out.logits if hasattr(out, "logits") else out[0]
            fut = p[0, -1, :horizon] * std[0, 0] + mean[0, 0]
            arr = fut.float().cpu().numpy().ravel()
        p_end = float(arr[-1])
        last = float(ctx[-1])
        score = _norm_score(p_end, last, ctx, horizon)
        return PredDetail(
            score=score, direction=direction_from_score(score),
            confidence=abs(score), pred_end=p_end, last_price=last)


# ═══════════════════════════════════════════════════════════════════════
#  Moirai（447M）—— 隔离 venv 子进程调用，绝不污染本机生产 numpy
#
#  Moirai 依赖 uni2ts（需独立环境）。按用户铁律，安装到
#  {ROOT}/models/moirai_venv_py312（或兼容的 moirai_venv）这个独立 venv，
#  由本类通过子进程调用其 moirai_infer.py 推理。
#  生产环境（.venv）的 numpy/torch 完全不受影响。
#  若 venv 尚未装好，ready() 返回 False，面板显示「未安装·需独立环境」。
# ═══════════════════════════════════════════════════════════════════════
class MoiraiP(Predictor):
    name = "Moirai(447M)"

    # 优先使用 Python3.12 独立 venv（numpy 1.26 有 cp312 预编译包，无需 MSVC）。
    # 老路径 moirai_venv 保留兼容（若用户之前已建好且能跑）。
    VENV_CANDIDATES = ["moirai_venv_py312", "moirai_venv"]

    def __init__(self):
        self.venv_py = None
        self.script = None

    def _find_venv(self) -> str | None:
        script = os.path.join(os.path.dirname(__file__), "moirai_infer.py")
        for name in self.VENV_CANDIDATES:
            venv = os.path.join(MODELS, name)
            py = os.path.join(venv, "Scripts", "python.exe")
            if os.path.exists(py) and os.path.exists(script):
                return py
        return None

    def ready(self) -> bool:
        py = self._find_venv()
        if py is None:
            return False
        script = os.path.join(os.path.dirname(__file__), "moirai_infer.py")
        # 轻量校验：venv 内能否真正 import uni2ts。
        # uni2ts 锁 numpy~=1.26，而 numpy1.26 无 Python3.13 预编译包，
        # 在错误 Python 版本下这里就会失败 → 视为未就绪，面板显示「需独立 venv」，
        # 而不是每轮刷新都起一个必败的子进程。
        try:
            r = subprocess.run([py, "-c", "import uni2ts"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return False
        except Exception:  # noqa: BLE001
            return False
        self.venv_py = py
        self.script = script
        return True

    def unready_reason(self) -> str:
        """返回「为什么 Moirai 还没就绪」，给前端展示明确原因。"""
        script = os.path.join(os.path.dirname(__file__), "moirai_infer.py")
        if not os.path.exists(script):
            return "推理脚本缺失"
        py = self._find_venv()
        if py is None:
            return "未安装·需独立 venv（隔离 numpy/torch/uni2ts，不污染生产环境）"
        try:
            r = subprocess.run([py, "-c", "import uni2ts"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return ("venv 已建但 uni2ts 不可用：需 Python3.12 + numpy1.26 预编译包"
                        "（本机当前 3.13 无预编译包，源码编译需 MSVC）")
        except Exception as e:  # noqa: BLE001
            return "venv 校验异常：" + str(e)[:100]
        return "未就绪"

    def predict_detail(self, ctx, horizon):
        ctx_list = [float(x) for x in ctx]
        inf = tempfile.NamedTemporaryFile("w", suffix=".json",
                                          delete=False, encoding="utf-8")
        json.dump({"ctx": ctx_list, "horizon": int(horizon)}, inf)
        inf.close()
        outp = inf.name + ".out.json"
        try:
            # 参考服务必须在 60s 内给出结果：Moirai 正常推理只需数秒，
            # 但首次加载模型/本地权重可能耗时；若超时则让本轮参考面板显示
            # 该模型暂不可用，避免子进程挂起拖垮 uvicorn 健康探测。
            r = subprocess.run(
                [self.venv_py, self.script, inf.name, outp],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or not os.path.exists(outp):
                raise RuntimeError((r.stderr or "")[-300:])
            with open(outp, encoding="utf-8") as f:
                d = json.load(f)
            pred = float(d["pred_end"])
            last = float(ctx[-1])
            score = _norm_score(pred, last, ctx, horizon)
            lo = float(d["lo"]) if d.get("lo") is not None else None
            hi = float(d["hi"]) if d.get("hi") is not None else None
            return PredDetail(
                score=score, direction=direction_from_score(score),
                confidence=abs(score), pred_end=pred, last_price=last,
                lo=lo, hi=hi)
        finally:
            for fp in (inf.name, outp):
                try:
                    safe_remove(fp)
                except Exception:  # noqa: BLE001
                    pass


# ═══════════════════════════════════════════════════════════════════════
#  实时行情获取（只读，不影响交易 worker）
# ═══════════════════════════════════════════════════════════════════════
def load_live_rates(symbol: str = "XAUUSD", tf: str = "H1", bars: int = 320):
    """从 MT5 拉近期 K 线；失败回退到上次缓存（标记 live=False）。

    返回 (closes, highs, lows, live_flag)。
    注意：只做 mt5.initialize()（幂等、不干扰 worker），绝不调用 mt5.shutdown()，
    避免误关同一进程内交易 worker 的连接。
    """
    try:
        import MetaTrader5 as mt5
        TF = {
            "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
        }
        if not mt5.initialize():
            raise RuntimeError(f"MT5 初始化失败: {mt5.last_error()}")
        cands = [symbol, symbol + "m", symbol + ".", "XAUUSD", "GOLD"]
        rates = None
        used = None
        for s in cands:
            try:
                r = mt5.copy_rates_from_pos(s, TF[tf], 0, bars)
            except Exception:  # noqa: BLE001
                r = None
            if r is not None and len(r) > 100:
                rates, used = r, s
                break
        if rates is None:
            raise RuntimeError(f"取不到 K 线，尝试过: {cands}")
        closes = np.asarray([float(x["close"]) for x in rates], dtype=float)
        highs = np.asarray([float(x["high"]) for x in rates], dtype=float)
        lows = np.asarray([float(x["low"]) for x in rates], dtype=float)
        # 落缓存（下次 MT5 不可用时回退用），静默失败不影响主流程
        try:
            np.savez(_RATES_CACHE, closes=closes, highs=highs, lows=lows)
        except Exception:  # noqa: BLE001
            pass
        return closes, highs, lows, True
    except Exception as e:  # noqa: BLE001
        # 回退到缓存
        if os.path.exists(_RATES_CACHE):
            try:
                z = np.load(_RATES_CACHE)
                return (z["closes"], z["highs"], z["lows"], False)
            except Exception:  # noqa: BLE001
                pass
        # 连缓存都没有：抛，让上层标记 unavailable
        raise RuntimeError(f"实时行情不可用且无缓存: {type(e).__name__}: {str(e)[:120]}")
