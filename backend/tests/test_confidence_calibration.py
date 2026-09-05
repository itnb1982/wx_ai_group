# -*- coding: utf-8 -*-
"""置信校准层单元测试（纯 stdlib，无需 MT5 / numpy）"""
import os
import tempfile
import math
import random
import pytest

from app.core.confidence_calibrator import (
    pava, fit_platt, predict_platt, brier, ece, ConfidenceCalibrator,
)


def test_pava_monotonic_and_keys():
    xs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    # 故意构造一个非单调的 y，PAVA 应输出非递减
    ys = [0.1, 0.5, 0.3, 0.6, 0.4, 0.7, 0.5, 0.8, 0.6]
    ox, oy = pava(xs, ys)
    assert len(ox) == len(oy)
    for i in range(1, len(oy)):
        assert oy[i] >= oy[i - 1] - 1e-9, "PAVA 输出应单调非递减"


def test_platt_roundtrip():
    # 生成 logit 线性可分的样本，Platt 应能较好拟合
    random.seed(0)
    xs, ys = [], []
    for _ in range(200):
        x = random.uniform(0.05, 0.95)
        p = 1.0 / (1.0 + math.exp(-(1.5 * __import__("math").log(x / (1 - x)) + 0.2)))
        y = 1.0 if random.random() < p else 0.0
        xs.append(x)
        ys.append(y)
    a, b = fit_platt(xs, ys)
    preds = [predict_platt(a, b, x) for x in xs]
    # 拟合后 Brier 应明显低于「全预测 0.5」（即学到结构）
    assert brier(ys, preds) < brier(ys, [0.5] * len(ys))


def _make_overconfident_samples(n=400, seed=42):
    """自报高置信但真实命中率偏低（过自信）的样本，按"时间"升序。"""
    random.seed(seed)
    samples = []
    for i in range(n):
        raw = random.uniform(0.4, 0.98)
        # 真实命中率 = raw^2（高 raw 时严重过自信）
        true_p = max(0.05, min(0.95, raw ** 2))
        y = 1.0 if random.random() < true_p else 0.0
        samples.append((raw, y))
    return samples


def test_calibration_improves_brier():
    samples = _make_overconfident_samples()
    # ★ 关键：用临时 cache_path，禁止 fit()→save() 污染生产文件
    #   backend/data/confidence_calibration.json（否则每次 pytest 都会用合成数据覆盖真实校准映射）
    _tmp = os.path.join(tempfile.gettempdir(), "test_cal_improve.json")
    cal = ConfidenceCalibrator(method="auto", cache_path=_tmp)
    assert cal.fit(samples, min_samples=60)
    # 校准后在高置信区应低于 raw（纠正过自信）
    assert cal.calibrate(0.95) < 0.95
    assert cal.calibrate(0.85) < 0.85
    # 全样本校准后 Brier 优于 raw
    raw_pred = [s[0] for s in samples]
    cal_pred = [cal.calibrate(s[0]) for s in samples]
    ys = [s[1] for s in samples]
    assert brier(ys, cal_pred) < brier(ys, raw_pred)


def test_passthrough_when_unavailable():
    cal = ConfidenceCalibrator(cache_path=os.path.join(tempfile.gettempdir(), "nonexist_cal.json"))
    assert not cal.available()
    assert cal.calibrate(0.8) == 0.8  # 透传


def test_save_load_roundtrip(tmp_path):
    samples = _make_overconfident_samples()
    p = str(tmp_path / "cal.json")
    cal = ConfidenceCalibrator(cache_path=p, method="auto")
    assert cal.fit(samples, min_samples=60)
    rep = cal.report()
    assert rep["available"] and rep["method"] in ("platt", "isotonic")
    # 重新加载
    cal2 = ConfidenceCalibrator(cache_path=p)
    assert cal2.available()
    assert abs(cal2.calibrate(0.9) - cal.calibrate(0.9)) < 1e-9
