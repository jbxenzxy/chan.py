# chan.py `custom-dev` 架构评审 · 问题核查结论

> 核查对象：`github.com/jbxenzxy/chan.py` 分支 `custom-dev`
> 实际拉取 HEAD：`dd87140`（2026-08-28 22:30:11）
> 评审报告：`C:/my_chan_project/Docs/chan_architecture_review.md`（报告自称基准 commit 同为 `dd87140`）
> 核查方式：逐条 gre/读取源码，以 file:line 为据，不采信注释
> 结论：**报告与当前代码 100% 同源（同一 commit）。报告列出的问题绝大多数真实存在，5 个 P0 全部成立。报告唯一的小偏差见 §9。**

---

## 0. 总判定表

| 编号 | 报告声称的问题 | 核查结果 | 证据 |
|---|---|---|---|
| P0-1 | SSE 期货 K 线缓存是类级全局，多连接互相踩 | **成立** | `TqSdkAPI.py:208` `_records_by_symbol = {}` 类属性；`CTqSdkCSSESource.py:316` `CTqSdkAPI._records_by_symbol.pop(...)` 类级操作 |
| P0-2 | 期货双窗区间套 key 写读不一致，100% 静默失效 | **成立** | 写 `make_futures_sub_key(symbol,sub_freq)`=`符号:子周期`；读 `f"{parent.code.upper()}:{sub_freq}"` 且 `parent.code=f"{symbol}:{主周期秒}"` → 多一级 `:60`，永不等 |
| P0-3 | 盘后下载不失效股票分析缓存 + 无清缓存入口 | **成立（股票侧）** | `ElTdxAPI.py:585-589` finally 只重置 `_download_state`；`AppData` 无 `invalidate_all`；`/api/stocks/scan/close`→`clear_cache` 空实现 `{"cleared":0}` |
| P0-4 | FrontAPI 回退分支 `m.OUTPUT_DIR` 启动即崩 | **成立** | `FrontAPI.py:652` `m.OUTPUT_DIR`；`m=AppEngine`(`:57`)，而 `AppEngine.py` 全文件无 `OUTPUT_DIR`/`output_dir` |
| P0-5 | SSE 把全局 root 日志级别永久改 WARNING 且不恢复 | **成立** | `AppSSE.py:251-254` 循环 `h.setLevel(logging.WARNING)` |
| P1-1 | 四把锁分散，无统一登记；AppAMO 为拿锁依赖 AppChart | **成立** | `_ENGINE_LOCK`(AppChart:43) `_scan_lock`(AppScan:53) `_stock_analysis_lock`(AppEngine:294) `_cache_lock`(AppData:1230)；`AppAMO.py:40` `from App.AppChart import _ENGINE_LOCK` |
| P1-2 | LOCK_POLICY 只登记不执行，含 2 个零调用项 | **成立** | `LOCK_POLICY`(AppOrch:98) 登记 `run_analysis`/`fetch_and_inject`；二者定义于 AppChart:67/492 且零调用 |
| P1-3 | SELF_CONTAINED 分类错误；REPLAY_MODE 被两类路径无锁互踩 | **成立** | SSE 路径(`:317/716/738`) 与股票复盘(`:671/683`) 均读写 `CMyBSPointList.REPLAY_MODE`，无统一锁 |
| P1-4 | 第三方向内核 `from App.AppData import app_data` 反向依赖 | **成立** | `BSPointList.py:698` 与 `:725` 直接 `from App.AppData import app_data` |
| P1-5 | `Scanner.abort()` 无任何调用方 | **成立** | 定义 `AppScan.py:660`；全仓仅 `abort_check=lambda:_scan_aborted`(`:129`) 引用，`_scan_aborted` 永不被置 True → 中断无效 |
| P2（抽验） | `run_analysis`/`fetch_and_inject` 死代码 | **成立** | 同上 P1-2 证据 |
| P2（抽验） | `stock_manual_select_point` 绕过 `analyze_stock` 直调 `_analyze_stock_internal` | **成立** | `AppChart.py:341` `result = _m._analyze_stock_internal(...)` |
| §4 #4 | 双窗选点缓存失效靠手工删 key，脆弱 | **成立（逻辑成立）** | 全仓无统一清股票缓存入口（见 P0-3） |

**一句话**：报告可信，问题真实存在。这是一份质量很高的评审——核心论断（5 个 P0、架构耦合、注释误导）全部经得起源码复核。

---

## 1. P0-1 · 期货 SSE 记录缓存是类级全局（成立）

