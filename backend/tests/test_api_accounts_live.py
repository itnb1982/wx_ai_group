"""MT5 多账号 CRUD 的端到端 API 测试（★ 破坏性，默认永不执行）。

原为 backend/test_api.py 脚本式验证。Phase -1 收编时发现它是**危险脚本**：
开头就 `for a in GET /api/accounts: DELETE /api/accounts/{id}` —— 遍历删除目标
实例上的**全部账号**。若误指向生产实例，会一次性清空客户的 MT5 账号配置。

因此本文件采用三重保险，任一条不满足即 skip：
  1. pytest marker `live`  —— pytest.ini 已设 addopts = -m "not live"，默认不收集
  2. 环境变量 WX_ALLOW_DESTRUCTIVE_API_TEST=1 —— 必须显式开启
  3. 目标端口拒绝生产端口 8080/8081 —— 必须显式指定一个测试实例地址

跑法（仅在一次性测试实例上）：
    set WX_ALLOW_DESTRUCTIVE_API_TEST=1
    set WX_TEST_API_BASE=http://127.0.0.1:9090
    pytest tests/test_api_accounts_live.py -m live
"""
import os

import pytest

requests = pytest.importorskip("requests", reason="需要 requests 库")

# 生产实例端口黑名单：backend 常驻 8080，备用 8081
_PRODUCTION_PORTS = ("8080", "8081")

pytestmark = pytest.mark.live


def _resolve_base() -> str:
    """解析目标实例地址，任一保险不满足即 skip。"""
    if os.environ.get("WX_ALLOW_DESTRUCTIVE_API_TEST") != "1":
        pytest.skip(
            "破坏性测试未授权：本测试会删除目标实例上的全部 MT5 账号。"
            "确认是一次性测试实例后，设 WX_ALLOW_DESTRUCTIVE_API_TEST=1 再跑。"
        )

    base = os.environ.get("WX_TEST_API_BASE", "").rstrip("/")
    if not base:
        pytest.skip("未指定 WX_TEST_API_BASE，拒绝猜测目标实例（防误伤生产）。")

    for port in _PRODUCTION_PORTS:
        if base.endswith(f":{port}"):
            pytest.fail(
                f"拒绝在疑似生产实例上运行破坏性测试：{base}（端口 {port} 为常驻服务端口）"
            )
    return base


@pytest.fixture
def api():
    """返回 (base_url, auth_headers)。"""
    base = _resolve_base()
    resp = requests.post(
        f"{base}/api/auth/login",
        json={"email": "test@test.com", "password": "test123"},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    return base, {"Authorization": f"Bearer {token}"}


def test_account_crud_full_cycle(api):
    """账号 增 → 查 → 改 → 验 → 删 全链路。"""
    base, headers = api

    # 清场（破坏性，已由三重保险守住）
    existing = requests.get(f"{base}/api/accounts/", headers=headers, timeout=10).json()
    for acc in existing:
        requests.delete(f"{base}/api/accounts/{acc['id']}", headers=headers, timeout=10)

    # 新增
    resp = requests.post(
        f"{base}/api/accounts/",
        headers=headers,
        json={
            "name": "回归测试账户",
            "account_id": "888888",
            "password": "abc123",
            "server": "STARTRADER-Live",
            "terminal_path": "C:\\Program Files\\STARTRADER Financial MetaTrader 5\\terminal64.exe",
            "account_type": "real",
        },
        timeout=10,
    )
    assert resp.status_code == 200, f"新增账号失败: {resp.text}"
    acc = resp.json()

    # 列表
    listed = requests.get(f"{base}/api/accounts/", headers=headers, timeout=10).json()
    assert len(listed) == 1, f"期望 1 个账号，实际 {len(listed)}"

    # 修改
    resp = requests.put(
        f"{base}/api/accounts/{acc['id']}",
        headers=headers,
        json={"name": "回归测试账户-已改", "terminal_path": "F:\\mt52\\terminal64.exe"},
        timeout=10,
    )
    assert resp.status_code == 200, f"修改账号失败: {resp.text}"

    # 验证修改落库
    updated = requests.get(f"{base}/api/accounts/", headers=headers, timeout=10).json()[0]
    assert updated["name"] == "回归测试账户-已改"
    assert "mt52" in updated["terminal_path"]

    # 删除
    resp = requests.delete(f"{base}/api/accounts/{acc['id']}", headers=headers, timeout=10)
    assert resp.status_code == 200
    remaining = requests.get(f"{base}/api/accounts/", headers=headers, timeout=10).json()
    assert len(remaining) == 0, f"删除后仍剩 {len(remaining)} 个账号"


def test_mt5_terminal_discovery(api):
    """MT5 终端自动发现接口可用（只读，无副作用）。"""
    base, headers = api
    resp = requests.get(f"{base}/api/mt5/discover", headers=headers, timeout=15)
    assert resp.status_code == 200
    payload = resp.json()
    assert "count" in payload and "terminals" in payload
