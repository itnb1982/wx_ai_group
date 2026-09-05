# 万象AI — 外部指标 API 技术调研

> 调研日期：2026-08-02  
> 目标：DXY（美元指数）、VIX（恐慌指数）实时+历史数据的获取方案  
> 硬约束：免费优先、延迟 < 5 分钟、Python 可集成、不依赖付费 Bloomberg 终端

---

## 一、核心结论（TL;DR）

| 指标 | 首选方案 | 备选方案 | 月费 |
|------|----------|----------|------|
| **DXY** | yfinance (`DX-Y.NYB`) | TradingView WebSocket / Alpha Vantage | $0 |
| **VIX** | CBOE 官方 CSV + yfinance (`^VIX`) 互补 | FRED (`VIXCLS`) / Alpha Vantage | $0 |
| **TradingView 增强** | `pytradingview` PyPI 包 | `tradingview-api` PyPI 包 | $0 |

**关键发现**：当前 STARTRADER MT5 终端 **大概率不提供 DXY/VIX 交易符号**（这些是指数 CFD，不是所有经纪商都支持）。因此必须引入外部 API 作为数据源。

---

## 二、DXY（美元指数）获取方案

### 2.1 方案A：yfinance（✅ 推荐）

```
Ticker: DX-Y.NYB（ICE Futures US Dollar Index）
类型: 历史日线 + 日内延迟（15分钟）
频率: 每分钟可轮询
费用: 完全免费，无需 API Key
```

**Python 代码**：
```python
import yfinance as yf

# 获取最新 DXY 实时报价
dxy = yf.Ticker("DX-Y.NYB")
quote = dxy.history(period="1d", interval="1m")  # 1分钟K线
current = quote['Close'].iloc[-1]
print(f"DXY 当前: {current:.2f}")

# 获取历史日线（用于相关性计算）
hist = dxy.history(period="1y", interval="1d")
print(f"1年数据: {len(hist)} 条")
```

**可用字段**: Open, High, Low, Close, Volume  
**已知限制**:  
- `DX=F`（期货连续合约）也有效，但 `DX-Y.NYB` 更稳定
- 偶尔 Yahoo Finance API 限流，需要 exponential backoff 重试
- 非交易时段 Close 不变（符合预期）

### 2.2 方案B：TradingView WebSocket（实时推送）

```
Python包: pytradingview (>=0.5.0) 或 tradingview-api (>=1.0.1)
类型: WebSocket 实时推送
频率: Tick级
费用: 免费（匿名WebSocket连接）
```

**Python 代码**：
```python
# 方案B1: pytradingview
from pytradingview import TVclient

client = TVclient()
chart = client.Chart
chart.set_up_chart()
chart.set_market("TVC:DXY", {"timeframe": "1"})
chart.on_update(lambda data: print(f"DXY: {chart.get_periods['close']}"))
client.create_connection()

# 方案B2: tradingview-api
from TradingView import TradingViewClient, ChartSession
client = TradingViewClient()
client.connect()
session = ChartSession(client, "TVC:DXY")
session.on_update = lambda data: print(f"DXY: {data}")
session.subscribe()
```

**优势**: 真正的实时数据，延迟 < 1 秒  
**劣势**: 需要维持长连接，断线需重连逻辑

### 2.3 方案C：Alpha Vantage（备选）

```
API: https://www.alphavantage.co/query?function=FX_DAILY&from_symbol=USD&to_symbol=EUR&apikey=YOUR_KEY
类型: REST API
频率: 免费层 25次/天
费用: 免费层可用，但频率低
```

**不推荐作为主力**：免费层限制太死（25次/天），付费版 $49/月起。

---

## 三、VIX（恐慌指数）获取方案

### 3.1 方案A：CBOE 官方 CSV（✅ 推荐，历史数据）

```
URL: https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
类型: 每日更新 CSV
频率: 每日盘后更新
费用: 完全免费，无需 API Key
历史: 1990-01-02 至今（9202+ 行）
```

