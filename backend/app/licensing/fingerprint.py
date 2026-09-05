"""
机器指纹 — 把「这台机器」变成一串可比对的哈希（V6 Phase 8.1）

═══ 为什么不是一个指纹，而是三个 ═══

商业授权要绑机器，但「机器」这个概念在现实里是会漂移的：
客户换一张网卡、主板刷个 BIOS、CPU 打个微码补丁，硬件标识就变了。
如果只取单一标识做绑定，一次很正常的硬件维护就会把付费客户锁在门外——
凌晨三点收到「我什么都没动，系统说我没授权」的投诉，是最糟糕的商业事故。

所以采集三个互相独立的要素：
    board  主板 UUID   —— 最稳，换主板 ≈ 换电脑，正是我们想识别的边界
    cpu    CPU ID      —— 次稳，同型号批量机器可能重复，故不能单独用
    mac    物理网卡 MAC —— 最易变（换网卡/加网卡/虚拟网卡干扰）

判定规则：**三取二**。任意两个要素对得上，就认定还是同一台机器。
    · 换网卡  → board+cpu 命中 → 放行（正常维护，不该打扰客户）
    · 换主板  → 只剩 cpu 命中 → 拒绝（这确实是另一台机器了）
    · 整机复制→ 三个全不中 → 拒绝（这正是要防的盗用场景）

═══ 三条硬约束 ═══

1) **采集失败不能把系统搞挂**。取不到就是取不到，缺失要素记为空串，
   由上层按「命中数 < 2」处理。绝不让一次 WMI 抽风导致客户无法启动。
2) **必须有超时**。WMI/PowerShell 在个别机器上会挂死几十秒，
   授权校验卡住 = 交易线程卡住，这是不可接受的。统一 8 秒硬超时。
3) **必须缓存**。心跳每分钟都要指纹，但硬件不会每分钟变。
   进程内缓存一次采集结果，全生命周期复用。

═══ 隐私 ═══
对外只出哈希，不出原始 UUID/MAC。原始值连日志都不打（日志会被客户看到、
会被发给我们排障，里面不该有客户机器的裸标识）。
"""
from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

# 三要素的固定顺序（复合指纹拼接时必须稳定，否则同一台机器算出两个值）
FACTOR_KEYS = ("board", "cpu", "mac")

# 三取二
MATCH_THRESHOLD = 2

# 外部命令硬超时（秒）。见模块说明约束 2。
_PROBE_TIMEOUT = 8.0

# 哈希盐。带上版本号，将来若要换算法可以平滑并存。
_SALT = "wxai-fp-v1"

# ── 无效值黑名单 ──────────────────────────────────────────────
# 大量 OEM 主板出厂没烧 UUID，WMI 会返回这些占位符。
# 如果不过滤，成千上万台机器会算出**同一个指纹**，绑定形同虚设。
_INVALID_PATTERNS = (
    "00000000-0000-0000-0000-000000000000",
    "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
    "TO BE FILLED BY O.E.M.",
    "SYSTEM SERIAL NUMBER",
    "DEFAULT STRING",
    "NOT SPECIFIED",
    "NONE",
    "UNKNOWN",
)

_cache: Optional[Dict[str, str]] = None
_cache_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════
#  底层：安全地跑一条外部命令
# ══════════════════════════════════════════════════════════════
def _run(cmd: list[str]) -> str:
    """跑命令拿 stdout。任何异常一律返回空串——采集失败不是错误，是常态之一。"""
    try:
        kwargs = {
            "capture_output": True,
            "timeout": _PROBE_TIMEOUT,
            "text": True,
            # Windows 中文系统 stdout 是 GBK，遇到非法字节直接替换，
            # 绝不能让一个 UnicodeDecodeError 把授权校验炸掉。
            "errors": "replace",
        }
        if platform.system() == "Windows":
            # 不弹黑窗（打包成桌面应用后，每次心跳闪一个控制台窗口是灾难级体验）
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]
        return (proc.stdout or "").strip()
    except Exception as e:
        logger.debug(f"[指纹] 命令执行失败 {cmd[0]}: {type(e).__name__}")
        return ""


def _normalize(raw: str) -> str:
    """规范化原始标识：去空白、大写、剔除无效占位符。"""
    if not raw:
        return ""
    val = re.sub(r"\s+", "", raw).upper()
    if len(val) < 4:
        return ""
    for bad in _INVALID_PATTERNS:
        if val == bad.replace(" ", ""):
            return ""
    return val


