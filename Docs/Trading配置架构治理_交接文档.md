# Trading 配置架构治理 —— 问题分析与实施交接文档

| 项 | 内容 |
|---|---|
| 创建日期 | 2026-09-14 |
| 分析基线 | `jbxenzxy/chan.py` 分支 `custom-dev`，commit `101fca5`（2026-09-14） |
| 当前状态 | **Phase 0–4 全部实施完毕**（2026-09-14）。Phase 0/1/2 已提交为 `custom-dev@b7f897b5`；Phase 3（Fix B）+ Phase 4（收尾）以补丁包形式交付、尚未提交。各阶段完成判据与实证见第五节勾选与附录 A |
| 文档目的 | ① 记录问题分析与设计决策；② 分阶段实施指南，任一阶段完成后可中断，接手人凭本文档继续 |
| 文中行号口径 | 均基于 commit `101fca5`。行号漂移后以**符号名**（类/方法/字段）为准定位 |

---

## 〇、给接手人的一句话导读

本次治理**不是**把 `Infra/` 三个文件的配置挪进 `Config.py`（该方案已被否决，理由见 1.2），而是治理三个真实病灶：**策略参数双源、InstrumentSpec 身份不清、分区轴未显式声明**。实施分 4 个 Phase，按风险从小到大排序，每个 Phase 有独立完成判据和验证命令，做完一个 Phase 提交一次即可中断。**（截至 2026-09-14，Phase 0–4 均已实施完毕；第五节的勾选即最终进度台账。）**

---

## 一、背景与问题定义

### 1.1 触发问题（用户原始疑问）

> Trading/Config.py 已按自动下单功能的架构层次留出各层配置区域；但 Infra/ 下的 InstrumentSpec.py、PeriodProfile.py、ProductProfile.py 三个文件也有很多配置。这些配置按「被架构各层消费」的角度分类，能否挪动到 Config.py 中对应区域？

### 1.2 已确认的结论（用户已认可，勿再反复）

**不挪。** 核心理由：Config.py 的分区轴是「消费层」（①信号源/③策略/④风控/⑥Broker），三张档案表的分区轴是「变异维度」（随品种/随周期），**两把尺子正交**。档案表在「按层分类」下没有唯一归属——例如 ProductProfile 一张表里：

- `min_r_points / r_multiple_tp / breakeven_buffer_ticks` → ③策略层消费（LayeredExitPolicy）
- `prefer_lock_over_closetoday` → ⑤执行层消费（Engine 转移④ / `_pre_trade_check`）
- `price_tick / multiplier` → ⑥Broker 层消费（InstrumentSpec / 报单价格口径）

一行供③、一行供⑤、一行供⑥，无法整表归入某一层。此外 `App/AppTrader.py` 只 import `Trading.Infra.ProductProfile` 的纯函数做白名单闸门（L368、L622），表若挪进 Config.py，主程序侧就得拉起整个 `TradingConfig`（pydantic_settings + .env 语义），耦合面反而变大。

### 1.3 真正的病灶（三个乱源）

| # | 病灶 | 严重度 | 对应处方 |
|---|---|---|---|
| 乱源① | **策略参数双源**：`ExitConfig` 与 `ProductProfile` 存在同名字段双默认值（min_r_points / r_multiple_tp / breakeven_buffer_ticks），Config.py 为缝合两处长出 ~70 行注入机制（model_fields_set 守护 / user-explicit-wins / force 双语义 / 换品种重载） | 高 | Fix A（Phase 2） |
| 乱源② | **InstrumentSpec 三重身份**：静态配置载体 + 运行时可变状态容器（apply_quote 回填 verified/涨跌停/费率）+ 计算服务（round_price / cost_points），三种生命周期挤在一个模型 | 高 | Fix B（Phase 3） |
| 乱源③ | **分区轴不统一但未显式声明**：Config.py 按消费层分区、档案按变异维度分区，两把尺子各自成立但关系只散落在三个文件的 docstring 里 | 低 | Fix C（Phase 1） |

---

## 二、现状盘点（事实快照，基于 commit 101fca5）

> ⚠️ **本节是分析时点的快照，不是现状。** Phase 2 已消除「品种注入机制」、Phase 3 已消除
> 「运行时状态就地改写 spec」。下方 2.2 的相关条目现仅作**问题溯源**阅读；当前实际形态见
> 第五节各 Phase 的「已完成」注，以及 `Trading/Config.py` 顶部「双轴声明」段。