**Python 代码**：
```python
import pandas as pd

url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
vix = pd.read_csv(url, parse_dates=["DATE"])
vix = vix.rename(columns=str.lower).set_index("date").sort_index()
latest = vix['close'].iloc[-1]
print(f"VIX 最新收盘: {latest:.2f}")

# 同类指数也可获取（改文件名即可）：
# VIX9D_History.csv  — 9日波动率
# VIX3M_History.csv  — 3个月波动率（前 VXV）
# VVIX_History.csv   — 波动率的波动率
# VXN_History.csv    — 纳斯达克100波动率
```

### 3.2 方案B：yfinance（日内实时补充）

```
Ticker: ^VIX
类型: 历史日线 + 日内延迟（15分钟）
频率: 每分钟可轮询
```

**Python 代码**：
```python
import yfinance as yf

vix = yf.Ticker("^VIX")
# 日内实时（15分钟延迟）
intraday = vix.history(period="1d", interval="1m")
current_vix = intraday['Close'].iloc[-1]

# 历史日线
daily = vix.history(period="1y", interval="1d")
```

### 3.3 方案C：FRED（宏观经济学备用）

```
Series: VIXCLS
来源: Federal Reserve Economic Data
方式: pandas_datareader
```

```python
import pandas_datareader.data as web
from datetime import datetime

vix = web.DataReader("VIXCLS", "fred", start=datetime(2020,1,1))
```

---

## 四、XAUUSD 与 DXY/VIX 的相关性实证

### 4.1 XAUUSD-DXY 负相关

| 数据源 | 相关系数 | 说明 |
|--------|:--------:|------|
| TradingNX | **-0.85** | 日线级别 |
| pro-scalper | -0.70 ~ -0.90 | 不同时段有差异 |
| GitHub 量化项目 | -0.82 | 18个月滚动窗口 |

**关键发现**：DXY 对 XAUUSD 影响权重最大的时段是 **美盘（13:00-22:00 UTC）**，亚盘（00:00-08:00 UTC）相关性显著下降。

### 4.2 复合信号胜率（pro-scalper 18个月实证）

| 信号组合 | 平均盈利 | 胜率 |
|----------|:--------:|:----:|
| DXY趋势 + 黄金对齐 | 45-80点 | **68%** |
| 收益率趋势 + 黄金对齐 | 38-65点 | 65% |
| **三者全部对齐** | **60-120点** | **74%** |
| DXY/黄金背离 | 55-100点 | **72%**（反转信号） |
| 全部中性（无信号） | N/A | 39%（=随机） |

**结论**：DXY + VIX 双指标可显著提升 AI 方向判断准确率，是 P0 级依赖。

### 4.3 VIX 对黄金的影响机制

- **VIX > 25**: 恐慌模式 → 黄金避险需求↑ → XAUUSD 上涨（正向相关）
- **VIX 15-25**: 正常模式 → 黄金主要受 DXY/利率驱动
- **VIX < 15**: 低波动 → 趋势策略失效风险↑ → 应减仓或切换区间策略

---

## 五、架构集成方案

### 5.1 数据获取层设计

```
┌─────────────────────────────────────────────────┐
│               MarketDataProvider                 │
│  (新增模块: backend/app/services/market_data.py)   │
├─────────────────────────────────────────────────┤
│  ┌──────────────┐ ┌──────────────┐ ┌──────────┐ │
│  │ yfinance      │ │ CBOE CSV      │ │ TV WS     │ │
│  │ (DXY日内+历史)│ │ (VIX历史日线) │ │ (实时Tick) │ │
│  └──────┬───────┘ └──────┬───────┘ └─────┬────┘ │
│         │                │               │       │
│  ┌──────┴────────────────┴───────────────┴──────┐ │
│  │           DataCache (TTL 60s)                 │ │
│  │  _cache: { "DXY": (ts, value), "VIX": ... }  │ │
│  └──────────────────────┬───────────────────────┘ │
│                         │                         │
│              get_macro_snapshot()                  │
│              → {dxy, vix, dxy_change%,           │
│                  vix_regime, correlation_signal}  │
└─────────────────────────────────────────────────┘
```

### 5.2 API 接口设计

