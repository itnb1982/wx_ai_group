"""
授权与激活 API（V6 Phase 8.3）

端点：
  GET  /api/license/status      当前授权状态（前端徽章/到期提示条的数据源）
  GET  /api/license/machine     本机机器码（客户报障、申请授权时要报给我们）
  POST /api/license/activate    离线激活（粘贴令牌）
  POST /api/license/heartbeat   主动心跳（续期下发 / 吊销通知）
  POST /api/license/release     释放某账号占用的配额坑

╔══════════════════════════════════════════════════════════════════════╗
║ 为什么 status 和 machine 不要求登录：                                  ║
║   授权过期后客户可能连登录页都过不去（未来若把登录也做成受限功能），    ║
║   而「查看自己的授权状态」和「读出机器码去申请授权」恰恰是这时候        ║
║   最需要的两个动作。把它们锁在登录后面，等于把求助电话线也剪了。        ║
║   这两个端点不返回任何敏感信息：授权码已脱敏，机器码只是哈希。          ║
║                                                                      ║
║ activate / release 则必须登录：它们会改变系统状态。                    ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from typing import Optional

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel

from app.licensing import service as license_service
from app.licensing import fingerprint as fp
from app.models.user import User
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/license", tags=["授权"])


class ActivateRequest(BaseModel):
    token: str
    customer: Optional[str] = ""


class ReleaseRequest(BaseModel):
    mt5_login: str


@router.get("/status")
def get_status():
    """当前授权状态。前端每分钟拉一次，服务端有 30s 缓存，不会打穿。"""
    try:
        return {"success": True, "data": license_service.get_status()}
    except Exception as e:
        # 连状态都取不到时也不能把前端卡死——返回一个"未知但放行"的状态，
        # 前端据此不显示任何拦截提示，避免虚假告警吓到客户。
        logger.error(f"[授权API] status 异常: {type(e).__name__}: {e}")
        return {
            "success": True,
            "data": {"state": "disabled", "state_label": "未知", "allow_open": True, "message": ""},
        }


@router.get("/machine")
def get_machine_code():
    """
    本机机器码。客户申请/迁移授权时需要把它报给我们。

    只返回哈希，不返回主板 UUID/MAC 原文 —— 客户会把这个页面截图发到群里，
    原始硬件标识不该出现在截图上。
    """
    try:
        d = fp.describe()
        return {
            "success": True,
            "data": {
                "machine_code": d["fingerprint"],
                "factors_count": d["factors_count"],
                "factors_present": d["factors_present"],
                "platform": d["platform"],
                "hint": "请将机器码提供给服务商以获取授权令牌",
            },
        }
    except Exception as e:
        return {"success": False, "message": f"机器码获取失败: {e}"}


@router.post("/activate")
def activate(req: ActivateRequest, current_user: User = Depends(get_current_user)):
    """离线激活。刻意不依赖联网——客户机可能在隔离网络里。"""
    result = license_service.activate(req.token, customer_hint=req.customer or "")
    if not result.get("ok"):
        return {"success": False, "message": result.get("message"), "code": result.get("code")}
    return {"success": True, "message": result.get("message"), "data": result.get("status")}


@router.post("/heartbeat")
def heartbeat(current_user: User = Depends(get_current_user)):
    """
    主动心跳。

    ★ 心跳失败**不影响交易**。它只做两件事：把续期结果拉下来、把吊销通知拉下来。
      如果把心跳做成开仓前置条件，我们的服务器一挂，全体客户同时停摆——
      这个耦合在商业上是不可接受的（见 config 里 LICENSE_SERVER_URL 的注释）。
    """
    return {"success": True, "data": license_service.heartbeat()}


@router.post("/release")
def release(req: ReleaseRequest, current_user: User = Depends(get_current_user)):
    """释放账号占用的配额坑（软删除，保留对账痕迹）。"""
    r = license_service.release_account(req.mt5_login)
    if not r.get("ok"):
        return {"success": False, "message": r.get("message")}
    return {"success": True, "message": f"已释放 {r.get('released', 0)} 个坑位"}
