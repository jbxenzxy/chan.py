# chan.py 互斥锁设计（v5）

> 分支 `custom-dev` @ `b4d3cd4` · 2026-08-30
>
> 本文独立成文，不依赖也不修订 `chan_lock_redesign.md` /
> `chan_lock_redesign_selfcontained.md`（那两份是早期讨论稿，本文的结论与
> 改造已落地到代码，以本文 + `App/AppOrch.py` 的
> `SHARED_RESOURCE_REGISTRY` 为准）。
>
> **一句话结论**：锁乱的根源不是"用多了"，是**登记表按「入口」索引而非按
> 「资源」索引**，导致锁与资源对不上号。本文先按「执行体 → 共享资源 →
> 锁」重新梳理，再消除共享根因，最终生产路径只剩 4 把分工明确的进程内锁。

---

## 一、分析方法：对每个数据问三句

不要从"这里要不要加锁"开始想，要从"这个数据的作用域"开始想。

| 步骤 | 问题 | 作用 |
|------|------|------|
| ① | 被哪种**执行体**承载？ | 决定它与谁并发 |
| ② | 在哪个**作用域**共享？ | 决定该用哪一层锁 |
| ③ | 存在跨执行体的**写-写**或**读-改-写**吗？ | 决定要不要锁 |

第 ③ 条最容易被忽略：**纯只读共享不需要锁**。`app_config`、TDX vipdoc
`.day` 文件、`FREQ_SEC_MAP` 都是全进程共享但零写者，加锁纯属浪费。

### 1.1 四类执行体

| 执行体 | 承载的请求 | 并发边界 |
|--------|-----------|---------|
| **事件循环线程** | `async def` 且内部无阻塞的路由（`/api/health`、`/api/futures/read/status`）、中间件、异常处理器 | 单线程，内部零并发 |
| **线程池** | 绝大多数 REST（`run_in_threadpool`，Starlette 默认 40 线程） | **彼此共享整个进程堆** |
| **进程池 worker** | 批量扫描 `/api/stocks/scan/submit` → `_worker_scan_one`（spawn，1~16） | 进程隔离，内存不共享 |
| **SSE 常驻线程** | `/api/futures/read/stream`（同步生成器，每连接 1 条线程） | **与线程池是同进程兄弟线程** |

第四类最容易被漏掉，也最容易出事。原代码里的 `SELF_CONTAINED` 分类名有
误导性——它只说明 `TqApi` / `CChan` / 记录缓存是每连接独立的，
**`app_data` 单例仍然是全进程共享的**。期货下窗 CChan 就是经
`app_data.set_futures_sub_chan` 从 SSE 线程写进去、被 REST 线程读走的。

### 1.2 锁的三层作用域（不可混用）

```
进程内堆对象  → threading.Lock / RLock
跨进程        → OS 层：SQLite WAL / fcntl / multiprocessing.Lock
事件循环内    → asyncio.Lock，且临界区不得跨 await
```

最危险的是第二种误用成第一种。**`threading.Lock` 跨进程无效，但看起来
有效**——单进程测试全绿，上线后多进程一跑就失效。项目里 `scan_tasks.db`
选对了（WAL + `busy_timeout`），但 `AppScanStore` 的
`_init_lock` / `_scan_store_lock` 都是 `threading.Lock`，它们只护
"本进程内的单例创建"，**不护数据库**。这类注释必须写死，否则后人会误解。

### 1.3 消除共享优先于加锁

锁是承认设计失败后的补丁。能靠 per-request 隔离解决的，不要靠串行解决。
本轮最大的一笔收益就来自这一步（见 §3.1）。

---

## 二、改造前的锁全景与问题

| 锁 | 位置 | 名义保护对象 | 实际问题 |
|----|------|-------------|---------|
| `_ENGINE_LOCK` + `engine_section` | `AppChart.py` | "引擎串行" | **套娃壳**：与下面的根因锁护同一段代码，净贡献 0 |
| `_stock_analysis_lock` | `AppEngine.py:258`（临界区 `:625-733`） | `CTdxAPI._tdx_data` | **真·根因锁**，但只有它一个在干活 |
| `_cache_lock` (RLock) | `AppData.py` | 分析结果缓存 | 有效，但覆盖不全（下窗缓存、期货缓存不在内） |
| `_annotations_lock` (RLock) | `AppData.py` | 标注 JSON | 有效，可与选点合并 |
| `_saved_point_lock` (RLock) | `AppData.py` | 选点 CSV | 有效，可与标注合并 |
| `_scan_lock` | `AppScan.py:53` | "扫描串行" | **死代码**：API 进程无路由调 `scan_one`；worker 内单线程不竞争 |
| `_pool_lock` | `AppScanPool.py` | 进程池单例 + 引用计数 | 有效，保留 |
| `_scan_store_lock` | `AppScanStore.py` | 本进程单例 | 有效，保留（不跨进程） |
| `_xdxr_lock` / `_ACTIVE_SOURCES_LOCK` 等 | `DataAPI/*` | 数据源内部 | 正交，不动 |