```python
# market_data.py — 核心接口
class MarketDataProvider:
    def get_macro_snapshot(self) -> dict:
        """供 MarketAnalyzer 调用的宏观快照"""
        return {
            "dxy": {
                "value": 100.95,
                "change_pct": -0.05,
                "trend": "weakening",  # strengthening/weakening/neutral
                "source": "yfinance",
                "updated_at": "2026-08-02T21:50:00Z"
            },
            "vix": {
                "value": 18.42,
                "regime": "normal",    # low(<15)/normal(15-25)/elevated(25-30)/fear(>30)
                "change_pct": +2.3,
                "source": "cboe+yfinance",
                "updated_at": "2026-08-02T21:50:00Z"
            },
            "correlation_signal": {
                "dxy_gold_aligned": True,      # DXY与黄金方向一致？
                "vix_regime_impact": "neutral", # 当前VIX对黄金的方向影响
                "composite_score": 0.72,        # 0~1，越高越看涨黄金
                "confidence": "high"            # high/medium/low
            }
        }
    
    def get_historical_correlation(self, days: int = 90) -> dict:
        """滚动相关性矩阵"""
        # 1. 下载 XAUUSD, DXY, VIX 历史日线
        # 2. 计算 Pearson 相关系数
        # 3. 输出相关性趋势
        ...
```

### 5.3 与现有 MarketAnalyzer 的集成

现有 `market_analyzer.py` 中的 `analyze_market_conditions()` 方法需增加一个参数：

```python
# 改造前（只依赖MT5行情主号Worker提供的数据）
def analyze_market_conditions(self) -> dict:
    mt5_data = get_mt5_market_snapshot()  # 来自主号Worker
    ...

# 改造后（增加外部API数据）
def analyze_market_conditions(self, macro_data: dict = None) -> dict:
    mt5_data = get_mt5_market_snapshot()
    if macro_data is None:
        macro_data = market_data_provider.get_macro_snapshot()
    
    # 新增：DXY-VIX增强分析
    dxy_trend = macro_data["dxy"]["trend"]
    vix_regime = macro_data["vix"]["regime"]
    
    # 将宏观信号融入现有趋势强度评分
    if dxy_trend == "weakening" and vix_regime != "fear":
        trend_score *= 1.15  # DXY走弱利好黄金
    if vix_regime == "fear":
        trend_score *= 1.20  # 恐慌模式黄金避险需求↑
    if dxy_trend == "strengthening" and vix_regime == "low":
        trend_score *= 0.85  # 美元走强+低波动=黄金承压
    ...
```

---

## 六、依赖安装清单

```bash
# 核心外部数据获取（免费）
pip install yfinance pandas

# TradingView 实时数据（可选，P2增强）
pip install pytradingview

# 数据缓存（已有？）
pip install cachetools  # TTL缓存装饰器
```

**总依赖**: 仅 2-3 个轻量 PyPI 包，无 API Key 申请，零费用。

---

## 七、风险与降级策略

| 风险 | 概率 | 降级方案 |
|------|:----:|----------|
| Yahoo Finance API 限流 | 中 | 自动切换到 CBOE CSV + TradingView WS |
| TradingView WebSocket 断连 | 低 | 自动回退到 yfinance 轮询模式（60s间隔）|
| CBOE CSV 格式变更 | 极低 | 降级到 FRED VIXCLS |
| 三个来源全部不可用 | 极低 | AI 仅依赖 MT5 内置指标（当前行为） |

所有外部数据获取均带 **try/except + 降级链路**，不会因外部 API 故障导致 AI 报错。

---

## 八、实施优先级

| 优先级 | 内容 | 工作量 |
|:------:|------|:------:|
| **P0** | `market_data.py` — yfinance 获取 DXY+VIX 实时数据 + TTL缓存 | 1h |
| **P0** | CBOE VIX CSV 解析 + 历史数据补全 | 0.5h |
| **P0** | `MarketAnalyzer` 改造 — 接收 macro_data 参数 | 0.5h |
| **P1** | 相关性计算引擎 — XAUUSD vs DXY/VIX 滚动窗口 | 1h |
| **P1** | 前端仪表盘展示 DXY/VIX 实时卡片 | 1h |
| **P2** | TradingView WebSocket 实时推送（降低延迟到 <1s）| 2h |

**P0 共计 2 小时，可立即动手。**
