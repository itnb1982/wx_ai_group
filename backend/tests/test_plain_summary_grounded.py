"""
plain_summary 接地（Grounding）单测 —— 2026-08-12 修复验证。

核心回归：此前 `_build_plain_summary` 按 final_decision 硬写「看涨」并贴置信度数字，
导致「三模型全 SELL 却文案写看涨」的颠倒 bug（见 #380912653）。
修复后方向断言 100% 来自真实投票(ds_final/hy_final/chronos_dir)。
"""
import pytest
from app.core.meta_agent import _build_plain_summary, _normalize_decision


def test_override_all_sell_but_meta_buy_must_not_say_bullish():
    """三模型全 SELL、Meta 翻 BUY → 文案必须如实说看空投票 + 逆共识，不得写「看涨」误导。"""
    txt = _build_plain_summary(
        final_decision="BUY",
        debate_consensus="moderate",
        risk_level="medium",
        risk_score=5,
        ds_final="SELL", hy_final="SELL",
        ds_confidence=0.55, hy_confidence=0.48,
        market_regime="ranging",
        chronos_dir="SELL", chronos_weight=0.22, chronos_agree=False,
    )
    print("\n[override] =>", txt)
    assert "DeepSeek 看跌" in txt, "必须如实写出 DeepSeek 真实投票=看跌"
    assert "混元 看跌" in txt, "必须如实写出 混元 真实投票=看跌"
    assert "Chronos 看跌" in txt, "必须如实写出 Chronos 真实投票=看跌"
    assert "逆共识" in txt, "Meta 逆多数投票翻向必须标注逆共识"
    assert "多数 AI 倾向看涨" not in txt, "绝不能再把全 SELL 粉饰成看涨"
    assert "三模型共振" not in txt, "全 SELL 不得误标为共振"


def test_three_way_buy_consensus_says_bullish():
    """三模型全 BUY → 三模型共振看涨，且如实写出各模型看多。"""
    txt = _build_plain_summary(
        final_decision="BUY",
        debate_consensus="strong",
        risk_level="low",
        risk_score=3,
        ds_final="BUY", hy_final="BUY",
        ds_confidence=0.72, hy_confidence=0.70,
        market_regime="uptrend",
        chronos_dir="BUY", chronos_weight=0.22, chronos_agree=True,
    )
    print("\n[3way-buy] =>", txt)
    assert "三模型共振" in txt
    assert "DeepSeek 看涨" in txt
    assert "混元 看涨" in txt
    assert "Chronos 看涨" in txt


def test_majority_buy_with_one_hold():
    """DS/HY 看多、Chronos 未参与 → 多数 AI 倾向看涨，如实写出。"""
    txt = _build_plain_summary(
        final_decision="BUY",
        debate_consensus="moderate",
        risk_level="medium",
        risk_score=5,
        ds_final="BUY", hy_final="BUY",
        ds_confidence=0.6, hy_confidence=0.58,
        market_regime="uptrend",
        chronos_dir="NEUTRAL", chronos_weight=0.0, chronos_agree=False,
    )
    print("\n[maj-buy] =>", txt)
    assert "多数 AI 倾向「看涨」" in txt
    assert "DeepSeek 看涨" in txt
    assert "Chronos 未参与" in txt


def test_split_votes_meta_follows_one_side():
    """DS 看多、HY 看空、Meta 翻多 → 分歧文案，不得粉饰成多数看涨。"""
    txt = _build_plain_summary(
        final_decision="BUY",
        debate_consensus="disagreement",
        risk_level="high",
        risk_score=7,
        ds_final="BUY", hy_final="SELL",
        ds_confidence=0.6, hy_confidence=0.55,
        market_regime="ranging",
        chronos_dir="HOLD", chronos_weight=0.0, chronos_agree=False,
    )
    print("\n[split] =>", txt)
    assert "分歧" in txt
    assert "DeepSeek 看涨" in txt
    assert "混元 看跌" in txt
    assert "多数 AI 倾向看涨" not in txt


def test_normalize_decision_covers_synonyms():
    """normalize 兼容大小写/中文/同义词，防止投票方向判错。"""
    assert _normalize_decision("buy") == "BUY"
    assert _normalize_decision("做空") == "SELL"
    assert _normalize_decision("NEUTRAL") == "HOLD"
    assert _normalize_decision("") == "HOLD"
    assert _normalize_decision(None) == "HOLD"


if __name__ == "__main__":
    test_override_all_sell_but_meta_buy_must_not_say_bullish()
    test_three_way_buy_consensus_says_bullish()
    test_majority_buy_with_one_hold()
    test_split_votes_meta_follows_one_side()
    test_normalize_decision_covers_synonyms()
    print("\nALL PASS")