### 2.1 具体缺口（逐条核实，带行号）

**① 期货下窗缓存：三个入口三套规矩**

| 操作 | 位置 | 执行体 | 同步手段 |
|------|------|--------|---------|
| 写 `set_futures_sub_chan` | `AppSSE.py:734` | SSE 线程 | **无锁** |
| 删 `pop_futures_sub_chan` | `AppSSE.py:1107` | SSE 线程 | **无锁** |
| 读 `futures_cache_get` | `AppChart.py:398` | REST 线程 | 靠外层 `_ENGINE_LOCK` 兜 |
| 清空 `futures_cache_clear` | `AppSSE.py:1752` | REST 线程 | 持 `_cache_lock` |

四个访问点、三种约定。CPython 下单次 `get/set/pop` 是原子的所以不崩，
但 `clear()` 与 SSE 的 `put` 会互相穿插 → 清不干净；且
`/api/futures/cleanup` 会清掉**正在推送**的连接的下窗 CChan。
更糟的是 `_cache_lock` 只护了 `clear`、不护 `get/put`，是自欺欺人的一致性。

**② 股票下窗缓存 `_stocks_sub_chan_cache`：完全无锁**

`AppData.py` 的 `stocks_sub_cache_get/put/pop` 三个方法一把锁都没有。
写在 `_stock_analysis_lock` 内（`AppEngine.py:665`），读/删在
`_ENGINE_LOCK` 内（`AppChart.py:276/348/428`）——靠**两把不同的**外层锁
"恰好"覆盖。这是隐式契约：任何人在新路径上调一次而没持外层锁，就静默出错。

**③ 双层套娃**

`call_analysis` → `_ENGINE_LOCK` → `analyze_stock` → `_stock_analysis_lock`。
两层护同一段代码；外层只是把"取哪把锁"的决策搬到了一张按入口索引的表里。

**④ `_scan_lock` 是死代码**

API 进程没有任何路由调 `scan_one`（`FrontAPI.py` 只有 submit/status/cancel
三个批量入口）；worker 进程内每个 worker 串行取任务、且每进程一份
`_scan_lock`，永不竞争。它带着大段注释解释"并发价值"，是最大误导源。

**⑤ 文件裸写无锁**

`last_code_freq.json`（`AppData.py`）、`zxg.blk`、`float_mc_cache.json`
都是 `open(w)` 非原子写且无锁。`last_code_freq` 还在任何分析锁之外
（`FrontAPI.py:256` 单独一次 `run_in_threadpool`），并发 analyze 会互相
截断成半截 JSON。对照：`stock_names.json` / `stock_pettm_index.json`
用了 `safe_write_json_file`（tmp + 原子替换）就没事。

**⑥ `_refresh_status` 的 check-then-act**（`AppRefresh.py`）

`if running` 到 `t.start()` 之间没有屏障 → 两个并发 POST 能同时起刷新线程。

**⑦ 惰性加载标志无锁**

`_names_loaded` / `_pe_loaded` / `_float_mc_loaded` / `_annotations_loaded`
无锁。幂等重复解析，无害，但说明"哪些算共享可变状态"没被系统识别过。

---

## 三、改造内容

### 3.1 根因消除：`CTdxAPI._tdx_data` 改为每请求线程局部注入

**根因**：`CTdxAPI._tdx_data` 是**类变量**，`set_data()` 是 `classmethod`
写类属性，`CChan(data_src="custom:TdxAPI.CTdxAPI")` 内部实例化
`CTdxAPI` 后从类上读。数据流经一个进程级全局，并发必然互相覆盖——这是
`_stock_analysis_lock` 存在的**唯一**理由。

**改造**（`DataAPI/TdxAPI.py`）：

```python
_CURRENT_TDX_DATA = _threading.local()

@contextmanager
def tdx_data_context(data):
    prev = getattr(_CURRENT_TDX_DATA, "data", None)
    _CURRENT_TDX_DATA.data = data
    try:
        yield
    finally:
        _CURRENT_TDX_DATA.data = prev
```

