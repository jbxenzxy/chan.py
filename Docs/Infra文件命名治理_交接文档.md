# Trading/Infra 文件命名治理方案（交接文档）

> 日期：2026-09-15
> 状态：**核心议题已拍板（§六 1–4、6），实施未开始、未动代码**
> 背景：Phase 0~4（双轴声明 / 策略参数单源化 / InstrumentSpec 拆分 / 收尾）已完成并回归通过（50/51，1 失败为 tqsdk 环境缺包，与基线一致）。本方案是后续独立一轮治理的**提案**。
> 仓库：jbxenzxy/chan.py `custom-dev` 分支；工作副本 `chan-py-custom-dev/`

---

## 一、问题陈述

用户反馈：`Infra/` 下 `InstrumentSpec.py`、`Types.py`、`Store.py` 三个文件**看到名字不知道是干嘛的**；而 `PeriodProfile.py`、`ProductProfile.py` 一看就懂。

### 病根诊断

PeriodProfile / ProductProfile 好用的原因：**名字 = 领域概念**（周期档案、品种档案），名字直接回答"这个文件管什么"。

另外三个文件犯的是另一套毛病——**按技术机制命名**：

| 文件 | 大小 | 病根 |
|---|---|---|
| `InstrumentSpec.py` | 37KB（全目录最胖） | **名不副实**。Phase 3 之后文件里住着两个生命周期不同的东西：`InstrumentSpec`（静态规格）+ `InstrumentState`（运行时状态 + 定价/成本计算）。名字只覆盖前一半 |
| `Types.py` | 23KB | **万能抽屉名**。"Types" 零信息，必须打开才知道。实际内容是三个互不相干的关注点：交易时钟/交易日历、五个语义枚举、七个领域记录 |
| `Store.py` | 14KB | **只说机制不说对象**。回答不了"存的是什么、存在哪"。它实际是 SQLite 状态库——测试用语早就叫它 `state.db`（test_p36_state_db_no_legacy） |

**共同规律：Profile 系按领域概念命名，另外三个按技术机制命名——两套惯例混在同一目录，读起来割裂。**

---

## 二、概念澄清（方案的地基，先读这节）

### 2.1 为何叫 Instrument？

"Instrument" 是交易行业的标准术语，指**被交易的那个标的合约**，不是自造词：

- **CTP（期货实盘柜台 API）**的合约查询接口就叫 `ReqQryInstrument`，返回结构体 `InstrumentField`，字段 `InstrumentID` / `ExchangeID` / `PriceTick` / `VolumeMultiple` / `ExpireDate`
- tqsdk 的 quote 对象也是同一套词（`price_tick` / `volume_multiple`）
- 国际侧：IB / FIX 协议同样用 Instrument 指合约

`InstrumentSpec` 的字段几乎就是 CTP `InstrumentField` 的镜像：

| InstrumentSpec 字段 | CTP InstrumentField 字段 |
|---|---|
| `price_tick` | `PriceTick` |
| `multiplier` | `VolumeMultiple` |
| `exchange` | `ExchangeID` |
| `last_trade_date` | `ExpireDate` |

所以 `InstrumentSpec = 合约规格`，名字从柜台协议借来，语义对齐 A′（apply_quote 回填的正是 InstrumentField 那套字段）。

~~**若嫌行业黑话，备选 `ContractSpec`（合约规格）**——更直白，同样是领域词。取舍见"未拍板事项"。~~ → **已拍板：不改名**（§六-1）——Instrument 对齐 CTP 行业词且与 A′ 语义闭环，备选 ContractSpec 否决。

### 2.2 per-instrument 与 per-product 的区别（2026-09-15 术语修正：原稿误用 per-symbol）

两个**粒度**（领域概念），外加一个只是**代码字符串叫法**的词：

