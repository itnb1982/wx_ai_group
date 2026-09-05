"""
Phase 6 降级车道 — 本地 LLM（Qwen3-8B via Ollama）测试
========================================================
重点验证「小模型能力边界」被工程手段守住：
  * 校对员只查不判（提示词不得诱导方向、返回结构不含 decision）
  * 副驾三道锁（有票 / 置信达标 / Chronos 同向）缺一不可
  * 探活负缓存（Ollama 没装是常态，不能每轮白等）
  * JSON 容错（小模型输出必然带杂质：think 块、代码围栏、后缀解释）
  * 全异常安全（任何失败返回 None，绝不上抛炸主链路）
"""
import json
import time

import pytest

import app.services.local_llm_service as llm
from app.services.local_llm_service import (
    COPILOT_MIN_CONFIDENCE,
    KEEP_ALIVE,
    NUM_CTX,
    CopilotVote,
    LocalLLMService,
    ProofreadResult,
    copilot_gate,
    get_local_llm,
    set_transport,
)

pytestmark = pytest.mark.unit


# ============================================================
#  伪传输层
# ============================================================
class FakeOllama:
    """可编程的假 Ollama。记录全部请求，便于断言 payload。"""

    def __init__(self, models=("qwen3:8b",), response="", status=200,
                 raise_on=None, tags_status=200):
        self.models = list(models)
        self.response = response
        self.status = status
        self.raise_on = raise_on  # "GET" / "POST" / None
        self.tags_status = tags_status
        self.calls = []

    def __call__(self, method, url, payload, timeout):
        self.calls.append({"method": method, "url": url,
                           "payload": payload, "timeout": timeout})
        if self.raise_on and method.upper() == self.raise_on:
            raise ConnectionError("connection refused")
        if url.endswith("/api/tags"):
            if self.tags_status != 200:
                return self.tags_status, ""
            body = json.dumps({"models": [{"name": m} for m in self.models]})
            return 200, body
        if self.status != 200:
            return self.status, ""
        return 200, json.dumps({"response": self.response})

    @property
    def generate_calls(self):
        return [c for c in self.calls if c["url"].endswith("/api/generate")]

    @property
    def tag_calls(self):
        return [c for c in self.calls if c["url"].endswith("/api/tags")]


@pytest.fixture
def svc():
    s = LocalLLMService()
    yield s
    set_transport(None)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WX_LOCAL_LLM_DISABLED", raising=False)
    monkeypatch.delenv("WX_OLLAMA_URL", raising=False)
    monkeypatch.delenv("WX_LOCAL_LLM_MODEL", raising=False)
    yield
    set_transport(None)


# ============================================================
#  可用性探测
# ============================================================
class TestAvailability:
    def test_available_when_model_present(self, svc):
        set_transport(FakeOllama(models=["qwen3:8b"]))
        assert svc.available() is True

    def test_unavailable_when_ollama_down(self, svc):
        set_transport(FakeOllama(raise_on="GET"))
        assert svc.available() is False
        assert "不可达" in svc.status()["reason"]

    def test_unavailable_when_model_missing(self, svc):
        set_transport(FakeOllama(models=["llama3:70b"]))
        assert svc.available() is False
        assert "未找到模型" in svc.status()["reason"]

    def test_model_tag_variant_accepted(self, svc):
        """Ollama 可能把模型登记成 qwen3:8b-q4_K_M，不该判为缺失。"""
        set_transport(FakeOllama(models=["qwen3:8b-q4_K_M"]))
        assert svc.available() is True

    def test_negative_cache_avoids_repeated_probes(self, svc):
        """★ Ollama 没装是本机当前的真实状态：不能每轮花 2s 连一个必然失败的端口。"""
        fake = FakeOllama(raise_on="GET")
        set_transport(fake)
        for _ in range(10):
            assert svc.available() is False
        assert len(fake.tag_calls) == 1, f"负缓存失效，探测了 {len(fake.tag_calls)} 次"

    def test_positive_cache(self, svc):
        fake = FakeOllama()
        set_transport(fake)
        for _ in range(5):
            assert svc.available() is True
        assert len(fake.tag_calls) == 1

    def test_force_bypasses_cache(self, svc):
        fake = FakeOllama()
        set_transport(fake)
        svc.available()
        svc.available(force=True)
        assert len(fake.tag_calls) == 2

    def test_kill_switch_skips_probe_entirely(self, svc, monkeypatch):
        monkeypatch.setenv("WX_LOCAL_LLM_DISABLED", "1")
        fake = FakeOllama()
        set_transport(fake)
        assert svc.available() is False
        assert fake.tag_calls == [], "关闭后不该发起任何探测"

    def test_custom_url_and_model_from_env(self, svc, monkeypatch):
        monkeypatch.setenv("WX_OLLAMA_URL", "http://10.0.0.5:9999")
        monkeypatch.setenv("WX_LOCAL_LLM_MODEL", "qwen3:4b")
        fake = FakeOllama(models=["qwen3:4b"])
        set_transport(fake)
        assert svc.available() is True
        assert fake.tag_calls[0]["url"].startswith("http://10.0.0.5:9999")


