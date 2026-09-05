# -*- coding: utf-8 -*-
"""
万象Ai · 置信校准离线拟合脚本（提准非拦截）

读取生产库 wx_prod.dat 中历史 AI 决策的 (meta_agent_confidence, 平仓盈亏)，
拟合「自报置信度 → 历史观测命中率」映射，写出 data/confidence_calibration.json。
meta_agent 运行期仅加载该 JSON 做轻量查表，无任何 DB 访问。

用法（在 backend 目录下执行）：
    python scripts/calibrate_confidence.py
可选参数：
    --min-samples 60        最少样本量（不足则跳过，保持透传）
    --method auto           auto | platt | isotonic

建议节奏：每积累一批新平仓单后跑一次（可接入每日定时任务/手动），
闭环让校准映射随市场体制演化持续更新（paperswithbacktest：校准是领域/提示依赖的，需周期重拟合）。
"""
import os
import sys
import sqlite3
import argparse

# 让脚本可直接从 backend/ 或 backend/scripts/ 运行都能 import 到 app
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# 重要：confidence_calibrator 是纯标准库模块，但它在 app.core 包内。
# 若走 `from app.core.confidence_calibrator import ...`，会先执行 app/__init__.py
# （触发 SQLAlchemy 连接与 WAL 相关初始化），实测会令本进程内对 live WAL 库的
# 读取无法回放 WAL（过滤查询读到全 NULL 列 → 0 行）。故用 importlib 直接加载该
# 模块文件本身，避开 app 包初始化；校准器本身不依赖 app 包。
import importlib.util as _ilu

_cc_path = os.path.join(_BACKEND, "app", "core", "confidence_calibrator.py")
_cc_spec = _ilu.spec_from_file_location("confidence_calibrator_standalone", _cc_path)
_cc = _ilu.module_from_spec(_cc_spec)
_cc_spec.loader.exec_module(_cc)
ConfidenceCalibrator = _cc.ConfidenceCalibrator


def _resolve_data_dir():
    # confidence_calibrator.py 位于 backend/app/core/，上溯三级到 backend，再进 data。
    # 与运行时 meta_agent 加载的 DEFAULT_CACHE (backend/data/confidence_calibration.json) 同目录，
    # 保证「拟合用的 DB」与「运行期读取的 JSON」落在同一 backend/data。
    core = os.path.dirname(os.path.abspath(_cc.__file__))
    return os.path.join(os.path.dirname(os.path.dirname(core)), "data")


def load_samples(db_path: str):
    # 用 sqlite3.backup() 对 live WAL 库做一致性快照再读，彻底避免与运行后端
    # (活跃写入) 的 WAL 读竞态（直接 shutil 复制 -wal 偶发不一致→被忽略→读到全 NULL 列）。
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="wxcal_")
    dst = os.path.join(tmp, "wx_prod.dat")
    try:
        src = sqlite3.connect(db_path, timeout=30)
        src.execute("PRAGMA busy_timeout=30000")
        dst_con = sqlite3.connect(dst, timeout=15)
        src.backup(dst_con)  # 一致性快照（内部处理 WAL 回放）
        dst_con.close()
        src.close()
        con = sqlite3.connect(dst, timeout=15)
        con.execute("PRAGMA busy_timeout=15000")
        cur = con.cursor()
        # 方向决策在 meta_agent_decision；盈亏用 net_profit
        q = (
            "SELECT meta_agent_decision, meta_agent_confidence, net_profit "
            "FROM trades "
            "WHERE meta_agent_decision IN ('BUY','SELL') "
            "AND meta_agent_confidence IS NOT NULL "
            "AND meta_agent_confidence > 0 "
            "AND close_time IS NOT NULL "
            "AND net_profit IS NOT NULL "
            "ORDER BY close_time ASC"
        )
        cur.execute(q)
        rows = cur.fetchall()
        con.close()
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    samples = []
    for dec, conf, pnl in rows:
        try:
            conf = float(conf)
            pnl = float(pnl)
        except Exception:
            continue
        if not (0.0 < conf < 1.0):
            # 置信度应在 (0,1)；越界视为异常跳过
            continue
        outcome = 1.0 if pnl > 0 else 0.0
        samples.append((conf, outcome))
    return samples


def reliability_table(samples, bins=10):
    """等频分箱：自报置信度区间 vs 实际命中率（校准前的偏差诊断）。"""
    n = len(samples)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: samples[i][0])
    chunk = max(1, n // bins)
    rows = []
    s = 0
    while s < n:
        e = min(s + chunk, n)
        idx = order[s:e]
        pred = sum(samples[i][0] for i in idx) / (e - s)
        obs = sum(samples[i][1] for i in idx) / (e - s)
        rows.append((pred, obs, e - s))
        s = e
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=60)
    ap.add_argument("--method", default="auto")
    args = ap.parse_args()

    db_path = os.path.join(_resolve_data_dir(), "wx_prod.dat")
    if not os.path.exists(db_path):
        print(f"[校准] 生产库未找到: {db_path}")
        sys.exit(1)

    samples = load_samples(db_path)
    print(f"[校准] 读取历史样本: {len(samples)} 笔（meta_agent_decision∈BUY/SELL 且已平仓）")

    if len(samples) < args.min_samples:
        print(f"[校准] 样本不足 {args.min_samples}，跳过拟合，保持透传（calibrated==raw）。")
        sys.exit(0)

    # 校准前诊断
    print("\n=== 校准前「可靠度」诊断（等频分箱：自报置信 vs 实际命中）===")
    print(f"{'自报区间中值':>14} | {'实际命中':>8} | {'样本数':>6}")
    for pred, obs, cnt in reliability_table(samples):
        bias = "过自信" if obs < pred - 0.03 else ("欠自信" if obs > pred + 0.03 else "基本校准")
        print(f"{pred:>14.2%} | {obs:>8.2%} | {cnt:>6}   {bias}")

    cal = ConfidenceCalibrator(method=args.method)
    ok = cal.fit(samples, min_samples=args.min_samples)
    rep = cal.report()
    print("\n=== 拟合结果 ===")
    print(f"  选用方法      : {rep['method']}")
    print(f"  训练样本量    : {rep['n_samples']}")
    print(f"  测试集 Brier  : {rep['test_brier']}")
    print(f"  测试集 ECE    : {rep['test_ece']}")
    print(f"  映射文件      : {cal.cache_path}")
    print(f"  拟合成功      : {ok}")

    # 抽样演示映射效果
    print("\n=== 映射演示（raw → calibrated）===")
    for r in [0.55, 0.65, 0.75, 0.85, 0.95]:
        print(f"  raw {r:>5.0%} -> calibrated {cal.calibrate(r):>5.0%}")
    print("\n完成。重启后端后 meta_agent 将自动加载该映射用于闸门阈值判定。")


if __name__ == "__main__":
    main()
