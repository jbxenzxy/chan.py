# chan.py `custom-dev` 分支 · 独立架构评审

> 评审对象：`https://github.com/jbxenzxy/chan.py/tree/custom-dev`（commit `dd87140`，2026-08-28）
> 评审方式：源码逐文件通读 + AST 级死代码/依赖图扫描，**结论以代码为准，注释仅作对照**
> 重点范围：`FrontAPI.py`、`App/`、`DataAPI/`、`Frontend/`、`Test/`
> 不评审：`Bi/ BuySellPoint/ Combiner/ Common/ KLine/ Math/ Plot/ Seg/ ZS/`（第三方缠论内核）
> —— 但下文 §5-P1 会指出：这条边界在代码层面**并不成立**，必须单独处理。

---

## 0. 结论摘要

| 维度 | 评价 |
|---|---|
| 分层意图 | 清楚，注释里写得比多数同类项目都细 |
| 分层落地 | **只有"物理分文件"，没有"逻辑分层"**：4 类锁、5 类全局可变状态、3 处惰性 import 环、2 处下游反向 import |
| 端到端一致性 | 16 个功能中 **10 个达标、6 个有不同程度缺口**，其中 2 个是静默功能失效 |
| 冗余 | 生产代码 23.6k 行里，约 **1.5k 行可删**，外加 2.1 万行历史 HTML 文档、2 个未接入 CI 的测试 |
| 注释可信度 | **低**。发现 12 处注释与代码直接冲突，其中 3 处会误导后续维护者做出错误修改 |

**一句话**：这是一次"把 1 万行脚本拆成 15 个文件"的重构，不是一次"建立架构"的重构。拆分本身有价值，但缺少三件关键东西——**显式的状态所有权、可执行的架构约束、有效的缓存失效**。

---

## 1. 代码事实：这个软件实际长什么样

### 1.1 体量分布

| 目录 | 行数 | 说明 |
|---|---:|---|
| `App/` | 9,768 | 业务层 15 个模块（含 1,500 行硬编码行业映射表） |
| `DataAPI/` | 5,399 | 数据源适配器（`TdxAPI.py` 2,769 行为最大文件） |
| `Frontend/` | 8,759 | 单 IIFE，`app.js` 7,797 行 |
| `Test/` | 10,038 | 29 个文件，多为"结构/文本守卫" |
| `Docs/` | **21,408** | 15 个 HTML 架构文档（V5 / V7 / V10 / V15 多轮重构的残留） |
| 生产代码合计 | **23,593** | |

文档量 ≈ 生产代码的 91%。这本身就是信号：**架构在文档里反复重写，但代码没跟上**。

### 1.2 真实依赖图（AST 扫描，含函数内惰性 import）

```
FrontAPI.py ──▶ AppOrch ──▶ AppChart ──▶ AppSSE ──▶ AppEngine ──▶ DataAPI ──▶ 缠论核心
                   │            │            │           │
                   │            │            │           └──▶ AppData ◀──┐
                   │            │            │                  │        │
                   └──▶ AppScan ─┴──▶ ScanPool ──▶ AppOrch (环①)  │        │
                        AppScan ◀──── ScanPool          (环②)     │        │
                   AppData ◀────────── utils (环③) ────────────────┘
```

三处**惰性 import 环**（之所以能跑，纯粹因为 import 写在函数体内）：

| 环 | 路径 | 为什么危险 |
|---|---|---|
| ① | `AppScan → AppScanPool → AppOrch → AppScan` | 改任一模块的 import 顺序都可能触发 `ImportError`；`ScanPool` 里 `from App.AppOrch import scanner` 依赖 `AppOrch` 已完成初始化 |
| ② | `AppScan ↔ AppScanPool`（`_scan_skip_log` 反向取） | 模块级状态被另一个模块直接读写，绕开了 `Scanner` 类的封装 |
| ③ | `AppData ↔ utils`（`_get_stock_name` ↔ `_get_stock_market_code`） | 两个"最底层"模块互相依赖，`AppData` 不再是叶子 |

另有一处**下游反向 import**（见 §5-P1-4）：
`BuySellPoint/BSPointList.py:698,725` → `from App.AppData import app_data`。

### 1.3 全局可变状态清单（架构的真正"地心"）

所有并发问题、缓存问题、串数据问题的根，都在这张表里。**Layering 文档里没有一张这样的表，这是最大的缺失。**

