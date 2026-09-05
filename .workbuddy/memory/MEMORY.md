# 万象Ai XAUUSD 交易系统 — 项目长期记忆

## 核心设计铁律（2026-08-03 确立·最高优先级）
1. **多账号优先**：N 变量动态渲染，绝不写死账号数
2. **纯AI系统定位**：AI 驱动决策，非 if/else 堆指标；前端高端酷炫可视化
3. **交易策略铁律**：多交易多赚钱（PF>1），提准非拦截，不限制开单

## 关键技术决策记录
- **DB模式**：SQLite DELETE 模式（弃用 WAL，WAL 的 -shm 文件在单进程写场景下成 Defender 锁靶点）
- **DB路径**：`C:/WXDB/wx_prod.dat`
- **成交记忆**：内存环形缓冲 `ai_memory._trade_buf` 优先（永远可写），DB 持久化降级
- **前端 dist 切换**：`.env` 的 `FRONTEND_DIST_DIR=dist_vN`

## 已修复的重大阻塞（2026-08-05 审计）
| 日期 | 问题 | 修复 | 文件 |
|------|------|------|------|
| 08-05 | MetaAgent SPLIT_MIN_CONF=0.85 过高 | →0.55 | meta_agent.py:187 |
| 08-05 | BUY 方向+0.05 惩罚（R4遗留） | 删除，BUY/SELL 一视同仁 | trade_executor.py:565 |
| 08-05 | SQLite WAL 模式→readonly | 改 DELETE 模式 | database.py |

## 反模式警示
- ❌ 禁止用历史胜率统计做前置过滤（BUY惩罚教训）
- ❌ 禁止在执行层二次拦截 AI 已放行的方向信号
- ❌ 禁止 `range(4)`/固定账号列表/写死 N 列
- ❌ **禁止EA世界观灌输**：system prompt分析原则/喂数据维度不可停留在传统技术指标空间（RSI/MACD/布林带思维）。必须用SMC/订单流/宏观NLP/Regime多维机构空间。

## 产品根本方向认知（2026-08-05 用户严肃追问）
- **模型已是V4**（deepseek-v4-pro/v4-flash），非V3。用户看到的ai_daemon.py/deepseek-chat是旧快照。
- **但"EA世界观里跑LLM"是真问题**：prompt框架=传统技术分析思维，导致"山顶开BUY"等开错单。
- **真进化≠经验回注**：当前只把历史盈亏塞prompt（权重不变）。需本地RL/ML层+因果复盘。
- **升级路线**（详见AI大脑升级方案.html）：①换原始评判数据(SMC+订单流+宏观NLP+Regime) ②加反转哨兵代理制衡趋势跟踪 ③本地RL/ML真进化层。按全权授权自主落地，不回问参数。
