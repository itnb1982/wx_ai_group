"""supervisor 日志轮转（2026-08-08 审计新增）。

背景：uvicorn 子进程的 stdout 以 "a" 模式纯追加写入，从不轮转。
实测 supervisor_uvicorn.log 已达 130MB 且持续增长。7×24 交易下最终会把
磁盘吃满——磁盘一满，写库和写日志全线失败，足以拖死整个交易系统。

这些用例守住三件事：
  1. 没超限的日志不许动（避免无谓地打断正在排查的日志）
  2. 超限时正确轮转，且历史文件数量有上限（不能把"一个大文件"变成"一堆大文件"）
  3. 轮转失败绝不能抛异常挡住服务启动
"""
import os

import pytest

from supervisor import _rotate_if_big

pytestmark = pytest.mark.unit


def _mk(path, mb: float):
    """造一个指定大小（MB）的文件。"""
    with open(path, "wb") as f:
        f.write(b"x" * int(mb * 1024 * 1024))


def test_small_log_is_left_alone(tmp_path):
    p = tmp_path / "app.log"
    _mk(p, 0.1)
    _rotate_if_big(str(p), max_mb=50)
    assert p.exists(), "未超限的日志被动了"
    assert not (tmp_path / "app.log.1").exists()


def test_oversized_log_rotates(tmp_path):
    p = tmp_path / "app.log"
    _mk(p, 2)
    _rotate_if_big(str(p), max_mb=1)
    assert not p.exists(), "超限日志应被改名让位给新文件"
    assert (tmp_path / "app.log.1").exists()


def test_history_is_capped(tmp_path):
    """连续轮转多次，历史文件数量必须被 keep 限制住。"""
    p = tmp_path / "app.log"
    for _ in range(6):
        _mk(p, 2)
        _rotate_if_big(str(p), max_mb=1, keep=3)

    rotated = sorted(f.name for f in tmp_path.iterdir() if ".log." in f.name)
    assert rotated == ["app.log.1", "app.log.2", "app.log.3"], (
        f"历史文件未被限制在 keep=3，实际: {rotated}"
    )


def test_rotation_preserves_newest_first(tmp_path):
    """.1 必须是最近一次轮转出来的内容（顺序不能错位）。"""
    p = tmp_path / "app.log"

    with open(p, "wb") as f:
        f.write(b"A" * (2 * 1024 * 1024))
    _rotate_if_big(str(p), max_mb=1, keep=3)

    with open(p, "wb") as f:
        f.write(b"B" * (2 * 1024 * 1024))
    _rotate_if_big(str(p), max_mb=1, keep=3)

    assert (tmp_path / "app.log.1").read_bytes()[:1] == b"B", ".1 应是最新的"
    assert (tmp_path / "app.log.2").read_bytes()[:1] == b"A", ".2 应是较旧的"


def test_missing_file_is_noop(tmp_path):
    _rotate_if_big(str(tmp_path / "nope.log"))  # 不应抛异常


def test_never_raises_on_error(tmp_path, monkeypatch):
    """轮转出任何问题都只能吞掉——绝不能挡住服务启动。"""
    p = tmp_path / "app.log"
    _mk(p, 2)

    def boom(*a, **kw):
        raise OSError("模拟文件被占用")

    monkeypatch.setattr(os, "replace", boom)
    _rotate_if_big(str(p), max_mb=1)  # 不抛 = 通过
    assert p.exists(), "轮转失败时原文件应保留，继续写大文件也比崩了强"
