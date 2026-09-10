# PositionOrigin 全量改名 + Types.py 注释清理（2026-09-10）

> **本包不兼容旧状态库，且要求先删一个旧文件。** 应用前请读「⚠️ 应用步骤」。

---

## ⚠️ 应用步骤（两步，别漏第二步）

```bash
# 1) 覆盖（Trading/ 整个目录 + Frontend/app.js）
#    Trading/ 与 Frontend/ 直接解压覆盖到仓库根即可

# 2) 必删：上一包引入的旧名测试文件（不改名会 import 失败，拖垮全套回归）
rm Trading/Test/test_p25_entrymode_decoupled.py

# 3) 必删：旧 schema 状态库（见下节"旧 schema 闸门"）
rm Trading/State/state.db
```

---

## 一、改名映射（669 处替换 / 18 个文件）

`entry_mode` 这个名字是早期设计的残留：当时假定"OPEN_FIRST → 锁仓软离场 /
UNLOCK_FIRST → 平仓硬离场"，即**来源决定离场方式**。规则 ⑸ 改成按建仓日期判定后，
该假定作废，名字却留着 —— 正是容易误导后来人的隐患。本次彻底改：

| 旧 | 新 | 说明 |
|---|---|---|
| `EntryMode` | `PositionOrigin` | 枚举名：它是**来源**标记，不是"模式开关" |
| `EntryMode.OPEN_FIRST` | `PositionOrigin.SIGNAL_OPEN` | 值 `"open_first"` → `"signal_open"` |
| `EntryMode.UNLOCK_FIRST` | `PositionOrigin.UNLOCK_UPGRADE` | 值 `"unlock_first"` → `"unlock_upgrade"` |
| `EntryMode.LOCKED` | `PositionOrigin.SOFT_EXIT_LOCK` | 值 `"locked"` → `"soft_exit_lock"` |
| `Position.entry_mode` | `Position.origin` | 字段名，JSON 键同步改为 `"origin"` |

**枚举值也改了**（你拍板"无需 db 兼容"）—— 所以旧 state.db 不可用，见下节闸门。

替换范围：`Trading/**/*.py` + `Frontend/app.js`。
`App/` 目录经核查**不引用**该字段，未改动。

### 为什么 `SOFT_EXIT_LOCK` 而不是 `LOCK_LEG`
三个值现在语义统一：都回答"这笔腿是怎么来的"。
`SOFT_EXIT_LOCK` = 来自"软离场锁仓"，与 `ExitMode.SOFT_EXIT` 用词一致。
（`LOCK_LEG` 回答的是"它是什么"，会让枚举语义混两种问法。）

---

## 二、旧 schema 闸门（本次新增，安全配套）

改名把持久化键 `entry_mode` → `origin`。**不加防护会出事**：

```
旧 state.db 的锁仓腿（原 entry_mode="locked"）
  → Position.from_dict 读不到 "origin" → 静默回退成 SIGNAL_OPEN
  → 引擎把它当"真实净敞口" → 接进 L1-L3 止盈止损
  → 可能对锁仓腿发平仓单（账实不符）
```

而 `_reconcile_positions` 只在 `real_vol > engine_vol` 时**告警**、不纠正
（Reconcile.py:45「不自动接管未知持仓」），兜不住这个错。

**新增闸门**：`Engine._restore` 开头调用 `_reject_legacy_state()`，
发现持仓记录仍含 `entry_mode` 键就**抛 RuntimeError 拒绝启动**，
错误信息直接给出处理方式，并先写一条 `state_schema_incompatible` 事件。

设计取舍：**宁可拒绝启动，不可静默猜**。交易系统里"猜错的持仓类别"
比"起不来"危险得多。

### 闸门实弹验证（`verify_schema_gate.py`，可复跑）

```
[1] 旧 schema：拒绝启动 ✓
    异常信息首行：state.db 的持仓记录仍是旧 schema（含 entry_mode 键），与当前代码不兼容：
[2] 抛错前写了 state_schema_incompatible 事件：✓
[3] 新 schema：正常启动 ✓  簿内 1 笔，state=idle
    锁仓腿恢复为 IDLE（未当敞口腿接进 IN_TRADE）：✓
[4] 反事实：旧记录直接 from_dict → origin = 'signal_open'
    锁仓腿被伪装成敞口腿（若静默恢复，引擎会判 IN_TRADE 并接进 L1-L3）：✓ 隐患成立
```

[4] 是关键：[3] 证明新 schema 下锁仓腿正确落为 `IDLE`（不误伤），
[4] 证明旧 schema 若静默恢复，同一笔腿会变成 `signal_open` → 引擎判 `IN_TRADE`
并把它接进止盈止损。两者对照说明这道闸门不是多此一举。

---

## 三、Types.py 过程注释清理

你说"很多无用的过程注释，能删就删" —— 本次按"只留当前不变量，删掉过程叙事"
的原则过了一遍：

