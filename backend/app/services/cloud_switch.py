"""
云模型总开关 —— 运行时热切换。

把原本静态的 settings.ENABLE_CLOUD_MODELS 升级为：
  1. 启动时从 runtime_config 表读取持久化值（无记录则回退 .env/settings 默认值）。
  2. 运行时通过 /api/keys/cloud-toggle 修改，立即写库并同步内存 settings。
  3. 交易链路统一调用 effective_cloud_enabled()，自动判定：
     主开关 ON 且至少有一个云端 Key 源（DB 启用 或 .env fallback）→ 云端双脑混跑
     否则 → 本地多模型融合决策

这样既有「一键切本地」的显式开关，又有「所有 Key 被禁用后自动降级本地」的兜底。
"""
from __future__ import annotations

from loguru import logger

from app.config import settings
from app.database import SessionLocal, safe_commit
from app.models.runtime_config import RuntimeConfig

CLOUD_CONFIG_KEY = "enable_cloud_models"


def init_cloud_switch() -> None:
    """启动时调用：从 runtime_config 加载云模型开关到内存 settings。"""
    # ★ 幂等自修复：RuntimeConfig 模型在 init_db() 之后才被导入注册到 Base.metadata，
    #   历史部署会漏建 runtime_config 表，导致云开关写库失败、跨重启回退默认 True。
    #   此处显式建表（仅本表），确保开关状态可持久化、热切换跨重启保持。
    try:
        from app.database import engine, Base
        Base.metadata.create_all(bind=engine, tables=[RuntimeConfig.__table__])
    except Exception as _e:  # noqa: BLE001
        logger.warning(f"[CloudSwitch] 建表自修复失败（可忽略）: {_e}")
    try:
        with SessionLocal() as db:
            row = db.query(RuntimeConfig).filter(RuntimeConfig.key == CLOUD_CONFIG_KEY).first()
            if row and row.value is not None:
                enabled = str(row.value).strip().lower() in ("true", "1", "yes", "on")
                settings.ENABLE_CLOUD_MODELS = enabled
                logger.info(f"[CloudSwitch] 已从 DB 加载云模型开关: {enabled}")
            else:
                # 无记录则回退 .env/settings 默认值，并顺手落库，避免下次仍走回退
                enabled = bool(getattr(settings, "ENABLE_CLOUD_MODELS", True))
                _persist(db, enabled)
                logger.info(f"[CloudSwitch] 无 DB 记录，回退 .env 默认值: {enabled}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CloudSwitch] 启动加载失败（不影响运行）: {e}")


def get_cloud_master_enabled() -> bool:
    """用户/运维显式设置的主开关状态（内存 settings，实时）。"""
    return bool(getattr(settings, "ENABLE_CLOUD_MODELS", True))


def set_cloud_master_enabled(enabled: bool) -> bool:
    """切换主开关：写库 + 同步内存。失败时返回 False 但不抛异常。"""
    settings.ENABLE_CLOUD_MODELS = enabled
    try:
        with SessionLocal() as db:
            _persist(db, enabled)
        logger.info(f"[CloudSwitch] 云模型主开关已切换为: {enabled}")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"[CloudSwitch] 切换失败: {e}")
        return False


def has_any_active_cloud_key() -> bool:
    """是否存在可用的云端 Key 源（DB 中启用的 Key 或 .env fallback）。"""
    try:
        from app.services.key_pool import get_all_pools

        for provider, pool in get_all_pools().items():
            if pool.size() > 0:
                return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CloudSwitch] 探测 Key 池失败: {e}")
    return False


def effective_cloud_enabled() -> bool:
    """
    交易链路/仪表盘应使用的「有效云模型状态」。
    仅当主开关打开且至少有一个云端 Key 可用时才真正启用云模型。
    """
    return get_cloud_master_enabled() and has_any_active_cloud_key()


def cloud_status() -> dict:
    """供前端 /api/keys/cloud-status 使用的状态聚合。"""
    master = get_cloud_master_enabled()
    pools = {}
    try:
        from app.services.key_pool import get_all_pools

        for provider in ("deepseek", "hunyuan"):
            pool = get_all_pools().get(provider)
            if pool and pool.size() > 0:
                # 区分是否只有 .env fallback
                has_env = any(getattr(it, "is_env_fallback", False) for it in pool.items)
                only_env = has_env and pool.size() == 1
                pools[provider] = {
                    "available": True,
                    "source": "env_fallback_only" if only_env else ("db+env" if has_env else "db"),
                    "count": pool.size(),
                }
            else:
                pools[provider] = {"available": False, "source": "missing", "count": 0}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CloudSwitch] 聚合 Key 池状态失败: {e}")
        pools = {
            "deepseek": {"available": False, "source": "error", "count": 0},
            "hunyuan": {"available": False, "source": "error", "count": 0},
        }

    effective = master and any(p.get("available") for p in pools.values())
    mode = "cloud_hybrid" if effective else "local_only"
    mode_label = "云端双脑混跑" if effective else "本地多模型融合决策"
    sub_label = (
        "DeepSeek + 混元 + 本地时序融合票协同裁决"
        if effective else
        "所有云端 Key 已停用 / 无可用 Key，仅由本地时序融合票 + Qwen3-8B 校对员决策"
    )

    return {
        "master_enabled": master,
        "effective_enabled": effective,
        "mode": mode,
        "mode_label": mode_label,
        "sub_label": sub_label,
        "providers": pools,
    }


def _persist(db, enabled: bool) -> None:
    """写库；调用方须持有 session 并自行 commit（通过 safe_commit）。"""
    row = db.query(RuntimeConfig).filter(RuntimeConfig.key == CLOUD_CONFIG_KEY).first()
    value = "true" if enabled else "false"
    if row:
        row.value = value
    else:
        row = RuntimeConfig(key=CLOUD_CONFIG_KEY, value=value)
        db.add(row)
    safe_commit(db)
