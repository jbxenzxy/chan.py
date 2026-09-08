# Step 2.1 可行性分析 · 周期敏感参数入 PeriodProfile

> 配套代码：`Trading/Infra/PeriodProfile.py`（Step 1B 建的骨架，Step 2.1 填充并接入）
> 状态：**可行性分析已出，待你拍板 6 项设计决策后动手实现**
> 前置：✅ 2.0.3b 已合入远端 `custom-dev`（`fc46127`，2026-09-08 11:34），合并完整

---

## 0. 合并验证结论（2.0.3b 落地证据）

直接从 `https://github.com/jbxenzxy/chan.py/tree/custom-dev` 拉取最新（shallow clone + 证书绕过），对照 2.0.3b 期望结构：

| 校验项 | 结果 |
|---|---|
| 远端 HEAD ≠ 此前 tip（`5478def`） | ✅ 新 commit `fc46127772c15f765667dcbec098c97c95663238`（2026-09-08 11:34:36 +0800，"更新"） |
| `Config.py` 含 `EntryConfig` / `ExitConfig` / `entry_params` / `exit_params` | ✅ 4 项全在 |
| `Strategy/Entry.py` / `Exit.py` 用 `EntryConfig` / `ExitConfig` | ✅ |
| `main.py` / `Engine/Engine.py` / `README.md` 用新名 + 新字段 | ✅ |
| `cfg.entry_policy` / `cfg.exit_policy`（旧选择器字段访问） | ✅ 0 处 |
| `class DefaultExitPolicy`（整树） | ✅ 0 处 |
| `EntryPolicyConfig` / `ExitPolicyConfig` / `register_exit` / `register_entry` / `build_*_policy` / `EXIT_POLICIES` / `ENTRY_POLICIES` | ✅ 全部 0 处 |
| `Test/test_p11_intent_exitmode.py` 出现 `DefaultExitPolicy` | ✅ 仅 1 处注释（line 121，历史注解），无 import / 实例化 |

→ **2.0.3b 合并完整。** 远端与交付 zip 结构一致。

---

## 1. 目标

把 8 项**周期敏感参数**从「散落在各层模型 + `PERIOD_SENSITIVE_FIELDS` 只读索引」迁移到 `PeriodProfile`，使每个周期（15s / 1m / 5m / 30m）拥有**独立、可覆盖、可追溯**的参数集。`TradingConfig` 按 `source.freq` 自动选 profile。

| 项 | 2.0.3b 现状 | 2.1 目标 |
|---|---|---|
| 取值归属 | 散落在 `SourceConfig` / `ExitConfig` / `RiskConfig` 各层模型 | 集中在 `PeriodProfile` 各周期条目 |
| 索引 | `PERIOD_SENSITIVE_FIELDS` 8 项只读 path 索引 | `PeriodProfile` 即新 SSOT，`PERIOD_SENSITIVE_FIELDS` 改为派生视图 |
| 选 profile 机制 | 无（flat 字段无周期区分） | `source.freq` → `PERIOD_PROFILES[source.freq]` 自动选 |
| 跨周期一致性 | 人工保证（手动改 freq 时忘改其它字段） | 框架保证（profile 原子切换） |
| 调参入口 | 各层模型字段，分散 | `PeriodProfile` 单文件 4 周期对照表 |

---

## 2. 8 项周期敏感参数清单

| # | 字段（path） | 当前所在层 | 在 PeriodProfile 中的处理 |
|---|---|---|---|
| 1 | `source.freq` | ① `SourceConfig` | **作为 profile 选择器**（不存于 profile 内部；profile 的 key 即 freq） |
| 2 | `source.signal_max_age_minutes` | ① `SourceConfig` | 移入 profile |
| 3 | `exit_params.max_hold_bars` | ③ `ExitConfig` | 移入 profile |
| 4 | `exit_params.max_hold_seconds` | ③ `ExitConfig` | 移入 profile |
| 5 | `exit_params.eod_lead_bars` | ③ `ExitConfig` | 移入 profile |
| 6 | `exit_params.bar_secs` | ③ `ExitConfig` | 移入 profile（显式秒数替代运行时推导） |
| 7 | `exit_params.session_end_hhmm` | ③ `ExitConfig` | 移入 profile |
| 8 | `risk.max_trades_per_day` | ④ `RiskConfig` | 移入 profile |

