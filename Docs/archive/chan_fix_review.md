# chan.py `custom-dev` 整改复查报告

> ⚠️ **功能状态提示（2026-08 更新）**：本档复查的「盘后下载 / ElTdxAPI」相关项已随功能移除（`ElTdxAPI.py`、`AppDownload.py` 已删），文中相关结论为历史快照，仅作追溯。

> 基线：`dd87140`（2026-08-28）→ 复查对象：`1a0aba5`（2026-08-29）
> 方式：`git diff dd87140 HEAD` 逐文件核对 + AST 死代码复扫 + 可独立验证项写 harness 实测
> 环境限制：本机无 pandas/numpy/fastapi/tqsdk，无法跑 `run_all` 全量；能跑的组件都跑了

---

## 0. 总览

| 类别 | 数量 | 说明 |
|---|---:|---|
| 修对了 | 12 | 其中 4 项我做了独立机制验证，结果全过 |
| 修了但引入新问题 / 有代价 | 4 | 见 §3 |
| 未修 | 8 | 见 §4 |

生产代码：`App` 9768→9623、`DataAPI` 5399→5335、`Frontend` 8759→8408 行。
真死符号（AST 复扫）：**约 400 行 → 6 行**。

---

## 1. 修对了的（12 项）

### P0-1 · SSE 期货记录缓存类级全局 ✅ 机制正确

`CTqSdkAPI._records_by_symbol` / `_lock` 从类属性改为实例字段，经线程局部 `_CURRENT_SESSION` 在 `CTqSdkAPI.__init__` 绑定当前 `CTqSdkSession` 的缓存；`session_context` / `session_set` / `session_clear` 三个入口覆盖生成器生命周期。

我写了一个独立 harness 实测（避开 tqsdk 依赖，用 FakeSession 替身），5 项全过：

| 验证项 | 结果 |
|---|---|
| 无会话时两实例不共享缓存 | ✅ |
| 同会话内多实例共享同一 dict、写入互相可见 | ✅ |
| 跨会话隔离 | ✅ |
| `session_set` / `session_clear` 绑定与解绑 | ✅ |
| 同会话共享同一把锁（不会自锁死） | ✅ |

配套改动也到位：`_build_futures_chan` 的 `src is None` 回退分支改为显式 `raise`（不再有"半静默"路径）；`CTqSdkSession` 的 `set_data/append_bar/get_data/get_last_n/clear_all_cache/cleanup_records` 全部改为操作自身实例字段。

### P0-2 · 期货双窗区间套 key 不一致 ✅ 修复正确

新增 `make_futures_sub_key_from_code(chan_code, sub_freq)`，`rsplit(":", 1)[0]` 去掉周期后缀后委托 `make_futures_sub_key`，与写侧同源。
实测：`KQ.M@CFFEX.IM:60` + `15s` → `KQ.M@CFFEX.IM:15s`，与 `set_futures_sub_chan` 写入 key 一致。新增 `Test/test_futures_sub_key.py` 我跑过，4 组断言全过（含"老拼接必然不等"的反向守护）。

### P0-4 · `m.OUTPUT_DIR` 启动即崩 ✅
改为 fail fast 抛 `RuntimeError`。小建议：`raise` 前加一句 `log.error(...)`，否则 import 期异常在 uvicorn 下输出较乱。

### P0-5 · SSE 永久压制全局日志 ✅
三行 root handler 改动删除。顺带把 `AppLog.trace_id` / `current_trace_id` 一并删掉（前者只在 SSE 用过，后者本就无人调用，残留已清零）。
**代价**：SSE 日志不再带连接标识，同 symbol 的两个连接日志无法区分。可接受，但要知情。

### P1-2 · LOCK_POLICY 可执行化 ✅ 做得好
`engine_section(entry)` 按 `LOCK_POLICY[entry]` 取锁，未登记入口 `KeyError` fail fast；4 个 `call_*` + `call_amo` 全部改走它。守护测试反向断言"残留手写 `with _ENGINE_LOCK:`"→ 现在会 FAIL。
**局限**：仍然只映射 `SERIAL → _ENGINE_LOCK`，其余分类不加锁，与改前行为等价。真正的互斥保护仍在 `AppEngine._stock_analysis_lock`。别把它当成"锁治理已解决"。

### P1-5 · 中断链路 ✅ 删干净
`_scan_aborted` / `Scanner.abort` / `ScanStore.abort_all_running` / `abort_check` 传递全清，并加了守护测试断言零残留。选择"删"而不是"接路由"是合理的（前端只走 task cancel）。
**残留**：`TdxAPI` 里 `abort_check` 参数链还在 6 个函数上，已无人传，成僵尸参数。

### P1-7 · 前端扫描四分支合一 ✅ 高质量
`doStartScan` 717 → 404 行，抽出 `runScan(spec)`，差异收敛为 `mode / classify / normalize / initialSummary / progressLine / renderRows / renderFinal`。

我逐条比对了四个模式与旧版的行为等价性：

