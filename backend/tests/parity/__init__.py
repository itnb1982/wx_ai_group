"""回归比对台（V6 12.3 / L3 契约层）。

三件套：
  recorder  录制线上真实调用的入参与返回值（默认关闭，环境变量开启）
  replayer  离线回放样本
  asserter  逐字段比对，任何差异都点名到字段路径

存在的意义：重构一个决策函数时，能用真实历史输入证明「行为没变」，
而不是靠肉眼读 diff 和祈祷。
"""
from .asserter import Diff, ParityReport, assert_parity, diff_values
from .recorder import Recorder, install, recording, uninstall
from .replayer import Case, load_cases, replay_all
from .serde import from_jsonable, to_jsonable

__all__ = [
    "Diff", "ParityReport", "assert_parity", "diff_values",
    "Recorder", "install", "recording", "uninstall",
    "Case", "load_cases", "replay_all",
    "to_jsonable", "from_jsonable",
]
