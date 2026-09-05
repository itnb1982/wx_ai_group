"""
授权与激活测试（V6 Phase 8）

除了正向流程，重点压这几类**反向守卫**——它们对应的都是真实会赔钱/丢客户的场景：
  · 永久授权（valid_until=None）绝不能被判成"立即过期" —— 买断客户被锁死
  · 空指纹不能互相匹配 —— 两台都取不到 CPU ID 的机器会认亲，绑定失效
  · 授权模块异常必须 fail-open —— 一个 bug 让全体客户停摆
  · 拒绝路径绝不能触碰平仓 —— 授权是商业契约，不是风控
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.licensing import fingerprint as fp
from app.licensing import service as ls
from app.licensing import token as tk

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _issue(priv, **kw):
    payload = tk.build_payload(
        license_key=kw.pop("license_key", "WXAI-TESTA-TESTB-TESTC-TESTD"),
        **kw,
    )
    return tk.sign_payload(payload, priv)


# ══════════════════════════════════════════════════════════════
#  1. 令牌签发 / 验签
# ══════════════════════════════════════════════════════════════
class TestToken:
    def test_roundtrip(self, keypair):
        priv, pub = keypair
        token = _issue(priv, edition="pro", customer="测试客户", max_accounts=10,
                       valid_until=datetime.utcnow() + timedelta(days=30))
        c = tk.verify_token(token, public_key=pub)
        assert c.edition == "pro"
        assert c.edition_label == "专业版"
        assert c.max_accounts == 10
        assert c.customer == "测试客户"
        assert 28 <= (c.days_remaining() or 0) <= 30

    def test_tampered_payload_rejected(self, keypair):
        """改一个字节就必须验不过——否则客户改 max_accounts 就白嫖了。"""
        import base64

        priv, pub = keypair
        token = _issue(priv, max_accounts=1)
        prefix, body_b64, sig = token.split(".")
        body = json.loads(base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4)))
        body["max_accounts"] = 999
        nb = base64.urlsafe_b64encode(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode().rstrip("=")

        with pytest.raises(tk.TokenError) as e:
            tk.verify_token(f"{prefix}.{nb}.{sig}", public_key=pub)
        assert e.value.code == "TOKEN_BAD_SIGNATURE"

    def test_foreign_key_rejected(self, keypair):
        """别人拿自己的私钥签的令牌，用我们的公钥必须验不过。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        _, pub = keypair
        forged = _issue(Ed25519PrivateKey.generate(), edition="pro", max_accounts=999)
        with pytest.raises(tk.TokenError) as e:
            tk.verify_token(forged, public_key=pub)
        assert e.value.code == "TOKEN_BAD_SIGNATURE"

    @pytest.mark.parametrize("bad,code", [
        ("", "TOKEN_EMPTY"),
        ("abc", "TOKEN_MALFORMED"),
        ("WXAI9.aa.bb", "TOKEN_MALFORMED"),
        ("WXAI1.@@@.bb", "TOKEN_MALFORMED"),
    ])
    def test_malformed(self, keypair, bad, code):
        _, pub = keypair
        with pytest.raises(tk.TokenError) as e:
            tk.verify_token(bad, public_key=pub)
        assert e.value.code == code

    def test_perpetual_license_never_expires(self, keypair):
        """★ 反向守卫：valid_until=None 是永久买断，不是立即过期。搞反会锁死买断客户。"""
        priv, pub = keypair
        c = tk.verify_token(_issue(priv, valid_until=None), public_key=pub)
        assert c.valid_until is None
        assert c.is_expired() is False
        assert c.days_remaining() is None
        # 就算把时间推到 100 年后也不该过期
        assert c.is_expired(datetime.utcnow() + timedelta(days=36500)) is False

    def test_not_yet_valid(self, keypair):
        priv, pub = keypair
        future = datetime.utcnow() + timedelta(days=10)
        c = tk.verify_token(_issue(priv, valid_from=future,
                                   valid_until=future + timedelta(days=30)), public_key=pub)
        assert c.not_yet_valid() is True

    def test_pubkey_fingerprint_guard_blocks_swapped_key(self, monkeypatch, tmp_path):
        """★ 换掉 pem 文件必须被指纹校验挡下，否则自签一张永久证书就完事了。"""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        rogue = Ed25519PrivateKey.generate().public_key()
        pem = rogue.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        monkeypatch.setenv("WX_LICENSE_PUBLIC_KEY", pem)
        monkeypatch.setattr(tk, "_pubkey_loaded", False)
        monkeypatch.setattr(tk, "_pubkey_cache", None)
        assert tk.load_public_key(force=True) is None, "伪造公钥竟然被加载了"

        # 复位，别污染同进程的其它用例
        monkeypatch.delenv("WX_LICENSE_PUBLIC_KEY", raising=False)
        tk.load_public_key(force=True)

    def test_shipped_pubkey_matches_pinned_digest(self):
        """随包发的公钥必须与代码里钉死的指纹一致，否则打包出去客户端一张令牌都验不过。"""
        assert tk.EXPECTED_PUBLIC_KEY_SHA256, "公钥指纹不能留空发布"
        assert tk.load_public_key(force=True) is not None, "内置公钥与钉死的指纹不匹配"

    def test_license_key_alphabet_excludes_confusables(self):
        """客户是照着邮件手打授权码的，0/O/1/I/L 必须排除。"""
        for ch in "01OIL":
            assert ch not in tk._ALPHABET
        key = tk.generate_license_key()
        assert re.fullmatch(r"WXAI(-[A-Z2-9]{5}){4}", key), key

    @pytest.mark.parametrize("raw", [
        "wxai-d723t-vzf7q-faxhc-vqc9s",
        " WXAI D723T VZF7Q FAXHC VQC9S ",
        "WXAID723TVZF7QFAXHCVQC9S",
    ])
    def test_normalize_key(self, raw):
        assert tk.normalize_license_key(raw) == "WXAI-D723T-VZF7Q-FAXHC-VQC9S"


