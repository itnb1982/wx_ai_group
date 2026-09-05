"""
可移植性审计规则的自测 —— 「守卫的守卫」

审计脚本本身也是代码，也会写错。而它写错的后果格外隐蔽：
误报会让人习惯性忽略输出（狼来了），漏报会让整个审计变成安慰剂。

2026-08-08 实例：F 段第一版正则 `[A-Za-z]:[\\/]` 把
`DEEPSEEK_BASE_URL=https://api.deepseek.com/v1` 当成了盘符路径——
因为 "https" 的 "s" + "://" 正好长得像 "S:/"。
加负向后顾 `(?<![A-Za-z])` 修掉后，必须有测试钉住，
否则下次有人"简化"这个正则时会悄悄退回去。
"""
import pytest

pytestmark = pytest.mark.unit


def _load_env_pattern():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "audit_portability.py"
    spec = importlib.util.spec_from_file_location("audit_portability", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── 必须报出来的（真·绝对路径，换机即废）────────────────────────
MUST_FLAG = [
    "DATA_DIR=F:/WanxiangAI/data",
    "DATABASE_URL=sqlite:///F:/WanxiangAI/backend/data/wx_prod.dat",
    r"LOG_DIR=C:\Users\somebody\logs",
    "MODEL_PATH=D:/models/qwen3-8b",
    "  BACKUP =  E:\\backup\\wx",
]

# ── 绝不能报的（正常配置，报了就是噪音）──────────────────────────
MUST_PASS = [
    "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1",
    "HUNYUAN_BASE_URL=https://tokenhub.tencentmaas.com/v1",
    "LICENSE_SERVER_URL=http://127.0.0.1:9000/api",
    "PORT=8080",
    "FRONTEND_DIST_DIR=dist",
    "SECRET_KEY=5b195a9f0e0998b66e51faaeb49c10e3",
    "HOST=127.0.0.1",
    "DATA_DIR=",
    "AI_DECISION_INTERVAL=60",
]


@pytest.mark.parametrize("line", MUST_FLAG)
def test_absolute_path_in_env_is_flagged(line):
    mod = _load_env_pattern()
    assert mod._ENV_ABS.match(line), f"应被判为绝对路径却漏报：{line}"


@pytest.mark.parametrize("line", MUST_PASS)
def test_normal_config_is_not_flagged(line):
    mod = _load_env_pattern()
    assert not mod._ENV_ABS.match(line), f"正常配置被误报为绝对路径：{line}"


def test_real_env_files_have_no_absolute_paths():
    """当前仓库里的 .env / .env.example 必须是干净的。

    这条会随代码库一起演进——将来谁再往 .env 里写死一个盘符，
    这个测试立刻变红，而不是等客户装不上才发现。
    """
    mod = _load_env_pattern()
    hits = mod.audit_config_abs_paths()
    assert hits == [], "配置文件出现绝对路径：\n" + "\n".join(hits)


def test_first_deploy_chain_is_intact():
    """部署链三个断点的回归守卫。

    只要有人删掉 init_deployment.py、或把它从 bootstrap.bat 里摘出去、
    或让它不再处理 SECRET_KEY / is_admin，这条立刻失败。
    """
    mod = _load_env_pattern()
    hits = mod.audit_first_deploy()
    assert hits == [], "首次部署链不完整：\n" + "\n".join(hits)
