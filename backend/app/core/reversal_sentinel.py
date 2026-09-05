"""
万象Ai XAUUSD — 反转哨兵代理（第3辩论角色·制衡趋势跟踪）

职责：专找「趋势末端 / 高位接飞刀 / 低位接刀」反转信号，作为趋势跟踪代理
      (DeepSeek/Hunyuan) 的制衡方，防止在山顶盲目开BUY、在谷底盲目开SELL。

输入：market_data（含 regime / smc_features / timeframes）
输出：{signal: REVERSE_SELL/REVERSE_BUY/NONE, confidence, evidence, reasoning}

调研支撑（2026-08-05 海外交叉验证）：
  - pro-scalper / 用户历史教训：价格延伸度 Z>2.5×ATR = 统计罕见延伸，回归概率骤升；
    H4 RSI>72(非70，黄金波动大)后约68%三根内回EMA
  - RegimeRisk(2026)：体制作为「最外层硬约束门」，趋势末端强制缩仓/禁顺势单
  - algomatrix.trade：Liquidity Sweep(影线刺穿摆动点收回) = SMC 最高概率时机工具
  - HuggingFace XAUUSD-ML-Trader：CHoCH/BOS + 流动性扫荡 = 结构转弱铁证

设计铁律：
  - 只在「强证据」(体制末端 + 高延伸 + SMC结构证伪 多重共振) 才发反转信号，
    不轻易否定正常顺势单 → 符合「提准非拦截」，只杀明显接飞刀，不杀好信号。
  - 纯行情、全局共享、多账号优先（不绑定账号）。
"""


