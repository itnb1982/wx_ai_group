"""
XAU/USD万象Ai自动量化交易系统 — AES-256 加密工具
用于加密存储 API Key、MT5 密码等敏感数据
"""
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from app.config import settings


# ★ P0-4 根因修复：禁止任何默认回退密钥。缺失 SECRET_KEY 时拒绝启动，
# 否则 AES 密钥会用已知的 "wanxiangai-default-key-change-me" 派生，
# 导致 API Key / MT5 密码等加密凭证可被本地逆向还原（凭证泄露）。
_CRYPTO_SECRET = settings.SECRET_KEY
if not _CRYPTO_SECRET:
    raise RuntimeError(
        "SECRET_KEY 未配置：拒绝启动。AES 凭证加密密钥派生将退化为已知默认值，"
        "存在加密数据被还原的泄露风险，请在 backend/.env 中设置 SECRET_KEY。"
    )


def _get_cipher() -> Fernet:
    """获取 AES-256 加密器"""
    # 使用 SECRET_KEY 派生加密密钥
    secret = _CRYPTO_SECRET
    salt = b"wanxiangai_salt_2026"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """AES-256 加密"""
    if not plaintext:
        return ""
    cipher = _get_cipher()
    return cipher.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """AES-256 解密"""
    if not ciphertext:
        return ""
    cipher = _get_cipher()
    return cipher.decrypt(ciphertext.encode()).decode()
