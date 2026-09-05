# -*- coding: utf-8 -*-
"""fusion_v2 时序融合票 —— 聚合逻辑与三道安全门回归测试。

★ 为什么这些测试是红线级别的：
   融合票会直接改写 MetaAgent 的第三票方向，等于直接指挥真实下单方向。
   一旦「合成行情 / 僵死快照 / 单模型伪融合」漏进决策链，
   AI 会照着假数据下真单 —— 这是资金级事故，不是显示 bug。
   所以三道门每一道都必须有测试钉死，禁止后续重构悄悄绕过。
"""
import time

import pytest

from app.services.fusion_service import (
    TS_FUSION_MAX_STALE_SEC,
    FusionService,
)


def _model(name, direction="BUY", conf=0.8, hit=0.6, available=True, lo=None, hi=None, score=0.5):
    return {
        "name": name, "direction": direction, "confidence": conf,
        "hit_rate": hit, "available": available, "lo": lo, "hi": hi, "score": score,
    }


def _snap(models, live=True, age_sec=0.0):
    return {
        "live": live,
        "updated_at": time.time() - age_sec,
        "models": models,
    }


@pytest.fixture()
def svc():
    return FusionService()


# ── 安全门 1：合成行情 ────────────────────────────────────────────
def test_gate_synthetic_quotes_rejected(svc):
    """MT5 掉线时参考面板用合成行情，融合票必须作废（否则拿假数据指挥真单）。"""
    snap = _snap([_model("Chronos-2"), _model("TimesFM-2.5")], live=False)
    v = svc._aggregate(snap)
    assert v.available is False
    assert "合成行情" in v.note


# ── 安全门 2：僵死快照 ────────────────────────────────────────────
def test_gate_stale_snapshot_rejected(svc):
    """快照超过阈值未刷新 → 作废。用 15 分钟前的方向指挥现在的交易比不用更危险。"""
    snap = _snap([_model("Chronos-2"), _model("TimesFM-2.5")],
                 age_sec=TS_FUSION_MAX_STALE_SEC + 60)
    v = svc._aggregate(snap)
    assert v.available is False
    assert "僵死" in v.note


def test_fresh_snapshot_accepted(svc):
    """刚刷新的快照（一轮内）必须放行，不能误杀。"""
    snap = _snap([_model("Chronos-2"), _model("TimesFM-2.5")], age_sec=120)
    v = svc._aggregate(snap)
    assert v.available is True


def test_missing_timestamp_rejected(svc):
    snap = {"live": True, "models": [_model("Chronos-2"), _model("TimesFM-2.5")]}
    v = svc._aggregate(snap)
    assert v.available is False


# ── 安全门 3：单模型不叫融合 ──────────────────────────────────────
def test_gate_single_model_rejected(svc):
    """只剩 1 个模型时应回退单 Chronos 老路径，而不是伪装成融合票。"""
    snap = _snap([
        _model("Chronos-2"),
        _model("TimesFM-2.5", available=False, direction="TIMEOUT"),
        _model("Time-MoE", available=False, direction="ERROR"),
    ])
    v = svc._aggregate(snap)
    assert v.available is False
    assert v.model_count == 1
    assert "回退单Chronos" in v.note


def test_all_unavailable_rejected(svc):
    snap = _snap([
        _model("Chronos-2", available=False, direction="TIMEOUT"),
        _model("Moirai(447M)", available=False, direction="N/A"),
    ])
    v = svc._aggregate(snap)
    assert v.available is False
    assert v.model_count == 0


# ── 聚合正确性 ───────────────────────────────────────────────────
def test_all_agree_buy(svc):
    snap = _snap([
        _model("Chronos-2", "BUY", 0.8, 0.7),
        _model("TimesFM-2.5", "BUY", 0.75, 0.6),
        _model("Time-MoE", "BUY", 0.7, 0.55),
        _model("Moirai(447M)", "BUY", 0.65, 0.5),
    ])
    v = svc._aggregate(snap)
    assert v.available is True
    assert v.direction == "BUY"
    assert v.agree is True
    # ★ 2026-08-19 定稿P0-1 单锚化：model_count=参与投票模型数=1（仅锚），
    #   观测模型不计入（避免"4模型"误导）；hit_rate_avg 只算锚命中率。
    assert v.model_count == 1
    assert v.hit_rate_avg == pytest.approx(0.7, abs=1e-6)
    assert v.score > 0
    assert v.weight_scale == pytest.approx(1.0, abs=1e-6)


