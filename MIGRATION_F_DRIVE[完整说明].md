# 万象Ai 交易系统 — F 盘迁移说明

## 迁移时间
2026-08-06

## 迁移原因
将整套 AI 智能交易系统代码、数据库、日志、模型数据统一迁移到独立的 F 数据盘，与 C 盘系统、D/E 盘其他数据隔离，降低误删/Defender/权限等意外风险。

## 新旧路径对照

| 用途 | 旧路径 | 新路径 |
|---|---|---|
| 项目代码根目录 | `C:\Users\15588\WorkBuddy\WanxiangAI` | `F:\WanxiangAI` |
| 数据/日志目录 | `C:\WXDB` | `F:\WanxiangAI\data` |
| SQLite 生产库 | `C:\Users\15588\WorkBuddy\WanxiangAI\backend\data\wx_prod.dat` | `F:\WanxiangAI\backend\data\wx_prod.dat` |
| 启动脚本 | `C:\Users\15588\WorkBuddy\WanxiangAI\start_all.bat` | `F:\WanxiangAI\start_all.bat` |
| 后端目录 | `C:\Users\15588\WorkBuddy\WanxiangAI\backend` | `F:\WanxiangAI\backend` |
| 前端构建产物 | `C:\Users\15588\WorkBuddy\WanxiangAI\frontend\dist_v28` | `F:\WanxiangAI\frontend\dist_v28` |

## 已更新的关键文件

- `F:\WanxiangAI\backend\.env`
  - `DATA_DIR=F:/WanxiangAI/data`
  - `DATABASE_URL=sqlite:///F:/WanxiangAI/backend/data/wx_prod.dat`
- `F:\WanxiangAI\start_all.bat`：启动目录改为 `F:\WanxiangAI\backend`
- `F:\WanxiangAI\backend\app\main.py`：日志路径、last_cycle_ts 路径改为读 `DATA_DIR` 环境变量
- `F:\WanxiangAI\backend\app\routers\trading.py`：last_cycle_ts 写入路径改为 `DATA_DIR`
- `F:\WanxiangAI\backend\app\services\trade_executor.py`：反转状态文件路径改为 `DATA_DIR`
- `F:\WanxiangAI\backend\*.py`：批量替换 `C:/WXDB` 和旧项目路径
- `F:\WanxiangAI\backend\*.ps1`：批量替换 Defender 排除路径和任务计划路径
- `F:\WanxiangAI\*.py`：批量替换旧项目路径

## 存储空间规划

F 盘总容量：1.86 TB，可用：1.86 TB。

当前实际占用：

| 项目 | 大小 |
|---|---|
| 代码 + 前端构建产物 | ~210 MB |
| WXDB 日志/历史数据 | ~66 MB |
| SQLite 生产库 | ~27 MB |
| node_modules | ~39 MB |
| **合计** | **~350 MB** |

未来预留：

| 项目 | 预计占用 |
|---|---|
| 本地 7B 模型（Q4 GGUF） | ~4 GB |
| LoRA 训练检查点 | ~2-5 GB |
| 5-10 年 XAU 历史数据 | ~1-5 GB |
| 运行日志（按 7 天轮转） | ~10 GB |
| 模型迭代/回测数据 | ~50 GB |
| **总计** | **~70 GB** |

F 盘 1.86 TB 富余极大，无需担心空间。

## 如何启动新位置系统

1. 如果旧系统还在运行，先关闭旧的 supervisor/cmd 窗口（或运行旧目录的 `restart_backend.py` 杀掉 8080 端口）。
2. 双击运行：`F:\WanxiangAI\start_all.bat`
3. 浏览器访问：`http://127.0.0.1:8080`

## 验证项

- [x] Python 配置读取：`DATABASE_URL=sqlite:///F:/WanxiangAI/backend/data/wx_prod.dat`
- [x] SQLite 连接成功：41 张表
- [x] `app.main` 导入成功，前端静态文件指向 `F:\WanxiangAI\frontend\dist_v28`
- [x] 关键 `.py`、`.ps1`、`.bat` 中的旧路径已替换

## 备份与回滚

- 完整备份保留在：`F:\WanxiangAI_backup_20260806`
- 旧目录保留在：`C:\Users\15588\WorkBuddy\WanxiangAI`（未删除，可回滚）
- 回滚方式：关闭新系统，重新运行旧目录的 `start_all.bat` 即可。

## 注意事项

1. 当前仍使用 C 盘的 Python 解释器：`C:\Users\15588\.workbuddy\binaries\python\versions\3.13.12\python.exe`。这是 WorkBuddy 管理的隔离运行时，不随项目迁移，继续可用。
2. 旧目录未删除，后续确认新系统稳定运行后，可手动删除 `C:\Users\15588\WorkBuddy\WanxiangAI` 释放空间。
3. 如果后续要装 Ollama/本地模型，建议模型目录也放在 `F:\WanxiangAI\models`，保持数据统一。
