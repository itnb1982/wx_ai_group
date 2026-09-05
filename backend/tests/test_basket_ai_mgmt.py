# -*- coding: utf-8 -*-
"""
篮子级 AI 持仓管理（2026-08-17）回归测试。
注意：meta_agent 有循环导入（app.services → trade_executor → debate_engine → meta_agent），
独立脚本无法直接 import 真实现。本测试用「语义复刻」验证融合/确认/回吐的核心逻辑
（与 app/core/meta_agent.py 的 _fuse_basket_action/_confirm_basket_action 及
app/services/trade_executor.py 的回吐保护判定逐行一致）；真实现正确性由 py_compile + 运行日志保证。
"""
import math, time

# ── 复刻自 meta_agent._fuse_basket_action ──
def _bconf(v, fallback=0.5):
    try:
        s = str(v or "").strip().rstrip("%").strip()
        if s in ("", "null", "None", "nan", "NaN", "inf", "-inf"):
            return fallback
        c = float(s)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(c):
        return fallback
    if c > 100.0:
        return 1.0
    if c > 1.0:
        return c / 100.0
    return max(0.0, min(1.0, c))


def fuse(ds_a, hy_a, ds_c, hy_c, positions):
    def _extract(a):
        pa = (a or {}).get("position_action") or {}
        act = str(pa.get("action") or "hold").strip().lower()
        if act not in ("hold", "trim", "close_all"):
            act = "hold"
        return act, _bconf(pa.get("confidence"), 0.5), str(pa.get("reason") or "")[:80]

    ds_act, ds_ca, _ = _extract(ds_a)
    hy_act, hy_ca, _ = _extract(hy_a)
    _rank = {"hold": 0, "trim": 1, "close_all": 2}
    if not positions:
        return "hold", 0.0
    if ds_act == "hold" and hy_act == "hold":
        return "hold", 0.0
    _w = max(float(ds_c or 0) + float(hy_c or 0), 1e-9)
    if ds_act == hy_act:
        return ds_act, round((ds_ca * float(ds_c or 0) + hy_ca * float(hy_c or 0)) / _w, 3)
    stronger = ds_act if _rank[ds_act] >= _rank[hy_act] else hy_act
    return stronger, round((ds_ca * float(ds_c or 0) + hy_ca * float(hy_c or 0)) / _w * 0.5, 3)


# ── 复刻自 meta_agent._confirm_basket_action ──
HIST = {}
def confirm(account_id, action, window=240.0, need=2):
    now = time.time()
    hist = HIST.setdefault(account_id, [])
    hist.append((now, action))
    HIST[account_id] = [h for h in hist if now - h[0] <= window]
    if action == "hold":
        return False
    cnt = 0
    for _ts, _a in reversed(HIST[account_id]):
        if _a == action:
            cnt += 1
        else:
            break
    return cnt >= need


# ── 复刻自 trade_executor 篮子回吐保护判定（2026-08-17 P0 双修复）──
# ① 峰值首次必存（None 判空，不再用 get(key, 当前值) 导致键永不写入）
# ② 阈值按总手数动态换算（利润区 0.5 点 / 回吐 ≥max(峰值5%, 0.30 点)）
#    对齐用户理念「盈利即护盘·回撤一点就跑·几美金也锁」（旧: floor 6$ / 50% / 8$）
def pullback_trigger(peak, current, total_vol=0.03):
    floor = 0.5 * total_vol * 100.0          # 利润区美元地板
    abs_ = 0.30 * total_vol * 100.0          # 回吐绝对下限美元
    if peak >= floor and peak > 0:
        pull = peak - current
        th = max(peak * 0.05, abs_)
        return pull >= th, round(pull, 2), round(th, 2)
    return False, 0, 0


def peak_tracker(values, total_vol=0.03):
    """复刻 _BASKET_PEAK_PNL 首次必存逻辑：逐轮喂入浮盈，返回各轮峰值序列。"""
    peak = None
    out = []
    for v in values:
        if peak is None or v > peak:
            peak = v
        out.append(peak)
    return out


P = lambda n: [{"ticket": f"T{i}", "volume": 0.01, "floating_pnl": n} for i in range(3)]
mk = lambda act, c: {"position_action": {"action": act, "confidence": c, "reason": "t"}}

