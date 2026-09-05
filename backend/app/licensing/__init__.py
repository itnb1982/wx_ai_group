"""
授权与激活（V6 Phase 8）

包内分工：
    fingerprint.py  机器指纹三要素采集与三取二比对
    token.py        Ed25519 令牌的签发/验签与授权码规范化
    service.py      授权状态机（离线宽限期、配额、失效行为）

★ 红线（与 V6 9.4.2「只关水龙头不抽水」同源）：
  授权失效**只停开新仓，绝不强平任何持仓**。
  授权是商业契约，不是风险控制手段。客户欠费是商务问题，
  用强平客户仓位去催款，会把一次续费纠纷变成一次赔付诉讼。
  已有仓位继续交由各账号自身的 SL/TP/SmartExit 正常管理。
"""
from app.licensing.token import (  # noqa: F401
    LicenseClaims,
    TokenError,
    generate_license_key,
    normalize_license_key,
    verify_token,
)

__all__ = [
    "LicenseClaims",
    "TokenError",
    "verify_token",
    "generate_license_key",
    "normalize_license_key",
]
