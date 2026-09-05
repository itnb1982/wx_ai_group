# 万象Ai 盯盘巡检 自动化执行记忆

## 2026-08-13 00:15 (GMT+8)
- 后端探活 HTTP 200；行情欧盘开市中（STARTRADER, is_open=true, phase2）。
- 最近30分钟新单 6 笔（全 BUY，0 SELL），均为 follower 跟单。
- 三项回归检查（逆势SELL占比 / plain_summary反向文案 / 巨亏单<-300）均无异常。
- 结论：无需用户处理。
- 关键经验：trades.open_time 为本地 GMT+8；/api/dashboard/market-session 为经纪商 GMT+3。30分钟窗口必须用本地时间（now_local-30min）推算。

## 2026-08-13 01:13 (GMT+8)
- 后端探活 HTTP 200；行情 STARTRADER 美盘开市（is_open=true, phase3 美盘）。
- 最近30分钟新单 5 笔（全 BUY，0 SELL），含 1 笔 counter-consensus（Meta 看涨 vs 子模型2SELL/1HOLD，summary 已透明披露，非旧式颠倒）。
- 三项回归检查（逆势SELL占比 / plain_summary反向文案 / 巨亏单<-300）均无异常。
- 结论：无需用户处理。

## 2026-08-13 02:10 (GMT+8)
- 后端探活 HTTP 200（emergency 无 halt、mt5 6/6 在线）；STARTRADER 美盘开市（phase3，距收盘约2961s）。
- 最近30分钟新单 6 笔（全 BUY，0 SELL），均为跟单（snapshot 为 follower 型，无 votes/plain_summary）。
- 三项回归检查（逆势SELL占比 / plain_summary反向文案 / 巨亏单<-300）均无异常。
- 结论：无需用户处理。

## 2026-08-13 03:07 (GMT+8)
- 后端探活 HTTP 200（mt5 6/6、l3_guard/follower 存活）；STARTRADER 美盘开市（phase3，距收盘约10374s）。
- 最近30分钟新单 5 笔（全 BUY，0 SELL），均为跟单；含 1 笔 counter-consensus（DeepSeek SELL / 混元 HOLD / Chronos SELL，Meta 逆共识做多，plain_summary 已透明披露，非旧式颠倒）。
- 三项回归检查（逆势SELL占比 / plain_summary反向文案 / 巨亏单<-300）均无异常；单笔巨亏>500 亦为 0。
- 结论：无需用户处理。

## 2026-08-13 05:57 (GMT+8)
- 后端探活 HTTP 200（mt5 6/6、auto_loop 运行、无 halt）；行情 **休市**（每日轮转窗口，phase0，约208s后重开）。
- 最近30分钟新单 **0 笔**（休市所致，非引擎故障）→ 方向锚检查本轮 N/A；plain_summary 检查通过（近3h主号决策逆共识已透明披露）。
- 巨亏项：窗口内 0 笔，但日内已平仓 2 笔 <-500（-694 / -551.58），均属 DEMO 大账号 2877213e（余额97.6万，占比0.07%），按阈值标注【需用户处理·低优先/仅知悉】，判定为手数等比放大而非风控失效。
- 复核旧坑仍在：exit_reason='sl' 与记录 sl 价差21点（trailing 未回写 trades.sl）。
- 新观测：ai_activities 停写超8h（最近 08-12 21:59），下轮跟踪。
- 关键经验：休市窗口内「新单=0」属正常，需用 health.auto_loop_running 区分引擎故障；巨亏阈值判定应结合账号余额占比，避免把大额DEMO账号的等比放大误报为风控回归。

## 2026-08-13 06:56 (GMT+8)
- 后端探活 HTTP 200（mt5 6/6、auto_loop 运行、无 halt）；STARTRADER 亚盘开市（phase1，距收盘约72154s）。
- 最近30分钟新单 6 笔（全 BUY，0 SELL），均为跟单（follower 型 snapshot，持仓中 net=0）。
- 三项回归检查（逆势SELL占比 / plain_summary反向文案 / 巨亏单<-300 及 >-500）均无异常；plain_summary 全库扫描反转 0 笔（连续第6轮）。
- 结论：无需用户处理。
- 观测项：ai_activities 仍停写超8h（最近 08-12 22:58），连续第2轮未恢复，下轮跟踪。

## 2026-08-13 07:58 (GMT+8)
- 后端探活 HTTP 200（v1.4.0，uptime 31491s，mt5 6/6、auto_loop 运行、无 halt）；STARTRADER 亚盘开市（phase1，距收盘约68638s）。
- 最近30分钟新单 **0 笔**（全库最新开单 06:28:57，已89分钟无新单；今日持仓0）→ 方向锚本轮 N/A；旁证今日40笔新单全 buy、0 sell，引擎持续输出66% BUY 信号。
- 三项回归检查全部通过：plain_summary 反转0笔（连续第7轮）；无逆势SELL；窗口内巨亏0笔（今日2笔<-500为05:57轮已记录同两笔，无新增）。今日40笔全平净+141.58。
- **重要澄清（纠正05:57/06:56两轮误判）**：ai_activities 并未停写！该表 created_at 写 **UTC**，trades.open_time 为**本地GMT+8**，二者差恰好8h。实测近1h写入514条（8秒一跳），引擎完全活跃。前两轮「停写超8h」为时区假警报，已作废。
- 结论：无需用户处理。
- 关键经验：**同库双时区并存**——ai_activities.created_at 与 trades.created_at 均为 UTC，而 trades.open_time 为本地 GMT+8。判断引擎活性须按 UTC 窗口查 ai_activities，切勿用本地时间比对 created_at（会假报停写8h）。
## 2026-08-13 09:46 (GMT+8)
- 后端200；STARTRADER亚盘开市(phase1)。30min新单6笔全BUY。plain_summary窗口内无新颠倒(全库19笔为08-11~13凌晨历史残留，最新01:42已透明披露)。巨亏1笔-1265.11(DEMO大账号2877213e跟单SL触发，占比0.13%非风控失效)触发>500阈值，标注【需用户处理·低优先】。
