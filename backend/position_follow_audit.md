# 主号 ↔ 挂号 同步审计与根治报告

> 审计目标（用户原话）：**「主号跟挂号除了手数（本机不同手数不同）不同之外，其他的开单、管理订单和平仓都应该跟随主号。」**
> 审计日期：2026-08-14 ｜ 系统：万象Ai 智能交易系统（多租户 XAUUSD 纯 AI）

---

## 一、审计范围与方法

逐文件通读三条同步链路的主代码，定位任何"跟号脱离主号、独立决策"的旁路：

| 环节 | 主代码位置 | 审计重点 |
|---|---|---|
| 开单跟随 | `copy_order` `trade_executor.py:2383` | 是否严格复制主号方向/价/SL/TP，仅改手数 |
| 管理订单跟随 | `_mirror_leader_exits` `trade_executor.py:3859` + 主号 `publish_leader_exit` | 主号改 SL/TP 后跟号是否同步 |
| 平仓跟随 | 主号全平路径 + 跟号 `_mirror_leader_exits` 消费 | 跟号平仓是否**唯一**来自主号广播 |

---

## 二、审计结论（三环节）

### 2.1 开单跟随 ✅ 合格
`copy_order`（`trade_executor.py:2383`）严格复制主号：
- **方向**：100% 复制 `signal.direction`；
- **入场价**：用跟号实时行情对齐主号同一时刻成交（不沿用主号历史价，防滑点漂移）；
- **SL/TP**：复用主号 SL/TP 相对其开仓价的**偏移量**套到跟号入场价（`entry + (_leader_sl - _leader_entry)`），主副号**点数·ATR 风险结构完全一致**；且开仓即查主号当前真实持仓已上移的 SL/TP 套用（`trade_executor.py:2462`）；
- **手数**：唯一差异来源——按跟号自身 `base_capital`/风险% 计算，与真实余额脱钩（符合用户"只手数不同"要求）。

### 2.2 管理订单(SL/TP)跟随 ✅ 合格
主号任何 SL/TP 变动都先 `publish_leader_exit` 广播，跟号 `_mirror_leader_exits` 消费并按**相对偏移**复刻：

| 主号动作 | 主号广播点 | 跟号复刻点 |
|---|---|---|
| 智能止损上移 `move_sl` | `trade_executor.py:3267` | `trade_executor.py:3976` |
| 追踪止盈上移 `move_tp` | `trade_executor.py:3284` | （绝对价对齐，注释 L3905） |
| AI/PM 追踪锁利 | 同上 `move_sl` | 同上 |
| 主动 SL 对齐（每轮） | — | `trade_executor.py:3881` 直接读主号当前 SL 套用 |

SL 用相对偏移、TP 用绝对价，主副**盈利保护位始终同步**，无误砍。

### 2.3 平仓跟随 ❌→✅ 发现并根治偏离点

**主号所有主动平仓均经 `publish_leader_exit` 广播，跟号 `_mirror_leader_exits` 消费跟随**（已验证全平/分批/反转/L3/熔断/BASKET 全清均有广播）：

| 主号平仓路径 | 广播点 | 跟号消费 |
|---|---|---|
| 普通智能全平 | `trade_executor.py:3299` | `trade_executor.py:3921` |
| L2 反转防抖全平 | `trade_executor.py:3208` | 同上 |
| 主号独立反转即时平 | `trade_executor.py:3383` | 同上 |
| L3 篮子锁利 | `trade_executor.py:2969` / `:3446` | 同上 + `:4014` |
| 浮亏熔断/单笔熔断 | `trade_executor.py:3490` | 同上 |

**但发现一处跟号"独立平仓"旁路，违反"平仓跟随主号"铁律：**
> `trading.py` 主循环跟号段原调用 `f_exec._close_opposite_for_decision(_opp_decision)`（原 L278），让**跟号自己**查自身持仓、按主号方向**独立平掉反向仓**，不消费主号广播。