# ============================================================
#  显存纪律：请求参数必须锁死
# ============================================================
class TestVRamDiscipline:
    def test_generate_payload_locks_vram_params(self, svc):
        """8GB 卡的生死线：num_ctx=4096 + keep_alive=30m + /no_think。"""
        fake = FakeOllama(response='{"issues": [], "severity": "none"}')
        set_transport(fake)
        svc.proofread({"decision": "BUY", "confidence": 0.8}, {"current_price": 4200})
        gen = fake.generate_calls[0]["payload"]
        assert gen["options"]["num_ctx"] == NUM_CTX == 4096, "num_ctx 放大到 8192 会 OOM"
        assert gen["keep_alive"] == KEEP_ALIVE == "30m"
        assert gen["stream"] is False
        assert gen["prompt"].startswith("/no_think"), "必须关思维链（省时延省显存）"
        assert 0 < gen["options"]["temperature"] <= 0.5

    def test_no_model_load_on_probe(self, svc):
        """★ L0 常态不加载模型：探活只打 /api/tags，绝不触发 /api/generate。"""
        fake = FakeOllama()
        set_transport(fake)
        svc.available()
        assert fake.generate_calls == [], "探活阶段就加载模型 = 白占 5GB 显存"


# ============================================================
#  身份 A：校对员
# ============================================================
class TestProofreader:
    def test_clean_decision_returns_ok(self, svc):
        set_transport(FakeOllama(response='{"issues": [], "severity": "none"}'))
        r = svc.proofread({"decision": "BUY", "confidence": 0.75,
                           "entry_price": 4200, "stop_loss": 4180}, {"current_price": 4200})
        assert isinstance(r, ProofreadResult)
        assert r.ok is True and r.issues == [] and r.severity == "none"

    def test_issues_detected(self, svc):
        set_transport(FakeOllama(
            response='{"issues": ["BUY 的止损高于入场价"], "severity": "major"}'))
        r = svc.proofread({"decision": "BUY", "stop_loss": 4300, "entry_price": 4200}, {})
        assert r.ok is False
        assert r.severity == "major"
        assert "止损" in r.issues[0]

    def test_unavailable_returns_none_not_ok(self, svc):
        """★ None ≠ ok。把「没查」当「没事」是监控设计里最经典的自欺。"""
        set_transport(FakeOllama(raise_on="GET"))
        r = svc.proofread({"decision": "BUY"}, {})
        assert r is None

    def test_prompt_forbids_giving_direction(self, svc):
        """铁律二：校对员提示词必须明令禁止给方向。"""
        fake = FakeOllama(response='{"issues": [], "severity": "none"}')
        set_transport(fake)
        svc.proofread({"decision": "BUY", "confidence": 0.7}, {"current_price": 4200})
        p = fake.generate_calls[0]["payload"]["prompt"]
        assert "校对员" in p
        assert "禁止" in p and ("观点" in p or "方向建议" in p)

    def test_result_carries_no_direction_field(self, svc):
        """结构性守卫：校对结果里**不得**出现 decision 字段。

        一旦有人给它加上方向输出，8B 就会悄悄变成投票者 → 违反铁律二。
        """
        set_transport(FakeOllama(
            response='{"issues": [], "severity": "none", "decision": "SELL"}'))
        r = svc.proofread({"decision": "BUY"}, {})
        assert not hasattr(r, "decision")
        assert "decision" not in r.as_dict()

    def test_empty_decision_short_circuits(self, svc):
        fake = FakeOllama()
        set_transport(fake)
        assert svc.proofread({}, {}) is None
        assert fake.calls == [], "空决策不该发起任何请求"

    def test_issues_as_plain_string_normalized(self, svc):
        """小模型偶尔把 issues 写成字符串而非数组，不能因此崩。"""
        set_transport(FakeOllama(response='{"issues": "止损方向反了", "severity": "major"}'))
        r = svc.proofread({"decision": "BUY"}, {})
        assert r.issues == ["止损方向反了"] and r.ok is False

    def test_severity_inferred_when_missing(self, svc):
        """LLM 漏了 severity 字段时，推断结果必须是 minor 而非 major。

        ★ 2026-08-08 修正：本用例原本断言 major，固化了一个真实 bug——
        8B 的 JSON 输出格式本来就不稳，"漏字段 + 报了疑点"是高频情况，
        判成 major 会在断路器里直接把单子砍掉（格式抖动 = 砍单）。
        疑点照记，但不许升到可拦单的级别。
        """
        set_transport(FakeOllama(response='{"issues": ["缺 confidence"]}'))
        r = svc.proofread({"decision": "BUY"}, {})
        assert r.severity == "minor"
        # 来源必须是 LLM 侧；代码侧没发现问题就不能污染 code_severity，
        # 否则断路器会被间接触发。
        assert r.llm_severity == "minor"
        assert r.code_severity == "none"

    def test_structural_audit_survives_llm_failure(self, svc):
        """模型在线但 LLM 生成偶发 500 时，结构审计仍独立返回结果（不为 None）。

        ★ Phase 9.1 契约：None 仅代表「模型彻底不可用（available=False）」，
        不再代表「本次没查成」。这样 LLM 抖动不会让断路器失效。
        """
        set_transport(FakeOllama(status=500))
        r = svc.proofread({"decision": "BUY"}, {"current_price": 2650.0})
        assert r is not None, "模型在线时不应返回 None"
        assert r.ok is True, "干净决策在 LLM 失败时仍应判 clean（仅结构审计）"

    def test_structural_audit_catches_sl_reversal_without_llm(self, svc):
        """即使 LLM 完全不可用（500），代码侧结构审计仍能抓出 SL 挂反 → major。"""
        set_transport(FakeOllama(status=500))
        r = svc.proofread(
            {"decision": "BUY", "entry_price": 2650.0, "stop_loss": 2660.0,
             "take_profit": 2630.0},
            {"current_price": 2650.0},
        )
        assert r is not None
        assert r.severity == "major"
        assert any("止损方向" in i for i in r.issues)

    def test_structural_audit_tp_reversal(self, svc):
        """止盈方向错（BUY 的 TP 低于入场）→ major。"""
        set_transport(FakeOllama(status=500))
        r = svc.proofread(
            {"decision": "BUY", "entry_price": 2650.0, "stop_loss": 2640.0,
             "take_profit": 2640.0},
            {"current_price": 2650.0},
        )
        assert r.severity == "major"
        assert any("止盈方向" in i for i in r.issues)

    def test_structural_audit_hallucination(self, svc):
        """决策价偏离真实盘口 >5% → major（价格幻觉）。"""
        set_transport(FakeOllama(status=500))
        r = svc.proofread(
            {"decision": "BUY", "entry_price": 2650.0, "stop_loss": 2640.0,
             "take_profit": 2630.0},
            {"current_price": 1000.0},  # 真实盘口 1000，决策价 2650 明显幻觉
        )
        assert r.severity == "major"
        assert any("幻觉" in i for i in r.issues)

    def test_structural_audit_rr_minor(self, svc):
        """盈亏比失衡（回报/风险<0.3）→ minor（不拦，仅告警）。

        BUY：入场 2650，止损 2649（距 1.0，过近→minor），止盈 2650.2（方向正确>2650），
        RR = 0.2 / 1.0 = 0.2 < 0.3 → minor。
        """
        set_transport(FakeOllama(status=500))
        r = svc.proofread(
            {"decision": "BUY", "entry_price": 2650.0, "stop_loss": 2649.0,
             "take_profit": 2650.2},
            {"current_price": 2650.0},
        )
        assert r.severity == "minor"
        assert any("盈亏比" in i for i in r.issues)


