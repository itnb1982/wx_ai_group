"""
人工紧急处置窗口 — 单元测试（Phase 0 / V6）

这个模块是"AI 失灵时人能伸手进去"的最后一道防线，
所以它自己的正确性必须比它保护的东西更可靠。

重点验证的不是"功能能用"，而是几个致命失效模式：
  · 进程重启后停止状态会不会被悄悄抹掉（崩溃自愈场景最危险）
  · 状态文件损坏时会不会静默漏停
  · 停一个账号会不会误伤其他账号（多账号铁律）
  · 解除全局会不会顺手把出问题的账号也放出去
"""
import os
import json
import importlib

import pytest

import app.services.emergency as em


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch, request):
    """每个用例独立状态文件 + 清空进程内缓存，避免相互污染与写到生产文件。

    用 tmp_path_factory 的 session 根目录 + 唯一文件名，而不是每用例一个 tmp_path：
    本文件用例较多，逐个建目录会在系统 Temp 下堆出上百个待清理目录。
    """
    base = tmp_path_factory.getbasetemp()
    f = base / f"em_{abs(hash(request.node.nodeid)) % 10**10}.json"
    monkeypatch.setattr(em, "STATE_FILE", str(f))
    monkeypatch.setattr(em, "_cache", None, raising=False)
    monkeypatch.setattr(em, "_cache_at", 0.0, raising=False)
    monkeypatch.setattr(em, "_cache_mtime", -1.0, raising=False)
    yield f


def _reset_cache(monkeypatch):
    """模拟"新进程启动"：内存缓存全丢，只剩磁盘文件。"""
    monkeypatch.setattr(em, "_cache", None, raising=False)
    monkeypatch.setattr(em, "_cache_at", 0.0, raising=False)
    monkeypatch.setattr(em, "_cache_mtime", -1.0, raising=False)


# ─────────────────────────── 基础档位语义 ───────────────────────────

@pytest.mark.unit
def test_default_is_normal_and_everything_allowed():
    assert em.effective_level() == em.LEVEL_NORMAL
    ok, _ = em.allow_open("acct-1")
    assert ok is True
    ok2, _ = em.allow_auto_exit("acct-1")
    assert ok2 is True


@pytest.mark.unit
def test_halt_new_blocks_open_but_keeps_protecting_positions():
    """HALT_NEW 的语义核心：别再进场，但已有的单该保护还得保护。

    如果这条错了（连止损也停），人工停止反而会让持仓裸奔——
    比不停更危险。
    """
    em.halt(em.LEVEL_HALT_NEW, reason="盘面异常", by="tester")

    ok, why = em.allow_open("acct-1")
    assert ok is False
    assert "HALT_NEW" in why

    ok2, _ = em.allow_auto_exit("acct-1")
    assert ok2 is True, "HALT_NEW 绝不能停掉止损/止盈，否则持仓裸奔"


@pytest.mark.unit
def test_halt_all_blocks_open_but_keeps_protective_exits():
    """HALT_ALL 拒开仓，AI 自动平仓冻结；MT5 原生 SL/TP 仍兜底（铁律6 的正确落地）。

    ★ 2026-08-17 契约精化：铁律6「MANUAL_HALT 期间 SL/TP/SmartExit 仍有效」
      的正确落地方式是——MT5 **原生 SL/TP**（券商端自动触发，不依赖系统发指令）
      天然守护持仓防裸奔；而 HALT_ALL = 人工判定系统失灵，**AI 驱动的平仓指令**
      （close_position / modify_sl_tp）必须冻结，否则等于没停。
      HALT_NEW（仅停开仓）时 AI 自动平仓照常放行（见 test_halt_new_keeps_exits）。
    """
    em.halt(em.LEVEL_HALT_ALL, reason="AI疯了", by="tester")
    assert em.allow_open("acct-1")[0] is False
    assert em.allow_auto_exit("acct-1")[0] is False, (
        "HALT_ALL 期间 AI 平仓指令必须冻结（全停=人工接管，原生 SL/TP 兜底）"
    )


