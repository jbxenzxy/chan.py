# ChanPy.exe 交付说明（custom-dev 430eed1）

## 0. 需求与对照索引

| # | 需求原文 | 对应章节 |
|---|---|---|
| 1 | 拉取 `custom-dev` 最新代码，基于最新代码生成 exe | §1 基线、§2 方案、§3 产物 |
| 2 | Win11 上双击 exe 启动，替代每次开 PyCharm | §5 使用、§6 配置 |
| 3 | （本轮追加）复用 `D:\CChan\App` 下的 7 个文件，修复左上角输入代码不显示名称 | §2.3 数据复用、§4 验证 |

形态：**一键启动器型 exe**（不把业务代码与第三方依赖冻结进 exe）。

## 1. 基线

| 项 | 值 |
|---|---|
| 远端 | `github.com/jbxenzxy/chan.py` · `custom-dev` |
| HEAD | `430eed18976bbf2526ef7a509aa76f8fc6175638`（2026-11-05 19:34:31 +0800） |
| 拉取方式 | `git fetch` + `git archive FETCH_HEAD` 出树；只读，未 checkout、未改 `D:\CChan` |
| 本地工作树 | HEAD `c014000`，另 3 改 1 未跟踪；**这 4 个文件的 blob sha 与 430eed1 完全相同**，其余文件在 `c014000..430eed1` 之间无差异 ⇒ 工作树内容 == 430eed1 |
| PyCharm 入口 | `.idea/workspace.xml` 的 RunManager `Python.FrontAPI` → `FrontAPI.py` |
| 服务 | uvicorn + `App/AppConfig.py` 的 `HOST/PORT`，默认 `127.0.0.1:18081`，前端 `Frontend/app.html` |
| 运行环境 | `D:\CChan\.venv\Scripts\python.exe`（3.14.7） |
| 冻结环境 | 启动器用 **Python 3.13 + PyInstaller 6.22.3** 冻成单文件；只含标准库，跨进程只传环境变量与命令行，故与运行环境的 3.14.7 不冲突 |

## 2. 方案

### 2.1 为什么是启动器而不是完整冻结

`App/AppConfig.py` 的 `_REPO_ROOT` 由 `__file__` 推导，`frontend_dir` / `app_data_dir` / `.env` 全部从它派生；`App/AppTrader.py` 会 spawn 交易子进程，tqsdk 侧还会拉适配进程。冻结后 `__file__` 指向 `_MEIPASS`，数据目录、相对路径与子进程语义全变。所以 exe 只负责：定位代码目录与解释器 → 拉起 `FrontAPI.py` → 等就绪 → 开浏览器 → 退出时回收整棵进程树。

### 2.2 两种运行模式（`code_dir`）

| 模式 | `code_dir` | 代码 | 数据 | 适用 |
|---|---|---|---|---|
| **默认：直接跑日常目录** | `"D:/CChan"` | 你当前工作树（内容 == 430eed1） | `D:\CChan\App`、`.env`、`Trading/State/` 全同一份 | 本机日常使用，与 PyCharm 完全一致 |
| 备用：跑包内副本 | `"chanpy"` | 包内 430eed1 原样副本 | 启动前自动从 `D:\CChan\App` 补 7 个文件（见 2.3） | 想把 exe 固定在某版本、或离线备用 |

### 2.3 App 数据复用（本轮新增）

**症状根因**：`stock_names.json`、`scan_tasks.db`、`stock_float_mc.json`、`stock_index_belong.json`、`trader_launch_record.json` 都在 `.gitignore` 里（第 8 行 `*.db`、第 20 行运行时缓存），`git archive` 出的代码树本来就不带它们。备用模式下 `AppData.get_stock_name()` 读不到缓存 → 返回 `None` → `AppEngine` 兜底成 `market+code`，界面就只显示 `sh000001` 而不是「上证指数」。

**处理**：启动器在拉起服务前执行 `_sync_app_data()`：