`DataAPI/TqSdkAPI.py:208`：
```python
_records_by_symbol = {}          # 类属性，所有 CTqSdkSession 实例共享
_lock = threading.Lock()
```
所有读写都是 `@classmethod` 操作 `cls._records_by_symbol`（`set_records`/`append_bar`/`get_records`/`last_records`/`clear`）。`CTqSdkCSSESource.py:316` 在一条连接 `finally` 里 `CTqSdkAPI._records_by_symbol.pop(code_key, None)`——直接 pop 掉公共 key。

报告描述的三个后果（写覆盖、重复追加、另一连接 `last_records()` 突然返回 `[]`）在逻辑上成立：多连接同 `symbol+freq` 共享同一 dict，且清理是"整 key 删除"而非引用计数。

修法（与报告一致，低风险）：把 `_records_by_symbol` 改为 `CTqSdkSession` 实例字段，方法改实例方法，`CTqSdkCSSESource` 仅作兼容转发。

---

## 2. P0-2 · 期货双窗区间套 100% 静默失效（成立，已逐字核对 key）

**写入方** `App/AppSSE.py:728`：
```python
app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)
# → AppData.set_futures_sub_chan → futures_cache_put(make_futures_sub_key(symbol, sub_freq), chan)
# make_futures_sub_key = f"{symbol.upper()}:{sub_freq}"   (AppData.py:253-255)
# 例：KQ.M@CFFEX.IM:1m
```

**读取方** `BuySellPoint/BSPointList.py:725-727`：
```python
from App.AppData import app_data
cache_key = f"{parent.code.upper()}:{sub_freq}"
sub_chan = app_data.futures_cache_get(cache_key)
```

而 `parent` 是主窗 CChan，其 `code` 在 `AppSSE.py:1118` 构造：
```python
chan_code = code or f"{symbol}:{freq_sec}"     # 例：KQ.M@CFFEX.IM:60
chan = CChan(code=chan_code, ...)
```

→ 读取 key = `KQ.M@CFFEX.IM:60:1m`，写入 key = `KQ.M@CFFEX.IM:1m`。**多出一级主窗周期秒数 `:60`，永远不等。**

`BSPointList.py:728-731` 命中 `sub_chan is None` 后直接 `return True`（按子级别背驰处理），并打一句调试日志。即：期货双窗的区间套背驰判定从未生效，用户看到的买卖点比设计少一类——与报告完全一致。

> 报告指出的"注释误导"也成立：`AppData.py:209-211` 仍写着"第三方引擎（BSPointList）按同一格式拼接，兼容不变"，而实际读取方自己拼了 `parent.code`（含 `:freq_sec`），并非同一格式。修法：改 `_build_futures_chan` 使 `code=symbol`（去掉 `:freq_sec` 后缀），或让 `BSPointList` 改调 `app_data.get_futures_sub_chan(symbol, sub_freq)` 并补断言测试。

---

## 3. P0-3 · 盘后下载不失效缓存（成立，但"无任何清缓存入口"措辞需修正）

- `DataAPI/ElTdxAPI.py:585-589`：下载 `finally` 仅重置 `_download_state`（`running=False/progress=100/end_time=...`），**不触碰 `AppData._stocks_analysis_cache`**。grep `cache|invalidate` 在 ElTdxAPI 全文件零命中。
- `AppData` 中**没有 `invalidate_all()` 方法**（grep `invalidate_all` 零命中）。
- 清缓存入口现状：
  - `POST /api/stocks/scan/close`（`FrontAPI.py:359`）→ `orch.scanner.clear_cache` → `AppScan.py:679` 空实现 `return {"cleared": 0}`。
  - ⚠ **报告偏差**：报告称"全项目没有任何清缓存入口"，但当前代码**已存在 `POST /api/futures/cleanup`**（`FrontAPI.py:501`→`orch.futures_cleanup`→`app_data.futures_cache_clear()`），可清**期货**分析缓存。该端点有效，只是**不覆盖股票分析缓存、也不在下载后自动触发**。
  - 股票分析缓存（`_stocks_analysis_cache`，LRU 50，每条含完整 records + CChan 对象）**确实无任何清缓存/失效入口**，下载完成后用户看到的仍是旧数据。

→ 核心论断成立；报告唯一的误差是把"无股票清缓存入口"说成了"全项目无清缓存入口"。严格表述应为：**下载不失效 + 股票分析缓存无清缓存入口**。

---

## 4. P0-4 · FrontAPI 回退分支 `m.OUTPUT_DIR` 启动即崩（成立）

`FrontAPI.py:57` `from App import AppEngine as m`，`:652`：
```python
app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")
```
而 `AppEngine.py` 全文 grep `OUTPUT_DIR|output_dir` **零命中**。`AppConfig` 里有 `output_dir`（property），但此处 `m` 是 `AppEngine` 而非 `AppConfig`。一旦 `Frontend/` 目录缺失，服务启动即 `AttributeError`，并非"降级"。与报告一致。

---

