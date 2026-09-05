# -*- coding: utf-8 -*-
"""
万象Ai · 置信校准层（提准非拦截）

═══════════════════════════════════════════════════════════════════════════
问题（调研实证，≥3 独立来源交叉验证）
───────────────────────────────────────────────────────────────────────────
大语言模型集成输出的「自报置信度」系统性过自信——说 80% 时真实命中率常只有 ~60%
（PredictEngine《Best Practices for LLM-Powered Trade Signals》明确：
 "Raw LLM outputs are almost always overconfident. You need a calibration layer"；
 dev.to Polymarket 研究："when an LLM says 80%, the true frequency is often ~60%"；
 paperswithbacktest LoRA 去偏：过自信在 0.6–0.9 区间最严重，导致系统性超配仓+回撤放大）。

对本系统的连锁后果：
  「逆共识高置信闸门」「新闻感知闸门」都以 final_confidence 作阈值判定。
  若 LLM 虚高置信，本该被降级到共识方向的弱逆共识单被放行 → 拖累信号准度
  （正是上一轮大脑审计「发现1」：逆共识单胜率 51% < 共识单 56%）。

方法（与文献一致的后处理校准层 post-hoc calibration）
───────────────────────────────────────────────────────────────────────────
把「自报置信度」映射为「历史观测命中率」：
  · Platt Scaling ：p=sigmoid(a·logit(p_raw)+b)，2 参数、数据高效、小样本稳
                     （KDnuggets：小校准集/需塞进现有管线时首选）。
  · Isotonic Reg.  ：PAVA 保序回归，分段常数单调映射；数据充足时 ECE/Brier 更优
                     （KDnuggets 实证 Isotonic 显著优于 Platt；本系统 2700+ 样本→适用）。
  选哪个：在「时间切分」校准集上用 Brier Score 选优（ai-prediction.info、predictengine
       均建议用独立集 + Brier 评估，避免前视），最终在全量数据上 refit 部署。

数据流（不污染主链路、零额外推理成本、可移植）
───────────────────────────────────────────────────────────────────────────
  · 离线脚本 scripts/calibrate_confidence.py 读生产库 wx_prod.dat
    （meta_agent_decision + net_profit），拟合后写出 data/confidence_calibration.json。
  · meta_agent 运行期**仅加载 JSON 做查表映射**（轻量、无 DB 访问、无锁风险）；
    若 JSON 缺失则原样透传（calibrated==raw），绝不拖垮交易。
  · 校准后置信度用于「逆共识闸门 / 新闻闸门」阈值判定（让 0.80 真正表示 80% 命中率）；
    不改变已开仓笔数（降级仍保留交易、只改方向到共识），默认不改变仓位大小
    （CONFIDENCE_CALIBRATION_AFFECTS_SIZING=False，避免净利意外波动，守住及格线）。

校准质量指标：ECE（预期校准误差）、Brier Score，写进 JSON 与大脑审计，供闭环验证。
"""
from __future__ import annotations

import json
import os
import math
import bisect
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.abspath(__file__))   # .../app/core
# 上溯两级到 backend，再进 data（与 brain_audit.py / 运行后端同目录）：
#   core -> app -> backend，故用 "..", ".."
_DATA = os.path.abspath(os.path.join(_BASE, "..", "..", "data"))
DEFAULT_CACHE = os.path.join(_DATA, "confidence_calibration.json")


# ───────────────────────── 纯函数工具 ─────────────────────────
def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1.0 - p))


def pava(xs: list, ys: list):
    """Pool Adjacent Violators Algorithm：保序回归。
    返回 (sorted_x, calibrated_y) 阶梯函数；x 代表值取块内加权平均。"""
    pairs = sorted((float(x), float(y)) for x, y in zip(xs, ys))
    blocks = []  # 每个块: [x_sum, y_sum, count, x_rep]
    for x, y in pairs:
        blocks.append([x, y, 1, x])
        # 若当前块均值 < 前一块均值 → 违反单调性，合并
        while len(blocks) >= 2 and (blocks[-1][1] / blocks[-1][2]) < (blocks[-2][1] / blocks[-2][2]):
            b2 = blocks.pop()
            b1 = blocks[-1]
            b1[0] += b2[0]
            b1[1] += b2[1]
            b1[2] += b2[2]
            b1[3] = b1[0] / b1[2]
    out_x = [b[3] for b in blocks]
    out_y = [b[1] / b[2] for b in blocks]
    return out_x, out_y


def fit_platt(xs: list, ys: list):
    """逻辑回归拟合 (a, b)：p=sigmoid(a·logit(x)+b)。小批量梯度下降（纯 stdlib）。"""
    a, b = 1.0, 0.0
    lr = 0.4
    n = max(1, len(xs))
    for _ in range(500):
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            z = a * _logit(x) + b
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - y
            ga += err * _logit(x)
            gb += err
        ga /= n
        gb /= n
        a -= lr * ga
        b -= lr * gb
        if abs(ga) < 1e-6 and abs(gb) < 1e-6:
            break
    return a, b


def predict_platt(a: float, b: float, x: float) -> float:
    return 1.0 / (1.0 + math.exp(-(a * _logit(x) + b)))


def brier(ys_true: list, ys_pred: list) -> float:
    if not ys_true:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(ys_pred, ys_true)) / len(ys_true)