`CTdxAPI.__init__` 里 `self._tdx_data = getattr(_CURRENT_TDX_DATA, "data", None)`，
类变量与 `set_data` classmethod 一并删除。

**为什么用线程局部而不是传参**：`CChan` 经 `data_src="custom:..."` 自行
实例化数据源，构造参数只有 `code/k_type`，无法携带数据。这与期货/SSE 路径
已稳定运行的 `session_context` 是同一机制——那条路径零锁跑了很久，是现成的
免锁样本。

**关键约束**：`step_load()` 的消费必须在 `with` 内完成。数据源实例是在
`step_load` → `load` → `init_lv_klu_iter` → `get_load_stock_iter` 内部
**惰性创建**的（见 `Chan.py:103`），不是 `CChan(...)` 构造时。

**结果**：`_stock_analysis_lock` 与 `_ENGINE_LOCK` 一起删除。

### 3.2 锁集合重建（按资源命名）

`AppData` 持有三把，锁名即资源名：

| 锁 | 类型 | 护的资源 | 为什么这么切 |
|----|------|---------|-------------|
| `_stocks_cache_lock` | RLock | 股票分析结果 LRU + 股票下窗 CChan 缓存 | REST 线程，毫秒级访问 |
| `_futures_cache_lock` | RLock | 期货下窗 CChan 缓存 | **独立成锁**：访问者是 SSE 常驻线程（高频写、生命周期以分钟计），不该和 REST 的毫秒级缓存操作抢同一把锁 |
| `_user_store_lock` | RLock | 标注 / 选点 / last_code_freq / float_mc / zxg.blk | 都是短耗时读-改-写，合并消除跨锁顺序死锁 |

`AppRefresh._refresh_state_lock`：护 `_refresh_status`，`running` 的检查
与置位必须在锁内完成（CAS）。

`AppScanPool._scan_pool_lock`：护进程池单例 + `_active_scans` 引用计数，保留。

### 3.3 删除清单

| 符号 | 原因 |
|------|------|
| `AppEngine._stock_analysis_lock` | 根因（类变量 `_tdx_data`）已消除 |
| `AppChart._ENGINE_LOCK` / `engine_section` | 套在根因锁外的第二层壳，净贡献 0 |
| `AppScan._scan_lock` | 死代码：API 进程无调用，worker 内不竞争 |
| `AppOrch.LOCK_POLICY` | 主键选错（按入口），改为按资源索引的 `SHARED_RESOURCE_REGISTRY` |
| `CMyBSPointList.REPLAY_MODE` | 进程级可变全局；改为线程局部的 `set_replay_mode` / `in_replay_mode` |

`call_*` 漏斗**保留**：它的价值是"路由层禁止直连引擎"的单一入口约束
（由 `Test/test_phase3_guards.py` 守护），不是加锁。

### 3.4 文件层：统一原子写 + 锁覆盖

- 新增 `_atomic_write_text(path, text, encoding, newline)`：tmp + `os.replace`。
- `last_code_freq` / `float_mc` / `zxg.blk` / 选点 CSV / 标注 JSON 全部纳入
  `_user_store_lock` 且走原子写。
- **Windows 特有问题**：目标文件被另一线程持句柄 `open()` 读取时，
  `os.replace` 抛 `PermissionError(WinError 5)`（POSIX 不受影响）。
  新增 `_atomic_replace()`：失败退避重试 6 次（20ms 线性放大）。
  这个坑在"刷新线程写 `stock_names.json`、扫描线程同时读"的场景下真实发生。

---

## 四、改造后的锁全景

| 资源 | 作用域 | 保护手段 | 访问者 |
|------|--------|---------|--------|
| 股票分析结果 + 股票下窗缓存 | 进程内 | `AppData._stocks_cache_lock` (RLock) | REST / SSE |
| 期货下窗 CChan 缓存 | 进程内 | `AppData._futures_cache_lock` (RLock) | SSE 写 / REST 读、清 |
| 标注 / 选点 / last_code_freq / float_mc / zxg.blk | 进程内 + 文件 | `AppData._user_store_lock` + 原子写 | REST / worker（只读加载） |
| 进程池单例 + 引用计数 | 进程内 | `AppScanPool._scan_pool_lock` | REST |
| `scan_tasks.db` | **跨进程** | SQLite WAL + `busy_timeout`（OS 文件锁） | REST / worker |
| `_refresh_status` | 进程内 | `AppRefresh._refresh_state_lock` | REST / 刷新线程 |
| 除权缓存 / TqApi 注册表 | 进程内 | `DataAPI` 内部锁 | 正交，未动 |
| `CTdxAPI._tdx_data` | **每请求局部** | 无需锁（线程局部注入） | REST / worker |
| CChan / TqApi / CTqSdkSession | **每连接局部** | 无需锁 | SSE |
| `app_config` / vipdoc `.day` / `FREQ_SEC_MAP` | 只读共享 | 无需锁（无写者） | 全部 |

