"""Chronos torch 子进程探针隔离测试（Phase 2 · 崩溃隔离）

★ 背景（2026-08-08 生产事故）：
  venv 的 Python 被就地升级（pyvenv.cfg 记 3.13.12，实际 3.13.14），
  torch 2.6.0 的 `_C.cp313-win_amd64.pyd` 与新解释器 ABI 不匹配，
  `import torch` 触发 **原生 access violation（0xC0000005 / 退出码 3221225477）**。

  原实现用 `try/except Exception` 包裹 `import torch` —— 这是「虚假降级保护」：
  原生段错误不经过 Python 异常机制，整个 uvicorn 进程瞬间消失，
  supervisor 误判"无声假死"→ 无限重启死循环。

★ 本测试守护的不变量：
  1. 主进程**永不裸 import torch**，必须先经子进程探针；
  2. 探针子进程崩溃（任意非零退出码）→ 主进程优雅降级，绝不崩；
  3. 探针超时 → 降级，且**不**写磁盘缓存（可能只是机器临时慢）；
  4. 降级是**永久**的（不重复探测，避免每轮决策都 fork 一次）；
  5. 缓存文件损坏 → 静默重探，不抛异常；
  6. status 属性不得裸 `__import__("torch")`。

★ 自证有效（反向用例）：
  test_regression_guard_* 通过静态源码检查确保"探针在 import torch 之前"，
  一旦有人把探针删掉或挪到 import 之后，测试立刻炸。
"""
import inspect
import json
import re
import subprocess
import sys

import pytest

from app.services import chronos_service as cs
from app.services import chronos_shared as csh  # ★ 2026-08-17 修复：探针缓存实现已迁至 chronos_shared，测试须 patch 新模块


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """每个用例独立缓存文件 + 清空进程内缓存，避免互相污染。"""
    monkeypatch.setattr(csh, "_PROBE_CACHE_PATH", str(tmp_path / "torch_probe_cache.json"))
    # ★ 2026-08-17：Git Bash 下 _is_msys_env()=True 会阻止崩溃结论写盘（故意设计），
    #   测试在 Git Bash 跑会误挂。统一模拟非 MSYS 环境，使"确定性失败写盘"可测。
    monkeypatch.setattr(csh, "_is_msys_env", lambda: False)
    csh._PROBE_MEM_CACHE.clear()
    csh._PROBE_RESULT = None   # ★ 2026-08-17 修复：get_probe 模块级惰性缓存，不清理会跨用例污染
    csh._LOAD_ERROR = None
    csh._PIPE = None           # ★ 2026-08-17 修复：永久降级标志/实例不清，下个用例直接跳过加载路径
    csh._PIPE_LOADED = False
    yield
    csh._PROBE_MEM_CACHE.clear()
    csh._PROBE_RESULT = None
    csh._LOAD_ERROR = None
    csh._PIPE = None
    csh._PIPE_LOADED = False


@pytest.fixture
def fresh_engine():
    """全新引擎实例（绕开单例，避免跨用例状态泄漏）。"""
    eng = cs.ChronosEngine()
    return eng


def _fake_run(returncode, stdout="", stderr="", raise_timeout=False):
    """构造 _run_probe_subprocess 的替身。"""
    def _inner(python_exe, timeout):
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=[python_exe], timeout=timeout)
        return returncode, stdout, stderr
    return _inner


_OK_STDOUT = json.dumps({"ok": True, "torch": "2.6.0+cu124", "cuda": True})


# ---------------------------------------------------------------- 探针本身


@pytest.mark.unit
def test_probe_ok_parses_torch_version(monkeypatch):
    """探针子进程正常退出 → ok=True，带回 torch 版本与 cuda 标志。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(0, _OK_STDOUT))
    r = cs.probe_torch_usable()
    assert r["ok"] is True
    assert r["torch_version"] == "2.6.0+cu124"
    assert r["cuda"] is True


@pytest.mark.unit
def test_probe_access_violation_is_caught_not_crash(monkeypatch):
    """★ 核心：子进程 access violation(3221225477) → 主进程活着并返回 ok=False。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(3221225477, "", ""))
    r = cs.probe_torch_usable()
    assert r["ok"] is False
    assert r["returncode"] == 3221225477
    # 原因里必须能一眼看出是原生崩溃（含 0xC0000005 线索），便于运维定位
    assert "0xC0000005" in r["reason"] or "3221225477" in r["reason"]


@pytest.mark.unit
def test_probe_import_error_is_caught(monkeypatch):
    """普通 ImportError（未装 torch）→ ok=False，reason 带 stderr 摘要。"""
    monkeypatch.setattr(
        csh, "_run_probe_subprocess",
        _fake_run(1, "", "ModuleNotFoundError: No module named 'torch'"),
    )
    r = cs.probe_torch_usable()
    assert r["ok"] is False
    assert "torch" in r["reason"]


