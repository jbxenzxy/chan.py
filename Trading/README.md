# Trading —— 自动下单网关

把 chan.py 的**缠论买卖点信号**接到**期货柜台下单**的独立进程网关。

它只做一件事：订阅信号 → 判据 → 报单 → 记账 → 对账。所有缠论分析都在 chan.py 里完成，
本目录**不 import chan.py 任何模块**，只走 HTTP/SSE 取数（约束写在模块 docstring，`Trading/Infra/Period.py:29`）。

> **基线**：本文所有 `文件:行` 与默认值取自 commit `6cd71748654ebb8a529659e1c67ca82b271894fd`
> （`custom-dev`，2026-09-22T13:19:38Z）。已逐文件 sha256 与远端比对确认：工作副本
> `Trading/**` 与该 commit 一致。
>
> ⚠️ **上一版基线写的是 `7cd182da`（2026-09-17T00:46），早就不成立了** —— 此后
> `custom-dev` 又推进了 41 个 commit，其中 2026-09-17 的**「A′ 合约参数 SSOT」**与
> **「引擎自动兜底拆除」**两次重构删掉了本文大量引用的符号（`EngineConfig` /
> `close_retry_bars` / `close_max_streak` / `close_stuck_bars` / `close_max_chase` /
> `chase_interval` / `instrument_fetch_policy` / `instrument_fetch_timeout` /
> `apply_quote` / `_ref_price_out_of_band` / `trailing_atr_multiple`），
> 其余引用则整片行号漂移。本文已按现行为逐条重取（核对脚本见交付说明）。
> **行号会随代码继续漂移 —— 引用前请用 `grep -n "def <名字>"` 重新取。**
>
> **引用约定**：`文件:行` 默认指向**可执行语句**（能 grep 到的那一行代码）；
> 少数必须指向说明文字的，正文一律写明「docstring」或「注释」，读时当线索、不当证据。

---

## 一、30 秒速览

| 维度 | 事实 |
|---|---|
| 通道 | `dry_run`（离线模拟，零依赖）/ `simnow`（仿真）/ `live`（实盘 CTP），注册表本体 `Broker/Base.py:37` |
| 信号源 | `sse`（订阅 chan.py 实时流）/ `replay`（回放录制数据），注册表 `Source/Base.py:19` |
| 品种白名单 | **8 个**：IF/IH/IC/IM（中金所期指）、AU/AG/CU（上期所金属）、TA（郑商所 PTA）—— `Infra/Product.py:438` |
| 周期 | **4 档**：`15s` / `1m` / `5m` / `30m` —— `Infra/Period.py:53` |
| 每笔手数 | **只有一个来源**：品种执行策略表第 3 列 `EXEC_POLICY[code].lots_per_order`（`Infra/Product.py:241`） |
| 决策侧读费率吗 | **不读**。走不走平今由执行策略表第 1 列直接给定，代码只读表（表本体 `Infra/Product.py:241`） |
| 配置入口 | **只有一处**：`Trading/Config.py`。没有 `config.json`，没有 `--config`（CLI 定义 `Trading/main.py:517-537`） |
| 依赖方向 | `Infra ← (Source/Strategy/Broker/Risk) ← Engine ← App`，严格单向，无反向 import（包 docstring 层表，`Trading/__init__.py:5`） |
| 一次运行几份 Instrument | **只有一份**，main.py 建好后同交 Broker（写）与 Engine（读）（docstring 所有权规则，`Infra/Instrument.py:53`） |
| 回归测试 | `Trading/Test/` 下 **64 个独立脚本**（63 `test_*.py` + 1 `smoke_*.py`），按退出码判定 |

运行链路的第三方依赖只有 `pydantic` / `pydantic-settings`（`Trading/Config.py`）+ `tqsdk`
（只在 `simnow`/`live` 下**懒加载**，函数内 `import` 见 `Broker/SimNow.py:697`）；`openpyxl` 只有费率生成器与费率对账
测试用（`Tool/GenFeeTable.py`、`Test/test_product_fee_table.py`）。所以
`dry_run` + `replay` 组合可在无网络、无行情库的机器上跑通全链路。

---

## 二、目录结构

```
Trading/                                  # 自动下单网关（独立 Python 包）
                                          # 各文件行数口径 = `wc -l`（`content.count("\n")`）
├── main.py                 543 行   CLI 入口：装配 → 事件循环 → 摘要 → 收尾
├── Config.py               562 行   配置唯一总入口（pydantic 模型默认值 + .env + CLI）
├── replay_data/                     demo 回放数据（signals.json + klines.json）
├── Source/                 ① 信号源
│   ├── Base.py              56 行   Source 接口 + 注册表
│   ├── SSE.py              215 行   订阅 chan.py 实时流（零侵入）
│   └── Replay.py            88 行   离线回放 M0 录制产物
├── Strategy/               ③ 策略层（各只有一个实现，无选择器）
│   ├── Entry.py             58 行   EntryPolicy（入场）
│   └── Exit.py             485 行   LayeredExitPolicy（L1-L3 分层出场）+ ExitCheck
├── Risk/                   ④ 风控层
│   └── __init__.py          12 行   空壳：本层仅剩交割月护栏，参数在 Config.RiskConfig
├── Engine/                 ⑤ 执行层
│   ├── Engine.py          2403 行   事件驱动状态机（唯一枢纽）
│   ├── PositionBook.py     261 行   持仓簿（容器 + 查询，无决策）
│   └── Reconcile.py        406 行   持仓对账（Mixin；原「CLOSE 卡单复核」已随兜底拆除删除）
├── Broker/                 ⑥ 执行通道
│   ├── Base.py             375 行   Broker 接口 + 注册表 + 拒单分类器
│   ├── DryRun.py           100 行   离线模拟撮合
│   └── SimNow.py          1923 行   SimNow 仿真 + live 实盘（tqsdk/TqAccount）
├── Infra/                  横切·基础设施
│   ├── Records.py          446 行   Signal/Bar/Order/Position/Trade/ExitPlan + 四个枚举
│   ├── Clock.py            186 行   墙钟 / 交易日归属 / 时间戳单位 / SESSION_SECS
│   ├── Period.py           135 行   周期档案：freq↔秒 单一事实源
│   ├── Product.py          648 行   品种档案 + 执行策略表 + 费率生成区块
│   ├── Instrument.py       515 行   合约：部署配置（frozen）+ 唯一运行时对象
│   ├── StateDB.py          335 行   sqlite：幂等键 / 委托 / 成交 / kv
│   ├── TradeStats.py       290 行   成交统计（只读，供前端面板）
│   └── EventLog.py          85 行   jsonl 事件日志
├── Test/                           64 个独立回归脚本（按退出码判定）
└── Tool/                           辅助工具（见 §9.2）
    ├── GenFeeTable.py              费率表生成器（xlsx → Product.py 生成区块）
    ├── GenReplayData.py            生成贴近当前市价的回放数据
    ├── MakeDemoData.py             生成 demo 回放数据
    ├── Recorder/                   M0 信号录制器 + 离线分析
    └── SimNow/                     SimNow 诊断 / 冒烟 / 强平 / 跨日探针
```

