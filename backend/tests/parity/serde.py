"""比对台序列化层：把真实调用的入参/返回值在「Python 对象」与「JSON」之间往返。

难点在于决策函数的入参不是纯数据：strategy 是 SQLAlchemy 模型实例，
ai_decision 是 DebateDecision 对象。直接 json.dumps 会炸。

做法：
  · 落盘时把对象摊平成 {"__obj__": 类名, "fields": {...}} 的纯数据；
  · 回放时统一还原成 SimpleNamespace。

为什么还原成 SimpleNamespace 而不是原类？
  被决策函数消费的对象一律通过 getattr(x, "字段", 默认值) 访问（这是本项目的
  既有约定，follow_leader 继承机制也依赖它）。SimpleNamespace 完全满足，
  且不需要 DB 会话、不会触发 SQLAlchemy 懒加载 —— 回放因此可以离线、毫秒级。
"""
from __future__ import annotations

import datetime as _dt
import math
from types import SimpleNamespace
from typing import Any

# 绝不落盘的字段（即便当前对象里没有，也先钉死，防日后新增字段泄漏）
_SECRET_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "encrypted_key", "private_key", "secret_key", "investor_password",
}


def _is_secret(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _SECRET_KEYS)


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """把任意对象转成可 json.dumps 的纯数据（脱敏、防环、限深）。"""
    if _depth > 12:
        return "<max-depth>"

    if obj is None or isinstance(obj, (bool, int, str)):
        return obj

    if isinstance(obj, float):
        # NaN / Inf 不是合法 JSON，落盘会变成非标准字面量，回放时解析出错
        if math.isnan(obj) or math.isinf(obj):
            return {"__float__": repr(obj)}
        return obj

    if isinstance(obj, (_dt.datetime, _dt.date)):
        return {"__datetime__": obj.isoformat()}

    if isinstance(obj, dict):
        return {
            str(k): ("<redacted>" if _is_secret(str(k)) else to_jsonable(v, _depth + 1))
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v, _depth + 1) for v in obj]

    # SQLAlchemy 模型实例：按表列摊平（不碰关系属性，避免懒加载连库）
    table = getattr(type(obj), "__table__", None)
    if table is not None:
        fields = {}
        for col in table.columns:
            name = col.name
            fields[name] = "<redacted>" if _is_secret(name) else to_jsonable(
                getattr(obj, name, None), _depth + 1
            )
        return {"__obj__": type(obj).__name__, "fields": fields}

    # 普通对象（DebateDecision / SimpleNamespace / dataclass 等）
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        fields = {
            k: ("<redacted>" if _is_secret(k) else to_jsonable(v, _depth + 1))
            for k, v in d.items()
            if not k.startswith("_")
        }
        return {"__obj__": type(obj).__name__, "fields": fields}

    return {"__repr__": repr(obj)}


def from_jsonable(data: Any) -> Any:
    """to_jsonable 的逆操作。对象一律还原为 SimpleNamespace。"""
    if isinstance(data, dict):
        if "__obj__" in data:
            fields = {k: from_jsonable(v) for k, v in (data.get("fields") or {}).items()}
            ns = SimpleNamespace(**fields)
            # 保留原始类名，便于比对报告里指认是哪种对象
            object.__setattr__(ns, "__parity_type__", data["__obj__"])
            return ns
        if "__datetime__" in data:
            return _dt.datetime.fromisoformat(data["__datetime__"])
        if "__float__" in data:
            return float(data["__float__"])
        if "__repr__" in data:
            return data["__repr__"]
        return {k: from_jsonable(v) for k, v in data.items()}

    if isinstance(data, list):
        return [from_jsonable(v) for v in data]

    return data