| 状态 | 位置 | 作用域 | 保护方式 | 评价 |
|---|---|---|---|---|
| `app_data._stocks_analysis_cache` | `AppData` 单例 | 进程 | `_cache_lock`（RLock，只保护单操作） | ⚠ 读-改-写跨操作无锁 |
| `app_data._saved_point_times` | `AppData` 单例 | 进程 | `_saved_point_lock`（仅写路径） | ⚠ 读路径（`AppEngine:418/592`）裸读 |
| `app_data._annotations` | `AppData` 单例 | 进程 | `_annotations_lock` | ✅ 完整 |
| `app_data._futures_analysis_cache` | `AppData` 单例 | 进程 | **无锁** | ⚠ 普通 dict 并发写 |
| `app_data._stocks_sub_chan_cache` | `AppData` 单例 | 进程 | **无锁** | ⚠ 同上 |
| **`CTdxAPI._tdx_data`** | **类属性** | 进程 | `AppEngine._stock_analysis_lock` | ⚠ 引擎与分析两类路径共用 |
| **`CTqSdkAPI._records_by_symbol`** | **类属性** | 进程 | `cls._lock`（单次操作） | ❌ **跨 SSE 连接互相踩** |
| **`CMyBSPointList.REPLAY_MODE`** | **第三方类旗** | 进程 | 无 | ❌ 股票复盘与期货 SSE 互踩 |
| `_ENGINE_LOCK` / `_scan_lock` / `_stock_analysis_lock` / `_cache_lock` | 4 个模块各自定义 | — | — | ⚠ 无统一登记与强制 |
| 模块级 `_scan_aborted` / `_scan_skip_log` / `_refresh_status` / `_page_index_code` | `AppScan` / `AppRefresh` | 进程 | 无 | ⚠ 多批次扫描串状态 |

---

## 2. 做对了的部分（这些不要动）

必须先把话说清楚，否则容易变成"全盘否定"：

1. **路由收敛到单一文件**。`FrontAPI.py` 33 条路由全部是"校验 → `run_in_threadpool(orch.*)` → 组装响应"，没有一条塞业务。这是对的，而且做得干净。
2. **领域异常体系**。`App/AppErrors.py` 6 个异常带 `status_code`，`FrontAPI` 两个 `@app.exception_handler` 统一兜底。数据不可用返回 502、持久化失败 503，语义准确。
3. **批量扫描的 ProcessPool + SQLite 跨进程方案**。`AppScanStore` 用 WAL + `INSERT OR IGNORE` 幂等 + `completed` 由行数派生（不落列，杜绝漂移），`_monitor_task` 用 `try/finally` 保证池引用一定归还，`_active_scans` 引用计数解决并发批次互毁。这一段是**全仓库工程质量最高的部分**。
4. **原子写**。`safe_write_json_file` 先写临时文件再校验再 `os.replace`，标注 / 选点 / 配置都走它。
5. **SSE 生命周期对天勤约束的遵守**。`close()` 只置旗、`close_api()` 只在生成器线程 `finally` 调用——这个约束（tqsdk 的 `_loop.is_running()` 检查）处理得正确。
6. **A/B 实现开关 `CHAN_STOCK_DUAL_IMPL`**。`_dual_impl_tag()` 作为缓存 key 第 6 维隔离两种实现，防止切换开关后串用缓存。想得很细。
7. **结构化缓存键工厂**（`make_single_key` / `make_dual_*_key` / `make_futures_sub_key`）。消除了字符串拼接漂移，方向正确。
8. **`_env_bool` 修掉了 `bool(raw)` 陷阱**。说明维护者在认真修 bug。

---

## 3. 我认为的最优架构

### 3.1 三条第一性原理

这个项目只有三个真正的复杂度来源，架构应当全部指向它们：

1. **缠论引擎是有状态、非线程安全的**（`CChan` 对象大、构建慢、内部有 `REPLAY_MODE` 之类的类级旗标）。
   → 所以**引擎调用点必须收敛**，且**引擎不得持有跨请求状态**。
2. **数据来自多个彼此不同步的源**（本地 TDX 文件、天勤实时流、腾讯/新浪 HTTP、AKShare、同花顺云）。
   → 所以**数据源必须是端口 + 适配器**，且**每个适配器的会话状态随对象生命周期，不得是类属性**。
3. **同一份数据要在 4 种视图下呈现**（单窗 / 双窗 / 选点后 / 复盘截断），且每种视图的取数边界不同。
   → 所以**"取数边界"必须是一个显式的值对象**，而不是散落在函数参数里的 `end_date` + `start_time` + `step` + `lookback` 四件套。

### 3.2 目标分层（六层 + 四条横切）

```
┌─ 1 前端渲染层 ─────────────────────────────────────────────┐
│  画布 / 视图状态 / 交互。不做任何缠论计算。                  │
│  契约：后端快照即渲染真值；红框灰框只做区间映射。            │
└──────────────────────┬─────────────────────────────────────┘
┌─ 2 接口层（FrontAPI）──────────────────────────────────────┐
│  参数校验 / 异常映射 / 响应组装。一行路由 = 一次用例调用。    │
└──────────────────────┬─────────────────────────────────────┘
┌─ 3 用例服务层（每功能一个对象，依赖注入）──────────────────┐
│  AnalyzeUseCase / SelectPointUseCase / ScanUseCase /        │
│  RealtimeUseCase / RefreshUseCase / DownloadUseCase ...     │
│  职责：编排 + 声明自己需要的锁与缓存。不含算法。            │
└──────────────────────┬─────────────────────────────────────┘
┌─ 4 分析引擎层（无状态 facade）─────────────────────────────┐
│  ChanGraph.analyze(klines, config, sub_klines=None)         │
│              -> StructureSnapshot                            │
│  输入 K 线序列 + 配置，输出结构快照。                        │
│  不持有缓存、不加锁、不 import 应用层。可并行、可重入。      │
│  区间套所需的子级别数据走**显式入参**，不走全局缓存。        │
└──────────────────────┬─────────────────────────────────────┘
┌─ 5 数据源端口层（端口 + 适配器）──────────────────────────┐
│  KLineSource 端口：fetch(symbol, window) -> list[Bar]       │
│  适配器：TdxSource / TqSource / ElTdxSource                 │
│  只做 I/O。**禁止类级可变字段**，会话状态随实例。            │
└──────────────────────┬─────────────────────────────────────┘
┌─ 6 持久化层（仓储接口）────────────────────────────────────┐
│  PointRepo / AnnotationRepo / TaskRepo / CacheRepo          │
│  读-改-写在**同一把锁**内；原子写；带版本号。                │
└─────────────────────────────────────────────────────────────┘
横切（各一处，不得散落）：
  并发策略 · 缓存（key 工厂 + 失效版本）· 配置 · 日志与异常
```