@pytest.mark.unit
def test_probe_timeout_degrades_and_is_not_cached_on_disk(monkeypatch, tmp_path):
    """超时 → 降级；但不写磁盘缓存（机器临时慢不该被永久判死）。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(0, raise_timeout=True))
    r = cs.probe_torch_usable()
    assert r["ok"] is False
    assert "超时" in r["reason"] or "timeout" in r["reason"].lower()
    assert not (tmp_path / "torch_probe_cache.json").exists()


@pytest.mark.unit
def test_probe_deterministic_failure_is_cached_on_disk(monkeypatch):
    """确定性失败（崩溃/非零退出）→ 写磁盘缓存，下次冷启动秒降级不再 fork。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(3221225477, "", ""))
    cs.probe_torch_usable()

    with open(csh._PROBE_CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data, "确定性失败必须落盘"
    assert all(v.get("ok") is False for v in data.values())


@pytest.mark.unit
def test_probe_uses_memory_cache_no_second_subprocess(monkeypatch):
    """同一进程内只探一次（避免每轮决策 fork）。"""
    calls = {"n": 0}

    def _counting(python_exe, timeout):
        calls["n"] += 1
        return 0, _OK_STDOUT, ""

    monkeypatch.setattr(csh, "_run_probe_subprocess", _counting)
    cs.probe_torch_usable()
    cs.probe_torch_usable()
    cs.probe_torch_usable()
    assert calls["n"] == 1


@pytest.mark.unit
def test_probe_corrupt_cache_file_recovers(monkeypatch):
    """缓存文件损坏 → 静默重探，绝不抛异常。"""
    with open(csh._PROBE_CACHE_PATH, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(0, _OK_STDOUT))
    r = cs.probe_torch_usable()
    assert r["ok"] is True


@pytest.mark.unit
def test_probe_disk_cache_hit_skips_subprocess(monkeypatch):
    """磁盘缓存命中 → 不再起子进程（冷启动提速的关键）。"""
    fp = csh._torch_fingerprint()
    with open(csh._PROBE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({fp: {"ok": False, "reason": "历史崩溃", "returncode": 3221225477}}, f)

    def _boom(python_exe, timeout):
        raise AssertionError("缓存命中时不应起子进程")

    monkeypatch.setattr(csh, "_run_probe_subprocess", _boom)
    r = cs.probe_torch_usable()
    assert r["ok"] is False
    assert r["reason"] == "历史崩溃"


@pytest.mark.unit
def test_fingerprint_changes_with_interpreter(monkeypatch):
    """★ 换解释器 → 指纹变 → 旧结论作废（正是本次事故的场景：3.13.12→3.13.14）。"""
    fp1 = csh._torch_fingerprint()
    monkeypatch.setattr(sys, "version", "3.13.14 (main, Jun 11 2026) [MSC v.1944 64 bit]")
    fp2 = csh._torch_fingerprint()
    assert fp1 != fp2


# ---------------------------------------------------------------- 引擎降级


@pytest.mark.unit
def test_ensure_loaded_degrades_when_probe_fails(monkeypatch, fresh_engine):
    """★ 核心：探针失败 → _ensure_loaded 返回 False，且**没有**在本进程 import torch。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(3221225477, "", ""))

    imported = {"torch": False}
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _guard(name, *a, **kw):
        if name == "torch" or name.startswith("torch."):
            imported["torch"] = True
            raise AssertionError("探针失败后主进程绝不允许 import torch！")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _guard)
    ok = fresh_engine._ensure_loaded()
    monkeypatch.undo()

    assert ok is False
    assert imported["torch"] is False
    assert fresh_engine._load_error
    assert "探针" in fresh_engine._load_error


@pytest.mark.unit
def test_degradation_is_permanent(monkeypatch, fresh_engine):
    """降级后不重复探测（第二次调用不再 fork）。"""
    calls = {"n": 0}

    def _counting(python_exe, timeout):
        calls["n"] += 1
        return 3221225477, "", ""

    monkeypatch.setattr(csh, "_run_probe_subprocess", _counting)
    assert fresh_engine._ensure_loaded() is False
    assert fresh_engine._ensure_loaded() is False
    assert fresh_engine._ensure_loaded() is False
    assert calls["n"] == 1


@pytest.mark.unit
def test_forecast_returns_none_when_degraded(monkeypatch, fresh_engine):
    """降级状态下 forecast 返回 None（调用方走 SMC/Regime 回退），不抛异常。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(3221225477, "", ""))
    out = fresh_engine.forecast([2000.0 + i for i in range(64)])
    assert out is None


@pytest.mark.unit
def test_status_never_imports_torch_when_degraded(monkeypatch, fresh_engine):
    """status 属性在降级态不得裸 import torch（原实现 __import__('torch') 会二次触发崩溃）。"""
    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(3221225477, "", ""))
    fresh_engine._ensure_loaded()

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _guard(name, *a, **kw):
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("status 不得 import torch！")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _guard)
    st = fresh_engine.status
    monkeypatch.undo()

    assert st["loaded"] is False
    assert st["probe_ok"] is False
    # ★ 2026-08-17：合并后 Chronos-2 强制 CPU，cuda_available 恒 False（有意设计），旧断言 None 过时
    assert st["cuda_available"] is False


