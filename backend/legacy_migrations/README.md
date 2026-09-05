# 历史迁移脚本归档（不要再执行）

本目录下的脚本是 Alembic 接管之前，用来手写 `ALTER TABLE` 加字段的一次性工具。
它们**已全部完成历史使命**，其效果已包含在生产库当前结构中，并被
`alembic/versions/*_baseline_schema.py` 基线快照固化。

## 为什么归档而不是删除

保留可追溯：想知道某个字段是哪一次、为什么加进来的，这里是唯一线索。

## 为什么绝对不能再执行

1. 它们直接对生产库跑 DDL，**没有备份、没有事务边界、没有版本记录**，
   失败即留下半截结构，无法回滚。
2. 它们与 Alembic 版本表（`alembic_version`）互不知情。手工改完结构后
   Alembic 仍以为库停在旧版本，下一次 `upgrade` 会二次施加变更 → 冲突或数据丢失。
3. 多客户部署下，各客户库的执行历史无法核对，结构会悄悄分叉。

## 从此以后：改数据库结构的唯一入口

```bash
cd backend

# 1. 改完 app/models/*.py 后，自动生成迁移
python -m alembic revision --autogenerate -m "简述这次改了什么"

# 2. 打开 alembic/versions/ 下新生成的文件，逐行确认（autogenerate 不是万能的）

# 3. 执行（会自动先备份生产库到 backend/backups/schema_migrations/）
python -m alembic upgrade head

# 查看当前版本 / 历史
python -m alembic current
python -m alembic history
```

回滚：`python -m alembic downgrade -1`；若结构已损坏，直接用
`backend/backups/schema_migrations/` 里迁移前的那份库文件覆盖回去。

## 基线说明

- 基线版本：`17ac6904264d`（baseline schema，2026-08-07）
- 已存在的生产库通过 `alembic stamp head` 纳管，**未执行任何 DDL**。
- 全新部署直接 `alembic upgrade head` 即可从零建出全部 7 张业务表。
