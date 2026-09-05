"""
XAU/USD万象Ai自动量化交易系统 — 交易执行器
整合 AI决策 + 风控审核 + MT5下单 → 完整闭环
"""
import time
import re
import json
import threading
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from loguru import logger

# 进程级共享状态总线：提供原子占坑(claim/release)，根除"先查再写"式跨线程竞态。
# 该模块不依赖 app 内任何东西，不会引入循环导入。
from app.services import signal_bus
from app.services.local_llm_service import get_local_llm

# Phase 4 决策溯源：DebateDecision → 落库快照的**唯一**序列化点。
# 纯函数模块（对外只惰性依赖 platform_health_monitor），不会引入循环导入。
from app.services.decision_snapshot import (
    build_decision_snapshot,
    current_degrade_level,
    flat_columns,
    snapshot_to_json,
)

# ── 智能平仓进程级状态（auto_loop 单进程单线程，所有账号共享）──
# 反转防抖：记录每账号每持仓类型的"上轮反转意图"(方向, 时间戳)，连续 N 轮同向才全平
# ★ 审计修复(2026-08-05)：进程每1-2分钟重启一次→内存dict丢失→L2永远无法确认。
#   改用文件持久化(_REVERSAL_FILE)，重启后自动恢复状态，真正实现"连续N轮确认"。
_REVERSAL_STATE: dict = {}
# ★ 2026-08-14 视觉持仓看护：每账号(进程) 只初始化一次 provider + 后台生产者线程
_VE_INITED: set = set()
# ★ 可移植性(2026-08-08)：原兜底写死 "F:/WanxiangAI/data"。客户机若没设 DATA_DIR，
#   这个文件会落到一个不存在的盘符 → 反转状态每轮都读不回来 →
#   "连续 N 轮确认"退化成"永远确认不了"，L2 反转平仓静默失效（不报错，最难查）。
import sys as _sys
from pathlib import Path as _Path
_BACKEND_DIR = _Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in _sys.path:
    _sys.path.insert(0, str(_BACKEND_DIR))
from runtime_paths import data_path as _data_path  # noqa: E402

_REVERSAL_FILE = _data_path(".reversal_state.json")

def _load_reversal_state():
    """从文件恢复反转防抖状态（启动时调用）"""
    global _REVERSAL_STATE
    try:
        if os.path.exists(_REVERSAL_FILE):
            with open(_REVERSAL_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 过滤过期条目(>180s)
            now = time.time()
            cleaned = {}
            for acc, types_dict in data.items():
                cleaned[acc] = {}
                for pos_type, hist in types_dict.items():
                    fresh = [h for h in hist if (now - h[1]) < 180]
                    if fresh:
                        cleaned[acc][pos_type] = fresh
            _REVERSAL_STATE = cleaned
            if any(len(v) > 0 for v in cleaned.values()):
                logger.info(f"[反转防抖] 从文件恢复状态: {sum(len(h) for d in cleaned.values() for h in d.values())}条有效记录")
    except Exception as e:
        logger.debug(f"[反转防抖] 状态文件读取跳过: {e}")
        _REVERSAL_STATE = {}

def _save_reversal_state():
    """持久化反转防抖状态到文件"""
    try:
        tmp = _REVERSAL_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_REVERSAL_STATE, f)
        os.replace(tmp, _REVERSAL_FILE)  # 原子替换
    except Exception as e:
        logger.debug(f"[反转防抖] 状态保存失败: {e}")

# 启动时恢复状态
_load_reversal_state()

# L3 篮子护盾冷却：避免同 cycle 内重复全平（平掉后持仓空，下一轮自然归零，双保险）
_L3_LAST_LOCK: dict = {}
# ★★ 2026-08-17 篮子级 AI 持仓管理（用户铁律：开完仓核心任务=维护持仓）
#   · _BASKET_PEAK_PNL：篮子浮盈峰值跟踪（回吐保护用），按 account_id
#   · _BASKET_EXEC_COOLDOWN：篮子处置执行冷却（close_all/trim 后 120s 内不重复）
_BASKET_PEAK_PNL: dict = {}
_BASKET_EXEC_COOLDOWN: dict = {}
# ★ 2026-08-15 per-position 追踪止损（2s 级，仅主号/独立号）：ATR 缓存 + 峰值利润缓存
#   ATR 每 30s 刷新一次（避免每 2s 调 get_market_snapshot 过重）；峰值利润跨 2s 循环累计做峰值追踪。
_LEADER_TRAIL_ATR: dict = {}            # account_id -> (ts:float, atr:float)
_LEADER_TRAIL_PEAK: dict = {}           # (account_id, ticket) -> 峰值浮盈点数（有利方向）
# ★ M2：Reflexion 捕捉率反思限频——每 20 笔主号平仓最多生成 1 条教训（ATLAS 噪声约束：过度反思致模型瘫痪）
_M2_TRADE_SINCE_LESSON = 0
# ★ 同方向开仓间隔冷却：防止强势趋势中每轮都开新单导致持仓无限滚动
_LAST_OPEN_TS: dict = {}  # key=f"{account_id}:{direction}" -> 时间戳
_OPEN_INTERVAL_LOCK = threading.Lock()


# ════════════════════ 信号塔：主号出场动作广播总线 ════════════════════
# 仅主号(_is_leader=True)在 _manage_positions 内写入平仓/移损动作；
# 跟号读取并镜像 → 副号平仓严格跟随主号，且跟号零 AI 调用、零独立规则。
# key = f"{leader_account_id}:{leader_ticket}"；value = 本轮主号对该持仓的动作列表。
_LEADER_EXIT_BUS: dict = {}
_BUS_LOCK = threading.Lock()
_BUS_TTL = 180  # 总线动作存活秒数：覆盖最大循环间隔(~120s)，过期动作不镜像（已平持仓不会重现）
# 条目回收阈值：必须 >= _BUS_TTL，否则未过期动作会被 GC 误删。
# ★ 2026-08-07：过期判定已改为【动作级】，本阈值仅用于回收整条空条目，不参与可见性判断。
_BUS_GC_TTL = _BUS_TTL * 2

# 全局动作序号：每条广播动作带唯一 id，供跟号做"已镜像"幂等去重
# （跟号既有『主周期镜像』又有『实时守护线程镜像』，必须去重，否则一笔会平两次）。
_BUS_SEQ = 0
_BUS_SEQ_LOCK = threading.Lock()
# 跟号已镜像的动作去重表（2026-08-06 修复死循环）：
#   key=f"{follower_id}:{leader_ticket}:{action_type}" -> 时间戳；TTL 后自动清理
#   ★ 必须用(票号+动作类型)做幂等，不能用递增 action_id（每次 publish 都生成新 id 导致死循环）
#   ★ 2026-08-07 Phase 2：本表已收编进 SignalBus（app/services/signal_bus.py）。
#     动机与 _LAST_COPIED_SIGNAL 完全相同——"先 _is_mirrored 查、再 close_position、
#     最后 _mark_mirrored 标"是跨线程 TOCTOU。而 consume_leader_exit() 名字虽叫
#     "消费"，实现却是非破坏性读取（return list(acts)，不弹出），
#     主循环与 10s 跟单守护线程会各自拿到同一批动作，谁也拦不住谁。
#     partial_close 因此可能被执行两次：主号只要求平 50%，跟号 1.00 手被平光
#     ⇒ 本该奔跑的利润腿没了，跟号与主号仓位彻底脱钩。
#     现改用 _claim_mirror() 原子占坑。下方两个别名保留给存量代码/测试。
_MIRRORED_REG = signal_bus.MIRRORED
_MIRRORED: dict = _MIRRORED_REG.data
_MIRRORED_LOCK = _MIRRORED_REG.lock
_MIRRORED_TTL_SECONDS = 600

# ── AI 活动流保留窗口（根治 ai_activities 无界增长，task #294 教训）──
# 活动流仅用于「AI 工作剧场」实时展示，无需永久保留。全局保留最新 N 条，
# 超出部分按 created_at 清理，避免长跑 demo 撑爆表 / 拖慢查询。
# 5000 条 ≈ 4 账号数天活动量，足够 UI 滚动展示。
MAX_AI_ACTIVITIES = 5000
_AI_ACT_PRUNE_EVERY = 200          # 每写入 200 条触发一次清理（分摊开销）
_AI_ACT_WRITE_COUNT = 0
_AI_ACT_WRITE_LOCK = threading.Lock()


def prune_ai_activities(cap: int = MAX_AI_ACTIVITIES) -> int:
    """保留最新 cap 条 AI 活动，删除更早的。返回删除行数（异常返回 0）。

    用 WriteSession（原生 sqlite3，已验证可读写，不受主 session rolled back 状态影响）。
    按 created_at 取第 (cap+1) 新的时间作为切点，删更早的全部。
    """
    try:
        from app.database import WriteSession
        from app.models.ai_activity import AIActivity
        db = WriteSession()
        try:
            cutoff_row = (
                db.query(AIActivity.created_at)
                .order_by(AIActivity.created_at.desc())
                .limit(1).offset(cap).first()
            )
            if not cutoff_row:
                return 0
            cutoff = cutoff_row[0]
            deleted = (
                db.query(AIActivity)
                .filter(AIActivity.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            db.commit()
            if deleted:
                logger.info(f"[ai_activities] 保留窗口清理：删除 {deleted} 条旧活动（保留最新 {cap}）")
            return deleted or 0
        finally:
            db.close()
    except Exception as _e:
        logger.warning(f"[ai_activities] 清理失败（不影响交易）: {_e}")
        return 0


def maybe_prune_ai_activities():
    """模块级计数触发清理，分摊到每次写入，避免一次性大删除阻塞。"""
    global _AI_ACT_WRITE_COUNT
    with _AI_ACT_WRITE_LOCK:
        _AI_ACT_WRITE_COUNT += 1
        if _AI_ACT_WRITE_COUNT < _AI_ACT_PRUNE_EVERY:
            return
        _AI_ACT_WRITE_COUNT = 0
    prune_ai_activities()
# 跟号镜像平仓连续失败计数：key=f"{follower_id}:{leader_ticket}:{action}" -> (次数, 末次时间)
# 达上限(3次)强制标记已平，防连接故障导致无限重试死循环。
#
# ★ 必须带 TTL（2026-08-07 修复）。原实现只存裸计数、只增不删，两个后果：
#   ① 内存泄漏：key 含 leader_ticket，每笔新单都是新 key，7x24 运行持续堆积。
#   ② 更要命的正确性 bug：_MIRRORED 幂等表 600s 过期后同一笔单会重新进入镜像流程，
#      而旧计数还在 → 失败预算已被上一轮耗尽，新一轮第 1 次失败就判超限、
#      强制标记已平 → 跟号放弃平仓，主号已平而跟号持仓裸奔，两边失同步。
#   故预算窗口与幂等窗口对齐：TTL 内累计，过期归零。
_MIRROR_FAIL: dict = {}
_MIRROR_FAIL_LOCK = threading.Lock()
_MIRROR_FAIL_TTL = 600  # 与 _MIRRORED 幂等窗口一致；小于它会造成"幂等仍拦着但预算已重置"的错配
# 反向对账节流：key=account_id -> 上次对账时间戳；每账号每 60s 最多一次
_RECON_LAST: dict = {}
_RECON_LAST_LOCK = threading.Lock()
# ★ 2026-08-07 Phase 1：key=account_id -> 上次对账是否成功（账本是否可信）。
#   节流跳过的周期沿用此结论，避免"每隔一轮就当作未知"而误伤交易频率。
#   写入按账号独立 key，无跨账号竞争（多账号铁律：账号间状态严格隔离）。
_RECON_OK: dict = {}
# ★ 2026-08-06 修复跟号重复跟单：记录每个跟号已复制的(主号票号)，全局去重 TTL 300s
#   主周期 copy_order + 守护线程补单 两条路径可能并发/重复调用，靠 comment 字段做去重不可靠
#   （MT5 返回的 position.comment 可能缺失/被截断），故在进程级内存做硬去重。
#   ★ 2026-08-07 Phase 2：本表已收编进 SignalBus（app/services/signal_bus.py）。
#     收编的理由不是整洁，而是原有的两段式去重**根本挡不住并发**：
#         _is_copied(...)    ← 加锁读，读完立刻放锁
#         place_order(...)   ← 不可逆副作用，耗时数百毫秒（裸奔窗口）
#         _mark_copied(...)  ← 再次加锁写
#     主循环(trading.py:277) 与副号实时跟单守护线程(trading.py:439，10s)
#     真并发调用 copy_order，两条线程都能在裸奔窗口里读到"没跟过"
#     ⇒ 同一张主号单被跟两次 ⇒ 跟号双倍敞口。
#     （已由 tests/test_copy_concurrency.py 稳定复现，非理论推演。）
#     现改用 registry.claim()：一次加锁内完成"检查+占坑"，并发中只有一条能拿到。
#     下面三个名字保留为别名，仅为兼容既有调用与测试，语义不变。
_COPIED_REG = signal_bus.COPIED
_LAST_COPIED_SIGNAL: dict = _COPIED_REG.data   # key=f"{follower_id}:{leader_ticket}"
_LAST_COPIED_LOCK = _COPIED_REG.lock
_COPIED_TTL_SECONDS = 300
# ★ 2026-08-06 修复平亏秒开churn：记录每个账号每个方向最近平仓时间，用于抑制同方向秒级重开
_LAST_CLOSE_TS: dict = {}               # key=f"{account_id}:{direction}" -> 时间戳
_LAST_CLOSE_LOCK = threading.Lock()

# ★★ 2026-08-10 模块级防重复减半标记（进程内跨实例共享）：
#    TradeExecutor 每交易周期(60s) new 一个实例（ai_exit.py:75），实例变量 _partial_done
#    每轮被清空 → 浮盈达标锁50% 被切成指数碎单（0.5→0.25→0.125→0.06→0.01，实测多次）。
#    用模块级 dict 持久跨实例；key=(account_id, ticket)，value=首次 partial 时间戳，
#    超过 24h 惰性清理防内存泄漏。
#    ★ 2026-08-15 P2-7 语义澄清：此 dict 为【进程内】单例 —— 同一 Python 进程内所有
#     TradeExecutor 实例共享（解决周期重建导致的实例变量清空）；但【跨进程不共享】
#     （每个后端进程各自持有一份，互不通信）。多账号 Worker 若各自独立进程，须靠
#     业务层幂等 / DB 去重兜底，不可依赖此 dict 做跨进程互斥。
_PARTIAL_DONE: dict = {}

# ★★ 2026-08-17 P0 修复：pending_verify 重扫节流（幽灵单不刷屏/不阻塞周期）。
#   根因：_rescan_pending_verify 的查询【无 ORDER BY】→ SQLite 按 rowid 恒取最早插入的
#   20 笔 pending 单（08-11/08-12 拉不到 deal 的老幽灵单），今天新平仓的单永远排不上队，
#   真实盈亏永久丢失；且每轮 20 笔 × 3 级 MT5 查询把主号周期拖到 140s+（实测 last_cycle_sec
#   89→140），日志刷屏 5300+ 次。
#   修复：①查询 ORDER BY close_time DESC → 新单优先回填（今天平仓的单 deal 必在 MT5 窗口内）
#        ②同一 ticket 距上次尝试 <600s 直接跳过 → 拉不到的旧单降频，不再每轮重扫占坑。
#   key=(account_id, mt5_ticket) -> 上次尝试 epoch；进程内跨实例共享（与 _PARTIAL_DONE 同款）。
_PENDING_RESCAN_TS: dict = {}
_PENDING_RESCAN_TTL = 600  # 秒；同一 ticket 10 分钟内最多重扫一次
_PENDING_RESCAN_LOCK = threading.Lock()


def clear_leader_exit_bus():
    """清空总线。现已弃用周期清理：改为靠 TTL + 跟号幂等去重，
    避免周期开始/结束清总线把高频监控线程在周期间发布的平仓动作冲掉。保留函数以兼容旧调用。"""
    with _BUS_LOCK:
        _LEADER_EXIT_BUS.clear()


def _mirror_key(follower_id: str, leader_ticket, action_type: str) -> str:
    return f"{follower_id}:{leader_ticket}:{action_type}"


def _is_mirrored(follower_id: str, leader_ticket, action_type: str) -> bool:
    """只读检查：同一跟号对同一主号票号的同一种动作，10分钟内是否已执行过。

    ⚠️ 与 `_is_copied` 同理，这只是**廉价预检**，读完就放锁。
    并发去重必须用 `_claim_mirror()`。
    """
    return _MIRRORED_REG.is_active(_mirror_key(follower_id, leader_ticket, action_type),
                                   ttl=_MIRRORED_TTL_SECONDS)


def _claim_mirror(follower_id: str, leader_ticket, action_type: str) -> bool:
    """★ 原子占坑：并发调用中**有且只有一条**线程拿到 True。

    必须在 close_position / modify_sl_tp（不可逆点）**之前**调用。
    拿到 True 的线程有义务：
      · 动作完成（或确认无需重做）→ 保持占坑；
      · 动作失败且应当重试 → 立刻 `_release_mirror` 归还。
    占坑不归还的后果比重复镜像更严重：主号已平、跟号平不掉，
    留下一条裸奔的反向敞口直到止损。
    """
    return _MIRRORED_REG.claim(_mirror_key(follower_id, leader_ticket, action_type),
                               ttl=_MIRRORED_TTL_SECONDS)


def _release_mirror(follower_id: str, leader_ticket, action_type: str):
    """归还占坑（动作失败、下一轮需要重试时使用）。"""
    _MIRRORED_REG.release(_mirror_key(follower_id, leader_ticket, action_type))


def _mark_mirrored(follower_id: str, leader_ticket, action_type: str):
    """标记/续期某动作已镜像（防死循环）。让 TTL 从动作完成时刻起算。"""
    _MIRRORED_REG.mark(_mirror_key(follower_id, leader_ticket, action_type))
    _MIRRORED_REG.gc(ttl=_MIRRORED_TTL_SECONDS)


def _bump_mirror_fail(key: str) -> int:
    """累计一次镜像失败并返回当前窗口内的失败次数（自带过期回收）。

    返回值供调用方判断是否达到强制放弃阈值。窗口外的旧计数视为不存在，
    保证每一轮镜像尝试都拿到完整的重试预算。
    """
    now = time.time()
    with _MIRROR_FAIL_LOCK:
        cnt, ts = _MIRROR_FAIL.get(key, (0, 0.0))
        cnt = cnt + 1 if (now - ts) <= _MIRROR_FAIL_TTL else 1
        _MIRROR_FAIL[key] = (cnt, now)
        # 顺带回收过期条目，避免长跑堆积（与 _mark_mirrored 同思路）
        for kk in [k for k, (_, t) in _MIRROR_FAIL.items()
                   if now - t > _MIRROR_FAIL_TTL]:
            _MIRROR_FAIL.pop(kk, None)
        return cnt


def _copy_key(follower_id: str, leader_ticket) -> str:
    return f"{follower_id}:{leader_ticket}"


def _is_copied(follower_id: str, leader_ticket) -> bool:
    """只读检查：该跟号是否已在 TTL 内复制过该主号票号。

    ⚠️ 这只是**廉价预检**，用来在并发之外省掉一整套手数计算/风控开销。
    它绝不能单独用来做并发去重——读完就放锁，返回值在下一瞬间就可能过时。
    真正防重复跟单的是 `_claim_copy()`。
    """
    if not leader_ticket:
        return False
    return _COPIED_REG.is_active(_copy_key(follower_id, leader_ticket),
                                 ttl=_COPIED_TTL_SECONDS)


def _claim_copy(follower_id: str, leader_ticket) -> bool:
    """★ 原子占坑：并发调用中**有且只有一条线程**拿到 True。

    必须在 place_order（不可逆点）之前调用。拿到 True 的线程有义务：
      · 成交 → 保持占坑（_mark_copied 续期）；
      · 未成交 → 立刻 `_release_copy` 归还，否则 TTL 内这张主号单再也跟不上
        （守护线程的补单兜底会被自己挡在门外）⇒ 漏跟，违反「多交易多赚钱」。
    """
    if not follower_id or not leader_ticket:
        return True  # 无法构成有效键：不拦截，交由上游逻辑决定（宁可放行不可误杀）
    return _COPIED_REG.claim(_copy_key(follower_id, leader_ticket),
                             ttl=_COPIED_TTL_SECONDS)


def _release_copy(follower_id: str, leader_ticket):
    """归还占坑（下单未成交时使用）。"""
    if not follower_id or not leader_ticket:
        return
    _COPIED_REG.release(_copy_key(follower_id, leader_ticket))


def _mark_copied(follower_id: str, leader_ticket):
    """标记/续期该跟号已复制该主号票号（成交后调用，让 TTL 从成交时刻起算）。"""
    if not follower_id or not leader_ticket:
        return
    _COPIED_REG.mark(_copy_key(follower_id, leader_ticket))
    _COPIED_REG.gc(ttl=_COPIED_TTL_SECONDS)


def _record_close_for_churn(account_id: str, direction: str):
    """记录某账号某方向最近平仓时间，用于抑制秒级同方向重开。"""
    if not account_id or not direction:
        return
    k = f"{account_id}:{direction.upper()}"
    with _LAST_CLOSE_LOCK:
        _LAST_CLOSE_TS[k] = time.time()


def _is_churn_suppressed(account_id: str, direction: str, cooldown: float = 90.0) -> bool:
    """检查该账号该方向是否在 cooldown 秒内刚平仓过；若是则抑制重开（防churn）。"""
    if not account_id or not direction:
        return False
    k = f"{account_id}:{direction.upper()}"
    with _LAST_CLOSE_LOCK:
        ts = _LAST_CLOSE_TS.get(k)
        if ts and (time.time() - ts) < cooldown:
            return True
    return False


def publish_leader_exit(leader_account_id: str, leader_ticket, action: str,
                        close_pct: float = None, new_sl: float = None,
                        leader_open_price: float = None):
    """主号每执行一笔平仓/移损，广播一次，供所有跟号镜像（高频锁利线程也在周期间发布）。

    ★ 2026-08-07 根治「主号平了跟号没平」的总线级根因：
      旧实现把 ts 只写在【条目】上、追加动作时不刷新，consume 又按条目年龄整体过期。
      后果：同一 key 上后发布的动作会继承首次创建时间被误判过期。
      对固定 key `__BASKET_CLOSE_ALL__`（永久复用）尤其致命 —— 条目创建后
      180~300s 这段窗口内所有篮子全平广播对跟号完全不可见（每 5 分钟瞎 2 分钟）。
      根治：每条动作自带 ts，过期判定下沉到动作级；条目 ts 退化为「最后活跃时间」仅供 GC。
    """
    global _BUS_SEQ
    key = f"{leader_account_id}:{leader_ticket}"
    now = time.time()
    with _BUS_SEQ_LOCK:
        _BUS_SEQ += 1
        aid = _BUS_SEQ
    with _BUS_LOCK:
        # GC：回收「最后活跃时间」超过阈值的整条目，防内存无限增长。
        # 阈值 >= _BUS_TTL，保证绝不会回收掉仍在有效期内的动作。
        expired = [k for k, v in _LEADER_EXIT_BUS.items()
                   if now - v.get("ts", 0) > _BUS_GC_TTL]
        for k in expired:
            _LEADER_EXIT_BUS.pop(k, None)
        ent = _LEADER_EXIT_BUS.get(key)
        if ent is None:
            ent = {"actions": [], "ts": now}
            _LEADER_EXIT_BUS[key] = ent
        # 刷新条目活跃时间，并丢弃本条目内已过期的历史动作（避免长期复用的 key 无限堆积）
        ent["ts"] = now
        ent["actions"] = [a for a in ent["actions"]
                          if now - a.get("ts", 0) <= _BUS_TTL]
        ent["actions"].append({"id": aid, "ts": now, "action": action,
                               "close_pct": close_pct, "new_sl": new_sl,
                               "leader_open_price": leader_open_price})


def consume_leader_exit(leader_account_id: str, leader_ticket):
    """跟号取走主号对该持仓的未过期出场动作（列表）；无则 None。

    ★ 过期判定为【动作级】：每条动作按自身发布时间独立计龄，
      不再受同 key 上更早动作的条目创建时间牵连（见 publish_leader_exit 注释）。
    """
    key = f"{leader_account_id}:{leader_ticket}"
    now = time.time()
    with _BUS_LOCK:
        ent = _LEADER_EXIT_BUS.get(key)
        if not ent or not ent["actions"]:
            return None
        # 仅返回未过期动作（过期=上上轮，对应持仓早已平，避免误镜像）
        acts = [a for a in ent["actions"] if now - a.get("ts", 0) <= _BUS_TTL]
        return list(acts) if acts else None


def _call_checked(fn, *args):
    """调用 checked 系列接口并**校验返回形状**；形状不符则返回 None（视为该接口不可用）。

    为什么必须做形状校验（2026-08-07 实测踩坑）：
      getattr(obj, "xxx_checked", None) 只能探测"属性存不存在"。
      MagicMock 会假装拥有任何属性，探测必定成功，但调用后拿到的是
      MagicMock 而不是 (ok, list) —— 直接解包抛 ValueError，
      会把 _manage_positions 整条出场链路打断，导致该轮止损/止盈/熔断全不执行。

    生产环境注入的是真实单例，本不会走到这里；但出场链路是最后一道保命逻辑，
    "宁可降级成旧接口，也绝不能因为一个类型错误就整轮不管持仓"。
    """
    try:
        res = fn(*args)
    except Exception as _e:
        logger.warning(f"[持仓查询] checked 接口调用异常，回退旧接口: {_e}")
        return None
    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], bool):
        # ★ 2026-08-11 防御：res[1] 若混入非 dict（如 worker 偶发返回字符串被 list() 拆成字符），
        #   在此直接剔除，避免下游 p.get("ticket") 抛 'str' object has no attribute 'get' 使
        #   止损/锁利/熔断整轮静默失效。
        return bool(res[0]), [p for p in (res[1] or []) if isinstance(p, dict)]
    return None


def _positions_checked(account_id: str, symbol: str = "XAUUSD"):
    """取指定品种持仓，**可分辨"查询失败"与"真的空仓"**。返回 (ok, positions)。

    存在意义见 mt5_service.get_positions_checked：旧接口两种情况都返回 []，
    凡是要据结果动手（开仓放行、平仓、改账本）的调用方都必须走本函数。

    对未升级的注入对象（旧 mock / 旧插件）回退旧接口并视为 ok=True，
    行为与重构前完全一致，保证零破坏性。
    """
    fn = getattr(mt5_service, "get_positions_checked", None)
    if fn is not None:
        got = _call_checked(fn, account_id, symbol)
        if got is not None:
            return got
    return True, (mt5_service.get_positions(account_id, symbol) or [])


def get_all_positions_rescanned(account_id: str, max_rounds: int = 3, gap: float = 0.5):
    """★ 2026-08-06 修复"智能平仓只处理单订单"根因B（MT5 竞态漏单）。

    实盘教训：3 笔 SELL，MT5 positions_get() 竞态只返回 2 笔 → 第 3 笔漏平 → 继续亏损。
    本函数对账号【全部品种持仓】做多轮扫描：第 1 轮后若有持仓被平掉/刷新，
    下一轮重新拉取并集，确保"处理过的票号"之外的遗漏持仓也能被纳入，杜绝单/部分订单盲区。

    返回：
      list  — 去重后的全部持仓列表（按 ticket 去重，保留最后一次出现的完整字段）
      []    — **确认**账号无持仓
      None  — 所有轮次都查询失败，**不知道**有没有持仓（不可信）

    ★ 2026-08-07 Phase 1 修复（本函数曾在最需要它的时候提前收工）：
      原实现调旧接口 get_all_positions()，它在 Worker 掉线/管道断开/超时时
      同样返回 []，而下面又写着 `if not positions: break` ——
      于是"我查不到"被当成"确认没有持仓"，第一轮就放弃重试，
      这与本函数"多轮扫描防漏单"的存在意义完全相反。

      本函数是五道保护性防线的共同数据源：
        _manage_positions(智能出场) / L3篮子锁利 / 篮子浮亏熔断 / 单笔浮亏熔断
      它们拿到空列表后一律 return —— MT5 抖一下，止损、锁利、熔断全线静默失效，
      而持仓正在亏钱。故：查询失败必须继续重试，全败则如实返回 None。

      None 与 [] 对调用方的 `if not positions: return` 行为一致（都跳过），
      因此零破坏性；区别只在于语义可分辨 + 能打出告警。
    """
    merged: dict = {}
    any_ok = False
    _checked = getattr(mt5_service, "get_all_positions_checked", None)
    for r in range(max_rounds):
        got = _call_checked(_checked, account_id) if _checked is not None else None
        if got is not None:
            ok, positions = got
        else:
            # 兼容未升级/形状不符的注入对象：退回旧接口，行为同重构前
            ok, positions = True, (mt5_service.get_all_positions(account_id) or [])
        if not ok:
            # 查询失败 ≠ 无持仓。不并入、不 break，把剩余轮次用完继续找。
            logger.warning(
                f"[全持仓扫描] {account_id[:8]} 第{r + 1}/{max_rounds}轮查询失败 → 重试"
            )
            if r < max_rounds - 1:
                time.sleep(gap)
            continue
        any_ok = True
        positions = positions or []
        for p in positions:
            if not isinstance(p, dict):
                # ★ 2026-08-11 防御：任何非 dict 元素（字符串/字符）直接跳过，绝不 p.get(...)
                continue
            tk = p.get("ticket")
            if tk is None:
                continue
            merged[tk] = p
        # 确认无任何持仓 → 提前结束（此处的空是可信的空）
        if not positions:
            break
        # 最后一轮不再等待
        if r < max_rounds - 1:
            time.sleep(gap)
    if not any_ok:
        logger.error(
            f"[全持仓扫描] {account_id[:8]} {max_rounds}轮全部查询失败 → 返回不可信(None)。"
            f"本轮止损/锁利/熔断将跳过，等待下轮；请检查 MT5 Worker 连通性"
        )
        return None
    return list(merged.values())

from app.core.debate_engine import DebateEngine, DebateDecision
# ★ 2026-08-12 防御性导入：早期版本曾存在 app/core/config.py，该模块已并入
#   app/config.py。若进程加载了引用旧路径的陈旧字节码，裸 import 会因模块不存在
#   而令 settings 未绑定，导致 execute_cycle 每轮抛 NameError、彻底不开单。
#   故先尝试历史路径，失败一律回退到当前唯一有效的 app.config.settings。
try:
    from app.core.config import settings  # 历史兼容（app/core/config.py 已废弃）
except Exception:  # noqa: BLE001
    from app.config import settings
from app.services.risk_engine import RiskEngine
from app.services.intelligent_sizing import compute_intelligent_size, count_same_direction_positions
from app.services.capital_authority import (
    BROKER_MIN_LOT,
    effective_capital,
    risk_check_capital,
)
from app.services.smart_exit import evaluate_position as smart_evaluate_position, compute_initial_sl_tp
from app.services.ai_exit import AIExitAgent, get_exit_agent
from app.services.position_manager import get_position_manager
from app.services.mt5_service import mt5_service
from app.services import emergency


def _merge_hard_floor_sl(*, pos_type: str, current_sl: float, rule_new_sl, m1_new_sl):
    """
    ★ 保本/追踪硬地板合并：规则引擎保本/追踪 SL 不可被 M1 跳过。

    逻辑：候选集 = {规则引擎算出的 new_sl, M1 的 new_sl, 当前 SL} 中有效的（非 None/0）。
          - buy 单：取最大 SL = 最锁利（离入场最远、留利最多）
          - sell 单：取最小 SL = 最锁利
          若最优者等于当前 SL，则不移动（返回 None，避免无意义的 modify）。

    效果：M1 接管某持仓时，只要规则引擎算出更锁利的保本/追踪 SL，就一定应用；
          仅当 M1 给出比规则引擎更优（更锁利）的 SL 时才覆盖。浮盈单永不失守。
    """
    pos_type = (pos_type or "").lower()
    candidates = [
        float(s) for s in (rule_new_sl, m1_new_sl, current_sl)
        if s not in (None, 0)
    ]
    if not candidates:
        return None
    if pos_type == "buy":
        best = max(candidates)
    elif pos_type == "sell":
        best = min(candidates)
    else:
        return None
    # 不比当前 SL 更优 → 不移动
    if abs(best - float(current_sl or 0)) < 1e-9:
        return None
    return round(best, 2)
from app.models.trade import Trade
from app.models.api_key import APIKey
from app.models.mt5_account import MT5Account
from app.models.strategy import StrategyConfig
from app.utils.crypto import decrypt
from sqlalchemy.orm import Session


class TradeExecutor:
    """交易执行器 — AI决策 → 风控 → MT5下单 → 数据库记录"""

    def __init__(self, account_id: str, strategy: StrategyConfig, user_id: str, db: Session,
                 engine=None):
        self.account_id = account_id
        self.strategy = strategy
        self.user_id = user_id
        self.db = db

        # ★ 2026-08-05 独立风控：记录本账号是否跟随主号。
        #   follow_leader=True → 只镜像主号出场；False → 跑自己的 AI 决策+完整 L2/L3/AI 出场。
        self._follow_leader = bool(getattr(strategy, "follow_leader", True))

        # ★ 2026-08-07 Phase 1：单轮幂等标记。execute_cycle 每轮开头重置，
        #   _manage_positions 执行一次后置 True，同轮后续调用直接跳过。
        #   （详细动机见 _manage_positions 里的守卫注释）
        self._pm_cycle_done = False

        # ★ 2026-08-19 毫秒级跟单：主号在 place_order（不可逆点）之前把信号
        #   "早广播"给 auto_loop 的回调，挂号与主号并行发单（不再等主号成交）。
        #   主号失败时挂号裸奔仓由 [跟号对账兜底] 机制强制平掉（安全兜底已有）。
        self._early_copy_cb = None

        # 若外部传入共享引擎（如 dashboard 单例），则复用，避免
        # auto_loop 自己另开一套引擎导致 API 调用重复/解析不一致
        if engine is not None:
            self.debate_engine = engine
        else:
            # Fallback: 独立创建引擎（兼容老路径：手动触发 / 探针等）
            deepseek_key = self._get_decrypted_key("deepseek")
            hunyuan_key = self._get_decrypted_key("hunyuan")
            market_primary_id = self._get_market_primary_id(db)
            self.debate_engine = DebateEngine(
                deepseek_key=deepseek_key,
                hunyuan_key=hunyuan_key,
                mt5_service=mt5_service,
                market_primary_id=market_primary_id,
            )
        # P0-1：传入 mt5_service + account_id，使风控 6 层（点差/持仓数/同向/日亏/回撤）
        # 能经 IPC 向本账号 Worker 查询真实数据，而非在父进程误调 mt5.*（静默失效）。
        self.risk_engine = RiskEngine(strategy, mt5_service=mt5_service, account_id=self.account_id)
        # M1: AI 出场 Agent（复用辩论引擎的 DeepSeek 快速模型客户端；无 Key 时降级为 None→回退规则引擎）
        # ★ 用 get_exit_agent 单例：按 account_id 跨周期复用同一实例，决策缓存(self._cache)持久不丢。
        try:
            self.exit_agent = get_exit_agent(self.debate_engine.deepseek, self.account_id)
        except Exception:
            self.exit_agent = None
        # ★ 2026-08-14 Position Manager（纯加法·AI 自主仓位管理）：
        #   按 account_id 单例；POSITION_MANAGER_ENABLED=False 时 get_position_manager 返回 None，
        #   整层失效（原有 M1 云端 + 规则引擎完全不动）。确定性「利润走不动」机械平仓 +
        #   本地 qwen3:8b「开错单最小亏损平 / 追踪锁利」增强。
        try:
            self.position_manager = get_position_manager(self.account_id)
        except Exception:
            self.position_manager = None

    def _push_feed(self, kind: str, detail: str, direction: str = "",
                   confidence: float = 0.0, pnl: float = 0.0, ticket: str = "",
                   open_price: float = 0.0, close_price: float = 0.0, reason: str = ""):
        """交易活动流推送的**安全外壳**（Phase 1 / 2026-08-07 新增）。

        这层壳存在的唯一理由：活动流是纯展示，绝不允许它把异常冒泡进交易链路。

        原先本方法没有任何外层 try，而内部第 ① 步（内存缓冲 push_trade_event）
        也是裸调用——被保护的恰恰是不要紧的 ② DB 持久化。
        全文 5 个调用点里有 2 个（主号开仓 / 跟号开仓）不在 try 内，
        一旦抛就会跳过后面的状态转移（冷却时间戳、跟单去重标记），
        让"前端少显示一条记录"升级成"仓位闸门失效 / 重复跟单"。

        修法是把风险摁死在源头：不管未来谁往实现里加什么，都出不来。
        """
        try:
            self._push_feed_impl(
                kind, detail, direction=direction, confidence=confidence,
                pnl=pnl, ticket=ticket, open_price=open_price,
                close_price=close_price, reason=reason,
            )
        except Exception as _fe:
            logger.warning(f"[执行器] 交易活动流推送失败(已忽略，不影响交易链路): {_fe}")

    def _push_feed_impl(self, kind: str, detail: str, direction: str = "",
                        confidence: float = 0.0, pnl: float = 0.0, ticket: str = "",
                        open_price: float = 0.0, close_price: float = 0.0, reason: str = ""):
        """推送交易事件到内存实时缓冲 + DB 持久化，供前端「交易执行流」展示

        ★ 毫秒级可靠性修复(2026-08-05):
          1. 内存缓冲(push_trade_event)永远先写——这是实时的、不依赖DB
          2. DB持久化用独立session——绝不毒杀调用方的主session
             (根因：Defender锁DB→commit失败→主session进入rolled back状态
              → 整个cycle的MT5订单全部被回滚=orders=0)
          3. 写入失败仅WARNING，不抛异常，不阻塞交易链路
        ★ 2026-08-05 增强：透传 pnl/ticket/open_price/close_price/reason，
          供 AI 开仓决策注入「最近真实盈亏」（内存缓冲永远可写）。
        """
        from app.services.ai_memory import push_trade_event
        from app.models.ai_activity import AIActivity
        # ① 内存实时缓冲（永远成功，毫秒级）
        # 查账户名/登录号用缓存避免DB查询（Defender锁时query也崩）
        acct_name = getattr(self, '_acct_name_cache', '')
        acct_login = getattr(self, '_acct_login_cache', '')
        if not acct_name:
            try:
                acct = self.db.query(MT5Account).filter(MT5Account.id == self.account_id).first()
                if acct:
                    self._acct_name_cache = acct.name or ""
                    self._acct_login_cache = acct.account_id or ""
                    acct_name = self._acct_name_cache
                    acct_login = self._acct_login_cache
            except Exception:
                pass  # DB锁时连查询都失败，用空字符串兜底
        push_trade_event(kind, detail, account_id=self.account_id,
                         account_name=acct_name, account_login=acct_login,
                         direction=direction, confidence=confidence,
                         pnl=pnl, ticket=ticket,
                         open_price=open_price, close_price=close_price, reason=reason)
        # ② DB持久化（独立session，失败不影响交易）
        try:
            from app.database import SessionLocal
            local_db = SessionLocal()
            try:
                local_db.add(AIActivity(
                    user_id=self.user_id,
                    mt5_account_id=self.account_id,
                    kind=kind,
                    symbol="XAUUSD",
                    timeframe="M15/H1/H4",
                    direction=direction,
                    confidence=round(float(confidence or 0), 3),
                    detail=detail,
                ))
                local_db.commit()
                # 活动流保留窗口：分摊触发清理，根治无界增长（task #294）
                maybe_prune_ai_activities()
            except Exception:
                local_db.rollback()
            finally:
                local_db.close()
        except Exception as _ae:
            logger.warning(f"[执行器] AI活动流写入失败: {_ae}")

    # ── 毫秒级可靠性：DB操作安全包装 ──
    # 核心原则：MT5下单永远不依赖DB成功。DB写入是"审计旁路"，失败不阻塞交易。
    # Defender锁DB时所有commit都会失败→SQLAlchemy session进入rolled back状态
    # → 后续所有query/commit全部静默失败 → orders被回滚=0（信号丢失根因）
    #
    # ★ 2026-08-05 修复：优先使用 WriteSession（原生 sqlite3 引擎，已验证 100% 可写）
    def _sync_trade_sl_to_db(self, ticket, new_sl: float) -> None:
        """★ 2026-08-11 P0 假账修复：MT5 端 SL 修改成功后回写 trades.sl。

        背景：smart_exit 上移 SL 只改 MT5 端（modify_sl_tp），从不写回 trades 表
        → 对账 `_reconcile_positions` deal 未匹配时用【陈旧原始 SL】推算平仓价
        → 浮盈 +10 的单被记成平在原始 SL 的假亏损（实证 3 笔主号 BUY 假亏 -144.86，
          含 378596055 真实 +2.00 记成 -56.32）。

        铁律：MT5 端止损与本地账本必须同源。本方法在每次 SL 修改成功后调用，
        用独立写 session 更新 trades.sl（不动主 session，失败只留日志不阻断交易链）。
        """
        if not ticket or not new_sl:
            return
        try:
            self._safe_db_write(
                lambda db: db.query(Trade)
                .filter(Trade.mt5_account_id == self.account_id,
                        Trade.mt5_ticket == str(ticket),
                        Trade.close_time.is_(None))
                .update({"sl": round(float(new_sl), 2)}, synchronize_session=False),
                label="SL回写",
            )
        except Exception as _e:
            logger.warning(f"[SL回写] ticket={ticket} 回写异常(不影响交易): {_e}")

    def _safe_db_write(self, write_fn, label="db_write"):
        """用独立session执行DB写入，失败不影响主session和交易链路"""
        try:
            from app.database import WriteSession
            local_db = WriteSession()
            try:
                write_fn(local_db)
                local_db.commit()
                return True
            except Exception as e:
                local_db.rollback()
                logger.warning(f"[{label}] DB写入失败(写引擎): {e}")
                return False
            finally:
                local_db.close()
        except Exception as e:
            logger.warning(f"[{label}] DB写入异常: {e}")
            return False

    def _safe_db_commit(self, label="commit"):
        """安全提交主session：失败后立即rollback恢复session状态"""
        try:
            self.db.commit()
            return True
        except Exception as e:
            logger.warning(f"[{label}] 主session commit失败(自动rollback恢复): {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
            return False

    def _emit_risk_event(self, *, event_type: str, stage: str, codes, reasons,
                         direction: str = "", intended_lots=None,
                         confidence=None, symbol: str = "XAUUSD") -> None:
        """记录一条「为什么没开单」的风控事件（Phase 4 溯源）。

        ★ 这个方法永远不抛。
          客户早上问「昨晚一单没开，你们系统是不是死了」，能不能当场答上来
          全靠这条记录；但「记不下来」绝不允许升级成「开不了单」——
          所以整段包在 try 里，失败只留 debug 日志。
          这是 risk_event_log 模块头第 1 条硬约束在调用侧的呼应。

        ★ 为什么在这里包一层而不是各点直接调 record_risk_event：
          执行器有自己的 `_safe_db_write`（独立写 session + 瞬锁重试 +
          崩溃不阻塞交易），风控事件必须复用它，否则在 Defender 锁住
          DB 文件时会新开一条没有重试保护的写路径。
        """
        try:
            from app.services.risk_event_log import record_risk_event

            record_risk_event(
                user_id=self.user_id,
                mt5_account_id=self.account_id,
                event_type=event_type,
                stage=stage,
                codes=codes,
                reasons=reasons,
                symbol=symbol,
                direction=direction,
                intended_lots=intended_lots,
                confidence=confidence,
                db_writer=lambda fn: self._safe_db_write(fn, label="风控事件"),
            )
        except Exception as _e:  # noqa: BLE001
            logger.debug(f"[{self.account_id[:8]}] 风控事件记录跳过: {_e}")

    def _get_decrypted_key(self, provider: str) -> str:
        """从 APIKey 表获取并解密密钥"""
        key_record = self.db.query(APIKey).filter(
            APIKey.user_id == self.user_id,
            APIKey.provider == provider,
            APIKey.is_active == True,
        ).first()
        if key_record and key_record.encrypted_key:
            return decrypt(key_record.encrypted_key)
        return ""

    def _get_market_primary_id(self, db: Session) -> str:
        """查找当前设置为行情主号的账号 ID"""
        primary = db.query(MT5Account).filter(
            MT5Account.user_id == self.user_id,
            MT5Account.is_market_primary == True,
        ).first()
        if primary:
            return primary.id
        # 降级：取第一个已连接的账号
        first = db.query(MT5Account).filter(
            MT5Account.user_id == self.user_id,
            MT5Account.is_connected == True,
        ).first()
        if first:
            return first.id
        return ""

    # ── 从 DB 直接读取策略参数（绕过 SQLAlchemy ORM 缓存）──
    # 根因：2026-08-03 审计发现 self.strategy 对象缓存了 ALTER TABLE 前的旧数据，
    # 导致 enable_l3_guard / basket_tp_amount / reversal_confirm_cycles 等新字段全部为 None。
    # 所有新增风控/平仓参数必须通过此方法获取"新鲜"DB 值。
    def _fresh_strat(self, field_name, default=None):
        try:
            row = self.db.query(StrategyConfig).filter(
                StrategyConfig.mt5_account_id == self.account_id
            ).first()
            if row is not None:
                val = getattr(row, field_name, default)
                if val is not None:
                    return val
        except Exception as e:
            logger.warning(f"[{self.account_id[:8]}] _fresh_strat({field_name}) 查询失败: {e}")
        return default

    def _check_loss_cooldown(self) -> str:
        """
        亏损冷却期检查 — 防止报复性交易
        调研依据：algomatrix.trade "3-candle cooldown after every loss"
        本系统实现：查最近平仓交易，若亏损且在冷却窗口内则跳过开新仓
        - 单笔亏损 → 冷却 60 秒（1 个决策周期）
        - 连续 3+ 笔亏损 → 冷却 180 秒（3 个决策周期）
        注意：冷却期仍执行持仓管理（止损/止盈/追踪），只不开新仓
        """
        try:
            recent = self.db.query(Trade).filter(
                Trade.mt5_account_id == self.account_id,
                Trade.close_time.isnot(None),
            ).order_by(Trade.close_time.desc()).limit(5).all()

            if not recent:
                return ""

            # 计算连续亏损数
            consec_losses = 0
            for t in recent:
                # ★ 2026-08-10 兜底修复：旧数据 net_profit 恒为 0(非 None)，
                #   原 `is not None` 判断失效 → 恒取 0 → 连续亏损冷却永不触发。
                #   改为 `or` 链：net_profit 为 0 时回退 profit。
                pnl = (t.net_profit or t.profit or 0)
                if pnl < 0:
                    consec_losses += 1
                else:
                    break

            if consec_losses == 0:
                return ""

            # 确定冷却时长
            cooldown_seconds = 60 if consec_losses < 3 else 180
            last_close = recent[0].close_time
            if last_close is None:
                return ""
            # ★ 2026-08-18 修复：SQLite 读出 close_time 常丢 tz(tzinfo=None, 但值按 UTC 写)，
            #   与 datetime.now(timezone.utc) 相减抛 "offset-naive/offset-aware" → 被 except 吞掉
            #   → 冷却检查恒返回 "" → 亏损冷却静默失效(可报复性交易)。读侧补 tzinfo=utc 对齐写入端。
            if last_close.tzinfo is None:
                last_close = last_close.replace(tzinfo=timezone.utc)
            # ★ 2026-08-15 复检P1修复：close_time 已统一 UTC 写入（上轮 fix），
            #   此处必须用 UTC now 相减——原 `datetime.now()`（本地 GMT+8 naive）与
            #   UTC close_time 相减恒偏 8h → elapsed 恒 > 冷却窗 → 亏损冷却静默永久失效
            #   （防报复交易形同虚设）。改成 timezone.utc 后与写入端同基准。
            elapsed = (datetime.now(timezone.utc) - last_close).total_seconds()
            if elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                msg = f"亏损冷却中（连亏{consec_losses}笔），{remaining}秒后恢复开仓 — 持仓管理照常执行"
                logger.info(f"[冷却] {msg}")
                return msg
            return ""
        except Exception as e:
            logger.warning(f"[冷却] 检查失败: {e}")
            return ""

    def _calc_position_size(self, balance: float, entry_price: float, sl_points: float,
                            signal_confidence: float = 0.7,
                            same_direction_count: int = 0,
                            ai_target_risk_pct: float = None,
                            adx: float = None) -> dict:
        """
        智能手数自适应：
        base_risk$ × (vol×sig×dir×adx) / (atr×100)，clamp 到 [min_lot, max_lot]
        返回 {lots, reason, components} — 方便前端展示决策过程
        ★ 2026-08-07 v5：ai_target_risk_pct 由 AI 自主仓位管理传入（reduce 时砍半），
          覆盖策略固定风险占比，实现 AI 主动收缩总敞口。
        ★ 2026-08-10 v6：新增 adx 参数（实时 H1 ADX 趋势强弱），按趋势加/减码，
          实现用户需求"底线~红线之间按趋势强弱自动开单手数"。
        """
        result = compute_intelligent_size(
            balance=balance,
            atr=sl_points,                 # 用止损点数当 ATR 近似
            signal_confidence=signal_confidence,
            same_direction_count=same_direction_count,
            strategy=self.strategy,
            ai_target_risk_pct=ai_target_risk_pct,
            adx=adx,
        )
        logger.info(
            f"[手数] {result['reason']} | balance=${balance:.0f} conf={signal_confidence:.0%} "
            f"同向={same_direction_count} → {result['lots']}手"
        )
        return result

    def _cap_to_risk_limit(self, balance: float, sl_points: float, position_size: float):
        """风控硬上限反钳手数（根治小账号被 2% 上限卡死不开单）。

        背景：手数引擎按 base_capital(参考本金) 算，但风控按【真实余额】校验。
              当真实余额 < base_capital（如真实 $2408 / 设了 $32000）时，算出的手数
              在真实账户上风险超标被拒 → 小账号永久不开单，违反"多交易多赚钱 + 自适应手数"。
        修法：把实际手数压到「真实余额可承受的风险合规尺寸」，下限为券商最小手数(0.01)。
              - 合规(≤max_risk%) → 返回合规尺寸，照常开仓（小账号按比例自适应，落袋为安）
              - 连最小手数都超标 → 返回 (None, 提示)，调用方跳过（此时确实无法安全开仓，守住不爆仓）
        大账号手数本就合规 → risk_implied 远大于其手数 → 完全不受影响。
        return: (final_lots_or_None, note)

        ★ 2026-08-07 Phase 1（V6 §4.2 权威链收口）：
          本函数曾是差异清单点名的「第三口径」—— 裸用 balance，不走权威模块。
          现改为 risk_check_capital(effective_capital(...))：
            · capital_source='live'（生产库 4 个账号现状）→ 返回 balance，**逐值等价**，零行为变化；
            · manual/input 且设定 > 余额 → 取余额（原意保留：不能拿不存在的钱冒险）；
            · manual/input 且设定 < 余额 → 取设定（**补齐的语义**：客户主动调低本金要被尊重，
              旧实现拿真实余额放行，等于无视客户的保守设定）。

          ⚠ 零新增拒单铁律：上面第三条绝不允许演变成"更容易拒单"。
          客户把本金设得很小时，只压手数不砍单 —— 只要【真实余额】撑得住券商最小手数
          就照常成交（用户铁律：多交易多赚钱，限幅可以，过滤不行）。
        """
        try:
            _max_risk = float(getattr(self.strategy, "max_risk_per_trade_pct", 2.0) or 2.0)
            _broker_min = BROKER_MIN_LOT
            if sl_points <= 0 or not (balance > 0) or _max_risk <= 0:
                return position_size, ""

            # ── 权威链裁定风控本金（唯一入口，禁止在此内联 base_capital 判断）──
            _cap_decision = effective_capital(self.strategy, balance)
            _risk_capital = risk_check_capital(_cap_decision)
            _lot_risk = sl_points * 100.0          # 黄金每手每点 $100
            _cap_tag = (
                f"真实余额${balance:.0f}"
                if _cap_decision.source == "live"
                else f"风控本金${_risk_capital:.0f}({_cap_decision.label}/余额${balance:.0f})"
            )

            risk_implied = (_risk_capital * _max_risk / 100.0) / _lot_risk
            risk_implied = int(risk_implied * 100) / 100.0   # 向下取整到 0.01（floor，宁可小不可超）

            if risk_implied < _broker_min:
                # 设定本金撑不住最小手数 → 再用【真实余额】兜底判断能否成交。
                # live 模式下两者相等，永远走不进这个分支 → 等价性不受影响。
                _real_implied = (balance * _max_risk / 100.0) / _lot_risk
                if _real_implied >= _broker_min:
                    return _broker_min, (
                        f"[风控钳手] {_cap_tag} 设定过小(理论{risk_implied}手)，"
                        f"压到券商最小{_broker_min}手成交 —— 真实余额可承受，不拒单"
                    )
                # ★ 零拒单铁律（用户硬律：多交易多赚钱，限幅可以过滤不行）：
                #   live 本金即便 min 手"理论风险超标"，也按券商最小手数成交。
                #   真实风险极小 —— 黄金 1手1点≈$1，0.01手×SL点数(例2250)≈$22.5，
                #   仅占 $1000 本金的 2.25%；由 L3 篮子护盾 + 日损熔断兜底"不爆仓"。
                #   绝不让风险模型的 100× 保守估计剥夺小客户（$1000 起）的交易权。
                return _broker_min, (
                    f"[风控钳手] 真实余额${balance:.0f} 偏小，按券商最小{_broker_min}手成交 "
                    f"(真实风险≈${_broker_min * sl_points * 1.0:.0f}，由篮子护盾兜底，不拒单)"
                )

            if position_size > risk_implied + 1e-9:
                return risk_implied, (
                    f"[风控钳手] {_cap_tag} 手数 {position_size}→{risk_implied}手 "
                    f"(SL={sl_points:.1f}点, 风险上限{_max_risk}%)"
                )
            return position_size, ""
        except Exception as _e:
            logger.warning(f"[风控钳手] 计算失败，沿用原手数: {_e}")
            return position_size, ""

    def _reconcile_positions(self) -> bool:
        """反向对账（MT5→本地 Trade 表）：根治 AI 失明。

        返回值 = 本地账本是否可信（True 才允许进入决策/开新仓）。

        每轮先把 MT5 真实持仓拉回来，与本地 trades 表(open 状态)比对：
        - 本地标记 open 但 MT5 已无此单 → 该单被外部平掉(手动/止损/爆仓/桥漏事件)，
          立即回填平仓价/盈亏/时间，标记 closed，让本地账本与现实一致。
        - 这样 AI 决策时读的「我有哪些持仓」永远是真实值，不会基于过期账本重复下单
          (调研支撑: purvik6062 MultiPositionManager.recoverActivePositions；skopaqtrader
          「always reads fresh token, no stale cache」)。
        失败不抛异常——对账是辅助，绝不让它阻塞持仓管理（止损止盈必须继续）；
        但**会阻止开新仓**：不知道自己有什么仓的时候下单，就是盲开。
        节流：每账号每 60s 最多跑一次，避免 DB 不可写时(Defender只读)的写入风暴。
        """
        _now_ts = time.time()
        if _now_ts - _RECON_LAST.get(self.account_id, 0) < 60:
            # 节流跳过：沿用上一次对账结论。首轮不会走到这里（无节流记录），故必有值。
            return _RECON_OK.get(self.account_id, True)
        _RECON_LAST[self.account_id] = _now_ts
        try:
            # ★ 2026-08-07 Phase 1：必须用 checked 版。旧的 get_positions() 在
            #   Worker 掉线/超时时同样返回 []，与"真的空仓"不可分辨；一旦当成
            #   空仓，下面会把本地全部 open trades 判成"已被外部平掉"批量写 closed，
            #   AI 下一轮读账本以为空仓 → 在已有持仓之上重复开仓 → 突破持仓上限。
            _q_ok, live = mt5_service.get_positions_checked(self.account_id, "XAUUSD")
            if not _q_ok:
                _RECON_OK[self.account_id] = False
                logger.warning(
                    f"[反向对账] {self.account_id[:8]} MT5 持仓查询失败 → 本轮不改账本、"
                    f"不开新仓（持仓保护继续）。宁可少做一单，不可盲开一单"
                )
                return False
            live_tickets = {str(p.get("ticket", "")) for p in live}
            open_trades = self.db.query(Trade).filter(
                Trade.mt5_account_id == self.account_id,
                Trade.close_time.is_(None),
                Trade.action.in_(["buy", "sell"]),
            ).all()
            if not open_trades:
                _RECON_OK[self.account_id] = True
                # ★★ 2026-08-12 P0 根治：空仓期也必须回填 pending_verify。
                #   旧实现在此直接 return True，导致函数末尾（1186行）的
                #   _rescan_pending_verify() 永不执行。而 pending_verify 单
                #   close_time 已非空、不进 open_trades ——恰恰只有"全平仓后的
                #   空仓期"才是回填它们的最佳时机（MT5 历史此刻已完全同步）。
                #   结果：pending 单永久挂账、真实盈亏永久丢失（实测卡死 34 笔）。
                #   修复：早退前先跑一次重扫，让空仓期成为回填主战场。
                self._rescan_pending_verify()
                return True
            # 批量查最近成交，按 ticket 匹配平仓盈亏
            # ★★ 2026-08-10 修复：原 limit=50 太小——主号/跟号频繁交易时（今日各50笔平仓），
            #   外部平仓(MT5端SL/TP/manual)的单会滑出最近50笔范围 → deals 匹配不上 →
            #   _pf=0/_cp=open_price → 6 笔 mt5_closed_external 开平价相同 profit=0（盈亏丢失）。
            #   调大至 500（worker 端拉最近30天 deals 取最后 N 笔，性能可接受）。
            _deals_map = {}
            try:
                _rd = mt5_service.get_recent_deals(self.account_id, limit=500) or {}
                # ★★ 2026-08-10 修复：worker 返回键名是 "recent"（非 "deals"）——原读
                #   "deals" 恒空 → 外部平仓 deal 从未匹配 → profit 恒 0、开平价相同。
                #   兼容两种键名，且优先按 position_id（=持仓 ticket）匹配；
                #   同一 position 有开仓/平仓两个 deal，必须优先取【平仓 deal】
                #   （entry=1 out / 3 out_by，带真实盈亏），开仓 deal profit=0 会误覆盖。
                for d in (_rd.get("recent") or _rd.get("deals") or []):
                    _dt = str(d.get("position_id") or d.get("ticket") or d.get("id") or "")
                    if not _dt:
                        continue
                    _entry = int(d.get("entry") or -1)
                    _is_out = _entry in (1, 3) or bool(float(d.get("profit") or 0))
                    if _is_out:
                        _deals_map[_dt] = d          # 平仓 deal 覆盖（带盈亏）
                    # ★ 2026-08-15 审计P2修复：不再用开仓 deal 兜底——
                    #   开仓 deal（entry=0, profit=0）被消费时会被误判为「平在开仓价 breakeven」
                    #   （假盈亏静默入库，违背「绝不假造」）。缺失即视为未匹配，由下方
                    #   get_deal_by_position 精准拉取；仍失败则 pending_verify。
            except Exception:
                pass
            # ★ 2026-08-15 复检P2修复：外部对账时间基准统一 UTC（与自平路径一致）——
            #   原 datetime.now()（本地 GMT+8 naive）写入 close_time/exit_time 后，
            #   _to_utc_iso 按 UTC 解释 → 外部平仓行展示/统计偏移 8h；且
            #   get_deal_by_position(close_time=_now) 窗口基准也随之漂移。
            _now = datetime.now(timezone.utc)
            _txs = []  # ★ 2026-08-10 外部平仓明细（trade_exits）
            _reconciled = 0
            _deal_miss = 0  # ★ 2026-08-11 诊断：未匹配到 deal 的 ticket 数
            for t in open_trades:
                if str(t.mt5_ticket) in live_tickets:
                    continue  # MT5 仍在，正常
                # MT5 已无此单 → 本地需补平仓
                # ★★ 2026-08-11 三阶 fallback 修复（对账 deal 匹配失败 → profit=0 假 breakeven）：
                #   ① 按 ticket 精准拉 deal（history_deals_get(position=ticket)，不依赖缓存窗口）
                #   ② fallback 到 recent 缓存窗口匹配
                #   ③ 仍失败 → 用 SL/TP 反向推算（至少不是 0，归因不丢）
                _d = _deals_map.get(str(t.mt5_ticket)) or {}
                _d_matched = bool(_d)
                if not _d:
                    try:
                        # ★ 2026-08-12 P0 根治：传 close_time=_now（≈实时平仓时间），
                        #   让 worker 用窄窗口精准命中该笔平仓成交，绕过 MT5 宽窗口截断。
                        _pd = mt5_service.get_deal_by_position(self.account_id, t.mt5_ticket, close_time=_now, open_price=t.open_price, action=t.action)
                        # ★ 2026-08-11 兼容：_pd 可能是 str（旧 worker 未注册该命令时返回错误串）
                        #   或 dict。确保 dict 形态再 .get()，否则按拉不到处理。
                        if not isinstance(_pd, dict):
                            _pd = {}
                        _dl = _pd.get("deal")
                        if not isinstance(_dl, dict):
                            _dl = None
                        if _dl:
                            _d = _dl
                            _d_matched = True
                    except Exception:
                        _pd = {}
                if not _d_matched:
                    _deal_miss += 1
                _cp = float(_d.get("price") or _d.get("close_price") or 0) or 0
                # ★ #9 net_profit 含佣优先（含佣/已实现结算），缺失时回退 MT5 deal profit
                _pf = float(_d.get("net_profit") or _d.get("profit") or 0) or 0
                # ★ P1-#4 REAL 券商平仓价 price=0 缺回退（通用安全网，覆盖 recent 缓存窗口路径）：
                #   REAL 账户 deal.price 返回 0（DEMO 正常）但 profit 真实。用 open_price +
                #   盈亏/手数 反推真实平仓价，避免把 close_price 记 0 污染归因/可视化。
                #   worker 端 get_deal_by_position 已对称回退，此处兜底覆盖 recent 缓存窗口路径。
                if _cp == 0 and _pf != 0 and _d_matched:
                    _cp_vol = float(_d.get("volume") or t.volume or 0) or 0
                    if _cp_vol > 0 and t.open_price:
                        _cp_move = _pf / (_cp_vol * 100.0)
                        _cp = round(
                            (t.open_price + _cp_move) if str(t.action).upper() == "BUY"
                            else (t.open_price - _cp_move), 2)
                # ★★ 2026-08-11 P0 修复：deal 完全拉不到时【不再用陈旧 SL 推算假盈亏】。
                #   原实现用 trades.sl（开仓时原始值，smart_exit 上移后不回写）硬推平仓价
                #   → 浮盈 +10 的单记成平在原始 SL 的假亏损（实证：378596055 真实 +2.00
                #   @4379.21，DB 记 -56.32 @4350.05；上午 3 笔主号 BUY 假亏 -144.86、
                #   跟号合计 -8.8k 全是同一根因）。
                #   正确语义：不知道就是不知道——标记「外部平仓·盈亏待核实」，
                #   绝不把盈利单伪装成亏损（假亏损污染净利统计/亏损冷却/AI 学习）。
                #   待 get_deal_by_position 恢复或人工核实后回填。
                # ★ 2026-08-13 审计(P1)：REAL 保本平仓 price=0 且 profit=0 → 平仓价=开仓价(近似保本)，
                #   不再永久 pending_verify（deal 已命中、盈亏确为0，可推导）。
                if _d_matched and _cp == 0 and _pf == 0:
                    _cp = round(float(t.open_price or 0), 2)
                _unverified = not _d_matched or (_cp == 0 and _pf == 0)
                if _unverified:
                    t.close_price = None
                    t.close_time = _now
                    t.profit = 0.0
                    t.net_profit = 0.0
                    t.result = "pending_verify"
                    t.exit_reason = "mt5_closed_external_unverified"
                    logger.warning(
                        f"[反向对账] {self.account_id[:8]} ticket={t.mt5_ticket} "
                        f"外部平仓但 deal 拉不到 → 标 pending_verify，绝不假造盈亏"
                    )
                    try:
                        from app.models.trade_exit import TradeExit
                        _txs.append(TradeExit(
                            trade_id=t.id, user_id=t.user_id, mt5_account_id=t.mt5_account_id,
                            mt5_ticket=t.mt5_ticket, action=t.action,
                            exit_volume=round(float(t.volume or 0), 2),
                            exit_price=float(t.open_price or 0),
                            profit=0.0, net_profit=0.0,
                            result="pending_verify", exit_reason="mt5_closed_external_unverified",
                            partial=False, exit_time=_now,
                        ))
                    except Exception:
                        pass
                    _reconciled += 1
                    continue
                t.close_price = _cp
                t.close_time = _now
                # ★ 2026-08-15 审计P2修复：外部对账主行盈亏改【累加式】（与自平路径 4702 同构）——
                #   原覆盖式 `t.profit=_pf` 会丢掉此前 partial 已实现盈亏（早期 partial 计入后
                #   被最后一笔覆盖，多次平仓账本聚合失真）。主行 volume 保持「开仓量」语义
                #   （与自平一致），每次平仓量在 trade_exits 明细中留痕。
                t.profit = round((t.profit or 0) + _pf, 2)
                t.net_profit = round((t.net_profit or 0) + _pf, 2)
                t.result = "win" if _pf > 0 else ("loss" if _pf < 0 else "breakeven")
                t.exit_reason = "mt5_closed_external"
                # ★ 2026-08-10 外部平仓（MT5 端 SL/TP/manual 触发）也写平仓明细，审计完整链
                try:
                    from app.models.trade_exit import TradeExit
                    _ev = float(_d.get("volume") or t.volume or 0)
                    # ★ 2026-08-15 审计P2修复：partial 动态判定（不再写死 False）——
                    #   本次平仓量 < 主行开仓量 → 部分平仓，明细须标 partial 供审计区分。
                    _is_partial = _ev > 0 and float(t.volume or 0) > 0 and _ev < float(t.volume or 0) - 0.005
                    _txs.append(TradeExit(
                        trade_id=t.id, user_id=t.user_id, mt5_account_id=t.mt5_account_id,
                        mt5_ticket=t.mt5_ticket, action=t.action,
                        exit_volume=round(_ev, 2), exit_price=_cp,
                        profit=round(_pf, 2), net_profit=round(_pf, 2),
                        result=t.result, exit_reason="mt5_closed_external",
                        partial=_is_partial, exit_time=_now,
                    ))
                except Exception:
                    pass
                _reconciled += 1
            # ★ 2026-08-11 诊断日志：对账 deal 匹配率（防"假盈亏"再静默）
            if _reconciled:
                logger.info(
                    f"[反向对账] {self.account_id[:8]} 补平 {_reconciled} 笔，"
                    f"deal 未匹配 {_deal_miss} 笔（标 pending_verify，不假造盈亏）"
                )
            if _reconciled:
                # 落库：将补平改动真实合并进独立 session 并提交。
                # 关键修复：原实现传 lambda db: None 是空操作，t.close_price 等修改从不落库，
                # 导致幽灵单每轮重复出现（之前误判为 DB 只读，实为未 commit）。DB 只读时
                # _safe_db_write 仍静默失败，下一轮再补，不阻塞交易。
                _to_close = [t for t in open_trades if str(t.mt5_ticket) not in live_tickets]
                for t in _to_close:
                    self.db.expunge(t)  # 从主 session 分离，避免跨 session 冲突
                self._safe_db_write(
                    lambda db: [db.merge(t) for t in _to_close] + [db.add(x) for x in _txs],
                    label="反向对账落库",
                )
                logger.warning(
                    f"[反向对账] {self.account_id[:8]} 发现 {_reconciled} 笔本地开仓但MT5已平"
                    f"（外部平仓/结算），已补平并落库"
                )
            _RECON_OK[self.account_id] = True
            # ★★ 2026-08-11 P0 补强：pending_verify 自动回填（冷启动竞态自愈）。
            #   背景：worker 重启瞬间（MT5 历史未同步）对账抢跑 → 外部平仓 deal 拉不到
            #   → 标 pending_verify。这类单 close_time 已非空、不进 open_trades，
            #   若不做后续重扫，pending 永远挂账、真实盈亏丢失。
            #   修复：每轮对账完成后，把本账号 pending_verify 单再拉一次 deal，
            #   拉到真实盈亏即回填（result 恢复 win/loss），拉不到保持 pending（不假造）。
            self._rescan_pending_verify()
            return True
        except Exception as _e:
            # 对账过程异常同样意味着"账本状态未知" → 保守拒绝开新仓，但不影响持仓管理
            _RECON_OK[self.account_id] = False
            # ★ 2026-08-11 加 traceback 定位 str 异常真实源头（不再静默吞）
            import traceback as _tb
            logger.warning(f"[反向对账] 异常(不阻塞持仓管理): {_e}\n{_tb.format_exc()}")
            return False

    def _rescan_pending_verify(self) -> None:
        """★ 2026-08-11 P0 补强：pending_verify 单自动回填真实盈亏。

        触发：每轮 _reconcile_positions 完成后。
        范围：本账号 result='pending_verify' 的已平仓单。
        动作：逐个按 ticket 精准拉 deal（get_deal_by_position）→ 拉到即回填
              close_price/profit/result（win/loss/breakeven），exit_reason 标
              'mt5_closed_external_verified'；仍拉不到保持 pending（绝不假造）。
        失败全吞：回填是补账，绝不阻断交易链。
        """
        try:
            from app.models.trade_exit import TradeExit
            # ★★ 2026-08-17 P0 修复：ORDER BY close_time DESC。
            #   原实现无排序 → rowid 恒取最早插入的 20 笔 = 老幽灵单（08-11/08-12 拉不到 deal），
            #   今天新平仓的单永远排不上队（真实盈亏永久丢失，实测 96 笔 pending 积压）。
            #   新单优先回填：今天平仓的单 deal 必在 MT5 历史窗口内，先扫先命中。
            _pend = self.db.query(Trade).filter(
                Trade.mt5_account_id == self.account_id,
                Trade.result == "pending_verify",
                Trade.close_time.isnot(None),
            ).order_by(Trade.close_time.desc()).limit(20).all()
            if not _pend:
                return
            # ★★ 2026-08-17 P0 修复：节流过滤（幽灵单降频，不刷屏/不阻塞周期）。
            #   同一 ticket 距上次尝试 <600s 直接跳过——拉不到 deal 的旧单不再每轮重扫占坑，
            #   把名额让给真正可回填的新单。进程内跨实例共享（与 _PARTIAL_DONE 同款）。
            _now_ts = time.time()
            with _PENDING_RESCAN_LOCK:
                _pend = [t for t in _pend
                         if _now_ts - _PENDING_RESCAN_TS.get(
                             (self.account_id, t.mt5_ticket), 0) >= _PENDING_RESCAN_TTL]
                if not _pend:
                    return
                for t in _pend:
                    _PENDING_RESCAN_TS[(self.account_id, t.mt5_ticket)] = _now_ts
            # ★ 2026-08-12 可观测性补强：入口留痕。
            #   本函数原先全程静默（拉不到 deal 就 continue、_fills 空就 return），
            #   导致排障时无法区分「函数未被调用」与「调用了但 deal 拉不到」——
            #   本次 P0 因此连续误判 3 轮。入口日志是定位调用链断点的最低成本手段。
            logger.info(
                f"[反向对账] {self.account_id[:8]} pending_verify 待回填 {len(_pend)} 笔 → 开始重扫")
            _fills = []   # [(trade, cp, pf)]
            _bad_ticket = []   # ticket 无效、物理上无法验证的记录
            for t in _pend:
                try:
                    # ★ 2026-08-12 补强：ticket 无效（空串/0/None）时物理上无法按 position 查 deal，
                    #   继续留在 pending_verify 会永久占用重扫名额(limit 20)，挤掉真正可回填的单。
                    #   标记为 unverifiable 明确「无法验证」——不是伪造盈亏，符合「绝不假造」原则。
                    #   根因是开仓路径未写入 mt5_ticket，需单独排查（本函数只负责不被其拖死）。
                    _tk = t.mt5_ticket
                    if _tk is None or str(_tk).strip() in ("", "0", "None"):
                        t.result = "unverifiable"
                        t.exit_reason = "no_ticket_cannot_verify"
                        _bad_ticket.append(t)
                        continue
                    # ★ 2026-08-12 P0 根治：pending_verify 单已写入 close_time（≈真实平仓时间），
                    #   传它让 worker 用窄窗口精准命中平仓成交并回填真实盈亏。
                    _pd = mt5_service.get_deal_by_position(self.account_id, _tk, close_time=t.close_time, open_price=t.open_price, action=t.action)
                    # ★ 2026-08-11 兼容：_pd 可能是 dict 也可能是 str（旧 worker 异常时返回字符串），
                    #   任何非 dict 形态都按拉不到处理，保持 pending，不阻断。
                    if not isinstance(_pd, dict):
                        continue
                    _dl = _pd.get("deal")
                    if not isinstance(_dl, dict):
                        continue
                    if not _dl:
                        continue  # 仍拉不到 → 保持 pending，下轮再试
                    _cp = float(_dl.get("price") or _dl.get("close_price") or 0) or 0
                    # ★ #9 net_profit 含佣优先（含佣/已实现结算），缺失时回退 MT5 deal profit
                    _pf = float(_dl.get("net_profit") or _dl.get("profit") or 0) or 0
                    # ★ 2026-08-13 审计补全(P1-#4 对称)：REAL 券商平仓价 price=0 但 profit 真实
                    #   → 用 open ± profit/手数 反推真实平仓价（与 _record_close reconcile 路径一致）。
                    if _cp == 0 and _pf != 0:
                        _cp_vol = float(_dl.get("volume") or t.volume or 0) or 0
                        if _cp_vol > 0 and t.open_price:
                            _cp_move = _pf / (_cp_vol * 100.0)
                            _cp = round(
                                (t.open_price + _cp_move) if str(t.action).upper() == "BUY"
                                else (t.open_price - _cp_move), 2)
                    # ★ 2026-08-13 审计(P1)：REAL 保本平仓 price=0 且 profit=0 → 平仓价=开仓价(近似保本)，
                    #   不再永久 pending_verify（deal 已命中、盈亏确为0，可推导）。
                    if _cp == 0 and _pf == 0:
                        _cp = round(float(t.open_price or 0), 2)
                    t.close_price = round(_cp, 2)
                    t.profit = round(_pf, 2)
                    t.net_profit = round(_pf, 2)
                    t.result = "win" if _pf > 0 else ("loss" if _pf < 0 else "breakeven")
                    t.exit_reason = "mt5_closed_external_verified"
                    _fills.append((t, round(_cp, 2), round(_pf, 2)))
                except Exception:
                    continue
            # 无效 ticket 记录单独落库（与可回填单解耦，避免互相拖累）
            if _bad_ticket:
                try:
                    for _bt in _bad_ticket:
                        self.db.expunge(_bt)
                    self._safe_db_write(
                        lambda db: [db.merge(_bt) for _bt in _bad_ticket],
                        label="pending标记-无效ticket",
                    )
                    logger.warning(
                        f"[反向对账] {self.account_id[:8]} {len(_bad_ticket)} 笔无 mt5_ticket "
                        f"→ 标记 unverifiable（开仓路径未写票号，需单独排查）: "
                        + ", ".join(f"vol={_bt.volume} close={str(_bt.close_time)[:19]}"
                                    for _bt in _bad_ticket))
                except Exception as _be:
                    logger.warning(f"[反向对账] 无效ticket标记失败(不影响交易): {_be}")
            if not _fills:
                return
            # 同步 trade_exits 明细（按 mt5_ticket 更新对应 pending 行）
            for t, cp, pf in _fills:
                try:
                    self._safe_db_write(
                        lambda db, _t=t, _cp=cp, _pf=pf: db.query(TradeExit)
                        .filter(TradeExit.mt5_ticket == _t.mt5_ticket,
                                TradeExit.result == "pending_verify")
                        .update({"exit_price": _cp, "profit": _pf, "net_profit": _pf,
                                 "result": _t.result,
                                 "exit_reason": "mt5_closed_external_verified"},
                                synchronize_session=False),
                        label="pending回填-明细",
                    )
                except Exception:
                    pass
            # 主 trades 行合并落库
            for t, _, _ in _fills:
                self.db.expunge(t)
            self._safe_db_write(
                lambda db: [db.merge(t) for t, _, _ in _fills],
                label="pending回填-主行",
            )
            logger.info(
                f"[反向对账] {self.account_id[:8]} pending_verify 回填 {len(_fills)} 笔: "
                + ", ".join(f"#{t.mt5_ticket} {t.result} {t.profit:+.2f} @{t.close_price}"
                            for t, _, _ in _fills)
            )
        except Exception as _e:
            logger.warning(f"[反向对账] pending_verify 重扫跳过(不影响交易): {_e}")

    def _reconcile_against_leader(self):
        """★ 2026-08-06 修复主副仓不同步：跟号对照主号真实持仓，清理主号已平但跟号残留的孤儿单。

        机制：跟号 copy_order 时把主号票号写入 comment（WXAI-L{leader_ticket}）。
        本方法读取跟号当前所有 XAUUSD 持仓，提取 leader_ticket，再拉取主号当前真实持仓。
        若某 leader_ticket 已不在主号持仓中，则跟号对应持仓为孤儿单 → 立即市价平仓。
        这是信号塔广播漏跟/跟号独立硬止损提前离场后的最终兜底，确保主副仓保持一致。
        节流：每账号每 30s 最多跑一次（避免高频扫描）。
        """
        if not getattr(self, "_follow_leader", True):
            return
        leader = self._leader_account()
        if leader is None:
            return
        # ★ 2026-08-06 安全护栏：主号自身不对自己做对账——其原始持仓无 L{ticket} 标记，
        #   会被误判为孤儿单全部平仓（灾难性）。仅跟号需要对照主号清孤儿单。
        if leader.id == self.account_id:
            return
        _now = time.time()
        _key = f"recon_leader:{self.account_id}"
        if _now - _RECON_LAST.get(_key, 0) < 30:
            return
        _RECON_LAST[_key] = _now
        try:
            # ★ 2026-08-07 Phase 1 致命修复：这两处查询的结果会被直接用来**平仓**，
            #   必须能分辨"查询失败"与"真的空仓"。旧的 get_positions() 两种情况都返回 []：
            #   一旦主号 Worker 掉线/超时，leader_tickets 变成空集合，跟号每一笔带
            #   L{ticket} 标记的持仓都会被判成孤儿单 → 全部市价平仓。
            #   即"主号抖一下，跟号被清仓"，是真金白银的损失。
            #   702 行那条护栏（主号不对自己对账）是同源问题的另一半，这里补上另一半。
            _f_ok, follower_positions = mt5_service.get_positions_checked(
                self.account_id, "XAUUSD")
            if not _f_ok:
                logger.warning(
                    f"[主副对账] {self.account_id[:8]} 跟号持仓查询失败 → 本轮跳过，不平任何仓")
                return
            if not follower_positions:
                return
            _l_ok, leader_positions = mt5_service.get_positions_checked(leader.id, "XAUUSD")
            if not _l_ok:
                logger.warning(
                    f"[主副对账] {self.account_id[:8]} 主号({leader.account_id[:8]})持仓查询失败 → "
                    f"本轮跳过。查询失败只说明「不知道主号有什么」，不等于「主号什么都没有」，"
                    f"据此平仓等于拿噪声当事实"
                )
                return
            leader_tickets = {str(p.get("ticket", "")) for p in leader_positions}

            # ★ 2026-08-13 根治「主号持仓实时查询瞬断 → 跟号被误判孤儿全平」：
            #   实时 get_positions_checked 在【部分平仓结算窗口 / worker 持仓快照滞后】
            #   可能 ok=True 却漏返主号仍在场的票号（实证事故：23:10:14 主号锁50%部分
            #   平仓，23:10:37 跟号对账时主号 382748246 实时查询未返回 → 跟号被全平，
            #   而主号该票 close_time 始终为 NULL 仍开仓）。
            #   以 DB trades 表（close_time 是否为空）作为【权威交叉验证】：
            #     · 主号该票 DB 仍 close_time IS NULL（主号确未平）→ 实时查询瞬断，
            #       跟号不是孤儿 → 跳过平仓（fail-safe）；
            #     · 主号该票 DB 确已平（close_time 非空）→ 真孤儿 → 平仓；
            #     · DB 查询本身异常 → 同样 fail-safe 跳过，绝不误平。
            #   原则：宁可漏平（下次节流重扫会再判），绝不因单次实时读不可见而误平跟号。
            def _leader_open_in_db(lt: str):
                """True=主号该票DB仍开仓；False=主号已平/无记录；None=DB异常(应fail-safe)。"""
                try:
                    rec = self.db.query(Trade).filter(
                        Trade.mt5_account_id == leader.id,
                        Trade.mt5_ticket == str(lt),
                    ).first()
                    if rec is None:
                        return False  # 主号无此成交 → 不是主号开的 → 可判孤儿
                    return rec.close_time is None
                except Exception as _dbe:
                    logger.warning(
                        f"[主副对账] {self.account_id[:8]} DB交叉验证异常(票L{lt}): {_dbe} → fail-safe 跳过")
                    return None

            # 分组：按 leader_ticket 统计跟号持仓
            by_leader: dict = {}
            no_leader = []
            for p in follower_positions:
                cm = str(p.get("comment") or "")
                m = re.search(r"L(\d+)", cm)
                if not m:
                    no_leader.append(p)
                    continue
                lt = m.group(1)
                by_leader.setdefault(lt, []).append(p)

            orphans = []
            # ① 主号实时查询无对应票号 → 先用 DB 交叉验证，避免瞬断误平
            for lt, ps in by_leader.items():
                if lt in leader_tickets:
                    continue
                _db_open = _leader_open_in_db(lt)
                if _db_open is True:
                    logger.warning(
                        f"[主副对账] {self.account_id[:8]} 票L{lt} 实时查询未返回但DB主号仍开仓"
                        f" → 判定瞬断，跳过平仓(fail-safe)")
                    continue
                if _db_open is None:
                    continue  # DB 异常 → fail-safe 跳过
                # _db_open is False → 主号确已平（DB为证）→ 真孤儿
                orphans.extend(ps)
            # ② 同一 leader_ticket 被复制了多笔 → 仅当主号实时确有该票才判重复
            #   （主号实时无该票则归①，由 DB 交叉验证决定是否孤儿，避免重复路径绕过验证）
            duplicates = []
            for lt, ps in by_leader.items():
                if lt in leader_tickets and len(ps) > 1:
                    # 保留：盈利最高（或开仓时间最新）的一笔；其余为重复复制
                    sorted_ps = sorted(ps, key=lambda x: (float(x.get("profit", 0) or 0), x.get("time", 0)), reverse=True)
                    duplicates.extend(sorted_ps[1:])
            # ③ 无 leader_ticket 标记的非跟单持仓：跟号正常复制应带 L{ticket}。
            #   若标记缺失，先用 DB 验证本账号是否确有该成交开仓记录——有则只是标记
            #   丢失（不能平），无才是真 stray（独立账号手动机才会有的孤儿）。
            if no_leader:
                _real_stray = []
                for p in no_leader:
                    try:
                        _rec = self.db.query(Trade).filter(
                            Trade.mt5_account_id == self.account_id,
                            Trade.mt5_ticket == str(p.get("ticket")),
                            Trade.close_time.is_(None),
                        ).first()
                    except Exception:
                        _rec = True  # 查询异常 → 假定有记录 → fail-safe 不平
                    if _rec is None:
                        _real_stray.append(p)
                    else:
                        logger.warning(
                            f"[主副对账] {self.account_id[:8]} 票{p.get('ticket')} 无L标记但DB有开仓记录"
                            f" → 视为标记丢失，跳过平仓(fail-safe)")
                if _real_stray:
                    logger.warning(
                        f"[主副对账] {self.account_id[:8]} 发现 {len(_real_stray)} 笔无标记真孤儿(stray)持仓")
                    orphans.extend(_real_stray)

            to_close = orphans + duplicates
            if to_close:
                reason = "孤儿单" if orphans else "重复跟单"
                logger.warning(
                    f"[主副对账] {self.account_id[:8]} 发现 {len(orphans)} 笔孤儿单 + "
                    f"{len(duplicates)} 笔重复跟单（主号{leader.account_id[:8]}）→ 市价平仓"
                )
                for p in to_close:
                    try:
                        cr = mt5_service.close_position(self.account_id, p["ticket"])
                        if "error" not in cr:
                            self._record_close(None, p, cr, f"主副对账-清{reason}", partial=False)
                            logger.info(
                                f"[主副对账] ticket={p['ticket']} 已平 P/L={float(p.get('profit',0)):+.2f}"
                            )
                        else:
                            logger.error(f"[主副对账] ticket={p['ticket']} 平仓失败: {cr.get('error')}")
                    except Exception as _oe:
                        logger.warning(f"[主副对账] ticket={p.get('ticket')} 异常: {_oe}")
        except Exception as _e:
            logger.warning(f"[主副对账] 异常(不影响主流程): {_e}")

    # ────────────────────────────────────────────────────────────────────────
    # ★ 决策质量门控（加法型软门，绝不破坏既有【策略风控】）
    #   ② 体制门 regime_open_mode：仅在 strong 趋势体制放开开单
    #   ③ 空头约束 short_guard_mode：除非体制转空 + 反转哨兵确认，否则不放空
    #   设计铁律：默认 soft（提准非拦截）；hard 才硬拦；off 全放开。
    #   仅作用于「主号 / 独立账号」的真实 AI 决策开单（跟号不跑 execute_cycle，
    #   直接镜像主号已被门控过的信号，故不存在双重门控）。
    # ────────────────────────────────────────────────────────────────────────
    def _apply_decision_gates(self, ai_decision, market_data: dict) -> dict:
        """返回:
          passed:          是否放行（hard 模式 false=硬拦截，不开新仓）
          min_conf_penalty:soft 模式需额外抬升的置信门槛（叠加到 min_confidence，
                            若 AI 置信仍够则照常开，不够则自然落入"置信不足"分支）
          block_reason:    hard 拦截原因
          detail:          审计说明
        """
        decision = (getattr(ai_decision, "decision", None) or "HOLD").upper()
        # 跟号镜像场景下决策可能为 None，直接放行（不该发生，防御性）
        if decision not in ("BUY", "SELL"):
            return {"passed": True, "min_conf_penalty": 0.0, "block_reason": "", "detail": "非开仓决策，门控跳过"}

        # 读取本账号（或继承主号）的质量门控配置；缺省 soft（提准非拦截，向后兼容）
        rmode = str(self._fresh_strat("regime_open_mode", "soft") or "soft").lower()
        smode = str(self._fresh_strat("short_guard_mode", "soft") or "soft").lower()

        reg = ((market_data or {}).get("regime") or {}).get("regime", "unknown")
        is_strong = reg in ("trend_up", "trend_down")   # 强趋势体制
        is_bearish = reg == "trend_down"
        is_bullish = reg == "trend_up"

        # 体制末端极值（山顶/谷底）——来自 regime_detect，与反转哨兵同源
        _reg_d = (market_data or {}).get("regime") or {}
        at_top = bool(_reg_d.get("at_stale_top", False))
        at_bottom = bool(_reg_d.get("at_stale_bottom", False))

        # 反转哨兵：纯行情、全局共享，本地重算（快照已含 smc_features）
        try:
            from app.core.reversal_sentinel import evaluate as _sentinel_eval
            sentinel = _sentinel_eval(market_data) or {}
        except Exception as _se:
            logger.warning(f"[{self.account_id[:8]}] 反转哨兵重算失败(门控降级放行): {_se}")
            sentinel = {}
        s_sig = (sentinel.get("signal") or "NONE").upper()
        s_conf = float(sentinel.get("confidence") or 0.0)

        penalty = 0.0
        block_reason = ""
        notes = []

        # ===== ② 体制门 =====
        if rmode == "off":
            notes.append("体制门=off(不限制)")
        elif rmode == "soft":
            if not is_strong:
                # ★ 2026-08-07 调研修正（CapTradeAI 实证：震荡市仅 +2% 门槛；海外共识交易：
                #   三模型共振=最高conviction，应豁免体制惩罚而非加罚）。
                #   原 +0.08 偏严，且与"多交易多赚钱"铁律冲突 → 共振豁免、非共振仅 +0.03。
                if getattr(ai_decision, "chronos_agree", False):
                    notes.append(f"体制门(soft): 三模型共振豁免区间整理惩罚(共识即放行)")
                else:
                    penalty += 0.03
                    notes.append(f"体制门(soft): 体制={reg}非强趋势→开仓置信门槛+0.03(提准非拦截·已放宽)")
        elif rmode == "hard":
            # ★ 重设计（调研精髓·提准非拦截）：
            #   只硬拦「明显接飞刀 / 逆势」单，震荡(均值回归)双向放行，保护交易笔数。
            #   ① 山顶(REVERSE_SELL/at_stale_top)追BUY = 接飞刀 → 硬拦
            #   ② 下跌趋势中抄底BUY(除非确认谷底反转) = 接落刀 → 硬拦
            #   ③ 谷底(REVERSE_BUY/at_stale_bottom)追SELL = 接刀 → 硬拦
            #   ④ 上涨趋势中摸顶SELL(除非确认山顶反转) = 接飞刀 → 硬拦
            #   震荡/确认的逆向反转点 → 放行（多交易）
            is_valley_rev = (s_sig == "REVERSE_BUY") or at_bottom
            is_mountain_rev = (s_sig == "REVERSE_SELL") or at_top
            if decision == "BUY":
                if is_mountain_rev:
                    block_reason = f"体制门(hard): 山顶/反转哨兵REVERSE_SELL确认，禁止BUY接飞刀"
                    notes.append(block_reason)
                elif is_bearish and not is_valley_rev:
                    block_reason = f"体制门(hard): 强空头体制(trend_down)非谷底反转，禁止逆势BUY抄底"
                    notes.append(block_reason)
                else:
                    notes.append(f"体制门(hard): BUY放行(趋势向上/震荡/确认谷底反转)")
            else:  # SELL
                if is_valley_rev:
                    block_reason = f"体制门(hard): 谷底/反转哨兵REVERSE_BUY确认，禁止SELL接刀"
                    notes.append(block_reason)
                elif is_bullish and not is_mountain_rev:
                    block_reason = f"体制门(hard): 强多头体制(trend_up)非山顶反转，禁止逆势SELL摸顶"
                    notes.append(block_reason)
                else:
                    notes.append(f"体制门(hard): SELL放行(趋势向下/震荡/确认山顶反转)")

        # ===== ③ 空头约束（仅当决策为 SELL 时生效）=====
        if decision == "SELL":
            if smode == "off":
                notes.append("空头约束=off(不限制)")
            elif smode == "soft":
                # ★ 2026-08-15 审计P1修复：去除 SELL 基线 handicap(+0.08 when not bearish)。
                #   该基线使 SELL 系统性比 BUY 多罚≈0.08→方向不对称压制空头盈利单，违背「提准非拦截」。
                #   保留真接飞刀惩罚(REVERSE_BUY谷底/上涨延伸+RSI)，符合2026-07-21方法论
                #   (延伸度+RSI 区分健康回踩与趋势末端接刀，而非 blanket 压制空头)。
                if s_sig == "REVERSE_BUY":
                    # 哨兵判定处于趋势末端谷底 → 顺势追 SELL=接刀，加重惩罚
                    penalty += 0.10
                    notes.append(f"空头约束(soft): 反转哨兵=REVERSE_BUY(谷底警告)→SELL 再+0.10")
                # ★ 2026-08-07 实盘修复：价格已处于H1均线上方延伸且RSI偏强，
                #   说明上涨动能仍在，此时SELL属于逆势摸顶，额外+0.12门槛。
                _rg = (market_data or {}).get("regime") or {}
                _ext_z = float(_rg.get("extension_z", 0.0) or 0.0)
                _rsi = float(_rg.get("rsi_h1", 50.0) or 50.0)
                if _ext_z > 0.5 and _rsi > 55:
                    penalty += 0.12
                    notes.append(f"空头约束(soft): 价格处于上涨延伸(z={_ext_z:.2f},rsi={_rsi:.0f})→SELL 再+0.12")
            elif smode == "hard":
                # 空头约束 hard：谷底/REVERSE_BUY 接刀是最差单，任何体制都硬拦；
                #           强多头体制(trend_up)硬拦；震荡均值回归/顺势下跌放行（多交易）。
                if s_sig == "REVERSE_BUY" or at_bottom:
                    block_reason = "空头约束(hard): 反转哨兵=REVERSE_BUY/谷底确认，禁止SELL接刀(最差单)"
                    notes.append(block_reason)
                elif is_bullish:
                    block_reason = "空头约束(hard): 强多头体制(trend_up)禁止SELL"
                    notes.append(block_reason)
                else:
                    notes.append(f"空头约束(hard): SELL放行(震荡均值回归/顺势下跌)")

        passed = block_reason == ""
        if passed and penalty > 0:
            notes.append(f"软门总惩罚={penalty:.2f}(叠加到开仓置信门槛)")
        return {
            "passed": passed,
            "min_conf_penalty": round(penalty, 3),
            "block_reason": block_reason,
            "detail": "; ".join(notes) if notes else "无门控",
        }

    def execute_cycle(self) -> dict:
        # ── 大脑审计：记录执行器消费（开单/智慧仓位实际落地）──
        try:
            from app.services.brain_audit import record as _ba_rec_te
        except Exception:
            _ba_rec_te = None
        """执行一个完整 AI 决策 → 交易周期"""
        result = {
            "timestamp": datetime.now().isoformat(),
            "account_id": self.account_id,
            "decision": None,
            "orders": [],
            "errors": [],
            "placed": False,
            "signal": None,
        }

        # ★ 2026-08-17 持仓感知：暴露本账号当前持仓状态给 auto_loop 的间隔计算。
        #   有仓 → 决策/管仓间隔压缩到 30s（管仓优先，推理跟上行情变化）；
        #   空仓 → 保持平静拉长（60s）省云消耗、专注找开仓机会。
        #   失败静默置 False（最坏=按空仓节奏，不影响交易正确性，绝不因本字段抛异常）。
        try:
            result["has_positions"] = bool(
                mt5_service.get_positions(self.account_id, symbol="XAUUSD")
            )
        except Exception:
            result["has_positions"] = False

        # ★ 单轮幂等：本轮持仓管理尚未执行。必须在 try 之前重置——
        #   放进 try 里的话，一旦上一轮留下 True 且本轮在重置前抛异常，
        #   这一轮的持仓保护就会被守卫整个跳过（止损止盈静默失效）。
        self._pm_cycle_done = False

        try:
            # ★ E0：人工紧急处置窗口（Phase 0）——排在所有闸门最前面，先于数据库查询。
            #   理由：E1 需要查库，而人工停止必须在"数据库都挂了"的时候依然生效。
            #   铁律6：MANUAL_HALT(HALT_NEW / HALT_ALL) 只"关水龙头"(拒新开仓)，
            #   不"抽走桶里的水"——SL/TP/SmartExit 等保护性持仓管理照常运行，否则持仓裸奔。
            _open_ok, _halt_why = emergency.allow_open(self.account_id)
            if not _open_ok:
                result["errors"].append(_halt_why)
                # 保护性持仓管理继续跑（止损止盈不能停，铁律6）
                try:
                    _ai = self.debate_engine.decide(debate_rounds=1, account_id=self.account_id)
                    self._manage_positions(_ai)
                    result["errors"].append("MANUAL_HALT：已有持仓的止损/止盈/追踪保护继续运行（铁律6）")
                except Exception as _me:
                    result["errors"].append(f"MANUAL_HALT 下持仓保护异常: {_me}")
                logger.warning(f"[{self.account_id[:8]}] {_halt_why}")
                return result

            # E1：账号交易开关检查——用户在前端'停交易'后，禁止任何开仓/平仓动作
            # 覆盖 自动循环 / 手动 /execute / 手动 /order 全部入口（execute_cycle 是统一执行体）
            _acc = self.db.query(MT5Account).filter(
                MT5Account.id == self.account_id
            ).first()
            if _acc and not _acc.is_trading_enabled:
                result["errors"].append("该账号已停用交易（前端'停交易'），本轮不执行任何下单/平仓")
                return result

            # ★ 反向对账（每轮第一步，先于一切决策）：把 MT5 真实持仓同步回本地账本，
            #   根治「本地还以为有单、实际已被外部平掉」的失明 → 防止基于过期账本重复下单。
            _ledger_ok = self._reconcile_positions()

            # ★ 2026-08-06 主副仓兜底对账：跟号对照主号真实持仓，清理主号已平但跟号残留的
            #   孤儿单 / 重复跟单（广播漏跟或跟号独立硬止损提前离场后的最终兜底，确保主副一致）。
            #   放在 _reconcile_positions 之后、冷却期之前，保证每轮都跑（独立于开不开仓）。
            self._reconcile_against_leader()

            # ★ 2026-08-07 Phase 1（V6 §5.4）：对账未通过 ⇒ 不得进入决策。
            #   "在不知道自己真实持仓的情况下做决策"正是「有的开了有的没开」的结构性来源。
            #   对交易频率无实质损失：对账失败的成因是 MT5 查询失败，而下单走同一条管道，
            #   此时本来也下不出去——挡掉的是账本污染，不是成交机会。
            if not _ledger_ok:
                result["errors"].append("持仓对账未通过：本轮不开新仓（已有持仓的保护继续）")
                try:
                    # 先确认真有仓可管，避免 MT5 掉线时每轮白烧一次云 AI 调用
                    _pos_ok, _pos = mt5_service.get_all_positions_checked(self.account_id)
                    if _pos_ok and _pos:
                        _ai = self.debate_engine.decide(
                            debate_rounds=1, account_id=self.account_id)
                        self._manage_positions(_ai)
                except Exception as _pe:
                    result["errors"].append(f"对账未通过分支下持仓保护异常: {_pe}")
                return result

            # E2：亏损冷却期 — 防止报复性交易（调研支撑：algomatrix.trade 推荐3根K线冷却）
            # 单亏 → 跳过 1 周期(60s)；连亏 3+ → 跳过 3 周期(180s)
            cooldown = self._check_loss_cooldown()
            if cooldown:
                result["errors"].append(cooldown)
                # 仍执行持仓管理（可能需要止损/止盈），只是不开新仓
                try:
                    ai_decision = self.debate_engine.decide(debate_rounds=1, account_id=self.account_id)
                    self._manage_positions(ai_decision)
                except Exception:
                    pass
                return result

            # Step 1: AI 辩论决策（注入主号持仓，让 AI 看见账本再投票）
            debate_rounds = getattr(settings, 'AI_DEBATE_ROUNDS', 2) or 2
            ai_decision = self.debate_engine.decide(debate_rounds=debate_rounds, account_id=self.account_id)
            result["decision"] = {
                "action": ai_decision.decision,
                "confidence": ai_decision.confidence,
                "deepseek_vote": ai_decision.deepseek_vote,
                "hunyuan_vote": ai_decision.hunyuan_vote,
                "weights": f"DS:{ai_decision.deepseek_weight:.2f}/HY:{ai_decision.hunyuan_weight:.2f}",
                "risk_level": ai_decision.risk_level,
                "summary": ai_decision.reasoning_summary,
            }

            # ★ 决策质量门控（加法型软门，绝不破坏既有【策略风控】）：
            #   ② 体制门（仅 strong 趋势放开开单） ③ 空头约束（体制转空+哨兵确认才放空）
            #   在 min_confidence 检查之前施加；取一次市场快照供门控 + 后续 ATR 复用。
            gate_snap = self.debate_engine.market.get_market_snapshot()

            # ★★ 2026-08-17 篮子级 AI 持仓管理：AI 已确认平仓处置（close_all/trim 连续2轮）
            #   → 本轮先执行持仓处置，跳过开新仓（杜绝"AI 喊平仓又同轮开新仓"的平了又开）
            _bask_a = str(getattr(ai_decision, "basket_action", "hold") or "hold")
            _bask_confirmed = bool(getattr(ai_decision, "basket_action_confirmed", False))
            if _bask_a in ("close_all", "trim") and _bask_confirmed:
                result["errors"].append(
                    f"AI 篮子处置 {_bask_a} 已确认（conf={ai_decision.basket_action_conf:.0%}）"
                    f" → 本轮执行持仓处置，跳过开新仓"
                )
                logger.warning(
                    f"[篮子AI处置·开仓守卫] {self.account_id[:8]} {_bask_a} 确认 → "
                    f"先处置持仓不开新仓（{getattr(ai_decision, 'basket_action_reason', '')[:60]}）"
                )
                self._manage_positions(ai_decision)
                return result

            gate = self._apply_decision_gates(ai_decision, gate_snap)
            result["gate_detail"] = gate.get("detail", "")
            if not gate["passed"]:
                # hard 模式：明确拦截，不开新仓，但仍管理现有持仓（止损/止盈/保本）
                result["errors"].append(f"决策质量门控拦截: {gate['block_reason']}")
                logger.info(f"[{self.account_id[:8]}] 决策质量门控(hard)拦截: {gate['detail']}")
                self._manage_positions(ai_decision)
                # ★ 2026-08-06 修复：门控拦截只拦截开新仓，不拦截反向持仓平仓。
                #   若AI已给出BUY/SELL方向，现有反向仓必须立即平掉，避免主号持有反向单而跟号已平的不一致。
                self._close_opposite_for_decision(ai_decision)
                return result
            # soft 模式：将惩罚叠加到开仓置信门槛（提准非拦截）；off 模式惩罚为 0
            min_conf_penalty = gate.get("min_conf_penalty", 0.0) or 0.0
            if min_conf_penalty > 0:
                logger.info(f"[{self.account_id[:8]}] 决策质量门控(soft)提准: {gate['detail']}")

            # 置信度不足 → 不开新仓，但仍需管理现有持仓（止损/止盈/保本）
            # ★ 2026-08-05 修正（用户实盘教训：BUY惩罚导致强势上涨中2小时不开单）：
            #   旧逻辑(R4): "BUY 胜率30% < SELL 57% → BUY 额外+0.05"
            #   问题：①这是前置过滤（违反铁律"提准非拦截"）②用历史胜率拦当前AI决策=越跑越笨
            #   修正：BUY/SELL 一视同仁，方向选择权完全交给 AI 模型。
            #   基础门槛保持 0.65（与 MetaAgent SPLIT 阈值对齐），不做任何方向性加成。
            base_min_conf = float(self._fresh_strat("min_confidence", settings.RISK_MIN_CONFIDENCE) or settings.RISK_MIN_CONFIDENCE)
            min_confidence = base_min_conf + min_conf_penalty  # ★ 叠加决策质量门控软惩罚（提准非拦截）
            # ★★ 2026-08-17 盯盘 P0 修复：lean 放行此前是"假放行"——打日志后控制流仍落进
            #   下方 rejected/return 分支，导致"lean 放行日志出现但 0 成交"（14:03:00 cycle#10 实测：
            #   BUY 0.585 打"lean 路径放行"后无下单、无 rejected 记录，信号被静默吞掉）。
            #   根因：L1828 的 if 只打日志不改变控制流，L1928 的 return 无差别执行。
            #   修复：_lean_pass 标记 + 下方跳过被拒记录/return，真正放行进入开仓流程。
            _lean_pass = False
            if ai_decision.confidence < min_confidence - 1e-9:  # ★ 浮点数epsilon避免精度陷阱
                # ★ 2026-08-15 倾斜单(lean)路径：方向明确但置信略低于门槛时，仍开仓，
                #   但手数随置信缩放（_calc_position_size 已按 signal_confidence 缩手数）。
                #   提准非拦截：不硬拦分歧信号，用"小仓"而非"不开"来管风险；既保多交易，
                #   又不接纯噪声。低于 LEAN_TRADE_MIN_CONF 才真正拦截（纯噪声/无方向）。
                _lean_floor = float(getattr(settings, "LEAN_TRADE_MIN_CONF", 0.42))
                if ai_decision.decision in ("BUY", "SELL") and ai_decision.confidence >= _lean_floor - 1e-9:
                    logger.info(
                        f"[开仓] lean 路径放行: 方向={ai_decision.decision} "
                        f"置信{ai_decision.confidence:.2f}∈[{_lean_floor:.2f},{min_confidence:.2f}) "
                        f"→ 手数随置信缩放(提准非拦截)"
                    )
                    # ★ 2026-08-17 修复：真正放行（置标记，跳过下方 rejected/return），
                    #   继续走开仓流程（_calc_position_size 按低置信自动缩手数）
                    _lean_pass = True
                else:
                    result["errors"].append(f"AI置信度不足({ai_decision.confidence:.2f}<{min_confidence})")
                # ★★ 2026-08-11 被拒信号记录（验证跟踪）：AI 给了方向但置信不足被拦 →
                #   记录到 rejected_signals 表，之后对照价格走势判断"这个信号准不准、
                #   哪个模型更准"，为调 min_confidence/权重积累数据（用户要求的工作流）。
                #   ★ 关键：必须确保写入成功——ORM 路径可能因 metadata 未注册
                #   （进程启动早于建表）失败，原生 sqlite3 路径带 busy_timeout 应对 Defender 写锁。
                #   双重 fallback：write_engine pool（带调优）→ 原生 sqlite3（最稳）。
                _rs_saved = False
                # ★★ 2026-08-17 盯盘修复：lean 真放行时跳过 rejected 记录（否则"放行却被记成被拒"，
                #   审计数据自相矛盾），并跳过下方 return 继续开仓流程。
                if _lean_pass:
                    _rs_saved = True  # 信号未真正拒绝，跳过记录
                # 路径1：write_engine pool（与 _safe_db_write 同样的连接池/调优）
                try:
                    from app.database import WriteSession
                    from app.models.rejected_signal import RejectedSignal
                    # ★ 2026-08-16 审计P0-2修复：旧代码引用 entry_price/atr 局部变量，
                    #   但它们在 execute_cycle 内 L1982/L1991 才赋值 → 此处 NameError 被
                    #   except 吞掉 → rejected_signals 表从未写入（审计闭环静默断链）。
                    #   改用 getattr(ai_decision) + gate_snap 兜底，恢复记录。
                    _rs_entry = 0.0
                    try:
                        _rs_entry = float(getattr(ai_decision, "entry_price", 0) or 0)
                    except (TypeError, ValueError):
                        _rs_entry = 0.0
                    _rs_atr = 0.0
                    try:
                        _snap_vol = (gate_snap or {}).get("volatility_metrics", {}) or {}
                        _rs_atr = float(_snap_vol.get("h1_atr") or _snap_vol.get("d1_atr") or 0)
                    except (TypeError, ValueError):
                        _rs_atr = 0.0
                    _rs = RejectedSignal(
                        mt5_account_id=self.account_id,
                        direction=ai_decision.decision,
                        confidence=float(ai_decision.confidence or 0),
                        min_confidence=float(min_confidence or 0),
                        deepseek_vote=getattr(ai_decision, "deepseek_vote", "") or "",
                        hunyuan_vote=getattr(ai_decision, "hunyuan_vote", "") or "",
                        chronos_dir=str(getattr(ai_decision, "ts_fusion_dir", "") or ""),
                        entry_price=_rs_entry,
                        atr=_rs_atr,
                        regime=str(getattr(ai_decision, "quality_regime", "") or ""),
                        reject_reason="confidence",
                    )
                    _rs_eng = WriteSession().get_bind()
                    _rs_eng.dispose()
                    # ★ 2026-08-16 审计终检修复：原无条件 `_rs_saved = True`——写失败(返回 False)
                    #   时 fallback 被跳过，rejected_signals 仍静默断链。改为按返回值赋值。
                    _rs_saved = self._safe_db_write(lambda db: [db.add(_rs)], label="被拒信号记录")
                except Exception:
                    pass
                # 路径2：原生 sqlite3（含 busy_timeout / journal_mode 调优）
                if not _rs_saved:
                    try:
                        import sqlite3 as _sqlite3_rs
                        import uuid as _uuid_rs
                        from app.database import engine as _rs_engine
                        # 复刻 _raw_creator 的调优（15s busy_timeout + WAL）
                        # ★ P2-#5 可移植修复：原写死 F:/WanxiangAI/... 绝对路径，违反可移植铁律
                        #   （客户机无 F 盘即失效）。统一走 settings.get_database_url() 推导。
                        _rs_db_url = settings.get_database_url()
                        _rs_db_path = _rs_db_url.replace("sqlite:///", "") if _rs_db_url.startswith("sqlite:///") else _rs_db_url
                        _rs_conn = _sqlite3_rs.connect(
                            _rs_db_path,
                            timeout=15, isolation_level=None,
                        )
                        _rs_conn.execute("PRAGMA journal_mode=WAL")
                        _rs_conn.execute("PRAGMA busy_timeout=15000")
                        _rs_conn.execute(
                            """INSERT OR IGNORE INTO rejected_signals
                               (id, mt5_account_id, direction, confidence, min_confidence,
                                deepseek_vote, hunyuan_vote, chronos_dir,
                                entry_price, atr, regime, reject_reason, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (str(_uuid_rs.uuid4()), self.account_id, ai_decision.decision,
                             float(ai_decision.confidence or 0), float(min_confidence or 0),
                             str(getattr(ai_decision, "deepseek_vote", "") or ""),
                             str(getattr(ai_decision, "hunyuan_vote", "") or ""),
                             str(getattr(ai_decision, "ts_fusion_dir", "") or ""),
                             # ★ 2026-08-16 审计终检修复：fallback 路径此前仍引用未定义的
                             #   entry_price/atr 局部变量（NameError 被吞 → 表从不写入），
                             #   与 ORM 路径同源改用 _rs_entry/_rs_atr。
                             _rs_entry, _rs_atr,
                             str(getattr(ai_decision, "quality_regime", "") or ""),
                             "confidence", datetime.now()),
                        )
                        _rs_conn.commit()
                        _rs_conn.close()
                        _rs_saved = True
                    except Exception:
                        pass
                # ★★ 2026-08-17 盯盘修复：lean 真放行 → 跳过 return 继续开仓流程。
                #   此时仍执行持仓管理（防漏护仓），但不 return。
                self._manage_positions(ai_decision)
                if _lean_pass:
                    # 真放行：持仓管理已做，继续走下方正常开仓路径（Step 2.5 反转/风控/手数/下单）
                    pass
                else:
                    # ★ 2026-08-06 修复：置信不足只拦截开新仓，不拦截反向持仓平仓。
                    #   跟号在trading.py里会按主号方向执行_close_opposite_for_decision；
                    #   主号若此处跳过，就会出现主号持仓与跟号不一致（如ticket 1610097175所见）。
                    self._close_opposite_for_decision(ai_decision)
                    return result

            # 决策为HOLD → 不交易，但检查现有持仓是否需要平仓
            if ai_decision.decision == "HOLD":
                self._manage_positions(ai_decision)
                return result

            # Step 2: 管理现有持仓（方向反转时平仓）
            self._manage_positions(ai_decision)

            # ★ Step 2.5: 反方向冲突即时平仓（毫秒级修复 2026-08-05）
            #   用户实盘教训：浮盈$10没及时平→行情反转→亏损$60。
            #   当 AI 决策方向与现有持仓相反时（如 BUY + 存在 SELL），这是明确的反转信号。
            #   原逻辑仅"拒绝开新仓"→对冲锁死；现改为"立即全平反方向仓+再开新仓"。
            #   不等 L2 防抖确认（防抖是防止假反转，但这里已有 AI 高置信决策做背书）。
            # ★ 二次扫描：MT5 positions_get() 有已知竞态（持仓刷新瞬间可能漏返回），
            #   平完第一轮后必须再查一次，确保零遗漏（用户实盘3笔SELL只查到2笔的教训）。
            self._close_opposite_for_decision(ai_decision)

            # ★★ Phase 6 降级熔断闸门（L3）★★
            #   位置经过刻意选择，是本铁律的物理体现：
            #     它在 _manage_positions() 与 _close_opposite_for_decision() **之后**，
            #     在开仓准备之前。于是 L3 生效时：
            #       ✔ 智能平仓/止损/反向平仓 全部照常跑（桶里的水一滴不动）
            #       ✘ 只有「开新仓」这条路被关掉（关掉水龙头）
            #   若把它挪到本方法开头，就会连持仓保护一起停掉——那是在系统能力
            #   最弱的时刻放弃对客户已有仓位的守护，是灾难性的错误。
            try:
                from app.services.platform_health_monitor import (
                    allow_new_entry as _allow_entry,
                    degrade_enabled as _deg_on,
                    snapshot_dict as _deg_snap,
                )
                if _deg_on() and not _allow_entry():
                    _s = _deg_snap()
                    _msg = (
                        f"平台降级 {_s.get('level_name')}（{_s.get('label')}）：{_s.get('reason')} "
                        f"→ 停发新开仓；已有持仓仍由止损/止盈/智能平仓守护"
                    )
                    result["errors"].append(_msg)
                    logger.warning(f"[{self.account_id[:8]}] ⛔ {_msg}")
                    # 溯源：平台级熔断是「全体客户同时没交易」的唯一合法原因，
                    # 必须留痕到每个账号，否则客户之间会互相印证出"系统集体故障"的错觉。
                    self._emit_risk_event(
                        event_type="halt", stage="degrade_gate",
                        codes=["DEGRADE_L3_CIRCUIT"], reasons=[_msg],
                        direction=str(getattr(ai_decision, "decision", "") or ""),
                        confidence=float(getattr(ai_decision, "confidence", 0) or 0),
                    )
                    return result
            except Exception as _de:
                # 熔断判定自身故障 → 放行（监控故障不该变成隐形停机）
                logger.debug(f"[{self.account_id[:8]}] 降级闸门检查跳过: {_de}")

            # ★★ Phase 8 授权闸门 ★★
            #   紧挨着 L3 熔断闸门放置，不是偶然——两者语义完全同源：
            #     ✔ 平仓/止损/追踪止盈 全部照常（授权到期不该让客户的钱失去保护）
            #     ✘ 只关掉「开新仓」这一条路
            #   授权是**商业契约**，不是风控手段。用强平催款会把一次续费纠纷
            #   升级成赔付诉讼，还踩「客户资金独立」的合规红线。
            #   同理，这里绝不能挪到方法开头——那会连持仓守护一起停掉。
            try:
                from app.licensing.service import check_open_allowed as _lic_gate

                _lic_ok, _lic_reason, _lic_code = _lic_gate(account_id=self.account_id)
                if not _lic_ok:
                    result["errors"].append(_lic_reason)
                    logger.warning(f"[{self.account_id[:8]}] ⛔ 授权闸门: {_lic_reason}")
                    # 复用 Phase 4 溯源通道：客户问「为什么不开单」，
                    # 前端风控事件流能直接答出「授权已到期」而不是一片空白。
                    self._emit_risk_event(
                        event_type="halt", stage="license_gate",
                        codes=[_lic_code], reasons=[_lic_reason],
                        direction=str(getattr(ai_decision, "decision", "") or ""),
                        confidence=float(getattr(ai_decision, "confidence", 0) or 0),
                    )
                    return result
            except Exception as _le:
                # fail-open：授权模块自身故障绝不能变成全体客户停摆
                logger.debug(f"[{self.account_id[:8]}] 授权闸门检查跳过: {_le}")

            # Step 3: 风控审核
            account_info = mt5_service.get_account_info(self.account_id)
            if "error" in account_info:
                result["errors"].append(f"无法获取账户信息: {account_info['error']}")
                return result

            balance = account_info.get("balance", 0)
            if not (balance > 0):
                result["errors"].append("账户余额为0")
                return result

            # 获取当前价格
            market_data = self.debate_engine.market._get_current_price()
            is_buy = ai_decision.decision == "BUY"
            entry_price = market_data.get("ask" if is_buy else "bid", 0)
            if entry_price <= 0:
                result["errors"].append("无法获取当前价格")
                return result

            # 基于市场快照获取真实 ATR（代替之前的硬编码 20）
            # 根因修复：_get_current_price() 只返 tick 不返指标，ATR 需从 volatility_metrics 取
            try:
                snap = gate_snap  # 复用门控阶段已取快照（避免重复拉取外部行情）
                atr = (snap.get("volatility_metrics", {}).get("h1_atr") or
                       snap.get("volatility_metrics", {}).get("d1_atr") or 20)
            except Exception:
                atr = 20  # 降级兜底
            sltp = compute_initial_sl_tp(
                side=ai_decision.decision,
                entry_price=entry_price,
                atr=atr,
                strategy=self.strategy,
                quality_regime=getattr(ai_decision, "quality_regime", "") or "",
                chronos_tp_ceiling=getattr(ai_decision, "chronos_tp_ceiling", None),
                chronos_p10=getattr(ai_decision, "chronos_p10", None),
                # ★ 2026-08-13 结构锚定：把 market_analyzer 算出的结构位传给 SL/TP
                #   无结构锚时 structure_sl/tp=None → compute_initial_sl_tp 回退 ATR（零行为变化）
                structure_sl=(snap.get("structure_anchors") or {}).get(
                    "sl_anchor_buy" if is_buy else "sl_anchor_sell"),
                structure_tp=(snap.get("structure_anchors") or {}).get(
                    "tp_anchor_buy" if is_buy else "tp_anchor_sell"),
            )

            # ── 进场价位对齐（2026-08-14 根治「AI 想在 4329 开空、执行却在 4315 市价开」）──
            # 若 AI 给出优于当前价的入场目标（SELL 想更高 / BUY 想更低）且落在「可达区间」，
            # 则【推迟市价开仓】，每轮检查价格是否回到该 zone，回到即点火；TTL 到期放弃。
            # 完全复用现有市价单路径（不引入任何新 MT5 订单类型），零新增执行风险；
            # 属「提准非拦截」：交易仍在条件满足时发生，只是进场更聪明、盈亏比更好，
            # 绝不减少交易笔数（太远的 target 自动退回市价，不漏单）。
            _defer_res = self._maybe_defer_entry(ai_decision, entry_price, atr, result)
            _deferred, _entry_scale = _defer_res if isinstance(_defer_res, tuple) else (bool(_defer_res), 1.0)
            if _deferred:
                # 本轮「仍在等 zone」，未开仓，跳过后续开仓步骤（持仓保护已在前面执行）
                return result
            # 追价缩仓：由 _maybe_defer_entry 返回 scale∈(0,1]，后续计算 position_size 时生效
            self._entry_scale = _entry_scale
            sl_price = sltp["sl"]
            tp_price = sltp["tp"]

            # ── 本地校对员·下单前强制结构闸门（Phase 9.1，2026-08-08）──
            # ★ 根因：校对员此前在 debate_engine 阶段运行，而那时 decision 上
            #   还没有 stop_loss/take_profit（SL/TP 在此处才算），导致结构审计
            #   永远拿不到 SL/TP → 对「止损挂反/止盈挂反」这类最致命错误完全失明，
            #   断路器形同虚设。现改为：**SL/TP 已知的下单前**，用代码侧
            #   `_structural_audit` 做强制闸门（不依赖 LLM，确定性、零额外时延）。
            #   这是「断路器」不是「否决权」：只拦结构自杀单，不改方向、不投票，
            #   符合「提准非拦截」红线。语义层（理由与方向自相矛盾）仍由
            #   debate_engine 的 LLM 校对员负责。
            _proof = get_local_llm()._structural_audit(
                {
                    "decision": ai_decision.decision,
                    "entry_price": entry_price,
                    "stop_loss": sl_price,
                    "take_profit": tp_price,
                    "reason": getattr(ai_decision, "reasoning_summary", "") or "",
                },
                entry_price,
            )
            _proof_issues, _proof_sev = _proof
            if _proof_sev == "major":
                _reason = "本地校对员(Qwen3-8B)拦截结构性缺陷：" + "；".join(_proof_issues)[:200]
                ai_decision.proofread_blocked = True
                ai_decision.block_reason = _reason
                result["errors"].append(_reason)
                result["blocked_by"] = "local_proofreader"
                self._emit_risk_event(
                    event_type="reject", stage="local_proofreader",
                    codes=["PROOFREAD_STRUCTURAL_MAJOR"],
                    reasons=_proof_issues,
                    direction=ai_decision.decision,
                    intended_lots=0.0,
                    confidence=float(ai_decision.confidence or 0),
                )
                logger.warning(f"[{self.account_id[:8]}] 🛑 校对员结构闸门拦截开仓：{_reason}")
                return result

            sl_points = abs(entry_price - sl_price)

            # 智能手数：考虑信号强度 + 同向持仓
            # ★ 2026-08-07 Phase 1：必须用 checked 版。这份 existing_positions 的
            #   len() 直接喂给下面的 E3「最大持仓数硬限制」——旧接口在 MT5 抖动时
            #   返回 []，current_count 就成了 0，已满仓也照样再叠一笔。
            #   注意上游对账门挡不住这条路：_reconcile_positions 每账号 60s 节流一次，
            #   节流期间沿用上轮结论(True)，而主循环周期 27~111s，窗口内完全敞开。
            _pos_ok, existing_positions = _positions_checked(self.account_id, "XAUUSD")
            if not _pos_ok:
                result["errors"].append(
                    "开仓前持仓查询失败：本轮不开新仓（持仓上限无从校验，宁可漏一单不可叠一单）"
                )
                logger.warning(
                    f"[{self.account_id[:8]}] 开仓前持仓查询失败 → 跳过开新仓；"
                    f"持仓保护已在本轮 Step 2 执行"
                )
                return result
            same_dir_n = count_same_direction_positions(existing_positions, ai_decision.decision)
            # ★ 2026-08-07 v5 AI 自主仓位管理：
            #   add 意图(趋势确认+高共识)→ 突破同向衰减(dir_mult)，允许金字塔加仓加码；
            #   但 max_positions / risk_engine 同方向并发上限(3) / 单笔风险上限 仍全程硬兜底，防爆仓。
            # ★ 2026-08-10 提到同向去重判断之前（去重需用 _add_intent 区分加仓/开仓）
            _add_intent = (getattr(ai_decision, "position_intent", "open") == "add")
            # ★★ 2026-08-10 信号塔统一·主号同向去重（用户方案 A）★★
            #   根因：主号(follow_leader=False 信号源)每个决策周期(~60s)都跑 AI，
            #   趋势持续时连续判同向 → 主号连开多单(23:18+23:23 两单 0.02)，
            #   而跟号的 _LATEST_LEADER_SIGNAL 120s TTL 过期后不再补跟 →
            #   "主号 2 单、跟号 1 单" 信号塔同步破缺。
            #   设计：仅主号(信号源)去重，跟号天然不适用(镜像)；同方向已有持仓 → 不开第二单；
            #   但 AI 显式 position_intent=add(金字塔加仓) 仍放行(加仓=新信号,广播给所有跟号)。
            #   这符合用户铁律"信号塔统一：方向/时机/平仓完全一致，只是手数不同"。
            _is_signal_source = bool(getattr(self, "_is_leader", False)) or (
                not bool(getattr(self, "_follow_leader", True))
            )
            # ★★ 2026-08-14 修正（用户铁律：恢复同方向最多 3 单篮子）★★
            #   根因：此前硬编码 same_dir_n > 0 → return，把用户在 strategy 里设置的
            #   max_concurrent_same_direction=3 架空成「永远只开 1 单」，导致篮子永远
            #   凑不齐 3 单 → 永远到达不了 L3 篮子锁利阈值($100)。
            #   修正：尊重用户配置的上限，仅当同向持仓「已达上限」才拦截；
            #   未达上限则允许趋势续单（金字塔/连续加单），AI 信号门(ai_decision)
            #   仍须确认方向，风险层 check_same_direction 同步按上限放行。
            #   _add_intent(金字塔加仓意图) 仍放行——由 risk_engine/max_positions 兜底防爆仓。
            _max_same_dir = int(self._fresh_strat("max_concurrent_same_direction", 3) or 3)
            if _is_signal_source and same_dir_n >= _max_same_dir and not _add_intent:
                _msg = (
                    f"[{self.account_id[:8]}] 信号塔同向去重：已有 {same_dir_n}/{_max_same_dir} 笔"
                    f"同方向({ai_decision.decision})持仓达上限，主号不再开新单(等平仓后新信号)"
                )
                result["errors"].append(_msg)
                logger.info(_msg)
                return result
            _size_same_dir = 0 if _add_intent else same_dir_n
            _ai_risk = getattr(ai_decision, "target_risk_pct", None)
            if _add_intent:
                logger.info(
                    f"[{self.account_id[:8]}] AI 仓位意图=add(金字塔加仓)→突破同向衰减，"
                    f"按基础手数加码(仍受硬上限保护)"
                )
            size_result = self._calc_position_size(
                balance, entry_price, sl_points,
                signal_confidence=float(ai_decision.confidence),
                same_direction_count=_size_same_dir,
                ai_target_risk_pct=_ai_risk,
                # ★ 2026-08-10 v6 趋势强弱自适应手数：实时 H1 ADX → 加/减码
                adx=(snap or {}).get("volatility_metrics", {}).get("h1_adx"),
            )
            position_size = size_result["lots"]

            # ★ 风控硬上限反钳手数：让小账号也能按真实本金比例开单（详见 _cap_to_risk_limit）
            _cap_lots, _cap_note = self._cap_to_risk_limit(balance, sl_points, position_size)
            if _cap_note:
                logger.info(_cap_note)
            if _cap_lots is None:
                result["errors"].append(_cap_note)
                return result
            position_size = _cap_lots

            # ★ 进场价位对齐·追价缩仓：_maybe_defer_entry 判定 AI 目标价已跑出可等区间时，
            #   按追价程度线性缩仓（但不拦截），防山顶/山底重仓追单。纯加法，默认开启。
            _entry_scale = getattr(self, "_entry_scale", 1.0)
            self._last_entry_scale = _entry_scale   # 供跟号 signal 继承（self_sizing 路径同步缩仓）
            if _entry_scale < 1.0 and position_size > 0:
                _scaled = round(position_size * _entry_scale, 2)
                if _scaled < 0.01:
                    _scaled = 0.01
                logger.info(
                    f"[{self.account_id[:8]}] 进场价位对齐·追价缩仓：原手数 {position_size} → "
                    f"{_scaled}（scale={_entry_scale:.2f}）"
                )
                position_size = _scaled
            self._entry_scale = 1.0  # 用完即清，避免污染下一轮

            # ★ 2026-08-16 审计终检修复：手数=0/负 必须直接 return（防 L3 熔断/降级把手数算成
            #   0 后仍进 place_order 开 0.01 手假单）。跟号路径已有同款守卫，主号路径此前缺失。
            if position_size <= 0:
                result["errors"].append(
                    f"手数计算结果为 {position_size}（≤0），跳过开仓（L3 熔断/降级保护）"
                )
                logger.warning(
                    f"[{self.account_id[:8]}] 手数={position_size}≤0 → 跳过开仓（防 0.01 假单）"
                )
                return result

            # 风控审核
            risk_result = self.risk_engine.check_trade_allowed(
                symbol="XAUUSD",
                volume=position_size,
                entry_price=entry_price,
                stop_loss=sl_price,
                account_balance=balance,
                direction=ai_decision.decision,
            )

            if not risk_result.passed:
                result["errors"].extend(risk_result.reject_reasons)
                # 溯源：reject_codes 来自 risk_engine 的 Reason(str) 子类，
                # 机器可筛（前端按码聚合"本周最常见拦截原因"），reasons 保留中文原文给人看。
                self._emit_risk_event(
                    event_type="reject", stage="risk_engine",
                    codes=risk_result.reject_codes,
                    reasons=risk_result.reject_reasons,
                    direction=ai_decision.decision,
                    intended_lots=position_size,
                    confidence=float(ai_decision.confidence or 0),
                )
                return result

            # E3：最大持仓数硬限制（per-account，防止无限开仓）
            # 根因修复：2026-08-03 发现 execute_cycle 全程无持仓数检查，
            # 导致同向13单仍继续开第14单（用户设8/DB存10均被突破）
            # 直接从 DB 查询最新值（绕过 SQLAlchemy session 缓存的过期策略对象）
            _db_strat = self.db.query(StrategyConfig).filter(
                StrategyConfig.mt5_account_id == self.account_id
            ).first()
            max_pos = int(getattr(_db_strat, 'max_positions', 8) or 8) if _db_strat else 8
            current_count = len(existing_positions)
            logger.debug(f"[最大持仓检查] {self.account_id[:8]} current={current_count} max={max_pos} db_max={getattr(_db_strat, 'max_positions', None) if _db_strat else 'N/A'}")
            if current_count >= max_pos:
                result["errors"].append(
                    f"已达最大持仓数({current_count}/{max_pos})，跳过开新仓（仍执行持仓管理）"
                )
                logger.warning(
                    f"[最大持仓] {self.account_id[:8]} 持仓={current_count}≥"
                    f"{max_pos}(max_positions) → 跳过开仓，仅执行持仓管理"
                )
                self._emit_risk_event(
                    event_type="reject", stage="executor",
                    codes=["EXECUTOR_MAX_POSITIONS"],
                    reasons=[f"已达最大持仓数({current_count}/{max_pos})"],
                    direction=ai_decision.decision,
                    intended_lots=position_size,
                    confidence=float(ai_decision.confidence or 0),
                )
                self._manage_positions(ai_decision)
                return result

            # ★ 同方向开仓间隔冷却：防止强势趋势中每轮都开新单导致持仓无限滚动
            #   2026-08-06 修复：主号 50 分钟内开了 22 笔 BUY（每 2.3 分钟一笔），
            #   smart_exit 平旧单速度赶不上开仓速度 → 持仓永远堆积 8~9 笔。
            #   2026-08-14 修正：默认从 180s 降到 90s，配合 max_concurrent_same_direction=3，
            #   让趋势中能续单、篮子能真正填满(3 单)，又不致每轮都开(每轮开仍受上限+churn 钳制)。
            #   同一方向至少间隔 90s（~1.5 个决策周期）才允许开下一笔，让利润奔跑+篮子积累。
            _dir = ai_decision.decision
            _ok = f"{self.account_id}:{_dir}"
            with _OPEN_INTERVAL_LOCK:
                _last_ts = _LAST_OPEN_TS.get(_ok, 0)
            _min_interval = float(self._fresh_strat("open_interval_seconds", 90) or 90)
            if time.time() - _last_ts < _min_interval:
                _remain = int(_min_interval - (time.time() - _last_ts))
                result["errors"].append(
                    f"同方向{_dir}冷却中(剩余{_remain}s)，跳过开新仓（仍执行持仓管理）"
                )
                logger.info(f"[开仓冷却] {self.account_id[:8]} {_dir} 冷却剩余{_remain}s")
                # 溯源：冷却/churn 是实盘里最高频的「没开单」原因，比风控层还常见。
                # 不记录的话，客户看到 AI 明明喊了 BUY 却没下单，只会认为系统失灵。
                self._emit_risk_event(
                    event_type="reject", stage="executor",
                    codes=["EXECUTOR_OPEN_INTERVAL"],
                    reasons=[f"同方向{_dir}开仓冷却中，剩余{_remain}s"],
                    direction=_dir, intended_lots=position_size,
                    confidence=float(ai_decision.confidence or 0),
                )
                self._manage_positions(ai_decision)
                return result

            # ★ 2026-08-06 修复平亏秒开churn：同方向刚平仓不久，禁止立即重开
            #   实盘表现：L3/硬止损把持仓平掉后，AI 下一秒又开同方向 → 错上加错。
            #   该冷却独立于 open_interval，专门抑制 close→open 的秒级翻转。
            _churn_cooldown = float(self._fresh_strat("churn_cooldown_seconds", 60.0) or 60.0)
            if _is_churn_suppressed(self.account_id, _dir, cooldown=_churn_cooldown):
                _remain = int(_churn_cooldown - (time.time() - _LAST_CLOSE_TS.get(_ok, 0)))
                result["errors"].append(
                    f"{_dir}方向刚平仓，churn抑制中(剩余{_remain}s)，跳过开新仓"
                )
                logger.info(f"[churn抑制] {self.account_id[:8]} {_dir} 刚平仓，剩余{_remain}s")
                self._emit_risk_event(
                    event_type="reject", stage="executor",
                    codes=["EXECUTOR_CHURN_COOLDOWN"],
                    reasons=[f"{_dir}方向刚平仓，churn抑制中，剩余{_remain}s"],
                    direction=_dir, intended_lots=position_size,
                    confidence=float(ai_decision.confidence or 0),
                )
                self._manage_positions(ai_decision)
                return result

            # ★★ 2026-08-17 P0 反手冷却（海外调研：nof1.ai Alpha Arena Core Policy Rule 3 +
            #   MQL5 Hysteresis Channel + JoinQuant 冷却期，≥3 源交叉验证）：
            #   实证事故（19:39→19:55）：BUY@4404 被反转平仓(-390) → 2 秒内反手 SELL@4396
            #   → 2 分钟内又被篮子 close_all + 反转平掉 → 大仓来回打脸 -409。
            #   根因：churn_cooldown 只抑制「同方向重开」，反手（平 BUY 立刻开 SELL）无冷却。
            #   方案（自适应·非固定分钟）：反手冷却 = 基础 5 个决策周期(5×30s=150s)，
            #   若刚才那笔平仓是亏损的 → 再延长到 8 个周期(240s)——亏损反手最易被行情甩脸，
            #   亏损越重冷却越长（线性插值封顶 10 个周期/300s）。盈利平仓说明方向对，
            #   反手多为新机会，冷却取基础档。只拦反手，不拦同向续单（续单仍由 open_interval 管）。
            _opp_dir = "SELL" if _dir == "BUY" else "BUY"
            _opp_key = f"{self.account_id}:{_opp_dir}"
            with _LAST_CLOSE_LOCK:
                _opp_close_ts = _LAST_CLOSE_TS.get(_opp_key, 0)
            if _opp_close_ts:
                _since_close = time.time() - _opp_close_ts
                # 反手冷却基础档：5 个决策周期（持仓 30s × 5 = 150s）
                _rev_base = float(getattr(settings, "REVERSAL_COOLDOWN_CYCLES", 5) or 5) * 30.0
                # 亏损加档：查该反方向最近平仓盈亏（从内存/DB 取，拿不到则按基础档）
                _opp_loss_ext = 0.0
                try:
                    _last_close = self.db.query(Trade).filter(
                        Trade.mt5_account_id == self.account_id,
                        Trade.action.in_(["buy", "sell"]),
                        Trade.close_time.isnot(None),
                        Trade.result.in_(["win", "loss"]),
                    ).order_by(Trade.close_time.desc()).first()
                    if _last_close is not None and str(_last_close.action).upper() == _opp_dir:
                        _last_pnl = float(_last_close.net_profit or _last_close.profit or 0)
                        if _last_pnl < 0:
                            # 亏损 ≤ -5$：基础 150s → 240s；≤ -50$：→ 300s（封顶）
                            _opp_loss_ext = min(150.0, max(0.0, abs(_last_pnl) / 50.0 * 150.0))
                except Exception:
                    pass
                _rev_cooldown = _rev_base + _opp_loss_ext
                if _since_close < _rev_cooldown:
                    _remain = int(_rev_cooldown - _since_close)
                    result["errors"].append(
                        f"反手冷却：{_dir} 前刚平 {_opp_dir}（{_since_close:.0f}s前），"
                        f"冷却 {int(_rev_cooldown)}s 内不反手（防 BUY↔SELL 来回打脸）"
                    )
                    logger.info(
                        f"[反手冷却] {self.account_id[:8]} {_dir} 前刚平{_opp_dir} "
                        f"{_since_close:.0f}s前，冷却剩余{_remain}s"
                    )
                    self._emit_risk_event(
                        event_type="reject", stage="executor",
                        codes=["EXECUTOR_REVERSAL_COOLDOWN"],
                        reasons=[f"{_dir} 前刚平 {_opp_dir}（{_since_close:.0f}s前），反手冷却中，剩余{_remain}s"],
                        direction=_dir, intended_lots=position_size,
                        confidence=float(ai_decision.confidence or 0),
                    )
                    self._manage_positions(ai_decision)
                    return result

            # ★ 2026-08-08 Phase 3 执行层错峰：N 个客户共用同一 XAUUSD 信号源，
            #   同一秒对同一品种打市价单 → 挤单滑点递增 + 易被经纪商识别为同一策略群。
            #   用随机抖动（不是排队）把下单时刻打散：N=1 零延迟，N 越大窗口越宽但
            #   封顶 800ms（排队会把第 N 个客户拖后几十秒 → 漏单，违背"多交易多赚钱"）。
            #   任何异常都直接放行——错峰是优化，不是闸门。
            _jitter_ms = 0.0
            try:
                from app.core.account_lane import active_accounts, apply_order_jitter
                _lane_n = active_accounts()
                _jitter_ms = apply_order_jitter(_lane_n)
            except Exception:
                _lane_n = 1

            # ★ 2026-08-13 ③ 回头看极端进场护栏：下单前用实时行情校验方向，
            #   发现山底买空/山顶追多等极端延伸签名直接放弃不进场（不调LLM），
            #   边界冲突送校对员二级确认。阈值极端，不砍正常单（守「多交易多赚钱」）。
            _block, _why, _feats = self._pre_entry_lookback_guard(
                ai_decision.decision, entry_price, sl_price, tp_price)
            if _block:
                logger.info(
                    f"[回头看护栏·拦截] {self.account_id[:8]} {ai_decision.decision} 放弃开仓: {_why}")
                self._emit_risk_event(
                    event_type="reject", stage="executor_lookback",
                    codes=["LOOKBACK_GUARD_BLOCK"],
                    reasons=[_why],
                    direction=ai_decision.decision, intended_lots=position_size,
                    confidence=float(ai_decision.confidence or 0),
                )
                self._manage_positions(ai_decision)
                return result

            # ★★ 2026-08-19 毫秒级跟单核心：主号在 place_order（不可逆点）之前广播"早信号"。
            #   挂号 copy_order 不依赖主号成交（各账号独立市场价成交），等主号成交才复制
            #   是纯串行浪费（实测 +0.55~1.3s）。此处回调 auto_loop 的分发器 → 挂号与
            #   主号 place_order 并行发单，成交时差收敛到网络/撮合差异（亚秒级）。
            #   开关 EARLY_COPY_ENABLED=False 可完全回退旧串行路径。
            try:
                _early_on = bool(getattr(settings, "EARLY_COPY_ENABLED", True))
            except Exception:
                _early_on = True
            if _early_on and self._is_leader and self._early_copy_cb is not None:
                try:
                    self._early_copy_cb({
                        "direction": ai_decision.decision,
                        "symbol": "XAUUSD",
                        "entry": float(entry_price or 0),
                        "sl": float(sl_price or 0),
                        "tp": float(tp_price or 0),
                        "confidence": float(ai_decision.confidence or 0),
                        "ticket": None,          # 主号成交后由 2a 兜底回填记录
                        "early": True,
                    })
                except Exception as _ec:
                    logger.warning(f"[毫秒跟单] 早信号广播异常(不阻塞主号下单): {_ec}")

            # Step 4: 执行下单（带止损止盈）
            order_result = mt5_service.place_order(
                account_id=self.account_id,
                symbol="XAUUSD",
                order_type=ai_decision.decision,
                volume=position_size,
                sl=sl_price,
                tp=tp_price,
                comment=f"WXAI|{ai_decision.decision}|C{ai_decision.confidence:.0%}",
            )

            if "error" in order_result:
                result["errors"].append(order_result["error"])
                return result

            # ★★ 2026-08-07 Phase 1 修复（原子状态转移，与 copy_order 对称）：
            #   走到这里 = 单子已在 MT5 成交，不可逆。open_interval 冷却的时间戳
            #   是这次成交的"配套状态转移"，必须立刻落地。
            #   原先它排在整段 Step 5 记账 + _push_feed 展示之后（约 40 行），
            #   而 _push_feed 在本函数里**没有任何 try 包裹**（全文 5 个调用点里
            #   唯一还裸奔的一个）。它一抛 → 外层 except 吞掉 → 时间戳丢失 →
            #   冷却默认 180s 但主循环仅 27~111s ⇒ 下一轮同方向立刻再补一刀。
            #   一个纯展示故障能掀翻仓位闸门，这是状态机设计错误，不是概率问题。
            #   注意必须放在"成交判定之后"：下单被拒时绝不能记冷却，
            #   否则会误杀之后 180s 内所有真实开仓机会（反向违反多交易多赚钱）。
            _ok = f"{self.account_id}:{ai_decision.decision}"
            with _OPEN_INTERVAL_LOCK:
                _LAST_OPEN_TS[_ok] = time.time()

            result["orders"].append({
                **order_result,
                "sl": sl_price,
                "tp": tp_price,
                "risk_pct": getattr(self.strategy, 'max_risk_per_trade_pct', 2.0),
            })

            # ★ 2026-08-06 补强⑥：记录成交滑点（经纪商执行质量遥测，注入 AI 上下文）
            try:
                from app.services.execution_telemetry import record_fill
                record_fill(ai_decision.decision, entry_price, float(order_result.get("price", entry_price)))
            except Exception:
                pass

            # ★ 2026-08-08 Phase 3：并发滑点归因。上面的遥测是"经纪商级"平均数，
            #   回答不了"滑点变差是行情还是我们自己挤单"。这里带上并发账号数与
            #   实际抖动，按并发档位分组统计——错峰窗口该调大还是白加，由数据说话。
            try:
                from app.core.account_lane import record_fill as _lane_record
                _lane_record(
                    self.account_id, ai_decision.decision, entry_price,
                    float(order_result.get("price", entry_price)),
                    concurrent_n=_lane_n, jitter_ms=_jitter_ms,
                )
            except Exception:
                pass

            # ★ 主号信号输出：供其他账号（跟号）复制
            # 主号先下，跟号按自身策略(本金/风控)复制同一方向/止损/止盈
            # ★ 2026-08-10 信号塔统一：signal 带主号实成交手数 volume，
            #   供 follower_volume_mode=mirror_leader 的跟号直接镜像手数。
            result["placed"] = True
            result["signal"] = {
                "direction": ai_decision.decision,      # BUY / SELL
                "symbol": "XAUUSD",
                "entry": order_result.get("price", entry_price),
                "sl": sl_price,
                "tp": tp_price,
                "confidence": float(ai_decision.confidence),
                "ticket": order_result.get("ticket"),
                "volume": float(order_result.get("volume", 0) or 0),   # 主号实成交手数（已含追价缩仓）
                "entry_scale": float(getattr(self, "_last_entry_scale", 1.0)),  # 主号实际应用追价缩仓比例
                "comment": f"WXAI|{ai_decision.decision}|C{ai_decision.confidence:.0%}",
            }

            # Step 5: 记录交易到数据库
            context = self.debate_engine.get_last_context()
            # ★ Phase 4 溯源冻结：三票 / Q分 / Chronos分位 / 降级档位，在成交这一刻
            #   一次性写死进这条 trade 记录。
            #   为什么必须当场冻结、而不是事后从 ai_activities 里拼回来：
            #   决策每分钟刷新，事后回查拿到的是"后来的想法"；而复盘要回答的问题是
            #   「当时凭什么开的这一单」。两者经常不一致，用后者复盘等于自欺。
            _snap = build_decision_snapshot(ai_decision)
            _flat = flat_columns(_snap)
            # ★ 2026-08-11 防御（P0 真实账号假巨亏）：worker 可能返回 price=0（REAL 券商），
            #   回查 MT5 持仓真实开仓价，避免 open_price=0 污染后续盈亏计算。
            _open_price = order_result.get("price", entry_price)
            if not _open_price or _open_price <= 0:
                try:
                    _ok, _poss = _positions_checked(self.account_id, "XAUUSD")
                    if _ok:
                        for _p in _poss:
                            if str(_p.get("ticket")) == str(order_result.get("ticket")):
                                _pop = float(_p.get("price_open") or 0)
                                if _pop > 0:
                                    _open_price = _pop
                                break
                except Exception:
                    pass
            if not _open_price or _open_price <= 0:
                _open_price = entry_price
            trade_record = Trade(
                user_id=self.user_id,
                mt5_account_id=self.account_id,
                mt5_ticket=str(order_result.get("ticket", "")),
                symbol="XAUUSD",
                action=ai_decision.decision.lower(),
                volume=order_result.get("volume", position_size),
                open_price=_open_price,
                sl=sl_price,
                tp=tp_price,
                deepseek_decision=ai_decision.deepseek_vote,
                deepseek_confidence=getattr(ai_decision, "deepseek_confidence", None) or ai_decision.deepseek_weight,
                deepseek_reasoning=str(context.get("deepseek_analysis", {}).get("reasoning", "")),
                hunyuan_decision=ai_decision.hunyuan_vote,
                hunyuan_confidence=getattr(ai_decision, "hunyuan_confidence", None) or ai_decision.hunyuan_weight,
                hunyuan_reasoning=str(context.get("hunyuan_analysis", {}).get("reasoning", "")),
                debate_summary=ai_decision.reasoning_summary,
                meta_agent_decision=ai_decision.decision,
                meta_agent_confidence=ai_decision.confidence,
                risk_check_passed=True,
                # 三个平铺列 = 需要被 WHERE / GROUP BY 命中的维度（按 Chronos 票面
                # 分组算胜率、按 Q 分段看 PF、按降级档位对比表现）；
                # 其余全部进 decision_snapshot JSON，避免表宽度失控。
                chronos_vote=_flat["chronos_vote"],
                q_score=_flat["q_score"],
                degrade_level=_flat["degrade_level"],
                decision_snapshot=snapshot_to_json(_snap),
                mfe=0.0,
                mae=0.0,
                # ★ 2026-08-15 审计P1修复：统一 UTC 写入（配合 _to_utc_iso 读出端闭环，
                #   根治 #5 时区收口未闭环导致的交易时间 8h 偏移）
                open_time=datetime.now(timezone.utc),
            )
            # ★ 毫秒级可靠性：trade记录用独立session写，Defender锁时不阻塞交易
            self._safe_db_write(lambda db: db.add(trade_record), label="开仓trade记录")

            # 写 AI 活动流（开仓）— 让真实交易进入「交易执行流」面板
            pos_size_str = f"{position_size}手"
            self._push_feed("open",
                f"开仓 {ai_decision.decision} {pos_size_str} @{entry_price}",
                direction=ai_decision.decision,
                open_price=float(entry_price or 0),
                confidence=float(ai_decision.confidence))

            # ── 大脑审计：记录执行器实际开单（消费 AI 决策）──
            if _ba_rec_te is not None:
                try:
                    _ba_rec_te("trade_executor", "adoption",
                               output={"direction": getattr(ai_decision, "decision", "?"),
                                       "lots": position_size, "sl": sl_price, "tp": tp_price,
                                       "ai_risk_used": _ai_risk is not None,
                                       "intent": getattr(ai_decision, "position_intent", "open")},
                               adopted=1, consumer="MT5",
                               notes=f"AI仓位意图={getattr(ai_decision,'position_intent','open')}")
                except Exception:
                    pass
            logger.info(f"[执行器] ✅ {ai_decision.decision} {position_size}手 @{entry_price} SL={sl_price} TP={tp_price} ticket={order_result.get('ticket')}")
            # 注：同方向开仓时间戳已在成交点紧邻处记录（见上方原子状态转移说明）
        except Exception as e:
            logger.error(f"[执行器] 执行失败: {e}")
            result["errors"].append(str(e))

        return result

    def copy_order(self, signal: dict) -> dict:
        """
        跟单：复制主号(信号主号)的订单到本账号(跟号)。
        ★ 核心：手数按本账号自身策略(base_capital/风险%)计算，与真实余额脱钩；
               风控由各账号独立审核（笔数/手数/同向并发/单笔风险）。
        signal: {direction, symbol, entry, sl, tp, confidence, ticket, comment}
        """
        result = {"order": None, "errors": []}
        try:
            # ★ E0：人工紧急处置窗口（Phase 0）——跟单是独立于 execute_cycle 的第二条开仓入口，
            #   必须单独挡。漏掉这里会出现"主号停了，跟号照跟"的荒谬场景。
            _open_ok, _halt_why = emergency.allow_open(self.account_id)
            if not _open_ok:
                result["errors"].append(f"{_halt_why}（跟单已跳过）")
                logger.warning(f"[{self.account_id[:8]}] 跟单被人工停止拦截: {_halt_why}")
                return result

            # E1：本账号交易开关
            _acc = self.db.query(MT5Account).filter(MT5Account.id == self.account_id).first()
            if _acc and not _acc.is_trading_enabled:
                result["errors"].append("该跟号已停用交易，跳过复制")
                return result

            # E2：亏损冷却期（防报复性跟单）
            # ★ 2026-08-12 修复镜像跟单延时：纯镜像跟号(follow_leader)复制的是主号信号，
            #   不是独立报复性开仓——主号自身已通过其风控闸门。跟号自身的亏损冷却会把镜像
            #   复制延迟几十秒（主号开仓后同步复制被静默拦截，只能等10s补单循环反复重试、
            #   冷却到期才补上），直接违反"副号须与主号实时同步、金融产品不能有延时"硬要求，
            #   且导致跟号以更差价入场（实测 4418.57 vs 主号 4415.84，差≈$2.73）。
            #   故镜像跟号跳过自身亏损冷却，仅由主号风控兜底；独立账号仍保留冷却防报复交易。
            _follow_leader = bool(
                getattr(self.strategy, "follow_leader", False)
                or getattr(self, "_follow_leader", False)
            )
            if not _follow_leader:
                cooldown = self._check_loss_cooldown()
                if cooldown:
                    result["errors"].append(cooldown)
                    return result

            direction = (signal or {}).get("direction")
            symbol = (signal or {}).get("symbol", "XAUUSD")
            confidence = float((signal or {}).get("confidence", 0.7) or 0.7)
            leader_ticket = (signal or {}).get("ticket")
            _leader_entry = float((signal or {}).get("entry", 0) or 0)
            _leader_sl = float((signal or {}).get("sl", 0) or 0)
            _leader_tp = float((signal or {}).get("tp", 0) or 0)

            if direction not in ("BUY", "SELL"):
                result["errors"].append("无效跟单方向")
                return result

            # ★ ④ 跟号开仓价/时机与主号对齐：
            #   时机：跟号在本轮紧跟主号执行（trading.py 循环顺序保证），用"当前实时行情价"入场，
            #         与主号同一时刻成交对齐；不沿用主号历史成交价(stale)以免滑点漂移。
            #   风险结构：主号 SL/TP 为 ATR 偏移量，跟号复用"同一偏移"套到当前价 → 主副号
            #         止损/止盈距离(点数·ATR)完全一致，盈亏结构与风控节奏对齐。
            is_buy = direction == "BUY"
            try:
                _md = self.debate_engine.market._get_current_price()
                entry_price = float(_md.get("ask" if is_buy else "bid", 0) or 0)
            except Exception:
                entry_price = 0.0
            if entry_price <= 0:
                entry_price = _leader_entry  # 降级：实时行情不可用时用主号成交价
            if entry_price <= 0:
                result["errors"].append("主号成交价与实时行情均为空，无法跟单")
                return result

            # 复用主号 SL/TP 相对其成交价的偏移（带方向），套到跟号当前入场价
            if _leader_entry > 0 and _leader_sl > 0:
                sl_price = round(entry_price + (_leader_sl - _leader_entry), 2)
            else:
                sl_price = _leader_sl
            if _leader_entry > 0 and _leader_tp > 0:
                tp_price = round(entry_price + (_leader_tp - _leader_entry), 2)
            else:
                tp_price = _leader_tp

            # ★ 2026-08-11 智能增强：跟号 SL/TP 直接镜像主号【当前真实持仓】的 SL/TP
            #   （含 smart_exit 上移后的盈利保护位），而非 signal 可能过时的初始 SL。
            #   背景：主号开仓后 smart_exit 会把 SL 上移到成本之上（盈利保护），但 signal._leader_sl
            #   仍是开仓初始值，move_sl 广播对新单有时机盲区 → 跟号 SL 卡在成本下裸奔。
            #   这里开仓即查主号当前真实 SL/TP 并套用，让跟号与主号风控完全同步（高大上智能化）。
            try:
                # ★ 2026-08-15 审计P1修复：查主号记录同样防 ticket 撞号——排除本账号
                #   （跟号自己的同号单），并按 id 倒序取最近一条（镜像场景 leader_ticket
                #   必是主号刚开的单，最近创建命中率最高）。根治方案=signal 携带 leader
                #   账号字段（留待 P2 契约批次）。
                # ★ 2026-08-15 复检P2修复：再补 user_id 过滤——ticket 各券商从 1 递增，
                #   仅排除本账号仍可能命中【他租户】同名 ticket 记录 → 误用他人 SL/TP 偏移
                #   （多租户串扰）。同租户内查主号最稳。
                _lt_rec = self.db.query(Trade).filter(
                    Trade.user_id == self.user_id,
                    Trade.mt5_account_id != self.account_id,
                    Trade.mt5_ticket == str(leader_ticket)
                ).order_by(Trade.id.desc()).first()
                _leader_acc = _lt_rec.mt5_account_id if _lt_rec else None
                if _leader_acc:
                    _lp_ok, _lp_list = _positions_checked(_leader_acc, symbol)
                    if _lp_ok:
                        for _lp in _lp_list:
                            # ★ 2026-08-17 防御（铁律：持仓元素非 dict 一律跳过）：
                            #   曾出现 'list' object has no attribute 'get'（copy_order 复制失败
                            #   刷屏 85s），嫌疑即此处/同向计数处对非 dict 元素调 .get()。
                            if not isinstance(_lp, dict):
                                continue
                            if str(_lp.get("ticket")) == str(leader_ticket):
                                _l_sl = float(_lp.get("sl") or 0)
                                _l_tp = float(_lp.get("tp") or 0)
                                # ★ 2026-08-15 P2-1 修复：偏移基准必须用主号【真实成交价 price_open】，
                                #   不能用 _leader_entry(=signal["entry"])。signal["entry"] 是 AI 目标价，
                                #   REAL 券商 order_send 返回 price=0 时回退为 0/目标价，并非真实开仓价
                                #   → 基准失真 → 跟号 SL/TP 偏移算错、手数引擎失真。
                                #   主号实时持仓 price_open 才是真实成交价，以此为基准套跟号入场价，
                                #   且同步 smart_exit 上移后的盈利保护位（同风险结构、同节奏）。
                                #   （2026-08-13 改为相对偏移的修复仍成立，此处仅纠正偏移基准来源。）
                                _l_open = float(_lp.get("price_open") or 0)
                                if _l_sl > 0 and _l_open > 0:
                                    sl_price = round(entry_price + (_l_sl - _l_open), 2)
                                if _l_tp > 0 and _l_open > 0:
                                    tp_price = round(entry_price + (_l_tp - _l_open), 2)
                                break
            except Exception:
                pass

            # ★ 2026-08-06 修复跟号重复跟单：进程级硬去重（MT5 comment 不可靠）
            if _is_copied(self.account_id, leader_ticket):
                result["errors"].append(f"主号#{leader_ticket}已在跟单队列中，跳过重复复制")
                return result

            # ★ 2026-08-06 修复平亏秒开churn：跟号同方向刚平仓不久，不立即补单
            # ★ 2026-08-07 Phase 1 修复：原先这里硬编码 cooldown=60.0，忽略客户在策略里
            #   配置的 churn_cooldown_seconds，造成两个方向的错：
            #   ① 客户调短（如 30s）时跟号仍按 60s 拦 → 主号已开的单跟号漏跟 →
            #      主副持仓不一致且跟号少赚（直接违反"零新增拒单/多交易多赚钱"铁律）；
            #   ② 客户调长（如 300s）时跟号只拦 60s → 客户明确要求的 churn 保护形同虚设。
            #   现与主号 execute_cycle 同源读取，跟号与主号 churn 语义完全一致。
            _f_churn_cd = float(self._fresh_strat("churn_cooldown_seconds", 60.0) or 60.0)
            if _is_churn_suppressed(self.account_id, direction, cooldown=_f_churn_cd):
                result["errors"].append(f"{direction}方向刚平仓，抑制秒级补单")
                return result

            # 账户信息（真实余额仅用于日亏/回撤保护，不参与手数计算）
            account_info = mt5_service.get_account_info(self.account_id)
            if "error" in account_info:
                result["errors"].append(f"无法获取跟号账户信息: {account_info['error']}")
                return result
            balance = account_info.get("balance", 0)

            # 手数：按本账号自身策略(base_capital)算（与真实余额脱钩）
            sl_points = abs(entry_price - sl_price) if sl_price > 0 else 20.0
            # ★ 2026-08-07 Phase 1：跟号开仓同样不能在"不知道自己持有几笔"时下单。
            #   查询失败会让 same_dir_n=0（同向衰减系数失真→手数偏大），
            #   且跟号自身的持仓规模同样失去校验依据。
            #   代价评估：此刻 MT5 查询都不通，下单请求本也大概率失败，
            #   挡掉的主要是"基于错误持仓数算出的错误手数"，不是有效交易机会。
            _pos_ok, existing_positions = _positions_checked(self.account_id, symbol)
            if not _pos_ok:
                result["errors"].append("跟号开仓前持仓查询失败：本轮不跟单（避免按失真持仓数放大手数）")
                logger.warning(f"[跟号开仓] {self.account_id[:8]} 持仓查询失败 → 跳过本次跟单")
                return result
            same_dir_n = count_same_direction_positions(existing_positions, direction)
            # ★ 2026-08-10 v6 跟号同样按趋势强弱自适应手数：
            #   取 market_analyzer 最新缓存快照的 h1_adx（紧跟主号执行，缓存新鲜；
            #   取不到则 adx=None → 引擎按 1.0 不干预，向后兼容）。
            try:
                _cached_snap = getattr(self.debate_engine.market, "_cached_snapshot", None) or {}
                _f_adx = (_cached_snap.get("volatility_metrics") or {}).get("h1_adx")
            except Exception:
                _f_adx = None
            size_result = self._calc_position_size(
                balance, entry_price, sl_points,
                signal_confidence=confidence,
                same_direction_count=same_dir_n,
                adx=_f_adx,
            )
            position_size = size_result["lots"]

            # ★★ 2026-08-10 信号塔统一（B 方案）手数来源：
            #   follower_volume_mode="mirror_leader"（挂主号的跟号，如7175）→ 直接镜像主号手数，
            #   保证"客户看到的单子和主号一模一样"（同方向同时机同手数）。
            #   = "self_sizing"（独立账号统一信号，如3299/3301）→ 按客户自己填的策略风控
            #   （min_lot/max_lot/风险%，走上方 _calc_position_size）。主号 volume 取不到时兜底自身 sizing。
            _fv_mode = str(self._fresh_strat("follower_volume_mode", "self_sizing") or "self_sizing").lower()
            _leader_vol = float((signal or {}).get("volume", 0) or 0)
            _mirror_mode = (_fv_mode == "mirror_leader" and _leader_vol > 0)
            if _mirror_mode:
                position_size = _leader_vol
                # ★★ 2026-08-10 mirror_leader：镜像主号手数是用户明确要求（"挂单按主账号执行"），
                #   必须跳过下方 _cap_to_risk_limit 反钳——否则真实余额 2408×2%=$48 会把
                #   主号 0.02 手压回 0.01（实测 22:35-23:07 全部 0.01，min_lot=0.02 被反钳覆盖）。
                #   仍受两道兜底：①客户 max_lot_per_trade 硬 cap（红线，尊重客户设置）；
                #               ②下方 risk_engine.check_trade_allowed 独立审核（拒单留痕）。
                _max_lot_cap = float(self._fresh_strat("max_lot_per_trade", 0.0) or 0.0)
                if _max_lot_cap > 0 and position_size > _max_lot_cap:
                    logger.info(
                        f"[跟号手数] {self.account_id[:8]} mirror_leader：主号 {_leader_vol} 手"
                        f"超客户 max_lot={_max_lot_cap}，cap 到 {_max_lot_cap}"
                    )
                    position_size = _max_lot_cap
                logger.info(
                    f"[跟号手数] {self.account_id[:8]} mirror_leader：镜像主号手数 {position_size} 手"
                    f"（跳过风险反钳，受 max_lot={_max_lot_cap} cap）"
                )

            # ★ 风控硬上限反钳手数：跟号也能按真实本金比例开单
            # ★ 2026-08-10 mirror_leader 跳过反钳（镜像手数是用户明确要求，已受 max_lot cap + risk_engine 兜底）
            if _mirror_mode:
                pass
            else:
                _cap_lots, _cap_note = self._cap_to_risk_limit(balance, sl_points, position_size)
                if _cap_note:
                    logger.info(_cap_note)
                if _cap_lots is None:
                    result["errors"].append(_cap_note)
                    return result
                position_size = _cap_lots
                # ★ 2026-08-14 进场价位对齐·追价缩仓传导（self_sizing 跟号）：
                #   主号若因「AI目标价比实际成交价更优、但已跑出可等区间」而缩仓追单，
                #   该追价事实对所有跟号同样成立（跟号同价镜像主号入场）。
                #   mirror_leader 跟号已通过 signal["volume"] 继承主号实成交（已缩仓）手数，
                #   故此处仅对 self_sizing 跟号按主号 entry_scale 同步缩仓，避免跟号山顶重仓。
                #   纯加法·不拦截·不漏单（与 _maybe_defer_entry 同源）。
                _lead_scale = float((signal or {}).get("entry_scale", 1.0) or 1.0)
                if _lead_scale < 1.0 and position_size > 0:
                    _scaled = round(position_size * _lead_scale, 2)
                    if _scaled < 0.01:
                        _scaled = 0.01
                    logger.info(
                        f"[{self.account_id[:8]}] 跟单追价缩仓：主号 entry_scale={_lead_scale:.2f}，"
                        f"本号手数 {position_size} → {_scaled}"
                    )
                    position_size = _scaled

            if position_size <= 0:
                result["errors"].append("跟号手数计算为0，跳过")
                return result

            # 风控审核（各账号独立：笔数/手数/同向并发/风险%）
            risk_result = self.risk_engine.check_trade_allowed(
                symbol=symbol,
                volume=position_size,
                entry_price=entry_price,
                stop_loss=sl_price,
                account_balance=balance,
                direction=direction,
            )
            if not risk_result.passed:
                # ★ 2026-08-12 修复 mirror_leader 跟单延迟：主号信号已通过自身风控闸门，
                #   mirror_leader 跟号强制镜像主号手数。若再用跟号真实余额按单笔风险比例
                #   审核，小余额账号会出现"手数必须跟主号一样，但风险比例又超限"的结构性
                #   冲突 → 反复被拒、延迟 10+ 次才能入场（实测 21:11:37 信号到 21:13:14
                #   才成交，延迟 97s）。
                #   处理：mirror_leader 模式下仅放行 PER_TRADE_RISK_LIMIT 这一项；
                #   点差、持仓上限、同向并发、日亏、回撤、交易时段等风控仍然保留。
                _per_trade_only = (
                    _mirror_mode
                    and len(risk_result.reject_codes) == 1
                    and risk_result.reject_codes[0] == "PER_TRADE_RISK_LIMIT"
                )
                if _per_trade_only:
                    logger.warning(
                        f"[跟号风控] {self.account_id[:8]} mirror_leader 单笔风险比例"
                        f"({risk_result.reject_reasons[0]})，但主号已过闸 → 强制放行镜像手数 {position_size}"
                    )
                else:
                    result["errors"].extend(risk_result.reject_reasons)
                    # 溯源（跟号）：stage 标 risk_engine_follower —— 跟号被拦和主号被拦
                    # 是两种完全不同的运营问题（前者是本账号风控参数太紧，
                    # 后者是信号本身没过闸），前端必须能分开统计。
                    self._emit_risk_event(
                        event_type="reject", stage="risk_engine_follower",
                        codes=risk_result.reject_codes,
                        reasons=risk_result.reject_reasons,
                        symbol=symbol, direction=direction,
                        intended_lots=position_size,
                        confidence=float(confidence or 0),
                    )
                    return result

            # ★★ 2026-08-07 Phase 2（SignalBus）：跨线程去重的**唯一权威点**。
            #   上方那次 _is_copied 只是省算力的廉价预检；真正挡住并发的是这里。
            #   为什么占坑点必须钉在这一行、而不是提到函数开头：
            #     从预检到这里之间还隔着 6 条 return 路径（churn/账户信息/持仓查询/
            #     钳手/手数为0/风控拒绝）。占早了，每一条都得记得归还，
            #     漏掉任意一条 = 这张主号单在 TTL(300s) 内永远跟不上 ⇒ 静默漏跟。
            #   占坑与不可逆动作紧邻，需要归还的路径就只剩下面两条，可穷举、可验证。
            if not _claim_copy(self.account_id, leader_ticket):
                result["errors"].append(
                    f"主号#{leader_ticket}已被并发路径跟单，跳过重复复制")
                return result

            # ★ 2026-08-08 Phase 3 执行层错峰（与主号开仓路径对称）：
            #   跟号是最容易造成同秒挤单的一类——一个主号信号会瞬间触发 N-1 个跟号
            #   同方向打单。这里同样用随机抖动打散，异常直接放行。
            _jitter_ms = 0.0
            _lane_n = 1
            try:
                from app.core.account_lane import active_accounts, apply_order_jitter
                _lane_n = active_accounts()
                _jitter_ms = apply_order_jitter(_lane_n)
            except Exception:
                pass

            # ★ 2026-08-13 ③ 回头看护栏（跟号同样执行，保护真实资金账号）：
            #   主号已通过护栏，但跟号入场价/快照可能略有差异，独立复核极端延伸签名。
            #   拦截时释放占坑(_release_copy)，避免该主号票在 TTL 内永久漏跟。
            _block, _why, _feats = self._pre_entry_lookback_guard(
                direction, entry_price, sl_price, tp_price)
            if _block:
                logger.info(
                    f"[回头看护栏·拦截] {self.account_id[:8]} {direction} 放弃跟单: {_why}")
                self._emit_risk_event(
                    event_type="reject", stage="executor_lookback_follower",
                    codes=["LOOKBACK_GUARD_BLOCK"],
                    reasons=[_why],
                    symbol=symbol, direction=direction,
                    intended_lots=position_size, confidence=float(confidence or 0),
                )
                _release_copy(self.account_id, leader_ticket)
                return result

            # 下单（同方向/同止损止盈，手数按本账号）
            # 注意：MT5 comment 有效长度上限约 29 字符（_sanitize_comment 截断到 20），故只用短标记。
            # 信号塔映射：跟单来源(主号票号)写入 comment 的 L{主号票号}，供出场同步时跟号回查。
            try:
                order_result = mt5_service.place_order(
                    account_id=self.account_id,
                    symbol=symbol,
                    order_type=direction,
                    volume=position_size,
                    sl=sl_price,
                    tp=tp_price,
                    comment=f"WXAI-L{leader_ticket}",
                )
            except Exception:
                # 归还路径①：下单过程抛异常，成交与否未知。
                # 这里选择归还——宁可让补单兜底再试一次（MT5 侧真成交了的话，
                # 下一轮 _reconcile_against_leader 会按 comment 认领，不会重复堆仓），
                # 也不能占着坑让这张主号单彻底跟不上。
                _release_copy(self.account_id, leader_ticket)
                raise
            if "error" in order_result:
                # 归还路径②：明确被拒（无报价/资金不足/市场关闭）。没有任何副作用发生，
                # 必须干净归还，让 10s 守护线程的补单兜底能真正补上这一单。
                _release_copy(self.account_id, leader_ticket)
                result["errors"].append(order_result["error"])
                return result

            # ★★ 2026-08-07 Phase 1 修复（原子状态转移）：
            #   下单已经成交 = 不可逆副作用已发生，配套的状态转移必须"立刻"完成，
            #   不能排在记账代码之后。原先顺序是
            #       place_order 成交 → 构造 Trade → _safe_db_write 记账 → _mark_copied
            #   只要中间任何一步抛异常（Trade 字段问题、DB 锁、session 崩），
            #   函数就被外层 except 捕获返回，而去重标记根本没落地 →
            #   下一轮 _is_copied 仍为 False → 同一张主号票被跟第二次 →
            #   真金白银重复开仓（主副仓位失衡 + 双份风险敞口）。
            #   同理 _LAST_OPEN_TS 也是"已成交"的事实记录，落后于记账会让 open_interval
            #   冷却保护在异常路径上失效。故二者一并上移，紧贴成交点。
            _mark_copied(self.account_id, leader_ticket)
            _ok = f"{self.account_id}:{direction}"
            with _OPEN_INTERVAL_LOCK:
                _LAST_OPEN_TS[_ok] = time.time()

            result["order"] = {
                **order_result,
                "sl": sl_price,
                "tp": tp_price,
                "risk_pct": getattr(self.strategy, 'max_risk_per_trade_pct', 2.0),
                "copied_from": leader_ticket,
            }

            # ★ 2026-08-06 补强⑥：记录成交滑点（跟单同样计入执行质量遥测）
            try:
                from app.services.execution_telemetry import record_fill
                record_fill(direction.upper(), entry_price, float(order_result.get("price", entry_price)))
            except Exception:
                pass

            # ★ 2026-08-08 Phase 3：并发滑点归因（跟号路径，与主号对称）
            try:
                from app.core.account_lane import record_fill as _lane_record
                _lane_record(
                    self.account_id, direction.upper(), entry_price,
                    float(order_result.get("price", entry_price)),
                    concurrent_n=_lane_n, jitter_ms=_jitter_ms,
                )
            except Exception:
                pass

            # 记录 + 写活动流（标注"跟单"）
            # ★ 2026-08-11 防御（P0 真实账号假巨亏）：worker 可能返回 price=0（REAL 券商），
            #   回查 MT5 持仓真实开仓价，避免 open_price=0 污染后续盈亏计算。
            _open_price = order_result.get("price", entry_price)
            if not _open_price or _open_price <= 0:
                try:
                    _ok, _poss = _positions_checked(self.account_id, symbol)
                    if _ok:
                        for _p in _poss:
                            if str(_p.get("ticket")) == str(order_result.get("ticket")):
                                _pop = float(_p.get("price_open") or 0)
                                if _pop > 0:
                                    _open_price = _pop
                                break
                except Exception:
                    pass
            if not _open_price or _open_price <= 0:
                _open_price = entry_price
            trade_record = Trade(
                user_id=self.user_id,
                mt5_account_id=self.account_id,
                mt5_ticket=str(order_result.get("ticket", "")),
                symbol=symbol,
                action=direction.lower(),
                volume=order_result.get("volume", position_size),
                open_price=_open_price,
                sl=sl_price,
                tp=tp_price,
                meta_agent_decision=direction,
                meta_agent_confidence=confidence,
                debate_summary=f"跟单复制主号#{leader_ticket}（本账号独立风控）",
                risk_check_passed=True,
                # 跟号没有自己的辩论过程（方向来自主号），但「在什么系统状态下复制的这一单」
                # 必须留痕：同样一笔跟单，L0 下开出和 L2 降级下开出，事后归因结论完全不同。
                degrade_level=current_degrade_level() or None,
                decision_snapshot=snapshot_to_json({
                    "source": "follower",
                    "leader_ticket": str(leader_ticket or ""),
                    "decision": str(direction or "").upper(),
                    "confidence": float(confidence or 0),
                    "degrade_level": current_degrade_level(),
                }),
                mfe=0.0, mae=0.0,
                open_time=datetime.now(timezone.utc),
            )
            # ★ 毫秒级可靠性：跟单trade记录用独立session写
            self._safe_db_write(lambda db: db.add(trade_record), label="跟单trade记录")

            # 注：_mark_copied / _LAST_OPEN_TS 已在成交点紧邻处完成（见上方原子状态转移说明）

            self._push_feed("open",
                f"跟单 {direction} {position_size}手 @{entry_price} (复制主号#{leader_ticket})",
                direction=direction, confidence=confidence,
                open_price=float(entry_price or 0), ticket=str(leader_ticket or ""))
            logger.info(
                f"[跟单] ✅ {self.account_id[:8]} 复制主号#{leader_ticket} "
                f"{direction} {position_size}手 @{entry_price} SL={sl_price} TP={tp_price}"
            )
        except Exception as e:
            # ★ 2026-08-17 可观测性：完整 traceback 落盘。此前只打异常消息，
            #   实际根因（'list' object has no attribute 'get'）无法定位到行。
            import traceback as _tb
            logger.error(
                f"[跟单] 复制失败: {e} | traceback:\n{_tb.format_exc()}"
            )
            result["errors"].append(str(e))
            # ★ 2026-08-17 P0 修复：只有「未成交」才归还占坑（允许下轮重试）。
            #   若异常发生在成交点之后（_mark_copied 已落地，如 Trade 构造/记账炸），
            #   必须**保留去重标记**——否则同一主号票下轮被跟号再跟一次 = 双倍敞口
            #   （test_dedupe_mark_survives_bookkeeping_failure 守护的语义）。
            #   原实现无条件 _release_copy 会把已落地的去重标记清掉。
            if not _is_copied(self.account_id, leader_ticket):
                _release_copy(self.account_id, leader_ticket)
        return result

    def _build_exit_context(self, snap: dict, ai_decision, md: dict = None) -> dict:
        """构建 AI 出场决策所需市场背景（纯行情+状态，不含敏感信息）"""
        vol = (snap or {}).get("volatility_metrics", {}) or {}
        tfs = (snap or {}).get("timeframes", {}) or {}
        # ★ 价格延伸度/拥挤度（防"接飞刀"）：取 H1 布林带 position(Z-score) + MA + 趋势
        h1 = (tfs.get("H1", {}) or {})
        bb = (h1.get("bollinger", {}) or {})
        ma = (h1.get("ma", {}) or {})
        price_ext_z = bb.get("position")          # (close-MA)/(2*std)*100，正=高于中轨(延伸过度)
        ctx = {
            "symbol": "XAUUSD",
            "current_price": (snap or {}).get("current_price") or (snap or {}).get("price"),
            "h1_atr": vol.get("h1_atr"),
            "d1_atr": vol.get("d1_atr"),
            "regime": (snap or {}).get("regime") or (snap or {}).get("market_regime") or "unknown",
            "spread": (snap or {}).get("spread"),
            "ai_open_decision": getattr(ai_decision, "decision", "HOLD"),
            "ai_open_confidence": round(float(getattr(ai_decision, "confidence", 0) or 0), 2),
            # ★ 价格延伸度特征：让 AI 自己判断是否高位/末端（属"提准"而非硬拦截）
            "price_extension_z": round(float(price_ext_z), 1) if price_ext_z is not None else None,
            "ma20": round(float(ma["MA20"]), 2) if ma.get("MA20") else None,
            "ma50": round(float(ma["MA50"]), 2) if ma.get("MA50") else None,
            "trend_h1": h1.get("trend"),
            "note": "AI只输出出场意图；SL必须保留且置于市价内侧；盈利单倾向让利润奔跑，结构转弱才收。"
                    " 关键参考 price_extension_z(当前价距布林中轨的标准化偏离)：显著为正=已延伸过度/高位区"
                    "(盈利单优先锁定利润、谨慎追高；亏损多单警惕反转)；显著为负=已超卖/低位区"
                    "(亏损空单警惕止跌反转，不要恐慌杀跌)。结合 ma20/ma50 多空排列与 trend_h1 趋势强度综合判断。",
        }
        # ★ 修复：原 return 在记忆库注入代码之前，导致记忆库教训/出场哲学从未生效（死代码）。
        #   现统一合并到 ctx 后返回，让 AI 出场 Agent 真正看到历史教训与演化的出场哲学（AI 进化可视化）。
        try:
            from app.services.memory_bank import get_memory_bank
            bank = get_memory_bank()
            ctx["lessons"] = bank.top_lessons(5)              # M2 反射教训(top-5，实证教训更充分)
            ctx["exit_philosophy"] = bank.aggressiveness_prompt()  # M4 OPRO 演化出的出场哲学
        except Exception as _e:
            logger.debug(f"[出场上下文] 记忆库注入跳过: {_e}")
        # ★ 2026-08-13 审计F1(P0)：管理出场大脑此前收不到 反转哨兵/质量陪审团/进化洞察/历史成交/仓位快照，
        #   导致「防失明」在管理侧实质断裂——出场官对趋势末端/止盈regime聋哑。现透传 debate_engine 注入过的
        #   market_data（与开仓大脑同源），让出场决策也看见这些信号（加法增强，非拦截）。
        if md:
            ctx["reversal_sentinel"] = md.get("reversal_sentinel") or {}
            ctx["meta_quality"] = md.get("meta_quality") or {}
            ctx["evolution_advice"] = md.get("evolution_advice") or []
            ctx["recent_closed_trades"] = md.get("recent_closed_trades") or []
            ctx["portfolio_state"] = md.get("portfolio_state") or {}
        return ctx

    def _auto_exit_blocked(self, where: str) -> bool:
        """★ Phase 0：该账号的"自动平仓类动作"是否被人工 HALT_ALL 冻结。

        为什么要在方法级挡而不是只在 execute_cycle 挡：
        trading.py 的守护线程会绕过 execute_cycle 直接调 _fast_l3_lock /
        _manage_positions / _mirror_leader_exits / _close_opposite_for_decision。
        只守主入口的话，HALT_ALL 期间这些旁路照样自动平仓——等于没停。

        铁律6：MANUAL_HALT 只拒新开仓，SL/TP/SmartExit 等保护性自动平仓
        始终有效（不"抽走桶里的水"）。本方法恒返回 False——
        人工停止冻结的是"新开仓"，不是"持仓保护"。
        """
        ok, why = emergency.allow_auto_exit(self.account_id)
        if ok:
            return False
        logger.warning(f"[{self.account_id[:8]}] {where} 被人工紧急停止冻结: {why}")
        return True

    # ============================================================
    #  ★ 2026-08-14 视觉持仓看护（AI 实时看图管理订单 / 反转提前锁利）
    # ============================================================
    def _ensure_vision_exit(self):
        """懒初始化视觉看护：仅在主号/独立号(follow_leader=False)路径执行。

        ★★ 2026-08-16 铁律（用户纠正）：信号跟随主号、跟单复制主号——本方法只被
        _manage_positions 内主号/独立号分支调用（跟单号在镜像分支已 return，不启动本服务），
        故视觉看护推理次数与账号数 N 无关（6/100 账号永远只有 ~1 次推理），结果经
        publish_leader_exit 广播给跟单账号。

        模块级单例(get_service)保证生产者线程只起一次；provider 用闭包持有本账号
        account_id，后台线程低频渲染该账号持仓图表送 GPU1 视觉模型，缓存 ExitVote。
        _manage_positions 同步读取缓存（零延迟），作为最高优先级出场信号。
        """
        aid = self.account_id
        if aid in _VE_INITED:
            return
        try:
            from app.services.vision_exit import get_service as _get_ve
            ve = _get_ve()

            def _provider():
                try:
                    positions = get_all_positions_rescanned(aid, max_rounds=2, gap=0.4)
                    market = mt5_service.get_market_data(aid, "XAUUSD")
                    if not isinstance(market, dict) or not market.get("timeframes"):
                        # 行情取不到：仍返回持仓(让看护票 fall-back)，但标记 market=None
                        return (positions or [], None)
                    # 给每笔持仓补 open_time_epoch（视觉提示用）+ side（★ 2026-08-16 审计P1修复：
                    #   worker 序列化字段是 type(buy/sell)，vision_exit._call_vision 读 p.get("side")
                    #   恒空 → 模型看不到持仓方向，full_close/partial_close 判断失去依据）
                    try:
                        from datetime import datetime as _dt
                        for _p in (positions or []):
                            _ot = _p.get("open_time")
                            _ep = 0
                            if _ot:
                                try:
                                    _ep = _dt.fromisoformat(str(_ot)).timestamp()
                                except Exception:
                                    _ep = 0
                            _p["open_time_epoch"] = _ep
                            _pt = str(_p.get("type") or "").lower()
                            if _pt in ("buy", "sell"):
                                _p["side"] = "BUY" if _pt == "buy" else "SELL"
                            elif _p.get("side") is None:
                                _p["side"] = ""
                    except Exception:
                        pass
                    return (positions or [], market)
                except Exception:
                    return (None, None)

            ve.set_provider(_provider)
            ve.start()
            _VE_INITED.add(aid)
            logger.info(f"[视觉看护] {aid[:8]} provider 已注册并启动后台生产者线程")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[视觉看护] {aid[:8]} 初始化失败（降级：无看护票）: {e}")

    def _get_vision_exit_vote(self):
        """返回当前账号的视觉看护票（无/不可用则为空票）。"""
        try:
            from app.services.vision_exit import get_service as _get_ve
            return _get_ve().get_vote()
        except Exception:
            return None

    def _manage_positions(self, ai_decision: DebateDecision):
        """
        智能持仓管理（5 道防线 + 视觉看护）：
        1) smart_exit.evaluate_position 评估每笔持仓（4级分批+追踪+早期保本）
        2) AI 反向 + 置信度门控 → 全平
        3) ★ 视觉看护：AI 实时看图识别反转，提前锁利 / 收紧止损（最高优先级覆盖 hold）
        """
        if self._auto_exit_blocked("持仓管理"):
            return

        # ★★ 2026-08-07 Phase 1 单轮幂等守卫（V6 ExecutionController 前置语义）
        #   execute_cycle 的 Step 2 已经调过一次本方法；之后若命中「只拦开新仓」的
        #   三道闸门（E3 最大持仓 / 同向开仓冷却 / churn 抑制），代码又各调了一次。
        #   这三条都是高频路径（主号常年满仓；冷却 180s 远大于主循环 27~111s），
        #   重复执行有真实破坏性：
        #     ① L2 反转防抖被腰斩：reversal_confirm_cycles=2 的 hist 一轮内被 append
        #        两次 → 当场确认全平，"连续 N 轮确认"的防抖形同虚设，一次假反转就
        #        清掉整批持仓——而命中分支恰恰是持仓最多、最需要防抖的时候；
        #     ② 分批平仓翻倍：第二次重查拿到的是已平后的剩余量，1.0 手 50% 会变成
        #        实际平掉 75%，把分批止盈留着吃后续行情的仓位提前砍掉（违反"多赚钱"）；
        #     ③ AI 出场 Agent 被重复调用，云 token 双倍烧。
        #   反证：下方 L3 篮子锁利自带 120s 冷却挡住了重入，说明这个坑作者在 L3 上
        #   防过，L2 与分批平仓漏防。
        #   第二次调用的输入（同一个 ai_decision、同一个市场快照时点）与第一次完全
        #   相同，没有任何新增信息，跳过不会削弱保护。
        if getattr(self, "_pm_cycle_done", False):
            logger.debug(
                f"[持仓管理] {self.account_id[:8]} 本轮已执行过，跳过重复调用"
                f"（防 L2 防抖腰斩 / 分批平仓翻倍）"
            )
            return
        self._pm_cycle_done = True

        # ★ 2026-08-06 修复"智能平仓只处理单订单"根因A+B：
        #   ① 用【全持仓二次扫描】替代单次 XAUUSD 过滤快照，确保账号下所有品种/所有订单
        #      都被纳入，且 MT5 竞态漏返回的持仓也能在后续轮次补齐（去重并集）。
        #   ② AI 出场与规则引擎都将基于"完整持仓清单"决策，杜绝只处理单一/部分订单。
        positions = get_all_positions_rescanned(self.account_id, max_rounds=3, gap=0.4)
        # ★★ 2026-08-12 修复 `object of type 'NoneType' has no len()`：
        #   get_all_positions_rescanned 在全部重试轮次都失败时返回 None（表示
        #   "查询结果不可信"，与"真的空仓 []"语义不同）。原实现先 len(positions)
        #   打日志、后做 `if not positions` 守卫 —— 防御在后、崩溃在前，
        #   MT5 未就绪（如重启初期）时整个持仓管理直接抛异常被上层吞掉。
        #   修复：守卫前置，并区分两种语义各自记录。
        if positions is None:
            logger.warning(
                f"[持仓管理] {self.account_id[:8]} 全持仓查询不可信(None) → 本轮跳过"
                f"持仓管理，等待下轮（不误判空仓、不动已有持仓）"
            )
            return
        logger.debug(f"[持仓管理] account={self.account_id[:8]} 全持仓={len(positions)}")
        if not positions:
            # ★★ 2026-08-18 P0 修复：空仓时必须清零篮子峰值，否则上一笔盈利持仓的
            #   残留峰值会误杀下一批新仓（L3/SL/TP/smart_exit 等平仓路径均可能漏清）。
            if self.account_id in _BASKET_PEAK_PNL:
                _BASKET_PEAK_PNL[self.account_id] = 0.0
            return

        # ── 让 AI 真正"看见"自己当前所有持仓（非单一订单）──
        #   把全持仓摘要推到活动流 + 记录到日志，确保 AI 决策上下文包含"全部订单"，
        #   避免只围绕单笔持仓做判断。
        try:
            _summary = "; ".join(
                f"#{p.get('ticket')}({ (p.get('type') or '')[:1].upper() }) "
                f"P/L={float(p.get('profit',0)):+.2f}"
                for p in positions[:12]
            )
            if len(positions) > 12:
                _summary += f" …共{len(positions)}笔"
            logger.info(f"[持仓全景] {self.account_id[:8]} 当前持仓 {len(positions)} 笔: {_summary}")
            self._push_feed("positions_snapshot",
                            f"持仓全景 {len(positions)} 笔: {_summary}",
                            direction=getattr(ai_decision, 'decision', ''),
                            confidence=float(getattr(ai_decision, 'confidence', 0) or 0))
        except Exception as _pe:
            logger.debug(f"[持仓全景] 推送跳过: {_pe}")

        # ═══ 信号塔分流：双套逻辑（跟单 / 独立）═══
        # · 跟单(follow_leader=True)：镜像主号出场，自身不跑 L3（风控跟主号，靠主号广播）。
        # · 独立(follow_leader=False) 与主号：跑完整 AI 出场 + L3 篮子止盈（用自身DB参数）。
        is_leader = getattr(self, "_is_leader", False)
        follow_leader = getattr(self, "_follow_leader", True)
        _follower_guard_only = False
        if (not is_leader) and follow_leader:
            self._mirror_leader_exits(positions, ai_decision)
            # ★★ 2026-08-17 云AI护盘 P0 修复（用户实锤"AI 没专心护盘/大仓裸奔"）：
            #   原实现跟号在这里直接 return → L3 护盾/篮子回吐保护/篮子AI处置全不跑 →
            #   大仓(0.58手)与主号小仓(0.02手)风险差 29 倍却共用主号保护 →
            #   实测 liumanchun1 大仓浮盈 239→58 回吐无人管，用户 4404 手动平 -99.76。
            #   修复：跟号镜像后【不 return】，继续落入下方篮子级保护层
            #   （L3 护盾 / 篮子回吐 / 篮子AI处置 close_all·trim 独立执行），
            #   到逐仓 smart_exit 前才 return（SL 移动仍跟随主号信号，防双套 SL 冲突）。
            _follower_guard_only = True

        # ★★ 2026-08-18 修复跟号"篮子回吐保护"误杀新单（P0 回归·对不齐根因）★★
        #   根因：跟号持仓由主号镜像出场平掉(follower_mirror)，该路径不重置账户级
        #   _BASKET_PEAK_PNL；且本函数 positions 快照在 _mirror_leader_exits 中途平掉
        #   持仓后已陈旧 → 下方篮子块用【陈旧快照+残留峰值】把新单/已平单误判"回吐"秒平
        #   （实盘:挂号1 0.54手开1秒亏8.1被平、挂号2 0.01手开38秒亏0.28被平，主号仍开→对不齐）。
        #   修复(只影响跟号,主号/独立号走原路径)：
        #   ① 跟号镜像后重拉实时持仓喂给篮子块，杜绝陈旧快照；
        #   ② 若实时持仓面空→清零峰值（峰值只属于"当前持仓"，上一笔赢利残留峰值是误杀源）。
        #   查询失败(_fk_ok=False)则保守保持原样(不误平原则,与 L4939 同),不重置峰值。
        if _follower_guard_only:
            _fk_ok, _live_pos = _positions_checked(self.account_id, "XAUUSD")
            if _fk_ok:
                positions = _live_pos or []
                if not positions and self.account_id in _BASKET_PEAK_PNL:
                    _BASKET_PEAK_PNL[self.account_id] = 0.0
                    logger.info(
                        f"[跟号镜像] {self.account_id[:8]} 持仓面空→清零篮子峰值"
                        f"(防残留赢单峰值误杀新单)"
                    )

        # ===== L3 篮子浮盈护盾：所有持仓浮盈总和达阈值 → 全平锁利重开（防由赢转亏）=====
        # 审计修复：必须从 DB 直接读取（self.strategy 缓存旧对象，新字段为 None）
        enable_l3 = bool(self._fresh_strat("enable_l3_guard", True))

        # ═══ 篮子级 AI 持仓管理（2026-08-17·用户铁律：开完仓核心任务=维护持仓）═══
        # 消费双脑 position_action 融合结果（meta_agent 已连续 2 轮防抖确认）：
        #   close_all=全平锁利 / trim=每笔减半；置信≥BASKET_AI_MIN_CONF 才执行；
        #   执行后 120s 冷却。AI 先判断（提前量），L3 规则兜底（浮盈大）。
        # 另含规则层篮子回吐保护：浮盈从峰值回吐≥阈值 → 全平（AI 不动作时的机械兜底）。
        if positions:
            try:
                from app.config import settings as _bask_s
                _bask_enabled = bool(getattr(_bask_s, "BASKET_AI_MGMT_ENABLED", True))
                _bask_min_conf = float(getattr(_bask_s, "BASKET_AI_MIN_CONF", 0.60) or 0.60)
                _pb_on = bool(getattr(_bask_s, "BASKET_PULLBACK_ENABLED", True))
                _pb_trim_only = bool(getattr(_bask_s, "BASKET_PULLBACK_TRIM_ONLY", False))
            except Exception:
                _bask_enabled, _bask_min_conf = True, 0.60
                _pb_on, _pb_trim_only = True, False
            _basket_profit = sum(float(p.get("profit", 0) or 0) for p in positions)

            # ① AI 篮子处置（最高优先级·提前量）
            _bask_executed = False
            if _bask_enabled and ai_decision is not None:
                _ba = str(getattr(ai_decision, "basket_action", "hold") or "hold")
                _ba_conf = float(getattr(ai_decision, "basket_action_conf", 0) or 0)
                _ba_ok = bool(getattr(ai_decision, "basket_action_confirmed", False))
                _ba_reason = str(getattr(ai_decision, "basket_action_reason", "") or "")
                if _ba in ("close_all", "trim") and _ba_ok and _ba_conf >= _bask_min_conf:
                    _bc_rem = 120 - (time.time() - _BASKET_EXEC_COOLDOWN.get(self.account_id, 0))
                    if _bc_rem <= 0:
                        logger.warning(
                            f"[篮子AI处置] {self.account_id[:8]} {_ba} conf={_ba_conf:.0%} "
                            f"确认✓（{_ba_reason}）篮子浮盈={_basket_profit:+.2f}$ → 执行"
                        )
                        _bask_executed = True
                        _ok_cnt = 0
                        _fail_cnt = 0
                        _trimmed = (_ba == "trim")
                        for _bp in positions:
                            try:
                                if _ba == "trim":
                                    _vol = float(_bp.get("volume", 0) or 0)
                                    if _vol <= 0.02:   # 防碎单：0.02 手以下不减
                                        continue
                                    _close_vol = round(_vol * 0.5, 2)
                                    _cr = mt5_service.close_position(self.account_id, _bp["ticket"], _close_vol)
                                    _part = True
                                else:
                                    _cr = mt5_service.close_position(self.account_id, _bp["ticket"])
                                    _part = False
                                if "error" not in _cr:
                                    _ok_cnt += 1
                                    _ev = "partial_close" if _part else "full_close"
                                    publish_leader_exit(self.account_id, _bp["ticket"], _ev,
                                                       close_pct=0.5 if _part else None)
                                    self._record_close(ai_decision, _bp, _cr,
                                                       f"篮子AI处置·{_ba}({_ba_reason})", partial=_part)
                                    # ★ 2026-08-17 P0修复：trim 也要写 _PARTIAL_DONE 防同轮
                                    #   smart_exit 再 partial 造成碎单（与 partial 防重机制一致）
                                    if _part:
                                        try:
                                            _PARTIAL_DONE[self.account_id] = _bp["ticket"]
                                        except Exception:
                                            pass
                                else:
                                    _fail_cnt += 1
                                    logger.warning(f"[篮子AI处置] 平 {_bp.get('ticket')} 失败: {_cr.get('error')}")
                            except Exception as _be:
                                _fail_cnt += 1
                                logger.warning(f"[篮子AI处置] 平 {_bp.get('ticket')} 异常: {_be}")
                        if _ok_cnt > 0 and _fail_cnt == 0:
                            # ★ 2026-08-17 P1修复：全部成功才置冷却（对齐 L3 语义），
                            #   部分失败时下轮继续重试失败单，避免"平不干净"
                            _BASKET_EXEC_COOLDOWN[self.account_id] = time.time()
                            # ★ 2026-08-17 P1修复：执行成功 → 清除防抖确认历史，
                            #   否则同动作每轮恒 confirmed（防抖被击穿，连续减半至碎单）
                            try:
                                from app.core.meta_agent import _reset_basket_action
                                _reset_basket_action(str(self.account_id), _ba)
                            except Exception:
                                pass
                        if _ba == "close_all" and _ok_cnt > 0 and _fail_cnt == 0:
                            # ★ 2026-08-17 P0修复：AI 全平成功必须重置峰值——
                            #   否则旧峰值残留会误杀下一批新仓（回吐保护误判）
                            _BASKET_PEAK_PNL[self.account_id] = 0.0
                            # ★ 2026-08-17 P1修复：全平成功直接 return，
                            #   不再走到 L3/逐仓循环对已平仓再跑一遍（避免 stale 快照误平）
                            logger.warning(
                                f"[篮子AI处置] {self.account_id[:8]} close_all 全部成功 → "
                                f"本轮返回（峰值已重置）"
                            )
                            return
                        elif _ba == "trim" and _ok_cnt > 0:
                            # ★ 2026-08-17 P0修复（二版）：trim 后峰值按"剩余仓位比例"回摆——
                            #   一版用平仓前快照算 _after 恒≈_basket_profit（_remain_ratio≈1.0）
                            #   导致峰值没回摆、下轮必被回吐保护误全平（审计 P1-1）。
                            #   正解：平仓后无法重拉真实浮盈（快照陈旧），直接用
                            #   trim 比例近似——减半 → 峰值减半（剩余仓位同价位波动浮盈减半）。
                            _prev_peak = _BASKET_PEAK_PNL.get(self.account_id, _basket_profit)
                            _trim_pct = float(getattr(_bask_s, "BASKET_AI_TRIM_PCT", 0.5) or 0.5)
                            _new_peak = round(_prev_peak * max(0.0, 1.0 - _trim_pct), 2)
                            _BASKET_PEAK_PNL[self.account_id] = _new_peak
                            _aid8 = str(self.account_id)[:8]
                            logger.warning(
                                '[篮子AI处置] ' + _aid8 + ' trim 后峰值回摆 %.2f->%.2f',
                                _prev_peak, _new_peak,
                            )
                            # trim 全部成功 → 本轮 return，防止 L3 用 stale 快照误平剩余仓
                            if _fail_cnt == 0:
                                logger.warning(
                                    '[篮子AI处置] ' + _aid8 + ' trim 全部成功 → 本轮返回'
                                )
                                return
                    else:
                        logger.debug(
                            f"[篮子AI处置] {_ba} 冷却剩余 {_bc_rem:.0f}s（120s 内已执行过）"
                        )
                elif _ba in ("close_all", "trim") and not _ba_ok:
                    logger.debug(f"[篮子AI处置] {_ba} 确认中：{getattr(ai_decision, 'basket_action_confirm_note', '')}")

            # ② 规则层篮子回吐保护（AI 不动作时的机械兜底·用户案例直接修法）
            if _pb_on and not _bask_executed:
                # ★★ 2026-08-17 P0 用户理念对齐：盈利即护盘·回撤一点就跑 ★★
                #   旧阈值（floor=6$ / 回吐≥50%或8$）与用户理念严重不符：
                #   ① 0.01 手浮盈 $3.5 永远进不了利润区（<6$）→ 小浮盈不护盘；
                #   ② 回吐 50% 才跑（用户："10回到9就应该跑"=回吐10%）；
                #   ③ 用户："几美金也可以，为什么非要等到亏损" → 利润区地板降到 0.5 点。
                #   修法：阈值全部按【总手数】动态换算（与 smart_exit 逐笔同口径）：
                #     利润区地板 = 0.5 点 × 总手数 × 100（$）
                #     回吐绝对下限 = 0.30 点 × 总手数 × 100（$）
                #     回吐比例 = 峰值 5%（用户"回撤一点就跑"）
                _basket_vol = sum(max(float(p.get("volume", 0) or 0), 0.01) for p in positions) or 0.01
                _pb_floor_eff = 0.5 * _basket_vol * 100.0          # 利润区美元地板
                _pb_abs_eff = 0.30 * _basket_vol * 100.0           # 回吐绝对下限美元
                _pb_pct_eff = 0.05                                  # 峰值 5%
                # ★★ 2026-08-17 P0 修复：峰值键从未写入 → 回吐保护永远不触发 ★★
                #   原写法 `_BASKET_PEAK_PNL.get(self.account_id, _basket_profit)`：
                #   首次调用 dict 无键 → get 返回【当前浮盈】→ `_basket_profit > _peak`
                #   恒为 False → 键永不写入 → 下一轮 _peak 又是当前值 → 回吐恒为 0 →
                #   保护形同虚设。实锤：2877213e 大仓 0.48 手 23:36 浮盈 +72.96 →
                #   23:38 回吐到 +0.48 → 23:40 转亏 -27.36 全程无人管。
                #   与 ai_exit._track_mfe 曾修过的同款 bug（那边已修，这里漏了）。
                #   改为 None 判空 + 首次必存（与 _track_mfe 同款修法）。
                _peak = _BASKET_PEAK_PNL.get(self.account_id)
                if _peak is None or _basket_profit > _peak:
                    _BASKET_PEAK_PNL[self.account_id] = _basket_profit
                    _peak = _basket_profit
                if _peak >= _pb_floor_eff and _peak > 0:
                    # ★ 2026-08-17 语义修正：floor 判定用【峰值】而非当前浮盈——
                    #   若用当前浮盈，+10 回吐到 +5（跌破 floor 6）时保护反而关闭，
                    #   正是用户案例"三单 +10 回吐到亏损"的痛点。峰值达标即启用。
                    _pull = _peak - _basket_profit
                    _th = max(_peak * _pb_pct_eff, _pb_abs_eff)
                    if _pull >= _th:
                        logger.warning(
                            f"[篮子回吐保护] {self.account_id[:8]} 峰值{_peak:+.2f}→"
                            f"当前{_basket_profit:+.2f} 回吐{_pull:.2f}≥{_th:.2f} → "
                            f"{'减半' if _pb_trim_only else '全平'}锁利"
                        )
                        _ok_cnt = 0
                        for _bp in positions:
                            try:
                                if _pb_trim_only:
                                    _vol = float(_bp.get("volume", 0) or 0)
                                    if _vol <= 0.02:
                                        continue
                                    _cr = mt5_service.close_position(self.account_id, _bp["ticket"],
                                                                     round(_vol * 0.5, 2))
                                    _part = True
                                else:
                                    _cr = mt5_service.close_position(self.account_id, _bp["ticket"])
                                    _part = False
                                if "error" not in _cr:
                                    _ok_cnt += 1
                                    publish_leader_exit(self.account_id, _bp["ticket"],
                                                        "partial_close" if _part else "full_close",
                                                        close_pct=0.5 if _part else None)
                                    self._record_close(ai_decision, _bp, _cr,
                                                       "篮子回吐保护·锁利", partial=_part)
                                else:
                                    logger.warning(f"[篮子回吐保护] 平 {_bp.get('ticket')} 失败: {_cr.get('error')}")
                            except Exception as _be:
                                logger.warning(f"[篮子回吐保护] 平 {_bp.get('ticket')} 异常: {_be}")
                        if _ok_cnt > 0:
                            _BASKET_EXEC_COOLDOWN[self.account_id] = time.time()
                            _BASKET_PEAK_PNL[self.account_id] = 0.0  # 已锁利，重置峰值
                            return
        if enable_l3:
            basket_th = float(self._fresh_strat("basket_tp_amount", 100.0) or 100.0)
            basket_profit = sum(float(p.get("profit", 0) or 0) for p in positions)
            last_lock = _L3_LAST_LOCK.get(self.account_id, 0)
            cooldown_rem = max(0, 120 - (time.time() - last_lock))
            # 每轮输出 DEBUG 方便排查为何不触发
            logger.debug(
                f"[L3护盾] {self.account_id[:8]} 浮盈={basket_profit:+.2f}$"
                f" 阈值={basket_th:.2f}$ 冷却剩余={cooldown_rem:.0f}s"
                f" enable_l3={enable_l3}"
            )
            if basket_profit >= basket_th and (time.time() - last_lock) > 120:
                logger.info(
                    f"[L3护盾] {self.account_id[:8]} 篮子浮盈 {basket_profit:+.2f}≥{basket_th:.2f}$ "
                    f"→ 全平锁利重开"
                )
                # ★ 2026-08-15 审计P1修复：原实现平仓循环后无条件 return + 无条件置 120s
                #   冷却——部分平仓失败时 L2 反转/追踪止损当轮全被跳过，且冷却窗口内
                #   不再重试，回吐亏损放大。改为：全部平成功才 return+置冷却；
                #   有失败则继续走下方 L2/追踪保护（失败单下轮由 L3 或 L2 接管）。
                _l3_all_ok = True
                for p in positions:
                    try:
                        cr = mt5_service.close_position(self.account_id, p["ticket"])
                        if "error" not in cr:
                            publish_leader_exit(self.account_id, p["ticket"], "full_close")
                            self._record_close(ai_decision, p, cr, "L3篮子浮盈锁利", partial=False)
                        else:
                            _l3_all_ok = False
                            logger.warning(f"[L3护盾] 平 {p.get('ticket')} 失败: {cr.get('error')}")
                    except Exception as _e:
                        _l3_all_ok = False
                        logger.warning(f"[L3护盾] 平 {p.get('ticket')} 异常: {_e}")
                if _l3_all_ok:
                    _L3_LAST_LOCK[self.account_id] = time.time()
                    # ★★ 2026-08-18 P0 修复：L3 全平锁利后必须重置篮子峰值，
                    #   否则残留峰值会把下一批新仓误判为"回吐"而秒平。
                    _BASKET_PEAK_PNL[self.account_id] = 0.0
                    logger.info(
                        f"[L3护盾] {self.account_id[:8]} 全平锁利成功 → 篮子峰值已清零"
                    )
                    return
                logger.warning(
                    f"[L3护盾] {self.account_id[:8]} 部分平仓失败 → 不置冷却、"
                    f"本轮继续走 L2 反转/追踪止损保护（失败单下轮重试）"
                )

        # ★ 2026-08-17 P0 修复：跟号在跑完篮子级保护（L3/回吐/篮子AI处置）后在此收尾，
        #   不进入逐仓 smart_exit/AI出场——SL 移动跟随主号信号（mirror 已处理），
        #   避免跟号独立移动 SL 与主号信号双套逻辑冲突/互相覆盖。
        if _follower_guard_only:
            # ★★ 2026-08-17 P0 修复：跟号 SL 缺失对账补设。
            #   实测：跟号 MT5 端 SL=0（trades 记账有 SL=4386.96/4380.02 但实盘缺失，
            #   dashboard 三次确认 sl=0.0，主号同路径显示正常排除显示 bug）→ 大仓 0.5 手裸奔。
            #   根因可能是券商市场执行模式拒绝订单内 SL（需成交后 modify）或信号塔信号缺 sl。
            #   无论根因，每轮对账补设是兜底：记账 SL > 0 且 MT5 端 sl=0 → 补设（失败下轮重试）。
            self._repair_follower_sl(positions)
            return

        # 取当前真实 ATR（从市场快照，轻量降级）
        try:
            snap = self.debate_engine.market.get_market_snapshot()
            current_atr = float(
                (snap.get("volatility_metrics", {}) or {}).get("h1_atr")
                or (snap.get("volatility_metrics", {}) or {}).get("d1_atr")
                or 20
            )
        except Exception as e:
            logger.warning(f"[持仓管理] get_market_snapshot 失败: {e}, 降级 ATR=20")
            snap = {}
            current_atr = 20.0

        # ★ 结构确认锚点（2026-08-12）：把快照里的 SMC 结构偏向缓存到 self，
        #   供第⑥道防线 _adverse_move_exit 门控「真反转才砍、健康回踩不砍」。
        #   snap["smc_features"] 由 market_analyzer 每周期 compute_smc 现成算出，零额外开销。
        #   只取 bullish/bearish 明确偏向（neutral/空 不写，保留原价格阈值行为，不挡）。
        try:
            _smc = (snap or {}).get("smc_features") or {}
            _gb = str(_smc.get("global_bias") or "").lower()
            _h1b = str(((_smc.get("per_tf") or {}).get("H1") or {}).get("bias") or "").lower()
            _bias = _gb if _gb in ("bullish", "bearish") else _h1b
            if _bias in ("bullish", "bearish"):
                self._adverse_struct = {"bias": _bias, "ts": time.time()}
        except Exception:
            pass

        logger.info(f"[持仓管理] {self.account_id[:8]} 正在检查 {len(positions)} 笔持仓, ATR={current_atr:.1f}")

        # ★★ 2026-08-17 用户理念 P0 修正：篮子级回吐锁利（要么全平要么全持有）★★
        #   用户实盘纠正（22:59）：主号两笔 SELL 都盈利（+8/+1.8），回吐锁利逐笔独立评估
        #   → 只平了触发阈值的第一笔（+3.34），第二笔刚开无峰值 → 留着 → 转亏 -1.26。
        #   用户原话："两个仓位都盈利+10，最后只平一个仓才走，10回到9就应该跑"。
        #   修复：同向多笔时按**篮子整体**评估回吐——篮子峰值浮盈(ΣMFE)回吐≥10% → 全平所有同向笔，
        #   不再"平一仓留一仓"。
        _basket_retrace_done = False
        try:
            from app.services import ai_exit as _ai_exit2
            _dir_grp: dict = {}
            for p in positions:
                if not isinstance(p, dict):
                    continue
                _t = str(p.get("type") or "").lower()
                _dir_grp.setdefault(_t, []).append(p)
            for _t, _grp in _dir_grp.items():
                if len(_grp) < 2 or _basket_retrace_done:
                    continue
                _bk_peak = 0.0
                for p in _grp:
                    _ai_exit2._track_mfe(self.account_id, p.get("ticket"), float(p.get("profit") or 0))
                    _bk_peak += float(_ai_exit2._EXIT_MFE.get(
                        (self.account_id, str(p.get("ticket"))), 0.0) or 0.0)
                _bk_cur = sum(float(p.get("profit") or 0) for p in _grp)
                _bk_vol = sum(max(float(p.get("volume") or 0), 0.01) for p in _grp)
                _bk_retrace = _bk_peak - _bk_cur
                # 利润区（篮子浮盈 ≥0.5 点×总手数——用户"小浮盈即护盘"）+
                # 回吐 ≥max(峰值5%, 0.30点×总手数) —— 用户"回撤一点就跑，几美金也锁"
                _bk_pt_start = 0.5 * _bk_vol * 100.0
                _bk_retrace_min = max(_bk_peak * 0.05, 0.30 * _bk_vol * 100.0)
                if _bk_peak >= _bk_pt_start and _bk_retrace >= _bk_retrace_min - 0.005:
                    logger.warning(
                        f"[篮子级回吐锁利] {self.account_id[:8]} {_t}×{len(_grp)}笔 "
                        f"篮子峰值{_bk_peak:.2f}$→当前{_bk_cur:.2f}$ 回吐{_bk_retrace:.2f}"
                        f"≥{_bk_retrace_min:.2f} → 全平{len(_grp)}笔（要么全平要么全持有）"
                    )
                    for _bp in _grp:
                        try:
                            _cr = mt5_service.close_position(self.account_id, _bp["ticket"])
                            if "error" not in _cr:
                                publish_leader_exit(self.account_id, _bp["ticket"], "full_close")
                                self._record_close(ai_decision, _bp, _cr, "篮子级回吐锁利(全平)")
                        except Exception as _be:
                            logger.warning(f"[篮子级回吐锁利] 平 {_bp.get('ticket')} 异常: {_be}")
                    _basket_retrace_done = True
        except Exception as _bre:
            logger.warning(f"[篮子级回吐锁利] 计算失败(跳过,逐笔回吐仍兜底): {_bre}")

        ai_conf = float(getattr(ai_decision, 'confidence', 0) or 0)

        # ===== M1: AI 出场决策（优先），失败/超时整批回退 smart_exit 规则引擎 =====
        ai_exit_on = bool(self._fresh_strat("ai_exit_enabled", True))
        exit_decisions: dict = {}
        if ai_exit_on and self.exit_agent is not None and positions:
            _md = getattr(self.debate_engine, "_last_market_data", {}) or {}
            market_context = self._build_exit_context(snap, ai_decision, _md)
            try:
                exit_decisions = self.exit_agent.evaluate(
                    positions, current_atr, self.strategy, market_context,
                    ai_decision=ai_decision.decision, ai_confidence=ai_conf)
            except Exception as e:
                logger.warning(f"[AI出场] 评估异常→回退规则引擎: {e}")
                exit_decisions = {}

        # ★★ 2026-08-07 Phase 1：L2 反转防抖的「本轮已推进」缓存。
        #   _REVERSAL_STATE 的计数器是按【持仓方向】聚合的（key=pos["type"]），
        #   但推进动作原本写在下面这个【按每笔持仓】迭代的循环里 —— 两者粒度不匹配：
        #     持 1 笔 SELL：要 2 轮才确认（符合设计）
        #     持 2 笔 SELL：第 1 笔把计数推到 1、第 2 笔推到 2 → **同一轮当场确认平仓**
        #   注释写的是"连续 N 轮同向确认才全平"，N 指决策周期，不是持仓笔数。
        #   主号常年堆 8~9 笔同向仓，等于每轮都能把计数推满 → 防抖被完全击穿，
        #   一次假反转就砍掉一批仓（且持仓越多砍得越多）。
        #   修法：每轮对每个方向只推进一次，同轮其余持仓复用该结论。
        _rev_bumped: dict = {}      # pos_type -> 本轮是否已确认（True=该方向本轮该全平）

        # ★ 2026-08-14 视觉持仓看护：确保后台生产者已启动，并同步读取缓存看护票（零延迟）。
        #   视觉看护票作为**最高优先级出场信号**覆盖 smart_exit 的 hold（但硬 SL/TP 与
        #   L3 篮子仍为最终兜底）；仅在主号/独立路径生效（跟号在上方已镜像返回）。
        self._ensure_vision_exit()
        _vision_exit_vote = self._get_vision_exit_vote()
        _vision_exit_by_ticket: dict = {}
        if _vision_exit_vote and getattr(_vision_exit_vote, "available", False):
            for _d in _vision_exit_vote.decisions:
                _vision_exit_by_ticket[_d.ticket] = _d
            logger.debug(
                f"[视觉看护] {self.account_id[:8]} 取票: "
                + ("; ".join(f"#{d.ticket}→{d.action}({d.confidence:.0%})"
                             for d in _vision_exit_vote.decisions if d.action != "hold") or "全 hold")
            )
        _vision_exit_min_conf = 0.6
        _vision_exit_on = True
        try:
            from app.config import settings as _s
            _vision_exit_min_conf = float(getattr(_s, "VISION_EXIT_MIN_CONF", 0.6) or 0.6)
            _vision_exit_on = bool(getattr(_s, "VISION_EXIT_ENABLED", True))
        except Exception:
            pass

        for pos in positions:
            try:
                # === 第 1 步：规则引擎保本/追踪(硬地板) 永远先算 + AI 出场决策(M1) 叠加 ===
                # ★ 硬地板修复（2026-08-04）：无论 M1 是否接管该笔持仓，规则引擎的保本/追踪
                #   SL 永远先计算，作为"不可被跳过的地板"。M1 接管时只取它的 action/reason，
                #   new_sl 必须经由 _merge_hard_floor_sl 取"规则引擎 vs M1"更锁利者——
                #   杜绝"M1 命中缓存返回 hold 无 new_sl → 浮盈单失去确定性保本 → 由赚变亏"。
                # ★ 2026-08-10 浮盈回吐锁利：把 ai_exit 跟踪的 MFE 峰值(美元)换算成
                #   价格偏移(peak_move, 与 smart_exit 内部 move 同单位)传给规则引擎，
                #   让"浮盈从峰值回吐≥30%"能主动全平锁利（补 AI 只判反向才平的盲区）。
                try:
                    # ★ 2026-08-10 双 bug 修复：
                    #   ① `_ai_exit` 在本函数范围内未定义（旧 except 吞掉 NameError → peak_move=None → 永远不触发）
                    #   ② _EXIT_MFE 只在 ai_exit.evaluate_exit 里更新；若 AI 不触发出场，峰值不更新 → 重启后内存空
                    from app.services import ai_exit as _ai_exit
                    _cur_profit_usd = float(pos.get("profit") or 0)
                    _ai_exit._track_mfe(self.account_id, pos.get("ticket"), _cur_profit_usd)
                    _mfe_usd = float(_ai_exit._EXIT_MFE.get(
                        (self.account_id, str(pos.get("ticket"))), 0.0) or 0.0)
                    _pos_vol = max(float(pos.get("volume") or 0), 0.01)
                    _peak_move = round(_mfe_usd / (_pos_vol * 100.0), 3)
                except Exception:
                    _peak_move = None
                rule_plan = smart_evaluate_position(
                    position=pos,
                    atr=current_atr,
                    ai_decision=ai_decision.decision,
                    ai_confidence=ai_conf,
                    strategy=self.strategy,
                    ai_reverse_th=float(self._fresh_strat("ai_reverse_close_confidence", 0.42) or 0.42),
                    quality_regime=getattr(ai_decision, "quality_regime", "") or "",
                    chronos_tp_ceiling=getattr(ai_decision, "chronos_tp_ceiling", None),
                    chronos_p10=getattr(ai_decision, "chronos_p10", None),
                    peak_move=_peak_move,
                )
                rule_new_sl = rule_plan.get("new_sl")

                m1_plan = exit_decisions.get(str(pos.get("ticket")))
                _m1_driven = m1_plan is not None   # 该笔出场决策是否由 M1(AI) 驱动；None=纯规则引擎(smart_exit)
                if m1_plan is None:
                    # 无 M1 决策 → 完全用规则引擎（含保本/追踪/分批/反向），与原逻辑一致
                    plan = rule_plan
                else:
                    # 有 M1 决策 → action/reason 用 M1；new_sl 走硬地板合并（保本/追踪不可被跳过）
                    merged_sl = _merge_hard_floor_sl(
                        pos_type=(pos.get("type") or "").lower(),
                        current_sl=float(pos.get("sl") or 0),
                        rule_new_sl=rule_new_sl,
                        m1_new_sl=m1_plan.get("new_sl"),
                    )
                    plan = dict(m1_plan)
                    plan["new_sl"] = merged_sl
                action = plan.get("action")
                reason = plan.get("reason", "")

                # ===== ★ 2026-08-14 Position Manager（纯加法·AI 自主仓位管理）=====
                # 让 AI 大脑「按行情自主管理仓位」：利润走不动立马平仓、开错单找最小亏损位置平。
                # 优先级：stall 机械平仓 > min_loss 最小亏损平 > 本地 8B 追踪锁利。
                # 仅增强 plan，不新增订单类型；复用现有红线(亏损单保护/硬地板/防碎单)。
                if self.position_manager is not None:
                    try:
                        _pm_plan = self.position_manager.evaluate(
                            pos, current_atr, self.strategy, snap)
                        if _pm_plan:
                            _pm_act = _pm_plan.get("action")
                            _pm_profit = float(pos.get("profit", 0) or 0)
                            if _pm_act == "full_close":
                                plan = dict(plan)
                                plan["action"] = "full_close"
                                plan["reason"] = _pm_plan.get("reason", "PositionManager")
                                if _pm_plan.get("min_loss_exit"):
                                    # 开错单·最小亏损平：PM 已用确定性 M5 反转门槛把关，
                                    # 显式豁免下方「亏损单保护」拦截（避免越亏越多）。
                                    plan["_pm_min_loss"] = True
                                action = "full_close"
                                reason = plan["reason"]
                                logger.info(
                                    f"[仓位管家] {self.account_id[:8]} #{pos.get('ticket')} "
                                    f"触发全平: {plan['reason']}")
                            elif _pm_act == "trail_tighten" and _pm_plan.get("new_sl") is not None and _pm_profit >= 0:
                                merged_sl = _merge_hard_floor_sl(
                                    pos_type=(pos.get("type") or "").lower(),
                                    current_sl=float(pos.get("sl") or 0),
                                    rule_new_sl=rule_new_sl,
                                    m1_new_sl=_pm_plan.get("new_sl"),
                                )
                                plan = dict(plan)
                                plan["new_sl"] = merged_sl
                                plan["reason"] = _pm_plan.get("reason", "PositionManager 追踪锁利")
                                reason = plan["reason"]
                                logger.info(
                                    f"[仓位管家] {self.account_id[:8]} #{pos.get('ticket')} "
                                    f"追踪锁利 SL→{merged_sl}")
                    except Exception as e:
                        logger.warning(f"[仓位管家] 合并异常→跳过: {e}")

                # ===== ★ 亏损单保护(2026-08-12)：根治「AI 出场甩鞭把盈利单砍成巨亏」 =====
                # PF 崩塌归因(当日 14:30 后)：AI 出场(full_close)接管了本该由经纪商硬止损处理的
                #   浮亏单，把可盈利单提前砍成巨亏——净盈亏比从 3.43 崩到 0.39(R:R 2.20→0.35)。
                #   同段 mt5_closed_external_verified(经纪商执行平仓) 全部净为正，证明方向/信号没问题，
                #   问题纯粹在 AI 在浮亏单上「噪音砍仓」(whipsaw / 甩鞭)。
                # 准则(提准非拦截)：禁止 AI 在【浮亏单】上执行 full_close / partial_close；
                #   亏损单一律交经纪商硬止损 + L2 反转确认(reverse_signal 分支)兜底——
                #   AI 只负责【盈利单】的追踪锁利(让利润奔跑)。这与「砍信号/加过滤」无关，
                #   只是让出场端更准(提准)，符合用户「多交易多赚钱 + 赚钱不亏本」双目标。
                # 范围：① M1(AI) 驱动的浮亏 full_close  → 必拦(hold)
                #       ② M1(AI) 驱动的浮亏 partial_close → 必拦(避免锁定部分亏损)
                #       ③ 规则引擎顺势浮亏 full_close → 维持原顺势保护(体制未翻转不追噪音)
                # 例外：reverse_signal 走下方独立分支(连续 N 轮确认，正确过滤假反转)，不受此拦。
                _reg_now = ((snap or {}).get("regime") or {})
                if isinstance(_reg_now, dict):
                    _reg_now = _reg_now.get("regime", "")
                else:
                    _reg_now = str(_reg_now)
                _pos_dir = (pos.get("type") or "").lower()
                _with_trend = (
                    (_pos_dir == "buy" and _reg_now in ("trend_up", "strong_uptrend"))
                    or (_pos_dir == "sell" and _reg_now in ("trend_down", "strong_downtrend"))
                )
                _pos_profit = float(pos.get("profit", 0) or 0)
                _float_loss = _pos_profit < 0
                _block_reason = ""
                # ★ 2026-08-14 显式豁免：Position Manager 的「开错单·最小亏损平」已用确定性
                #   M5 反转门槛(反转动能 + 结构破位)双确认，等价于正确的止损动作，
                #   不被亏损单保护拦截（否则逆势错单越亏越多）。
                if plan.get("_pm_min_loss"):
                    _block_reason = ""
                else:
                    # ★ 2026-08-12 纠偏（用户实测·逆势SELL平掉是对的）：亏损单保护仅对「顺势浮亏单」
                    #   生效(让回撤恢复/交经纪商硬止损)，逆势浮亏单(如 bullish 里的 SELL)AI 砍仓=正确
                    #   止损，绝不强制 hold(否则越亏越多)。故 AI 分支加 _with_trend 闸门。
                    if action == "full_close" and _float_loss and _m1_driven and _with_trend:
                        _block_reason = "[AI亏损单保护] 顺势AI浮亏单禁止提前砍仓→hold(交经纪商硬止损+L2反转确认)"
                    elif action == "partial_close" and _float_loss and _m1_driven and _with_trend and plan.get("close_pct", 0) > 0:
                        _block_reason = "[AI亏损单保护] 顺势AI浮亏单禁止部分砍仓→hold(避免锁定部分亏损)"
                    elif action == "full_close" and _with_trend and _float_loss and _reg_now not in ("", "unknown"):
                        _block_reason = "[顺势保护] 顺势浮亏单不追噪音砍仓→hold(等体制翻转/结构破位或机械SL)"
                if _block_reason:
                    action = "hold"
                    reason = _block_reason
                    plan = dict(plan)
                    plan["action"] = "hold"
                    plan["reason"] = reason
                    logger.info(
                        f"[亏损单保护] ticket={pos.get('ticket')} {_pos_dir} "
                        f"浮亏{_pos_profit:+.2f}$ 决策源={'AI' if _m1_driven else '规则'}→拦截转hold"
                    )

                # ===== ★★ 2026-08-14 视觉持仓看护最高优先级覆盖（用户核心诉求落地）=====
                #   让 AI 模型实时看图(多周期蜡烛)管理订单：识别反转结构 → 提前锁利润 / 收紧止损。
                #   仅当视觉票建议「非 hold 且置信达阈值」时覆盖当前 action（smart_exit 的 hold 被接管）；
                #   硬 SL/TP 被扫 与 L3 篮子锁利 仍为最终兜底（不被本覆盖取代）。
                #   亏损单保护一视同仁：视觉要在浮亏单上 full/partial close 仍被拦(转hold)，
                #   只有「盈利单平仓 / tighten_sl(永不增风险)」放行——与既有亏损单保护铁律一致。
                _vdec = _vision_exit_by_ticket.get(pos.get("ticket"))
                if _vision_exit_on and _vdec and _vdec.action != "hold" and _vdec.confidence >= _vision_exit_min_conf:
                    _vact = _vdec.action
                    if _vact == "tighten_sl":
                        _vnew = float(_vdec.new_sl or 0)
                        if _vnew > 0:
                            _cur_sl = float(pos.get("sl") or 0)
                            # 只收紧(降低风险)：BUY 上移SL / SELL 下移SL，绝不放松
                            _eff = max(_cur_sl, _vnew) if _pos_dir == "buy" else min(_cur_sl, _vnew)
                            if _eff != _cur_sl and _eff > 0:
                                plan = dict(plan)
                                plan["new_sl"] = _eff
                                reason = f"[视觉看护] 收紧SL→{_eff:.2f} ({_vdec.reason})"
                                logger.info(
                                    f"[视觉看护] {self.account_id[:8]} #{pos.get('ticket')} "
                                    f"tighten_sl→{_eff:.2f} conf={_vdec.confidence:.0%}")
                    elif _vact in ("full_close", "partial_close"):
                        if _float_loss and not plan.get("_pm_min_loss"):
                            logger.info(
                                f"[视觉看护] #{pos.get('ticket')} 浮亏单→尊重亏损单保护转hold"
                                f"（交硬止损+L2反转确认）")
                        else:
                            plan = dict(plan)
                            plan["action"] = _vact
                            if _vact == "partial_close":
                                _pc = max(0.0, min(1.0, float(_vdec.close_pct or 0)))
                                # ★ 2026-08-16 审计终检修复：close_pct 越界时按 full_close 语义处理——
                                #   跟号镜像要求 partial 区间 [0.05,0.95]，越界会被忽略 → 主平跟留背离。
                                #   ≥0.95 视为全平（与下方 partial 分支 vol_close>=vol_total 转全平一致）。
                                if _pc >= 0.95:
                                    _vact = "full_close"
                                    plan["action"] = "full_close"
                                else:
                                    plan["close_pct"] = _pc
                            plan["reason"] = f"[视觉看护] {_vact}({_vdec.confidence:.0%}) {_vdec.reason}"
                            action = _vact
                            reason = plan["reason"]
                            logger.info(
                                f"[视觉看护] {self.account_id[:8]} #{pos.get('ticket')} "
                                f"{_vact} conf={_vdec.confidence:.0%}: {_vdec.reason}")
                    # 视觉票置信不足已在上游 _parse 强制为 hold；此处再兜底一次

                # ===== L2 反转意图：连续 N 轮同向确认才全平（防抖，避免黄金假反转反复洗）=====
                # N 来自本账号策略 reversal_confirm_cycles（默认 2），真正可配、按账号隔离。
                # ★ 审计修复(2026-08-05)：状态持久化到文件，进程重启不再丢失。
                if action == "reverse_signal":
                    need = int(self._fresh_strat("reversal_confirm_cycles", 2) or 2)
                    _REVERSAL_STATE.setdefault(self.account_id, {})
                    st = _REVERSAL_STATE[self.account_id]
                    _ptype = pos["type"]
                    if _ptype in _rev_bumped:
                        # 本轮该方向已经推进过计数：同方向的其余持仓直接复用结论，
                        # 绝不再推一次（否则 N 笔持仓 = N 次确认，防抖被笔数击穿）
                        confirmed = _rev_bumped[_ptype]
                    else:
                        now = time.time()
                        hist = st.get(_ptype) or []  # 历史 [(decision, time)]
                        hist = [h for h in hist if (now - h[1]) < 180]  # 仅保留窗口内
                        if hist and hist[-1][0] == ai_decision.decision:
                            hist.append((ai_decision.decision, now))
                        else:
                            hist = [(ai_decision.decision, now)]
                        st[_ptype] = hist
                        confirmed = len(hist) >= need
                        _rev_bumped[_ptype] = confirmed
                        # 每次更新后持久化（文件IO轻量：JSON <1KB）
                        _save_reversal_state()
                    if confirmed:
                        st.pop(_ptype, None)
                        _save_reversal_state()  # 确认后清除也持久化
                        close_result = mt5_service.close_position(self.account_id, pos["ticket"])
                        # ★ 2026-08-16 审计终检修复：原代码无条件广播 full_close——主号 close
                        #   瞬时失败仍广播 → 跟号平掉、主号保留 → 主跟仓背离。与下方 full_close
                        #   分支"失败绝不广播"对齐：仅成功/已不存在才广播，失败归还防抖状态重试。
                        if "error" not in close_result:
                            publish_leader_exit(self.account_id, pos["ticket"], "full_close")
                            logger.info(f"[反转平仓] ticket={pos['ticket']} {reason}")
                            self._record_close(ai_decision, pos, close_result, reason, partial=False)
                        else:
                            logger.warning(
                                f"[反转平仓] ticket={pos['ticket']} 平仓失败，不广播跟号，"
                                f"归还反转防抖状态待下轮重试: {close_result.get('error')}"
                            )
                            # 归还防抖状态（否则主号确认后不再触发，跟号也永远等不到广播）
                            st[_ptype] = hist
                            _save_reversal_state()
                    else:
                        logger.info(
                            f"[反转防抖] {self.account_id[:8]} 记录反转意图 "
                            f"{pos['type']}→{ai_decision.decision}({ai_conf:.0%}) "
                            f"{len(hist)}/{need}，待下一轮确认"
                        )
                    continue

                # 部分平仓
                # ★ 2026-08-10 防重复减半：smart_exit 的"浮盈达标锁50%"每轮循环都返回
                #   partial_close（条件持续满足）→ 若无防重，0.5手会被切成 0.25→0.125→0.06→0.01
                #   的指数碎单（实测 18:14/15/20/22 四次小份额平仓）。每笔持仓只允许 partial 一次。
                #
                # ★★ 2026-08-10 二次修正：实例变量在「每交易周期(60s) new 一个 TradeExecutor」
                #   （ai_exit.py:75 注释证实）下每轮都被清空 → 19:14/16/19 又切了 3 次
                #   （#377544705 0.5→0.25→0.12）。改用模块级 _PARTIAL_DONE dict（跨实例共享），
                #   key=(account_id, ticket)，value=首次 partial 时间戳；超过 24h 自动清理防泄漏。
                import time as _time_mod
                _pk = (self.account_id, str(pos.get("ticket")))
                if action == "partial_close" and plan.get("close_pct", 0) > 0:
                    _now_ts = _time_mod.time()
                    # 惰性清理超过 24h 的旧记录（防内存泄漏）
                    if len(_PARTIAL_DONE) > 500:
                        for _old_k in [k for k, v in _PARTIAL_DONE.items() if _now_ts - v > 86400]:
                            _PARTIAL_DONE.pop(_old_k, None)
                    if _pk in _PARTIAL_DONE:
                        logger.info(
                            f"[智能平仓] {self.account_id[:8]} #{pos['ticket']} 已减半过，"
                            f"本轮不再重复 partial（防碎单，交由回吐/SL/AI反向接管）"
                        )
                        action = "hold"
                        plan = dict(plan)
                        plan["action"] = "hold"
                    else:
                        _PARTIAL_DONE[_pk] = _now_ts
                        vol_total = float(pos.get("volume", 0) or 0)
                        vol_close = round(vol_total * float(plan["close_pct"]), 2)
                        vol_close = max(vol_close, 0.01)  # 至少平 0.01
                        if vol_close >= vol_total:
                            vol_close = 0  # 0 表示全平
                        close_result = mt5_service.close_position(self.account_id, pos["ticket"], vol_close)
                        if "error" not in close_result:
                            publish_leader_exit(self.account_id, pos["ticket"], "partial_close", close_pct=plan.get("close_pct"))
                            logger.info(
                                f"[智能平仓] ticket={pos['ticket']} {reason} "
                                f"平仓量={vol_close if vol_close else vol_total}手"
                            )
                            self._record_close(ai_decision, pos, close_result, reason, partial=True)
                        else:
                            # ★ 2026-08-15 审计P1修复：close 失败必须归还防重标记（与镜像路径
                            #   4316/4357 失败 _release_mirror 对称），否则一次瞬断后该仓
                            #   24h 内永不再 partial → 浮盈锁 50% 静默永久失效，回吐无人护。
                            _PARTIAL_DONE.pop(_pk, None)
                            logger.warning(
                                f"[智能平仓] partial 失败，归还防重标记（下轮重试）: "
                                f"{close_result.get('error')}"
                            )

                # 移动止损
                new_sl = plan.get("new_sl")
                if new_sl is not None and new_sl != float(pos.get("sl") or 0):
                    # ★ 2026-08-17 P0 修复：modify 必须同时携带 sl+tp（未改动方用当前值）——
                    #   单字段修改实测会被 STARTRADER 服务器清缺省字段（TP→0，SL→0）。
                    #   此处漏改是 2026-08-17 早间"SL/TP 合并修改"修复未覆盖的路径。
                    _cur_tp_keep = float(pos.get("tp") or 0)
                    mod = mt5_service.modify_sl_tp(self.account_id, pos["ticket"],
                                                   sl=new_sl, tp=_cur_tp_keep)
                    if "error" not in mod:
                        _leader_open = float(pos.get("price_open") or pos.get("open_price") or 0)
                        publish_leader_exit(self.account_id, pos["ticket"], "move_sl",
                                            new_sl=new_sl, leader_open_price=_leader_open)
                        logger.info(f"[智能止损] ticket={pos['ticket']} SL→{new_sl} ({reason})")
                        # ★★ 2026-08-11 P0 假账修复：SL 上移必须回写 trades.sl。
                        #   否则对账 deal 未匹配时用【原始 SL】推算平仓价 → 浮盈单记成
                        #   假亏损（实证：378596055 真实 +2.00 @4379.21，DB 记 -56.32 @4350.05）。
                        #   smart_exit 改了 MT5 端却不写回 DB = 账本与真实脱节，必须回写。
                        self._sync_trade_sl_to_db(pos["ticket"], float(new_sl))
                    else:
                        logger.warning(f"[智能止损] ticket={pos['ticket']} 改SL失败: {mod.get('error')}")

                # ★ 移动止盈（追踪止盈：让利润奔跑，2026-08-05 新增）
                # smart_exit.evaluate_position 现在返回 new_tp，每周期上移 MT5 原生 TP
                new_tp = plan.get("new_tp")
                if new_tp is not None and new_tp != float(pos.get("tp") or 0):
                    # ★ 2026-08-17 P0 修复：同上方 SL——单字段 modify 会被服务器清另一字段。
                    _cur_sl_keep = float(pos.get("sl") or 0)
                    mod = mt5_service.modify_sl_tp(self.account_id, pos["ticket"],
                                                   sl=_cur_sl_keep, tp=new_tp)
                    if "error" not in mod:
                        publish_leader_exit(self.account_id, pos["ticket"], "move_tp", new_tp=new_tp)
                        logger.info(f"[追踪止盈] ticket={pos['ticket']} TP→{new_tp} ({reason})")
                    else:
                        logger.warning(f"[追踪止盈] ticket={pos['ticket']} 改TP失败: {mod.get('error')}")

                # 全平（智能分批已全平或追踪被扫等）
                if action == "full_close":
                    close_result = mt5_service.close_position(self.account_id, pos["ticket"])
                    if "error" in close_result and "不存在" not in str(close_result.get("error", "")):
                        # 瞬时失败（MT5 context busy / 断线）重试一次，避免误判孤儿单
                        time.sleep(0.3)
                        close_result = mt5_service.close_position(self.account_id, pos["ticket"])
                    if "error" not in close_result:
                        # ★ 仅主号自身平仓成功才广播，跟号据此平仓；失败绝不广播（否则跟号平掉自己对应持仓、
                        #   主号这笔反而成孤儿，正是"主号平了跟号丢单"的根因）
                        publish_leader_exit(self.account_id, pos["ticket"], "full_close")
                        logger.info(f"[智能全平] ticket={pos['ticket']} {reason}")
                        self._record_close(ai_decision, pos, close_result, reason, partial=False)
                    elif "不存在" in str(close_result.get("error", "")):
                        # 持仓已被 MT5/上一轮平掉：仍广播，让跟号同步平掉自己的对应持仓
                        publish_leader_exit(self.account_id, pos["ticket"], "full_close")
                        logger.warning(f"[智能全平] ticket={pos['ticket']} 持仓已不存在(或已被平)，仍广播跟号同步")
                    else:
                        logger.error(
                            f"[智能全平·失败] ticket={pos['ticket']} {reason} "
                            f"错误={close_result.get('error')}（不广播，下轮重试）"
                        )
                    continue
            except Exception as e:
                # 防御性：pos 为循环变量，理论上已绑定；但若将来重构把 for 移入 try 内，
                # 异常在首次迭代赋值前抛出会导致 pos 未定义 → 二次 NameError 使整个持仓管理崩溃。
                # 故此处不直接依赖 pos，用安全取值包裹（金融系统零容忍二次崩溃）。
                try:
                    _tk = pos.get("ticket")
                except Exception:
                    _tk = "?"
                logger.warning(f"[持仓管理] 处理 ticket={_tk} 时出错: {e}")

    def _repair_follower_sl(self, positions) -> None:
        """跟号 SL 缺失补设：MT5 端 sl=0 但 trades 记账有 SL → 补设到实盘（防裸奔）。"""
        for _p in positions:
            try:
                _cur = float(_p.get("sl") or 0)
                if _cur > 0:
                    continue
                _tk = _p.get("ticket")
                _rec = None
                try:
                    if self.db is not None:
                        _rec = self.db.query(Trade).filter(Trade.mt5_ticket == _tk).first()
                except Exception:
                    _rec = None
                _sl_val = float(getattr(_rec, "sl", 0) or 0) if _rec is not None else 0.0
                if _sl_val <= 0:
                    continue
                # ★ 2026-08-17 P0 修复：modify 必须同时携带 sl+tp（服务器对缺省字段
                #   的 SLTP 修改会清 0——mirror TP 对齐曾把跟号 SL 清成 0）。补设 SL
                #   时把当前持仓 TP 一并带上，防清 TP。
                _cur_tp = float(_p.get("tp") or 0)
                _mod = mt5_service.modify_sl_tp(self.account_id, _tk, sl=_sl_val, tp=_cur_tp)
                if "error" not in _mod:
                    # ★ 2026-08-17 P0 修复：modify 返回成功但需重查验证（worker 曾缺 type
                    #   导致 DONE 假成功、SL 未落位，5 轮补设白做）。补设后立即重查持仓，
                    #   确认 MT5 端 sl 真实落位才打"修复成功"；仍缺失则打警告（下轮重试）。
                    _verify_ok = False
                    try:
                        _vpos = mt5_service.get_positions(self.account_id, symbol=None)
                        _vlist = _vpos[1] if isinstance(_vpos, tuple) else []
                        for _vp in _vlist:
                            if isinstance(_vp, dict) and int(_vp.get("ticket") or 0) == _tk:
                                _verify_ok = float(_vp.get("sl") or 0) > 0
                                break
                    except Exception:  # noqa: BLE001
                        _verify_ok = False
                    if _verify_ok:
                        logger.warning(
                            f"[跟号SL修复] {self.account_id[:8]} #{_tk} 补设 SL={_sl_val:.2f}"
                            f"（MT5 端原缺失，已修复并验证落位）"
                        )
                    else:
                        logger.warning(
                            f"[跟号SL修复] {self.account_id[:8]} #{_tk} 补设返回成功但重查仍缺失"
                            f"（SL={_sl_val:.2f}）→ 下轮重试"
                        )
                else:
                    logger.warning(f"[跟号SL修复] {self.account_id[:8]} #{_tk} 补设失败: {_mod.get('error')}")
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"[跟号SL修复] {self.account_id[:8]} #{_p.get('ticket')} 异常: {_e}")

    def _close_opposite_for_decision(self, ai_decision):
        """反转即时平仓（带二次扫描防遗漏 + 置信闸门）：AI决策方向与持仓相反且置信达标时，
        立即全平反方向仓。

        用户实盘教训（2026-08-05）：
          - 3笔SELL持仓，MT5 positions_get() 竞态只返回2笔 → 第3笔漏平 → 继续亏损
          - 浮盈$10没及时平→行情反转→亏损$60
        修复：第一轮平仓后等0.5s再查一次，发现遗漏立即补平（最多2轮扫描）。

        ★ 2026-08-17 用户铁律改定（覆盖旧"高门槛防鞭锯"）：
          平仓=防守动作，「AI 方向翻转 → 立即止损全平」优先于「等够自信再动」。
          实证：AI 翻 BUY 45%（DS55/融合票BUY58/ChronosBUY），旧门槛 0.60/0.65 永不触发
          → SELL 死扛扩亏，正是用户实盘最痛的点。
          现门槛对齐 lean 放行下限 0.42：AI 给出明确方向(≥0.42)且与持仓相反 → 立即全平。
          防鞭锯由 L2 连续确认(reversal_confirm_cycles，主号=1 即平)承担，不再用高置信门槛。
          （旧注释保留供追溯：2026-08-07 曾因 14:25:50 SELL 0.36 平 BUY @ -2718 教训收紧，
          但 0.36 < 0.42，仍被新门槛拦住——防鞭锯本质未丢，只是不再挡真反转。）
        """
        if self._auto_exit_blocked("反转即时平仓"):
            return
        action = (ai_decision.decision or "").upper()
        if action not in ("BUY", "SELL"):
            return
        # ★ 置信闸门（2026-08-17 用户铁律）：对齐 lean 放行下限 0.42，方向翻转即止损。
        _rev_conf = float(self._fresh_strat("ai_reverse_close_confidence", 0.42) or 0.42)
        _ai_conf = getattr(ai_decision, "confidence", 0.0) or 0.0
        if _ai_conf < _rev_conf - 1e-9:
            logger.info(
                f"[反转即时平仓] {self.account_id[:8]} AI决策{action} 置信{_ai_conf:.0%}"
                f"<{_rev_conf:.0%}门槛→不强制平反向仓(防鞭锯)，交由L2/机械风控"
            )
            return
        target_type = "sell" if action == "BUY" else "buy"
        opp_label = "SELL" if action == "BUY" else "BUY"
        total_closed = 0

        for scan_round in range(1, 3):  # 最多扫2轮
            # ★ 2026-08-07 Phase 1：查询失败 ≠ 没有反向仓。旧接口两者都返回 []，
            #   会让本轮直接 break，反向仓留在账上与新方向对锁 —— 而本函数存在的
            #   全部意义就是"AI 已反向，旧仓必须立刻走"。失败时继续下一轮重扫。
            _q_ok, positions = _positions_checked(self.account_id, "XAUUSD")
            if not _q_ok:
                logger.warning(
                    f"[反转即时平仓] {self.account_id[:8]} 第{scan_round}轮持仓查询失败 → "
                    f"不能据此认定无反向仓，继续重扫"
                )
                continue
            opposite = [
                p for p in positions
                if (p.get("type") or "").lower() == target_type
            ]
            if not opposite:
                break  # 确认没有反方向持仓了，结束

            logger.warning(
                f"[反转即时平仓] {self.account_id[:8]} 第{scan_round}轮扫描: "
                f"AI决策{action} → 发现{len(opposite)}笔反向{opp_label}: "
                + ", ".join(f"#{p['ticket']} P/L={float(p.get('profit',0)):+.2f}" for p in opposite[:5])
            )
            for pos in opposite:
                try:
                    cr = mt5_service.close_position(self.account_id, pos["ticket"])
                    if "error" not in cr:
                        publish_leader_exit(self.account_id, pos["ticket"], "full_close")
                        self._record_close(ai_decision, pos, cr,
                            f"AI反转即时平仓({action}清{opp_label})_第{scan_round}轮", partial=False)
                        total_closed += 1
                        logger.info(
                            f"[反转即时平仓] 第{scan_round}轮 ticket={pos['ticket']} "
                            f"已平 P/L={float(pos.get('profit',0)):+.2f}"
                        )
                    else:
                        logger.error(f"[反转即时平仓] ticket={pos['ticket']} 平仓失败: {cr.get('error')}")
                except Exception as _ce:
                    logger.warning(f"[反转即时平仓] ticket={pos.get('ticket')} 异常: {_ce}")

            if scan_round == 1:
                time.sleep(0.5)  # 等MT5刷新持仓状态

        if total_closed > 0:
            logger.info(f"[反转即时平仓] {self.account_id[:8]} 共平{total_closed}笔反向{opp_label}")

    def _fast_l3_lock(self):
        """高频双向篮子守护（供守护线程调用，零 AI）：主号 / 跟号通用。

        包含两条独立规则（阈值各自分开，客户可在前端分别调整，互不影响）：
          ① 篮子盈利锁利（L3 护盾）：所有持仓浮盈合计 ≥ basket_tp_amount → 全平锁利重开；
          ② 篮子浮亏熔断（第⑤道防线）：所有持仓浮亏合计 ≤ -hard_loss_basket_amount → 全平止损。
        另含单笔浮亏熔断（见 _check_single_loss_cut，跟号独立调用）。
        主号达标后 publish 到信号塔总线，跟号镜像；跟号自身也跑本方法做兜底（不依赖广播）。
        """
        if self._auto_exit_blocked("篮子护盾/熔断"):
            return
        # ★ 浮亏熔断 + 单笔熔断 对所有账号（主号/独立/跟号）都跑，作为安全网；
        #   盈利锁利(L3篮子)双套逻辑：跟单靠镜像主号，独立/主号跑自身。
        _is_leader = getattr(self, "_is_leader", False)
        _follow_leader = getattr(self, "_follow_leader", True)
        self._check_basket_loss_cut(is_leader=_is_leader)
        self._check_single_loss_cut(is_leader=_is_leader)
        # 第⑥道防线·反向即跑（所有账号通用，含跟号）：价格反向移动达阈值立即全平，零 AI
        self._adverse_move_exit()
        # 跟单账号到此（盈利锁利依赖镜像主号）；独立账号 + 主号继续跑自身 L3
        if (not _is_leader) and _follow_leader:
            return

        # === 独立账号 / 主号：L3 篮子盈利锁利 ===
        enable_l3 = bool(self._fresh_strat("enable_l3_guard", True))
        # ★ 2026-08-06 修复"只处理单订单"根因A：用全持仓二次扫描替代单次 XAUUSD 快照
        positions = get_all_positions_rescanned(self.account_id, max_rounds=2, gap=0.3)
        if not positions:
            return

        # ① 篮子盈利锁利
        if enable_l3:
            basket_tp = float(self._fresh_strat("basket_tp_amount", 100.0) or 100.0)
            basket = sum(float(p.get("profit", 0) or 0) for p in positions)
            last_lock = _L3_LAST_LOCK.get(self.account_id, 0)
            if basket >= basket_tp and (time.time() - last_lock) > 120:
                logger.info(
                    f"[L3快监] {self.account_id[:8]} 篮子浮盈 {basket:+.2f}≥{basket_tp:.2f}$ "
                    f"→ 全平锁利(高频)"
                )
                # ★ 2026-08-15 复检P2修复：与主循环 L3 同构——原实现失败仍无条件置冷却，
                #   失败单 120s 内不再重试（回吐放大）。改为全部平成功才 return 置冷却。
                _l3_all_ok = True
                for p in positions:
                    try:
                        cr = mt5_service.close_position(self.account_id, p["ticket"])
                        if "error" not in cr:
                            publish_leader_exit(self.account_id, p["ticket"], "full_close")
                            self._record_close(None, p, cr, "L3篮子浮盈锁利(快监)", partial=False)
                        else:
                            _l3_all_ok = False
                            logger.warning(f"[L3快监] 平 {p.get('ticket')} 失败: {cr.get('error')}")
                    except Exception as _e:
                        _l3_all_ok = False
                        logger.warning(f"[L3快监] 平 {p.get('ticket')} 异常: {_e}")
                if _l3_all_ok:
                    _L3_LAST_LOCK[self.account_id] = time.time()
                    # 账号级全清广播：仅主号广播（驱动跟号镜像）；跟号只平自己不广播
                    if _is_leader:
                        try:
                            publish_leader_exit(self.account_id, "__BASKET_CLOSE_ALL__", "basket_full_close")
                            logger.info(f"[L3快监] {self.account_id[:8]} 已广播账号级全清信号 → 跟号将清全部XAUUSD持仓")
                        except Exception:
                            pass
                else:
                    logger.warning(
                        f"[L3快监] {self.account_id[:8]} 部分平仓失败 → 不置冷却（下轮重试，防回吐放大）"
                    )

        # ② 篮子浮亏熔断（第⑤道防线，参数独立于盈利锁利）
        self._check_basket_loss_cut(is_leader=True)
        # ③ 单笔浮亏熔断
        self._check_single_loss_cut(is_leader=True)

    # ─────────────────────────────────────────────────────────────────────────
    # ★ 2026-08-15 per-position 追踪止损（2s 级）：把 smart_exit 的追踪/保本逻辑
    #   从 30~60s 主循环下沉到 2s 守护线程，仓位安全实现准实时保护。
    #
    #   设计依据（全球调研 ≥3 源交叉验证，2026-08-15）：
    #   - Pro-Scalper(XAUUSD 专精)：BE trigger 10~15pip / trail 20~25pip 利润后激活、trail 10~15pip；ATR 自适应优于固定 pip。
    #   - Scalper Gem Pro(mql5 实盘 EA)：Level1 BE → Level2 锁利 → Level3 峰值追踪 三级，以点数/USD 阈值驱动。
    #   - AlgoMatrix：ATR×1.5 追踪(M5)，"never trail tighter than 20 points on gold"（tick 级追踪会被 0.3s 影线假突破）。
    #   - mql5 论坛 2026-07 实盘讨论：trailing distance 与 trigger 分离；结构/峰值追踪优于距离追踪；固定硬 SL 作保险。
    #   - iqoption 1R/3R 框架：+1R 才激活、+3R 收紧。
    #   结论：tick 级追踪对黄金有害（噪音大），正确做法是「规则引擎 + 分级追踪 + 服务端硬 SL 保险」，且在快循环跑。
    #   参数全锁（不依赖 AI、不回问）：R=初始硬 SL 距离；BE@1.0R / Lock@2.0R(锁0.5R) / Trail@2.5R(峰值-1.5ATR,地板20pt)。
    # ─────────────────────────────────────────────────────────────────────────
    def _fast_leader_trailing(self):
        """高频 per-position 追踪止损（2s 守护线程调用，零 AI，仅主号/独立号）。

        在 _fast_l3_lock 之后调用：对每笔持仓做 BE→Lock→Trail 三级 SL 上移。
        跟号不在此自管（_follower_mirror_loop 已按相对偏移同步主号 SL，见 _mirror_leader_exits）。
        所有 SL 修改只向有利方向移动，且落 MT5 服务端——系统/AI/视觉全死也不影响兜底。
        """
        if self._auto_exit_blocked("主号追踪止损"):
            return
        _is_leader = getattr(self, "_is_leader", False)
        _follow_leader = getattr(self, "_follow_leader", True)
        # 仅主号/独立号自管追踪；跟号走镜像
        if (not _is_leader) and _follow_leader:
            return
        try:
            positions = get_all_positions_rescanned(self.account_id, max_rounds=2, gap=0.3)
        except Exception as _e:
            return
        if not positions:
            return
        atr = self._cached_leader_atr()
        if atr <= 0:
            atr = 20.0
        for pos in positions:
            try:
                self._trail_one_position(pos, atr)
            except Exception as _e:
                logger.warning(f"[主号追踪] ticket={pos.get('ticket')} 异常: {_e}")

    def _cached_leader_atr(self) -> float:
        """ATR 带 30s 缓存，避免每 2s 调 get_market_snapshot 过重。失败降级 20。"""
        _now = time.time()
        _cached = _LEADER_TRAIL_ATR.get(self.account_id)
        if _cached and (_now - _cached[0]) < 30:
            return _cached[1]
        try:
            snap = self.debate_engine.market.get_market_snapshot()
            atr = float(
                (snap.get("volatility_metrics", {}) or {}).get("h1_atr")
                or (snap.get("volatility_metrics", {}) or {}).get("d1_atr")
                or 20
            )
        except Exception:
            atr = 20.0
        _LEADER_TRAIL_ATR[self.account_id] = (_now, atr)
        return atr

    def _trail_one_position(self, pos, atr: float):
        """单笔持仓三级 SL 上移（BE/Lock/Trail）。只向有利方向移动，绝不收紧。"""
        ticket = pos.get("ticket")
        if not ticket:
            return
        open_price = float(pos.get("price_open") or pos.get("open_price") or 0)
        sl = float(pos.get("sl") or 0)
        ptype = str(pos.get("type") or "").lower()
        is_buy = ptype in ("buy", "0") or pos.get("action") == "buy"
        is_sell = ptype in ("sell", "1") or pos.get("action") == "sell"
        # 无明确方向 / 无服务端硬 SL → 跳过（绝不裸奔改 SL）
        if not (is_buy or is_sell) or open_price <= 0 or sl <= 0:
            return
        tick = mt5_service.get_tick(self.account_id, "XAUUSD")
        if "error" in tick:
            return
        cur = float(tick.get("bid") if is_buy else tick.get("ask"))
        if cur <= 0:
            return
        # 浮盈点数（向有利方向，始终 ≥0）
        profit_pts = (cur - open_price) if is_buy else (open_price - cur)
        if profit_pts <= 0:
            return  # 还没盈利，不动 SL（保留初始硬 SL）
        R = abs(sl - open_price)  # 初始风险点数
        if R <= 0:
            return
        # 峰值利润累计（跨 2s 循环），用于 Level3 峰值追踪
        peak_key = (self.account_id, int(ticket))
        _peak = _LEADER_TRAIL_PEAK.get(peak_key, profit_pts)
        if profit_pts > _peak:
            _peak = profit_pts
            _LEADER_TRAIL_PEAK[peak_key] = _peak

        new_sl = None
        # Level 1 保本(BE)：浮盈 ≥ 1.0×R → SL 移到 open + 2pt 缓冲（消除"赢转亏"）
        if profit_pts >= 1.0 * R:
            new_sl = open_price + (2.0 if is_buy else -2.0)
        # Level 2 锁利：浮盈 ≥ 2.0×R → SL 移到 open + 0.5×R（锁定半 R 利润）
        if profit_pts >= 2.0 * R:
            lock_sl = open_price + (0.5 * R if is_buy else -0.5 * R)
            new_sl = lock_sl if new_sl is None else (max(new_sl, lock_sl) if is_buy else min(new_sl, lock_sl))
        # Level 3 峰值追踪：浮盈 ≥ 2.5×R → SL = 峰值价 - max(1.5×ATR, 20pt)（只上移）
        if profit_pts >= 2.5 * R:
            trail_dist = max(1.5 * atr, 20.0)
            peak_price = open_price + (_peak if is_buy else -_peak)
            trail_sl = peak_price - (trail_dist if is_buy else -trail_dist)
            if new_sl is None:
                new_sl = trail_sl
            else:
                new_sl = max(new_sl, trail_sl) if is_buy else min(new_sl, trail_sl)

        if new_sl is None:
            return
        # ① 只向有利方向移动（BUY: 新SL>旧SL；SELL: 新SL<旧SL）
        if is_buy and new_sl <= sl + 0.01:
            return
        if is_sell and new_sl >= sl - 0.01:
            return
        # ② 留呼吸空间：新 SL 距市价至少 max(0.3×ATR, 8pt)（与 smart_exit MIN_SL_DIST 同款思想）
        min_dist = max(0.3 * atr, 8.0)
        if is_buy and (cur - new_sl) < min_dist:
            new_sl = round(cur - min_dist, 2)
        if is_sell and (new_sl - cur) < min_dist:
            new_sl = round(cur + min_dist, 2)
        # ③ 不得越过市价（BUY 新SL必须<cur；SELL 新SL必须>cur）
        if is_buy and new_sl >= cur:
            return
        if is_sell and new_sl <= cur:
            return
        # ④ 越市价后可能破坏有利性，最后再校验一次
        if is_buy and new_sl <= sl + 0.01:
            return
        if is_sell and new_sl >= sl - 0.01:
            return
        # ★ 2026-08-17 P0 修复：modify 必须同时携带 sl+tp（单字段会被服务器清另一字段）
        _cur_tp_keep = float(pos.get("tp") or 0)
        _mod = mt5_service.modify_sl_tp(self.account_id, int(ticket),
                                        sl=float(new_sl), tp=_cur_tp_keep)
        if "error" not in _mod:
            # ★ 2026-08-15 审计P1修复：与主循环路径(3475-3488)同构——SL 上移后必须
            #   （1）广播 move_sl 让跟号镜像对齐（消除时机盲区）；
            #   （2）回写 trades.sl，否则对账 deal 未匹配时用原始 SL 推算平仓价 →
            #       浮盈单记成假亏损（铁律：对账禁用量原始 SL，须用最近上移 SL）。
            try:
                _l_open = float(open_price or 0)
                publish_leader_exit(self.account_id, int(ticket), "move_sl",
                                    new_sl=float(new_sl), leader_open_price=_l_open)
            except Exception:
                pass
            try:
                self._sync_trade_sl_to_db(int(ticket), float(new_sl))
            except Exception as _se:
                logger.warning(f"[主号追踪] SL 回写 DB 失败(不影响交易): {_se}")
            logger.info(
                f"[主号追踪] {self.account_id[:8]} ticket={ticket} SL {sl:.2f}→{new_sl:.2f} "
                f"(浮盈{profit_pts:.1f}pt/R={R:.1f}/ATR={atr:.1f})"
            )

    def _check_basket_loss_cut(self, is_leader: bool):
        """第⑤道防线·篮子浮亏熔断：所有持仓浮亏合计 ≤ -hard_loss_basket_amount → 全平止损。

        与主号盈利锁利共用高频守护线程框架，但阈值独立（hard_loss_basket_amount），
        不捆绑 basket_tp_amount。is_leader=False 时仅做跟号兜底（不广播）。
        """
        if not bool(self._fresh_strat("enable_hard_loss_cut", True)):
            return
        th = float(self._fresh_strat("hard_loss_basket_amount", 50.0) or 50.0)
        if th <= 0:
            return
        # ★ 2026-08-06 修复"只处理单订单"根因A：全持仓二次扫描
        positions = get_all_positions_rescanned(self.account_id, max_rounds=2, gap=0.3)
        if not positions:
            return
        basket_loss = sum(float(p.get("profit", 0) or 0) for p in positions)  # 负数=亏
        if basket_loss <= -th:
            logger.warning(
                f"[浮亏熔断·篮子] {self.account_id[:8]} 篮子浮亏 {basket_loss:+.2f}≤-{th:.2f}$ "
                f"→ 全平止损(第⑤道防线)"
            )
            for p in positions:
                try:
                    cr = mt5_service.close_position(self.account_id, p["ticket"])
                    if "error" not in cr:
                        if is_leader:
                            publish_leader_exit(self.account_id, p["ticket"], "full_close")
                        self._record_close(None, p, cr, "第⑤道防线·篮子浮亏熔断", partial=False)
                except Exception as _e:
                    logger.warning(f"[浮亏熔断·篮子] 平 {p.get('ticket')} 失败: {_e}")
            if is_leader:
                try:
                    publish_leader_exit(self.account_id, "__BASKET_CLOSE_ALL__", "basket_full_close")
                except Exception:
                    pass

    def _check_single_loss_cut(self, is_leader: bool):
        """第⑤道防线·单笔浮亏熔断：任意一笔持仓浮亏 ≤ -hard_loss_per_trade_amount → 平该笔。

        独立参数 hard_loss_per_trade_amount，不捆绑篮子阈值。防止单笔订单无限死扛。
        is_leader=False 时仅做跟号兜底（不广播；跟号自身独立判定自身持仓）。
        """
        if not bool(self._fresh_strat("enable_hard_loss_cut", True)):
            return
        th = float(self._fresh_strat("hard_loss_per_trade_amount", 30.0) or 30.0)
        if th <= 0:
            return
        # ★ 2026-08-06 修复"只处理单订单"根因A：全持仓二次扫描
        positions = get_all_positions_rescanned(self.account_id, max_rounds=2, gap=0.3)
        if not positions:
            return
        for p in positions:
            pl = float(p.get("profit", 0) or 0)
            if pl <= -th:
                logger.warning(
                    f"[浮亏熔断·单笔] {self.account_id[:8]} ticket={p['ticket']} "
                    f"浮亏 {pl:+.2f}≤-{th:.2f}$ → 平该笔(第⑤道防线)"
                )
                try:
                    cr = mt5_service.close_position(self.account_id, p["ticket"])
                    if "error" not in cr:
                        if is_leader:
                            publish_leader_exit(self.account_id, p["ticket"], "full_close")
                        self._record_close(None, p, cr, "第⑤道防线·单笔浮亏熔断", partial=False)
                except Exception as _e:
                    logger.warning(f"[浮亏熔断·单笔] ticket={p.get('ticket')} 平仓失败: {_e}")

    # ------------------------------------------------------------------
    # ★ 2026-08-13 ③ 回头看极端进场护栏（规则优先 + LLM 边界二级确认）
    # ------------------------------------------------------------------
    def _maybe_defer_entry(self, ai_decision, current_price, atr, result) -> tuple[bool, float]:
        """进场价位对齐（根治山底/山顶追单）。

        返回 (True, 1.0)  → 本轮选择「等待价格回到 AI 的 zone 再开仓」，调用方应跳过后续开仓步骤。
        返回 (False, 1.0) → 按原逻辑立即市价开仓（含「不值得等→市价」「TTL 到期→重判」「已回到zone→点火」）。
        返回 (False, scale) → price 已跑出「值得等」区间但 AI 仍要求更优价：
                              不拦截、不漏单，按追价比例缩仓后市价开仓（scale∈[0.2,1.0]）。

        机制（纯加法·复用现有市价单路径·零新订单类型）：
          · AI 想要更优价且落在可达区间 → 推迟，每轮检查价格是否回到 zone；
          · 价格回到 zone（容差内）→ 清意图并返回 (False,1.0)，由主流程以「当前≈zone 价」市价点火；
          · TTL 到期 → 清意图返回 (False,1.0)，交回主流程（重新市价 or 重新设意图）；
          · target 不在「值得等」区间：太近→立即市价开；太远(已追出 zone)→按追价程度缩仓后市价开。
        """
        if not getattr(settings, "ENTRY_ZONE_DEFER_ENABLED", True):
            return (False, 1.0)
        if ai_decision.decision not in ("BUY", "SELL"):
            self._pending_entry = None
            return (False, 1.0)
        _target = getattr(ai_decision, "entry_price", None)
        try:
            _target = float(_target)
        except (TypeError, ValueError):
            _target = None
        # ★ 2026-08-15 审计P1修复：原硬编码[4000,5000]对当前XAU≈3300-3500完全失效，
        #   导致限价延迟入场功能死掉（任何合理入场价被判越界→退回市价）。
        #   改为相对 current_price 的动态区间(±50%)，current_price 缺失时回退宽绝对区间。
        _price_ref = current_price if (isinstance(current_price, (int, float)) and current_price > 0) else 3400.0
        if _target is None or not (_price_ref * 0.5 <= _target <= _price_ref * 1.5):
            self._pending_entry = None
            return (False, 1.0)

        _is_buy = ai_decision.decision == "BUY"
        # ★ 2026-08-17 P0 修复（ATR 自适应最小距离，海外调研 nof1.ai/MQL5 同构）：
        #   原固定 3.0 点 → 事故：AI 目标 4396 vs 市价 4393.63 差 2.37<3.0 → 否决等待 → 市价追空 -228。
        #   现 max(绝对下限 2.0, 0.15×ATR)。ATR=14 → 2.1 点 → 2.37>2.1 会正确等待。
        _min_dist = max(
            float(getattr(settings, "ENTRY_ZONE_MIN_DIST_ABS", 2.0) or 2.0),
            float(getattr(settings, "ENTRY_ZONE_MIN_DIST_ATR_MULT", 0.15) or 0.15) * float(atr or 20.0),
        )
        _max_atr = float(getattr(settings, "ENTRY_ZONE_MAX_ATR_MULT", 1.5))
        _ttl = float(getattr(settings, "ENTRY_ZONE_TTL_MIN", 30)) * 60.0
        _tol = float(getattr(settings, "ENTRY_ZONE_FILL_TOL", 2.0))
        _atr = float(atr or 20.0)

        # 「更优」方向判定：SELL 想更高(target>current)、BUY 想更低(target<current)
        if _is_buy:
            _better = _target < current_price - _min_dist
            _reachable = (current_price - _target) <= _max_atr * _atr
        else:
            _better = _target > current_price + _min_dist
            _reachable = (_target - current_price) <= _max_atr * _atr

        if not (_better and _reachable):
            self._pending_entry = None
            # ★ 2026-08-17 审计补强：AI 给了明确目标价但未满足等待条件时，必须留痕。
            #   事故教训（20:17）：AI 目标 4396 被 MIN_DIST 静默否决 → 市价追空被套，
            #   日志无任何记录 → 无法区分「AI 没给价位」与「给了价位被机械参数否决」。
            if _target is not None:
                _dist_now = (current_price - _target) if _is_buy else (_target - current_price)
                logger.info(
                    f"[{self.account_id[:8]}] 进场价位对齐：AI目标 {_target:.2f} 当前 {current_price:.2f} "
                    f"距 {_dist_now:.2f}点(ATR={_atr:.1f} min_dist={_min_dist:.2f}) "
                    f"better={_better} reachable={_reachable} → 市价开（目标太近/已跑远，不漏单）"
                )
            if _better and not _reachable and getattr(settings, "ENTRY_ZONE_CHASE_SCALE_ENABLED", True):
                # AI 仍要求更优价，但当前价已跑出「值得等」区间 → 判定为追价。
                # 按超出倍数线性缩仓：刚出区间(1×ATR) scale≈1.0；超出 1 倍(2×ATR) scale≈0.5；
                # 再远 floor=0.2 兜底。纯加法·不拦截·不漏单，只降低追单风险。
                _dist = (current_price - _target) if _is_buy else (_target - current_price)
                _chase_mult = max(1.0, _dist / (_max_atr * _atr))          # 追价倍数
                _floor = float(getattr(settings, "ENTRY_ZONE_CHASE_SCALE_FLOOR", 0.2))
                _scale = max(_floor, 1.0 - (_chase_mult - 1.0) * 0.5)
                result["errors"].append(
                    f"进场价位对齐：AI目标 {_target:.2f}，当前 {current_price:.2f} 已跑出 "
                    f"{_max_atr:.1f}×ATR 区间（追价 {_dist:.2f} / {_max_atr*_atr:.2f}），"
                    f"按 {int(_scale*100)}% 缩仓追单（提准非拦截）"
                )
                logger.info(
                    f"[{self.account_id[:8]}] 进场价位对齐：AI目标 {_target:.2f}，当前 {current_price:.2f} "
                    f"已跑出 {_max_atr:.1f}×ATR 区间，按 {_scale:.2f} 缩仓追单"
                )
                return (False, _scale)
            # 不优或更优但已远：立即市价开（不漏单）
            return (False, 1.0)

        # 到达这里：AI 想要更优价且可达。
        # ★ 2026-08-18 修复「不开单」死循环：原逻辑无论配置一律推迟入场。
        #   震荡/单向市里 AI 的 SELL 目标(如4410)常高于现价(如4399)，defer 等价格涨回 zone
        #   永远等不到→TTL到期重判又 defer→永久不开单，违反「提准非拦截·多开顺势单」。
        #   现改为：仅 ENTRY_ZONE_DEFER_ENABLED=True 才推迟；默认关闭→直接市价开
        #   （上方追价缩仓逻辑保留：目标太远仍按距缩仓，不漏单也不盲目追）。
        if not getattr(settings, "ENTRY_ZONE_DEFER_ENABLED", True):
            return (False, 1.0)
        _now = time.time()
        _pend = getattr(self, "_pending_entry", None)
        # 方向变了（或此前为 None）→ 清旧意图重设
        if _pend and _pend.get("side") != ai_decision.decision:
            _pend = None
        if _pend is None:
            self._pending_entry = {
                "side": ai_decision.decision,
                "target": _target,
                "set_at": _now,
                "expires_at": _now + _ttl,
            }
            logger.info(
                f"[{self.account_id[:8]}] 进场价位对齐: AI 目标 {_target:.2f} 优于当前"
                f"{current_price:.2f}，推迟市价开仓，等价格回到 zone（TTL {int(_ttl/60)}分钟）"
            )
            result["errors"].append(
                f"进场价位对齐：AI 目标入场 {_target:.2f} 优于当前 {current_price:.2f}，"
                f"等待价格回到该 zone 再开 {ai_decision.decision}（不追单）"
            )
            return (True, 1.0)

        # 已有意图：TTL 到期 → 放弃本轮、重判
        if _now > _pend["expires_at"]:
            logger.info(f"[{self.account_id[:8]}] 进场意图 TTL 到期({_target:.2f})，放弃本轮、重判")
            self._pending_entry = None
            return (False, 1.0)

        # 价格是否已回到 zone → 点火（清意图，主流程以当前≈zone 价市价开）
        _reached = (
            (current_price >= _pend["target"] - _tol)
            if not _is_buy
            else (current_price <= _pend["target"] + _tol)
        )
        if _reached:
            logger.info(
                f"[{self.account_id[:8]}] 价格已回到 zone({_pend['target']:.2f})，"
                f"按 AI 目标价位点火市价 {ai_decision.decision}@≈{current_price:.2f}"
            )
            self._pending_entry = None
            return (False, 1.0)

        # 仍在等 zone
        result["errors"].append(
            f"进场价位对齐：等待价格回到 {_pend['target']:.2f} 再开 {ai_decision.decision}"
            f"（当前 {current_price:.2f}，剩余 {int((_pend['expires_at']-_now)/60)}分钟）"
        )
        return (True, 1.0)

    def _pre_entry_lookback_guard(self, direction, entry_price, sl_price=None, tp_price=None):
        """下单前用实时行情做「回头看」，校验方向是否处于极端延伸位
        （山底买空 / 山顶追多）。用户铁律(2026-08-13)：发现此类极端签名，
        宁愿放弃这单也不冒风险——0.01 手已是最低下单手数，减仓=放弃，
        故极端处直接放弃不进场（不调 LLM）；仅单信号/边界冲突→送校对员
        二级确认；正常→放行。

        规则层复用 NumpyDirectionGuard（用户 2026-07-21 实证阈值）：
        价格偏离 MA > 2.5σ 且近 5 根均值 > 1.5σ 且 RSI>72/28 =
        山底买空 / 山顶追多双信号共振。阈值刻意极端，避免 broad 闸门
        砍好单（违背「多交易多赚钱」铁律）。

        返回 (block: bool, why: str, features: dict)。block=True 表示放弃进场。
        """
        if direction in (None, "HOLD", ""):
            return (False, "无方向-放行", {})
        try:
            from app.services.numpy_direction_guard import NumpyDirectionGuard
        except Exception:
            return (False, "护栏模块不可用-放行", {})
        try:
            _snap = (self.debate_engine.market.get_market_snapshot() or {}) \
                if getattr(self, "debate_engine", None) else {}
            _tfs = (_snap.get("timeframes", {}) or {})
            _closes = None
            for _tf in ("H1", "M15", "M5"):
                _d = _tfs.get(_tf, {}) or {}
                _c = _d.get("closes")
                if not _c and isinstance(_d.get("bars"), list):
                    try:
                        _c = [float(b.get("close", 0)) for b in _d["bars"]
                              if isinstance(b, dict) and b.get("close")]
                    except Exception:
                        _c = None
                if _c and len(_c) >= 60:
                    _closes = [float(x) for x in _c]
                    break
            if not _closes:
                logger.warning(
                    f"[回头看护栏] {self.account_id[:8]} {direction} H1/M15/M5 收盘序列不足，"
                    f"跳过护栏(放行)")
                return (False, "行情序列不足-放行", {})
            _cur = float(entry_price or 0)
            if _cur <= 0:
                _cp = _snap.get("current_price")
                if isinstance(_cp, dict):
                    _cur = float(_cp.get("last") or _cp.get("ask") or _cp.get("bid") or 0)
                else:
                    _cur = float(_cp or 0)
            if _cur <= 0:
                return (False, "当前价无效-放行", {})
            _g = NumpyDirectionGuard()
            _res = _g.review(_closes, _cur, direction)
            _z = float(_res.features.get("price_to_ma_z", 0.0))
            _z5 = float(_res.features.get("z_avg_5", 0.0))
            _rsi = float(_res.features.get("rsi14", 50.0))
            # 双信号共振极端签名（山底买空 / 山顶追多）：
            #   价格偏离 MA>2.5σ 且近5根均值>1.5σ(=延伸过度) 且 RSI 极端(=动量耗尽)。
            #   三者全满足 = 双信号共振 → 直接放弃不进场(不调LLM)。
            #   仅满足「价格延伸」或仅满足「RSI极端」其一 = 单信号/边界 → 送 LLM 二级确认。
            #   其余(布林带突破/趋势反向/轻度拥挤)一律放行，避免 broad 闸门砍好单。
            if direction.upper() == "BUY":
                _z_ext = (_z > NumpyDirectionGuard.Z_MAJOR and _z5 > NumpyDirectionGuard.Z_MINOR)
                _rsi_ext = (_rsi > NumpyDirectionGuard.RSI_OVERBOUGHT)
                _sig = "山顶追多"
            elif direction.upper() == "SELL":
                _z_ext = (_z < -NumpyDirectionGuard.Z_MAJOR and _z5 < -NumpyDirectionGuard.Z_MINOR)
                _rsi_ext = (_rsi < NumpyDirectionGuard.RSI_OVERSOLD)
                _sig = "山底买空"
            else:
                _z_ext = _rsi_ext = False
                _sig = ""
            _extreme = _z_ext and _rsi_ext
            _single = _z_ext != _rsi_ext  # 异或：恰好一个极端
            if _extreme:
                logger.warning(
                    f"[回头看护栏·放弃] {self.account_id[:8]} {direction} 检测{_sig}双信号共振"
                    f"(z={_z:.2f}, z5={_z5:.2f}, rsi={_rsi:.1f})→直接放弃不进场(不调LLM)")
                return (True, f"回头看护栏:{_sig}双信号共振(z={_z:.2f},rsi={_rsi:.1f})→放弃不进场",
                        _res.features)
            if _single:
                _ok, _why = self._lookback_llm_secondary(direction, _cur, _res, sl_price, tp_price)
                if not _ok:
                    return (True, f"回头看单信号边界LLM否决:{_why}", _res.features)
                return (False, f"回头看单信号边界LLM通过:{_why}", _res.features)
            return (False, "回头看规则通过", _res.features)
        except Exception as _e:
            logger.warning(f"[回头看护栏] {self.account_id[:8]} 异常: {_e} → 放行(不挡)")
            return (False, f"护栏异常-放行:{_e}", {})

    def _lookback_llm_secondary(self, direction, cur, guard_res, sl_price=None, tp_price=None):
        """边界/单信号冲突 → 送校对员(本地 qwen3)做二级确认。
        返回 (allow: bool, why: str)。校对员不可用 / 超时 / 判 major →
        保守放弃（用户铁律：宁愿放弃这单也不冒风险）。"""
        try:
            from app.services.local_llm_service import proofread
            _dec = {
                "decision": direction,
                "confidence": 0.6,
                "sl": sl_price,
                "tp": tp_price,
                "reason": f"回头看护栏二级确认(冲突:{guard_res.conflict_level}；{guard_res.reason})",
            }
            _snap = (self.debate_engine.market.get_market_snapshot() or {}) \
                if getattr(self, "debate_engine", None) else {}
            _pr = proofread(_dec, _snap)
            if _pr is None:
                logger.warning(
                    f"[回头看护栏·LLM] {self.account_id[:8]} 校对员不可用→保守放弃")
                return (False, "校对员不可用-保守放弃")
            _sev = getattr(_pr, "severity", "none")
            if _sev == "major":
                return (False, f"校对员判major:{getattr(_pr, 'issues', [])[:1]}")
            return (True, f"校对员通过(sev={_sev})")
        except Exception as _e:
            logger.warning(
                f"[回头看护栏·LLM] {self.account_id[:8]} 异常: {_e} → 保守放弃")
            return (False, f"LLM二级确认异常-保守放弃:{_e}")

    def _adverse_move_exit(self, positions=None):
        """第⑥道防线·反向即跑机械止损（零 AI）：价格反向移动 ≥ max(最小点数, SL距离×系数)
        立即全平该笔，不等 AI 置信。覆盖主号 / 独立 / 跟号所有账号。

        用户铁律（2026-08-11 16:30 复盘）：行情不对就要减少亏损，立马就要跑。
        旧有出场路径全带 AI 置信门槛（反转即时平仓需≥0.60、smart_exit L2 需反向置信+连续N轮），
        导致 SELL 开错方向后价格涨 13~28 分钟才被 SL/外部平仓止损，亏损持续扩大。
        本方法提供纯价格机械止损：价格朝不利方向走了 SL 距离的 adverse_exit_sl_mult 比例就跑，
        远早于打到 SL（≈SL距离×0.35）。属「提准加保护」不是「拦截」——只动已错向的仓，不挡新开仓。
        """
        if not bool(self._fresh_strat("enable_adverse_exit", True)):
            return
        # ★ 2026-08-12 盈亏比提准：0.35→0.6。原 0.35×1.5ATR≈0.53ATR 触发，远低于
        #   黄金共识甜蜜区 1.0~2.0×ATR，把大量正常波动的趋势单当错向砍掉（亏损单均值
        #   持仓仅 14.5min）。提到 0.6×1.5ATR≈0.9ATR：仍远快于原生 1.5ATR SL、且零 AI
        #   等待，保留「行情不对立马跑」精神；但给正常波动留空间，减少 whipsaw 误杀，
        #   直接抬升盈亏比（调研依据：jyforex 0.75~1.0×ATR 攻守兼得 / quantum-algo 1.5~2ATR）。
        _mult = float(self._fresh_strat("adverse_exit_sl_mult", 0.6) or 0.6)
        _min_pts = float(self._fresh_strat("adverse_exit_min_points", 6.0) or 6.0)
        # ★ 2026-08-13 重校准地板（提准非拦截，砍噪音误杀）：
        #   旧地板 _min_pts=6.0 是固定硬值，对 XAUUSD(ATR 常 10~15pt) 落在噪音带内
        #   (≈0.4~0.6×ATR)，把大量正常波动的趋势单当错向砍掉（历史 731 笔<8pt 噪音
        #   whipsaw 是最大出血源）。改为 ATR 自适应地板 ≈1.0×ATR（与 jyforex 0.75~1.0×ATR
        #   攻守兼得一致）：只有反向移动真正越过噪音带才砍，健康回踩不误杀。
        #   阈值 = max(ATR地板, 硬下限6pt, SL距离×系数)，三取大。
        #   对紧 SL 小账号(SL<1.0×ATR)：ATR地板>SL → 机械止损在 SL 之前永不触发，
        #   交原生 SL 兜底（已远快于旧 AI 出场 13~28min 延迟），零 whipsaw 误杀。
        _atr_floor_mult = float(self._fresh_strat("adverse_exit_atr_floor_mult", 1.0) or 1.0)
        if _mult <= 0 and _min_pts <= 0 and _atr_floor_mult <= 0:
            return
        # 取当前真实 ATR（h1 优先，d1 兜底，异常降级 20），用于自适应地板
        try:
            _snap = self.debate_engine.market.get_market_snapshot() or {}
            _vm = (_snap.get("volatility_metrics", {}) or {})
            _atr = float(_vm.get("h1_atr") or _vm.get("d1_atr") or 0)
        except Exception:
            _atr = 0.0
        if _atr <= 0:
            _atr = 20.0
        _atr_floor = _atr * _atr_floor_mult
        if positions is None:
            positions = get_all_positions_rescanned(self.account_id, max_rounds=2, gap=0.3)
        if not positions:
            return
        for p in positions:
            try:
                _dir = (p.get("type") or "").lower()
                _open = float(p.get("price_open") or p.get("open_price") or 0)
                _cur = float(p.get("price_current") or p.get("current_price") or 0)
                _sl = float(p.get("sl") or 0)
                if _open <= 0 or _cur <= 0:
                    continue
                # 反向移动距离 _adverse 与阈值均为「价格美元」单位（XAUUSD 报价即美元/盎司）：
                #   adverse_exit_min_points 默认 6.0 = 反向 $6 即视为达标下限；
                #   SL 距离(_sl_dist)同为美元，× adverse_exit_sl_mult 得动态阈值，二者取大。
                if _dir == "sell":
                    _adverse = _cur - _open          # 价格上涨=亏损
                elif _dir == "buy":
                    _adverse = _open - _cur          # 价格下跌=亏损
                else:
                    continue
                # ★ 2026-08-12 修复：_sl_dist 必须取绝对值。原代码按方向用 (_open-_sl)/(_sl-_open)，
                #   对正常仓位结果恒为负数，导致 max(_min_pts, 负数) 只 fallback 到最小阈值 6.0，
                #   把正常波动回撤误判为错向行情而提前砍仓。
                _sl_dist = abs(_sl - _open) if _sl > 0 else 0.0
                if _adverse <= 0:
                    continue  # 还在赚钱方向，不跑
                _th = max(_atr_floor, _min_pts, _sl_dist * _mult)
                # ★★ 结构确认门（2026-08-12，调研 dual-confirmation）：
                #   零 AI 机械止损只应在「真反转」时砍，健康回踩(结构仍顺向)应交给原生 SL。
                #   仅当结构偏向确认了不利方向才砍；结构仍顺向且未逼近原生SL(灰度区)→不砍。
                #   三档响应（与 QuantInsti 护栏一致，避免牛市回撤误砍）：
                #     · 结构确认反转(bias 与不利方向一致) → 照砍（满足「行情不对立马跑」）
                #     · 结构仍顺向 + 未逼近SL(灰度区) → 不砍，交原生1.5ATR硬止损
                #     · 结构未知/过期(>120s) → 退回原价格阈值行为（不挡，零回归）
                #   逼近原生SL(_adverse≥0.95×SL距离)视为极端，无论结构如何都砍（保命优先）。
                _near_sl = (_sl_dist > 0 and _adverse >= _sl_dist * 0.95)
                _struct = getattr(self, "_adverse_struct", None)
                _struct_ok = bool(_struct) and (time.time() - _struct.get("ts", 0)) < 120
                _struct_bias = (_struct or {}).get("bias", "")
                if _struct_ok and _struct_bias and not _near_sl:
                    _confirm = (_dir == "buy" and _struct_bias == "bearish") or \
                               (_dir == "sell" and _struct_bias == "bullish")
                    if not _confirm:
                        logger.debug(
                            f"[反向即跑·结构门] {self.account_id[:8]} ticket={p['ticket']} {_dir.upper()} "
                            f"反向{_adverse:.2f}<阈值{_th:.2f}? 结构仍顺向({_struct_bias})→健康回踩不砍,"
                            f"交原生SL(距离{_sl_dist:.2f})"
                        )
                        continue
                if _adverse >= _th - 1e-9:
                    logger.warning(
                        f"[反向即跑·第⑥道防线] {self.account_id[:8]} ticket={p['ticket']} {_dir.upper()} "
                        f"反向移动 {_adverse:.2f}≥阈值{_th:.2f}(ATR地板{_atr_floor:.2f}/硬下限{_min_pts}/"
                        f"SL距离{_sl_dist:.2f}×{_mult})→ 立即全平止损(零AI)"
                    )
                    cr = mt5_service.close_position(self.account_id, p["ticket"])
                    if "error" not in cr:
                        # 主号广播驱动跟号镜像；跟号自身只平自己不广播
                        if getattr(self, "_is_leader", False):
                            try:
                                publish_leader_exit(self.account_id, p["ticket"], "full_close")
                            except Exception:
                                pass
                        self._record_close(None, p, cr, "第⑥道防线·反向即跑机械止损", partial=False)
                    else:
                        logger.error(f"[反向即跑] ticket={p.get('ticket')} 平仓失败: {cr.get('error')}")
            except Exception as _e:
                logger.warning(f"[反向即跑] ticket={p.get('ticket')} 异常: {_e}")

    def _mirror_leader_exits(self, positions, ai_decision):
        """跟号镜像主号出场（信号塔）：读主号广播动作并复刻到本号对应持仓。
        零 AI 调用；MT5 原生 SL/TP 已随主号同价生效，此处补齐主号 AI 出场(分批/追踪/反转/护盾)。"""
        if self._auto_exit_blocked("镜像主号出场"):
            return
        leader = self._leader_account()
        if leader is None:
            return
        leader_id = leader.id
        # ★ 2026-08-11 智能增强：每轮查一次主号当前持仓（含 smart_exit 上移的 SL/TP），
        #   供跟号持仓主动对齐，解决 move_sl 广播对存量/新单的时机盲区。
        # ★ 2026-08-15 根治「主号平了跟号没平」：提前到循环外取一次，
        #   并构建主号当前开仓票号集合，供文末「孤儿单对账兜底」使用。
        _lk, _lps = _positions_checked(leader_id, "XAUUSD")
        _leader_open_tickets = {str(p.get("ticket")) for p in (_lps or [])} if _lk else set()
        for pos in positions:
            try:
                cm = str(pos.get("comment") or "")
                m = re.search(r"L(\d+)", cm)
                if not m:
                    continue
                lt = m.group(1)  # 主号票号（copy_order 时写入 comment: L{主号票号}）
                # ★ 主动对齐主号当前真实 SL/TP（盈利保护位同步，消除跟号裸奔）
                # 2026-08-12 修复：绝对 SL 价格不能直接复制给开仓价不同的跟号，否则
                # 主号保本位会变成跟号的"亏损/过早出场位"。改为同步"相对开仓价的偏移"。
                if _lk and _lps:
                    for _lp in _lps:
                        if str(_lp.get("ticket")) == str(lt):
                            _l_sl = float(_lp.get("sl") or 0)
                            _l_tp = float(_lp.get("tp") or 0)
                            _l_open = float(_lp.get("price_open") or _lp.get("open_price") or 0)
                            _my_sl = float(pos.get("sl") or 0)
                            _my_tp = float(pos.get("tp") or 0)
                            _my_open = float(pos.get("price_open") or pos.get("open_price") or 0)
                            # SL：用相对偏移同步，且只向更锁利方向移动
                            _sl_changed = False
                            _target_tp = _my_tp
                            if _l_sl > 0 and _l_open > 0 and _my_open > 0 and _my_sl > 0:
                                _sl_offset = _l_sl - _l_open
                                _target_sl = round(_my_open + _sl_offset, 2)
                                _is_buy = str(pos.get("type") or "").lower() in ("buy", "0") or pos.get("action") == "buy"
                                _is_sell = str(pos.get("type") or "").lower() in ("sell", "1") or pos.get("action") == "sell"
                                _move = False
                                if _is_buy and _target_sl > _my_sl:
                                    _move = True
                                elif _is_sell and _target_sl < _my_sl:
                                    _move = True
                                if _move and abs(_target_sl - _my_sl) > 0.01:
                                    _sl_changed = True
                            # TP：所有账号共享同一目标价，直接对齐绝对价格
                            _tp_changed = False
                            if _l_tp > 0 and abs(_l_tp - _my_tp) > 0.01:
                                _target_tp = _l_tp
                                _tp_changed = True
                            # ★★ 2026-08-17 P0 修复：SL/TP 修改必须【一次请求同时携带 sl+tp】★★
                            #   原实现分两次单字段修改（SL 对齐只带 sl、TP 对齐只带 tp）→
                            #   实测 STARTRADER demo 服务器对"缺省字段"的 SLTP 修改会把
                            #   缺省值清 0（跟号 SL=4417.28 开仓落位 → mirror SL 对齐后
                            #   仍 4417.1 → TP 对齐(只带tp) 后 SL 被清成 0，多轮补设全失败）。
                            #   修复：任何一次修改都同时携带 sl+tp（未改动方用当前值），
                            #   杜绝单字段请求触发服务器清空。
                            if _sl_changed or _tp_changed:
                                _mod = mt5_service.modify_sl_tp(
                                    self.account_id, pos["ticket"],
                                    sl=float(_target_sl) if _sl_changed else _my_sl,
                                    tp=float(_target_tp),
                                )
                                if "error" not in _mod:
                                    _log_parts = []
                                    if _sl_changed:
                                        _log_parts.append(f"SL {_my_sl}→{_target_sl}")
                                    if _tp_changed:
                                        _log_parts.append(f"TP {_my_tp}→{_target_tp}")
                                    logger.info(f"[跟号镜像·对齐] ticket={pos['ticket']} " + " / ".join(_log_parts))
                            break

                # ★ 2026-08-15 根治「主号平了跟号没平」的根因缺口（内存总线 180s TTL 发射后不管）：
                #   若跟号 Worker 在主号平仓那 180s 内掉线/死循环（本系统已实测两次），
                #   广播过期即永久丢失，重连后 consume 拿不到 → 跟号仓位裸奔到止损。
                #   补一道【对账兜底】：每轮用主号真实开仓集合反查——
                #   跟号持有 L{主号票号} 但该票号在主号已不复存在(已平)，
                #   且本轮总线尚未处理过该票号 → 强制平仓（彻底不依赖内存总线）。
                # ★ 2026-08-15 审计P1修复：去掉 `_lps` 非空前提——主号平掉**最后一笔**后
                #   _lps=[]，此时 _leader_open_tickets=空集，跟号 L 单不在其中恰是需要兜底的
                #   最坏场景；原 `_lk and _lps` 使该场景恒 False → 根治留口（跟号裸奔到止损）。
                #   仅保留 `_lk`（查询成功）前提：查询失败时空集不可靠，绝不能误平跟号好仓。
                if (_lk and lt and str(lt) not in _leader_open_tickets
                        and not _is_mirrored(self.account_id, lt, "full_close")
                        and not _is_mirrored(self.account_id, lt, "reconcile_close")):
                    # ★ 2026-08-17 P0修复（审计）：快监把 copy_order 提前后，
                    #   下一轮快监此处兜底可能把"刚补的新单"误平——主号持仓查询
                    #   瞬时漏返（_lk=True 但 _leader_open_tickets 缺该票）时，
                    #   跟号新补的 L 单不在集合 → 被当孤儿强平。
                    #   _reconcile_against_leader 已有 _leader_open_in_db 交叉验证，
                    #   此处同构补齐：主号 DB 该票未平（close_time IS NULL）即跳过。
                    _skip_reconcile = False
                    try:
                        _lt_rec = (
                            self.db.query(Trade)
                            .filter(Trade.mt5_ticket == str(lt))
                            .order_by(Trade.id.desc())
                            .first()
                        )
                        if _lt_rec is not None and _lt_rec.close_time is None:
                            # 主号 DB 仍视为未平 → 可能瞬时漏返，跳过本轮防误平
                            _skip_reconcile = True
                    except Exception:
                        _skip_reconcile = False  # DB 查不了则按原逻辑走（查询失败保守放行）
                    if not _skip_reconcile and _claim_mirror(self.account_id, lt, "reconcile_close"):
                        r = mt5_service.close_position(self.account_id, pos["ticket"])
                        if "error" not in r:
                            self._record_close(ai_decision, pos, r, "跟号对账兜底·主号已平本号仍持", partial=False)
                            _mark_mirrored(self.account_id, lt, "reconcile_close")
                            logger.warning(
                                f"[跟号对账兜底] ticket={pos['ticket']} 主号票号{lt}已平→强制平掉裸奔仓"
                            )
                        else:
                            err = str(r.get("error", ""))
                            if "不存在" in err:
                                # 主号已平、本号持仓也已不存在 → 标记已平，避免死循环
                                _mark_mirrored(self.account_id, lt, "reconcile_close")
                            else:
                                # 真正失败（连接/context busy）：归还占坑，下轮重试
                                _release_mirror(self.account_id, lt, "reconcile_close")
                                logger.error(f"[跟号对账兜底·平仓失败] ticket={pos['ticket']} 错误={err}（下轮重试）")
                    # 占坑失败（并发）= 让另一条线程处理，本线程跳过

                actions = consume_leader_exit(leader_id, lt)
                if not actions:
                    continue
                logger.info(f"[跟号镜像] {self.account_id[:8]} ticket={pos['ticket']} 收到主号{len(actions)}个动作: {[a.get('action') for a in actions]}")
                for act in actions:
                    # ★ 2026-08-06 修复死循环：幂等用(主号票号+动作类型)而非递增 action_id
                    a = (act.get("action") or "").lower()
                    if _is_mirrored(self.account_id, lt, a):
                        continue
                    if a == "full_close":
                        # ★★ Phase 2：占坑点钉在不可逆动作紧邻处（与 copy_order 同构）。
                        #   上面那次 _is_mirrored 只是省算力的廉价预检。
                        if not _claim_mirror(self.account_id, lt, a):
                            continue
                        r = mt5_service.close_position(self.account_id, pos["ticket"])
                        if "error" not in r:
                            self._record_close(ai_decision, pos, r, "跟号镜像主号全平", partial=False)
                            _mark_mirrored(self.account_id, lt, a)
                        else:
                            err = str(r.get("error", ""))
                            if "不存在" in err:
                                logger.warning(
                                    f"[跟号镜像] ticket={pos['ticket']} 主号要求全平但本号持仓已不存在→标记已平(防死循环)"
                                )
                                _mark_mirrored(self.account_id, lt, a)
                            else:
                                # 真正失败（连接/context busy）：连续≥3次强制标记已平
                                _fk = f"{self.account_id}:{lt}:{a}"
                                _fc = _bump_mirror_fail(_fk)
                                if _fc >= 3:
                                    logger.error(
                                        f"[跟号镜像·全平失败超限] ticket={pos['ticket']} 连续{_fc}次失败: {err} "
                                        f"→ 强制标记已平并告警"
                                    )
                                    _mark_mirrored(self.account_id, lt, a)
                                else:
                                    # 归还占坑：这一轮没平成、下一轮还要重试。
                                    # 不归还 = 主号已平而跟号永远平不掉 ⇒ 裸奔反向敞口。
                                    _release_mirror(self.account_id, lt, a)
                                    logger.error(
                                        f"[跟号镜像·全平失败] ticket={pos['ticket']} 错误={err}（下轮重试 {_fc}/3）"
                                    )
                    elif a == "partial_close":
                        frac = float(act.get("close_pct") or 0)
                        if 0.05 <= frac <= 0.95:
                            vol_total = float(pos.get("volume", 0) or 0)
                            vol_close = round(vol_total * frac, 2)
                            vol_close = max(vol_close, 0.01)
                            if vol_close >= vol_total:
                                vol_close = 0
                            # ★★ Phase 2：分批平是**双重执行代价最高**的动作——
                            #   主号只要求平 50%，两条线程各平 50% 就把整仓平光了，
                            #   本该留着奔跑的利润腿直接没了。占坑必须先于下单。
                            if not _claim_mirror(self.account_id, lt, a):
                                continue
                            r = mt5_service.close_position(self.account_id, pos["ticket"], vol_close)
                            if "error" not in r:
                                self._record_close(ai_decision, pos, r, "跟号镜像主号分批平", partial=True)
                                _mark_mirrored(self.account_id, lt, a)
                            else:
                                _release_mirror(self.account_id, lt, a)
                                logger.error(
                                    f"[跟号镜像·分批平失败] ticket={pos['ticket']} 错误={r.get('error')}（下轮重试）"
                                )
                    elif a == "move_sl":
                        new_sl = act.get("new_sl")
                        leader_open = float(act.get("leader_open_price") or 0)
                        if new_sl is not None and leader_open > 0:
                            if not _claim_mirror(self.account_id, lt, a):
                                continue
                            _my_open = float(pos.get("price_open") or pos.get("open_price") or 0)
                            _my_sl = float(pos.get("sl") or 0)
                            if _my_open > 0 and _my_sl > 0:
                                _sl_offset = float(new_sl) - leader_open
                                _target_sl = round(_my_open + _sl_offset, 2)
                                _is_buy = str(pos.get("type") or "").lower() in ("buy", "0") or pos.get("action") == "buy"
                                _is_sell = str(pos.get("type") or "").lower() in ("sell", "1") or pos.get("action") == "sell"
                                _move = False
                                if _is_buy and _target_sl > _my_sl:
                                    _move = True
                                elif _is_sell and _target_sl < _my_sl:
                                    _move = True
                                if _move and abs(_target_sl - _my_sl) > 0.01:
                                    # ★ 2026-08-17 P0 修复：同上方——单字段 modify 会被
                                    #   STARTRADER 服务器清缺省字段（实测 TP→0）。
                                    _cur_tp_keep = float(pos.get("tp") or 0)
                                    mod = mt5_service.modify_sl_tp(self.account_id, pos["ticket"],
                                                                  sl=float(_target_sl), tp=_cur_tp_keep)
                                    if "error" not in mod:
                                        logger.info(f"[跟号镜像] ticket={pos['ticket']} 相对偏移 {_sl_offset:+.2f} SL→{_target_sl}")
                                        _mark_mirrored(self.account_id, lt, a)
                                    else:
                                        _release_mirror(self.account_id, lt, a)
                                        logger.warning(f"[跟号镜像] 改SL失败: {mod.get('error')}")
                                else:
                                    # 无需移动或方向错误，仍打标记避免重复尝试
                                    _mark_mirrored(self.account_id, lt, a)
                            else:
                                _release_mirror(self.account_id, lt, a)
                                logger.warning(f"[跟号镜像] 无法计算相对SL: 本单开仓价或SL缺失")
            except Exception as e:
                logger.warning(f"[跟号镜像] 处理 ticket={pos.get('ticket')} 异常: {e}")

        # ★ 账号级全清消费（2026-08-05 修复）：L3篮子护盾触发主号全平时，
        #   除逐笔广播外还发了 __BASKET_CLOSE_ALL__ 特殊信号。
        #   跟号此处消费：无条件清掉自身所有剩余 XAUUSD 持仓（孤儿单/遗漏单）。
        basket_acts = consume_leader_exit(leader_id, "__BASKET_CLOSE_ALL__")
        if basket_acts:
            # ★★ Phase 2：幂等检查改为**原子占坑**。
            #   原写法 `not _is_mirrored(...)` 是两段式：主循环与 10s 守护线程
            #   可同时通过，双双进入全清流程重复下平仓指令。
            #   bid 为 None 时构不成幂等键 → 保持原行为放行（宁可重做不可漏做）。
            bid = basket_acts[0].get("id")
            if bid is None or _claim_mirror(self.account_id, bid, "__BASKET_CLOSE_ALL__"):
                # ★ 2026-08-07 Phase 1：查询失败必须与"确实没有遗留仓"分开。
                #   前者绝不能打幂等标记 —— 信号靠 _BUS_TTL(180s) 存活，主循环
                #   27~111s 一轮，只剩 1~2 次重试机会；一旦误标已处理，
                #   跟号就永久失去这次 L3 锁利保护，持仓裸奔到止损。
                _r_ok, remaining = _positions_checked(self.account_id, "XAUUSD")
                if not _r_ok:
                    # 归还占坑，等价于 Phase 1 的"不标记已处理"语义。
                    if bid is not None:
                        _release_mirror(self.account_id, bid, "__BASKET_CLOSE_ALL__")
                    logger.warning(
                        f"[跟号全清] {self.account_id[:8]} 收到L3全清信号但持仓查询失败 → "
                        f"不标记已处理，等待下轮重试（信号 TTL 内仍有效）"
                    )
                elif remaining:
                    logger.warning(
                        f"[跟号全清] {self.account_id[:8]} 收到主号L3篮子全清信号 → "
                        f"清掉{len(remaining)}笔遗留XAUUSD持仓"
                    )
                    for rp in remaining:
                        try:
                            rc = mt5_service.close_position(self.account_id, rp["ticket"])
                            if "error" not in rc:
                                self._record_close(ai_decision, rp, rc,
                                    "跟号L3篮子全清(孤儿单)", partial=False)
                                logger.info(f"[跟号全清] ticket={rp['ticket']} 已平 P/L={float(rp.get('profit',0)):+.2f}")
                            else:
                                logger.error(f"[跟号全清] ticket={rp['ticket']} 平仓失败: {rc.get('error')}")
                        except Exception as _re:
                            logger.warning(f"[跟号全清] ticket={rp.get('ticket')} 异常: {_re}")
                    if bid is not None:
                        _mark_mirrored(self.account_id, bid, "__BASKET_CLOSE_ALL__")
                else:
                    # 同样归还：原实现此处不打标记，保留了"下轮再查一次"的能力。
                    # 万一有跟单晚成交的散单在本轮之后才出现，下一轮还能把它清掉
                    # —— L3 篮子全清是锁利保护，漏掉散单等于保护失效。
                    # 收编不得夹带行为变更，故必须显式归还。
                    if bid is not None:
                        _release_mirror(self.account_id, bid, "__BASKET_CLOSE_ALL__")
                    logger.info(f"[跟号全清] {self.account_id[:8]} 收到L3全清信号但无遗留持仓，跳过")

    def _leader_account(self):
        """取本用户行情主号(信号主号)记录，用于出场同步映射。"""
        try:
            return self.db.query(MT5Account).filter(
                MT5Account.user_id == self.user_id,
                MT5Account.is_market_primary == True).first()
        except Exception:
            return None

    def _m2_reflexion(self, pos, close_result):
        """★ M2 Reflexion 捕捉率学习（仅主号调用）
        捕捉率 = 实盈 / MFE(历史最大有利偏移)；<0.3 视为"该赚没赚/让利润回吐"→有界反思生成教训。
        限频：每 20 笔主号平仓最多 1 条教训（ATLAS 2025-10 实证：过度反思引入噪声致模型瘫痪）。
        教训入 Semantic 记忆，并由 _build_exit_context 注入后续 AI 出场决策。
        """
        global _M2_TRADE_SINCE_LESSON
        from app.services import ai_exit as _ai_exit
        from app.services.memory_bank import get_memory_bank
        ticket = str(pos.get("ticket"))
        mfe = float(_ai_exit._EXIT_MFE.get((self.account_id, ticket), 0.0) or 0.0)
        profit = float(close_result.get("profit", 0) or 0)
        if mfe > 0.01:
            capture_rate = profit / mfe
        else:
            capture_rate = 1.0 if profit >= 0 else 0.0
        _M2_TRADE_SINCE_LESSON += 1
        # ★ 进化可见化（2026-08-05）：每次评估都记录，让用户能看见 AI 在持续学习/自检，
        # 而非"空转"。捕捉率<0.3 视为该赚没赚/回吐 → 才进入反思生成。
        _dir = (pos.get("type") or "pos").lower()
        logger.info(
            f"[M2反思·评估] ticket={ticket} 方向={_dir} 实盈=${profit:.2f} MFE=${mfe:.2f} "
            f"捕捉率={capture_rate:.2f}（{'良好' if capture_rate >= 0.3 else '偏低·待反思'}）"
            f" 距下次可生成教训还需 {max(0, 10 - _M2_TRADE_SINCE_LESSON)} 笔"
        )
        if capture_rate >= 0.3:
            return  # 捕捉良好，无需反思
        if _M2_TRADE_SINCE_LESSON < 10:
            return  # 限频：每 10 笔最多 1 条（提升响应，仍避免噪声致模型瘫痪）
        _M2_TRADE_SINCE_LESSON = 0
        direction = (pos.get("type") or "pos").lower()
        if profit < 0:
            lesson = (f"持仓{ticket}({direction})亏${profit:.2f}平仓,MFE曾达${mfe:.2f}未止盈回吐→"
                      f"教训:浮盈达MFE的50%即启动分批止盈,不贪等反转。")
        else:
            cr = max(0.0, min(1.0, capture_rate))
            lesson = (f"持仓{ticket}({direction})实盈${profit:.2f}仅占MFE${mfe:.2f}的{cr:.0%},过早全平→"
                      f"教训:用追踪止损替代一次性全平,捕捉更多MFE利润。")
        bank = get_memory_bank()
        if bank.add_lesson(lesson, source="reflexion"):
            logger.info(f"[M2反思] 捕捉率{capture_rate:.2f}<0.3 生成教训: {lesson}")
            # 进化可见化：生成教训时推送到前端活动流，让用户直接看到 AI 在自我纠正
            try:
                self._push_feed("evolution",
                    f"AI进化(M2反思): {lesson}",
                    direction=direction, confidence=0.0)
            except Exception:
                pass
            bank.add_episodic(
                f"close {direction} mfe={mfe:.2f} profit={profit:.2f}",
                {"capture_rate": round(capture_rate, 3), "lesson": lesson},
            )

    @staticmethod
    def _normalize_exit_reason(reason: str) -> str:
        """把平仓原因归并为可分组枚举（供 trades.exit_reason 归因统计）。
        原始 reason 可能为中文长串（L3篮子浮盈锁利/第⑤道防线/第⑥道防线…）或 smart_exit 的
        tp/sl/breakeven/ai/reverse 等。注意：第⑥防线(反向即跑机械止损)必须归并为独立
        枚举 "adverse_exit"，不得被下方 "止损" 分支吞成 "sl"（否则误杀率不可审计）。"""
        if not reason:
            return "unknown"
        r = str(reason)
        low = r.lower()
        if "l3" in low and ("锁利" in r or "tp" in low or "basket" in low):
            return "l3_tp_lock"
        if "第⑤道防线" in r or "熔断" in r or "risk" in low and "cut" in low:
            return "risk_cut"
        # ★ 2026-08-13 修复「第⑥防线记录失真」根因：原归一化在下方 "sl"/"止损" 分支
        #   把 "第⑥道防线·反向即跑机械止损" 含「止损」二字误判成 "sl"，导致该机械止损
        #   的全部砍单与真实 SL 命中混在一起，无法审计其误杀率。
        #   现于 "sl" 分支之前单独识别第⑥防线签名（含「第⑥道防线」或「反向即跑」或
        #   adverse_exit 英文），归并为独立枚举 "adverse_exit"，与真实 sl/tp 完全区分。
        if "第⑥道防线" in r or "反向即跑" in r or ("adverse" in low and "exit" in low):
            return "adverse_exit"
        if "跟号" in r or "镜像" in r:
            return "follower_mirror"
        if "保本" in r or "breakeven" in low:
            return "breakeven"
        if "追踪" in r or "trailing" in low:
            return "trailing"
        if "反转" in r or "reverse" in low:
            return "reverse"
        if "tp" in low or "止盈" in r:
            return "tp"
        if "sl" in low or "止损" in r:
            return "sl"
        if "ai" in low:
            return "ai"
        # 兜底保留原文前 40 字符
        return r[:40]

    def _record_close(self, ai_decision, pos, close_result, reason, partial: bool):
        """记录平仓到数据库 + 写 AI 活动流 + 反馈 MetaAgent + 真进化引擎接入"""
        # ★ 2026-08-16 下单幂等键配套：Worker 侧已去重（duplicate 响应）时：
        #   · 旧行为：首次执行已记录过平仓 → 跳过，避免再记一笔 0 盈亏假明细。
        #   · ★ 2026-08-16 审计P0-3修复：若 duplicate 回执携带【首次成交快照】
        #     （volume/close_price/profit/net_profit 非 0）——这是"首次回执丢失后重发
        #     回放真实数据"的恢复场景，必须照常记账，否则 partial 已实现盈亏永久丢失；
        #     仅当 duplicate 无快照（老 exec_req 记录，全 0）才跳过。
        if isinstance(close_result, dict) and close_result.get("duplicate"):
            _dup_vol = float(close_result.get("volume") or 0)
            _dup_pnl = float(close_result.get("profit") or 0)
            _dup_npnl = float(close_result.get("net_profit") or 0)
            if _dup_vol <= 0 and abs(_dup_pnl) < 1e-9 and abs(_dup_npnl) < 1e-9:
                logger.info(
                    f"[平仓] ticket={pos.get('ticket')} 幂等去重（该 req_id 已执行过且无快照）"
                    f"→ 跳过重复记录"
                )
                return
            logger.info(
                f"[平仓] ticket={pos.get('ticket')} 幂等去重但带回放快照 "
                f"(vol={_dup_vol} pnl={_dup_pnl} net={_dup_npnl}) → 恢复记账"
            )
        # ★ 2026-08-06 修复 P0「真进化空转」：每笔平仓（含部分平仓的末笔）回调接入
        #   本地进化引擎 EvolutionEngine.record(direction, pnl, tags)，让"情境→期望盈亏"
        #   映射真正从实盘累积变聪明（真在线学习，非经验回注反模式）。
        #   仅主号写入全局进化引擎（跟号平仓是主号动作副本，写入会双重计数/偏移数据集）。
        try:
            if getattr(self, "_is_leader", False) and not partial:
                _dir = (pos.get("type") or "").upper()
                if _dir in ("BUY", "SELL"):
                    _pnl = float(close_result.get("profit", 0) or 0)
                    # 情境标签从行情快照派生（regime/smc），与 local_rl._extract_tags 对齐
                    _tags = []
                    try:
                        _snap = self.debate_engine.market.get_market_snapshot() or {}
                        _reg = (_snap.get("regime") or _snap.get("market_regime") or {})
                        if isinstance(_reg, str):
                            _reg = {"regime": _reg}
                        _r = _reg.get("regime")
                        # ★ 顺势毒教训隔离(2026-08-06)：AI 主动砍「顺势浮亏单」(方向对、体制未翻转、
                        #   被短期噪音吓退)的亏损，反映的是 AI 出场噪音而非方向错误。若照常喂
                        #   regime:trend_up→负，真进化引擎会学到「上涨做多亏」的毒教训，恶性循环
                        #   越砍越凶。故此类平仓只打 ai_premature_cut 标签，不污染 regime/smc 方向学习。
                        _is_ai_cut = "AI出场" in (reason or "")
                        _pos_dir = (pos.get("type") or "").lower()
                        _with_trend = (
                            (_pos_dir == "buy" and _r in ("trend_up", "strong_uptrend"))
                            or (_pos_dir == "sell" and _r in ("trend_down", "strong_downtrend"))
                        )
                        if _is_ai_cut and _with_trend:
                            _tags = ["ai_premature_cut"]
                        else:
                            if _r:
                                _tags.append(f"regime:{_r}")
                            if _reg.get("at_stale_top"):
                                _tags.append("stale_top")
                            if _reg.get("at_stale_bottom"):
                                _tags.append("stale_bottom")
                            _ext = float(_reg.get("extension_z", 0) or 0)
                            if _ext > 2:
                                _tags.append("ext_high")
                            elif _ext < -2:
                                _tags.append("ext_low")
                            _smc = _snap.get("smc_features") or {}
                            _bias = _smc.get("global_bias")
                            if _bias and _bias != "neutral":
                                _tags.append(f"smc:{_bias}")
                    except Exception as _te:
                        logger.debug(f"[真进化] 标签派生跳过: {_te}")
                    if not _tags:
                        _tags = ["regime:unknown"]
                    from app.services.local_rl import get_engine as _get_evo
                    _get_evo().record(_dir, _pnl, _tags)
                    logger.info(
                        f"[真进化] 接入平仓 {pos.get('ticket')} {_dir} pnl={_pnl:+.2f} "
                        f"tags={_tags} → 更新情境期望盈亏映射"
                    )
        except Exception as _ee:
            logger.warning(f"[真进化] 接入失败(忽略): {_ee}")

        # 记录平仓时间（用于抑制秒级同方向重开，即 churn）
        try:
            _record_close_for_churn(self.account_id, str(pos.get("type") or "").upper())
        except Exception:
            pass

        try:
            # ★ 2026-08-15 审计P1修复：按 ticket 查账必须滤 mt5_account_id——不同券商
            #   ticket 各自从 1 递增可同号，不滤账号会把盈亏记到别人账本上（多租户串扰）。
            trade = self.db.query(Trade).filter(
                Trade.mt5_account_id == self.account_id,
                Trade.mt5_ticket == str(pos["ticket"])
            ).first()
            if trade:
                # ★★ 2026-08-10 平仓明细根治：partial_close 多次平仓不再覆盖主行。
                #   旧逻辑每次 UPDATE 同一条 trade（volume/profit/close_time 被最后一次覆盖），
                #   历史平仓明细全部丢失（如 lium3 #377415351 18:05 平0.5手+716.50 被 18:22
                #   平0.01手+10.34 覆盖）→ DB 聚合与 MT5 真实 deals 严重不符。
                #   新逻辑：
                #     ① 每次平仓（partial/full）INSERT 一条 trade_exits 明细（审计完整链）
                #     ② trades 主行改"生命周期累计"语义：volume=开仓量(不再覆盖)、
                #        profit/net_profit=累计已实现、result=短标记(win/loss/breakeven/partial)
                _pnl = float(close_result.get("profit") or 0)
                _npnl = float(close_result.get("net_profit") or _pnl or 0)
                _exit_vol = float(close_result.get("volume") or 0)
                if _exit_vol <= 0:
                    _exit_vol = float(pos.get("volume") or 0)
                if _exit_vol <= 0:
                    _exit_vol = float(trade.volume or 0)
                _short_res = "partial" if partial else (
                    "breakeven" if abs(_pnl) < 0.01 else ("win" if _pnl > 0 else "loss")
                )
                try:
                    from app.models.trade_exit import TradeExit
                    # ★ 2026-08-15 审计修复：明细行 MFE/MAE 此前误取 ai_decision.mfe/mae
                    #   （DebateDecision 无此属性）→ 恒 0，归因审计失效。改取 ai_exit 真实值
                    #   （与主行同源，L4706 处 pop），此处仅 .get 不 pop，避免影响主行回写。
                    _mk = (self.account_id, str(pos.get("ticket")))
                    try:
                        from app.services import ai_exit as _aexit
                        _tx_mfe = float(_aexit._EXIT_MFE.get(_mk, 0.0) or 0.0)
                        _tx_mae = float(getattr(_aexit, "_EXIT_MAE", {}).get(_mk, 0.0) or 0.0)
                    except Exception:
                        _tx_mfe = 0.0
                        _tx_mae = 0.0
                    _tx = TradeExit(
                        trade_id=trade.id,
                        user_id=trade.user_id,
                        mt5_account_id=trade.mt5_account_id,
                        mt5_ticket=trade.mt5_ticket,
                        action=trade.action,
                        exit_volume=round(_exit_vol, 2),
                        exit_price=float(close_result.get("close_price") or 0),
                        profit=round(_pnl, 2),
                        net_profit=round(_npnl, 2),
                        result=_short_res,
                        exit_reason=self._normalize_exit_reason(reason),
                        partial=partial,
                        mfe=_tx_mfe,
                        mae=_tx_mae,
                        exit_time=datetime.now(),
                    )
                    self.db.add(_tx)
                except Exception as _tx_e:
                    logger.warning(f"[执行器] 平仓明细(trade_exits)写入跳过: {_tx_e}")

                # ★ 2026-08-11 防御（P0 真实账号假巨亏）：开仓价若被记0，
                #   用持仓真实开仓价(pos.price_open)回填，避免历史污染 + 仪表盘失真。
                if not trade.open_price or trade.open_price <= 0:
                    _pop = float(pos.get("price_open") or 0)
                    if _pop > 0:
                        trade.open_price = _pop
                # ★★ 2026-08-10 二次修正 + 2026-08-15 补丁：partial 平仓不得写 close_time / close_price。
                #   旧逻辑每次平仓都写 → 0.5手切到0.13手时主行已"判死"(close_time IS NOT NULL)，
                #   且主行 close_price 被写成部分平仓价，破坏"开仓行 close_time 为 NULL=未平"约定。
                #   只有真正全平（MT5 剩余≈0）才写 close_time 与 close_price。
                _rem_after = float(pos.get("volume") or 0) - _exit_vol
                if _rem_after > 0.005:
                    pass  # 真 partial：保持 open 状态，剩余由后续循环继续管理（不动主行 close_*）
                else:
                    trade.close_price = close_result.get("close_price", 0)
                    trade.close_time = datetime.now(timezone.utc)
                # ★ 2026-08-10 修复：volume 用 MT5 实际成交手数（原值=开仓手数，
                #   partial 平仓后失真——如开 1.00 平 0.5，DB 仍记 1.00 导致归因错误）。
                #   worker 侧已返回本次实平手数；所有仓位统一生效。
                #   ★ 2026-08-10 二次修正：主行 volume 语义=「开仓量」不再被覆盖
                #   （开仓多少就是多少，供 recent_trades 稳定显示；每次平仓量查 trade_exits）。
                #   仅当主行 volume 仍为 0 或异常时用实平手数兜底。
                if not trade.volume or float(trade.volume) <= 0:
                    trade.volume = round(_exit_vol, 2)
                # 累计已实现盈亏（partial 多次平仓求和，full close 覆盖=本次即总额，语义一致）
                trade.profit = round((trade.profit or 0) + _pnl, 2)
                # ★ 2026-08-10 字段一致性修复：net_profit 此前从不写入(恒为 0)，
                #   导致「连续亏损冷却」按 net_profit 判断时永远不触发。
                #   平仓时与 profit 一起落库（优先取 worker 返回的 net_profit）。
                trade.net_profit = round((trade.net_profit or 0) + _npnl, 2)
                # ★ 2026-08-10 result 语义修复：此前塞 `closed_by_ai|{长文本原因}`，
                #   result='win'/'loss' 统计永远 0。改为短标记（win/loss/breakeven/partial），
                #   长文本原因只进 exit_reason 与 trade_exits.exit_reason。
                trade.result = _short_res
                # ★ 2026-08-06 审计修复：持久化平仓原因 + MFE/MAE。
                #   此前 exit_reason 恒为 NULL（仅外部平仓记 mt5_closed_external），
                #   mfe/mae 恒为 0（开仓写默认后从未更新）→ 无法归因、MFE回灌空转、仪表盘MFE为假。
                trade.exit_reason = self._normalize_exit_reason(reason)
                _mk = (self.account_id, str(pos.get("ticket")))
                try:
                    # ★ 2026-08-10 修：_ai_exit 在本作用域未定义（旧 except 吞 NameError → MFE/MAE 永不入库）
                    from app.services import ai_exit as _ai_exit
                    _mfe = float(_ai_exit._EXIT_MFE.get(_mk, 0.0) or 0.0)
                    _mae = float(getattr(_ai_exit, "_EXIT_MAE", {}).get(_mk, 0.0) or 0.0)
                    if _mfe:
                        trade.mfe = round(_mfe, 2)
                    if _mae:
                        trade.mae = round(_mae, 2)
                    _ai_exit._EXIT_MFE.pop(_mk, None)
                    _ai_exit._EXIT_MAE.pop(_mk, None)
                except Exception as _mfe_e:
                    logger.debug(f"[执行器] MFE/MAE 回写跳过: {_mfe_e}")
                # ★ 毫秒级可靠性：平仓更新用安全commit，失败自动rollback恢复session
                self._safe_db_commit(label="平仓trade更新")
                # ★★ 2026-08-18 P0 修复：非 partial 全平后若账户已无持仓，
                #   必须清零 _BASKET_PEAK_PNL。SL/TP/smart_exit/外部平仓等路径此前均不清峰值，
                #   残留盈利峰值会把下一批新仓误判为"回吐"而秒平（ticket=387482813 实测）。
                if _rem_after <= 0.005:
                    try:
                        _pk_ok, _pk_pos = _positions_checked(self.account_id, "XAUUSD")
                        if _pk_ok and not _pk_pos and self.account_id in _BASKET_PEAK_PNL:
                            _BASKET_PEAK_PNL[self.account_id] = 0.0
                            logger.debug(
                                f"[平仓记录] {self.account_id[:8]} 全平后持仓面空 → 篮子峰值已清零"
                            )
                    except Exception:
                        pass

            # AI 活动流
            kind = "close_partial" if partial else "close"
            pnl = close_result.get("profit", 0)
            self._push_feed(kind,
                f"{'部分' if partial else ''}平仓 {pos['ticket']} {reason} 盈亏{pnl:+.2f}",
                direction=getattr(ai_decision, 'decision', ''),
                confidence=float(getattr(ai_decision, 'confidence', 0) or 0),
                pnl=pnl, ticket=str(pos.get('ticket') or ''),
                open_price=float(pos.get('price_open') or pos.get('open_price') or 0),
                close_price=float(close_result.get('close_price') or 0),
                reason=reason)

            # MetaAgent 反馈（仅主号）：AI 进化只看主号真实决策结果。
            # 跟号镜像平仓只是主号动作的副本，若也喂进化，会随副号增删而双重计数/偏移数据集，
            # 违背用户硬要求"增删副号不影响 AI 进化"。故门控 _is_leader，跟号平仓仍记录+展示但不污染进化。
            if getattr(self, "_is_leader", False):
                try:
                    from app.core.debate_engine import DebateDecision
                    if trade:
                        feedback_decision = DebateDecision(
                            decision=trade.meta_agent_decision or "HOLD",
                            confidence=trade.meta_agent_confidence or 0.0,
                            deepseek_weight=trade.deepseek_confidence or 0.5,
                            hunyuan_weight=trade.hunyuan_confidence or 0.5,
                            deepseek_vote=trade.deepseek_decision or "HOLD",
                            hunyuan_vote=trade.hunyuan_decision or "HOLD",
                            reasoning_summary=trade.debate_summary or "",
                            risk_level="medium",
                        )
                        self.debate_engine.meta_agent.feedback(
                            decision=feedback_decision,
                            was_profitable=(close_result.get("profit", 0) > 0),
                            profit=close_result.get("profit", 0),
                            mt5_account_id=self.account_id,
                            event_time=trade.close_time if trade else None,
                            ticket=pos.get("ticket"),
                        )
                except Exception as e:
                    logger.warning(f"[执行器] MetaAgent 反馈失败: {e}")
                # ── M2 Reflexion：捕捉率学习 ──
                # 捕捉率 = 实盈 / MFE(历史最大有利偏移)；<0.3 视为"该赚没赚/让利润回吐"→有界反思生成教训
                # 仅主号（跟号平仓是主号副本，喂进化会双重计数偏移数据集，违背"增删副号不影响进化"）
                try:
                    self._m2_reflexion(pos, close_result)
                except Exception as _e:
                    logger.warning(f"[执行器] M2 反思失败(忽略): {_e}")
                # ── M4 OPRO 演化：累积主号平仓 PnL，达窗口自动演化出场激进度 ──
                try:
                    from app.services.opro_evolver import get_evolver
                    get_evolver().record_trade(float(close_result.get("profit", 0) or 0))
                except Exception as _e:
                    logger.warning(f"[执行器] M4 演化失败(忽略): {_e}")
        except Exception as e:
            logger.warning(f"[执行器] 平仓记录失败: {e}")
