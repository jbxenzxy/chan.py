# memory-chanpy.md —— chan.py 专项：出场口径 / 测试环境 / 术语护栏 / 命名 / 硬规则

> **【读法】** 本文件不常驻上下文（子文件不自动注入）。命中 `~/.workbuddy/MEMORY.md` 索引表主题词后 →
> 按本表定位节号 → **只 Read 该节**（用 `###` 子节标题当 `offset`/`limit` 锚点，别整份读）；同会话已读过的节不重读。
> **`§N` 编号被 MEMORY.md 铁律 5.5 / 11 引用，不要重编号。**
>
> | 命中什么 | 读 |
> |---|---|
> | 出场 / 止盈止损 / R 倍数 / 保本 / 跟踪 / reason / tp 已删 / 两套价 / 判定时序 | **§0** |
> | 跑测试、门禁 vs 发现式、凭据、readonly 缺口、注入套路、耗时基线 | **§1** |
> | 禁用词（腿 / leg / 双仓…）、改文件前必过 p26 | **§2** |
> | 命名（新建文件 / 目录、改名交付） | **§3** |
> | 单行设计法律（交易所名 / 费率 / Infra / 配置键 / 引擎状态 / D3…） | **§4** |
> | Trading/README 行号引用、核验三件套、Docs 失真残余 | **§5** |
>
> **【体检】** 2026-09-23 重整：**18,185 → ~11K 字符**（去重 + 节内重排成 `###` 子节 + 顶部导航 + 过程证据移档）；
> 上游：09-15 自 MEMORY.md 拆分 → 09-17 首次瘦身（32,752→~3K）。
> **再瘦信号**：聊 chan.py 时出现「细节记不清、要回翻归档」，或单个 `###` 子节 > 1,500 字符。
> **移档**：本次移出的过程证据（过期条数 / 耗时实测、已失效口径、被取代的旧状态描述、备查差异）**一字未删**，
> 全文在 `memory-chanpy.full-2026-09-23.md`（按关键字 Grep 之）；更早存档 `…full-2026-09-17.md`、
> `MEMORY.full-2026-09-15.md`。项目当前状态（基线 / SimNow / runner / 待补）以工作区 MEMORY.md 为准。

## 0. 出场策略口径：触发看收盘价 / 达标看极值 / **先抬价再判触发**

**分层与 R**（`Trading/Strategy/Exit.py`）：L1 结构 R 定基线 / L2 ATR 定宽窄 / L3 保本 + 跟踪锁利；**无 L4、无第二套
出场策略**。`R = max(A, B)`：A = 分型极值距离（多 `entry − fractal_low` / 空 `fractal_high − entry`，≤0 钳 0）；
B = `atr_sl_multiple(2.0) × ATR(14)`；**无绝对点数地板**（`min_r_points` 已删）。

**⚠️ 三个最易踩的口径**：
1. **两套价分工（设计如此，别「顺手统一」）**：**触发判据**（止损 / 保本 / 跟踪价是否被穿）**只读 `bar.close`**；
   **达标判据**（浮盈够不够 1R 或 `win_loss_ratio×R`、跟踪锚「至今最好价」）读**根内有利侧极值**——多 `bar.high` /
   空 `bar.low`，且**只经 `_fav_extreme()`**（全文件唯一读极值入口）。
2. **边界一律严格不等**（2026-09-22 拍板「按需求原文 ＜ / ＞」）：`close < stop`（多）/ `close > stop`（空）、达标用
   `fav_profit > 阈值`。「收盘恰好 == 保护价」「极值恰好 == k×R」都**不算**触发 / 达标 —— 等号在 tick 粒度**可达**，
   改后整体晚一格离场；**止损侧无例外**。
3. **判定顺序 = 先抬价、再判触发**（2026-09-22 拍板，推翻同日早先「同根绝不二次判定」）：`check()` 里 L3 达标
   （调 `_fav_extreme()`）排在触发比较**之前**；抬价后局部 `stop` 被覆盖为**新**保护价，紧接着用本根 `close` 判是否
   跌破；达标那根若 `close` 落在新保护价不利侧 → **当根即离场**（`fill_price = close`）**且同时带出 plan**
   （`ExitCheck.plan` 非空、`only_update=False`）。配套：`Engine._settle_positions` 已把「计划落盘 / 阶段 toast /
   `exit_plan_update` 事件」从 `only_update` 分支提为**公共路径**，否则「因进保本 / 跟踪而离场」的因果链会断
   （事件流只剩一条离场、看不出为何在当前价走）。
   - 影响 = **达标根常在设定当根即走**（冲高回落形态下车更早、更易被长上影打掉），换来「不再承担达标根收盘 →
     下一根收盘的漂移」。**纯时序互换、无单向优劣**，优劣要靠「达标根之后那根 close 的分布」定。
   - 另一条路（触发改读本根 low/high）**不可行**：跟踪价来自本根 high ⇒ `low < high − trail_dist` 退化成
     「本根振幅 > trail_dist 就离场」；且与「收盘才认、不被插针扫」冲突。

**跟踪距离**（2026-09-22 晚变更，易记反）：`trail_dist = trailing_trigger_r × R`（`Config.py` 默认 **0.5**），
**取代**旧的 `trailing_atr_multiple(1.0) × ATR` 与 ATR 缺失时的 `trailing_distance_points` 兜底。两个后果：
① R ≥ 2×ATR ⇒ 新距离恒 **≥** 旧距离（回吐更多）；② 旧实现在「ATR 未就绪」窗口（每日开盘前 atr_period+1 根）
**跟踪整层失效**（兜底点数为 0），新实现照常按 0.5R 抬价。
⚠️ 名字叫 `trailing_trigger_r` 但语义是**距离不是触发**（触发阈值 = `win_loss_ratio`）；`Exit.py` docstring 里
「已删 trailing_trigger_r」那句是**陈旧自述**，别当现役口径。

