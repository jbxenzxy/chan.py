# SimNow 自动下单实施指南

> 目标：用 chan.py 的缠论买卖点信号，驱动 SimNow 仿真账户自动开仓（买点开多、卖点开空）。
> 本文档是**唯一**的操作手册，取代之前分散的多个 README。

---

## 0. 一句话总览

chan.py 负责「看盘 + 算信号」，我交付的三个工具负责「执行」。三者是**独立进程**，互相解耦：

| 工具 | 干什么 | 是否下单 | 依赖 |
|---|---|---|---|
| **M0 信号录制器** `trader_gateway/tools/m0_recorder` | 只录信号不下单，量化信号重绘率/振幅/期望 | ❌ 绝不下单 | 仅标准库 |
| **M2a 探针** `trader_gateway/tools/simnow` | 一次性验证 SimNow 通道通不通 | 可选下一笔测试单 | tqsdk |
| **交易网关** `trader_gateway` | 信号→策略→风控→下单 的完整引擎 | ✅ 可 dry_run / simnow | 标准库 + (simnow 时) tqsdk |

> 关键认知：这三个工具**没有修改 chan.py 任何源码**，它们是放在 chan.py 目录旁、独立运行的程序。它们只通过 HTTP 订阅 chan.py 已有的 SSE 接口。

---

## 1. 交付物全清单（照此核对，不遗漏）

```
C:\my_chan_project\
├── (你的 chan.py 源码，原样未动)
├── trader_gateway/tools/m0_recorder\          ← 工具①，共 2 个 .py（signal_recorder.py / analyze.py）
├── trader_gateway/tools/simnow\            ← 工具②，共 1 个 .py（simnow_probe.py）
└── trader_gateway\              ← 工具③，共 27 个源文件（tg/ 包 + run_gateway.py + 配置 + demo 数据）
```

三个工具各自的内容（文件级）：