# ============================================================
#  JSON 容错
# ============================================================
class TestJsonTolerance:
    @pytest.mark.parametrize("raw", [
        '{"issues": [], "severity": "none"}',
        '```json\n{"issues": [], "severity": "none"}\n```',
        '<think>让我想想...</think>{"issues": [], "severity": "none"}',
        '好的，检查结果如下：\n{"issues": [], "severity": "none"}\n希望有帮助！',
        '<think>\n复杂推理\n</think>\n```json\n{"issues": [], "severity": "none"}\n```\n',
    ])
    def test_extract_json_from_noisy_output(self, svc, raw):
        set_transport(FakeOllama(response=raw))
        r = svc.proofread({"decision": "BUY"}, {})
        assert r is not None and r.ok is True

    def test_garbage_output_degrades_gracefully(self, svc):
        set_transport(FakeOllama(response="我不知道该说什么"))
        r = svc.proofread({"decision": "BUY"}, {})
        assert r is not None and r.ok is True and r.severity == "none"

    def test_copilot_garbage_returns_none(self, svc):
        """副驾抠不出 JSON 必须返回 None（宁可不开仓，不可瞎猜方向）。"""
        set_transport(FakeOllama(response="嗯……市场很复杂"))
        assert svc.copilot({"current_price": 4200}) is None


