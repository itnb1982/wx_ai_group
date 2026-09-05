# -*- coding: utf-8 -*-
"""
万象Ai · 大脑审计标准 + 记录器（统一审计大脑）

审计目的（用户铁律：信号准不准就是靠大脑）：
  对每一个"大脑"模型，按同一标准审计三件事：
    ① 接入（喂了什么）—— 决策时被喂入的真实数据字段 / 完整性 / 实时性
    ② 输出（输出了什么）—— 方向 / 置信 / 仓位意图 / 手数 / SL-TP / 平仓动作
    ③ 消费（下游听了多少）—— 该输出是否被 meta_agent / 执行器 / 平仓真正采用
  并预留「闭环准度归因」：模型原始票 vs 最终成交 / 盈亏（从生产库读）。

设计原则：
  · 零业务侵入：所有 record() 调用包 try/except，审计失败绝不拖垮交易主链路。
  · 独立存储：写到 data/brain_audit.db，不污染生产库 wx_prod.dat。
  · 纯标准库：仅 sqlite3/json/os/uuid/datetime，沙箱缺 numpy/fastapi 也能 import。
  · 可一键关：BRAIN_AUDIT_ENABLED=False 时 record() 直接 return（配置在 config.py）。
"""

import sqlite3
import json
import os
import uuid
import threading
from datetime import datetime, timezone

# ───────────────────────── 路径 ─────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.abspath(os.path.join(_BASE, "..", "data"))
os.makedirs(_DATA, exist_ok=True)
AUDIT_DB = os.path.join(_DATA, "brain_audit.db")

# 配置项（优先读 config，失败回退内置默认，保证可 import）
try:
    from app.core.config import settings
    _ENABLED = bool(getattr(settings, "BRAIN_AUDIT_ENABLED", True))
except Exception:
    _ENABLED = True

# ───────────────────────── 统一审计标准（契约） ─────────────────────────
# 这是审计的"标尺"：每个大脑必须被喂入 standardized 输入、产出 standardized 输出、被下游消费。
# 真实字段名来自代码核查（meta_agent.py / chronos_service.py / deepseek_client.py /
# trade_executor.py / smart_exit.py / local_llm_service.py）。
STANDARD = {
    "market_snapshot": {  # 所有大脑的共用「接入」对象（market_analyzer.get_market_snapshot）
        "fed_to": ["deepseek", "hunyuan", "chronos", "qwen3_copilot", "qwen3_proofread"],
        "required_fields": [
            "symbol", "bid", "ask", "spread",
            "timeframes(M1/M5/H1/H4).ohlc", "timeframes.*.atr", "timeframes.*.rsi",
            "external(DXY/VIX/US10Y/黄金)", "news(舆情)",
            "my_open_positions(持仓账本)", "portfolio_state(全局仓位)",
            "meta_quality(Chronos质量陪审团)", "reversal_sentinel(反转哨兵)",
            "recent_closed_trades(历史复盘)", "empirical_lessons(进化教训)",
        ],
    },
    "deepseek": {
        "role": "云主脑 · 方向 + 置信 + 机构级推理",
        "fed": "market_data → analyze()；market_data+对手反驳 → debate_rebuttal()",
        "output": "{decision, confidence, reasoning, key_factors}",
        "consumed_by": "meta_agent：ds_vote / ds_weight / ds_final（加权第一票）",
    },
    "hunyuan": {
        "role": "云副脑 · 方向 + 置信 + 推理（与 DS 辩论）",
        "fed": "market_data → analyze()；market_data+对手反驳 → debate_rebuttal()",
        "output": "{decision, confidence, reasoning, key_factors}",
        "consumed_by": "meta_agent：hy_vote / hy_weight / hy_final（加权第二票）",
    },
    "chronos": {
        "role": "本地时序风险先验 · 风险区间 + 方向（非方向终审）",
        "fed": "XAU 收盘价序列 + 可选协变量(DXY/US10Y/VIX)",
        "output": "{p10,p50,p90,direction,uncertainty,covariates}",
        "consumed_by": "meta_agent：chronos_vote / chronos_weight（加权第三票）+ 动态TP天花板",
    },
    "qwen3_copilot": {
        "role": "本地 8B 副驾(L2) · 降级补位（DS失联时顶票）",
        "fed": "market_data → copilot()",
        "output": "CopilotVote{decision, confidence, reason}",
        "consumed_by": "copilot_gate → 与 Chronos 同向才放行(手数40%)；DS失联时补 ds_vote",
    },
    "qwen3_proofread": {
        "role": "本地 8B 校对员(L0) · 下单前结构审计（不改方向/不投票）",
        "fed": "decision(dict) + market_snapshot → proofread()",
        "output": "proofread_status(skipped/clean/issues) + proofread_issues + proofread_blocked",
        "consumed_by": "下单前强制闸门：major 结构性错 → 降级 HOLD（拦自杀单，不干预正常单）",
    },
    "meta_agent": {
        "role": "元智能体裁决 · 融合四票 + 仓位意图 + 风控闸门",
        "fed": "ds/hy 分析 + chronos 票 + ts_fusion 第四票 + market_data",
        "output": "DebateDecision{decision, confidence, position_intent, target_risk_pct, "
                  "stop_loss, take_profit, chronos_*, ts_fusion_*, proofread_*, direction_guard_*}",
        "consumed_by": "trade_executor.execute_cycle → 真正开单 / 手数 / SL-TP",
    },
    "trade_executor": {
        "role": "执行器 · 把 AI 决策变成真实订单（开单 + 智慧仓位）",
        "fed": "DebateDecision(方向/置信/仓位意图/风险占比) + 行情",
        "output": "{direction, lots(实际手数), sl_price, tp_price, ticket, adopted_ai_risk}",
        "consumed_by": "MT5 真实成交；AI 的 target_risk_pct 经 _calc_position_size 实际生效",
    },
    "smart_exit": {
        "role": "智能平仓 · 分批止盈 / 追踪止损 / 反转平仓",
        "fed": "持仓 + 行情(timeframes/atr) + AI 反向置信",
        "output": "{action(hold/partial_close/full_close/reverse_signal), close_pct, new_sl, new_tp, reason}",
        "consumed_by": "MT5 实际平仓 / 上移 SL / 改 TP",
    },
}

