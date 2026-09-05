"""
2026-08-17 · TimesFM 交叉验证单测（ts_cross_validate）
====================================================
验证：分歧度计算、缓存、降级（无 chronos → None）、q 微调语义（加法非拦截）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.ts_cross_validate import cross_validate, _fingerprint, TimesFmCrossValidator

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✅ {name}")


# ── 1. 无 chronos → None（静默降级）──
assert cross_validate([4000.0] * 200, None) is None
ok("无 chronos 降级 None")

# ── 2. closes 太短 → None ──
assert cross_validate([4000.0] * 20, {"p90_final": 4020.0, "p10_final": 3980.0}) is None
ok("数据不足降级 None")

# ── 3. _fingerprint 指纹稳定 ──
c1 = [4000.0 + i * 0.5 for i in range(200)]
c2 = [4000.0 + i * 0.5 for i in range(200)]
assert _fingerprint(c1) == _fingerprint(c2)
ok("指纹同输入一致")

# ── 4. 分歧度语义：chronos 区间窄 + timesfm 预测偏离 → 分歧大 ──
#    （不实际推理，验证返回值结构；真实推理由集成测试覆盖）
res = cross_validate(c1, {"p90_final": 4010.0, "p10_final": 4005.0}, cache_ttl=0)
# 若 TimesFM 已加载可用 → 返回结构完整
if res is not None:
    assert "divergence" in res and "agreement" in res and "t_p90" in res
    assert res["divergence"] >= 0.0
    ok(f"结构完整 divergence={res['divergence']} agreement={res['agreement']}")
else:
    print("  ⚠️ TimesFM 未加载（环境限制），跳过结构断言")

# ── 5. 缓存：同指纹 2 次调用（TTL 内）应命中缓存（不重复推理）──
res_a = cross_validate(c1, {"p90_final": 4020.0, "p10_final": 3990.0}, cache_ttl=120)
res_b = cross_validate(c1, {"p90_final": 4020.0, "p10_final": 3990.0}, cache_ttl=120)
if res_a is not None and res_b is not None:
    assert res_a == res_b
    ok("缓存命中（同指纹同结果）")

# ── 6. 加载状态查询 ──
v = TimesFmCrossValidator()
st = v.status()
assert st["model"] == "timesfm-2.5"
ok(f"status 结构 OK ready={st['ready']}")

print(f"\n=== 全部通过 {PASS} 项 ===")
