"""
跟号镜像失败预算 — 计数必须随 TTL 重置，且不能无限堆积（Phase 1 / V6）

背景：跟号镜像主号平仓失败时会累计次数，连续 3 次就"强制标记已平"放弃这笔。
这个兜底本身是对的（防止连接故障导致无限重试死循环），
但计数表 `_MIRROR_FAIL` 原本只增不删，带来两个问题：

1) 内存泄漏。key 含 leader_ticket，每笔新单都是新 key，
   7x24 跑 demo 实盘会持续堆积。

2) 更要命的是正确性：`_MIRRORED` 幂等表有 600s TTL，过期后同一笔单
   会重新进入镜像流程——可这时 `_MIRROR_FAIL` 里的旧计数还在。
   失败预算已被上一轮耗尽，新一轮第 1 次失败就直接判超限、
   强制标记已平 → 跟号放弃平仓，主号已平而跟号持仓裸奔，两边失同步。
   本该给足 3 次重试的单，实际只有 1 次。

所以失败预算必须和幂等表同周期：TTL 内累计，过期即归零。
"""
import time

import pytest

import app.services.trade_executor as te


@pytest.fixture(autouse=True)
def clean_fail_table():
    """每个用例独立的计数表，避免相互污染。"""
    with te._MIRROR_FAIL_LOCK:
        te._MIRROR_FAIL.clear()
    yield
    with te._MIRROR_FAIL_LOCK:
        te._MIRROR_FAIL.clear()


@pytest.mark.unit
def test_counts_accumulate_within_window():
    """窗口内连续失败要正常累加，兜底才能在第 3 次触发。"""
    k = "acc-1:12345:close_all"
    assert te._bump_mirror_fail(k) == 1
    assert te._bump_mirror_fail(k) == 2
    assert te._bump_mirror_fail(k) == 3


@pytest.mark.unit
def test_budget_resets_after_ttl():
    """★ 核心：TTL 过期后失败预算必须归零。

    否则同一笔单在幂等表过期、重新进入镜像流程时，
    会带着上一轮耗尽的预算，第一次失败就被判超限放弃平仓。
    """
    k = "acc-1:12345:close_all"
    te._bump_mirror_fail(k)
    te._bump_mirror_fail(k)
    assert te._bump_mirror_fail(k) == 3          # 预算已耗尽

    # 把这条记录的时间戳推到 TTL 之外，模拟 10 分钟后重新尝试
    with te._MIRROR_FAIL_LOCK:
        cnt, _ = te._MIRROR_FAIL[k]
        te._MIRROR_FAIL[k] = (cnt, time.time() - te._MIRROR_FAIL_TTL - 1)

    assert te._bump_mirror_fail(k) == 1, (
        "TTL 过期后仍带着旧计数——新一轮第一次失败就会被误判超限，"
        "跟号会提前放弃平仓导致与主号失同步"
    )


@pytest.mark.unit
def test_expired_entries_are_garbage_collected():
    """过期条目必须被回收，否则 7x24 运行会无限堆积。"""
    now = time.time()
    with te._MIRROR_FAIL_LOCK:
        for i in range(50):
            # 全部标记为早已过期
            te._MIRROR_FAIL[f"acc:{i}:close_all"] = (1, now - te._MIRROR_FAIL_TTL - 10)

    te._bump_mirror_fail("acc:new:close_all")   # 任一次写入应顺带 GC

    with te._MIRROR_FAIL_LOCK:
        remaining = dict(te._MIRROR_FAIL)
    assert len(remaining) == 1, f"过期条目未被回收，残留 {len(remaining)} 条（内存泄漏）"
    assert "acc:new:close_all" in remaining, "GC 误删了刚写入的活跃条目"


@pytest.mark.unit
def test_gc_does_not_touch_live_entries():
    """GC 只能清过期的——误删活跃计数会让失败重试预算凭空变多，
    连接持续故障时退化成无限重试。"""
    live = "acc:live:close_all"
    te._bump_mirror_fail(live)
    te._bump_mirror_fail(live)

    with te._MIRROR_FAIL_LOCK:
        te._MIRROR_FAIL["acc:old:close_all"] = (2, time.time() - te._MIRROR_FAIL_TTL - 5)

    te._bump_mirror_fail("acc:other:close_all")

    with te._MIRROR_FAIL_LOCK:
        assert live in te._MIRROR_FAIL, "活跃条目被 GC 误删"
        assert te._MIRROR_FAIL[live][0] == 2, "活跃条目的计数被改动"
        assert "acc:old:close_all" not in te._MIRROR_FAIL


@pytest.mark.unit
def test_distinct_keys_do_not_share_budget():
    """不同账号/票号/动作各自独立计数，绝不能串味。

    串味会让 A 账号的失败拖累 B 账号提前放弃平仓（多账号铁律）。
    """
    a = te._bump_mirror_fail("accA:111:close_all")
    b = te._bump_mirror_fail("accB:111:close_all")
    c = te._bump_mirror_fail("accA:222:close_all")
    d = te._bump_mirror_fail("accA:111:partial_close")
    assert a == b == c == d == 1


@pytest.mark.unit
def test_ttl_covers_idempotency_window():
    """失败预算的 TTL 必须 >= 幂等表 600s 窗口。

    若失败预算过期得比幂等表早，会出现"幂等仍拦着不重试、
    但预算已重置"的错配窗口，兜底语义变得不可预期。
    """
    assert te._MIRROR_FAIL_TTL >= 600