### 2.1 四个文件的角色

| 文件 | 数据部分 | 行为部分 | 逻辑挂载点 |
|---|---|---|---|
| `Trading/Config.py`（560 行） | 六层分区配置模型 | env/CLI 优先级、品种注入 | 根 `TradingConfig` |
| `Trading/Infra/InstrumentSpec.py`（449 行） | 静态规格默认值 + **运行时状态**（verified / source / fee_source / 涨跌停） | apply_quote 回填、价格对齐、成本计算、护栏判定 | `TradingConfig.instrument`（L138） |
| `Trading/Infra/PeriodProfile.py`（194 行） | FREQ_SEC / PERIOD_PROFILES | 时间戳嗅探、hhmm 解析等时间工具 | `TradingConfig.period_profile` property（L156） |
| `Trading/Infra/ProductProfile.py`（257 行） | PRODUCT_PROFILES（8 品种档案：IF/IH/IC/IM/AU/AG/CU/TA） | 符号解析、白名单闸门 | `TradingConfig.product_profile` property（L236） |

**关键事实**：InstrumentSpec 本来就挂在 Config.py 的 `TradingConfig.instrument` 上——「配置纳入 Config.py」按逻辑挂载点已经成立，缺的是物理归属清晰和身份拆分。

### 2.2 关键消费点地图（grep 快照）

**品种注入机制（乱源①病灶，Phase 2 要拆的部分）**

- `Config.py` L161-L234：`_reconcile_product_profile` / `_apply_product_profile_values` / `apply_product_profile`（force 双语义）
- `Trading/main.py` L74-L76：`--symbol` 换品种后调 `cfg.apply_product_profile()` 重注入
- `Trading/main.py` L166-L167：`EntryPolicy(cfg.entry_params.model_dump())` / `LayeredExitPolicy(cfg.exit_params.model_dump())` ← **合并点将建在这里**

**运行时状态机制（乱源②病灶，Phase 3 要拆的部分）**

- `Broker/SimNow.py` L728-L969：`spec.apply_quote` / `spec.apply_fee_rates` / `instrument_verified=True` / `fee_source` 全部就地改写 spec
- `Engine/Engine.py` L1047-L1112：`_check_spec_drift`（读 `instrument_verified`、比对 PRODUCT_PROFILES）；L1128-L1133：平今经济性（读 `fee_source`）；L1172：白名单+verified 联合判定
- `Trading/main.py` L152-L165：离线路径 `spec.mark_config_offline()` + `derive_exchange` 填充；L280：启动横幅读 `cfg.instrument.instrument_verified`
- `Infra/InstrumentSpec.py` L95-L125：五个运行时字段（instrument_verified / instrument_source / fee_source / upper_limit / lower_limit）+ 三个 SOURCE_* 常量族

**直接构造 InstrumentSpec 的测试**（Phase 3 改动面）：p7/p8/p9/p10/p12/p14a/p15a/p15b/p16/p17/p20/p23/p24/p27/p28/p29/p30/p32/p33/p34/p35/p36/p37/p38/p39/p40/p42/p43/p45/p46/p51/p52/p53、test_engine_config、test_source_reconnect、smoke_simnow_phase_g、CrossDayProbe（约 36 处）

**经 exit_params 设置品种参数的测试**（Phase 2 改动面）：p27/p29/p32/p33/p34/p35/p36/p40 用 `base["exit_params"].update({"min_r_points": 3.0, ...})`；p30 用 `cfg.exit_params.min_r_points = 3.0` 属性赋值

**外部依赖（不可破坏的契约）**

- `App/AppTrader.py` L368 / L622-L630：import `assert_product_allowed` / `PRODUCT_PROFILES` / `parse_product_key`（纯函数，**不得反向依赖 Config.py**）
- `describe_unknown_product` 文案含两个被测试钉死的子串：`"拒绝启动交易引擎"`（test_p20 [9w3]）、`"禁止启动"`（test_p47 [3]）
- `parse_product` 与 `parse_product_key` 语义分工不可互换（test_period_profile L114 钉死 parse_product 保留月份）
- Trading/ 对 chan.py 主程序**零 import**（PeriodProfile docstring 明示，自持 FREQ_SEC 的原因）

