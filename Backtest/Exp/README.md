# `Backtest/Exp` —— 入出场回测实验台

这里**不是**单元测试，是"验证一条交易规则到底有没有用"的实验脚本。
单测在 `Backtest/Test/`。

实验分两条**互不干扰**的流水线 —— 这是本目录唯一的组织原则：

| 目录 | 回答什么问题 | 文档 |
|---|---|---|
| `entry/` | **入场**：缠论买卖点信号本身有没有优势？（哪个周期、哪类信号） | `Docs/止盈止损/入出场回测/入场信号质量回测.md` |
| `exit/` | **出场**：给定信号，止盈止损参数怎么设？ | `Docs/止盈止损/入出场回测/出场策略回测.md` |
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
`CChanConfig()` 全默认）由 `Test/test_bt02_config_contract.py` 钉住，与页面逐字段相等。

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

语义 = 与冻结的 vipdoc K 线（末日 2026-09-30）配对的一份**数据快照**，
等价于"页面此刻去问 eltdx"，差异仅在抓取之后再发生的新除权事件。

> ⚠ **快照为空 ⇒ 前复权被静默跳过** —— 根数、日期范围看起来都对，但长期高分红股的
> 价格序列整个变样。`install_xdxr()` 现在会在快照为空时往 **stderr** 打一条醒目告警
> 并给出要跑的命令。**看到那条告警就别看结论。**

---

## 目录树

```
Backtest/Exp/
├── README.md                ← 本文件
├── common/                  ← 两条流水线共用
│   ├── tdx_source.py        ★ 页面同源取数（唯一允许的入口）
│   ├── page_kline.py          旧 fetch_kline 接口的页同源薄壳（签名兼容）
│   ├── data_parity.py         数据同源核验（给"到底差多少"出证据）
│   ├── exp_policy.py          出场策略覆盖层（只被 exit/ 的 A/B 用）
│   ├── bench_pool.py          并行基准（线程池 vs 进程池）
│   ├── verify_equiv.py        exp_policy 与真实策略的等价性护栏
│   ├── fetch_kline.py         旧外部链路（腾讯）——**仅 data_parity 用**
│   ├── fetch_min.py           旧外部链路（新浪，不复权）——**仅 data_parity 用**
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

本机 20 逻辑核，**甜点 8~10 个进程**（16 个反而变慢）。取数（I/O）才用线程池。

> ⚠ `exp_policy.install()` 改的是**父进程**的模块属性，`spawn` 的子进程**不继承**
> ⇒ 子进程会静默跑**原始策略**（结果看起来像基线，还不报错）。
> 防线：进程池 `initializer` 里重装 + 首轮用 3 只标的与线程版**逐笔对拍**。

### 取数内存（硬规则）

**单进程脚本扫全池时，一律用 `tdx_source.records(code, freq, use_cache=False)`。**

`records()` 默认会把结果 memo 起来，等于把**每只标的的整段 K 线**留在内存里
（日K 800 只 ≈ 1.1M 个 dict）。实测 `exit/atr_scale.py` 跑到日K 时 RSS 涨到 **1.8GB**、
输出停滞 15 分钟以上，5m（300 × 11 616 根）更糟。改用 `use_cache=False` 后整表约 6 分钟跑完。

**进进程池的脚本不受影响**（每个子进程只装自己那一份），它们沿用自己的取法即可。

---

## 交付边界

- 本目录脚本只做**实验**，不进 `Trading/`、不改生产默认值。
- `exp_policy.ExpExitPolicy` 只在实验进程内 monkey-patch，且默认**全部开关关闭**，
  与真实 `LayeredExitPolicy` 的等价性由 `verify_equiv.py` 钉住。
- `.kcache*`、`参照结果/*.json` 里的大文件不入库；入库的是脚本、`参照日志/*.txt`
  与 `common/.kcache/_universe.json`（53KB，复现同一股票池的唯一凭据）。
