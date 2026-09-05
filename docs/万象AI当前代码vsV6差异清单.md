# 万象AI 当前代码 vs V6 架构设计 — 差异清单（待用户确认）

> 调研范围：`F:\WanxiangAI\backend\app`（55 个 .py，核心 13,516 行）。本次只做测绘，未改任何代码。

## 总览

| 状态 | 数量 | 含义 |
|------|------|------|
| ✅ 完全具备 | 6 项 | Chronos、MetaQuality、ReversalSentinel、DebateEngine、SmartExit、AIExit |
| 🟡 部分具备 | 9 项 | AccountManager、MarketDataService、MetaAgent、RiskEngine、本金/手数权威链、账号隔离、主号跟号、AI 溯源、并发 |
| ❌ 完全缺失 | 2 项 | **ExecutionController（第④层执行控制层）**、**SignalBus（事件总线）** |

**一句话**：V6 的第④层（执行控制层）根本不存在，所有逻辑堆在 `trade_executor.py`（2231 行巨文件）+ 9 个全局 dict 上；权威链概念有雏形但断裂在 3 个文件；另外发现 3 个真实运行期 Bug。

---

## 一、❌ 完全缺失：执行控制层（V6 第④层）

**V6 规划**：`app/core/execution_controller.py`，账号级状态机（对账→决策→风控→下单→持仓管理→平仓）。

**现状**：
- 文件不存在，`app/core/` 只有 `debate_engine / deepseek_client / hunyuan_client / market_analyzer / meta_agent / reversal_sentinel`。
- 全部承载在 `app/services/trade_executor.py` 的 `class TradeExecutor`（单类 1950 行，30 个方法）。
- 核心函数 `execute_cycle()`（`:853-1192`，**单函数 340 行**）把「AI 决策 → 门控 → 账户查询 → SL/TP → 手数 → 风控钳手 → RiskEngine → 冷却 → 下单 → 落库」11 个阶段全串在一起，无法单独测试、无法复用、无状态持久化。

---

## 二、❌ 完全缺失：SignalBus（V6 事件总线）

**V6 规划**：`app/core/signal_bus.py`，统一事件总线 + 统一幂等/TTL + 锁。

**现状**：不存在，用 **9 个模块级裸 dict + 6 把锁** 顶替。

| 变量 | 位置 | TTL | 幂等 | 锁 | 问题 |
|------|------|-----|------|-----|------|
| `_REVERSAL_STATE` | trade_executor.py:18 | 180s | 无 | **无锁** | 并发写入无保护 |
| `_L3_LAST_LOCK` | trade_executor.py:58 | 120s | 无 | **无锁** | — |
| `_LAST_OPEN_TS` | trade_executor.py:62 | 永不清理 | 无 | 有 | **内存泄漏** |
| `_LEADER_EXIT_BUS` | trade_executor.py:70 | 消费180s/清理300s | 有 | 有 | **TTL 自相矛盾** |
| `_MIRRORED` | trade_executor.py:81 | 600s | 有 | 有 | — |
| `_MIRROR_FAIL` | trade_executor.py:84 | 永不清理 | 无 | 有 | **内存泄漏** |
| `_RECON_LAST` | trade_executor.py:87 | 60s节流 | 无 | 有 | — |
| `_LAST_COPIED_SIGNAL` | trade_executor.py:92 | 300s | 有 | 有 | — |
| `_LAST_CLOSE_TS` | trade_executor.py:96 | 90s | 无 | 有 | — |
| `_LATEST_LEADER_SIGNAL` | **routers/trading.py:56** | 120s | 无 | **无锁** | **跨文件**总线 |

**问题汇总**：无统一幂等/TTL（5 套不同 TTL）；2 个 dict 永不清理（泄漏）；3 个 dict 无锁却并发写入；主号信号在 `trading.py`、出场动作在 `trade_executor.py`，无单一入口；进程内存态重启即丢。

---

## 三、🔴 本金权威链：断裂在 3 个文件 + `input_capital` 不存在

**V6 规划**：`effective_capital = input_capital(用户输入) > base_capital(manual) > balance(live)`，单一权威函数。

**现状**：
- `input_capital` 全项目 **零命中**（grep 确认）→ V6 三级权威链目前只有后两级。
- 三处取本金逻辑，**表面一致、实际分叉**：