---

## 三、快速上手

### 3.1 离线回放（先跑这个，几秒出结果，零依赖）

```bash
cd Trading
python main.py --source replay --replay-dir ./replay_data --out ./run1
```

回放模式**默认清空派生状态**（`Trading/main.py:293`），所以可以反复重跑同一份数据。
`replay_data/` 是**合成随机行情**，结果必然负期望，只用于验证链路与幂等 —— 别拿它下结论。

### 3.2 实时接 chan.py（dry_run，不发真实委托）

```bash
# 先在另一个终端起 chan.py API 服务（默认 127.0.0.1:18081）
python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live
```

### 3.3 接 SimNow 仿真

```bash
# 凭据只走环境变量，不落盘、不进 Config.py（Broker/SimNow.py:612）
# Windows CMD：set SN_ACCOUNT=... & set SN_PASSWORD=... & set TQ_ACCOUNT=... & set TQ_PASSWORD=...
python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 1m \
    --broker simnow --out ./run_sim
```

`--broker simnow` 与 `dry_run` 的差异只有两处：**真实撮合**（成交价由仿真撮合决定，不做滑点让价）
与**主连自动映射**（`KQ.m@...` 连接后用 `underlying_symbol` 解析出真实月份合约，
不用手改 `trade_symbol`，`Broker/SimNow.py:886`）。

上号前建议先跑通道探针（`Tool/SimNow/SimNowProbe.py`，只查不下单）：

```bash
python Tool/SimNow/SimNowProbe.py
```

### 3.4 从网页开自动下单

前端 K 线页右上角的开关走**五个**端点（`FrontAPI.py:685` 起）：

| 动作 | 端点 | 后端行为 |
|---|---|---|
| 开 | `POST /api/trader/auto-order/on` | `App/AppTrader` 拉起 `Trading/main.py` 子进程 |
| 关 | `POST /api/trader/auto-order/off` | 写 `.stop_request` → 引擎收尾（停信号 + 锁全部未锁定持仓） |
| 状态 | `GET /api/trader/auto-order/status` | 前端 5s 轮询 |
| 品种校验 | `GET /api/trader/product-check` | 未标定品种 → 开关置灰 |
| 成交 | `GET /api/trader/trades` | 成交统计面板数据源 |

跨平台停机的唯一可靠触发是 `{out}/.stop_request` **文件**——Windows 上 SIGTERM 是
`TerminateProcess`，handler 不执行（改用 `_STOP_REQUEST` 握手，`Trading/main.py:67`）。

### 3.5 CLI 参数

| 参数 | 说明 | 定义处 |
|---|---|---|
| `--source {replay,sse}` | 信号源 | `Trading/main.py:517` |
| `--broker <名>` | 执行通道（从注册表取 choices） | `Trading/main.py:518` |
| `--replay-dir` / `--sse-base` | 数据目录 / chan.py 地址 | `Trading/main.py:519-520` |
| `--symbol` `--freq` | 合约与周期（`--symbol` 会同步打通到 `instrument.signal_symbol`） | `Trading/main.py:521-522` |
| `--bar-mode {confirmed,last}` | 只取已闭合 K 线 / 含未闭合 | `Trading/main.py:523` |
| `--speed` `--only-alive` | 回放速度 / 只回放存活信号 | `Trading/main.py:525-526` |
| `--out` | 状态目录（默认 `Config.state_dir` = `./State`） | `Trading/main.py:528` |
| `--fresh` / `--no-fresh` | 清 / 留派生状态（回放默认清、实盘默认留） | `Trading/main.py:529` / `:532` |
| `--max-bars` | 处理 N 根 K 线后停止 | `Trading/main.py:535` |
| `--summary-json` | 摘要另存 JSON | `Trading/main.py:534` |
| `--quiet` `--echo-all` | 静默 / 连 bar 与 order 一起 echo | `Trading/main.py:536-537` |

### 3.6 落盘产物

运行目录（`--out`，默认 `Trading/State/`）下：

| 文件 | 内容 | 何时写 |
|---|---|---|
| `state.db` | `processed_signals` / `orders` / `trades` / `kv` 四张表（`Infra/StateDB.py:24`） | 每笔委托、成交、信号 |
| `events.jsonl` | 全量事件流水（signal / order / open / close / order_rejected / alert …） | 实时追加 |
| `gateway.log` | 引擎 stdout/stderr（行缓冲，退出后可按时间查） | 启动即重定向（`Trading/main.py:248`） |
| `.stop_request` | 停机请求标志 | 由 AppTrader 写，引擎看护线程轮询 |

> **重放同一目录不会重复成交** —— 幂等键 `{date}|{type}|{B/S}` 落 `processed_signals`。
> 想换一套账本就换个 `--out`（或 `TRADING_STATE_DIR`）。

---

## 四、架构与依赖

### 4.1 分层

```
信号源 source ──bar/signal──▶ 引擎 engine ──决策──▶ 策略 policy ──▶ 风控 risk ──▶ broker
 (sse/replay)                 (四态状态机)        (LayeredExit)   (交割月护栏)   (dry_run/simnow/live)
                                       ▲                                              │
                                       └────────── Infra（Records/Clock/Period/Product/Instrument/
                                                      StateDB/TradeStats/EventLog）──┘
```

依赖方向严格单向：`Infra` 层零跨层 import；下层零反向 import。`Engine` 是唯一枢纽。
`Reconcile.py` 以 Mixin 形式挂回引擎，只依赖 Infra 数据结构与 `self` 注入的上下文
（`Engine/Reconcile.py:20`）。

### 4.2 配置的两把尺子（双轴，勿混）

| 轴 | 载体 | 怎么改 | 装什么 |
|---|---|---|---|
| **消费层** | `Trading/Config.py` 的各层 `*Config` | `.env` / 环境变量 / CLI 覆盖 | 与品种、周期无关的**部署参数** |
| **变异维度** | `Infra/Period.py`（随周期变）<br>`Infra/Product.py`（随品种变） | **改文件 = 改代码资产**，走 git 评审 + 对账测试 | 跨层横切的**领域注册表** |

两轴正交，所以两张档案表**不按消费层挪进 Config.py**：以 `Product` 为例，一行供策略层
（`win_loss_ratio`）、一行供执行层（`exec_policy`）、一行供 broker 层（`price_tick`/`multiplier`），
整表没有唯一归属层（模块 docstring 双轴说明，`Trading/Config.py:65`）。

**推论（调参前必读）**：品种相关项（`price_tick` / `multiplier` / `win_loss_ratio`）的真值
**只在档案里**。在 `Config.py` 或 `.env` 里写同名字段不再生效；改品种参数 = 改
`Infra/Product.py`。品种无关的出场参数（ATR / 跟踪 / `breakeven_buffer_r`）在 `Config.py`
的 `ExitConfig` 调。

### 4.3 一次运行只有一份 Instrument

