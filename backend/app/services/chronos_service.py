"""
Chronos-2 本地时序预测引擎（纯本地 · 零 token 费 · 可离线 · 可动画）

★ 设计定位（v4 方案）：
  本地时序大脑——替代云 DeepSeek 的"价格预判"职能，提供未来价格分布的
  P10/P50/P90 分位数预测，作为：
    ① 动态止盈天花板（TP ceiling）：HIGH 质量信号时让利润奔跑到 P90；
    ② 本地信号质量陪审团的方向/不确定性输入（喂给 meta_quality）。

★ 2026-08-12 Chronos 双实例合并：
  本模块**不再自行加载** Chronos-2。模型权重由「中立项」app.services.chronos_shared
  以进程内唯一单例（CPU / float32）加载，决策链（本模块）与参考面板
  （ts_reference_models.ChronosP）共用同一份实例，彻底消除双实例。

★ 子进程探针隔离（根因/教训见 chronos_shared）：
  原生 torch 段错误绕过 Python 异常，必须由 chronos_shared 的子进程探针隔离。
  本模块**永不裸 import torch**——仅通过 chronos_shared 间接加载。

★ 硬件：RTX 3060 Ti 8GB 仅留给 Qwen3-8B；Chronos-2 与另 3 个 CPU 时序模型同跑 CPU。
"""
import os
import sys
import time
import logging
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from app.services.chronos_shared import (
    get_chronos2_pipeline,
    get_probe,
    predict_quantiles_safe,
    LOCAL_MODEL_DIR,
    probe_torch_usable,  # 向后兼容：原探针函数引用（实现已迁至 chronos_shared）
)

logger = logging.getLogger("chronos_service")
# 桥接 loguru（项目统一日志框架，落 wanxiang_backend_{pid}.log），
# 让"协变量预测成功"等可观测信号可见（标准库 logger 默认不落盘）。
try:
    from loguru import logger as loguru_logger
except Exception:  # noqa: BLE001
    loguru_logger = logger

CTX_LIMIT = 1024        # Chronos-2 上下文窗口上限 8192，取 1024 足够且省资源
DEFAULT_PRED_LEN = 24    # 默认预测未来 24 步（约 6 小时 @ M15）
QL = [0.1, 0.5, 0.9]     # 取 P10/P50/P90 三档（索引 0/1/2）