## 5. P0-5 · SSE 永久压制全局日志（成立）

`AppSSE.py:251-254`：
```python
logging.getLogger("tqsdk").setLevel(logging.WARNING)
logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
for h in logging.root.handlers:
    h.setLevel(logging.WARNING)     # root handler 全局级别，从不恢复
```
`TqSdkAPI.py` 顶层注释明确要求"绝不设置 root 级别"——此处换了一种形式（改 handler 而非 logger）达成同样效果：只要建立过一次期货 SSE 连接，整个进程 `log.info`（扫描/刷新/分析进度）全部消失且不可恢复。与报告一致。

---

## 6. P1 架构一致性（全部成立）

- **P1-1 四把锁分散**：`_ENGINE_LOCK`(AppChart:43) `_scan_lock`(AppScan:53) `_stock_analysis_lock`(AppEngine:294) `_cache_lock`(AppData:1230)，无统一登记处；`AppAMO.py:40` 为拿 `_ENGINE_LOCK` 而 `from App.AppChart import _ENGINE_LOCK`，只读两个指数文件的模块因此依赖整个图表域。
- **P1-2 LOCK_POLICY 只登记不执行**：`AppOrch.py:98` 登记表仍含 `run_analysis`(SERIAL)、`fetch_and_inject`(RAW) 两项零调用符号；守护测试 `test_phase3_guards` 只做文本匹配，不验证"SERIAL 与 SCAN 是否互斥"等行为。
- **P1-3 SELF_CONTAINED 误分类 + REPLAY_MODE 互踩**：SSE 路径(`:317/716/738`) 与股票复盘(`:671/683`) 均读写 `CMyBSPointList.REPLAY_MODE` 第三方类级旗，无统一锁。
- **P1-4 反向依赖**：`BSPointList.py:698/725` `from App.AppData import app_data`（第三方向内核直连应用层）。
- **P1-5 `Scanner.abort()` 死代码**：定义 `AppScan.py:660`，全仓无调用方；`_scan_aborted` 仅由 `abort()`/`start()` 设置 → 恒为 `False` → `abort_check`(`:129`) 恒返回 False，成分股抓取期间中断无效；`ScanStore.abort_all_running()` 连带死代码。

---

## 7. P2 死代码/注释冲突（抽验均成立）

- `run_analysis`(AppChart:67)、`fetch_and_inject`(AppChart:492)：定义且仍注册于 `LOCK_POLICY`，**零调用**（grep `run_analysis(`/`fetch_and_inject(` 仅命中 `def` 行）。
- `stock_manual_select_point`(AppChart:341) 直调 `_m._analyze_stock_internal(...)`，绕过"统一入口"契约（报告 §4 #4 / P1-4）。
- 注释误导典型案例（报告 §5 附：注释冲突清单）已直接验证：`AppData.py:209` 与 `P0-2` 同因——注释声称 BSPointList 按同一格式拼接，实际 key 不一致。

---

## 8. 报告可信度评估

- **优点**：分层意图、依赖图方向、全局可变状态清单、端到端功能核对、5 个 P0 的 file:line 定位**全部经得起源码复核**。这是一份高水位评审，不是"印象流"。
- **唯一偏差（非问题实质）**：P0-3 中"全项目没有任何清缓存入口"——已存在 `POST /api/futures/cleanup`（仅期货）。但股票分析缓存无清缓存入口、下载不失效这两个实质论断完全成立。
- **整体**：报告结论"这是一次物理拆分而非逻辑分层重构，缺状态所有权/可执行架构约束/有效缓存失效"——核查后完全站得住。

---

## 9. 建议优先级（与报告一致，按性价比）

1. **止血（1 天内）**：删 `AppSSE.py:251-254` 三行 root 级别改动；`FrontAPI.py:652` 改 `app_config.output_dir` 或 fail-fast；统一期货下窗 key（改 `_build_futures_chan` 的 `code=symbol` 并补断言）；`CTqSdkAPI._records_by_symbol` 改实例字段。
2. **接线（2 天内）**：下载 `finally` → `app_data.invalidate_all()`（需先实现该方法）+ 新增 `POST /api/stocks/cleanup`；`Scanner.abort()` 接路由或整条删除。
3. **结构收敛**：抽 `App/concurrency.py` 集中四把锁；删死代码（约 400 行）+ 删 `step` 全链路；内核外包 `ChanFacade` 打断 `BSPointList→AppData` 反向依赖。

---

## 附：核查命令产出要点（可复现）

- HEAD：`git log -1` → `dd87140` 2026-08-28 22:30:11
- 全部结论通过 `Grep` 在 `DataAPI/`、`App/`、`BuySellPoint/`、`FrontAPI.py` 定位 file:line 得到，未运行被测程序（无 TDX/天勤环境）。
