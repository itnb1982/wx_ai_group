"""
万象Ai — Adaptive-OPRO 在线演化循环（M4 升级版）
==============================================
把"出场激进度(exit_aggressiveness, 0.3~0.8)"作为被 OPRO 优化的变量。

★ 2026-08-15 升级（第三优先·前沿 R&D）：
  原 M4 是朴素坐标下降（avg>0 → +0.03，否则 -0.03）。现升级为
  **Adaptive-OPRO（DeepMind arxiv:2309.03409 真义）**：本地 qwen3:8b 充当
  优化器，喂入「(解=激进度, 得分=fitness) 历史 + 当前最优 + 问题描述」的
  meta-prompt，由 8B 提出下一候选激进度。即 LLM-as-Optimizer。

铁律安全设计（全部对齐「提准非拦截 + 拦截须 walk-forward 验证能提升净盈利」）：
  1) LLM **只提议标量(0.3~0.8)**，绝不重写提示词文本 → 杜绝提示漂移失稳；
  2) 沿用 set_aggressiveness 钳制 + **PF<1/回撤>15$ 强制回滚闸**（live walk-forward
     验证，坏提议自动回退）；
  3) LLM 调用超时 15s，失败/越界 → 回退朴素 ±0.03 步，绝不阻塞演化；
  4) 低频触发（每 200 笔才询 LLM），不抢热路径、不干扰副驾/校对；
  5) 纯加法增强优化器搜索能力，**非拦截闸门**（激进度只改变"让利润奔跑 vs 优先保本"
     的软风格，不砍单、不 HOLD）。

fitness = PF × 胜率 / (1 + 回撤/$100)
  - PF = 窗口内总盈利 / 总亏损
  - 回滚：PF<1 或 回撤>15$ → 回退历史最佳激进度
  - 演化方向：窗口平均盈利→更激进；平均亏损→更保守

无 LLM 调用压力：仅在主号触发（跟号平仓是副本，不喂演化，守"增删副号不影响进化"铁律）。
"""

import os
import re
import threading
from collections import deque
from loguru import logger

# ★ 是否启用 LLM 作优化器（本地 qwen3:8b）。默认开；设 WX_OPRO_LLM=0 关回朴素模式。
_LLM_OPT_ENABLED = os.getenv("WX_OPRO_LLM", "1") not in ("0", "false", "False", "")
_LLM_EVERY = int(os.getenv("WX_OPRO_LLM_EVERY", "200"))  # 每多少笔主号平仓询一次 LLM
_LLM_TIMEOUT = float(os.getenv("WX_OPRO_LLM_TIMEOUT", "15"))
_HISTORY_CAP = 40  # 喂给 LLM 的(解,得分)历史容量


