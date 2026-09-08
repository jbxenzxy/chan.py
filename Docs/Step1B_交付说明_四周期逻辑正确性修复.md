# Step 1B 交付说明：四周期逻辑正确性修复（30m / 5m / 1m / 15s）

- 基线：`custom-dev` @ `5478def`
- 日期：2026-09-08（v2 修订：BUG-5 误判已撤回）
- 范围：仅 `Trading/` 目录，主程序（chan.py 本体）**零改动**
- 目标：Step 1 —— 换个周期代码**能正确跑通**（不含盈利性调参，那是 Step 2）

---

## 〇、v2 修订（重要：撤回 BUG-5 误判）

**v1 把 `max_hold_bars=30` 当作 Step 1 缺陷**（"跨周期语义漂移 120 倍"）并改成秒，**错的**。
复查 `C:\my_chan_project\Docs\止盈止损\TP_SL_L4_时间止损_+_收盘强平原理.html` 与
`止盈止损四层策略原理.html` 后确认：设计文档 L4 明确写 **"N 根 K 线无进展 → 走"**，
计量单位是 **bar（信息量单位）** 不是墙钟时间 —— 30 根在任何周期下都是 30 根，与周期无关。

v1 的变更在 15s / 1m / 30m 上**静默改变了策略行为**（30m 30 根 15h → 5 根 2.5h；15s 30 根 7.5min → 600 根 2.5h），
等于在 Step 1 越界做了 Step 2 调参。v2 撤回：

- `Config.py`：`max_hold_bars: int = 30` 恢复为主口径（默认行为**与基线完全一致**）
- `Config.py`：新增 `max_hold_seconds: float = 0.0` 作**可选**附加顶（默认关闭，不干扰基线）
- `Exit.py` / 测试：恢复根数主口径 + 新增 [5c] 测试"秒顶为可选，谁先到谁生效"

教训记入长期记忆：**用户指出我没解释清楚的设计选择时，要先去 SSOT 文档核实，再回话。**
不要把"标定差异"当成"代码缺陷"修。

---

## 一、一句话结论

8 个周期相关缺陷中，**6 个已修复并钉成回归测试，1 个改为显式告警（属 Step 2 调参），1 个是架构约束非代码缺陷**。
干净副本复验：**原有测试全绿 + 新增 103 项断言全绿**（`test_p8` 由 35 → 39 项）。

---

## 二、缺陷修复对照

| ID | 问题 | 修复 | 验证 |
|---|---|---|---|
| BUG-1 🔴 | `_bar_secs` 推断把**毫秒当秒**比 `60~14400` → 4 个周期全部推断失败、静默降级 | ① 周期改由引擎按 `source.freq` **注入**（不再运行时猜）；② 兜底推断用 `ts_scale()` 按时间戳**绝对量级**判定单位 | 矩阵 [3] 8 项 + 对账 [1] |
| BUG-2 🔴 | 平仓冷却 `_close_retry_bars=5`（根数）拿**毫秒时间戳差值**比较 → 实际 5 毫秒，冷却从未生效 | 改用 `bars_seen` 序号差（真正的根数，与周期/单位无关） | p15b、p20 全绿 |
| BUG-3 🔴 | 收盘判定用 **bar 起点** → 30m 起点只有 :00/:30，永不可达（死代码）；15s 的 `date` 带秒被 `[:5]` 截掉 | 统一走 `eod_triggered()`：`起点 + bar_secs + lead×bar_secs ≥ 阈值`；时间解析用 `parse_hhmmss()` 保留秒 | 矩阵 [4] 12 项 + [4b] 旧口径复算 |
| BUG-4 🟠 | ATR 缓冲跨日不清空 → 隔夜跳空把 30m 近两天 ATR 全顶高 | `on_bar` 检测日期变化清空缓冲 | 矩阵 [7] 4 项 + [7b] 不误伤 |
| BUG-5 🟠 | `max_hold_bars=30`（根数）跨周期语义漂移 **120 倍** | **撤回**：30 根的语义自始至终就是 30 根（**根数**为计量单位，不是墙钟时间，与周期无关）；120 倍是墙钟跨度的"标定差异"，属 Step 2 调参。bug 误判已修，新增 `max_hold_seconds` 作**可选附加顶**（默认 0=关），主口径仍是 `max_hold_bars` | 矩阵 [5] 8 项 + [5b] 标定参考 |
| BUG-6 🟠 | 追价窗口（20s）长于 15s 的一根 bar | **不自动改**（属 Step 2 调参）→ 启动期落 `chase_window_exceeds_bar` 事件，可见而非静默 | 引擎启动事件 |
| BUG-7 🟡 | `signal_max_age_minutes` 在 SSE 里硬编码 60，无法配置 | 默认值单一事实源移到 `Config.SourceConfig`（60.0），SSE 读取配置 | 配置层 |
| BUG-8 🟡 | 15s 数据纵深仅 1 个交易日 | **架构约束非缺陷**：补纵深 → SSE 每帧 payload 爆炸。Step 2 决策 | — |

### 顺带修正上一轮的判断
上一轮说 BUG-1 "仅 15s 失效"，**错误**。核实 `App/AppSSE.py:390` 后确认 `timestamp` 是毫秒，
**4 个周期 × 双数据源全部失效**；且 30m 因 bar 起点限制，EOD 兜底是**双失效**。

