# Trading —— 自动下单网关

把 chan.py 的**缠论买卖点信号**接到**期货柜台下单**的独立进程网关。

它只做一件事：订阅信号 → 判据 → 报单 → 记账 → 对账。所有缠论分析都在 chan.py 里完成，
本目录**不 import chan.py 任何模块**，只走 HTTP/SSE 取数（`Trading/Infra/Period.py:29`）。

> **基线**：本文所有 `文件:行` 与默认值取自 commit `7cd182da5dfebb077c232ab8fea3d38974449830`
> （`custom-dev`，2026-09-17T00:46:53Z）。已用 CRLF 归一后逐字节比对确认：工作副本
> `Trading/**` 与该 commit **102 个文件零差异**（唯一例外是本文件本身）。
> 行号会随代码漂移 —— 引用前请用 `grep -n "def <名字>"` 重新取。

---

## 一、30 秒速览

| 维度 | 事实 |
|---|---|
| 通道 | `dry_run`（离线模拟，零依赖）/ `simnow`（仿真）/ `live`（实盘 CTP），注册表 `Broker/Base.py:40` |
| 信号源 | `sse`（订阅 chan.py 实时流）/ `replay`（回放录制数据），注册表 `Source/Base.py:19` |
| 品种白名单 | **8 个**：IF/IH/IC/IM（中金所期指）、AU/AG/CU（上期所金属）、TA（郑商所 PTA）—— `Infra/Product.py:414` |
| 周期 | **4 档**：`15s` / `1m` / `5m` / `30m` —— `Infra/Period.py:53` |
| 每笔手数 | **只有一个来源**：品种执行策略表第 3 列 `EXEC_POLICY[code].lots_per_order`（`Infra/Product.py:240`） |
| 决策侧读费率吗 | **不读**。走不走平今由执行策略表第 1 列直接给定，代码只读表（`Infra/Product.py:183`） |
| 配置入口 | **只有一处**：`Trading/Config.py`。没有 `config.json`，没有 `--config`（`Trading/main.py:458`） |
| 依赖方向 | `Infra ← (Source/Strategy/Broker/Risk) ← Engine ← App`，严格单向，无反向 import（`Trading/__init__.py:5`） |
| 一次运行几份 Instrument | **只有一份**，main.py 建好后同交 Broker（写）与 Engine（读）（`Infra/Instrument.py:53`） |
| 回归测试 | `Trading/Test/` 下 **58 个独立脚本**（57 `test_*.py` + 1 `smoke_*.py`），按退出码判定 |

运行链路的第三方依赖只有 `pydantic` / `pydantic-settings`（`Trading/Config.py`）+ `tqsdk`
（只在 `simnow`/`live` 下**懒加载**，`Broker/SimNow.py:12`）；`openpyxl` 只有费率生成器与费率对账
测试用（`Tool/GenFeeTable.py`、`Test/test_product_fee_table.py`）。所以
`dry_run` + `replay` 组合可在无网络、无行情库的机器上跑通全链路。

---

## 二、目录结构

```
Trading/                                  # 自动下单网关（独立 Python 包）
├── main.py                 485 行   CLI 入口：装配 → 事件循环 → 摘要 → 收尾
├── Config.py               623 行   配置唯一总入口（pydantic 模型默认值 + .env + CLI）
├── replay_data/                     demo 回放数据（signals.json + klines.json）
├── Source/                 ① 信号源
│   ├── Base.py              49 行   Source 接口 + 注册表
│   ├── SSE.py              194 行   订阅 chan.py 实时流（零侵入）
│   └── Replay.py            88 行   离线回放 M0 录制产物
├── Strategy/               ③ 策略层（各只有一个实现，无选择器）
│   ├── Entry.py             58 行   EntryPolicy（入场）
│   └── Exit.py             383 行   LayeredExitPolicy（L1-L3 分层出场）+ ExitCheck
├── Risk/                   ④ 风控层
│   └── __init__.py          12 行   空壳：本层仅剩交割月护栏，参数在 Config.RiskConfig
├── Engine/                 ⑤ 执行层
│   ├── Engine.py          2322 行   事件驱动状态机（唯一枢纽）
│   ├── PositionBook.py     261 行   持仓簿（容器 + 查询，无决策）
│   └── Reconcile.py        382 行   持仓对账 + CLOSE 卡单复核（Mixin）
├── Broker/                 ⑥ 执行通道
│   ├── Base.py             370 行   Broker 接口 + 注册表 + 拒单分类器
│   ├── DryRun.py           100 行   离线模拟撮合
│   └── SimNow.py          1754 行   SimNow 仿真 + live 实盘（tqsdk/TqAccount）
├── Infra/                  横切·基础设施
│   ├── Records.py          407 行   Signal/Bar/Order/Position/Trade/ExitPlan + 四个枚举
│   ├── Clock.py            186 行   墙钟 / 交易日归属 / 时间戳单位 / SESSION_SECS
│   ├── Period.py           135 行   周期档案：freq↔秒 单一事实源
│   ├── Product.py          624 行   品种档案 + 执行策略表 + 费率生成区块
│   ├── Instrument.py       595 行   合约：部署配置（frozen）+ 唯一运行时对象
│   ├── StateDB.py          335 行   sqlite：幂等键 / 委托 / 成交 / kv
│   ├── TradeStats.py       256 行   成交统计（只读，供前端面板）
│   └── EventLog.py          85 行   jsonl 事件日志
├── Test/                           58 个独立回归脚本（按退出码判定）
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

回放模式**默认清空派生状态**（`Trading/main.py:245`），所以可以反复重跑同一份数据。
`replay_data/` 是**合成随机行情**，结果必然负期望，只用于验证链路与幂等 —— 别拿它下结论。

### 3.2 实时接 chan.py（dry_run，不发真实委托）

```bash
# 先在另一个终端起 chan.py API 服务（默认 127.0.0.1:18081）
python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live
```

### 3.3 接 SimNow 仿真

```bash
# 凭据只走环境变量，不落盘、不进 Config.py（Broker/SimNow.py:529）
# Windows CMD：set SN_ACCOUNT=... & set SN_PASSWORD=... & set TQ_ACCOUNT=... & set TQ_PASSWORD=...
python main.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 1m \
    --broker simnow --out ./run_sim
