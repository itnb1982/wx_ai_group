"""
万象AI — 人工紧急处置窗口 API（Phase 0 / V6）

端点：
  GET  /api/emergency/status    查看当前处置状态
  POST /api/emergency/halt      触发紧急停止（HALT_NEW / HALT_ALL，全局或单账号）
  POST /api/emergency/resume    解除停止
  POST /api/emergency/flatten   一键全平（平仓 + 自动封盘，防止 AI 立刻开回来）

╔══════════════════════════════════════════════════════════════════════╗
║ 关于"停止"的边界（这是最容易被误解的一点，改代码前务必读懂）：          ║
║                                                                      ║
║   紧急停止约束的是【系统自动发起】的交易动作，                        ║
║   不约束【人在界面上显式点击】的操作。                                ║
║                                                                      ║
║ 为什么必须这样划：                                                    ║
║   紧急停止的目的是"把控制权夺回到人手里"，不是"把人的手也铐上"。       ║
║   若 HALT 连人工操作一起挡，那么"一键全平"这个人工动作                 ║
║   会被自己刚触发的 HALT 挡住——逻辑自相矛盾，且人在最危急时反而        ║
║   什么都做不了。                                                      ║
║                                                                      ║
║ 手动下单端点(/api/trade/order)在 HALT 期间仍可用，但会强制要求         ║
║ override_halt=true 二次确认并留痕，避免"忘了自己停过机器"的误操作。     ║
╚══════════════════════════════════════════════════════════════════════╝
"""
import time
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db
from app.models.user import User
from app.models.mt5_account import MT5Account
from app.routers.auth import get_current_user
from app.services import emergency
from app.services.mt5_service import mt5_service

router = APIRouter(prefix="/api/emergency", tags=["紧急处置"])


class HaltRequest(BaseModel):
    level: str = emergency.LEVEL_HALT_NEW      # HALT_NEW / HALT_ALL
    scope: str = emergency.SCOPE_GLOBAL        # "global" 或 account_id
    reason: str = ""


class ResumeRequest(BaseModel):
    scope: str = emergency.SCOPE_GLOBAL


class FlattenRequest(BaseModel):
    scope: str = emergency.SCOPE_GLOBAL        # "global"=该用户全部账号，或单个 account_id
    reason: str = ""
    halt_after: bool = True                    # 平完是否自动封盘（默认是，见下方说明）


def _owned_accounts(user: User, db: Session, scope: str) -> List[MT5Account]:
    """按 scope 取出本用户名下的目标账号。

    账号数是变量（1 个或 10+ 个客户），此处一律动态查询，不假设数量。
    """
    q = db.query(MT5Account).filter(MT5Account.user_id == user.id)
    if scope != emergency.SCOPE_GLOBAL:
        q = q.filter(MT5Account.id == scope)
    accounts = q.all()
    if scope != emergency.SCOPE_GLOBAL and not accounts:
        raise HTTPException(404, f"账号不存在或不属于当前用户: {scope}")
    return accounts


def _flatten_one(account_id: str, max_rounds: int = 3) -> dict:
    """平掉单个账号的全部持仓，并复查确认真的平干净了。

    为什么要多轮：MT5 的 positions_get() 存在竞态，单次查询可能漏返回持仓
    （本项目已多次踩坑，get_all_positions_rescanned 就是为此而生）。
    紧急全平若漏一笔，人以为已经空仓、实际还在裸奔——这是不可接受的。
    所以这里"平完再查，查到还有就再平"，最后如实汇报是否清空。
    """
    closed, failed = [], []
    seen = set()

    for rnd in range(max_rounds):
        try:
            positions = mt5_service.get_all_positions(account_id) or []
        except Exception as e:
            failed.append({"ticket": None, "error": f"查询持仓失败: {e}"})
            break

        if not positions:
            break

        for p in positions:
            ticket = p.get("ticket")
            if ticket in seen:
                continue
            seen.add(ticket)
            try:
                r = mt5_service.close_position(account_id, ticket)
                if isinstance(r, dict) and r.get("error"):
                    failed.append({"ticket": ticket, "error": r["error"]})
                else:
                    closed.append(ticket)
            except Exception as e:
                failed.append({"ticket": ticket, "error": str(e)})

        time.sleep(0.4)   # 给 MT5 一点成交回执时间，再进入下一轮复查

    # 最终复查：如实汇报残留，不粉饰
    try:
        remaining = mt5_service.get_all_positions(account_id) or []
    except Exception:
        remaining = []

    return {
        "account_id": account_id,
        "closed": closed,
        "closed_count": len(closed),
        "failed": failed,
        "remaining_count": len(remaining),
        "remaining_tickets": [p.get("ticket") for p in remaining][:20],
        "fully_flat": len(remaining) == 0 and not failed,
    }


