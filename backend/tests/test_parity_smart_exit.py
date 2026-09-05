"""回归比对台自检 + smart_exit.evaluate_position 首个真实决策的录制/回放。

分两类：
  1. 比对台自身的有效性（最关键）—— 必须证明它真能抓出差异。
     一个只会说"通过"的比对台比没有比对台更危险，因为它会给人虚假的安全感。
  2. 对 evaluate_position 的录制→回放→逐字段一致（V6 Phase -1 验收第三条）。

样本刻意覆盖 evaluate_position 的多条互斥分支（反向信号 / HIGH 质量吃满 /
早期保本 / 分批止盈 / 数据不足），这样将来重构任何一条分支都会被这台机器逮到。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.strategy import StrategyConfig
from app.services import smart_exit
from tests.parity import (
    assert_parity,
    diff_values,
    from_jsonable,
    load_cases,
    recording,
    replay_all,
    to_jsonable,
)
from tests.parity import recorder as parity_recorder

TAG = "smart_exit.evaluate_position"


# ────────────────────────── 样本工厂 ──────────────────────────

def _strategy() -> StrategyConfig:
    """真实 ORM 实例（不入库）。用它而不是 dict，是为了顺带验证
    serde 对 SQLAlchemy 模型走 __table__ 摊平的那条路径。"""
    return StrategyConfig(
        smart_tp_enabled=True,
        tp1_atr_mult=1.0, tp1_close_pct=0.40,
        tp2_atr_mult=1.5, tp2_close_pct=0.30,
        tp3_atr_mult=2.5, tp3_close_pct=0.20,
        breakeven_after_tp1=True, breakeven_buffer_points=0.5,
        trailing_atr_mult=1.5, trailing_activate_after_tp2=True,
        enable_trailing_sl=True,
        ai_reverse_close_confidence=0.60,
    )


def _pos(**kw) -> dict:
    base = {
        "ticket": 900001, "type": "buy", "volume": 0.10,
        "price_open": 4250.00, "price_current": 4250.20,
        "sl": 4240.00, "tp": 4270.00, "profit": 2.0,
    }
    base.update(kw)
    return base


def _call_kwargs() -> list[dict]:
    """一组覆盖互斥分支的入参。每条都对应 evaluate_position 里一个 return 点。"""
    s = _strategy()
    return [
        # ① 数据不足 → hold
        dict(position=_pos(volume=0), atr=12.0, ai_decision="BUY",
             ai_confidence=0.8, strategy=s),
        # ② AI 反向且置信达阈值 → reverse_signal
        dict(position=_pos(type="buy"), atr=12.0, ai_decision="SELL",
             ai_confidence=0.82, strategy=s, ai_reverse_th=0.60),
        # ③ HIGH 质量 · BUY 触及 P90 → 全平吃满
        dict(position=_pos(type="buy", price_current=4281.00), atr=12.0,
             ai_decision="BUY", ai_confidence=0.55, strategy=s,
             quality_regime="HIGH", chronos_tp_ceiling=4280.00, chronos_p10=4230.00),
        # ④ HIGH 质量 · SELL 触及 P10 → 全平（方向感知修复后的正确行为）
        dict(position=_pos(type="sell", price_open=4250.0, price_current=4229.0,
                           sl=4260.0, tp=4230.0), atr=12.0,
             ai_decision="SELL", ai_confidence=0.55, strategy=s,
             quality_regime="HIGH", chronos_tp_ceiling=4280.00, chronos_p10=4230.00),
        # ⑤ 早期保本（move ≥ 0.15×ATR 且 SL 在错误一侧）
        dict(position=_pos(type="buy", price_current=4253.00, sl=4240.00),
             atr=12.0, ai_decision="HOLD", ai_confidence=0.30, strategy=s),
        # ⑥ TP1 分批（move_atr 1.02 ≥ 1.0 且 < 保本地板 floor+buffer=12.5 → partial 40%）
        #   ★ 2026-08-17：旧参数 move=32 触发早期保本先 return（2026-08-13 防噪音修复
        #   后保本分支在 TP 分批之前），partial_close 分支无法覆盖 → 改 move 落入
        #   [tp1×ATR, floor+buffer) 极窄窗口。
        dict(position=_pos(type="buy", price_current=4262.20, sl=4250.50),
             atr=12.0, ai_decision="HOLD", ai_confidence=0.30, strategy=s),
        # ⑦ LOW 质量啃头皮（阈值 ×0.6）
        dict(position=_pos(type="buy", price_current=4269.00, sl=4250.50),
             atr=12.0, ai_decision="HOLD", ai_confidence=0.30, strategy=s,
             quality_regime="LOW"),
    ]


# ────────────────────── 一、比对台自身有效性 ──────────────────────

@pytest.mark.unit
def test_serde_roundtrip_keeps_orm_fields_readable():
    """ORM 实例摊平后还原成 SimpleNamespace，getattr 取值必须和原来一样。
    这是回放能成立的前提（smart_exit._cfg 全靠 getattr）。"""
    s = _strategy()
    revived = from_jsonable(to_jsonable(s))

    assert getattr(revived, "__parity_type__", "") == "StrategyConfig"
    for field in ("tp1_atr_mult", "tp3_close_pct", "ai_reverse_close_confidence",
                  "smart_tp_enabled", "enable_trailing_sl"):
        assert getattr(revived, field) == getattr(s, field), field


@pytest.mark.unit
def test_serde_redacts_secrets():
    """密码/令牌类字段绝不落盘。"""
    payload = {"password": "hunter2", "api_key": "sk-xxx", "volume": 0.1}
    dumped = to_jsonable(payload)
    assert dumped["password"] == "<redacted>"
    assert dumped["api_key"] == "<redacted>"
    assert dumped["volume"] == 0.1


@pytest.mark.unit
def test_serde_handles_nan_and_inf():
    """NaN/Inf 不是合法 JSON，必须包装后往返无损。"""
    revived = from_jsonable(to_jsonable({"a": float("nan"), "b": float("inf")}))
    assert revived["a"] != revived["a"]          # NaN
    assert revived["b"] == float("inf")


@pytest.mark.unit
@pytest.mark.parametrize("expected,actual,kind", [
    ({"a": 1}, {"a": 2}, "value"),
    ({"a": 1}, {}, "missing"),
    ({}, {"a": 1}, "extra"),
    ({"a": 1}, {"a": "1"}, "type"),
    ({"a": True}, {"a": 1}, "type"),            # bool 不等于 int
    ({"a": [1, 2]}, {"a": [1]}, "length"),
    ({"a": None}, {"a": 0}, "value"),
])
def test_asserter_catches_every_kind_of_change(expected, actual, kind):
    """反向验证：断言器必须能抓出每一类差异，且分类正确。"""
    diffs = diff_values(expected, actual)
    assert diffs, f"应当抓到 {kind} 差异却放行了"
    assert any(d.kind == kind for d in diffs), \
        f"差异类型应为 {kind}，实际 {[d.kind for d in diffs]}"


@pytest.mark.unit
def test_asserter_tolerates_only_float_noise():
    """末位浮点抖动放行，真实数值变化（哪怕只有 0.01）必须报。"""
    assert not diff_values({"sl": 4250.5}, {"sl": 4250.5 + 1e-12})
    assert diff_values({"sl": 4250.50}, {"sl": 4250.51})


# ─────────────────── 二、evaluate_position 录制/回放 ───────────────────

@pytest.fixture
def recorded(tmp_path):
    """用真实函数跑一遍全部样本并落盘，返回 (录制目录, 样本数)。"""
    with recording(TAG, out_dir=tmp_path) as rec:
        cases = _call_kwargs()
        for kw in cases:
            result = smart_exit.evaluate_position(**kw)
            assert rec.capture(kwargs=kw, result=result), "样本落盘失败"
    return tmp_path, len(cases)


@pytest.mark.contract
def test_record_then_replay_is_field_identical(recorded):
    """V6 Phase -1 验收：真实决策录制后回放，逐字段一致。"""
    out_dir, n = recorded
    cases = load_cases(TAG, out_dir=out_dir)

    assert len(cases) == n, f"应读回 {n} 条样本，实得 {len(cases)}"
    assert not any(c.tainted for c in cases), "样本入参被脱敏污染，比对结论不可信"

    report = replay_all(cases, smart_exit.evaluate_position)
    assert report.total == n
    assert_parity(report)


@pytest.mark.contract
def test_samples_actually_cover_distinct_branches(recorded):
    """比对台的价值取决于样本覆盖度。若所有样本都返回同一个 action，
    那这台机器其实只在守一条分支 —— 这里把覆盖度本身钉成断言。"""
    out_dir, _ = recorded
    actions = {c.expected["action"] for c in load_cases(TAG, out_dir=out_dir)}
    assert {"hold", "reverse_signal", "full_close", "partial_close"} <= actions, \
        f"分支覆盖不足，实际只覆盖 {actions}"


@pytest.mark.contract
def test_parity_fails_when_behavior_silently_changes(recorded):
    """核心反向验证：模拟一次"手滑改错阈值"的重构，比对台必须炸。

    这里把早期保本触发系数从 0.15 改成 0.5（一个看起来无害的调参），
    比对台应当立刻指出受影响样本的 new_sl / reason 变了。
    """
    out_dir, _ = recorded
    cases = load_cases(TAG, out_dir=out_dir)

    def mutated(**kwargs):
        """行为被改动的"新版"函数：早期保本更难触发。"""
        pos = dict(kwargs["position"])
        atr = float(kwargs["atr"])
        # 只在早期保本这一段做手脚：把本该触发的样本压回不触发
        move = ((float(pos.get("price_current") or 0) - float(pos.get("price_open") or 0))
                if (pos.get("type") == "buy")
                else (float(pos.get("price_open") or 0) - float(pos.get("price_current") or 0)))
        if 0.15 * atr <= move < 0.5 * atr:
            pos["price_current"] = pos["price_open"]     # 抹平浮盈 → 不再保本
            kwargs = {**kwargs, "position": pos}
        return smart_exit.evaluate_position(**kwargs)

    report = replay_all(cases, mutated)
    assert not report.ok, "行为已被改动，比对台却报了通过 —— 安全网是瞎的"
    assert report.mismatched >= 1
    assert "new_sl" in report.render() or "reason" in report.render(), \
        f"差异未定位到具体字段：\n{report.render()}"


@pytest.mark.contract
def test_replay_reports_exception_instead_of_crashing(recorded):
    """回放中单条抛异常应记成差异并继续，不能让整批比对中断。"""
    out_dir, n = recorded

    def boom(**_kwargs):
        raise RuntimeError("模拟新版实现崩了")

    report = replay_all(load_cases(TAG, out_dir=out_dir), boom)
    assert report.total == n and report.mismatched == n
    assert "RuntimeError" in report.render()


# ─────────────────── 三、录制器不得伤害生产 ───────────────────

@pytest.mark.unit
def test_recorder_is_off_by_default(monkeypatch):
    """没有 WX_PARITY_RECORD 时 install 必须是空操作 —— 录制器绝不能默认上生产。"""
    monkeypatch.delenv("WX_PARITY_RECORD", raising=False)
    holder = SimpleNamespace(fn=lambda x: x * 2)
    assert parity_recorder.install(holder, "fn", tag="noop") is None
    assert not getattr(holder.fn, "__parity_wrapped__", False)


@pytest.mark.unit
def test_recorder_wrapper_is_transparent_and_reversible(tmp_path, monkeypatch):
    """包装后：返回值不变、业务异常照抛、可完整卸载。"""
    monkeypatch.setenv("WX_PARITY_RECORD", "1")
    parity_recorder.reset_counts("transparent")

    def fn(x, *, k=1):
        if x < 0:
            raise ValueError("业务异常必须原样抛出")
        return {"v": x * k}

    holder = SimpleNamespace(fn=fn)
    parity_recorder.install(holder, "fn", tag="transparent", out_dir=tmp_path)

    assert holder.fn(3, k=2) == {"v": 6}
    with pytest.raises(ValueError):
        holder.fn(-1)

    # 重复 install 不套娃
    parity_recorder.install(holder, "fn", tag="transparent", out_dir=tmp_path)
    assert holder.fn(2, k=2) == {"v": 4}

    cases = load_cases("transparent", out_dir=tmp_path)
    assert len(cases) == 2, "只有成功调用才录制，异常调用不录"
    assert cases[0].args == [3] and cases[0].kwargs == {"k": 2}

    assert parity_recorder.uninstall(holder, "fn") is True
    assert holder.fn is fn


@pytest.mark.unit
def test_recorder_never_raises_on_bad_payload(tmp_path):
    """落盘失败（不可序列化/磁盘问题）只能静默返回 False，绝不冒泡到交易主循环。"""
    with recording("bad", out_dir=tmp_path / "nested" / "deep") as rec:
        class Weird:
            __slots__ = ()          # 无 __dict__ 无 __table__ → 走 repr 分支
        assert rec.capture(kwargs={"o": Weird()}, result={"ok": 1}) is True

    with recording("capped", out_dir=tmp_path, max_records=2) as rec:
        assert rec.capture(kwargs={}, result=1) is True
        assert rec.capture(kwargs={}, result=2) is True
        assert rec.capture(kwargs={}, result=3) is False, "超过上限应停录而不是写爆磁盘"
