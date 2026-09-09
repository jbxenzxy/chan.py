# custom-dev 合入后复审报告（第 2 轮）

> 评审时间：2026-09-09 05:30
> 评审对象：**HEAD = `3aff491`（2026-09-08 21:59）**，即上一轮评审（20:09 快照 = `8ca03aed`）之后新合入的 3 个提交
> 评审方法：GitHub API 取 3 个提交的完整补丁 → 与上一轮快照逐文件 diff → 全量包交叉校验 → 28 个测试实跑 → 残留引用全仓扫描 → 用真实回放数据做新逻辑前提假设的实证
> 结论速览：**上一轮 7 项问题已修 6 项（P1-1 为部分修复，留了一个绕过口）**；本轮**新增了一处语义重写（信号新鲜度口径）**，带来 1 个需要真实数据回归的**行为风险**；另发现 **3 个 P2 + 4 个 P3** 新问题。**28/28 测试通过、编译通过、main 冒烟与基线一致、无回归。**

---

## 一、这次合进来了什么（3 提交 / 11 文件）

| 提交 | 时间 | 内容 |
|---|---|---|
| `532b91b1` | 09-08 20:39 | **修复上轮问题**：RiskConfig 加 1..20 校验、删 InstrumentSpec.sessions/in_session、EventLog 删 risk_block/day_roll 标签、main.py 排障文案改指 open_silenced/order_rejected、Store 注释改"历史键"、Engine 删 `if new_lots>0: pass` 死块、README 配置示例重写 |
| `f16ec0e0` | 09-08 21:52 | **语义重写**：`source.signal_max_age_minutes`（墙钟分钟）退役 → 改为 `source.signal_k_tol_bars`（K 线相对根数容差，默认 1）；SSE 过滤逻辑重写；PeriodProfile 删该字段；`_apply_profile_values` 变 no-op；测试同步 |
| `3aff4917` | 09-08 21:59 | 注释补充（tol = tolerance） |

---

## 二、上一轮问题逐项复核

| 上轮编号 | 问题 | 状态 | 证据 |
|---|---|---|---|
| **P1-1** | 删了开仓前 20 手交易所上限拦截 | **已按方案 A 修（但留绕过口，见新问题 P2-1）** | `Config.py:311-320` 新增 `RiskConfig._check_max_volume`，`1..20` 越界 fail-fast |
| **P2-1** | `InstrumentSpec.sessions` / `in_session()` 死代码 | ✅ 已修 | 字段与方法已删；全仓 grep（`Trading/` + `App/` + `Docs/`）**零残留** |
| **P2-2** | `Store.py` 仍宣称持久化 `day_stats` | ✅ 已修 | `Store.py:11`、`:138-150` 明确改写为"历史键，仅清理旧库时删，不再写入" |
| **P2-3** | 排障文案指向不存在的 `risk_block` | ✅ 已修 | `EventLog.py:24` 删 `risk_block`/`day_roll` 两标签；`main.py:46` ECHO 集合同步；`main.py:173-174` 改指 `open_silenced / order_rejected`（二者均有真实写入点，已核实） |
| **P2-4** | `Engine._unlock_position` 的 `if new_lots>0: pass` 死块 | ✅ 已修 | 整块删除，注释并入 `unlock_result` 事件处 |
| **P3-1** | README 配置示例列出已删键导致启动报错 | ✅ 已修 | risk 段改为 `max_volume / max_open_positions / unlock_no_new_open`；exit_params 删 L4 五键；多周期表删 3 行；架构图"风控 risk(闸门)"→"手数/持仓上限" |
| **P3-2** | Docs 旧基线文档描述已删功能 | ❌ **未处理** | `Docs/Step1B_交付说明…md:46,137`、`Docs/Step2_0_交付说明…md:150` 仍把 `signal_max_age_minutes` 列为周期敏感调参项——**本次该字段已退役，文档比上一轮更旧了** |

---

## 三、本轮新增的语义变更及其风险（重点）

### 变更内容：信号新鲜度判定从「墙钟分钟」改为「K 线相对根数」

| | 旧 | 新 |
|---|---|---|
| 配置 | `source.signal_max_age_minutes = 60.0`（分钟） | `source.signal_k_tol_bars = 1`（根） |
| 判据 | `now − first_seen_at ≤ 60min` | `(最新K时间戳 − 信号K时间戳) / bar_ms ≤ 1` |
| 5m 周期下的实际松紧 | ≈ **12 根** | **1 根** |
| 周期敏感性 | 强周期敏感（15s 下 60min=240 根） | 非周期敏感（改周期不用调） |