def evaluate(market_data: dict) -> dict:
    regime = (market_data or {}).get("regime") or {}
    smc = (market_data or {}).get("smc_features") or {}
    per_tf = smc.get("per_tf", {}) or {}

    at_top = bool(regime.get("at_stale_top", False))
    at_bottom = bool(regime.get("at_stale_bottom", False))
    ext_z = float(regime.get("extension_z", 0) or 0)
    rsi_h1 = float(regime.get("rsi_h1", 50) or 50)
    global_bias = smc.get("global_bias", "neutral")

    evidence = []
    sell_score = 0.0
    buy_score = 0.0

    # ── SMC 结构证伪扫描（ Liquidity Sweep / 溢价区 / CHoCH ） ──
    down_sweep = False
    up_sweep = False
    for tf, d in per_tf.items():
        if not isinstance(d, dict) or not d.get("available"):
            continue
        for s in (d.get("liquidity_sweeps") or []):
            if s.get("type") == "down":
                down_sweep = True
            elif s.get("type") == "up":
                up_sweep = True
        pd = d.get("premium_discount") or {}
        if pd.get("current_zone") == "premium":
            evidence.append(f"{tf}当前处于溢价高位区(禁追多,等折扣区)")
        elif pd.get("current_zone") == "discount":
            evidence.append(f"{tf}当前处于折扣低位区(禁追空,等溢价区)")

    # ── 山顶 REVERSE_SELL（针对 BUY） ──
    # ★ 2026-08-12 根因修复（AI 逆势开 SELL·端到端审计）：
    #   REVERSE_SELL 仅在「真山顶」(at_top) 触发。bullish 体制下 ext_z>2.5 单纯延伸 =
    #   趋势延续(黄金强趋势常态)，绝不作为反转/做空依据。旧逻辑 (ext_z>2.5 独立触发) 在多头里
    #   频繁误发 REVERSE_SELL → 注入云模型 prompt 被误读为"做空指令" → 逆势开 SELL(用户实测
    #   5m/15m 偏多时大脑仍傻开 sell)。现要求 at_top(真山顶) + 结构/超买共振(总分≥0.7) 才发，
    #   提准非拦截：正常顺势多单不受影响，只杀真山顶接飞刀。
    if at_top:
        sell_score += 0.5
        evidence.append(
            f"体制:趋势末端/高位延伸区(延伸Z={ext_z}, RSI_H1={rsi_h1}>72)"
        )
        if down_sweep:
            sell_score += 0.25
            evidence.append("SMC出现向下流动性扫荡(影线刺穿摆动高收回=结构转弱)")
        if ext_z > 2.5:
            sell_score += 0.15
            evidence.append(f"价格延伸度Z={ext_z}>2.5(统计罕见延伸,回归概率骤升)")
        if global_bias == "bullish" and rsi_h1 > 75:
            sell_score += 0.1
            evidence.append(f"H1 RSI={rsi_h1}极端超买(黄金阈值72+)")
    else:
        # 非真山顶：bullish 延伸/超买 = 顺势延续，仅记录，绝不误发 REVERSE_SELL
        if ext_z > 2.5 and global_bias == "bullish":
            evidence.append(
                f"价格延伸度Z={ext_z}>2.5 但非真山顶(at_top=False)→多头延续,不判反转(提准非拦截)"
            )
        if global_bias == "bullish" and rsi_h1 > 75:
            evidence.append(f"H1 RSI={rsi_h1}>75 但多头延续中可长期钝化→不判反转")

    # ── 谷底 REVERSE_BUY（针对 SELL） ──（对称修复：仅真谷底 at_bottom 触发）
    if at_bottom:
        buy_score += 0.5
        evidence.append(
            f"体制:趋势末端/低位超卖区(延伸Z={ext_z}, RSI_H1={rsi_h1}<28)"
        )
        if up_sweep:
            buy_score += 0.25
            evidence.append("SMC出现向上流动性扫荡(影线刺穿摆动低收回=结构转强)")
        if ext_z < -2.5:
            buy_score += 0.15
            evidence.append(f"价格延伸度Z={ext_z}<-2.5(统计罕见超卖,反弹概率骤升)")
        if global_bias == "bearish" and rsi_h1 < 25:
            buy_score += 0.1
            evidence.append(f"H1 RSI={rsi_h1}极超卖(黄金阈值28-)")
    else:
        if ext_z < -2.5 and global_bias == "bearish":
            evidence.append(
                f"价格延伸度Z={ext_z}<-2.5 但非真谷底(at_bottom=False)→空头延续,不判反转"
            )

    # ── 裁决 ──
    # 仅「真山顶/真谷底」(at_top/at_bottom) + 多重共振(总分≥0.7) 才发反转信号，
    # 杜绝 bullish 延伸/超买被误判为反转 → 根治逆势开 SELL。
    if at_top and sell_score >= 0.7:
        signal = "REVERSE_SELL"
        confidence = min(0.95, 0.6 + sell_score * 0.2)
        reasoning = "真山顶(at_top)确认 + SMC结构转弱/超买共振：当前价位顺势追BUY=接飞刀，应等回调至机构需求区或反转确认"
    elif at_bottom and buy_score >= 0.7:
        signal = "REVERSE_BUY"
        confidence = min(0.95, 0.6 + buy_score * 0.2)
        reasoning = "真谷底(at_bottom)确认 + SMC结构转强/超卖共振：当前价位顺势追SELL=接刀，应等反弹至机构供给区或反转确认"
    else:
        signal = "NONE"
        confidence = 0.0
        reasoning = "无真山顶/真谷底反转证据，趋势跟踪主导(提准非拦截)"

    return {
        "signal": signal,
        "confidence": round(confidence, 3),
        "evidence": evidence,
        "reasoning": reasoning,
        "extension_z": round(ext_z, 3),
        "rsi_h1": round(rsi_h1, 1),
        "global_bias": global_bias,
        # ★ 2026-08-15 审计P3修复：补齐 meta_agent L1213/L1219 消费的 4 个布尔字段
        #   （原缺失 → get() 恒 False → 哨兵对 SELL/BUY 的体制背书通道双侧死代码）。
        #   语义：at_extreme_high=真山顶(at_top)、overbought=山顶或 H1 RSI 极端超买(>72)、
        #   at_stale_bottom=真谷底(at_bottom)、oversold=谷底或 H1 RSI 极端超卖(<28)。
        #   （阈值与上方评分逻辑对齐，仅补「是否极端」布尔，不改变反转信号判定。）
        "at_extreme_high": bool(at_top),
        "overbought": bool(at_top) or rsi_h1 > 72,
        "at_stale_bottom": bool(at_bottom),
        "oversold": bool(at_bottom) or rsi_h1 < 28,
    }
