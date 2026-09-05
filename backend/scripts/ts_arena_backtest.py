# -*- coding: utf-8 -*-
"""本地时序模型「方向预测」竞技场回测台。

回答一个问题，只回答这一个问题：
    ── 在 XAUUSD 真实历史行情上，谁的方向判断更准？值不值得当方向终审？

设计红线（对应用户铁律「提准非拦截」「结果说话不拍脑袋」）：
  1. **严禁未来函数**：第 i 根 K 线做预测时，只能看 closes[:i]，
     真实答案 closes[i+H] 只用于事后打分，绝不回流进特征。
  2. **同一批样本、同一套打分**：所有模型跑完全相同的切片，
     否则"谁更准"没有可比性。
  3. **必须有对照组**：传统趋势指标(MA/RSI 多周期投票) 代表
     "云模型靠滞后指标判方向"的水平。时序模型若赢不过它，
     就没有资格当方向终审——这是硬门槛。
  4. **覆盖率与准确率一起看**：只在 5% 的样本上开口而准确率 90%，
     和在 90% 样本上开口而准确率 55%，是完全不同的两件事。
     只看准确率会掉进"砍交易换纸面胜率"的老坑。

用法（原生 PowerShell / cmd，**不要用 Git Bash**，否则 torch 段错误）：
    .venv\\Scripts\\python.exe backend\\scripts\\ts_arena_backtest.py --tf M15 --bars 6000 --horizon 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


class _Tee:
    """把 stdout 同时写进日志文件。

    为什么需要：PowerShell 5.1 的 Start-Process 一旦带 -RedirectStandardOutput
    就会因环境字典里同时存在 'Path' 与 'PATH' 抛 ArgumentException，
    所以进程自己负责落盘，外部只管启动。
    """

    def __init__(self, stream, path):
        self.stream = stream
        self.f = open(path, "w", encoding="utf-8", buffering=1)

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:  # noqa: BLE001
            pass
        self.f.write(s)
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:  # noqa: BLE001
            pass
        self.f.flush()

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(BACKEND)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
MODELS = os.path.join(ROOT, "models")

import numpy as np  # noqa: E402


# ══════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════
def load_rates(symbol: str, tf: str, bars: int):
    """复用参考服务模块的统一行情拉取（只读，不影响交易 worker）。
    返回 (closes, highs, lows) 三元组，与历史调用方签名一致。"""
    from app.services.ts_reference_models import load_live_rates
    closes, highs, lows, _live = load_live_rates(symbol, tf, bars)
    return closes, highs, lows

class Predictor:
    name = "base"

    def ready(self) -> bool:
        return True

    def predict(self, ctx: np.ndarray, horizon: int) -> float:
        """返回方向分 -1..+1（>0 看多，<0 看空，≈0 无观点）。"""
        raise NotImplementedError


class TrendBaseline(Predictor):
    """对照组：传统多周期趋势指标投票 —— 代表"云模型靠滞后指标"的水平。"""
    name = "趋势指标基线(对照组)"

    def predict(self, ctx, horizon):
        votes = []
        for w in (10, 20, 50):
            if len(ctx) < w + 1:
                continue
            ma = ctx[-w:].mean()
            votes.append(1.0 if ctx[-1] > ma else -1.0)
        if len(ctx) >= 15:
            d = np.diff(ctx[-15:])
            up, dn = d[d > 0].sum(), -d[d < 0].sum()
            rsi = 100 - 100 / (1 + up / (dn + 1e-9))
            votes.append(1.0 if rsi > 50 else -1.0)
        return float(np.mean(votes)) if votes else 0.0


class NumpyGuard(Predictor):
    """本项目现役纯 NumPy 方向终审器（无 torch 依赖）。"""
    name = "NumPy规则终审器"

    def __init__(self):
        from app.services.numpy_direction_guard import NumpyDirectionGuard
        self.g = NumpyDirectionGuard()

    def predict(self, ctx, horizon):
        r = self.g.review(ctx.tolist(), float(ctx[-1]), "BUY")
        return float(r.direction_score)


from app.services.ts_reference_models import ChronosP, TimesFMP, TimeMoEP, MoiraiP



# ══════════════════════════════════════════════════════════════
#  评分
# ══════════════════════════════════════════════════════════════
# 置信度分档边界。为什么必须分档：
#   一个模型整体准确率 48%（不如抛硬币）并不等于它一无是处——
#   有可能它 80% 的时候在瞎猜，20% 强观点时却很准。若真如此，
#   它就该被用作「高置信增强」而不是「全局终审」。
#   反过来，如果越自信越错，那它连参考价值都没有，必须踢出决策链。
# 这一档统计是「提准非拦截」能否落地的唯一依据，不能省。
BUCKETS = [(0.15, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 1.01)]


@dataclass
class Score:
    name: str
    n_total: int = 0
    n_signal: int = 0          # 非 HOLD 的样本数
    n_correct: int = 0
    pnl_points: float = 0.0    # 按信号方向持有 horizon 根的累计点数
    wins: float = 0.0          # 盈利点数总和（算 PF 用）
    losses: float = 0.0        # 亏损点数总和
    ms: float = 0.0            # 累计推理耗时
    # 每档: [样本数, 正确数, 净点数]
    buckets: list = field(default_factory=lambda: [[0, 0, 0.0] for _ in BUCKETS])

    def add(self, score: float, fut_ret: float, thr: float, dt_ms: float):
        self.n_total += 1
        self.ms += dt_ms
        if abs(score) < thr:
            return
        self.n_signal += 1
        d = 1.0 if score > 0 else -1.0
        pnl = d * fut_ret
        self.pnl_points += pnl
        ok = pnl > 0
        if ok:
            self.n_correct += 1
            self.wins += pnl
        else:
            self.losses += -pnl

        a = abs(score)
        for bi, (lo, hi) in enumerate(BUCKETS):
            if lo <= a < hi:
                self.buckets[bi][0] += 1
                self.buckets[bi][1] += 1 if ok else 0
                self.buckets[bi][2] += pnl
                break

    def bucket_report(self) -> list:
        out = []
        for (lo, hi), (n, c, p) in zip(BUCKETS, self.buckets):
            out.append({
                "区间": f"|分|{lo:.2f}~{hi if hi <= 1 else 1.0:.2f}",
                "样本": n,
                "准确率": round(c / n * 100, 2) if n else 0.0,
                "净点数": round(p, 1),
            })
        return out

    def report(self) -> dict:
        acc = self.n_correct / self.n_signal if self.n_signal else 0.0
        cov = self.n_signal / self.n_total if self.n_total else 0.0
        pf = self.wins / self.losses if self.losses > 1e-9 else float("inf")
        return {
            "模型": self.name,
            "方向准确率": round(acc * 100, 2),
            "覆盖率": round(cov * 100, 2),
            "信号数": self.n_signal,
            "净点数": round(self.pnl_points, 1),
            "PF": round(pf, 3) if pf != float("inf") else 999,
            "单次耗时ms": round(self.ms / max(self.n_total, 1), 1),
            "分档": self.bucket_report(),
        }


# ============================================================================
#  集成评估（2026-08-08 新增）
#
#  动机：用户提议「把这几个时序模型集成在一起，作为开仓和平仓的依据」。
#  单模型排名只能说明「各自多强」，说明不了「合议是否更强」——弱学习器只要
#  错误方向互补，集成后完全可能反超。所以必须在**同一批样本**上把集成方案
#  也跑一遍，用数据回答，而不是凭直觉否定。
#
#  评估四种合议方式（都不引入任何未来信息）：
#    · 等权平均   —— 三个时序模型分数取均值
#    · 多数投票   —— 至少 2 票同向（弃权票不计）
#    · 全体一致   —— 三个模型方向必须完全一致（高精度低覆盖的极端）
#    · 时序+规则  —— 时序集成方向须与 NumPy 终审器一致
# ============================================================================
TS_MODELS = ["Chronos-2(120M)", "TimesFM-2.5(200M)", "Time-MoE(200M)", "Moirai(447M)"]
GUARD_NAME = "NumPy规则终审器"


def _votes(sample_scores: dict, names: list, thr: float):
    """把各模型分数折成有效票。|分| < thr 视为弃权（该模型这轮没意见）。"""
    return [sample_scores.get(n, 0.0) for n in names
            if abs(sample_scores.get(n, 0.0)) >= thr]


def _ens_mean(ss: dict, thr: float):
    vals = [ss.get(n, 0.0) for n in TS_MODELS if n in ss]
    return sum(vals) / len(vals) if vals else 0.0


def _ens_majority(ss: dict, thr: float):
    v = _votes(ss, TS_MODELS, thr)
    if not v:
        return 0.0
    up = [x for x in v if x > 0]
    dn = [x for x in v if x < 0]
    if len(up) >= 2 and len(up) > len(dn):
        return sum(up) / len(up)
    if len(dn) >= 2 and len(dn) > len(up):
        return sum(dn) / len(dn)
    return 0.0


def _ens_unanimous(ss: dict, thr: float):
    v = _votes(ss, TS_MODELS, thr)
    if len(v) < len(TS_MODELS):
        return 0.0  # 有模型弃权就不算一致
    if all(x > 0 for x in v) or all(x < 0 for x in v):
        return sum(v) / len(v)
    return 0.0


def _ens_ts_plus_guard(ss: dict, thr: float):
    ts = _ens_majority(ss, thr)
    g = ss.get(GUARD_NAME, 0.0)
    if ts == 0.0 or abs(g) < thr:
        return 0.0
    if (ts > 0) == (g > 0):
        return (ts + g) / 2.0
    return 0.0


ENSEMBLES = [
    ("集成·等权平均(3时序)", _ens_mean),
    ("集成·多数投票(≥2同向)", _ens_majority),
    ("集成·全体一致(3/3)", _ens_unanimous),
    ("集成·时序多数+规则一致", _ens_ts_plus_guard),
]


def evaluate_ensembles(per_sample: list, thr: float) -> list:
    """在已记录的逐样本分数上评估各集成策略，返回与单模型同构的报告行。"""
    scs = {name: Score(name) for name, _ in ENSEMBLES}
    for rec in per_sample:
        ss, fut = rec["s"], rec["fut"]
        for name, fn in ENSEMBLES:
            try:
                v = fn(ss, thr)
            except Exception:  # noqa: BLE001
                v = 0.0
            scs[name].add(v, fut, thr, 0.0)
    return [scs[name].report() for name, _ in ENSEMBLES]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M15")
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--horizon", type=int, default=4, help="预测未来几根")
    ap.add_argument("--ctx", type=int, default=256, help="上下文窗口")
    ap.add_argument("--step", type=int, default=5, help="每隔几根取一个样本")
    ap.add_argument("--thr", type=float, default=0.15, help="方向分阈值，低于视为 HOLD")
    ap.add_argument("--out", default=os.path.join(BACKEND, "data", "ts_arena_report.json"))
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    sys.stdout = _Tee(sys.stdout, os.path.splitext(a.out)[0] + ".log")

    closes, _, _ = load_rates(a.symbol, a.tf, a.bars)
    if len(closes) < a.ctx + a.horizon + 100:
        print("[错误] K 线不足")
        return 1

    cands = [TrendBaseline(), NumpyGuard(), ChronosP(), TimesFMP(), TimeMoEP(), MoiraiP()]
    live = []
    for p in cands:
        t0 = time.time()
        ok = p.ready()
        print(f"[加载] {p.name}: {'可用' if ok else '不可用'} ({time.time()-t0:.1f}s)")
        if ok:
            live.append(p)
    if not live:
        print("[错误] 无可用模型")
        return 2

    scores = {p.name: Score(p.name) for p in live}
    idxs = list(range(a.ctx, len(closes) - a.horizon, a.step))
    print(f"[回测] 样本数={len(idxs)} tf={a.tf} horizon={a.horizon} "
          f"ctx={a.ctx} 阈值={a.thr}")

    t_start = time.time()
    per_sample = []   # 逐样本留档，供集成评估复用（不必二次推理）
    for n, i in enumerate(idxs):
        ctx = closes[i - a.ctx:i]              # 只看过去
        fut_ret = float(closes[i + a.horizon] - closes[i])   # 事后打分用
        row = {}
        for p in live:
            t0 = time.time()
            try:
                s = p.predict(ctx, a.horizon)
            except Exception as e:  # noqa: BLE001
                if n == 0:
                    print(f"[{p.name}] predict 异常: {type(e).__name__}: {str(e)[:120]}")
                s = 0.0
            row[p.name] = s
            scores[p.name].add(s, fut_ret, a.thr, (time.time() - t0) * 1000)
        per_sample.append({"fut": fut_ret, "s": row})
        if n and n % 100 == 0:
            el = time.time() - t_start
            print(f"  进度 {n}/{len(idxs)}  已用{el:.0f}s  "
                  f"预计剩余{el/n*(len(idxs)-n):.0f}s")

    rows = [scores[p.name].report() for p in live]
    rows.sort(key=lambda r: (-r["净点数"], -r["方向准确率"]))

    print("\n" + "=" * 92)
    print(f"XAUUSD {a.tf} 方向预测竞技场  |  样本 {len(idxs)}  |  "
          f"持有 {a.horizon} 根  |  阈值 {a.thr}")
    print("=" * 92)
    hdr = f"{'模型':<26}{'准确率%':>9}{'覆盖率%':>9}{'信号数':>8}{'净点数':>11}{'PF':>8}{'耗时ms':>9}"
    print(hdr)
    print("-" * 92)
    for r in rows:
        print(f"{r['模型']:<26}{r['方向准确率']:>9}{r['覆盖率']:>9}"
              f"{r['信号数']:>8}{r['净点数']:>11}{r['PF']:>8}{r['单次耗时ms']:>9}")
    print("=" * 92)

    # 置信度分档 —— 决定「能否当高置信终审」的关键证据
    print("\n【置信度分档】越自信是否越准？(若高档准确率不升反降 = 该模型的置信度是噪声)")
    for r in rows:
        segs = []
        for b in r["分档"]:
            if b["样本"] == 0:
                continue
            segs.append(f"{b['区间']}: n={b['样本']:<4} 准确率={b['准确率']:<6} 净点={b['净点数']}")
        print(f"  {r['模型']}")
        for s in segs:
            print(f"      {s}")
        if not segs:
            print("      (无信号)")

    # ── 集成评估：回答「几个模型合起来做开平仓依据是否更强」 ──────────
    ens_rows = evaluate_ensembles(per_sample, a.thr)
    ens_rows.sort(key=lambda r: (-r["净点数"], -r["方向准确率"]))
    best_single = max(rows, key=lambda r: r["净点数"])

    print("\n" + "=" * 92)
    print("多模型集成评估（同一批样本，无未来函数）")
    print("=" * 92)
    print(hdr)
    print("-" * 92)
    for r in ens_rows:
        print(f"{r['模型']:<26}{r['方向准确率']:>9}{r['覆盖率']:>9}"
              f"{r['信号数']:>8}{r['净点数']:>11}{r['PF']:>8}{r['单次耗时ms']:>9}")
    print("-" * 92)
    best_ens = ens_rows[0] if ens_rows else None
    if best_ens:
        delta = best_ens["净点数"] - best_single["净点数"]
        print(f"最佳集成 [{best_ens['模型']}] 净点 {best_ens['净点数']} "
              f"vs 最佳单模型 [{best_single['模型']}] 净点 {best_single['净点数']}  "
              f"→ 差值 {delta:+.1f}")
        print("判定：" + (
            "集成【跑赢】最佳单模型，值得进一步验证是否纳入决策链。"
            if delta > 0 else
            "集成【未跑赢】最佳单模型 —— 弱信号叠加并没有产生互补效应，"
            "把它们并成开平仓依据只会增加成本与复杂度。"
        ))
    print("=" * 92)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({
            "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "参数": vars(a), "样本数": len(idxs),
            "结果": rows, "集成": ens_rows,
        }, f, ensure_ascii=False, indent=2)
    print(f"[报告] 已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