**① trader_gateway/tools/m0_recorder/**（2 文件）
- `signal_recorder.py` —— 订阅 SSE 录制信号
- `analyze.py` —— 分析重绘率 / 振幅分布 / 参数期望

**② trader_gateway/tools/simnow/**（1 文件）
- `simnow_probe.py` —— 验证 SimNow 通道

**③ trader_gateway/**（27 文件）
- `run_gateway.py`（CLI 入口）
- `config.example.json`（配置模板）、`config_trailing.json`（移动止损示例）
- `replay_data/`（demo 回放数据）、`tools/make_demo_data.py`
- `tg/` 包：`types.py`、`config.py`、`events.py`、`store.py`、`symbols.py`、`risk.py`、`engine.py`
- `tg/strategy/`：`base.py`、`default_policy.py`、`example_trailing.py`
- `tg/brokers/`：`base.py`、`dry_run.py`、`simnow.py`
- `tg/sources/`：`base.py`、`sse_source.py`、`replay_source.py`

---

## 2. 正确执行顺序（总路线图）

```
阶段 A（可选但推荐）  M0 录制 1~2 周真实信号
         ↓   目的：确认信号重绘率 + 振幅 + 这组止盈止损参数的期望是正还是负
         ↓   ★ 若负期望，M0 就是最便宜的一次止损，后面都不用做
阶段 B                M2a 探针：验证 SimNow 通道（连上 → 查主力合约 → 下一笔测试单）
         ↓   目的：30 秒把「账号错/权限/时段/天勤中转」四个坑一次性暴露
阶段 C                M2b 网关：--broker dry_run 回放验证 → --broker simnow 真实仿真下单
         ↓   目的：先 dry_run 离线把管道跑通，再切 simnow 发真（仿真）单
阶段 D（未来）        M3 接创元期货实盘（需向期货公司申请程序化 AppID/AuthCode）
```

**你现在的状态**：A 未开始，B 未跑，C 的 trader_gateway 已就位（dry_run 可跑，simnow 未验证通道）。

---

## 3. 工具①：M0 信号录制器（先做，推荐）

**放哪**：`C:\my_chan_project\trader_gateway\tools\m0_recorder\`（独立目录，不进 chan.py 源码）

**怎么跑**（用项目自带 `.venv` 的 python，无需装任何东西）：

```bat
:: ① 先启动 chan.py 的 API 服务（另开一个终端）
cd C:\my_chan_project
.venv\Scripts\python.exe FrontAPI.py

:: ② 再开一个终端，长期挂着录制 IF 5 分钟信号
cd C:\my_chan_project\trader_gateway\tools\m0_recorder
..\.venv\Scripts\python.exe signal_recorder.py --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./out_if5m
```

> 默认连 `http://127.0.0.1:18081`，端口不对就加 `--base http://127.0.0.1:<端口>`。

**③ 攒够 ≥5 个交易日 / ≥50 条信号后分析**：

```bat
cd C:\my_chan_project\trader_gateway\tools\m0_recorder
..\.venv\Scripts\python.exe analyze.py --out ./out_if5m --take-profit 10 --scan
```

**看什么**：① 重绘率（信号消失/位移比例）② 信号 K 线振幅分布（决定止损距离→保本胜率）③ 那组参数的真实期望（含平今费+滑点）。

---

## 4. 工具②：M2a SimNow 探针（接实盘前做）

**放哪**：`C:\my_chan_project\trader_gateway\tools\simnow\`（独立目录）

**凭据**：你已用图形界面设好系统环境变量（`SN_ACCOUNT` / `SN_PASSWORD` / `TQ_ACCOUNT` / `TQ_PASSWORD`），所以**不需要**再打 `set` 命令。

> ⚠️ 用图形界面设的变量，**必须重开终端**（或重启 IDE）才能读到；之前已经开着的窗口不会刷新。

**怎么跑**：

```bat
cd C:\my_chan_project\trader_gateway\tools\simnow
..\.venv\Scripts\python.exe simnow_probe.py --symbol "KQ.m@CFFEX.IF"
```

四步全绿后，再加 `--place-order` 下一笔 1 手测试单（可立即撤）。跑完把四步输出贴回来。

---

## 5. 工具③：交易网关 trader_gateway（核心引擎）

**放哪**：`C:\my_chan_project\trader_gateway\`（已就位）

**第一步：dry_run 离线回放，验证管道**（零依赖，几秒出结果）：

```bat
cd C:\my_chan_project\trader_gateway
..\.venv\Scripts\python.exe run_gateway.py --source replay --replay-dir ./replay_data --out ./run1
```

**第二步：实时接 chan.py（仍 dry_run，观察不下单）**：

```bat
:: 先确保 chan.py 的 FrontAPI.py 在跑
cd C:\my_chan_project\trader_gateway
..\.venv\Scripts\python.exe run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --out ./run_live
```

**第三步：接 SimNow 真实仿真下单**（前提：探针四步已全绿）：

```bat
cd C:\my_chan_project\trader_gateway
..\.venv\Scripts\python.exe run_gateway.py --source sse --symbol "KQ.m@CFFEX.IF" --freq 5m --broker simnow --out ./run_live
```

> tqsdk 你的 `.venv` 里已装（3.10.2），无需再装。

---

## 6. 「探针」和「--broker simnow」到底什么区别

| | M2a 探针 `simnow_probe.py` | M2b 网关 `--broker simnow` |
|---|---|---|
| 本质 | 一次性体检脚本，手动跑一次 | 网关的「真实下单开关」，持续运行 |
| 接不接策略 | ❌ 不接，只测通道 | ✅ 接完整引擎（信号→策略→风控→下单） |
| 下什么单 | 一笔测试单（可选） | 按信号自动下真实仿真单 |
| 目的 | 先暴露账号/权限/网络坑 | 正式跑策略 |

**顺序永远是：先探针 → 全绿 → 再 `--broker simnow`。**

---

## 7. 你的项目状态核对（2026-09-04 实测）

我重新拉了 `jbxenzxy/chan.py` 的 `custom-dev` 最新代码，逐文件比对了：

| 项 | 结论 |
|---|---|
| chan.py 源码 | 上游最新代码与我分析时**完全一致**，没有过时、没有漏 |
| `trader_gateway` 核心代码（`tg/**/*.py` + `run_gateway.py` + `tools/`） | ✅ 你 push 到 GitHub 的版本与我交付的**完全一致**，一个 `.py` 都没漏 |
| `config.example.json` / `config_trailing.json` / `replay_data/*` | 只有**换行符 / 你重新生成**的差异，内容等价，不是问题 |
| `config.json`、`run1/`、`run_live/`、`state/` | 这是你**自己运行产生的本地产物**，不是交付物 |

**你真正遗漏的（本地项目里没有、GitHub 上也没有）：**

1. ❌ `trader_gateway/tools/m0_recorder\` —— 工具①
2. ❌ `trader_gateway/tools/simnow\` —— 工具②

这两个就在本交付包里，解压后放到 `C:\my_chan_project\` 下即可补齐。

**一个让项目「变乱」的原因**：`run1/`、`run_live/`、`state/`、`config.json` 这些**运行产物不该进 git**。建议在 `trader_gateway/` 里加一个 `.gitignore`：

```
run*/
state/
config.json
*.db
*.db-*
__pycache__/
```

把这些从仓库移除后，git 就干净了。

---

## 8. 安全边界

- `dry_run` **不发任何真实委托**，也不连任何账户。
- `simnow` 只连 SimNow **仿真**环境（假钱），不影响真实资金。
- 接创元实盘前：需向期货公司申请程序化接入的 AppID/AuthCode。
- 网关是独立进程，崩溃不影响 chan.py 看盘。