@router.get("/status")
def get_status(user: User = Depends(get_current_user)):
    """当前紧急处置状态（前端红色横幅数据源）。"""
    return {"ok": True, "data": emergency.summary()}


@router.post("/halt")
def trigger_halt(req: HaltRequest, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """触发紧急停止。

    HALT_NEW：只封新仓，已有持仓的止损/止盈继续保护（最常用）
    HALT_ALL：封新仓 + 停 AI 主动出场（视觉看护/反转全平/锁利）；
             但硬 SL/TP/追踪等保护性兜底仍有效（铁律6：只关水龙头不抽水）。
    """
    lv = (req.level or "").upper()
    if lv not in (emergency.LEVEL_HALT_NEW, emergency.LEVEL_HALT_ALL):
        raise HTTPException(400, f"level 仅支持 {emergency.LEVEL_HALT_NEW} / {emergency.LEVEL_HALT_ALL}")

    # 全局（跨客户）停止 = 平台级操作，仅管理员可触发（多租户隔离红线）
    if req.scope == emergency.SCOPE_GLOBAL and not user.is_admin:
        raise HTTPException(403, "全局紧急停止需要管理员权限")

    if req.scope != emergency.SCOPE_GLOBAL:
        _owned_accounts(user, db, req.scope)   # 越权校验：不能停别人的账号

    try:
        emergency.halt(lv, scope=req.scope, reason=req.reason,
                       by=getattr(user, "username", None) or user.id[:8])
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"ok": True, "data": emergency.summary()}


