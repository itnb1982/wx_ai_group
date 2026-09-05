# -*- coding: utf-8 -*-
"""app.services 业务服务包。

★ 2026-08-17 修复循环导入：此前此处直接 import trade_executor/mt5_service 等，
  导致 `from app.services.cloud_switch import ...` 触发 trade_executor → debate_engine
  → meta_agent 循环（meta_agent 初始化到一半再被 debate_engine 导入 → ImportError）。
  本包只做子模块容器，业务模块一律由调用方显式 `from app.services.xxx import ...` 导入。
"""
