"""
授权状态机 —— 「这套系统现在能不能开新仓」的唯一判定处（V6 Phase 8.2）

═══ 三条不可动摇的原则 ═══

【一】失效只停开新仓，**绝不强平任何持仓**
    与 V6 9.4.2「只关水龙头，不抽走桶里的水」同源。授权是商业契约，不是风控。
    客户欠费是商务问题；用强平仓位去催款，会把一次续费纠纷升级成赔付诉讼，
    而且触碰「客户资金独立」的合规红线。已有仓位继续走各账号自己的
    SL/TP/SmartExit —— 该止盈止盈，该止损止损，和授权状态完全无关。

【二】授权模块自身故障必须 **fail-open**（放行）
    这条反直觉，但商业账算得很清楚：
        授权模块出 bug 而 fail-close  → 全体付费客户同时停摆，
                                        赔付 + 口碑 + 客服雪崩，损失是确定的、巨大的；
        授权模块出 bug 而 fail-open   → 少数盗版多跑几天，损失是有限的、可追回的。
    所以本模块所有异常路径一律返回「允许」，只在日志里喊。
    真正的护城河从来不是这段 if，而是服务端的云模型 API Key 发放（见 token.py）。

【三】判定只信 **验签后的 claims**，不信数据库字段
    licenses 表里的 edition/max_accounts/valid_until 全是缓存副本，方便 SQL 查询而已。
    客户拿 SQLite 工具把 max_accounts 改成 999 毫无意义 —— 每次判定都重新验签，
    用令牌里的原值。这也是为什么本模块看起来「多此一举」地反复 verify_token。

═══ 状态机 ═══
    TRIAL            未激活，处于内置试用期内            → 放行 + 前端提示剩余天数
    ACTIVE           令牌有效、机器匹配                  → 放行
    GRACE            已过期但在 72h 缓冲内               → 放行 + 前端黄条催续费
    EXPIRED          过期且超出缓冲                      → 停开新仓
    UNLICENSED       试用期已过且未激活                  → 停开新仓
    MACHINE_MISMATCH 令牌绑的不是这台机器                → 停开新仓
    SUSPENDED        平台侧停用（心跳下发）              → 停开新仓
    CLOCK_TAMPERED   系统时间被往回调                    → 停开新仓
    QUOTA_EXCEEDED   （按账号判定）该账号超出配额        → 仅该账号停开新仓
    DISABLED         未开启强制校验（开发/内部机）       → 放行
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app.licensing import fingerprint as fp
from app.licensing.token import LicenseClaims, TokenError, normalize_license_key, verify_token

# ── 策略常量 ────────────────────────────────────────────────
# 到期后的缓冲。给 72h 而不是 0 的理由很实际：授权常在周末/节假日到期，
# 客户财务不上班、我们客服也不上班。到点即停 = 周一早上一堆投诉。
GRACE_HOURS = 72

# 内置试用期。未激活的新装机允许先跑起来，否则客户装完看到「未授权」直接卸载，
# 连体验的机会都没有。
TRIAL_DAYS = 7

# 时钟回拨容差。跨时区/NTP 校准会有小幅回退，24h 之内不算作弊。
CLOCK_TOLERANCE_HOURS = 24

# 判定结果缓存时长。开仓路径每轮都要问一次，不能每次都验签+查库。
_CACHE_TTL_SECONDS = 30

_ALLOWED_STATES = {"trial", "active", "grace", "disabled"}


class LicenseState(str):
    TRIAL = "trial"
    ACTIVE = "active"
    GRACE = "grace"
    EXPIRED = "expired"
    UNLICENSED = "unlicensed"
    MACHINE_MISMATCH = "machine_mismatch"
    SUSPENDED = "suspended"
    CLOCK_TAMPERED = "clock_tampered"
    QUOTA_EXCEEDED = "quota_exceeded"
    DISABLED = "disabled"


STATE_LABELS = {
    LicenseState.TRIAL: "试用中",
    LicenseState.ACTIVE: "已授权",
    LicenseState.GRACE: "已到期（宽限期）",
    LicenseState.EXPIRED: "授权已到期",
    LicenseState.UNLICENSED: "未授权",
    LicenseState.MACHINE_MISMATCH: "授权与本机不匹配",
    LicenseState.SUSPENDED: "授权已被停用",
    LicenseState.CLOCK_TAMPERED: "系统时间异常",
    LicenseState.QUOTA_EXCEEDED: "超出账号配额",
    LicenseState.DISABLED: "未启用授权校验",
}


@dataclass
class LicenseSnapshot:
    state: str = LicenseState.DISABLED
    allow_open: bool = True
    message: str = ""
    license_key: str = ""
    edition: str = ""
    edition_label: str = ""
    customer: str = ""
    max_accounts: int = 0
    used_accounts: int = 0
    days_remaining: Optional[int] = None
    valid_until: Optional[str] = None
    grace_until: Optional[str] = None
    machine_fingerprint: str = ""
    last_heartbeat_at: Optional[str] = None
    evaluated_at: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "state": self.state,
            "state_label": self.state_label,
            "allow_open": self.allow_open,
            "message": self.message,
            "license_key": _mask_key(self.license_key),
            "edition": self.edition,
            "edition_label": self.edition_label,
            "customer": self.customer,
            "max_accounts": self.max_accounts,
            "used_accounts": self.used_accounts,
            "days_remaining": self.days_remaining,
            "valid_until": self.valid_until,
            "grace_until": self.grace_until,
            "machine_fingerprint": self.machine_fingerprint,
            "last_heartbeat_at": self.last_heartbeat_at,
            "evaluated_at": self.evaluated_at,
        }
        d.update(self.detail)
        return d


def _mask_key(key: str) -> str:
    """展示用脱敏：WXAI-8CM8E-****-****-DWWP5。授权码是凭证，截图/日志里不该出全。"""
    if not key:
        return ""
    parts = key.split("-")
    if len(parts) < 4:
        return key
    return "-".join([parts[0], parts[1], "****", "****", parts[-1]])


_cache: Optional[LicenseSnapshot] = None
_cache_at: Optional[datetime] = None
_lock = threading.RLock()


def invalidate_cache() -> None:
    """激活/解绑/心跳后调用，让下一次判定重新走全套。"""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = None


# ══════════════════════════════════════════════════════════════
#  安装时间（试用期起点）
# ══════════════════════════════════════════════════════════════
def _install_time() -> datetime:
    """
    取安装时间：`.install_stamp` 文件 与 users 表最早记录，**取更早的那个**。

    为什么要两个来源：删掉 stamp 文件就能重置试用期，那试用期形同虚设；
    users 表最早记录只有重装（丢掉全部历史交易）才能清掉，门槛高得多。
    两者取早，意味着客户必须两个都清才能刷试用——那已经等于重装了。
    """
    candidates = []

    try:
        from app.config import settings

        stamp = Path(settings.DATA_DIR) / ".install_stamp"
        if stamp.exists():
            candidates.append(datetime.fromtimestamp(stamp.stat().st_mtime))
        else:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
            candidates.append(datetime.utcnow())
    except Exception:
        pass

    try:
        from app.database import SessionLocal
        from app.models.user import User

        db = SessionLocal()
        try:
            row = db.query(User.created_at).order_by(User.created_at.asc()).first()
            if row and row[0]:
                candidates.append(row[0])
        finally:
            db.close()
    except Exception:
        pass

    return min(candidates) if candidates else datetime.utcnow()


# ══════════════════════════════════════════════════════════════
#  读取本机安装的授权
# ══════════════════════════════════════════════════════════════
def _load_license_row():
    """客户端上 licenses 表最多一行。多于一行取最近激活的（历史遗留场景）。"""
    from app.database import SessionLocal
    from app.models.license import License

    db = SessionLocal()
    try:
        return (
            db.query(License)
            .order_by(License.activated_at.desc().nullslast(), License.created_at.desc())
            .first()
        )
    finally:
        db.close()


def _count_used_accounts(license_key: str) -> int:
    from app.database import SessionLocal
    from app.models.license import ActivationRecord

    db = SessionLocal()
    try:
        return (
            db.query(ActivationRecord.mt5_login)
            .filter(
                ActivationRecord.license_key == license_key,
                ActivationRecord.revoked.is_(False),
            )
            .distinct()
            .count()
        )
    finally:
        db.close()


def _row_factors(row) -> Dict[str, str]:
    """解析本地激活时记录的三要素。解析不了就当没绑定（宁可放行，也不误伤）。"""
    try:
        data = json.loads(row.bound_factors or "{}")
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _touch_last_seen(row_id: str, now: datetime) -> None:
    """把「见过的最大时间」往前推。时钟回拨检测的写入侧。"""
    from app.database import SessionLocal
    from app.models.license import License

    db = SessionLocal()
    try:
        row = db.query(License).filter(License.id == row_id).first()
        if row and (row.last_seen_at is None or now > row.last_seen_at):
            row.last_seen_at = now
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════
#  主判定
# ══════════════════════════════════════════════════════════════
def evaluate(force: bool = False) -> LicenseSnapshot:
    """
    计算当前授权快照。**永不抛异常**（见原则二 fail-open）。
    """
    global _cache, _cache_at
    now = datetime.utcnow()

    with _lock:
        if (
            not force
            and _cache is not None
            and _cache_at is not None
            and (now - _cache_at).total_seconds() < _CACHE_TTL_SECONDS
        ):
            return _cache

    try:
        snap = _evaluate_inner(now)
    except Exception as e:
        # fail-open：授权模块自己挂了，绝不能连累交易
        logger.error(f"[授权] 判定异常，按放行处理（fail-open）: {type(e).__name__}: {e}")
        snap = LicenseSnapshot(
            state=LicenseState.DISABLED,
            allow_open=True,
            message="授权模块暂时不可用，已按放行处理",
            evaluated_at=now.isoformat(timespec="seconds"),
            detail={"fail_open": True, "error": f"{type(e).__name__}"},
        )

    with _lock:
        _cache = snap
        _cache_at = now
    return snap


def _evaluate_inner(now: datetime) -> LicenseSnapshot:
    from app.config import settings

    base = {"evaluated_at": now.isoformat(timespec="seconds")}

    # ── 0) 未开启强制校验（开发机 / 内部演示机）────────────────
    if not getattr(settings, "LICENSE_ENFORCE", True):
        return LicenseSnapshot(
            state=LicenseState.DISABLED,
            allow_open=True,
            message="当前未启用授权校验（开发模式）",
            **base,
        )

    row = _load_license_row()

    # ── 1) 没装授权 → 内置试用期 ──────────────────────────────
    if row is None or not row.token:
        installed = _install_time()
        trial_end = installed + timedelta(days=TRIAL_DAYS)
        left = (trial_end - now).days
        if now < trial_end:
            return LicenseSnapshot(
                state=LicenseState.TRIAL,
                allow_open=True,
                message=f"试用中，剩余 {max(0, left)} 天。请在到期前完成授权激活。",
                edition="trial",
                edition_label="试用版",
                days_remaining=max(0, left),
                valid_until=trial_end.isoformat(timespec="seconds"),
                machine_fingerprint=fp.compute_fingerprint(),
                **base,
            )
        return LicenseSnapshot(
            state=LicenseState.UNLICENSED,
            allow_open=False,
            message="试用期已结束，未检测到有效授权。已停止开新仓；现有持仓不受影响，仍会正常止盈止损。",
            machine_fingerprint=fp.compute_fingerprint(),
            **base,
        )

    # ── 2) 验签（唯一可信来源）────────────────────────────────
    try:
        claims: LicenseClaims = verify_token(row.token)
    except TokenError as e:
        return LicenseSnapshot(
            state=LicenseState.UNLICENSED,
            allow_open=False,
            message=f"授权校验未通过（{e.message}）。已停止开新仓；现有持仓不受影响。",
            machine_fingerprint=fp.compute_fingerprint(),
            detail={"code": e.code},
            **base,
        )

    used = _count_used_accounts(claims.license_key)
    common = dict(
        license_key=claims.license_key,
        edition=claims.edition,
        edition_label=claims.edition_label,
        customer=claims.customer,
        max_accounts=claims.max_accounts,
        used_accounts=used,
        valid_until=claims.valid_until.isoformat(timespec="seconds") if claims.valid_until else None,
        days_remaining=claims.days_remaining(now),
        machine_fingerprint=row.machine_fingerprint or fp.compute_fingerprint(),
        last_heartbeat_at=row.last_heartbeat_at.isoformat(timespec="seconds") if row.last_heartbeat_at else None,
        **base,
    )

    # ── 3) 时钟回拨 ───────────────────────────────────────────
    if row.last_seen_at and now < row.last_seen_at - timedelta(hours=CLOCK_TOLERANCE_HOURS):
        return LicenseSnapshot(
            state=LicenseState.CLOCK_TAMPERED,
            allow_open=False,
            message=(
                f"检测到系统时间异常（当前 {now:%Y-%m-%d}，此前已运行至 {row.last_seen_at:%Y-%m-%d}）。"
                "请校准系统时间后重试。已停止开新仓；现有持仓不受影响。"
            ),
            **common,
        )
    _touch_last_seen(row.id, now)

    # ── 4) 平台侧停用（由心跳下发）────────────────────────────
    if row.status == "suspended":
        return LicenseSnapshot(
            state=LicenseState.SUSPENDED,
            allow_open=False,
            message="授权已被停用，请联系服务商。已停止开新仓；现有持仓不受影响。",
            **common,
        )

    # ── 5) 机器绑定（三取二）──────────────────────────────────
    # 绑定依据的优先级：
    #   ① 令牌里的 machine —— 平台在线签发的绑定令牌，最权威，改不了；
    #   ② 本地 bound_factors —— 离线激活时记下的「第一次装在哪台机」。
    # 为什么②必须存在：离线签发的令牌 machine 是空的，若只看①，
    # 客户把整个 data 目录（含 licenses 表）拷到十台机器上，每台都验签通过。
    # 有了②，整库复制会因为指纹对不上而被挡下——这是离线体系里性价比最高的一道锁。
    bound_factors = claims.machine if claims.is_bound else _row_factors(row)
    if bound_factors and any(bound_factors.values()):
        current = fp.collect_factors()
        ok, hits = fp.match_factors(current, bound_factors)
        if not ok:
            return LicenseSnapshot(
                state=LicenseState.MACHINE_MISMATCH,
                allow_open=False,
                message=(
                    f"本机与授权绑定的设备不符（硬件特征命中 {hits}/3，需至少 2）。"
                    "若您更换过主板或整机，请联系服务商重新激活。已停止开新仓；现有持仓不受影响。"
                ),
                detail={"factor_hits": hits},
                **common,
            )

    # ── 6) 尚未生效（预售/预约生效的授权）─────────────────────
    if claims.not_yet_valid(now):
        return LicenseSnapshot(
            state=LicenseState.EXPIRED,
            allow_open=False,
            message=f"授权尚未到生效日期（{claims.valid_from:%Y-%m-%d}）。已停止开新仓；现有持仓不受影响。",
            **common,
        )

    # ── 7) 到期与宽限期 ───────────────────────────────────────
    if claims.is_expired(now):
        grace_end = claims.valid_until + timedelta(hours=GRACE_HOURS)
        if now < grace_end:
            hours_left = int((grace_end - now).total_seconds() // 3600)
            return LicenseSnapshot(
                state=LicenseState.GRACE,
                allow_open=True,
                message=f"授权已到期，处于 {GRACE_HOURS} 小时宽限期内（剩余约 {hours_left} 小时），请尽快续期。",
                grace_until=grace_end.isoformat(timespec="seconds"),
                **common,
            )
        return LicenseSnapshot(
            state=LicenseState.EXPIRED,
            allow_open=False,
            message=(
                f"授权已于 {claims.valid_until:%Y-%m-%d} 到期且宽限期已过。"
                "已停止开新仓；现有持仓不受影响，仍会正常止盈止损。"
            ),
            grace_until=grace_end.isoformat(timespec="seconds"),
            **common,
        )

    # ── 8) 正常 ───────────────────────────────────────────────
    left = claims.days_remaining(now)
    msg = "授权正常" if left is None else f"授权正常，剩余 {left} 天"
    if left is not None and left <= 15:
        msg = f"授权将在 {left} 天后到期，请及时续期"
    return LicenseSnapshot(state=LicenseState.ACTIVE, allow_open=True, message=msg, **common)


# ══════════════════════════════════════════════════════════════
#  执行器闸门：某个 MT5 账号现在能不能开新仓
# ══════════════════════════════════════════════════════════════
def _resolve_login(account_id: str) -> str:
    """
    内部账号主键 → MT5 登录号。

    ⚠ 命名陷阱：MT5Account.id 是内部 uuid，MT5Account.account_id 才是 MT5 登录号。
    执行器持有的 self.account_id 是**前者**。这里做一次翻译，
    否则配额会按 uuid 计数——重装后 uuid 变了，客户会莫名其妙"超配额"。
    """
    if not account_id:
        return ""
    from app.database import SessionLocal
    from app.models.mt5_account import MT5Account

    db = SessionLocal()
    try:
        row = db.query(MT5Account.account_id).filter(MT5Account.id == account_id).first()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""
    finally:
        db.close()


def check_open_allowed(
    mt5_login: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """
    返回 (是否允许开新仓, 中文原因, 事件码)。允许时原因为空串。

    ★ 只管「开新仓」。平仓、改止损、追踪止盈一律不经过这里 ——
      授权状态绝不能影响已有仓位的风险管理（原则一）。

    参数二选一：直接给 MT5 登录号，或给内部 account_id 由本函数翻译。
    """
    try:
        snap = evaluate()

        if snap.state not in _ALLOWED_STATES:
            return False, snap.message, f"LICENSE_{snap.state.upper()}"

        login = str(mt5_login) if mt5_login else (_resolve_login(account_id or "") if account_id else "")

        # 配额是按账号判定的：超配额只挡超出的那个账号，
        # 已占坑的账号照常交易 —— 不能因为客户多加了一个号就把全部停掉。
        if login and snap.state in ("active", "grace") and snap.max_accounts > 0:
            ok, reason = _ensure_account_slot(login, snap, account_id=account_id)
            if not ok:
                return False, reason, "LICENSE_QUOTA_EXCEEDED"

        return True, "", ""
    except Exception as e:
        logger.error(f"[授权] 闸门异常，放行（fail-open）: {type(e).__name__}: {e}")
        return True, "", ""


def _ensure_account_slot(
    mt5_login: str,
    snap: LicenseSnapshot,
    account_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    懒占坑：账号第一次要开仓时才占用配额。

    为什么懒占而不是加账号时就占：客户常常先把号都填进来再挑着跑，
    加号即占坑会让「填了 5 个号只跑 2 个」的客户莫名其妙被判超配额。
    以实际交易为准，对客户更公平，对我们也更好解释。
    """
    from app.database import SessionLocal
    from app.models.license import ActivationRecord

    db = SessionLocal()
    try:
        exists = (
            db.query(ActivationRecord)
            .filter(
                ActivationRecord.license_key == snap.license_key,
                ActivationRecord.mt5_login == mt5_login,
                ActivationRecord.revoked.is_(False),
            )
            .first()
        )
        if exists:
            return True, ""

        used = (
            db.query(ActivationRecord.mt5_login)
            .filter(
                ActivationRecord.license_key == snap.license_key,
                ActivationRecord.revoked.is_(False),
            )
            .distinct()
            .count()
        )
        if used >= snap.max_accounts:
            return False, (
                f"账号 {mt5_login} 超出授权配额（{snap.edition_label} 允许 {snap.max_accounts} 个账号，"
                f"已占用 {used} 个）。该账号已停止开新仓；其余账号与现有持仓不受影响。"
            )

        db.add(
            ActivationRecord(
                license_key=snap.license_key,
                mt5_login=mt5_login,
                mt5_account_id=account_id or None,
                machine_fingerprint=snap.machine_fingerprint,
            )
        )
        db.commit()
        invalidate_cache()
        logger.info(f"[授权] 账号 {mt5_login} 占用配额坑位 {used + 1}/{snap.max_accounts}")
        return True, ""
    except Exception as e:
        db.rollback()
        logger.error(f"[授权] 配额占坑失败，放行: {type(e).__name__}: {e}")
        return True, ""
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════
#  激活 / 解绑
# ══════════════════════════════════════════════════════════════
def activate(token: str, customer_hint: str = "") -> Dict[str, Any]:
    """
    本地激活：验签 → 校验机器绑定 → 落库。返回 {"ok":bool,"message":str,"status":{...}}

    刻意**不要求联网**。客户机可能在隔离网络里，激活流程如果强依赖我们的服务器，
    等于把「客户能不能开工」和「我们服务器在不在」绑死了。
    联网只在续期/吊销时通过心跳做增量校正。
    """
    from app.database import SessionLocal
    from app.models.license import License

    try:
        claims = verify_token((token or "").strip())
    except TokenError as e:
        return {"ok": False, "message": f"激活失败：{e.message}", "code": e.code}

    now = datetime.utcnow()
    if claims.is_expired(now + timedelta(hours=-GRACE_HOURS)):
        # 允许拿宽限期内的令牌激活（客户续费前先装上），但纯过期令牌直接拒
        if claims.valid_until and now >= claims.valid_until + timedelta(hours=GRACE_HOURS):
            return {
                "ok": False,
                "message": f"该授权已于 {claims.valid_until:%Y-%m-%d} 到期，请联系服务商续期。",
                "code": "LICENSE_EXPIRED",
            }

    current = fp.collect_factors()
    if claims.is_bound:
        ok, hits = fp.match_factors(current, claims.machine)
        if not ok:
            return {
                "ok": False,
                "message": f"该授权已绑定其他设备（硬件特征命中 {hits}/3，需至少 2）。请联系服务商解绑后重试。",
                "code": "MACHINE_MISMATCH",
            }

    db = SessionLocal()
    try:
        row = db.query(License).filter(License.license_key == claims.license_key).first()
        if row is None:
            row = License(license_key=claims.license_key)
            db.add(row)

        row.token = token.strip()
        row.edition = claims.edition
        row.customer_name = claims.customer or customer_hint or row.customer_name
        row.max_accounts = claims.max_accounts
        row.valid_from = claims.valid_from
        row.valid_until = claims.valid_until
        row.status = "active"
        row.machine_fingerprint = fp.compute_fingerprint(current)
        row.bound_factors = json.dumps(claims.machine or current, ensure_ascii=False)
        row.activated_at = row.activated_at or now
        row.last_seen_at = now
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[授权] 激活落库失败: {type(e).__name__}: {e}")
        return {"ok": False, "message": "激活失败：本地保存出错，请重试或联系技术支持。", "code": "DB_ERROR"}
    finally:
        db.close()

    invalidate_cache()
    snap = evaluate(force=True)
    logger.info(f"[授权] 激活成功 {_mask_key(claims.license_key)} / {claims.edition_label} / {claims.max_accounts} 账号")
    return {"ok": True, "message": f"激活成功：{claims.edition_label}（{claims.max_accounts} 个账号）", "status": snap.to_dict()}