这正是 2026-08-14 实测「主号没平、挂号全平」的**架构根源**（ticket 383644306/383644634/383644696）：跟号有了独立于主号的平仓决策权，一旦与主号逻辑/时机出现差分，主副即不同步。

**注**：主号自身的 `_close_opposite_for_decision`（`:1759/1847/1865`）保留——它平的是主号自己反向仓并已在 `:3383` 广播，跟号靠广播跟随，无需跟号再独立跑。

---

## 三、根因与修复

**根因**：跟号平仓决策权被一分为二——① 主号广播（正确路径）② 跟号 `_close_opposite_for_decision` 独立判断（错误路径，制造不一致）。

**修复**：彻底移除跟号独立反转防护调用（`trading.py` 原 L261-278 整段）。修复后：
- 跟号平仓**唯一来源** = 主号广播（full_close / partial_close / move_sl / `__BASKET_CLOSE_ALL__`）+ MT5 原生 SL/TP（与主号同偏移距离，作为防爆仓最后防线）；
- 主号反转平仓 → 广播 → 跟号 `_mirror_leader_exits` 跟随，主副**同进同退**；
- 跟号在 `_manage_positions`（`:2942` return）与守护线程 `_follower_mirror_loop`（`:478/:489`，且 `:480` 注释已确认不再跑 `_fast_l3_lock`）中均**零独立平仓**。

`py_compile` 全后端 `EXIT=0` 通过。

---

## 四、修复后主副跟随完整链路

```
【开单】  主号信号塔开单 → copy_order(严格复制·仅改手数) → 跟号同向下单
                              │
【管理】  主号 smart_exit/AI/PM 上移 SL/TP → publish_leader_exit(move_sl/move_tp)
                              │
                              ▼
                      跟号 _mirror_leader_exits（相对偏移复刻 + 每轮主动对齐）
                              │
【平仓】  主号 full_close / partial / reverse / L3 / 熔断
          → publish_leader_exit(full_close/partial_close/__BASKET_CLOSE_ALL__)
                              │
                              ▼
                      跟号 _mirror_leader_exits 消费 → 同价同量平仓
                              │
         MT5 原生 SL/TP（跟号与主号同偏移距离）作为最后防线兜底
```

**跟号零独立平仓决策**，除手数外与主号完全同构。

---

## 五、改动清单

| 文件 | 行 | 改动 |
|---|---|---|
| `app/routers/trading.py` | 原 L261-278 | **删除**跟号独立反转防护段（`_close_opposite_for_decision` 调用 + `_opp_decision` 构造），替换为"纯跟随主号"注释说明 |

> `position_manager.py` 经核查实际仅输出 `full_close` 与 `trail_tighten` 两种 action（设计注释亦只列"停滞平仓>最小亏损平>追踪锁利"），`trade_executor.py` 集成已完整覆盖，**无需补充**。

---

## 六、生效与验证

- 当前后台进程（`pid=9040`，~11:54 启动）为旧代码，**必须重启才能生效**。
- 重启会先经 `restart_task_backend.bat` 把当前主号仍在扛的 SELL 浮亏单按市价平掉，再加载新代码。
- 生效后可观察日志验证：跟号不再出现独立 `反转即时平仓` 日志，所有跟号平仓均应来自 `[跟号镜像]` 消费主号广播。

---

## 七、遗留问题（非本次范畴，需另立项）

- **方向准确率本身**：近 7 天已实现 -$48,150（852 笔），说明模型融合层方向判断在现阶段失效。Position Manager 只解决"错了尽快跑"，不解决"为什么总开错"。
- 用户提及另一纯本地模型方向更准——若经回测验证确实更准，应研究接入主决策链（而非仅当降级副驾），但须走 ≥3 源调研 + 回测，不拍脑袋替换。
- 顺序建议：先重启加载"主副同步修复 + Position Manager"止血 → 再立项调研方向准确率。