# ══════════════════════════════════════════════════════════════
#  2. 机器指纹
# ══════════════════════════════════════════════════════════════
class TestFingerprint:
    BASE = {"board": "b" * 64, "cpu": "c" * 64, "mac": "m" * 64}

    def test_all_three_match(self):
        assert fp.match_factors(self.BASE, self.BASE) == (True, 3)

    def test_nic_replaced_still_same_machine(self):
        """换网卡是常规维护，不该把付费客户锁在门外。"""
        cur = dict(self.BASE, mac="x" * 64)
        ok, hits = fp.match_factors(cur, self.BASE)
        assert ok is True and hits == 2

    def test_mainboard_replaced_is_another_machine(self):
        """换主板只剩 1 个要素命中 —— 那确实是另一台机器了。"""
        cur = {"board": "z" * 64, "cpu": "c" * 64, "mac": "y" * 64}
        ok, hits = fp.match_factors(cur, self.BASE)
        assert ok is False and hits == 1

    def test_whole_machine_copy_rejected(self):
        cur = {"board": "1" * 64, "cpu": "2" * 64, "mac": "3" * 64}
        assert fp.match_factors(cur, self.BASE) == (False, 0)

    def test_empty_factors_never_match(self):
        """★ 反向守卫：两台都取不到标识的机器绝不能因为"都是空"而互相认亲。"""
        blank = {"board": "", "cpu": "", "mac": ""}
        assert fp.match_factors(blank, blank) == (False, 0)
        # 一边有一边空，也不算命中
        half = {"board": "b" * 64, "cpu": "", "mac": ""}
        assert fp.match_factors(half, blank) == (False, 0)
        assert fp.match_factors(blank, self.BASE) == (False, 0)

    def test_partial_collection_still_usable(self):
        """只采到 2 个要素时仍应能正常绑定（虚拟机/精简系统常见）。"""
        partial = {"board": "b" * 64, "cpu": "c" * 64, "mac": ""}
        assert fp.match_factors(partial, partial) == (True, 2)

    def test_normalize_filters_oem_placeholders(self):
        """OEM 占位 UUID 若不过滤，成千上万台机器会算出同一个指纹。"""
        for bad in ("00000000-0000-0000-0000-000000000000",
                    "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
                    "To Be Filled By O.E.M.", "Default string", ""):
            assert fp._normalize(bad) == "", bad
        assert fp._normalize(" 4c4c4544-0031-... ") != ""

    def test_hash_is_salted_per_factor(self):
        """同一个原始值在不同要素位上必须得到不同哈希，避免跨要素碰撞。"""
        assert fp._hash_factor("board", "ABC") != fp._hash_factor("cpu", "ABC")
        assert fp._hash_factor("board", "") == ""

    def test_composite_is_stable_and_short(self):
        a = fp.compute_fingerprint(self.BASE)
        b = fp.compute_fingerprint(dict(self.BASE))
        assert a == b and len(a) == 32

    def test_real_collection_smoke(self):
        """真机采集：至少拿到 1 个要素，且不抛异常。"""
        d = fp.describe()
        assert d["factors_count"] >= 1
        assert len(d["fingerprint"]) == 32


