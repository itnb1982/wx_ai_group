"""
授权令牌 — Ed25519 离线可验的授权凭证（V6 Phase 8.1）

═══ 为什么是非对称签名，不是「联网查授权」 ═══

交易系统必须能离线跑。客户机断网、我们的服务器挂了、客户公司防火墙拦了外网——
这些情况下系统都得继续管理已有仓位。如果授权靠「每次开仓前查一下服务器」，
那我们的服务器就成了客户交易的单点故障，这在商业上是不可接受的。

所以用非对称签名：
    平台持**私钥**签发令牌（私钥永不出平台，不进客户安装包）
    客户端内置**公钥**本地验签（断网也能验，验的是数学，不是网络）

客户能读到公钥，但公钥推不出私钥，所以**伪造不了新令牌**。
客户能做的最多是「继续用一张已到期的旧令牌」，而这被 valid_until 和
心跳挡住（见 service.py 的 72h 宽限期状态机）。

═══ 防破解的诚实边界（写在这里，免得后人误以为这是铜墙铁壁）═══

客户端代码在客户机器上，理论上一定可破：改公钥、patch 掉验签调用、
直接 return True，都能绕过。我们不假装能防住这些。

真正的护城河在服务端 ——
    · 云模型 API Key 由平台按授权状态发放，破解版拿不到有效 Key，
      DeepSeek/混元 全线不可用，只剩本地 L2 档，决策质量断崖式下降；
    · 破解版拿不到模型/参数的持续更新。
本层的作用是**抬高门槛、留下证据、让正常客户走正规流程**，不是绝对防御。

═══ 令牌格式 ═══
    WXAI1.<base64url(payload_json)>.<base64url(ed25519_signature)>

签名对象是 base64 解码后的**原始 payload 字节**，不是重新序列化的 JSON。
这一点很关键：如果验签时重新 json.dumps 再签，任何 key 顺序/空格差异
都会导致验签失败，而这类失败极难排查（"明明是我们自己签的却验不过"）。
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

TOKEN_PREFIX = "WXAI1"

# 公钥文件位置（随客户端分发）
_KEY_DIR = Path(__file__).resolve().parent / "keys"
_PUBLIC_KEY_FILE = _KEY_DIR / "license_public.pem"

# ★ 公钥指纹硬校验（发布前必须填）
#   只把公钥放文件里，破解者替换 pem 文件 + 自己签一张永久证书就完事了。
#   把公钥的 SHA256 写死在代码里，替换 pem 就必须同时改代码/改二进制，
#   门槛从「记事本编辑」抬到「逆向打包产物」。留空 = 跳过校验（仅开发期）。
EXPECTED_PUBLIC_KEY_SHA256 = "07fc4d060034264612fabbad8f2e8a4b7ddabd656d0ed9c6ddee6ba666d12540"

# 档位。max_accounts 的默认值只是兜底，真实配额以令牌里的字段为准。
EDITIONS = {
    "trial": {"label": "试用版", "default_accounts": 1},
    "standard": {"label": "标准版", "default_accounts": 1},
    "pro": {"label": "专业版", "default_accounts": 10},
}

_pubkey_cache: Any = None
_pubkey_loaded = False


# ══════════════════════════════════════════════════════════════
#  base64url（无填充）
# ══════════════════════════════════════════════════════════════
def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    """
    严格 base64url 解码。

    刻意用 b64decode(validate=True) 而不是 urlsafe_b64decode：后者会**静默丢弃**
    非法字符，于是「客户粘贴令牌时混进了换行或空格」会一路走到验签那步，
    报出「签名校验失败」——客服据此会去查订单以为授权无效，实际只是粘贴脏了。
    严格解码把这类问题当场定性为「编码损坏，请重新复制」，省一通电话。
    """
    padded = (s + "=" * (-len(s) % 4)).encode()
    return base64.b64decode(padded, altchars=b"-_", validate=True)


# ══════════════════════════════════════════════════════════════
#  Claims
# ══════════════════════════════════════════════════════════════
@dataclass
class LicenseClaims:
    """验签通过后的授权内容。所有时间均为 UTC naive（与全库 datetime.utcnow 一致）。"""

    license_key: str = ""
    edition: str = "trial"
    customer: str = ""
    max_accounts: int = 1
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    machine: Dict[str, str] = field(default_factory=dict)  # 绑定的三要素哈希，空 = 未绑定
    issued_at: Optional[datetime] = None
    activation_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_bound(self) -> bool:
        return bool(self.machine) and any(self.machine.values())

    @property
    def edition_label(self) -> str:
        return EDITIONS.get(self.edition, {}).get("label", self.edition)

    def not_yet_valid(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        return bool(self.valid_from and now < self.valid_from)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        # valid_until 为空 = 永久授权（买断客户），不是「立即过期」。
        # 这个默认值搞反过会直接把永久客户锁死，务必留意。
        return bool(self.valid_until and now >= self.valid_until)

    def days_remaining(self, now: Optional[datetime] = None) -> Optional[int]:
        if not self.valid_until:
            return None  # 永久
        now = now or datetime.utcnow()
        return max(0, (self.valid_until - now).days)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "license_key": self.license_key,
            "edition": self.edition,
            "edition_label": self.edition_label,
            "customer": self.customer,
            "max_accounts": self.max_accounts,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "is_bound": self.is_bound,
            "activation_id": self.activation_id,
            "days_remaining": self.days_remaining(),
        }


class TokenError(Exception):
    """令牌不可用。message 是可以直接展示给客户的中文。"""

    def __init__(self, message: str, code: str = "TOKEN_INVALID"):
        super().__init__(message)
        self.code = code
        self.message = message


# ══════════════════════════════════════════════════════════════
#  公钥加载
# ══════════════════════════════════════════════════════════════
def load_public_key(force: bool = False):
    """
    加载内置公钥。找不到返回 None（由上层决定是「拒绝一切令牌」还是「开发模式放行」）。

    优先级：环境变量 > 内置 pem 文件。环境变量是给我们自己测试用的
    （可以临时切到测试密钥对，不用改文件），生产客户机上不会设。
    """
    global _pubkey_cache, _pubkey_loaded
    if _pubkey_loaded and not force:
        return _pubkey_cache

    _pubkey_loaded = True
    _pubkey_cache = None

    pem_text = os.environ.get("WX_LICENSE_PUBLIC_KEY", "").strip()
    source = "env"
    if not pem_text:
        source = "file"
        try:
            pem_text = _PUBLIC_KEY_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning(f"[授权] 未找到公钥文件 {_PUBLIC_KEY_FILE.name}，令牌验签不可用")
            return None

    try:
        import hashlib

        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        key = load_pem_public_key(pem_text.encode())

        # 公钥指纹硬校验（防 pem 替换）
        if EXPECTED_PUBLIC_KEY_SHA256:
            from cryptography.hazmat.primitives import serialization

            raw = key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            digest = hashlib.sha256(raw).hexdigest()
            if digest != EXPECTED_PUBLIC_KEY_SHA256:
                logger.error(
                    f"[授权] 公钥指纹不匹配（期望 {EXPECTED_PUBLIC_KEY_SHA256[:12]}…，"
                    f"实际 {digest[:12]}…），拒绝加载。安装包可能已被篡改。"
                )
                return None

        _pubkey_cache = key
        logger.info(f"[授权] 公钥已加载（来源：{source}）")
        return key
    except Exception as e:
        logger.error(f"[授权] 公钥加载失败: {type(e).__name__}: {e}")
        return None


def public_key_available() -> bool:
    return load_public_key() is not None


# ══════════════════════════════════════════════════════════════
#  签发（仅平台侧使用 —— 客户机上没有私钥，调用必然失败）
# ══════════════════════════════════════════════════════════════
def build_payload(
    *,
    license_key: str,
    edition: str = "standard",
    customer: str = "",
    max_accounts: int = 1,
    valid_from: Optional[datetime] = None,
    valid_until: Optional[datetime] = None,
    machine: Optional[Dict[str, str]] = None,
    activation_id: str = "",
) -> Dict[str, Any]:
    return {
        "v": 1,
        "license_key": license_key,
        "edition": edition,
        "customer": customer,
        "max_accounts": int(max_accounts),
        "valid_from": (valid_from or datetime.utcnow()).isoformat(timespec="seconds"),
        "valid_until": valid_until.isoformat(timespec="seconds") if valid_until else None,
        "machine": machine or {},
        "activation_id": activation_id,
        "issued_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def sign_payload(payload: Dict[str, Any], private_key) -> str:
    """用私钥签发令牌字符串。sort_keys 保证同一份 payload 永远得到同一串字节。"""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    sig = private_key.sign(body)
    return f"{TOKEN_PREFIX}.{_b64e(body)}.{_b64e(sig)}"


# ══════════════════════════════════════════════════════════════
#  验签（客户端主路径）
# ══════════════════════════════════════════════════════════════
def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def verify_token(token: str, public_key=None) -> LicenseClaims:
    """
    验签并解析令牌。任何一步不过一律抛 TokenError —— 绝不返回「半可信」的结果。

    调用方拿到 LicenseClaims 就意味着：签名是平台签的、内容没被改过。
    但**有效期和机器绑定不在这里判**（那是策略，不是密码学），由 service.py 负责。
    """
    if not token or not isinstance(token, str):
        raise TokenError("授权令牌为空", "TOKEN_EMPTY")

    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise TokenError("授权令牌格式不正确", "TOKEN_MALFORMED")

    key = public_key or load_public_key()
    if key is None:
        raise TokenError("客户端公钥不可用，无法校验授权", "TOKEN_NO_PUBKEY")

    try:
        body = _b64d(parts[1])
        sig = _b64d(parts[2])
    except Exception:
        raise TokenError("授权令牌编码损坏", "TOKEN_MALFORMED")

    try:
        key.verify(sig, body)  # Ed25519：失败抛 InvalidSignature
    except Exception:
        # 不在日志里打完整 token（它是凭证）
        raise TokenError("授权令牌签名校验失败", "TOKEN_BAD_SIGNATURE")

    try:
        data = json.loads(body.decode())
    except Exception:
        raise TokenError("授权令牌内容无法解析", "TOKEN_MALFORMED")

    if not isinstance(data, dict) or not data.get("license_key"):
        raise TokenError("授权令牌缺少必要字段", "TOKEN_MALFORMED")

    machine = data.get("machine") or {}
    if not isinstance(machine, dict):
        machine = {}

    return LicenseClaims(
        license_key=str(data.get("license_key", "")),
        edition=str(data.get("edition", "trial")),
        customer=str(data.get("customer", "")),
        max_accounts=int(data.get("max_accounts", 1) or 1),
        valid_from=_parse_dt(data.get("valid_from")),
        valid_until=_parse_dt(data.get("valid_until")),
        machine={str(k): str(v) for k, v in machine.items()},
        issued_at=_parse_dt(data.get("issued_at")),
        activation_id=str(data.get("activation_id", "")),
        raw=data,
    )


# ══════════════════════════════════════════════════════════════
#  授权码（人手输入的那一串）
# ══════════════════════════════════════════════════════════════
# 刻意排除 0/O/1/I/L —— 客户是照着邮件手打的，这几个字符抄错率最高，
# 一个抄错就是一通客服电话。
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_license_key(prefix: str = "WXAI") -> str:
    """生成 WXAI-XXXXX-XXXXX-XXXXX-XXXXX 形式的授权码（平台侧签发用）。"""
    import secrets

    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(5)) for _ in range(4)]
    return f"{prefix}-" + "-".join(groups)


def normalize_license_key(raw: str) -> str:
    """
    规范化客户输入：去空格、转大写、补分隔符。

    客户会用各种方式输入同一个码：小写、粘贴带空格、丢掉横杠。
    在入口统一归一，比在数据库里存四种写法然后到处 OR 查询要干净得多。
    """
    if not raw:
        return ""
    s = "".join(ch for ch in str(raw).upper() if ch.isalnum())
    if not s:
        return ""
    if s.startswith("WXAI") and len(s) == 24:
        body = s[4:]
        return "WXAI-" + "-".join(body[i:i + 5] for i in range(0, 20, 5))
    return str(raw).strip().upper()
