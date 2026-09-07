# 自动下单功能对比：`09061` → `custom-dev`（最新）

> 生成时间：2026-09-07 · 对照**当前最新** **`custom-dev`（HEAD** **`46949d0`）**，以 `09061` 为基准。
> 用途：快速看清自动下单功能在 6 层架构里"哪层动了、哪层没动、每个功能各自变了什么"。
> 结论以代码事实为准（不看注释），代码位置已标注两版本对应文件。

***

## 〇、对比基准

| 项         | 值                                                                                                                                                                                                                      |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 基准（主干/后者） | tag `09061`（commit `646d26a`，目录名为 `trader_gateway/tg/` 扁平结构）                                                                                                                                                           |
| 最新        | 分支 `custom-dev` HEAD `46949d0`（目录重构为 `Trading/` 三层包结构）                                                                                                                                                                 |
| 二者关系      | **09061 是 custom-dev 的祖先**，同一条演进线，中间仅 5 个提交                                                                                                                                                                            |
| 目录形态      | 核心功能全部被迁入 `Trading/`（`Broker`/`Engine`/`Infra`/`Risk`/`Source`/`Strategy`/`Test`）；`default_policy.py`/`layered_exit.py`/`example_trailing.py` 被删除，改出 `Strategy/Entry.py` 与 `Strategy/Exit.py`；新增 `Engine/Reconcile.py` |

**6 层口径**（按模块清单）：①信号源 ②策略层 ③风控 ④执行 ⑤Broker ⑥基础设施。

> 文档 v1.0 里那个"信号适配层"的职责（解析 / 快照冻结 / 幂等去重）未单独成层，已按其宿主文件归入 ①信号源、④执行、⑥基础设施。

***

## 一、6 层改动总览（哪层动了 / 哪层没动）

| 层                       | 判定                 | 一句话结论                                                                            |
| ----------------------- | ------------------ | -------------------------------------------------------------------------------- |
| ① 信号源                   | 🟢 **没动**          | SSE 实时源、历史回放源原样保留                                                                |
| ② 策略层                   | 🟢 **功能没动**（纯文件重组） | 入场 / 分层出场 / 移动止损仅被拆并到 `Entry.py`、`Exit.py`，逻辑逐字节一致                               |
| ③ 风控                    | 🔵 **简化**          | 五项硬闸门 + 三种手数算法全保留；只删"分仓批次"相关计算，补开语义翻转                                            |
| ④ 执行                    | 🔴 **改动最大**        | 开仓"拆 N 笔"→"一笔挂 N 手"；解锁"批次"→"单笔+补开"；新增中金所 20 手上限；状态机 / 信号门 / 离场语义 / 对账 / 卡单复核全部保留 |
| ⑤ Broker                | 🟡 **报单升级**        | 普通限价 + 软件超时撤单 → 恒定 FOK（交易所保证全成或全撤）；离场追价新增 `chase_interval` 间隔；超价幅度 0.6→1.0       |
| ⑥ 基础设施                  | 🔵 **只动配置**        | `store`/`events`/`types` 逐字节一致；`config` 删批键、合并超价、新增 `chase_interval`、翻转折补开默认值    |
| （App 壳层 · 进程托管，不在 6 层内） | 🟢 **没动**          | 启动 / 停止 / 探活 / `_restore_from_file` / 实盘双确认 / `.stop_request` flag 协议逐字节一致       |

**核心结论**：真正变的是「**执行层的入场方式**」和「**Broker 层的报单方式**」，其余四层一行业务逻辑没动。

***

## 二、功能清单 × 6 层归类 × 变了什么

### ① 信号源层 — 🟢 全保留

| 功能         | 09061 做什么                                  | custom-dev 最新                           | 性质 |
| ---------- | ------------------------------------------ | --------------------------------------- | -- |
| A 实时 SSE 源 | `tg/sources/sse_source.py` 每 5m K 线闭合推一帧快照 | `Trading/Source/SSE.py` 仅改 import，行为零变化 | 🟢 |
| B 历史回放源    | `tg/sources/replay_source.py` 测试 / 复现用     | `Trading/Source/Replay.py` 仅改 import    | 🟢 |

### ② 策略层 — 🟢 功能没动（仅文件拆并）

