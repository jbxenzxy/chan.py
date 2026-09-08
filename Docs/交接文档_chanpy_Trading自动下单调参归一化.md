# 交接文档 · chan.py Trading 自动下单「调参归一化」专项

> **本文档的目的**：让你（接手的人 / 未来某次新会话）不需要从头考古对话记录，
> 拿到本文档就能**按部就班续推**——知道在做什么、为什么、做到哪一步、下一步怎么走、有哪些坑。
>
> **生成日期**：2026-09-08（工作日）
> **状态**：**Step 2.0–2.6 全部完成（结构归一阶段收官 ✅，2026-09-08）**；2.1 已合入 real project，**2.2–2.6 待用户合并**（直接用 `step2_6_Trading.zip` 整包覆盖）；**下一步是 2.7+（调值阶段，被数据缺口阻塞：真实行情数据 + 5m 基线确认）**
> **维护约定**：本文档是**进度 SSOT**。每次续推前先读 §4（当前精确状态）与 §5（下一步）；每次做完一段回来更新 §4 状态表。

---

## 0. 一句话定位

在 **chan.py 量化交易框架**（fork 自 `github.com/jbxenzxy/chan.py`）的 **`Trading/` 自动下单模块**上，
把「各周期（30m/5m/1m/15s）可能涉及的**调参项**」**从散落代码各处归一到 `Trading/Config.py` 一个地方**，
**先立架构、后调数值**，用缠论买卖点信号做期货（CFFEX 股指）自动开仓的 L1–L4 分层出场策略。

**关键词**：归一（不是调值）、一个地方、先架构后数值、SSOT、断点续传。

---

## 1. 项目背景（为什么会有这件事）

### 1.1 三层目标链条

| 层 | 目标 | 状态 |
|---|---|---|
| **业务层** | 基于缠论买卖点信号**自动开仓**（买点开多、卖点开空），走 SimNow 仿真 | 已能在 Kuaiqi3（快期3）用 CTP 登录 SimNow；`trader_gateway`(tg) 单项目已跑通 |
| **架构层**（本专项 Step 2） | 把跨周期调参项**归一到 `Trading/Config.py`**，改周期只改一处 | **进行中（2.0–2.1 已完成，2.2+ 待续）** |
| **数值层**（Step 2.7+） | 逐周期差异化标定参数值 | **被数据缺口卡住**（见 §4.3） |

### 1.2 用户核心需求（原话，根本锚点）

> 「调参我觉得不是最重要的，最重要的是先在**代码架构**上把各周期可能涉及的调参项**归一到 Trading/Config.py 中**，
> 方便后续在这一个地方调整，而不是散落在代码各处」

> 「因为无法一次性做完，所以以上这些都需要**纳入文档管理**，下次再接着做的时候，知道做到哪里了，可以延续推进下去」

> 「我不会选它（策略选择器），我只用 **L1-L4 策略**。也就是说，**入场就一个策略，出场也是一个策略，无需第二个选择**！」

> 「调参不是最重要的……先架构后数值」

### 1.3 技术要点（接手前必读）

- **`Trading/` 是独立网关**：HTTP SSE 取数，对 chan.py 主程序零侵入（自成一包，可独立 `python Trading/main.py`）。
- **4 周期**：`30m / 5m / 1m / 15s`，`SUPPORTED_FREQS` 白名单。
- **出场策略（生产唯一）**：`LayeredExitPolicy`（L1–L4 分层）。入场（生产唯一）：`DefaultEntryPolicy`。
- **SSOT 原则**：每个默认值**只在一处声明**，禁止跨文件 fallback 重复。pydantic v2 `extra="forbid"` 严格模式。
- **六层配置模型**（在 `Trading/Config.py`）：`BrokerConfig / SourceConfig / RiskConfig / SizingConfig / EntryConfig / ExitConfig`，根配置 `TradingConfig`。
- **测试**：`Trading/Test/*.py` 是**独立脚本**（非 pytest），`check(name, got, expected)` 辅助 + 末尾打印「结果: X 通过 / Y 失败」+ `sys.exit(1)` 失败即退。当前 **25 个 test_** 脚本。

---

## 2. 目录与仓库拓扑（极重要，先认路）

