"""
API Key 管理路由
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User
from app.models.api_key import APIKey
from app.utils.crypto import encrypt
from app.utils.time_utils import _to_utc_iso  # ★ 2026-08-15 #5 统一时区
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/keys", tags=["API密钥"])


class AddKeyRequest(BaseModel):
    provider: str  # deepseek / hunyuan
    key_name: str
    api_key: str
    secret_key: str = ""


class CloudToggleRequest(BaseModel):
    enabled: bool


@router.get("/")
def list_keys(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取当前用户的API Key（密钥脱敏）+ 系统内置 .env 回退 Key 列表。
    返回结构：{ keys: [DB Key 列表], env_fallbacks: [.env 回退 Key 列表（只读）] }
    前端兼容：原本 listKeys() 直接拿数组，新版自动适配（取 keys 字段）。
    """
    keys = db.query(APIKey).filter(APIKey.user_id == user.id).all()
    db_keys = [
        {
            "id": k.id,
            "provider": k.provider,
            "key_name": k.key_name,
            "is_active": k.is_active,
            "is_valid": k.is_valid,
            "last_validated_at": _to_utc_iso(k.last_validated_at),
            "total_tokens": k.total_tokens,
            "monthly_tokens": k.monthly_tokens,
            "monthly_cost": round(k.monthly_cost, 4),
            "source": "db",
            "is_env_fallback": False,
            "is_system": False,
        }
        for k in keys
    ]
    # 附加 .env 回退 Key（系统内置 / 不可手动管理 / 仅作状态展示）
    from app.services.key_pool import get_all_pools
    env_keys = []
    for provider, pool in get_all_pools().items():
        for it in pool.items:
            if not it.is_env_fallback:
                continue
            env_keys.append({
                "id": it.key_id,           # 虚拟 id（前端按字符串处理即可）
                "provider": it.provider,
                "key_name": it.key_name,
                "is_active": True,          # env fallback 永远"启用"
                "is_valid": True,
                "total_tokens": it.total_tokens,
                "monthly_tokens": it.total_tokens,
                "monthly_cost": round(it.total_cost_usd, 4),
                "source": "env_fallback",
                "is_env_fallback": True,
                "is_system": True,          # 标记"系统内置 / 不可手动管理"
                "masked_key": (it.api_key[:4] + "***" + it.api_key[-2:]) if it.api_key and len(it.api_key) > 8 else "***",
            })
    return {"keys": db_keys, "env_fallbacks": env_keys}


@router.post("/")
def add_key(req: AddKeyRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """添加 API Key"""
    if req.provider not in ["deepseek", "hunyuan"]:
        raise HTTPException(status_code=400, detail="不支持的提供商")

    key = APIKey(
        user_id=user.id,
        provider=req.provider,
        key_name=req.key_name,
        encrypted_key=encrypt(req.api_key),
        encrypted_secret=encrypt(req.secret_key) if req.secret_key else "",
    )
    db.add(key)
    db.commit()
    return {"id": key.id, "provider": key.provider, "key_name": key.key_name}


@router.delete("/{key_id}")
def delete_key(key_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """删除 API Key"""
    key = db.query(APIKey).filter(APIKey.id == key_id, APIKey.user_id == user.id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key不存在")
    db.delete(key)
    db.commit()
    return {"ok": True    }


def _toggle_key_sync(key_id: str, active: bool, user: User, db: Session):
    """toggle_key 的同步实现体，供线程池 offload。"""
    from app.utils.crypto import decrypt
    from app.config import settings
    from openai import OpenAI
    from datetime import datetime, timezone

    key = db.query(APIKey).filter(APIKey.id == key_id, APIKey.user_id == user.id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key不存在")

    # 禁用：直接关，不调 API
    if not active:
        key.is_active = False
        db.commit()
        return {"is_active": False, "is_valid": key.is_valid, "message": "已禁用"}

    # 启用：先验证，验证通过才真正启用
    try:
        plain = decrypt(key.encrypted_key) if key.encrypted_key else ""
    except Exception as e:
        return {"is_active": False, "is_valid": False, "error": f"解密失败: {str(e)[:200]}"}
    if not plain:
        return {"is_active": False, "is_valid": False, "error": "Key 加密串为空"}

    try:
        if key.provider == "deepseek":
            cli = OpenAI(api_key=plain, base_url=settings.DEEPSEEK_BASE_URL)
            cli.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": "请只回复OK"}],
                max_tokens=10,
            )
        elif key.provider == "hunyuan":
            base = getattr(settings, "HUNYUAN_BASE_URL", "https://api.hunyuan.tencent.com/v1")
            model = getattr(settings, "HUNYUAN_MODEL", "hunyuan-pro")
            cli = OpenAI(api_key=plain, base_url=base)
            cli.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "请只回复OK"}],
                max_tokens=10,
            )
        else:
            return {"is_active": False, "is_valid": False, "error": f"不支持的 provider: {key.provider}"}
    except Exception as e:
        err = str(e)[:240]
        key.is_valid = False
        key.is_active = False  # 验证失败不允许启用
        key.last_validated_at = datetime.now(timezone.utc)
        db.commit()
        return {"is_active": False, "is_valid": False, "error": err}

    # 验证通过：启用 + 标记有效
    key.is_active = True
    key.is_valid = True
    key.last_validated_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "is_active": True,
        "is_valid": True,
        "last_validated_at": _to_utc_iso(key.last_validated_at),
        "message": "已启用并通过验证",
    }