@router.post("/resume")
def trigger_resume(req: ResumeRequest, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """解除停止。

    注意：解除全局不会连带解除账号级停止——两者是独立开关。
    否则"我只想放开其他账号"会把出问题的那个也一起放出去。
    """
    if req.scope != emergency.SCOPE_GLOBAL:
        _owned_accounts(user, db, req.scope)
    elif not user.is_admin:
        # 全局（跨客户）恢复 = 平台级操作，仅管理员可触发
        raise HTTPException(403, "全局恢复需要管理员权限")

    emergency.resume(scope=req.scope, by=getattr(user, "username", None) or user.id[:8])
    return {"ok": True, "data": emergency.summary()}


def _flatten_all_sync(req: FlattenRequest, user: User, db: Session):
    """flatten_all 的同步实现体，供线程池 offload。

    ★★ 2026-08-10 修复：此前误定义为 `async def`，却被 `asyncio.to_thread`（同步包装）
    调用 → 子线程里调用 async 函数只返回 coroutine 不执行 body → HTTP 500
    `'coroutine' object is not iterable` → 一键全平/重启预平仓全部失效。
    必须是普通 def（同步函数）才能被 to_thread 正确 offload。
    """
    accounts = _owned_accounts(user, db, req.scope)
    if not accounts:
        return {"ok": True, "msg": "该范围内没有账号", "results": []}

    operator = getattr(user, "username", None) or user.id[:8]

    # ★ 先封盘再平仓：顺序很重要。
    #   若先平后封，两个动作之间那几秒钟主循环可能刚好开出新仓，
    #   于是"平完发现还有单"——先封住入口再清场，才不会边扫边漏。
    if req.halt_after:
        emergency.halt(
            emergency.LEVEL_HALT_NEW, scope=req.scope,
            reason=f"一键全平自动封盘：{req.reason or '未填写原因'}",
            by=operator,
        )

    logger.warning(
        f"[紧急处置] ★★ 一键全平启动 | 操作人={operator} | 范围={req.scope} "
        f"| 涉及账号={len(accounts)} | 原因={req.reason or '未填写'}"
    )

    results = [_flatten_one(a.id) for a in accounts]

    total_closed = sum(r["closed_count"] for r in results)
    total_remaining = sum(r["remaining_count"] for r in results)
    all_flat = all(r["fully_flat"] for r in results)

    req_id = emergency.record_flatten(
        scope=req.scope, reason=req.reason, by=operator,
        result={
            "accounts": len(accounts),
            "closed": total_closed,
            "remaining": total_remaining,
            "fully_flat": all_flat,
        },
    )

    if not all_flat:
        logger.error(
            f"[紧急处置] ★★ 一键全平未完全清空！残留 {total_remaining} 笔，"
            f"请立即人工到 MT5 终端确认（request={req_id}）"
        )

    return {
        "ok": True,
        "request_id": req_id,
        "fully_flat": all_flat,
        "total_closed": total_closed,
        "total_remaining": total_remaining,
        "halted": req.halt_after,
        "results": results,
        "warning": None if all_flat else "存在未平掉的持仓，请立即人工到 MT5 终端核实",
        "state": emergency.summary(),
    }


@router.post("/flatten")
async def flatten_all(req: FlattenRequest, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """一键全平：立刻平掉目标范围内的全部持仓。

    ★ 默认平完自动封盘（halt_after=True），这不是多此一举：
      不封盘的话，主循环下一轮（30~120 秒后）AI 照样会开新仓，
      人辛苦平掉的仓位几分钟内就回来了——那这个按钮等于没按。
      要单纯清仓而不封盘，需显式传 halt_after=false。

    执行路径直连 MT5，不经过 AI、不经过决策链——紧急场景下速度优先。

    2026-08-09：MT5 平仓操作可能阻塞，改为 async + to_thread offload。
    """
    import asyncio
    # ★★ 2026-08-10 修复：_flatten_all_sync 已是同步函数，to_thread 正确 offload。
    #   下方曾有一段 `return` 之后的死代码（_owned_accounts 二次查询），永远执行不到，
    #   已删除，避免误导。
    return await asyncio.to_thread(_flatten_all_sync, req, user, db)

    total_closed = sum(r["closed_count"] for r in results)
    total_remaining = sum(r["remaining_count"] for r in results)
    all_flat = all(r["fully_flat"] for r in results)

    req_id = emergency.record_flatten(
        scope=req.scope, reason=req.reason, by=operator,
        result={
            "accounts": len(accounts),
            "closed": total_closed,
            "remaining": total_remaining,
            "fully_flat": all_flat,
        },
    )

    if not all_flat:
        logger.error(
            f"[紧急处置] ★★ 一键全平未完全清空！残留 {total_remaining} 笔，"
            f"请立即人工到 MT5 终端确认（request={req_id}）"
        )

    return {
        "ok": True,
        "request_id": req_id,
        "fully_flat": all_flat,
        "total_closed": total_closed,
        "total_remaining": total_remaining,
        "halted": req.halt_after,
        "results": results,
        "warning": None if all_flat else "存在未平掉的持仓，请立即人工到 MT5 终端核实",
        "state": emergency.summary(),
    }


@router.get("/history")
def flatten_history(limit: int = 20, user: User = Depends(get_current_user)):
    """一键全平的历史留痕（事后复盘用）。"""
    return {"ok": True, "data": emergency.get_flatten_history(limit)}
