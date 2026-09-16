# Trading/Infra 划分与费率治理（交接文档）

> **状态**：设计已拍板（§0.2 六项），**实施未开始、未动一行代码**。
> **代码基线**：`jbxenzxy/chan.py` 分支 `custom-dev`，HEAD **`840324d1a8`**（2026-09-15T02:41:28Z）。
> **拉取方式**：GitHub API 文件树 + raw 通道；全仓 **236 个 `.py` 全部下载，0 失败**；另取该 HEAD 之前 4 个提交的完整 diff。
> **测试基线**：**52 / 52 全绿**（52 个测试脚本，194s，含网络门控 `test_p20`）。
> **本文是唯一必读文档**。它**取代**并合并了此前两份：
> `Docs/Infra文件命名治理_交接文档.md`（旧交接稿）与 `Docs/Infra命名治理_再设计_20260915.md`（再设计稿）。
> 两者的**独有内容已全部并入本文**（对应关系见 §0.4），**旧文件请删除**（清单见 §附E）。
>
> **按角色跳读**：
> | 你是 | 读 |
> |---|---|
> | 拍板人（只需决策） | §1 结论摘要 → §8 仍未决 |
> | 实施者（要动手） | §0.2 拍板结论 → §7 实施顺序与逐文件规格 → §附A 证据索引 |
> | 评审者（要核对） | §2 事实校准 → §4 关键修正 → §附A / §附B |
> | 接手者（新来的人） | §0 需求原文 → §附F 术语锚点 → §5 推荐划分 |

---

## §0 需求原文存档

> 按既有纪律：交付分析/设计/交接类文档时，**用户最初的需求原文一字不改放在开头**（本节即 §0.1、§0.2），并配「原文条目 → 本文位置」对照索引（§0.3）。
> 理由：接手者可能拿不到原始对话；**分析结论可能出错，原文不会**。

### §0.1 第一轮原文（划分 + 费率静态化）

> ⑴ 拉取 https://github.com/jbxenzxy/chan.py/tree/custom-dev 最新代码，再次审核下 @"C:\my_chan_project\Docs\Infra文件命名治理_交接文档.md" 文档，我看里面有过期的术语，比如：min_r_points=3.0
> ⑵ 再次审核下ProductProfile.py、InstrumentSpec.py 和 PeriodProfile.py的划分。按文档中对品种product，合约instrument的定义，我感觉 Product.py 放品种共性（比如IF这个品种，最小变动 0.2、乘数 300、盈亏比、开仓费率、平昨费率、平今费率），Instrument.py 放置合约特有（IF2509 比如到期日，涨跌停价，最新价，每个合约不同）；Period.py放置周期特有（IF2509 含四个周期30分/5分/1分/15秒，啥事特定周期相关的？我没想清楚）；现在的Spec（交易所/部署事实）和 Profile（交易经验标定）分类画蛇添足，因为按交易经验标定和部署实施分类，纯粹是自娱自乐，没有带来实际价值
> ⑶ 而且"开仓费率、平昨费率、平今费率"三种费率主要用于判断是否平今更优吧？用从交易商拿到的数据 @"C:\my_chan_project\Docs\手续费标准-.xlsx" 作为静态配置即可（因为这个数据很少变动），无需像现在这样还要等第一次交易成功后才能确定，搞太复杂了
> 你结合以上几点（以上几点只是我的初步想法，仅仅是抛砖引玉），跳出现有框架，想想是不是有更好的设计方案。

### §0.2 第二轮原文（拍板）

> ⑴ 中金所平今到底按哪个口径（万3.45 还是万2.3）？白银同理 —— 费率相关的以手续费标准-.xlsx为准
> ⑵ 费率放 Product 行内
> ⑶ 实施顺序我建议 P-A 费率静态化 → P-B 字段归位 → P-D 术语锚点 → P-C 改名 —— 接纳你的建议
> ⑷ InstrumentSpec + InstrumentState 要不要合并成一个 Instrument？—— 我倾向合并，为啥会破坏配置对象不可变？举例说明
> ⑸ MEMORY.md 中第二条：每个阶段完成后，把改动文件按仓库原目录层级打包 zip，再补充一下：md文档无需打包到zip中（你看这次你给我的zip中有md文档，然后又单独发了md文档，会重复）
> ⑹ 你给我三个文档「证据_计数_探针_基线_20260915.md」，Infra文件命名治理_交接文档.md 和 Infra命名治理_再设计_20260915.md，让我都晕了，不知道看哪个？按我理解，既然有了  Infra命名治理_再设计_20260915.md 文档，此文档也作为交接问题，另一个 Infra文件命名治理_交接文档.md 就不需要了吧？

### §0.3 原文条目 → 本文位置（含落实状态）

| 轮次 | 你要点 | 本文位置 | 结论一句话 | 状态 |
|---|---|---|---|---|
| 一 | ⑴ 拉新代码 + 文档过期术语 | §2 | 确认，抓到 **11 处**过期项，其中 3 处 🔴（含 1 处**文档内部自相矛盾**、1 处整条轴缺失） | 已校准 |
| 一 | ⑵ 三文件划分 / Spec-Profile 二分"画蛇添足" | §3、§5 | **成立且能量化**：为一份重复数据付出约 **122 行生产代码 + 1 个测试文件**，其中 **24 行结构性不可达** | 已采纳 |
| 一 | ⑵ "Period 放什么" | §5.2 | **现在没有"周期特有的策略参数"了**；它是"周期↔秒登记表 + 与 chan.py 的对账护栏" | 已答复 |
| 一 | ⑶ 三费率只为判断平今；改用券商表静态配置 | §6 | 方向成立，且比设想更值得做；**但要补一处**：费率一半用在**成交会计**，不只用于决策 | 已采纳 |
| 二 | ⑴ 平今口径 | §6.5 | **以 xlsx 为准**：中金所平今 **万2.3**（代码现值万3.45 作废）；白银基准 **万0.1** | **已裁决** |
| 二 | ⑵ 费率放哪 | §6.3 | **Product 行内** | **已裁决** |
| 二 | ⑶ 实施顺序 | §7 | **P-A → P-B → P-D → P-C** | **已裁决** |
| 二 | ⑷ 是否合并 Instrument | §4 | **合并可以，但你的前提是错的**：配置对象**现在就不不可变**（4 条实证）→ 见 §4.2 举例 | **已裁决** |
| 二 | ⑸ 交付格式补一条 | §7.5 | **md 不进 zip**；zip 只装需按目录层级覆盖的代码/配置 | **已落记忆** |
| 二 | ⑹ 文档太多 | 本文 | **三份 → 一份** | **已合并** |

### §0.4 旧文件内容去向（合并对照）

| 旧文件 | 独有章节 | 去向 |
|---|---|---|
| `Infra文件命名治理_交接文档.md` | §一 问题陈述 / §二 概念澄清（为何叫 Instrument、per-instrument vs per-product、为何不叫 Profile、标识符审计） | **全文并入** §附F |
| 同上 | §三 目标布局 / §四 命名三原则 | 原则 1、2 并入 §附C；**原则 3（后缀编码语义）已废止**，理由见 §3.3 |
| 同上 | §五 迁移成本与顺序 / §六 未拍板 | 并入 §7（顺序）/ §8（未决） |
| 同上 | §七 代码核对 / 附 现状快照 | 并入 §2 与 §附D |
| `Infra命名治理_再设计_20260915.md` | 全部 | **全文并入** §1–§7、§附A–§附B |
| `reports/证据_计数_探针_基线_20260915.md` | 证据原文 | 关键条目并入 §附A / §附B / §4.2；**该 md 不再单独交付**（避免"三份文档"） |

---

## §1 结论摘要

1. **`min_r_points` 确实没了，你记的没错**——2026-09-14 删除。HEAD 全仓 34 处提及**全部**在注释 / docstring / 负向断言里，**0 处实代码**。文档里那个 `3.0` 现在指向的是另一件东西：`LayeredExitPolicy.r_alert_a_floor = 3.0`（`Exit.py:76`，2026-09-15 新增），它是**告警灵敏度旋钮**，`Exit.py:70-75` 明写「**不参与、也不会改变** R = max(A, B) 的取值」。文档把它写成"交易经验标定值"，语义正好反了。

2. **"Spec = 交易所事实 / Profile = 经验标定"这条分界线，在你 ⑶ 的需求下当场自相矛盾**——你要求把费率（交易所/券商事实）放进 Product。同一文件里住着"标定"和"事实"，后缀语义就废了。**建议直接废掉这条规则**，换回最朴素的一条：**文件名 = 它描述的那个领域的粒度**（见 §3.3）。

3. **Spec/Profile 二分的真实代价可量化**：为在两份文件里各存一份 `price_tick` / `multiplier`，代码付出了 `for_product()` 播种桥、`_seed_instrument()`、`model_dump(exclude={...})` 的躲闪、`_check_spec_drift` + `_check_spec_drift_online` + 两个置位标志（约 99 行）+ `test_p49_spec_drift.py`。更糟的是**其中"离线对账"那 24 行在启动路径上结构性不可达**（§3.4-D，有探针实证）。这不是"自娱自乐"，是**负收益**。

4. **`InstrumentSpec.py` 真正的病不是"名不副实"，而是"一个类装了四条互相正交的变化轴"**（品种 / 交易所 / 合约 / 部署）+ broker 轴的费率。所以"按生命周期拆文件"只是外科手术——**不先归位就拆，等于把一份错位的东西切成两份错位的东西**。

5. **⚠️ 本轮最重要的修正：`InstrumentSpec` 现在就不是只读的**（§4）。它的 `model_config` 只有 `{'extra': 'forbid'}`、**没有 `frozen`**；而生产代码 `SimNow.py:718/803/856` 与 `main.py:108` 正在**就地改它**，改完的值**正被引擎的分支逻辑读**（`Engine.py:977` / `:1033` / `:1237`）。探针实证：`broker.spec is engine.spec is cfg.instrument` → **是同一块内存**。所以⑷里"合并会破坏配置对象不可变"这个前提**不成立**——不可变性从未实现。

6. **费率静态化你的方向对，实现形态要按"两档 × 两种计价 × 合约覆盖"三层**：从 xlsx 派生的 8 品种表见 §6.1。你的表自己揭穿了"费率 = 品种常量"：上期所对**特定月份合约**加收（黄金 6/12 月 10→20 元/手、白银 万0.1→万0.5、螺纹钢 万0.2→万1、燃料油按月分了 6 档）。

7. **"三档费率"实际只有两档**——xlsx 全表 90 条带独立平今行的条目里，**没有任何一行把"平昨"单独列出来** → **平昨恒等于开仓**。所以 `close_fee_rate` 是 `open_fee_rate` 的副本，可以删（§6.4）。

8. **`prefer_lock_over_closetoday` 这个手写布尔可以删掉，改成纯派生**——判定是**两层**：
   - **① 能力闸门**（交易所事实，不看费率）：只有 SHFE/INE 有 CLOSETODAY 指令，其余四家传平今直接报错（**发不出去**，不是"不划算"）→ 强制锁仓；
   - **② 费率会计**（由费率表派生）：比「**平今路径 2 笔**」vs「**锁仓路径 4 笔**」——
     `X + Y` vs `4X`（X = 开仓单手费，Y = 平今单手费；xlsx 已证 平昨 ≡ 开仓）
     ⟺ 约掉手数：**`Y < 3 × X` → 走平今；否则走反向开仓（现状流程）**。
   **本轮定稿（按你的口径）**：终态取**回到空仓**。实测锁仓路径要 **4 笔**成交才回到空仓
   （开 A + 反向开 B + 平 A + 平 B），且这 4 笔**全部由引擎现有转移表自动走出，不需要任何新功能**（§6.5.3）。
   ⚠️ 仓库现成的 `evaluate_closetoday_economy()` 是 **1× 口径**（只比 平今 vs 平昨**单笔**），
   **漏算了锁仓路径多出的两笔平仓** → 判据错（CU 上结论相反）。删开关时它属被删项，派生逻辑新写（§6.5.4）。

9. **三档费率不只用在你说的"判断平今更优"**——它另一半用在**成交会计**（`cost_points` → `Trade.cost_points/net_cash` → `state.db` → 前端/报表；调用点 `Engine.py:1501`、`Reconcile.py:209`）。所以"费率的单位能表达几种形态"是**回测盈亏对不对**的问题，不只是决策问题。现行 `cost_points()` **只有比例一种口径**，表达不了 PTA 3 元/手、黄金 10 元/手 → **离线回测净盈亏会错**。

10. **本轮已把三份文档收敛为一份**（本文）。后续每阶段交付：**md 单独给，不进 zip**。

---

## §2 文档过期术语 / 事实校准（回应 ⑴）

> 方法：以 HEAD `840324d1a8` 工作树为准，全仓 236 个 `.py` + `Frontend/` 全量 grep。
> 口径：**不含**"注释里提到某概念已删除"这类解释性字样（那是**留档**，不是残留）。
> 这些校准**已落到旧文档**里；本文保留清单是为了说明"为什么这轮要重写整份文档"——**结论本身也被动摇了**。

