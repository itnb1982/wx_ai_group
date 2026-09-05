# 回归比对台（Regression Parity Bench）

> V6 架构 12.3 / 测试分层 L3。目的只有一个：**重构一个决策函数时，用真实历史输入证明"行为没变"**，而不是靠肉眼读 diff 和祈祷。

## 为什么需要它

这套系统的决策函数（`smart_exit.evaluate_position`、仓位计算、风控闸门…）都是
"输入一堆参数 → 输出一个动作"的纯逻辑。它们的共同特点是：

- 分支多，且分支之间互斥（改 A 分支很容易顺手破坏 B 分支）；
- 出错不报警 —— 算错的止损不会抛异常，只会在几小时后变成一笔亏损；
- 单测只能覆盖"我想得到的情况"，覆盖不到线上真实出现过的参数组合。

比对台补的正是最后一块：**拿线上真实发生过的入参，重跑，逐字段核对**。

## 三件套

| 模块 | 职责 |
|---|---|
| `serde.py` | 入参/返回值 ↔ JSON 互转。ORM 实例按表列摊平，回放还原成 `SimpleNamespace`；密码令牌类字段落盘即脱敏；NaN/Inf/datetime 包装后无损往返 |
| `recorder.py` | 录制。猴子补丁包装目标函数，调用后把入参与返回值追加进 JSONL |
| `replayer.py` | 回放。读 JSONL、还原入参、重跑、交给断言器 |
| `asserter.py` | 逐字段比对。差异精确到 `#3.result.new_sl` 这种路径，区分 值变/类型变/缺字段/多字段/长度变 |

## 用法

### 1）离线比对（重构前后对比，最常用）

```python
from tests.parity import load_cases, replay_all, assert_parity

cases = load_cases("smart_exit.evaluate_position")     # 读已录好的样本
report = replay_all(cases, new_impl)                   # 用新实现重跑
assert_parity(report)                                  # 有任何差异就炸，带字段级清单
```

差异报告长这样：

```
比对失败：30 条样本中 2 条不一致，共 3 处差异
  [值不同] #7.result.new_sl: 录制 4311.52 → 回放 4310.0
  [值不同] #7.result.reason: 录制 '早期保本(+4.9点)→SL移入' → 回放 '持有'
  [缺字段] #12.result.new_tp: 录制 None，回放没有
```

### 2）测试内录制（自给自足，不依赖仓库里的样本文件）

```python
from tests.parity import recording, load_cases, replay_all

with recording("my_tag", out_dir=tmp_path) as rec:
    rec.capture(kwargs=kw, result=func(**kw))

report = replay_all(load_cases("my_tag", out_dir=tmp_path), func)
```

### 3）线上录制真实调用

录制器**默认关闭**，必须显式开环境变量才生效：

```bash
set WX_PARITY_RECORD=1      # Windows；Linux 用 export
```

然后在进程启动处挂钩（挂在调用方模块上，因为调用方 `from ... import` 时已绑定了名字）：

```python
from tests.parity import recorder
import app.services.trade_executor as te

recorder.install(te, "smart_evaluate_position", tag="smart_exit.evaluate_position")
```

样本落在 `tests/parity/recordings/<tag>.jsonl`，该目录已 gitignore。

## 设计红线

录制器跑在交易主循环里，所以它自己绝不能变成事故源：

1. **默认关闭** —— 没有环境变量，`install()` 是空操作，一行代码都不改；
2. **录制失败静默** —— 序列化炸了、磁盘满了，一律返回 `False`，绝不冒泡；
3. **完全透明** —— 不改返回值；业务异常照常抛出（且不录，异常不属于返回值比对范畴）；
4. **有条数上限** —— 默认每 tag 200 条，跑一夜也不会把磁盘写满；
5. **可完整卸载** —— `uninstall()` 还原原函数，重复 `install()` 不套娃。

## 关于脱敏的诚实说明

`serde` 会把 `password` / `token` / `api_key` 一类字段落盘成 `<redacted>`。
如果某个被脱敏的字段恰好参与决策计算，那这条样本的回放结果就**不可信**。

所以 `load_cases()` 会主动扫描入参里的 `<redacted>`，命中就把样本标成 `tainted`，
并在比对报告里点名。宁可吵，也不要让一条被污染的样本冒充"比对通过"。

## 自检：这台机器本身是可信的吗

一个只会说"通过"的比对台，比没有比对台更危险 —— 它给的是虚假的安全感。
所以 `tests/test_parity_smart_exit.py` 里专门有一组**反向验证**：

- `test_asserter_catches_every_kind_of_change` —— 七种差异类型逐一验证能抓到，
  且分类正确（含 `True` vs `1` 这种 Python 里相等、契约上不等的坑）；
- `test_parity_fails_when_behavior_silently_changes` —— 模拟一次"手滑把早期保本
  系数从 0.15 改成 0.5"的重构，比对台**必须**炸，且差异要定位到具体字段；
- `test_replay_reports_exception_instead_of_crashing` —— 单条样本崩了要记成差异
  并继续，不能让整批比对中断。

## 已接入的函数

| 函数 | 样本来源 | 测试文件 |
|---|---|---|
| `smart_exit.evaluate_position` | 构造样本（覆盖 7 条互斥分支） | `tests/test_parity_smart_exit.py` |
| `smart_exit.evaluate_position` | 生产库真实策略配置 + 真实成交价 | `tests/test_parity_live_smart_exit.py`（标 `live`，默认不跑） |

> 真实数据的一个实测观察：最近 30 笔真实成交回放下来，**动作全部落在 `hold`（早期保本）
> 一条分支上**。这说明真实样本的分支覆盖天然很窄 —— 它能证明"常走的路没变"，
> 但守不住冷门分支。两类样本必须并存，缺一不可。

## 下一个该接入谁

优先接"改动频繁 + 出错无声"的纯函数：仓位计算（`sizing`）、风控闸门
（`decision_gates`）、初始 SL/TP（`compute_initial_sl_tp`）。
接入成本很低：录一批样本 + 一个 `replay_all` 断言即可。