---

## 三、设计原则（归属判据）

判断一项数据该放哪，问两个问题：

1. **谁改它？**
   - 运维在 .env / 命令行调（**部署资产**）→ Config.py
   - 凭交易经验标定、要进 git 评审和测试对账（**代码资产**）→ 档案文件（Infra/ 三表）
   - 行情 / 成交回报自动回填（**运行时状态**）→ 不属于任何配置，属于状态模型
2. **它随什么变？**
   - 随品种 / 周期变的**跨层横切数据** → 档案表（按变异维度归口）
   - 只被单一层消费且不随品种变 → Config.py 对应层区域

按此判据：三张表除乱源①②两处需修，其余落位正确。

---

## 四、解决方案设计

### Fix C（Phase 1）：分区轴显式化

**目标**：把「两把尺子」的关系从三个文件的 docstring 收敛为 Config.py 顶部一段明确声明，消除「散」的感受。

**改动**：仅注释/文档，零行为变化。

- Config.py 模块 docstring 增补「双轴声明」段：
  > 本文件按**消费层**分区（①③④⑥），是部署配置入口（env/CLI 可覆盖）；
  > `Infra/PeriodProfile.py`、`Infra/ProductProfile.py` 按**变异维度**分区（随周期/随品种），是领域注册表（代码资产，git 评审 + 对账测试守护）；
  > 两轴正交，档案不按层挪入本文件。`TradingConfig.period_profile / product_profile` property 是「入口聚合档案」的唯一形态。
- 三个 Infra 文件 docstring 顶部各加一行反向指引（指向 Config.py 的双轴声明）。

### Fix A（Phase 2）：策略参数单源化（消乱源①的 exit 部分）

**目标**：品种相关出场参数**只**存在于 ProductProfile；ExitConfig 只保留品种无关参数。

**设计要点**：

1. `ExitConfig` **删除三字段**：`min_r_points` / `r_multiple_tp` / `breakeven_buffer_ticks`（其余 ATR / trailing / 触发倍数等品种无关项全部保留）。
2. `ProductProfile` 新增方法 `exit_overrides() -> Dict[str, float]`：返回上述三字段（档案成为唯一默认值来源）。
3. `Config.py` 新增模块级函数 `resolved_exit_params(cfg) -> Dict[str, Any]`：**唯一合并点**——`{**cfg.exit_params.model_dump(), **cfg.product_profile.exit_overrides()}`。品种未标定时抛 `ValueError(describe_unknown_product(...))`（与引擎白名单同文案，把闸门从 Engine._restore 提前到启动期，先例：main.py 已有周期 fail-fast）。
4. `main.py` L167 改为 `exitp = LayeredExitPolicy(resolved_exit_params(cfg))`。
5. Config.py 注入机制**瘦身**（不是全删）：
   - 删除 exit 三字段相关的全部注入分支（L203-205 force 路径、L210-215 补缺路径）
   - **保留** instrument 的 `multiplier` / `price_tick` 播种（L206-207、L216-222）——这两个字段的归属在 Phase 3（拆 state）时一并重新设计，Phase 2 不动，避免两阶段改动纠缠。
   - `apply_product_profile()`（换品种 force 重载）保留但只管 instrument 两字段。
6. `product_profile` property 保留（只读视图）。

**影响与代价（已评估，接受）**：

- `TRADING_EXIT_PARAMS__MIN_R_POINTS` 等 env 覆盖将直接报错（extra=forbid）。**这是刻意的**：调参 = 改档案 = git 评审 + 对账测试，不再允许 .env 绕过（见决策记录 D1）。
- 约 10+ 个测试需迁移：`base["exit_params"].update({"min_r_points": 3.0})` / `cfg.exit_params.min_r_points = 3.0` 的写法改为「先 `resolved_exit_params(cfg)` 得 dict，再改 dict，再传 LayeredExitPolicy」。

### Fix B（Phase 3）：InstrumentSpec 拆分为「静态规格 + 运行时状态」（消乱源②，含 Fix A 遗留的 instrument 播种治理）

**目标**：静态配置（部署资产）与运行时状态（行情/回报回填）分离，恢复 Config「构造后只读」的语义。

**字段分配表**：