| 模式 | 分类谓词 | 进度串 | 市场汇总 | 排序 |
|---|---|---|---|---|
| fx_d | `!!data.is_fx_d` | 一致 | 一致 | 一致 |
| ma | `ma_category !== undefined && >= 0` | 一致 | 改为类目计数（与旧版一致） | 一致 |
| fangliang | `!!data.is_fangliang` | 一致 | 一致 | 一致 |
| bsp | buy/sell 非空 | 一致 | 一致 | 一致 |

顺带清掉 4 个从未被读取的 `hasRenderedAny`。**行为等价，无回归。**

### P2 · 其余清理 ✅
- 死代码：`run_analysis`、`fetch_and_inject`、`get_saved_point_times`、`get_saved_point`、`futures_cache_*`、`futures_*_sub_chan`、`get_stock_market_code/_market_code/_stock_name`、`start_download`、`eltdx_available`、`ths_cloud_available`、`CTdxAPI_Sliced`、`read_zz1000/sz50/hs300/zz500_stocks`、`DEFAULT_FUTURES_SYMBOLS`、`FUTURES_DUAL_*_MAP`、`HK_/DS_CODE_PREFIX`、`TDX_MARKET_MAP`、`_get_float_mc_from_cache`、`_update_float_mc_cache`、`_save_point_time`、`_cache_remove` —— 全部清除，无残留引用。
- `func_map_check.py`：行号漂移 + 函数总数比对下线；`SCRIPT_DIR` / `SYMBOL_CODE` 由 RETIRE 更正为活状态登记。
- `test_app_amo.py` / `test_stocks_dual_algo.py` 注册进 `run_all`（现 23 个组件）。
- `test_phase6_guards`：删掉守护死注册表 `ChanApp.components` 的 ⑦ 项。
- `.env.example`：补全 11 个未登记配置项 + `App/ths_*.py` → `Script/ths_*.py` 路径修正。
- 前端 `VIEW_COUNT` 377 → 233，与后端 `app_config.view_count` 对齐。
- `AppConfig._env_lookback_dict`：补齐 `STOCKS_/FUTURES_LOOKBACK_CONFIG` 的降级解析器支持（我实测解析正确）。

---

## 2. 我上一轮的一处措辞错误（你纠正得对）

P0-3 我写成"全项目没有任何清缓存入口"，实际 `/api/futures/cleanup` 是有效端点（清期货缓存）。准确表述应为：**下载不失效 + 股票分析缓存无清缓存入口**。已在你的 `Docs/chan_review_verification.md` 中指出，我确认这个纠正成立。

---

## 3. 修了但引入新问题 / 有代价的（4 项）

### 3.1 ❗ `test_phase3_guards.py` 与 FrontAPI 现在互相矛盾

从 `EXPECTED_ROUTES` 删掉了：
```
("GET", "/api/futures/read/status")
("GET", "/api/futures/read/config")
```
但**这两个路由在 FrontAPI 里还在**（`FrontAPI.py:518` 和 `:524`），`/api/futures/read/status` 还是那个硬编码桩 `{"ok": True, "architecture": "self-contained"}`。

之所以测试没炸，是因为 `baseline = frozen or EXPECTED_ROUTES`，冻结快照 `Test/snapshots/phase3_routes.json` 仍是 30 条（含这两条）且优先生效。

**后果**：仓库里有两份互相矛盾的基线（快照 30 条 vs EXPECTED 27 条）。一旦快照丢失、或有人改了基线加载顺序，立刻 FAIL。而且"我删了两个端点"这个意图在代码里并没有兑现。

**建议**：要么真的删掉这两个路由（并把快照一起 `--update`），要么把两行从 EXPECTED_ROUTES 加回去。别只改一半。

### 3.2 ❗ 新增的 P0-2 回归测试没注册进 `run_all`

`Test/test_futures_sub_key.py` 写得很扎实，但 `run_all.py` 里没有它 —— 上次我指出 `test_app_amo` / `test_stocks_dual_algo` 没注册，这次这两个补上了，**新写的那个又漏了**。

建议：`run_all.py` 的 `COMPONENTS` 加一条，或者干脆加个守护"Test/ 下所有 `test_*.py` 必须出现在 COMPONENTS 里"，从机制上杜绝。

### 3.3 ⚠ `Scanner.clear_cache` 改成真清，但挂载点错位

```python
def clear_cache(self):
    from App.AppData import app_data
    cleared = app_data.stocks_cache_clear()   # 清空整个 50 条 LRU
```

它挂在 `POST /api/stocks/scan/close`（关闭扫描面板）上。语义上"关面板" ≠ "清缓存"：用户正在看的图表的分析结果也一起没了，下次任何操作都要全量重建（股票 472 根日线的 CChan 重建是秒级的）。

而且这**不能替代 P0-3 的修复**——下载完成后用户不会去关扫描面板。

建议：`stocks_cache_clear` 保留，但触发点改到下载完成回调；`/api/stocks/scan/close` 恢复成只清扫描产生的条目（按 kind 过滤），或干脆让它继续做 no-op。