| # | 位置 | 原文 | 实际（HEAD 证据） | 级别 |
|---|---|---|---|---|
| 1 | §2.3 表 | 「交易经验标定（**min_r_points=3.0**、r_multiple_tp=2.0，要 git 评审守护）」 | `min_r_points` **2026-09-14 已删除**。全仓 34 处提及**全在注释/docstring/负向断言**：`Exit.py:165`、`ProductProfile.py:88`、`Config.py:327`/`:581`、`test_p8_layered_exit.py:418`、`test_p45:97`、`test_p46:150`、`test_p47:156`、`test_period_profile.py:137` …，**0 处实代码**。现存那个 `3.0` 是 `r_alert_a_floor`＝**告警灵敏度**，且**不是配置项**（`ExitPolicyParams` 是 `extra="forbid"`） | 🔴 |
| 2 | §2.3 表（整表） | 「ProductProfile = 交易经验标定 ／ InstrumentSpec = 交易所·部署事实」 | 该分界线在 §3.3 被证不成立，且与 ⑶ 直接冲突（费率＝事实，要进 Product） | 🔴 |
| 3 | §七 标题 | 核对基线 `HEAD 6b7925d0f6`（2026-09-14T14:51） | HEAD 已是 **`840324d1a8`**，其后 4 个提交：`d4c0172125`（旧文档入库 + ProductProfile docstring）、`eac333991b`（ExitConfig 参数序校验器）、`3e7775035f`（`r_alert_a_floor`）、`840324d1a8`（**新增 `Docs/手续费标准-.xlsx`**，改 Exit.py / Config.py 注释） | 🟡 |
| 4 | 附·现状快照 | 基线 `101fca5`「已落后 HEAD 6 个 commit」 | `101fca5` 距 HEAD 已是 **10 个提交**（"6" 是站在 `6b7925d0` 时的数字） | 🟡 |
| 5 | 附·现状快照 | `EventLog(3KB) / InstrumentSpec(37KB) / PeriodProfile(10KB) / ProductProfile(19KB) / Store(14KB) / Types(23KB)` | 实测字节：`3219 / 36629 / 9519 / 17422 / 13388 / 22402` → 应为 **3 / 36 / 9 / 17 / 13 / 22 KB** | 🟡 |
| 6 | §2.4 正文 | `signal_symbol` **89 处/17 文件**、`trade_symbol` **129 处/26 文件**、合计 **218** | 实测 **87/16**、**124/24**、合计 **211**——与同一文档 §7.2 的校准行完全一致，但 §2.4 正文没回填 → **文档内部两组数字互相矛盾**，且 §2.4 的"成本/收益"论证建立在 218 上 | 🟡 |
| 7 | §五 步骤③ | 「49 文件 59 处（**`now_cn` 全仓在用**）」 | 引用面数字基本对（`Types` 整体 import 面 = **49 文件 / 60 处**）；但括号里的**理由不准**：`now_cn` 单看只有 **14 文件 / 38 处** | 🟡 |
| 8 | 附·现状快照 | `InstrumentState`（**L256**）、`derive_exchange`（**L584**） | 实测 **L255 / L583**（§7.2 校准过，附没同步） | 🟡 |
| 9 | 全文 | 完全没有覆盖**费率（Phase 12）这条轴** | 而费率机器横跨 5 个文件（`fee_source` 58 处），正是 ⑶ 要动的东西 | 🔴（缺口） |
| 10 | §7.1 | Types.py「5 枚举 + 7 记录」；Store.py `class Store` L76；PeriodProfile 三函数 + 两阈值 | **全部复核成立**：枚举 `Side` L107 / `DecisionType` L124 / `OrderIntent` L138 / `AccountState` L173 / `EngineState` L199；记录 `Signal` L208 / `Bar` L293 / `Order` L314 / `ExitPlan` L339 / `Position` L362 / `Trade` L446 / `Decision` L474；`Store` L76；`ts_scale` L75 / `norm_delta_sec` L87 / `parse_hhmmss` L123 / `_TS_MS_THRESHOLD` L66 / `_DELTA_MS_THRESHOLD` L72 | ✅ |
| 11 | §2.2、§附 | `parse_product("CFFEX.IF2609")=="IF2609"` 钉在 `test_period_profile.py:133` | 正确：`test_period_profile.py:132-133`；`ProductProfile.py:206` 的 docstring 也已同步 | ✅ |
| 12 | §7.1 | 「全仓 236 个 `.py`」 | 正确：HEAD **236 个 `.py` / 351 blob** | ✅ |

**顺带提醒**：`Docs/**` 是刻意保留的审计快照（术语护栏不扫它）。但**活文档不在豁免范围**——它 §七 自称"对照 custom-dev 最新代码"，所以上面过期项要修。

> ⚠️ **改 `Trading/` 下任何字符串（含注释、日志、断言描述）前必须先过 `test_p26_terminology_guard.py`**：它扫描 `Trading/` `App/` `Frontend/` 与仓库根 `verify_*.py`（**不扫 `Docs/**`**）。禁用词见 `C:\Users\river\.workbuddy\memory-chanpy.md` 的术语护栏。

---

## §3 现有三文件划分的真实缺陷（回应 ⑵）

### 3.1 换一把尺子：从「谁定的」换成「什么变了它才变」

文档用的尺子是**来源**（交易所事实 / 经验标定 / 部署实施）。这把尺子不好用，因为它要求你**先知道一个值是谁定的**，才知道该放哪——而这件事恰恰是看不出来的（`price_tick` 是交易所定的，可它同时"随品种变"，那它算事实还是算品种？）。

换一把：**只问一句"什么变了它才变"**（variation key）。答案立刻唯一：

| 变异轴 | 什么变了它才变 | 例子 | 现在的家 |
|---|---|---|---|
| **period** 周期 | 换 K 线周期 | `bar_secs=1800`（30m） | `PeriodProfile.py` |
| **product** 品种 | 换品种 | `multiplier=300`、`price_tick=0.2`、`r_multiple_tp=2.0`、**三档费率** | `ProductProfile.py`（+ `InstrumentSpec` 的副本） |
| **exchange** 交易所 | 换交易所 | CZCE 只吃 FAK、只有 SHFE/INE 有平今指令 | **无家**（散在 `InstrumentSpec` + `Engine.py:977`） |
| **instrument** 合约 | 换月份合约 | `trade_symbol=CFFEX.IF2509`、`last_trade_date=2025-09-19` | `InstrumentSpec.py` |
| **session** 会话/行情 | 盘中每一刻 | 当日涨跌停绝对价、有效 tick、`verified` | `InstrumentState` |
| **deployment** 部署 | 换账户/换机器/调参数 | `slippage_ticks`、券商加收 | `InstrumentSpec.py` |

### 3.2 `InstrumentSpec.py` 真正的病：一个类装了四条正交变化轴

文档说它"名不副实"（`InstrumentSpec` + `InstrumentState` 两个生命周期住一起）。这诊断**浅了一层**。598 行 / 36.6KB 的真正成因是：

```
InstrumentSpec 一个类里同时装着：
  · product 轴    price_tick(L74) / multiplier(L75)        ← 与 ProductProfile.py:74-75 重复
  · instrument 轴 trade_symbol(L72) / last_trade_date(L119)
  · exchange 轴   exchange(L104) / order_advanced(L88) → effective_order_advanced(L179)
                  closetoday_first(L89) → supports_closetoday(L201) / limit_*_pct(L115-118)
  · deployment 轴 signal_symbol(L71) / slippage_ticks(L83) / price_band_points(L99)
  · broker 轴     三档费率(L76-78)                          ← 来源是券商报价表
```

四条正交轴挤在一个类里 → 它必然"名字盖不住内容"，而且**拆成两份之后仍然错位**：文档的拆法是"按生命周期拆"（静态 vs 运行时），可 `price_tick` 的错位**不是生命周期问题**，是它**同时属于 product 轴却被复制到了 instrument 轴上**。外科手术做了，病灶还在。

### 3.3 "Spec = 交易所事实 / Profile = 经验标定"这条线为什么站不住

旧文档 §2.3 用 `min_r_points=3.0` 当"经验标定"的例子、用"乘数 300 / 最小变动 0.2"当"交易所事实"的例子。三个问题：

1. **例子已经不存在**：`min_r_points` 2026-09-14 就删了。文档在用一个已删字段给一条命名规则做论证。
2. **"标定"只剩一个字段**：去掉 `min_r_points` / `breakeven_buffer_ticks`（都已删）后，`ProductProfile` 里真正算"标定"的**只有 `r_multiple_tp` 一个**。为一个字段立一条命名规则，太薄。
3. **⑶ 直接把这条线撞断**：费率是**交易所/券商事实**（旧文档明说 `InstrumentSpec` 才是"交易所/部署事实"的家），你要求它进 Product。于是 Product 里同时住着"标定"（`r_multiple_tp`）与"事实"（费率、乘数、tick）——**后缀语义当场自相矛盾**。

> **结论：废掉"后缀编码语义"这条规则**（旧文档 §四 原则 3）。回到最朴素、也最耐用的一条：**文件名 = 它描述的那个领域对象的粒度（product / instrument / period）**。至于某个字段是"事实"还是"标定"，那是**它该不该走 git 评审**的问题，用**另一套手段**表达（§5.4），不要混进文件名。

### 3.4 三处"同一件事存多份"的实证

**A. `price_tick` / `multiplier` 各存两份，还配了一套专职对账**

| 环节 | 位置 | 规模 |
|---|---|---|
| 副本 1 | `ProductProfile.py:74`(multiplier) / `:75`(price_tick) | 2 字段 |
| 副本 2 | `InstrumentSpec.py:74`(price_tick) / `:75`(multiplier) | 2 字段 |
| 播种桥 | `main._seed_instrument()`（`main.py:65-82`）+ `InstrumentSpec.for_product()`（`InstrumentSpec.py:136-164`）+ `model_dump(exclude={"price_tick","multiplier"})` | ~47 行 |
| 对账机制 | `Engine._check_spec_drift()` L1063-1120 + `_check_spec_drift_online()` L1121-1137 + `_spec_drift_checked` / `_spec_drift_offline_checked` 两标志 | ~75 行 |
| 专用测试 | `Trading/Test/test_p49_spec_drift.py`（217 行） | 1 文件 |

合计 **约 122 行生产代码 + 1 个测试文件**，存在的唯一理由是"同一个数字在两个文件里各写了一遍"。**合并副本 → 全部消失**。

**B. 平今这一件事有三个源** —— 见 §6.2（独立成节，因为它是 ⑶ 的主战场）。

**C. 交易所能力判定散在 2 处**

`InstrumentSpec.effective_order_advanced()`（`InstrumentSpec.py:197-199`：CZCE→FAK）声称"一处配置、所有报单点读取，避免 Broker/ 里散落 `if exchange=="CZCE"`"——但 `Engine.py:977` 又硬编码了一次 `if self.spec.exchange == "CZCE": return 1`（OPEN 手数钉 1 手）。**"只改一处"的承诺已破**。

**D. ⚠️ 附带发现：那 24 行"离线对账"在生产路径上结构性不可达**

**代码**：`Engine._check_spec_drift()` 的离线分支（`Engine.py:1096-1119`，含 `_spec_drift_offline_checked` 标志）—— 2026-09-14 评审 P2-3 专门加的，理由是"离线路径永远不查，兜底值过期在离线侧完全静默"。

**探针实证**：

```
情形 A｜走生产启动路径（main.py 口径）：
  用户显式配 price_tick=0.5 / multiplier=100
  → 播种前  cfg.instrument = 0.5 / 100.0
  → 播种后  spec = 0.2 / 300.0        ← for_product() 强制覆盖
  → state = 0.2 / 300.0 ，档案 = 0.2 / 300.0
  → 离线漂移判定命中吗？ False        ← 恒 False

情形 B｜跳过播种的旁路（直接 TradingConfig(...)，不走 _seed_instrument）：
  → state = 0.5 / 100.0 ，档案 = 0.2 / 300.0
  → 离线漂移判定命中吗？ True
```

**为什么恒 False**：`for_product()` 对 `price_tick` / `multiplier` 是**无条件强制取档案值**（`InstrumentSpec.py:159-163`）。所以离线时 `state.price_tick ≡ profile.price_tick`，比较式永假。**这条分支只在"没走启动播种"的非生产路径上才可能命中**——而那条路径恰恰不是它要保护的场景。

**连带两个后果**：
1. `test_p49_spec_drift.py` 的 [1]-[6] **全部只覆盖在线分支**（[1] 用 `verified=False` 但断言的是 **online 标志** `_spec_drift_checked`，离线分支的 `_spec_drift_offline_checked` 无人断言）→ **这 24 行既不可达、也无测试**。
2. 情形 A 顺带暴露一个**真实（且更该管）的隐患**：用户显式写进配置的 `price_tick=0.5 / multiplier=100` 被**静默吞掉**，不报错、不告警。这是 D1 拍板（放弃 `.env` 覆盖品种参数）的既定代价，但"静默"这一半值得显性化。