| 去向 | 字段 | 说明 |
|---|---|---|
| InstrumentSpec（静态，留 `TradingConfig.instrument`） | signal_symbol, trade_symbol, exchange, 三档费率默认值, slippage_ticks, order_advanced, closetoday_first, price_band_points, limit_up_pct, limit_down_pct, last_trade_date, night_session | 部署/档案资产，启动期后不变 |
| InstrumentSpec（静态离线默认） | price_tick, multiplier | **仅作离线兜底种子**；品种播种（原注入机制的 instrument 部分）改为 `InstrumentSpec.for_product(profile)` 类方法，一次性、无 model_fields_set 游戏 |
| InstrumentState（运行时，新模型） | price_tick, multiplier（**有效值**）, upper_limit, lower_limit, verified, source, fee_source, 三档费率（有效值） | 行情/成交回报回填；Engine 持有并传给 Broker |

**结构设计（定稿推荐）**：

- `InstrumentState` 为普通类（非 pydantic），**持有 spec 引用**：`InstrumentState(spec)`。构造时从 spec 播种有效值（tick/乘数/费率），之后只被行情/回报路径改写。
- 方法迁移：
  - `apply_quote` / `apply_fee_rates` / `mark_config_offline` / `mark_fee_config` / `evaluate_closetoday_economy` → **state 的方法**（改写有效值，原子性纪律不变）
  - `round_price` / `align_entry` / `align_exit` / `slip_price` / `cost_points` / `points_to_cash` → **state 的方法**（订单定价与成本是运行时语义，读 `self.spec` 的静态项 + `self` 的有效值，签名零 churn）
  - `effective_order_advanced` / `supports_closetoday` / `delivery_guard_blocked` / `is_new_day` / `derive_exchange` → **留 spec**（纯静态判定）
  - SOURCE_* 常量族 → 跟随 state
- **所有权规则**：Engine 构造 `state = InstrumentState(cfg.instrument)` 并传给 Broker；SimNow 写 state（apply_quote / apply_fee_rates），Engine 读 state（`_pre_trade_check` / `_check_spec_drift` / 平今经济性）；main.py 离线路径改为 `state.mark_config_offline()`，启动横幅读 `state.verified`。
- `Broker.build_broker` 签名相应扩展（state 引用注入），DryRun 不写 state、只读种子值。

**注意**：Phase 3 完成后，Config.py 里 `instrument` 播种的 model_fields_set 逻辑（Fix A 遗留部分）随之删除——由 `InstrumentSpec.for_product` 显式播种替代。

---

## 五、实施步骤（分阶段，可中断续做）

> **进度勾选**：接手人在每个小项完成时打勾并把本节作为唯一进度台账。
>
> 测试运行方式（本项目测试为**独立脚本、退出码判定**，非 pytest）：
> ```powershell
> # 单个：python Trading/Test/test_p50_review_fixes.py；$LASTEXITCODE -eq 0 即通过
> # 全量（在仓库根执行）：
> Get-ChildItem Trading/Test/test_*.py | ForEach-Object {
>   python $_.FullName; if ($LASTEXITCODE -ne 0) { "FAIL: $($_.Name)" }
> }
> ```
> 依赖：pydantic v2 + pydantic_settings（requirements.txt）。建议从仓库根建分支 `refactor/trading-config-architecture`。

### Phase 0：基线准备

- [x] P0.1 全量跑一遍 `Trading/Test/test_*.py`，记录基线通过清单（结果见附录 B：51 脚本，50 过 / 1 环境失败——test_p20 的 [8] 段需 tqsdk 真连 SimNow，本机未装）
- [x] P0.2 建分支 `refactor/trading-config-architecture`
- [x] P0.3 通读本文档第二节消费点地图，与最新代码核对符号是否漂移（克隆即 `101fca5`，分析期已全量 grep 核实，无漂移）

**完成判据**：基线清单落档（可追加在本文档末尾附录）。

### Phase 1：Fix C —— 双轴声明（零风险，先做）

- [x] P1.1 Config.py 模块 docstring 增补「双轴声明」段（文案见 Fix C）
- [x] P1.2 InstrumentSpec.py / PeriodProfile.py / ProductProfile.py docstring 顶部各加一行反向指引
- [x] P1.3 全量测试回归（应 100% 通过——本阶段无行为变化）

