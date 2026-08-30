# chan.py 锁全景图（基于 custom-dev @ ba8d58b）

> 目的：让你直观理解"四把锁分散"这个结论——它们保护同一份非线程安全的分析引擎，
> 却分散在 4 个文件、只有 1 把进策略表、真正的跨路径互斥锁还"藏"在引擎内部。

## 0. 先对齐"四把锁"的口径

P1-1「四把锁分散」指 **4 个模块级、进程级全局锁，都用来保护同一个非线程安全的分析引擎及其编排**：

| # | 锁对象 | 定义位置 | 语义 | 是否进 LOCK_POLICY |
|---|--------|----------|------|--------------------|
| 1 | `_ENGINE_LOCK` | `App/AppChart.py:45` | SERIAL 类请求全局串行（REST 交互式分析） | ✅ 经 `engine_section` 驱动 |
| 2 | `_stock_analysis_lock` | `App/AppEngine.py:265` | 实际 `CChan` 构建（**每次引擎调用都拿**） | ❌ 硬编码在 AppEngine |
| 3 | `_scan_lock` | `App/AppScan.py:53` | 同步扫描路径的引擎串行 | ❌ 硬编码在 AppScan |
| 4 | `_pool_lock` | `App/AppScanPool.py:62` | 进程池生命周期（创建/销毁） | ❌ 硬编码在 AppScanPool |

> 项目里其实还有 10 个锁原语（AppData 的 3 个 RLock、数据源/下载的 3 个、SSE/期货会话的 4 个），
> 但那些保护的是**不同关切**（数据持久化、外部 API 串行、会话隔离），不在"四把锁"之列。

## 1. 两条调用链，怎么拿到这些锁

**REST 调用链（SERIAL）**
```
call_analysis (AppChart:95)
  └─ with engine_section("call_analysis")        → ① _ENGINE_LOCK (AppChart:45)
       └─ _m.analyze_stock (AppEngine:1616)
            └─ _analyze_stock_internal (AppEngine:301)
                 └─ with _stock_analysis_lock     → ② _stock_analysis_lock (AppEngine:265)
                      └─ CChan 构建 + 共享状态读写
```
**一次 REST 分析同时持 ① 和 ② 两把锁**（外层 SERIAL 串行，内层引擎互斥）。

**扫描调用链（SCAN，同步旧路径）**
```
Scanner.scan_one (AppScan:408)
  └─ with _scan_lock                              → ① _scan_lock (AppScan:53)
       └─ _m.analyze_stock → _analyze_stock_internal
            └─ with _stock_analysis_lock           → ② _stock_analysis_lock (AppEngine:265)
                 └─ CChan 构建 + 共享状态读写
```
**扫描也同时持 ①(_scan_lock) 和 ② 两把锁**。

**批量扫描（SCAN_ASYNC）**：`submit_batch_scan` 把任务派发到进程池，引擎调用在 worker 内走 `scan_one`。
- 进程模式（spawn）：每个 worker 是独立进程，`_scan_lock`/`_stock_analysis_lock` 各进程一份，
  `app_data` 也是各进程独立拷贝 → 天然不共享，无需跨进程锁。
  （注：扫描现仅用进程池，不再提供线程降级路径。）

## 2. 为什么叫"四把锁分散"——5 个具体证据

1. **物理分散在 4 个文件**：想搞清楚"引擎被哪些锁保护"，必须同时打开
   `AppChart.py` / `AppEngine.py` / `AppScan.py` / `AppScanPool.py`，外加 `AppOrch.py`（策略表）共 5 个文件。
   没有任何单一归属点（single owner）。

2. **策略表只真正驱动了 1 把**：`LOCK_POLICY`（`AppOrch.py:97`）登记了 5 类行为，
   但只有 SERIAL 类经 `engine_section` 实际取 `_ENGINE_LOCK`。
   其余 `_stock_analysis_lock` / `_scan_lock` / `_pool_lock` **根本不在 `LOCK_POLICY` 里**，
   只是注释里"描述"了意图（`Scanner.scan_one` 的注释写着"全局 _scan_lock 内串行"），
   实际加锁是各自模块里手写的 `with`。策略与实现依然分道扬镳——这正是 P1-2 只改了一半。