@pytest.mark.unit
def test_reject_reason_carries_who_when_why():
    """拒绝原因必须能回答"谁、什么时候、为什么停的"，否则事后无法复盘。"""
    em.halt(em.LEVEL_HALT_NEW, reason="非农数据前避险", by="liumanchun")
    _, why = em.allow_open("acct-1")
    assert "liumanchun" in why
    assert "非农数据前避险" in why


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "NORMAL", "STOP", "halt_everything", None])
def test_invalid_level_rejected(bad):
    with pytest.raises(ValueError):
        em.halt(bad)


# ─────────────────────── 多账号隔离（铁律） ───────────────────────

@pytest.mark.unit
def test_account_halt_does_not_affect_others():
    """停一个账号绝不能误伤别人——一账号=一客户。"""
    em.halt(em.LEVEL_HALT_ALL, scope="acct-A", reason="该客户要求", by="tester")

    assert em.allow_open("acct-A")[0] is False
    assert em.allow_open("acct-B")[0] is True
    assert em.allow_open("acct-C")[0] is True
    assert em.allow_open()[0] is True, "账号级停止不应污染全局"


@pytest.mark.unit
def test_effective_level_takes_the_stricter_one():
    """全局与账号级并存时取严格者，不能互相稀释。"""
    em.halt(em.LEVEL_HALT_NEW, scope=em.SCOPE_GLOBAL, reason="全局降级", by="t")
    em.halt(em.LEVEL_HALT_ALL, scope="acct-A", reason="单号加严", by="t")

    assert em.effective_level("acct-A") == em.LEVEL_HALT_ALL
    assert em.effective_level("acct-B") == em.LEVEL_HALT_NEW
    # ★ 2026-08-17 契约精化（同上）：HALT_ALL 冻结 AI 平仓；HALT_NEW 放行保护性平仓
    assert em.allow_auto_exit("acct-A")[0] is False, "HALT_ALL 冻结 AI 平仓指令（原生 SL/TP 兜底）"
    assert em.allow_auto_exit("acct-B")[0] is True, "HALT_NEW 仅停开仓，保护性平仓照常"


@pytest.mark.unit
def test_global_halt_all_overrides_weaker_account_entry():
    """账号级较松时不得把全局的严格档位拉低。"""
    em.halt(em.LEVEL_HALT_NEW, scope="acct-A", reason="", by="t")
    em.halt(em.LEVEL_HALT_ALL, scope=em.SCOPE_GLOBAL, reason="", by="t")
    assert em.effective_level("acct-A") == em.LEVEL_HALT_ALL


@pytest.mark.unit
def test_scales_to_many_accounts():
    """账号数是变量：10 个账号里停 3 个，其余必须照常。"""
    ids = [f"acct-{i}" for i in range(10)]
    for aid in ids[:3]:
        em.halt(em.LEVEL_HALT_NEW, scope=aid, reason="批量停", by="t")

    for aid in ids[:3]:
        assert em.allow_open(aid)[0] is False, f"{aid} 应被停"
    for aid in ids[3:]:
        assert em.allow_open(aid)[0] is True, f"{aid} 不该被误伤"


# ─────────────────────────── 解除语义 ───────────────────────────

@pytest.mark.unit
def test_resume_global_does_not_release_account_level():
    """关键安全语义：恢复全局不能顺手把出问题的那个账号也放出去。"""
    em.halt(em.LEVEL_HALT_NEW, scope=em.SCOPE_GLOBAL, reason="", by="t")
    em.halt(em.LEVEL_HALT_ALL, scope="acct-BAD", reason="该号连亏", by="t")

    em.resume(scope=em.SCOPE_GLOBAL, by="t")

    assert em.allow_open("acct-OK")[0] is True, "其他账号应恢复"
    assert em.allow_open("acct-BAD")[0] is False, "问题账号必须仍被停住"


@pytest.mark.unit
def test_resume_account_only():
    em.halt(em.LEVEL_HALT_ALL, scope="acct-A", reason="", by="t")
    em.resume(scope="acct-A", by="t")
    assert em.allow_open("acct-A")[0] is True
    assert em.effective_level("acct-A") == em.LEVEL_NORMAL


# ─────────────── 持久化：崩溃重启后停止不得失效 ───────────────