登记表以代码形式落在 `App/AppOrch.py` 的 `SHARED_RESOURCE_REGISTRY`
（按资源索引，每行四项：作用域 / 保护手段 / 访问者 / 说明），由
`Test/test_phase3_guards.py` 守护字段完整性。

---

## 五、验证

### 5.1 新增守护用例

| 用例 | 断言 |
|------|------|
| `Test/test_lock_v5_guards.py` | 41 项：线程局部注入隔离 / 复盘标志线程局部 / AppData 三把锁覆盖 / 原子写 / 已删符号防回潮 |
| `Test/test_chan_data_isolation.py` | 8 线程 × 3 轮并发建链 ≡ 串行基线（K线数 / 末根收盘 / 笔数 / 中枢数 / 末根高点） |
| `Test/test_chan_data_isolation_control.py` | **对照实验**：类变量注入必然串数据，线程局部注入隔离 |

三个用例已注册进 `Test/run_all.py`（组件 24~26）。

### 5.2 关于对照实验（重要）

第一版并发测试**对照组也全绿**——说明测试没有牙齿：GIL 切换没落在
`set_data` 与 CChan 读取之间的竞争窗口里。

改用**确定性交错**替代调度运气：

```
线程 A：注入 A 数据 → 发信号 → 等 B 改完 → 建 CChan → 读数
线程 B：等 A 的信号 → 注入 B 数据 → 通知 A
```

结果：

```
[隔离 ✅] 改造后（线程局部注入）: A 读到末收 106.0
[串数据 ❌] 改造前（类变量注入）: A 读到末收 206.0（读到了 B 的数据）
```

这才证明判据对这个差异敏感。**写并发测试时，务必先做"让它失败"的对照。**

### 5.3 回归结果

全部通过：`test_phase2_guards` / `test_phase3_guards` / `test_phase7_guards`
（含真实 ProcessPool 端到端）/ `test_determinism` /
`test_futures_session_binding` / `test_scanpool_fallback` / `test_app_amo` /
`test_blk_parsing` / `test_sse_concurrent` / `test_code_resolution_guards` /
`test_futures_sub_key` / `test_industry_mapping`。

> 沙箱无 `pandas` / `fastapi` / `chinese_calendar`，上述测试在最小 stub
> 下运行（仅顶替导入，不替换被测逻辑）。真实环境直接 `python Test/run_all.py`。

### 5.4 顺带修的一处基线漂移

`Test/snapshots/phase3_routes.json` 仍含 3 条已删除的盘后下载路由
（`/api/stocks/download/*`，`AppDownload.py` / `ElTdxAPI.py` 早已删除），
导致 `test_phase3_guards` ③ 路由收敛长期失败。已重冻为 27 条。
**这是既有问题，与本次锁改造无关。**

---

## 六、给后续改动的规矩

1. **加锁前先回答三句话**（执行体 / 作用域 / 是否有写-写或读-改-写），
   答不上来就别加。
2. **一把锁 = 一个资源，锁名 = 资源名**。禁止出现"引擎锁""流程锁"这种
   按代码段命名的锁。
3. **新共享资源必须登记进 `SHARED_RESOURCE_REGISTRY`**，写明作用域与保护
   手段。跨进程资源一律标注"threading.Lock 无效"。
4. **优先消除共享，其次才加锁**。能 per-request / per-connection 隔离的，
   不要靠串行兜。
5. **改并发相关代码时，先写一个会失败的对照用例**，再写正向用例。

## 七、已知遗留

- `save_annotations()` 仍会整表重写 JSON（标注量大时有写放大），暂不影响
  正确性；若标注规模增长，考虑改成增量落盘。
- `AppScanStore` 的 `_init_lock` / `_scan_store_lock` 是 `threading.Lock`，
  只护本进程单例。已在注释写明；跨进程一致性由 SQLite WAL 保证。
- 惰性加载标志（`_names_loaded` 等）仍无锁，幂等重复解析，未处理。