### 3.3 三条硬约束（比分层本身更重要）

**约束 A：并发策略必须是"可执行的"，不是"登记的"。**

现在的 `LOCK_POLICY` 是一个 `dict[str, tuple[str, str]]`，只被测试读取用来断言"登记了、且源码里有 `with _ENGINE_LOCK:` 字样"。它不能阻止任何人绕过。

应当改成：

```python
# App/concurrency.py —— 全项目唯一的锁定义处
ENGINE_LOCK = threading.Lock()      # 引擎构建期（含数据源 set_data / step_load）
CACHE_LOCK  = threading.RLock()     # 缓存与持久化
SCAN_LOCK   = threading.Lock()      # 扫描批次

@contextmanager
def engine_section(kind: str):      # kind ∈ {"serial","scan","self_contained"}
    """进引擎前的统一入口。SELF_CONTAINED 直接 yield，其余按 kind 取锁。"""
```

并配一条**静态检查**（不是文本 grep，而是 AST）：凡调用引擎构建函数的模块，必须在 `engine_section` 上下文内。

**约束 B：缓存必须带"失效版本"，key 工厂只是及格线。**

现在有 key 工厂（好），但没有失效机制。`_stocks_analysis_cache` 的 key 是 `(kind, market, code, freq, date, impl)`，其中 `date` 只有复盘时才变。后果见 §5-P0-3：**盘后下载更新了 .day 文件，缓存里还是昨天的数据，且没有任何接口能清掉它**。

建议：key 增加 `data_version`，由数据源在数据目录 mtime / 下载完成事件变化时递增；并补一个 `POST /api/stocks/cache/clear`。

**约束 C：引擎层不得 import 应用层。**

`BSPointList.py` 直接 `from App.AppData import app_data`，这条边必须打断。做法是把"子级别数据"作为参数传进去：

```python
# 现在（错）：引擎自己去全局缓存捞
sub_chan = app_data.stocks_sub_cache_get(parent.code, sub_freq)

# 应该：由用例层在构造 CChan 时注入
chan = CChan(..., nested_provider=NestedProvider(sub_chan_or_none))
```

做不到完全改内核也没关系——至少**在内核外面包一层 `ChanFacade`**，把"注入子级别数据"这个动作收敛到一处，并用测试锁死 `grep -rn "from App" BuySellPoint/` 为空。

### 3.4 取数边界值对象

现在 `end_date` / `start_time` / `step` / `lookback` / `dual` / `sub_freq` 六个参数在 `FrontAPI → call_analysis → analyze_stock → _analyze_stock_internal` 之间裸传，中途还有"双窗不读 CSV 选点""复盘不读选点""日内周期右边界推到 23:59:59"等隐式规则散落在 4 个地方。

建议合成一个值对象：

```python
@dataclass(frozen=True)
class Window:
    symbol: str
    freq: str
    right: datetime | None      # 复盘终点，None = 实时
    left: datetime | None       # 选点起点，None = 按 lookback
    dual_sub: str | None        # 双窗下窗周期
    replay: bool = False        # 由 right 派生

    def slice(self, records) -> list: ...
```

好处：边界规则只有一份实现；`step` 这种已废弃参数会自然消失；测试可以直接对 `Window` 做单测（现在做不到）。

---

## 4. 端到端功能核对表

对每个功能，从"用户点击"追到"数据落盘/推送"，再回到"前端渲染"。