`Infra/Instrument.py` 同时承载两个模型，语义完全不同：

- `InstrumentConfig`（pydantic，**`frozen=True`**，`Infra/Instrument.py:95`）—— 部署级配置，
  启动前定死：`signal_symbol` / `trade_symbol` / `slippage_ticks` / `order_advanced` /
  `closetoday_first` / `price_band_points`，外加三个**只留档案位、当前不消费**的
  `limit_up_pct` / `limit_down_pct` / `night_session`。**被写即炸**（ValidationError）。
- `Instrument`（普通类，`Infra/Instrument.py:200`）—— **唯一运行时对象**：静态身份转发 +
  运行时身份（`trade_symbol`/`last_trade_date` 行情回填）+ 有效值（`price_tick`/`multiplier`）
  + `verified`/`source` + 定价与成本。

main.py 构造一份后**同时**交给 `Broker.build_broker(..., state=instr)` 与
`TradingEngine(..., state=instr)`：Broker 写（`verified`/`source`）、Engine 读（闸门/对账/成本）。
必须同源 —— 否则 SimNow 置的 `verified` 引擎看不见，A′ 闸门会恒拒单且看不出原因
（`Trading/main.py:308`）。

有效值 **SSOT = 品种档案**（tick / 乘数；2026-09-17 拍板：没有任何信息需要从行情获取）——
构造期一次性播种（`Infra/Instrument.py:239`），在线与离线的运行值相同，运行期
**没有任何合法改写路径**。两个有效值收在不可变的 `EffectiveSpec`（`Infra/Instrument.py:179`）
里整体只读转发，"半新半旧"（新 tick 配旧乘数）在类型层面不可能出现。
原「实盘由行情 `apply_quote()` 原子覆盖」那条通道已随 A′ 改造**整体删除**。

---

## 五、配置

### 5.1 三个优先级（高 → 低）

1. **命令行参数** —— `--symbol` / `--freq` / `--broker` / `--source` …
2. **环境变量 / 仓库根 `.env`** —— 前缀 `TRADING_`，嵌套用双下划线：
   `TRADING_SOURCE__FREQ=15s`、`TRADING_RISK__DELIVERY_GUARD_DAYS=3`、`TRADING_BROKER=simnow`
3. **`Trading/Config.py` 模型字段默认值** —— 默认值的**唯一来源**，别处不再写第二套

每个 section 都是 `extra="forbid"`：传未知键、缺字段一律**启动期 fail-fast**，没有
`p.get(key, 兜底值)` 那种第二套默认值（每个 section 都是 `extra="forbid"`，`Trading/Config.py:228`）。

### 5.2 配置分区与真实字段

| 层 | 模型（定义行） | 关键字段（定义行 = 默认值） |
|---|---|---|
| 根配置 | `TradingConfig` `Config.py:146` | `broker="dry_run"` `:166`；`state_dir="./State"` `:167`；`instrument` `:172` |
| ① 信号源 | `SourceConfig` `Config.py:226` | `type="replay"` `:230`；`sse_base="http://127.0.0.1:18081"` `:232`；`symbol="KQ.m@CFFEX.IF"` `:233`；`freq="5m"` `:234`；`bar_mode="confirmed"` `:236`；`signal_k_tol_bars=1` `:245`；重连三参数 `:259-261` |
| ③ 入场 | `EntryConfig` `Config.py:269` | `reverse_on_opposite_signal=False` `:273` |
| ③ 出场 | `ExitConfig` `Config.py:276` | `stop_buffer_ticks=0.0` `:300`；`use_atr=True` `:303`；`atr_period=14` `:304`；`atr_sl_multiple=2.0` `:305`；`breakeven_trigger_r=1.0` `:308`；`breakeven_buffer_r=0.5` `:309`；`trailing_trigger_r=0.5` `:310`（= 跟踪缓冲倍数 ×R） |
| ④ 风控 | `RiskConfig` `Config.py:380` | `delivery_guard_days=1` `:397`（**本层没有手数旋钮**） |
| ⑥ 引擎时序 | **本层已整体删除** | 2026-09-17 拍板「例外 → 弹窗 → 用户干预，引擎不自动兜底」：冷却重试 / 连拒清幻影仓 / 卡单复核三套机制拆除，`EngineConfig`（`close_retry_bars` / `close_max_streak` / `close_stuck_bars`）与 `TradingConfig.engine` 一并删除 |
| ⑥ Broker | `BrokerConfig` `Config.py:488` | `overprice_ticks=5` `:502`；`fill_timeout_open=5.0` `:503`；`fill_timeout_close=5.0` `:504`；`chase_max_number=3` `:505`；`close_chase_ticks=2` `:508`；`connect_retries=3` `:509`；`tq_market="simnow"` `:511`；`confirm_live_trading=False` `:512`；`channel` `:518` |
| ⑥a 通道时序 | `ChannelTimingConfig` `Config.py:460` | `quote_stale_seconds=30.0` `:470`（其余 9 项即 `Config.py:471-479`） |

出场参数有一条**构造期不变式**：`breakeven_buffer_r < breakeven_trigger_r`，
违反即抛错（缓冲 ≥ 触发时保本止损会被抬到市价之上，下一根 bar 立刻被打掉，
`Trading/Config.py:317`）。

组装 `LayeredExitPolicy` 的完整参数一律经 `resolved_exit_params(cfg)`
（品种无关项 + 档案的 `win_loss_ratio` 合并，`Trading/Config.py:532`）—— 直接传
`cfg.exit_params.model_dump()` 会缺 `win_loss_ratio`，构造期 AttributeError。

### 5.3 被删掉的配置键：三种处理方式（别当成一种）

| 键 | 处理 | 位置 |
|---|---|---|
| `risk.max_volume` / `max_open_positions` / `unlock_no_new_open` | **丢弃 + WARNING**，不阻断启动 | 清单 `Trading/Config.py:405`，判定 `:417` |
| `instrument.price_tick` / `multiplier` / `exchange` / `last_trade_date` / 三档费率键 / `instrument_verified` / `instrument_source` | **构造期 ValueError 并指路**（显式报错，不静默忽略） | `Infra/Instrument.py:80`，校验 `:160` |
| `sizing` 整节 / `RiskGate` 五道硬闸门 / `PositionSizing` | **在代码里不存在**，配置里写它同样报错 | 见附录 A |

### 5.4 品种执行策略表（8 行，代码只读不推）

`Infra/Product.py:241`。这是"今仓怎么离场 / 报单属性 / 一笔几手"三件事的**唯一事实源**
（三列含义与硬断言见 `Infra/Product.py:166` 起的表头注释）：

| 品种 | `today_exit`（今仓离场） | `order_advanced` | `lots_per_order` | 交易所（仅注释备案） |
|---|---|---|---|---|
| IF / IH / IC / IM | R-OPEN（反向开仓锁仓） | FOK | 2 | CFFEX |
| AU / AG / CU | CLOSETODAY（直接平今） | FOK | 2 | SHFE |
| TA | R-OPEN | FAK | 1 | CZCE |

