# chan.py 互斥锁审计（v6）—— 对 v5 设计的复核

> 分支 `custom-dev` @ `34e058b`
>
> 本文不推翻 `chan_lock_design_v5.md`，而是**用它自己的方法论去审它的落地结果**。
>
> **一句话结论**：v5 的分析框架（执行体 → 作用域 → 是否 RMW）是对的，登记表也
> 建对了。但落地只覆盖了「已知的门」——资源本身没有被封装，底层容器通过
> `@property` 和模块级别名裸露出去，于是又长出了 9 个绕过锁的访问路径。
> 更关键的是：v5 赖以成立的两个**前提**经实测均不成立。

---

## 〇、先说两个被推翻的前提

v5 有两处结论建立在「假设」而非「验证」上，这两处一旦不成立，靠它免锁的整条
期货链路就没有地基。

### 前提 A：「SSE 每连接独占一条常驻线程」 —— 不成立

`FrontAPI.py:9`、`AppSSE.py:315`、`AppOrch.py:127` 都写着这条。实际机制是：

```python
# starlette/concurrency.py:51-59
async def iterate_in_threadpool(iterator):
    as_iterator = iter(iterator)
    while True:
        try:
            yield await anyio.to_thread.run_sync(_next, as_iterator)   # ← 每帧一次独立派发
        except _StopIteration:
            break
```

**每一帧 `next()` 都是一次独立的 `anyio.to_thread.run_sync`**，从工作线程池里
现取一条线程。线程与连接之间没有任何绑定关系。

实测（6 条并发 SSE 连接 × 12 帧，`probe_sse_thread.py`）：

```
conn0: 帧数=12  不同线程数=4
conn1: 帧数=12  不同线程数=4
conn2: 帧数=12  不同线程数=4
conn3: 帧数=12  不同线程数=4
conn4: 帧数=12  不同线程数=1
conn5: 帧数=12  不同线程数=1

发生「跨线程迭代」的连接数: 4/6
thread-local 读到别人会话（串数据）的帧数: 28
```

于是 `AppSSE.py:260 / 634` 的 `session_set(src)`（只在生成器入口设置一次）
在后续帧里**必然可能落在另一条线程上**：

- 若那条线程没设过 → `getattr(_CURRENT_SESSION, "session", None)` 得 `None`
  → `CTqSdkAPI.__init__`（`TqSdkAPI.py:295`）回退到**自己的空缓存** → 建链无数据；
- 若那条线程是另一个连接刚跑过的 → 读到**别人的记录缓存** → 串品种数据。

而 `step_load()` 每次都会新建数据源实例，不是只在首次建链时：

```python
# Chan.py:126-130 → 202-206
def step_load(self):
    self.do_init()                       # ← 清空全部 kl_datas
    for idx, snapshot in enumerate(self.load(...)):
def load(self, step=False):
    stockapi_cls = self.GetStockAPI()
    for lv_idx, klu_iter in enumerate(self.init_lv_klu_iter(stockapi_cls)):   # ← 新建 CTqSdkAPI
```

实时循环每根 K 线完成都会调 `_drain_chan(chan)`（`AppSSE.py:550 / 990`）→
`step_load()` → 新建 `CTqSdkAPI` → 读线程局部。**这就是缺口的正中央。**

> `AppSSE.py:258-259` 的注释其实已经准确描述了这条依赖（「实时循环每根K线
> step_load 会重建 CChan 数据源」），只是把「所以要靠入口那次 session_set 覆盖」
> 当成了解法——而它覆盖不到别的线程。

**⚠ 不要用 contextvars「修」它**（`probe_ctxvar.py` 实测）：

```
帧0: 线程=13408 ContextVar='session-A'
帧1: 线程=13408 ContextVar=None      ← 同一条线程也丢了
帧2: 线程=13408 ContextVar=None
```