| 位置 | 删掉的内容 | 替换为 |
|---|---|---|
| 模块内枚举段头 | `# Phase A 命名固化（2026-09-05）` + 3 行阶段叙事 | 一行分组标题 |
| `PositionOrigin` docstring | 整段"命名沿革与陷阱"（20 行）＋写死的"9 处判定" | 当前语义 + 一条禁令（"离场不由本枚举决定"） |
| `ExitMode` / `EngineState` | `（4 态，Phase A）`、`不再由 EntryMode 联动` | 补一句当前判据 |
| `Position` 类 docstring | **`v1 只支持"一个实例最多一手"，不做锁仓`** ← **已是完全错误的描述** | 现行多仓 + 双向锁仓语义 |
| `origin` 字段 | 9 行"旧注释已作废、勿恢复" | 一行 + 指向枚举 docstring |
| `entry_date` 字段 | `v1.3（S1）：` 版本标签、Q2 文档引用 | 只留不变量（怎么取值、空串怎么处理） |
| `lock_pair_id` | `v1.3（S4）：` 版本标签 | 只留配对语义 |
| `from_dict` | `em_raw` 影子变量 + "向后兼容旧持仓记录" | `raw`/`origin`，说明改为"脏记录容错" |

**顺带修掉两处已脱节的注释**（核查后确认与代码不符）：

1. `Order.action` 原写 `# "open" | "close"` —— 实际是 `OrderIntent.value`，
   取值可达 `open|unlock|close|lock`（SimNow 四值都可能）。
2. `Trade.reason` 原写 `# tp / sl / signal_reverse / eod / manual` ——
   `signal_reverse` 与 `eod` 在代码里**根本不存在**；实际是
   `tp / sl`（L1-L3）、`settle_exit`（兜底）、`auto_order_off`、`manual`。
3. `OrderIntent.CLOSE` 原写"按 spec.close_today_first 决定 CloseToday/Auto" ——
   该分支上一包已删除，CLOSE 恒为平昨（offset=CLOSE）。

同时把写死的计数**去掉**（原文"出现在 9 处判定"，实际已是 11 处）——
写死数字的注释必然腐烂，改成描述性表述。

---

## 四、测试

`test_p25_entrymode_decoupled.py` → **`test_p25_origin_decoupled.py`**，并扩展到 32 项：

| 组 | 内容 |
|---|---|
| [1] 行为等价 | `origin ∈ {SIGNAL_OPEN, UNLOCK_UPGRADE}` × `entry_date ∈ {今日, 跨日}` 四组合，`_exit_intent` 只随 `entry_date` 变 |
| [2] 源码契约 | AST 剥掉 docstring 后，`_exit_intent` 代码体不得出现那两个值 |
| [3] 全仓扫描 | `Trading/` 非测试代码禁止对这两个值做 `is`/`is not`/`==`/`!=` |
| [4] 持久化往返 | 三个值序列化不变；缺/坏值回退 `SIGNAL_OPEN` |
| **[5] 旧 schema 闸门（新增）** | 旧键记录被准确识别；抛错路径实弹验证（RuntimeError + 事件 + 可执行提示）；新键不误伤 |

**回归：30 个测试文件全部通过，零失败。**

### 改名踩到的坑（值得记一笔）

`test_p15b` 有 2 项一开始挂了 —— 不是逻辑问题，是**字母序副作用**：
测试对 `origin.value` 做了 `sorted()`，改名后 `signal_open` 排到了
`soft_exit_lock` 前面，而期望值是按旧字母序硬写的。已修正期望值并加注释
说明"期望值须按字母序书写"。

另一处：测试桩里直接赋 `_legacy_position_records = TradingEngine.xxx`
（staticmethod 从类上取已是裸函数）会变成实例方法 → 必须包一层
`staticmethod(...)`。

---

## 五、交付内容

```
Trading/                      ← 整个目录，直接覆盖（含全部改名 + 闸门 + 注释清理）
Frontend/app.js               ← 1 行：p.origin === 'soft_exit_lock'
verify_schema_gate.py         ← 可复跑：旧 schema 拒绝启动 + 反事实隐患证明
变更说明_PositionOrigin改名_20260910.md
```

未包含 `Trading/State/`、`Trading/replay_data/`（运行时数据，请自行保留）。

---

## 六、Docs 未改（等你决定）

`Docs/` 下多个 md（如 `自动下单_Q1-Q6决策全记录_v1.3.md`、
`自动下单_账户状态机核对与改造方案_v1.3.md`、`变更说明_规则5改造_20260910.md`）
仍在用 `entry_mode` / `OPEN_FIRST` 等旧名。

我**故意没动**：这些是带日期的审计快照（v1.2/v1.3 并存），改了会变成
"事后篡改历史"，反而破坏可追溯性。

如果你希望"当前有效"的那几份跟上新命名，说一声，我可以：
- 只改 v1.3 / 20260910 这两批"当前有效"文档；或
- 在 Docs 下加一份《术语对照表：entry_mode → origin》索引，旧文档保持原样。
