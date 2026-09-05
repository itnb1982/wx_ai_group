"""
风控事件流 + 决策快照 — 「为什么没开单」这条链路的守护测试（Phase 4 / V6）

这个文件盯三类会静默腐化的东西：

1. **语义契约会被"顺手简化"掉**。
   `chronos_weight == 0`（本地模型没参与）和 `chronos_vote == "HOLD"`（本地模型
   建议观望）票面完全一样，但含义相反。任何一次"这里判断重复了，删一个吧"的
   重构，都会把前者伪装成后者，前端于是把"模型掉线"画成"模型说别动"。
   客户据此以为系统在思考，实际上系统在瞎。

2. **事件码和标签会漂移**。码在 risk_engine / trade_executor 两处产生，
   标签在 risk_event_log 一处维护。新增码却忘了加标签，症状是前端弹出一串
   `PER_TRADE_RISK_LIMIT` 英文给客户看 —— 上线前没人会发现，因为它不报错。
   所以这里直接扫源码做覆盖率断言。

3. **保留窗口会失效**。ai_activities 无界增长已经让这个系统吃过一次亏
   （几百兆的 SQLite、查询变慢、备份变重）。修剪逻辑一旦被改坏，
   同样是几个月后才爆发。这里用真实 DB 把它钉住。
"""
import json
import re
from pathlib import Path

import pytest

# 破环：app.core.__init__ → deepseek_client → app.services → trade_executor →
# app.core.debate_engine 构成 import 环，先让 app.services 完成初始化。
import app.services.trade_executor  # noqa: F401  （仅为破环，勿删）

from app.services.decision_snapshot import (
    build_decision_snapshot,
    flat_columns,
    snapshot_to_json,
)
from app.services.risk_engine import RejectCode, Reason, reason_code
from app.services.risk_event_log import CODE_LABELS, code_label

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[1]


