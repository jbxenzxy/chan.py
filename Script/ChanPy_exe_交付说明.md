# ChanPy.exe 交付说明（custom-dev 430eed1 · Script 精简版）

## 0. 需求与对照索引

| # | 需求原文 | 对应章节 |
|---|---|---|
| 1 | 拉取 `custom-dev` 最新代码，基于最新代码生成 exe | §1 基线、§2 方案、§3 产物 |
| 2 | Win11 上双击 exe 启动，替代每次开 PyCharm | §5 使用、§6 配置 |
| 3 | 复用 `D:\CChan\App` 下的 7 个文件，修复左上角输入代码不显示名称 | §2.3、§4 验证 |
| 4 | **（本轮）** 打包生成的exe文件和使用说明.txt文件，放到 D:\CChan\Script 目录下 | §3 产物、§5 使用 |
| 5 | **（本轮）** 需要的支撑或配套文件，能直接用D:\CChan目录下文件的就直接用 | §2.2、§3 产物、§7 边界 |
| 6 | **（本轮）** 如果不是必须产生 config.json 文件，能用D:\CChan目录下相关文件的就直接用，不要再创建新文件config.json | §2.4、§6 配置 |

形态：**一键启动器型 exe**（不把业务代码与第三方依赖冻结进 exe）。本轮起交付物收敛为 **2 个文件**，直接放在 `D:\CChan\Script\`；不再附带 `chanpy/` 代码副本、不再附带 `.env` 副本、不再产生或读取 `config.json`。

**按角色跳读**

| 角色 | 建议阅读 |
|---|---|
| 使用者（只想双击启动） | §3 产物、§5 使用、§6 配置 |
| 接手者（要维护/重打 exe） | §1 基线、§2 方案、§4 验证、§8 修订 |
| 评审者（核对本轮三条需求是否落地） | §0 对照索引、§2.2 / §2.4、§3、§4 |

## 1. 基线

| 项 | 值 |
|---|---|
| 远端 | `github.com/jbxenzxy/chan.py` · `custom-dev` |
| HEAD | `430eed18976bbf2526ef7a509aa76f8fc6175638`（2026-11-05 19:34:31 +0800） |
| 拉取方式 | 上一轮 `git fetch` + `git archive FETCH_HEAD` 出树；只读，未 checkout、未改 `D:\CChan` |
| 本地工作树 | HEAD `c014000`，另 3 改 1 未跟踪；这 4 个文件的 blob sha 与 430eed1 完全相同，其余文件在 `c014000..430eed1` 之间无差异 ⇒ 工作树内容 == 430eed1 |
| PyCharm 入口 | `.idea/workspace.xml` 的 RunManager `Python.FrontAPI` → `FrontAPI.py` |
| 服务 | uvicorn + `App/AppConfig.py` 的 `_FIELD_DEFAULTS`（`HOST=127.0.0.1` / `PORT=18081`），前端 `Frontend/app.html` |
| 运行环境 | `D:\CChan\.venv\Scripts\python.exe`（3.14.7），装齐 `requirements.txt` |
| 冻结环境 | 启动器用 **Python 3.13.14 + PyInstaller 6.22.3** 冻成单文件；只含标准库，跨进程只传环境变量与命令行，故与运行环境的 3.14.7 不冲突 |
| 本轮改动范围 | 只改启动器源码并重新冻结；**未直接写入** `D:\CChan\Script\` 与 `D:\CChan\Docs\`，改动全部留在沙盒以 zip 交付。冒烟会拉起真实服务，服务自身按既有语义重写 `App/trader_launch_record.json`（字节不变、仅 mtime），收尾已还原（§4） |

## 2. 方案

### 2.1 为什么是启动器而不是完整冻结

`App/AppConfig.py` 的 `_REPO_ROOT` 由 `__file__` 推导，`frontend_dir` / `app_data_dir` / `.env` 全部从它派生；`App/AppTrader.py` 会 spawn 交易子进程，tqsdk 侧还会拉适配进程。冻结后 `__file__` 指向 `_MEIPASS`，数据目录、相对路径与子进程语义全变。所以 exe 只负责：定位代码目录与解释器 → 拉起 `FrontAPI.py` → 等就绪 → 开浏览器 → 退出时回收整棵进程树。

### 2.2 支撑文件直接用 `D:\CChan`（本轮核心改动）

exe 约定放在 `<仓库根>\Script\` 下，`repo = exe 所在目录的父目录`（本机即 `D:\CChan`）。所有支撑件都从这一个根推导，不复制、不打包第二份：

| 支撑件 | 本轮起直接用 | 上一版（已取消） |
|---|---|---|
| 代码树 | `D:\CChan\FrontAPI.py` 及 `Bi/`、`Trading/`、`Frontend/` 等全部目录 | 包内 `chanpy/` 501 文件副本 |
| 解释器与依赖 | `<repo>\.venv\Scripts\python.exe` | 同左（路径写在 config.json） |
| 配置与凭据 | `<repo>\.env` | 包内 `chanpy\.env` 副本 + `config.json` |
| 地址/端口默认 | `<repo>\App\AppConfig.py` 的 `_FIELD_DEFAULTS` | `config.json` 的 `host` / `port` |
| App 缓存与状态 | `<repo>\App` 下 7 个文件 | 从 `D:\CChan\App` 复制到包内 + 账本 |
| 交易状态 | `<repo>\Trading\State\` | 同左 |

因此**不再有两种运行模式**：默认模式就是直接跑日常目录，上一版的「备用模式（`code_dir=chanpy`）」连同代码副本一起删除。用户改了工作树，exe 下次启动就跑改后的版本。

### 2.3 App 数据：同源直接共用（本轮改动）

**上一版的症状根因**：`stock_names.json`、`scan_tasks.db`、`stock_float_mc.json`、`stock_index_belong.json`、`trader_launch_record.json` 都在 `.gitignore` 里（`*.db` 与运行时缓存规则），`git archive` 出的代码树本来就不带它们；跑包内副本时 `AppData.get_stock_name()` 读不到缓存 → 返回 `None` → `AppEngine` 兜底成 `market+code`，界面只显示 `sh000001` 而不是「上证指数」。

**本轮处理**：既然代码目录就是 `D:\CChan`，`App/` 下的 7 个文件天然在位，不存在「缺数据」这个前提。启动器把上一版的 `_sync_app_data()`（账本 `.chanpy_appdata_sync.json`、`_app_data_prev\` 备份、SQLite `backup()` 复制、原子替换）整段删除，只保留 `_report_app_data()` 打印「在位 N 个」清单：

- 只读 `<repo>\App` 下 7 个文件是否存在，**不复制、不建账本、不建目录、不写任何文件**；
- 缺失项只提示「首次运行会自动生成，或可在界面点刷新」；
- 全程对 `D:\CChan` 只读（服务自身运行时的正常读写除外）。

这一改动同时消除了上一版「备用模式下 exe 期间新建的扫描任务不回写 `D:\CChan\App`」这一整类边界问题。

### 2.4 配置：不产生 `config.json`（本轮核心改动）

**可行性判定**：`config.json` 不是必须的。上一版启动器本就有内置 `DEFAULT_CONFIG`，`config.json` 只是「可覆盖的副本」；但 `_load_config()` 在文件缺失时会**自动写出一份**，这正是「产生新文件」的来源。本轮把整个读写逻辑替换为纯内存解析：

```
启动器配置 = 内置默认 → <repo>\.env → 环境变量 → 命令行参数
```

| 配置项 | 事实源 | 覆盖方式 |
|---|---|---|
| `repo`（代码目录） | exe 所在目录的父目录 | `CHANPY_CODE_DIR` / `--code-dir` |
| `python_exe` | `<repo>\.venv\Scripts\python.exe` | `CHANPY_PYTHON` / `--python` |
| `entry` | `FrontAPI.py` | `--entry` |
| `HOST` / `PORT` | **`App/AppConfig.py` 的 `_FIELD_DEFAULTS`** | `<repo>\.env` 的 `HOST/PORT` → 环境变量 `HOST/PORT` → `--port` |
| 浏览器 / 超时 | 内置默认 | `CHANPY_BROWSER` / `CHANPY_NO_BROWSER` / `CHANPY_TIMEOUT` / `--no-browser` |

关键在 `HOST/PORT`：**不在启动器里再存一份默认值**（避免跨文件重复默认值漂移）。启动器用 AST 只读 `App/AppConfig.py` 里 `_FIELD_DEFAULTS` 的字面量，不 import 业务模块（不拉起 pydantic 依赖、不触发配置单例副作用）。`.env` 与环境变量的优先级与服务端 `AppConfig` 完全一致；启动器再把最终值注入 `HOST/PORT` 环境变量，保证探测地址与服务监听地址恒一致，`--port` 覆盖也同步生效。

`_read_env_file()` 只读 `.env` 里的 `HOST` / `PORT` 两个键，**不读取、不打印账号密码等凭据**。读不到 `_FIELD_DEFAULTS` 时明确报错，不静默回退到另一份硬编码。

### 2.5 其它关键行为（沿用上一版）

- 18081 上已有 chan.py 服务 → 复用不重复拉起，并打印该实例的 `chan_path`（避免「换了包但浏览器连的是旧实例」这种假象）。
- 18081 被非 chan.py 程序占用 → 明确报错、等回车后退出（rc=2）。
- 进程纳入 Job Object（`KILL_ON_JOB_CLOSE`），关窗/强杀启动器都会连带回收交易子进程。

## 3. 产物

**交付位置：`D:\CChan\Script\`**（本轮需求 4；zip 内按 `Script/` 层级），只有 2 个文件：

```
D:\CChan\
└── Script\
    ├── ChanPy.exe       8,260,285 字节（7.88 MiB），双击即用
    └── 使用说明.txt      6,547 字节，UTF-8 BOM + CRLF，记事本直接打开
