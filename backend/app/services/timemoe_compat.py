"""
Time-MoE × transformers 5.14 兼容层（2026-08-17 · 模型科学规划）
================================================================
transformers 5.14 移除了 DynamicCache.seen_tokens（改用 get_seq_length()），
且 GenerationMixin 新增 _extract_past_from_model_output 调用。Time-MoE
自带的 ts_generation_mixin.py 基于旧 API，直接 generate 会崩。

本模块通过 monkey-patch 注入兼容 shim，让 Time-MoE 能在 transformers 5.14
下跑通推理（不影响生产其他模块；补丁仅在本模块 import 时生效）。

用法：
    import timemoe_compat  # 先注入补丁
    from transformers import AutoConfig, AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(...)
"""
import logging

logger = logging.getLogger("timemoe_compat")

_PATCHED = False


def _patch_seen_tokens():
    """给 DynamicCache 补 seen_tokens 属性（5.14 移除，旧代码仍引用）。"""
    from transformers import DynamicCache

    if not hasattr(DynamicCache, "seen_tokens"):
        def seen_tokens(self):
            try:
                return self.get_seq_length()
            except Exception:  # noqa: BLE001
                return 0

        DynamicCache.seen_tokens = property(seen_tokens)
        logger.info("[Time-MoE兼容] 已补 DynamicCache.seen_tokens → get_seq_length()")


def _patch_extract_past():
    """给 Time-MoE 模型类补 _extract_past_from_model_output（5.14 新增调用）。"""
    import sys
    import os

    # 加载 Time-MoE 模块（trust_remote_code 已缓存到 huggingface transformers_modules）
    try:
        import importlib
        # 直接找已注册的类
        from transformers import AutoConfig
        # 动态加载 modeling 模块
        model_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "models", "timemoe-200m")
        sys.path.insert(0, model_dir)
        from modeling_time_moe import TimeMoeForPrediction

        if not hasattr(TimeMoeForPrediction, "_extract_past_from_model_output"):
            def _extract_past_from_model_output(self, outputs, standardize_cache_format=True):
                # 兼容：Time-MoE 的 greedy 循环从 outputs 取 past_key_values
                past = getattr(outputs, "past_key_values", None)
                if past is None:
                    past = outputs[1] if isinstance(outputs, (tuple, list)) and len(outputs) > 1 else None
                return past

            TimeMoeForPrediction._extract_past_from_model_output = _extract_past_from_model_output
            logger.info("[Time-MoE兼容] 已补 TimeMoeForPrediction._extract_past_from_model_output")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Time-MoE兼容] _extract_past 补丁失败: {e}")


def ensure_patch():
    global _PATCHED
    if _PATCHED:
        return
    try:
        _patch_seen_tokens()
        _patch_extract_past()
        _PATCHED = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Time-MoE兼容] 补丁未完全生效: {e}")


# ★ 2026-08-17 兼容别名：ts_reference_models.TimeMoEP.ready() 引用的是
#   apply_timemoe_compat（历史命名），本模块此前只有 ensure_patch →
#   import 直接失败 → Time-MoE 在参考面板永远"不可用"。补别名对齐。
def apply_timemoe_compat():
    ensure_patch()


ensure_patch()
