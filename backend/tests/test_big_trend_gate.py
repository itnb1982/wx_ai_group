# -*- coding: utf-8 -*-
"""A门（大周期过滤器）+ B门（HY HOLD 刹车）回归测试。

背景（2026-08-11 用户实盘复盘）：
  23:35 DeepSeek 观察到 H4/H1 bullish，却用 M15 反弹受阻 + CVD 背离判反转开 SELL
  → 价格 1 小时涨 14 美元浮亏 -1345。根因：大周期该主导方向、小周期只定入场，
  且 DS=SELL 58% + HY=HOLD 25% 的分裂决策被 norm≥0.55 放行。

A门：大周期明确上涨(trend_up) → 禁止 SELL；明确下跌(trend_down) → 禁止 BUY
     （三模型共振 + 价格已统计性延伸(|z|≥1.5) 或 趋势末端 才豁免，提准非拦截）。
B门：双脑分歧且持方向方逆大周期 → HOLD（Chronos 同向背书豁免）。

★ 2026-08-12 盯盘修正（防假绿）：
   生产 detect_regime() 返回 "trend_up"/"trend_down"（非 "uptrend"），_rg_map 才映射成功。
   旧 helper 传 "uptrend" 导致 trend_regime="normal"、A门整段被跳过、测试靠
   min_confidence 兜底"假绿"。本版 helper 改用生产字符串，真正验证 A门。

自验方法（防假绿）：断言错误路径确实打印了门日志（用 caplog / 直接查 HOLD 结果）。
"""
from types import SimpleNamespace

import pytest

from app.core.meta_agent import MetaAgent


# ★ 2026-08-17 修复：测试环境无云 Key → effective_cloud_enabled()=False → 云权重全 0
#   → decision_scores 全 0 → max() 返回字典首键 "BUY"（垃圾决策），A/B 门逻辑空转。
#   本测试守护 A/B 门（云决策链的一部分），必须模拟云开启让权重正常参与。
@pytest.fixture(autouse=True)
def _force_cloud_enabled(monkeypatch):
    monkeypatch.setattr("app.core.meta_agent.effective_cloud_enabled", lambda: True)
    yield


# ── 通用构造（使用生产 regime 字符串）──

def _adjudicate(*, ds, hy, ds_conf=0.6, hy_conf=0.3, regime="trend_up",
                chronos_dir="NEUTRAL", extension_z=None, at_stale_top=False,
                meta_quality=None, smc_bias=None, news=None):
    """按参数构造一次 adjudicate 调用。regime 用生产 detect_regime 字符串。"""
    agent = MetaAgent()
    ds_an = {"decision": ds, "confidence": ds_conf, "risk_assessment": {}}
    hy_an = {"decision": hy, "confidence": hy_conf, "risk_assessment": {}}
    # 反驳：默认保持初判方向（不翻转）
    ds_re = {"decision": ds, "confidence": ds_conf, "agree_with_opponent": False}
    hy_re = {"decision": hy, "confidence": hy_conf, "agree_with_opponent": False}
    is_up = regime in ("trend_up", "strong_uptrend")
    is_down = regime in ("trend_down", "strong_downtrend")
    if extension_z is None:
        extension_z = 0.8 if is_up else (-0.8 if is_down else 0.0)
    _mq = meta_quality or {"chronos_dir": chronos_dir, "uncertainty": 0.1}
    md = {
        "regime": {"regime": regime,
                   "direction_bias": "up" if is_up else ("down" if is_down else "neutral"),
                   "extension_z": extension_z,
                   "rsi_h1": 60 if is_up else (40 if is_down else 50),
                   "at_stale_top": at_stale_top, "at_stale_bottom": False},
        "timeframes": {"H1": {"trend": "up" if is_up else ("down" if is_down else "neutral")},
                        "H4": {"trend": "up" if is_up else ("down" if is_down else "neutral")}},
        "meta_quality": _mq,   # 注：meta_quality 占位，chronos_dir 实际取自 market_data.meta_quality
        "my_open_positions": [],
        "portfolio_state": {},
    }
    if smc_bias is not None:
        md["smc_features"] = {"global_bias": smc_bias}
    # adjudicate 读 chronos_dir 来自 meta_quality
    md["meta_quality"] = _mq
    if news is not None:
        md["news"] = news
    return agent.adjudicate(ds_an, hy_an, ds_re, hy_re, md)


# ── A门测试 ──

def test_a_gate_blocks_sell_in_uptrend():
    """大周期上涨(trend_up) + 双云SELL（非三模型共振）→ A门拦截为 HOLD。"""
    dec = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.6, hy_conf=0.5, regime="trend_up")
    assert dec.decision == "HOLD", f"A门未拦截逆势SELL: {dec.decision}"


def test_a_gate_blocks_buy_in_downtrend():
    """大周期下跌(trend_down) + 双云BUY → A门拦截为 HOLD。"""
    dec = _adjudicate(ds="BUY", hy="BUY", ds_conf=0.6, hy_conf=0.5, regime="trend_down")
    assert dec.decision == "HOLD", f"A门未拦截逆势BUY: {dec.decision}"


