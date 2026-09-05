"""Phase 7.1 版本治理 — 单一权威源验收测试。

背景（V6 设计文档 第十章 / 版本治理）：
    历史上版本号散落三处且互相矛盾——
      backend/app/config.py   APP_VERSION = "1.0.0"
      frontend/package.json   "version": "0.1.0"
      frontend Login.jsx      硬编码 "v1.0.0"
    客户报障时说不清跑的是哪一版，dist_vN 目录已堆到 29 个。

铁律：
    版本号只有一个权威源 —— 项目根 VERSION 文本文件。
    后端读它、前端构建注入它、/api/health 吐它、CHANGELOG 记它。
    任何地方再出现硬编码版本字符串即为回归。
"""
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = PROJECT_ROOT / "VERSION"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


class TestVersionFile:
    """权威源本身必须存在且合法。"""

    def test_version_file_exists(self):
        assert VERSION_FILE.exists(), f"版本权威源缺失：{VERSION_FILE}"

    def test_version_file_is_semver(self):
        raw = VERSION_FILE.read_text(encoding="utf-8").strip()
        assert SEMVER_RE.match(raw), f"VERSION 必须是 SemVer，实际={raw!r}"

    def test_version_file_single_line(self):
        """只放版本号，不放注释——避免解析歧义。"""
        raw = VERSION_FILE.read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert len(lines) == 1, f"VERSION 只允许一行，实际 {len(lines)} 行"


class TestVersionModule:
    """后端通过 app.version 读取权威源，不得自带副本。"""

    def test_get_version_matches_file(self):
        from app.version import get_version

        expected = VERSION_FILE.read_text(encoding="utf-8").strip()
        assert get_version() == expected

    def test_get_version_env_override(self, monkeypatch):
        """打包分发场景：允许 WX_VERSION 覆盖（安装器注入）。"""
        from app import version as version_mod

        monkeypatch.setenv("WX_VERSION", "9.9.9")
        version_mod.get_version.cache_clear()
        try:
            assert version_mod.get_version() == "9.9.9"
        finally:
            version_mod.get_version.cache_clear()

    def test_fallback_never_raises(self, monkeypatch):
        """VERSION 文件读不到时必须优雅兜底，绝不能让后端起不来。"""
        from app import version as version_mod

        monkeypatch.delenv("WX_VERSION", raising=False)
        monkeypatch.setattr(version_mod, "_candidate_paths", lambda: [])
        version_mod.get_version.cache_clear()
        try:
            v = version_mod.get_version()
            assert isinstance(v, str) and v, "兜底版本不能为空"
        finally:
            version_mod.get_version.cache_clear()

    def test_build_info_shape(self):
        """/关于弹窗 需要 version + buildTime + gitCommit（合规留痕）。"""
        from app.version import get_build_info

        info = get_build_info()
        for key in ("version", "build_time", "git_commit"):
            assert key in info, f"build_info 缺少字段 {key}"
        assert info["version"] == VERSION_FILE.read_text(encoding="utf-8").strip()


class TestConfigUsesAuthority:
    """config.APP_VERSION 必须来自权威源，而不是写死的字面量。"""

    def test_config_version_matches_file(self):
        from app.config import settings

        expected = VERSION_FILE.read_text(encoding="utf-8").strip()
        assert settings.APP_VERSION == expected, (
            f"config.APP_VERSION={settings.APP_VERSION} 与 VERSION={expected} 不一致"
        )

    def test_no_hardcoded_version_literal_in_config(self):
        """防回归：config.py 里不得再出现 APP_VERSION: str = "x.y.z"。"""
        src = (PROJECT_ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8")
        bad = re.search(r'APP_VERSION\s*:\s*str\s*=\s*["\']\d+\.\d+\.\d+["\']', src)
        assert bad is None, "config.py 仍硬编码版本号，必须改读 app.version.get_version()"


class TestFrontendSync:
    """前端 package.json 必须与权威源同步（构建期由脚本校验/同步）。"""

    def test_package_json_version_matches(self):
        pkg = PROJECT_ROOT / "frontend" / "package.json"
        assert pkg.exists()
        data = json.loads(pkg.read_text(encoding="utf-8"))
        expected = VERSION_FILE.read_text(encoding="utf-8").strip()
        assert data.get("version") == expected, (
            f"package.json version={data.get('version')} 与 VERSION={expected} 不一致"
        )

    def test_vite_injects_app_version(self):
        """Vite 必须 define __APP_VERSION__，否则前端拿不到构建期版本。"""
        vite = PROJECT_ROOT / "frontend" / "vite.config.js"
        src = vite.read_text(encoding="utf-8")
        assert "__APP_VERSION__" in src, "vite.config.js 未注入 __APP_VERSION__"
        assert "VERSION" in src, "vite.config.js 未读取根 VERSION 文件"

    def test_no_hardcoded_version_in_frontend_src(self):
        """防回归：前端源码不得出现 v1.0.0 这类硬编码版本字面量。"""
        src_dir = PROJECT_ROOT / "frontend" / "src"
        offenders = []
        pattern = re.compile(r"v\d+\.\d+\.\d+")
        for f in src_dir.rglob("*.jsx"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in pattern.finditer(text):
                offenders.append(f"{f.relative_to(PROJECT_ROOT)}: {m.group()}")
        for f in src_dir.rglob("*.js"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in pattern.finditer(text):
                offenders.append(f"{f.relative_to(PROJECT_ROOT)}: {m.group()}")
        assert not offenders, "前端仍有硬编码版本号：\n" + "\n".join(offenders)


class TestChangelog:
    """发版必须留痕。"""

    def test_changelog_exists(self):
        assert (PROJECT_ROOT / "CHANGELOG.md").exists(), "缺少 CHANGELOG.md"

    def test_changelog_contains_current_version(self):
        expected = VERSION_FILE.read_text(encoding="utf-8").strip()
        text = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert expected in text, f"CHANGELOG.md 未记录当前版本 {expected}"