@pytest.mark.unit
def test_probe_ok_then_normal_load_path(monkeypatch, fresh_engine):
    """探针通过 → 进入真实加载路径；模型目录缺失时按普通异常降级（可被 try/except 抓住）。

    ★ 必须 stub torch/chronos：否则在 venv(ABI 错配) 下真实 import torch 会原生崩溃，
      在基础解释器下 import torch 会 ModuleNotFoundError——两者都到不了"模型目录缺失"分支。
    """
    import types
    _torch_stub = types.ModuleType("torch")
    _torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    _torch_stub.bfloat16 = "bfloat16"
    _chronos_stub = types.ModuleType("chronos")
    _chronos_stub.Chronos2Pipeline = object
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "torch", _torch_stub)
    monkeypatch.setitem(_sys.modules, "chronos", _chronos_stub)

    monkeypatch.setattr(csh, "_run_probe_subprocess", _fake_run(0, _OK_STDOUT))
    monkeypatch.setattr(csh, "LOCAL_MODEL_DIR", "F:/__definitely_not_exists__/chronos-2")
    ok = fresh_engine._ensure_loaded()
    assert ok is False
    assert "模型目录" in (fresh_engine._load_error or "")


# ---------------------------------------------------------------- 回归守卫（自证有效）


@pytest.mark.unit
def test_regression_guard_probe_before_import_torch():
    """★ 反向用例：源码中探针调用必须出现在任何 `import torch` 之前。

    如果有人"顺手"把 import torch 挪回函数顶部，或删掉探针，本测试立刻炸。
    """
    src = inspect.getsource(cs.ChronosEngine._ensure_loaded)
    # ★ 2026-08-17：重构后 _ensure_loaded 调用 get_probe()（探针在 chronos_shared 内），
    #   旧契约查 probe_torch_usable 已失效。守卫本意 = "探针必须先于任何 torch 导入"，
    #   这里检查探针入口 get_probe 在模块中先于 import torch 的位置即可。
    probe_pos = src.find("get_probe")
    if probe_pos < 0:
        probe_pos = src.find("probe_torch_usable")
    assert probe_pos >= 0, "_ensure_loaded 必须调用探针（get_probe/probe_torch_usable）"

    m = re.search(r"^\s*import\s+torch\b", src, re.MULTILINE)
    if m:
        assert m.start() > probe_pos, "import torch 必须在子进程探针之后！"


@pytest.mark.unit
def test_regression_guard_no_bare_import_torch_at_module_level():
    """模块顶层绝不能 import torch（否则 import 该模块就炸整个进程）。"""
    with open(cs.__file__, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, ln in enumerate(lines, 1):
        if re.match(r"^(import\s+torch\b|from\s+torch\b)", ln):
            pytest.fail(f"chronos_service.py 第 {i} 行在模块顶层 import torch：{ln!r}")


@pytest.mark.unit
def test_regression_guard_status_has_no_dunder_import_torch():
    """status 属性不得再出现 __import__('torch') 这种绕过守卫的写法。"""
    src = inspect.getsource(cs.ChronosEngine)
    assert '__import__("torch")' not in src and "__import__('torch')" not in src


# ---------------------------------------------------------------- 真子进程（不 mock）


@pytest.mark.integration
def test_real_subprocess_nonzero_exit_is_detected():
    """真起一个子进程模拟崩溃退出码，验证 _run_probe_subprocess 与判定链路真实可用。"""
    rc, out, err = csh._run_probe_subprocess(
        sys.executable, timeout=30, _code="import sys; sys.exit(3221225477)"
    )
    # Windows 退出码为无符号 32 位；不同壳可能回传有符号值，两者都接受
    assert rc not in (0,), f"应为非零退出，实际 {rc}"


@pytest.mark.integration
def test_real_subprocess_ok_path():
    """真起子进程跑一段等价的 ok 探针代码，验证 stdout JSON 解析链路。"""
    code = "import json,sys; sys.stdout.write(json.dumps({'ok':True,'torch':'0.0.0-test','cuda':False}))"
    rc, out, err = csh._run_probe_subprocess(sys.executable, timeout=30, _code=code)
    assert rc == 0
    assert json.loads(out)["torch"] == "0.0.0-test"