def test_a_gate_allows_with_trend():
    """大周期上涨(trend_up) + 顺势 BUY → 不受影响（提准非拦截）。"""
    dec = _adjudicate(ds="BUY", hy="BUY", ds_conf=0.7, hy_conf=0.6, regime="trend_up")
    assert dec.decision == "BUY", f"顺势BUY被误拦: {dec.decision}"


def test_a_gate_blocks_three_way_consensus_without_extension():
    """★ 2026-08-12 修复后：大周期上涨 + 三模型SELL共振 + 价格未统计性延伸(z=0.8<1.5)
    → 逆势摸顶（今日实盘#380501075/-703 根因），A门拦截为 HOLD（提准非拦截）。"""
    dec = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.7, hy_conf=0.6, regime="trend_up",
                      chronos_dir="SELL", extension_z=0.8)
    assert dec.decision == "HOLD", f"无延伸度的三模型共振逆势SELL未被拦截: {dec.decision}"


def test_a_gate_allows_three_way_consensus_with_extension():
    """大周期上涨 + 三模型SELL共振 + 价格已统计性延伸(z=2.0≥1.5) → 真反转 exhaustion，放行SELL。"""
    dec = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.7, hy_conf=0.6, regime="trend_up",
                      chronos_dir="SELL", extension_z=2.0)
    assert dec.decision == "SELL", f"有延伸度的逆势SELL被误拦: {dec.decision}"


def test_a_gate_allows_three_way_consensus_at_stale_top():
    """大周期上涨 + 三模型SELL共振 + 处于趋势末端山顶 → 接飞刀合理，放行SELL。"""
    dec = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.7, hy_conf=0.6, regime="trend_up",
                      chronos_dir="SELL", extension_z=0.8, at_stale_top=True)
    assert dec.decision == "SELL", f"趋势末端逆势SELL被误拦: {dec.decision}"


# ── B门测试 ──

def test_b_gate_blocks_split_against_trend():
    """大周期上涨(trend_up) + DS=SELL(0.6) + HY=HOLD(0.3)（分裂）→ B门/A门刹车为 HOLD。"""
    dec = _adjudicate(ds="SELL", hy="HOLD", ds_conf=0.6, hy_conf=0.3, regime="trend_up")
    assert dec.decision == "HOLD", f"B门/A门未拦截逆势分裂决策: {dec.decision}"


def test_b_gate_split_chronos_backed_blocked_by_a_gate():
    """★ 真实生产行为：B门放行(Chronos背书)的逆势分裂单，仍被后续 A门(逆大周期)拦截为 HOLD。
    修正原'假绿'测试（旧helper传'uptrend'使A门跳过，误判放行）。"""
    dec = _adjudicate(ds="SELL", hy="HOLD", ds_conf=0.6, hy_conf=0.3, regime="trend_up",
                      chronos_dir="SELL")
    assert dec.decision == "HOLD", f"逆势分裂单未被A门拦截: {dec.decision}"


def test_b_gate_allows_split_with_trend():
    """大周期上涨(trend_up) + DS=BUY + HY=HOLD（方向顺大周期）→ 不受影响。"""
    dec = _adjudicate(ds="BUY", hy="HOLD", ds_conf=0.65, hy_conf=0.3, regime="trend_up")
    assert dec.decision == "BUY", f"顺势分裂决策被误拦: {dec.decision}"


def test_b_gate_neutral_regime_unaffected():
    """大周期中性（range）→ B门不干预。"""
    dec = _adjudicate(ds="SELL", hy="HOLD", ds_conf=0.6, hy_conf=0.3, regime="range")
    assert dec.decision in ("SELL", "HOLD"), f"中性体制被B门误干预: {dec.decision}"


# ── 2026-08-18 第三处修复（趋势明确时降权反向"提准器"·提准非拦截）──

def test_trend_strong_chronos_oppose_exempt_from_dead_zone():
    """★ 结构性死区修复B：强跌趋势 + 双云SELL共识 + Chronos反向BUY → 顺势能开(SELL)，
    且归一化置信高于同场景range（趋势强时Chronos/融合票反向降权×0.25，不再撑大
    active_weight分母把顺势SELL压到<0.58→不开单）。"""
    down = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.6, hy_conf=0.6,
                       regime="strong_downtrend", chronos_dir="BUY")
    rng = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.6, hy_conf=0.6,
                     regime="range", chronos_dir="BUY")
    assert down.decision == "SELL", f"强跌趋势双云SELL共识被反向Chronos压死: {down.decision}"
    assert down.confidence > rng.confidence, (
        f"趋势强未豁免反向Chronos→顺势SELL仍被压: down={down.confidence:.3f} <= range={rng.confidence:.3f}")