# 输入完整性校验所需字段（用于给每次「接入」打分）
_REQUIRED_INPUT_KEYS = [
    "symbol", "bid", "ask",
    "timeframes", "external", "news",
    "my_open_positions", "portfolio_state",
    "meta_quality", "reversal_sentinel", "recent_closed_trades",
]


class BrainAudit:
    """大脑审计记录器（线程安全单例）。"""

    _lock = threading.Lock()
    _local = threading.local()

    # ── 周期管理：一次决策链路共享一个 cycle_id ──
    @classmethod
    def start_cycle(cls, cycle_id=None):
        cid = cycle_id or uuid.uuid4().hex[:12]
        cls._local.cycle = cid
        return cid

    @classmethod
    def current_cycle(cls):
        return getattr(cls._local, "cycle", None) or cls.start_cycle()

    # ── 输入完整性评分 ──
    @classmethod
    def _input_completeness(cls, market_data):
        if not isinstance(market_data, dict):
            return 0.0, {}
        present = {}
        score = 0
        total = len(_REQUIRED_INPUT_KEYS)
        for k in _REQUIRED_INPUT_KEYS:
            v = market_data.get(k)
            ok = v not in (None, "", [], {})
            present[k] = ok
            if ok:
                score += 1
        # timeframes 子结构抽查
        tf = market_data.get("timeframes") or {}
        if isinstance(tf, dict):
            for t in ("M1", "M5", "H1", "H4"):
                blk = tf.get(t) or {}
                if isinstance(blk, dict) and (blk.get("atr") is not None or blk.get("rsi") is not None):
                    present["tf_%s" % t] = True
                    score += 0.25
                    total += 0.25
        pct = round(100.0 * score / total, 1) if total else 0.0
        return pct, present

    # ── 核心记录 ──
    @classmethod
    def record(cls, brain, phase, *, input_fields=None, output=None,
               adopted=None, consumer=None, notes=None, cycle_id=None):
        """记录一次大脑调用。

        brain: deepseek/hunyuan/chronos/qwen3_copilot/qwen3_proofread/meta_agent/
               trade_executor/smart_exit
        phase: input(接入) / output(输出) / adoption(消费) / exit(平仓)
        input_fields: dict（market_data 或其摘要），自动算完整性
        output: dict（方向/置信/手数/SL-TP/动作等）
        adopted: -1 未知 / 0 未采用 / 1 采用 / 2 部分采用
        """
        if not _ENABLED:
            return
        # ★ 2026-08-19 定稿P1-3：云端 DS/HY 永久弃用，屏蔽云端探测记录噪音
        #   （关云时权重 0，deepseek/hunyuan 的"尝试探测"调用无决策价值，只会污染审计统计）。
        _b = str(brain or "").lower()
        if _b in ("deepseek", "hunyuan"):
            try:
                from app.services.cloud_switch import effective_cloud_enabled
                if not effective_cloud_enabled():
                    return
            except Exception:
                return
        cid = cycle_id or cls.current_cycle()
        try:
            pct, present = (cls._input_completeness(input_fields)
                            if isinstance(input_fields, dict) else (None, {}))
            row = (
                datetime.now(timezone.utc).isoformat(),
                cid,
                brain, phase,
                json.dumps(present, ensure_ascii=False) if present else None,
                pct,
                json.dumps(output, ensure_ascii=False, default=str) if output is not None else None,
                adopted if adopted is not None else -1,
                consumer, notes,
            )
            with cls._lock:
                con = sqlite3.connect(AUDIT_DB)
                con.execute(
                    """CREATE TABLE IF NOT EXISTS brain_audit_log(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT, cycle_id TEXT, brain TEXT, phase TEXT,
                        input_present TEXT, input_completeness REAL,
                        output TEXT, adopted INTEGER, consumer TEXT, notes TEXT
                    )"""
                )
                con.execute(
                    "INSERT INTO brain_audit_log VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", row
                )
                con.commit()
                con.close()
        except Exception as e:
            # 审计失败绝不拖垮主链路
            try:
                import loguru
                loguru.logger.warning("[BrainAudit] 记录失败(已忽略): %s" % e)
            except Exception:
                pass

    # ── 查询 ──
    @classmethod
    def get_logs(cls, limit=200, brain=None, cycle_id=None):
        con = sqlite3.connect(AUDIT_DB)
        con.row_factory = sqlite3.Row
        sql = "SELECT * FROM brain_audit_log"
        args = []
        where = []
        if brain:
            where.append("brain=?")
            args.append(brain)
        if cycle_id:
            where.append("cycle_id=?")
            args.append(cycle_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = con.execute(sql, args).fetchall()
        con.close()
        return [dict(r) for r in rows]

    @classmethod
    def cycles_summary(cls, limit=20):
        con = sqlite3.connect(AUDIT_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """SELECT cycle_id, COUNT(*) n,
                      MAX(ts) last_ts,
                      SUM(CASE WHEN phase='input' THEN 1 ELSE 0 END) inputs,
                      SUM(CASE WHEN phase='output' THEN 1 ELSE 0 END) outputs,
                      SUM(CASE WHEN phase='adoption' THEN 1 ELSE 0 END) adopts
               FROM brain_audit_log GROUP BY cycle_id ORDER BY last_ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]


# 便捷函数（避免各调用点写 ClassName）
def record(*a, **kw):
    return BrainAudit.record(*a, **kw)


def start_cycle(*a, **kw):
    return BrainAudit.start_cycle(*a, **kw)


if __name__ == "__main__":
    # 自测
    cid = start_cycle()
    record("deepseek", "input", input_fields={"symbol": "XAUUSD", "bid": 1, "timeframes": {"M5": {"atr": 2}}})
    record("deepseek", "output", output={"decision": "BUY", "confidence": 0.7}, adopted=1, consumer="meta_agent")
    print("self-test OK, cycles:", BrainAudit.cycles_summary())