**`tp` 固定止盈单已整条删除**（2026-09-22 拍板「删干净」—— 留任一片段，「只有 L1–L3」在代码里就还会再次失真）：
`use_trailing`、`ExitPlan.tp_price`、`check()` 的 tp 分支与 reason `"tp"`、引擎 3 处落盘键、`AppTrader` 的 `"tp"`
投影、前端「止盈 ×」tooltip、10 个测试文件 18 处 `use_trailing=False`，全删。
- **删除后的现状口径（比删除动作更重要）**：**价格线只有一条 `stop_price`**（初始止损 → 保本 → 跟踪逐级改写它，
  故不存在「只改止损漏改止盈」）；**`ExitPlan` 只剩 `name` / `stop_price` / `params`**；reason 只可能
  `breakeven` / `trailing` / `sl`。
- 残留扫描（9 组关键字全仓穷举）只剩两处**非残留**：`Exit.py` 模块 docstring 的「已删除」史注、
  `Trading/Tool/Recorder/Analyze.py` 的独立 `--take-profit` 静态回测参数（与自动下单出场链无关）。
- 钉子：p8 [8b]（`ExitPlan.to_dict()` 键恰为 `["name","params","stop_price"]`）、p61 [8]、p60 [5]。

**reason 只记「规则身份」、不表达盈亏**（2026-09-22 拍板）：`ExitCheck.reason` 按 `plan.params["_phase"]` 细分
`breakeven` / `trailing` / `sl`（未进 L3、或旧库存量持仓缺 `_phase` 时也退到 `sl`）；保本的名义愿望是止盈、被滑点
打成净亏的也有 ⇒ 盈亏一律由 `net_cash` 符号派生。`TradeStats.summarize()["by_reason"]` 与
`Engine.summary()["by_reason"]` **同名同形** = `{规则: {n, wins, losses, flat, net}}`（组内三分口径与顶层一致；
前端面板不读它）。`Engine._notify_close` 文案**只按 `reason` 路由**（不再回读 `_phase`，那是同一件事的第二份副本）；
`Engine.summary()` 顶层 `losses` 由 `<= 0` 改 `< 0` 并补 `flat`（净额恰为 0 的笔原被算作亏损，与 TradeStats 分叉）。
- 护栏：p61 [8] / p60 [5a-2][5b-2][5d-2]（落盘 `Trade.reason` 端到端）/ `test_trade_stats_formulas.py [I]`（分组键 =
  规则身份、组内胜负平、**非恒真**证明 = 同组内正负笔并存）/ `test_aol_ledger_display.py`（`ExitCheck("sl",…)` 字面量
  已改变量 `stop_reason`，**计数是 3** —— 多 / 空触发两支 + ③ `only_update` 支，并合取「无一处写死字面量」；
  `== 3` 别改回 `== 2`）。
- **同根的巨阴**（极值达标 + 收盘破旧止损）：reason = **抬价后的那一层**（`breakeven` / `trailing`），不再记 `sl`
  —— reason 只说「哪层保护价被跌破」，不说盈亏。p61 [6] 钉死。

**护栏与用例分工**：`test_p61_exit_close_only.py` 用 AST 钉死 `check()` 里**不得**直接出现 `.high`/`.low`，豁免只剩
`_HILO_WHITELIST = {"_atr", "_fav_extreme"}`（在两个豁免函数外读 high/low 立刻翻红）。p61 各条守：[1] 源码白名单 +
**时序先后**（AST 断言 `_fav_extreme` 调用行 < 触发比较行）/ [3] 触发侧行为 / [4] 达标侧行为 / [5] 启动阈值边界
（判据 = `plan._phase`，**不是 `only_update`** —— 新时序下「启动」那几根本根就离场）/ [6] 当根离场 / [7] 达标根当根
判触发（[7a] close 在不利侧 → 当根离场；[7b] 有利侧 → 只更新计划）；`test_p8_layered_exit.py` 的 [12]/[12b] **不再放
本节**（仅注释说明）。`Exit.py` 的 `bars_held` 形参收了但**从不使用** ⇒ 确认无时间止损；L4 已删、无收盘强平 ⇒
**价格型离场是唯一出口**，持仓可能过夜（转移⑤ 平昨）。

**核对结论（2026-09-22，基线 `ce12a039`）**：用户拍板的口径（1R = max(分型距离, 2ATR)、收盘价触发、极值达标、
1R 保本 0.5R、2R|3R 跟踪 0.5R、实际成交价记账）**代码已完整实现**；A1（R = max(A,B) 合成方式）与 A3（早盘 ATR
未就绪）经用户裁定**不是问题、忽略**。

⚠️ **`Docs/出场判定口径与1R播报_交付说明_20260922.html` 是改前旧口径**（整层只读 close）：严格不等边界、reason
细分 / by_reason、tp 整条删除**均未同步**，需要时另行授权刷新。失真行（基线 `ce12a039`）：`:65-67` / `:85` / `:235` /
`:253-254` / `:283` / `:301`（口径）+ `:547`（Exit.py 行号已漂移，实际 `:251-252` / `:256-257`）。

## 1. 测试环境（跑测试前必读）

### 1.1 跑法（唯一正确姿势）
- 解释器 `C:\my_chan_project\.venv\Scripts\python.exe`；项目 `.venv` 是 **Python 3.14.7**（base=Python314），**不是
  3.13** ⇒ 评估「预编译 wheel 覆盖」（vnpy_ctp / ctp-python 等）**必须按 cp314 算**（第三方 C 扩展覆盖基本为零）。
- **Windows 风格 `PYTHONPATH`**（`C:\...`；git-bash 的 `/c/...` Python 认不出）+ **cwd = 仓库根 / 树根**。用沙盒
  python ⇒ 大量 `No module named` **假 FAIL**。
- **脚本式**，不是 pytest：逐个 `python Trading/Test/test_XX.py`（内部 `sys.exit(1)`）。
- 跑完全量会生成运行时产物（`Trading/State/*`、`__pycache__`、report）→ **打包 / 比对前必须清**。