def test_all_agree_sell(svc):
    snap = _snap([
        _model("Chronos-2", "SELL", 0.8, 0.7),
        _model("TimesFM-2.5", "SELL", 0.75, 0.6),
        _model("Time-MoE", "SELL", 0.7, 0.55),
    ])
    v = svc._aggregate(snap)
    assert v.direction == "SELL"
    assert v.score < 0
    assert v.agree is True


def test_split_models_yield_hold(svc):
    """★ 2026-08-19 定稿P0-1 单锚化契约：2BUY vs 2SELL 势均力敌时，
    锚(Chronos)有方向即为主导 → 融合方向=锚方向（弱票观测化不参与意见）。
    旧语义（2v2→HOLD）见 test_split_models_yield_hold_legacy。
    """
    snap = _snap([
        _model("Chronos-2", "BUY", 0.8, 0.6),
        _model("TimesFM-2.5", "BUY", 0.8, 0.6),
        _model("Time-MoE", "SELL", 0.8, 0.6),
        _model("Moirai(447M)", "SELL", 0.8, 0.6),
    ])
    v = svc._aggregate(snap)
    assert v.direction == "BUY"      # 锚 BUY 主导，弱票 SELL 分歧被忽略
    assert v.agree is True           # 锚有方向即无分歧
    # 非锚模型应标记为观测（qw=0, mode=observe）
    for m in v.per_model:
        if "Chronos" in (m.get("name") or ""):
            assert m.get("qw", -1) > 0
        else:
            assert m.get("mode") == "observe"


def test_split_models_yield_hold_legacy(svc, monkeypatch):
    """旧等权融合契约（单锚化关闭）：2BUY vs 2SELL 势均力敌 → 必须 HOLD。"""
    monkeypatch.setattr("app.services.fusion_service.settings.TS_FUSION_SINGLE_ANCHOR", False)
    snap = _snap([
        _model("Chronos-2", "BUY", 0.8, 0.6),
        _model("TimesFM-2.5", "BUY", 0.8, 0.6),
        _model("Time-MoE", "SELL", 0.8, 0.6),
        _model("Moirai(447M)", "SELL", 0.8, 0.6),
    ])
    v = svc._aggregate(snap)
    assert v.direction == "HOLD"
    assert v.agree is False


def test_weight_scale_single_anchor_full(svc):
    """★ 2026-08-19 定稿P0-1 单锚化：w_scale 恒 1.0（方向锚满权重，
    不因参与模型数降权——单锚是设计选择而非能力不足）。"""
    one = svc._aggregate(_snap([_model("Chronos-2", "BUY")]))
    four = svc._aggregate(_snap([
        _model("Chronos-2", "BUY"), _model("TimesFM-2.5", "BUY"),
        _model("Time-MoE", "BUY"), _model("Moirai(447M)", "BUY"),
    ]))
    assert one.weight_scale == pytest.approx(1.0, abs=1e-6)
    assert four.weight_scale == pytest.approx(1.0, abs=1e-6)


def test_weight_scale_scales_with_model_count(svc, monkeypatch):
    """旧等权融合契约（单锚化关闭）：4 模型同向的话语权必须严格大于 2 模型同向。"""
    monkeypatch.setattr("app.services.fusion_service.settings.TS_FUSION_SINGLE_ANCHOR", False)
    two = svc._aggregate(_snap([
        _model("Chronos-2", "BUY"), _model("TimesFM-2.5", "BUY"),
    ]))
    four = FusionService()._aggregate(_snap([
        _model("Chronos-2", "BUY"), _model("TimesFM-2.5", "BUY"),
        _model("Time-MoE", "BUY"), _model("Moirai(447M)", "BUY"),
    ]))
    assert two.weight_scale < four.weight_scale
    assert two.weight_scale == pytest.approx(0.70, abs=1e-6)