| 概念 | 中文 | 例 | 一份档案管多少 | 换月时 |
|---|---|---|---|---|
| **product** | 品种 | IF / IH / AU / PTA | 整个品种族（IF2509、IF2512、IF2603…） | 不换 |
| **instrument** | 合约 | IF2509 / IF2512 / AU2512 | **一张**具体合约 | IF2509 → IF2512 换一份新的 |
| symbol | 代码字符串（非粒度） | `KQ.m@CFFEX.IF` / `CFFEX.IF2609` | ——（本仓字段词汇：signal_symbol / trade_symbol） | —— |

> **术语修正**：原稿写 "per-symbol"，是照代码字段名（signal_symbol / trade_symbol）说话；
> 领域术语应为 **per-instrument**。判据：主连 `KQ.m@CFFEX.IF` 是 symbol 但**不是**
> instrument（滚动引用，无到期日）；`CFFEX.IF2609` 才是 instrument。spec 里真正逐份不同的
> 字段（last_trade_date / trade_symbol / 该合约涨跌停）描述的是后者。
> 之所以直觉上 "symbol 更像 product"：股票世界里 ticker/symbol = 产品级标识（AAPL）；
> 期货因一个品种裂成一堆月份合约，symbol 字符串被迫带月份才唯一，于是期货代码惯例里
> "symbol" 指完整合约代码——两个世界的用法打架，"instrument" 才是无歧义的词。

**举例**：IF2509 和 IF2512 是同一品种（IF）的两张合约。

- `multiplier=300`、`price_tick=0.2`：IF 全族都一样 → **per-product** → 放 ProductProfile 兜底有底气
- `last_trade_date`：IF2509 = 2025-09-19，IF2512 = 2025-12-19，每张合约不同 → **per-instrument** → 品种档案给不了，只有具体合约知道
- `trade_symbol`：天生属于一张合约 → per-instrument

所以 `InstrumentSpec` 是 **per-instrument 对象**（每张合约一份，换月即换新的一份），内部携带着从 per-product 档案播种来的通用值（`for_product(profile, ...)` 的形态 = 拿品种档案打底 + 落具体 instrument 的专属值）。

**代码里的直接印证**（`ProductProfile.py` 现成的函数对，勿混用）：

```python
parse_product("CFFEX.IF2609")     -> "IF2609"   # 保月份 = 认到具体合约（instrument 粒度）
parse_product_key("CFFEX.IF2609")  -> "IF"       # 剥月份 = 查品种档案（product 粒度）
```

这对函数的分工就是"instrument 与 product 是两个粒度"的体现（test_period_profile.py:133 钉死前者行为）。

### 2.3 为什么 InstrumentSpec 不叫 InstrumentProfile

（回应"它不是标定资产"）

| | ProductProfile | InstrumentSpec |
|---|---|---|
| 值的来源 | **交易经验标定**（min_r_points=3.0、r_multiple_tp=2.0，要 git 评审守护） | **交易所/部署事实**（乘数 300、最小变动 0.2，照抄即可） |
| 粒度 | per-product（一行管全族） | per-instrument（每张合约一份） |
| 变更纪律 | 改 = 改代码资产，走评审 + 对账测试 | 换月自动重播种，不算"调参" |

"Profile" 后缀在本仓语义 = 经验标定的领域档案。InstrumentSpec 的内容是事实不是标定，且粒度是 instrument 不是 product——两个理由都不配叫 Profile。

### 2.4 代码术语审计（2026-09-15 追加）：结论 = 标识符不改名

**问题**：per-instrument 术语修正（§2.2）之后，代码里 `signal_symbol` / `trade_symbol` 等标识符是否要跟着改？

**审计结论：不改。** 逐项理由：

