"""
MT5 账号管理路由
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from loguru import logger

from app.database import get_db, safe_commit
from app.models.user import User
from app.models.mt5_account import MT5Account, AccountType, AccountStatus
from app.models.ai_activity import AIActivity
from app.models.evolution_log import EvolutionLog
from app.models.trade_exit import TradeExit
from app.utils.crypto import encrypt, decrypt
from app.services.mt5_service import mt5_service
from app.routers.auth import get_current_user

router = APIRouter(prefix="/api/accounts", tags=["MT5账号"])


class AddAccountRequest(BaseModel):
    name: str
    account_id: str
    password: str
    server: str
    terminal_path: str = ""  # MT5 terminal64.exe 绝对路径
    account_type: str = "demo"  # demo / real


class UpdateAccountRequest(BaseModel):
    name: str | None = None
    account_id: str | None = None
    password: str | None = None
    server: str | None = None
    terminal_path: str | None = None
    account_type: str | None = None


@router.get("/")
async def list_accounts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取当前用户的MT5账号列表（已连接账号自动返回实时余额/净值/利润）"""
    accounts = db.query(MT5Account).filter(MT5Account.user_id == user.id).all()

    # 并行拉取已连接账号的实时余额，避免单个 Worker 卡死拖慢整页
    async def _fetch_live(a):
        try:
            info = await asyncio.to_thread(mt5_service.get_account_info, a.id)
            if "error" not in info:
                return info.get("balance", a.balance), info.get("equity", a.equity), info.get("profit", a.profit)
        except Exception:
            pass
        return a.balance, a.equity, a.profit

    live_tasks = [(_fetch_live(a) if a.is_connected else None) for a in accounts]
    live_results = await asyncio.gather(*[t for t in live_tasks if t is not None])
    live_map = {}
    idx = 0
    for a in accounts:
        if a.is_connected:
            live_map[a.id] = live_results[idx]
            idx += 1

    out = []
    for a in accounts:
        balance, equity, profit = live_map.get(a.id, (a.balance, a.equity, a.profit))
        out.append({
            "id": a.id,
            "name": a.name,
            "account_id": a.account_id,
            "server": a.server,
            "terminal_path": a.terminal_path or "",
            "account_type": a.account_type.value if a.account_type else "demo",
            "status": a.status.value if a.status else "offline",
            "is_connected": a.is_connected,
            "is_market_primary": a.is_market_primary,
            "balance": balance,
            "equity": equity,
            "profit": profit,
            "is_trading_enabled": a.is_trading_enabled,
        })
    return out