class _FakeDecision:
    """鸭子类型的 DebateDecision。刻意不 import 真的 dataclass ——
    快照函数对入参的宽容度本身就是被测行为之一。"""

    def __init__(self, **kw):
        defaults = dict(
            decision="BUY", confidence=0.72, risk_level="medium",
            consensus="strong", plain_summary="三方一致看多",
            deepseek_vote="BUY", deepseek_weight=0.4,
            hunyuan_vote="BUY", hunyuan_weight=0.35,
            chronos_vote="BUY", chronos_weight=0.25,
            chronos_agree=True, quality_regime="HIGH",
            q_score=0.81, chronos_p10=4200.5, chronos_p50=4215.0,
            chronos_tp_ceiling=4232.5,
            position_intent="open", target_risk_pct=1.2,
            portfolio_state="normal",
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


# ══════════════════════════════════════════════════════════════
# 1. 决策快照
# ══════════════════════════════════════════════════════════════
def test_snapshot_carries_all_three_votes():
    """决策票必须都在快照里 —— 前端辩论擂台画气泡的数据基础。

    ★ 2026-08-17 契约更新：2026-08-14 视觉票 / 2026-08-10 融合票 /
      2026-08-15 副驾票加入决策链后，快照含 5 票（deepseek/hunyuan/chronos
      /fusion/vision/copilot 中可用者）。本断言不再限定票数，改为守护
      「chronos 票与权重契约」+「degrade_level」核心字段。
    """
    snap = build_decision_snapshot(_FakeDecision(), degrade_level="L0")
    votes = snap["votes"]
    assert {"deepseek", "hunyuan", "chronos"} <= set(votes.keys())
    assert votes["chronos"]["vote"] == "BUY"
    assert votes["chronos"]["weight"] == pytest.approx(0.25)
    assert snap["degrade_level"] == "L0"


def test_chronos_unavailable_is_not_the_same_as_hold():
    """★ 语义契约：weight==0 → available=False，哪怕票面写着 BUY。

    这条断言存在的唯一目的，就是让"把 available 简化成 vote != 'HOLD'"
    这类重构当场失败。
    """
    snap = build_decision_snapshot(_FakeDecision(chronos_weight=0.0, chronos_vote="BUY"))
    assert snap["votes"]["chronos"]["available"] is False

    # 反过来：模型在线且明确建议观望 —— available 必须为 True
    snap2 = build_decision_snapshot(_FakeDecision(chronos_weight=0.2, chronos_vote="HOLD"))
    assert snap2["votes"]["chronos"]["available"] is True
    assert snap2["votes"]["chronos"]["vote"] == "HOLD"


def test_p90_and_tp_ceiling_are_the_same_number():
    """tp_ceiling 就是 P90。两个键并存只为兼容旧前端，值必须一致，
    否则前端画出来的分位带和实际止盈天花板对不上。"""
    snap = build_decision_snapshot(_FakeDecision())
    q = snap["quality"]
    assert q["p90"] == q["chronos_tp_ceiling"] == pytest.approx(4232.5)
    assert q["p10"] == pytest.approx(4200.5)


def test_nan_and_inf_become_none_not_zero():
    """NaN/inf 会让 json.dumps 产出非法字面量（NaN），前端 JSON.parse 直接抛。
    同样重要的是：缺失值不能被伪装成 0.0 —— Q=0 是"质量极差"，
    Q=None 是"没算出来"，两者在复盘时的结论完全不同。"""
    snap = build_decision_snapshot(_FakeDecision(q_score=float("nan"),
                                                 chronos_p10=float("inf"),
                                                 target_risk_pct=None))
    assert snap["quality"]["q_score"] is None
    assert snap["quality"]["p10"] is None
    assert snap["position"]["target_risk_pct"] is None
    # 必须是合法 JSON（json.loads 不接受 NaN 需显式关闭，这里用严格模式验证）
    txt = snapshot_to_json(snap)
    json.loads(txt, parse_constant=_reject_constant)


def _reject_constant(x):
    raise AssertionError(f"快照里混进了非法 JSON 字面量: {x}")


def test_snapshot_never_raises_on_garbage_input():
    """快照在下单主链路上。入参再脏也只能降级，不能把开仓搞失败。"""
    assert build_decision_snapshot(None) == {}
    assert build_decision_snapshot(object())["decision"] == "HOLD"
    assert snapshot_to_json({}) is None


def test_flat_columns_extract_filterable_dimensions():
    """三个平铺列是要被 WHERE / GROUP BY 命中的，不能只躺在 JSON 里。"""
    snap = build_decision_snapshot(_FakeDecision(), degrade_level="L2")
    flat = flat_columns(snap)
    assert flat == {"chronos_vote": "BUY", "q_score": pytest.approx(0.81),
                    "degrade_level": "L2"}
    # 空快照不能炸，返回全 None
    assert flat_columns({}) == {"chronos_vote": None, "q_score": None,
                                "degrade_level": None}


# ══════════════════════════════════════════════════════════════
# 2. 风控事件码：零破坏注入
# ══════════════════════════════════════════════════════════════
def test_reason_is_still_a_plain_string():
    """★ Reason 是 str 子类，携带 .code 的同时必须完全兼容旧调用点。

    现有代码里有 8 处 `passed, reason = fn(...)` 加字符串比较/拼接。
    如果 Reason 变成 dataclass 或 tuple，这些地方会静默错乱
    （比如 f-string 拼出 <Reason object at 0x...> 写进客户看的错误提示）。
    """
    r = Reason("点差过宽(5.2 > 4.0)", RejectCode.SPREAD_TOO_WIDE)
    assert isinstance(r, str)
    assert r == "点差过宽(5.2 > 4.0)"
    assert f"拒绝：{r}".endswith("4.0)")
    assert reason_code(r) == "SPREAD_TOO_WIDE"
    # 普通字符串取码不能抛，返回空串
    assert reason_code("某个没带码的老原因") == ""


def test_reject_codes_property_fills_unknown():
    """没带码的原因不能让整条事件失去可聚合性，兜底成 UNKNOWN。"""
    from app.services.risk_engine import RiskCheckResult

    res = RiskCheckResult(passed=False)
    res.reject_reasons = [
        Reason("单笔风险 3.1% 超上限 2.0%", RejectCode.PER_TRADE_RISK_LIMIT),
        "历史遗留的裸字符串原因",
    ]
    assert res.reject_codes == ["PER_TRADE_RISK_LIMIT", "UNKNOWN"]


# ══════════════════════════════════════════════════════════════
# 3. 码 ↔ 标签 防漂移（新增码忘加标签 → 这里红）
# ══════════════════════════════════════════════════════════════
def test_every_reject_code_has_a_label():
    codes = {v for k, v in vars(RejectCode).items()
             if not k.startswith("_") and isinstance(v, str)}
    missing = sorted(codes - set(CODE_LABELS))
    assert not missing, f"RejectCode 新增了码但没在 CODE_LABELS 登记中文标签: {missing}"


def test_executor_literal_codes_have_labels():
    """执行器里的码是写死的字面量（EXECUTOR_* / DEGRADE_*）。
    直接扫源码，避免"加了新拦截点却没登记标签"这种只有客户才发现的问题。"""
    src = (BACKEND_DIR / "app" / "services" / "trade_executor.py").read_text(encoding="utf-8")
    # 只扫代码，注释里出现的码不算（Phase 6 踩过注释误伤的坑）
    code_only = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    literals = set(re.findall(r'codes=\[\s*"([A-Z0-9_]+)"', code_only))
    assert literals, "没扫到任何 codes=[...] 字面量，说明接线被删了或写法变了"
    missing = sorted(literals - set(CODE_LABELS))
    assert not missing, f"执行器用了未登记标签的事件码: {missing}"


def test_code_label_falls_back_to_raw_code():
    """未登记的码原样返回，而不是变成空串 —— 露出英文码总好过什么都不显示。"""
    assert code_label("SOME_NEW_CODE") == "SOME_NEW_CODE"
    assert code_label("SPREAD_TOO_WIDE") == "点差过宽"


# ══════════════════════════════════════════════════════════════
# 4. 保留窗口：真实 DB
# ══════════════════════════════════════════════════════════════
@pytest.fixture
def temp_event_db(tmp_path, monkeypatch):
    """把 SessionLocal 换成临时库。

    ★ 必须 setattr 到模块对象上，不能 monkeypatch.setitem(sys.modules, ...)：
      risk_event_log 内部是 `from app.database import SessionLocal`（函数级），
      每次调用都会重新取模块属性，patch 属性才生效。
      （这个坑在 Phase 3 已经踩过一次。）
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.database as dbmod
    from app.models.risk_event import RiskEvent

    engine = create_engine(f"sqlite:///{tmp_path}/evt.db",
                           connect_args={"check_same_thread": False})
    # 只建这一张表：SQLite 建表时不校验外键引用表是否存在
    RiskEvent.__table__.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    return Session


def test_event_written_and_queryable(temp_event_db):
    from app.services.risk_event_log import record_risk_event, query_risk_events

    ok = record_risk_event(
        user_id="u1", mt5_account_id="acc1",
        event_type="reject", stage="risk_engine",
        codes=[RejectCode.DAILY_LOSS_LIMIT],
        reasons=["当日亏损 -3.2% 超上限 -3.0%"],
        direction="buy", intended_lots=0.12, confidence=0.7,
        degrade_level="L0",
    )
    assert ok is True

    rows = query_risk_events(user_id="u1", mt5_account_id="acc1", limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["codes"] == ["DAILY_LOSS_LIMIT"]
    assert r["direction"] == "BUY"          # 统一大写，前端不必再 normalize
    assert r["intended_lots"] == pytest.approx(0.12)
    assert r["degrade_level"] == "L0"


def test_write_failure_never_raises(monkeypatch):
    """★ 硬约束：记录被拦这件事，绝不能反过来把交易搞挂。"""
    import app.database as dbmod

    def _boom():
        raise RuntimeError("DB 挂了")

    monkeypatch.setattr(dbmod, "SessionLocal", _boom)
    from app.services.risk_event_log import record_risk_event

    assert record_risk_event(user_id="u1", mt5_account_id="a", codes=["X"],
                             reasons=["y"]) is False   # 返回 False，不抛


def test_retention_window_prunes_old_events(temp_event_db):
    """写超过保留窗口后，旧事件必须被裁掉；且裁的是旧的、留的是新的。"""
    from app.services.risk_event_log import (
        record_risk_event, query_risk_events, prune_account_events,
    )

    for i in range(30):
        record_risk_event(user_id="u1", mt5_account_id="acc1",
                          codes=["SPREAD_TOO_WIDE"], reasons=[f"第{i}条"])

    deleted = prune_account_events("acc1", keep=10)
    assert deleted == 20

    rows = query_risk_events(user_id="u1", mt5_account_id="acc1", limit=200)
    assert len(rows) == 10
    # 留下的必须是最后写的那批
    assert "第29条" in rows[0]["reasons"]


def test_prune_noop_when_under_window(temp_event_db):
    """没超窗口时一条都不能删 —— 修剪逻辑写错方向的典型症状是把新数据删光。"""
    from app.services.risk_event_log import record_risk_event, prune_account_events

    for i in range(5):
        record_risk_event(user_id="u1", mt5_account_id="acc1",
                          codes=["MAX_POSITIONS"], reasons=[f"第{i}条"])
    assert prune_account_events("acc1", keep=100) == 0