`run_sync` 每次在调用方上下文的**副本**里执行，帧内的 `set()` 不会传回，
下一帧拿到的是新副本。换 contextvars 会让问题从「偶发」变成「必然」。

### 前提 B：「锁护住了资源」 —— 只护住了容器

`_futures_cache_lock` 保护的是 `dict` 的 get/put/pop/clear 四个瞬间。但存进去的
是**一个活着的 CChan 对象**，SSE 线程在持续改它：

```python
AppSSE.py:733   app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)  # 发布活对象
AppSSE.py:1058  _process_one_window(sub_klines, sub_chan, ...)             # 每根K线
                  └→ _drain_chan(sub_chan) → step_load() → do_init()       # 清空重建！
```

```python
# Chan.py:90-94
def do_init(self):
    self.kl_datas = {}                    # ← 整个替换成全新空 CKLine_List
    for idx in range(len(self.lv_list)):
        self.kl_datas[self.lv_list[idx]] = CKLine_List(...)
```

读取方拿到指针就出锁，然后在锁外遍历对象图：

```python
# AppChart.py:400-412（REST 线程）
chan_obj = app_data.stocks_sub_cache_get(chan_code, sub_freq)   # 锁内，只是取指针
...
kl_list = chan_obj[_m._get_kl_type(sub_freq)]                   # 锁外
bi_list = kl_list.bi_list                                       # 锁外
sliced_bis = bi_list[start_bi:end_bi + 1]                       # 锁外遍历
```

`BSPointList.py:754` 的 `futures_cache_get` 同理。所以 `/api/futures/{symbol}/select/point`
或区间套计算，可能正好撞在 SSE 线程 `do_init()` 之后、数据回填之前——读到空的
`bi_list`，表现为「红框内无完整笔」「下窗缓存已过期」这类看起来像业务错误的报错。

**锁的作用域必须与资源的作用域同级**——v5 自己写了这条，但资源的真实边界是
「CChan 这张对象图」，不是「装它的那个 dict」。

---

## 一、执行体 → 资源矩阵（核对后的实际情况）

| 执行体 | 承载 | 数量 |
|---|---|---|
| 事件循环线程 | `/api/health`、`/api/futures/read/status`、中间件、异常处理器 | 1 |
| 线程池（anyio worker） | 其余全部 REST（`run_in_threadpool`）**＋ SSE 的每一帧** | 默认 40 |
| 进程池 worker | `_worker_scan_one`（spawn，1~16） | 1~16 进程 |
| 刷新工作线程 | `refresh_stock_names_async` 起的 `threading.Thread` | 0~1 |
| 扫描收割线程 | `AppScanPool._monitor_task` | 每批 1 条 |

**SSE 不是第四类执行体，它就是线程池的一部分**——这是 v5 分类里最需要改的一格。
把它单列会诱导出「每连接独占线程」的错觉。