---

## §4 ⚠️ 关键修正：`InstrumentSpec` 现在就不是只读的（回应 ⑷）

> **你的问题**：InstrumentSpec + InstrumentState 要不要合并成一个 Instrument？我倾向合并，为啥会破坏配置对象不可变？举例说明。
>
> **直接回答**：**它不破坏——因为"配置对象不可变"这件事现在根本没实现。** 下面是四条实证 + 两个"合并后才真正出现"的歧义举例 + 合并的正确做法。

### 4.1 文档宣称 vs 代码事实

三处 docstring 都在宣称只读：

| 位置 | 原话 |
|---|---|
| `InstrumentSpec.py:17-18` | 「它经 `TradingConfig.instrument` 挂进配置树 —— 正因如此，**它必须只读**：TradingConfig 是配置对象，构造后不该有任何字段被行情推着改。」 |
| `InstrumentSpec.py:64` | 「⚠️ 本模型是 `TradingConfig.instrument`，**构造后必须只读**。」 |
| `Engine.py:112-122` | 「`self.spec` = 静态规格……与 Phase 3 之前 `self.spec = cfg.instrument` 的可见性完全等价。」 |

**代码事实（探针输出，可复现）**：

```
1) InstrumentSpec.model_config = {'extra': 'forbid'}      ← 没有 frozen
2) spec.exchange = 'SHFE'  → 不报错，赋值成功             ← 可变
3) broker.spec is cfg.instrument -> True
   id(broker.spec)=1933385833344  id(cfg.instrument)=1933385833344   ← 同一块内存
4) state.spec is cfg.instrument -> True
5) spec.supports_closetoday = True                        ← 引擎分支真的读它
```

### 4.2 举例：不可变性是怎么被破的

**例 1 —— 生产代码正在就地改"配置对象"，而且改完的值被引擎读**

| 写入点 | 代码 | 它改的是谁 |
|---|---|---|
| `SimNow.py:718` | `self.spec.trade_symbol = q.underlying_symbol` | = `cfg.instrument` |
| `SimNow.py:803` | `self.spec.exchange = derive_exchange(self._trade_symbol)` | = `cfg.instrument` |
| `SimNow.py:856` | `self.spec.last_trade_date = _extract_last_trade_date(quote)` | = `cfg.instrument` |
| `main.py:108` | `cfg.instrument.signal_symbol = args.symbol` | `cfg.instrument` 本身 |

而引擎**正在读这些被改的字段**做分支：

| 读取点 | 代码 | 依赖 |
|---|---|---|
| `Engine.py:977` | `if self.spec.exchange == "CZCE": return 1` | `exchange`（被 SimNow 改过） |
| `Engine.py:1033` / `:1279` | `and self.spec.supports_closetoday` | `exchange` 的派生 |
| `Engine.py:1237` | `self.spec.delivery_guard_blocked(today, ...)` | `last_trade_date`（被 SimNow 改过） |
| `Engine.py:441` / `:639` | `my_symbol = self.spec.trade_symbol` | `trade_symbol`（被 SimNow 改过） |

**结论**：所谓"不可变配置"已经是一个**被生产代码原地改、且改写结果直接改变引擎行为**的可变对象。它今天没出事，是因为这些写入是**幂等且方向一致**的（行情解析出的真实合约就是你要的那张），不是因为有护栏。

**例 2 —— 合并后才真正出现的歧义（这是唯一需要你权衡的点）**

现在至少还有**文字边界**：`InstrumentSpec` 的字段看起来都是"配置"，`InstrumentState` 的字段看起来都是"运行时"。合并成一个 `Instrument` 后：

```python
# 合并后，cfg.instrument 就是这个 Instrument
cfg.instrument.price_tick   # ← 这是"你配的 tick"还是"行情回填后的 tick"？
```

带来的实际麻烦有两处，都不是理论问题：

1. **`model_dump()` 会把运行时值当配置导出。** 现存调用点 `main.py:82`（`for_product(**cfg.instrument.model_dump(exclude={...}))`）与 `Tool/SimNow/CrossDayProbe.py:106` 都吃这个 dump；`TradingConfig.redacted_dict()` / `to_dict()`（`Config.py:174/182`）直接 `return self.model_dump()` → **整个配置树快照里会混进 `verified` / `fee_source` / 当日涨跌停**。
2. **`import` 面会把"配置"概念传染出去。** `TradingConfig` 是到处传的对象；一旦它的一个字段是会变的运行时状态，任何"读 cfg 得到启动快照"的假设都不成立——而这类假设现在写在注释里（`Engine.py:112-122`），以后会写在别人脑子里。

### 4.3 合并的正确做法（推荐）

**合并本身是对的**（概念更少、播种桥消失）；**但不能只是"把两个类拼一起"**，要同时把**配置层从 `TradingConfig` 里摘出去**：

```
现在：
  TradingConfig.instrument : InstrumentSpec(pydantic, 无 frozen, 被就地改)
  InstrumentState(spec)    : 运行时有效值 + 定价成本   ← broker/engine 共享

推荐：
  TradingConfig.instrument_config : InstrumentConfig(pydantic, **frozen=True**)
        └ 只放"部署级"：signal_symbol / trade_symbol / order_advanced /
          slippage_ticks / price_band_points / closetoday_first
  Instrument(config, product) : **唯一一份**运行时对象
        ├ 静态：identity(trade_symbol/signal_symbol/last_trade_date)
        ├ 有效值：price_tick / multiplier / upper_limit / lower_limit / verified / source
        └ 定价成本：round_price / align_* / slip_price / cost_*
        ← main.py 建一份，同时交给 broker 与 engine（即现在 state 的位置）
```

**收益**：
- 类从 2 个变 1 个（`Instrument`），**配置类缩到 6 个字段且 `frozen=True` 是真的**——这时"配置不可变"才第一次成为**断言级事实**（pydantic 会给 `ValidationError`，而不是 docstring 里的一句话）。
- `for_product()` 播种桥 / `_seed_instrument()` / `model_dump(exclude=...)` 的躲闪 **全部消失**（tick/乘数/费率直接从 `Product` 读）。
- "启动后只有一份运行时规格"这个不变量**更容易守**：现在靠"main.py 记得传同一个 state 给两处"的人工纪律（`InstrumentSpec.py:33-38` 专门写了一段警告说搞错会"闸门恒拒单"），合并后这个对象就是那个 state。

**代价（必须诚实说）**：
- 波及面最大的一步。实测构造点：`InstrumentSpec(` **152 处 / 38 文件**、`InstrumentState(` **27 处 / 8 文件**、`state.spec` / `x.spec` 引用 **104 处**；绝大多数在测试里（`InstrumentSpec()` 默认构造）。
- 但它**不改名**（`InstrumentSpec` → `InstrumentConfig` 是一处 class 名），所以只读引用不动。

**建议位置**：放进 **P-B（字段归位）**同一步做，或紧随其后单开 **P-E**。理由：P-B 本来就要把 tick/乘数/费率从 `InstrumentSpec` 摘出去 → 那时 `InstrumentSpec` 已经瘦成"部署 + 合约身份"，正好顺势完成合并，避免二次动同一批文件。

### 4.4 若不合并（保守方案）

保留 `InstrumentSpec` + `InstrumentState` 两个类，但**必须补三件**，否则"不可变"继续是幻觉：
1. `InstrumentConfig` 加 `model_config = ConfigDict(extra="forbid", frozen=True)`；
2. `SimNow` 的三处写入（`trade_symbol` / `exchange` / `last_trade_date`）改为写**运行时对象**，不再写配置；
3. `main.py:108` 的 `cfg.instrument.signal_symbol = args.symbol` 改为构造期传入。

> 也就是说：**"不合并"并不省事**，它省的是"改 152 个构造点"，但省不掉"把三处写入从配置挪到运行时"。两条路都要动同一批文件，合并还多赚一个概念。

---

## §5 推荐划分（回应 ⑵ 与"跳出现有框架"）

### 5.1 四个领域文件，按变异轴排

```
Trading/Infra/
├── TradingClock.py    ← Types.py 的时间部分 + PeriodProfile 的时间工具      [时钟]
├── Records.py         ← Types.py 的枚举+记录（5 枚举 + 7 记录）              [词汇表]
├── Period.py          ← PeriodProfile.py                                   [周期轴]
├── Product.py         ← ProductProfile.py + 吸收费率 / exchange / 派生能力   [品种轴]
├── Instrument.py      ← InstrumentSpec.py + InstrumentState.py 归位后合并   [合约轴]
├── StateDB.py         ← Store.py 改名                                      [持久化]
└── EventLog.py        （不变）
```

各轴职责（**这张表就是本文的核心答案**）：

| 文件 | 变异轴 | 装什么 | 与现状的差 |
|---|---|---|---|
| `Period.py` | 换周期才变 | `freq` / `bar_secs` / `SUPPORTED_FREQS`（时间工具迁 `TradingClock`） | **内容基本不变**，只是改名 + 时间工具搬家 |
| `Product.py` | 换品种才变 | `price_tick` / `multiplier` / `r_multiple_tp` / **两档费率** / `exchange`（及派生的 `supports_closetoday`、`order_advanced`、`prefer_closetoday`） | **吸收** `InstrumentSpec` 的 tick/乘数副本 + 费率 + exchange；丢掉 `prefer_lock_over_closetoday` 手写布尔（改派生） |
| `Instrument.py` | 换合约 / 盘中才变 | ① 身份：`signal_symbol` / `trade_symbol` / `last_trade_date`；② 会话内有效值：有效 tick/乘数 / 涨跌停 / `verified` / `source`；③ 定价与成本：`round_price` / `align_*` / `slip_price` / `cost_*` | tick/乘数/费率**不再自带副本**，有效值初值直接取 `Product`；删掉 `for_product` 播种桥 |
| `TradingClock.py` | —— | `CN_TZ` / `now_cn` / `now_ms` / 交易日 / `NIGHT_SESSION_START_HOUR` / `ts_scale` / `norm_delta_sec` / `parse_hhmmss` / 两阈值常量 | 旧文档 §五 拍板 4 已定，照做 |
| `Records.py` | —— | 5 枚举 + 7 记录 | 旧文档 §五 拍板 2 已定，照做 |

**关键差别（相对旧文档的 §三）**：
- 旧文档把 `InstrumentSpec.py` 拆成 `InstrumentSpec.py` + `InstrumentState.py`（**按生命周期拆**）；
- 本文把 tick / 乘数 / 费率**从 Instrument 轴摘出去归回 Product 轴**（**按变异轴归位**），两个对象的**文件边界与旧文档一致**，但**内容归属全变了**——这才是治病的那一刀。

### 5.2 你的追问"`Period.py` 放什么"——诚实答案

**现在没有任何"周期特有的策略参数"了。** 证据（`PeriodProfile.py:162-168` 自己的 docstring）：

- `max_hold_bars` / `max_hold_seconds` / `eod_lead_bars` / `session_end_hhmm` —— 2026-09-08 删
- `max_trades_per_day`（风控五道硬闸门）—— 删
- `signal_max_age_minutes` —— 改为周期无关的 `signal_k_tol_bars`，迁出本档案
- 止盈止损参数（R 地板 / 盈亏比）—— **明确"不随周期变、随品种变"，归 `ProductProfile`**

**所以剩下的只有 `freq` / `bar_secs` 两个字段 × 4 行。** 这不是"没想清楚"，这是**事实就这么多**。它的价值不在"装参数"，而在两件事：

1. **周期↔秒的单一事实源**：`FREQ_SEC` 一处定义，`SUPPORTED_FREQS`、`bar_secs_for(strict=True)`、`bars_per_day` 都从这里长出去；`main.py:130-136` 的启动期周期 fail-fast 也读它。
2. **与 chan.py 主程序的对账护栏**：`test_period_consistency.py` 把本表的 `FREQ_SEC` 与主程序 `Common.CEnum` 的表逐项比对——主程序加周期而这里没跟上，测试直接红。这条护栏是**跨仓一致性**的唯一保险，删不掉。

**建议**：`Period.py` 保留（改名去掉 `Profile` 后缀，因为它已经不是"标定档案"了），并在模块 docstring 里**显式写清"本模块当前只承载周期的时间语义；周期相关的行为参数于 2026-09-08 删除，若未来回归，家在这里"**。这样下一个人不会再问同样的问题。

> 若哪天要砍文件数：`Period.py` 只有 2 个字段，硬要合并的候选是并入 `Product.py`（错，周期与品种正交）或并入 `TradingClock.py`（可接受，但会把"周期登记"混进"时钟"）。**都不建议**——同轴内的东西放一起，比省一个文件重要。

### 5.3 `exchange` 轴怎么办：**先不建 `Exchange.py`**

`exchange` 轴的内容目前只有两条规则（`InstrumentSpec.py:197-199` CZCE→FAK、`:215` SHFE/INE→平今）+ 本轮新增的费率派生。**在现在的品种表里，"per-exchange" 退化成 "per-product 常量"**——因为每个品种只属于一个交易所（IF 恒属 CFFEX）。

