# `Backtest/Exp` —— 入出场回测实验台

这里**不是**单元测试，是"验证一条交易规则到底有没有用"的实验脚本。
单测在 `Backtest/Test/`。

实验分两条**互不干扰**的流水线 —— 这是本目录唯一的组织原则：

| 目录 | 回答什么问题 | 文档 |
|---|---|---|
| `entry/` | **入场**：缠论买卖点信号本身有没有优势？（哪个周期、哪类信号） | `Docs/回测方案/入场信号回测.md` |
| `exit/` | **出场**：给定信号，止盈止损参数怎么设？ | `Docs/回测方案/出场策略回测.md` |
| `common/` | 两条流水线共用的**取数层**与护栏 | — |

> 为什么要分开：出场参数（保本/跟踪门槛）是个**无底洞**，把它和入场验证搅在一起，
> 会让人分不清"这个回测是在验信号还是在调参数"。所以**代码分目录、文档分文件**。

---

## ⚠ 铁律：回测的 K 线必须与页面逐根相同

页面/实盘链路（`App/AppEngine._analyze_stock_internal`）读的是**通达信本地 vipdoc**，
并在取数阶段做**自实现前复权**（按 xdxr 事件从最新往旧递推）：

```
CTdxAPI.fetch_main_level(market, code, freq)          ← DataAPI/TdxAPI.py
  → w   由前复权**日线**合成（_resample_day_to_week）
    30m / 15m 由前复权 **5m** 合成
    d / 5m 直接读 .day / .lc5
  → _forward_adjust(records, market, code, end_date)
  → tdx_data_context(records) → CChan(data_src="custom:TdxAPI.CTdxAPI", autype=AUTYPE.NONE)
```

本目录**唯一允许**的取数入口是 `common/tdx_source.py`（它直接调同一个
`CTdxAPI.fetch_main_level`）。`CChan` 的构造参数（`autype=AUTYPE.NONE` +
`CChanConfig()` 全默认）由 `Backtest/Test/test_bt02_config_contract.py` 钉住，与页面逐字段相等。

**但"调同一个函数"还不够 —— 还有第二刀：页面加载窗口。**
页面喂给 `CChan` 的不是 vipdoc 全量：`AppEngine._analyze_stock_internal` 会
① 优先按 `App/double_click_dt.csv` 的**双击选点**截左边界（截了就不再按根数限），
否则 ② 按 `AppConfig.STOCKS_LOOKBACK_CONFIG` 取**末尾 N 根**
（本机：`w` 不限制 / `d` **500** / `30m` **800** / `15m` **960** / `5m` **960**）。
`tdx_source.page_window()` 复刻这一刀，**上限与选点都现读 App 层**（不抄数字）。

| 周期 | 页面（= 回测） | vipdoc 全量 |
|---|---|---|
| 周K | 294 | 294 |
| 日K | **500** | 1 393 |
| 30分 | **800** | 1 936 |
| 15分 | **960** | 3 872 |
| 5分 | **960** | 11 616 |

> 忘了这一刀＝笔/段起点错位＝整条结构全变，和"换数据源"是同等级的错。
> 只想拿全量做敏感性对照时用 `EXP_FULL_DATA=1`（或 `set_page_window(False)`），
> **不得**用它出结论。详见 `Docs/回测方案/入场信号回测.md` §1.2。

**不用它会发生什么**（实测，见 `common/data_parity.py` 与 `参照日志/log_data_parity.txt`）：

| 项 | 页面（vipdoc） | 旧外部链路（腾讯/新浪） | 后果 |
|---|---|---|---|
| 日K 根数 | 1393 | 800（腾讯，可指定） | 笔数差中位 **−43**；买卖点差中位 −2 |
| 周K 根数 | **294**（由日线合成） | 621~800（腾讯原生周线） | 笔数差中位 **+25**；收盘价差 >1% 占 **6.2%** |
| 分钟线复权 | **前复权** | 新浪**不复权** | 跨分红日多一次跳空 |