# ══════════════════════════════════════════════════════════════
#  三要素采集
# ══════════════════════════════════════════════════════════════
def _probe_windows() -> Dict[str, str]:
    """
    Windows 采集。

    为什么用 PowerShell CIM 而不是 wmic：wmic 自 Win10 21H1 起标记弃用，
    Win11 24H2 已默认不安装。但老机器上 PowerShell 可能被策略禁用，
    所以两条路都留，谁先出结果用谁。

    为什么三个值合到一次 PowerShell 调用：PowerShell 冷启动约 0.5~1s，
    调三次就是 3 秒。授权校验在开仓路径上，这个开销必须压掉。
    """
    out = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        # 用制表符分隔单行输出，避免 JSON 序列化在老版 PS 上的兼容问题
        "$b=(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID;"
        "$c=(Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1).ProcessorId;"
        "Write-Output \"$b`t$c\"",
    ])
    board = cpu = ""
    if out:
        parts = out.splitlines()[0].split("\t")
        if len(parts) >= 2:
            board, cpu = _normalize(parts[0]), _normalize(parts[1])

    # PowerShell 拿不到 → 回退 wmic（老系统还在的话）
    if not board:
        board = _normalize(_tail_value(_run(["wmic", "csproduct", "get", "uuid"])))
    if not cpu:
        cpu = _normalize(_tail_value(_run(["wmic", "cpu", "get", "processorid"])))

    return {"board": board, "cpu": cpu}


def _tail_value(wmic_out: str) -> str:
    """wmic 输出是「表头 + 值」两行，取最后一个非空行。"""
    lines = [ln.strip() for ln in wmic_out.splitlines() if ln.strip()]
    return lines[-1] if len(lines) >= 2 else ""


def _probe_linux() -> Dict[str, str]:
    """Linux 采集。客户主力是 Windows，这条路是为将来的容器化部署留的。"""
    board = ""
    for p in ("/sys/class/dmi/id/product_uuid", "/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            board = _normalize(Path(p).read_text().strip())
            if board:
                break
        except Exception:
            continue

    cpu = ""
    try:
        txt = Path("/proc/cpuinfo").read_text()
        # 没有稳定的 CPU 序列号时，用「型号+核心数」做弱标识
        model = re.search(r"model name\s*:\s*(.+)", txt)
        cores = txt.count("processor\t:")
        if model:
            cpu = _normalize(f"{model.group(1)}x{cores}")
    except Exception:
        pass

    return {"board": board, "cpu": cpu}


def _probe_mac_address() -> str:
    """
    物理网卡 MAC。

    刻意不用 `uuid.getnode()`：它在拿不到真实 MAC 时会**返回一个随机数**，
    而且不告诉你。那样每次重启指纹都变，客户天天掉授权。

    选取规则：排除回环/虚拟网卡（VMware/VirtualBox/Hyper-V/docker/蓝牙），
    剩下的按接口名排序取第一个 —— 排序是为了让多网卡机器每次取到同一张。

    2026-08-09 根因修复：项目铁律已禁用 psutil（本机 psutil.process_iter 永久挂死）。
    改为通过 PowerShell Get-NetAdapter 获取物理网卡 MAC，与 psutil 解耦。
    """
    virtual_hints = (
        "vmware", "virtualbox", "vethernet", "hyper-v", "loopback", "docker",
        "bluetooth", "vpn", "tap", "tun", "wsl", "npcap", "teredo", "isatap",
    )
    candidates = []
    try:
        import subprocess
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-NetAdapter | Where-Object { $_.HardwareInterface -eq $true -and $_.Status -eq 'Up' } "
                "| Select-Object Name, MacAddress | ConvertTo-Csv -NoTypeInformation",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            for line in lines[2:]:  # 跳过 CSV 头两行（标题+空行）
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                name = parts[0].strip().strip('"')
                mac = parts[1].strip().strip('"')
                low = name.lower()
                if any(h in low for h in virtual_hints):
                    continue
                if re.fullmatch(r"([0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}", mac):
                    if mac.replace("-", "").upper() in ("000000000000", "FFFFFFFFFFFF"):
                        continue
                    candidates.append((name, mac.replace("-", ":")))
    except Exception as e:
        logger.debug(f"[指纹] 网卡枚举失败: {type(e).__name__}: {e}")

    # 兜底：WMIC 枚举网卡（兼容旧系统）
    if not candidates:
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "nic", "where", "NetConnectionStatus=2", "get", "Name,MACAddress"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            lines = out.splitlines()
            if len(lines) >= 2:
                header = lines[0]
                mac_idx = header.find("MACAddress")
                name_idx = header.find("Name")
                if mac_idx >= 0 and name_idx >= 0:
                    for line in lines[1:]:
                        if len(line) <= max(mac_idx, name_idx):
                            continue
                        mac = line[mac_idx:mac_idx + 17].strip()
                        name = line[name_idx:].strip()
                        low = name.lower()
                        if any(h in low for h in virtual_hints):
                            continue
                        if re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
                            if mac.replace(":", "").upper() in ("000000000000", "FFFFFFFFFFFF"):
                                continue
                            candidates.append((name, mac))
        except Exception as e:
            logger.debug(f"[指纹] WMIC 兜底枚举失败: {type(e).__name__}: {e}")

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0])
    return _normalize(candidates[0][1])