所以：把 `exchange` 作为 `Product` 的字段，规则做成 `Product` 上的**只读派生属性**（`supports_closetoday` / `order_advanced` / `prefer_closetoday`），**不新建文件**。等同一交易所有 ≥3 个品种（例如真上多个 CZCE 品种）再抽 `Exchange.py` 表去重。

> 这条是刻意的：**不要为两条规则建一个轴**。

### 5.4 那"该不该走 git 评审"怎么表达？

旧文档把两种不一致的东西压进了一个后缀（Profile=标定走评审 / Spec=事实照抄）。撤销后缀规则后，改用**两个正交的手段**：

| 手段 | 管什么 | 落点 |
|---|---|---|
| **模块 docstring 顶部的一段「变更纪律」声明** | 声明本文件里哪些字段是标定值（改动需 git 评审 + 对账测试）、哪些是事实（随交易所/券商公告同步） | `Product.py` 一处 |
| **对账测试** | 把"事实"钉死：`test_period_consistency`（周期）、新增 `test_product_fee_table`（费率 vs xlsx 快照）、保留 `_check_spec_drift` 的**在线**分支（行情 vs 档案） | 测试层 |

这样"纪律"表达在**注释 + 测试**里，而不是**挤在文件名后缀**里——文件名只回答"这是什么粒度的东西"。

---

## §6 费率：静态档案 + 单源派生（回应 ⑴⑶）

### 6.1 从 xlsx 派生的 8 品种费率表

> 表由脚本从 `Docs/手续费标准-.xlsx` 派生（解析规则写在脚本里，**不手抄**）。原始行号见 §附A。
> 单位：`%%` ＝ **万分之**（用"股指 0.23%% × 4000 点 × 300 = **27.6 元/手**"交叉验证过，读法成立）；纯数字 ＝ **元/手**。

| 品种 | 交易所 | 开仓（＝平昨） | 平今 | 备注 |
|---|---|---|---|---|
| **IF** 沪深300 | CFFEX | 交易 **0.23%%** | **平今 2.3%%** | 另有「交割 0.5%%」——本系统不参与交割，归档不消费 |
| **IH** 上证50 | CFFEX | 交易 0.23%% | 平今 2.3%% | 同上 |
| **IC** 中证500 | CFFEX | 交易 0.23%% | 平今 2.3%% | 同上 |
| **IM** 中证1000 | CFFEX | 交易 0.23%% | 平今 2.3%% | 同上 |
| **AU** 黄金 | SHFE | **10 元/手** | **平今免**（=0） | 覆盖档：「黄金 6、12 合约 & 2607/2608/2609/2610」= **20 元/手** |
| **AG** 白银 | SHFE | **0.1%%** | **无独立平今行 → 平今＝开仓** | 覆盖档：「白银 6、12 合约 & 2607–2610」= **0.5%%** |
| **CU** 铜 | SHFE | **0.5%%** | **1%%** | 无覆盖档 |
| **TA** PTA | CZCE | **3 元/手** | **平今免**（=0） | 无覆盖档 |

**与代码现值的逐项对照**：

| 品种 | 代码现值（`InstrumentSpec.py:76-78`） | xlsx 裁决值 | 判定 |
|---|---|---|---|
| IF/IH/IC/IM | open `0.000023`（万0.23）/ close `0.000023` / **closetoday `0.000345`（万3.45）** | 万0.23 / 万0.23 / **万2.3（0.00023）** | 开仓、平昨 ✅；**平今要改** |
| AU | 兜底按 IF 那套（万0.23 / 万3.45），实际靠 note 说明 | 10 元/手 固定 / 平今 0 | **须改单位模型** |
| AG | 同 IF 兜底 | 万0.1（6/12 合约 万0.5）/ 平今＝开仓 | **须改数值 + 单位** |
| CU | 同 IF 兜底 | 万0.5 / 平今 万1 | **须改数值** |
| TA | 同 IF 兜底 | 3 元/手 固定 / 平今 0 | **须改单位模型** |

> ⚠️ **一个值得你花 10 秒确认的点**：代码现值 `closetoday_fee_rate = 0.000345` **恰好 = xlsx 的万2.3 × 1.5**（0.00023 × 1.5 = 0.000345）。两种解读：
> **(a)** 旧口径写错（例如写成"开仓的 15 倍"）；**(b)** 你的期货公司在交易所基准上**加收 50% 佣金**（2.3 × 1.5 = 3.45，是个很"整齐"的券商加收比例）。
> 你已裁决"以 xlsx 为准" → 本文按 **(a)** 落值（万2.3）。**但如果实盘成交单显示确实按 1.5 倍收**，只需把 `Product` 里那一个数改成 0.000345 —— 静态配置，一行。
> 同理**白银**：xlsx 基准万0.1 → 8000 元/kg × 万0.1 × 15 = **1.2 元/手**；6/12 合约档万0.5 → **6 元/手**。若你的账户白银不分合约统一按万0.5 收，把基准行改成 `0.5` 即可（一行）。**这是全表唯一需要你目视确认的一个数。**

**其它交易所口径（本期不消费，但设计要容纳）**：大商所用「隔日开平 X / 当日开平 Y」措辞（焦炭 1%%→1.4%%、生猪 1%%→2%%）；能源交易所原油 20→平今60 元/手；广期所碳酸锂 0.8%%→3.2%%。**这些都能被 §6.3 的模型直接表达**，无需改模型。

**备注行提示**：xlsx 尾行注明上期所/能源所自 2024-07-22 起、大商所自 2024-08-01 起、广期所自 2025-05-06 起对**套期保值**交易手续费减半。本系统是**投机**交易，**不适用**——但接入套保时这条要回到表里。

### 6.2 现状：平今这一件事，有三个源

| 源 | 位置 | 角色 |
|---|---|---|
| ① 静态费率默认值 | `InstrumentSpec.py:76-78` `open_fee_rate` / `closetoday_fee_rate` / `close_fee_rate` | 兜底种子 |
| ② **手写布尔** `prefer_lock_over_closetoday` | `ProductProfile.py:76`（AU/AG=False，其余默认 True） | **运行时唯一真正生效的那个**（`Engine._decide_exit` L1032） |
| ③ 自动派生建议 `evaluate_closetoday_economy()` | `InstrumentSpec.py:465-511` | 由①派生，**只发告警、永不改②**（`Engine.py:1149` 明写"只读建议，**永不改写**（决策留给人）"） |

表里 ③ 有**两个**病：位置不对（算了不改，见下），**口径不对**——它只比「平今 vs 平昨**单笔**」（1× 口径），
而锁仓路径实际要 **4 笔**成交（含两笔平仓），正确判据是 **3 × 开仓费**（见 §6.5.2 / §6.5.4）。

驱动链：`SimNow._apply_fee_rates`(L863-921) / `_sample_fee_from_fill`(L923-990) → `state.apply_fee_rates` → `fee_source` 非空 → `Engine._check_closetoday_economy`(L1139-1173) → 只发一条 warn。

**问题在③的位置**：它算出来的结论**和②不一致时，代码继续按②执行，只打一条日志**。系统明知哪个更省，却把决策权留给了一个手填的布尔。

### 6.3 推荐设计

**(a) 费率值类型：带"计价方式"，统一折算成"每手元"再比较**

```python
@dataclass(frozen=True)
class Fee:
    """一档费率。两种计价方式（交易所基准表就是这两种混排的）。"""
    kind: str          # "rate" = 按成交额比例（值 = 万分之几，如 0.23） | "per_lot" = 每手固定元
    value: float

    @staticmethod
    def free() -> "Fee":
        return Fee("per_lot", 0.0)     # 平今免收：显式，不靠 0 兼表"免"和"未填"

    def cash(self, price: float, multiplier: float) -> float:
        """折算为**每手成本（元）**——所有比较与记账都在这一个单位上做。"""
        return self.value * 1e-4 * price * multiplier if self.kind == "rate" else self.value
```

> **为什么 `value` 存"万分之几"而不是小数**：档案里的数**必须能直接对上 xlsx 打印出来的字**（xlsx 写 `0.23%%`，档案就写 `0.23`）。这样"对账"退化成一次肉眼比对 / 一次 grep，而不是"0.000023 到底是万几"的心算。转换只在 `cash()` 里做一次。
>
> **为什么 `cost_*` 一律返回元**：现行 `cost_points()` 返回"点数"，再由乘数回折成钱——这条往返在 `per_lot` 档下会失真（`10 元/手 ÷ multiplier` 再 `× multiplier`，中间还经过浮点）。**统一在元上做**，`per_lot` 与 `rate` 的可比较性由 `cash()` 一次性解决。

**(b) 费率放 `Product`，两档 + 一个合约级覆盖位**

```python
@dataclass(frozen=True, kw_only=True)
class Product:
    product: str
    exchange: str
    price_tick: float
    multiplier: float
    r_multiple_tp: float
    open_fee: Fee                 # 开仓（＝平昨，见 §6.4）
    closetoday_fee: Optional[Fee] = None   # None = 同开仓；Fee.free() = 免收
    fee_overrides: tuple = ()     # 合约月份档覆盖：(匹配规则, open_fee, closetoday_fee)
    note: str = ""

    @property
    def supports_closetoday(self) -> bool:
        return self.exchange in ("SHFE", "INE")

    @property
    def order_advanced(self) -> str:
        return "FAK" if self.exchange == "CZCE" else "FOK"

    def fee_for(self, trade_symbol: str) -> tuple[Fee, Fee]:
        """按合约代码取（开仓费, 平今费）——先匹配覆盖档，未命中回基准。"""
        ...

    # 锁仓路径**比平今路径多出来的开仓费档成交笔数**（§6.5.1 实测）：
    #   平今路径 = 2 笔：开 A + 平今 A
    #   锁仓路径 = 4 笔：开 A + 反向开 B + 平 A + 平 B
    #              （两笔平仓建仓于前一日，按**平昨**计；xlsx 已证 平昨 ≡ 开仓）
    #   ⇒ 4X > X + Y  ⟺  Y < 3X  → 走平今
    #   这 4 笔全部由现有转移表自动走出（③ 拆锁 + ⑤ 出场），**不需要新路径**（§6.5.3）
    LOCK_PATH_MULT: int = 3

    def prefer_closetoday(self, trade_symbol: str, ref_price: float) -> bool:
        """今仓离场：直接平今 还是 反向锁仓？**纯派生，无手写布尔**（§6.5）。

        两层判定，缺一不可：
          ① 能力闸门：交易所无 CLOSETODAY 指令 → 只能锁仓（早退，不看费率）
          ② 费率会计：比「平今路径 2 笔」vs「锁仓路径 4 笔」→ 阈值 = LOCK_PATH_MULT × 开仓费

        **不要复用 `InstrumentState.evaluate_closetoday_economy`**：它是 1× 口径
        （只比平今 vs 平昨**单笔**），**漏算锁仓路径多出的两笔平仓** → 判据错（§6.5.4）。
        """
        # ① 能力闸门（交易所事实）：无 CLOSETODAY 指令 → 结构上发不出去
        if not self.supports_closetoday:
            return True

        # ② 费率会计：平今单手费  vs  锁仓路径比平今多付的 3 笔开仓费档
        open_fee, ct_fee = self.fee_for(trade_symbol)
        o = open_fee.cash(ref_price, self.multiplier)
        ct = ct_fee.cash(ref_price, self.multiplier)
        lock = self.LOCK_PATH_MULT * o     # xlsx 已证 平昨 ≡ 开仓，故平仓笔按开仓档计
        return ct >= lock      # ct < 3o → 平今；ct >= 3o → 锁仓
                               # 相等归锁仓：判据是 4X > X + Y（严格大于才改走平今）
```

**为什么必须有合约级覆盖位**——你的 xlsx 自己揭穿了"费率 = 品种常量"（§6.1 覆盖档列）：

| 品种 | 基准档 | 特定合约档 |
|---|---|---|
| 黄金 AU | 10 元/手，平今免 | 「黄金 **6、12 合约 & 2607–2610**」= **20 元/手** |
| 白银 AG | 万0.1 | 「白银 **6、12 合约 & 2607–2610**」= **万0.5** |
| 螺纹钢（未支持） | 万0.2 | 「1、5、10 合约 & 2607–2609」= **万1** |
| 燃料油（未支持） | 万1（平今 万3） | 按月份分了 **6 档** |

→ **变异键是（品种 × 合约月份档）**，不是"品种"一个。

> ⚠️ **覆盖位本期可以先不实现**：8 个受支持品种里只有 AU / AG 有覆盖档，且当前主力（IF2609 / AU2602 等）不落在"6、12 合约"档。**建议先留字段位 + 一个 TODO，不写匹配引擎**（YAGNI）——但**字段位必须留**，否则下次主力换到 2612 时又要动模型（这正是 `price_band_points` 字段位当年留着的同一条道理）。

**(c) 决策点改成一行**

