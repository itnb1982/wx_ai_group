"""
授权 API 响应契约测试（V6 Phase 8.4）

╔══════════════════════════════════════════════════════════════════════╗
║ 为什么专门为这几个端点写契约测试：                                     ║
║                                                                      ║
║   Phase 8.4 联调时踩了一个真实的坑 —— 前端按"数组"消费                 ║
║   /license/machine 的 factors_present，而后端返回的是"字典"。          ║
║   `arr?.length` 在字典上是 undefined，于是那一块 UI **什么都不显示、    ║
║   也不报错**。这种静默失效比崩溃难查十倍：没有红字、没有堆栈，          ║
║   只有客户说"我这儿看不到硬件信息"。                                   ║
║                                                                      ║
║   前端没有测试栈（无 vitest/jest），把契约钉在后端是当前唯一            ║
║   低成本且有效的防线：字段名/类型/取值域一旦改动立刻炸测试。            ║
╚══════════════════════════════════════════════════════════════════════╝
"""
import pytest

from app.licensing import service as license_service
from app.licensing import fingerprint as fp

pytestmark = pytest.mark.contract


# ─────────────────────────────────────────────────────────────
# /api/license/status → license_service.get_status()
# ─────────────────────────────────────────────────────────────

#: 前端 services/license.js + 三个组件实际消费到的字段。
#: 少一个都会让 UI 出现 undefined。
STATUS_REQUIRED_KEYS = {
    "state",
    "state_label",
    "allow_open",
    "message",
    "license_key",
    "edition",
    "edition_label",
    "customer",
    "max_accounts",
    "used_accounts",
    "days_remaining",
    "valid_until",
    "grace_until",
    "machine_fingerprint",
    "last_heartbeat_at",
    "evaluated_at",
}

#: 前端 bannerLevel()/actionHint() 里 switch 到的全部状态。
#: 后端新增状态而前端没跟上时，客户会看到一个没有行动指引的空提示条。
KNOWN_STATES = {
    "trial",
    "active",
    "grace",
    "expired",
    "unlicensed",
    "machine_mismatch",
    "suspended",
    "clock_tampered",
    "quota_exceeded",
    "disabled",
}


def test_status_has_all_fields_frontend_consumes():
    d = license_service.get_status()
    missing = STATUS_REQUIRED_KEYS - set(d)
    assert not missing, f"/license/status 缺少前端依赖字段: {sorted(missing)}"


def test_status_state_is_known_to_frontend():
    d = license_service.get_status()
    assert d["state"] in KNOWN_STATES, (
        f"后端返回了前端未覆盖的状态 {d['state']!r}；"
        f"请同步更新 frontend/src/services/license.js 的 actionHint()"
    )


def test_status_field_types_are_stable():
    """类型错配是静默失效的主要来源，逐个钉死。"""
    d = license_service.get_status()
    assert isinstance(d["state"], str)
    assert isinstance(d["state_label"], str)
    assert isinstance(d["allow_open"], bool)
    assert isinstance(d["message"], str)
    assert isinstance(d["license_key"], str)
    assert isinstance(d["max_accounts"], int)
    assert isinstance(d["used_accounts"], int)
    # 这三个允许为 None（永久授权 / 非宽限期），但不允许是别的乱七八糟类型
    assert d["days_remaining"] is None or isinstance(d["days_remaining"], int)
    assert d["valid_until"] is None or isinstance(d["valid_until"], str)
    assert d["grace_until"] is None or isinstance(d["grace_until"], str)


def test_status_license_key_is_masked():
    """
    授权码是凭证。这个页面客户一定会截图发到群里问问题，
    出全就等于把凭证公开了。
    """
    d = license_service.get_status()
    key = d["license_key"]
    if not key:
        pytest.skip("当前无授权，无可脱敏内容")
    assert "****" in key, f"授权码未脱敏: {key}"
    # 中段必须被盖住，只留首尾便于客服核对
    parts = key.split("-")
    assert parts[2] == "****" and parts[3] == "****"