| 标识符 | 实际持有的值 | 审计判定 |
|---|---|---|
| `InstrumentSpec`（类名） | 每张合约一份的静态规格 | **本来就是 per-instrument 命名，正确**——术语分析反而确认了它 |
| `ProductProfile`（类名） | 一行管全族 | 本来就是 per-product，正确 |
| `signal_symbol`（89 处/17 文件） | `KQ.m@CFFEX.IF`（主连）或 CLI 直给的 `CFFEX.IF2609` | **改名反而错**：主连是滚动引用、无到期日、无 last_trade_date，不是 instrument——它就是"信号源的代码字符串"。此处 symbol 用的正是"代码字符串"义，名实相符 |
| `trade_symbol`（129 处/26 文件） | `CFFEX.IF2609`（真实合约代码） | 是 instrument 的**代码字符串**。"symbol = 合约代码字符串"是 API 界通行用法，且与 signal_symbol 对仗工整；SimNow 下单处 `api.insert_order(symbol=self._trade_symbol)`——**tqsdk 自己的参数名就叫 symbol**，改名会在调用边界制造错位 |

**为什么"symbol"与"instrument"不冲突**：两者各占一层语义——instrument / product 是**概念粒度**（哪一层事实），symbol 是**字符串叫法**（代码怎么写）。一张 instrument 的代码字符串照样可以叫 symbol。tqsdk 自身两种都用：quote 的身份字段叫 `instrument_id`（概念名），下单参数叫 `symbol`（字符串名）——恰证明两个词分工明确，不是谁对谁错。

**成本/收益**：218 处纯机械替换 + 若干测试钉死字符串，换来的可读性增益 ≈ 0（类名 InstrumentSpec / ProductProfile 本来就对）。

**替代动作（低成本高收益）**：术语治理落在**文档层**——概念讨论、docstring、交接文档统一按 §2.2 的三行表用词（instrument = 具体合约 / product = 品族 / symbol = 代码字符串）。可在 InstrumentSpec.py 与 ProductProfile.py 的模块 docstring 各加一小段术语锚点（约 5 行），随 §五 步骤② 搭车做，不单开一轮。

---

## 三、目标布局（重做一遍的答案）

```
Trading/Infra/
├── TradingClock.py      # ← Types.py 的时间部分：CN_TZ / 交易日 / 夜盘边界
├── Records.py           # ← Types.py 的枚举+记录部分：交易域的词汇表
├── PeriodProfile.py     # （不变）周期档案
├── ProductProfile.py    # （不变）品种档案
├── InstrumentSpec.py    # ← 瘦身后只留静态规格（名不变，名实相符）
├── InstrumentState.py   # ← 拆出：运行时有效值 + 定价成本
├── StateDB.py           # ← Store.py 改名：SQLite 状态库
└── EventLog.py          # （不变）事件日志，名字够清楚
```

### 各文件理由

**1. InstrumentSpec.py → 拆成 InstrumentSpec.py + InstrumentState.py**

拆开后每个名字自解释，且延续"类名 = 文件名"惯例（PeriodProfile.py 住 PeriodProfile 类）。保留 "Spec" 后缀：**Profile = 经验标定的领域档案（随品种/周期变），Spec = 交易所/部署事实（每张合约一份）**——后缀编码语义，不混用。

**2. Types.py → 拆成 TradingClock.py + Records.py**

- `TradingClock.py`：回答"现在几点、今天算哪个交易日"——`CN_TZ`、`now_cn`、`now_ms`、`trading_day_*`、`NIGHT_SESSION_START_HOUR`。顺带吸收 PeriodProfile.py 里的 `ts_scale` / `norm_delta_sec` / `parse_hhmmss`（时间语义目前散在两个文件，本身就是个小 wart）
- `Records.py`：七个记录（`Signal` / `Bar` / `Order` / `Position` / `Trade` / `Decision` / `ExitPlan`）+ 五个枚举（`Side` / `OrderIntent` / `AccountState` / `EngineState` / `DecisionType`）= 整个引擎流通的**交易词汇表**。备选 `Domain.py`，但 Records 更直白
- 不想拆的退路：改名 `TradingTypes.py`——只是给抽屉贴更大的标签，不推荐

**3. Store.py → StateDB.py**

纯改名即可治好。名字直接回答"存什么（状态）+ 存在哪（DB）"，与测试用语、main.py 产物 `state.db` 对齐。备选 `Persistence.py`，但和 Store 一样只说机制，不推荐。

---

## 四、命名三原则（可复用）