class OproEvolver:
    def __init__(self, window: int = 50):
        self.window = window
        self.recent: deque = deque(maxlen=200)   # 近期主号平仓 PnL（美元）
        self.trade_count = 0
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=_HISTORY_CAP)  # (激进度, fitness) 历史
        from app.services.memory_bank import get_memory_bank
        self.bank = get_memory_bank()

    def record_trade(self, pnl: float):
        """主号每平一笔调用：累积 PnL，达窗口则演化一次。"""
        with self._lock:
            self.recent.append(float(pnl))
            self.trade_count += 1
            if self.trade_count % self.window != 0:
                return
            pnls = list(self.recent)
        self._evolve(pnls)

    # ───────── LLM 作优化器（Adaptive-OPRO）─────────
    def _llm_propose_next(self, fitness: float) -> "float | None":
        """用本地 qwen3:8b 从(解,得分)历史提出下一候选激进度。失败返回 None。"""
        if not _LLM_OPT_ENABLED:
            return None
        try:
            from app.services.local_llm_service import get_local_llm
            svc = get_local_llm()
            if svc is None or not svc.available():
                return None
            hist_lines = []
            for i, (v, s) in enumerate(self.history):
                hist_lines.append(f"  候选{i+1}: 激进度={v:.2f} → fitness={s:.3f}")
            hist_block = "\n".join(hist_lines) if hist_lines else "  (暂无历史)"
            cur = self.bank.exit_aggressiveness
            best = self.bank.best_aggressiveness
            best_f = self.bank.best_fitness
            prompt = (
                "你是优化器。任务：优化一个连续参数 exit_aggressiveness(取值0.30~0.80)。\n"
                "含义：0.30=保守(优先保本、快速平仓)；0.80=激进(让利润奔跑、追踪止损)。\n"
                "目标：最大化 fitness = PF×胜率/(1+回撤/$100)，其中 PF=总盈利/总亏损。\n"
                "已知：当前激进度={:.2f}，历史最优激进度={:.2f}(fitness={:.3f})。\n"
                "历史尝试(解,得分)对：\n{}\n"
                "请基于以上历史(哪些值得分高、趋势如何)推断下一个应尝试的激进度值，\n"
                "以探索更高 fitness。只输出一个 0.30~0.80 之间的浮点数，不要任何解释。\n"
                "下一候选激进度="
            ).format(cur, best, best_f, hist_block)
            raw = svc.generate_text(prompt, timeout=_LLM_TIMEOUT)
            if not raw:
                return None
            # 取第一个落在 [0.30,0.80] 的浮点
            for mm in re.finditer(r"0\.\d+|\b[01]?\.\d+\b", raw):
                val = float(mm.group(0))
                if 0.30 <= val <= 0.80:
                    return round(val, 3)
            return None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[M4演化] LLM优化器异常(降级朴素步): {e}")
            return None

    def _evolve(self, pnls: list):
        try:
            if len(pnls) < 10:
                return
            gross_profit = sum(p for p in pnls if p > 0)
            gross_loss = -sum(p for p in pnls if p < 0)
            pf_ratio = (gross_profit / gross_loss) if gross_loss > 0 else (2.0 if gross_profit > 0 else 0.0)
            win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
            # 权益曲线最大回撤
            eq = 0.0
            peak = 0.0
            maxdd = 0.0
            for p in pnls:
                eq += p
                peak = max(peak, eq)
                maxdd = max(maxdd, peak - eq)
            drawdown = maxdd
            fitness = pf_ratio * win_rate / (1.0 + drawdown / 100.0)

            cur_val = self.bank.exit_aggressiveness
            self.history.append((cur_val, round(fitness, 3)))

            # ★ 强制回滚：PF<1 或 回撤>15$ → 回退历史最佳
            if pf_ratio < 1.0 or drawdown > 15.0:
                self.bank.rollback_to_best()
                self.bank.report_fitness(fitness)
                logger.warning(
                    f"[M4演化] PF={pf_ratio:.2f}<1 或 回撤${drawdown:.2f}>15 → "
                    f"回滚最佳激进度={self.bank.exit_aggressiveness:.2f} (fitness={fitness:.3f})"
                )
                return

            # ★ Adaptive-OPRO：LLM 作优化器提出下一候选（低频），失败回退朴素步
            avg = sum(pnls) / len(pnls)
            new = None
            if _LLM_OPT_ENABLED and (self.trade_count % _LLM_EVERY == 0):
                proposed = self._llm_propose_next(fitness)
                if proposed is not None and 0.30 <= proposed <= 0.80:
                    new = proposed
                    src = "LLM优化器"
                else:
                    new = cur_val + 0.03 if avg > 0 else cur_val - 0.03
                    src = "朴素步(LLM无效)"
            else:
                new = cur_val + 0.03 if avg > 0 else cur_val - 0.03
                src = "朴素步"

            self.bank.set_aggressiveness(new)
            self.bank.report_fitness(fitness)
            logger.info(
                f"[M4演化] 窗口{len(pnls)}笔 PF={pf_ratio:.2f} 胜率{win_rate:.0%} "
                f"回撤${drawdown:.2f} fitness={fitness:.3f} → 来源={src} "
                f"激进度 {cur_val:.2f}→{new:.2f}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[M4演化] 演化异常(忽略): {e}")


_evolver: "OproEvolver | None" = None
_evolver_lock = threading.Lock()


def get_evolver() -> OproEvolver:
    global _evolver
    if _evolver is None:
        with _evolver_lock:
            if _evolver is None:
                _evolver = OproEvolver()
    return _evolver