**已完成（2026-09-14）**：diff = 4 文件 32 行纯插入（零删除）；回归 51 脚本 = 基线（50 过 / 1 环境失败 test_p20，无新增失败）。变更位于工作副本 `chan-py-custom-dev/`（分支 `refactor/trading-config-architecture`，基于 `101fca5`），未推送远端、未提交 commit——后续阶段可直接在其上继续，或由用户自行 commit。

**完成判据**：`git diff` 只含注释；全量测试与基线一致。可提交、可中断。

### Phase 2：Fix A —— 策略参数单源化

- [x] P2.1 `ProductProfile.exit_overrides()` 方法（返回 min_r_points / r_multiple_tp / breakeven_buffer_ticks 三键 dict）
- [x] P2.2 `Config.py`：删除 ExitConfig 三字段；新增 `resolved_exit_params(cfg)`（未标定品种抛 `describe_unknown_product`）
- [x] P2.3 `Config.py`：注入机制瘦身——删 exit 分支（L203-205 / L210-215 部分），保留 instrument 两字段播种；`apply_product_profile` 收窄为只管 instrument
- [x] P2.4 `main.py` L167：`LayeredExitPolicy(resolved_exit_params(cfg))`
- [x] P2.5 测试迁移（清单见 2.2「经 exit_params 设置品种参数的测试」）：统一改为「`resolved_exit_params(cfg)` 得 dict → 改 dict → 传 LayeredExitPolicy」
- [x] P2.6 扫尾：`DEFAULT_CONFIG` 快照 / test_p50 中断言注入行为的用例改为断言「合并点」行为；grep `min_r_points|r_multiple_tp|breakeven_buffer_ticks` 确认 Config.py 内仅剩 resolved_exit_params 与 instrument 播种
- [x] P2.7 全量测试回归

**已完成（2026-09-14）**：落在 commit `b7f897b5`（相对分析基线 `101fca5`：修改 27 文件、新增本文档）。落地形态与设计略有出入，均为**等价实现**，记录如下：

- 三字段没有从 Config.py 里「消失」，而是下沉到新子类 `ExitPolicyParams(ExitConfig)` ——
  `ExitConfig` 只留品种无关项，品种相关三字段退化为**无档案直连场景的兜底**（如测试直接
  构造 `LayeredExitPolicy`）。`resolved_exit_params(cfg)` 合并时**档案值胜出**，故生产路径上
  `.env` 覆盖依然无效 —— D1 的目的达成。
- P2.3 的「保留 instrument 两字段播种」是本阶段的**刻意中间态**（决策 4：与 Phase 3 解耦、
  各自可回滚），已于 Phase 3.2 随 `InstrumentSpec.for_product()` 上线一并删除。

**完成判据**：`ExitConfig` 与 `ProductProfile` 无同名字段；全量测试通过。**回滚**：单 commit revert 即可（本阶段不触碰 Phase 3 的内容）。

### Phase 3：Fix B —— InstrumentSpec 拆分（风险最高，单独排期）

- [x] P3.1 `InstrumentState` 类（推荐放 InstrumentSpec.py 同文件，见决策 D2）：字段 + SOURCE_* 常量 + 持 spec 引用
- [x] P3.2 `InstrumentSpec.for_product(profile)` 显式播种类方法；main.py `--symbol` 路径改调它；删除 Config.py 的 instrument 播种 model_validator（含 model_fields_set 逻辑——至此乱源①的注入机制**全部**消失）
- [x] P3.3 方法迁移：apply_quote / apply_fee_rates / mark_* / evaluate_closetoday_economy → state；定价与成本五方法 → state；静态判定留 spec（分配表见 Fix B）
- [x] P3.4 消费方切换：SimNow.py L728-L969（写 state）；Engine.py `_check_spec_drift` / `_pre_trade_check` / 平今经济性（读 state）；main.py L152-L165 + L280（离线标记与横幅）
- [x] P3.5 删除 InstrumentSpec 上的运行时字段（verified / source / fee_source / upper_limit / lower_limit）及 `Broker/Base.py` L169 相关注释同步
- [x] P3.6 测试迁移：p42（apply_quote 语义）、p49（spec drift）、p51/p52（平今经济性）、p53（交割护栏）、smoke/CrossDayProbe 及所有直接构造 InstrumentSpec 的用例（清单见 2.2）
- [x] P3.7 全量测试回归