| 文件 | 行号 | 逻辑 |
|------|------|------|
| `intelligent_sizing.py` | 115-120 | `manual+base>0 → base 否则 balance` |
| `risk_engine.py` | 68-74 | 同上（注释写"与 sizing 完全一致"） |
| `trade_executor.py` | 531-563 `_cap_to_risk_limit` | **只用真实 balance**，不看 capital_source/base_capital |

- `trade_executor.py:548` 只按真实余额算风控手数上限，而 `intelligent_sizing` 可能按 `base_capital` 算 → 真实余额 < base_capital 时手数被拒（代码注释自己承认矛盾）。
- **没有单一 `effective_capital()` 函数**：`intelligent_sizing.py:115` 与 `risk_engine.py:68` 是两份复制粘贴代码，任一处改动即失配。

---

## 四、🔴 手数权威链：硬边界被"auto 缩放"自动突破 + AI 无上调通道

**V6 规划**：`user_min_lot`/`user_max_lot` 单笔硬边界不可突破；`max_position_lots` 同方向总持仓硬边界；AI 在硬边界内可上调下调。

**现状**：
- 字段命名不符：`min_lot_per_trade` / `max_lot_per_trade` / `max_position_lots`（无 `user_` 前缀）。
- **硬边界被静默突破**：`intelligent_sizing.py:129-135`，默认 `sizing_scale_mode="auto"` 时：
  ```python
  _scale = effective_balance / 1000
  max_lot     = max_lot * min(_scale, 50.0)   # ← 用户上限被放大最多 50 倍
  max_position_lots = max_position_lots * min(_scale, 50.0)
  ```
  $10000 账号的 `max_lot_per_trade=1.0` 会被自动放大成 **10.0 手** —— 直接违反 V6 "硬边界不可突破"。且 `_pos_cap=4.0`（`:132`）定义后**从未使用**（死变量）。
- **AI 无直接上调手数通道**：`target_risk_pct` 代码路径上只会被赋 `1.0`（reduce 砍半）；`position_intent="add"` 只是"跳过同向衰减"，不是直接抬手数。真正在放大边界的是机械的 `auto` 缩放，与 AI 无关。
- `trade_executor.py:555` 再做单向下钳（`position_size > risk_implied → 返回 risk_implied`），只降不升。

---

## 五、🟡 主号/跟号：取消跟随能恢复 ✅，但本金/手数"不继承"与 V6 相反

**V6 规划**：设主号后，跟号跟随主号本金口径、单笔手数范围、总持仓上限、日 DD 逻辑；取消跟随恢复自身参数。

**现状**：
- 主号识别复用 `MT5Account.is_market_primary`，无独立 leader/follower 字段；跟随开关 `strategy.follow_leader`（默认 True）。
- **是复制订单+复制出场动作**，耦合到 MT5 ticket 与 comment 正则 `L(\d+)`（注释自己承认 comment 会被券商截断、不可靠）。
- 跟号：手数跑自己、6 层风控跑自己、AI 不跑、出场/L3/浮亏熔断纯镜像主号（`trade_executor.py:1454-1458` 直接 return）。
- 参数继承（`routers/trading.py:104-140`，用 `copy.copy` 内存副本，不写回 DB）：
  - ✅ 继承：日 DD、回撤、总持仓上限、各类平仓参数。
  - ❌ **不继承（与 V6 相反）**：`base_capital`/`capital_source`（本金口径）、`min_lot_per_trade`/`max_lot_per_trade`（单笔手数范围）。
- 取消跟随 → 自动恢复自身参数 ✅（因从未写回 DB，符合 V6）。

---

## 六、🔴 AI 决策溯源：Chronos/Q/分位数只在文本里，前端读不到结构

**V6 规划**：前端固定展示 DS/HY/Chronos 三票 + 质量分 Q + P10/P50/P90。

**现状**：
- Chronos **确实参与裁决**（`meta_agent.py:455-484` 加权第三票），但 `DebateDecision` 无 Chronos 结构化字段。
- Q 分、Chronos 票、P10/P50/P90 **只以文本塞进 `reasoning_summary`**（Q 数值：`meta_agent.py:767`；P10 有字段但 P50/P90 没有，而 `meta_quality.py:160-162` 明明算出了三个值）。
- 前端（`routers/dashboard.py:728-736`）：DS/HY 两票是结构化的；Chronos 票、Q 数值、P50/P90 **全部只能从 `reasoning` 长文本里字符串解析**。

---

## 七、🟡 风控散落：RiskEngine 6 层独立，但另有 10 处风控在 trade_executor