```python
# Engine._decide_exit 转移④
if product.prefer_closetoday(spec.trade_symbol, ref_price):
    return _Action(OrderIntent.CLOSETODAY, ...)
```

`ref_price` 在决策点天然可得（持仓入场价 / 当前 bar 收盘价），**不需要任何全局状态**。
**数学上的一个便利**：两档都是 `rate` 时，比较式两边同乘 `price × multiplier`，**价格自动约掉**——所以只有"混合计价"（一个 rate 一个 per_lot）时才真正依赖 `ref_price`，不用特判。

**(d) 观测改为启动横幅**（替代运行期告警）

启动时打一行，比"等成交后才告警"早得多、也不用状态机：

```
[gw] 品种 IF（CFFEX）：开仓 万0.23 ｜ 平昨 万0.23 ｜ 平今 万2.30
     → 平今更贵 → 今仓离场走反向锁仓（平今指令已禁用）
[gw] 品种 AU（SHFE）：开仓 10.00 元/手 ｜ 平昨 10.00 元/手 ｜ 平今 免收
     → 平今更省 → 今仓离场直接平今
```

### 6.4 三档 → 两档：`close_fee_rate` 是 `open_fee_rate` 的副本

脚本对 xlsx 全表逐条核对：**带独立平今行的基准条目 90 条，其中 74 条是"平今免"；没有任何一行把"平昨"单独列出来**。

→ **xlsx 只有两个口径：交易（＝开仓＝平昨）与平今。**
→ 所以"开仓费率 / 平昨费率 / 平今费率"**实为两档**，`close_fee_rate` 可以直接删。
→ 这修正了你 ⑶ 里"三种费率"的表述：**是两种**；第三种的独立性是命名造成的错觉。

### 6.5 今日仓离场：先对齐状态机，再定判定规则

> 本节回答两件事：**（一）三态状态机现在长什么样**（本轮重读 + 实测，**这部分不动**）；
> **（二）「今仓离场走平今还是反向锁仓」该怎么判**（把 `prefer_lock_over_closetoday` 换成纯派生）。
> 驱动方式统一：真实 `TradingEngine` + `DryRunBroker`，**只经 `on_bar` / `on_signal` 两个公开入口，不手工改簿**
> （脚本 `exp_state_machine.py` / `exp_transition2.py`）。

#### 6.5.0 状态机现状：三态 + 两个入口 + 五个转移（**不动**）

**三态的唯一判据是净敞口**（架构约束 A1，唯一实现点 `Engine.account_state()` L292-298）：

```
net != 0            -> RUNNING 运行态
net == 0 且簿非空   -> LOCKED  锁仓态（多空互锁，盈亏锁定）
net == 0 且簿空     -> FLAT    空仓态
```

**两个事件入口**：
- `on_bar`（K 线闭合）：① 结算 L1-L3 出场（**只对运行态**）→ ② 处理信号。顺序反了就会"同一根 K 线既开仓又平仓"。
- `on_signal`（缠论买卖点）：运行态**不响应**信号（规则 ⑶），出场只由 L1-L3 负责。

**五个转移**（决策集中在 `_decide_action` / `_decide_exit` 两个纯函数；本轮逐个实跑验证）：

| 转移 | 触发（入口 + 条件） | 动作 | 成交后落点 | 实测 |
|---|---|---|---|---|
| ① | 空仓态 + 信号 | `OPEN`（信号方向） | 净敞口 !=0 → **运行态** | ✅ |
| ② | 锁仓态 + **今仓** + 信号 | `OPEN`（信号方向，顺势加仓） | 净敞口 !=0 → **运行态** | ✅ |
| ③ | 锁仓态 + **跨日仓** + 信号 | `CLOSE`（平反向最早一笔 = 拆锁） | 净敞口 !=0 → **运行态**（单边） | ✅ |
| ④ | 运行态 + **今仓** + 出场 | `CLOSETODAY`（闸门+费率允许）或 `OPEN`（反向） | 平今 → **空仓态**；反向开 → **锁仓态** | ✅ |
| ⑤ | 运行态 + **跨日仓** + 出场 | `CLOSE`（平同向最早一笔） | **空仓态** 或 **锁仓态**（簿内还有反向仓时） | ✅ |

**「今仓 / 跨日」的唯一判据 = `positions.latest().entry_date` 与当前交易日比**（`entry_date >= today` 即今仓）。
只看最近一笔就够 —— 运行态 / 锁仓态下簿内仓单要么全是今仓、要么全是跨日仓，不会混合（`PositionBook` 附录 A.3）。

**`is_exit` 决定 broker 追不追价**（`Base.py:22-27`、`SimNow.py:1193-1213` / `:1278-1282`）：

| 转移 | `is_exit` | broker 行为 |
|---|---|---|
| ①②③（信号驱动，**含 ③ 拆锁**） | `False` | **单次超价、不追价** |
| ④（**软离场**） | `True` | 追价（`close_max_chase` 轮） |
| ⑤（**硬离场**） | `True` | 追价 |

**⇒ 本轮要动的范围只有一处**：转移 ④ 的分支条件（`_decide_exit` L1032-1042）—— 把「读手写布尔」换成「按费率派生」。
**不新增转移、不新增状态、不改落账路径、不改 `is_exit` 语义。**

#### 6.5.1 两条路径的成交笔数（实测，非推理）

同一触发剧本（D1 开仓 → 当日 L1 不利离场），**只换品种**：

**平今路径**（AU / SHFE，档案开关 False）—— 全部落在 D1：

| # | `intent` | `offset` | `is_exit` | 转移 | 角色 |
|---|---|---|---|---|---|
| 1 | `open` | `OPEN` | False | ① | 开 A（D1） |
| 2 | `closetoday` | `CLOSETODAY` | True | ④ | 平今 A（D1） |

→ **2 笔，终态 = 空仓态**。

**锁仓路径**（IF / CFFEX，默认反向开仓）—— D1 两笔 + D2 两笔：

| # | `intent` | `offset` | `is_exit` | 转移 | 角色 |
|---|---|---|---|---|---|
| 1 | `open` | `OPEN` | False | ① | 开 A（D1） |
| 2 | `open` | `OPEN` | True | ④ | 反向开 B（D1，锁仓） |
| 3 | `close` | `CLOSE` | False | ③ | 平 A（D2，信号拆锁，**不追价**） |
| 4 | `close` | `CLOSE` | True | ⑤ | 平 B（D2，出场） |

→ **4 笔，终态 = 空仓态**。

**第 3、4 笔都走「平昨档」**：A、B 都建在 D1，D2 平它们时 `pos.entry_date < today`
→ `_book_close` 取 `close_fee_rate`（`Engine.py:1497-1504`）。因 xlsx 已证 **平昨 ≡ 开仓**，这两笔按**开仓费档**计。

#### 6.5.2 阈值：`Y < 3 × X` 走平今

记 `X` = 开仓单手费用（= 平昨单手费用），`Y` = 平今单手费用，`N` = 开仓手数（实测用例 N=2）：

| 路径 | 逐笔费用 | 合计 |
|---|---|---|
| **锁仓** | 开 A `NX` + 反向开 B `NX` + 平 A `NX` + 平 B `NX` | **4NX** |
| **平今** | 开 A `NX` + 平今 A `NY` | **NX + NY** |

判定 `4NX > NX + NY` ⟺ **`Y < 3X` → 走平今**（否则走反向开仓的现状流程）。

**手数 `N` 自动约掉** —— 每笔都是 N 手（反向开仓量 = `abs(net)`、拆锁量 = `min(lots_per_signal, target.volume)`、
离场量 = `min(abs(net), target.volume)`，在单一方向簿下都 = N）。

**价格也自动约掉**（同品种内 X 与 Y 同计价方式时，两边同乘 `price × multiplier`）
—— 所以判定可以**完全静态化**，不需要运行时价格。

#### 6.5.3 锁仓路径能自己「回到空仓」吗？——**能**（更正上一轮的错误结论）

**能，而且全部由现有转移表自动走出**（实测见 §6.5.1 第 3、4 笔）：

1. **D2 出现对向信号** → 转移 ③ `CLOSE` 平掉反向最早一笔（拆锁）→ 运行态（单边持仓）；
2. **该单边持仓触发 L1-L3** → 转移 ⑤ `CLOSE` → 空仓态。

> ⚠️ **更正上一轮**：上一轮写「锁仓态没有回空仓路径、取 3× 必须先给引擎补新功能」——**这是错的**。
> 错误来源：把 `Engine.py:1652-1658` / `:1682-1690` 那段注释当成了通用结论。**那段注释的语境是
> 「关闭自动下单之后」**（`shutdown_and_lock_all`）—— 此时 `on_signal` 顶部直接 `return`，等不到任何信号，
> 所以"没有自动出口"。**自动下单开启时，锁仓态等得到信号**，转移 ③ 就能拆锁（实测第 3 笔）。
>
> 锁仓态确实**不参与 L1-L3**（`on_bar` 里只有运行态才调 `_settle_positions`；实测锁仓态喂同日 bar、
> 跨日 bar 都无动作）—— 但这只说明"锁仓态不能自主离场、要等信号"，**不等于"回不了空仓"**。

**两点如实说明（不改变判定方向）**：

- 锁仓路径的 4 笔**跨 2 个交易日**，中间隔一夜；且第 3 笔（拆锁，`is_exit=False`）**不追价**，
  成交概率低于平今那笔（`is_exit=True` 追价）。这两点只会让锁仓路径**更不利**，
  所以按 4 笔费率比较得到的判定是**保守下界**（实际更该偏平今）。
- **若锁仓后再也不来信号**（例如随后关闭自动下单），费用停在 2 笔（`NX + NX`）——那是**冻结态**，不是"离场"。
  业务上"离场"的终态取**空仓**，故按 4 笔比。

#### 6.5.4 仓库现成那条规则：**1× 口径，漏算两笔**

`evaluate_closetoday_economy()`（`InstrumentSpec.py:465-511`）写的是 `suggest_lock = ct > cl`，
其中 `cl` 是**平昨费率**。

因 xlsx 已证 **平昨费 ≡ 开仓费**，它数值上 = `Y > X` → 锁仓，也就是 **1× 口径**。
但锁仓路径实际是 **4 笔**，正确判据是 **3×** —— **它漏算了锁仓路径多出的两笔平仓费。**

分歧区间 = `X < Y < 3X`：1× 判锁仓、3× 判平今。**CU（Y = 2X）正落在这里**。

**⇒ 可以复用它现在的结果（8 品种里 7 个相同），但不能复用它的实现。** 派生逻辑新写。

> 📌 **也更正上一轮一句不准确的话**：上轮说"它的参考量『平昨费』在离场路径里根本不出现"——
> 锁仓路径的第 3、4 笔**就是**平昨。它的问题不是"参考量不存在"，而是**只比了 1 笔、漏了 2 笔**。

#### 6.5.5 回算：唯一分歧品种是 CU

（脚本 `exp_user_rule.py`，费率为 xlsx 派生值）

| 品种 | 交易所 | 有平今指令 | Y/X | 3× 判定 | 现值开关 | 是否变更 |
|---|---|---|---|---|---|---|
| IF / IH / IC / IM | CFFEX | ❌ | 10.0 | 锁仓（**闸门短路**） | True(锁仓) | 不变 |
| AU | SHFE | ✅ | 0（平今免） | **平今** | False(平今) | 不变 |
| AG | SHFE | ✅ | 1.0 | **平今** | False(平今) | 不变 |
| **CU** | SHFE | ✅ | **2.0** | **平今** | True(锁仓) | **★ 变更** |
| TA | CZCE | ❌ | 0（平今免） | 锁仓（**闸门短路**） | True(锁仓) | 不变 |

**CU 逐笔**（示例价 80000 元/吨、5 吨/手 → 开仓 20 元/手、平今 40 元/手）：

```
锁仓路径：开A 20x2 + 反向开B 20x2 + 平A 20x2 + 平B 20x2 = 160 元
平今路径：开A 20x2 + 平今 40x2                       = 120 元
                                          → 平今省 40 元/手
```

> 📌 CU 的 `ProductProfile.note`（`ProductProfile.py:160`）写「平今=锁仓后再平昨的总费、不占便宜 → 保持默认 True」
> —— 这句正是按 **1× 口径**下的判断，本轮按 3× 口径更正为**平今**。
>
> 📌 **不要再用「与现值一致」论证阈值正确性**：8 品种里只有 CU 靠近分界（其余被闸门短路，或 Y/X ∈ {0, 1}），
> 1× / 2× / 3× 都能凑出 7/8 或 8/8 一致。一致性只能证明"迁移无行为变更"；要判对错，得**独立算经济账**。

### 6.6 现行机器的四个缺陷（含代码证据）