3. **真正的跨路径互斥锁"藏"在引擎内部**：`_stock_analysis_lock` 是 REST 与 SCAN 唯一能互相排斥的锁
   （两条链都进 `_analyze_stock_internal` 才拿它）。但它定义在 `AppEngine.py`，
   `LOCK_POLICY` 完全不知道它的存在。换句话说，**策略表描述的所有分类，其实都靠一个它没登记的锁兜底**。

4. **一把逻辑操作拿两把锁**：REST 分析同时持 `_ENGINE_LOCK`（外层）+ `_stock_analysis_lock`（内层）。
   内层才是真正的引擎互斥锁，外层 `_ENGINE_LOCK` 额外把整个 SERIAL 请求（含非引擎的前后置）串行化。
   两把锁各自独立定义、各自独立维护，没有任何代码表达"它们保护的是同一份共享状态"。

5. **加锁顺序只靠代码结构 + 注释隐式保证**：当前顺序是"外层(_ENGINE_LOCK/_scan_lock) → 内层(_stock_analysis_lock)"，
   两条链都一致，所以不会死锁。但**没有任何中央契约**约束这个顺序——
   将来谁加一条"先拿 `_stock_analysis_lock` 再拿 `_ENGINE_LOCK`"的路径，就是一个潜在死锁，守护测试也发现不了。

## 3. 当前到底有没有 bug？（诚实结论）

**大概率是"没有活跃的数据竞争"，但安全是"撞巧的"，不是"被设计保证的"。**

- 因为 `_stock_analysis_lock` 是 ② 真正的跨路径互斥锁，且两条链都经它，所以在曾经的**线程降级模式**下
  REST 与 SCAN 对引擎状态（`stocks_analysis_cache` / `REPLAY_MODE` / TdxAPI 内部 buffer）是互斥的；
  进程模式下各 worker 状态隔离，也无共享问题。（注：现仅用进程池，线程降级已移除。）
- 但 REPLAY_MODE 是全局类属性（P1-3 已单列，你暂缓），其在引擎构建块外是否被别处读取/改写，
  不在这把锁的覆盖范围内——那是另一个独立问题。

**结论**：分散本身是**结构性/可维护性债务**，不是当前必现的崩溃。它的真实风险是：
- 改一把锁时，你无法在一个地方看到它的全部使用点；
- 删/合并其中一把（比如有人觉得 `_ENGINE_LOCK` 多余想清掉），可能悄无声息地破坏跨路径互斥；
- 守护测试 `func_map_check` 只 grep 字面量 `with _ENGINE_LOCK:`，验证不了"SERIAL 与 SCAN 是否互斥"。

## 4. 集中化（P1-1 的修法）长什么样

目标：让"哪把锁、保护什么、哪些入口用、顺序如何"在一个模块里说清楚，且由策略表驱动。

```
# App/locks.py  （唯一归属点）
ENGINE_LOCK        = threading.Lock()   # SERIAL 外层：REST 交互式请求间串行
STOCK_ANALYSIS_LOCK = threading.Lock()  # 引擎构建：所有路径的真实互斥锁
SCAN_LOCK          = threading.Lock()   # 扫描外层串行
POOL_LOCK          = threading.Lock()   # 进程池生命周期

# 策略表只描述"入口 → 锁（含顺序）"，不再只是文字
LOCK_POLICY = {
    "call_analysis":            ["ENGINE_LOCK", "STOCK_ANALYSIS_LOCK"],
    "Scanner.scan_one":         ["SCAN_LOCK",    "STOCK_ANALYSIS_LOCK"],
    "Scanner.submit_batch_scan":["POOL_LOCK"],   # 仅 API 进程持池锁
    ...
}

@contextmanager
def locked(entry):                      # 替代 engine_section，覆盖全部分类
    locks = [getattr(LOCKS, l) for l in LOCK_POLICY[entry]]
    with ExitStack() as es:
        for lk in locks: es.enter_context(lk)
        yield
```
四个模块改为 `from App.locks import ...`，不再各自 `threading.Lock()`；
`func_map_check` 升级为**运行时断言**（用 `locked()` 进入时记录持有的锁集合，验证 SERIAL 与 SCAN 的锁集合交集非空 = 真的互斥），而不是 grep 字面量。

## 5. 一句话总结

"四把锁分散" = 保护同一引擎的 4 把全局锁，定义在 4 个文件，只有 1 把被 `LOCK_POLICY` 驱动，
真正的跨路径互斥锁还埋在 `AppEngine` 内部、不在策略表内；当前能跑是因为嵌套顺序恰好正确，
但这层正确性没有任何单一可维护的契约来保证。