@pytest.mark.unit
def test_halt_survives_process_restart(monkeypatch, isolated_state):
    """★ 最危险的场景：系统刚崩过，supervisor 自动重启，
    如果停止状态只在内存里，重启后 AI 会立刻又开始下单。
    """
    em.halt(em.LEVEL_HALT_ALL, reason="崩溃后人工封盘", by="ops")
    assert os.path.exists(isolated_state)

    _reset_cache(monkeypatch)   # 模拟新进程

    assert em.effective_level() == em.LEVEL_HALT_ALL
    assert em.allow_open("any")[0] is False


@pytest.mark.unit
def test_account_halt_survives_restart(monkeypatch):
    em.halt(em.LEVEL_HALT_NEW, scope="acct-X", reason="", by="ops")
    _reset_cache(monkeypatch)
    assert em.allow_open("acct-X")[0] is False
    assert em.allow_open("acct-Y")[0] is True


@pytest.mark.unit
def test_state_file_is_always_valid_json(isolated_state):
    em.halt(em.LEVEL_HALT_NEW, reason="含中文原因与特殊字符 \" \\ /", by="测试员")
    with open(isolated_state, "r", encoding="utf-8") as f:
        data = json.load(f)          # 能解析即证明原子写没留半截
    assert data["global"]["level"] == em.LEVEL_HALT_NEW
    assert data["global"]["by"] == "测试员"


# ─────────────── fail-safe：读失败既不漏停也不误停 ───────────────

@pytest.mark.unit
def test_corrupted_file_falls_back_to_last_known_halt(isolated_state):
    """★ 文件损坏时绝不能静默恢复交易。

    这是最阴险的失效模式：人停了机器，磁盘写坏了，
    系统读不到停止状态就当作没停——于是继续下单。
    """
    em.halt(em.LEVEL_HALT_ALL, reason="人工封盘", by="ops")

    with open(isolated_state, "w", encoding="utf-8") as f:
        f.write("{ 这不是合法JSON ]]")

    em._cache_at = 0.0        # 强制穿透 TTL 去读盘
    em._cache_mtime = -999.0

    assert em.effective_level() == em.LEVEL_HALT_ALL, "损坏文件不得让停止失效"
    assert em.allow_open("acct-1")[0] is False


@pytest.mark.unit
def test_corrupted_file_without_prior_state_defaults_to_normal(monkeypatch, isolated_state):
    """从没读到过状态 + 文件损坏 → 只能当 NORMAL。

    这是有意识的取舍：此时无从判断人是否停过机器，
    若一律当 HALT，任何首次 IO 抖动都会误停整个系统。
    """
    with open(isolated_state, "w", encoding="utf-8") as f:
        f.write("!!! broken")
    _reset_cache(monkeypatch)
    assert em.effective_level() == em.LEVEL_NORMAL


@pytest.mark.unit
def test_missing_file_is_normal(monkeypatch, isolated_state):
    if os.path.exists(isolated_state):
        os.remove(isolated_state)
    _reset_cache(monkeypatch)
    assert em.effective_level() == em.LEVEL_NORMAL
    assert em.allow_open("x")[0] is True


@pytest.mark.unit
def test_hand_edited_file_is_tolerated(monkeypatch, isolated_state):
    """人在紧急时可能直接手改文件，字段缺斤少两也得能用。"""
    with open(isolated_state, "w", encoding="utf-8") as f:
        json.dump({"global": {"level": "halt_new"}}, f)   # 小写 + 缺字段
    _reset_cache(monkeypatch)
    assert em.effective_level() == em.LEVEL_HALT_NEW


@pytest.mark.unit
def test_unknown_level_in_file_degrades_to_normal(monkeypatch, isolated_state):
    with open(isolated_state, "w", encoding="utf-8") as f:
        json.dump({"global": {"level": "HALT_EVERYTHING_NOW"}}, f)
    _reset_cache(monkeypatch)
    assert em.effective_level() == em.LEVEL_NORMAL


# ─────────────────────── 缓存不得掩盖新指令 ───────────────────────