**设计方向是对的**：按"相对根数"计量确实消除了周期敏感，也免去了 15s 下必须重标的麻烦。前提假设我也做了实证——用 `Trading/replay_data` 的 144 根 5m K 线与 7 个真实信号核对：**7/7 信号的时间戳精确等于某一根 K 线的时间戳**（`Trading/Source/SSE.py:141-146` 的计算前提成立）。

### R1（P1 / 行为风险）默认 N=1 比原口径严格一个量级，且过滤是**完全静默**的

`Trading/Source/SSE.py:144-149`：

```python
if self._bar_ms and latest_bar_ts:
    dist_bars = (latest_bar_ts - s.timestamp) / self._bar_ms
    if dist_bars > self.signal_k_tol_bars:
        cur.add(s.key)   # ← 直接丢弃
        continue
```

1. **放宽→收紧**：5m 下从"12 根内都收"变成"1 根内才收"。chan.py 的 SSE 是**累计推**语义，一个 bsp 一旦在某个帧被判定过期，后续帧的 `dist_bars` 只会越来越大，**永远不会再补回来**。所以只要 chan.py 的买卖点确认滞后 ≥2 根 K，实时信号就是**永久丢失**，而不是延后。
2. **静默无埋点**：被丢弃的信号**不写任何事件、不加任何计数**（`EventLog` 里也没有对应 kind）。一旦 N 设小了，故障现象是"实盘一个信号都没有"，而 `events.jsonl` / `gateway.log` 里**查不到任何原因**——排障成本极高。
3. **未能本地实证**：我尝试用本地 chan.py 逐根喂这 144 根 K 实测"bsp 确认滞后根数"，被两件事挡住——
   - 默认 `CChanConfig()` 在这段数据上**未产出任何买卖点**；
   - 回放数据本身有 **16/144 根 K 线非法**（见附带发现），chan.py 直接抛 `KL_DATA_INVALID`。
   所以 N=1 够不够，**必须由对方用真实 SSE 流回归一次**，我不下结论。

**建议**：① 给被过滤信号加 `signal_stale` 事件或计数（至少首连阶段）；② 首次实盘前先跑一段真实流，打印 `dist_bars` 分布再定 N；③ 保守起见默认取 2~3 更稳妥（滞后 2 根仍算新鲜，同时首连几天前的历史信号依然是几百根，过滤效果不打折）。

### R2（P3 / 风险面小）未知周期或帧内无 klines 时，过滤整体失效

`SSE.py:82`：`bar_secs_for(freq, default=None)` 未知周期返回 `None` → `self._bar_ms` 为空 → 整段过滤跳过，首连重放保护归零，只剩引擎幂等兜底。
不过 CLI 路径下 `main.py:85-91` 已对未知 freq fail-fast，实际只剩"某帧恰好没带 klines"这一种情况。可接受，但建议在跳过时打一条 WARNING，别静默降级。

---

## 四、新发现的问题

### P2-1 新增的 1..20 校验可以被「构造后赋值」绕过

- `Config.py:297`：`RiskConfig.model_config = ConfigDict(extra="forbid")`，**没有 `validate_assignment=True`**；全文件 grep 无 `validate_assignment`。
- 因此 `RiskConfig(max_volume=25)` 会报错，但 `cfg.risk.max_volume = 25` **不会**。
- 证据：`Test/test_p15a_open_lots.py:143-147` 的 `make_engine()` 正是构造后赋值，`:337-343` 场景断言 **"max_volume=25：一笔挂 25 手（无 20 手上限截断/拒单）"** 且**测试通过**——与新增校验的意图直接冲突。
- 影响：生产路径（`main.py` 走 `TradingConfig` 构造）是安全的；但 AppTrader / 任何用赋值方式改配置的代码路径都能绕过，且该行为已被测试钉死为"预期"。
- 建议：加 `validate_assignment=True`，并把 p15a 的 25 手场景改为 20 边界内（20 手放行 / 21 手断言构造期报错）。

### P2-2 新的过滤逻辑零测试覆盖