| # | 缺陷 | 证据 |
|---|---|---|
| **D-a** | **要等成交**：实盘费率靠成交回报反推（`费率 = commission / (成交价 × 手数 × 乘数)`，`SimNow.py:926-927`），三档要**攒齐才原子回填** | `SimNow.py:457`「每个样本 = (commission, 价×手数乘积)。**三档都攒到**才原子回填」；`apply_fee_rates` 一次性语义 `SimNow.py:878`。**即：要等到开仓/平昨/平今三类成交都发生过**——你说的"等第一次交易成功"实际比这更晚 |
| **D-b** | **离线永不触发**：`mark_fee_config()`（离线显式声明费率来源）**全仓 0 处生产调用**（`main.py` 只调 `mark_config_offline`） | `mark_fee_config` 8 处 / 2 文件，全在自身定义 + 测试。它的 docstring 自己写着「本方法在仓库里**目前没有调用点**」→ 离线 `fee_source` 恒空 → ③ 在离线**从不运行** |
| **D-c** | **建议与执行分叉**（见 §6.2）：算出更优解，仍按手写布尔走，只出声不改行为 | `Engine.py:1149-1150` |
| **D-d** | **单位表达不了两种形态**（**这条最实质**）：`cost_points()` 只有 `价格 × 费率` 一种口径 | `InstrumentSpec.py:546-550`。而 xlsx 里 **PTA=3、黄金=10** 是**每手固定元**；**IF=0.23%%、铜=0.5%%** 是**按成交额比例**。固定元只能"按当时价折成一个 rate"（`SimNow.py` 就是这么干的），价格一变就失真；**离线 FEE_CONFIG 路径连折算都没有** → `PTA / 黄金` 的**净盈亏在回测里会算错**。而 `cost_points` 是**真在算钱**的（`Engine.py:1501`、`Reconcile.py:209` → `Trade.cost_points/net_points/net_cash` → `state.db` → 前端/报表 `Analyze.py:171`） |

### 6.7 对你主张的评估

**你的主张（费率改用券商数据做静态配置）成立**，理由：

1. **费率的真值源本来就是券商费率表**，不是行情、不是成交回报。现方案是"从间接观测量反推一个直接可得的事实"。
2. **变动频率与 tick/乘数同级**（交易所/券商公告级，月/年级），完全够格做静态档案——而 tick/乘数已经在档案里了，费率没道理特殊。
3. **代价是净负**：换来的是"延迟可用 + 离线不可用 + fail-closed 到保守侧（多付一次开仓费 + 一次跨日平仓费）+ 建议与执行分叉"，收益是零（费率本来就能手填）。
4. **简化面极大**（§6.8）：删掉状态机 + 两条取数通道 + 一个 `fail-closed` 语义分支，**"取不到费率"这个状态彻底消失**。

**一处需要补正**：三档费率**不只用于判断平今**。它另一半用在**成交会计**（`cost_points` → 落库 → 报表，见 §6.6-D-d）。所以静态化时**必须同时修单位模型**，否则回测的净盈亏仍然错。

### 6.8 可删清单与波及面

| 删除项 | 位置 | 规模 |
|---|---|---|
| `InstrumentSpec.open_fee_rate` / `closetoday_fee_rate` / `close_fee_rate` | `InstrumentSpec.py:76-78` | 3 字段 |
| `InstrumentState.apply_fee_rates()` | `InstrumentSpec.py:412-447` | 36 行 |
| `InstrumentState.mark_fee_config()` | `InstrumentSpec.py:449-463` | 15 行（**0 处生产调用**） |
| `InstrumentState.evaluate_closetoday_economy()` | `InstrumentSpec.py:465-511` | 47 行 |
| `InstrumentState.fee_source` + `SOURCE_FEE_QUOTE/TRADE/CONFIG` | `InstrumentSpec.py:283-292`、`:325-326` | 5 符号 |
| `SimNow._apply_fee_rates()` | `SimNow.py:863-921` | ~59 行 |
| `SimNow._sample_fee_from_fill()` | `SimNow.py:923-990` | ~68 行 |
| `Engine._check_closetoday_economy()` + `_closetoday_economy_checked` | `Engine.py:1139-1173`、`:1228` | ~36 行 |
| `Engine._prefer_lock_over_closetoday()` | `Engine.py:1052-1061` | 10 行（改读 `Product` 派生属性） |
| `ProductProfile.prefer_lock_over_closetoday` 字段 | `ProductProfile.py:76` | 1 字段 → 1 property |
| `test_p52_fee_economy.py` | — | 整文件（建议**改写**为"费率表 → 派生结论"的纯函数测试，不要裸删） |
| `Engine._check_spec_drift` 离线分支 + `_spec_drift_offline_checked` | `Engine.py:1096-1119` | 24 行（§3.4-D 已证结构性不可达） |

> ⚠️ **不在删除范围（别顺手删）**：`supports_closetoday` 属性及其 19 处消费点。它是**交易所能力闸门**（§6.5 第①层），**不是**这个手写布尔的一部分。删了它，平今分支就失去唯一守卫，会向 CFFEX/DCE/CZCE/GFEX 发出必然被拒的 `CLOSETODAY` 单。
>
> ⚠️ **也不建议复用** `evaluate_closetoday_economy` 当派生器（它是 **1× 口径、漏算两笔**，见 §6.5.4）——它在删除清单里，属**删掉重写**，不是**改造复用**。

**合计约 290 行生产代码 + 1 个测试文件**，横跨 `Infra/InstrumentSpec.py`、`Broker/SimNow.py`、`Engine/Engine.py`、`Infra/ProductProfile.py` 四个文件。
**收益**：删掉一个 `fail-closed` 状态、两条取数通道、一个"建议与执行分叉"的设计裂缝；费率从"运行时才能确定"变成"启动即确定"。

> ⚠️ **`test_p52_fee_economy.py:164` 的注释里硬编码了旧口径**：`S = InstrumentState(InstrumentSpec())  # 默认 IF：open=2.3e-5 close=2.3e-5 ct=3.45e-4`。P-A 必须同步改（否则测试会红在一个"注释与代码不一致"的地方，反而更难查）。

### 6.9 从 xlsx 到代码：一次转换 + 一道对账

1. **一次性解析脚本**（`Trading/Tool/`）：读 `Docs/手续费标准-.xlsx` → 品种名归一（沪深300指数→IF、上证50→IH、中证500→IC、中证1000→IM、黄金→AU、白银→AG、铜→CU、PTA→TA）→ 解析 `"0.23%%"`（万分比）、`"3"`（元/手）、`"平今免"` → 生成 `Product` 行的费率字面量。**解析规则写在脚本里**（A 列切交易所、B 列空+D 列非空 = 平今行、含"合约/&" = 覆盖档），本轮已跑通，可直接复用。
2. **对账测试** `test_product_fee_table.py`：把 xlsx 解析结果固化成 JSON 快照，断言代码里 8 个品种的费率与快照一致（照 `test_period_consistency.py` 的套路写）。券商/交易所调价时，改档案 → 重新生成快照 → 测试告诉你哪里没跟上。
3. **`broker` 加收怎么表达**：默认 = 直接用 xlsx 的交易所基准（你的原话"从交易商拿到的数据"）。若将来要"基准 + 加收倍率"，最省的落点是在 `Product` 行上再乘一个部署级系数——**但现在不要做**（YAGNI，见 §8-4）。

---

## §7 实施顺序与改动面

> **已拍板：P-A → P-B → P-D → P-C**。
> 原则：**一步一个可整体替换的改动，每步跑全量 52 脚本，按仓库目录层级打 zip**（md 不进 zip，见 §7.5）。

| 步 | 做什么 | 触及 | 引用面（HEAD 实测） | 风险 |
|---|---|---|---|---|
| **P-A** | **费率静态化**（§6）：`Fee` 类型 + 费率进 `Product` + `prefer_closetoday` 派生；删 §6.8 全部条目；加 `test_product_fee_table.py` | `ProductProfile.py` / `InstrumentSpec.py` / `SimNow.py` / `Engine.py` / `main.py`；`test_p51` / `test_p52` / `test_p45/p46/p47`（有断言读 `prefer_lock_over_closetoday`） | 生产 4 文件 + 测试约 6 文件 | 中。**收益最大、独立于改名** |
| **P-B** | **归位**（§3.1 / §5.1）：tick/乘数/费率从 `InstrumentSpec` 摘除 → 运行时对象初值取 `Product`；删 `for_product` / `_seed_instrument` / 离线漂移分支；**并做 §4.3 的合并（`Instrument` + 冻结的 `InstrumentConfig`）** | `InstrumentSpec.py` / `main.py` / `Config.py` / `Engine.py` / `Broker/*` | `InstrumentSpec(` **152 处 / 38 文件**、`InstrumentState(` **27 / 8**、`state.spec` 引用 **104**（绝大多数是测试构造点 + 只读引用，**不改名**） | 中 |
| **P-D** | **文档层术语锚点**：`Product.py` / `Instrument.py` docstring 各加 5 行术语锚点（§附F）+ 变更纪律声明（§5.4）、`Period.py` 写明"周期特有参数当前为空"（§5.2） | 3 个 docstring | 0 引用 | 低 |
| **P-C** | **改名与拆分**（旧文档 §五 ①②③，方向不变）：`Store.py`→`StateDB.py`；`Types.py`→`Records.py`+`TradingClock.py`（含 `PeriodProfile` 时间工具搭车）；`PeriodProfile.py`→`Period.py` | 全仓 | `Store` 39 文件/155 处；`Types` 49 文件/60 处（import 面）；`PeriodProfile` 11 文件/33 处；`ProductProfile` 18 文件/44 处 | **最高**。建议**先只改"名字明显有病的"**（`Types` / `Store` / `PeriodProfile`） |

**`InstrumentSpec` 改名的处置（沿用旧文档已拍板结论）**：**不改**。它本来就是 per-instrument 命名，正确；`cfg.instrument.*` 引用 **311 处 / 53 文件**，收益 ≈ 0（§附F.4 有逐项审计）。

### 7.1 P-A 逐文件改动规格（可直接照做）

| 文件 | 改动 |
|---|---|
| `Infra/ProductProfile.py` | 加 `Fee` 类型（放本文件或 `Infra/` 下小模块）；8 个档案各加 `open_fee` / `closetoday_fee`（值见 §6.1）+ `exchange`；删 `prefer_lock_over_closetoday` 字段（改 property）；删 note 里关于费率托管给 InstrumentSpec 的整段说明（`ProductProfile.py:101-103`） |
| `Infra/InstrumentSpec.py` | 删 3 个费率字段（L76-78）；删 `apply_fee_rates` / `mark_fee_config` / `evaluate_closetoday_economy` / `fee_source` / 3 个 `SOURCE_FEE_*`；`cost_points` 改为**返回元**（`Fee.cash`）并改签名接 `Product` |
| `Broker/SimNow.py` | 删 `_apply_fee_rates` / `_sample_fee_from_fill` 及调用点；`_resolve_trade_symbol` / `_apply_instrument_quote` 里对 spec 的写入见 §4.2（P-B 一并处理） |
| `Engine/Engine.py` | 删 `_check_closetoday_economy` + `_closetoday_economy_checked`；`_prefer_lock_over_closetoday` 改读 `Product.prefer_closetoday(...)`；`_check_spec_drift` 删离线分支（保在线分支）；`L1501` 附近成本计算改用 `Fee.cash` |
| `Engine/Reconcile.py` | `L209-213` 的成本口径改用 `Fee.cash`（与 Engine 同源） |
| `main.py` | 加启动横幅一行（§6.3-d） |
| `Test/test_product_fee_table.py` | **新增**：xlsx 快照 vs 档案值对账 |
| `Test/test_p52_fee_economy.py` | **改写**：为"费率 → 派生结论"的纯函数测试；改掉 L164 的旧口径注释 |
| `Test/test_p45/p46/p47/p51` | 有断言读 `prefer_lock_over_closetoday` → 改为读派生 property |

### 7.2 P-B 里的"静默吞掉"顺手处理

§3.4-D 情形 A 暴露：用户显式写进配置的 `price_tick=0.5` 被静默忽略。P-B 摘除这两个字段后，配置里**不再有这两个键**（`extra="forbid"` 会让旧配置直接报错，这是好事），但**要在启动期给一条明确提示**，而不是让用户自己发现"我填的没生效"。

### 7.3 每步的验收口径

1. 全量 52 脚本全绿（**基线 52/52**，不是旧记的 "50 过 / 1 环境失败"）。
2. `test_p26_terminology_guard.py` 必须过（改动含注释/日志/断言描述时尤其）。
3. 从 zip **解压到干净目录复验**：读回关键文件断言新标记在、旧标记消失（`grep -rE "旧符号"` 应只在"已删除"注释里命中）。
4. md5 往返校验（打包前后一致 → 免重跑回归）。

### 7.4 每步独立可回退

P-A / P-B / P-D 三步**彼此独立**（P-A 只动费率；P-B 只动字段归属；P-D 只动 docstring），任一步出问题可单独回退而不牵连其它。

### 7.5 交付格式（本轮新增一条，已写进用户级记忆）

**md 文档不进 zip。** zip 只装"需要按仓库目录层级覆盖的文件"（代码 / 配置 / 测试）；`.md` 报告与设计文档**单独作为附件给出**。
理由：上一轮把设计 md 同时打进 zip 又单独发一次，造成重复，且解压覆盖时会把 md 落进仓库树。