def release_account(mt5_login: str) -> Dict[str, Any]:
    """释放某账号占用的配额坑（客户停用某个号后腾位置）。软删除，保留对账痕迹。"""
    from app.database import SessionLocal
    from app.models.license import ActivationRecord

    db = SessionLocal()
    try:
        rows = (
            db.query(ActivationRecord)
            .filter(
                ActivationRecord.mt5_login == str(mt5_login),
                ActivationRecord.revoked.is_(False),
            )
            .all()
        )
        for r in rows:
            r.revoked = True
            r.revoked_at = datetime.utcnow()
        db.commit()
        invalidate_cache()
        return {"ok": True, "released": len(rows)}
    except Exception as e:
        db.rollback()
        return {"ok": False, "message": str(e)}
    finally:
        db.close()


def heartbeat() -> Dict[str, Any]:
    """
    向平台心跳一次：上报「我是谁、装在哪台机」，取回「续期结果 / 是否被吊销」。

    ★ 心跳是**增量校正**，不是开仓前置条件。
      失败只写 last_heartbeat_error，绝不改变 allow_open。理由很直白：
      把心跳做成前置条件，等于让我们的服务器成为客户交易的单点故障——
      我们宕机 10 分钟，全体客户停摆 10 分钟，赔付远超盗版损失。

    返回结构对前端友好：{"ok":..., "mode":"offline|online", "message":...}
    """
    from app.config import settings

    url = (getattr(settings, "LICENSE_SERVER_URL", "") or "").strip()
    if not url:
        return {
            "ok": True,
            "mode": "offline",
            "message": "当前为离线授权模式，令牌本地验签，无需联网",
        }

    row = _load_license_row()
    if row is None or not row.token:
        return {"ok": False, "mode": "online", "message": "本机尚未激活授权"}

    payload = {
        "license_key": row.license_key,
        "machine_fingerprint": row.machine_fingerprint or fp.compute_fingerprint(),
        "version": getattr(settings, "APP_VERSION", ""),
    }

    from app.database import SessionLocal
    from app.models.license import License

    try:
        import requests

        resp = requests.post(f"{url.rstrip('/')}/api/license/heartbeat", json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:200]
        logger.warning(f"[授权] 心跳失败（不影响交易）: {msg}")
        db = SessionLocal()
        try:
            r = db.query(License).filter(License.id == row.id).first()
            if r:
                r.last_heartbeat_error = msg
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        return {"ok": False, "mode": "online", "message": "暂时无法连接授权服务器，不影响当前交易"}

    now = datetime.utcnow()
    new_token = str(data.get("token") or "").strip()
    server_status = str(data.get("status") or "active")

    db = SessionLocal()
    try:
        r = db.query(License).filter(License.id == row.id).first()
        if r:
            r.last_heartbeat_at = now
            r.last_heartbeat_error = None
            # 平台侧停用/恢复以服务端为准
            if server_status in ("active", "suspended", "expired"):
                r.status = server_status
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # 续期：服务端下发了新令牌就地换上（客户无感续费）
    if new_token and new_token != row.token:
        res = activate(new_token)
        if res.get("ok"):
            logger.info("[授权] 心跳带回新令牌，已完成续期")
            return {"ok": True, "mode": "online", "message": "授权已续期", "renewed": True}

    invalidate_cache()
    return {"ok": True, "mode": "online", "message": "心跳正常", "status": server_status}


def get_status() -> Dict[str, Any]:
    """给接口/前端用的完整状态。"""
    snap = evaluate()
    d = snap.to_dict()
    d["fingerprint_detail"] = fp.describe()
    d["grace_hours"] = GRACE_HOURS
    d["trial_days"] = TRIAL_DAYS
    return d