| # | 功能 | 链路 | 状态 | 问题 |
|---|---|---|---|---|
| 1 | 查询 / 切周期 | `switchFreq → GET /analyze → call_analysis → analyze_stock → TdxAPI → CChan → extract → JSON → render` | ✅ 达标 | 单窗口路径干净，缓存命中逻辑正确 |
| 2 | 双窗口（股票） | `+dual=1&sub_freq → 独立双窗路径（先下后上）` | ⚠ 基本达标 | A/B 双实现并存（`independent`/`legacy`），legacy 分支是纯回滚通道，属技术债但可接受；`dual_sub` 缓存与运行时下窗缓存是两套，**复盘态下两者可能不一致** |
| 3 | 复盘（股票） | `gotoDate → end_date → 截断 → 重建` | ✅ 达标 | 日内周期 23:59:59 修正已修（代码注释里记录了原 bug，处理正确） |
| 4 | 手动选点（股票） | `select/point → 找左肩 T → 存 CSV → 清缓存 → 重建` | ⚠ 有缺口 | ① `stock_manual_select_point` **绕过 `analyze_stock` 直调 `_m._analyze_stock_internal`**，违反"统一入口"契约；② 双窗选点缓存失效靠手工删 3 个 key，脆弱 |
| 5 | 取消选点 | `delete/point → clear_saved_point` | ✅ 达标 | CSV + 内存 + 缓存三处一致 |
| 6 | 红框中枢 | `red-range → compute_red_range_zs` | ⚠ 有缺口 | 独立实现读**运行时下窗缓存**（无日期维度），复盘态下可能读到实时下窗 |
| 7 | **期货实时流 SSE** | `read/stream → 两生成器` | ❌ **不达标** | `CTqSdkAPI._records_by_symbol` 是**类属性**，多连接同 symbol+freq 会互相覆盖与重复追加；`cleanup_records` 在一条连接 finally 里 pop 掉公共 key（§5-P0-1） |
| 8 | **期货双窗区间套** | SSE 双窗 → `check_nested_diver` | ❌ **静默失效** | 写 key 与读 key 不一致，100% miss，永远走"按子级别背驰处理"（§5-P0-2） |
| 9 | 期货选点 | `futures/{sym}/select/point` | ⚠ 有缺口 | 与 SSE 流共用同一份类级 records 缓存，且与 SSE 路径**不在同一把锁下** |
| 10 | 搜索 | `search → search_stocks` | ⚠ 实现正确，结构差 | 搜索逻辑放在 `AppChart`（图表域）里，且与 `AppSSE` 的期货别名逻辑形成三角依赖 |
| 11 | 批量扫描 | `scan/submit → ProcessPool → SQLite → 轮询` | ✅ 达标 | 全仓库最佳；唯一缺口是**没有全局中断**（§5-P1-5） |
| 12 | 自选股保存 | `scan/save/zxg → TDX blk + 同花顺云` | ✅ 达标 | `.blk` 格式知识在 `AppData` 与 `TdxAPI` 各存一份（有注释承认），是可接受的成本 |
| 13 | 盘后下载 | `download/start → ElTdxAPI` | ❌ 不达标 | **下载完成后不失效分析缓存**（§5-P0-3） |
| 14 | 刷新（名称/PE/归属/板块） | `stocks/refresh → AppRefresh` | ✅ 达标 | 异步 + 状态轮询 + 装饰器守卫，处理得当 |
| 15 | 标注 CRUD | `annotation read/save` | ✅ 达标 | 锁完整、去重正确 |
| 16 | 市场量能 | `amo/read → AppAMO` | ✅ 达标 | 纯函数式，仅依赖 `_ENGINE_LOCK`（但见 §5-P2：为了拿一把锁去 import `AppChart` 是不必要的耦合） |

**达标 10 / 缺口 4 / 失效 2。**

---

## 5. 缺陷清单

### P0 · 正确性与功能失效（必须修）

#### P0-1 · SSE 期货流的 K 线记录缓存是类级全局，多连接互相踩

`DataAPI/TqSdkAPI.py:208` `_records_by_symbol = {}` 是**类属性**，被 `CTqSdkSession` 的所有实例共享（`CTqSdkCSSESource.py:265-275` 全部转发到它）。而 `LOCK_POLICY` 把 SSE 登记为 `SELF_CONTAINED`，注释写的是"每连接独立 TqApi + CChan，不加锁"——`TqApi` 确实独立，**但喂给 CChan 的记录缓存不是**。

后果（两个浏览器标签同时开同一个合约同一周期，或双窗的上窗与另一连接的下窗撞车时）：
- `set_data()` 后写覆盖前写；
- `append_bar()` 两条连接各追加一根 → **chan 里出现重复 K 线**；
- 任一连接 `finally` 里的 `cleanup_records(code_key)` 直接 `pop` 掉整条 key → 另一条连接的 `last_records()` 突然返回 `[]`，去重判断失效，再次追加重复数据。

修法（小改，风险可控）：把 `_records_by_symbol` 变成 `CTqSdkSession` 的**实例字段**，方法改为实例方法；`DataAPI/CommonStockAPI.py` 里保留类级接口只作兼容。

#### P0-2 · 期货双窗"区间套背驰"100% 静默失效

- 写入方：`App/AppSSE.py:728` `app_data.set_futures_sub_chan(symbol, sub_freq, sub_chan)`
  → 实际 key = `make_futures_sub_key(symbol, sub_freq)` = `"KQ.M@CFFEX.IM:1m"`
- 读取方：`BuySellPoint/BSPointList.py:726-727`
  ```python
  cache_key = f"{parent.code.upper()}:{sub_freq}"
  sub_chan  = app_data.futures_cache_get(cache_key)
  ```
  `parent.code` 是 CChan 的 `code` 属性，由 `AppSSE._build_futures_chan:1118` 定为 `f"{symbol}:{freq_sec}"`
  → 实际 key = `"KQ.M@CFFEX.IM:60:1m"`（多了一级主窗周期秒数）

