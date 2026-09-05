# -*- coding: utf-8 -*-
def patch(path, old, new, count=1):
    with open(path, 'r', encoding='utf-8') as f:
        s = f.read()
    n = s.count(old)
    if n != count:
        raise SystemExit(f"FAIL [{path}] expected {count} occurrence(s), found {n}\nOLD={old[:90]!r}")
    s = s.replace(old, new)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(s)
    print(f"OK {path}")

BASE = "F:/WanxiangAI/backend"

# ---------- 1. market_analyzer.py ----------
ma = f"{BASE}/app/core/market_analyzer.py"
patch(ma,
      "from datetime import datetime, timedelta\nfrom typing import Optional, List\nfrom loguru import logger\n",
      "from datetime import datetime, timedelta\nfrom typing import Optional, List\nfrom loguru import logger\nimport time\n")
patch(ma,
      "\n\nclass MarketAnalyzer:/n",
      "\n\n# ★ 2026-08-15：订单流CVD 源状态缓存（供仪表盘展示\"真 Binance / 代理 MT5\"）\n"
      "_LAST_ORDERFLOW_STATUS: dict = {\"ts\": 0.0, \"data\": None}\n"
      "\n"
      "\n"
      "def _cache_orderflow_status(of: dict) -> None:/n"
      "    \"\"\"把最近一次订单流CVD 源状态写入模块级缓存，供 get_orderflow_status() 读取。\"\"\"\n"
      "    if not isinstance(of, dict):\n"
      "        return\n"
      "    _src = of.get(\"source\")\n"
      "    _primary = of.get(_src) if _src else None\n"
      "    _is_real = (_primary or {}).get(\"is_real_cvd\") if isinstance(_primary, dict) else None\n"
      "    if _is_real is None:/n"
      "        _is_real = of.get(\"is_real_cvd\")\n"
      "    _LAST_ORDERFLOW_STATUS[\"ts\"] = time.time()\n"
      "    _LAST_ORDERFLOW_STATUS[\"data\"] = {\n"
      "        \"available\": bool(of.get(\"available\")),\n"
      "        \"source\": _src,\n"
      "        \"is_real_cvd\": _is_real,\n"
      "        \"available_sources\": of.get(\"available_sources\", []) or [],\n"
      "        \"reading\": of.get(\"reading\"),\n"
      "        \"buy_pressure_dry\": bool(of.get(\"buy_pressure_dry\")),\n"
      "        \"sell_pressure_high\": bool(of.get(\"sell_pressure_high\")),\n"
      "    }\n"
      "\n"
      "\n"
      "def get_orderflow_status() -> dict:/n"
      "    \"\"\"返回最近一次订单流CVD 源状态（供仪表盘呈现\"真/代理\"）。无数据时 available=False。\"\"\"\n"
      "    _d = _LAST_ORDERFLOW_STATUS.get(\"data\")\n"
      "    if not _d:/n"
      "        return {\n"
      "            \"available\": False, \"source\": None, \"is_real_cvd\": None,\n"
      "            \"available_sources\": [], \"reading\": None,\n"
      "            \"buy_pressure_dry\": False, \"sell_pressure_high\": False, \"ts\": 0.0,\n"
      "        }\n"
      "    return {\n"
      "        \"available\": _d.get(\"available\", False),\n"
      "        \"source\": _d.get(\"source\"),\n"
      "        \"is_real_cvd\": _d.get(\"is_real_cvd\"),\n"
      "        \"available_sources\": _d.get(\"available_sources\", []),\n"
      "        \"reading\": _d.get(\"reading\"),\n"
      "        \"buy_pressure_dry\": _d.get(\"buy_pressure_dry\", False),\n"
      "        \"sell_pressure_high\": _d.get(\"sell_pressure_high\", False),\n"
      "        \"ts\": _LAST_ORDERFLOW_STATUS.get(\"ts\", 0.0),\n"
      "    }\n"
      "\n"
      "\n"
      "class MarketAnalyzer:/n")
patch(ma,
      "            snapshot[\"orderflow\"] = get_orderflow_snapshot(tfs_raw)\n            _of = snapshot[\"orderflow\"]\n",
      "            snapshot[\"orderflow\"] = get_orderflow_snapshot(tfs_raw)\n            _of = snapshot[\"orderflow\"]\n            _cache_orderflow_status(_of)\n")