→ profile 容纳 **#2–#8 共 7 项**；`#1 freq` 是选 profile 的 key。

---

## 3. 关键设计决策（6 项，需你拍板）

### 决策 A：profile 落值策略——是「SSOT 迁移」还是「影子覆盖」？

| 方案 | A1：真迁移（profile 持有唯一值，flat 字段从 profile 派生） | A2：影子覆盖（profile 提供默认，flat 字段可独立覆盖） |
|---|---|---|
| 含义 | 7 项值**物理上**只存在于 profile；flat 字段是 profile 的"读视图" | 7 项值在 profile + flat 两边都有；profile 提供默认，flat 字段最终值（profile 默认 + 用户 JSON 覆盖） |
| 消费者 | 改为读 `cfg.period_profile.xxx`（或维持 `cfg.xxx` 通过 property） | 维持 `cfg.exit_params.eod_lead_bars`（零改动） |
| 测试影响 | 24 个测试若读 flat 字段 → 需 property 或全改 | 零改动 |
| 风险 | property 装饰 / pydantic 限制多；改测试面广 | 仍存在「profile 改了 flat 没改」的不一致窗口（启动期一次性 reconcile 可消） |
| **推荐** | ❌ 改动面太大、与"单可替换单元"原则冲突 | ✅ **采纳 A2**：profile 是默认源，flat 字段是消费面，启动期 reconcile 一次 |

> A2 的 reconcile 机制：`TradingConfig.model_validator(mode='after')` 启动期按 `source.freq` 选 profile，把 profile 的 7 个值**灌进** flat 字段（仅当 flat 字段仍是模型默认时；用户显式 JSON 覆盖的字段不动）。

### 决策 B：profile 选择器位置——`freq` 自动选 vs 用户显式指定？

| 方案 | B1：完全按 `source.freq` 自动选 | B2：用户显式 `period_profile: PeriodProfile` 字段 + freq 仅作 fallback |
|---|---|---|
| 含义 | 启动期只看 `source.freq` → `PERIOD_PROFILES[freq]` | TradingConfig 顶层有 `period_profile` 字段；填了用它，没填按 freq 选 |
| 灵活度 | 够用（一个 freq 对一个 profile） | 允许「同 freq 不同 profile」实验（罕见） |
| 复杂度 | 简单 | 多一层 |
| **推荐** | ✅ **B1**：freq→profile 1:1 足够；省一层配置 | — |

### 决策 C：profile 模型结构——`name` 怎么定、`note` 要不要？

```python
class PeriodProfile(BaseModel):
    note: str = ""                         # 调参记录 / 数据来源 / 标定状态
    signal_max_age_minutes: float = 60.0   # #2
    max_hold_bars: int = 30                # #3
    max_hold_seconds: float = 0.0          # #4
    eod_lead_bars: int = 1                 # #5
    bar_secs: int = 0                      # #6
    session_end_hhmm: str = "14:55"        # #7
    max_trades_per_day: int = 20           # #8
```

`note` 字段建议保留：调参时必填（"5m baseline 2026-09-08 沿用 2.0 默认；15s/1m/30m 待 2.7+ 真实数据重标"）。这强制留下标定痕迹，与"SSOT 可追溯"一致。

### 决策 D：`bar_secs` 与引擎 `set_bar_secs()` 注入的关系

当前：flat `exit_params.bar_secs=0` → 引擎启动期 `set_bar_secs()` 按 freq 注入秒数（Step 1B 修 BUG-1）。
2.1 后：profile 显式给值（15s→15、1m→60、5m→300、30m→1800）→ flat 字段被 reconcile 成该值 → 引擎 `set_bar_secs()` 仍读 flat 字段 → **行为等价**（profile 替人填了原本要注入的值，引擎代码不动）。