`Trading/Test/` 里只有 `test_period_profile.py`、`test_step2_smoke_freq.py` 碰了 `signal_k_tol_bars`，但都只是**配置层**断言（默认值=1、负值被拒）。**SSE 过滤分支本身没有任何测试**（没有测试构造 `SSESource` 或喂 payload）。
建议补一个不联网的纯逻辑测试：构造假 `payload`（`klines` + `bsps`，制造 dist=0/1/2 三种信号），断言只有 ≤N 的被 yield。

### P2-3 过滤静默、不可观测
同 R1-②：被丢弃信号不落任何事件/计数。建议至少加计数（首连 N 根内统计 `stale_filtered`），或在 `signal_skip` 里带 reason。

### P3-1 注释失真：`Config.py:155` 仍写"把 **6 项**周期敏感参数影子覆盖进 flat 字段"
实际 `_apply_profile_values()`（`:166-172`）已是纯 `return`，覆盖项 = 0。

### P3-2 空方法保留
`_apply_profile_values` 只剩 `return`，注释说"保留本方法仅为兼容 main.py 的 apply_period_profile 调用约定"。属于可理解的过渡，但建议直接删掉方法、把 `apply_period_profile` 也标为 no-op 或加 TODO，避免后人以为是生效逻辑。

### P3-3 `Trading/Infra/InstrumentSpec.py:17` 残留未使用 import
`from typing import Any, Dict, List` —— `List` 随 `sessions` 删除后已无使用点。

### P3-4 Docs 未同步（即上轮 P3-2，仍未处理）
`Docs/Step1B_…md:46,137`、`Docs/Step2_0_…md:150` 仍把 `signal_max_age_minutes` 当现行调参项；本轮该字段退役后，建议至少在文首加一行"2026-09-08 起该字段已改为 `signal_k_tol_bars`（非周期敏感）"。

---

## 五、附带发现（与本次改动无关，但建议查）

`Trading/replay_data/klines.json` 中有 **16/144 根 K 线的 high/low 不是 OHLC 极值**（如 `2026-09-01 13:00  o=4561.0 h=4560.0 l=4558.0 c=4558.5`，开盘价高于最高价）。chan.py 的 `KLine_Unit.check()` 会直接抛 `KL_DATA_INVALID`。数据来自 `Trading/Tool/GenReplayData.py` 或上游取数，回放路径不校验所以没暴露，但会阻碍任何"用真实数据直接驱动 chan.py 核心"的验证。

---

## 六、已验证无问题的部分

- **28/28 测试全部通过**（`Trading/Test/test_*.py` 逐个实跑，0 失败）
- `python -m compileall Trading` 无错误
- `main.py --source replay` 冒烟输出与基线完全一致（`[gw] 周期档案 freq=5m bar_secs=300s…`，exit 0）
- **代码树交叉校验一致**：用 GitHub API 取 HEAD 的 11 个文件拼出的代码树，与 60MB 全量包解压结果 `diff -rq` 完全一致 → 确认本次只改了这 11 个文件，没有遗漏
- 残留引用清零：`sessions` / `in_session` / `risk_block` / `day_roll` / `signal_max_age_minutes`（代码层）在 `Trading/`、`App/` 均无残留
- SSE 旧代码清理干净：`_parse_ts` / `_ts_formats` / `import datetime as _dt` 已随新逻辑一并移除，无悬空引用
- `bar_secs_for(freq, default=None)` 签名与 SSE 调用方式匹配，无 TypeError 风险

---

## 七、给对方确认的问题（建议逐条拍板）

1. **R1**：`signal_k_tol_bars` 默认 1 是否用真实 SSE 流验证过？能否确认 chan.py 的 bsp 从"所属 K"到"出现在 payload"的滞后 ≤1 根？如果没验证过，建议先加埋点跑一段真实流再定默认值。
2. **R1 附带**：被过滤的信号是否接受"完全静默"？建议加 `signal_stale` 事件或计数。
3. **P2-1**：`RiskConfig` 是否加 `validate_assignment=True`？若加，`test_p15a` 的 25 手场景要一并改成 ≤20。
4. **P2-2**：新过滤分支是否补一个不联网的 payload 级单测？
5. **P3-2 / P3-4**：Docs 里已退役的 `signal_max_age_minutes` 描述是否加一行时效标注？

---

*本报告基于 `sandbox/latest`（上轮评审快照 `8ca03aed`）与 `sandbox/latest3`（HEAD `3aff491`）的逐文件 diff，全部证据可通过上述路径与行号复现。*