def test_status_datetime_fields_are_iso_sliceable():
    """
    前端用字符串下标切片显示（valid_until.slice(0,10) / evaluated_at.slice(11,19)），
    这要求必须是 ISO8601 "YYYY-MM-DDTHH:MM:SS" 形态。
    如果哪天后端改成时间戳数字或本地化格式，前端会显示成乱码而不报错。
    """
    d = license_service.get_status()
    for field in ("valid_until", "grace_until", "evaluated_at"):
        v = d.get(field)
        if not v:
            continue
        assert isinstance(v, str) and len(v) >= 19, f"{field} 非 ISO 字符串: {v!r}"
        assert v[4] == "-" and v[7] == "-" and v[10] == "T", f"{field} 非 ISO 格式: {v!r}"


# ─────────────────────────────────────────────────────────────
# /api/license/machine → fingerprint.describe()
# ─────────────────────────────────────────────────────────────

def test_machine_describe_shape():
    d = fp.describe()
    assert set(d) >= {"fingerprint", "factors_present", "factors_count", "platform"}
    assert isinstance(d["fingerprint"], str) and len(d["fingerprint"]) == 32
    assert isinstance(d["factors_count"], int) and 0 <= d["factors_count"] <= 3
    assert isinstance(d["platform"], str)


def test_machine_factors_present_is_dict_not_list():
    """
    ★ 这就是被踩到的那个坑，用测试把形态钉死。
      前端 presentFactorNames() 现在两种形态都兼容，但后端形态一旦无声改变，
      仍应该有人被吵醒 —— 因为 UI 的中文标签映射是按 board/cpu/mac 三个 key 写的。
    """
    d = fp.describe()
    fpres = d["factors_present"]
    assert isinstance(fpres, dict), (
        "factors_present 必须是 {board/cpu/mac: bool} 字典；"
        "改成数组会让前端标签映射失效（且不报错）"
    )
    assert set(fpres) == set(fp.FACTOR_KEYS), (
        f"硬件要素 key 集合变了: {sorted(fpres)} != {sorted(fp.FACTOR_KEYS)}；"
        f"前端 FACTOR_CN 中文映射需同步"
    )
    assert all(isinstance(v, bool) for v in fpres.values())
    assert d["factors_count"] == sum(1 for v in fpres.values() if v)


def test_machine_code_leaks_no_raw_hardware_id():
    """
    机器码页客户会截图。绝不能把主板 UUID / MAC 原文带出去。
    describe() 只应返回哈希与布尔位。
    """
    d = fp.describe()
    blob = repr(d)
    factors = fp.collect_factors()
    for key, raw in factors.items():
        if not raw:
            continue
        assert raw not in blob, f"describe() 泄漏了原始硬件标识 {key}"
        # MAC 常见分隔形态也一并排查
        assert raw.replace("-", ":") not in blob


# ─────────────────────────────────────────────────────────────
# 前后端共识：拒绝文案必须安抚"持仓"
# ─────────────────────────────────────────────────────────────

def test_frontend_hint_source_declares_positions_safe():
    """
    前端 actionHint() 里所有"已停开新仓"类文案必须显式写明持仓不受影响。
    这条和后端 test_licensing.py 的同名守卫是一对：
    后端保证 message 安抚，前端保证 hint 也安抚 —— 客户看到的是前端那句。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "frontend" / "src" / "services" / "license.js"
    if not src.exists():
        pytest.skip("前端源码不在当前检出中")
    text = src.read_text(encoding="utf-8")
    # 'expired' 分支是最典型的"已停开新仓"状态
    assert "持仓不受影响" in text or "止损止盈仍在正常工作" in text, (
        "前端 actionHint() 缺少对持仓的安抚话术；"
        "客户看到「已停止开新仓」第一反应是「我的单被平了？」"
    )