### 1.2 两条入口：门禁 = 判决器 / 发现式 = 清点器
- **门禁 `Test/run_all.py`**：白名单 `COMPONENTS`（`("名", [命令])`，**顺序即执行顺序**，**现 120 条**）—— 答「这批
  改动能不能合」，红 = **阻断合并**；宁可牺牲覆盖率只收判得准的 ⇒ 给 CI 的一行命令。
- **发现式 `run_all_tests.py`**：扫 `TEST_ROOTS=("Test","Trading/Test")` × `NAME_PREFIXES=("test_","repro_","smoke_")`
  —— 答「哪些测试被遗忘了」，红 = **记账排查、不拦合并**；宁可多跑误报 ⇒ 给人做现场排查。
- **互为差集，不是包含**：① 门禁有 **4 个组件发现式永远扫不到**（名字不带前缀）：`gen_fixtures.py` /
  `snapshot_runner.py` / `func_map_check.py` / `_mutate_p3.py`；② 反向 **`Trading/Test` 下 60+ 条交易域用例只在发现式
  跑**（门禁只注册 3 条交易域用例：`trade_stats_formulas` / `aol_ledger_display` / `p61_exit_close_only`）⇒ **改交易域
  代码时只看门禁会漏**。
- **发现式不能当门禁**：docstring「注意 3」自列 5 类**红 ≠ 回归**（`repro_n2` 退出 1 = 命中 / `repro_n4` 恒 0 /
  `smoke_simnow_phase_g` 凭据被隔离必红 / p20·p60 凭据齐全会真登录 / `test_user_store_rmw` 耗时 5.5~62s 波动）；
  且**故意不做**冻结基线（注意 1），`--update` 只在门禁。
- ⇒「发现式覆盖全、何必还要门禁」是**伪问题**：覆盖集合互有差集，**目标函数相反**（门禁查准 / 发现式查全）；
  **只有跑两条、看差集才能发现漏注册**（p61 与 62 个交易域用例即如此查出）。
- 仓库**无任何 CI 配置**（无 `.github` / Makefile / pytest.ini），门禁就是那一条命令。

### 1.3 何时跑哪个（2026-09-23 拍板；用户不必记，他说「验一下」即按下述全跑）
- **改完 `Trading/` 任何代码 → 必跑门禁**：`…\.venv\Scripts\python.exe Test/run_all.py`（cwd = 仓库根）。
  **退出码 0/1 就是判决**，明细读 `Test/report.json`。单条红**先单独重跑该脚本**再定性（phase7 / scanpool 的偶发
  假红已于 2026-09-23 修掉，见 §1.5；其余按「单独重跑可绿 = 偶发」处理）。
  - ⚠️ **沙盒环境假红**：本会话往子进程注入了批量删除拦截（`CODEBUDDY_SAFE_DELETE_*` + shim 目录在 `PYTHONPATH`），
    清理自己几千个临时 fixture 的用例（`app_amo_behavior` / `lock_completeness` / `aol_ledger_display`）会 rc=1，
    输出有 `[safe-delete]` 字样。判据与清环境重跑命令见 `memory-env.md` 首条。
- **四种时刻跑发现式**（同一解释器 + `run_all_tests.py`）：① 新写完 / 改完一个测试文件（先确认绿且稳定，再决定是否
  注册进门禁）② 拉远端 / 大合并之后（资产盘点：有没有人加了测试忘注册）③ Phase 收尾 / 交付前（全量体检）
  ④ 门禁某条红了要现场定位（`--only Test --filter <关键字> --stream --stall 10 --diag`，这套设施门禁没有）。
  `--list` 看清单，`--filter` 搜关键字。
- **小改动后不要跑发现式**（慢 + 红点不全是真红 + 真信号被淹）。
- **新测试进门禁 = 手动往 `COMPONENTS` 加一行**，**无任何自动机制**。够格 = 退出码语义干净 / 不联网不依赖凭据 /
  耗时稳定；不够格的按 n2 / n4 写法在末尾注明「暂不注册 + 原因」（现存三条：`smoke_phase7` / `smoke_simnow_phase_g` /
  `repro_n4`）。
- ⚠️ **重任务不要并发启动**：两轮门禁会**交错写同一个日志文件**（日志自相矛盾）并抢 CPU 污染计时 ⇒ 逐项数据一律以
  `Test/report.json`（或 `--report` 的 json）为准，**别读被重定向的 stdout 日志**。
- **耗时基线**：全量 ≈ **9~10 分钟**；最慢 `p3_mutation_gate` 39~64s（变异测试，波动最大）、`p20_phase_i1` 17~79s、
  `p60` 曾偶发 173.4s（网络抖动，非稳定差异）。

### 1.4 门禁与凭据：按组件清凭据（2026-09-23「C 方案」已落地）
- **两份入口凭据策略相反是设计**：门禁默认 `env = dict(os.environ)`（**保留凭据**）；发现式 `build_env` pop 掉 6 个
  凭据键（`SN_` / `TQ_` / `LIVE_` 各一对）。
- 本机 `SN_*` / `TQ_*` 4 个键**均有值** ⇒ 不处理时 `test_p20_phase_i1` 与 `test_p60_trade_toasts_reconcile_gap`
  **在门禁里真启动 tqsdk 连接模块**（末尾可见 `Task cancelling ... TqModule._up_handle`），耗时被网络抖动放大
  （同机各 2 次对照：**p60 21.74/19.17s → 清空 4.09/4.09s（5.2×）**；**p20 16.48/18.87s → 7.38/6.69s（2.5×）**）。
- **落地形态**：`Test/run_all.py` 新增 `CREDENTIAL_ENV_KEYS`（6 键）+ `CLEAN_CREDENTIALS_COMPONENTS`（登记 p20 /
  p60），`run_component` 对登记名改用**剔除凭据的环境副本**（不就地改调用方那份 env）；自证护栏
  `Test/test_gate_credential_isolation.py`（两份常量一致 / 登记名在册 / 脚本存在 / 行为三断言）已注册。实测 p20
  16.5→**6.2s**、p60 21.7→**3.5s**，`Task cancelling` 联机痕迹消失 ⇒ **全量 120/120 rc=0**。