def test_disagreement_penalizes_weight(svc, monkeypatch):
    """★ 2026-08-19 定稿P0-1：单锚化下弱票分歧不参与 → 内部分歧不再打权重折。
    （分歧打折语义属于旧等权融合，见 legacy 版本。）"""
    agree = svc._aggregate(_snap([
        _model("Chronos-2", "BUY", 0.9, 0.7),
        _model("TimesFM-2.5", "BUY", 0.9, 0.7),
        _model("Time-MoE", "BUY", 0.9, 0.7),
    ]))
    disagree = FusionService()._aggregate(_snap([
        _model("Chronos-2", "BUY", 0.9, 0.7),
        _model("TimesFM-2.5", "BUY", 0.9, 0.7),
        _model("Time-MoE", "SELL", 0.2, 0.3),
    ]))
    assert agree.weight_scale == pytest.approx(1.0, abs=1e-6)
    assert disagree.weight_scale == pytest.approx(1.0, abs=1e-6)


def test_disagreement_penalizes_weight_legacy(svc, monkeypatch):
    """旧等权融合契约（单锚化关闭）：内部分歧时权重要打折。"""
    monkeypatch.setattr("app.services.fusion_service.settings.TS_FUSION_SINGLE_ANCHOR", False)
    agree = svc._aggregate(_snap([
        _model("Chronos-2", "BUY", 0.9, 0.7),
        _model("TimesFM-2.5", "BUY", 0.9, 0.7),
        _model("Time-MoE", "BUY", 0.9, 0.7),
    ]))
    disagree = FusionService()._aggregate(_snap([
        _model("Chronos-2", "BUY", 0.9, 0.7),
        _model("TimesFM-2.5", "BUY", 0.9, 0.7),
        _model("Time-MoE", "SELL", 0.2, 0.3),
    ]))
    assert disagree.weight_scale < agree.weight_scale


def test_interval_is_conservative_intersection(svc, monkeypatch):
    """★ 2026-08-19 定稿P0-1：单锚化下预测区间只取锚（Chronos）的区间——
    弱票观测化后不再参与区间聚合，lo/hi 即锚的 lo/hi。"""
    snap = _snap([
        _model("Chronos-2", "BUY", lo=3300.0, hi=3400.0),
        _model("TimesFM-2.5", "BUY", lo=3320.0, hi=3380.0),
    ])
    v = svc._aggregate(snap)
    assert v.lo == 3300.0   # 锚的 lo（弱票不参与）
    assert v.hi == 3400.0   # 锚的 hi


def test_interval_is_conservative_intersection_legacy(svc, monkeypatch):
    """旧等权融合契约（单锚化关闭）：预测区间取交集（最保守）。"""
    monkeypatch.setattr("app.services.fusion_service.settings.TS_FUSION_SINGLE_ANCHOR", False)
    snap = _snap([
        _model("Chronos-2", "BUY", lo=3300.0, hi=3400.0),
        _model("TimesFM-2.5", "BUY", lo=3320.0, hi=3380.0),
    ])
    v = svc._aggregate(snap)
    assert v.lo == 3320.0   # max of los
    assert v.hi == 3380.0   # min of his


def test_confidence_bounded(svc):
    snap = _snap([_model("Chronos-2", "BUY", 1.0, 1.0), _model("TimesFM-2.5", "BUY", 1.0, 1.0)])
    v = svc._aggregate(snap)
    assert 0.0 <= v.confidence <= 0.98


def test_empty_snapshot_safe(svc):
    assert svc._aggregate(None).available is False
    assert svc._aggregate({}).available is False
    assert svc._aggregate(_snap([])).available is False


def test_no_exception_on_garbage_fields(svc):
    """脏字段不得抛异常（决策链里抛异常会静默吞掉整票）。"""
    snap = _snap([
        {"name": "X", "direction": "BUY", "available": True,
         "confidence": None, "hit_rate": None, "score": None, "lo": None, "hi": None},
        {"name": "Y", "direction": "SELL", "available": True,
         "confidence": None, "hit_rate": None, "score": None, "lo": None, "hi": None},
    ])
    v = svc._aggregate(snap)
    assert v.available is True
