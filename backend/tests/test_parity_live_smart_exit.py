"""用生产库里的真实数据跑一次录制→回放（V6 Phase -1 验收：比对台跑通真实决策）。

和 test_parity_smart_exit.py 的区别：那边是构造样本（保证分支覆盖），
这边是真实客户配置 + 真实成交价 + 真实 AI 决策，验证比对台在真数据形状上也站得住。

安全性：
  · 只读。用 sqlite mode=ro URI 打开，物理上写不进去，不会干扰运行中的后端。
  · 默认不跑（标 live）。需要时 `pytest -m live -k parity_live` 显式执行。
  · 库不在 / 无数据 → skip，不在别人机器上炸。

atr 说明：trades 表不落 ATR，此处取黄金常态值 12.0 并记在 meta 里。
它对"同一入参两次结果是否一致"的比对不构成影响。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.config import settings
from app.models.strategy import StrategyConfig
from app.services import smart_exit
from tests.parity import assert_parity, load_cases, recording, replay_all

TAG = "live.smart_exit.evaluate_position"
ASSUMED_ATR = 12.0
MAX_SAMPLES = 30

pytestmark = pytest.mark.live


def _readonly_engine():
    url = settings.get_database_url()
    if not url.startswith("sqlite"):
        pytest.skip(f"仅支持 sqlite 生产库，当前 {url.split(':')[0]}")
    db_path = Path(url.replace("sqlite:///", ""))
    if not db_path.exists():
        pytest.skip(f"生产库不存在：{db_path}")
    # mode=ro：物理只读，任何误写都会直接报错而不是污染生产库
    return create_engine(f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true",
                         connect_args={"uri": True})


def _load_real_inputs(conn):
    """真实策略配置 + 真实已平仓成交 → evaluate_position 入参。"""
    strat_rows = conn.execute(text(
        "SELECT * FROM strategy_configs LIMIT 10"
    )).mappings().all()
    if not strat_rows:
        pytest.skip("生产库无 strategy_configs")

    trade_rows = conn.execute(text(
        "SELECT action, volume, open_price, close_price, sl, tp, profit, "
        "       meta_agent_decision, meta_agent_confidence, mt5_ticket "
        "FROM trades "
        "WHERE open_price IS NOT NULL AND close_price IS NOT NULL "
        "  AND volume > 0 AND action IN ('buy','sell') "
        "ORDER BY created_at DESC LIMIT :n"
    ), {"n": MAX_SAMPLES}).mappings().all()
    if not trade_rows:
        pytest.skip("生产库无可用于回放的已平仓成交")

    # 用真实字段构造 ORM 实例（不入库、不绑 session，纯内存）
    cols = {c.name for c in StrategyConfig.__table__.columns}
    strategies = [StrategyConfig(**{k: v for k, v in row.items() if k in cols})
                  for row in strat_rows]

    inputs = []
    for i, t in enumerate(trade_rows):
        strategy = strategies[i % len(strategies)]
        inputs.append(dict(
            position={
                "ticket": t["mt5_ticket"],
                "type": t["action"],
                "volume": float(t["volume"]),
                "price_open": float(t["open_price"]),
                "price_current": float(t["close_price"]),   # 真实成交收盘价
                "sl": float(t["sl"] or 0),
                "tp": float(t["tp"] or 0),
                "profit": float(t["profit"] or 0),
            },
            atr=ASSUMED_ATR,
            ai_decision=(t["meta_agent_decision"] or "HOLD").upper(),
            ai_confidence=float(t["meta_agent_confidence"] or 0.0),
            strategy=strategy,
        ))
    return inputs


@pytest.mark.contract
def test_parity_live_record_replay_identical(tmp_path):
    """真实数据录制后回放，逐字段一致；顺带体检真实样本触达了哪些分支。"""
    engine = _readonly_engine()
    with engine.connect() as conn:
        inputs = _load_real_inputs(conn)

    with recording(TAG, out_dir=tmp_path) as rec:
        for kw in inputs:
            rec.capture(kwargs=kw, result=smart_exit.evaluate_position(**kw))

    cases = load_cases(TAG, out_dir=tmp_path)
    assert cases, "真实样本未落盘"
    assert not any(c.tainted for c in cases), "真实样本入参被脱敏污染，结论不可信"

    report = replay_all(cases, smart_exit.evaluate_position)
    assert report.total == len(cases)
    assert_parity(report)

    actions = sorted({c.expected["action"] for c in cases})
    print(f"\n[比对台·真实数据] {len(cases)} 条样本，覆盖动作：{actions}")