- ⚠️ **条数台账 55 → 118 → 119 → 120**；`run_all.py` / `run_all_tests.py` 里**写死的组件数要人工 grep 同步**
  （不同步没人会红）。

### 1.5 `readonly database` 结果缺口（2026-09-23 已修）
- **一个根因，不是两个问题**（签名实测一致）：`test_phase7_guards.py` ⑧（seq=1、2 落库失败 → `completed=1 != 3`、
  `结果行 seq 不连续: [0]`）、`test_scanpool_fallback.py` ③（`completed 1 != 2`）、手工工具 `smoke_phase7.py` 同源。
- **代码路径**：`AppScanPool._worker_scan_one` 对 `put_result` 的 except **有意吞掉**（业务结果照常返回）⇒ future
  **不抛异常** ⇒ 收割线程兜底只挂「future 抛异常」这一条分支 ⇒ 缺口永不补齐；`AppScanStore._execute` 又只对
  `"locked"` 重试、`readonly` 直接抛。后果：`completed` 停在 total 之下（进度悬挂）+ **该票静默消失**（不进结果、
  也不进跳过汇总 —— 比进度数字更严重）。
- **修法 = 收割时补齐缺口行**：`_monitor_task` 用 futures 登记的 seq 减 `store.existing_seqs(task_id)`
  （`AppScanStore` 新增只读方法），缺哪票补哪票（error ⇒ 经 `iter_error_rows` 进跳过汇总，失败因此可见；中止语义
  补 aborted 行）；插入位置在「错误明细并入跳过记录」**之前**。
- **禁止改成「按 future 结束数记 completed」**：`test_phase7_guards.py::test_store_contract` 断言 `ScanStore.get_task`
  必须含 `SELECT COUNT(*) FROM scan_results`（W2 单一事实源），⑧ 还断言 `sorted(seq) == list(range(total))` —— 换计数
  来源会同时踩这两条已注册护栏。
- **护栏**：`Test/test_scanpool_result_gap.py`（故障注入、确定性、5 项断言；摘掉修复段 → 3 项红 ⇒ 有拦截力）；
  端到端 A/B 用 §1.6 注入套路：同一注入下两组件**无修复 rc=1、带修复 rc=0**。

### 1.6 复现 / 验证脚本的硬坑与取证套路
- ① 读 `events.jsonl` 前**必须 `eng.ev.flush()`**（EventLog 1s/64 条批量刷，直读会以为「事件没写」）。
- ② 复现**对账类**缺口必须让证据门先「见过」该侧：先跑一轮读数一致的 prime（`_mirror_note` 会同时初始化
  `_mirror_max_seen` 与 `_mirror_last`）；**直接预置 `_mirror_max_seen` 会因 `_mirror_last` 未初始化 AttributeError**
  （`Reconcile.py:174`）。
- ③ dry_run 撮合**含 1 tick 滑点** ⇒ 离场价断言用 `o.filled_price`（不是 ref_price），费率断言同理按 fill 价算。
- ④ 复现「run 恢复」必须用 **`eng._persist()`**，不能用 `_persist_run()`（后者只落 run 那条 KV、**持仓簿不落** ⇒
  新引擎走「有 run、无敞口 → 清残留」分支把 run 清掉，字段变空串，像「值真丢了」的假象）。
- 另：`Order` 没有 `.intent`，判意图读 `o.meta["intent"]`。
- **跨进程（spawn worker）确定性注入**：`sitecustomize.py` 在本机**不会被导入**（别走这条路）；改注入到
  **`App/AppLog.py` 末尾**（`AppScanPool` 模块级 import 它，spawn worker 必定执行）；判别「我是 worker」**只能用**
  `"from multiprocessing.spawn import spawn_main" in " ".join(sys.orig_argv)`（worker 的 `sys.argv[0]` 被改写成测试
  脚本的绝对路径，`sys.argv[:1] == ["-c"]` 与「argv 含 spawn_main」两种直觉写法都会**静默失效**）；注入后**先写
  追踪文件确认生效再下结论**。
- **给「快照采集路径」补行为覆盖**（2026-09-19）：复用 `Test/snapshot_runner` 三件套 —— `install_data_source(fixture)`
  （打桩 `CTdxAPI.fetch_main_level/fetch_sub_level`）+ `isolate_side_effects()`（清缓存 / 置空窗口 / 选点隔离）+
  `_seed_reference()`（name / PE / 归属 / 减持），`Test/test_trigger_step_replay.py` 即此范式。两条纪律：① **窗口等
  可变配置由用例自己写死**，不读 AppConfig 默认值（否则随宿主机漂移 = 假红）；② `_seed_reference()` 必须调，否则
  `_get_reduction_flag` 会真连 7615 网关（联网）。
- **前端展示类需求要走到「真函数」层**（2026-09-23，p62 定型）：静态断言（字符串在不在）**证不了**渲染结果。手法 =
  用正则把 `Frontend/app.js` 里的渲染函数**连同它的格式化依赖**（如 `fmtPx`）抽出来，喂一个 stub 元素到 `node` 里
  跑真函数，逐样本比对 `textContent / className / style.display / title`。范式见 `Test/test_aol_ledger_display.py`
  §5/§6 与 `Trading/Test/test_p62_ao_protection_price.py` §10。
  ⚠️ stub 要**预置哨兵值**（如 `title: 'STALE'`），否则"该清没清"的残留分支测不出来；harness 读回的字段必须先
  归一（`x === undefined ? null : x`），否则 JS `undefined` 会被 `JSON.stringify` 丢掉、Python 侧 `KeyError` 中断用例。