两者**永远不等**。`BSPointList.py:729` 的调试日志会打一句"期货下窗暂无缓存 → 按子级别背驰处理"然后 `return True`。也就是说：**期货双窗口的区间套背驰判定从未生效过，一直静默降级为"按子级别背驰处理"，用户看到的买卖点比设计少一类。**

`AppData.py:209` 的注释还写着"第三方引擎（BSPointList）按同一格式拼接，兼容不变"——这恰恰说明作者以为两者一致。**这是"注释误导"造成真实 bug 的典型案例。**

修法：让 `BSPointList` 不再自己拼 key，改由 `AppData` 提供 `get_futures_sub_chan(parent_code, sub_freq)` 并在内部做兼容解析；或更简单——`_build_futures_chan` 建 CChan 时 `code=symbol`（去掉 `:freq_sec` 后缀），使 `parent.code` 与 symbol 同形。改完必须补一条断言测试。

#### P0-3 · 盘后下载后分析缓存不失效，且无任何清缓存入口

`DataAPI/ElTdxAPI.py` 里 `grep -n cache` **零结果**——下载任务写完 `.day` / `.lc5` 后，没有任何回调去动 `AppData._stocks_analysis_cache`（LRU 50，每条含完整 `records` + `CChan` 对象）。

后果：用户下载完最新数据 → 回到 K 线图 → 命中缓存 → **看到的还是下载前的数据**，只能等 LRU 淘汰或重启服务。

更糟的是：全项目**没有任何清缓存入口**。最接近的 `/api/stocks/scan/close` → `Scanner.clear_cache()` 是空实现：
```python
def clear_cache(self):
    log.info("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
    return {"cleared": 0}
```
而 `Test/func_map_check.py:229` 还登记着它 "→ FrontAPI + AppData.cache_remove"，注释与实现已分道扬镳。

修法：① 下载任务的 `finally` 里调 `app_data.invalidate_all()`；② 新增 `POST /api/stocks/cleanup`；③ 把 `clear_cache` 的空实现改成真的清或干脆删掉端点。

#### P0-4 · `FrontAPI` 静态资源回退分支会 `AttributeError`

```python
# FrontAPI.py:651-652
log.warning(f"[警告] Frontend/ 目录不存在 ({_frontend_dir})，回退到 OUTPUT_DIR 静态挂载")
app.mount("/", StaticFiles(directory=m.OUTPUT_DIR, html=True), name="static")
```
`AppEngine` **没有** `OUTPUT_DIR` 这个符号（`grep -rn OUTPUT_DIR App/` 零结果；`AppConfig` 里叫 `output_dir` 且是 property）。也就是说：一旦 `Frontend/` 目录缺失，服务不是"降级"，而是**启动即崩**。

讽刺的是，这个 bug 由守护测试间接造成：`Test/test_phase2_guards.py:43` 与 `test_phase4_guards.py:314` 明确禁止 `AppEngine` 里出现 `OUTPUT_DIR` 别名（这个禁令本身是对的），但清理时漏改了消费方。

修法：改成 `app_config.output_dir`，或直接删掉这个回退分支（`Frontend/` 是随仓库发布的，缺失就是部署错误，应当 fail fast 并给明确提示）。

#### P0-5 · SSE 生成器把全局日志级别永久改成 WARNING

```python
# App/AppSSE.py:251-254
logging.getLogger("tqsdk").setLevel(logging.WARNING)
logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
for h in logging.root.handlers:
    h.setLevel(logging.WARNING)     # ← 全局 handler，且从不恢复
```

`DataAPI/TqSdkAPI.py:15-18` 的注释明明白白写着："只抑制 tqsdk 自身 logger，**绝不设置 root 级别**——若设置 root.setLevel(WARNING) 会覆盖 App/AppLog.py 的全局 INFO，导致股票名/PE-TTM/指数归属等 log.info 进度被静默抑制（历史根因）。"

同一个坑在 `AppSSE.py` 里换了个形式（改 handler 而不是 logger 级别），**效果完全相同，而且更隐蔽**：只要建立过一次期货 SSE 连接，此后整个进程的 `log.info` 全部消失（扫描进度、刷新进度、分析耗时全没了），且不可恢复。

修法：把这三行删掉，只保留对 `tqsdk` / `shinny` logger 的抑制（已在 `TqSdkAPI.py` 顶层做过一次，这里根本不需要重复）。

---

### P1 · 架构一致性（应当排期修）

1. **四把锁分散在四个模块，无统一登记，也无强制**。`_ENGINE_LOCK`（AppChart）、`_scan_lock`（AppScan）、`_stock_analysis_lock`（AppEngine）、`_cache_lock`（AppData）。`AppAMO` 为了拿 `_ENGINE_LOCK` 而 `from App.AppChart import _ENGINE_LOCK` —— 一个只读两个指数文件的模块，因此依赖了整个图表域。应抽 `App/concurrency.py`。

