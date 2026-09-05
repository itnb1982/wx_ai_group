"""
万象AI — 市场时钟服务（当前盘口 + 休市倒计时）

权威时区：STARTRADER (XM 集团)，GMT+2(冬) / GMT+3(夏, 美国DST)
时区权威来源（双保险）：
  1. 优先：MT5 主号 Worker 真实服务器墙钟时间 + 品种交易时段
  2. 兜底：UTC + 服务器偏移 + 静态盘口模型

盘口定义（GMT+3 基准）：
  - 亚盘：04:00 ~ 13:00（亚洲时段）
  - 欧盘：13:00 ~ 20:00（伦敦时段）
  - 美盘：20:00 ~ 次日 00:00（纽约时段，含跨日）
  - 凌晨：00:00 ~ 04:00（视为美盘延伸/XAU 流动性低谷，标"夜盘收尾"）
  - 周末休市：周六 00:00 ~ 周日 24:00 + 周一 00:00 ~ 04:00
"""
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.mt5_account import MT5Account
from app.services.mt5_service import mt5_service
from loguru import logger


# ── 静态盘口模型（GMT+3 分钟数；Python weekday: 0=周一..6=周日）──
# 1=亚盘 2=欧盘 3=美盘/夜盘 0=休市
_PHASE_MAP = {
    0: [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
    # 周一: 00:00-04:00 休市(周末延伸), 04:00-13:00 亚, 13:00-20:00 欧, 20:00-24:00 美
    1: [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
    2: [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
    3: [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3],
    4: [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 2, 0, 0, 0, 0, 0, 0, 0],
    # 周五: 22:00-24:00 归 0 (休市过渡)
    5: [0] * 1440,  # 周六全天休市
    6: [0] * 1440,  # 周日全天休市
}
# GMT+3 04:00 = minute 240; 13:00 = 780; 20:00 = 1200; 24:00 = 1440
def _fill_phase_map():
    """XAUUSD 在 STARTRADER/XM 真实交易时段：周一 01:05 ~ 周五 23:55 连续
    盘口分（GMT+3 基准）：
      01:00-13:00 亚盘
      13:00-20:00 欧盘
      20:00-24:00 美盘
    周末/凌晨 00:00-01:00 归休市
    """
    out = {0: [], 1: [], 2: [], 3: [], 4: [], 5: [], 6: []}
    for wd in (0, 1, 2, 3, 4):
        m = []
        m += [0] * 60                       # 00:00-01:00 凌晨休市
        m += [1] * 720                      # 01:00-13:00 亚盘
        m += [2] * 420                      # 13:00-20:00 欧盘
        m += [3] * 240                      # 20:00-24:00 美盘
        out[wd] = m
    out[5] = [0] * 1440  # 周六全天休市
    out[6] = [0] * 1440  # 周日全天休市
    return out


_PHASE_MAP = _fill_phase_map()

_PHASE_LABEL = {
    0: "休市",
    1: "亚盘",
    2: "欧盘",
    3: "美盘",
}
_PHASE_FULL = {
    0: "周末休市",
    1: "亚盘正常交易中",
    2: "欧盘正常交易中",
    3: "美盘正常交易中",
}

_BROKER = "STARTRADER (XM 集团)"


def _market_primary_id(user_id: Optional[str] = None) -> str:
    """行情主号：统一走 primary_selector（尊重 Worker 真实存活 + 多租户 user_id），
    不再裸查 is_market_primary 字段（旧逻辑会抓到已死主号导致行情静默退化）。
    user_id 为空时做全局查询（仅供无用户上下文的后台线程）。
    """
    db: Session = SessionLocal()
    try:
        from app.services.primary_selector import pick_market_primary_id as _pick
        return _pick(db, user_id)
    except Exception as e:
        logger.warning(f"[MarketSession] 查主号失败: {e}")
        return ""
    finally:
        db.close()


def _server_offset(dt_utc: datetime) -> int:
    """XM/STARTRADER 偏移：冬令+2 / 夏令+3（美国 DST 规则）"""
    year = dt_utc.year
    def nth_weekday(month, weekday, n):
        from datetime import date
        d = date(year, month, 1)
        off = (weekday - d.weekday()) % 7
        return d + timedelta(days=off + 7 * (n - 1))
    start = nth_weekday(3, 6, 2)  # 3月第2周日
    end = nth_weekday(11, 6, 1)   # 11月第1周日
    s = datetime(year, start.month, start.day, 2, 0)
    e = datetime(year, end.month, end.day, 2, 0)
    return 3 if s <= dt_utc < e else 2


def _static_server_now() -> Tuple[datetime, int]:
    utc_now = datetime.utcnow()
    off = _server_offset(utc_now)
    server_dt = (utc_now + timedelta(hours=off))
    return server_dt, off


def _phase_at(server_dt: datetime) -> int:
    """返回盘口代号：0=休市 1=亚 2=欧 3=美"""
    wd = server_dt.weekday()
    mins = server_dt.hour * 60 + server_dt.minute
    if mins < 0 or mins >= 1440:
        return 0
    return _PHASE_MAP[wd][mins]


def _week_open_moment(server_dt: datetime) -> Optional[datetime]:
    """本周一 01:00（GMT+3）开盘时刻（XAUUSD 实际 01:05，标 01:00）"""
    wd = server_dt.weekday()
    days_to_mon = (0 - wd) % 7
    this_mon_1am = server_dt.replace(hour=1, minute=0, second=0, microsecond=0) - timedelta(days=wd)
    if server_dt < this_mon_1am:
        this_mon_1am = this_mon_1am - timedelta(days=7)
    return this_mon_1am


def _next_open_moment(server_dt: datetime) -> Optional[datetime]:
    """下一个 01:00（GMT+3）开盘时刻（XAUUSD 真实 01:05 开盘，简化为整点）"""
    wd = server_dt.weekday()
    today_1am = server_dt.replace(hour=1, minute=0, second=0, microsecond=0)
    # 当前还没到今天 01:00
    if server_dt < today_1am:
        if wd in (5, 6):  # 周六/周日：跳到下周一 01:00
            days_to_mon = (7 - wd) % 7
            return today_1am + timedelta(days=days_to_mon)
        return today_1am
    # 已过今天 01:00
    if wd == 4 and server_dt.hour >= 22:  # 周五晚 22:00 后：下周一 01:00
        return today_1am + timedelta(days=3)
    if wd in (5, 6):  # 周末：下周一 01:00
        days_to_mon = (7 - wd) % 7
        return today_1am + timedelta(days=days_to_mon)
    return today_1am + timedelta(days=1)


def get_session_state() -> Dict[str, Any]:
    """
    返回市场时钟状态。
    优先使用 MT5 主号真实服务器时间；失败用静态 GMT+3 兜底。
    返回字段：
      - server_time / timezone / broker
      - phase_code (0/1/2/3), phase_label (亚盘/欧盘/美盘/休市), phase_full (亚盘正常交易中/...)
      - is_open (bool)
      - open_since (本周一开盘时刻 ISO, 休市时为 None)
      - open_since_min (整数分钟, 便于前端展示)
      - countdown_to_open (距下个开盘秒数, 休市时)
      - countdown_to_close (距今日美盘收尾秒数, 交易中)
      - source (mt5 / static)
    """
    primary_id = _market_primary_id()
    server_dt: Optional[datetime] = None
    offset: Optional[int] = None
    source = "static"

    if primary_id:
        info = mt5_service.get_server_info(primary_id)
        # ★ 2026-08-15 防御：双保险 isinstance(dict)（源头 get_server_info 已保证 dict，
        #   这里防未来改动/异常路径回退非 dict 形态）
        if isinstance(info, dict) and "error" not in info and info.get("server_time"):
            try:
                server_dt = datetime.fromisoformat(info["server_time"])
                offset = info.get("server_offset")
                if offset is None:
                    offset = _server_offset(datetime.utcnow())
                source = "mt5"
            except Exception as e:
                logger.warning(f"[MarketSession] 解析主号时间失败: {e}")

    if server_dt is None:
        server_dt, offset = _static_server_now()
        source = "static"

    phase = _phase_at(server_dt)
    is_open = (phase != 0)

    # 距今交易分钟数（本周开盘至今）
    open_since_iso = None
    open_since_sec = 0
    if is_open:
        week_open = _week_open_moment(server_dt)
        if week_open and week_open <= server_dt:
            open_since_sec = int((server_dt - week_open).total_seconds())
            open_since_iso = week_open.isoformat()

    # 距下个开盘秒数（休市时）
    countdown_to_open_sec = 0
    if not is_open:
        nxt = _next_open_moment(server_dt)
        if nxt:
            countdown_to_open_sec = max(0, int((nxt - server_dt).total_seconds()))

    # 距今日美盘收尾秒数（交易中，22:00 GMT+3 算收尾）
    countdown_to_close_sec = 0
    if is_open:
        today_close = server_dt.replace(hour=22, minute=0, second=0, microsecond=0)
        if server_dt < today_close:
            countdown_to_close_sec = int((today_close - server_dt).total_seconds())
        else:
            # 美盘已开过 22:00 → 距明天 01:00 凌晨收尾
            tomorrow_1am = server_dt.replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(days=1)
            countdown_to_close_sec = int((tomorrow_1am - server_dt).total_seconds())

    tz_label = f"GMT+{offset}" + (" (夏令时/DST)" if offset == 3 else " (冬令时)")

    return {
        "server_time": server_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": tz_label,
        "broker": _BROKER,
        "is_open": is_open,
        "phase_code": phase,
        "phase_label": _PHASE_LABEL[phase],
        "phase_full": _PHASE_FULL[phase],
        "open_since": open_since_iso,
        "open_since_sec": open_since_sec,
        "countdown_to_open_sec": countdown_to_open_sec,
        "countdown_to_close_sec": countdown_to_close_sec,
        "source": source,
    }


def get_session_state_fast() -> Dict[str, Any]:
    """
    快速版市场时钟：跳过 MT5 IPC 调用，纯静态 GMT+3 计算。
    用于 _auto_loop 等后台线程，避免 MT5 Worker 未就绪时卡死。
    返回字段与 get_session_state() 完全一致（source 恒为 "static"）。
    """
    server_dt, offset = _static_server_now()
    source = "static"

    phase = _phase_at(server_dt)
    is_open = (phase != 0)

    open_since_iso = None
    open_since_sec = 0
    if is_open:
        week_open = _week_open_moment(server_dt)
        if week_open and week_open <= server_dt:
            open_since_sec = int((server_dt - week_open).total_seconds())
            open_since_iso = week_open.isoformat()

    countdown_to_open_sec = 0
    if not is_open:
        nxt = _next_open_moment(server_dt)
        if nxt:
            countdown_to_open_sec = max(0, int((nxt - server_dt).total_seconds()))

    countdown_to_close_sec = 0
    if is_open:
        today_close = server_dt.replace(hour=22, minute=0, second=0, microsecond=0)
        if server_dt < today_close:
            countdown_to_close_sec = int((today_close - server_dt).total_seconds())
        else:
            tomorrow_1am = server_dt.replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(days=1)
            countdown_to_close_sec = int((tomorrow_1am - server_dt).total_seconds())

    tz_label = f"GMT+{offset}" + (" (夏令时/DST)" if offset == 3 else " (冬令时)")

    return {
        "server_time": server_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": tz_label,
        "broker": _BROKER,
        "is_open": is_open,
        "phase_code": phase,
        "phase_label": _PHASE_LABEL[phase],
        "phase_full": _PHASE_FULL[phase],
        "open_since": open_since_iso,
        "open_since_sec": open_since_sec,
        "countdown_to_open_sec": countdown_to_open_sec,
        "countdown_to_close_sec": countdown_to_close_sec,
        "source": source,
    }


def local_to_server(dt_local: datetime) -> datetime:
    """
    把本机本地时间换算成服务器墙钟时间（用于「今日」日切口径统一）。
    本机通常在中国 GMT+8；服务器 GMT+2/+3，差 5~6 小时。
    """
    utc_now = datetime.utcnow()
    off = _server_offset(utc_now)
    local_off = -__import__("time").timezone // 3600
    utc_dt = dt_local - timedelta(hours=local_off)
    return utc_dt + timedelta(hours=off)