- **子进程架构下同一份视图有【两处投影】，改一处必同步另一处**（2026-09-23，p62 实测差点交付半残）：
  引擎 `Engine.auto_order_status()` 的 `run` 与 API 侧 `App/AppTrader._read_engine_switch()` 的 `run_view` **各写一份**
  （API 侧刻意不 import 引擎：进程托管层不与状态机耦合），前端 `GET /api/trader/auto-order/status` 走的是**后者**。
  ⇒ 只改引擎侧 ⇒ 前端拿到的新字段恒 `undefined`（静默半残，静态断言全绿）。
  **护栏写法**：① 两处 dict 字面量的**键集合**用 AST 断言逐字相等；② 取值来源用 AST 取 `x.get('K')` 的常量键名
  （断言是 `_phase` / `R` / `_tp_nominal`，**别用子串匹配** —— `str(prm.get("_phase") or "")` 会让子串法失效）；
  ③ **真实 `state.db` 上两处逐字段相等**（引擎写库 → 关连接 → 调 API 侧函数 → 比对初始/保本/跟踪三个时点）。
- **读投影字段一律 `.get()` + 数值包装**（2026-09-23 踩）：用例里写 `rv["phase"]`，一旦该键被删，整条用例在第一个
  断言处 **`KeyError` 中断**，后面 40 项一条都看不到（门禁只显示一个 Traceback）。正确：缺键返回 `None`、
  数值读不到返回 `nan`（比较恒 False → 报红），让每一项都能独立报红。
- **新写的测试不会自动进门禁**：必须在 `Test/run_all.py` 的 `COMPONENTS` 手加一行（`("名字", [sys.executable,
  os.path.join("Trading","Test","test_xxx.py")])`）。加完顺手 grep 同步 `run_all_tests.py` docstring 里写死的组件数。
- ⚠️ **`fixtures_integrity` 在 Windows 工作树上恒红（预存在，2026-09-23 定性）**：git checkout 把
  `Test/fixtures/*.json` 落成 **CRLF**，而 `Test/gen_fixtures.py` 显式写 **LF**，`--check` 用 `filecmp` 做**字节**比对
  ⇒ 6 条 `CHECK-FAIL` 恒红，真漂移被淹没。**判据**：在真实项目目录跑 `gen_fixtures.py --check`（只读）得到同样 6 条。
  ⇒ 门禁出现这 1 条红时**先判它是不是它**，别去查自己的改动。
- ⚠️ **在工作树里跑 `Test/run_all.py` 会改写 4 个未跟踪的本地数据文件**：`App/stock_float_mc.json`、
  `App/text_annotation.json`（手工标注数据！）、`App/double_click_dt.csv`、`App/last_code_freq.json`
  （`AppConfig.app_data_dir` 指向 `App/`，相关测试未重定向到临时目录）。⇒ 在**真实仓库**里跑门禁前先备份这 4 个文件；
  ⇒ 做交付时这 4 个（连同 `Test/report.json`）**绝不能进 zip**，否则会覆盖用户真实数据。

## 2. 术语护栏：改 / 交付任何文件前必读（防回潮）
- **「引擎」必须写全称（2026-09-17 拍板）**：**交易引擎** = `Trading/`（自动下单功能）；**缠论引擎** = `Trading/`
  之外的部分（K线 / 笔段 / 买卖点）。新增文档、注释、弹窗文案、日志描述一律用全称，不裸写「引擎」。
- **`Trading/Test/test_p26_terminology_guard.py` 是可执行护栏**。来历：2026-09-10 把 `Trading/` 的废弃术语「腿」全替换
  （155 处→0），随后两轮改动回潮 15 处，遂固化为断言。
- **禁用词**：`腿`（含全部复合词）/ `双仓` / `丢仓` / `腿态` / `挑腿` / 英文 `leg`、`legs`（含 `_leg` 标识符）/
  旧枚举名 `open_first`、`unlock_first`。
- **允许表述**：配对持仓 / 配对同向持仓 / 双向持仓 / 运行中持仓 / 剩余持仓 / 锁仓持仓 / 今仓 / 昨仓 / **笔数**。
- **扫描范围**：`Trading/` `App/` `Frontend/` 的 .py / .js ＋ 仓库根 `verify_*.py`；**不扫** `Docs/**`（审计快照满是
  旧术语是设计如此，不要去「修」）。
- ⚠️ **新增任何字符串（log / 注释 / check 描述 / 交付脚本）都要先过 p26**；改完必须重跑 p26 —— 只跑自己改的文件
  看不出来。交付脚本放仓库根也会被扫。

## 3. 命名规范：新增文件 / 目录、改名前必读
- **判据 = 同目录 / 同类既有文件的多数派，不是 PEP8**。生产代码全仓 PascalCase（`Engine.py` / `StateDB.py` /
  `Product.py` / `Records.py` / `Instrument.py` / `SimNow.py` / `AppTrader.py`…）。
- **目录结构**：`Trading/{Broker,Engine,Infra,Risk,Source,Strategy,Test,Tool}`，每包带 `__init__.py`。
- **合法例外**：测试 `test_pNN_主题.py` / `smoke_*.py`；入口 `Trading/main.py`；复合词保留 `KLine_List.py` /
  `KLine_Unit.py` / `BS_Point.py` / `Combine_Item.py`。
- **改名交付 = 唯一「比覆盖解压多一步」的场景**：zip 只有新路径，用户仓库旧文件不会被覆盖消失 → 必须明确让用户手动
  `del` 旧路径（否则留个无人 import 的死模块）。改名前 `grep -rn "<old>" --include=*.py --include=*.md --include=*.html
  --include=*.js .` 全量核对。
- 历史违例已修：`trade_stats.py` → `TradeStats.py`（2026-09-16 用户拍板；Infra 第 8 个模块，见
  `Docs/Infra划分治理_交接文档.md` 附H）。其余历史 snake_case（`func_util` / `cache` / `ccxt` / `ths_*`）**不动**
  —— 扩大改名面 = 加大 pull 冲突。