| 功能                                         | 09061 做什么                                                                                                                                                      | custom-dev 最新                                                                                             | 性质 |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -- |
| C 入场策略（买点开多 / 卖点开空 + 三道过滤）                 | `tg/strategy/default_policy.py` 的 `DefaultEntryPolicy`；`max_signal_range_points` / `min_stop_distance_points` / `max_stop_distance_points` 三道过滤**在 09061 已存在** | `Trading/Strategy/Entry.py` 独成一文件，逻辑逐字节一致（**非新增**）                                                        | 🟢 |
| D 分层出场 L1–L4（止盈 / 止损 / 时间 / 保本 / ATR / 收盘） | `tg/strategy/layered_exit.py` 的 `LayeredExitPolicy`                                                                                                            | `Trading/Strategy/Exit.py` 中 `LayeredExitPolicy` 原样保留，并聚合了 `DefaultExitPolicy` 与移动止损 `TrailingExitPolicy` | 🟢 |

### ③ 风控层 — 🔵 简化（硬闸门全保留）

| 功能                                                  | 09061 做什么                                                | custom-dev 最新                                                                                    | 性质    |
| --------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----- |
| E 五项硬闸门（时段 / 尾盘 / 单笔手数 / 日笔数 / 日亏）                  | `tg/risk.py` 的 `check_open` 五道门 + `on_trade_closed` 日亏累加 | `Trading/Risk/RiskGate.py` 逻辑逐行保留，仅删 `check_open` 的 `batch_count`/`existing_same_side` 参数及其诊断校验块 | 🟢→🔵 |
| F 三种手数算法（fixed / capital\_pct / atr\_risk）          | `tg/sizing.py` 的 `size()`                                | `Trading/Risk/PositionSizing.py` 的 `size()` 原样保留                                                 | 🟢    |
| G 解锁补开语义 `unlock_no_new_open`                       | 09061 默认 `False`（解锁后可补开缺额）                               | **默认翻转为** **`True`**（只平昨仓、不补开今仓，规避平今高费）                                                          | 🟡    |
| H 分仓批次 `size_batch` / `batch_open` / `batch_unlock` | 09061 "每信号拆 N 笔"的规模拆分                                    | **整体删除**（引擎不再拆批）；新增 `_CFFEX_SINGLE_ORDER_MAX=20` 兜底默认、模块级 `capital_gate()` 资金闸门函数                | 🔴    |

### ④ 执行层 — 🔴 改动最大，但骨架 / 离场语义全保留

| 功能                                     | 09061 做什么                                                                                                          | custom-dev 最新                                                                                 | 性质 |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- | -- |
| I 四态状态机 + 信号门                          | `tg/engine.py`：IDLE / OPENING / IN\_TRADE / EXITING；同向 `skip`、反向 `close_only`、in\_flight 忽略                        | `Trading/Engine/Engine.py` 的 `on_signal` / `EngineState` 逐行等价                                 | 🟢 |
| J 幂等去重 / 快照冻结                          | `try_mark_signal`（sqlite 原子标记）+ `Signal` 结构冻结                                                                      | 保留（`on_signal` 第一道门 + `Infra/Store.py`）                                                       | 🟢 |
| K **开仓入场**                             | `_open_positions` 把 N 手拆成 `batch_count` 笔独立报单，每笔落一个 Position                                                       | **改为 1 笔 FOK 报单挂满 N 手、簿记 1 笔 Position**；`_open_positions`/`_size_batch` 删除                    | 🔵 |
| L **解锁入场**                             | `_unlock_batch_entry` 串行解多笔 + 批次 5s 复核 + 三分支结算（`_check_unlock_batch`/`_unlock_batch_settle`/`_reconstruct_signal`） | **整块删除** → 单笔 FOK 解锁最老一笔 LOCKED 仓，缺额按 `unlock_no_new_open` 补开；`_unlock_in_flight`(F1 单笔复核) 保留 | 🟡 |
| M 资金闸门 `_capital_gate`                 | 内联、带 `batch_count`：`cap = X // batch_count`                                                                        | 取消除法（`cap = X`，因不再分仓）；逻辑平移为 `PositionSizing.capital_gate()` 薄委托                               | 🔵 |
| N 离场结算（止盈 / 止损 / 时间 / 收盘 / 锁全部 / FIFO） | `_settle_positions`/`_after_close`/`shutdown_and_lock_all`/`_close_positions`                                      | 全部保留；仅事件字段 `batch_size`→`pos_count`                                                           | 🟢 |
| O 入场方式决定离场方式 `_exit_intent`            | 硬规则：OPEN\_FIRST→LOCK / UNLOCK\_FIRST→CLOSE / LOCKED→UNLOCK                                                         | 逐行等价                                                                                          | 🟢 |
| P 锁仓落簿（LOCK→LOCKED）                    | LOCK 成交后反向仓以 `entry_mode=LOCKED` 落簿，次日对向信号自动 UNLOCK                                                                | 保留                                                                                            | 🟢 |
| Q 卡单复核闭环（F1 / 对账 / 启动首拉）               | `_check_unlock_stuck` / `_reconcile_positions` / `_restore` 首拉，全部内联在 engine.py                                     | **原样拆到新文件** **`Trading/Engine/Reconcile.py`（`ReconcileMixin`）**，行为不变                          | 🟢 |
| R 中金所 20 手上限                           | 无                                                                                                                  | **新增** `_CFFEX_LIMIT_MAX=20` 单笔硬上限防御                                                          | 🆕 |