**V6 规划**：RiskEngine 6 层物理风控，最终否决权，风控与 AI 决策门控分离。

**现状**：
- `risk_engine.py`（345 行）6 层齐全 ✅（点差/持仓笔数+手数/同向并发/日亏损/回撤/单笔风险/交易时段），统一入口 `check_trade_allowed`。
- **但另有 10 处风控散在 `trade_executor.py`**：`regime_open_mode` 体制门、`short_guard_mode` 空头约束、置信惩罚、亏损冷却、风控钳手、max_positions 二次检查、open_interval 冷却、churn 抑制、L3 篮子锁利、第⑤道浮亏熔断。
- **L2/L5 被重复实现**：`_cap_to_risk_limit`（trade_executor:531）与 `_check_per_trade_risk`（risk_engine:298）算同一件事但基准不同；`max_positions` 在两处各查一次。

---

## 八、🟡 并发模型：每用户一线程串行 + 180s 超时墙

**现状**（`routers/trading.py`）：
- `_auto_loop`（单线程，每 60s 一轮）→ `ThreadPoolExecutor(max_workers=用户数)`（用户级并发）→ `_run_cycle_with_timeout(uid, 180)`（**180s 超时墙**）。
- 用户内部：主号**串行先跑**，跟号在主线程**串行镜像**，只有 `follow_leader=False` 的独立账号并发。
- 超时只是"放弃等待"，工作线程是 daemon 不被杀，**可能造成下一轮重复下单**。
- 3 个循环线程（60s/10s/2s）会各自 `new TradeExecutor()`，**同一账号同时有 3 个执行器实例**，仅靠全局 dict TTL 互斥。
- **V6 要求"每账号独立状态机" → 现状是每用户一条串行主链，主号是全链路阻塞点。**

---

## 九、⚠️ 调研中发现的 3 个真实 Bug（与架构无关，但现在就影响运行）

| Bug | 位置 | 影响 |
|-----|------|------|
| **Bug 1** `_is_mirrored`/`_mark_mirrored` 定义 3 参数，调用只传 2 个 → TypeError | trade_executor.py:107/115 定义，:1969/1988 调用 | 主号 L3 篮子全平时，跟号孤儿单清理兜底路径**必定抛异常失效** |
| **Bug 2** `chronos_agree` 是死代码 | trade_executor.py:777 调用，meta_agent.py 未写入 `DebateDecision` | 三模型共振豁免分支永不生效，所有非强趋势一律 +0.03 惩罚 |
| **Bug 3** `_pos_cap` 定义未使用 | intelligent_sizing.py:132 | 总持仓上限与单笔共用 50 倍封顶，非设计意图的 4 倍 |

---

## 十、优先级建议（按风险 × 工作量）

| 优先级 | 事项 | 依据 |
|--------|------|------|
| **P0** | 修 Bug 1（`_is_mirrored` TypeError） | 跟号 L3 全清兜底完全失效 |
| **P0** | 修 Bug 2（`chronos_agree` 死代码，补 `DebateDecision` 字段） | 一行即可修复 |
| **P0** | 抽 `effective_capital()` 单一权威函数 | 消除 sizing/risk/trade_executor 三套口径 |
| **P1** | 明确 `sizing_scale_mode=auto` 是否允许突破用户 `max_lot_per_trade` | 当前静默放大 50 倍，与硬边界原则冲突 |
| **P1** | 收编 9 个 dict 为 SignalBus | 最小改动解耦 |
| **P1** | `DebateDecision` 补 `chronos_vote/chronos_weight/q_score/p50/p90` | 打通 V6 溯源 |
| **P2** | 拆 `execute_cycle` 为 ExecutionController 状态机 | 工作量最大，建议 P0/P1 稳定后做 |

---

## 十一、请用户确认的方向

1. **是否先修 3 个真实 Bug（P0）**？它们现在就在影响运行（尤其 Bug 1 让跟号篮子全平兜底失效）。
2. **`sizing_scale_mode=auto` 的 50 倍放大要不要保留**？V6 原则"硬边界不可突破"与之冲突，需要你拍板。
3. **落地的切入口**：是按 V6 路线图从 Phase 1（ExecutionController + 权威链）开始，还是先 P0 止血再重构？
4. 主号/跟号"本金口径 + 单笔手数范围不继承"这条，**确认要改成跟随主号**（与 V6 一致）吗？