| 资源 | 真实作用域 | 登记表声称的保护 | 实际是否成立 |
|---|---|---|---|
| `_stocks_analysis_cache` 容器 | 进程内堆 | `_stocks_cache_lock` | ✅ |
| 缓存条目里的 dict（`{"result","chan","records"}`） | 进程内堆 | 同上 | ❌ RMW 跨锁边界，读者还会改它 |
| `_stocks_sub_chan_cache` 容器 | 进程内堆 | `_stocks_cache_lock` | ✅ |
| 下窗 CChan 对象图 | 进程内堆 | 同上 | ⚠ 股票侧发布后不再改，勉强安全 |
| `_futures_analysis_cache` 容器 | 进程内堆 | `_futures_cache_lock` | ✅ |
| 期货下窗 CChan 对象图 | 进程内堆 | 同上 | ❌ SSE 持续 `do_init()` 重建 |
| `_annotations` | 进程内堆 + 文件 | `_user_store_lock` | ❌ `get_annotated_codes` 无锁遍历 |
| `_names` | 进程内堆 | 未登记 | ❌ `replace_names` 无锁 clear+update |
| `_pe` / `_belong` | 进程内堆 | 未登记 | ❌ 无锁写 + 提前置位 loaded |
| `_float_mc` | 进程内堆 + 文件 | `_user_store_lock` | ⚠ 写持锁，`load_*` 的 clear+update 无锁 |
| `_saved_point_times` | 进程内堆 + 文件 | `_user_store_lock` | ⚠ 写持锁，`AppEngine` 3 处无锁读 |
| `zxg.blk` | **跨进程**文件 | `_user_store_lock` + 原子写 | ❌ 另有一条无锁非原子追加路径 |
| `_scan_skip_log` | 进程内堆 | **完全未登记** | ❌ 无锁，REST 清 / 收割线程追加 |
| `_refresh_status` | 进程内堆 | `_refresh_state_lock` | ⚠ 只有读者持锁 |
| `_active_scans` / `_pool` | 进程内堆 | `_scan_pool_lock` | ⚠ 计数获取与释放不在同一 try/finally |
| `scan_tasks.db` | 跨进程 | SQLite WAL + busy_timeout | ✅ 选对了 |
| `CTdxAPI._tdx_data` | 每请求局部 | 线程局部注入 | ✅ 真的安全（见 §3.1） |
| 会话记录缓存 | 每连接局部 | 线程局部绑定 | ❌ SSE 逐帧换线程（见前提 A） |

---

## 二、问题清单（已复现的标 ✔）

`repro_lock_gaps.py` 直接跑真实 `app_data`，用确定性交错（Event 卡点）而非调度
运气，**5/5 复现**。

### P0-1 SSE 会话绑定失效 —— 见前提 A
`AppSSE.py:260, 634`（`session_set`）+ `AppSSE.py:550, 990`（`_drain_chan`）
+ `TqSdkAPI.py:290-296`（回退空缓存）
**后果**：单连接 → 建链读不到数据；多连接 → 串品种。实测 28/72 帧读到别人会话。

### P0-2 期货下窗 CChan：锁护容器不护内容 —— 见前提 B
`AppSSE.py:733` 发布 → `AppSSE.py:1058` 持续 `do_init()` 重建；
读取方 `AppChart.py:400`、`BSPointList.py:754` 锁外遍历。

### P0-3 ✔ 缓存条目的读-改-写跨越锁边界
```python
# AppEngine.py:779-786（双窗主级别，单窗 804-811 同形）
main_cached = _cache_get(main_cache_key)      # ← 锁内
if main_cached is None: main_cached = {}
main_cached["records"] = full_records         # ← 锁外改
main_cached["chan"] = chan
main_cached["result"] = main_result
_cache_put(main_cache_key, main_cached)       # ← 锁内
```
每一步都持锁，整个序列不是原子的。复现结果：
```
最终缓存 = ['records']，B 写入的 'chan' 被 A 整体覆盖丢失
```
同一位置还有更隐蔽的一处——**读者写共享缓存**：
```python
# AppEngine.py:353-354
result = main_cached["result"]      # 共享 dict
result["sub"] = sub_cached["result"]   # 直接改缓存里的对象，然后 return 给路由序列化
```
两个并发请求（不同 `sub_freq`）会互相污染响应体；FastAPI 在事件循环上序列化这个
dict 时若另一线程正在改，可直接抛 `RuntimeError`。

### P1-1 ✔ `get_annotated_codes` 无锁遍历标注表
`AppData.py:2073-2076`（`load_annotations()` + `for key, anns in self._annotations.items()`，
全程无 `_user_store_lock`）vs `add_annotation`（`:2001` 持锁写）。
```
RuntimeError: dictionary changed size during iteration
```
路由 `/api/stocks/scan/annotation` 与 `/api/stocks/{code}/save/annotation` 并发即触发 → 500。