2. **`LOCK_POLICY` 只登记不执行**，且登记了 2 个零调用项：`run_analysis` 和 `fetch_and_inject`（见 §6）。守护测试 `test_phase3_guards` 断言"登记项源码里有 `with _ENGINE_LOCK:` 字样"——**这是文本匹配，不是行为验证**。它验证不了"SERIAL 与 SCAN 是否互斥"这类真问题。

3. **`SELF_CONTAINED` 这个分类是错的**。P0-1 证明 SSE 路径并非自包含；此外 `CMyBSPointList.REPLAY_MODE`（第三方类级旗）被 SSE 路径（`AppSSE:317/716/738`，无锁）和股票复盘路径（`AppEngine:676-683`，在 `_stock_analysis_lock` 内）同时读写，可互相污染。

4. **"第三方内核不优化"这个边界在代码上不成立**。`BuySellPoint/BSPointList.py` 里含有大量应用专属逻辑：`_STOCKS_SUB_FREQ_MAP` / `_FUTURES_SUB_FREQ_MAP` / `_KL_TYPE_TO_FREQ` 三套周期映射、股票与期货两套红框算法分派、`app_data` 直连、以及 `chan._stocks_dual_sub_freq` 这种"从 App 层挂到 CChan 对象上的私有属性"。**任何对 `AppData` 的重构都会打到内核**。建议在内核外包一层 `ChanFacade` 收敛这些点，并用测试锁死 `grep -rn "from App" <内核目录>` 为空。

5. **全局中断能力缺失**。`Scanner.abort()`（`AppScan.py:660`）**没有任何调用方**——没有路由、前端也没调（前端只调 `/api/stocks/scan/{task_id}/cancel`）。后果：
   - `_scan_aborted` 恒为 `False`，`AppScan.py:422/426/639` 三处分支**永不可达**；
   - `AppScan.py:129` 的 `abort_check=lambda: _scan_aborted` 永远返回 False → **成分股抓取（akshare 网络阻塞）期间点击中断无效**；
   - `ScanStore.abort_all_running()` 连带成为死代码，而它的注释还在描述一个已不存在的路由 `/api/scan_abort`。
   要么接上路由，要么删干净。

6. **前端组件注册表是纯写入的死结构**。`ChanApp.components` 11 处赋值、0 处读取；跨组件调用实际全部走 `window.*` 全局（`app.js` 里 60+ 处 `window.xxx = function`，`index.html` 里 40+ 处内联 `onclick`）。注册表只被 `test_frontend_smoke` 用正则"确认字符串存在"——**这个测试守护的是一个没人用的东西**。要么让调用真的走注册表，要么删掉注册表及其测试项。

7. **`doStartScan` 717 行、4 个近乎复制的分支**。`fx_d`(5166-5321) / `ma`(5323-5471) / `fangliang`(5472-5580) / `bsp`(5619-5803)，每个分支都在函数内重新定义 `finishScan` / `updatePanel` / `_doUpdatePanel` 三个闭包（共 12 个近似副本）。差异只有三处：`mode` 字符串、结果分类谓词、渲染函数。可收敛为一个 `runScan({mode, classify, render})`。

8. **下载完成后无回写通知**（同 P0-3，此处指架构层面）：`AppDownload` 完全不知道 `AppData` 的存在，也就不可能做失效。应在用例层编排"下载完成 → 缓存失效 → 前端提示"。

---

### P2 · 冗余与注释错误（可批量清理）

#### 死代码（AST 扫描确认：定义处之外零引用）

| 符号 | 位置 | 规模 |
|---|---|---|
| `run_analysis` | `AppChart.py:67` | 21 行，异步入口，零调用（连 LOCK_POLICY 都给它登了记） |
| `fetch_and_inject` | `AppChart.py:492` | 13 行，零调用（同样在 LOCK_POLICY 里） |
| `get_saved_point_times` | `AppChart.py:634` | 4 行；注释称"FrontAPI 经此只读访问"，实际 FrontAPI 没用 |
| `get_saved_point` | `AppChart.py:676` | 6 行；`AppSSE` 另写了一个私有的 `_get_saved_point`（重复实现） |
| `futures_cache_get/put/pop`（漏斗） | `AppChart.py:640-655` | 18 行；`AppSSE` 直接用 `app_data.*` |
| `futures_set/get/pop_sub_chan`（漏斗） | `AppChart.py:658-673` | 21 行；同上 |
| `get_stock_market_code` / `get_market_code` / `get_stock_name`（漏斗） | `AppChart.py:729-741` | 15 行；只在文件头注释里被提到 |
| `start_download`（非 checked 版） | `AppDownload.py:57` | 7 行；前端只用 `start_download_checked` |
| `eltdx_available` / `download_dir` | `AppDownload.py:21-31` | 10 行 |
| `ths_cloud_available` | `AppScan.py:223` | 3 行 |
| `CTdxAPI_Sliced` 类 | `TdxAPI.py:1393` | 43 行；注释称"用于双击选点后重新计算"，但选点走的是 `_analyze_stock_internal(start_time=...)` 重建，从不实例化它 |
| `DEFAULT_FUTURES_SYMBOLS` | `TqSdkAPI.py:93` | 18 行数据 |
| `FUTURES_DUAL_REVERSE_MAP` | `TqSdkAPI.py:192` | 5 行 |
| `FUTURES_DUAL_FREQ_MAP` | `TqSdkAPI.py:185` | 5 行（`App/utils.py` 另有一份同名常量，两处定义） |
| `HK_CODE_PREFIX` / `DS_CODE_PREFIX` / `TDX_MARKET_MAP` | `AppEngine.py:193-201` | 7 行 |
| `_get_float_mc_from_cache` / `_load_pe_ttm_cache` / `_update_float_mc_cache` / `_save_point_time` / `_cache_remove`（引擎委托壳） | `AppEngine.py:242-330` | 20 行；`AppData` 拆分后已无人消费 |
| `read_zz1000/sz50/hs300/zz500_stocks` | `TdxAPI.py:1530-1575` | 40 行 |
| `current_trace_id` | `AppLog.py:64` | 3 行 |
| `step` 参数全链路 | `FrontAPI:233` / `AppChart:50,90,492` / `AppEngine:340,529-546` / `AppSSE:205` | 约 40 行 |
| `Scanner.clear_cache` 空实现 | `AppScan.py:679` | 4 行 |
| `Scanner.abort` + `ScanStore.abort_all_running` | `AppScan.py:660` / `AppScanStore.py:208` | 30 行 |

