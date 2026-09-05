"""AI 决策核心包。

★ 为什么这里是惰性导出而不是直接 `from .x import Y`：

原写法在包初始化时就 eager import 全部子模块，形成了这样一条死环：
    app.core.__init__
      → deepseek_client
        → app.services.key_pool
          → app.services.__init__
            → trade_executor
              → app.core.debate_engine
                → app.core.deepseek_client  ← 此时仍在初始化中，ImportError

后果是：**谁先被导入决定了能不能导入成功**。从 app.services.* 进来碰巧能过，
从 app.core.* 进来就崩。新增任何 core 模块（如 execution_controller）都会
踩中，这不是"小心一点"能规避的，是包结构问题。

改为 PEP 562 模块级 __getattr__ 后：
    · `from app.core import DebateEngine` 等既有写法完全不变，照常可用
    · 但只有真正取属性时才加载对应子模块，包初始化阶段零依赖，环被打断
    · 直接 `from app.core.execution_controller import X` 不再拖起整条 AI 链路，
      单测启动也快得多
"""
from importlib import import_module
from typing import TYPE_CHECKING

# 导出名 → 所在子模块
_EXPORTS = {
    "DeepSeekClient": ".deepseek_client",
    "HunyuanClient": ".hunyuan_client",
    "MarketAnalyzer": ".market_analyzer",
    "MetaAgent": ".meta_agent",
    "DebateDecision": ".meta_agent",
    "DebateEngine": ".debate_engine",
}


def __getattr__(name: str):
    """按需加载导出符号（PEP 562）。"""
    mod_path = _EXPORTS.get(name)
    if mod_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(mod_path, __name__), name)


def __dir__():
    return sorted(set(list(globals().keys()) + list(_EXPORTS.keys())))


# 仅供类型检查器/IDE 静态解析，运行时不执行（不会触发循环导入）
if TYPE_CHECKING:  # pragma: no cover
    from .debate_engine import DebateEngine  # noqa: F401
    from .deepseek_client import DeepSeekClient  # noqa: F401
    from .hunyuan_client import HunyuanClient  # noqa: F401
    from .market_analyzer import MarketAnalyzer  # noqa: F401
    from .meta_agent import DebateDecision, MetaAgent  # noqa: F401

__all__ = list(_EXPORTS.keys())