## 4. 单行硬规则（用户拍板的设计法律 + 踩坑指针）
- **代码里绝不出现按交易所名字判断的逻辑**（2026-09-16 两次拍板，用户原话「一定注意」）：离场走平今还是锁仓**只看
  品种执行策略表第 3 列**（CLOSE / CLOSETODAY），第 2 列交易所 = 纯注释；连表格自洽校验也不要 —— 填错的保护由人负责。
- **改费率 = 改 `Docs/手续费标准-.xlsx` → `Trading/Tool/GenFeeTable.py` 重跑生成区块**，别在 `Product.py` 手写部分加
  数字（生成式 SSOT；`--check` + 测试自检闸门会咬人）。
- **凡往 `Trading/Infra/` 加文件，先读 `Docs/Infra划分治理_交接文档.md` §5.1**（模块定死、按变异轴排）—— 踩过两次；
  受纪律约束的目录加文件前先读纪律文档，生成物优先「内联标记区块」而非新文件。
- **凡删配置键，先 grep 全仓把它当「非法样本 / 默认值样本」的测试**（`max_volume` 教训：键被丢弃后「怎么写都不报错」，
  该段测试退化成恒真）。
- `Engine._state` 是**订单生命周期**（idle / opening / in_trade / exiting），**不是账户三态** —— 锁仓时 `_state` 也是
  idle，看到 idle 别当空仓。
- FAK ⟹ `lots_per_order==1` 硬约束（`ExecPolicy.__post_init__` 违反即拒启）；SimNow **不撮合**（价格合适即全成交），
  不存在部分成交。
- tqsdk 取不到的字段返回 **nan 不是 None**（`not nan` 为 False，判空会漏）→ 校验必须 `math.isfinite(v) and v > 0`。
- D3 卡死（簿内同向手数不一致 ⇒ `close_volume_below_target` ⇒ 离场永久拒单、关闭自动下单不能自愈）触发前提 =
  **持仓期改表第 3 列后又开过仓**；用户已裁定降级（不会持仓期改表），**勿再当新缺陷报**。
- 别把 `shutdown_and_lock_all` 的注释「引擎无自动清仓路径」当通用结论 —— 只在**关闭自动下单之后**成立；开启时锁仓态
  等得到信号 → 转移③ 拆锁 → 转移⑤ → 空仓。
- **给 `Trading/Broker/*` 新增实例字段一律 `getattr` 兜底**（2026-09-19 踩）：本仓库 broker 单测用
  `Broker.__new__(Broker)` 跳过 `__init__`（不连网 / 不查凭据）→ 直接属性访问 = AttributeError；新字段还要放在
  `__init__` **凭据检查之前**（`_sig_orders` 同款理由）。附加的观测 / 日志逻辑必须整块独立 try —— 它绝不允许反过来
  把保活打断。
- **改 `Test/run_all.py` 的 `COMPONENTS` 后 grep 同步写死的组件数**（`run_all_tests.py` docstring 里写着「已注册进
  COMPONENTS 的 NN 个组件」，注册数一变它就陈旧且**没人会红**）。

## 5. 文档维护：README 引用核验
- **`Trading/README.md` 已整份刷新**（2026-09-22）：基线 `7cd182da`（09-17T00:46）→
  `6cd71748654ebb8a529659e1c67ca82b271894fd`（09-22T13:19:38Z，**41 个 commit**）；现 677 行 / 167 条 `文件:行` 引用
  （追补后；追补节见 README 附录 B）。**后续代码改动动了被引行号，同步 README 是改动的分内事**，不是「另开一轮」。
- **核验三件套**（脚本在会话沙盒 `internal/`，换会话须按此逻辑重建）：
  ① `verify_readme_citations_v3.py` —— 行号越界 / 文件解析（**双前缀** `Infra/X.py` 与 `Trading/Infra/X.py` 都试，
  basename 歧义单报不猜）/ 锚点符号**必须紧邻**引用且出现在目标行 / `` `文件:行` `` 后紧跟的「…」引文必须出现在目标行
  （抓「行号写错但内容对得上」）/ 无锚点时的弱关联兜底 / **目标行必须是可执行语句**（落在注释 / docstring 上且正文
  没写明的报错）；
  ② `scan_readme_symbols.py` —— 符号存在性（全仓生产语料带边界搜索，0 命中 = 已删或从未存在）；
  ③ `check_readme_structure.py` —— BOM / 行尾 / 结尾换行 / 图片 / 外链 / 栅栏配平 / 表格列数 / 反引号内裸竖线 / `§`
  与「附录」交叉引用是否有目标。
- **首选做法：等行数替换**（2026-09-23 定型）—— 改注释 / docstring 时**把它压进原有行数**，行号根本不漂，
  下面那套映射追补就完全用不上。实测：本轮给 `Strategy/Exit.py` / `Infra/Product.py` 写说明时先各多出 1~3 行
  ⇒ 会顶动 README 的 `Strategy/Exit.py:478-479`、`Infra/Product.py:438` 等引用；压成等行数后
  **四个文件（`Product.py` / `Exit.py` / `Config.py` / `README.md`）全部 Δ=0 行**，零追补。
  手法：长句合并、删冗词、把说明塞进既有注释行，而**不是新增行**。`Frontend/index.html` 同理（本轮搬元素
  位置也做成 16 行 → 16 行，`Docs/data_source_inventory.md` 的 `index.html:71` 引用不受影响）。
  ⇒ 判据：**能等行数就等行数**；只有行数不得不变（真需要新段落）时才走下面那条映射流程。
- **改注释 / docstring 会顶动行号**（2026-09-23 实测：`Exit.py` docstring 加 25 行 → README 4 处 `Strategy/Exit.py:N`
  全漂）。做法：先把改前原版单独下到 `_new_raw/`，用 `difflib.SequenceMatcher` 的 equal 段建 **old→new 行号映射**
  （改前 N → 改后 N+Δ，取最近的前一个 equal 行），再逐条改 README；改完**按行取内容复核**。**只改注释也要走这一步。**
  `Docs/**` 的历史快照引用**不动**。
