"""主号出场广播总线（SignalBus 前身）的行为契约测试。

背景：V6 架构诊断的头号病症是「主号平了、跟号没平」。
本文件把该病症的根因钉成回归测试 —— 任何人改动 publish/consume
只要破坏下列契约，测试立刻红。

★ 2026-08-07 发现的根因 bug：
  条目的 ts 是「首次创建时间」，追加动作时从不刷新；
  而 consume 用 `now - ent.ts <= TTL` 做整条目过期判定。
  → 一条 10 秒前刚发布的全平动作，会因为同 key 的条目是 3 分钟前建的而被丢弃。
  → 对固定 key `__BASKET_CLOSE_ALL__` 尤其致命：该 key 被永久复用，
    条目创建后 180~300s 这段窗口内所有篮子全平广播对跟号完全不可见。
"""
import time

import pytest

from app.services.trade_executor import (
    _BUS_TTL,
    _LEADER_EXIT_BUS,
    clear_leader_exit_bus,
    consume_leader_exit,
    publish_leader_exit,
)

LEADER = "leader-acct-1"
BASKET_KEY = "__BASKET_CLOSE_ALL__"


@pytest.fixture(autouse=True)
def _clean_bus():
    clear_leader_exit_bus()
    yield
    clear_leader_exit_bus()


def _age_entry(key: str, seconds: float):
    """把某条目的创建时间往前拨，模拟它是 N 秒前建的。"""
    ent = _LEADER_EXIT_BUS[f"{LEADER}:{key}"]
    ent["ts"] -= seconds
    for a in ent["actions"]:
        if "ts" in a:
            a["ts"] -= seconds


def _age_actions_only(key: str, seconds: float):
    """只把已有动作往前拨，条目本身的创建时间同步拨（模拟时间流逝）。"""
    _age_entry(key, seconds)


class TestFreshActionMustSurvive:
    """核心契约：新发布的动作必须能被消费，与同 key 的旧动作无关。"""

    def test_fresh_action_after_old_action_same_ticket(self):
        """先发 move_sl，170 秒后发 full_close —— 全平必须能取到。

        这是「主号平了跟号没平」的直接根因场景。
        """
        publish_leader_exit(LEADER, 123, "move_sl", new_sl=4300.0)
        _age_actions_only(123, 170)

        publish_leader_exit(LEADER, 123, "full_close")

        acts = consume_leader_exit(LEADER, 123)
        assert acts is not None, "全平动作刚发布 0 秒就被丢弃了 —— 跟号将永不平仓"
        assert any(a["action"] == "full_close" for a in acts), (
            f"取到的动作里没有 full_close: {acts}"
        )

    def test_basket_key_reused_forever_still_delivers(self):
        """固定 key __BASKET_CLOSE_ALL__ 被永久复用，200 秒后的新广播必须可见。

        200s 落在旧实现的死窗口（TTL 180 < 200 < GC 300）内。
        """
        publish_leader_exit(LEADER, BASKET_KEY, "basket_full_close")
        consume_leader_exit(LEADER, BASKET_KEY)
        _age_actions_only(BASKET_KEY, 200)

        publish_leader_exit(LEADER, BASKET_KEY, "basket_full_close")

        acts = consume_leader_exit(LEADER, BASKET_KEY)
        assert acts, "篮子全平广播落进死窗口 —— 跟号篮子不会跟随主号清仓"

    @pytest.mark.parametrize("gap", [0, 50, 179, 181, 250, 299, 400])
    def test_new_action_visible_regardless_of_entry_age(self, gap):
        """无论同 key 条目多老，新动作一律可见。"""
        publish_leader_exit(LEADER, 777, "move_sl", new_sl=1.0)
        _age_actions_only(777, gap)
        publish_leader_exit(LEADER, 777, "full_close")

        acts = consume_leader_exit(LEADER, 777) or []
        assert any(a["action"] == "full_close" for a in acts), (
            f"条目年龄 {gap}s 时新全平动作不可见"
        )


class TestStaleActionMustExpire:
    """反向契约：真正过期的动作绝不能复活（否则会误平已了结的持仓）。"""

    def test_action_older_than_ttl_is_dropped(self):
        publish_leader_exit(LEADER, 456, "full_close")
        _age_actions_only(456, _BUS_TTL + 5)

        assert consume_leader_exit(LEADER, 456) is None, (
            "超龄动作复活 —— 可能误平一个早已平掉又重开的持仓"
        )

    def test_mixed_ages_only_fresh_returned(self):
        """同一 key 上老动作过期、新动作存活，必须精确区分。"""
        publish_leader_exit(LEADER, 888, "move_sl", new_sl=1.0)
        _age_actions_only(888, _BUS_TTL + 10)
        publish_leader_exit(LEADER, 888, "full_close")

        acts = consume_leader_exit(LEADER, 888) or []
        kinds = {a["action"] for a in acts}
        assert "full_close" in kinds, "新动作丢失"
        assert "move_sl" not in kinds, "过期动作复活"


class TestBasicContract:

    def test_unknown_ticket_returns_none(self):
        assert consume_leader_exit(LEADER, 999999) is None

    def test_每个动作都带独立时间戳(self):
        """结构契约：动作级过期判定的前提是动作自带 ts。"""
        publish_leader_exit(LEADER, 111, "full_close")
        ent = _LEADER_EXIT_BUS[f"{LEADER}:111"]
        for a in ent["actions"]:
            assert "ts" in a, "动作缺少独立时间戳，无法做动作级过期判定"

    def test_action_id_unique(self):
        publish_leader_exit(LEADER, 222, "full_close")
        publish_leader_exit(LEADER, 222, "move_sl", new_sl=1.0)
        acts = consume_leader_exit(LEADER, 222)
        assert len({a["id"] for a in acts}) == 2, "动作 id 不唯一，跟号幂等去重会失效"

    def test_memory_does_not_grow_unbounded(self):
        """GC 契约：大量陈旧 key 必须被回收，否则长跑进程内存泄漏。"""
        for i in range(200):
            publish_leader_exit(LEADER, 10000 + i, "full_close")
        for i in range(200):
            _age_actions_only(10000 + i, 10_000)
        publish_leader_exit(LEADER, 20001, "full_close")

        assert len(_LEADER_EXIT_BUS) < 50, (
            f"陈旧条目未回收，当前 {len(_LEADER_EXIT_BUS)} 条 —— 长跑必然内存膨胀"
        )
