"""决策闸门：纯函数、零外部依赖，便于单测与审计复用。

逆共识高置信闸门 —— 基于大脑闭环审计「发现1」：
  227 笔 AI 决策 / 146 已平仓中，META 逆三脑(DS/HY/Chronos)共识单胜率 51%
  < 共识单 56%，元智能体独立 override 在小幅拖累信号准度。
设计原则「提准非拦截」：逆共识低置信时**降级采用共识方向**(保留交易、不腰斩笔数)，
而非 HOLD/拦杀；仅高置信才放行逆共识。
"""


def consensus_dir_of(ds_final, hy_final, chronos_dir):
    """三脑(DS/HY/Chronos)多数方向；不足 2/3 同向返回 None（无明确共识可降级）。"""
    votes = [v for v in (ds_final, hy_final, chronos_dir) if v in ("BUY", "SELL")]
    buy = votes.count("BUY")
    sell = votes.count("SELL")
    if buy >= 2:
        return "BUY"
    if sell >= 2:
        return "SELL"
    return None


def apply_contrarian_gate(final_decision, final_confidence,
                          ds_final, hy_final, chronos_dir, min_conf):
    """逆共识高置信闸门（提准非拦截）。

    当 META 终裁方向与三脑共识相反、且置信 < min_conf 时，降级采用共识方向；
    否则放行。返回 (new_decision, downgraded)。
    """
    if final_decision not in ("BUY", "SELL"):
        return final_decision, False
    cdir = consensus_dir_of(ds_final, hy_final, chronos_dir)
    if cdir and cdir != final_decision and final_confidence < min_conf:
        return cdir, True
    return final_decision, False