**已完成（2026-09-14）**：交付形态是**补丁包**（20 个源文件 overlay + git 原生 patch），
**未提交 commit** —— 因此 P3.4 设想的「2-3 个 commit」粒度未落地，回退改为整包性质（见文末回滚）。

- `InstrumentState` 与 `InstrumentSpec` **同文件**（决策 D2）：spec 为 pydantic 静态模型；
  state 为普通类、持 `spec` 引用，构造时从 spec 播种有效值，之后只被行情 / 回报路径改写。
- `InstrumentSpec.for_product(profile)` 上线后，`Config.py` 的 instrument 播种 `model_validator`
  （`_reconcile_product_profile` / `_apply_product_profile_values` / `apply_product_profile`）
  **整体删除** —— 至此乱源①的注入机制（原约 70 行）**全部**消失。启动期统一走 `main._seed_instrument()`。
- 「**一次运行只有一份 `InstrumentState`**」是本阶段的关键不变量：`main.build_runtime` 建一份，
  同时交给 `Broker`（写：行情 / 成交回报回填）与 `TradingEngine`（读：A′ 闸门 / 漂移对账 /
  平今经济性 / 成本）。不同源会让 A′ 闸门**恒拒单且看不出原因** —— 已由 test_p42 的 `[4e]` / `[5e]` 钉死。
- **有意偏离分配表的一处**：`SimNow._fill_delivery_calendar` 仍写 `spec.last_trade_date`。该字段是
  **静态元数据**（交割月日历，随合约定死），不属「行情 / 回报回填」的运行时状态，故留在 spec。
- **P3.6 清单的两点修正**：① `smoke_simnow_phase_g` / `CrossDayProbe` 经核实**无需改**
  （它们不读 `instrument_verified`）；实际迁移了 10 个测试文件。② `test_p24_close_offset.py`
  **不在 P3.6 清单内**，但它有一个与本阶段**无关**的墙钟缺陷，已于 Phase 4 修掉（见第七节坑点 11）。
- 全量回归 **51/51**；断言**净增 41 条**（p42 68→71、p50 74→110、period_profile 42→44）。
  其中 `test_p50` 新增的 `[8b]` 把「拆分完成」做成**可执行判据**：逐条钉死 spec 上无运行时字段、
  state 上无静态判定、定价五方法已迁走。

**完成判据**：InstrumentSpec 无运行时可变状态字段；`TradingConfig` 构造后只读。**回滚**：本阶段以补丁包交付（未提交 commit），故回退是**整包性质**的 —— 代码侧 `git checkout -- Trading/` 即回到 `b7f897b5`（Phase 0/1/2）。不涉及数据迁移、不改配置格式、不动仓库结构。

### Phase 4：收尾

- [x] P4.1 Trading/README.md 架构段补「双轴」说明（一两句，指向 Config.py 声明）
- [x] P4.2 全量测试 + 手工冒烟（dry_run 回放一轮）
- [x] P4.3 在本文档第六节补记实际实施中拍板的细节、追加变更日志

**已完成（2026-09-14）**：

- **P4.1**：`Trading/README.md` 架构段新增「配置的两把尺子（双轴，勿混）」小节，并同步纠正
  两处被 Phase 2/3 改动作废的表述 —— ①「出场参数直接在 `ExitConfig` 调」收窄为「品种无关项」；
  ② §四 配置样例里 `price_tick` / `multiplier` / `r_multiple_tp` / `min_r_points` 四处标注
  「品种相关，真值在档案」。另把目录树中 `InstrumentSpec.py` 的职责描述更新为
  「InstrumentSpec（静态规格）+ InstrumentState（运行时状态）」。
- **P4.2**：全量回归 **51/51 通过**，且是**在 20:00 之后**跑的 —— 这正是 Phase 3 交付时唯一
  未收敛的「夜间假红」窗口，修掉 p24 日期锚后已消除。另跑一轮 dry_run 回放冒烟：
  `python Trading/main.py --source replay --replay-dir Trading/replay_data --out <空目录>` → rc=0，
  144 根 K 线 / 7 个信号 / 6 笔成交，`events.jsonl` + `state.db` + `gateway.log` 三产物齐全。
- **P4.3**：本小节的补记 + 第六节决策记录（D1/D2/D3 转入已拍板、新增待拍板 D4）+ 附录 A 变更日志
  + 附录 B 口径补充 + 第七节坑点 11。

---

## 六、决策记录