- 源 = `app_data_from`（默认 `D:\CChan\App`），目标 = `<code_dir>\App`。
- 源与目标解析为同一目录（默认模式就是这种情况）→ 只打印「在位 N 个」清单，**不复制、不建账本**。
- 否则按 7 个文件逐个处理：
  - 源没变动过（以账本 `.chanpy_appdata_sync.json` 记的 mtime+size 判定）→ 保留包内自己写入的，不回灌覆盖；
  - 源变动过 → 以日常目录为准覆盖，旧目标先备份到 `App\_app_data_prev\`，每个文件只留最近 3 份；
  - `scan_tasks.db` 是 WAL 模式，走 SQLite `backup()` API，不会丢掉 `-wal` 里还没 checkpoint 的写入；
  - 原子替换（同盘 `os.replace`），失败只告警不阻断启动。
- **全程只读 `D:\CChan`，从不回写。** `app_data_sync` 可设 `missing`（只在目标缺失时补）或 `off`。

### 2.4 其它关键行为

- `config.json` 为事实源；注入 `HOST`/`PORT` 环境变量，让改端口真正生效（`AppConfig` 环境变量优先级高于 `.env`）。
- 18081 上已有 chan.py 服务 → 复用不重复拉起，并打印该实例的 `chan_path`（避免「换了包但浏览器连的是旧实例」这种假象）。
- 18081 被非 chan.py 程序占用 → 明确报错、等回车后退出（rc=2）。
- 进程纳入 Job Object（`KILL_ON_JOB_CLOSE`），关窗/强杀启动器都会连带回收交易子进程。

## 3. 产物

`D:\A0.断舍离\视频\TS转MP4\ChanPy_exe_430eed1_20261106.zip`（与本说明同目录）
52,338,300 字节（49.9 MiB / 52.3 MB），504 项。

已解压的 `D:\A0.断舍离\视频\TS转MP4\ChanPy_exe\` 也已就地换成新版：只替换了 `ChanPy.exe`、`config.json`、`使用说明.txt` 三个文件，旧文件在同目录 `_旧版备份_20261106\`；`chanpy/` 代码树与新版一致，未动。

```
ChanPy/
├── ChanPy.exe        9,192,561 字节（8.77 MiB），双击即用
├── config.json       11 个键，见 §6
├── 使用说明.txt       离线可读，UTF-8 BOM，记事本直接打开
└── chanpy/           custom-dev 430eed1 代码（501 文件，按原目录层级）
```

`Image/` 下 40 个仓库文档插图未随包附带（`Frontend/` 下 grep `Image/|.png|.jpg|.svg` 零命中），其余远端文件全部保留。

## 4. 验证证据

| 检查 | 结果 |
|---|---|
| 代码树 vs 远端 HEAD（逐文件 git blob sha） | 远端 541 / 本地 501；逐字节相同 43、仅行尾不同 458（`core.autocrlf=true`）、**内容不同 0**；本地多出 0；远端多出 40 且全为 `Image/` |
| 打包内容 | 504 项，无 `__pycache__` / `.pyc` / `.diff` / `.patch`；四个必需项均在 |
| **默认模式**（解压后双击等价启动） | `health.chan_path = D:\CChan`；`/api/search?q=000001` → 上证指数、平安银行；日志 `从缓存文件加载股票名称: 8941只` |
| **备用模式**（`code_dir=chanpy`，包内无任何 App 缓存） | 启动前打印「App 数据复用：… 已同步 7 个」并逐条列出；`/api/search?q=000001` → 上证指数、平安银行；`App\` 下 7 个文件齐全、`scan_tasks.db` 大小正常、账本已生成 |
| **中文 + 空格路径**（`D:\临时验证_ChanPy 中文\`） | 启动即打印「在位 7 个：…」；`/api/search?q=600000` → 浦发银行 |
| 已有服务复用分支 | rc=0，打印「检测到 chan.py 服务已在运行」+ 该实例 `chan_path`，未重复拉起 |
| 关闭窗口回收 | 只杀 exe 进程（不带 `/T`），Job Object 连带回收服务进程；18083 / 18087 / 18088 端口均释放 |
| 数据复用单测 | 13/13 通过（首次补齐、WAL 数据不丢、源未变不回灌、源变覆盖+备份、备份只留 3 份、源缺文件不动、`missing`/`off`、同源不复制、源缺失跳过、归档 mtime 更新仍以日常为准） |
| 用户现有服务 | 全程未触碰；校验用的进程均按 PID 回收，未用 `taskkill /IM` |

## 5. 使用

1. 解压 zip 到任意目录（路径含中文、空格都行，已实测）。
2. 双击 `ChanPy/ChanPy.exe`。
3. 控制台应依次出现：

```
[信息] 代码目录：D:\CChan
[信息] 解释器  ：D:\CChan\.venv\Scripts\python.exe
[信息] 服务地址：http://127.0.0.1:18081/
[信息] App 数据：D:\CChan\App（与代码目录同源，直接共用，不复制）
[信息]   在位 7 个：double_click_dt.csv、…、trader_launch_record.json
[就绪] 服务已启动：http://127.0.0.1:18081/
```

看到「在位 7 个」就说明名称、选点、扫描任务、市值、指数归属、上次周期、托管记录都是日常那份，左上角输入代码会显示中文名。

4. 关闭窗口或 Ctrl+C 停止。
5. 若打印的是「检测到 chan.py 服务已在运行」，说明浏览器连的是已存在的实例——看它下一行打印的目录对不对，不对就先停掉旧实例再双击。

## 6. 配置 `config.json`（11 键）

| 键 | 默认 | 说明 |
|---|---|---|
| `python_exe` | `D:/CChan/.venv/Scripts/python.exe` | 装齐 `requirements.txt` 的解释器 |
| `code_dir` | `D:/CChan` | 代码目录；改 `"chanpy"` 走包内副本 |
| `entry` | `FrontAPI.py` | 入口脚本，一般不改 |
| `host` / `port` | `127.0.0.1` / `18081` | 改后浏览器地址同步变化 |
| `open_browser` | `true` | 改 `false` 只起服务不开浏览器 |
| `browser_path` | `""` | 留空 = 系统默认；可填 Chrome/Edge 完整路径 |
| `startup_timeout_sec` | `120` | 等待服务就绪的秒数 |
| `app_data_from` | `D:/CChan/App` | App 数据复用来源 |
| `app_data_files` | 7 个文件名 | 要复用的清单 |
| `app_data_sync` | `auto` | `auto` / `missing` / `off` |

## 7. 边界与限制

- 本包不含 Python 运行时，需要本机保留 `D:\CChan\.venv`，或把 `python_exe` 指向另一个装齐依赖的解释器；不能只把 exe 单独拷到没装环境的电脑。
- **默认模式跑的是你当前工作树的代码**（现在等价于 430eed1）。你改了工作树，exe 下次启动就跑改后的版本；要钉死版本就把 `code_dir` 改成 `"chanpy"`。
- 备用模式下 `App\` 里的数据是**副本**：exe 期间新建的扫描任务、刷新的名称不会回写 `D:\CChan\App`。需要两边完全同一份就用默认模式。
- `.env`：包内 `chanpy\.env` 与 `D:\CChan\.env` 的键值完全一致（只有 3 个 `TRADING_` 前缀键），无需手动复制；包里不含任何账号密码，天勤/SimNow 凭据走系统环境变量。其中 `TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING=true` 会让界面「实盘」选项可选，真正下单还需 `LIVE_ACCOUNT`/`LIVE_PASSWORD`，缺任一 `SimNow.py` 拒绝启动；默认链路仍是 `simnow`。
- 改端口后，前端自动下单开关传的是 `location.origin`，SSE 地址会跟着走；但 `Trading/Config.py` 等处仍有 `18081` 兜底默认值，遇到异常优先回到 18081。
- Win11 首次双击可能撞三件事：未签名 exe 的 SmartScreen 提示（更多信息 → 仍要运行；属性页有「解除锁定」就勾上）、单文件 exe 首启要解压到 `%TEMP%\_MEIxxxx` 所以慢几秒、个别杀软对 PyInstaller 误报。

## 8. 相对上一版的修订

| # | 上一版的问题 | 本版 |
|---|---|---|
| 1 | 没提包里**已经带了 `.env`**，却让用户从 `D:\CChan` 复制；也没说 `CONFIRM_LIVE_TRADING=true` 的含意 | §7 写明键值一致、无需复制，并说明实盘闸门的实际约束 |
| 2 | §5 只列 5 个 config 键，与 `使用说明.txt` 不一致 | §6 列全 11 键 |
| 3 | 「50 MB」不准 | 实测 52,338,300 字节（49.9 MiB） |
| 4 | 验证只覆盖系统临时目录 | 补中文 + 空格路径实测 |
| 5 | 缺 Win11 首次双击的现实障碍 | §7 末条 |
| 6 | 缺「端口被非 chan.py 程序占用」分支 | §2.4 |
| 7 | 没写 exe 由哪个解释器冻结 | §1 冻结环境一行 |
| 8 | —— | §2.3 新增 App 数据复用，并补两种模式对照表 |
