"""
万象Ai — FinMem 风格三层记忆银行（M3b）
=====================================
为 M2(Reflexion lesson) 与 M4(OPRO 演化) 提供持久化记忆底座。

为何 JSON 而非 SQLite：
  Windows Defender 实时扫描会对被写入的 SQLite 文件异步加只读锁，
  导致 ai_activities / evolution_logs 落库间歇性失败。本题只写小型
  JSON（原子替换），绕开该坑，重启不丢记忆。

三层（容量按方案全锁）：
  - Working   50   近期逐笔平仓事件（原始观察，滚动）
  - Episodic  500  场景→结果对（可回放的情境记忆）
  - Semantic  100  抽象教训 lesson（去重，M2 注入 AI 出场 / M4 演化读取）

所有写操作均 try/except 包裹，记忆故障绝不波及交易主链路。
"""
import json
import os
import time
import threading
from collections import deque

# 落盘路径：backend/memory_bank.json
_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "memory_bank.json",
)

# ★ 2026-08-05 修复"越跑越忘"：原 Semantic=100 / Episodic=500 的 ring buffer
#   跑几天后最早学到的教训被 FIFO 挤出，AI 看似"开了几天单却不开窍"。
#   扩大容量并放宽落盘间隔，让实盘教训更持久地参与决策。
_CAP_WORKING = 120
_CAP_EPISODIC = 1500
_CAP_SEMANTIC = 400
_SAVE_INTERVAL = 15.0  # 落盘限频（秒）

_lock = threading.Lock()


class MemoryBank:
    def __init__(self, path: str = None):
        self.path = path or _FILE
        if not getattr(self, "_atexit_registered", False):
            import atexit
            atexit.register(self._maybe_save, force=True)  # 进程退出前强制落盘(根治重启丢记忆)
            self._atexit_registered = True
        self.working: deque = deque(maxlen=_CAP_WORKING)
        self.episodic: deque = deque(maxlen=_CAP_EPISODIC)
        self.semantic: deque = deque(maxlen=_CAP_SEMANTIC)
        self._seen_lessons = set()      # lesson 文本哈希，去重
        self._last_save = 0.0
        self._load()
        # ★ M4 OPRO 演化标量：出场激进度(0.3~0.8)，映射为注入 AI 出场的指令文本
        self.exit_aggressiveness = 0.55
        self._best_aggressiveness = 0.55
        self._best_fitness = -999.0

    # ───────── 写入 ─────────
    def add_working(self, item: dict):
        with _lock:
            self.working.append({"t": time.time(), **item})
        self._maybe_save()

    def add_episodic(self, scenario: str, outcome: dict):
        with _lock:
            self.episodic.append({"t": time.time(), "scenario": scenario, "outcome": outcome})
        self._maybe_save()

    def add_lesson(self, lesson: str, source: str = "reflexion") -> bool:
        """新增抽象教训（去重）。返回是否真正新增。"""
        lesson = (lesson or "").strip()
        if not lesson:
            return False
        key = lesson[:80]
        with _lock:
            if key in self._seen_lessons:
                return False
            self._seen_lessons.add(key)
            self.semantic.append({
                "t": time.time(), "lesson": lesson, "source": source,
            })
        self._maybe_save()
        return True

    def top_lessons(self, k: int = 3) -> list:
        with _lock:
            return [x["lesson"] for x in list(self.semantic)[-k:]]

    # ───────── M4：OPRO 演化接口 ─────────
    def set_aggressiveness(self, val: float):
        self.exit_aggressiveness = max(0.3, min(0.8, round(float(val), 3)))
        self._maybe_save()

    def report_fitness(self, fitness: float):
        """OPRO 每轮回报 fitness；若优于历史最佳则固化，否则保留最佳（强制回滚在 evolver 内）。"""
        with _lock:
            if fitness > self._best_fitness:
                self._best_fitness = fitness
                self._best_aggressiveness = self.exit_aggressiveness
        self._maybe_save()

    def rollback_to_best(self):
        with _lock:
            self.exit_aggressiveness = self._best_aggressiveness
        self._maybe_save()

    # ───────── 公开只读访问器（避免外部直接读 _best_* 私有属性）─────────
    @property
    def best_aggressiveness(self) -> float:
        return self._best_aggressiveness

    @property
    def best_fitness(self) -> float:
        return self._best_fitness

    def aggressiveness_prompt(self) -> str:
        """把当前激进度映射为注入 AI 出场的指令文本（无 LLM，确定性）。"""
        a = self.exit_aggressiveness
        if a >= 0.7:
            return ("出场偏激进：浮盈达 MFE 的 60% 即启动分批止盈，允许让利润奔跑但设追踪止损；"
                    "亏损单在回吐 30% 浮盈前果断离场。")
        if a >= 0.5:
            return ("出场中性：浮盈达 MFE 的 50% 启动分批止盈(≤4批)，回吐超 40% 浮盈即离场；"
                    "结构转弱优先锁利。")
        return ("出场偏保守：浮盈达 MFE 的 40% 即先平一半锁利，剩余单 tight 追踪止损；"
                "任何回吐超 25% 浮盈立即全平，优先保本。")

    # ───────── 持久化 ─────────
    def _maybe_save(self, force: bool = False):
        try:
            now = time.time()
            if not force and (now - self._last_save) < _SAVE_INTERVAL:
                return
            self._last_save = now
            payload = {
                "working": list(self.working)[-_CAP_WORKING:],
                "episodic": list(self.episodic)[-_CAP_EPISODIC:],
                "semantic": list(self.semantic)[-_CAP_SEMANTIC:],
                "exit_aggressiveness": self.exit_aggressiveness,
                "best_aggressiveness": self._best_aggressiveness,
                "best_fitness": self._best_fitness,
            }
            self._save_once(payload, attempts=3)
        except Exception as e:  # noqa: BLE001
            import loguru
            loguru.logger.warning(f"[记忆库] 落盘失败(不影响交易): {e}")

    def _save_once(self, payload, attempts: int = 3):
        """原子写 + WinError 指数退避重试(国际调研精髓: LangGraph 持久化最佳实践)。
        落盘失败不崩、内存 deque 不丢，下次周期/退出时再保存。"""
        import loguru
        last = None
        for i in range(attempts):
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                return
            except (PermissionError, OSError) as e:
                last = e
                if i < attempts - 1:
                    time.sleep(0.2 * (2 ** i))
                    continue
        loguru.logger.warning(f"[记忆库] 落盘失败(内存保留,下次重试): {last}")

    def _load(self):
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.working = deque(d.get("working", []), maxlen=_CAP_WORKING)
            self.episodic = deque(d.get("episodic", []), maxlen=_CAP_EPISODIC)
            self.semantic = deque(d.get("semantic", []), maxlen=_CAP_SEMANTIC)
            self._seen_lessons = {x.get("lesson", "")[:80] for x in self.semantic}
            self.exit_aggressiveness = float(d.get("exit_aggressiveness", 0.55))
            self._best_aggressiveness = float(d.get("best_aggressiveness", self.exit_aggressiveness))
            self._best_fitness = float(d.get("best_fitness", -999.0))
        except Exception as e:  # noqa: BLE001
            import loguru
            loguru.logger.warning(f"[记忆库] 加载失败(使用空记忆): {e}")


# 全局单例（与 MetaAgent 同生命周期：进程级共享）
_bank: "MemoryBank | None" = None


def get_memory_bank() -> MemoryBank:
    global _bank
    if _bank is None:
        _bank = MemoryBank()
    return _bank