构造期硬断言（`Infra/Product.py:220`）：三列取值合法；`lots_per_order` 落在 1..20；
**FAK ⟹ `lots_per_order == 1`**（否则"待报/全成/全撤"三态不变量被破坏）。

> **为什么要有这张表**（用户原话）："让程序按交易费率计算是否平今，代码逻辑复杂
> （有些品种还涉及两种交易费率）。用这张表，相当于用户基于费率已算好了是否要平今，
> 无需代码去计算，代码只需读这个表格即可。"
> 所以**决策侧零费率引用**，代码里也禁止用交易所名字判断任何走向
> （第 4 列只是注释，`Infra/Product.py:191`）。

品种档案含策略标定值 `win_loss_ratio`（IF/IH/AU/AG/CU/TA = 2.0，IC/IM = 3.0）、
`price_tick` / `multiplier`（离线兜底，实盘以行情为准）、费率两档（由生成区块注入）。
入口 `Infra/Product.py:438`，逐品种 note 里有来源说明。

### 5.5 费率：静态化 + 生成式

```
Docs/手续费标准-.xlsx
      │  Tool/GenFeeTable.py:282（--check 用于只校验不写）
      ▼
Infra/Product.py 的 GENERATED 标记区块（>>> GENERATED … <<< END GENERATED，Infra/Product.py:123）
      │  _fee_kw(code)（Infra/Product.py:362，费率进入档案的唯一入口）
      ▼
Product.open_fee / closetoday_fee → Fee.cash() → Instrument.cost_cash()（元/手）
```

- 费率在 `Product.py` 的手写部分里**一个字面量都没有**；区块内是纯数据元组，
  有人手改一个数字，`GenFeeTable.py --check` 与 `Test/test_product_fee_table.py` 立刻变红。
- 费率**只服务会计侧**（回测/记账）。"走不走平今"看执行策略表第 1 列（表本体 `Infra/Product.py:241`）。
- 成本口径统一在**元**：毛利仍是点（`gross_points`），净值 = 毛利元 − 成本元（`net_cash`）。
  `per_lot` 档（黄金 10 元/手、PTA 3 元/手）在点数口径下无法无损表达。
- 覆盖档（AU/AG 的 6、12 合约差异化费率）**字段位已留但当前不消费**
  （`Infra/Product.py:317`）—— 启用方式与量化后果写在字段注释里。

### 5.6 周期

`source.freq` 改一个字段即可切换，不支持的周期**启动期直接报错退出**，不会静默降级
（`Trading/main.py:222`）。新增周期的正确姿势：改 `FREQ_SEC` → 跑
`Test/test_period_consistency.py`（与主程序 `Common.CEnum` 对账）→ 跑
`Test/test_period_matrix.py`。

`signal_k_tol_bars`（信号归属 K 距最新 K 的容差根数，默认 1；0 = 必须最右一根）是
**按 K 线相对根数**计的，**不随周期改变**（`Trading/Config.py:245`）。

---

## 六、引擎：状态、转移、时序

### 6.1 账户三态 vs 引擎四态（正交，别混）

| 枚举 | 值 | 判据 |
|---|---|---|
| `AccountState` `Infra/Records.py:130` | `FLAT` / `LOCKED` / `RUNNING` | **唯一判据是净敞口**：净 0 且簿空 → FLAT；净 0 且簿非空 → LOCKED；净非 0 → RUNNING。判定收口在 `Engine.account_state()`（`Engine/Engine.py:259`），除它之外不得再写第二处三态判定 |
| `EngineState` `Infra/Records.py:156` | `IDLE` / `OPENING` / `IN_TRADE` / `EXITING` | 引擎**过程**状态机（含两个下单瞬态）。`IDLE` 同时覆盖 FLAT 与 LOCKED |

### 6.2 转移表（全部集中在 `_decide_action` / `_decide_exit`）

| # | 情形 | 动作 | 成交后 |
|---|---|---|---|
| ① | 空仓 + 信号 | OPEN（信号方向） | 净敞口 ≠ 0 → 运行态 |
| ② | 锁仓 + 信号 + 今仓 | OPEN（信号方向） | → 运行态 |
| ③ | 锁仓 + 信号 + 跨日仓 | CLOSE（信号方向） | → 运行态 |
| ④ | 运行 + 出场 + 今仓 | OPEN（净敞口反方向） | 净敞口 = 0 → 锁仓态 |
| ⑤ | 运行 + 出场 + 跨日仓 | CLOSE（净敞口方向） | → 锁仓态或空仓态 |

「今仓 / 跨日」判据 = 最近一笔的 `entry_date` 是否等于当前交易日。运行/锁仓态下簿内仓单
要么全是今仓、要么全是跨日仓，不可能混合（模块 docstring，`Engine/Engine.py:29`）。
表实现：`_decide_action` `Engine/Engine.py:1034`、`_decide_exit` `Engine/Engine.py:1074`。

**CTP 报文层只认三种 offset**：`OPEN` / `CLOSE`（恒平昨）/ `CLOSETODAY`（按执行策略表第 1 列），
见 `OrderIntent` `Infra/Records.py:91` 与 `INTENT_TO_OFFSET` `Broker/Base.py:84`。
历史上曾有 `LOCK` / `UNLOCK` 两个成员，它们与 OPEN/CLOSE 在报文层**完全等价**，只是记账标签 ——
已整体删除。"这笔仓是开出来的还是锁出来的"不再是一个需要记录的属性。

### 6.3 一段运行（run）与风控锚

净敞口从 0 变非 0 的那一刻开启一段 run，用**该次成交价**作风控锚；L1-L3 只判这段 run、
不逐笔判仓单 —— 这让"空仓做多"与"锁仓做多"的风控表现天然一致。净敞口回到 0（转移 ④⑤）
时 run 结束（模块 docstring，`Engine/Engine.py:33`）。

### 6.4 每根 K 线的处理顺序

1. **先结算已有持仓** —— 用刚闭合 K 线判出场：**收盘价判触发、有利侧极值判达标**（`_settle_positions` `Engine/Engine.py:799`）
2. **再处理落在这根 K 线上的信号** —— 决定开仓（`on_signal` `Engine/Engine.py:874`）

反过来会变成"同一根 K 线内既开仓又平仓"，是回测里最常见的作弊来源。

两条时序防护（模块 docstring，`Engine/Engine.py:44`）：

- **入场那根 K 线不参与出场判定** —— 结算时跳过 `bar.timestamp <= run.entry_bar_ts` 的 K 线
- **重复 / 回退的 bar 直接丢弃** —— SSE 重发或断线重连补发历史帧

### 6.5 出场：L1-L3 分层（`Strategy/Exit.py:127`）

| 层 | 干什么 | 参数 |
|---|---|---|
| L1 | R 倍数定基线 | `R = max(结构止损 A, atr_sl_multiple × ATR)` —— **取大，不设地板**；信号未带分型时 A=0，R 退化为 2×ATR |
| L2 | ATR 定宽窄 | `atr_period` / `atr_sl_multiple` |
| L3 | 保本 + 跟踪锁利 | 浮盈 **>** `breakeven_trigger_r`×R 时把止损抬到入场价 ± `breakeven_buffer_r`×R；跟踪缓冲 = `trailing_trigger_r`×R |