1. **名字 = 领域概念，不是技术机制**（TradingClock 优于 TimeKit，StateDB 优于 Persistence）
2. **一个文件一个概念，名字必须覆盖全文件**——名字盖不住时就该拆，而不是起个更大的名字（Types.py 的教训）
3. **后缀编码语义**：Profile（标定档案）/ Spec（部署事实）/ State（运行时）/ Records（词汇表）——不混用

---

## 五、迁移成本与顺序（供拍板）

| 步骤 | 改动 | 引用面 | 风险 |
|---|---|---|---|
| ① Store.py → StateDB.py | 纯改名 + 批量替换 import | 34 文件 | 最低 |
| ② InstrumentSpec 拆两份 | 拆文件 + import 调整；**搭车**：两个 Profile/Spec 模块 docstring 加术语锚点（§2.4） | 集中在 main / Engine / SimNow / 少数测试 | 中 |
| ③ Types.py 拆两份 | 拆文件 + import 调整；**搭车（拍板 4）**：PeriodProfile 的 `ts_scale` / `norm_delta_sec` / `parse_hhmmss`（含阈值常量 `_TS_MS_THRESHOLD` / `_DELTA_MS_THRESHOLD`）一并迁入 TradingClock | 49 文件 59 处（`now_cn` 全仓在用） | 面最广 |

~~③ 可分步：`Types.py` 留 re-export 桩过渡（`from .TradingClock import *`），消费方逐批切换，切完删桩。~~ → **已拍板：一步到位，不留桩**（§六-3）——`Types.py` 直接删除，49 文件 59 处 import 按符号归属一次性改指 `TradingClock` / `Records`。

**每步做完跑全量回归（51 脚本，基线 = 50 过 / 1 环境失败 test_p20 [8] tqsdk），并按交付规范打 zip 到 deliveries/（保留仓库目录层级）。**

---

## 六、未拍板事项（等用户决策）

1. ~~**`InstrumentSpec` 是否改叫 `ContractSpec`**：Instrument 对齐 CTP 行业词，Contract 更直白。二者取一~~ → **已拍板：不改**（2026-09-15）——Instrument 对齐 CTP 行业词（InstrumentField）且与 A′ 行情回填语义闭环；备选 ContractSpec 否决
2. ~~**Types.py 拆还是只改名**：拆 = 治本（TradingClock + Records）；只改名 TradingTypes = 治标~~ → **已拍板：拆**（2026-09-15）——TradingClock.py + Records.py 治本；只改名 TradingTypes 的治标路线否决
3. ~~**③ 是否用 re-export 桩过渡**：一步到位 vs 分批切~~ → **已拍板：一步到位**（2026-09-15）——不留桩，`Types.py` 直接删除，import 一次性切换
4. ~~**是否同时把 PeriodProfile 的时间工具函数（ts_scale / norm_delta_sec / parse_hhmmss）挪进 TradingClock**：顺手治，但会扩大 ③ 的改动面~~ → **已拍板：做**（2026-09-15）——随 ③ 搭车，含阈值常量，PeriodProfile 改为从 TradingClock import（Infra 内部纯工具依赖，不违反既有纪律与红线）
5. **改名的时机**：单独一轮做，还是搭下次功能改动顺车（未拍板；§五 ①②③ 三步可同轮分步做，每步独立回归）
6. ~~`signal_symbol` / `trade_symbol` 等标识符是否按 per-instrument 术语改名~~ → **已审计并拍板：不改**（2026-09-15，分析见 §2.4；理由：signal_symbol 持主连字符串改名反而错、trade_symbol 在 tqsdk 调用边界同名、218 处替换零收益。术语治理只落文档层）

---

## 七、代码核对结论（对照 custom-dev 最新代码，2026-09-15）