合计约 **350 行 + 18 行数据** 可直接删除；若把 `step` 与两个未接线的端点算上，约 400 行。

#### `step` 参数：注释、代码、前端三处不一致

- `FrontAPI.py:233` 声明 `step: str = Query(None)`
- `AppEngine.py:529-546` 有完整的"箭头步进"实现（约 18 行）
- `AppSSE._truncate_records_by_end` 也有 `step` 分支，但**所有调用点都不传**
- `Frontend/app.js:3927` 注释："复盘由 gotoDate 的 SSE 软断开（end_time）统一承载，**箭头逐根步进不再需要**"
- 但实际 `sse_futures_stream_single/dual` **根本没有 step 形参**，前端也从来不传 `step=`

即：**前端已明确废弃该功能，后端保留完整实现，测试里冻结了它的快照**（`snapshot_runner.py:293` `step=-5`、`test_trigger_step_replay.py` 全套）。测试在守护一个用户触发不到的路径。

#### 注释与代码冲突清单（12 处）

| 位置 | 注释说 | 代码是 |
|---|---|---|
| `AppEngine.py:17` | "期货分流：analyze_stock 延迟导入 AppSSE" | AppEngine 全文无 `AppSSE` 字样；期货是被直接拒绝 |
| `AppEngine.py:106`（`analyze_stock` 是统一入口） | 引擎调用一律经此 | `AppChart.stock_manual_select_point:341` 直调 `_m._analyze_stock_internal` |
| `AppData.py:209` | "BSPointList 按同一格式拼接，兼容不变" | 两者 key 不一致（P0-2 根因） |
| `AppSSE.py:57` + `LOCK_POLICY` | SSE "每连接独立 TqApi+CChan，不加锁" | records 缓存是类级共享（P0-1） |
| `TqSdkAPI.py:15-18` | "绝不设置 root 级别" | `AppSSE.py:253` 设了 root handler 级别（P0-5） |
| `AppDownload.py:34` | "GET/POST 双入口共用" | 只有 `POST /api/stocks/download/start`；GET 入口已删 |
| `func_map_check.py:229` | `/api/stocks/scan/close` → `AppData.cache_remove` | 实现是 `{"cleared": 0}` 空操作 |
| `AppScanStore.py:212` | "/api/scan_abort 无 task_id 参数" | 路由早已改为 `/api/stocks/scan/{task_id}/cancel` |
| `func_map_check.py:192` | `SYMBOL_CODE` → "RETIRE，随 main 下线" | `SYMBOL_CODE` 被 `FrontAPI` 的 `/api/health` 与启动日志实际使用 |
| `func_map_check.py:192` | `SCRIPT_DIR` → "随 do_GET 3a 删除" | 仍用于 `sys.path` 引导，是活的 |
| `FrontAPI.py:254` | "非复盘、非双窗口下窗、非期货"才持久化 | analyze 路径根本到不了期货（会被 `analyze_stock` 拒绝），条件恒真的部分无意义 |
| `AppSSE.py:725` | `sub_chan, sub_records, sub_kl_type, _ = sub_result` | 第二个变量实际收到的是 `klines` DataFrame，不是 records（命名错误，恰好未使用该变量） |

#### 其他