- **四条踩坑**：① 裸 `:N` 归因 = **文本序最近一次完整路径引用**（不是一律归 `Config.py`）；② **表格里的裸 `:N` 会被
  同行其他引用抢走** —— 一行若依据列先出现 `Strategy/Entry.py:45-46`，同行「位置」列的裸 `:10` 就被解析成
  `Strategy/Entry.py:10`（实测踩到）⇒ **表格「位置」列写完整路径**；③ `ast` 给的是 **1-based** 行号，判 docstring 用
  `n in dset`，写成 `n-1` 会整片假红；④ 区间引用 `N-M` 只能断言「符号出现在区间内任一行」，只查起始行会假红。
- **README 引用约定**（已写进正文顶部 stamp）：`文件:行` 默认指向**可执行语句**；少数必须指向说明文字的，正文一律
  写明「docstring」或「注释」，读时当线索不当证据。

## 6. 运行态保护价的「出口」（2026-09-23 定型，p62 编号已占用）

- **保护价只有一条线**：`ExitPlan.stop_price` 被「初始止损 → 保本价 → 跟踪价」**逐级改写同一个字段**
  （不另立字段，见 `Strategy/Exit.py` 模块 docstring ①）。**没有独立字段** ⇒ 前端想显示它，只能从**计划的投影**拿。
- **三项解释字段**（2026-09-23 新增，两处投影必须同构）：`phase`（`_phase`：`""` / `breakeven` / `trailing`）、
  `r`（`params["R"]`，1R 的点数）、`tp`（`params["_tp_nominal"]` = 风控锚 ± `win_loss_ratio×R`，**同时就是"转入跟踪层的那个价"**）。
  `stop` 恒取 `_run_plan.stop_price`（**绝不取仓单快照** —— 仓单上的 `exit_plan` 冻结在开仓那一刻）。
- **前端出口**：`Frontend` 顶部「自动下单」一组的**最右格（在「账本」之后**，2026-09-23 用户拍板；顺序契约由
  `test_p62` 的 `[8b]`/`[8b2]` 钉住）—— 常驻徽标 `#auto-order-px` / `renderAutoOrderPrice()`，
  文本 `保护价 <价>`，悬停给出层 / 风控锚 / 1R / 止盈启动价 / 离场判据；**只在有运行段时显示**
  （`run` 为 `None` 时 `display:none`、不占位、不留 `--`）。
  改前端**必须抬** `index.html` 的 `app.js?v=NN`（由 `Test/test_aol_ledger_display.py` ⑤ 组独占守卫，期望值写死）。
- **编号台账**：`p62` = `Trading/Test/test_p62_ao_protection_price.py`（87 项，11 组）；`p61` 之前已占用。
- **`win_loss_ratio` 已全品种统一 2.0**（2026-09-23 用户拍板；此前仅 IC/IM 单标 3.0，该分档取消）⇒ 门槛 = 锚 + 2R。
  **实盘那笔**（2026-09-23 IM2612 多 @7584.6、R=7.8、2 手）：按**当时**的 3R（门槛 7608.0）保本层内最高只到 7607.0
  ⇒ **差 1.0 点没进跟踪层**，保护价全程 7588.6 ⇒「回撤大却不止盈」是**正确行为**、不是 bug（用户当日提问的答案）。
  ⚠️ **改 2R 的代价有实证**：同一笔在 2R 下 10:41:30 进跟踪层、10:43:00 离场成交 7598.4（**+13.8 点**）；3R 下扛到
  10:54:30 成交 7607.8（**+23.2 点**）—— 提前锁利在这笔上**少赚 9.4 点**。单样本不足以定优劣，用户拍板改、AI 负责
  把代价摆出来（脚本 `sandbox/replay_wlr_compare.py`，两档各跑一遍同一段真实 K 线）。
- **口径护栏**：`test_period_profile.py` 钉「`PRODUCT_PROFILES` 的 `win_loss_ratio` **集合 == {2.0}**」（判据用
  **集合**、不绑品种个数 ⇒ 增减品种不必改这条，改口径必须改）。IC/IM 改回 3.0、或新增品种写错档，都当场变红。
- **单源核证（2026-09-23，用户问「改品种盈亏比，真正影响运行的只有档案那一个吗」→ 答案：是）**：链路
  `Product.exit_overrides()`（`Infra/Product.py:347-359`）→ `Config.resolved_exit_params()`（`Config.py:550-553`，
  档案值在 `**` **后**、必压过 `ExitPolicyParams` 的 2.0 默认）→ `main.py:283` 唯一构造点 →
  `Exit.py:167`。**消费点只 2 处**：`Exit.py:375`（写 `params["_tp_nominal"]`，**只落盘不落单**）、
  `Exit.py:478-479`（`tracking_started` = **唯一改变交易行为处**）。**不是来源**的三处：`Config.py:359` 字段默认值
  （只服务"无档案直连"）、`Engine.py:1786` 的 `getattr(pol,…)`（**只服务 toast 文案**，从 policy 读）、`App/`+前端
  （全仓零命中，前端只消费投影出的 `tp`）。注入旁路全堵：init 传参与 `TRADING_EXIT_PARAMS__WIN_LOSS_RATIO` 都撞
  `extra=forbid`。实证 = `sandbox/probe_wlr_ssot.py`（48 项：8 品种链路逐一 + 假想改档案到 7.0 的行为层对照 ——
  同一根 3R 浮盈，2.0 → `trailing`/保护价 125，7.0 → `breakeven`/保护价 105）。
