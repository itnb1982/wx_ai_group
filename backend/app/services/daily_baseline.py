"""
万象AI — 每日开盘余额基线服务

用途：计算「今日盈利 = 当前余额 − 今日开盘余额」（余额日变额法），
      与 MT5 终端「今日盈亏」口径完全一致，彻底摆脱历史成交(Deals)窗口 /
      时区误差带来的客户可见错误。

设计要点：
  - 数据源 = mt5_service.get_account_info().balance（与账户管理页同源，已验证可靠，
    终端授权、实时、不依赖 Deals/Orders 历史）。
  - 基线（某账号某日的开盘余额）仅在「当日首次调用」时，用一次 Deals 反推建立：
        今日开盘余额 = 当前余额 − 今日已实现盈亏(Deals)
    建立后，当日后续全部用余额日变额（权威、零误差），不再碰 Deals 窗口。
  - 基线持久化到 JSON 文件，进程重启 / 跨午夜均不丢、不重算。
  - 跨日自动重建：次日首次调用检测到日期变化，重新用 Deals 反推当日开盘余额。
"""
import json
import os
import threading
from datetime import date, timedelta

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "daily_baseline.json")
_lock = threading.Lock()
_cache = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    return _cache


def get(account_id: str):
    """返回 {'date': 'YYYY-MM-DD', 'balance': float} 或 None"""
    return _load().get(str(account_id))


def set(account_id: str, date_str: str, balance: float):
    d = _load()
    d[str(account_id)] = {"date": date_str, "balance": round(float(balance), 2)}
    with _lock:
        try:
            with open(_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception:
            pass


def today_profit(account_id: str, server_today: date, current_balance: float,
                 realized_today_via_deals: float) -> float:
    """
    计算某账号「今日盈利」（余额日变额法）。

    参数：
      account_id            账号 ID
      server_today          服务器时区的「今日」date
      current_balance       实时余额（get_account_info，权威）
      realized_today_via_deals  用 Deals 反推的今日已实现盈亏（仅用于首日/跨日建基线）

    返回：今日盈利（四舍五入 2 位）
    """
    today_str = server_today.isoformat()
    bl = get(account_id)
    if bl and bl.get("date") == today_str:
        # 基线已建立 → 直接用余额日变额（权威，零窗口误差）
        return round(current_balance - float(bl["balance"]), 2)
    # 当日首次调用 / 刚部署 / 跨日 → 用 Deals 反推今日开盘余额作为基线
    open_balance = current_balance - float(realized_today_via_deals)
    set(account_id, today_str, open_balance)
    return round(float(realized_today_via_deals), 2)
