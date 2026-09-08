# Step 2.1 交付说明 · 周期敏感参数入 PeriodProfile

> 配套代码：`step2_1_Trading.zip`（整包覆盖 `Trading/`，单可替换单元）
> 前置：2.0.3b 已合入远端 `custom-dev`（`fc46127`），合并完整 ✅
> 状态：**2.1 已实现并验证通过（25/25 测试全绿）**

---

## 1. 本轮做了什么

把 8 项周期敏感参数从「散落各层模型 + `PERIOD_SENSITIVE_FIELDS` 只读索引」收口到 `Infra/PeriodProfile.py` 的 `PERIOD_PROFILES`（每周期一份），`TradingConfig` 构造期按 `source.freq` 自动选档案并「影子覆盖」进 flat 字段。

### 设计决策（已拍板）

| # | 决策 | 结论 |
|---|---|---|
| A | 落值策略 | **A2 影子覆盖**：profile 是默认源，flat 字段是消费面；仅当 flat 字段仍是模型默认值时填入，用户显式覆盖优先 |
| B | 选择器 | **B1**：`source.freq` 自动选 `PERIOD_PROFILES[freq]` |
| C | 模型结构 | 6 项周期敏感值 + `bar_secs` + `note`（标定记录） |
| D | `bar_secs` | **不改动**：仍走引擎 `bar_secs_for(freq)` 自动推导（`PeriodProfile.bar_secs` 已是推导源）；flat 的 `bar_secs=0` 保留「手动覆盖」语义 |
| E | 未知 freq | **容错**：`TradingConfig` 不 fail-fast（AppTrader 等非引擎场景 freq 可能只是透传，如 test_p20 的 "15m"）；真正的 fail-fast 在 `main.py`（`bar_secs_for` 抛 `SystemExit`，已有） |
| F | `PERIOD_SENSITIVE_FIELDS` | **F1 保留**：静态说明（path/layer/kind/step2）+ 新增 `period_sensitive_summary()` 派生视图（4 周期 × 周期敏感值） |
| G | 取值策略 | **G1 统一 BASELINE 占位**：4 周期全用 5m 当前默认值，`note` 显式标「占位/待 2.7+ 重标」；不拍脑袋填差异化值 |

> ⚠️ 与可行性分析 §2 的一处修正：可行性文档里说「profile 容纳 7 项（含 bar_secs）」，
> 实现时发现 `bar_secs` 自 Step 1B 起就已由 `PeriodProfile.bar_secs` + 引擎 `bar_secs_for(freq)`
> 自动推导，flat 的 `bar_secs=0` 是「手动覆盖」旋钮。所以 2.1 实际 reconcile 的是 **6 项**
> （signal_max_age_minutes / max_hold_bars / max_hold_seconds / eod_lead_bars /
> session_end_hhmm / max_trades_per_day），`bar_secs` 保持自动推导不动 —— 更干净、零风险。

---

## 2. 改动清单（3 个文件 + 1 个新测试）

### `Infra/PeriodProfile.py`（核心）
- `PeriodProfile` dataclass 扩展：新增 `note` + 6 项周期敏感字段（默认 = BASELINE，即当前 5m 默认值）。
- `PERIOD_PROFILES` 改为 4 条显式条目（15s/1m/5m/30m），各自 `note` 标注「占位=BASELINE；待 2.7+ 真实数据重标」。

### `Config.py`
- 导入 `model_validator` + `PERIOD_PROFILES` / `PeriodProfile` / `SUPPORTED_FREQS`。
- `TradingConfig` 新增：
  - `_reconcile_period_profile`（`@model_validator(mode="after")`）——构造期按 `source.freq` 选档案，把 6 项「影子覆盖」进 flat 字段（仅当字段仍是模型默认值；未知 freq 跳过）。
  - `apply_period_profile()`——CLI `--freq` 覆盖后重对齐（`main.py` 调用）。
  - `period_profile` 只读 property——当前 freq 对应档案。
- 新增 `period_sensitive_summary()`——4 周期 × 周期敏感值派生视图（F1）。
- `PERIOD_SENSITIVE_FIELDS` 注释更新：值已归入 PeriodProfile，本表保留作静态说明。

### `main.py`
- CLI 覆盖后（`bar_secs_for` fail-fast 之后）调用 `cfg.apply_period_profile()`，让 flat 字段跟随 `--freq`。

### `Test/test_period_profile.py`（新增，55 项断言）
覆盖：PeriodProfile 字段自洽 / 影子覆盖 / 用户覆盖优先 / 未知 freq 容错 / apply_period_profile 重对齐 / period_sensitive_summary 派生视图。

---

## 3. 验证

- **compileall 全绿**。
- **冒烟**：默认 5m / 显式 15s / 未知 15m 容错 / 用户覆盖优先 / 改 freq 后重对齐 / 派生视图 4 行，全部符合预期。
- **全套 25 个 `test_*.py`：PASS=25 / FAIL=0**（原 24 个 + 新增 `test_period_profile.py`）。
- **从 zip 内读回**：`PeriodProfile.py` 含 `note`/6 项新字段/`占位=BASELINE`；`Config.py` 含 `model_validator`/`apply_period_profile`/`period_profile`/`period_sensitive_summary`；`main.py` 含 `cfg.apply_period_profile()` 调用；`Test/test_period_profile.py` 在包内。

### 行为等价性（关键）
G1 下 4 周期的 profile 值 == schema 默认值（BASELINE），所以影子覆盖是**幂等 no-op**——5m 行为与 2.0.3b 完全一致，24 个旧测试一个没改、全绿。2.1 是「把架构立起来」，不是「改数值」。

---

## 4. 待 2.7+（真实数据到位后才做）

- 拿到真实历史数据（当前仅 2 根合成 demo）后，逐周期差异化标定 6 项 + `bar_secs` 已在档案里。
- 届时只改对应 profile 的值 + 改 `note`（写数据来源 + 日期），`PERIOD_SENSITIVE_FIELDS` 的静态说明与 `period_sensitive_summary()` 会自动反映新值。
- 已知局限（已注释在代码里）：`_apply_profile_values` 用「== schema 默认」判定是否覆盖，若 2.7+ 让某 profile 值本身偏离 schema 默认、且 `--freq` 在构造后二次切换，需要把「profile 已填充过的字段」显式追踪起来再重对齐。当前 BASELINE 下无此问题。