> 笔/段是**从序列起点**递推出来的 ⇒ 起点不同，早期的笔就不同，买卖点也会变。
> 所以"只差一点点"是错觉：它换掉的是整条序列的结构。

`common/fetch_kline.py` / `fetch_min.py` / `fetch_fut.py` 保留在库里，
但**只能**被 `common/data_parity.py` 用来做对照，**不得**作为任何结论的依据。

### 数据快照（xdxr）

前复权要 `get_xdxr_data()`，那是 **eltdx 网络调用**（通达信 7709），被全局锁串行化、
单只 ~0.4s、且实测偶发挂起。`tdx_source` 的做法是**一次性抓下来冻结到磁盘**
（`common/.kcache_tdx/xdxr/`，900 只约 6 分钟），之后所有进程从快照读：

```bash
python Backtest/Exp/common/tdx_source.py --xdxr 900     # 断点续跑，已抓的跳过
```

语义 = 把 eltdx 当时返回的 `DataFrame` **原样 pickle 下来**，之后所有进程读本地文件
（替换点是 `DataAPI.TdxAPI` 的**模块属性** `get_xdxr_data` ——
`TdxAPI.py` 用 `from DataAPI.ElTdxAPI import get_xdxr_data` 绑定成了模块属性，
改 `ElTdxAPI` 侧无效）。等价于"页面此刻去问 eltdx"，
差异只在抓取之后再发生的新除权事件 —— 而那类事件比窗口内所有 K 线都新，
是一张**统一的正仿射映射**，保大小关系 ⇒ **笔/段/买卖点不变**。

**实测证据**（`common/xdxr_parity.py`，现场再问一次 eltdx 与快照逐项对拍）：
800/800 有文件（0 缺）、791 只有除权记录、9 只真空；抽 6 只 **内容全列相等**、
且两条 xdxr 各做一遍前复权后 **笔数与各类型买卖点数逐类相同**。

> ⚠ **快照为空 ⇒ 前复权被静默跳过** —— 根数、日期范围看起来都对，但长期高分红股的
> 价格序列整个变样。`install_xdxr()` 现在会在快照为空时往 **stderr** 打一条醒目告警
> 并给出要跑的命令。**看到那条告警就别看结论。**

---

## 目录树

```
Backtest/Exp/
├── README.md                ← 本文件
├── common/                  ← 两条流水线共用
│   ├── tdx_source.py        ★ 页面同源取数（唯一允许的入口；含 page_window 第二刀）
│   │                          取值入口的唯一事实源：`records()` / `recs_src()` / `ext_records()`
│   ├── page_kline.py          旧 fetch_kline 接口的页同源薄壳（签名兼容）
│   ├── data_parity.py         数据同源核验（给"到底差多少"出证据）
│   ├── xdxr_parity.py         xdxr 快照 ⟷ 现场 eltdx 对拍（内容 + 结构）
│   ├── exp_policy.py          出场策略覆盖层（只被 exit/ 的 A/B 用）
│   ├── bench_pool.py          并行基准（线程池 vs 进程池）
│   ├── verify_equiv.py        exp_policy 与真实策略的等价性护栏
│   ├── fetch_kline.py         旧外部链路（腾讯）—— 只被 `tdx_source.ext_records()` 调
│   ├── fetch_min.py           旧外部链路（新浪，不复权）—— 同上
│   ├── fetch_fut.py           期货取数（Phase 2 用，暂不参与股票回测）
│   └── .kcache/_universe.json 股票池快照（固定种子打散，复现凭据）
├── entry/                   ← 入场信号质量
│   ├── signal_quality.py    ★ 三口径主脚本
│   ├── type1_oos.py         ★ 1 类专项样本外（按标的切 + 按时间切）
│   ├── tab_cycle.py           周期 × 信号类型 交叉表
│   ├── sweep_regret.py        被扫率 / 浮盈回吐率
│   └── layer_activation.py    各周期百分比止盈层触发率
├── exit/                    ← 出场策略调优
│   ├── exit_fate.py         ★ 结构止损两族分解（真实策略逐根回放）
│   ├── be_ab.py               保本门槛端到端 A/B
│   ├── ab_run.py              A/B 引擎（run_plan/metrics）——6 个脚本共用
│   ├── ab_pct.py              百分比口径 A/B + 配对 bootstrap
│   ├── ab_atr.py              ATR 口径 A/B
│   ├── ab_final.py / ab_run2.py
│   ├── atr_scale.py           跨周期波动标定（5% 相当于几个 ATR）
│   ├── signif.py              配对显著性
│   ├── batch_stats.py         基线批量统计
│   ├── prefetch.py            取数预热（本机文件读取）
│   └── case_*.py / diag_r.py / replay.py   个案追踪与复现
└── 参照日志/ 参照结果/        基线跑分原始日志与聚合结果
```

