import sys, os, atexit

# 阻断 atexit 落盘，避免污染 meta_agent_state.json
atexit.register = lambda *a, **k: None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from app.core.meta_agent import MetaAgent, _normalize_decision

m = MetaAgent()


def mk(ds_d, ds_c, hy_d, hy_c, agree=False):
    """构造最小 adjudicate 输入"""
    ds_a = {"decision": ds_d, "confidence": ds_c,
            "risk_assessment": {"risk_score": 3, "volatility_regime": "正常"}}
    hy_a = {"decision": hy_d, "confidence": hy_c,
            "risk_assessment": {"risk_score": 3, "volatility_regime": "正常"}}
    ds_r = {"decision": ds_d, "confidence": ds_c, "agree_with_opponent": agree}
    hy_r = {"decision": hy_d, "confidence": hy_c, "agree_with_opponent": agree}
    md = {"timeframes": {"H1": {"trend": "normal"}}}
    return m.adjudicate(ds_a, hy_a, ds_r, hy_r, md)


print("=== 规范化测试 ===")
for raw, exp in [("buy", "BUY"), ("SELL", "SELL"), ("观望", "HOLD"),
                 ("买入", "BUY"), ("做空", "SELL"), ("", "HOLD"),
                 ("long", "BUY"), ("short", "SELL"), ("中性", "HOLD"),
                 ("FOO", "HOLD")]:
    got = _normalize_decision(raw)
    ok = "OK" if got == exp else "FAIL"
    print(f"  {ok}  {repr(raw):10s} -> {got} (期望 {exp})")

print("\n=== 裁决场景测试 (期望: 分歧/双观望→HOLD; 单模型低置信→HOLD; 单模型高置信/共识→开单) ===")
scenarios = [
    ("双反向 BUY vs SELL", "BUY", 0.7, "SELL", 0.7, "HOLD"),
    ("双观望", "HOLD", 0.6, "HOLD", 0.6, "HOLD"),
    ("单模型方向低置信 0.60", "BUY", 0.60, "HOLD", 0.5, "HOLD"),
    ("单模型方向临界 0.70", "BUY", 0.70, "HOLD", 0.5, "BUY"),
    ("单模型方向高置信 0.85", "BUY", 0.85, "HOLD", 0.5, "BUY"),
    ("双同向共识 0.60", "BUY", 0.60, "BUY", 0.60, "BUY"),
    ("双同向共识 0.55", "SELL", 0.55, "SELL", 0.55, "SELL"),
    ("中文决策 buy vs 观望", "买入", 0.85, "观望", 0.5, "BUY"),
    ("小写 buy vs sell 反向", "buy", 0.7, "sell", 0.7, "HOLD"),
]
all_ok = True
for name, dsd, dsc, hyd, hyc, exp in scenarios:
    r = mk(dsd, dsc, hyd, hyc)
    fd, fc = r.decision, r.confidence
    ok = "OK" if fd == exp else "FAIL"
    if fd != exp:
        all_ok = False
    print(f"  {ok}  {name:28s} -> {fd}({fc:.2f})  期望 {exp}")

print("\n结果:", "全部通过 ✅" if all_ok else "存在失败 ❌")
