"""
万象AI — 人工紧急处置窗口（Phase 0 / V6）

╔══════════════════════════════════════════════════════════════════════╗
║ 这个模块存在的唯一理由：当 AI 判断失灵时，人必须能立刻把手伸进去。      ║
╚══════════════════════════════════════════════════════════════════════╝

设计铁律（改这个文件前请先读完）：

1. **零依赖**。本模块只 import 标准库 + loguru。
   不 import 数据库、不 import MT5、不 import AI。
   理由：紧急停止是最后一道防线，它不能建立在"其他组件还活着"的假设上。
   数据库锁死时、MT5 断连时、AI 超时时，人依然要能停下这台机器。

2. **文件是权威，内存是缓存**。状态落盘 `backend/emergency_state.json`。
   进程重启后停止状态依然生效——否则"崩溃自动重启"会把人工停止悄悄抹掉，
   这正是最危险的场景（系统刚崩过，重启后又自己开单）。

3. **写必原子**。临时文件 + os.replace，读方永远看不到半截文件。

4. **读失败不误停也不漏停**。读不出来时沿用最后一次已知内存态；
   内存态也没有才当 NORMAL。
   - 若"读失败=NORMAL"→ 文件损坏时停止失效（漏停，致命）
   - 若"读失败=HALT" → IO 抖动就停掉交易（误停，同样有害）
   沿用最后已知态是唯一不引入新故障模式的选择。

5. **多账号平权**。全局停 + 单账号停，两级独立。
   任何"停止"都不得写死账号数量或假设只有一个账号。

── 三档状态 ─────────────────────────────────────────────────────────
  NORMAL    正常运行
  HALT_NEW  只停开新仓；持仓管理照常（止损/止盈/追踪继续跑）
            → 最常用。"别再进场了，但已有的单该保护还得保护"
  HALT_ALL  停开新仓 + 停 AI 主动出场建议（视觉看护/反转全平/锁利等由 AI 驱动的
            主动平仓动作不再执行）；但【硬 SL / 硬 TP / 追踪止损等保护性兜底仍有效】。
            → 用于"AI 判断不可信，别让它主动砍仓"，但绝不能因此让持仓裸奔——
              保护性止损止盈是最后安全网（铁律6：只关水龙头，不抽走桶里的水）。
            ⚠ 2026-08-16 审计确认：当前实现中 HALT_ALL 与 HALT_NEW 在
              allow_auto_exit 上均放行（保护性平仓恒有效），差异主要体现在"AI 主动
              出场"的拦截强度；如需严格停 AI 主动平仓，请在 trade_executor 的
              _auto_exit_blocked 中对 HALT_ALL 增加区分逻辑。

生效级别 = max(全局档位, 该账号档位)，按严格程度取高者。
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

# ── 状态文件位置（backend/emergency_state.json）──
_BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATE_FILE = str(_BASE_DIR / "emergency_state.json")

# ── 档位定义与严格程度排序 ──
LEVEL_NORMAL = "NORMAL"
LEVEL_HALT_NEW = "HALT_NEW"
LEVEL_HALT_ALL = "HALT_ALL"

_LEVEL_RANK = {
    LEVEL_NORMAL: 0,
    LEVEL_HALT_NEW: 1,
    LEVEL_HALT_ALL: 2,
}
VALID_LEVELS = tuple(_LEVEL_RANK.keys())

SCOPE_GLOBAL = "global"

# 状态缓存 TTL：主循环高频调用，避免每次都 stat 磁盘。
# 0.5s 延迟对紧急停止完全可接受（主循环一轮 30~120s）。
_CACHE_TTL = 0.5

_lock = threading.RLock()
_cache: Optional[dict] = None       # 最后一次成功读到的状态（fail-safe 兜底源）
_cache_at: float = 0.0              # 缓存时间戳
_cache_mtime: float = -1.0          # 缓存对应的文件 mtime


def _empty_state() -> dict:
    return {
        "version": 1,
        "global": {
            "level": LEVEL_NORMAL,
            "reason": "",
            "at": "",
            "by": "",
        },
        "accounts": {},        # {account_id: {level, reason, at, by}}
        "flatten_requests": [],  # 一键全平留痕（最近 50 条）
    }


def _normalize(raw) -> dict:
    """把任意读入内容规整成合法状态结构（容忍手工编辑过的文件）。"""
    st = _empty_state()
    if not isinstance(raw, dict):
        return st

    g = raw.get("global")
    if isinstance(g, dict):
        lv = str(g.get("level", LEVEL_NORMAL)).upper()
        st["global"] = {
            "level": lv if lv in _LEVEL_RANK else LEVEL_NORMAL,
            "reason": str(g.get("reason", "") or ""),
            "at": str(g.get("at", "") or ""),
            "by": str(g.get("by", "") or ""),
        }

    accs = raw.get("accounts")
    if isinstance(accs, dict):
        for aid, info in accs.items():
            if not isinstance(info, dict):
                continue
            lv = str(info.get("level", LEVEL_NORMAL)).upper()
            if lv not in _LEVEL_RANK:
                lv = LEVEL_NORMAL
            # NORMAL 的账号条目没有保留价值，直接丢弃保持文件干净
            if lv == LEVEL_NORMAL:
                continue
            st["accounts"][str(aid)] = {
                "level": lv,
                "reason": str(info.get("reason", "") or ""),
                "at": str(info.get("at", "") or ""),
                "by": str(info.get("by", "") or ""),
            }

    reqs = raw.get("flatten_requests")
    if isinstance(reqs, list):
        st["flatten_requests"] = [r for r in reqs if isinstance(r, dict)][-50:]

    return st


def _read_from_disk() -> Optional[dict]:
    """从磁盘读状态。读不到/读坏返回 None（调用方走 fail-safe 兜底）。"""
    try:
        if not os.path.exists(STATE_FILE):
            return _empty_state()
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return _normalize(json.load(f))
    except Exception as e:
        logger.warning(f"[紧急处置] 状态文件读取失败，沿用上次已知状态: {e}")
        return None


def _write_to_disk(state: dict) -> bool:
    """原子写盘。返回是否成功。"""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        logger.error(f"[紧急处置] 状态写盘失败: {e}")
        return False


def get_state(force: bool = False) -> dict:
    """读取当前紧急处置状态（带 TTL 缓存 + mtime 失效）。

    force=True 跳过缓存强制读盘（API 查询/人工操作前用，保证看到最新）。
    """
    global _cache, _cache_at, _cache_mtime
    with _lock:
        now = time.time()
        if not force and _cache is not None and (now - _cache_at) < _CACHE_TTL:
            return json.loads(json.dumps(_cache))  # 深拷贝，防调用方改缓存

        # mtime 未变则复用缓存（避免重复解析 JSON）
        try:
            mtime = os.path.getmtime(STATE_FILE) if os.path.exists(STATE_FILE) else 0.0
        except Exception:
            mtime = -1.0

        if not force and _cache is not None and mtime == _cache_mtime and mtime >= 0:
            _cache_at = now
            return json.loads(json.dumps(_cache))

        fresh = _read_from_disk()
        if fresh is None:
            # ★ fail-safe：读失败 → 沿用最后已知态；从没读到过才给空态
            #   （此分支有反向验证用例守护，见 test_corrupted_file_falls_back_to_last_known_halt）
            if _cache is not None:
                _cache_at = now
                return json.loads(json.dumps(_cache))
            return _empty_state()

        _cache = fresh
        _cache_at = now
        _cache_mtime = mtime
        return json.loads(json.dumps(fresh))


def _save(state: dict) -> bool:
    """写盘并同步刷新缓存。"""
    global _cache, _cache_at, _cache_mtime
    with _lock:
        ok = _write_to_disk(state)
        if ok:
            _cache = json.loads(json.dumps(state))
            _cache_at = time.time()
            try:
                _cache_mtime = os.path.getmtime(STATE_FILE)
            except Exception:
                _cache_mtime = -1.0
        return ok


def effective_level(account_id: Optional[str] = None) -> str:
    """某账号实际生效的档位 = max(全局, 该账号)。"""
    st = get_state()
    lv = st["global"]["level"]
    if account_id:
        acc = st["accounts"].get(str(account_id))
        if acc and _LEVEL_RANK.get(acc["level"], 0) > _LEVEL_RANK.get(lv, 0):
            lv = acc["level"]
    return lv


def _active_reason(account_id: Optional[str], level: str) -> str:
    """拼出"是谁在什么时候因为什么停的"，供日志与前端展示。"""
    st = get_state()
    src = None
    if account_id:
        acc = st["accounts"].get(str(account_id))
        if acc and acc["level"] == level:
            src = ("账号级", acc)
    if src is None and st["global"]["level"] == level:
        src = ("全局", st["global"])
    if src is None:
        return level
    tag, info = src
    who = info.get("by") or "未知"
    at = info.get("at") or ""
    why = info.get("reason") or "未填写原因"
    return f"{tag}{level}（{who} 于 {at} 触发：{why}）"


def allow_open(account_id: Optional[str] = None) -> Tuple[bool, str]:
    """是否允许开新仓。返回 (允许, 拒绝原因)。

    HALT_NEW / HALT_ALL 都禁止开仓。
    """
    lv = effective_level(account_id)
    if lv == LEVEL_NORMAL:
        return True, ""
    return False, f"人工紧急停止生效 → {_active_reason(account_id, lv)}"


def allow_auto_exit(account_id: Optional[str] = None) -> Tuple[bool, str]:
    """是否允许"自动平仓/自动改单"等由 AI 驱动的持仓管理动作。

    铁律6：MANUAL_HALT 期间拒新开仓，但 SL/TP/SmartExit 等保护性自动平仓
    仍必须有效——否则持仓将无人守护而裸奔（比不停更危险）。
    即「只关水龙头，不抽走桶里的水」。

    ★ 2026-08-17 精确化两档语义（对齐测试契约 + 铁律）：
      - HALT_NEW（仅停开仓）：AI 自动平仓照常 → 放行（铁律6 原文语义）。
      - HALT_ALL（全停，人工判定系统失灵）：AI 驱动的平仓/改单一律冻结
        （close_position / modify_sl_tp 零指令）——全停 = 人工接管，持仓由
        MT5 **原生 SL/TP** 继续兜底（它们不依赖系统发指令，天然守护防裸奔）。
    """
    lv = effective_level(account_id)
    if lv == LEVEL_HALT_ALL:
        return False, f"人工全停(HALT_ALL)生效 → {_active_reason(account_id, lv)}（AI 自动平仓冻结，MT5 原生 SL/TP 仍兜底）"
    return True, ""


def halt(level: str, scope: str = SCOPE_GLOBAL, reason: str = "", by: str = "manual") -> dict:
    """触发紧急停止。

    scope = "global" 或具体 account_id。
    """
    level = str(level or "").upper()
    if level not in _LEVEL_RANK or level == LEVEL_NORMAL:
        raise ValueError(f"非法档位: {level}（可选 {LEVEL_HALT_NEW} / {LEVEL_HALT_ALL}）")

    with _lock:
        st = get_state(force=True)
        entry = {
            "level": level,
            "reason": str(reason or ""),
            "at": datetime.now().isoformat(timespec="seconds"),
            "by": str(by or "manual"),
        }
        if scope == SCOPE_GLOBAL:
            st["global"] = entry
        else:
            st["accounts"][str(scope)] = entry
        _save(st)

    logger.warning(
        f"[紧急处置] ★ 已触发 {level} | 范围={scope} | 操作人={by} | 原因={reason or '未填写'}"
    )
    return get_state(force=True)


def resume(scope: str = SCOPE_GLOBAL, by: str = "manual") -> dict:
    """解除停止，恢复正常。

    注意：解除全局不会自动解除账号级停止——那是两个独立开关，
    否则"我只想恢复其他账号，结果把出问题那个也放出去了"。
    """
    with _lock:
        st = get_state(force=True)
        if scope == SCOPE_GLOBAL:
            st["global"] = {
                "level": LEVEL_NORMAL,
                "reason": "",
                "at": datetime.now().isoformat(timespec="seconds"),
                "by": str(by or "manual"),
            }
        else:
            st["accounts"].pop(str(scope), None)
        _save(st)

    logger.warning(f"[紧急处置] 已解除停止 | 范围={scope} | 操作人={by}")
    return get_state(force=True)


def record_flatten(scope: str, reason: str, by: str, result: Optional[dict] = None) -> str:
    """记录一次一键全平操作（留痕用，不执行实际平仓）。

    实际平仓由调用方（API 层）执行——本模块零依赖，碰不到 MT5。
    """
    req_id = uuid.uuid4().hex[:12]
    with _lock:
        st = get_state(force=True)
        st["flatten_requests"].append({
            "id": req_id,
            "scope": str(scope),
            "reason": str(reason or ""),
            "by": str(by or "manual"),
            "at": datetime.now().isoformat(timespec="seconds"),
            "result": result or {},
        })
        st["flatten_requests"] = st["flatten_requests"][-50:]
        _save(st)
    return req_id


def get_flatten_history(limit: int = 20) -> list:
    st = get_state()
    return list(reversed(st["flatten_requests"][-int(limit):]))


def summary() -> dict:
    """给前端/健康检查用的紧凑摘要。"""
    st = get_state()
    halted_accounts = {
        aid: info["level"] for aid, info in st["accounts"].items()
    }
    return {
        "global_level": st["global"]["level"],
        "global_reason": st["global"]["reason"],
        "global_at": st["global"]["at"],
        "global_by": st["global"]["by"],
        "halted_accounts": halted_accounts,
        "any_halt": st["global"]["level"] != LEVEL_NORMAL or bool(halted_accounts),
        "state_file": STATE_FILE,
        "recent_flatten": get_flatten_history(5),
    }