### ⑤ Broker 适配器层 — 🟡 报单升级 FOK 为核心

| 功能                                             | 09061 做什么                                                                                           | custom-dev 最新                                                                   | 性质 |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | -- |
| S Broker 接口骨架                                  | `tg/brokers/base.py`：`submit`/`pulse`/`real_position`/`trade_confirmed`/`cancel_pending`            | `Trading/Broker/Base.py` 逐字节一致                                                  | 🟢 |
| T **报单方式**                                     | `tg/brokers/simnow.py`：普通限价 `insert_order`（无 `advanced`）+ 软件 `fill_timeout` 超时后主动 `cancel_order` 撤单 | **四类报单（OPEN/UNLOCK/LOCK/CLOSE）全加** **`advanced="FOK"`**，交易所硬保证全成或全撤，从根上消灭部分成交残留 | 🟡 |
| U 离场追价节奏                                       | 撤单后立即进入下一轮重报                                                                                        | **新增** **`chase_interval`（默认 1.0s）**：FOK 全撤后间隔再报，防 CTP 报撤频超限                    | 🆕 |
| V 超价幅度                                         | 开 / 平 `overprice_points=0.6`                                                                        | **合并为 1.0 点**；新增 `_overprice()` 帮助方法                                            | 🟡 |
| 通道能力：成交判定 P6 / 主连映射 / 连接重试心跳 / 平仓方向修复 / 实盘安全闸门 | —                                                                                                   | 全部保留（`Trading/Broker/SimNow.py` / `App/AppTrader.py` `_check_live_gate`）        | 🟢 |

### ⑥ 基础设施层 — 🔵 只动配置

| 功能           | 09061 做什么                                                          | custom-dev 最新                                                                                                                                                                 | 性质 |
| ------------ | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -- |
| W 事件日志 jsonl | `tg/events.py` 16 类事件全量落盘                                          | `Trading/Infra/EventLog.py` 逐字节一致                                                                                                                                             | 🟢 |
| X sqlite 持久化 | `tg/store.py`：positions / signal\_action / day\_stats / bars\_seen | `Trading/Infra/Store.py` 逐字节一致                                                                                                                                                | 🟢 |
| Y 公共类型       | `tg/types.py`：Signal/Order/Position/EngineState/EntryMode…         | `Trading/Infra/Types.py` 逐字节一致，未新增类型                                                                                                                                          | 🟢 |
| Z 配置         | `tg/config.py`                                                     | `Trading/Infra/Config.py`：**删** **`batch_open`/`batch_unlock`** **键**；`overprice_points` 0.6→1.0；**新增** **`chase_interval`**；`unlock_no_new_open` 默认 True；`max_volume` 默认 1→2 | 🟡 |
| AA 品种规格      | `tg/symbols.py`                                                    | `Trading/Infra/InstrumentSpec.py` 逐字节一致                                                                                                                                       | 🟢 |

***

## 三、文件映射（09061 → custom-dev 最新）