# ============================================================
#  身份 B：副驾
# ============================================================
class TestCopilot:
    def test_vote_parsed(self, svc):
        set_transport(FakeOllama(
            response='{"decision":"SELL","confidence":0.72,"reason":"跌破结构"}'))
        v = svc.copilot({"current_price": 4200})
        assert isinstance(v, CopilotVote)
        assert v.decision == "SELL" and v.confidence == pytest.approx(0.72)

    @pytest.mark.parametrize("raw,expect", [
        ("BUY", "BUY"), ("buy", "BUY"), ("LONG", "BUY"), ("做多", "BUY"),
        ("SELL", "SELL"), ("short", "SELL"), ("卖出", "SELL"),
        ("WAIT", "HOLD"), ("", "HOLD"), (None, "HOLD"), ("胡说八道", "HOLD"),
    ])
    def test_decision_normalization(self, svc, raw, expect):
        set_transport(FakeOllama(
            response=json.dumps({"decision": raw, "confidence": 0.8})))
        v = svc.copilot({})
        assert v.decision == expect

    @pytest.mark.parametrize("raw,expect", [
        (0.75, 0.75), (75, 0.75), (1.0, 1.0), (150, 1.0),
        (-3, 0.0), ("0.6", 0.6), ("abc", 0.0), (None, 0.0),
    ])
    def test_confidence_normalization(self, svc, raw, expect):
        set_transport(FakeOllama(
            response=json.dumps({"decision": "BUY", "confidence": raw})))
        v = svc.copilot({})
        assert v.confidence == pytest.approx(expect)

    def test_prompt_requires_hold_when_unsure(self, svc):
        """副驾提示词必须显式要求「证据不足输出 HOLD」，抑制小模型硬猜。"""
        fake = FakeOllama(response='{"decision":"HOLD","confidence":0.3}')
        set_transport(fake)
        svc.copilot({"current_price": 4200})
        p = fake.generate_calls[0]["payload"]["prompt"]
        assert "HOLD" in p and ("证据不足" in p or "不要" in p)

    def test_unavailable_returns_none(self, svc):
        set_transport(FakeOllama(raise_on="GET"))
        assert svc.copilot({}) is None


