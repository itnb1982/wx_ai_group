"""
通过运行中的后端进程目录直接测试 market_analyzer + worker 数据链路
（在同一个 Python 进程中模拟，避免 yfinance 限流影响判断）
"""
import sys, os, time, json
os.chdir(r'F:\\WanxiangAI\backend')
sys.path.insert(0, r'F:\\WanxiangAI\backend')

from app.services.mt5_service import mt5_service
from app.core.market_analyzer import MarketAnalyzer
import sqlite3

# ── 1. 查找并连接行情主号 ──
conn = sqlite3.connect(r'C:\Users\15588\.wanxiangai\wanxiangai.db')
cur = conn.cursor()
cur.execute("SELECT id, name, account_id, password, server, terminal_path FROM mt5_accounts WHERE is_market_primary=1")
primary = cur.fetchone()
conn.close()

if not primary:
    print("❌ 未找到行情主号")
    sys.exit(1)

from app.utils.crypto import decrypt

primary_id, name, login, enc_pw, server, terminal = primary
password = decrypt(enc_pw)

print(f"行情主号: {name} (login={login})")
print(f"terminal: {terminal}")

# ── 2. 启动行情主号 Worker ──
ok = mt5_service.add_account(
    account_id=primary_id,
    login=str(login),
    password=password,
    server=server,
    name=name,
    terminal_path=terminal or "",
)
print(f"Worker 启动: {'✅' if ok else '❌'}")
time.sleep(1)

# ── 3. 测试原始数据获取 ──
print("\n[测试1] 从 Worker 获取原始 XAUUSD 数据...")
raw = mt5_service.get_market_data(primary_id, "XAUUSD")
if raw and "error" not in raw:
    print(f"  ✅ 成功: bid={raw['current']['bid']} ask={raw['current']['ask']}")
    for tf, data in raw.get("timeframes", {}).items():
        print(f"     {tf}: {data.get('count', 0)} bars")
else:
    print(f"  ❌ 失败: {raw.get('error', '?')}")

# ── 4. 测试 MarketAnalyzer 完整快照 ──
print("\n[测试2] MarketAnalyzer 构建完整快照...")
analyzer = MarketAnalyzer(mt5_service=mt5_service, market_primary_id=primary_id)
snapshot = analyzer.get_market_snapshot()

# 检查是否使用了模拟数据
is_mock = "ma" not in str(snapshot.get("timeframes", {}).get("H1", {}))
print(f"  数据来源: {'⚠️ 模拟数据' if is_mock else '✅ 真实MT5数据'}")
print(f"  价格: bid={snapshot['current_price']['bid']} ask={snapshot['current_price']['ask']}")
print(f"  点差: {snapshot.get('spread')}")

print("\n  技术指标 (5时间框架):")
for tf, data in snapshot.get("timeframes", {}).items():
    rsi = data.get("rsi", "?")
    trend = data.get("trend", "?")
    print(f"    {tf}: RSI={rsi}, trend={trend}")

print(f"\n  波动率: {snapshot.get('volatility_metrics', {}).get('volatility_regime', '?')}")
print(f"  关键价位: pivot={snapshot.get('key_levels', {}).get('pivot', '?')}")

# ── 5. 外部数据 ──
print("\n[测试3] 外部数据 (DXY/VIX)...")
ext = snapshot.get("external", {})
dxy_ok = ext.get("dxy") and ext["dxy"].get("price")
vix_ok = ext.get("vix") and ext["vix"].get("price")
corr_ok = ext.get("correlation") and ext["correlation"].get("correlation_20d") is not None

print(f"  DXY: {'✅ ' + str(ext['dxy']['price']) if dxy_ok else '⚠️ 限流(周末正常)'}")
print(f"  VIX: {'✅ ' + str(ext['vix']['price']) if vix_ok else '⚠️ 限流(周末正常)'}")
print(f"  相关性: {'✅ ' + str(ext['correlation']['correlation_20d']) if corr_ok else '⚠️ 限流(周末正常)'}")
print(f"  摘要: {ext.get('summary', '?')}")

# ── 6. 汇总 ──
print("\n" + "=" * 50)
if not is_mock:
    print("✅ 核心数据链路验证通过：行情主号 Worker → MarketAnalyzer → 真实MT5数据")
else:
    print("⚠️ 降级到模拟数据（可能是周末MT5无实时数据）")

if dxy_ok or vix_ok or corr_ok:
    print("✅ 外部数据链路验证通过")
elif not is_mock:
    print("⚠️ 外部数据不可用（可能是周末限流）")
else:
    print("ℹ️ 外部数据链路线测试（降级链路正常）")

print("=" * 50)

# ── 7. 清理 ──
mt5_service.shutdown_all()