| 09061                                    | custom-dev 最新                                                 | 判定                                         |
| ---------------------------------------- | ------------------------------------------------------------- | ------------------------------------------ |
| `tg/engine.py`                           | `Trading/Engine/Engine.py` + `Trading/Engine/Reconcile.py`    | 重写（相似度约 51%）；对账拆出                          |
| `tg/position_book.py`                    | `Trading/Engine/PositionBook.py`                              | 语义等价                                       |
| —                                        | `Trading/Engine/Reconcile.py`                                 | 新增（`ReconcileMixin`，接管持仓对账 + F1 卡单复核，行为不变） |
| `tg/risk.py`                             | `Trading/Risk/RiskGate.py`                                    | 简化（去批次参数）                                  |
| `tg/sizing.py`                           | `Trading/Risk/PositionSizing.py`                              | 简化（删批次，新增 `capital_gate`/20 手默认）           |
| `tg/strategy/default_policy.py`（删）       | `Trading/Strategy/Entry.py` + `Exit.py` 的 `DefaultExitPolicy` | 重组（逻辑不变）                                   |
| `tg/strategy/layered_exit.py`（删）         | `Trading/Strategy/Exit.py` 的 `LayeredExitPolicy`              | 重组（逻辑不变）                                   |
| `tg/strategy/example_trailing.py`（删）     | `Trading/Strategy/Exit.py` 的 `TrailingExitPolicy`             | 重组（逻辑不变）                                   |
| `tg/sources/*`                           | `Trading/Source/*`                                            | 纯 import 调整                                |
| `tg/brokers/simnow.py`                   | `Trading/Broker/SimNow.py`                                    | 改（FOK + chase\_interval + 超价）              |
| `tg/config.py`                           | `Trading/Infra/Config.py`                                     | 改（参数收口点）                                   |
| `tg/store.py` / `events.py` / `types.py` | `Trading/Infra/Store.py` / `EventLog.py` / `Types.py`         | 逐字节一致                                      |
| `tg/symbols.py`                          | `Trading/Infra/InstrumentSpec.py`                             | 逐字节一致                                      |
| `trader_gateway/run_gateway.py`          | `Trading/main.py`                                             | 逻辑一致                                       |
| `App/AppTrader.py` / 根 `FrontAPI.py`     | 同                                                             | 逐字节一致（App 壳层，非 6 层）                        |

***

## 四、删除 / 新增汇总

**删除（10 处，全部是"分仓 / 批次"机制）**

* 开仓拆批：`_open_positions`、`_size_batch`、`size_batch`、`batch_open`、`batch_unlock`

* 批次解锁：`_unlock_batch_entry`、`_check_unlock_batch`、`_unlock_batch_settle`、`_reconstruct_signal`、`_unlock_batch_in_flight`

* 风控 `check_open` 的 `batch_count` / `existing_same_side` 参数

* 测试：`test_p15a_batch_open.py`、`test_p19_phase_h2.py`

**新增（4 处）**

* 恒定 FOK 报单（四类 `advanced="FOK"`）

* `chase_interval`（默认 1.0s 追价间隔）

* 中金所单笔上限 `_CFFEX_LIMIT_MAX=20`（引擎）/ `_CFFEX_SINGLE_ORDER_MAX=20`（sizing）

* 测试：`test_p15a_open_lots.py`、`test_p19_unlock_lots.py`、`test_p23_fok.py`；文件：`Reconcile.py`、`Entry.py`、`Exit.py`

**行为微调（默认值 / 语义）**

* `unlock_no_new_open`：`False` → `True`（解锁只平昨仓、不补开今仓）

* `overprice_points`：`0.6` → `1.0`

* `max_volume`（risk / sizing / RiskConfig）：`1` → `2`；sizing 未配置时兜底默认改为中金所 20 手

***

## 五、结论

1. **只动了两处**：④执行层的「入场方式」（开仓拆多笔 → 一笔挂 N 手；解锁批次 → 单笔 + 补开）+ ⑤Broker 层的「报单方式」（限价 + 软件撤单 → 恒定 FOK）。
2. **离场语义零变化**：入场方式决定离场方式 `_exit_intent`、锁仓落簿、反向仅离场、止盈止损时间结算、收盘强平、关闭锁全部、F1 卡单复核、持仓对账、启动首拉对账——全部原样（对账仅物理拆到 `Reconcile.py`）。
3. **本质是"收敛 + 升级"**：删掉"金字塔分仓 + H2 批次解锁"两条复杂路径，统一为「一信号 = 一笔 FOK 报单 = 一笔持仓」的单一模型；报单层由交易所 FOK 硬保证全成或全撤，消灭部分成交残留。
4. **风险提示**：分仓 / 批次删除后，"同 K 线金字塔分批加仓"能力随之消失（属刻意删除）；`unlock_no_new_open` 默认 True 意味着解锁不补开（规避平今高费，属有意语义取舍）。策略层、信号源、store/events/types 均无逻辑改动。