价格线**只有一条 `stop_price`**（初始止损 → 保本 → 跟踪，逐级改写它），所以不存在
"只改了止损、漏改止盈"的可能；`plan()` 生成**零个止盈单**（返回的 `ExitPlan` 只有 `stop_price`，无止盈字段 `Strategy/Exit.py:387`），
止盈完全交给 L3 的跟踪兑现。两个"浮盈达标"阈值（进保本 / 进跟踪）与止损线一律**严格不等**
（`>` / `<`，"恰好相等"不算触发）—— 离场因此晚一格、不会提前打掉。

**每根 K 线内的判定顺序 = 先按有利侧极值抬保护价，再用本根收盘价判触发**
（`LayeredExitPolicy.check()` `Strategy/Exit.py:390`）：`check()` 先读根内极值（做多 `high` /
做空 `low`）定出**本根立即生效**的保护价，再拿本根收盘价比它 —— 达标那根若收盘已落在**新**
保护价的不利侧，**当根即离场**（成交参考价 = 该根收盘价，`fill_price`）。旧顺序（触发判据在
前、达标命中即只更新计划、新保护价最快下一根生效）已废弃。代价是"冲高回落"形态里下车更早、
更容易被一根长上影打掉；收益是不再承担"达标根收盘 → 下一根收盘"之间的漂移 —— 两边优劣
取决于达标根之后那根的收盘分布，不是"更早锁利"这么单向。两段判据的**先后**由护栏
`Test/test_p61_exit_close_only.py` 用 AST 行号钉死（`_fav_extreme()` 调用必须早于触发比较）。

L3 启动阈值 = 品种级 `win_loss_ratio`（IC/IM = 3R、其余 = 2R），故不同品种进 L3 的时机
天然不同（`Strategy/Exit.py:457-458`）。原 L4 时间/收盘兜底、`min_r_points` 地板、
`stop_at_signal_extreme` 开关均已删除。**`trailing_trigger_r` 仍在**（`Config.py:310`），
但角色已从"L3 触发阈值"换成"跟踪缓冲倍数"（× R，与 `breakeven_*_r` 同单位）。

两个刻意保留的保守设定（docstring 两条，`Strategy/Exit.py:56`）：价格对齐一律往**对自己不利**方向取整；
出场计划带参数快照落盘。（原第三项「同根 K 线同时触及止盈与止损按止损计」已随
"触发判据只读收盘价"的口径**变得不可达** —— 一个收盘价不可能既 > 止盈线又 < 止损线 ——
规则一并删除。）

CLOSE 侧**没有自动兜底**（2026-09-17 拍板：例外 → 弹窗 → 用户干预）：

- 离场追价跑满 `chase_max_number`（默认 3）轮仍未成交 → **severe 告警**（前端阻塞弹窗）
  转人工；引擎还会跨 K 线持续重试，实际效果是"直到成交"（`Engine/Engine.py:2228-2231`）
- 原「冷却重试 `close_retry_bars` / 连拒清幻影仓 `close_max_streak` / 卡单二次确认
  `close_stuck_bars`」三套自动兜底**已整体删除**，原 `EngineConfig` 随之删除
- 唯一还在的"连续"计数是 `_reject_streak`（连续被前置校验拦下、同一原因口径），
  它只负责把告警级别从 warn 提到 severe，**不触发任何清仓动作**（`Engine/Engine.py:2319-2322`）

---

## 七、报单与柜台交互

### 7.1 报单属性 FOK / FAK

按品种给定，唯一口径 = 执行策略表第 2 列（`Instrument.effective_order_advanced()`
`Infra/Instrument.py:336`）。`Broker/SimNow.py` 只调它取生效值，**不判交易所、不判品种**。

- **入场**：全撤 → 本笔作废（rejected），不追价，等下一信号
- **离场**：全撤 → 立即按最新对手价重新超价报单，最多 `chase_max_number` 轮（默认 3）；
  轮数用尽后由引擎跨 K 线持续重试

### 7.2 超价与追价

SimNow 不支持市价单：下单瞬间取实时对手价（买 = ask / 卖 = bid）± `overprice_ticks`×tick
（默认 5 tick，IF = 1.0 点）主动跨价差成交；取不到行情则回退到基于信号价的对齐价
（`Broker/SimNow.py:1080`）。追价轮数用尽仍不成交时，**"资金不足 / 非交易时段 / 平仓量超持仓"
三类拒单立即停追**（追 100 轮也不可能成交，只会空耗报撤单额度与监管计数）——
分类器 `classify_ctp_reject` `Broker/Base.py:130`，停追集合 `Broker/Base.py:104`。

### 7.3 A′ fail-closed 闸门

在线通道（simnow/live）必须**连接成功**才放行：`SimNow._connect` 成功即置
`Instrument.verified=True`（`Broker/SimNow.py:767`，`source=CONFIG`）；否则
`Engine._pre_trade_check` 拒单 + 严重告警（`Engine/Engine.py:1288`）。
**闸门只有一个判据**（在线通道连通与否）—— 因为**合约参数 SSOT = 品种档案**：
tick / 乘数构造期从 `Product` 播种，没有任何信息需要从行情取，所以
"取不到就回退配置值下单"这种分支根本不存在。

2026-09-17 的 A′ 改造把下面三件旧事**整体删除**（旧文档常照抄，别再用）：

| 旧（已不存在） | 现在 |
|---|---|
| 三档 `instrument_fetch_policy`（`strict` / `off` / `quote_partial`） | **键已删除**；只有"通道是否连通"一个判据 |
| 行情回填合约参数 `apply_quote` + `EffectiveSpec` 原子替换 | **通道整体删除**；有效值构造期播种，运行期无改写路径（`Infra/Instrument.py:239`） |
| 涨跌停区间校验（引擎参考价粗检 + broker 最终限价精检） | **整体删除** —— 报出必然被废的价格由交易所拒单 + 软件侧弹窗，用户手工干预 |

`InstrumentConfig` 的 `limit_up_pct` / `limit_down_pct` / `night_session` 三个**档案位
仍在、当前不消费**（`Infra/Instrument.py:129`）；报单用的 `price_band_points` 一期的
唯一合法值是 `0`（不限制）。

### 7.4 涨跌停 + 交割月护栏

- **涨跌停**：机制**已整体删除**（2026-09-17）—— 报出必然被废的价格由交易所拒单 +
  软件侧弹窗人工干预；`InstrumentConfig` 的 `limit_up_pct` / `limit_down_pct` 只留作
  档案记录（`Infra/Instrument.py:129`）。
- **交割月护栏**：距最后交易日不足 `delivery_guard_days` 个交易日时拦截
  （`delivery_guard_blocked` `Infra/Instrument.py:356`）。三态语义：**空仓态拦开仓、
  锁仓态拦平仓、运行态不拦**；只数工作日、不计节假日；`last_trade_date` 未知 → 不拦。
  护栏对象是现行主力的 `trade_symbol` —— 换月后自动解除。

