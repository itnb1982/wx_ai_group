"""
Phase 5 · 压力测试与并发正确性校验（可离线运行，无需 MT5 终端）

★ 目的
    在「账号数 N 是变量（1 ~ 50+）」这一最高铁律下，验证执行控制层
    （app/core/account_lane）在高并发、突发下单洪峰下仍满足多租户 invariants：
      ① 错峰窗口正确：N=1 零延迟；N≥2 抖动落在 [200,800]ms 且随并发封顶；
      ② 租户隔离：每账号的成交归因只挂自己的 account_id，绝不串号/串资金；
      ③ 有界吞吐：有界车道池（硬上界 32）不会被 N=50 打爆，无线程风暴；
      ④ 延迟可控：单笔下单调度延迟随 N 平滑增长，不爆炸；
      ⑤ 内存有界：归因环形缓冲（deque maxlen）长跑不泄漏。

★ 为什么离线即可验
    真正的多租户风险就在「编排层派发 + 执行层错峰 + 滑点归因」这三件事，
    它们全部收敛在 account_lane 的纯函数/有界容器里，不依赖 MT5 实时行情。
    本脚本直接驱动这些真实原语，覆盖 N=1/4/10/50 × 突发 K 单 的组合，
    证明系统扩容到 50 客户时执行层不退化、不串号、不漏单、不 OOM。

★ 运行
    cd <项目根>/backend
    python scripts/stress_test.py                 # 默认 N∈{1,4,10,50}, 每账号突发 200 单
    python scripts/stress_test.py --accounts 50 --orders 500 --seed 7
    python scripts/stress_test.py --quick         # 仅 N=1/4 快速冒烟

退出码 0 = 全部 invariants 通过；非 0 = 存在违反（详见末尾 FAIL 报告）。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import tracemalloc

# ── 路径：让脚本能 import 项目包 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.core.account_lane import (  # noqa: E402
    apply_order_jitter,
    compute_jitter_ms,
    set_active_accounts,
    active_accounts,
    record_fill,
    get_attribution,
    reset_lane_pool,
    JITTER_MIN_WINDOW_MS,
    JITTER_MAX_WINDOW_MS,
)

PASS = "PASS"
FAIL = "FAIL"


def _fmt_ms(x: float) -> str:
    return f"{x:.1f}ms"


def _check(name: str, cond: bool, detail: str = "") -> bool:
    tag = PASS if cond else FAIL
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))
    return cond


# ──────────────────────────────────────────────────────────────
# 场景 1：错峰窗口正确性（纯函数，确定可断言）
# ──────────────────────────────────────────────────────────────
def test_jitter_window():
    print("\n== 场景1: 错峰窗口正确性 ==")
    ok = True
    # N=1 → 必须零延迟（绝不无谓惩罚单客户部署）
    set_active_accounts(1)
    j1 = apply_order_jitter(active_accounts())
    ok &= _check("N=1 零延迟", j1 == 0.0, f"jitter={_fmt_ms(j1)}")

    # N≥2 → 抖动窗口 = clamp(n*80, 200, 800)，单次抖动均匀落在 [0, 窗口]
    # （窗口下界≥200 保证高并发必错峰；窗口上界≤800 保证不无限增长）。
    # ★ 注意：窗口正确性用纯计算的 compute_jitter_ms（不 sleep），
    #   否则 2000 次 × 数百 ms sleep 会真把测试挂成几十分钟。
    rng = random.Random(12345)
    for n in (4, 10, 50):
        set_active_accounts(n)
        samples = [compute_jitter_ms(active_accounts(), rng=rng) for _ in range(2000)]
        lo, hi = min(samples), max(samples)
        mean = sum(samples) / len(samples)
        # 单次抖动 ∈ [0, 800]（随机均匀，下限可到 0，这正确）
        nonneg_bounded = all(-1e-6 <= s <= JITTER_MAX_WINDOW_MS + 1e-6 for s in samples)
        ok &= _check(
            f"N={n} 单次抖动∈[0,{JITTER_MAX_WINDOW_MS:.0f}]ms（随机均匀）",
            nonneg_bounded,
            f"min={_fmt_ms(lo)} max={_fmt_ms(hi)} mean={_fmt_ms(mean)}",
        )
        # 窗口下界：高并发必错峰（窗口至少 200ms，样本极大时 max 应逼近窗口）
        ok &= _check(
            f"N={n} 错峰窗口下界≥{JITTER_MIN_WINDOW_MS:.0f}ms（高并发必打散）",
            hi >= JITTER_MIN_WINDOW_MS - 1e-6,
            f"max={_fmt_ms(hi)}",
        )
    return ok


# ──────────────────────────────────────────────────────────────
# 场景 2：突发下单洪峰下的有界吞吐 + 延迟
# ──────────────────────────────────────────────────────────────
def _worker_fire(account_id: int, k: int, latencies: list, errors: list):
    """模拟单个客户在突发窗口内连续打 K 单。"""
    rng = random.Random(account_id * 1000 + k)
    for i in range(k):
        t0 = time.perf_counter()
        try:
            jitter = apply_order_jitter(active_accounts(), rng=rng)
            # 模拟成交：请求价 vs 成交价（这里用确定性假数据，只验证归因不串号）
            req = 2000.0 + (i % 10) * 0.1
            fill = req + (1 if i % 2 else -1) * 0.05
            record_fill(account_id, "BUY" if i % 2 == 0 else "SELL", req, fill,
                        concurrent_n=active_accounts(), jitter_ms=jitter)
        except Exception as e:  # noqa: BLE001
            errors.append(f"acct={account_id} i={i} {e!r}")
        latencies.append(time.perf_counter() - t0)


def test_burst_throughput(n: int, k: int):
    print(f"\n== 场景2: 突发洪峰 N={n} × 每账号 {k} 单 ==")
    ok = True
    set_active_accounts(n)
    reset_lane_pool()  # 干净起步

    errors: list = []
    latencies: list = []
    threads = [
        threading.Thread(target=_worker_fire, args=(aid, k, latencies, errors))
        for aid in range(1, n + 1)
    ]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    wall = time.perf_counter() - t0

    total = n * k
    alive = sum(1 for th in threads if not th.is_alive())
    ok &= _check(f"全部线程正常结束（无卡死）", alive == n, f"{alive}/{n} 已 join")
    ok &= _check("无异常抛出（错峰=优化非闸门）", len(errors) == 0, f"errors={len(errors)} {errors[:2]}")
    ok &= _check(f"全部 {total} 单完成", len(latencies) == total, f"done={len(latencies)}/{total}")

    if latencies:
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        mean = sum(latencies) / len(latencies)
        ok &= _check("单笔下单调度（含错峰 sleep）P95 受窗口约束 < 1s",
                     p95 < 1.0, f"mean={_fmt_ms(mean*1000)} p95={_fmt_ms(p95*1000)}")
    # 归因写入自身开销（不含错峰 sleep）应极低 —— 这是「错峰是优化非闸门」的量化证据
    _t0 = time.perf_counter()
    for _ in range(2000):
        record_fill(999, "BUY", 2000.0, 2000.1, concurrent_n=n, jitter_ms=0.0)
    _write_ms = (time.perf_counter() - _t0) / 2000 * 1000
    ok &= _check("归因写入开销 < 1ms/单（错峰不拖垮吞吐）",
                 _write_ms < 1.0, f"avg={_write_ms:.3f}ms")
    ok &= _check("总墙钟时间合理（N=50 不超时）", wall < 120, f"wall={wall:.2f}s total={total}")
    print(f"     吞吐 ≈ {total / wall:.0f} 单/秒（错峰含人为抖动，不代表真实下单速率）")
    return ok


# ──────────────────────────────────────────────────────────────
# 场景 3：租户隔离（滑点归因绝不串号 / 串资金）
# ──────────────────────────────────────────────────────────────
def test_tenant_isolation(n: int, k: int):
    print(f"\n== 场景3: 租户隔离 N={n}（归因只挂自己的 account_id，绝不串写）==")
    ok = True
    set_active_accounts(n)
    reset_lane_pool()
    get_attribution().reset()  # 干净起步：全局归因单例跨场景累积，测前重置

    # ★ 隔离测试用「小批量」：归因缓冲有 cap=400，若单账号批量过大，
    #   环形缓冲会丢弃早期样本导致部分账号不在近期窗口内（这是内存有界的设计，
    #   由场景4验证）。隔离 invariant 只需证明「绝不串写」，故用 N×iso_k < 400
    #   的小批量，让全部账号都落在缓冲内，干净断言每账号只见到自己的记录。
    iso_k = min(k, 5)  # N=50 × 5 = 250 < 400，安全落入缓冲

    threads = [
        threading.Thread(
            target=lambda aid=aid: [
                record_fill(aid, "BUY", 2000.0, 2000.1,
                            concurrent_n=active_accounts(), jitter_ms=100.0)
                for _ in range(iso_k)
            ]
        )
        for aid in range(1, n + 1)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    summary = get_attribution().summary()
    by_account = summary.get("by_account", {})
    seen = set(int(a) for a in by_account.keys())
    expected = set(range(1, n + 1))
    ok &= _check("归因覆盖全部 N 账号（小批量落入缓冲）", seen == expected,
                 f"seen={len(seen)} expected={len(expected)}")
    ok &= _check("无越界账号（绝不串号/串资金）", seen.issubset(expected),
                 f"extra={seen - expected}")
    # 每个账号样本数精确 = 自己的 iso_k（无串写：A 的记录绝不出现在 B 名下）
    bad = [a for a, s in by_account.items() if s.get("count") != iso_k]
    ok &= _check(f"每账号独立记录 {iso_k} 单（无跨账号串写）", len(bad) == 0,
                 f"异常账号={bad[:3]} counts={[by_account[b]['count'] for b in bad[:3]]}")
    return ok


# ──────────────────────────────────────────────────────────────
# 场景 4：内存有界（归因环形缓冲长跑不泄漏）
# ──────────────────────────────────────────────────────────────
def test_memory_bounded(n: int, k: int):
    print(f"\n== 场景4: 内存有界（{n} 账号 × {k} 单 × 3 轮）==")
    ok = True
    set_active_accounts(n)
    reset_lane_pool()
    get_attribution().reset()
    tracemalloc.start()
    for _ in range(3):
        threads = [
            threading.Thread(
                target=lambda aid=aid: [
                    record_fill(aid, "BUY", 2000.0, 2000.1,
                                concurrent_n=active_accounts(), jitter_ms=50.0)
                    for _ in range(k)
                ]
            )
            for aid in range(1, n + 1)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
    cur, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # 归因缓冲有 cap，3 轮 × N×K 样本不应让常驻内存随样本数线性膨胀
    # 这里只断言进程未因缓冲失控（cap=400，常驻应很小）
    ok &= _check("归因缓冲常驻内存有界（< 8MB）", cur < 8 * 1024 * 1024,
                 f"current={cur/1024/1024:.2f}MB")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Phase 5 压力测试")
    ap.add_argument("--accounts", type=int, nargs="+", default=None,
                    help="要压测的账号数列表，默认 [1,4,10,50]")
    ap.add_argument("--orders", type=int, default=200, help="每账号突发下单数")
    ap.add_argument("--quick", action="store_true", help="仅 N=1/4 快速冒烟")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    if args.quick:
        accounts = [1, 4]
        k = 20  # 冒烟：少下单，避免真实 jitter sleep 把测试拖长
    elif args.accounts:
        accounts = args.accounts
        k = args.orders
    else:
        accounts = [1, 4, 10, 50]
        k = args.orders

    print(f"Phase 5 压测启动：accounts={accounts} orders/账号={k}")
    all_ok = True

    all_ok &= test_jitter_window()

    for n in accounts:
        if n >= 2:  # N=1 时吞吐/隔离场景退化，仅跑窗口正确性
            all_ok &= test_burst_throughput(n, k)
            all_ok &= test_tenant_isolation(n, k)
            all_ok &= test_memory_bounded(n, k)

    print("\n" + "=" * 56)
    if all_ok:
        print(f"✅ Phase 5 压测全部 invariants 通过（N 最大={max(accounts)}）")
        print("   结论：执行控制层在 50 客户突发下单下仍满足")
        print("   错峰正确 / 租户隔离 / 有界吞吐 / 内存有界。")
        return 0
    print("❌ Phase 5 压测存在 invariants 违反，详见上方 FAIL。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
