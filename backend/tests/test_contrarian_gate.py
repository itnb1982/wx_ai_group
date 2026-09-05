"""逆共识高置信闸门单元测试（纯函数，沙箱可跑，无需 MT5/torch）。

验证 decision_gates.apply_contrarian_gate / consensus_dir_of 的语义：
- 三脑多数方向（2/3 同向）才算共识，不足则无共识可降级
- 逆共识 + 低置信 → 降级采用共识方向（提准非拦截）
- 逆共识 + 高置信 → 放行（不降级）
- 无共识 / 方向一致 / HOLD → 原样放行（不误伤、不腰斩笔数）
"""
import pytest

from app.core.decision_gates import consensus_dir_of, apply_contrarian_gate


# ── consensus_dir_of ──
def test_consensus_dir_of_majority_buy():
    assert consensus_dir_of("BUY", "BUY", "SELL") == "BUY"
    assert consensus_dir_of("BUY", "SELL", "BUY") == "BUY"
    assert consensus_dir_of("SELL", "BUY", "BUY") == "BUY"


def test_consensus_dir_of_majority_sell():
    assert consensus_dir_of("SELL", "SELL", "BUY") == "SELL"
    assert consensus_dir_of("SELL", "BUY", "SELL") == "SELL"


def test_consensus_dir_of_no_consensus():
    # 1 BUY / 1 SELL / 1 HOLD → 无 2/3 同向
    assert consensus_dir_of("BUY", "SELL", "HOLD") is None
    assert consensus_dir_of("BUY", "SELL", "NEUTRAL") is None
    # 全 HOLD
    assert consensus_dir_of("HOLD", "HOLD", "HOLD") is None


# ── apply_contrarian_gate ──
def test_gate_downgrades_low_conf():
    dec, down = apply_contrarian_gate("SELL", 0.55, "BUY", "BUY", "BUY", 0.80)
    assert dec == "BUY" and down is True


def test_gate_allows_high_conf():
    dec, down = apply_contrarian_gate("SELL", 0.90, "BUY", "BUY", "BUY", 0.80)
    assert dec == "SELL" and down is False


def test_gate_no_consensus_passthrough():
    dec, down = apply_contrarian_gate("SELL", 0.55, "BUY", "SELL", "HOLD", 0.80)
    assert dec == "SELL" and down is False


def test_gate_hold_passthrough():
    dec, down = apply_contrarian_gate("HOLD", 0.55, "BUY", "BUY", "BUY", 0.80)
    assert dec == "HOLD" and down is False


def test_gate_agrees_passthrough():
    dec, down = apply_contrarian_gate("BUY", 0.55, "BUY", "BUY", "BUY", 0.80)
    assert dec == "BUY" and down is False


def test_gate_threshold_edge():
    # 恰好等于阈值 → 不降级（阈值含于"放行"侧）
    dec, down = apply_contrarian_gate("SELL", 0.80, "BUY", "BUY", "BUY", 0.80)
    assert dec == "SELL" and down is False
