"""模型喂数真实性回归测试。

覆盖 2026-08-13 审计发现的两处「模型没看到真实行情」根因：
1. 本地 qwen3 副驾 prompt 取错键 → 盲猜（local_llm_service._build_copilot_prompt）
2. 外部宏观 DXY/VIX 日级滞后却无标注 → 云模型当实时（market_analyzer._fetch_external_data）
"""
import sys
import types
import unittest

sys.path.insert(0, ".")


class TestCopilotSeesRealMarketData(unittest.TestCase):
    """副驾必须能从 market_data 正确取到嵌套在 timeframes[H1] 的指标。"""

    def _make_md(self):
        return {
            "current_price": {"bid": 4408.1, "ask": 4408.3, "last": 4408.2},
            "timeframes": {
                "H1": {"atr": 12.5, "rsi": 58.3, "trend": "up",
                        "ema20": 4410.0, "ema50": 4402.0},
                "M15": {"atr": 9.0, "rsi": 55.0, "trend": "up",
                         "ema20": 4409.0, "ema50": 4405.0},
            },
            "smc_features": {"global_bias": "bullish", "per_tf": {"H1": "扶手"}},
            "regime": {"label_zh": "震荡偏多", "regime": "range_up"},
        }

    def test_indicators_pulled_from_nested_timeframes(self):
        from app.services.local_llm_service import LocalLLMService
        p = LocalLLMService._build_copilot_prompt(self._make_md())
        self.assertIn("ATR(H1): 12.5", p, "ATR 必须从 timeframes[H1].atr 取到，不能 N/A")
        self.assertIn("RSI(H1): 58.3", p)
        self.assertIn("趋势(H1): up", p)
        self.assertIn("EMA快/慢(H1): 4410.0 / 4402.0", p)

    def test_current_price_is_scalar_not_dict(self):
        from app.services.local_llm_service import LocalLLMService
        p = LocalLLMService._build_copilot_prompt(self._make_md())
        # 旧的 bug：current_price 是 dict 被直接塞进 prompt
        self.assertIn("当前价格: 4408.2", p)
        self.assertNotIn("'bid'", p, "current_price 不应是 dict 对象")

    def test_no_na_blind_guess(self):
        from app.services.local_llm_service import LocalLLMService
        p = LocalLLMService._build_copilot_prompt(self._make_md())
        # H1 有值的指标不应出现 N/A
        self.assertNotIn("ATR(H1): N/A", p)
        self.assertNotIn("RSI(H1): N/A", p)
        self.assertNotIn("趋势(H1): N/A", p)


class TestExternalMacroStalenessLabeled(unittest.TestCase):
    """external 必须标注「日级滞后」，云模型不能把昨天当现在。"""

    def test_data_lag_note_injected(self):
        from unittest import mock
        import app.core.market_analyzer as ma

        fake_ext = {"dxy": {"price": 102.3}, "vix": {"price": 18.5},
                    "correlation": -0.65}
        with mock.patch.object(ma, "market_data_provider") as mp:
            mp.get_external_snapshot.return_value = fake_ext
            analyzer = ma.MarketAnalyzer()
            out = analyzer._fetch_external_data()
        self.assertIn("data_lag_note", out, "必须注入日级滞后标注")
        self.assertEqual(out.get("granularity"), "daily")
        self.assertIn("日级滞后", out["data_lag_note"])

    def test_label_survives_on_error_dict(self):
        from unittest import mock
        import app.core.market_analyzer as ma

        with mock.patch.object(ma, "market_data_provider") as mp:
            mp.get_external_snapshot.side_effect = RuntimeError("boom")
            analyzer = ma.MarketAnalyzer()
            out = analyzer._fetch_external_data()
        # 异常分支也应有标注，且结构完整
        self.assertIn("data_lag_note", out)
        self.assertIn("dxy", out)


if __name__ == "__main__":
    unittest.main()
