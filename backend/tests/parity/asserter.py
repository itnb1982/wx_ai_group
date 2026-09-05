"""比对台断言器：逐字段比对「录制时的返回值」与「回放后的返回值」。

V6 12.3 对 L3 的要求是"逐字段一致"，不是"看起来差不多"。所以这里：
  · 递归进 dict / list 的每一层，路径精确到 result.new_sl 这种粒度；
  · 浮点用相对+绝对混合容差（金融数值跨量级：0.4 的 close_pct 和 4281.35 的
    价格不能用同一个绝对容差衡量）；
  · 少字段、多字段、类型变了，都算差异 —— 重构最爱悄悄丢字段；
  · bool 和 int 严格区分（True == 1 在 Python 成立，但在契约上是两回事）。

刻意不做"智能忽略"：任何自动放行的规则，都会在某天放行一个真事故。
要忽略必须由调用方显式传 ignore_paths，写进代码里、看得见。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 相对容差：浮点运算重排（如 a*b/c 换成 a/c*b）带来的末位抖动
DEFAULT_RTOL = 1e-9
# 绝对容差：兜住 0 附近的比较
DEFAULT_ATOL = 1e-12

_MISSING = object()


@dataclass
class Diff:
    path: str
    kind: str          # value | type | missing | extra | length
    expected: Any
    actual: Any

    def __str__(self) -> str:
        if self.kind == "missing":
            return f"  [缺字段] {self.path}: 录制有 {self.expected!r}，回放没有"
        if self.kind == "extra":
            return f"  [多字段] {self.path}: 录制没有，回放多出 {self.actual!r}"
        if self.kind == "type":
            return (f"  [类型变] {self.path}: 录制 {type(self.expected).__name__}"
                    f"({self.expected!r}) → 回放 {type(self.actual).__name__}({self.actual!r})")
        if self.kind == "length":
            return f"  [长度变] {self.path}: 录制 {self.expected} 项 → 回放 {self.actual} 项"
        return f"  [值不同] {self.path}: 录制 {self.expected!r} → 回放 {self.actual!r}"


@dataclass
class ParityReport:
    total: int = 0
    mismatched: int = 0
    diffs: list[Diff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diffs

    def render(self, limit: int = 40) -> str:
        if self.ok:
            return f"比对通过：{self.total} 条样本逐字段一致"
        head = (f"比对失败：{self.total} 条样本中 {self.mismatched} 条不一致，"
                f"共 {len(self.diffs)} 处差异")
        body = "\n".join(str(d) for d in self.diffs[:limit])
        tail = "" if len(self.diffs) <= limit else f"\n  ...（另有 {len(self.diffs) - limit} 处）"
        return head + "\n" + body + tail


def _floats_equal(a: float, b: float, rtol: float, atol: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True          # 两边都 NaN 视为一致（NaN != NaN 会误报）
    if math.isinf(a) or math.isinf(b):
        return a == b        # inf 必须同号同类
    return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)


def diff_values(expected: Any, actual: Any, *, path: str = "result",
                rtol: float = DEFAULT_RTOL, atol: float = DEFAULT_ATOL,
                ignore_paths: Optional[Iterable[str]] = None) -> list[Diff]:
    """递归比对两个 jsonable 结构，返回全部差异（不是遇到第一个就停）。"""
    ignore = set(ignore_paths or ())
    return _diff(expected, actual, path, rtol, atol, ignore)


def _diff(exp: Any, act: Any, path: str, rtol: float, atol: float,
          ignore: set[str]) -> list[Diff]:
    if path in ignore:
        return []

    out: list[Diff] = []

    # bool 必须先于 int 判断：isinstance(True, int) 为真
    if isinstance(exp, bool) or isinstance(act, bool):
        if type(exp) is not type(act):
            return [Diff(path, "type", exp, act)]
        if exp != act:
            return [Diff(path, "value", exp, act)]
        return []

    if isinstance(exp, (int, float)) and isinstance(act, (int, float)):
        if not _floats_equal(float(exp), float(act), rtol, atol):
            out.append(Diff(path, "value", exp, act))
        return out

    if exp is None or act is None:
        if exp is not act:
            out.append(Diff(path, "value", exp, act))
        return out

    if isinstance(exp, dict) and isinstance(act, dict):
        for k in exp:
            sub = f"{path}.{k}"
            if sub in ignore:
                continue
            if k not in act:
                out.append(Diff(sub, "missing", exp[k], _MISSING))
            else:
                out.extend(_diff(exp[k], act[k], sub, rtol, atol, ignore))
        for k in act:
            sub = f"{path}.{k}"
            if k not in exp and sub not in ignore:
                out.append(Diff(sub, "extra", _MISSING, act[k]))
        return out

    if isinstance(exp, list) and isinstance(act, list):
        if len(exp) != len(act):
            out.append(Diff(path, "length", len(exp), len(act)))
        for i in range(min(len(exp), len(act))):
            out.extend(_diff(exp[i], act[i], f"{path}[{i}]", rtol, atol, ignore))
        return out

    if type(exp) is not type(act):
        return [Diff(path, "type", exp, act)]

    if exp != act:
        out.append(Diff(path, "value", exp, act))
    return out


def assert_parity(report: ParityReport) -> None:
    """报告里有任何差异就炸，并把可读差异清单带出来。"""
    if not report.ok:
        raise AssertionError(report.render())