### 3.4 ⚠ `CTqSdkAPI` 四个方法从 classmethod 改为实例方法（破坏性变更）

`set_data` / `append_bar` / `get_data` / `get_last_n` 全部改为实例方法。我查过所有调用点，无残留（AppSSE 的 `src is None` 回退已改 raise）。

但 `CTqSdkAPI.set_data(records, ...)` 这种历史调用方式现在会把 `self` 绑到 `records`（一个 list）上，报的是 `AttributeError: 'list' object has no attribute '_lock'`，很误导。如果项目外有脚本/工具这么调，会静默崩。建议在提交说明里写明这是 breaking change。

---

## 4. 未修的（8 项）

| 项 | 现状 | 备注 |
|---|---|---|
| **P0-3 下载不失效缓存** | `ElTdxAPI.py` 零改动，下载 `finally` 只重置 `_download_state` | 本次唯一未动的 P0。§3.3 的 `stocks_cache_clear` 是半个替代，触发点不对 |
| P1-1 四把锁分散 | 未抽 `App/concurrency.py`；`AppAMO` 改成 import `engine_section`，但**耦合方向没变**（仍是 AppAMO → AppChart） | |
| P1-3 `REPLAY_MODE` 类级旗互踩 | 未处理。SSE 路径（无锁）与股票复盘路径（`_stock_analysis_lock` 内）仍同时读写 | |
| P1-4 内核反向依赖 | 未处理；P0-2 的修复反而让 `BSPointList` 多 import 了一个 AppData 工厂 | 建议仍做 `ChanFacade` |
| P2 `step` 全链路 | `FrontAPI:233` / `AppChart:84,102` / `AppEngine:306,1621` / `AppSSE:207` 都在，测试仍冻着它的快照 | 约 40 行 + 2 个测试的维护成本 |
| P2 两个未调用的 futures 端点 | 路由还在（见 §3.1） | |
| P2 `.gitignore` / 已跟踪 JSON | 4 个 JSON 仍被 git 跟踪，`App/stock_float_mc.json` 本次又产生 diff | 本次被迫重冻快照：`pe_ttm` 19.99 → 20.0。**根因不解决，下次刷新还会再漂一次** |
| P2 Docs 清理 | 未清理，反而从 21,408 → **49,332** 行（3 个 HTML 移入 archive + 2 个 md）；`Test/*.html` 两份重复仍在 | |

另外两处小的不一致（新产生的或遗留的）：
- `AppChart.py` 文件头仍写着"选点 / 上次查看 / 期货子窗缓存漏斗"，但这些漏斗本次已删。
- `AppEngine._load_float_mc_cache` 现在是死壳（零调用），但 `test_phase4_guards.py` 的 `SHELL_FUNCS` 还在保护它。
- `AppSSE.py:752` `main_chan, main_records, main_kl_type, _ = main_result` —— 第二项实际是 klines DataFrame（`sub_result` 那处已改名）。

---

## 5. 一个重要的覆盖盲区：P0-1 没有任何回归测试

这是本次我最担心的点。`Test/test_sse_gray.py` 和 `Test/test_sse_concurrent.py` 的 `MockSource` **都不实现 `set_data`**，而 `_patch_business` 又直接把 `init_chan_symbol` 整个桩掉 —— 也就是说这两个 SSE 测试**根本不经过 `_build_futures_chan` / 记录缓存这条路径**。

结论：P0-1 那套"线程局部绑定"机制，改前改后测试都全绿，**没有任何东西能发现它被改坏**。

建议补一条不依赖 tqsdk 的测试：用 FakeSession（有 `_records_by_symbol` / `_lock`）套 `session_context`，断言 `CTqSdkAPI(...)._records_by_symbol is session._records_by_symbol`，以及两个不同 session 互不共享。我这次就是用这个 harness 验的，可以直接固化成用例。

顺带：`test_sse_concurrent.py`（8 连接并发隔离）本该是 P0-1 最自然的回归场，但它用 MockSource + 业务桩，验的是"事件序列"而不是"缓存隔离"。建议让它的 MockSource 也实现 records 缓存协议。

---

## 6. 下一步建议（按性价比）

1. **补 P0-3**：下载 `finally` → `app_data.stocks_cache_clear()`；新增 `POST /api/stocks/cleanup`。同时把 `/api/stocks/scan/close` 的清缓存语义撤掉或收窄。
2. **收口 §3.1**：二选一——真删两个 futures 端点（含快照 `--update`），或把 EXPECTED_ROUTES 加回去。
3. **注册 `test_futures_sub_key.py`** 进 `run_all`，并加一条"Test/ 下 test_*.py 必须登记"的守护。
4. **补 P0-1 回归用例**（§5 的 harness）。
5. 清掉 `TdxAPI` 的僵尸 `abort_check` 参数链、`_load_float_mc_cache`、`get_tdx_hy_mapping`（合计约 30 行）。
6. `git rm --cached App/*.json` —— 这条能同时治好"每次刷新都产生大 diff"和"快照被迫重冻"两个症状。

第 1～4 项合计约半天。