### 已拍板（本轮分析确认，勿推翻）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 三文件配置**不**按消费层挪入 Config.py | 两轴正交，档案表无法唯一归属某一层；外部纯函数依赖面会反向耦合 |
| 2 | 病灶 = 双源 + 身份不清 + 轴未声明，处方 = 三个 Fix | 见 1.3 |
| 3 | 实施顺序 = 轴声明 → 策略单源 → spec 拆分 | 风险递增；先删 exit 注入，避免与 spec 拆分纠缠 |
| 4 | InstrumentSpec 的 instrument 播种在 Phase 2 **保留**、Phase 3 一并治理 | 两阶段解耦，各自可回滚 |
| D1 | 放弃 `.env` 覆盖品种相关三字段（min_r_points 等）的能力 | 调参 = 改档案 = git 评审 + 对账测试；`.env` 覆盖是绕过对账测试的暗门。**已实施**（Phase 2：`ExitConfig` 三字段下沉到 `ExitPolicyParams`，档案值在 `resolved_exit_params()` 中胜出）|
| D2 | `InstrumentState` 放 InstrumentSpec.py **同文件** | 合约规格与状态一族一处。**已实施**（Phase 3.1，见 `Trading/Infra/InstrumentSpec.py`）|
| D3 | `resolved_exit_params` 对未标定品种**启动期抛错** | 与 Engine 白名单同文案、闸门提前到启动期（先例：周期 fail-fast）。**已实施**（Phase 2，抛 `ValueError(describe_unknown_product(...))`）|

### 待拍板（**仅剩 1 项**；原 D1/D2/D3 已实施，见上表末三行）

| # | 问题 | 推荐 | 影响 Phase |
|---|---|---|---|
| D4 | 品种档案成为 `price_tick` / `multiplier` 的**唯一真值**（配置里显式写同名字段不再生效）？ | **接受** —— 与 D1 同纪律（调参 = 改档案 = git 评审 + 对账测试），且实盘有行情回填 + A′ 闸门兜底 | Phase 3（**已按此实施，待明示确认**）|

> **D4 与原 D1 的关系**：D4 是 D1 在 **instrument** 侧的同类决策（Phase 3.2 `for_product` 的语义）。
> Phase 3 的交付说明把它叫作「D1」，为避免与上表 D1 撞号，本文档统一编号为 **D4**。
> 若不接受 D4，退路是让 `for_product` 保留「用户显式传入值」的优先权 —— 但那样必须把 `model_fields_set`
> 判据重新引入，等于 Phase 3 只做了一半，需重新评估。

---

## 七、交接注意事项（坑清单）

1. **行号会漂移**：本文所有行号基于 `101fca5`，定位以符号名为准。
2. **测试不是 pytest**：独立脚本 + 退出码；跑全量用第五节头部的 PowerShell 循环。
3. **文案契约**：改 `describe_unknown_product` 必须同时保留子串「拒绝启动交易引擎」（test_p20）与「禁止启动」（test_p47）。
4. **不可互换的解析口径**：`parse_product`（保留月份）vs `parse_product_key`（剥月份，查档案专用）——test_period_profile L114 钉死前者行为。
5. **ProductProfile 保持纯函数模块**：App/AppTrader 只 import 它的纯函数；不得让它 import Config.py，否则主程序侧耦合面爆炸。
6. **Trading/ 对 chan.py 零 import**：这是 PeriodProfile 自持 FREQ_SEC 的原因，别「顺手统一」到 Common.CEnum。
7. **RiskConfig 的 `dropped_legacy_keys` ClassVar**：是累计记账（诊断用），判据只认局部 hits——别改回共享类变量判据（2026-09-12 修过的坑）。
8. **`apply_quote` 的原子性纪律**：先全量校验再落值、nan 判空用 `math.isfinite`（tqsdk 缺字段返回 nan 不是 None）——迁移到 state 时原样保留。
9. **Phase 2 完成后 `model_fields_set` 守护只剩 instrument 两字段**：属预期中间态，Phase 3.2 随播种类方法上线一并删除，勿在 Phase 2 提前删。
10. **对账测试是档案表的守护网**：test_p45/p46/p50（品种真值）、test_period_consistency（周期对账）——Phase 2/3 改动后必跑。
11. **测试的日期锚必须与引擎同口径（交易日，不是自然日）**：引擎 `Engine._current_trading_day()`
    在无 K 线时走 `Infra/Types.trading_day_of_ms(now_ms())`，而夜盘口径下 **>= 20:00 归属次一交易日**
    （`NIGHT_SESSION_START_HOUR = 20`）。测试若用 `datetime.now(CN).date()` 作锚，则 **20:00 之后**
    「今仓 / 昨仓」判定整体错位：`test_p24_close_offset.py` 曾因此恒红 5 条（④ 变 ⑤），
    且**白天跑永远绿** —— 属「墙钟依赖」型假绿，2026-09-14 Phase 4 已修。
    新写涉及 `entry_date` / 交易日判定的测试，一律 `_TODAY = trading_day_of_ms(now_ms())`。
    **推论（回归结论的纪律）**：**时刻**和**环境变量**一样，是必须写进报告的口径维度 ——
    凡是「全绿」的结论，先问一句「它在什么时刻、什么环境下为真」。