class ChronosEngine:
    """Chronos-2 本地推理单例（复用 chronos_shared 共享 pipeline）。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._pipe = None
        self._loaded = False
        self._load_error = None
        self._probe = None      # 子进程探针结果（供 status/前端展示真实降级原因）
        # ── 运行期实证指标 ──────────────────────────────────────
        # 前端「本地双核」面板此前只能显示"加载成功/失败"，无法回答"它到底有没有在干活"。
        # 没有这些计数，一个加载成功但每次推理都异常的引擎会被显示成"在岗"——那就是虚标。
        self._calls_ok = 0
        self._calls_fail = 0
        self._last_error = ""
        self._last_ok_ts = 0.0
        self._last_latency_ms = 0.0
        self._last_multivariate = None
        self._last_covariates = None
        # ★ Chronos 推理是同步计算，直接在 uvicorn 主事件循环里跑会阻塞所有 HTTP 请求
        # （含 /api/health 心跳）。用单线程池把推理 offload 出去，让事件循环保持响应。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chronos_infer")

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self) -> bool:
        """懒加载模型；返回是否可用。

        ★ 2026-08-12 合并：不再自行加载 GPU 实例，改为复用进程内唯一的
           chronos_shared 共享 Chronos-2（CPU / float32）单例。
           探针与加载已在 chronos_shared 内完成（含子进程隔离），本方法只取结果。
        """
        if self._loaded:
            return self._pipe is not None
        self._loaded = True   # 无论成败都只探/加载一次：降级是永久的，不重复 fork

        self._probe = get_probe()
        self._pipe = get_chronos2_pipeline()
        if self._pipe is None:
            # ★ 2026-08-17 透出真实降级原因（探针失败/模型目录缺失/加载异常），
            #   而不是统一吞成"共享实例不可用"——status 与前端可展示可诊断的真实原因。
            try:
                from app.services.chronos_shared import get_load_error
                _detail = get_load_error()
            except Exception:  # noqa: BLE001
                _detail = None
            self._load_error = (
                _detail or "chronos_shared 共享实例不可用 → 永久降级（决策回退 SMC/Regime）"
            )
            logger.warning(f"[Chronos] {self._load_error}")
            try:
                loguru_logger.warning(f"[Chronos] {self._load_error}")
            except Exception:  # noqa: BLE001
                pass
            return False

        logger.info("[Chronos] 复用 chronos_shared 共享 Chronos-2 实例（CPU）成功")
        return True

    @staticmethod
    def _pick_target_variate(qt, target_last):
        """从 (n_variates, horizon, n_q) 中选出 target(黄金) 对应的变体。

        Chronos-2 多变量输出含 target + 各协变量。黄金末价量级(~2000)远超
        DXY(~100)/US10Y(~4)/VIX(~15)，故选末价(P50 末步)数值最大的变体即黄金，
        避免取到协变量自身的预测（量纲不同会毁掉方向/天花板）。
        """
        import numpy as np
        if qt.shape[0] == 1:
            return qt[0]
        last_vals = qt[:, -1, 1].cpu().numpy().astype(float)  # 各变体 P50 末步
        idx = int(np.argmax(np.abs(last_vals)))
        logger.info(f"[Chronos] 多变量 n_variates={qt.shape[0]} 各变体末价={np.round(last_vals, 2).tolist()} → 选 target 变体 idx={idx}")
        return qt[idx]

    def forecast(self, close_prices, prediction_length=DEFAULT_PRED_LEN, num_samples=20, covariates=None):
        """
        输入 XAU 收盘价序列（任意长度，自动截断到上下文窗口），
        可选 covariates: dict{name: list[float]} 与 close_prices 同长度（DXY/US10Y/VIX 等）。
        返回未来分位数预测；失败返回 None（调用方降级）。

        ★ Chronos-2 API 关键点（与 Bolt 完全不同）：
          predict_quantiles 的 inputs 必须是「字典列表」——每个序列一个 dict，
          单目标用 {"target": np.ndarray}，协变量放 past_covariates={"DXY": arr, ...}。
          输出 quantiles[0] 形状 = (n_targets, horizon, n_q)，单目标即 (1, H, 3)。
        """
        if not self._ensure_loaded():
            return None
        _t_start = time.time()
        try:
            import torch
            import numpy as np

            ctx = np.asarray([float(x) for x in close_prices], dtype=np.float32)
            if len(ctx) > CTX_LIMIT:
                ctx = ctx[-CTX_LIMIT:]
            if len(ctx) < 8:
                return None
            last = float(ctx[-1])

            # 协变量对齐：截到与 ctx 等长，长度不足前向填充，空则剔除该键
            def _align(arr, n):
                a = np.asarray(arr, dtype=np.float32)
                if a.size == 0:
                    return None
                if len(a) > n:
                    return a[-n:]
                if len(a) < n:
                    return np.concatenate([np.full(n - len(a), a[0], dtype=np.float32), a])
                return a

            aligned_cov = None
            if covariates:
                aligned_cov = {}
                for k, v in covariates.items():
                    a = _align(v, len(ctx))
                    if a is not None:
                        aligned_cov[k] = a
                if not aligned_cov:
                    aligned_cov = None

            # Chronos-2 一律 dict 输入
            def _build_input(cov):
                item = {"target": ctx}
                if cov:
                    item["past_covariates"] = {k: np.asarray(v, dtype=np.float32) for k, v in cov.items()}
                return [item]

            used_cov_keys = None
            # ★ 合并后统一走 chronos_shared 的线程安全封装（持有共享锁，避免与参考面板并发）
            def _predict(inp):
                return predict_quantiles_safe(
                    inputs=inp,
                    prediction_length=prediction_length,
                    quantile_levels=QL,
                )

            if aligned_cov:
                try:
                    quantiles, _mean = _predict(_build_input(aligned_cov))
                    used_cov_keys = list(aligned_cov.keys())
                    logger.info(f"[Chronos] 协变量预测成功 → {used_cov_keys}")
                    loguru_logger.info(f"[Chronos] 多变量协变量预测成功 → {used_cov_keys} (multivariate=True)")
                except Exception as e2:  # noqa: BLE001
                    logger.warning(f"[Chronos] 协变量预测失败({e2})，回退单变量")
                    quantiles, _mean = _predict(_build_input(None))
                    used_cov_keys = None
            else:
                # 无协变量：直接单变量（不打"失败"警告，这是正常降级）
                quantiles, _mean = _predict(_build_input(None))
                loguru_logger.info("[Chronos] 单变量降级（无协变量注入）")

            qt = quantiles[0]
            vt = self._pick_target_variate(qt, last)
            p10_series = vt[:, 0].cpu().numpy().tolist()
            p50_series = vt[:, 1].cpu().numpy().tolist()
            p90_series = vt[:, 2].cpu().numpy().tolist()
            p10_f = float(p10_series[-1])
            p50_f = float(p50_series[-1])
            p90_f = float(p90_series[-1])
            multivariate = used_cov_keys is not None

            # 方向：P50 末步相对当前（1.5bp 死区，避免噪声）
            if p50_f > last * 1.00015:
                direction = "BUY"
            elif p50_f < last * 0.99985:
                direction = "SELL"
            else:
                direction = "NEUTRAL"

            # 不确定性：P90-P10 带宽 / 末价（越大越不确定）
            uncertainty = (p90_f - p10_f) / max(last, 1e-6)

            # 实证指标：只有真的把预测算出来了才计成功，供前端证明"它在干活"
            self._calls_ok += 1
            self._last_ok_ts = time.time()
            self._last_latency_ms = round((self._last_ok_ts - _t_start) * 1000, 1)
            self._last_multivariate = multivariate
            self._last_covariates = used_cov_keys

            try:
                from app.services.brain_audit import record as _ba_rec
                _ba_rec("chronos", "output",
                        input_fields={"close_len": len(close_prices), "covariates": used_cov_keys},
                        output={"direction": direction, "uncertainty": round(uncertainty, 4),
                                "p50_final": p50_f, "multivariate": multivariate},
                        adopted=1, consumer="meta_agent")
            except Exception:
                pass

            return {
                "p10": p10_series,
                "p50": p50_series,
                "p90": p90_series,
                "last_price": last,
                "p50_final": p50_f,
                "p90_final": p90_f,
                "p10_final": p10_f,
                "direction": direction,
                "uncertainty": uncertainty,
                "prediction_length": prediction_length,
                "multivariate": multivariate,
                "covariates": used_cov_keys,
            }
        except Exception as e:  # noqa: BLE001
            self._calls_fail += 1
            self._last_error = str(e)[:200]
            logger.warning(f"[Chronos] 推理失败 → 降级: {e}")
            try:
                from app.services.brain_audit import record as _ba_rec
                _ba_rec("chronos", "output", input_fields={"close_len": len(close_prices)},
                        output=None, adopted=0, consumer="meta_agent",
                        notes=f"推理失败降级: {str(e)[:80]}")
            except Exception:
                pass
            return None

    async def forecast_async(self, close_prices, prediction_length=DEFAULT_PRED_LEN, num_samples=20, covariates=None):
        """异步包装：把同步推理 offload 到线程池，不阻塞 uvicorn 事件循环。

        交易循环（后台独立线程）仍用同步 forecast()；HTTP 请求路径改走本方法。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self.forecast,
            close_prices,
            prediction_length,
            num_samples,
            covariates,
        )

    @property
    def status(self) -> dict:
        """供前端/健康检查查询引擎状态。

        ★ 合并后 Chronos-2 强制 CPU，cuda_available 恒 False，device 显示「CPU（共享）」；
           probe 结果直接取自 chronos_shared（同样是子进程隔离，无原生段错误风险）。
        """
        probe = getattr(self, "_probe", None)
        loaded = self._pipe is not None
        return {
            "loaded": loaded,
            "available": loaded,
            "initialized": bool(self._loaded),   # 是否已尝试过加载（懒加载未触发时为 False）
            "model_dir": LOCAL_MODEL_DIR,
            "model_type": "Chronos-2",
            "load_error": self._load_error,
            # 合并后 Chronos-2 强制 CPU（GPU 仅留给 Qwen3-8B），故 cuda 恒 False
            "cuda_available": False,
            "device": "CPU（共享）" if loaded else "—",
            "probe_ok": bool(probe.get("ok")) if probe else False,
            "probe": probe,
            # 运行期实证：证明"在岗"不靠加载成功，靠真的产出过预测
            "calls_ok": self._calls_ok,
            "calls_fail": self._calls_fail,
            "last_error": self._last_error,
            "last_latency_ms": self._last_latency_ms,
            "last_ok_ago_s": (round(time.time() - self._last_ok_ts, 1)
                              if self._last_ok_ts else None),
            "last_multivariate": self._last_multivariate,
            "last_covariates": self._last_covariates,
        }


def get_chronos() -> "ChronosEngine":
    return ChronosEngine.get()