### 7.5 成交判定（P0/P3/P4/P5/P6）与对账

`Broker/SimNow.py:48` 记录了这套判定的演化，结论是：

- **权威层 = P6**：真成交 = （P3 两层）`status == "FINISHED"` 且 `volume_left == 0`
  **且** `order.trade_records` 成交量 ≥ 委托量。
- P4/P5（tqsdk position 端 delta 校验）**降级为纯诊断**，只告警不 reject ——
  tqsdk 的 position 缓存在 `insert_order` 后被乐观增减排、CTP 拒单也不回滚，
  拿它做判定会两头误判。

对账（`Engine/Reconcile.py:29`）：每侧（LONG/SHORT）独立与真实持仓比对。真实量 < 账本量 →
按 `entry_bar_seq` FIFO 部分平仓；真实量 > 账本量 → 仅告警（不自动接管未知持仓）；
全部清空 → 置 IDLE。行情陈旧（`quote_stale_seconds`，默认 30s）时 `real_position` 返回
None，对账**跳过该侧**而非误清。

> 已知盲区：该陈旧判据按"绝对时钟差"算，夜盘静默段会被恒判陈旧 → 夜盘对账被静默跳过
> （`Broker/SimNow.py:107` 有完整记录）。日盘 IF 无碍。

### 7.6 实盘安全闸门

`broker=live` 或 `tq_market != "simnow"` 时，必须显式 `confirm_live_trading=true`
（`Trading/Config.py:512`），否则启动期报错 —— 防"以为在仿真、其实在实盘"。

---

## 八、信号源

| 维度 | `replay` | `sse` |
|---|---|---|
| 定义 | `Source/Replay.py:27` | `Source/SSE.py:66` |
| 数据 | `replay_data/{signals.json,klines.json}` | chan.py `GET /api/futures/read/stream?symbol=&freq=` |
| 用途 | 离线调参、回测、参数敏感性分析 | 实盘前观察 / 实盘 |
| 幂等键 | `{date}\|{type}\|{B/S}` | 同左 |

两个源对外产出**完全相同的事件流**（`("bar", Bar)` / `("signal", Signal)`），
引擎不区分自己在跑实盘还是重放录像（模块 docstring，`Source/Base.py:5`）。

- **K 线闭合判定** `bar_mode`：`confirmed`（默认，只取已闭合；代价是出场判定滞后一根 K 线）
  / `last`（延迟低，但最后一根可能仍在形成中）。
- **信号新鲜度**：chan.py 的 SSE 是累计推语义，首连会重放一大批历史 bsp。网关按
  "信号归属 K 距最新 K 的根数 > `signal_k_tol_bars`"丢弃历史残留（`Source/SSE.py:154`）。
- **默认回放最终消失的信号**（保守）——只回放存活信号等于开未来函数，会系统性高估策略；
  `--only-alive` 仅供对比。
- `--max-bars` 之外的正常结束**不锁仓**；只有收到停止请求才执行 `shutdown_and_lock_all`
  （`Trading/main.py:493`）。

---

## 九、成交统计与工具

### 9.1 成交统计（供前端「成交统计」面板）

`Infra/TradeStats.py` 纯计算 + 只读取，sqlite 以 `mode=ro` 打开（绝不触发 schema 迁移写操作）。
DB 路径可传多个做 union，账户无关（`trades` 表无账户列）。

**按品种键合并**：盘前在主连上跑（`KQ.m@CFFEX.IF`）、盘后在月份合约上记账（`CFFEX.IF2609`），
归一后同键 → 合并统计。归一**只有一份实现**：`Product.product_key_of`（`Infra/Product.py:575`），
写入侧落在 `trades.product_key` 列（`Infra/StateDB.py:184`），查询侧比**列相等**。

口径（两个都出现，别混；面板上 `pl_ratio` 的标签写作「盈亏比(赔率)」以免读串）：

| 指标 | 中文 | 算法 |
|---|---|---|
| `pl_ratio` | **盈亏比(赔率)** | 平均每笔盈利 ÷ \|平均每笔亏损\|；无亏损 → None |
| `profit_factor` | **盈利因子** | 总盈利 ÷ \|总亏损\|；无亏损 → None（无定义，非 0） |

其余字段：`count` / `wins` / `losses` / `flat` / `win_rate` / `avg_win` / `avg_loss` /
`total_net` / `max_win` / `max_loss` / `expectancy` / `equity_curve` / `by_reason`
（`Infra/TradeStats.py:146`）。

### 9.2 工具

| 工具 | 干什么 |
|---|---|
| `Tool/GenFeeTable.py` | 从 `Docs/手续费标准-.xlsx` 刷新 `Product.py` 的 GENERATED 费率区块；`--check` 只校验不写 |
| `Tool/GenReplayData.py` | 生成贴近当前市价的回放数据（v2：信号价 = K 线 close） |
| `Tool/MakeDemoData.py` | 生成 demo 回放数据（格式与 M0 录制器一致） |
| `Tool/Recorder/SignalRecorder.py` | M0 信号录制器：订阅 SSE 录制买卖点生命周期 + K 线，零侵入 |
| `Tool/Recorder/Analyze.py` | M0 分析器：重绘率统计 + 止盈止损参数回测 |
| `Tool/SimNow/SimNowProbe.py` | M2a 通道探针：登录 / 资金 / 主连映射 /（可选）下单 |
| `Tool/SimNow/SimNowSmoke.py` | M2b 真成交冒烟：成交 → 持仓 → 平仓完整闭环 |
| `Tool/SimNow/SimNowDiag.py` | 账户诊断：资金 / 持仓 / 未成交委托 /（可选）一键平仓 |
| `Tool/SimNow/SimNowForceClose.py` | 不依赖引擎的独立强制平仓工具（市价单受限时的替代路径） |
| `Tool/SimNow/CrossDayProbe.py` | 跨日仓构造 + 平昨探针（第一套环境才有日切） |
| `Tool/SimNow/ProbeAfterClose.py` | 收盘后可交易性探测（登录 / 行情 / 报单 / 成交明细） |

---

## 十、测试

`Trading/Test/` 下 **64 个独立脚本**：63 个 `test_*.py` + `smoke_simnow_phase_g.py`。
每个脚本自己 `print` 结果并以退出码判定（0 通过 / 1 失败），可直接逐个跑：

```bash
python Trading/Test/test_product_fee_table.py     # 费率三方一致（区块 ⇄ 档案 ⇄ xlsx）
python Trading/Test/test_period_consistency.py    # 周期表与主程序对账
python Trading/Test/test_p47_product_case_whitelist.py
python Trading/Test/smoke_simnow_phase_g.py       # 需要真实 SimNow 凭据 + 网络
```

覆盖面（按主题归类，非穷举）：

