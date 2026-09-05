# 独立仿真：复刻 meta_agent SMC 硬翻向分支的新逻辑，验证两种场景
from collections import Counter

def simulate(ds_final, hy_final, chronos_dir, smc_bias, final_decision, final_confidence, at_extreme=False):
    _smc_bias = smc_bias.lower()
    _against = (_smc_bias=="bullish" and final_decision=="SELL") or (_smc_bias=="bearish" and final_decision=="BUY")
    # 三脑共识
    votes=[v for v in (ds_final,hy_final,chronos_dir) if str(v).upper() in ("BUY","SELL")]
    brain_cons=None
    if votes:
        bc=Counter(str(v).upper() for v in votes)
        if bc.most_common(1)[0][1]>=2: brain_cons=bc.most_common(1)[0][0]
    if not _against:
        return f"不逆订单流→保持{final_decision}(conf={final_confidence:.2f})"
    _flip_to="BUY" if _smc_bias=="bullish" else "SELL"
    _at_extreme=at_extreme
    if brain_cons is not None:
        pen=final_confidence*0.85
        return f"三脑共识={brain_cons}→仅降权→{pen:.2f}(不翻向)"
    if final_confidence>=0.75:
        return f"高置信→降权→{final_confidence*0.85:.2f}(不翻向)"
    if _at_extreme:
        return "趋势末端→HOLD"
    return f"无共识→翻向{_flip_to}"

print("场景A（bug复现）：三脑SELL共识 + smc=bullish + final=SELL conf=0.66")
print("  ->", simulate("SELL","HOLD","SELL","bullish","SELL",0.66))
print("场景B（强共识）：三脑SELL + smc=bullish + final=SELL conf=0.80")
print("  ->", simulate("SELL","SELL","SELL","bullish","SELL",0.80))
print("场景C（无共识·FixA原意）：DS=BUY HY=SELL Ch=HOLD + smc=bullish + final=SELL conf=0.66")
print("  ->", simulate("BUY","SELL","HOLD","bullish","SELL",0.66))
print("场景D（三脑BUY + smc=bearish + final=BUY conf=0.66）")
print("  ->", simulate("BUY","HOLD","BUY","bearish","BUY",0.66))