@router.post("/{key_id}/toggle")
async def toggle_key(key_id: str, active: bool = True, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    启用/禁用 Key。
    启用时（active=true）会自动调一次 API 验证：验证通过才真正启用，
    验证失败则保持禁用并返回错误原因——解决"启用却显示无效"的根因。

    2026-08-09：外部 API 调用可能阻塞 10-30s，改为 async + to_thread offload，
    避免阻塞事件循环导致前端 health 红条。
    """
    import asyncio
    return await asyncio.to_thread(_toggle_key_sync, key_id, active, user, db)


def _test_key_sync(key_id: str, user: User, db: Session):
    """test_key 的同步实现体，供线程池 offload。"""
    from app.utils.crypto import decrypt
    from app.config import settings
    from openai import OpenAI
    from datetime import datetime, timezone

    key = db.query(APIKey).filter(APIKey.id == key_id, APIKey.user_id == user.id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Key不存在")

    # 解密
    try:
        plain = decrypt(key.encrypted_key) if key.encrypted_key else ""
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解密失败: {e}")
    if not plain:
        raise HTTPException(status_code=400, detail="Key 加密串为空")

    # 实际调 API（轻量探测：max_tokens=10）
    err_msg = None
    content = ""
    try:
        if key.provider == "deepseek":
            cli = OpenAI(api_key=plain, base_url=settings.DEEPSEEK_BASE_URL)
            r = cli.chat.completions.create(
                model=settings.DEEPSEEK_MODEL,
                messages=[{"role": "user", "content": "请只回复OK两个字"}],
                max_tokens=10,
            )
            content = r.choices[0].message.content or ""
        elif key.provider == "hunyuan":
            base = getattr(settings, "HUNYUAN_BASE_URL", "https://api.hunyuan.tencent.com/v1")
            model = getattr(settings, "HUNYUAN_MODEL", "hunyuan-pro")
            cli = OpenAI(api_key=plain, base_url=base)
            r = cli.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "请只回复OK两个字"}],
                max_tokens=10,
            )
            content = r.choices[0].message.content or ""
        else:
            raise HTTPException(status_code=400, detail=f"不支持的 provider: {key.provider}")
    except Exception as e:
        err_msg = str(e)[:240]
        key.is_valid = False
        key.last_validated_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "ok": False,
            "is_valid": False,
            "last_validated_at": _to_utc_iso(key.last_validated_at),
            "error": err_msg,
        }

    # 调通
    key.is_valid = True
    key.last_validated_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "ok": True,
        "is_valid": True,
            "last_validated_at": _to_utc_iso(key.last_validated_at),
            "preview": (content or "")[:60],
    }


@router.post("/{key_id}/test")
async def test_key(key_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    真实调一次 API 验证 Key 有效性，并回写 is_valid + last_validated_at。
    解决"启用但显示无效"问题（之前审计时 401 自动下线留下的 is_valid=False 状态）。

    2026-08-09：外部 API 调用可能阻塞，改为 async + to_thread offload。
    """
    import asyncio
    return await asyncio.to_thread(_test_key_sync, key_id, user, db)


# ──────────────────────────────────────────────────────────────
# 云模型总开关（客户自选）
#   主开关 ON 且至少一个云端 Key 可用 → 云端双脑 + 本地融合混跑
#   否则 → 纯本地多模型融合决策（客户全部禁用 Key 时自动降级到此）
# 热切换、写库 + 同步内存 settings、立即生效、无需重启。
# ──────────────────────────────────────────────────────────────
@router.get("/cloud-status")
def cloud_status_endpoint(user: User = Depends(get_current_user)):
    """返回云模型总开关聚合状态：主开关 / 生效模式 / 各 provider 可用性。"""
    from app.services.cloud_switch import cloud_status
    return cloud_status()


@router.post("/cloud-toggle")
def cloud_toggle(req: CloudToggleRequest, user: User = Depends(get_current_user)):
    """
    切换云模型总开关（主开关）。写库 + 同步内存 settings，立即生效、无需重启。
    返回切换后的最新聚合状态，便于前端直接刷新。
    """
    from app.services.cloud_switch import set_cloud_master_enabled, cloud_status
    ok = set_cloud_master_enabled(req.enabled)
    if not ok:
        raise HTTPException(status_code=500, detail="云模型开关切换失败（数据库写入异常）")
    return cloud_status()