### P1-2 ✔ `replace_names` / `replace_index_belong` 的 clear+update 无锁
`AppData.py:1666-1671`、`1721-1725`。写者是刷新线程（`AppRefresh.py:490`），
读者是 `/api/search`（`AppChart.py:515 for compound_key, info in _m._stock_names_cache.items()`）、
`AppScan.py:103`、`AppData.py:2100`。两种形态都复现了：
```
②a 新表条数不同 → RuntimeError: dictionary changed size during iteration
②b 新表条数相同 → 不抛异常，但遍历到 旧表 6 条 + 新表 1994 条
```
②b 更值得警惕：`ma_used` 没变就绕过了 CPython 的迭代检查，**静默串表**，
搜索结果无声错乱。

### P1-3 ✔ `load_pe_ttm_cache` 先置位后填充
```python
# AppData.py:1679-1697
if self._pe_loaded: return self._pe
self._pe_loaded = True          # ← 先声明「已加载」
self._belong_loaded = True
...                              # ← 才开始读文件、逐条填 self._pe
```
并发读者见 `loaded=True` 直接返回半成品。复现：`并发读者拿到 None（文件里其实有 25.3）`
→ 扫描列表 PE / 指数归属列静默显空，且**这一轮再也不会重试**（flag 已是 True）。
`load_annotations`（`:1964`）、`load_float_mc_cache`（`:1735`）的 `clear()+update()`
是同一形状。

### P1-4 `zxg.blk` 两条写路径，一条完全裸奔
| 路径 | 位置 | 锁 | 原子 | 进程 |
|---|---|---|---|---|
| `save_to_zxg_blk` | `AppData.py:2163` `open(path,"a")` | 无 | 无 | API |
| `sync_zxg_blk` | `AppData.py:2204` | `_user_store_lock` | `_atomic_write_text` | **独立脚本进程** |

登记表写「zxg.blk 由 `_user_store_lock` + 原子落盘保护」，实际上：
① 生产路由 `POST /api/stocks/scan/save/zxg` 走的是无锁追加，并发两次会交错；
② 两个写者在**不同进程**，`threading.Lock` 天然无效——正是 v5 §1.2 点名的
「最危险的误用」，只是它出现在文件层而没被识别。

### P1-5 `_scan_skip_log` 整个逃出了登记表
`AppScan.py:53` 模块级 list。追加者是收割线程（`AppScanPool.py:201`），
清空/遍历者是 REST（`AppScan.py:626 .clear()`、`:648` 遍历、`:661 len()`）。
无锁、未登记。后果是汇总明细丢失或串批次——不崩，所以一直没人发现。
**这条最能说明问题**：v5 的守护测试问的是「这扇门有没有锁」，
没问「是不是所有门都上了锁」。

### P1-6 进程池引用计数可能泄漏
`_get_pool()`（`AppScanPool.py:113`）在锁内 `_active_scans += 1`，但从这里到
「收割线程接手负责 `_release_scan()`」之间有 4 条可抛异常的语句没有 try/finally
兜底（`:270 _resolve_workers`、`:272 log.info`、`:296 store.set_status`、
`:297 Thread(...).start()`）。任一处抛出 → 计数只增不减 → `_active_scans` 永远
`> 0` → **进程池永不销毁**，worker 缓存一直占内存（与「即用即弃」的设计意图相反）。

### P2 其余
- **`_refresh_status` 只有读者持锁**：`refresh_status()`（`AppRefresh.py:535`）
  持锁做快照，但写者（`:247 ["loaded"]`、`:258 ["step"]`、`:284 ["error"]`、
  `:525 ["running"]=False`）全部不持锁。键集合固定所以不会崩，但这是
  「看起来有锁」的典型——CAS 那部分（`:302-310`）是对的，其余是装饰。
- **`refresh_stock_names_async` 返回值不实**：`:553-555` 的预检查放锁后才起线程，
  两个并发 POST 都返回 `{"status":"started"}`，但装饰器 CAS 只放一个进去，
  另一条线程静默退出。
