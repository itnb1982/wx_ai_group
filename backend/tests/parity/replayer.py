"""比对台回放器：把落盘的样本喂回函数，逐字段核对返回值是否还和当初一样。

典型用法（重构前后对比）：
    cases = load_cases(path)
    report = replay_all(cases, smart_exit.evaluate_position)
    assert_parity(report)

关于脱敏的诚实说明：
    serde 会把 password/token 这类字段落盘成 "<redacted>"。如果某个被脱敏的
    字段恰好参与了决策计算，那这条样本的回放结果就不可信。所以这里主动扫描
    入参里的 <redacted>，命中就把样本标成 tainted 并在报告里点名 —— 宁可吵，
    也不要让一条被污染的样本冒充"比对通过"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .asserter import ParityReport, diff_values
from .recorder import DEFAULT_DIR, Recorder
from .serde import from_jsonable, to_jsonable

REDACTED = "<redacted>"


@dataclass
class Case:
    seq: int
    tag: str
    args: list
    kwargs: dict
    expected: Any            # 录制时的返回值（jsonable 原样，不还原成对象）
    meta: dict = field(default_factory=dict)
    tainted: bool = False    # 入参含脱敏字段 → 回放结论不可信

    def call(self, func: Callable) -> Any:
        """用还原后的入参重跑，返回 jsonable 化的实际结果。"""
        return to_jsonable(func(*self.args, **self.kwargs))


def _has_redacted(node: Any, _depth: int = 0) -> bool:
    if _depth > 12:
        return False
    if isinstance(node, str):
        return node == REDACTED
    if isinstance(node, dict):
        return any(_has_redacted(v, _depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_redacted(v, _depth + 1) for v in node)
    return False


def resolve_path(tag_or_path: str | Path, out_dir: Optional[Path] = None) -> Path:
    """允许传 tag（去默认录制目录找）或直接传文件路径。"""
    p = Path(tag_or_path)
    if p.suffix == ".jsonl" and (p.is_absolute() or p.exists()):
        return p
    base = Path(out_dir) if out_dir else DEFAULT_DIR
    return base / f"{Recorder._safe_name(str(tag_or_path))}.jsonl"


def load_cases(tag_or_path: str | Path, out_dir: Optional[Path] = None,
               limit: Optional[int] = None) -> list[Case]:
    """读 JSONL 样本并还原入参。坏行跳过而不是整批失败（录制可能被中途掐断）。"""
    path = resolve_path(tag_or_path, out_dir)
    if not path.exists():
        return []

    cases: list[Case] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue          # 半截行（进程被杀）直接丢弃
            raw_args = row.get("args") or []
            raw_kwargs = row.get("kwargs") or {}
            cases.append(Case(
                seq=int(row.get("seq", len(cases))),
                tag=str(row.get("tag", "")),
                args=list(from_jsonable(raw_args)),
                kwargs=dict(from_jsonable(raw_kwargs)),
                expected=row.get("result"),
                meta=dict(row.get("meta") or {}),
                tainted=_has_redacted(raw_args) or _has_redacted(raw_kwargs),
            ))
            if limit and len(cases) >= limit:
                break
    return cases


def replay_all(cases: Iterable[Case], func: Callable, *,
               ignore_paths: Optional[Iterable[str]] = None,
               rtol: float = 1e-9, atol: float = 1e-12,
               skip_tainted: bool = False) -> ParityReport:
    """逐条回放并比对。单条抛异常也记成差异，不中断整批。"""
    report = ParityReport()
    for case in cases:
        if case.tainted and skip_tainted:
            continue
        report.total += 1
        prefix = f"#{case.seq}"
        try:
            actual = case.call(func)
        except Exception as e:                      # noqa: BLE001
            report.mismatched += 1
            from .asserter import Diff
            report.diffs.append(
                Diff(f"{prefix}.<调用>", "value", "正常返回", f"{type(e).__name__}: {e}")
            )
            continue

        diffs = diff_values(case.expected, actual, path=f"{prefix}.result",
                            rtol=rtol, atol=atol, ignore_paths=ignore_paths)
        if diffs:
            report.mismatched += 1
            if case.tainted:
                from .asserter import Diff
                diffs.insert(0, Diff(f"{prefix}.<样本>", "value",
                                     "入参完整", "入参含 <redacted>，结论不可信"))
            report.diffs.extend(diffs)
    return report