# ---------- 2. dashboard.py system_health ----------
db = f"{BASE}/app/routers/dashboard.py"
db_old = (
    '    return {\n'
    '        "ok": len(faults) == 0,\n'
    '        "faults": faults,\n'
    '        "modules": {\n'
    '            "mt5_online": online,\n'
    '            "mt5_offline": offline,\n'
    '        },\n'
    '        "degrade": degrade,\n'
    '        "local_llm": local_llm,\n'
    '        "execution": execution,\n'
    '        "checked_at": datetime.utcnow().isoformat(),\n'
    '    }\n'
)
db_new = (
    '    # ★ 2026-08-15：订单流CVD 源状态（真 Binance / 代理 MT5），供仪表盘呈现"真实工作"\n'
    '    orderflow_status = {"available": False}\n'
    '    try:/n'
    '        from app.core.market_analyzer import get_orderflow_status\n'
    '        orderflow_status = get_orderflow_status()\n'
    '    except Exception:/n'
    '        pass\n'
    '\n'
    '    return {\n'
    '        "ok": len(faults) == 0,\n'
    '        "faults": faults,\n'
    '        "modules": {\n'
    '            "mt5_online": online,\n'
    '            "mt5_offline": offline,\n'
    '        },\n'
    '        "degrade": degrade,\n'
    '        "local_llm": local_llm,\n'
    '        "execution": execution,\n'
    '        "orderflow_status": orderflow_status,\n'
    '        "checked_at": datetime.utcnow().isoformat(),\n'
    '    }\n'
)
patch(db, db_old, db_new)

# ---------- 3. local_llm_service.py ----------
ll = f"{BASE}/app/services/local_llm_service.py"
with open(ll, 'r', encoding='utf-8') as f:
    lls = f.read()
if "_orderflow_line" not in lls:
    lls += (
        "\n\n"
        "# ★ 2026-08-15：从市场快照提取一行紧凑订单流CVD 摘要，供本地 8B 校对/副驾提准降噪\n"
        "def _orderflow_line(md: dict) -> str:/n"
        "    \"\"\"订单流CVD 紧凑摘要：真 Binance / 代理 MT5 + 读写 + 买枯/卖压。缺失则安全返回。\"\"\"\n"
        "    of = (md or {}).get(\"orderflow\") or {}\n"
        "    if not of.get(\"available\"):\n"
        "        return \"订单流CVD: 暂不可用\"\n"
        "    src = of.get(\"source\") or \"unknown\"\n"
        "    primary = of.get(src) if src else None\n"
        "    is_real = (primary or {}).get(\"is_real_cvd\") if isinstance(primary, dict) else None\n"
        "    if is_real is None:/n"
        "        is_real = of.get(\"is_real_cvd\")\n"
        "    tag = \"真CVD(Binance)\" if is_real else \"代理(MT5)\"\n"
        "    return (\n"
        "        f\"订单流CVD: 源={src}({tag}) 读={of.get('reading', '-')} \"\n"
        "        f\"买枯={of.get('buy_pressure_dry')} 卖压={of.get('sell_pressure_high')}\"\n"
        "    )\n"
    )
    with open(ll, 'w', encoding='utf-8') as f:
        f.write(lls)
    print(f"OK {ll} (appended helper)")
else:
    print(f"SKIP {ll} (helper already present)")
patch(ll,
      '            f"波动体制(Regime): {_regime_str}\\n\\n"',
      '            f"波动体制(Regime): {_regime_str}\\n"\n            f"{_orderflow_line(md)}\\n\\n"')
patch(ll,
      '            f"待校对决策：\\n{d}\\n\\n"',
      '            f"待校对决策：\\n{d}\\n\\n"\n            f"上下文补充：{_orderflow_line(snap) if snap else \'订单流CVD: 未知\'}\\n\\n"')

# ---------- 4. debate_engine.py ----------
de = f"{BASE}/app/core/debate_engine.py"
patch(de,
      '            snap = {"current_price": _cp_scalar}\n',
      '            # ★ 2026-08-15：把订单流CVD 上下文一并交给校对员（提准降噪，纯增强）\n'
      '            snap = {"current_price": _cp_scalar, "orderflow": (market_data or {}).get("orderflow")}\n')

# ---------- 5. MarketClock.jsx ----------
mc = "F:/WanxiangAI/frontend/src/components/MarketClock.jsx"
mc_old = (
    "        数据源：{session.source === 'mt5' ? 'MT5 实时' : '静态兜底'}\n"
    "        {open && session.countdown_to_close_sec > 0 ? (\n"
)
mc_new = (
    "        数据源：{session.source === 'mt5' ? 'MT5 实时' : '静态兜底'}\n"
    "        <br />\n"
    "        订单流CVD：\n"
    "        {health?.orderflow_status?.available ? (\n"
    "          <span style={{ color: health.orderflow_status.is_real_cvd ? '#22c55e' : '#f59e0b', fontWeight: 700 }}>\n"
    "            {health.orderflow_status.is_real_cvd ? '真CVD(Binance)' : '代理(MT5)'}\n"
    "          </span>\n"
    "        ) : (\n"
    "          <span style={{ color: '#9ca3af' }}>暂不可用</span>\n"
    "        )}\n"
    "        {health?.orderflow_status?.available ? ` · ${health.orderflow_status.reading || ''}` : ''}\n"
    "        {open && session.countdown_to_close_sec > 0 ? (\n"
)
patch(mc, mc_old, mc_new)

print("ALL DONE")