# ══════════════════════════════════════════════════════════════
#  对外 API
# ══════════════════════════════════════════════════════════════
def _hash_factor(name: str, raw: str) -> str:
    """单要素哈希。带要素名入盐，防止 board 和 cpu 恰好同值时产生跨要素碰撞。"""
    if not raw:
        return ""
    return hashlib.sha256(f"{_SALT}:{name}:{raw}".encode()).hexdigest()


def collect_factors(force: bool = False) -> Dict[str, str]:
    """
    采集三要素并逐个哈希，返回 {"board": <sha256>, "cpu": ..., "mac": ...}。
    取不到的要素值为空串（不是缺 key —— 上层按固定三个 key 处理更省心）。

    结果进程内缓存：硬件不会在运行期变，而心跳每分钟都要用。
    """
    global _cache
    if _cache is not None and not force:
        return dict(_cache)

    with _cache_lock:
        if _cache is not None and not force:
            return dict(_cache)

        system = platform.system()
        raw: Dict[str, str] = {"board": "", "cpu": "", "mac": ""}
        try:
            if system == "Windows":
                raw.update(_probe_windows())
            else:
                raw.update(_probe_linux())
            raw["mac"] = _probe_mac_address()
        except Exception as e:
            # 整体兜底：见模块说明约束 1
            logger.warning(f"[指纹] 采集异常，按缺失处理: {type(e).__name__}: {e}")

        factors = {k: _hash_factor(k, raw.get(k, "")) for k in FACTOR_KEYS}
        got = [k for k in FACTOR_KEYS if factors[k]]
        # 只打命中了哪些要素，绝不打原始值（隐私）
        logger.info(f"[指纹] 采集完成，命中要素 {len(got)}/3: {got}")
        _cache = factors
        return dict(factors)


def compute_fingerprint(factors: Optional[Dict[str, str]] = None) -> str:
    """
    复合指纹：三要素哈希按固定顺序拼接后再哈希，取前 32 位十六进制。

    这个短串只用于展示/日志（客户报障时说「我的机器码是 a1b2...」），
    **比对一律用 factors 三取二**，不能用它 —— 它是全等比较，换张网卡就变了。
    """
    f = factors if factors is not None else collect_factors()
    joined = "|".join(f.get(k, "") for k in FACTOR_KEYS)
    return hashlib.sha256(f"{_SALT}:composite:{joined}".encode()).hexdigest()[:32]


def match_factors(
    current: Dict[str, str],
    bound: Dict[str, str],
    threshold: int = MATCH_THRESHOLD,
) -> tuple[bool, int]:
    """
    三取二比对。返回 (是否同一台机器, 命中数)。

    注意空串不算命中 —— 否则两台都取不到 CPU ID 的机器会因为「都是空」
    而互相认亲，绑定直接失效。这是本函数最容易写错的一行。
    """
    if not bound:
        return False, 0
    hits = 0
    for k in FACTOR_KEYS:
        a, b = (current.get(k) or ""), (bound.get(k) or "")
        if a and b and a == b:
            hits += 1
    return hits >= threshold, hits


def describe() -> Dict[str, object]:
    """给诊断接口用的摘要（不含任何原始硬件标识）。"""
    f = collect_factors()
    return {
        "fingerprint": compute_fingerprint(f),
        "factors_present": {k: bool(f.get(k)) for k in FACTOR_KEYS},
        "factors_count": sum(1 for k in FACTOR_KEYS if f.get(k)),
        "platform": platform.system(),
    }