---

## 附录 A：变更日志（实施时追加）

| 日期 | Phase | 变更 | 执行人 |
|---|---|---|---|
| 2026-09-14 | P0.1-P0.3 | 基线测试完成并落档（见附录 B）；分支已建 | TRAE (GLM-5.3) |
| 2026-09-14 | Phase 1 | 双轴声明写入 4 个文件 docstring，回归与基线一致 | TRAE (GLM-5.3) |
| 2026-09-14 | Phase 2 | Fix A 策略参数单源化：`ProductProfile.exit_overrides()` + `Config.resolved_exit_params()` 唯一合并点；`ExitConfig` 三字段下沉到 `ExitPolicyParams`。落 commit `b7f897b5`（相对 `101fca5`：修改 27 文件、新增本文档） | （未记录；见 commit `b7f897b5`）|
| 2026-09-14 | Phase 3 | Fix B InstrumentSpec 拆分：新增 `InstrumentState` + `InstrumentSpec.for_product()`，删除 Config.py 注入 `model_validator`；20 文件 +957/−511，全量 51/51，断言净增 41 条 | WorkBuddy (Agent) |
| 2026-09-14 | Phase 4 | 收尾：修 `test_p24_close_offset.py` 交易日日期锚（消除 20:00 后假红）、`Trading/README.md` 补双轴说明、全量回归 51/51 + dry_run 回放冒烟、本文档补记 | WorkBuddy (Agent) |

## 附录 B：基线测试清单（P0.1，2026-09-14）

环境：py 3.14（pydantic 2.13.5 + pydantic_settings），Windows，commit `101fca5`，分支 `refactor/trading-config-architecture`。

**总计 51 个测试脚本：50 通过，1 失败。**

| 分类 | 数量 | 明细 |
|---|---|---|
| 通过 | 50 | test_channel_timing / test_engine_config / p5 / p6 / p7 / p8 / p9 / p10 / p12 / p14a / p15a / p15b / p16 / p17 / p21 / p23 / p24 / p26 / p27 / p28 / p29 / p30 / p32 / p33 / p34 / p35 / p36 / p37 / p38 / p39 / p40 / p41 / p42 / p43 / p44 / p45 / p46 / p47 / p49 / p50 / p51 / p52 / p53 / period_consistency / period_matrix / period_profile / simnow_guards / source_reconnect / step2_config_ssot / step2_smoke_freq |
| **存量失败（环境）** | 1 | `test_p20_phase_i1.py`：[1]-[7] 段全过，挂 [8] broker 路由段 —— `from tqsdk import ...` ModuleNotFoundError（本机未装 tqsdk，该段需真连 SimNow）。**非代码问题**；后续回归中该测试以「[8] 段前的输出 + 无新增失败」为判据 |
| 真实代码失败 | 0 | — |

> **口径补充（Phase 4 实测，2026-09-14 20:28）**：上表是 P0.1 **当时**的记录（无 tqsdk 的环境）。
> 在**装了 tqsdk 的环境**（本机 `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`，
> 含 pydantic / pandas / tqsdk）里，`test_p20_phase_i1.py` 的 [8] 段可以跑通 → 全量 **51/51 通过**；
> Phase 4 的回归即在该环境下、且**在 20:00 之后**跑出 51/51。
> 两个数字都对，差别只在**环境**与**时刻** —— 报数字时必须同时带上这两个口径。