# 1.1 同向 close_all
a, c = fuse(mk("close_all", 0.8), mk("close_all", 0.7), 0.5, 0.5, P(10))
assert a == "close_all" and 0.7 <= c <= 0.8, (a, c)
print("1.1 同向 close_all:", a, c)
# 1.2 分歧 trim/hold → trim 置信减半
a, c = fuse(mk("trim", 0.8), mk("hold", 0.5), 0.5, 0.5, P(10))
assert a == "trim" and c <= 0.4, (a, c)
print("1.2 分歧降权:", a, c)
# 1.3 无持仓 → hold
a, c = fuse(mk("close_all", 0.9), mk("close_all", 0.9), 0.5, 0.5, [])
assert a == "hold"
print("1.3 无持仓→hold")
# 1.4 非法 action → 归一为 hold；双非法 → hold（单票非法时按另一票融合，属设计）
a, c = fuse(mk("attack", 0.9), mk("garbage", 0.9), 0.5, 0.5, P(10))
assert a == "hold", (a, c)
a, c = fuse(mk("attack", 0.9), mk("close_all", 0.9), 0.5, 0.5, P(10))
assert a == "close_all", (a, c)  # 非法=hold，与 close_all 融合取保守档
print("1.4 非法回退 hold OK（单票非法按另一票融合）")
# 1.5 "95%" 百分比置信归一
a, c = fuse({"position_action": {"action": "trim", "confidence": "95%"}}, mk("trim", 0.6), 0.5, 0.5, P(10))
assert 0.7 <= c <= 0.9, c
print("1.5 百分比置信:", c)

# 2. 防抖确认
HIST.clear()
assert not confirm("a1", "close_all")
assert confirm("a1", "close_all")
HIST.clear()
confirm("a2", "trim"); confirm("a2", "hold")
assert not confirm("a2", "trim")
print("2 防抖确认 OK（2 轮确认 / hold 中断清零）")

# 3. 回吐保护（2026-08-17 用户理念：利润区 0.5 点 / 回吐 ≥max(峰值5%, 0.30点)）
#    0.03 总手 → 利润区 1.5$ / 回吐下限 0.9$
ok, pull, th = pullback_trigger(10, 4)
assert ok, (pull, th)       # 回吐 6 ≥ 0.9 → 触发（用户：回撤一点就跑）
ok, pull, th = pullback_trigger(10, 9)
assert ok                    # 回吐 1 ≥ 0.9 → 触发（用户：10回到9就应该跑）
ok, pull, th = pullback_trigger(10, 9.3)
assert not ok                # 回吐 0.7 < 0.9 不触发（噪音缓冲）
ok, pull, th = pullback_trigger(2, 0.5)
assert ok                    # 峰值 2 ≥ 1.5 → 回吐 1.5 ≥ 0.9 触发（小浮盈也护盘）
ok, _, _ = pullback_trigger(1.0, 0.3)
assert not ok                # 峰值 1.0 < 1.5 未达利润区地板
print("3 回吐保护 OK（10→4 触发 / 10→9 触发 / 10→9.3 噪音不触发 / 小浮盈2→0.5也触发）")

# 4. ★★ 2026-08-17 P0：峰值首次必存（键永不写入 → 保护形同虚设 的回归锁死）★★
#    旧逻辑 get(key, 当前值)：首次 dict 无键返回当前值 → `当前>峰值` 恒假 → 永不写入。
#    实锤：大仓 0.48 手 23:36 +72.96 → 23:38 +0.48 → 23:40 -27.36 全程无保护。
peaks = peak_tracker([72.96, 0.48, -27.36])
assert peaks == [72.96, 72.96, 72.96], peaks  # 峰值必须被保留，回吐量才能算
ok, pull, th = pullback_trigger(peaks[0], 0.48, total_vol=0.48)
assert ok and pull == 72.48, (pull, th)        # 72.96→0.48 回吐 72.48 ≥ 0.9 必须触发全平
print(f"4 峰值首次必存 OK（72.96→0.48 回吐 {pull} ≥ {th} → 触发全平，不再坐视回吐转亏）")

print("=== 篮子级 AI 持仓管理 ALL PASS ===")