- **文档 / 注释一律不复述档位数值（2026-09-23 拍板）**：倍数只许写在档案条目上，改一处即可生效。已清 10 文件
  40 处（**全等行数替换**，保 `文件:行号` 引用不漂移）。护栏 = **`test_p63_wlr_doc_guard.py`（编号 p63 已占用、
  已进 `Test/run_all.py`，26 项）**：载体 = 注释与字符串常量（`tokenize` 取 token，**docstring 是跨行 token、
  须按行拆开判**）；判据 = 「语境词 `win_loss_ratio`|`盈亏比`|`L3`（**L3 必须带词边界**，否则 `文档 L332`
  会被误伤）＋ 档位形态」**同行同现**；形态 = 比例式（两侧不许与数字相邻，避开 `09:41:33` 时间戳）/`统一|全为|均为|同在`+倍数/
  `倍数+启动|达标`/`win_loss_ratio=小数`/`集合 == {数字}`。**判据用形态不绑数值** ⇒ 档位改成 2.5 不必改这条。
  自证三层：[3] 8 条诱饵（诱饵区用 `p63-scan-off … p63-scan-on` 豁免，且 [1b] 断言该标记**全仓只许出现在 p63 自己**）/
  [4] 9 条正当写法零命中 / [5] 临时文件验"可执行实参不受扫描"；另有独立变异自证 5 条全拦
  （`sandbox/mutate_probe3.py`，5 文件写回复述 → 全红 → 逐字节还原）。
  **刻意保留**：档案实参、`test_period_profile` 的集合期望值 `[2.0]`（口径钉子）、用例自注入档位
  （`test_p61` 的 `阈值3R/阈值2R`、`test_p8` 的「止盈距:止损距 = 1:2」各属其自身入参，与档案无关）。
- ⚠️ **已知残余（未修，需另行裁决）**：
  - ✅ 已清（2026-09-22 深夜）：`Trading/__init__.py` 包 docstring 的 `:10`「Reconcile 对账 + F1」→「Reconcile **持仓
    对账**」；同处清掉 `:8` 的「EntryPolicy 入场**过滤**」→「EntryPolicy 入场」（信号质量过滤早已移除）。文件仍 18 行，
    故 `:7-13` / `:15` / `:10` 三个引用照旧有效；p26 复跑 24/24 通过；README 附录 A/B 已同步（残余清单 3 → 2 条）。
  - `Docs/止盈止损/止盈止损四层策略原理.html` 的 `:1160` / `:1167` / `:1333` / `:1334` 仍把 `trailing_atr_multiple` /
    `use_trailing` 当现役开关。
  - `Docs/出场判定口径与1R播报_交付说明_20260922.html:249` 仍写「只对固定止盈单模式生效」（历史快照性质，已累积多轮
    失真）；**完整失真行清单见 §0 末**。

## 8. R（初始风险距离）的构成，与它对「成交价 / 成交时点」的敏感度（2026-09-23 补充）

**链（每条都可执行）**：报单 `insert_order(volume=int(volume), advanced=表第2列)`（`Broker/SimNow.py:1153`）
→ 成交价 = **CTP 成交明细按 volume 加权均价**（`SimNow.py:466 _traded_price_from_records`；`1284-1306` 用它覆盖
`filled`，明细取不到才回退 tqsdk `order.trade_price`——同为均价）→ `Engine.py:1511 _run_start(o.filled_price, …)`
→ `1826 plan(sig, anchor_price, anchor=anchor_price)` ⇒ **风控锚 = 入场成交价**
→ `Exit.py:370 base = anchor` → `373 R = _initial_r(signal, base, state)`
→ 多仓 `A = max(base − signal.fractal_low, 0)`（`Exit.py:312`）、`B = atr_sl_multiple(默认 2.0) × ATR`（`326`）
→ `346 R = max(A, B)`。同价另落 `Position.entry_price`（`Engine.py:1556`）= 会计锚，**仅供 pnl 对账**。

**一笔 = 整量、只报一次**：开仓 `max_attempts = … if is_exit else 1`（`SimNow.py:1136`）⇒ 开仓**不追价重报**；
IM 表值 `lots_per_order=2` + `FOK` ⇒ 一笔 2 手要么全成要么全撤，**不存在「N 手只成 M 手」**。
TA = `FAK` + 强制 `lots_per_order=1`（`Infra/Product.py:232-236` 构造期硬断言）⇒ 单笔 1 手，无加权问题。
**委托限价不进成交价**，它只决定「能不能成交」。

**两段规律（"成交价怎么影响 R"的正解）**：
- **A 主导**（`成交价 − 分型极值 > 2×ATR`）：R 跟成交价走（斜率 1），**止损 ≡ 分型极值**（不动）⇒ 滑点全额吃掉风险缓冲；
- **B 主导**（`2×ATR` 更大）：**R 恒定**（滑点对 R 的影响 = 0），止损 = 成交价 − R **整体平移**；
- 分界：`成交价 − fractal_low == 2×ATR`。

**复现实盘 R 的钥匙（最容易错的一点）**：ATR 取**入场那一刻 `_bars` 缓冲**，而 `Engine.py:760` 是在 **bar 闭合后**
才喂 `on_bar` ⇒ 成交发生在某根 bar 中途时，**那根尚未闭合的 bar 不在缓冲里**。实盘样本（2026-09-23 IM2612 多，
成交 10:11:04）：截止 10:10:45 → atr=3.9、B=**7.8**（= 实盘 R，stop=7576.8 亦逐位吻合）；截止 10:11:00 →
atr=4.17143、B=8.343（`max(A,B) ≥ 8.343 ≠ 7.8` 与实盘矛盾 ⇒ 据此**排他**）。⇒ 必须用"最后一根已闭合 bar"截断。

**两个 B=0 窗口（R 只剩 A，最易"缩水"）**：进程重启后前 `atr_period+1`(15) 根 bar（`_bars` 是内存态，重启即空）；
每交易日开盘后前 15 根 bar（`on_bar` 跨日 `clear`，`Exit.py:200`）。实测：`gateway.log` 09-22 13:32:03
`atr=None B=0` → R 只由 A=1.2 决定。

**R 是入场时刻的快照**：写入 `params["R"]` 后不再变；但同一信号晚 15s 成交就可能换一档
（实测 B：7.6286 → 7.8 → 8.3429 → 8.3143）。另：`fractal_low` 在 B 主导时**不进 R**，从 stop 也反推不出它。
