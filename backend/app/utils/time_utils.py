"""
时区统一收口（2026-08-10 引入，2026-08-15 第三批#5 收口到本模块）

★ 背景：SQLite + SQLAlchemy 存 tz-aware datetime 会丢 tz，读出仍是 naive datetime
  → 直接 .isoformat() 无 +00:00 → 前端按本地(UTC+8) 解析后少显示 8h。

★ 契约：本模块是唯一权威的「datetime → UTC ISO 字符串」出口。
  - 入参 None → 返回 None（不伪装成 "1970..."）；
  - 入参 naive → 视作 UTC 并补 tz；
  - 入参已带 tz → 保留原 tz（不二次偏移）；
  - 返回固定 ISO 字符串（带 +00:00），前端可直接 new Date() 解析。

  任何把 datetime 序列化给前端的点都必须走这里，禁止再手写 .isoformat()，
  否则会重新引入 8h 偏移。这是 #5「统一 SQLite 时区」的核心落点。
"""
from __future__ import annotations

from datetime import datetime, timezone


def _to_utc_iso(dt):
    """把（可能 naive 的）datetime 规范为带 UTC 时区的 ISO 字符串。

    与 dashboard.py 的 datetime.now(timezone.utc) 写入路径配套闭环：
    写入时存 UTC，读出时补 UTC，前端解析一致。
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        # 已经是字符串（如 JSON 里取出的），原样返回，避免重复解析出错
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
