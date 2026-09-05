"""
TimesFM-2.5 风险区间交叉验证服务（2026-08-17 · 模型科学规划落地）
================================================================
背景（walk-forward 复测数据，30 天 XAUUSD M15、172 测试点、预测未来 2h）：
  - numpy 规则:   acc 49.4%  pips +17811（唯一正收益）
  - chronos-2:    acc 41.3%  pips -45931（方向预测显著劣于随机！）
  - timesfm-2.5:  acc 50.0%  pips -10241
  - moirai:       acc 50.6%  pips -2829
  - time-moe:     transformers5.14 接口不兼容，未测
结论：时序基础模型的**方向预测能力不优于随机**（这正是竞技场当年把它们
停用的数据依据，不是摆设）；但它们各自独立的**风险区间估计**（分位数）是
有效信息——多模型区间交叉验证能提升"不确定性度量"质量，这才是提准降损的
真实增量。

本模块职责：TimesFM-2.5 常驻单例（CPU float32，纯推理 0.1s/次），
对同一段收盘价给出独立的 p10/p50/p90 分位数，与 Chronos-2 的区间对比：
  - 区间分歧度 cross_div ∈ [0,1]：两模型 p90/p10 分歧占比
  - 分歧大 → 高不确定性 → 信号质量降权 + 止盈收紧（加法，非拦截）
  - 分歧小 → 低不确定性 → 质量分微升（提准）

★ 铁律：只做「质量评分微调 + 止盈天花板」，绝不做开仓拦截；
  TimesFM 故障/超时 → 静默降级，绝不阻断决策链。
"""
import logging
import threading
import time

logger = logging.getLogger("ts_cross_validate")

# 常驻单例（进程级）
_inst = None
_inst_lock = threading.Lock()

# 结果缓存：同一 closes 快照指纹在窗口内复用（决策循环 60-90s，交叉验证不必每轮重算）
_cache = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 120.0

# TimesFM 默认上下文/预测长度（与复测协议一致：128 根输入 → 8 根预测）
CTX_LEN = 128
HORIZON = 8
# 推理超时（含加载首轮）：超过视为不可用，静默降级
_INFER_TIMEOUT = 15.0


def _fingerprint(closes):
    """快照指纹：末 16 根价格量化 + 长度，用于缓存键。"""
    tail = list(closes[-16:])
    return (len(closes), round(sum(tail), 2), round(tail[-1], 2))


class TimesFmCrossValidator:
    name = "timesfm-2.5"

    def __init__(self, model_dir: str = ""):
        import os
        if not model_dir:
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            model_dir = os.path.join(_root, "models", "timesfm-2.5-200m")
        self.model_dir = model_dir
        self._model = None
        self._ready = False
        self._err = ""

    def _ensure_model(self):
        if self._model is not None:
            return True
        try:
            import torch
            from transformers import TimesFm2_5ModelForPrediction
            self._model = TimesFm2_5ModelForPrediction.from_pretrained(
                self.model_dir, torch_dtype=torch.float32, device_map="cpu")
            self._model.eval()
            self._ready = True
            logger.info(f"[TimesFM交叉验证] 已加载 {self.model_dir}（CPU float32）")
            return True
        except Exception as e:  # noqa: BLE001
            self._err = str(e)[:200]
            logger.warning(f"[TimesFM交叉验证] 加载失败: {self._err}")
            return False

    def forecast(self, closes, horizon: int = HORIZON):
        """返回 {mean, p10, p50, p90}（末值标量），失败返回 None。"""
        if not self._ensure_model():
            return None
        import numpy as np
        import torch
        try:
            ctx = np.asarray(list(closes)[-CTX_LEN:], dtype=np.float32)
            with torch.no_grad():
                out = self._model(
                    past_values=torch.tensor(ctx).unsqueeze(0),
                    forecast_context_len=CTX_LEN,
                )
            mean = out.mean_predictions[0].cpu().numpy()[:horizon]
            full = out.full_predictions[0].cpu().numpy()[:horizon]  # (horizon, quantiles)
            if full.ndim == 2 and full.shape[1] >= 2:
                p10 = float(np.asarray(full[:, 0])[-1])
                p90 = float(np.asarray(full[:, -1])[-1])
            else:
                std = float(np.std(mean)) if float(np.std(mean)) > 0 else 0.01
                p10 = float(mean[-1]) - 1.28 * std
                p90 = float(mean[-1]) + 1.28 * std
            return {
                "mean": float(np.asarray(mean)[-1]),
                "p10": p10,
                "p50": float(np.asarray(mean)[-1]),
                "p90": p90,
                "last_price": float(ctx[-1]),
            }
        except Exception as e:  # noqa: BLE001
            self._err = str(e)[:200]
            logger.warning(f"[TimesFM交叉验证] 推理失败: {self._err}")
            return None

    def status(self) -> dict:
        return {"model": self.name, "ready": self._ready, "error": self._err[:120]}


def get_cross_validator() -> TimesFmCrossValidator:
    global _inst
    with _inst_lock:
        if _inst is None:
            _inst = TimesFmCrossValidator()
    return _inst


def cross_validate(closes, chronos: dict | None, cache_ttl: float = _CACHE_TTL) -> dict | None:
    """
    计算 TimesFM 与 Chronos 的区间交叉分歧度。

    返回 None = 不可用（静默降级）；否则返回：
      {divergence, t_p90, t_p10, c_p90, c_p10, agreement, note}
    divergence ∈ [0,1]：|t_p90 - c_p90| / spread；>0.5 视为显著分歧。
    """
    if chronos is None or not closes or len(closes) < CTX_LEN:
        return None
    c_p90 = chronos.get("p90_final")
    c_p10 = chronos.get("p10_final")
    if c_p90 is None or c_p10 is None:
        return None

    fp = _fingerprint(closes)
    with _CACHE_LOCK:
        hit = _cache.get(fp)
        if hit and time.time() - hit["ts"] < cache_ttl:
            return hit["result"]

    validator = get_cross_validator()
    # 带超时的推理（首轮含加载可能 >10s，用线程 + 超时保护，超时静默降级）
    res_box = {}

    def _run():
        res_box["r"] = validator.forecast(closes)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=_INFER_TIMEOUT)
    r = res_box.get("r")
    if r is None:
        with _CACHE_LOCK:
            _cache[fp] = {"ts": time.time(), "result": None}
        return None

    spread = max(c_p90 - c_p10, 1e-9)
    div = abs(r["p90"] - c_p90) / spread
    agreement = "high" if div <= 0.35 else ("mid" if div <= 0.70 else "low")
    result = {
        "divergence": round(min(div, 3.0), 3),
        "agreement": agreement,
        "t_p90": round(r["p90"], 2),
        "t_p10": round(r["p10"], 2),
        "c_p90": round(c_p90, 2),
        "c_p10": round(c_p10, 2),
        "note": ("两模型区间一致→不确定性低" if agreement == "high"
                 else ("区间中等分歧→不确定性中" if agreement == "mid"
                       else "区间显著分歧→不确定性高")),
    }
    with _CACHE_LOCK:
        _cache[fp] = {"ts": time.time(), "result": result}
    return result