def ece(ys_true: list, ys_pred: list, bins: int = 10) -> float:
    """等频分箱 ECE（度量「自报概率」与「实际频率」的偏差）。"""
    n = len(ys_true)
    if n == 0:
        return 0.0
    order = sorted(range(n), key=lambda i: ys_pred[i])
    chunk = max(1, n // bins)
    err = 0.0
    s = 0
    while s < n:
        e = min(s + chunk, n)
        if e <= s:
            break
        idx = order[s:e]
        pred_mean = sum(ys_pred[i] for i in idx) / (e - s)
        obs_mean = sum(ys_true[i] for i in idx) / (e - s)
        err += ((e - s) / n) * abs(pred_mean - obs_mean)
        s = e
    return err


# ───────────────────────── 校准器 ─────────────────────────
class ConfidenceCalibrator:
    def __init__(self, cache_path: str = DEFAULT_CACHE, method: str = "auto"):
        self.cache_path = cache_path
        self.method = method  # 'auto' | 'platt' | 'isotonic'
        self._available = False
        self._params = None  # platt: [a,b]；isotonic: [xs, ys]
        self._chosen = None
        self._n = 0
        self._test_brier = None
        self._test_ece = None
        self.load()

    # ── 运行期主路径：仅加载预计算映射（零 DB 访问）──
    def load(self) -> bool:
        try:
            if not os.path.exists(self.cache_path):
                return False
            with open(self.cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._chosen = d.get("method")
            self._params = d.get("params")
            self._n = d.get("n", 0)
            self._test_brier = d.get("test_brier")
            self._test_ece = d.get("test_ece")
            self._available = bool(self._params and self._chosen)
            return self._available
        except Exception:
            self._available = False
            return False

    def available(self) -> bool:
        return self._available

    # ── 离线拟合（scripts/calibrate_confidence.py 调用）──
    def fit(self, samples: list, min_samples: int = 60, test_frac: float = 0.30) -> bool:
        """samples: list[(raw_conf, outcome 0/1)]，按时间升序（最旧在前）。"""
        if len(samples) < min_samples:
            self._available = False
            return False
        xs = [float(s[0]) for s in samples]
        ys = [float(s[1]) for s in samples]

        # 时间切分（避免前视；金融非平稳，stockalpha.ai 建议时间切分/滚动窗口）
        k = int(len(samples) * (1 - test_frac))
        k = max(min_samples // 2, min(k, len(samples) - min_samples // 2))
        tr_x, tr_y = xs[:k], ys[:k]
        te_x, te_y = xs[k:], ys[k:]
        if len(te_x) < 10:  # 样本不足则全量自测
            tr_x, tr_y, te_x, te_y = xs, ys, xs, ys

        pa, pb = fit_platt(tr_x, tr_y)
        ix, iy = pava(tr_x, tr_y)
        platt_pred = [predict_platt(pa, pb, x) for x in te_x]
        iso_pred = [self._iso_lookup(ix, iy, x) for x in te_x]
        b_platt = brier(te_y, platt_pred)
        b_iso = brier(te_y, iso_pred)

        if self.method == "platt":
            chosen, params = "platt", [pa, pb]
        elif self.method == "isotonic":
            chosen, params = "isotonic", [ix, iy]
        else:  # auto：Brier 选优
            if b_iso <= b_platt:
                chosen, params = "isotonic", [ix, iy]
            else:
                chosen, params = "platt", [pa, pb]

        # 全量 refit 部署
        if chosen == "isotonic":
            fx, fy = pava(xs, ys)
            self._params = [fx, fy]
        else:
            fa, fb = fit_platt(xs, ys)
            self._params = [fa, fb]

        self._chosen = chosen
        self._n = len(samples)
        self._test_brier = round(min(b_platt, b_iso), 4)
        self._test_ece = round(ece(te_y, (iso_pred if chosen == "isotonic" else platt_pred)), 4)
        self._available = True
        self.save()
        return True

    @staticmethod
    def _iso_lookup(xs: list, ys: list, x: float) -> float:
        """左阶查表（过自信时取更低校准值 → 偏保守 → 提准）。"""
        i = bisect.bisect_right(xs, x)
        if i == 0:
            return ys[0]
        if i >= len(xs):
            return ys[-1]
        return ys[i - 1]

    def calibrate(self, raw_conf) -> float:
        if not self._available or not self._params:
            return float(raw_conf)
        try:
            raw_conf = float(raw_conf)
        except Exception:
            return float(raw_conf)
        if self._chosen == "platt":
            a, b = self._params
            return float(predict_platt(a, b, raw_conf))
        xs, ys = self._params
        return float(self._iso_lookup(xs, ys, raw_conf))

    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            d = {
                "method": self._chosen,
                "params": self._params,
                "n": self._n,
                "test_brier": self._test_brier,
                "test_ece": self._test_ece,
                "fitted_at": datetime.now(timezone.utc).isoformat(),
                "note": "万象Ai 置信校准映射：raw meta_agent_confidence -> 历史观测命中率",
            }
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def report(self) -> dict:
        return {
            "available": self._available,
            "method": self._chosen,
            "n_samples": self._n,
            "test_brier": self._test_brier,
            "test_ece": self._test_ece,
            "cache_path": self.cache_path,
        }