| 主题 | 代表脚本 |
|---|---|
| 状态机与转移 | `test_p10` / `test_p33_state_machine_table` / `test_p34_scene_x_y_equiv` |
| 持仓簿与三态 | `test_p12_position_book` / `test_p32_net_exposure_ssot` / `test_p35_today_never_close` |
| 报单填充与 offset | `test_p23_fok` / `test_p37_fok_fullcancel_partial` / `test_p41_intent_offset_two` / `test_p51_closetoday_switch` |
| 产品 / 费率 / 白名单 | `test_p45_metal_products` / `test_p46_pta_product` / `test_p47` / `test_p55_product_ssot` / `test_product_fee_table` |
| 闸门与护栏 | `test_instrument_spec_ssot` / `test_p53_delivery_guard` / `test_simnow_guards` |
| 统计 | `test_p54_trade_stats_merge` / `test_p57_trades_product_key` / `test_trade_stats_ratio_naming` |
| 术语护栏 | `test_p26_terminology_guard` |

> `test_p26_terminology_guard.py` 是**强制项**：改任何日志/注释/断言文案前先看它的禁用词表，
> 改完必须重跑。

---

## 十一、已知限制与再次交接时的注意事项

1. **换月移仓**：主连自动映射只解决"下单落到当前主力"，跨月移仓（平旧月开新月）**没有自动实现**。
   `trade_symbol` 只在连接后解析一次（`Broker/SimNow.py:886`），若主力换月且引擎重启，
   旧月份持仓不会被加载进簿（`_restore` 只加载 `symbol == trade_symbol` 的持仓）。
2. **钱层面的兜底闸门缺失**：风控层现在只有交割月护栏。资金不足靠柜台拒单兜底
   （`REJECT_FUNDS` 会立即停追），没有事前保证金校验。实盘前需要一次性决策。
3. **测试套件**：`Test/` 与生产代码同批演进，无 CI 门禁。改动后请全量跑一遍
   （含 `smoke_simnow_phase_g.py` 需凭据）。
4. **夜盘对账**：见 §7.5 盲区。
5. **SimNow 环境限制**（不是 bug）：7x24 环境**无日切** → 造不出真正的昨仓、测不了平昨；
   仿真**不支持市价单**（代码用贴近市价的限价单模拟立即成交）。

---

## 附录 A：本文更正的旧错（口径留痕）

上一版 `Trading/README.md` 已整体过时，本次重写。下面列出**具体错在哪**，避免下一个人照抄旧文：

| 旧文写的 | 事实 |
|---|---|
| `Infra/Config.py`（JSON 配置加载） | **已删除**；配置入口只剩 `Trading/Config.py`，且**没有 `config.json` / `--config`**（CLI 定义 `Trading/main.py:517-537`） |
| 配置示例里 `price_tick` / `multiplier` / `open_fee_rate` / `closetoday_fee_rate` / `close_fee_rate` / `slippage_ticks` / `closetoday_first` 全放在 `instrument` 段 | 前两项与三档费率键**已归位到 `Product` 档案**；写进 `instrument` 会构造期 ValueError（`Infra/Instrument.py:80`） |
| 配置示例里 `risk.max_volume` | **已删除**；手数唯一来源 = 执行策略表第 3 列（`Infra/Product.py:241`） |
| 配置示例里 `stop_at_signal_extreme` | **已删除**；R = max(A, 2×ATR) 口径唯一，L3 触发 = 品种级 `win_loss_ratio`。⚠️ `trailing_trigger_r` **没删**（`Config.py:310`），它改了角色：跟踪缓冲倍数（×R），不再是 L3 触发阈值 |
| 「④ 风控层：手数/持仓上限全收敛在 RiskConfig」 | 本层现在**没有任何手数旋钮**，`max_open_positions` 也已删（已删键清单 `Trading/Config.py:405`） |
| 「Risk/ 风控层（手数/持仓上限）」+ `Risk.py` 内容 | `Trading/Risk/` 只剩 docstring，无代码（`Risk/__init__.py:2`） |
| `Infra/` 清单含 `Config.py`、不含 `TradeStats.py` | 实为 8 个模块，含 `TradeStats.py`（见 §二） |
| 「Test/ 下 test_p5 ~ test_p23 共 20 个回归测试」 | 实为 **64 个**（63 `test_*.py` + 1 `smoke_*.py`） |
| 「未做：创元实盘」 | `live` 通道**已实现**（`Broker/SimNow.py:1915` 的 `LiveCTPBroker`，`tq_market` 填期货公司名 + `confirm_live_trading=true`） |
| 「当前用 `CLOSE` 让交易所自动处理」平今 | 平今走 `CLOSETODAY`，是否启用 = 执行策略表第 1 列（成员定义 `Infra/Records.py:127`） |
| 「出场策略：注册表 / @register / 换类名」 | 策略选择器抽象**已删**，入场固定 `EntryPolicy`、出场固定 `LayeredExitPolicy`（`Strategy/__init__.py:10-16`） |
| 「Reconcile：F1 解锁卡单监控」 | "解锁"概念已删，**CLOSE 卡单复核也已随兜底拆除删除**；该文件现在只有**持仓对账**一项职能（模块 docstring，`Engine/Reconcile.py:3`） |
| 统计面板「平均盈亏比」 | 改名「**盈亏比(赔率)**」= `pl_ratio`；另有「**盈利因子**」= `profit_factor`（两者算法不同，见 §9.1） |
| 工具清单只列 4 个 | 实为 11 个脚本（见 §9.2） |

**同批修掉的注释层缺陷** —— 本节成稿后已改代码，下面的行号按**修后**的树：

1. `Trading/__init__.py:7-13` 包 docstring 层表过时：已改成 `EntryPolicy` / `LayeredExitPolicy`（L1-L3）、
   `Risk/` 标为「只余交割月护栏」、`Infra/` 清单去掉已迁走的 `Config` 并补上 `TradeStats`。
   同段 docstring `:15` 的依赖规则也从「Engine import 全部模块」改成实测口径（Infra 不 import 上层；生产代码无一处
   反向 import Engine，装配只在 `main.py`）。
   ⚠️ 当时该 docstring 里还留着两处「已删机制的词」未清（`:8` 的「入场过滤」指信号质量过滤、
   `:10` 的「对账+F1」指 F1 解锁卡单复核）；**两处均已在附录 B「追补」一节清掉**，同段 docstring 现已与代码一致。
2. 改名残留两处（均在 docstring）：`Trading/Config.py:36-38` 与 `:485` 写成了自我循环的「原名 `BrokerConfig` … 故改为
   `BrokerConfig`」；旧名已还原为 `BrokerParamsConfig` —— 这样「与 `SourceConfig` / `RiskConfig` 的命名习惯
   不一致」那句话才讲得通（`BrokerParamsConfig` 在代码里现为 0 命中，只作沿革留档）。
3. 重复 import：`Broker/SimNow.py:84` 只留一行 `from ..Config import BrokerConfig`。
4. `Trading/Config.py:19-20`（docstring）残句 + 一个已被证伪的陈述：原文「（…起配置类 frozen=True）」，但这 9 个模型
   实测 `frozen=None`（一律只有 `extra="forbid"`），已改成可核验的写法。