> `common/verify_equiv.py`（`exp_policy` 与真实策略的等价性护栏）**被 `exit/` 调用**，
> 所以它住在 `common/`，`exit/` 里没有副本。

---

## 怎么跑

前置：解释器用**仓库自带的**（需要 `eltdx`）：

```bash
D:\CChan\.venv\Scripts\python.exe Backtest/Exp/entry/signal_quality.py --freq d --limit 800 --procs 8
```

脚本都在 `__file__` 处向上找仓库根，放在仓库里就能直接跑。
若要在仓库**外**的副本上跑，设 `CHAN_REPO=D:\CChan`。

### 并行化（硬规则）

chan.py 的 `step_load` 是**纯 Python CPU 活** ⇒ **必须用进程池**，线程池会被 GIL 串行化：

| 并发方式 | 160 只 × 周K 墙钟 | 相对串行 |
|---|---|---|
| 线程 1 个 | 20.6s | 1.00× |
| 线程 12 个 | 22.1s | **0.93×（更慢）** |
| 进程 8 个 | **6.8s** | **3.05×** |

本机 20 逻辑核，**甜点 8~10 个进程**（16 个反而变慢）。

> ⚠ **取数也一样，它也不是 I/O。** `tdx_source.records()` 是「读本地文件 + 前复权 +
> 周期重采样」的**纯 Python CPU** 活（30m/15m 还要先把整条 5m 读进来再重采样），
> 所以**同样**只能靠进程池：
>
> | 场景 | 线程池 | 进程池 |
> |---|---|---|
> | 30m × 60 只（质量门） | `--workers 1` **12s** / `--workers 12` **14s**（零收益） | **6s** |
> | 30m × 800 只（质量门） | 单只 ~0.20s 线性累加 ⇒ ~160s | **48s** |
>
> 单只成本随规模**线性**（300 只 59.7s；末 50 只 / 前 50 只 = **1.04×**），
> 所以「跑得越久越慢」不是内存问题，纯粹是没用上多核。
> 本轮已把 `entry/signal_quality.py`、`exit/exit_fate.py` 的质量门、
> `exit/atr_scale.py` 的取数循环、`tdx_source.prefetch()` 全部改成进程池，
> 并删掉了那两个误导性的 `--workers` 参数。
>
> 也别指望"质量门预热"能加速随后的扫描 —— 扫描跑在**另一个**进程池里
> （`spawn` 的子进程各自持有独立的 `tdx_source._MEMO`），预热只是**预筛可用标的**。

> ⚠ `exp_policy.install()` 改的是**父进程**的模块属性，`spawn` 的子进程**不继承**
> ⇒ 子进程会静默跑**原始策略**（结果看起来像基线，还不报错）。
> 防线：进程池 `initializer` 里重装 + 首轮用 3 只标的与线程版**逐笔对拍**。

### 取数内存（硬规则）

**扫全池时一律用 `tdx_source.records(code, freq, use_cache=False)` 取数。**

`records()` 默认会把结果 memo 起来，等于把**每只标的的整段 K 线**留在内存里。
实测 `exit/atr_scale.py` 跑到日K 时 RSS 涨到 **1.8GB**、输出停滞 15 分钟以上，
5m 更糟。改用 `use_cache=False` 后整表约 6 分钟跑完。
（`raw_records()` 是"绕过页面窗口取全量"，只给对照用。）

