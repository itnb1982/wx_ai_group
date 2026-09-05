# per-position 追踪止损 2s 化：调研与落地方案

> 2026-08-15 · 针对「30s 不是 tick 级，平仓能否智能保护安全与利润最大化」的系统性答复与改造。
> 结论先行：**tick 级追踪对黄金有害（噪音大、0.3s 影线即假突破）；正确做法是「规则引擎 + 分级追踪 + 服务端硬 SL 保险」且在快循环跑**。本次把 per-position 追踪从 30~60s 主循环下沉到 2s 守护线程。

---

## 一、全球调研（≥3 家独立出处交叉验证）

| 来源 | 关键结论 | 与本案的对应关系 |
|---|---|---|
| **Pro-Scalper**（XAUUSD 专精） | BE trigger 10~15pip；trailing 在浮盈 20~25pip 后激活、trail 距离 10~15pip；**ATR 自适应优于固定 pip**；黄金常 80~150pip/日，新闻秒级 200~400pip。 | BE/Lock/Trail 三级阈值锚定 ATR（R=初始SL距离） |
| **Scalper Gem Pro**（mql5 实盘 EA） | **Peak Profit Trailing 三级**：L1 BE→L2 锁固定利润→L3 峰值追踪；以点数/USD 阈值驱动；Max Giveback($) 峰值回吐上限。 | 直接对应本系统三级结构 |
| **AlgoMatrix** | ATR×1.5 追踪（M5 默认）；**"never trail tighter than 20 points on gold"**；tick 级追踪会因 0.3s 影线触发假突破。 | Trail 距离地板 20pt；不用 tick 级 |
| **mql5 论坛 2026-07 实盘讨论** | trailing distance 与 trigger 分离；结构/峰值追踪优于距离追踪；**固定硬 SL 作保险**（电源/网络/软件故障兜底）。 | 本系统 L0 硬 SL 即此保险；峰值追踪 |
| **iqoption 1R/3R 框架** | +1R 才激活追踪，+3R 收紧；简单一致优于过度优化。 | BE@1.0R / Lock@2.0R / Trail@2.5R |
| **PickMyTrade 2025-2026 综述** | ML/RL 微调出场、Volatility-Targeted Trailing(ATR)、Partial+Trailing 数学上优于纯 trailing。 | 分级 + 分批（smart_exit 已含分批） |

**交叉共识**：
1. 黄金波动大、噪音强，**tick 级追踪有害**——应在「结构/峰值」层面用 ATR 自适应距离，且**只在快循环（秒级）做 SL 上移**，而非每跳。
2. 三级保护：**保本(BE) → 锁利(Lock) → 峰值追踪(Trail)**，全部「只上移 SL、不收紧」。
3. **服务端硬 SL 是最后保险**，AI/视觉/系统全死也不影响。

---

## 二、为什么 30s 不 tick 级也能保护安全与利润

| 层级 | 响应粒度 | 是否依赖 AI | 作用 |
|---|---|---|---|
| L0 券商硬 SL/TP | ≈0ms（Tick 级，MT5 端） | 否 | **保命底线**：最多亏到 SL，不可能无上限 |
| L1 2s 篮子锁利 + 跟号镜像 | 2s | 否 | 浮盈达标全平、极端反向即跑 |
| **★ 本次新增：2s per-position 追踪** | **2s** | **否（纯规则）** | **BE→Lock→Trail 三级 SL 上移，逐笔锁利** |
| L2 规则引擎 smart_exit | 30~60s | 否 | 分批止盈 + 保本 + 追踪（本次改后作为 2s 的慢速备份） |
| L3 视觉/8B AI 增强 | 30~90s 异步 | 是（增强） | 结构级方向/看护，慢但有硬 SL 兜底 |

**关键点**：仓位安全由 L0/L1/★2s追踪 在 tick/2s 接住；利润最大化靠 2s 追踪做「结构级」锁利（视觉本就看 H4/M15 结构，30s 足够）。30s 的 AI 慢，最多少赚一点，**绝不爆仓**。

---

## 三、落地方案（参数全锁，不回问）

**仅主号/独立号（follow_leader=False 集合）自管追踪**；跟号由 `_follower_mirror_loop` 按「相对开仓价偏移」自动同步主号 SL（已有机制，零改动）。

新增 `TradeExecutor._fast_leader_trailing()`，由 `_l3_profit_lock_monitor_loop`（2s 守护线程）在 `_fast_l3_lock()` 后调用。对每笔持仓：

- **R** = 初始硬 SL 距开仓点数（永远取真实硬 SL，不臆造）。
- **L1 保本**：浮盈 ≥ 1.0×R → SL 移到 `open + 2pt` 缓冲（消除"赢转亏"）。
- **L2 锁利**：浮盈 ≥ 2.0×R → SL 移到 `open + 0.5×R`（锁定半 R 利润）。
- **L3 峰值追踪**：浮盈 ≥ 2.5×R → SL = `峰值价 - max(1.5×ATR, 20pt)`（峰值追踪，只上移）。

**全部约束（防过紧/防越市价）**：
- 只向有利方向移动（BUY 新SL>旧SL；SELL 反之）。
- 新 SL 距市价 ≥ `max(0.3×ATR, 8pt)` 呼吸空间（与 `smart_exit.MIN_SL_DIST` 同款思想）。
- 新 SL 不得越过市价（BUY<cur；SELL>cur）。
- ATR 带 30s 缓存，避免每 2s 调 `get_market_snapshot` 过重；刷新失败降级 20。
- 峰值利润跨 2s 循环累计，支撑 L3 峰值追踪。

**为什么这些参数合理**：BE@1.0R（iqoption +1R，保守避噪音）、Lock@2.0R（Scalper Gem Pro L2 同思路）、Trail@2.5R+1.5ATR（AlgoMatrix 默认 M5、地板 20pt 防过紧）、min_dist 0.3ATR/8pt（复用既有 `PM_TRAIL_MIN_ATR_MULT`/`MIN_SL_DIST` 常量，全系统一致）。

---

## 四、与现有 smart_exit 的关系（加法，非手术）

smart_exit 的追踪/保本逻辑**保留不动**，作为 30~60s 慢速备份；本次 2s 追踪与它**同为「只上移 SL」**，互不冲突（SL 单调棘轮上移）。本改动纯增量、低风险：即使 2s 循环宕机，L0 硬 SL + smart_exit 仍兜底。

---

## 五、验证方式（周一实盘）

1. 重载后 `tail -f supervisor_uvicorn.log | grep 主号追踪` 看每 2s 是否出现 `SL x→y` 上移记录。
2. 观察主号持仓：浮盈过 1R→SL 到 open+2；过 2R→lock；过 2.5R→trail 跟随峰值。
3. 跟号日志应出现 `[跟号镜像·SL对齐]` 同步主号新 SL（确认 2s 传播）。
4. 极端反向时：L0 硬 SL 先接住，2s 追踪已提前上移则更早锁利。