→ 决策 D：**采纳 profile 显式值**，引擎 `set_bar_secs()` 保留作为防御性兜底（万一 profile 没填，回退到 freq 注入）。无需改 Engine.py。

### 决策 E：未支持的 freq 怎么办（容错）？

`source.freq` 填了 `7m` 之类没在 `PERIOD_PROFILES` 注册的值：
- 启动期 fail-fast：抛 `ValueError("unsupported freq: 7m; registered: 15s/1m/5m/30m")`。
- 与 Step 1B `main.py` 启动期 fail-fast 校验 freq 风格一致（不让悄悄跑）。

### 决策 F：`PERIOD_SENSITIVE_FIELDS`（只读索引）保留还是拆掉？

| 方案 | F1：保留，改为派生 | F2：删除 |
|---|---|---|
| 含义 | `PERIOD_SENSITIVE_FIELDS` 改为运行时从 `cfg.period_profile` 派生（不再硬编码 path） | 删掉；调用方直接读 profile |
| 用途 | 调试/审计/UI 列字段仍方便 | 减少抽象 |
| **推荐** | ✅ **F1**：保留作为「profile 内容摘要」视图（4 周期 × 7 字段的对照表），方便 Step 2.7+ 调参时一眼对比 | — |

---

## 4. 取值策略（核心难点：**数据缺口**）

### 4.1 现状盘点

| 周期 | 数据可得性 | 标定基线 |
|---|---|---|
| 30m | 无真实数据 | 无 |
| 5m | 仅 2 根 demo（`replay_data/klines.json`，合成线性） | 你此前答"不知道"是否手工调过 → **0 确认** |
| 1m | 无 | 无 |
| 15s | 无 | 无 |

→ **4 个周期的合理值全部未知。** 拍脑袋填会掩盖"未标定"事实，比统一占位更危险。

### 4.2 建议策略：**统一 BASELINE 占位 + note 标注未标定**

```python
BASELINE = {
    "signal_max_age_minutes": 60.0,
    "max_hold_bars": 30,
    "max_hold_seconds": 0.0,
    "eod_lead_bars": 1,
    "bar_secs": 0,        # 见下：profile 里 bar_secs 用 freq 真值，flat 仍默认 0
    "session_end_hhmm": "14:55",
    "max_trades_per_day": 20,
}

PERIOD_PROFILES: Dict[str, PeriodProfile] = {
    "15s": PeriodProfile(
        note="占位=BASELINE；待 2.7+ 真实 15s 数据重标；bar_secs=15（显式）",
        bar_secs=15, **BASELINE_NON_BARSECS,  # 6 项 baseline
    ),
    "1m":  PeriodProfile(note="占位=BASELINE；待 2.7+ 真实 1m 数据重标；bar_secs=60",  bar_secs=60,  ...),
    "5m":  PeriodProfile(note="占位=BASELINE；即使 5m 也是占位（无标定）；bar_secs=300", bar_secs=300, ...),
    "30m": PeriodProfile(note="占位=BASELINE；待 2.7+ 真实 30m 数据重标；bar_secs=1800", bar_secs=1800, ...),
}
```

→ **所有 4 周期 = BASELINE，note 显式标 "占位"**。Step 2.7+ 拿到真实数据后，只改对应 profile 的值 + 改 note 写数据来源/日期。

### 4.3 为什么不用「按周期粗估值」？

- Step 1B 可行性分析给过粗估（30m 持仓折 3.75 交易日、15s R=2 点打平胜率 63% 等），但**没有回测**就是"猜"。
- 上一轮你已经把"总腿数 ≠ bug"误判撤回过一次（"不要把标定差异当代码缺陷"教训）。
- **占位 + note** 比"看似精心的错值"安全得多——前者在 note 里明说，后者会让人误以为已标定。