@pytest.mark.unit
def test_external_process_halt_is_picked_up(monkeypatch, isolated_state):
    """CLI（另一个进程）写入停止后，后端进程必须能感知到。

    走的是 mtime 失效路径——只要文件变了就重读，不能被 TTL 缓存挡住太久。
    """
    assert em.effective_level() == em.LEVEL_NORMAL   # 先把缓存热起来

    with open(isolated_state, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "global": {"level": "HALT_ALL", "reason": "CLI紧急停止",
                       "at": "2026-08-07T20:00:00", "by": "cli"},
            "accounts": {}, "flatten_requests": [],
        }, f)

    em._cache_at = 0.0    # 越过 0.5s TTL（真实场景里等半秒即可）
    assert em.effective_level() == em.LEVEL_HALT_ALL


@pytest.mark.unit
def test_get_state_returns_copy_not_internal_cache():
    """调用方改了返回值不能反过来污染内部状态。"""
    em.halt(em.LEVEL_HALT_NEW, reason="x", by="t")
    s = em.get_state()
    s["global"]["level"] = em.LEVEL_NORMAL
    assert em.effective_level() == em.LEVEL_HALT_NEW


# ─────────────────────────── 留痕与摘要 ───────────────────────────

@pytest.mark.unit
def test_flatten_is_recorded_with_context():
    rid = em.record_flatten(scope="acct-A", reason="爆仓风险", by="ops",
                            result={"closed": 3, "failed": 0})
    assert rid
    hist = em.get_flatten_history()
    assert hist[0]["id"] == rid
    assert hist[0]["result"]["closed"] == 3
    assert hist[0]["reason"] == "爆仓风险"


@pytest.mark.unit
def test_flatten_history_is_capped():
    for i in range(60):
        em.record_flatten(scope="g", reason=str(i), by="t")
    st = em.get_state(force=True)
    assert len(st["flatten_requests"]) <= 50


@pytest.mark.unit
def test_summary_shape():
    em.halt(em.LEVEL_HALT_NEW, scope="acct-A", reason="r", by="t")
    s = em.summary()
    assert s["any_halt"] is True
    assert s["halted_accounts"]["acct-A"] == em.LEVEL_HALT_NEW
    assert s["global_level"] == em.LEVEL_NORMAL


@pytest.mark.unit
def test_summary_clean_when_nothing_halted():
    s = em.summary()
    assert s["any_halt"] is False
    assert s["halted_accounts"] == {}


# ─────────────────── 零依赖约束（架构护栏） ───────────────────

@pytest.mark.unit
def test_module_has_no_heavy_dependencies():
    """★ 架构护栏：紧急停止不能建立在"其他组件还活着"的假设上。

    一旦有人给这个模块加了 DB / MT5 / AI 依赖，
    它就会在最需要它的时刻（数据库锁死、MT5 断连）跟着一起死。
    这条断言就是拦住那次提交的。
    """
    src = open(em.__file__, "r", encoding="utf-8").read()
    forbidden = [
        "from app.database",
        "import app.database",
        "from app.models",
        "MetaTrader5",
        "mt5_service",
        "debate_engine",
        "sqlalchemy",
    ]
    hits = [k for k in forbidden if k in src]
    assert not hits, f"紧急处置模块混入了重依赖，会在故障时一起失效: {hits}"


# ─────────────────── 离线控制台可用性（紧急时刻的人因） ───────────────────

@pytest.mark.unit
def test_console_level_aliases_are_forgiving():
    """★ 紧急时刻人是手抖的：简写/小写/数字都必须能正确停下来。

    真出事时要求准确敲出 HALT_NEW 全称大写，是给自己埋雷——
    打错只回一行错误，人却容易以为已经停了。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_em_console", os.path.join(os.path.dirname(em.__file__),
                                    "..", "..", "emergency_console.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for raw in ["new", "NEW", "Halt_New", "halt-new", "1", " new ", "n"]:
        assert mod._normalize_level(raw) == "HALT_NEW", f"{raw!r} 未能识别为 HALT_NEW"
    for raw in ["all", "ALL", "halt_all", "HALT-ALL", "2", "a"]:
        assert mod._normalize_level(raw) == "HALT_ALL", f"{raw!r} 未能识别为 HALT_ALL"

    # 真正无法识别的必须明确失败，绝不能猜成某一档（猜错方向比不停更糟）
    for raw in ["", "stop", "xyz", "3"]:
        if raw == "":
            continue  # 空值走默认 HALT_NEW，是有意为之的保守默认
        assert mod._normalize_level(raw) == "", f"{raw!r} 不应被识别成任何档位"