---

## §8 仍未决 / 需你确认

**已闭环（本轮拍板）**：费率口径（§6.1）/ 费率放 Product 行内（§6.3-b）/ 实施顺序（§7）/ 是否合并 Instrument（§4.3，倾向"合并 + 冻结配置层"，落 P-B）。

**仍未决**：

1. **合并的落点**：放进 P-B 一起做（推荐，避免二次动同一批文件），还是单开 **P-E** 紧随其后？
2. **`fee_overrides`（合约月份档覆盖）本期是否实现**：8 品种里只有 AU/AG 有覆盖档，且当前主力不落在该档。**倾向：留字段位 + TODO，不写匹配引擎**（§6.3-b 注）。
3. **白银基准档取值**：按 xlsx 字面 = 万0.1（→1.2 元/手）；若你账户统一按万0.5 收则改基准行。**需要你花 10 秒目视确认**（§6.1 注）。
4. **是否引入"券商加收倍率"**：YAGNI，**倾向不做**，等真有多账户需求再说。（背景：代码现值万3.45 恰好 = xlsx 万2.3 × 1.5，见 §6.1 注。）
5. **`closetoday_first` 的处置**（P3，可选）：它当前的实际作用点是成交会计选费率档（`Engine.py:1503`、`Reconcile.py:209`）。更干净的语义是"用哪档费率由产生该成交的 **intent** 决定"（`CLOSETODAY`→平今费、`CLOSE`→平昨费、`OPEN`→开仓费），这样它可以删掉。**未细究边界，标"待确认"**，不要顺手改。
6. **P-C 的范围**：是否本轮只做 `Types` / `Store` / `PeriodProfile` 三个"名字确实有病"的改名，把其余延后？
7. ~~「离场」的终态定义~~ → **本轮已按你的口径定稿，无需再拍板**：
   终态 = **回到空仓**；阈值 = **`3 × 开仓费`**（`LOCK_PATH_MULT = 3`）。推导与实测见 §6.5.1 / §6.5.2。
   - **唯一代价**：**CU 由锁仓翻为平今**（其余 7 品种不变，行为零变更）——§6.5.5。
   - ✅ **不需要给引擎加任何新功能**：锁仓路径的 4 笔由现有转移表（③ 拆锁 + ⑤ 出场）自动走出，
     §6.5.3 已实测更正上一轮"无自动清仓路径"的错误结论。
8. **派生逻辑落点**（§6.5.4）：确认为 `Product.prefer_closetoday` property（**新写**），阈值取 `LOCK_PATH_MULT = 3`；
   而不是改造仓库现有 `evaluate_closetoday_economy`（它是 **1× 口径、漏算两笔**，判据错，属删除项）。

---

## 附A 证据索引（file:line，均为 HEAD `840324d1a8`）

| 事实 | 证据 |
|---|---|
| tick/乘数 两份副本 | `ProductProfile.py:74-75`、`InstrumentSpec.py:74-75` |
| 播种桥强制覆盖 | `InstrumentSpec.py:159-163`（`d = {k:v ... if k not in ("price_tick","multiplier")}`）、`main.py:80-82` |
| **`InstrumentSpec` 不是 frozen** | `InstrumentSpec.py:69` `model_config = ConfigDict(extra="forbid")`（**无 `frozen`**）；探针输出见 §4.1 |
| **生产代码就地改 spec** | `SimNow.py:718`（trade_symbol）、`:803`（exchange）、`:856`（last_trade_date）；`main.py:108`（signal_symbol） |
| **引擎读被改的字段** | `Engine.py:977`（exchange→CZCE 手数）、`:1033`/`:1279`（supports_closetoday）、`:1237`（last_trade_date）、`:441`/`:639`（trade_symbol） |
| 三对象是同一块内存 | 探针：`broker.spec is cfg.instrument` → True；`main.py:163` `spec = cfg.instrument` → `:170` 传 broker → `:238` 传 engine（`Engine.py:125` `self.spec = cfg.instrument`） |
| 离线漂移分支（不可达） | `Engine.py:1096-1119`；在线分支 `:1121-1137`；两标志 `:1091`/`:1097` |
| 手写平今布尔 | `ProductProfile.py:76`；消费点 `Engine.py:1052-1061`、`:1032-1040` |
| 派生建议（只出声） | `InstrumentSpec.py:465-511`；消费点 `Engine.py:1139-1173`（`:1149-1150` 明写"永不改写"） |
| 三档费率静态默认值 | `InstrumentSpec.py:76-78`（`open 0.000023` / `closetoday 0.000345` / `close 0.000023`） |
| `cost_points` 只有比例口径 | `InstrumentSpec.py:546-550` |
| `cost_points` 真在算钱 | `Engine.py:1501-1506`、`Reconcile.py:209-213`、`Store.py:52/157`、`Analyze.py:171` |
| 实盘费率要攒齐三档 | `SimNow.py:457`、`:863-921`、`:923-990`；反推公式 `:926-927` |
| `mark_fee_config` 无生产调用 | `InstrumentSpec.py:449-463` 自述「目前没有调用点」；`main.py:199` 只调 `mark_config_offline` |
| 交易所分支散 2 处 | `InstrumentSpec.py:197-199`、`Engine.py:977` |
| `r_alert_a_floor` = 观测旋钮 | `Exit.py:65-76`、`:223-235`、`:171-186` |
| `min_r_points` 已删（负向断言） | `test_p8_layered_exit.py:414-422`、`test_period_profile.py:137`、`test_p45:97`、`test_p46:150`、`test_p47:156` |
| PeriodProfile 已无周期特有参数 | `PeriodProfile.py:162-168` |
| 品种白名单/文案契约 | `ProductProfile.py:232-271`；`describe_unknown_product` 含「拒绝启动交易引擎」（test_p20）/「禁止启动」（test_p47） |
| 六家交易所平今支持 | `InstrumentSpec.py:202-215`（仅 SHFE/INE） |
| **费率表原始行**（xlsx Sheet1，0-based） | IF/IH/IC/IM：210-223（`交易0.23%%` / `平今2.3%%` / `交割0.5%%`）；AU：139-144（`10` / `平今免` / 覆盖档 141-142 `20` / `平今免`）；AG：132-133（`0.1%%` / 覆盖档 `0.5%%`）；CU：149-150（`0.5%%` / `1%%`）；TA：54-55（`3` / `平今免`）；套保减半备注：294-296 |
| 费率表结构（供解析器对齐） | A 列切交易所；B 列非空＝新条目（含"合约"/"&" → 覆盖档）；B 列空 + D 列非空＝平今行 |

## 附B 全仓符号计数（HEAD `840324d1a8`，236 个 `.py`）

| 符号 | 命中 | 文件 |
|---|---|---|
| `signal_symbol` | 87 | 16 |
| `trade_symbol`（含 `self._trade_symbol`） | 124 | 24 |
| `now_cn` | 38 | 14 |
| `min_r_points` | 34（**全在注释/负向断言**） | 8 |
| `prefer_lock_over_closetoday` | 28 | 10 |
| `fee_source` | 58 | 5 |
| `closetoday_fee_rate` | 22 | 6 |
| `apply_fee_rates` | 18 | 3 |
| `cost_points` | 18 | 9 |
| `InstrumentSpec`（类名） | 320 | 54 |
| `InstrumentSpec(`（构造点） | 152 | 38 |
| `InstrumentState` | 85 | 14 |
| `InstrumentState(`（构造点） | 27 | 8 |
| `PRODUCT_PROFILES` | 55 | 13 |
| `state.spec` / `x.spec` 引用 | 104 | — |

**模块被引用面**：`Store` 39 文件/155 处 ｜ `Types` 51/71（**import 语句 49 文件/60 处**）｜ `InstrumentSpec` 53/311 ｜ `EventLog` 35/108 ｜ `ProductProfile` 18/44 ｜ `PeriodProfile` 11/33

---

## 附C 命名三原则（可复用）

1. **名字 = 领域概念，不是技术机制**（`TradingClock` 优于 `TimeKit`，`StateDB` 优于 `Persistence`）
2. **一个文件一个概念，名字必须覆盖全文件**——名字盖不住时就该拆，而不是起个更大的名字（`Types.py` 的教训）
3. ~~**后缀编码语义**：Profile（标定档案）/ Spec（部署事实）/ State（运行时）/ Records（词汇表）——不混用~~ → **已废止**（§3.3）。改为：**文件名只表达粒度**；"该不该走 git 评审"用模块 docstring 的变更纪律声明 + 对账测试表达（§5.4）。

---

## 附D 现状快照（动手前的核对锚点）

- 分支 `custom-dev`，HEAD **`840324d1a8`**（2026-09-15T02:41:28Z）；旧记的基线 `101fca5` 距 HEAD 已 **10 个提交**，实施时刷新。
- `Trading/Infra/` 现有 7 个 py（**实测字节**）：`EventLog` 3219 ／ `InstrumentSpec` **36629**（598 行）／ `PeriodProfile` 9519 ／ `ProductProfile` 17422 ／ `Store` 13388 ／ `Types` 22402 ／ `__init__`
- `InstrumentSpec.py` 内（**实测行号**）：`InstrumentSpec`（L58，pydantic 静态）、`InstrumentState`（**L255**，运行时）、`for_product`（L137）、`derive_exchange`（**L583**）
- `Types.py`：5 枚举（`Side` L107 / `DecisionType` L124 / `OrderIntent` L138 / `AccountState` L173 / `EngineState` L199）+ 7 记录（`Signal` L208 / `Bar` L293 / `Order` L314 / `ExitPlan` L339 / `Position` L362 / `Trade` L446 / `Decision` L474）
- `Store.py`：`class Store` L76（SQLite 状态库，产物 `state.db`）
- `PeriodProfile.py`：`ts_scale` L75 / `norm_delta_sec` L87 / `parse_hhmmss` L123 / `_TS_MS_THRESHOLD` L66 / `_DELTA_MS_THRESHOLD` L72
- **契约勿破坏**：`describe_unknown_product` 文案钉死子串「拒绝启动交易引擎」（test_p20）、「禁止启动」（test_p47）；`parse_product` 保月份行为钉死（`test_period_profile.py:132-133`）
- **术语护栏**：`Test/test_p26_terminology_guard.py` 扫 `Trading/` `App/` `Frontend/` + 仓库根 `verify_*.py`，**不扫 `Docs/**`**；改 `Trading/` 下任何字符串前先过它

---

## 附E 覆盖与删除清单

**本文取代并合并**（内容已 100% 并入，见 §0.4）：

| 文件 | 处置 |
|---|---|
| `Docs/Infra文件命名治理_交接文档.md` | **删除**（§一/§二/§三/§四/§五/§六/§七/附 已分别并入本文 §附F / §2 / §5 / §附C / §7 / §8 / §2 / §附D） |
| `Docs/Infra命名治理_再设计_20260915.md` | **删除**（若你已保存；本文即它的升级版） |
| `reports/证据_计数_探针_基线_20260915.md` | **不再单独交付**，关键条目已并入 §4.2 / §附A / §附B |

> git 历史保留旧文件，需要审计快照时随时可检出。删除是为了**消除"该看哪一份"的歧义**——这正是 ⑹ 的诉求。

---

## 附F 术语锚点（原旧文档 §二 全文并入）

### F.1 为何叫 Instrument？

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

所以 `InstrumentSpec = 合约规格`，名字从柜台协议借来，语义对齐 A′（`apply_quote` 回填的正是 `InstrumentField` 那套字段）。

> **已拍板：不改名为 `ContractSpec`**——Instrument 对齐 CTP 行业词且与 A′ 语义闭环，备选 `ContractSpec` 否决。

### F.2 per-instrument 与 per-product 的区别

两个**粒度**（领域概念），外加一个只是**代码字符串叫法**的词：

| 概念 | 中文 | 例 | 一份档案管多少 | 换月时 |
|---|---|---|---|---|
| **product** | 品种 | IF / IH / AU / PTA | 整个品种族（IF2509、IF2512、IF2603…） | 不换 |
| **instrument** | 合约 | IF2509 / IF2512 / AU2512 | **一张**具体合约 | IF2509 → IF2512 换一份新的 |
| symbol | 代码字符串（非粒度） | `KQ.m@CFFEX.IF` / `CFFEX.IF2609` | ——（本仓字段词汇：`signal_symbol` / `trade_symbol`） | —— |

> **术语修正（historique）**：旧稿写 "per-symbol"，是照代码字段名说话；领域术语应为 **per-instrument**。判据：主连 `KQ.m@CFFEX.IF` 是 symbol 但**不是** instrument（滚动引用，无到期日）；`CFFEX.IF2609` 才是 instrument。
> 之所以直觉上 "symbol 更像 product"：股票世界里 ticker/symbol = 产品级标识（AAPL）；期货因一个品种裂成一堆月份合约，symbol 字符串被迫带月份才唯一，于是期货代码惯例里 "symbol" 指完整合约代码——两个世界的用法打架，"instrument" 才是无歧义的词。