```

`--broker simnow` 与 `dry_run` 的差异只有两处：**真实撮合**（成交价由仿真撮合决定，不做滑点让价）
与**主连自动映射**（`KQ.m@...` 连接后用 `underlying_symbol` 解析出真实月份合约，
不用手改 `trade_symbol`，`Broker/SimNow.py:705`）。

上号前建议先跑通道探针（`Tool/SimNow/SimNowProbe.py`，只查不下单）：

```bash
python Tool/SimNow/SimNowProbe.py
```

### 3.4 从网页开自动下单

前端 K 线页右上角的开关走四个端点（`FrontAPI.py:685` 起）：

| 动作 | 端点 | 后端行为 |
|---|---|---|
| 开 | `POST /api/trader/auto-order/on` | `App/AppTrader` 拉起 `Trading/main.py` 子进程 |
| 关 | `POST /api/trader/auto-order/off` | 写 `.stop_request` → 引擎收尾（停信号 + 锁全部未锁定持仓） |
| 状态 | `GET /api/trader/auto-order/status` | 前端 5s 轮询 |
| 品种校验 | `GET /api/trader/product-check` | 未标定品种 → 开关置灰 |
| 成交 | `GET /api/trader/trades` | 成交统计面板数据源 |

跨平台停机的唯一可靠触发是 `{out}/.stop_request` **文件**——Windows 上 SIGTERM 是
`TerminateProcess`，handler 不执行（`Trading/main.py:62`）。

### 3.5 CLI 参数

| 参数 | 说明 | 定义处 |
|---|---|---|
| `--source {replay,sse}` | 信号源 | `Trading/main.py:459` |
| `--broker <名>` | 执行通道（从注册表取 choices） | `Trading/main.py:460` |
| `--replay-dir` / `--sse-base` | 数据目录 / chan.py 地址 | `Trading/main.py:461` |
| `--symbol` `--freq` | 合约与周期（`--symbol` 会同步打通到 `instrument.signal_symbol`） | `Trading/main.py:463` |
| `--bar-mode {confirmed,last}` | 只取已闭合 K 线 / 含未闭合 | `Trading/main.py:465` |
| `--speed` `--only-alive` | 回放速度 / 只回放存活信号 | `Trading/main.py:467` |
| `--out` | 状态目录（默认 `Config.state_dir` = `./State`） | `Trading/main.py:470` |
| `--fresh` / `--no-fresh` | 清 / 留派生状态（回放默认清、实盘默认留） | `Trading/main.py:471` |
| `--max-bars` | 处理 N 根 K 线后停止 | `Trading/main.py:477` |
| `--summary-json` | 摘要另存 JSON | `Trading/main.py:476` |
| `--quiet` `--echo-all` | 静默 / 连 bar 与 order 一起 echo | `Trading/main.py:478` |

### 3.6 落盘产物

运行目录（`--out`，默认 `Trading/State/`）下：

| 文件 | 内容 | 何时写 |
|---|---|---|
| `state.db` | `processed_signals` / `orders` / `trades` / `kv` 四张表（`Infra/StateDB.py:24`） | 每笔委托、成交、信号 |
| `events.jsonl` | 全量事件流水（signal / order / open / close / order_rejected / alert …） | 实时追加 |
| `gateway.log` | 引擎 stdout/stderr（行缓冲，退出后可按时间查） | 启动即重定向（`Trading/main.py:189`） |
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
（`Engine/Reconcile.py:13`）。

### 4.2 配置的两把尺子（双轴，勿混）

| 轴 | 载体 | 怎么改 | 装什么 |
|---|---|---|---|
| **消费层** | `Trading/Config.py` 的各层 `*Config` | `.env` / 环境变量 / CLI 覆盖 | 与品种、周期无关的**部署参数** |
| **变异维度** | `Infra/Period.py`（随周期变）<br>`Infra/Product.py`（随品种变） | **改文件 = 改代码资产**，走 git 评审 + 对账测试 | 跨层横切的**领域注册表** |

两轴正交，所以两张档案表**不按消费层挪进 Config.py**：以 `Product` 为例，一行供策略层
（`win_loss_ratio`）、一行供执行层（`exec_policy`）、一行供 broker 层（`price_tick`/`multiplier`），
整表没有唯一归属层（`Trading/Config.py:56`）。

**推论（调参前必读）**：品种相关项（`price_tick` / `multiplier` / `win_loss_ratio`）的真值
**只在档案里**。在 `Config.py` 或 `.env` 里写同名字段不再生效；改品种参数 = 改
`Infra/Product.py`。品种无关的出场参数（ATR / 跟踪 / `breakeven_buffer_r`）在 `Config.py`
的 `ExitConfig` 调。

### 4.3 一次运行只有一份 Instrument

`Infra/Instrument.py` 同时承载两个模型，语义完全不同：

- `InstrumentConfig`（pydantic，**`frozen=True`**，`Infra/Instrument.py:95`）—— 部署级配置，
  启动前定死：`signal_symbol` / `trade_symbol` / `slippage_ticks` / `order_advanced` /
  `closetoday_first` / 价格带与涨跌停档案位。**被写即炸**（ValidationError）。
- `Instrument`（普通类，`Infra/Instrument.py:204`）—— **唯一运行时对象**：静态身份转发 +
  运行时身份（`trade_symbol`/`last_trade_date` 行情回填）+ 有效值（`price_tick`/`multiplier`/
  涨跌停/`verified`/`source`）+ 定价与成本。

main.py 构造一份后**同时**交给 `Broker.build_broker(..., state=instr)` 与
`TradingEngine(..., state=instr)`：Broker 写（`apply_quote`）、Engine 读（闸门/对账/成本）。
必须同源 —— 否则 SimNow 置的 `verified` 引擎看不见，A′ 闸门会恒拒单且看不出原因
（`Trading/main.py:197`）。

有效值初值**直接取品种档案**（tick/乘数），离线下这就是运行值；实盘由行情
`apply_quote()` 原子覆盖（`Infra/Instrument.py:411`）。四个有效值收在不可变的 `EffectiveSpec`
里整体替换，"半新半旧"（新 tick 配旧乘数）在类型层面不可能出现（`Infra/Instrument.py:178`）。

---

## 五、配置

### 5.1 三个优先级（高 → 低）

1. **命令行参数** —— `--symbol` / `--freq` / `--broker` / `--source` …
2. **环境变量 / 仓库根 `.env`** —— 前缀 `TRADING_`，嵌套用双下划线：
   `TRADING_SOURCE__FREQ=15s`、`TRADING_RISK__DELIVERY_GUARD_DAYS=3`、`TRADING_BROKER=simnow`
3. **`Trading/Config.py` 模型字段默认值** —— 默认值的**唯一来源**，别处不再写第二套

每个 section 都是 `extra="forbid"`：传未知键、缺字段一律**启动期 fail-fast**，没有
`p.get(key, 兜底值)` 那种第二套默认值（`Trading/Config.py:88`）。

### 5.2 配置分区与真实字段

| 层 | 模型（定义行） | 关键字段（定义行 = 默认值） |
|---|---|---|
| 根配置 | `TradingConfig` `Config.py:146` | `broker="dry_run"` `:168`；`state_dir="./State"` `:169`；`instrument` `:174` |
| ① 信号源 | `SourceConfig` `Config.py:228` | `type="replay"` `:232`；`sse_base="http://127.0.0.1:18081"` `:234`；`symbol="KQ.m@CFFEX.IF"` `:235`；`freq="5m"` `:236`；`bar_mode="confirmed"` `:238`；`signal_k_tol_bars=1` `:247`；重连三参数 `:261-263` |
| ③ 入场 | `EntryConfig` `Config.py:271` | `reverse_on_opposite_signal=False` `:275` |
| ③ 出场 | `ExitConfig` `Config.py:278` | `stop_buffer_ticks=0.0` `:302`；`use_atr=True` `:304`；`atr_period=14` `:305`；`atr_sl_multiple=2.0` `:306`；`use_trailing=True` `:308`；`breakeven_trigger_r=1.0` `:309`；`breakeven_buffer_r=0.5` `:310`；`trailing_atr_multiple=1.0` `:311` |
| ④ 风控 | `RiskConfig` `Config.py:383` | `delivery_guard_days=1` `:400`（**本层没有手数旋钮**） |
| ⑥ 引擎时序 | `EngineConfig` `Config.py:460` | `close_retry_bars=5` `:471`；`close_max_streak=20` `:473`；`close_stuck_bars=5` `:475` |
| ⑥ Broker | `BrokerConfig` `Config.py:518` | `overprice_ticks=5` `:532`；`fill_timeout_open/close=5.0` `:533-534`；`close_max_chase=20` `:535`；`chase_interval=1.0` `:537`；`connect_retries=3` `:538`；`tq_market="simnow"` `:540`；`confirm_live_trading=False` `:541`；`instrument_fetch_policy="strict"` `:562` |
| ⑥a 通道时序 | `ChannelTimingConfig` `Config.py:483` | `quote_stale_seconds=30.0` `:493`；`instrument_fetch_timeout=30.0` `:509`（其余 9 项即 `Config.py:494-502`） |

出场参数有一条**构造期不变式**：`breakeven_buffer_r < breakeven_trigger_r`，
违反即抛错（缓冲 ≥ 触发时保本止损会被抬到市价之上，下一根 bar 立刻被打掉，
`Trading/Config.py:320`）。

组装 `LayeredExitPolicy` 的完整参数一律经 `resolved_exit_params(cfg)`
（品种无关项 + 档案的 `win_loss_ratio` 合并，`Trading/Config.py:593`）—— 直接传
`cfg.exit_params.model_dump()` 会缺 `win_loss_ratio`，构造期 AttributeError。

### 5.3 被删掉的配置键：三种处理方式（别当成一种）

| 键 | 处理 | 位置 |
|---|---|---|
| `risk.max_volume` / `max_open_positions` / `unlock_no_new_open` | **丢弃 + WARNING**，不阻断启动 | 清单 `Trading/Config.py:408`，判定 `:420` |
| `instrument.price_tick` / `multiplier` / `exchange` / `last_trade_date` / 三档费率键 / `instrument_verified` / `instrument_source` | **构造期 ValueError 并指路**（显式报错，不静默忽略） | `Infra/Instrument.py:80`，校验 `:157` |
| `sizing` 整节 / `RiskGate` 五道硬闸门 / `PositionSizing` | **在代码里不存在**，配置里写它同样报错 | 见附录 A |

### 5.4 品种执行策略表（8 行，代码只读不推）

`Infra/Product.py:240`。这是"今仓怎么离场 / 报单属性 / 一笔几手"三件事的**唯一事实源**
（三列含义与硬断言见 `Infra/Product.py:164` 起的表头注释）：

| 品种 | `today_exit`（今仓离场） | `order_advanced` | `lots_per_order` | 交易所（仅注释备案） |
|---|---|---|---|---|
| IF / IH / IC / IM | R-OPEN（反向开仓锁仓） | FOK | 2 | CFFEX |
| AU / AG / CU | CLOSETODAY（直接平今） | FOK | 2 | SHFE |
| TA | R-OPEN | FAK | 1 | CZCE |

构造期硬断言（`Infra/Product.py:219`）：三列取值合法；`lots_per_order` 落在 1..20；
**FAK ⟹ `lots_per_order == 1`**（否则"待报/全成/全撤"三态不变量被破坏）。

> **为什么要有这张表**（用户原话）："让程序按交易费率计算是否平今，代码逻辑复杂
> （有些品种还涉及两种交易费率）。用这张表，相当于用户基于费率已算好了是否要平今，
> 无需代码去计算，代码只需读这个表格即可。"
> 所以**决策侧零费率引用**，代码里也禁止用交易所名字判断任何走向
> （第 4 列只是注释，`Infra/Product.py:190`）。

品种档案含策略标定值 `win_loss_ratio`（IF/IH/AU/AG/CU/TA = 2.0，IC/IM = 3.0）、
`price_tick` / `multiplier`（离线兜底，实盘以行情为准）、费率两档（由生成区块注入）。
入口 `Infra/Product.py:414`，逐品种 note 里有来源说明。

### 5.5 费率：静态化 + 生成式

```
Docs/手续费标准-.xlsx
      │  Tool/GenFeeTable.py:276（--check 用于只校验不写）
      ▼