- **固定 `.tmp` 名**：`safe_write_json_file` / `_atomic_write_text` 都用
  `path + ".tmp"`（`AppData.py:92, 119`）。目前同路径的写入恰好都被串行化了
  （单刷新线程 / `_user_store_lock`），所以没炸。但一旦哪天让 worker 落盘，
  `finally: os.remove(tmp_path)` 会删掉别的进程刚建的临时文件。建议改
  `tempfile.mkstemp(dir=同目录)`。
- **`AppEngine.py:378-379 / 552-553 / 1146-1147`** 无锁读 `_saved_point_times`，
  且是 `in` 判断后再下标的 check-then-act。写者 `clear_saved_points_by_prefix`
  只删 `KQ.` 前缀，与股票代码不重叠，所以目前撞不上——**靠数据巧合，不靠设计**。
- **`AppScanStore._init_db` 的 DROP TABLE 分支**（`:113-115`）由 `threading.Lock`
  保护，跨进程无效。目前父进程先建库、worker 后启动，所以撞不上，注释里说清即可。
- **`Common/cache.py` 的 `@make_cache`** 把结果写进 `instance._memoize_cache`。
  只要实例不跨线程共享就没事——但期货下窗 CChan 恰恰是跨线程共享的（P0-2）。

---

## 三、两条结构性根因

### 根因一：资源没被封装，只有方法被加锁

v5 把锁挂在 `AppData` 的方法上，但同时把底层容器**原样漏了出去**：

```python
# AppData.py:1376-1410 —— 9 个裸出口
@property
def stocks_analysis_cache(self): return self._stocks_analysis_cache
def futures_analysis_cache(self): return self._futures_analysis_cache
def names_cache(self):  return self._names
def pe_cache(self):     return self._pe
def belong_cache(self): return self._belong
def float_mc_cache(self):     return self._float_mc
def saved_point_times(self):  return self._saved_point_times
def annotations_cache(self):  return self._annotations
def stocks_cache_lock(self):  return self._stocks_cache_lock   # 锁也漏出去了
```

下游立刻把它们绑成模块级别名，从此谁都能绕过锁：

```python
AppEngine.py:200   _stock_names_cache  = app_data.names_cache
AppEngine.py:220   _pe_ttm_cache       = app_data.pe_cache
AppEngine.py:224   _index_belong_cache = app_data.belong_cache
AppEngine.py:253   _stocks_analysis_cache = app_data.stocks_analysis_cache
AppEngine.py:293   _saved_point_times  = app_data.saved_point_times
AppRefresh.py:35   _stock_names_cache  = app_data.names_cache
AppRefresh.py:38   _pe_ttm_cache       = app_data.pe_cache
AppRefresh.py:42   _index_belong_cache = app_data.belong_cache
AppScan.py:50      _stock_names_cache  = app_data.names_cache
```

这些别名的注释都写着「共享同一对象，零漂移」——**零漂移是目的，代价是零保护**。
P1-1、P1-2、P1-3、P2 里 `_saved_point_times` 那条，全部从这里长出来的。

一把锁只有在「持有资源的对象是唯一入口」时才成立。漏出容器 = 声明锁是可选的。

### 根因二：三问漏了第四问

v5 的三问（执行体 / 作用域 / 是否 RMW）在**容器**这一层是完备的，
但共享的往往不是容器，是**一张对象图**。缺的是第四问：

> ④ 跨执行体共享的是「容器」还是「容器里那个还活着的对象」？

- 容器共享 → `dict` 级别的锁就够（v5 做对了）
- 对象图共享 → 锁必须覆盖**读取方遍历对象图的全过程**，或者根本不共享活对象

P0-2、P0-3 都是第四问的漏网之鱼。

---

## 四、建议的修法（按性价比排序）

**先做的三件（消除共享，不加锁）**