```

| 项 | 值 |
|---|---|
| exe | PyInstaller 6.22.3 单文件、只含标准库；比上一版 9,192,561 字节小 932,276 字节（删掉数据同步/账本逻辑） |
| 使用说明.txt | 已重写：2 文件形态、配置四级优先、无 `config.json`、无 `chanpy/` 副本 |
| `config.json` | **不存在**（不随包、运行时也不生成） |
| `chanpy/` 代码副本 | **不存在**（直接跑 `D:\CChan`） |
| `.env` 副本 | **不存在**（直接用 `D:\CChan\.env`） |

交付包 `ChanPy_exe_Script_20261107.zip` 内按仓库原目录层级只含 `Script/ChanPy.exe` 与 `Script/使用说明.txt` 两项，解压覆盖到 `D:\CChan` 即到位。

> **交付方式（接手者必读）**：按既有约定，本轮**没有直接写 `D:\CChan`**。zip 与本文档都产在沙盒 `C:\Users\river\WorkBuddy\2026-11-07-08-40-24\sandbox\`，合并由使用者完成：把 zip 解压到 `D:\CChan`（保持 `Script/` 层级），再把本文档覆盖到 `D:\CChan\Docs\ChanPy_exe_交付说明.md`。原始文档备份在 `sandbox\internal\ChanPy_exe_交付说明_原始备份.md`（11,035 字节）。

上一版位于 `D:\A0.断舍离\视频\TS转MP4\ChanPy\` 的旧包（含 `chanpy/`、`config.json`、`_旧版备份_20261106\`）**本轮未改动、未删除**，需要清理由使用者自行决定。

## 4. 验证证据

在干净目录 `<sandbox>\smoke\Script\` 放入这 2 个文件，用 `--port 18087 --code-dir D:\CChan --no-browser` 启动，独立取证：

| 检查 | 结果 |
|---|---|
| 代码树 vs 远端 HEAD（上一轮逐文件 git blob sha） | 远端 541 / 本地 501；逐字节相同 43、仅行尾不同 458（`core.autocrlf=true`）、**内容不同 0**；远端多出 40 且全为 `Image/` |
| 启动器静态编译（`py_compile`） | 通过 |
| 配置解析（源码态单测） | `_FIELD_DEFAULTS` → `{'HOST': '127.0.0.1', 'PORT': 18081}`；`repo = D:\CChan`；`python_exe = D:\CChan\.venv\Scripts\python.exe`；`--port 18087` 与 `--no-browser` 覆盖生效 |
| 默认模式启动 | 日志 `代码目录：D:\CChan`、`地址/端口来源：App\AppConfig.py 的 _FIELD_DEFAULTS`、`在位 7 个` |
| `health.chan_path` | `D:\CChan` |
| `/api/search?q=000001` | 上证指数、平安银行（日志 `从缓存文件加载股票名称: 8941只`） |
| **交付目录恰好 2 个文件、零新增/零删除/零改写** | `['ChanPy.exe', '使用说明.txt']`；`new=[]`、`removed=[]`、`changed=[]`；无 `chanpy\` 目录、无 `.env` 副本 |
| **`config.json` 零产生** | 交付目录与仓库根磁盘均不存在；日志不含 `config.json` 与「已生成默认配置」；启动器源码 AST 里无 `config.json` 字面量 |
| **启动器零文件写入（静态 + 运行时双证）** | 源码 AST 审计：`open(w/a/x/+)`、`Path.write_text/write_bytes`、`os.replace/remove/rename`、`shutil.*`、`json.dump` 等写调用 **0 处**（只认可执行语句，docstring 不算证据）；运行时交付目录零改写 |
| 服务自身写入（与启动器区分） | 服务 shutdown 阶段重写 `App/trader_launch_record.json`：写入点 `App/AppTrader.py:283` `_write_state_file()`、`:290` `os.replace()`，调用点 `:413` / `:1151`；实测**字节不变、仅 mtime 变**。冒烟收尾还原为原始字节 + 原始 atime/mtime |
| **生产数据零残留（还原后）** | `D:\CChan\App` 与 `D:\CChan\Trading\State` 全量快照 `new/removed/changed` 均为 `[]` |
| 端口回收 | 等启动器打印 `[就绪]` 后广播 `CTRL_BREAK_EVENT`（模拟关窗），`stop_mode=ctrl_break` **无 fallback**；uvicorn 自报 PID 消失 + netstat 无 `LISTENING`，服务日志 `Shutting down` → `Application shutdown complete` → `服务已停止` |
| 用户现有服务 | 全程未触碰；校验用独立端口 18087，进程按 PID 回收，未用 `taskkill /IM` |
| **最终冒烟** | `ALL_PASS = True`，**29 / 29** 项检查全绿（结果：`sandbox\internal\smoke_result.json`，日志：`smoke_stdout9.log`） |

> **判据口径说明**（避免假绿/假红）：端口是否释放只看 netstat 的 `LISTENING` 行与 uvicorn 日志自报的 PID，不用 `socket.connect()`——本机 TIME_WAIT 残留会让 connect 返回 `TimeoutError`，既不等于「仍在监听」也不等于「已释放」。上一版验证里「18083 / 18087 / 18088 端口均释放」的结论在本轮以同样口径复现。
>
> **为什么不用「`App/` 全量快照前后恒等」直接当启动器判据**：新方案让 exe 直接跑 `D:\CChan`，被拉起的服务本来就会按既有业务语义读写 `App/` 与 `Trading/State/`；PyCharm 直接启动同样发生。把服务行为算到启动器头上会恒假红。因此「启动器零写入」用源码静态审计 + 交付目录快照证明，「生产数据」用还原后的全量快照证明，两层判据分开。早期一轮曾把 `os.environ.copy()` 误判成文件复制、把「字节相同但 mtime 变化」误判成未还原，两处判据均已收紧。
>
> **勘误与口径边界（据实说明）**：上表「生产数据零残留（还原后）」是**单次冒烟内部**的判据——以该次启动前的 `(size, mtime)` 快照为基线，收尾还原后回到该基线。跨整个验证会话看，`App/trader_launch_record.json` 的 mtime 因早期几轮冒烟（当时尚未加还原逻辑）发生过累计漂移（09:06:42 → 09:15:39），但**文件字节自始至终未变**（各轮均校验 `service_owned_bytes_unchanged=True`，内容恒为 `{"instances": []}`），且该文件被 `.gitignore:22` 的 `App/*.json` 规则忽略、不受 git 跟踪；`git status --porcelain --untracked-files=all` 与会话前一致（3 改 2 未跟踪，无新增项）。这一改动来自服务 shutdown 的既有语义，不是启动器；`D:\CChan\Script\` 与 `D:\CChan\Docs\` 本轮均未被写入。

## 5. 使用

1. 把交付 zip 解压到 `D:\CChan`（保持 `Script/` 层级），`ChanPy.exe` 与 `使用说明.txt` 应位于 `D:\CChan\Script\`，**保持这个位置**（exe 靠父目录定位代码）。
2. 双击 `D:\CChan\Script\ChanPy.exe`。
3. 控制台应依次出现：

```
[信息] 代码目录：D:\CChan
[信息] 解释器  ：D:\CChan\.venv\Scripts\python.exe
[信息] 服务地址：http://127.0.0.1:18081/
[信息] 地址/端口来源：App\AppConfig.py 的 _FIELD_DEFAULTS（.env 未设置 HOST/PORT）
[信息] App 数据：D:\CChan\App（与代码目录同源，直接共用，不复制）
[信息]   在位 7 个：double_click_dt.csv、…、trader_launch_record.json
[就绪] 服务已启动：http://127.0.0.1:18081/
```

看到「在位 7 个」就说明名称、选点、扫描任务、市值、指数归属、上次周期、托管记录都是日常那份，左上角输入代码会显示中文名。

4. 关闭窗口或 Ctrl+C 停止。
5. 若打印「检测到 chan.py 服务已在运行」，说明浏览器连的是已存在实例 —— 看下一行打印的目录对不对，不对就先停掉旧实例再双击。

## 6. 配置（不再有 `config.json`）

四级优先，从低到高，**全程不落盘任何配置文件**：

| 级别 | 位置 | 说明 |
|---|---|---|
| ① 代码默认 | `App/AppConfig.py` 的 `_FIELD_DEFAULTS` | `HOST=127.0.0.1` / `PORT=18081`；地址端口唯一事实源，启动器与服务端共用 |
| ② `.env` | `D:\CChan\.env` | 写 `HOST=…` / `PORT=…` 即覆盖 ①；凭据（`TRADING_` 前缀）也在这里，启动器不读不复制 |
| ③ 环境变量 | `HOST` / `PORT` | 优先级高于 `.env`；另有 `CHANPY_PYTHON`、`CHANPY_CODE_DIR`、`CHANPY_BROWSER`、`CHANPY_NO_BROWSER`、`CHANPY_TIMEOUT` |
| ④ 命令行 | `--port N` / `--no-browser` / `--code-dir PATH` / `--python PATH` / `--entry NAME` | 最高优先级；`--port` 同时注入给服务端 |

`CHANPY_CODE_DIR` 是「exe 被挪到别处」时的补救手段：把 exe 留在 `D:\CChan\Script\` 就不需要它。

## 7. 边界与限制

- 本包不含 Python 运行时，需要本机保留 `D:\CChan\.venv`，或用 `--python` / `CHANPY_PYTHON` 指向另一个装齐 `requirements.txt` 的解释器；不能只把 exe 单独拷到没装环境的电脑。
- **exe 默认跑的是你当前工作树的代码**（现在等价于 430eed1）。你改了工作树，exe 下次启动就跑改后的版本。想跑别的代码树用 `--code-dir`，但那棵树里的 `App/` 数据需要自己准备 —— 启动器不会从 `D:\CChan\App` 复制过去。
- exe 默认按「自身所在目录的父目录」定位仓库根，所以请留在 `D:\CChan\Script\` 下；移动到桌面/其它盘后需设 `CHANPY_CODE_DIR=D:\CChan` 或传 `--code-dir`。
- `.env`：直接用 `D:\CChan\.env`，键值只有 3 个 `TRADING_` 前缀键，无需手动复制；包里不含任何账号密码，天勤/SimNow 凭据走系统环境变量。其中 `TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING=true` 会让界面「实盘」选项可选，真正下单还需 `LIVE_ACCOUNT`/`LIVE_PASSWORD`，缺任一 `SimNow.py` 拒绝启动；默认链路仍是 `simnow`。
- 改端口后，前端自动下单开关传的是 `location.origin`，SSE 地址会跟着走；但 `Trading/Config.py` 等处仍有 `18081` 兜底默认值，遇到异常优先回到 18081。
- Win11 首次双击可能撞三件事：未签名 exe 的 SmartScreen 提示（更多信息 → 仍要运行；属性页有「解除锁定」就勾上）、单文件 exe 首启要解压到 `%TEMP%\_MEIxxxx` 所以慢几秒、个别杀软对 PyInstaller 误报。
- exe 本身不写任何文件，但被拉起的服务照常按既有业务语义读写 `D:\CChan\App` 与 `Trading\State`（例如 shutdown 时重写 `App/trader_launch_record.json`，本轮实测字节不变、仅 mtime 变）。这不是启动器新增的写入，PyCharm 直接启动同样发生。

## 8. 相对上一版的修订

| # | 上一版 | 本版（本轮） |
|---|---|---|
| 1 | 产物在 `D:\A0.断舍离\视频\TS转MP4\ChanPy\`，zip 52,338,300 字节 / 504 项 | 产物直接放 `D:\CChan\Script\`，**2 个文件**；zip 按 `Script/` 层级，解压即到位 |
| 2 | 随包附 `chanpy/` 501 文件代码副本（备用模式） | **删除代码副本**，直接跑 `D:\CChan`；备用模式一并取消 |
| 3 | `config.json` 为事实源，缺失时启动器自动写出一份 | **删除 config.json**，配置改为「代码默认 → `.env` → 环境变量 → 命令行」四级，纯内存解析 |
| 4 | `HOST/PORT` 默认值在启动器与 `AppConfig` 各存一份 | 启动器用 AST 只读 `App/AppConfig.py` 的 `_FIELD_DEFAULTS`，**单一事实源**，不再有副本 |
| 5 | 随包附 `chanpy\.env` 副本 | `D:\CChan\.env` 直接用，无副本；启动器只读其中 `HOST/PORT`，不碰凭据 |
| 6 | App 数据复用：账本、`_app_data_prev\` 备份、SQLite `backup()`、原子替换 | 代码与数据同源 → 整段删除，只保留「在位 N 个」只读提示；启动器零文件写入 |
| 7 | §6 是 `config.json` 11 键表 | §6 改为四级配置优先表 + 环境变量/命令行清单 |
| 8 | §7 有「备用模式下数据不回写」边界 | 该边界随副本删除而消失；新增「exe 摆放位置 / `CHANPY_CODE_DIR`」说明 |
| 9 | 验证证据含「数据复用单测 13/13」 | 该单测针对已删除的复制逻辑，不再适用；改以「启动器源码零写文件调用（AST）+ 交付目录零新增/零删除/零改写 + 服务自身写入还原后生产数据零残留」三层实证替代 |
| 10 | 旧包未清理 | 旧包位于 `D:\A0.断舍离\视频\TS转MP4\ChanPy\`，本轮**未改动、未删除**，清理由使用者决定 |