# ============================================================
#  副驾三道锁（copilot_gate）
# ============================================================
class TestCopilotGate:
    def _vote(self, d="BUY", c=0.8):
        return CopilotVote(decision=d, confidence=c)

    def test_all_locks_pass(self):
        g = copilot_gate(self._vote("BUY", 0.8), "BUY")
        assert g["allow"] is True and g["decision"] == "BUY"
        assert "40%" in g["reason"], "放行文案须提醒手数只有 40%"

    def test_lock1_no_vote(self):
        g = copilot_gate(None, "BUY")
        assert g["allow"] is False and g["decision"] == "HOLD"

    def test_lock1_hold_vote(self):
        g = copilot_gate(self._vote("HOLD", 0.9), "BUY")
        assert g["allow"] is False

    def test_lock2_low_confidence(self):
        g = copilot_gate(self._vote("BUY", COPILOT_MIN_CONFIDENCE - 0.01), "BUY")
        assert g["allow"] is False and "门槛" in g["reason"]

    def test_lock2_exact_threshold_passes(self):
        g = copilot_gate(self._vote("BUY", COPILOT_MIN_CONFIDENCE), "BUY")
        assert g["allow"] is True

    def test_lock3_chronos_opposite(self):
        g = copilot_gate(self._vote("BUY", 0.9), "SELL")
        assert g["allow"] is False and "相悖" in g["reason"]

    @pytest.mark.parametrize("cd", [None, "", "NEUTRAL", "UNKNOWN", "N/A"])
    def test_lock3_chronos_no_direction(self, cd):
        """★ Chronos 中性 = 没有背书 = 不放行。宁可不开，不可蒙开。"""
        g = copilot_gate(self._vote("BUY", 0.95), cd)
        assert g["allow"] is False and "Chronos" in g["reason"]

    @pytest.mark.parametrize("cd,vote,ok", [
        ("UP", "BUY", True), ("LONG", "BUY", True), ("buy", "BUY", True),
        ("DOWN", "SELL", True), ("SHORT", "SELL", True),
        ("UP", "SELL", False), ("DOWN", "BUY", False),
    ])
    def test_chronos_direction_aliases(self, cd, vote, ok):
        g = copilot_gate(self._vote(vote, 0.8), cd)
        assert g["allow"] is ok

    def test_gate_never_raises_on_weird_input(self):
        for cd in (123, object(), [], {}):
            g = copilot_gate(self._vote(), cd)  # type: ignore[arg-type]
            assert g["allow"] is False


# ============================================================
#  状态与异常安全
# ============================================================
class TestStatusAndSafety:
    def test_status_shape(self, svc):
        set_transport(FakeOllama())
        svc.available()
        st = svc.status()
        for k in ("enabled", "available", "reason", "model", "base_url",
                  "warmed", "calls_ok", "calls_fail", "num_ctx", "keep_alive"):
            assert k in st

    def test_counters_move(self, svc):
        set_transport(FakeOllama(response='{"issues": [], "severity": "none"}'))
        svc.proofread({"decision": "BUY"}, {})
        assert svc.status()["calls_ok"] == 1
        assert svc.status()["warmed"] is True

    def test_transport_error_invalidates_probe_cache(self, svc):
        """Ollama 中途挂掉：下次 available() 必须重新探测，不能继续用旧的 True。"""
        fake = FakeOllama(response='{"issues":[]}')
        set_transport(fake)
        assert svc.available() is True
        fake.raise_on = "POST"
        svc.proofread({"decision": "BUY"}, {})
        fake.raise_on = "GET"
        assert svc.available() is False, "传输异常后必须重新探测"

    def test_module_helpers_never_raise(self, monkeypatch):
        class Boom:
            def available(self):
                raise RuntimeError("x")

            def proofread(self, *a, **k):
                raise RuntimeError("x")

            def copilot(self, *a, **k):
                raise RuntimeError("x")

        monkeypatch.setattr(llm, "get_local_llm", lambda: Boom())
        assert llm.is_available() is False
        assert llm.proofread({"decision": "BUY"}) is None
        assert llm.copilot({}) is None

    def test_singleton(self):
        assert get_local_llm() is get_local_llm()

    def test_reset(self, svc):
        set_transport(FakeOllama())
        svc.available()
        svc.reset()
        assert svc.status()["available"] is False


# ============================================================
#  真实环境探测（本机当前无 Ollama —— 这本身就是 L2→L3 的触发条件）
# ============================================================
class TestRealEnvironment:
    def test_real_probe_degrades_gracefully(self):
        """不 mock，直接打真实端口：无论装没装 Ollama 都不许抛异常。"""
        set_transport(None)
        s = LocalLLMService()
        t0 = time.time()
        ok = s.available()
        dt = time.time() - t0
        assert isinstance(ok, bool)
        assert dt < 10.0, f"探活耗时 {dt:.1f}s，会拖慢 60s 决策轮"
        assert s.proofread({"decision": "BUY"}, {}) is None or ok