> 核对方：jbxenzxy/chan.py 分支 `custom-dev`（HEAD `6b7925d0f6`，2026-09-14T14:51）。方法：GitHub API 取文件树 + raw 通道逐文件下载 11 个关键文件 + 全仓 236 个 `.py` 批量 grep。完整报告见同包 `Infra命名治理_核对报告.md`。
> 总判断：**本方案核心诊断与方案前提基本正确，可直接拍板实施**；仅一处行号引用硬伤（已就地改准），其余为小的事实性偏差。

### 7.1 核心诊断全部成立（代码实证）

- `InstrumentSpec.py` 确住两份生命周期不同的东西：`InstrumentSpec`（BaseModel 静态, L58）+ `InstrumentState`（运行时, L255，含 `apply_quote` 定价回填）——"名不副实"成立。
- `Types.py` 实测恰好 **5 枚举 + 7 记录**：Side / OrderIntent / AccountState / EngineState / DecisionType；Signal / Bar / Order / Position / Trade / Decision / ExitPlan；外加时间函数 CN_TZ / now_cn / now_ms / NIGHT_SESSION_START_HOUR → "万能抽屉名"诊断成立。
- `Store.py` 确为 SQLite 状态库（`state.db`、多张 CREATE TABLE、`class Store` L76）→ 改名 `StateDB` 治本。
- `parse_product` / `parse_product_key` 语义与本文一致（`CFFEX.IF2609` → `IF2609` 保月份 / `IF` 剥月份）。
- `PeriodProfile` 的 `ts_scale` / `norm_delta_sec` / `parse_hhmmss` 及 `_TS_MS_THRESHOLD` / `_DELTA_MS_THRESHOLD` 均存在 → 拍板④迁 `TradingClock` 的前提成立。
- `describe_unknown_product` 文案含「拒绝启动交易引擎」「禁止启动」，test_p20 / test_p47 断言钉死；`SimNow` 下单 `api.insert_order(symbol=self._trade_symbol)` 与 tqsdk 参数本名一致 → §2.4「标识符不改名」结论成立。

### 7.2 需修正的事实性偏差（已处理 / 待实施校准）

| 严重度 | 位置 | 原文 | 实际 | 处理 |
|---|---|---|---|---|
| 🔴 硬伤 | §2.2 / §附 | `test_period_profile.py:114` 钉死 `parse_product` 保月份 | 保月份断言在 **L133**（L114 实为「未知 freq 容错」）；错误源自 `ProductProfile.py` docstring 原样继承 | **本文已改 L133**；`ProductProfile.py` docstring 同步改 L133（同包已交付） |
| 🟡 | 表头 / §附 | 分支 `09143` | 规范远程分支 `custom-dev` | **本文已改 `custom-dev`** |
| 🟡 | §附 | 基线 `101fca5` | 已落后 HEAD 6 个 commit | 实施时刷新快照 |
| 🟡 | §附 | `ProductProfile` 19KB | 实际 17.0KB（余 InstrumentSpec/PeriodProfile/Store 大小亦略高） | 实施时校准 |
| 🟡 | §2.4 | `signal_symbol` 89/17、`trade_symbol` 129/26、合计 218 | 实测 87/16、124/24、合计 211 | 数字略膨胀，结论不变 |
| 🟡 | §附 | `InstrumentState` L256、`derive_exchange` L584 | 实际 L255、L583 | off-by-one，实施时校准 |

---

## 附：现状快照（动手前的核对锚点）

- 分支 `custom-dev`，基线 `101fca5`（已落后 HEAD 6 个 commit，实施时刷新），diff = 38 文件 +1208/-612
- `Infra/` 现有 7 个 py：EventLog(3KB) / InstrumentSpec(37KB) / PeriodProfile(10KB) / ProductProfile(19KB) / Store(14KB) / Types(23KB) / __init__
- InstrumentSpec.py 内：`InstrumentSpec`（L58，pydantic 静态）、`InstrumentState`（L256，运行时）、`for_product`（L137）、`derive_exchange`（L584）
- 契约勿破坏：`describe_unknown_product` 文案钉死子串「拒绝启动交易引擎」（test_p20）、「禁止启动」（test_p47）；`parse_product` 保月份行为钉死（test_period_profile L133）