def test_trend_strong_smc_bullish_exempt_from_penalty():
    """★ 修复A：强跌趋势 + 双云SELL共识 + SMC全局偏bullish(逆势) → 顺势能开(SELL)，
    且归一化置信高于同场景range（趋势强时SMC软降权豁免：长周期背景不压短周期盘面）。"""
    down = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.6, hy_conf=0.6,
                       regime="strong_downtrend", smc_bias="bullish")
    rng = _adjudicate(ds="SELL", hy="SELL", ds_conf=0.6, hy_conf=0.6,
                     regime="range", smc_bias="bullish")
    assert down.decision == "SELL", f"强跌趋势SMC=bullish仍压死顺势SELL: {down.decision}"
    assert down.confidence > rng.confidence, (
        f"趋势强未豁免SMC软降权: down={down.confidence:.3f} <= range={rng.confidence:.3f}")


def test_d_down_trend_split_hy_hold_chronos_reverse_opens_sell():
    """★ 修复D(扩写)：强跌趋势 + DS=SELL + HY翻HOLD(分裂) + 时序反向BUY → 顺势能开SELL。
    对照 2026-08-18 重启后现场死锁（DS=SELL58%/HY翻HOLD/融合BUY命中0%→norm36%<42%→HOLD）。
    修复后：趋势对齐降权反向时序票 → SELL归一化破 lean(0.42) 门槛以小仓顺势开。
    regime 用 detect_regime 原始串 trend_down(→downtrend)。"""
    dec = _adjudicate(ds="SELL", hy="HOLD", ds_conf=0.58, hy_conf=0.25,
                      regime="trend_down", chronos_dir="BUY")
    assert dec.decision == "SELL", f"趋势明确分裂死锁未破: {dec.decision}/{dec.confidence:.3f}"
    assert dec.confidence >= 0.42, f"顺势SELL置信未破lean门槛: {dec.confidence:.3f}"


def test_d_up_trend_split_hy_hold_chronos_reverse_opens_buy():
    """★ 修复D 对称：强涨趋势 + DS=BUY + HY=HOLD + 时序反向SELL → 顺势能开BUY。
    regime 用原始串 trend_up(→uptrend)。"""
    dec = _adjudicate(ds="BUY", hy="HOLD", ds_conf=0.58, hy_conf=0.25,
                      regime="trend_up", chronos_dir="SELL")
    assert dec.decision == "BUY", f"强涨分裂死锁未破: {dec.decision}/{dec.confidence:.3f}"
    assert dec.confidence >= 0.42, f"顺势BUY置信未破lean门槛: {dec.confidence:.3f}"


def test_d_range_split_hy_hold_still_holds():
    """★ 回归保护：震荡市(range) + DS方向 + HY=HOLD + 时序反向 → 仍 HOLD（修复D不破坏保守震荡行为）。"""
    dec = _adjudicate(ds="SELL", hy="HOLD", ds_conf=0.58, hy_conf=0.25,
                      regime="range", chronos_dir="BUY")
    assert dec.decision == "HOLD", f"震荡市分裂不应开单: {dec.decision}/{dec.confidence:.3f}"



def test_e_news_exempt_trend_aligned_sell():
    """★ 第五步A：强跌趋势+顺势SELL+新闻高影响偏BUY → 新闻豁免不降权。
    对照生产死锁：强跌里新闻偏BUY(高影响)把顺势SELL从42%压到36%死锁。
    修复后趋势明确顺势单豁免舆情降权(舆情是短期噪音)。关云模式用chronos=SELL构造顺势final。"""
    base = dict(ds="SELL", hy="HOLD", ds_conf=0.58, hy_conf=0.25,
                regime="trend_down", chronos_dir="SELL")
    no_news = _adjudicate(**base)
    news = {"has_news": True, "gold_sentiment_score": 0.35, "high_impact_active": True}
    with_news = _adjudicate(**base, news=news)
    assert no_news.decision == "SELL"
    assert with_news.decision == "SELL"
    # 趋势明确顺势 → 新闻豁免，置信不被压（近似相等）
    assert abs(with_news.confidence - no_news.confidence) < 0.02, \
        f"新闻豁免失效: 无news={no_news.confidence:.3f} 有news={with_news.confidence:.3f}"


def test_e_news_not_exempt_in_range():
    """★ 回归保护：震荡市(range)+SELL+新闻高影响偏BUY → 仍降权(不豁免)。
    新闻豁免只在趋势明确时生效，震荡市仍尊重舆情降权，避免逆舆情乱开。"""
    base = dict(ds="SELL", hy="HOLD", ds_conf=0.58, hy_conf=0.25,
                regime="range", chronos_dir="SELL")
    no_news = _adjudicate(**base)
    news = {"has_news": True, "gold_sentiment_score": 0.35, "high_impact_active": True}
    with_news = _adjudicate(**base, news=news)
    # 震荡市不豁免新闻降权 → 置信被压低
    assert with_news.confidence < no_news.confidence * 0.9, \
        f"震荡市新闻应降权: 无news={no_news.confidence:.3f} 有news={with_news.confidence:.3f}"