本次工作横跨 4 类目录，**不要搞混**，尤其注意**沙盒纪律**。

| 目录 | 是什么 | 能不能改 | 备注 |
|---|---|---|---|
| **`C:\my_chan_project\`** | **用户的真实项目**（**非 git 仓库**，是合并后的工作副本） | 🔴 **绝对不碰**（写盘违规） | 已含 Step 2.0.3b + **2.1** 的合并结果 |
| **`C:\Users\river\WorkBuddy\2026-09-08-06-43-04\sandbox\Trading\`** | **活跃编码沙盒**（独立 Trading 包）——所有代码改动**只发生在这里** | 🟢 **改这里** | 交付物 = 把这里打成 zip |
| `...\sandbox\chan.py\` | **git 克隆**（完整 repo），用于**验证远端合并**是否完整 | 🟡 只读复验 | **已过时**（停在 `5478def`，早于用户合 fc46127）；仅合并校验用 |
| `...\output\` | **交付物目录**：zip 包 + md/html 文档 | 🟢 产出放这 | 每次交付在此留档 |
| `github.com/jbxenzxy/chan.py` **branch `custom-dev`** | 远端主干，用户会把自己的合并**推送**到这里 | 🔴 只由用户推 | 验证合并完整性时 `git clone` 到这对比 |

### 2.1 铁律（沙盒纪律，违反即事故）

1. **代码改动只发生在 `sandbox/Trading/`**。交付 = 打包成 zip，**合并动作永远由用户自己做**（用户合并进 `C:\my_chan_project` 后自行推送）。
2. `cp/rsync/git checkout` 指向 `C:\my_chan_project` 属于**违规操作**，即使"方便复验"也不行；复验一律**解压 zip 到独立临时目录**跑。
3. **本机打包/解压一律用 Python `zipfile`**（PowerShell `Compress-Archive`/`Expand-Archive` 在本机多次静默失败）。排除 `__pycache__`/`State`/`replay_data`/`.git`。
4. **会话内 Write/Edit 落盘可能不可靠**（曾出现改动"以为写了其实没落盘"）。写完**必须** grep/Read 校验；交付前从 **zip 包内读回**再校验一遍（双保险）。
5. **绝不一条消息对同一文件发两个 Edit**（同文件并行 Edit 会竞态丢改）；一批后 grep 校验。
6. 编辑后清 `__pycache__`（`find . -type d -name __pycache__ -exec rm -rf {} +`），否则陈旧字节码导致 `hasattr` 仍报旧名。
7. **正则 rename 防子串误伤**：如 `ExitParamsConfig` 是 `DefaultExitParamsConfig` 的子串，裸 `replace_all` 会误改；用 `(?<!Default)` 负向环视只改独立 token。

---

## 3. 已完成步骤全史（含每步交付物）

按时间线，方便追溯"某行代码为何长这样"。

| 阶段 | 做了什么 | 交付物（在 `output/`） | 状态 |
|---|---|---|---|
| **Step 1A** | 四周期（30m/5m/1m/15s）逻辑正确性**审计**（找出 bug，不改代码） | `Step1_四周期逻辑正确性审计.md/.html` | ✅ |
| **Step 1B** | 四周期逻辑正确性**修复**（改代码 + 测试，含 v2 撤回） | `Step1B_交付说明_四周期逻辑正确性修复.md`、`step1b_四周期修复_Trading.zip`、`step1b_changes.patch` | ✅ 已合 real project |
| **IF 15秒迁移分析** | 股指 IF 15s 周期迁移的专项分析 | `IF_15秒周期迁移分析.md/.html` | ✅ 参考 |
| **Step 2 路线图** | 调参项归一的全 phase 总览（2.0–2.6 + 2.7+ 调值）| `Step2_路线图_调参项归一.md/.html` | ✅ 本文档前身，进度以本文档为准 |
| **Step 2 可行性** | 周期参数定型可行性分析 | `Step2_可行性分析_周期参数定型.md/.html` | ✅ 参考 |
| **2.0 + 2.0.1 + 2.0.2** | 六层配置模型拆解、类名调整、`EntryParamsConfig`/`ExitParamsConfig` | `Step2_0_交付说明_配置分层与周期敏感归总.md` | ✅ |
| **2.0.3** | **彻底删除策略选择器抽象**：删 `DefaultExitPolicy`、`EntryPolicyConfig`/`ExitPolicyConfig`、注册表（`EXIT_POLICIES`/`register_*`/`build_*`）；14 测试迁移到 `LayeredExitPolicy()` | `step2_0_Trading.zip` | ✅ |
| **2.0.3b** | `EntryParamsConfig → EntryConfig`、`ExitParamsConfig → ExitConfig`（保护 `DefaultExitParamsConfig` 历史说明不被误改）；命名 `TradingConfig`/`BrokerConfig` 定稿 | `step2_0_Trading.zip`（重打包） | ✅ |
| **合并校验** | 拉 GitHub `custom-dev` 最新，验证 2.0.3b 合入完整（HEAD=`fc461277...`，2026-09-08 11:34）| — | ✅ |
| **2.1** | **周期敏感参数入 `PeriodProfile`**：8 项收口到 `PERIOD_PROFILES`（每周期一份），`TradingConfig` 构造期影子覆盖进 flat 字段 | `step2_1_Trading.zip`、`Step2_1_交付说明_周期参数入Profile.md`、`Step2_1_可行性分析_周期参数入Profile.md/.html` | ✅ **已合 real project** |
| **2.2** | **引擎常量 EngineConfig 化**：`_close_retry_bars=5`/`_close_max_streak=20`/`_unlock_stuck_bars=5` 三项硬编码收口到 `TradingConfig.engine`（拍板 E1+F1+G2）；`PositionBook.DEFAULT_MAX` 注释语义收窄（G2）；`main.py` 4.5h 收口到 `PeriodProfile.SESSION_SECS` | `step2_2_Trading.zip`（65 文件/304290B）、`Step2_2_交付说明_引擎常量EngineConfig化.md`、`Step2_2_可行性分析_引擎常量EngineConfig化.md` | ✅ 待用户合并 |
| **2.3** | **Broker/Channel 超时集中**：SimNow.py 散落的 10 处超时/等待/退避因子收口到 `ChannelTimingConfig`（挂 `BrokerConfig.channel`，拍板 A1+B1+C1）；SimNow 新增 `_timing()` 严格读取；`_QUOTE_STALE_SECONDS` 模块常量删除；微轮询 sleep 换命名常量 `_POLL_INTERVAL_FAST/SLOW` | `step2_3_Trading.zip`（66 文件/308832B）、`Step2_3_交付说明_BrokerChannel超时集中.md`、`Step2_3_可行性分析_BrokerChannel超时集中.md` | ✅ 待用户合并 |
| **2.4** | **重复常量合并**：两个语义相同的 `20` → SSOT 常量 `CFFEX_LIMIT_MAX`（PositionSizing 定义 + Engine 导入，`is` 同对象实证）；删 `per_lot_margin` 的 `else 0.15` 第二默认源（margin_rate=0 → 诚实走 bad_param fallback，唯一非纯重命名改动）；`test_p9` 断言语义同步 | `step2_4_Trading.zip`（67 文件/311573B）、`Step2_4_交付说明_重复常量合并.md`、`Test/test_const_merge.py`（14 断言） | ✅ 待用户合并 |
| **2.5** | **Source/Recorder 重连参数收口**：`SourceConfig` 新增 `reconnect_wait/reconnect_wait_max/reconnect_max_retry`（默认=原硬编码 5/60/0）；修复 SSE 三参数"死旋钮"缺陷（extra=forbid 下旧键传不进来）+ 删双默认源写法 + 构造期 fail-fast 守卫；`SignalRecorder` argparse 默认值同源化（`_build_parser()` + sys.path 引导导入） | `step2_5_Trading.zip`（68 文件/314693B）、`Step2_5_交付说明_Source重连参数收口.md`、`Test/test_source_reconnect.py`（20 断言） | ✅ 待用户合并 |
| **2.6** | **测试补齐 + 四周期启动冒烟（结构归一收官）**：审计发现 2.0 系列无专属测试 → 新增 `Test/test_step2_config_ssot.py`（43 断言：六层配置模型 / 选择器删除 / 类名改名 / 每模型单一定义）+ `Test/test_step2_smoke_freq.py`（65 断言：30m/5m/1m/15s 四周期 `build_runtime` 启动链路冒烟 + `--freq 15m` fail-fast 反向用例）；**零生产代码改动**；全量 31/31 全绿 | `step2_6_Trading.zip`（70 文件/319644B）、`Step2_6_交付说明_测试补齐与四周期冒烟.md` | ✅ 待用户合并 |

### 3.1 Step 2.0.3b 删除策略选择器的最终代码形态（2.1 继承此基础）

`TradingConfig` 字段：
```python
entry_params: EntryConfig        # = 唯一入场策略 DefaultEntryPolicy 的参数
exit_params:  ExitConfig         # = 唯一出场策略 LayeredExitPolicy 的参数
```
`Strategy/__init__.py` 只导出：
```python
from .Base import ExitCheck, ExitPolicy, EntryPolicy
from .Entry import DefaultEntryPolicy
from .Exit import LayeredExitPolicy
```
`main.py` 直接构造：
```python
entry = DefaultEntryPolicy(cfg.entry_params.model_dump())
exitp = LayeredExitPolicy(cfg.exit_params.model_dump())
```
引擎实例属性 `self.entry_policy` / `self.exit_policy` 是引擎字段，与 `cfg.*_policy` 配置路由无关，保留。

---

## 4. 当前精确状态（**接手先读这里**）

> 以下为 2026-09-08 实测（直接对 real project `C:\my_chan_project` 的 Trading 目录 grep）。

### 4.1 real project 合并状态（已核）

| 校验项 | 结果 |
|---|---|
| 是否 git 仓库 | ❌ **非 git 仓库**（合并工作副本，用户手动合并 + 推送） |
| `Trading/Infra/PeriodProfile.py` 存在 | ✅（11629 字节） |
| `Trading/Config.py:82` 导入 `PERIOD_PROFILES, PeriodProfile, SUPPORTED_FREQS` | ✅ |
| `Trading/Config.py:178` `apply_period_profile` | ✅ |
| `Config.py` 中 `period_profile` 命中 | 4 处 → **2.1 已合入** |
| `Config.py` 中 `entry_policy`（旧选择器名）残留 | 0 → **2.0.3 删除干净** |

### 4.2 沙盒与交付物（2026-09-08 14:20 状态，2.6 收官后）

| 项 | 值 |
|---|---|
| 活跃编码沙盒 | `sandbox/Trading/`（含 2.6 全部改动 + **31 个 test_**） |
| 最新交付包 | `output/step2_6_Trading.zip`（70 文件 / 319644 字节；= 2.5 包 + 2 个新测试，零生产代码改动） |
| 远端 custom-dev HEAD | **`96d9b04`（2026-09-08 14:14「更新」）——2.1–2.6 已全部合入推送（去 CRLF 全树 0 差异复验 ✅）** |
| `sandbox/chanpy_customdev/` | 2026-09-08 15:20 新建浅克隆（只读复验用；后续核对以它为准） |
| 测试基线 | 31/31 test_ 全绿（按退出码判定；zip 解压复验 30/31，唯一差异是 `B_replay_smoke` 依赖被排除的 `replay_data/`，环境数据依赖非回归） |

### 4.3 已知阻塞项（调值的真正瓶颈）

> **2026-09-08 14:30 更新**：数据缺口已完成可行性分析，见 **`Step2_7前置_数据补录可行性分析.md`**（结论：SSE 实时录制为主 + akshare 历史分钟线预标定为辅；4 项决策点 D1–D4 待拍板）。

| 阻塞 | 说明 | 影响 |
|---|---|---|
| 🔴 **真实历史行情缺失** | 仅 5m 有少量 demo（历史摘要：2 根合成/144 根不等），15s/1m/30m 无标定数据 | **2.7+ 差异化调值做不了** |
| 🟡 **5m 基线参数未确认** | 谁是"5m 基线"缺明确锚点 | 影响 G1 BASELINE 占位的合理性 |
| 🟡 **远端未含 2.1** | real project 已合 2.1，但 `custom-dev` 远端还停在 fc46127（2.0.3b） | 下次沙盒初始化要核对基线 |

### 4.4 2.1 的架构落点（理解后续 2.2 的前提）

- **8 项周期敏感参数**已收口到 `Infra/PeriodProfile.py` 的 `PERIOD_PROFILES`（每周期一份档案）。
- `TradingConfig` 构造期按 `source.freq` 选档案，`@model_validator(mode="after")` 做「**影子覆盖**」进 flat 字段——**仅当 flat 字段仍是模型默认值**才填（用户显式覆盖优先）。
- `bar_secs` **不 reconcile**：仍走引擎 `bar_secs_for(freq)` 自动推导（`PeriodProfile.bar_secs` 已是推导源），flat `bar_secs=0` 是手动覆盖旋钮。→ 2.1 实际 reconcile **6 项**：`signal_max_age_minutes / max_hold_bars / max_hold_seconds / eod_lead_bars / session_end_hhmm / max_trades_per_day`。
- **未知 freq 容错**（不 fail-fast）：`test_p20` 用 `freq="15m"` 构造 `TradingConfig` 测 CLI 透传（非引擎场景）→ 校验器跳过；真 fail-fast 在 `main.py` 的 `bar_secs_for`（已有）。
- **取值策略 G1 统一 BASELINE 占位**：4 周期全用 5m 当前默认值，`note` 显式标「占位=BASELINE；待 2.7+ 重标」。G1 下 profile 值 == schema 默认 → 影子覆盖幂等 no-op，5m 行为与 2.0.3b 完全一致。
- **已知局限**（已注释在代码）：`_apply_profile_values` 用「== schema 默认」判定是否覆盖；若 2.7+ 让某 profile 值偏离 schema 默认、且 `--freq` 构造后二次切换，需显式追踪「profile 已填字段」再重对齐。当前 BASELINE 下无此问题。

---

## 5. 下一步怎么走（按部就班，直接照做）

> 用户指示原文：「之后是 **2.2（EngineConfig 时序参数归总）**，还是要先解决数据/基线缺口再推进调参，你定」——中途插入做本文档。**本文档产出后，2.2 是默认续推方向**（结构归一不受数据缺口阻塞），数据缺口只阻塞 2.7+ 调值，不阻塞 2.2。

### 5.1 Step 2 剩余 phase 一览

| Phase | 主题 | 涉及 | 估算 | 前置 |
|---|---|---|---|---|
| **2.2** | **引擎常量 EngineConfig 化**：D-1/2/3（close 重试/MAX 连续/卡死解锁三常量）+ D-6 PositionBook 默认容量显式化 | ~80 行 | ✅ **已完成（2026-09-08）** |
| **2.3** | Broker/Channel 超时集中：~18 处 wait_update/重试秒数 → 新 `ChannelTimingConfig` 附在 `BrokerConfig` 内 | ~150 行 | ✅ **已完成（2026-09-08，实际收口 10 项 + 2 命名常量）** |
| **2.4** | **重复常量合并**：两个语义相同的 `20`（PositionSizing 的 sizing.max_volume 默认截断 + Engine._open_position 交易所限单检查）→ SSOT 常量 `CFFEX_LIMIT_MAX`（定义在 Risk/PositionSizing.py，Engine 导入）；删 `per_lot_margin` 的 `else 0.15` 第二默认源（margin_rate=0 现诚实走 bad_param fallback） | `step2_4_Trading.zip`（67 文件/311573B）、`Step2_4_交付说明_重复常量合并.md`、`Test/test_const_merge.py`（14 断言） | ✅ 待用户合并 |
| **2.5** | **Source/Recorder 重连参数收口**：`SourceConfig` 新增 `reconnect_wait/reconnect_wait_max/reconnect_max_retry`（默认=原硬编码 5/60/0）；修复 SSE 三参数"死旋钮"缺陷（extra=forbid 下旧键传不进来）+ 删双默认源写法 + 构造期 fail-fast 守卫；`SignalRecorder` argparse 默认值同源化 | `step2_5_Trading.zip`（68 文件/314693B）、`Step2_5_交付说明_Source重连参数收口.md`、`Test/test_source_reconnect.py`（20 断言） | ✅ 待用户合并 |
| **2.6** | 测试 + 启动校验：每 phase 加测试；6 段全完跑 `--freq {30m,5m,1m,15s}` 启动冒烟全通 | ✅ **已完成（2026-09-08，实际补 2.0 系列测试 43 断言 + 冒烟 65 断言，零生产代码改动）** |
| **2.7+** | **调值阶段**（数据依赖）：2.7 数据补录 → 2.8 回测 → 2.9 网格搜索 → 2.10 SimNow 实盘验证。**开工时必须一并处理 2.1/2.2 遗留（详见 §5.3 遗留清单）**：① 三项根数口径参数（`engine.close_retry_bars` / `engine.close_max_streak` / `engine.unlock_stuck_bars`）候选迁入 `PeriodProfile`（2.2 拍板 F1：先 flat、2.7+ 候选）；② `_apply_profile_values`「== schema 默认」判定局限升级（§4.4） | ⏳ **下一步（被数据缺口阻塞，见 §4.3）** |

### 5.2 续推 SOP（2.7+ 调值阶段，照抄）

> 结构归一（2.0–2.6）已收官。2.7+ 是**调值阶段**，前置条件：真实历史行情数据（15s/1m/30m 目前为 0）+ 5m 基线参数确认（§4.3）。

1. **读本档 §4.2/§4.3** 确认沙盒与交付物状态、阻塞项。
2. **数据补录**：解决真实行情数据缺口（chan.py SSE 录制或其它来源），归档到可复用目录。
3. **确认 5m 基线**：谁是 5m 基线缺明确锚点；同时处理遗留清单 §5.3（L-1 三项根数参数候选迁入 PeriodProfile + L-2「==schema 默认」判定局限升级）。
4. 差异化 `PERIOD_PROFILES`（不再全 BASELINE 占位）→ 回放回测 → 网格搜索 → SimNow 验证。
5. 每 phase 在 **`sandbox/Trading/`** 实现 → `py_compile + 全量 test_` 确认落盘 → 打包 zip（Python zipfile，只打 `Trading/` 子树）→ **zip 解压到干净目录复验** → 更新交付说明 + 本档 §3/§4 状态表。
6. 交付给用户合并进 real project 并推送。

### 5.3 ⚠️ 2.7+ 开工时的遗留清单（必办，勿漏）

| # | 遗留项 | 详情 | 出处 |
|---|---|---|---|
| L-1 | **三项根数口径参数候选迁入 `PeriodProfile`** | `engine.close_retry_bars(=5)` / `engine.close_max_streak(=20)` / `engine.unlock_stuck_bars(=5)`——语义上周期敏感（15s 的 5 根=75 秒 vs 30m 的 5 根=2.5 小时），2.2 拍板 F1「先放 `EngineConfig`（跨周期不变取值），2.7+ 差异化标定时候选迁入 profile」 | 2.2 可行性分析 §决策F + 2.2 交付说明 §4 |
| L-2 | **「== schema 默认」判定局限升级** | `_apply_profile_values` 用「flat 字段 == schema 默认」判定是否被 profile 覆盖；2.7+ 各周期值偏离 schema 默认后，若 `--freq` 构造后二次切换需显式追踪「profile 已填字段」再重对齐（当前 BASELINE 全等值下无此问题，一旦差异化就会暴露） | 2.1 交付说明 §4 + 本档 §4.4 |
| L-3 | **5m 基线锚点确认** | 谁是「5m 基线」缺明确确认，影响 G1 BASELINE 占位的合理性（§4.3） | §4.3 |
| L-4 | Docs/ 目录旧 `GatewayConfig` 名称引用 | 历史文档引用，不在代码包内，合并刷新时顺带处理（低优先级） | §5.3 原记载 |

> L-1/L-2 是**代码结构改动**（不受数据缺口阻塞，若想提前清障可单独做一小 phase）；
> L-3/L-4 依赖数据/文档合并。做完 L-1 时须同步扩 `PERIOD_SENSITIVE_FIELDS` 索引与 `test_period_profile` / `test_engine_config` 断言。

---

## 6. 踩坑与纪律清单（含代码级教训）

1. **会话写盘不可靠**：改动必须 grep/Read 校验，交付从 zip 包内读回双保险。
2. **同文件同消息两个 Edit 会丢改** → 顺序单发。
3. **陈旧 `__pycache__`** → 编辑后清空。
4. **rename 防子串误伤** → 负向环视正则（`(?<!Default)ExitParamsConfig`）。
5. **打包解压用 Python zipfile**，别用 PowerShell Compress-Archive/Expand-Archive（静默失败）。
6. **测试是独立脚本**，用 `sys.exit(1)`，跑全量用临时 runner（cwd 设 `sandbox/`，不是 `sandbox/Trading/`，否则 `import Trading` 路径错位）。
7. **编译/运行用** `C:\my_chan_project\.venv\Scripts\python.exe`（含依赖）；纯校验可用 `C:\Users\river\.workbuddy\binaries\python\versions\3.13.12\python.exe`。
8. **don't 把"标定差异"当"代码缺陷"修**（如 `max_hold_bars` 曾被误当 bug、`总腿数` 曾误判）——先问清楚是不是设计使然。
9. **单可替换单元**：交付优先整包 zip 覆盖 `Trading/`，避免全仓库 refactor；文档改动单独打包解耦。
10. **严格不碰 real project**：所有改动只发生在沙盒，合并永远由用户做。

---

## 7. 交付物索引（output/）

| 文件 | 说明 |
|---|---|
| `交接文档_chanpy_Trading自动下单调参归一化.md/.html` | **本文档（总入口）** |
| `Step2_路线图_调参项归一.md/.html` | Phase 全览（进度以本文档 §4 为准） |
| `Step2_0_交付说明_配置分层与周期敏感归总.md` | 2.0–2.0.3b 全史 + 删除选择器实证 |
| `Step2_6_交付说明_测试补齐与四周期冒烟.md` | 2.6 交付说明（含结构归一收官清单） |
| `Step2_7前置_数据补录可行性分析.md` | 2.7 前置：数据缺口可行性（录制主路线 + D1–D4 决策点） |
| `Step2续_周期敏感清单核对与策略风控全表_96d9b04.md` | Q1–Q5 核对结论 + 策略/风控层功能全表 + 删减评估（T-1~T-7 待拍板） |
| `Step2_5_交付说明_Source重连参数收口.md` | 2.5 交付说明（含 SSE 死旋钮缺陷说明） |
| `Step2_4_交付说明_重复常量合并.md` | 2.4 交付说明（含 margin_rate=0 语义变化说明） |
| `Step2_3_交付说明_BrokerChannel超时集中.md` | 2.3 交付说明 |
| `Step2_3_可行性分析_BrokerChannel超时集中.md` | 2.3 设计决策（A1/B1/C1） |
| `Step2_2_交付说明_引擎常量EngineConfig化.md` | 2.2 交付说明 |
| `Step2_2_可行性分析_引擎常量EngineConfig化.md` | 2.2 设计决策（E1/F1/G2） |
| `Step2_1_交付说明_周期参数入Profile.md` | 2.1 交付说明 |
| `Step2_1_可行性分析_周期参数入Profile.md/.html` | 2.1 设计决策 |
| `Step2_可行性分析_周期参数定型.md/.html` | Step 2 可行性 |
| `Step1_四周期逻辑正确性审计.md/.html` | Step 1A |
| `Step1B_交付说明_四周期逻辑正确性修复.md` | Step 1B |
| `IF_15秒周期迁移分析.md/.html` | 专项分析 |
| `step2_6_Trading.zip`（最新） | **当前最新交付包（待用户合并；含 2.0.3b→2.6 全部累积改动，直接整包覆盖即可；= 2.5 包 + 2 个新测试）** |
| `step2_5_Trading.zip` | 2.5 交付包（待用户合并） |
| `step2_2_Trading.zip` | 2.2 交付包（待用户合并） |
| `step2_1_Trading.zip` | 2.1 交付包（已合 real project） |
| `step2_0_Trading.zip` | 2.0.3b 交付包 |
| `step1b_四周期修复_Trading.zip` / `step1b_changes.patch` | Step 1B |