Infra/Product.py 的 GENERATED 标记区块（>>> GENERATED … <<< END GENERATED，Infra/Product.py:122）
      │  _fee_kw(code)（Infra/Product.py:354，费率进入档案的唯一入口）
      ▼
Product.open_fee / closetoday_fee → Fee.cash() → Instrument.cost_cash()（元/手）
```

- 费率在 `Product.py` 的手写部分里**一个字面量都没有**；区块内是纯数据元组，
  有人手改一个数字，`GenFeeTable.py --check` 与 `Test/test_product_fee_table.py` 立刻变红。
- 费率**只服务会计侧**（回测/记账）。"走不走平今"看执行策略表第 1 列（`Infra/Product.py:333`）。
- 成本口径统一在**元**：毛利仍是点（`gross_points`），净值 = 毛利元 − 成本元（`net_cash`）。
  `per_lot` 档（黄金 10 元/手、PTA 3 元/手）在点数口径下无法无损表达。
- 覆盖档（AU/AG 的 6、12 合约差异化费率）**字段位已留但当前不消费**
  （`Infra/Product.py:309`）—— 启用方式与量化后果写在字段注释里。

### 5.6 周期

`source.freq` 改一个字段即可切换，不支持的周期**启动期直接报错退出**，不会静默降级
（`Trading/main.py:160`）。新增周期的正确姿势：改 `FREQ_SEC` → 跑
`Test/test_period_consistency.py`（与主程序 `Common.CEnum` 对账）→ 跑
`Test/test_period_matrix.py`。

`signal_k_tol_bars`（信号归属 K 距最新 K 的容差根数，默认 1；0 = 必须最右一根）是
**按 K 线相对根数**计的，**不随周期改变**（`Trading/Config.py:240`）。

---

## 六、引擎：状态、转移、时序

### 6.1 账户三态 vs 引擎四态（正交，别混）

| 枚举 | 值 | 判据 |
|---|---|---|
| `AccountState` `Infra/Records.py:98` | `FLAT` / `LOCKED` / `RUNNING` | **唯一判据是净敞口**：净 0 且簿空 → FLAT；净 0 且簿非空 → LOCKED；净非 0 → RUNNING。判定收口在 `Engine.account_state()`（`Engine/Engine.py:295`），除它之外不得再写第二处三态判定 |
| `EngineState` `Infra/Records.py:124` | `IDLE` / `OPENING` / `IN_TRADE` / `EXITING` | 引擎**过程**状态机（含两个下单瞬态）。`IDLE` 同时覆盖 FLAT 与 LOCKED |

### 6.2 转移表（全部集中在 `_decide_action` / `_decide_exit`）

| # | 情形 | 动作 | 成交后 |
|---|---|---|---|
| ① | 空仓 + 信号 | OPEN（信号方向） | 净敞口 ≠ 0 → 运行态 |
| ② | 锁仓 + 信号 + 今仓 | OPEN（信号方向） | → 运行态 |
| ③ | 锁仓 + 信号 + 跨日仓 | CLOSE（信号方向） | → 运行态 |
| ④ | 运行 + 出场 + 今仓 | OPEN（净敞口反方向） | 净敞口 = 0 → 锁仓态 |
| ⑤ | 运行 + 出场 + 跨日仓 | CLOSE（净敞口方向） | → 锁仓态或空仓态 |

「今仓 / 跨日」判据 = 最近一笔的 `entry_date` 是否等于当前交易日。运行/锁仓态下簿内仓单
要么全是今仓、要么全是跨日仓，不可能混合（`Engine/Engine.py:29`）。
表实现：`_decide_action` `Engine/Engine.py:1015`、`_decide_exit` `Engine/Engine.py:1055`。

**CTP 报文层只认三种 offset**：`OPEN` / `CLOSE`（恒平昨）/ `CLOSETODAY`（按执行策略表第 1 列），
见 `OrderIntent` `Infra/Records.py:59` 与 `INTENT_TO_OFFSET` `Broker/Base.py:84`。
历史上曾有 `LOCK` / `UNLOCK` 两个成员，它们与 OPEN/CLOSE 在报文层**完全等价**，只是记账标签 ——
已整体删除。"这笔仓是开出来的还是锁出来的"不再是一个需要记录的属性。

### 6.3 一段运行（run）与风控锚

净敞口从 0 变非 0 的那一刻开启一段 run，用**该次成交价**作风控锚；L1-L3 只判这段 run、
不逐笔判仓单 —— 这让"空仓做多"与"锁仓做多"的风控表现天然一致。净敞口回到 0（转移 ④⑤）
时 run 结束（`Engine/Engine.py:33`）。

### 6.4 每根 K 线的处理顺序

1. **先结算已有持仓** —— 用刚闭合 K 线的 high/low 判止盈止损（`_settle_positions` `Engine/Engine.py:872`）
2. **再处理落在这根 K 线上的信号** —— 决定开仓（`on_signal` `Engine/Engine.py:914`）

反过来会变成"同一根 K 线内既开仓又平仓"，是回测里最常见的作弊来源。

两条时序防护（`Engine/Engine.py:44`）：

- **入场那根 K 线不参与出场判定** —— 结算时跳过 `bar.timestamp <= run.entry_bar_ts` 的 K 线
- **重复 / 回退的 bar 直接丢弃** —— SSE 重发或断线重连补发历史帧

### 6.5 出场：L1-L3 分层（`Strategy/Exit.py:65`）

| 层 | 干什么 | 参数 |
|---|---|---|
| L1 | R 倍数定基线 | `R = max(结构止损 A, atr_sl_multiple × ATR)` —— **取大，不设地板**；信号未带分型时 A=0，R 退化为 2×ATR |
| L2 | ATR 定宽窄 | `atr_period` / `atr_sl_multiple` |
| L3 | 保本 + 跟踪锁利 | 浮盈 ≥ `breakeven_trigger_r`×R 时把止损抬到入场价 ± `breakeven_buffer_r`×R；跟踪缓冲 = `trailing_atr_multiple`×ATR |

`use_trailing=True`（默认）时**不落硬止盈单**，止盈完全交给 L3 的跟踪兑现；L3 启动阈值 =
品种级 `win_loss_ratio`（IC/IM = 3R、其余 = 2R），故不同品种进 L3 的时机天然不同
（`Strategy/Exit.py:13`）。原 L4 时间/收盘兜底、`min_r_points` 地板、全局
`trailing_trigger_r`、`stop_at_signal_extreme` 开关均已删除。

三个刻意保留的保守设定（`Strategy/Exit.py:8`）：同根 K 线同时触及止盈与止损**按止损计**；
价格对齐一律往**对自己不利**方向取整；出场计划带参数快照落盘。

CLOSE 侧的三道运行期防护：

- `close_retry_bars`（默认 5）：CLOSE 被拒后冷却几根 bar 再试，避免每根 bar 重复报单顶监管计数
- `close_max_streak`（默认 20）：连续被拒达阈值 → 认定幻影仓，从簿中清除并**升级严重告警**（弹窗）
- `close_stuck_bars`（默认 5）：CLOSE 报单后超窗口未确认 → 二次确认复核（`Engine/Reconcile.py:283`）

三项**只作用于 CLOSE**；`shutdown_and_lock_all`（`Engine/Engine.py:1797`）豁免冷却 ——
用户当面点下的动作不等。

---

## 七、报单与柜台交互

### 7.1 报单属性 FOK / FAK

按品种给定，唯一口径 = 执行策略表第 2 列（`Instrument.effective_order_advanced()`
`Infra/Instrument.py:365`）。`Broker/SimNow.py` 只调它取生效值，**不判交易所、不判品种**。

- **入场**：全撤 → 本笔作废（rejected），不追价，等下一信号
- **离场**：全撤 → 立即按最新对手价重新超价报单，最多 `close_max_chase` 轮；轮数用尽后
  由引擎跨 K 线持续重试

### 7.2 超价与追价

SimNow 不支持市价单：下单瞬间取实时对手价（买 = ask / 卖 = bid）± `overprice_ticks`×tick
（默认 5 tick，IF = 1.0 点）主动跨价差成交；取不到行情则回退到基于信号价的对齐价
（`Broker/SimNow.py:17`）。追价轮数用尽仍不成交时，**"资金不足 / 非交易时段 / 平仓量超持仓"
三类拒单立即停追**（追 100 轮也不可能成交，只会空耗报撤单额度与监管计数）——
分类器 `classify_ctp_reject` `Broker/Base.py:130`，停追集合 `Broker/Base.py:104`。

### 7.3 A′ fail-closed 闸门

在线通道（simnow/live）必须**从行情取到并通过校验** `price_tick` / `volume_multiple` /
涨跌停区间，置 `Instrument.verified=True`（`Broker/SimNow.py:725`）；否则
`Engine._pre_trade_check` 拒单 + 严重告警（`Engine/Engine.py:1277`）。
**代码里不存在"取不到就回退配置值下单"的分支。**

三档 `instrument_fetch_policy`（`Trading/Config.py:562`）：

| 档 | 语义 |
|---|---|
| `strict`（默认） | 四字段全强制；任一缺失 → 拒单 |
| `off` | 只用配置值，**仅 dry_run/replay 离线生效**；在线通道配它照样拒单（调试开关不得绕过 A′） |
| `quote_partial` | 逃生舱（面向不走 tqsdk 的自研通道）：只强制 tick + 乘数，涨跌停取不到时**降级为不校验**并回 warn 告警 |

`apply_quote` 的原子性由结构保证：先校验全部待填值，任一不过抛错且**一个字段都不改**；
通过后四个值一次性装进新的不可变 `EffectiveSpec`（`Infra/Instrument.py:411`）。
取不到的字段 tqsdk 返回 `nan`（truthy），故判空必须用 `math.isfinite`。

### 7.4 涨跌停 + 交割月护栏

- **涨跌停**：引擎侧对**参考价**粗检（`_ref_price_out_of_band` `Engine/Engine.py:1420`），
  broker 侧对**最终限价**精确校验；区间未知（0，离线）→ 不校验未知的东西。
- **交割月护栏**：距最后交易日不足 `delivery_guard_days` 个交易日时拦截
  （`delivery_guard_blocked` `Infra/Instrument.py:385`）。三态语义：**空仓态拦开仓、
  锁仓态拦平仓、运行态不拦**；只数工作日、不计节假日；`last_trade_date` 未知 → 不拦。
  护栏对象是现行主力的 `trade_symbol` —— 换月后自动解除。

### 7.5 成交判定（P0/P3/P4/P5/P6）与对账

`Broker/SimNow.py:48` 记录了这套判定的演化，结论是：

- **权威层 = P6**：真成交 = （P3 两层）`status == "FINISHED"` 且 `volume_left == 0`
  **且** `order.trade_records` 成交量 ≥ 委托量。
- P4/P5（tqsdk position 端 delta 校验）**降级为纯诊断**，只告警不 reject ——
  tqsdk 的 position 缓存在 `insert_order` 后被乐观增减排、CTP 拒单也不回滚，
  拿它做判定会两头误判。

对账（`Engine/Reconcile.py:33`）：每侧（LONG/SHORT）独立与真实持仓比对。真实量 < 账本量 →
按 `entry_bar_seq` FIFO 部分平仓；真实量 > 账本量 → 仅告警（不自动接管未知持仓）；
全部清空 → 置 IDLE。行情陈旧（`quote_stale_seconds`，默认 30s）时 `real_position` 返回
None，对账**跳过该侧**而非误清。

> 已知盲区：该陈旧判据按"绝对时钟差"算，夜盘静默段会被恒判陈旧 → 夜盘对账被静默跳过
> （`Broker/SimNow.py:106` 有完整记录）。日盘 IF 无碍。

### 7.6 实盘安全闸门

`broker=live` 或 `tq_market != "simnow"` 时，必须显式 `confirm_live_trading=true`
（`Trading/Config.py:541`），否则启动期报错 —— 防"以为在仿真、其实在实盘"。

---

## 八、信号源

| 维度 | `replay` | `sse` |
|---|---|---|
| 定义 | `Source/Replay.py:27` | `Source/SSE.py:66` |
| 数据 | `replay_data/{signals.json,klines.json}` | chan.py `GET /api/futures/read/stream?symbol=&freq=` |
| 用途 | 离线调参、回测、参数敏感性分析 | 实盘前观察 / 实盘 |
| 幂等键 | `{date}\|{type}\|{B/S}` | 同左 |

两个源对外产出**完全相同的事件流**（`("bar", Bar)` / `("signal", Signal)`），
引擎不区分自己在跑实盘还是重放录像（`Source/Base.py:5`）。

- **K 线闭合判定** `bar_mode`：`confirmed`（默认，只取已闭合；代价是出场判定滞后一根 K 线）
  / `last`（延迟低，但最后一根可能仍在形成中）。
- **信号新鲜度**：chan.py 的 SSE 是累计推语义，首连会重放一大批历史 bsp。网关按
  "信号归属 K 距最新 K 的根数 > `signal_k_tol_bars`"丢弃历史残留（`Source/SSE.py:19`）。
- **默认回放最终消失的信号**（保守）——只回放存活信号等于开未来函数，会系统性高估策略；
  `--only-alive` 仅供对比。
- `--max-bars` 之外的正常结束**不锁仓**；只有收到停止请求才执行 `shutdown_and_lock_all`
  （`Trading/main.py:433`）。

---

## 九、成交统计与工具

### 9.1 成交统计（供前端「成交统计」面板）

`Infra/TradeStats.py` 纯计算 + 只读取，sqlite 以 `mode=ro` 打开（绝不触发 schema 迁移写操作）。
DB 路径可传多个做 union，账户无关（`trades` 表无账户列）。

**按品种键合并**：盘前在主连上跑（`KQ.m@CFFEX.IF`）、盘后在月份合约上记账（`CFFEX.IF2609`），
归一后同键 → 合并统计。归一**只有一份实现**：`Product.product_key_of`（`Infra/Product.py:551`），
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

`Trading/Test/` 下 **58 个独立脚本**：57 个 `test_*.py` + `smoke_simnow_phase_g.py`。
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
| 闸门与护栏 | `test_p42_price_from_quote` / `test_p53_delivery_guard` / `test_simnow_guards` |
| 统计 | `test_p54_trade_stats_merge` / `test_p57_trades_product_key` / `test_trade_stats_ratio_naming` |
| 术语护栏 | `test_p26_terminology_guard` |

> `test_p26_terminology_guard.py` 是**强制项**：改任何日志/注释/断言文案前先看它的禁用词表，
> 改完必须重跑。

---

## 十一、已知限制与再次交接时的注意事项

1. **换月移仓**：主连自动映射只解决"下单落到当前主力"，跨月移仓（平旧月开新月）**没有自动实现**。
   `trade_symbol` 只在连接后解析一次（`Broker/SimNow.py:627`），若主力换月且引擎重启，
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
| `Infra/Config.py`（JSON 配置加载） | **已删除**；配置入口只剩 `Trading/Config.py`，且**没有 `config.json` / `--config`**（`Trading/main.py:458`） |
| 配置示例里 `price_tick` / `multiplier` / `open_fee_rate` / `closetoday_fee_rate` / `close_fee_rate` / `slippage_ticks` / `closetoday_first` 全放在 `instrument` 段 | 前两项与三档费率键**已归位到 `Product` 档案**；写进 `instrument` 会构造期 ValueError（`Infra/Instrument.py:80`） |
| 配置示例里 `risk.max_volume` | **已删除**；手数唯一来源 = 执行策略表第 3 列（`Infra/Product.py:240`） |
| 配置示例里 `stop_at_signal_extreme` / `trailing_trigger_r` | **均删除**；R = max(A, 2×ATR) 口径唯一，L3 触发 = 品种级 `win_loss_ratio` |
| 「④ 风控层：手数/持仓上限全收敛在 RiskConfig」 | 本层现在**没有任何手数旋钮**，`max_open_positions` 也已删（`Trading/Config.py:366`） |
| 「Risk/ 风控层（手数/持仓上限）」+ `Risk.py` 内容 | `Trading/Risk/` 只剩 docstring，无代码（`Risk/__init__.py:2`） |
| `Infra/` 清单含 `Config.py`、不含 `TradeStats.py` | 实为 8 个模块，含 `TradeStats.py`（见 §二） |
| 「Test/ 下 test_p5 ~ test_p23 共 20 个回归测试」 | 实为 **58 个**（57 `test_*.py` + 1 `smoke_*.py`） |
| 「未做：创元实盘」 | `live` 通道**已实现**（`Broker/SimNow.py:1746` 的 `LiveCTPBroker`，`tq_market` 填期货公司名 + `confirm_live_trading=true`） |
| 「当前用 `CLOSE` 让交易所自动处理」平今 | 平今走 `CLOSETODAY`，是否启用 = 执行策略表第 1 列（`Infra/Records.py:59`） |
| 「出场策略：注册表 / @register / 换类名」 | 策略选择器抽象**已删**，入场固定 `EntryPolicy`、出场固定 `LayeredExitPolicy`（`Strategy/__init__.py:2`） |
| 「Reconcile：F1 解锁卡单监控」 | "解锁"概念已删；该文件现在是**持仓对账 + CLOSE 卡单复核**（`Engine/Reconcile.py:3`） |
| 统计面板「平均盈亏比」 | 改名「**盈亏比(赔率)**」= `pl_ratio`；另有「**盈利因子**」= `profit_factor`（两者算法不同，见 §9.1） |
| 工具清单只列 4 个 | 实为 11 个脚本（见 §9.2） |

**同批修掉的注释层缺陷** —— 本节成稿后已改代码，下面的行号按**修后**的树：

1. `Trading/__init__.py:8-13` 包 docstring 层表过时：已改成 `EntryPolicy` / `LayeredExitPolicy`（L1-L3）、
   `Risk/` 标为「只余交割月护栏」、`Infra/` 清单去掉已迁走的 `Config` 并补上 `TradeStats`。
   同段 `:15` 的依赖规则也从「Engine import 全部模块」改成实测口径（Infra 不 import 上层；生产代码无一处
   反向 import Engine，装配只在 `main.py`）。
2. 改名残留两处：`Trading/Config.py:35` / `:38` 与 `:515` 写成了自我循环的「原名 `BrokerConfig` … 故改为
   `BrokerConfig`」；旧名已还原为 `BrokerParamsConfig` —— 这样「与 `SourceConfig` / `RiskConfig` 的命名习惯
   不一致」那句话才讲得通（`BrokerParamsConfig` 在代码里现为 0 命中，只作沿革留档）。
3. 重复 import：`Broker/SimNow.py:83` 只留一行 `from ..Config import BrokerConfig`（删掉多余的第 84 行）。
   这是本文唯一一处**行号位移**：该文件 1755 行 → **1754 行**，全文 `SimNow.py:` 引用已同步。
4. `Trading/Config.py:19-20` 残句 + 一个已被证伪的陈述：原文「（…起配置类 frozen=True）」，但这 9 个模型
   实测 `frozen=None`（一律只有 `extra="forbid"`），已改成可核验的写法。
5. 上一轮注释清理留下的**残句 30 处**：删「阶段号 / 工单号 / 日期戳」时把句子的主语或虚词一起删掉了
   （典型：`（的根因之一）`、`出场策略起**只有**`、`（起本工具位于 Script/…）`、`价格反复报`）。已按
   「只补语法、不添新事实」逐个改回。其中 2 处是**误删语义词**：`Test/test_p37_fok_fullcancel_partial.py`
   的「第一轮的价格」指首次报单价（不是评审轮次），已恢复。