> 本轮补记：`atr_scale.py` 的串行 `for` 循环已改**进程池** —— 每个子进程只装
> 自己那一小段 code 的 K 线，memo 累积不起来，所以 `use_cache=False` 照旧保留；
> 五个周期合计从 ~10 分钟降到 ~2 分钟。

---

## 交付边界

- 本目录脚本只做**实验**，不进 `Trading/`、不改生产默认值。
- `exp_policy.ExpExitPolicy` 只在实验进程内 monkey-patch，且默认**全部开关关闭**，
  与真实 `LayeredExitPolicy` 的等价性由 `verify_equiv.py` 钉住。
- `.kcache*/`（K 线缓存 + xdxr 快照，约 60MB）**不入库**，唯一例外是
  `common/.kcache/_universe.json`（53KB，复现同一股票池的唯一凭据，`.gitignore` 里有取反规则）。
- **入库的产物**：脚本、`参照日志/*.txt`（各脚本的 stdout 原文）、`参照结果/*.json`（聚合结果）。
  两者都是"**当前这一版数据口径**"的跑分留档 —— **数据口径一改（换源 / 改窗口 / 改股票池）
  就必须同步刷新**，否则它们会和两份文档的数字打架。
- ⚠ **本轮留档另有一个"代码口径"前提**：`参照日志/` `参照结果/` 与两份文档都测于提交
  `a790959`，而该提交下 `BuySellPoint/BSPointList.py` 的 `_cal_bs0point_4th` /
  `_cal_bs0point_nth` 各有一处裸 `return`（`27a83bf` 引入）⇒ **0 类两支生成器被短接**。
  当前 HEAD `98d0284` 已删除这两行、**生成器恢复** ⇒ 两份文档里受影响的行都打了 **⚠ 待重测**，
  `_rerun.sh` 整套重跑排下一轮。机制、影响面与安全边界见两文档的顶部警示块与 §9/§10 勘误。
- `Backtest/Exp/_rerun.sh`（**就在本目录下**，不在仓库根）：一键把全部跑分重算一遍
  （stdout 直接落到本目录 `参照日志/`，聚合 json 归档到 `参照结果/`）。
  默认跳过两个联网段，加 `WITH_NET=1` 才跑 `data_parity.py` + `xdxr_parity.py`。
  它**按自身位置推仓库根**（`$0` 的上两级），所以放在 `Backtest/Exp/` 下就能直接跑，
  不再依赖"必须放仓库根"；整份拷到仓库外时才需要 `CHAN_REPO=<仓库根>`。
- **`.gitignore` 的中间产物规则已随交付提供**（仓库根 `.gitignore` 里，
  文件末尾「===== 回测实验（Backtest/Exp）中间产物 =====」那一段，7 条规则 + 5 行注释）——
  `_rerun.sh` 会在 `entry/` `exit/` 留下中间产物，否则每次重跑都脏 `git status`；
  同时那一段把「`参照日志/` `参照结果/` 才是入库快照」写进了注释，免得下次又有人把两者搞混：

  ```gitignore
  # ===== 回测实验（Backtest/Exp）中间产物 =====
  # 每次重跑覆写的逐笔明细与逐轮扫描（合计约 10MB），入库会让每次回归都产生无意义 diff。
  # 留档在 Backtest/Exp/参照日志/*.txt 与 Backtest/Exp/参照结果/*.json —— 那两处**是入库的**
  # 「当前数据口径的跑分快照」，口径一改（换源 / 改窗口 / 改股票池）必须整套刷新，
  # 详见 Backtest/Exp/README.md 的「交付边界」。
  Backtest/Exp/entry/sq_*.json
  Backtest/Exp/exit/fate_*.json
  Backtest/Exp/entry/sweep_regret.json
  Backtest/Exp/entry/layer_activation.json
  Backtest/Exp/exit/be_ab.json
  Backtest/Exp/exit/atr_scale.json
  Backtest/Exp/exit/ab_atr.json
  ```
