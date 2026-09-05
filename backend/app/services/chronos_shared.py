# -*- coding: utf-8 -*-
"""Chronos-2 本地时序模型「共享加载器」（中立项 · 进程内唯一实例）

══════════════════════════════════════════════════════════════════════
★ 2026-08-12 Chronos 双实例合并（用户指令：5 个实例够了就把 Chronos 合并）

  合并前：
    ① 决策链 app.services.chronos_service.ChronosEngine 加载一份 Chronos-2（GPU）；
    ② 参考面板 app.services.ts_reference_models.ChronosP 又加载一份 Chronos-2（CPU）。
    同一进程内两份相同权重，双倍内存/显存，且两处维护逻辑易漂移。

  合并后：
    本模块提供「进程内唯一」的 Chronos-2 pipeline 单例（CPU / float32），
    chronos_service 与 ts_reference_models.ChronosP 共用，彻底消除双实例。

★ 架构红线兼容（关键约束）：
    ts_reference_models 被禁止 import 任何决策链模块（含 app.services.chronos_service，
    见 ts_reference_models.py 红线声明）。本模块是「中立项」——既非决策链、也非参考面板，
    因此 ts_reference_models import 本模块完全合规，红线守卫测试不受影响。

★ 安全隔离（沿用 chronos_service 的根治方案）：
    主进程**永不裸 import torch**。先起子进程探针把「进程级原生段错误」转化为
    「可捕获的子进程退出码」，探针通过后才在主进程加载模型。详见下方探针段。

★ 设备决策（用户拍板：除 Qwen3-8B 用 GPU，其余 4 个本地时序模型全跑 CPU）：
    本共享实例**强制 CPU + float32**，不抢 RTX 3060 Ti 8GB 给 Qwen3-8B 用。
══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import json
import hashlib
import importlib.util
import subprocess
import threading
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("chronos_shared")

# ── 模型目录（与合并前 chronos_service 的解析逻辑完全一致）─────────────
LOCAL_MODEL_DIR = os.environ.get(
    "CHRONOS2_MODEL_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                 "models", "chronos-2"),
)


# ===================================================================
# torch 子进程探针（崩溃隔离层）— 由 chronos_service 迁入，逻辑不变
# ===================================================================
# 设计要点：
#   ① 主进程绝不裸 import torch —— 段错误抓不住，只能靠进程边界隔离；
#   ② 探针只探 `import torch` + `from chronos import Chronos2Pipeline` + cuda 可用性，
#      不加载模型权重（那是几秒 + 显存，且模型加载失败是普通异常，能被 try 抓住）；
#   ③ 结果按指纹缓存：进程内 dict + 磁盘 JSON，冷启动不重复 fork；
#   ④ 只缓存**确定性结论**（正常退出 / 明确崩溃）；超时不落盘（机器临时慢不该被永久判死）。

_PROBE_TIMEOUT = float(os.environ.get("CHRONOS_PROBE_TIMEOUT", "120"))
_PROBE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "torch_probe_cache.json",
)
_PROBE_MEM_CACHE: dict = {}
_PROBE_LOCK = threading.Lock()

# 探针子进程执行的代码：成功则往 stdout 吐一行 JSON；失败则由退出码/stderr 说话
_PROBE_CODE = (
    "import json,sys\n"
    "import torch\n"
    "from chronos import Chronos2Pipeline\n"
    "try:\n"
    "    cuda = bool(torch.cuda.is_available())\n"
    "except Exception:\n"
    "    cuda = False\n"
    "sys.stdout.write(json.dumps({'ok':True,'torch':torch.__version__,'cuda':cuda}))\n"
)


def _torch_fingerprint() -> str:
    """不 import torch，纯路径推断出「解释器 + torch 二进制」指纹。

    指纹必须包含解释器完整版本串：本次事故正是 3.13.12 就地升级到 3.13.14
    导致 ABI 错位，若只按目录名（未变）判断会命中过期的"可用"缓存。
    """
    parts = [sys.executable or "", sys.version]
    try:
        spec = importlib.util.find_spec("torch")
        origin = getattr(spec, "origin", None) if spec else None
        if origin:
            pkg_dir = os.path.dirname(origin)
            parts.append(origin)
            for cand in ("version.py", "_C.cp313-win_amd64.pyd", "_C.pyd"):
                p = os.path.join(pkg_dir, cand)
                if os.path.exists(p):
                    st = os.stat(p)
                    parts.append(f"{cand}:{st.st_size}:{int(st.m_mtime)}")
    except Exception:  # noqa: BLE001 —— 指纹失败不该影响主流程，退化为仅解释器指纹
        parts.append("fingerprint-partial")
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


def _is_msys_env() -> bool:
    """判断当前进程是否跑在 Git Bash / MSYS2 / Cygwin 环境里。

    同一个解释器、同一个 torch，在 Git Bash 下 import torch 必定 0xC0000005 段错误，
    在原生 cmd / PowerShell 下完全正常。MSYS 环境下崩溃结论一律不写盘，只当本次降级。
    """
    if os.environ.get("MSYSTEM"):          # Git Bash: MINGW64 / MSYS
        return True
    if "cygwin" in (os.environ.get("OSTYPE") or "").lower():
        return True
    shell = (os.environ.get("SHELL") or "").replace("\\", "/").lower()
    return shell.endswith("/bash") or shell.endswith("/sh")


def _load_probe_disk_cache() -> dict:
    try:
        with open(_PROBE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 缓存损坏/不存在一律当空，静默重探
        return {}


def _save_probe_disk_cache(fingerprint: str, result: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PROBE_CACHE_PATH), exist_ok=True)
        data = _load_probe_disk_cache()
        data[fingerprint] = result
        tmp = _PROBE_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PROBE_CACHE_PATH)
    except Exception as e:  # noqa: BLE001 —— 写缓存失败只是慢一点，绝不影响交易
        logger.debug(f"[chronos_shared] 探针缓存写入失败（忽略）: {e}")


def _run_probe_subprocess(python_exe: str, timeout: float, _code: str = None):
    """真正起子进程。抽成独立函数是为了可测试（测试里替换掉即可）。

    返回 (returncode, stdout, stderr)。超时向上抛 subprocess.TimeoutExpired。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [python_exe, "-c", _code if _code is not None else _PROBE_CODE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def probe_torch_usable(python_exe: str = None, timeout: float = None, use_cache: bool = True) -> dict:
    """在子进程中探测 torch/chronos 是否可安全导入。

    返回 {"ok": bool, "reason": str, "torch_version": str|None,
          "cuda": bool|None, "returncode": int|None}
    """
    python_exe = python_exe or sys.executable
    timeout = _PROBE_TIMEOUT if timeout is None else timeout
    fp = _torch_fingerprint()

    with _PROBE_LOCK:
        if use_cache:
            if fp in _PROBE_MEM_CACHE:
                return _PROBE_MEM_CACHE[fp]
            disk = _load_probe_disk_cache().get(fp)
            if isinstance(disk, dict) and "ok" in disk:
                _PROBE_MEM_CACHE[fp] = disk
                logger.info(f"[chronos_shared] torch 探针命中缓存 → ok={disk.get('ok')} ({disk.get('reason','')})")
                return disk

        cacheable = True
        try:
            rc, out, err = _run_probe_subprocess(python_exe, timeout)
        except subprocess.TimeoutExpired:
            rc, out, err = None, "", ""
            result = {
                "ok": False,
                "reason": f"torch 探针子进程超时（>{timeout:.0f}s），本次降级",
                "torch_version": None, "cuda": None, "returncode": None,
            }
            cacheable = False   # 超时可能只是机器临时慢，不写盘
        except Exception as e:  # noqa: BLE001 —— 起进程本身失败（解释器路径错等）
            result = {
                "ok": False, "reason": f"torch 探针无法启动: {e}",
                "torch_version": None, "cuda": None, "returncode": None,
            }
            cacheable = False
        else:
            if rc == 0:
                try:
                    payload = json.loads((out or "").strip().splitlines()[-1])
                except Exception:  # noqa: BLE001
                    payload = {}
                result = {
                    "ok": True, "reason": "ok",
                    "torch_version": payload.get("torch"),
                    "cuda": payload.get("cuda"),
                    "returncode": 0,
                }
            else:
                native = {
                    3221225477: "0xC0000005 ACCESS_VIOLATION",
                    -1073741819: "0xC0000005 ACCESS_VIOLATION",
                    3221225725: "0xC00000FD STACK_OVERFLOW",
                    3221226505: "0xC0000409 STACK_BUFFER_OVERRUN",
                }.get(rc)
                tail = (err or "").strip().splitlines()
                tail_txt = tail[-1] if tail else ""
                if native:
                    reason = f"torch 探针子进程崩溃 rc={rc} {native}"
                    if _is_msys_env():
                        reason += (
                            "；★检测到 Git Bash/MSYS 环境——该环境下 import torch"
                            " 必崩而原生 cmd/PowerShell 正常，本次结论**不写盘**，"
                            "请改用 start_all.bat 或 PowerShell 启动后端"
                        )
                        cacheable = False
                    else:
                        reason += "（原生终端下崩溃，才需怀疑 torch 与 Python ABI 不匹配）"
                else:
                    reason = f"torch 探针子进程失败 rc={rc}: {tail_txt or '(无 stderr)'}"
                result = {
                    "ok": False, "reason": reason,
                    "torch_version": None, "cuda": None, "returncode": rc,
                }

        _PROBE_MEM_CACHE[fp] = result
        if use_cache and cacheable:
            _save_probe_disk_cache(fp, result)
        lvl = logger.info if result["ok"] else logger.warning
        lvl(f"[chronos_shared] torch 子进程探针 → ok={result['ok']} {result['reason']}")
        return result


# ===================================================================
# 共享 pipeline 单例（进程内唯一 · CPU / float32）
# ===================================================================
_PIPE = None
_PIPE_LOADED = False
_PIPE_LOCK = threading.Lock()
_PROBE_RESULT = None
_LOAD_ERROR: str | None = None   # ★ 2026-08-17 记录真实降级原因（探针失败/模型目录缺失/加载异常），供调用方透出


def get_load_error() -> str | None:
    """返回最近一次 pipeline 加载失败的真实原因（供 chronos_service 透出到 status/_load_error）。"""
    return _LOAD_ERROR


def get_probe() -> dict:
    """返回最近一次 torch 探针结果（惰性触发一次）。供调用方 status/前端展示真实降级原因。"""
    global _PROBE_RESULT
    if _PROBE_RESULT is None:
        _PROBE_RESULT = probe_torch_usable()
    return _PROBE_RESULT


def _load_pipeline() -> object | None:
    """加载进程内唯一 Chronos-2 pipeline（CPU / float32）。

    必须在探针通过后才会 import torch；失败返回 None，调用方自动降级。
    """
    global _PIPE, _PIPE_LOADED
    if _PIPE_LOADED:
        return _PIPE
    _PIPE_LOADED = True   # 无论成败只探/加载一次：降级是永久的，不重复 fork

    probe = get_probe()
    if not probe.get("ok"):
        reason = (
            f"torch 探针失败 → 永久降级（决策回退 SMC/Regime）: {probe.get('reason')}"
        )
        global _LOAD_ERROR
        _LOAD_ERROR = reason
        logger.warning(f"[chronos_shared] {reason}")
        return None

    try:
        import torch
        from chronos import Chronos2Pipeline

        d = LOCAL_MODEL_DIR
        if not (os.path.isdir(d) and os.path.exists(os.path.join(d, "config.json"))):
            reason = f"模型目录/配置缺失: {d} → 降级（仅用 SMC/Regime）"
            _LOAD_ERROR = reason
            logger.warning(f"[chronos_shared] {reason}")
            return None

        # ★ 用户决策：Chronos-2 与另外 3 个 CPU 时序模型同跑 CPU，仅 Qwen3-8B 用 GPU。
        #   实测 float32 在 CPU 上稳定（bf16 在 CPU 精度异常），不抢 RTX 3060 Ti 8GB。
        pipe = Chronos2Pipeline.from_pretrained(
            d, device_map="cpu", torch_dtype=torch.float32,
        )
        if hasattr(pipe, "model"):
            pipe.model = pipe.model.to(device="cpu", dtype=torch.float32)
            if hasattr(pipe.model, "config"):
                pipe.model.config.torch_dtype = torch.float32
        _PIPE = pipe
        logger.info(f"[chronos_shared] Chronos-2 共享实例加载成功 → {d} (device=CPU, 类型=Chronos-2)")
        return pipe
    except Exception as e:  # noqa: BLE001
        reason = f"加载失败 → 降级: {e}"
        _LOAD_ERROR = reason
        logger.warning(f"[chronos_shared] {reason}")
        return None


def get_chronos2_pipeline() -> object | None:
    """返回进程内唯一的 Chronos-2 pipeline（CPU / float32）；不可用返回 None。"""
    with _PIPE_LOCK:
        return _load_pipeline()


def predict_quantiles_safe(inputs, prediction_length, quantile_levels):
    """线程安全的分位数预测封装。

    返回 (quantiles, mean)（与 Chronos2Pipeline.predict_quantiles 原生签名一致），
    推理时持锁，避免「决策链线程」与「参考面板刷新线程」并发访问同一 pipeline。
    模型不可用（None）时返回 None。
    """
    pipe = get_chronos2_pipeline()
    if pipe is None:
        return None

    def _run():
        import torch
        with torch.no_grad():
            return pipe.predict_quantiles(
                inputs=inputs,
                prediction_length=prediction_length,
                quantile_levels=quantile_levels,
            )

    with _PIPE_LOCK:
        return _run()