1. **期货下窗改「发布不可变快照」**。SSE 线程算完一帧后，把 REST 侧真正需要的
   东西（`bi_list` 的日期/价格序列等）提取成普通 list/dict 再 `futures_cache_put`，
   不要把活着的 CChan 发布出去。这样 `_futures_cache_lock` 护容器就真的够了，
   P0-2 和 `@make_cache` 的隐患一起消失。
2. **SSE 的会话绑定改成每帧重新绑定**：把每一帧的同步工作体包进
   `with session_context(src):`（不跨 `yield`），或在循环体开头无条件
   `session_set(src)`。生成器入口那一次留着无害，但不能作为唯一保障。
   长期正解是让引擎支持**显式注入数据源实例**，彻底删掉线程局部——
   `data_src="custom:..."` 这个字符串协议是所有线程局部戏法的根源。
   **不要改用 contextvars**（见前提 A 的实测）。
3. **缓存条目改「整体替换」而非「原地改」**：`_cache_get` 出来的 dict 不再改，
   构造一个新 dict 再 `cache_put`；或者给 `AppData` 加一个
   `cache_update(key, **fields)` 在锁内完成 RMW。同时 `AppEngine.py:353-354`
   必须改成先 `dict(main_cached["result"])` 复制再挂 `sub`。

**再做的两件（收边界）**

4. **删掉 9 个裸 property**，或改成返回快照（`dict(self._names)`）。下游别名
   全部换成方法调用。这一步会暴露出所有绕锁路径——那正是目的。
   现在的守护测试断言「这些入口持锁」，应该补一条**完整性断言**：
   遍历 `AppData` 的公开方法，凡是触碰受保护字段的必须持对应锁（可用
   `ast` 静态扫描实现，比运行时计数更彻底）。
5. **把 `_names` / `_pe` / `_belong` / `_scan_skip_log` 登记进
   `SHARED_RESOURCE_REGISTRY`** 并配锁。`_names` 与 `_pe`/`_belong` 建议共用一把
   `_meta_cache_lock`（写者只有刷新线程，读者是全部 REST，冲突面小）；
   `replace_names` 改成锁内整体替换，`load_pe_ttm_cache` 把置位挪到填充**之后**
   （并用双检锁避免重复解析）。

**兜底两件**

6. `zxg.blk` 统一到 `sync_zxg_blk` 一条写路径；跨进程部分上 OS 级文件锁
   （Windows `msvcrt.locking` / POSIX `fcntl.flock`），或干脆让脚本改为调 API。
7. `_get_pool()` 的引用计数改成上下文管理器，保证「递增」与「递减」在同一个
   `try/finally` 里；`.tmp` 换成 `tempfile.mkstemp`。

---

## 五、给后续改动的规矩（在 v5 §六上补 3 条）

1~5 条沿用 v5。新增：

6. **加锁前先问第四问**：共享的是容器还是对象图？共享活对象的，先想怎么不共享。
7. **不许漏出受保护的容器**。要暴露就暴露快照或方法，`@property` 直接 `return`
   内部可变对象等于宣布锁是装饰。
8. **前提要验证，不能靠推断**。「每连接一条线程」「GIL 保证原子」这类前提，
   写一个 20 行的探针跑一次的成本，远低于它错了之后的排查成本。
   本轮 `probe_sse_thread.py` / `probe_ctxvar.py` / `repro_lock_gaps.py` 三个探针
   合计不到 300 行，推翻了 2 个前提、复现了 5 个缺口。

---

## 附：本轮用到的探针

| 文件 | 作用 | 结果 |
|---|---|---|
| `probe_sse_thread.py` | 真实 `StreamingResponse` + 同步生成器，6 连接并发，记录每帧线程与 thread-local | 4/6 连接跨线程，28 帧串会话 |
| `probe_ctxvar.py` | `contextvars` 能否替代 `threading.local` | 同线程第 2 帧即丢值 |
| `repro_lock_gaps.py` | 对真实 `app_data` 做确定性交错 | 5/5 复现 |