5. 上一轮注释清理留下的**残句 30 处**：删「阶段号 / 工单号 / 日期戳」时把句子的主语或虚词一起删掉了
   （典型：`（的根因之一）`、`出场策略起**只有**`、`（起本工具位于 Script/…）`、`价格反复报`）。已按
   「只补语法、不添新事实」逐个改回。其中 2 处是**误删语义词**：`Test/test_p37_fok_fullcancel_partial.py`
   的「第一轮的价格」指首次报单价（不是评审轮次），已恢复。

---

## 附录 B：本轮刷新记录（2026-09-22 第二次刷新）

上一版基线是 `7cd182da`（2026-09-17T00:46）。此后 `custom-dev` 推进到
`6cd71748654ebb8a529659e1c67ca82b271894fd`（2026-09-22T13:19:38Z，**41 个 commit**），
本文因此整体失真。本轮**逐条重取**，改动分四类：

| 类 | 数量 | 说明 |
|---|---|---|
| 行号漂移 | 60 余处 `文件:行` | 全部按现行代码重取；`Config` 章因 `use_trailing` 字段删除整体 −1 |
| 已删机制（语义） | 5 段 | `EngineConfig` 三套自动兜底、§7.3 的 A′ 三档取值、§7.4 涨跌停、§4.3 的 `apply_quote` 通道、§6.5 的"三道防护" |
| 数值 / 计数 | 20 处 | §二 目录行数（13 个文件变了）、测试脚本数 58 → **64**、§3.4「四个端点」→ **五个** |
| 引用证据性 | 30 处引用 | 原引用有相当一部分落在**注释 / docstring** 上（注释不能当证据）。其中 **17 处**改指真实可执行行（如 `Exit.py:342` 注释 → `:361` 的 `return`、`SSE.py:19` 注释 → `:154` 的 `if dist_bars > …`），**13 处**目标确实只能是说明文字的，在正文写明「docstring」或「注释」 |

**核实方法**（可复跑，三个脚本都在沙盒 `internal/`）：

- `verify_readme_citations_v3.py` —— **五道核验**：① 行号越界/文件解析（双前缀、歧义单报）；② 锚点符号必须出现在目标行（锚点须与引用紧邻）；③ `文件:行` 后紧跟的「…」引文必须出现在目标行（专抓"行号写错、内容对得上"）；④ 弱关联（无锚点引用用行内标识符兜底）；⑤ **目标行必须是可执行语句** —— 落在注释/docstring 上的，正文没写明就报错。
- `scan_readme_symbols.py` —— 把本文出现的代码符号逐个在全仓生产代码里做**带边界**搜索，0 命中即判定"已删或从未存在"。
- `check_readme_structure.py` —— 编码/BOM/行尾/结尾换行/图片/外链/栅栏配平/表格列数/反引号内竖线/`§` 与「附录」交叉引用目标。

### 追补（本文成稿后）：包 docstring 里的两处「已删机制词残留」

`Trading/__init__.py:7-13` 的层表在附录 A 第 1 条已改过一轮，但同一段 docstring 里还留着两处
「已删机制的词」，本次一并清掉（该 docstring 仍 18 行，故 `Trading/__init__.py:7-13` / `:15` / `:10` 三个引用照旧有效）：

| 位置 | 原 | 现 | 依据（可核） |
|---|---|---|---|
| `Trading/__init__.py:8` | `EntryPolicy 入场过滤` | `EntryPolicy 入场` | 信号质量过滤（振幅 / 止损距离上下限）已移除（`Strategy/Entry.py:45-46` 注释）；`decide()` 只剩 OPEN / CLOSE_AND_REVERSE / CLOSE_AND_HOLD / SKIP（`Strategy/Entry.py:39-58`），无任何过滤判据。本文 §二 目录树亦作「EntryPolicy（入场）」 |
| `Trading/__init__.py:10` | `Reconcile 对账+F1` | `Reconcile 持仓对账` | F1「解锁卡单复核」已随兜底拆除整体删除；该文件现存方法只有 `_reconcile_position` / `_reconcile_positions` / `_mirror_note` / `_reconcile_empty_side` / `_reconcile_side`（`Engine/Reconcile.py:22-220`），模块 docstring 标题即「对账」 |

术语护栏 `Test/test_p26_terminology_guard.py` 复跑通过（新增字符串无禁用词）。

### 追补（时序改版）：出场判定改为「先抬价、再判触发」

2026-09-22 用户拍板改 `LayeredExitPolicy.check()` 的判定顺序：先按根内有利侧极值抬高保护价，
再用**本根**收盘价判它有没有被跌破 —— 达标那根若收盘已落在新保护价的不利侧，**当根即离场**
（原来要等下一根）。同轮连带：

- `Engine/Engine.py:839` 把"计划落盘 / 阶段 toast / `exit_plan_update` 事件"从 `only_update`
  分支提为**公共路径**（当根既抬价又离场时，"因进保本 / 进跟踪而离场"的因果链不能断）。
- 护栏 `Test/test_p61_exit_close_only.py` 整轮反转：新增 AST 行号断言（`_fav_extreme` 调用行
  必须早于触发比较行）、[6]/[7] 两组改为"当根离场"期望、[5] 的启动判据从 `only_update`
  换成计划里的 `_phase`（新时序下"启动"的那几根本根就离场，`only_update` 会整片假红）。
- 本文随之改动：§6.5 新增「判定顺序」段；§三 时序第 1 步的"用刚闭合 K 线的 `high`/`low`
  判止盈止损"改为"收盘价判触发、有利侧极值判达标"（旧写法是"触发只读收盘价"口径落地前的
  残留）；`Engine/Engine.py` 的 6 处引用因该文件 **+18 行**整体重取，`Strategy/Exit.py` 的
  4 处引用因该文件 **+33 行**整体重取。

终态（本文 + 追补全部引用）：①②③⑤ 失配 **0**（共 169 条）；④ 的启发式提示已逐条人工核对，**全部为误报** —— README 行内写的是取值/语义，不是被引行定义的名字。

**已知残余（未动，均属 Docs 层，需另行裁决）**：

1. `Docs/出场判定口径与1R播报_交付说明_20260922.html:249` 仍写「只对固定止盈单模式 / 旧库存量
   持仓生效」——固定止盈单已于同日整条删除（见 §6.5）。该文是「交付说明」性质的**历史快照**，
   是否按现行为刷新或加"截至该轮"戳记，需你裁决。

**已清（2026-09-23）**：`Docs/止盈止损/止盈止损四层策略原理.html` 的 `:1160` / `:1167` /
`:1332` / `:1333` / `:1334` 就地标注完毕 —— `use_trailing`（随固定止盈单整条删除）与
`trailing_atr_multiple`（现役字段为 `trailing_trigger_r` × R）都不再被写成现役开关，
文首另加了一条口径戳记；本文残余清单随之由 2 条收敛为 1 条。
