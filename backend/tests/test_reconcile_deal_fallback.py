# -*- coding: utf-8 -*-
"""对账 deal 匹配 fallback 回归测试（2026-08-11）。

背景：22:00 后主号 14 笔 mt5_closed_external 全部 profit=0 / 开平价相同——
      _reconcile_positions 的 _deals_map 匹配失败（MT5 断连/重启后 deals 历史丢失），
      fallback 到 open_price/0 导致"假 breakeven"。

修复演进：
  ① 2026-08-11 三阶 fallback：get_deal_by_position → recent 缓存 → SL/TP 推算。
  ② 2026-08-11 P0 二次修复：SL/TP 推算本身是错的！smart_exit 上移 SL 不回写 trades.sl
     → 用陈旧原始 SL 硬推平仓价 → 浮盈单记成假亏损（实证：378596055 真实 +2.00 @4379.21，
     DB 记 -56.32 @4350.05；上午 3 笔主号 BUY 假亏 -144.86）。新语义：deal 拉不到 =
     「不知道盈亏」→ 标 pending_verify，绝不假造。

本测试直接测 fallback 语义正确性（不依赖 MT5）。
"""
from types import SimpleNamespace


def _estimate(t, deals_map=None, deal_by_pos=None):
    """复刻 _reconcile_positions 的新 fallback 语义（纯函数版）。

    返回 (close_price, profit, verified)：
      - verified=True  → deal 命中，close/profit 为真实值
      - verified=False → deal 拉不到，close=None/profit=0（pending_verify，不假造）
    """
    _d = (deals_map or {}).get(str(t.mt5_ticket)) or {}
    _d_matched = bool(_d)
    if not _d and deal_by_pos is not None:
        _d = deal_by_pos or {}
        _d_matched = bool(_d)
    _cp = float(_d.get("price") or _d.get("close_price") or 0) or 0
    _pf = float(_d.get("profit") or _d.get("net_profit") or 0) or 0
    _unverified = not _d_matched or (_cp == 0 and _pf == 0)
    if _unverified:
        return None, 0.0, False  # pending_verify：不知道就是不知道
    return _cp, _pf, True


def _t(ticket, action, open_price, volume, sl=0, tp=0):
    return SimpleNamespace(
        mt5_ticket=str(ticket), action=action, open_price=open_price,
        volume=volume, sl=sl, tp=tp,
    )


# ── ① deal 精准匹配成功 → 用真实盈亏 ──
def test_deal_match_uses_real_profit():
    t = _t(1, "sell", 4349.17, 0.02, sl=4374.38)
    cp, pf, ok = _estimate(t, deals_map={"1": {"price": 4362.5, "profit": -26.66}})
    assert ok and cp == 4362.5 and pf == -26.66, f"deal 匹配应取真实盈亏: {cp}/{pf}/{ok}"


# ── ② deal 匹配失败 → pending_verify（绝不 SL 推算假亏损）──
#   ★ 2026-08-11 P0：这是上午 -8931 假账与 378596055 +2.00 记 -56.32 的根因。
#   旧行为：SELL 开 4349.17 SL=4374.38 vol=0.02 → 硬推 -50.42 假亏损。
#   新行为：不知道就是不知道 → None/0/False，标 pending_verify。
def test_deal_miss_is_pending_verify_not_sl_fabrication():
    t = _t(2, "sell", 4349.17, 0.02, sl=4374.38)
    cp, pf, ok = _estimate(t)
    assert ok is False, f"deal 未命中必须 verified=False（假 SL 推算已废除）: {ok}"
    assert cp is None and pf == 0.0, f"deal 未命中盈亏必须未知: {cp}/{pf}"


# ── ③ BUY 浮盈单 deal 未匹配 → pending_verify（不把盈利伪装成亏损）──
def test_buy_win_no_fabrication_when_deal_missing():
    # 复刻 378596055：开 4378.21 SL=4350.05（原始）真实 +2.00 @4379.21
    t = _t(3, "buy", 4378.21, 0.02, sl=4350.05)
    cp, pf, ok = _estimate(t)
    assert ok is False, "原始 SL 是开仓时的值，上移后已失真，不得用于推算"
    assert pf == 0.0 and cp is None, f"必须保持盈亏未知: {cp}/{pf}"


# ── ④ deal_by_position 精准命中优先 ──
def test_position_deal_overrides():
    t = _t(6, "sell", 4349.17, 0.02, sl=4374.38)
    cp, pf, ok = _estimate(t, deal_by_pos={"price": 4330.5, "profit": 37.34})
    assert ok and cp == 4330.5 and pf == 37.34, f"精准 deal 应覆盖: {cp}/{pf}/{ok}"


# ── ⑤ recent 缓存窗口命中（无精准 deal 时）──
def test_recent_cache_hit():
    t = _t(7, "buy", 4378.0, 0.02, sl=4350.0)
    cp, pf, ok = _estimate(t, deals_map={"7": {"price": 4379.21, "profit": 2.42}})
    assert ok and cp == 4379.21 and pf == 2.42, f"recent 缓存命中: {cp}/{pf}/{ok}"