---

## 5. 迁移影响

| 维度 | 改动 | 风险 |
|---|---|---|
| 新增/改 `Infra/PeriodProfile.py` | 大改（从骨架到完整） | 低（新增为主） |
| `Config.py` `TradingConfig` | 加 `model_validator(mode='after')` reconcile；7 个 flat 字段默认不变 | 低（默认行为不变） |
| `main.py` | 加 freq-vs-PROFILES 一致性 fail-fast | 低 |
| `Engine/Engine.py` | **不动**（仍读 flat 字段） | 无 |
| `Strategy/*.py` | **不动** | 无 |
| 24 个 `Test/*.py` | **不动**（仍读 flat 字段） | 无 |
| `Docs/六层架构改动总览.html` | 加一节"2.1 周期参数入 Profile" | 低（文档） |
| 性能 | 启动期多一次 profile lookup + 7 次字段赋值 | 纳秒级，无影响 |

→ **改动收口在 `Infra/PeriodProfile.py` + `Config.py` + `main.py` 三个文件**，符合"单可替换单元"原则。

---

## 6. 风险

1. **profile reconcile 与 pydantic v2 字段赋值顺序**：必须在 `model_validator(mode='after')` 里用 `object.__setattr__` 或 pydantic 允许的方式覆盖（pydantic v2 默认 frozen/validate 较严）。**预案**：用 `model_validator(mode='before')` 改 raw data，比 `mode='after'` 更稳。
2. **`max_hold_bars=30` 等字段在 ExitConfig 已写死默认**：reconcile 时若 flat 字段是用户 JSON 显式给的，不能覆盖——需用"字段值 == 字段 default 才覆盖"的判定，或直接用 `mode='before'` 注入。**已想好：用 `mode='before'`，仅在 raw data 中字段缺失或等于 schema 默认时才填 profile 值**。
3. **`bar_secs=0` 默认值在 profile 里被替换为 freq 真值**：会让 `set_bar_secs()` 行为从"运行时注入"变成"启动期赋值"，效果等价但 `bar_secs=0` 的"自动推导"语义在 profile 体系下消失。**接受**：profile 显式比推导好。
4. **5m baseline 未确认**：用户 JSON 可能显式给了 5m 调过的值，reconcile 不能覆盖。已含在风险 2 的方案里。

---

## 7. 待你拍板的 6 项决策（汇总）

| # | 决策 | 我的推荐 |
|---|---|---|
| A | profile 落值策略 | **A2 影子覆盖**（profile 默认 + flat 消费，启动期 reconcile） |
| B | profile 选择器 | **B1** freq 自动选 |
| C | profile 模型结构 | 7 项值 + `note` 字段 |
| D | `bar_secs` 与引擎注入 | profile 显式，引擎 set_bar_secs 保留作兜底 |
| E | 未注册 freq | 启动期 fail-fast |
| F | `PERIOD_SENSITIVE_FIELDS` | **F1 保留**，改为派生视图 |

外加 1 项核心策略确认：

| # | 策略 | 我的推荐 |
|---|---|---|
| G | 4 周期取值策略 | **统一 BASELINE 占位**（全用 5m 默认值，note 标"占位/待 2.7+ 重标"），不拍脑袋填差异化值 |

---

## 8. 下一步

- **你确认 A–G** 后，我开 2.1 实现：先 `Infra/PeriodProfile.py`（完整 model + 4 周期 profile + note）→ 再 `Config.py`（加 `model_validator(mode='before')` reconcile + PERIOD_SENSITIVE_FIELDS 派生）→ `main.py`（freq 一致性 fail-fast）→ 全套 24 测试必须仍 PASS → 重打包 `step2_1_Trading.zip` + 交付说明。
- **2.1 不做调参**（不动 4 周期的实际值），只把架构和"占位 + note"骨架立起来。
- **2.7+**（真实数据到位后）才动 4 周期差异化值，那时候这套 profile 结构能让改动收口在一处。
