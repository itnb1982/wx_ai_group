"""比对台录制器：把线上真实发生的决策调用，原样落盘成可回放的样本。

设计红线（安全网自己绝不能变成新的事故源）：
  1. 默认关闭。只有环境变量 WX_PARITY_RECORD=1 时 install() 才真正生效。
  2. 录制失败一律吞掉。任何序列化/磁盘异常都不得冒泡到交易主循环。
  3. 绝不改变被包装函数的返回值，也绝不吞掉业务异常（业务异常照常抛）。
  4. 有条数上限。默认每个 tag 最多 200 条，防止跑一夜把磁盘写满。

用法（线上录制）：
    from tests.parity import recorder
    import app.services.trade_executor as te
    recorder.install(te, "smart_evaluate_position", tag="smart_exit.evaluate_position")

用法（测试内录制）：
    with recorder.recording("demo", out_dir=tmp_path) as rec:
        rec.capture(kwargs=..., result=...)
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .serde import to_jsonable

# 录制文件默认落在 tests/parity/recordings/ 下（该目录已 gitignore）
DEFAULT_DIR = Path(__file__).resolve().parent / "recordings"
DEFAULT_MAX_RECORDS = 200

_lock = threading.Lock()
_counts: dict[str, int] = {}


def enabled() -> bool:
    """录制总开关。默认关闭，避免任何人误把它带上生产。"""
    return os.getenv("WX_PARITY_RECORD", "").strip() in ("1", "true", "TRUE", "yes")


class Recorder:
    """一个 tag 对应一个 JSONL 文件，一行一条调用样本。"""

    def __init__(self, tag: str, out_dir: Optional[Path] = None,
                 max_records: int = DEFAULT_MAX_RECORDS):
        self.tag = tag
        self.out_dir = Path(out_dir) if out_dir else DEFAULT_DIR
        self.max_records = max_records
        self.path = self.out_dir / f"{self._safe_name(tag)}.jsonl"

    @staticmethod
    def _safe_name(tag: str) -> str:
        return "".join(c if (c.isalnum() or c in "._-") else "_" for c in tag)

    def capture(self, *, args: tuple = (), kwargs: Optional[dict] = None,
                result: Any = None, meta: Optional[dict] = None) -> bool:
        """落盘一条样本。返回是否真的写入（超限/异常均返回 False，且不抛）。"""
        try:
            with _lock:
                n = _counts.get(self.tag, 0)
                if n >= self.max_records:
                    return False
                _counts[self.tag] = n + 1

            row = {
                "tag": self.tag,
                "ts": time.time(),
                "seq": n,
                "args": to_jsonable(list(args)),
                "kwargs": to_jsonable(kwargs or {}),
                "result": to_jsonable(result),
                "meta": to_jsonable(meta or {}),
            }
            line = json.dumps(row, ensure_ascii=False, allow_nan=False)

            self.out_dir.mkdir(parents=True, exist_ok=True)
            with _lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            return True
        except Exception:
            # 录制永远不许影响业务：静默失败
            return False

    def count(self) -> int:
        return _counts.get(self.tag, 0)


def install(module: Any, func_name: str, *, tag: Optional[str] = None,
            out_dir: Optional[Path] = None,
            max_records: int = DEFAULT_MAX_RECORDS,
            force: bool = False) -> Optional[Callable]:
    """把 module.func_name 替换成"调用后顺手落盘"的包装版。

    返回原函数（便于 uninstall）；未启用录制时返回 None、不做任何改动。
    幂等：重复 install 同一目标不会套娃。
    """
    if not (force or enabled()):
        return None

    original = getattr(module, func_name)
    if getattr(original, "__parity_wrapped__", False):
        return getattr(original, "__parity_original__", original)

    rec = Recorder(tag or f"{getattr(module, '__name__', '?')}.{func_name}",
                   out_dir=out_dir, max_records=max_records)

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        # 业务异常照常抛，不录（异常样本不属于"返回值一致性"比对范畴）
        result = original(*args, **kwargs)
        rec.capture(args=args, kwargs=kwargs, result=result)
        return result

    wrapper.__parity_wrapped__ = True          # type: ignore[attr-defined]
    wrapper.__parity_original__ = original     # type: ignore[attr-defined]
    wrapper.__parity_recorder__ = rec          # type: ignore[attr-defined]
    setattr(module, func_name, wrapper)
    return original


def uninstall(module: Any, func_name: str) -> bool:
    """还原被 install 包装过的函数。"""
    cur = getattr(module, func_name, None)
    if cur is not None and getattr(cur, "__parity_wrapped__", False):
        setattr(module, func_name, cur.__parity_original__)
        return True
    return False


def reset_counts(tag: Optional[str] = None) -> None:
    """清空条数计数（测试用；线上不要调）。"""
    with _lock:
        if tag is None:
            _counts.clear()
        else:
            _counts.pop(tag, None)


@contextlib.contextmanager
def recording(tag: str, out_dir: Optional[Path] = None,
              max_records: int = DEFAULT_MAX_RECORDS):
    """测试内手动录制：with recording("x") as rec: rec.capture(...)"""
    reset_counts(tag)
    try:
        yield Recorder(tag, out_dir=out_dir, max_records=max_records)
    finally:
        reset_counts(tag)
