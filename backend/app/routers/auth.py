"""
认证路由 — 用户注册/登录 + JWT 认证中间件
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt, JWTError

from app.database import get_db
from app.config import settings
from app.models.user import User, SubscriptionTier
from app.utils.time_utils import _to_utc_iso  # ★ 2026-08-15 #5 统一时区：禁止手写 .isoformat() 引入 8h 偏移

# ★ P0-3 根因修复：禁止任何硬编码回退密钥。缺失 SECRET_KEY 时拒绝启动，
# 否则 JWT 会用已知的 "dev-secret" 签发/校验，Token 可被任意伪造（认证绕过）。
_JWT_SECRET = settings.SECRET_KEY
if not _JWT_SECRET:
    raise RuntimeError(
        "SECRET_KEY 未配置：拒绝启动。请在 backend/.env 中设置 SECRET_KEY，"
        "禁止使用 dev-secret 等硬编码回退密钥（会导致 JWT 可被任意伪造）。"
    )

router = APIRouter(prefix="/api/auth", tags=["认证"])
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
security = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


# ========== JWT 认证依赖 — 所有受保护路由共用 ==========

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """从 JWT Token 解析当前登录用户，所有受保护路由注入此依赖"""
    if not credentials:
        raise HTTPException(status_code=401, detail="请先登录")
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token 无效")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已禁用")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """管理员专用依赖：非管理员一律 403。

    用途：平台级（跨客户）操作——如全局紧急停止/恢复——只能由运维管理员触发，
    防止任一普通客户越权操控全平台其他客户的交易（多租户隔离红线）。
    账号级操作走 _owned_accounts 归属校验即可，不必要求 admin。
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ========== 路由 ==========

@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    """用户注册"""
    import re
    # A1/A2：邮箱归一化(lower) + 格式校验；密码最小长度校验，杜绝弱注册
    email = (req.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if not req.password or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="邮箱已注册")
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(status_code=400, detail="用户名已占用")

    user = User(
        email=email,
        username=req.username,
        hashed_password=pwd_context.hash(req.password),
        subscription_tier=SubscriptionTier.FREE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = _create_token(user)
    return {"access_token": token, "token_type": "bearer", "user": _user_to_dict(user)}


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """用户登录"""
    email = (req.email or "").strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="该邮箱未注册，请先注册账号")
    if not pwd_context.verify(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="密码错误，请重新输入")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账户已禁用")

    token = _create_token(user)
    return {"access_token": token, "token_type": "bearer", "user": _user_to_dict(user)}


@router.get("/me")
def get_me(user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return _user_to_dict(user)


def _create_token(user: User) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user.id,
        "email": user.email,
        "exp": expire,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "subscription_tier": user.subscription_tier.value if user.subscription_tier else "free",
        "is_admin": user.is_admin,
        "created_at": _to_utc_iso(user.created_at),
    }
