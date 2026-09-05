"""
万象AI — 外部指标 + 行情主号 端到端集成测试
"""
import sys, os, io, time
os.chdir(r'F:\\WanxiangAI\backend')
sys.path.insert(0, r'F:\\WanxiangAI\backend')

# ═══════════════════════════════════════════
# PART 1: 测试外部数据模块 (DXY / VIX)
# ═══════════════════════════════════════════
print('=' * 60)
print('PART 1: 外部数据模块 (DXY / VIX)')
print('=' * 60)

from app.services.market_data import market_data_provider

print('\n[1.1] 测试 DXY...')
dxy = market_data_provider.get_dxy()
if dxy:
    print(f'  ✅ DXY = ${dxy["price"]} ({dxy["change_pct"]:+.2f}%)')
    print(f'     高={dxy["high"]} 低={dxy["low"]}')
else:
    print('  ❌ DXY 获取失败')

print('\n[1.2] 测试 VIX...')
vix = market_data_provider.get_vix()
if vix:
    print(f'  ✅ VIX = ${vix["price"]} ({vix["regime"]})')
    print(f'     涨跌={vix["change"]:+.2f} ({vix["change_pct"]:+.2f}%)')
else:
    print('  ❌ VIX 获取失败')

print('\n[1.3] 测试 XAU-DXY 相关性...')
corr = market_data_provider.get_xauusd_dxy_correlation()
if corr:
    print(f'  ✅ 20日相关系数 = {corr["correlation_20d"]} ({corr["strength"]})')
    print(f'     5日相关系数 = {corr["correlation_5d"]}')
    print(f'     DXY日涨跌 = {corr["dxy_change_1d"]:+.2f}')
    print(f'     XAU日涨跌 = {corr["xau_change_1d"]:+.2f}')
    print(f'     信号 = {corr["signal"]}')
else:
    print('  ❌ 相关性计算失败')

print('\n[1.4] 综合快照...')
snap = market_data_provider.get_external_snapshot()
print(f'  ✅ 摘要: {snap["summary"]}')

# ═══════════════════════════════════════════
# PART 2: 测试行情主号 → MarketAnalyzer
# ═══════════════════════════════════════════
print('\n' + '=' * 60)
print('PART 2: 行情主号 → MarketAnalyzer')
print('=' * 60)

from app.services.mt5_service import mt5_service
from app.core.market_analyzer import MarketAnalyzer
import sqlite3

# 查找行情主号
conn = sqlite3.connect(r'C:\Users\15588\.wanxiangai\wanxiangai.db')
cur = conn.cursor()
cur.execute("SELECT id, name FROM mt5_accounts WHERE is_market_primary=1")
primary = cur.fetchone()
conn.close()

if primary:
    primary_id, primary_name = primary
    print(f'\n✅ 行情主号 = {primary_name} ({primary_id[:8]}...)')
else:
    # 降级取第一个已连接
    print('⚠️ 未设置行情主号，尝试降级...')
    primary_id = list(mt5_service._workers.keys())[0] if mt5_service._workers else ""
    primary_name = "降级"

print(f'\n[2.1] 当前 Worker 列表: {list(mt5_service._workers.keys())}')

print('\n[2.2] 从行情主号获取原始数据...')
raw = mt5_service.get_market_data(primary_id, "XAUUSD")
if raw and "error" not in raw:
    print(f'  ✅ 原始数据获取成功')
    print(f'     symbol={raw.get("symbol")}')
    print(f'     bid={raw["current"]["bid"]} ask={raw["current"]["ask"]}')
    for tf_name, tf_data in raw.get("timeframes", {}).items():
        bars = tf_data.get("count", 0)
        print(f'     {tf_name}: {bars} bars')
else:
    print(f'  ❌ 原始数据获取失败: {raw.get("error", "未知")}')

print('\n[2.3] MarketAnalyzer 构建完整快照...')
analyzer = MarketAnalyzer(mt5_service=mt5_service, market_primary_id=primary_id)
snapshot = analyzer.get_market_snapshot()

print(f'  symbol = {snapshot.get("symbol")}')
cp = snapshot.get("current_price", {})
print(f'  bid={cp.get("bid")} ask={cp.get("ask")} spread={snapshot.get("spread")}')

print(f'\n  技术指标 (各时间框架):')
for tf_name, tf_data in snapshot.get("timeframes", {}).items():
    rsi_val = tf_data.get("rsi", "N/A")
    trend = tf_data.get("trend", "?")
    ma20 = tf_data.get("ma", {}).get("MA20", "?")
    print(f'    {tf_name}: RSI={rsi_val} trend={trend} MA20={ma20}')

print(f'\n  波动率:')
vm = snapshot.get("volatility_metrics", {})
print(f'    H1_ATR={vm.get("h1_atr")} D1_ATR={vm.get("d1_atr")} regime={vm.get("volatility_regime")}')

print(f'\n  关键价位:')
kl = snapshot.get("key_levels", {})
print(f'    pivot={kl.get("pivot")} 阻力={kl.get("resistance")} 支撑={kl.get("support")}')

# DXY/VIX
print(f'\n  外部数据:')
ext = snapshot.get("external", {})
print(f'    DXY={ext.get("dxy", {}).get("price") if ext.get("dxy") else "N/A"}')
print(f'    VIX={ext.get("vix", {}).get("price") if ext.get("vix") else "N/A"} regime={ext.get("vix", {}).get("regime") if ext.get("vix") else "N/A"}')
print(f'    相关性={ext.get("correlation", {}).get("correlation_20d") if ext.get("correlation") else "N/A"}')
print(f'    摘要: {ext.get("summary", "N/A")}')

print('\n' + '=' * 60)
print('✅ 端到端测试完成')
print('=' * 60)
