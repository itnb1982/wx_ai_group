# -*- mode: python ; coding: utf-8 -*-
"""
XAU/USD万象Ai自动量化交易系统 — PyInstaller 打包配置
将 Python 后端打包为独立 Windows EXE
"""

import sys
from pathlib import Path

block_cipher = None

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

a = Analysis(
    [str(PROJECT_ROOT / 'backend' / 'app' / 'main.py')],
    pathex=[str(PROJECT_ROOT / 'backend')],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / 'frontend' / 'index.html'), 'frontend'),
    ],
    hiddenimports=[
        'passlib.handlers.pbkdf2',
        'sqlalchemy.sql.default_comparator',
        'MetaTrader5',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'fastapi',
        'jose',
        'cryptography',
        'httpx',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'pandas',
        'numpy',
        'scipy',
        'PIL',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='WanxiangAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'installer' / 'assets' / 'icon.ico'),
)