---

## 三、改动文件清单（14 个）

### 新增（3）
| 文件 | 作用 |
|---|---|
| `Trading/Infra/PeriodProfile.py` | **周期语义唯一事实源**：`FREQ_SEC`、`ts_scale()`、`norm_delta_sec()`、`parse_hhmmss()`、`eod_triggered()`、`PERIOD_PROFILES` 骨架 |
| `Trading/Test/test_period_consistency.py` | 周期表**对账**：与主程序 `Common.CEnum.FREQ_SEC_MAP` / `FREQ_TABLE` 逐项比对（23 项） |
| `Trading/Test/test_period_matrix.py` | **四周期回归矩阵**：4 周期 × 毫秒/秒源 + 引擎级启动冒烟（80 项） |

### 修改（11）
`Config.py`、`main.py`、`README.md`、`Engine/Engine.py`、`Engine/Reconcile.py`、
`Source/SSE.py`、`Strategy/Base.py`、`Strategy/Exit.py`、
`Test/test_p8_layered_exit.py`、`Test/test_p15b_e33_fifo_close.py`、`Test/test_p20_phase_i1.py`

---

## 四、关键设计决策（按你的拍板）

**1. `bar_secs` 自持一份**（不 import 主程序 `FREQ_SEC_MAP`）
保持 Trading 对 chan.py 零侵入、零 import —— 它是独立部署的网关，只经 HTTP SSE 取数。
代价是两表可能漂移 → 由 `test_period_consistency.py` 在 CI 里逐项对账，主程序改周期而这里没跟上就红。

**2. 时间类参数保留「根数」主口径，墙钟作为可选附加顶**
`max_hold_bars`（根数，默认 30）= 主口径 —— 与设计文档 L4 描述一致（"N 根 K 线无进展 → 走"）。
`max_hold_seconds`（墙钟秒，默认 **0=关闭**）= 可选附加顶，与根数取"或"（谁先到谁生效）。
**这一项原本想改成"秒为主"**（Step 1 修复 BUG-5），但复查 L4 设计文档后确认：根数本就是正确语义，
120 倍墙钟跨度是"标定差异"不是"代码缺陷"，属 Step 2 调参。已在代码注释中留下更正记录，
并保留 `max_hold_seconds` 作为开关供 Step 2 在需要时启用（默认 0=关闭 → 行为与基线完全一致）。

**3. 30m 纳入**
它一天只有 8 根 bar，问题最多，但正因为极端才是最好的粗周期压力测试 —— 保留在测试矩阵里。

**4. 启动期 fail-fast**
`main.py` 校验 `freq`，不支持直接退出并提示改 `PeriodProfile.FREQ_SEC`：

```
[gw] 不支持的 K 线周期: '99m'（当前支持: 15s, 1m, 5m, 30m）...
[gw] 周期档案 freq=15s bar_secs=15s（约 1080.0 根/交易日）
```

---

## 五、复验结果（干净克隆 + 覆盖 zip）

```
test_p10  59✓  test_p11  59✓  test_p12  95✓  test_p13  51✓  test_p14a 56✓
test_p15a 59✓  test_p15b 89✓  test_p16  65✓  test_p17  43✓  test_p18  52✓
test_p19  50✓  test_p20  82✓  test_p21  11✓  test_p22  14✓  test_p23  36✓
test_p5    9✓  test_p6   21✓  test_p7   21✓  test_p8   41✓（基线 35，+6）
test_p9   71✓  simnow_guards 15✓
test_period_consistency 23✓   test_period_matrix 83✓
────────────────────────────────
合计 1136+ 通过 / 0 失败
```

复验方式：重新克隆 `custom-dev@5478def` 到独立目录 → 覆盖 zip → 跑全套。
⚠️ 注意：`Expand-Archive` / PowerShell `Compress-Archive` 在本机都会静默失败（已二次踩坑），
本机打包/解压一律用 Python `zipfile`。

---

## 六、合并方式

```bash
# 方式 A：整体覆盖（推荐）
#   解压 step1b_四周期修复_Trading.zip，把 Trading/ 覆盖到你的项目

# 方式 B：看 patch 后手动合
git apply step1b_changes.patch
```

合并后必跑：
```bash
python Trading/Test/test_period_consistency.py   # 与主程序周期表对账
python Trading/Test/test_period_matrix.py        # 四周期回归矩阵
```

---

## 七、遗留（Step 2 处理）

1. **`chase_window_exceeds_bar`**：15s 下追价窗口 20s > bar 15s，已有告警，需要调 `close_max_chase × chase_interval`。
2. **`signal_max_age_minutes`**：15s 下 60 分钟 = 240 根 bar，建议收紧。
3. **15s 数据纵深只有 1 个交易日**：大级别结构建不起来；补到 5 日需 4800 根 → SSE 帧负载爆炸。纵深与帧负载不可兼得，需拍板。
4. **锁仓累积**：`capital_gate` 只看单笔不看总腿数，15s 高频下会缓慢透支。
5. **盈利性参数**：`overprice_points` / `r_multiple_tp` / `atr_period` 等各周期差异化 —— 这是 Step 2 的主战场，
   骨架 `PERIOD_PROFILES` 已就位，届时可以只填数不改结构。