- **`func_map_check.py` 是负资产**：它校验"冻结 JSON 里每个 AppEngine 顶层函数的行号与当前代码一致"，且要求函数总数相等。这意味着**往 `AppEngine.py` 里加一个函数、或者在它上方插入任何一行，都会让 CI 失败**。迁移早已完成（`TARGET_CLASSES` 为空、`TARGET_ROUTES` 全是"✓已迁"），这个守护器已经从"迁移导航仪"变成"改动税"。建议下线，或改为只保留"孤儿检测"。
- **两个最有价值的测试没接进 CI**：`Test/test_app_amo.py`（335 行，真行为测试）与 `Test/test_stocks_dual_algo.py`（214 行，纯函数 4 向公式单测）**都不在 `run_all.py` 的 21 个组件里**。`run_all` 装的 21 个里，绝大多数是正则/AST 文本守卫。测试金字塔是倒的。
- **`Docs/` 21,408 行 HTML 与 `Test/` 下两份 HTML 完全重复**（`chan-migration-map.html`、`chan-interface-contract.html` MD5 相同）。
- **`.gitignore` 有 `App/*.json`，但 4 个 JSON 缓存已被 git 跟踪**（`stock_names.json` 623 KB 等），规则对已跟踪文件无效，每次刷新都会产生大 diff——正是该注释想避免的问题。需 `git rm --cached`。
- **前端 `VIEW_COUNT` 默认 377，后端 `app_config.view_count` 默认 233**。`/api/health` 成功时会被覆盖，失败时两者行为不一致。
- **`.env.example` 里 `App/ths_*.py` 路径已过期**（脚本在 `Script/`），且 `SCAN_MIN_FLOAT_MC` / `VIEW_COUNT` / `DUAL_SUB_FALLBACK_MIN` / `MAX_DUAL_CACHE_KEYS` / `MAX_STOCKS_SUB_CHAN` / `STOCKS_LOOKBACK_CONFIG` / `FUTURES_LOOKBACK_CONFIG` / `SSE_DEBUG` / `DEBUG_COLD_START_*` / `FORWARD_ADJUST_ENABLED` / `FULL_DATA_MODE` **全部未在 `.env.example` 中登记**。更要紧的是：降级解析器的 `_FIELD_TYPES` 里没有这些 dict 型字段，所以**没有 pydantic-settings 时它们根本无法配置**。
- **`/api/futures/read/config` 与 `/api/futures/read/status` 无前端调用**。后者是硬编码桩：`return {"ok": True, "architecture": "self-contained"}`。

---

## 6. 落地路线（按性价比排序）

### 第一批：止血（约 1 天，纯删除与改常量）

1. 删 `AppSSE.py:251-254` 三行 root 日志级别改动。
2. `FrontAPI.py:652` 的 `m.OUTPUT_DIR` → `app_config.output_dir`（或直接 fail fast）。
3. 统一期货下窗缓存 key：改 `BSPointList.py:726` 用 `app_data.get_futures_sub_chan` 的兼容解析，或改 `_build_futures_chan` 的 `code=symbol`。**改完立刻补一条"双窗建流后 `check_nested_diver` 能取到下窗"的断言测试**。
4. `CTqSdkAPI._records_by_symbol` → 实例字段（连带 `CTqSdkSession` 的方法改实例方法）。

### 第二批：接上断掉的线（约 2 天）

5. 下载任务 `finally` → `app_data.invalidate_all()`；新增 `POST /api/stocks/cleanup`。
6. `Scanner.abort()` 接一个 `/api/stocks/scan/cancel` 路由，前端在"中断扫描"时先调它再调 task cancel；或整条删除（连同 `abort_all_running`、三处不可达分支、`abort_check`）。
7. 把 `test_app_amo.py` / `test_stocks_dual_algo.py` 注册进 `run_all.py`，并删掉 `test_phase6_guards` 中守护 `ChanApp.components`（无人消费）的那一项——或让跨组件调用真的走注册表。

### 第三批：结构收敛（约 1～2 周，可分期）

8. 抽 `App/concurrency.py`，把 4 把锁集中；`AppAMO` 不再依赖 `AppChart`。
9. 删死代码（上表约 400 行）+ 删 `step` 全链路 + 删 `CTdxAPI_Sliced` + 删两个未调用的 futures 端点。
10. 引入 `Window` 值对象，收敛 `end_date/start_time/step/dual/sub_freq`。
11. 抽 `ChanFacade` 包住缠论内核，把 `chan._stocks_dual_sub_freq`、`REPLAY_MODE` 类旗、子级别数据注入全部收敛到这一层；用测试锁死内核目录 `from App` 为空。
12. `doStartScan` 的四分支合一。
13. `git rm --cached App/*.json`；清理 `Docs/`（保留最新版 1～2 份，其余移到 `Docs/archive/` 并加 README 说明）；`.env.example` 补全。
14. 下线 `func_map_check.py` 的行号漂移校验（保留孤儿检测即可）。

---

## 附：评审方法

- 逐文件通读：`FrontAPI.py`、`App/*.py` 全部 15 个模块、`DataAPI/{TdxAPI,TqSdkAPI,TqSdkCSSESource,CommonStockAPI,ElTdxAPI}.py`、`Frontend/{app.js,index.html}`、`Test/run_all.py` 及代表性守卫用例。
- AST 扫描：自定义脚本统计 `App/`、`DataAPI/` 顶层函数/类/常量在所有生产代码（含前端 JS/HTML）中的引用数，筛出"定义文件之外零引用"的符号。
- 依赖图：解析 import 语句（区分顶层与函数内惰性 import），识别环。
- 调用点核对：`grep` 定位每个 `AppOrch.__all__` 导出项的真实调用点，逐个确认是否为空。
- 所有结论均以代码为据，注释仅作为对照项列出。
