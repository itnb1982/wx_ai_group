# -*- coding: utf-8 -*-
"""架构解耦守卫：参考服务绝不接入交易决策链。

用户铁律「提准非拦截」「参考面板·未接入系统」的硬证明：
   ts_reference_models / ts_reference_service / moirai_infer / ts_reference 路由
   不得 import 任何开仓/平仓/风控/决策模块。
若有人把这些分数接回决策链，本测试立刻红。
"""
import os

_BASE = os.path.join(os.path.dirname(__file__), "..", "app")
_FILES = [
    os.path.join(_BASE, "services", "ts_reference_models.py"),
    os.path.join(_BASE, "services", "ts_reference_service.py"),
    os.path.join(_BASE, "services", "moirai_infer.py"),
    os.path.join(_BASE, "routers", "ts_reference.py"),
]
# 决策链模块名单（黑白名单式硬禁区）
_FORBIDDEN = [
    "debate_engine",
    "meta_agent",
    "trade_executor",
    "risk_engine",
    "smart_exit",
    "numpy_direction_guard",
    "chronos_service",
    "mt5_worker",
]


import re

# 仅匹配真正的 import 语句（剥离注释后），避免误伤文件头的红线警告注释
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
_COMMENT_RE = re.compile(r"#.*$")
_STRIP_RE = re.compile(r"(\"\"\"|''')(?:.|\n)*?\1")  # 去掉 docstring，防止其中列举禁区模块名被误判


def _extract_imported_modules(src: str):
    """从源码中抽取所有被 import 的顶级/完整模块名（已剥离注释与 docstring）。"""
    src = _STRIP_RE.sub("", src)
    lines = []
    for line in src.splitlines():
        # 去掉行内注释
        line = _COMMENT_RE.sub("", line)
        lines.append(line)
    modules = set()
    for line in lines:
        m = _IMPORT_RE.match(line)
        if m:
            modules.add(m.group(1))
    return modules


def test_reference_modules_do_not_import_decision_chain():
    missing = [f for f in _FILES if not os.path.exists(f)]
    assert not missing, f"待测文件缺失: {missing}"
    for f in _FILES:
        src = open(f, encoding="utf-8").read()
        imported = _extract_imported_modules(src)
        for bad in _FORBIDDEN:
            # 命中条件：存在以 bad 结尾的模块路径（含 app.core.debate_engine 这类）
            hit = [m for m in imported if m == bad or m.endswith("." + bad)]
            assert not hit, (
                f"{os.path.basename(f)} 违反了架构红线："
                f"不得 import 决策链模块「{bad}」（命中: {hit}）。"
                f"参考服务只观测，不接入交易。"
            )


def test_reference_snapshot_cannot_trigger_trading():
    """快照结构是纯观测数据，不含任何下单/风控动作字段。"""
    snap_fields = {"status", "live", "symbol", "tf", "horizon",
                   "last_price", "updated_at", "models", "hit_window",
                   "note", "decoupled"}
    # 仅做结构约定自检：若有人在快照里塞下单动作字段，这里应被发现
    forbidden_snap_keys = {"order", "execute", "close_position", "open_position",
                           "signal_action", "risk_action"}
    # 这里只声明约定，真正的字段合法性由运行时快照保证；
    # 静态层面确保这些动作词不出现在模块源码里。
    for f in _FILES:
        src = open(f, encoding="utf-8").read()
        for bad in forbidden_snap_keys:
            assert bad not in src, (
                f"{os.path.basename(f)} 不应包含交易动作字段「{bad}」"
            )