# ══════════════════════════════════════════════════════════════
#  3. 授权状态机
# ══════════════════════════════════════════════════════════════
def _fake_row(**kw):
    base = dict(
        id="row-1", token="WXAI1.x.y", status="active",
        machine_fingerprint="fp32", bound_factors=None,
        last_heartbeat_at=None, last_seen_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _claims(**kw):
    base = dict(
        license_key="WXAI-TESTA-TESTB-TESTC-TESTD", edition="pro", customer="测试",
        max_accounts=10, valid_from=datetime.utcnow() - timedelta(days=1),
        valid_until=datetime.utcnow() + timedelta(days=30), machine={},
    )
    base.update(kw)
    return tk.LicenseClaims(**base)


@pytest.fixture
def wired(monkeypatch):
    """把状态机的所有 IO 依赖换成内存假件，测试不碰真库、不采真指纹。"""
    monkeypatch.setattr(ls, "_touch_last_seen", lambda *a, **k: None)
    monkeypatch.setattr(ls, "_count_used_accounts", lambda k: 0)
    monkeypatch.setattr(fp, "collect_factors", lambda force=False: {"board": "b", "cpu": "c", "mac": "m"})
    monkeypatch.setattr(fp, "compute_fingerprint", lambda f=None: "fp32")
    ls.invalidate_cache()
    yield monkeypatch
    ls.invalidate_cache()


def _run(monkeypatch, row, claims=None, enforce=True):
    from app.config import settings

    monkeypatch.setattr(settings, "LICENSE_ENFORCE", enforce, raising=False)
    monkeypatch.setattr(ls, "_load_license_row", lambda: row)
    if claims is not None:
        monkeypatch.setattr(ls, "verify_token", lambda t: claims)
    return ls.evaluate(force=True)


class TestStateMachine:
    def test_active(self, wired):
        s = _run(wired, _fake_row(), _claims())
        assert s.state == ls.LicenseState.ACTIVE and s.allow_open is True

    def test_perpetual_is_active(self, wired):
        """★ 永久授权必须是 ACTIVE，不能被判过期。"""
        s = _run(wired, _fake_row(), _claims(valid_until=None))
        assert s.state == ls.LicenseState.ACTIVE
        assert s.allow_open is True
        assert s.days_remaining is None

    def test_grace_after_expiry(self, wired):
        """到期后 72h 内仍放行——授权常在周末到期，到点即停会引爆客服。"""
        s = _run(wired, _fake_row(), _claims(valid_until=datetime.utcnow() - timedelta(hours=10)))
        assert s.state == ls.LicenseState.GRACE
        assert s.allow_open is True
        assert "宽限期" in s.message

    def test_expired_beyond_grace_stops_open_only(self, wired):
        s = _run(wired, _fake_row(),
                 _claims(valid_until=datetime.utcnow() - timedelta(hours=ls.GRACE_HOURS + 5)))
        assert s.state == ls.LicenseState.EXPIRED
        assert s.allow_open is False
        # 文案必须明确告诉客户"持仓不受影响"，否则客户以为仓位被扔了
        assert "现有持仓不受影响" in s.message

    def test_suspended(self, wired):
        s = _run(wired, _fake_row(status="suspended"), _claims())
        assert s.state == ls.LicenseState.SUSPENDED and s.allow_open is False

    def test_machine_mismatch_from_token_binding(self, wired):
        s = _run(wired, _fake_row(),
                 _claims(machine={"board": "OTHER", "cpu": "OTHER", "mac": "OTHER"}))
        assert s.state == ls.LicenseState.MACHINE_MISMATCH and s.allow_open is False

    def test_machine_mismatch_from_local_binding(self, wired):
        """★ 整库复制到另一台机：令牌未绑定，但本地 bound_factors 必须挡住。"""
        row = _fake_row(bound_factors=json.dumps({"board": "X", "cpu": "Y", "mac": "Z"}))
        s = _run(wired, row, _claims(machine={}))
        assert s.state == ls.LicenseState.MACHINE_MISMATCH and s.allow_open is False

    def test_local_binding_same_machine_ok(self, wired):
        row = _fake_row(bound_factors=json.dumps({"board": "b", "cpu": "c", "mac": "m"}))
        s = _run(wired, row, _claims(machine={}))
        assert s.state == ls.LicenseState.ACTIVE and s.allow_open is True

    def test_clock_rollback_detected(self, wired):
        """把系统时间调回去是绕过到期检查最省事的办法。"""
        row = _fake_row(last_seen_at=datetime.utcnow() + timedelta(days=40))
        s = _run(wired, row, _claims())
        assert s.state == ls.LicenseState.CLOCK_TAMPERED and s.allow_open is False

    def test_small_clock_drift_tolerated(self, wired):
        """★ 反向守卫：NTP 校准的小幅回退不能误判成作弊。"""
        row = _fake_row(last_seen_at=datetime.utcnow() + timedelta(hours=2))
        s = _run(wired, row, _claims())
        assert s.state == ls.LicenseState.ACTIVE

    def test_trial_when_no_license(self, wired):
        wired.setattr(ls, "_install_time", lambda: datetime.utcnow() - timedelta(days=2))
        s = _run(wired, None)
        assert s.state == ls.LicenseState.TRIAL and s.allow_open is True

    def test_unlicensed_after_trial(self, wired):
        wired.setattr(ls, "_install_time", lambda: datetime.utcnow() - timedelta(days=ls.TRIAL_DAYS + 1))
        s = _run(wired, None)
        assert s.state == ls.LicenseState.UNLICENSED and s.allow_open is False
        assert "现有持仓不受影响" in s.message

    def test_enforce_off_bypasses_everything(self, wired):
        s = _run(wired, None, enforce=False)
        assert s.state == ls.LicenseState.DISABLED and s.allow_open is True

    def test_bad_token_is_unlicensed_not_crash(self, wired):
        def boom(t):
            raise tk.TokenError("签名坏了", "TOKEN_BAD_SIGNATURE")

        wired.setattr(ls, "_load_license_row", lambda: _fake_row())
        wired.setattr(ls, "verify_token", boom)
        from app.config import settings

        wired.setattr(settings, "LICENSE_ENFORCE", True, raising=False)
        s = ls.evaluate(force=True)
        assert s.state == ls.LicenseState.UNLICENSED and s.allow_open is False

    def test_fail_open_on_internal_error(self, wired):
        """★ 最重要的一条：授权模块自己挂了，绝不能连累全体客户停摆。"""
        def boom():
            raise RuntimeError("数据库炸了")

        wired.setattr(ls, "_load_license_row", boom)
        from app.config import settings

        wired.setattr(settings, "LICENSE_ENFORCE", True, raising=False)
        s = ls.evaluate(force=True)
        assert s.allow_open is True
        assert s.detail.get("fail_open") is True

    def test_cache_avoids_recompute(self, wired):
        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return _fake_row()

        from app.config import settings

        wired.setattr(settings, "LICENSE_ENFORCE", True, raising=False)
        wired.setattr(ls, "_load_license_row", counting)
        wired.setattr(ls, "verify_token", lambda t: _claims())
        ls.evaluate(force=True)
        for _ in range(20):
            ls.evaluate()
        assert calls["n"] == 1, "开仓路径每轮都问授权，必须走缓存"


# ══════════════════════════════════════════════════════════════
#  4. 执行器闸门与配额
# ══════════════════════════════════════════════════════════════
class TestGate:
    def test_allows_when_active(self, wired):
        _run(wired, _fake_row(), _claims())
        wired.setattr(ls, "_ensure_account_slot", lambda l, s, account_id=None: (True, ""))
        ok, reason, code = ls.check_open_allowed("12345")
        assert ok is True and code == ""

    def test_blocks_when_expired(self, wired):
        _run(wired, _fake_row(), _claims(valid_until=datetime.utcnow() - timedelta(days=10)))
        ok, reason, code = ls.check_open_allowed("12345")
        assert ok is False and code == "LICENSE_EXPIRED"

    def test_quota_exceeded_blocks_only_that_account(self, wired):
        _run(wired, _fake_row(), _claims(max_accounts=3))
        wired.setattr(ls, "_ensure_account_slot",
                      lambda l, s, account_id=None: (False, "超配额") if l == "999" else (True, ""))
        assert ls.check_open_allowed("111")[0] is True
        ok, reason, code = ls.check_open_allowed("999")
        assert ok is False and code == "LICENSE_QUOTA_EXCEEDED"

    def test_gate_fail_open(self, wired):
        wired.setattr(ls, "evaluate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert ls.check_open_allowed("1") == (True, "", "")

    def test_allowed_states_are_consistent(self):
        """_ALLOWED_STATES 里的每个状态都得在标签表里有中文，否则前端显示英文码。"""
        for st in ls._ALLOWED_STATES:
            assert st in ls.STATE_LABELS


# ══════════════════════════════════════════════════════════════
#  5. 红线守卫（源码扫描）
# ══════════════════════════════════════════════════════════════
class TestRedLine:
    def test_licensing_never_closes_positions(self):
        """
        ★ V6 红线：授权失效只停开新仓，绝不强平持仓。

        用源码扫描而不是行为测试，是因为这条红线的破坏方式是"某天有人顺手加一行
        close_all_positions"，那种改动跑现有用例全绿，只有扫描能挡住。
        """
        forbidden = (
            "close_position", "close_all", "order_close", "force_close",
            "平掉", "强平", "全平",
        )
        pkg = Path(ls.__file__).resolve().parent
        for py in pkg.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            # 剥掉注释与文档串再扫，避免把红线说明本身误判成违规
            code = "\n".join(
                ln for ln in src.splitlines()
                if not ln.strip().startswith("#")
            )
            code = re.sub(r'"""[\s\S]*?"""', "", code)
            for word in forbidden:
                assert word not in code, f"{py.name} 出现平仓语义 '{word}' —— 违反授权红线"

    def test_denial_messages_reassure_about_positions(self, wired):
        """每个拒绝状态的文案都要安抚持仓，否则客户以为仓位被系统扔了。"""
        cases = [
            _claims(valid_until=datetime.utcnow() - timedelta(days=10)),
            _claims(machine={"board": "X", "cpu": "Y", "mac": "Z"}),
        ]
        for c in cases:
            s = _run(wired, _fake_row(), c)
            assert s.allow_open is False
            assert "持仓不受影响" in s.message, f"{s.state} 文案缺少持仓安抚"

    def test_masked_key_never_leaks_middle(self):
        m = ls._mask_key("WXAI-D723T-VZF7Q-FAXHC-VQC9S")
        assert "VZF7Q" not in m and "FAXHC" not in m
        assert m.startswith("WXAI-D723T") and m.endswith("VQC9S")
