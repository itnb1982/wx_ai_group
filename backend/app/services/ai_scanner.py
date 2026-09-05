"""
AI 信号扫描器 — 后台周期任务
让「交易执行流」面板 7×24 持续产生真实活动：
  - 每轮拉取 XAUUSD 多周期行情快照
  - 用轻量趋势共振规则判定方向（多周期同向 → 潜在信号）
  - 写入 AIActivity 事件（scan / signal）
真实发生，不造假。营销上体现"AI 每秒都在替客户盯盘"。
"""
import threading
import time
from datetime import datetime
from loguru import logger

from app.database import SessionLocal
from app.models.ai_activity import AIActivity
from app.models.evolution_log import EvolutionLog
from app.models.mt5_account import MT5Account
from app.models.user import User
from app.services.mt5_service import mt5_service
from app.core.market_analyzer import MarketAnalyzer
from app.services.ai_memory import push_activity, push_evolution

_scanner_thread = None
_scanner_stop = False

_TREND_ARROW = {
    "strong_uptrend": "↑",
    "uptrend": "↑",
    "strong_downtrend": "↓",
    "downtrend": "↓",
    "ranging": "→",
    "sideways": "→",
    "unknown": "·",
}


def _get_primary() -> tuple:
    """取行情主号 (user_id, account_id)"""
    db = SessionLocal()
    try:
        # ★ 2026-08-09：统一走 primary_selector（含"主号掉线自动换活账号"降级）。
        #   扫描器是后台线程、无用户上下文，这里保持全局查询语义。
        from app.services.primary_selector import pick_market_primary

        acc = pick_market_primary(db, None)
        if acc:
            return acc.user_id, acc.id
    except Exception:
        pass
    finally:
        db.close()
    return None, None


def _single_scan() -> None:
    """单轮扫描：拉快照 → 判定 → 写 AIActivity"""
    from app.database import WriteSession
    db = WriteSession()
    try:
        user_id, account_id = _get_primary()
        if not user_id:
            return

        analyzer = MarketAnalyzer(mt5_service=mt5_service, market_primary_id=account_id or "")
        snap = analyzer.get_market_snapshot()
        if "error" in snap:
            return

        tfs = snap.get("timeframes", {})
        parsed = []
        for tf in ("M15", "H1", "H4"):
            d = tfs.get(tf, {})
            if "error" in d:
                parsed.append((tf, "·"))
                continue
            t = d.get("trend", "unknown")
            parsed.append((tf, _TREND_ARROW.get(t, "·")))

        bull = sum(1 for _, a in parsed if a == "↑")
        bear = sum(1 for _, a in parsed if a == "↓")

        bar = " ".join(f"{tf}{a}" for tf, a in parsed)

        if bull >= 2 and bull > bear:
            direction = "BUY"
            kind = "signal"
            detail = f"AI 扫描 XAUUSD · {bar} | 多头多周期共振，潜在做多信号（强度 {int(bull / 3 * 100)}%）"
            confidence = bull / 3
        elif bear >= 2 and bear > bull:
            direction = "SELL"
            kind = "signal"
            detail = f"AI 扫描 XAUUSD · {bar} | 空头多周期共振，潜在做空信号（强度 {int(bear / 3 * 100)}%）"
            confidence = bear / 3
        else:
            direction = "HOLD"
            kind = "scan"
            detail = f"AI 扫描 XAUUSD · {bar} | 多周期分歧，继续观望等待入场时机"
            confidence = 0.0

        act = AIActivity(
            user_id=user_id,
            mt5_account_id=account_id,
            kind=kind,
            symbol="XAUUSD",
            timeframe="M15/H1/H4",
            direction=direction,
            confidence=round(confidence, 3),
            detail=detail,
            meta_json=f'{{"bar":"{bar}"}}',
        )
        # 实时流保底：先推内存（永远可写），再尽力落库（Defender 锁库时自动跳过）
        push_activity({
            "kind": kind,
            "symbol": "XAUUSD",
            "timeframe": "M15/H1/H4",
            "direction": direction,
            "confidence": round(confidence, 3),
            "detail": detail,
            "meta_json": f'{{"bar":"{bar}"}}',
        })
        db.add(act)
        db.commit()
    except Exception as e:  # noqa: BLE001
        # DB 只读(Defender 锁库)属预期的 best-effort 降级：内存缓冲已承载实时流，
        # 此处降级为 DEBUG 避免刷屏；其它异常仍 WARNING。
        if "readonly database" in str(e):
            logger.debug(f"[扫描器] DB只读，跳过落库(内存流已保底): {e}")
        else:
            logger.warning(f"[扫描器] 单轮扫描异常: {e}")
    finally:
        db.close()


def write_init_evolution() -> None:
    """启动时写一条进化初始化事件，让进化时间线启动即有内容。

    注意：先无条件推入内存缓冲（永远可写），再尽力落库（DB 被 Defender 锁时自动跳过）。
    """
    _init_event = {
        "kind": "init",
        "subject": "MetaAgent",
        "before": "",
        "after": "载入",
        "delta": "",
        "reason": "双模型 AI 引擎初始化：DeepSeek V4（激进派）+ 混元 Hy3（稳健派）载入，初始权重 DS/HY = 0.50 / 0.50，开始 7×24 自进化",
        "label": "引擎初始化",
    }
    # 内存保底：无论 DB 是否可写，进化时间线都先有初始事件
    push_evolution(_init_event)

    from app.database import WriteSession
    db = WriteSession()
    try:
        user = db.query(User).first()
        if not user:
            return
        # 幂等：已写过则不重复落库
        existing = db.query(EvolutionLog).filter(EvolutionLog.kind == "init").first()
        if existing:
            return
        evo = EvolutionLog(
            user_id=user.id,
            kind="init",
            subject="MetaAgent",
            before_value="",
            after_value="载入",
            delta="",
            reason=_init_event["reason"],
            meta_json="{}",
        )
        db.add(evo)
        db.commit()
        logger.info("[进化] 写入初始化事件")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[进化] 初始化事件落库失败(已写入内存): {e}")
    finally:
        db.close()


def scanner_loop(interval: int = 8) -> None:
    global _scanner_stop
    while not _scanner_stop:
        try:
            _single_scan()
        except Exception:  # noqa: BLE001
            pass
        # 分段 sleep，便于快速退出
        for _ in range(interval):
            if _scanner_stop:
                break
            time.sleep(1)


def start_scanner(interval: int = 8) -> None:
    global _scanner_thread
    if _scanner_thread and _scanner_thread.is_alive():
        logger.info("[扫描器] 已在运行")
        return
    _scanner_stop = False
    _scanner_thread = threading.Thread(target=scanner_loop, args=(interval,), daemon=True)
    _scanner_thread.start()
    logger.info(f"[扫描器] 已启动，间隔 {interval}s")


def stop_scanner() -> None:
    global _scanner_stop
    _scanner_stop = True