**举例**：IF2509 和 IF2512 是同一品种（IF）的两张合约。

- `multiplier=300`、`price_tick=0.2`：IF 全族都一样 → **per-product** → 放 Product 兜底有底气
- `last_trade_date`：IF2509 = 2025-09-19，IF2512 = 2025-12-19，每张合约不同 → **per-instrument**
- `trade_symbol`：天生属于一张合约 → per-instrument

**代码里的直接印证**（`ProductProfile.py` 现成的函数对，勿混用）：

```python
parse_product("CFFEX.IF2609")      -> "IF2609"   # 保月份 = 认定到具体合约（instrument 粒度）
parse_product_key("CFFEX.IF2609")  -> "IF"       # 剥月份 = 查品种档案（product 粒度）
```

这对函数的分工就是"instrument 与 product 是两个粒度"的体现（`test_period_profile.py:132-133` 钉死前者行为）。

### F.3 为什么 `InstrumentSpec` 不叫 `InstrumentProfile`

> 本节原文写于"Spec/Profile 二分"仍成立时；**该划分线已被 §3.3 推翻**。此处保留，因为它同时是"后缀规则为什么薄"的原始论证材料。

| | ProductProfile | InstrumentSpec |
|---|---|---|
| 值的来源 | 交易经验标定（校准后：真正算标定的**只剩 `r_multiple_tp` 一个**） | 交易所/部署事实（乘数 300、最小变动 0.2，照抄即可） |
| 粒度 | per-product（一行管全族） | per-instrument（每张合约一份） |
| 变更纪律 | 改 = 改代码资产，走评审 + 对账测试 | 换月自动重播种，不算"调参" |

**替代方案（已采纳）**：文件名只表达**粒度**；"该不该走 git 评审"改用模块 docstring 声明 + 对账测试表达（§5.4）。

### F.4 代码术语审计：结论 = 标识符不改名

| 标识符 | 实际持有的值 | 审计判定 |
|---|---|---|
| `InstrumentSpec`（类名） | 每张合约一份的静态规格 | **本来就是 per-instrument 命名，正确**——术语分析反而确认了它 |
| `ProductProfile`（类名） | 一行管全族 | 本来就是 per-product，正确 |
| `signal_symbol`（**87 处/16 文件**） | `KQ.m@CFFEX.IF`（主连）或 CLI 直给的 `CFFEX.IF2609` | **改名反而错**：主连是滚动引用、无到期日、无 `last_trade_date`，不是 instrument——它就是"信号源的代码字符串"。此处 symbol 用的正是"代码字符串"义，名实相符 |
| `trade_symbol`（**124 处/24 文件**，含 `self._trade_symbol`） | `CFFEX.IF2609`（真实合约代码） | 是 instrument 的**代码字符串**。"symbol = 合约代码字符串"是 API 界通行用法，且与 `signal_symbol` 对仗工整；SimNow 下单处 `api.insert_order(symbol=self._trade_symbol)`——**tqsdk 自己的参数名就叫 symbol**，改名会在调用边界制造错位 |

**为什么"symbol"与"instrument"不冲突**：两者各占一层语义——instrument / product 是**概念粒度**（哪一层事实），symbol 是**字符串叫法**（代码怎么写）。tqsdk 自身两种都用：quote 的身份字段叫 `instrument_id`（概念名），下单参数叫 `symbol`（字符串名）——恰证明两个词分工明确。

**成本/收益**：211 处纯机械替换 + 若干测试钉死字符串，换来可读性增益 ≈ 0。

**替代动作（低成本高收益）**：术语治理落在**文档层**——概念讨论、docstring、交接文档统一按 F.2 的三行表用词。在 `Product.py` / `Instrument.py` 模块 docstring 各加一小段术语锚点（约 5 行），随 **P-D** 做，不单开一轮。
---

## 附G 落地补记（2026-09-15 夜 · 三项偏离修复 + 四项决定 + 费率改为内联生成区块）

> 本附录是**增量记录**，不改写上文任何分析（上文是审计稿，保留原貌）。
> 凡与上文口径冲突处，**以本附录为准**。基线：HEAD `5a04905db370`（未变）。

### G.1 一条纪律判定：费率生成物**不新开文件**

§5.1 把 `Trading/Infra/` 定死成 **7 个模块、按变异轴排**，§附C 原则 1 又明令
「名字 = 领域概念，不是技术机制」。落地 D-B（生成式 SSOT）时曾一度新增
`Trading/Infra/_FeeTable.py`（独立生成文件），**与上述两条同时冲突**：

- 它使 `Infra/` 变成第 8 个文件，且不属于任何一根变异轴；
- `_` 前缀是 Python 的**可见性机制**（不是领域概念），而该模块被 `Product.py`
  与测试跨模块引用 —— "私有"语义是假的。

**定案（用户拍板）**：生成的纯数据**不新开文件**，改为内联进 `Product.py` 的
`>>> GENERATED … <<< END GENERATED` 标记区块。机器生成内容与手写内容的分离
靠「标记 + 逐字节护栏」，而不是靠文件边界。

→ **`Trading/Infra/` 仍是 §5.1 定的 7 个模块**（`Clock / Records / Period /
Product / Instrument / StateDB / EventLog`），本条纪律未被突破。

### G.2 对 §6.1 / §6.9 的口径修正：费率改为生成式

| 上文口径 | 落地后的口径 |
|---|---|
| §6.1 的 8 品种费率表是**手抄进 `Product.py`** 的静态表 | 费率**不再手抄**。真值源 = `Docs/手续费标准-.xlsx` → `Trading/Tool/GenFeeTable.py` → `Product.py` 的 GENERATED 区块 → `_fee_kw()` 构造期注入档案 |
| §6.9「一次转换 + 一道对账」 | 仍成立，且对账升级为**逐字节**：`GenFeeTable.py --check` 与 `test_product_fee_table.py` 都拿"重新渲染的区块"与磁盘区块比对 |
| 改费率 = 改代码 | 改费率 = **改 xlsx（或改生成器解析规则）+ 重跑生成器**；`Product.py` 手写部分一个字面量都不许有（`[1f]` 断言） |

**三层护栏**（缺一不可）：

1. 生成器只重写两块标记之间的文本，**区块外逐字节不动**（`replace_block`；
   标记缺失/重复直接报错，不"以为替换成功"）；
2. `GenFeeTable.py --check` → 不一致 `rc=1`（CI 用；xlsx 缺失时 `rc=0` + skip）；
3. `Trading/Test/test_product_fee_table.py [3f]` 逐字节比对 + `[3g]-[3i]`
   **自检护栏会咬人**（未改副本 `rc=0`；手改区块一个数字 → `rc=1`）。

### G.3 对 §6.3(c) 的修正：`prefer_closetoday` 改静态属性

§6.3(c) 的样例是 `product.prefer_closetoday(spec.trade_symbol, ref_price)`。落地方案（决定 A）否定了这个签名 ——
`ref_price` 在 8 品种下是**死参数**（两档计价方式恒相同 → 价格约掉 → 结论恒定），
调用点被迫传魔法值。

| 现口径 | 说明 |
|---|---|
| `Product.prefer_closetoday -> Optional[bool]` | **无参 property**，结论在 `Product.__post_init__` 算一次并冻结 |
| `Product.prefer_closetoday_at(ref_price) -> bool` | 运行期路径，**仅当静态属性返回 `None`**（两档混合 `rate`/`per_lot`，价格不可约）时才用 |
| 引擎消费点 | `Engine._prefer_closetoday`：先读静态属性，`None` 才落回 `_at()` |
| 启动横幅 | `main.py:_fee_banner` 三态分支，不再传 `(1.0)` |

`Optional[bool]` 的 `None` 承载的是「**该结论是否已静态确定**」，不是"未知"。

### G.4 其余落地项（一句话表）

| 编号 | 内容 | 关键落点 |
|---|---|---|
| R1（P0） | 补建费率对账测试（原为**零覆盖**） | `Test/test_product_fee_table.py`（**100 断言**，7 组） |
| R2 | `LOCK_PATH_MULT` 改 `ClassVar`（原写在 dataclass 体内 = 字段，可逐实例覆写阈值） | `Product.py` |
| R3 | 补 `fee_overrides` 字段位（数据由生成区块填，**仍不消费**、行为零变化） | `Product.py` |
| 决定 C | 删 `.spec` 兼容别名，Broker/Engine/Source 统一 `.state` | `Broker/Base.py` 未初始化时显式 `RuntimeError` |
| 决定 D | 有效值收进 `frozen` 的 `EffectiveSpec`，`apply_quote` 改**单次整体替换** | `Instrument.py`；原子性从"靠代码顺序"升级为"结构保证" |

**测试基线**：51 → **52**（补回的费率测试以「区块 ⇄ 档案 ⇄ xlsx 三方一致」承接了
被删的 `test_p52_fee_economy.py` 的覆盖，兑现 §7.3 的验收纪律）。

### G.5 本轮**未动**（不在授权范围，探针复核后如实留档）

| 编号 | 现状（实测） | 建议修法 |
|---|---|---|
| R4 | `InstrumentConfig` 旧键守卫在 `__init__` → 构造期报错 ✅，但 `model_copy(update={"price_tick":0.5})` **静默通过**（pydantic v2 `model_copy` 不跑 `__init__`） | 挪到 `@model_validator(mode="before")` |
| R5 | `Instrument.cost_cash(self, product, …)` 仍收外部品种档案 → 同源性后门 | 去掉形参，内部读 `self.product` |
| R7 | `Infra/StateDB.py` 里类名仍叫 `Store`；文档写 `TradingClock.py`、实现是 `Clock.py` | 类名/文档口径二选一对齐 |
| R8 | `InstrumentState` / `InstrumentSpec` / `ProductProfile` 仍出现在多个 docstring 的历史叙述里 | 属留档说明，不改不影响行为 |

### G.6 复现命令

```bash
# 刷新费率区块（改了 xlsx 或生成器解析规则后）
python Trading/Tool/GenFeeTable.py
# CI 口径：校验区块与 xlsx 是否一致（不一致 rc=1；xlsx 缺失则 rc=0 + skip）
python Trading/Tool/GenFeeTable.py --check
# 费率对账（离线可跑；缺 xlsx/openpyxl 时 [3] 组自动 skip）
python Trading/Test/test_product_fee_table.py
```

---

## 附H 第 8 个模块登记：`Infra/trade_stats.py`（2026-09-16）

> 与附G 同样是**增量记录**：不改写 §5.1 正文，以本附录为准。

### H.1 事实

§5.1 把 `Trading/Infra/` 定死成 **7 个模块**（`Clock / Records / Period / Product /
Instrument / StateDB / EventLog`），§附G.1 又重申过一次这条纪律。
**2026-09-16 落地的「成交统计面板」新增了第 8 个文件**：`Trading/Infra/trade_stats.py`
（216 行：纯计算 + 只读）。本条把它正式登记并给出变异轴。

### H.2 它与 G.1 的定案不冲突，但**确实是第 8 个文件**

G.1 禁的是「**机器生成的纯数据**另开文件」—— 理由是"生成物与手写物"的分离该靠
「标记 + 逐字节护栏」，而不是靠文件边界。`trade_stats.py` **不是生成物**（无 xlsx
真值源、无 `--check`、无 GENERATED 区块），它是**手写的计算模块**，故 G.1 不适用。

但它**确实**让 `Infra/` 变成了第 8 个文件 —— **破的是"7 个"这个数字，
不是"按变异轴归位"这条原则**。所以处置是登记 + 说清变异轴，而不是硬塞进某个现有模块。

### H.3 变异轴

| 问句 | 答 |
|---|---|
| 它装什么 | 从已落库的 `trades` 表汇总「已兑现往返」的统计（胜率 / PF / 期望值 / 累计净值曲线） |
| 什么变了它才变 | **统计口径**（想看哪些指标、胜负怎么定义、曲线怎么画） |
| 与那 7 个的关系 | 与 `Product`（品种事实）/ `Instrument`（运行时状态）/ `StateDB`（持久化）**全部正交**：它们变它不变，它变它们不变 |
| 为什么不塞进 `StateDB` | `StateDB` 的变异轴是**持久化格式**；而统计**刻意绕开 `Store`**（`Store.__init__` 会 `executescript(_SCHEMA)`、对旧库还可能 `RENAME TABLE` —— 都是写操作），改用 `sqlite3.connect(uri mode=ro)`。塞进去等于把"只读统计"绑上"会写库"的载体 |
| 为什么不塞进 `Records` | `Records` 装的是**数据结构**（Position / Trade / Signal / Order），不装"从数据算出来的东西" |

### H.4 结论

`Trading/Infra/` 现为 **8 个模块** = §5.1 的 7 个 + `trade_stats.py`（本附录）。
§5.1 正文的"定死 7 个"**不改**（遵本附录惯例），但后续新增文件时请按 H.3 的
三问句先答一遍：答不上"什么变了它才变"的，就不该开新文件。
