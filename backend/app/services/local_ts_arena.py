"""
本地时序模型竞技场（Local Time-Series Arena）。

设计目标：
1. 把多个本地时序基础模型（Chronos-2 / TimesFM 2.5 / Time-MoE / Moirai-MoE）
   封装成统一接口，未来可一键替换/扩展；
2. 每个模型按需加载、独立可用，避免 8GB 显存同时被四个模型占满；
3. 提供与当前 `NumpyDirectionGuard` 一致的 `review()` 输出契约，
   使 debate_engine 中的方向终审器可无缝从"numpy 规则版"切换到"真实模型版"；
4. 当前工作机沙箱无法运行 PyTorch，因此本模块在沙箱内只注册接口、不做真实推理；
   在可跑 PyTorch 的部署机上自动启用。

使用方式：
    arena = TSArena()
    result = arena.review(closes, current_price, proposed_direction)
    # result 字段与 NumpyDirectionGuard 完全一致。
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from app.services.numpy_direction_guard import DirectionGuardResult, NumpyDirectionGuard


# 模型本地缓存目录（相对项目根）
MODEL_ROOT = Path(__file__).resolve().parents[2] / "models"


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class TSBackend(ABC):
    """单个本地时序模型的统一接口。"""

    name: str = ""
    weight: float = 1.0  # 在竞技场中的投票权重

    @abstractmethod
    def available(self) -> bool:
        """当前环境是否能加载并运行该模型。"""
        ...

    @abstractmethod
    def load(self) -> None:
        """加载模型权重到内存。按需调用，失败必须抛异常。"""
        ...

    def loaded(self) -> bool:
        """默认实现：子类可覆盖，返回模型是否已加载。"""
        return False

    @abstractmethod
    def forecast(self, closes: Sequence[float], horizon: int = 12) -> Optional[Dict[str, np.ndarray]]:
        """
        返回预测结果。

        返回字典约定：
        {
            "mean": np.ndarray[horizon],      # 点预测
            "p10":  np.ndarray[horizon],      # 10% 分位
            "p50":  np.ndarray[horizon],       # 中位
            "p90":  np.ndarray[horizon],       # 90% 分位
        }
        """
        ...

    def direction_signal(
        self,
        closes: Sequence[float],
        current_price: float,
        horizon: int = 12,
    ) -> Optional[DirectionGuardResult]:
        """
        把预测结果转换为统一的 DirectionGuardResult。

        默认实现：
        - 预测均值高于当前价一定比例 → 看多；
        - 低于一定比例 → 看空；
        - 各周期方向一致且极端 → 给出冲突等级；
        子类可覆盖以利用各自分位信息。
        """
        if not self.available():
            return None
        pred = self.forecast(closes, horizon)
        if pred is None or "mean" not in pred or pred["mean"] is None:
            return None
        mean = pred["mean"]
        if len(mean) == 0:
            return None
        future_mean = float(np.mean(mean))
        diff_pct = (future_mean - current_price) / current_price * 100.0
        # 阈值：XAUUSD 1% 以上才视为有效方向信号
        if diff_pct > 0.3:
            score = min(diff_pct / 2.0, 1.0)
            return DirectionGuardResult(
                direction_score=round(score, 4),
                conflict_level="none",
                reason=f"{self.name} 预测未来 {horizon} 根均值向上 {diff_pct:.2f}%",
                suggested_direction="BUY",
            )
        elif diff_pct < -0.3:
            score = max(diff_pct / 2.0, -1.0)
            return DirectionGuardResult(
                direction_score=round(score, 4),
                conflict_level="none",
                reason=f"{self.name} 预测未来 {horizon} 根均值向下 {abs(diff_pct):.2f}%",
                suggested_direction="SELL",
            )
        return DirectionGuardResult(
            direction_score=round(diff_pct / 2.0, 4),
            conflict_level="none",
            reason=f"{self.name} 预测未来 {horizon} 根均值变化 {diff_pct:.2f}%，方向不明",
            suggested_direction="HOLD",
        )


# ---------------------------------------------------------------------------
# Chronos-2 后端
# ---------------------------------------------------------------------------
class ChronosBackend(TSBackend):
    """Amazon Chronos-2 120M 多变量时序模型。"""

    name = "chronos-2"
    weight = 1.0

    def __init__(self, model_dir: Optional[Path] = None):
        self.model_dir = model_dir or MODEL_ROOT / "chronos-2"
        self._pipe = None

    def available(self) -> bool:
        """检测 PyTorch 是否可导入；若不可导入则直接不可用。"""
        try:
            import torch  # noqa: F401
            from chronos import ChronosPipeline  # noqa: F401
            return self.model_dir.exists()
        except Exception:
            return False

    def load(self) -> None:
        if not self.available():
            raise RuntimeError("Chronos-2 当前环境不可用")
        import torch
        from chronos import ChronosPipeline

        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._pipe = ChronosPipeline.from_pretrained(
            str(self.model_dir),
            device_map=device,
            torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
        )

    def loaded(self) -> bool:
        return self._pipe is not None

    def forecast(self, closes: Sequence[float], horizon: int = 12) -> Optional[Dict[str, np.ndarray]]:
        if self._pipe is None:
            self.load()
        import torch

        context = np.asarray(closes, dtype=np.float32)
        with torch.no_grad():
            quantiles, _ = self._pipe.predict_quantiles(
                context=context,
                prediction_length=horizon,
                quantile_levels=[0.1, 0.5, 0.9],
            )
        return {
            "p10": quantiles[0].cpu().numpy(),
            "p50": quantiles[1].cpu().numpy(),
            "p90": quantiles[2].cpu().numpy(),
            "mean": quantiles[1].cpu().numpy(),  # 用 p50 近似均值
        }


# ---------------------------------------------------------------------------
# TimesFM 2.5 后端
# ---------------------------------------------------------------------------
class TimesFMBackend(TSBackend):
    """Google TimesFM 2.5 200M。"""

    name = "timesfm-2.5"
    weight = 1.0

    def __init__(self, repo_id: str = "google/timesfm-2.5-200m-transformers"):
        self.repo_id = repo_id
        self._model = None
        self._tokenizer = None

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            from transformers import TimesFm2_5ModelForPrediction  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        if not self.available():
            raise RuntimeError("TimesFM 2.5 当前环境不可用")
        import torch
        from transformers import TimesFm2_5ModelForPrediction

        self._model = TimesFm2_5ModelForPrediction.from_pretrained(
            self.repo_id,
            torch_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            device_map="auto",
        )
        self._model.eval()

    def loaded(self) -> bool:
        return self._model is not None

    def forecast(self, closes: Sequence[float], horizon: int = 12) -> Optional[Dict[str, np.ndarray]]:
        if self._model is None:
            self.load()
        import torch

        context = np.asarray(closes, dtype=np.float32)
        with torch.no_grad():
            outputs = self._model(
                past_values=context,
                forecast_context_len=min(len(context), 1024),
            )
        mean = outputs.mean_predictions[0].cpu().numpy()[:horizon]
        full = outputs.full_predictions[0].cpu().numpy()[:horizon]
        # full 形状通常为 (horizon, quantiles)，取首尾作为 p10/p90
        if full.ndim == 2 and full.shape[1] >= 2:
            p10 = full[:, 0]
            p90 = full[:, -1]
        else:
            std = np.std(full) if np.std(full) > 0 else np.std(closes) * 0.05
            p10 = mean - 1.28 * std
            p90 = mean + 1.28 * std
        return {
            "mean": mean,
            "p10": p10,
            "p50": mean,
            "p90": p90,
        }


# ---------------------------------------------------------------------------
# Time-MoE 后端（占位）
# ---------------------------------------------------------------------------
class TimeMoeBackend(TSBackend):
    """Time-MoE 混合专家时序模型（90M~2B）。"""

    name = "time-moe"
    weight = 1.0

    def __init__(self, repo_id: str = "time-moe/time-moe-90m"):
        self.repo_id = repo_id
        self._model = None

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            from transformers import AutoModelForTimeSeriesForecasting  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        raise NotImplementedError("Time-MoE 后端待按实际仓库 API 实现")

    def forecast(self, closes: Sequence[float], horizon: int = 12) -> Optional[Dict[str, np.ndarray]]:
        raise NotImplementedError("Time-MoE 后端待按实际仓库 API 实现")


# ---------------------------------------------------------------------------
# Moirai-MoE 后端（占位）
# ---------------------------------------------------------------------------
class MoiraiBackend(TSBackend):
    """Salesforce Moirai-MoE。"""

    name = "moirai-moe"
    weight = 1.0

    def __init__(self, repo_id: str = "Salesforce/moirai-moe-1.0-R"):
        self.repo_id = repo_id
        self._model = None

    def available(self) -> bool:
        try:
            import torch  # noqa: F401
            return True
        except Exception:
            return False

    def load(self) -> None:
        raise NotImplementedError("Moirai-MoE 后端待按实际仓库 API 实现")

    def forecast(self, closes: Sequence[float], horizon: int = 12) -> Optional[Dict[str, np.ndarray]]:
        raise NotImplementedError("Moirai-MoE 后端待按实际仓库 API 实现")


# ---------------------------------------------------------------------------
# 竞技场聚合器
# ---------------------------------------------------------------------------
class TSArena:
    """
    多模型时序竞技场。

    策略：
    - 默认启用 NumpyDirectionGuard 作为 fail-open 兜底；
    - 在配置中显式启用的真实模型（chronos-2 / timesfm-2.5 / time-moe / moirai）
      才尝试加载，失败不阻断；
    - 各模型输出按 `direction_score` 加权投票，给出统一结果；
    - 当前设计为"轻量评委"，不是独立信号源：major 冲突 → 建议 HOLD。
    """

    DEFAULT_ENABLED = ["numpy"]

    def __init__(self, enabled: Optional[List[str]] = None):
        self._numpy = NumpyDirectionGuard()
        self._backends: Dict[str, TSBackend] = {}
        self._enabled = enabled or self.DEFAULT_ENABLED
        self._register_backends()

    def _register_backends(self) -> None:
        """注册所有已知后端，但只在 enabled 列表里时才加载。"""
        all_backends: Dict[str, TSBackend] = {
            "chronos-2": ChronosBackend(),
            "timesfm-2.5": TimesFMBackend(),
            "time-moe": TimeMoeBackend(),
            "moirai": MoiraiBackend(),
        }
        for name in self._enabled:
            if name == "numpy":
                continue
            if name in all_backends:
                self._backends[name] = all_backends[name]

    def review(
        self,
        closes: Sequence[float],
        current_price: float,
        proposed_direction: str,
    ) -> DirectionGuardResult:
        """综合所有启用模型的输出，给出统一方向终审结果。"""
        t0 = time.time()
        votes: List[DirectionGuardResult] = []

        # 1. NumPy 规则兜底永远参与（无 PyTorch 依赖、零额外成本）
        numpy_res = self._numpy.review(closes, current_price, proposed_direction)
        votes.append(numpy_res)

        # 2. 真实时序模型按需投票
        for name, backend in self._backends.items():
            try:
                if backend.available():
                    res = backend.direction_signal(closes, current_price)
                    if res:
                        res.model = name
                        votes.append(res)
            except Exception:
                # 单个模型失败不拖垮其它投票
                continue

        # 3. 聚合：按 direction_score 加权
        if not votes:
            return DirectionGuardResult(
                direction_score=0.0,
                conflict_level="none",
                reason="无可用方向终审模型",
                suggested_direction=proposed_direction,
            )

        total_weight = 0.0
        weighted_score = 0.0
        max_conflict = numpy_res.conflict_level
        reason_parts: List[str] = []
        active_models: List[str] = []

        for v in votes:
            model_name = getattr(v, "model", "numpy")
            backend = self._backends.get(model_name)
            w = backend.weight if backend else 1.0
            weighted_score += v.direction_score * w
            total_weight += w
            if v.conflict_level == "major":
                max_conflict = "major"
            elif v.conflict_level == "minor" and max_conflict == "none":
                max_conflict = "minor"
            if v.reason and v.reason not in ("原本 HOLD，无需终审", "历史数据不足，跳过方向终审"):
                reason_parts.append(f"[{model_name}] {v.reason}")
            active_models.append(model_name)

        final_score = weighted_score / total_weight if total_weight > 0 else 0.0
        suggested = proposed_direction
        blocked = False
        if max_conflict == "major":
            suggested = "HOLD"
            blocked = True

        latency_ms = (time.time() - t0) * 1000.0
        return DirectionGuardResult(
            direction_score=round(final_score, 4),
            conflict_level=max_conflict,
            reason="; ".join(reason_parts) if reason_parts else "方向终审通过",
            features={
                "models": ",".join(active_models),
                "votes": len(votes),
                "latency_ms": round(latency_ms, 1),
            },
            suggested_direction=suggested,
            model="arena",
        )

    def status(self) -> Dict[str, any]:
        """返回竞技场各模型可用状态，供 health 端点展示。"""
        backends_status = {"numpy": {"available": True, "loaded": True}}
        for name, backend in self._backends.items():
            try:
                backends_status[name] = {
                    "available": backend.available(),
                    "loaded": backend.loaded(),
                }
            except Exception:
                backends_status[name] = {"available": False, "loaded": False}
        return {
            "enabled": self._enabled,
            "backends": backends_status,
        }