@router.post("/")
async def add_account(req: AddAccountRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """添加 MT5 账号（2026-08-09 改为 async，DB 写操作 offload 避免事件循环阻塞）"""
    # B4：同用户重复 login 查重，避免加两份相同账号
    if db.query(MT5Account).filter(
        MT5Account.user_id == user.id,
        MT5Account.account_id == req.account_id,
    ).first():
        raise HTTPException(status_code=400, detail="该 MT5 账号已添加，请勿重复添加")

    # B1：若用户尚无行情主号，首个账号自动设为行情主号（保障主号始终有归属）
    has_primary = db.query(MT5Account).filter(
        MT5Account.user_id == user.id,
        MT5Account.is_market_primary == True,
    ).first()

    account = MT5Account(
        user_id=user.id,
        name=req.name,
        account_id=req.account_id,
        password=encrypt(req.password),
        server=req.server,
        terminal_path=req.terminal_path or "",
        account_type=AccountType.REAL if req.account_type == "real" else AccountType.DEMO,
        is_market_primary=not has_primary,
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    # ★ 2026-08-06 修复 P3「add_account 懒建策略行」：新增账号即建立 strategy_configs 行，
    #   user_id 与 mt5_accounts.user_id 严格对齐（铁律：策略行归属必须与账号一致，否则
    #   get_or_default_strategy 双过滤失效 → 独立风控/每账号参数全部回退默认）。
    #   彻底消除"新增账号无策略行"边界态，避免 follow_leader 等开关形同虚设。
    try:
        from app.models.strategy import StrategyConfig
        exists_row = db.query(StrategyConfig).filter(
            StrategyConfig.mt5_account_id == account.id
        ).first()
        if exists_row is None:
            strat = StrategyConfig(
                mt5_account_id=account.id,
                user_id=user.id,
                # 新增账号默认跟随主号（双套逻辑默认值），其余字段走模型默认
                follow_leader=True,
            )
            db.add(strat)
            db.commit()
            logger.info(
                f"[add_account] 已为账号 {account.account_id[:8]} 补建策略行 "
                f"(user_id={user.id[:8]}, follow_leader=True)"
            )
    except Exception as _se:
        logger.warning(f"[add_account] 补建策略行失败(忽略，GET会兜底): {_se}")
        try:
            db.rollback()
        except Exception:
            pass

    return {
        "id": account.id,
        "name": account.name,
        "account_id": account.account_id,
        "server": account.server,
        "terminal_path": account.terminal_path,
        "status": "offline",
        "connected": False,
        "is_market_primary": account.is_market_primary,
        "balance": 0,
        "equity": 0,
        "message": "账号已保存。请点击'连接'按钮测试 MT5 连通性",
    }


@router.delete("/{account_id}")
async def remove_account(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """移除 MT5 账号（级联清理策略/交易已 ORM cascade；此处补清活动/进化日志 + 主号转移）"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    was_primary = account.is_market_primary

    await asyncio.to_thread(mt5_service.remove_account, account_id)
    # B3：清理该账号残留的 AI 活动流 / 进化日志 / 平仓明细（这些表未配 ORM cascade）
    db.query(AIActivity).filter(AIActivity.mt5_account_id == account_id).delete(
        synchronize_session=False
    )
    db.query(EvolutionLog).filter(EvolutionLog.mt5_account_id == account_id).delete(
        synchronize_session=False
    )
    # ★ 2026-08-12 根治②补：trade_exits 与 trades 也无 ORM cascade，删除接口此前漏清，
    #   导致已删账号残留 701 行孤儿平仓明细，污染统计且无法再次删除时回填。
    #   现显式按 mt5_account_id 级联清理（先于 db.delete(account)，避免外键约束）。
    db.query(TradeExit).filter(TradeExit.mt5_account_id == account_id).delete(
        synchronize_session=False
    )
    db.delete(account)
    db.commit()

    # B2：若删除的是行情主号，自动将该用户下一个账号（优先已连接）提升为主号
    if was_primary:
        next_primary = db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
            MT5Account.is_connected == True,
        ).first() or db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
        ).first()
        if next_primary:
            next_primary.is_market_primary = True
            db.commit()

    return {"ok": True}


@router.put("/{account_id}")
async def update_account(account_id: str, req: UpdateAccountRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """修改 MT5 账号（名称/密码/服务器/终端路径等）（2026-08-09 改为 async）"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    changed = False
    if req.name is not None:
        account.name = req.name
        changed = True
    if req.account_id is not None:
        account.account_id = req.account_id
        changed = True
    if req.password is not None:
        account.password = encrypt(req.password)
        changed = True
    if req.server is not None:
        account.server = req.server
        changed = True
    if req.terminal_path is not None:
        account.terminal_path = req.terminal_path
        changed = True
    if req.account_type is not None:
        account.account_type = AccountType.REAL if req.account_type == "real" else AccountType.DEMO
        changed = True

    db.commit()

    return {
        "ok": True,
        "id": account.id,
        "message": "账号已更新。如修改了密码/服务器/终端路径，请点击'连接'按钮重新测试",
    }


@router.post("/{account_id}/connect")
async def connect_account(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """重新连接 MT5 账号（Worker 启动可能耗时 90s，offload 到线程池避免阻塞事件循环）"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    try:
        success = await asyncio.to_thread(
            mt5_service.add_account,
            account_id=account.id,
            login=account.account_id,
            password=decrypt(account.password),
            server=account.server,
            name=account.name,
            terminal_path=account.terminal_path or "",
        )
        account.is_connected = success
        account.status = AccountStatus.ONLINE if success else AccountStatus.ERROR
        if success:
            info = await asyncio.to_thread(mt5_service.get_account_info, account.id)
            if "error" not in info:
                account.balance = info.get("balance", 0)
                account.equity = info.get("equity", 0)
                account.profit = info.get("profit", 0)
                account.margin_level = info.get("margin_level", 0)
    except Exception as e:
        account.is_connected = False
        account.status = AccountStatus.ERROR
        account.status_message = str(e)[:200]
        success = False
    db.commit()
    return {"connected": success, "balance": account.balance, "equity": account.equity}


@router.post("/{account_id}/toggle-trading")
async def toggle_trading(account_id: str, enabled: bool = True, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """启用/禁用交易（2026-08-09 改为 async）"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    account.is_trading_enabled = enabled
    db.commit()
    return {"is_trading_enabled": enabled}


@router.post("/{account_id}/set-primary")
async def set_primary(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """设为信号主号（交易主号）：AI 在此账号决策并先下单，其他账号自动跟单。
    同一用户仅一个主号；设为该账号后，其余账号的 is_market_primary 自动清零。
    主号同时也是行情数据源（is_market_primary 复用）。（2026-08-09 改为 async）"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    # 先清掉同用户其他主号
    def _apply():
        db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
            MT5Account.is_market_primary == True,
        ).update({MT5Account.is_market_primary: False})
        account.is_market_primary = True

    try:
        # 健壮提交：消化吸收 Defender 间歇写锁，避免偶发 500 / 不落库
        safe_commit(db, apply=_apply)
    except Exception as _commit_err:
        raise HTTPException(status_code=500, detail=f"设为主号失败: {_commit_err}")
    return {"ok": True, "id": account.id, "is_market_primary": True}


@router.post("/{account_id}/sync")
async def sync_account(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """同步 MT5 实时余额到数据库"""
    account = db.query(MT5Account).filter(
        MT5Account.id == account_id, MT5Account.user_id == user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    info = await asyncio.to_thread(mt5_service.get_account_info, account_id)
    if "error" in info:
        account.is_connected = False
        account.status = AccountStatus.ERROR
        db.commit()
        return {"error": info["error"], "balance": account.balance}

    account.balance = info.get("balance", 0)
    account.equity = info.get("equity", 0)
    account.profit = info.get("profit", 0)
    account.margin_level = info.get("margin_level", 0)
    account.is_connected = True
    account.status = AccountStatus.ONLINE
    db.commit()
    return {
        "balance": account.balance,
        "equity": account.equity,
        "profit": account.profit,
        "margin_level": account.margin_level,
        "is_connected": True,
    }


@router.get("/{account_id}/positions")
async def get_positions(account_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取持仓（★ 2026-08-15 审计P0修复：必须先校验账号归属，杜绝跨租户越权读取）"""
    owned = db.query(MT5Account).filter(
        MT5Account.id == account_id,
        MT5Account.user_id == user.id,
    ).first()
    if owned is None:
        raise HTTPException(status_code=404, detail="账号不存在或无权访问")
    return await asyncio.to_thread(mt5_service.get_positions, account_id)


@router.get("/status")
async def get_all_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """获取当前用户所有账号连接状态（★ 审计P0修复：仅返回本用户账号集，不泄漏他人账号）"""
    owned_ids = {a.id for a in db.query(MT5Account.id).filter(MT5Account.user_id == user.id).all()}
    if not owned_ids:
        return []
    return await asyncio.to_thread(mt5_service.get_all_accounts_status, owned_ids)
