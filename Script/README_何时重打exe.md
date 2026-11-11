# ChanPy.exe 何时需要重打 —— 维护说明

本目录是 `ChanPy.exe` 的**可复现构建包**：启动器源码 + 一键打包脚本 + 已验证的产物。
原启动器源码在此前那轮会话目录被清理时遗失（`D:\CChan\Script\` 里只剩 exe 和说明文档），本包按
《ChanPy_exe_交付说明》§2（方案）、§6（配置）重建，并与现役 exe 做了同端口 A/B 对照。

---

## 1. 一句话结论

**业务代码更新不需要重打 exe。** exe 是「启动器」，不是「业务程序打包」，业务代码每小时改一次也不用动它。

> 判断口诀：改动落在 `D:\CChan\Script\ChanPy.exe` 之内（即启动器逻辑）→ 要重打；落在 `D:\CChan` 的其它任何目录 → 不用。

判据根因：exe 里只有标准库 + 启动器逻辑，**不含 `FrontAPI.py`、`Bi/`、`Trading/`、`Frontend/`、`App/`、pydantic、tqsdk 等任何业务件**。
启动时才从 `D:\CChan` 现读代码、用 `D:\CChan\.venv\Scripts\python.exe` 现跑。所以代码树变了，exe 下次启动跑的就是新代码。

---

## 2. 不需要重打（日常 99% 的情况）

| 变化 | 为什么不用重打 | 你真正要做的事 |
|---|---|---|
| `FrontAPI.py`、`Bi/`、`Trading/`、`Frontend/`、`App/`、`BuySellPoint/`、`DataAPI/` 等业务代码改动 | exe 不冻结这些文件，启动时现读 | 直接双击，跑的就是改后版本 |
| `git pull` / 切分支 / 改工作树 / 暂存未提交改动 | 同上 | 无 |
| `App/AppConfig.py` 的 `_FIELD_DEFAULTS` 改了 HOST/PORT | 启动器每次启动都用 AST 现读该文件，不存副本 | 无（保证启动器探测地址与服务监听地址一致） |
| `requirements.txt` 增删依赖 | 依赖装在 `.venv`，不在 exe 里 | 在 `D:\CChan` 下 `\.venv\Scripts\pip install -r requirements.txt`，**不是**重打 exe |
| `.env` 改配置（端口、凭据、交易链路参数） | 启动器只读 HOST/PORT，凭据原样继承环境 | 改完重新双击即可 |
| `App/` 下 7 个数据文件增删 | 启动器只读清单、不复制不改写 | 无（缺项会提示"首次运行会自动生成"） |
| `Trading/State/` 状态变化 | 由服务自身读写 | 无 |
| `Docs/`、`Test/`、注释、文档 | 与运行无关 | 无 |
| `.venv` 里升级依赖版本 | 解释器路径没变 | 无 |
| 换 Python 小版本重建 `.venv`（仍是 `<repo>\.venv\Scripts\python.exe`） | 路径约定未变 | 无 |

**为什么改了代码还跑旧代码的错觉不会发生**：Python 的 `__pycache__/*.pyc` 按源文件 mtime 失效，改了就重新编译，没有"缓存住旧代码"这条路。

---

## 3. 需要重打 exe（全部是"启动器侧"或"约定侧"的变化）

| # | 触发条件 | 说明 |
|---|---|---|
| 1 | **启动器逻辑本身要改** | 新增/修改参数（如 `--xxx`）、改就绪判定方式、改进程回收策略、改控制台文案、加开机自检等 —— 改 `chanpy_launcher.py` 后必须重打 |
| 2 | **仓库根定位约定变了** | 现在按"exe 所在目录的父目录"推导；若改成读注册表/固定路径/桌面快捷方式启动，要重打 |
| 3 | **入口脚本改名** | `FrontAPI.py` 改成别的名字（临时可用 `--entry` 绕过；想改默认值就得重打） |
| 4 | **地址端口事实源换了** | 现在只认 `App/AppConfig.py` 的 `_FIELD_DEFAULTS`；若以后改成 `config.yaml` 之类，重打 |
| 5 | **`App/` 数据清单变了** | 那 7 个文件被改名/增删 → 「在位 N 个」提示会不准（功能仍可用）；想让提示准确就重打 |
| 6 | **打包工具链要升级** | 换 Python 3.13→3.14、或升级 PyInstaller 修 bug/减体积/改杀软误报 |
| 7 | **exe 丢失 / 损坏 / 要拷给别的机器** | 单文件可整体复制，但那台机器仍需 `.venv` 装齐 `requirements.txt` |
| 8 | **要换图标 / 版本信息 / 代码签名** | 现 exe 无签名，Win11 首启可能弹 SmartScreen |
| 9 | **改成"完整冻结业务代码"的形态** | 那就不再是启动器，每次业务改动都要重打 —— 本方案刻意避免这条路（原因见《交付说明》§2.1：`_REPO_ROOT` 由 `__file__` 推导，冻结后数据目录与子进程语义全变） |

---

## 4. 怎么重打（一条命令）

```bat
:: 方式 A：双击 build.bat
:: 方式 B：命令行
build.bat
:: 产物：dist\ChanPy.exe
```

指定别的解释器：

```bat
build.bat "C:\Path\To\python.exe"
:: 或
set BUILD_PYTHON=C:\Path\To\python.exe
build.bat
```

然后覆盖到位（**先备份旧 exe**）：

```bat
copy /y "D:\CChan\Script\ChanPy.exe" "D:\CChan\Script\ChanPy.exe.bak"
copy /y dist\ChanPy.exe              "D:\CChan\Script\ChanPy.exe"
```

> 构建环境：脚本会在本目录下建 `.build-venv`（约 30 MB），装 `pyinstaller==6.22.3`（见 `requirements-build.txt`）。
> 不想留它，打完删掉 `.build-venv` 与 `work` 即可；`dist\ChanPy.exe` 是自包含单文件，不依赖它们。

---

## 5. 本包产物与现役 exe 的实测对照

两者都是 PyInstaller 单文件、只含标准库、同一套行为约定。

| 项 | 现役 `D:\CChan\Script\ChanPy.exe` | 本包 `dist\ChanPy.exe` |
|---|---|---|
| 大小 | 8,260,285 字节 | 8,264,304 字节 |
| 构建环境 | Python 3.13.x（exe 内嵌 `python313.dll`）+ PyInstaller 6.22.3（版本据《交付说明》§1） | Python 3.13.14 + PyInstaller 6.22.3（本机实测） |
| 首启控制台输出 | 见下 | 逐行一致 |
| `health.chan_path` | `D:\CChan` | `D:\CChan` |
| 复用已运行实例 | 打印实例目录 + 提示后 `rc=0` 退出 | 同左，文案逐字一致 |
| 端口被非 chan.py 占用 | `rc=2` + 提示 | 同左，文案逐字一致 |
| 强杀启动器本体后子进程 | 回收，端口释放，零残留 | 同左 |
| 启动器写文件 | 0 处 | 0 处（AST 审计：`open()` 仅 `r` 模式） |

**唯一文案差异**：用 `--port` 时，本包打印 `[信息] 地址/端口来源：命令行参数 --port`，现役版仍打印
`App\AppConfig.py 的 _FIELD_DEFAULTS（.env 未设置 HOST/PORT）`。本包的说法更准确，端口行为两者一致。

> 字节数不同（8,264,304 vs 8,260,285）是正常的：单文件 exe 会把主脚本的绝对路径编进代码对象，
> 两次构建换目录就会出现几十到几百字节的偏差；同一目录重复构建也会因 pyc 时间戳不同而略有差异。
> 判据看行为，不看哈希。

> 结论：**现役 exe 完全达标，不必替换。** 本包的价值是"源码没丢 + 随时可重打"，不是"新版本更好"。

---

## 6. 验证清单（重打后照此验收）

1. `dist\ChanPy.exe --code-dir D:\CChan --port 18099 --no-browser`（换个不冲突的端口，别占用 18081）
2. 控制台应出现：`[信息] 代码目录：D:\CChan`、`[信息] 解释器  ：...\.venv\Scripts\python.exe`、
   `[信息]   在位 7 个：...`、`[就绪] 服务已启动：http://127.0.0.1:18099/`
3. `http://127.0.0.1:18099/api/health` 里 `config.chan_path` 必须是 `D:\CChan`
4. 同端口再启动一次 → 应打印"检测到 chan.py 服务已在运行"并**立即退出**（rc=0，不停在等回车）
5. 关掉控制台窗口 → 端口释放、任务管理器里没有遗留 `python.exe ... FrontAPI.py`
6. 用默认双击方式再验一次 18081（此时不要设 `--port`）

---

## 7. 边界

- 本包**不含 Python 运行时**：目标机必须保留 `D:\CChan\.venv`（或 `--python` / `CHANPY_PYTHON` 指过去）。
- exe 必须留在 `D:\CChan\Script\`（按父目录定位仓库根）。挪走则要 `CHANPY_CODE_DIR=D:\CChan` 或 `--code-dir`。
- 重建版只做了启动链路验证（首启/复用/占用/回收/零写入），**未做长时间运行与真实下单链路验证**；
  若你要用它替换现役 exe，建议先并行跑一段行情、再做一次下单冒烟。

---

## 7.5 双击「一闪而过」的成因与处置（v2026-11-11 修正）

**成因**：exe 被放在 `D:\CChan\Script\` 以外（典型：解压后直接双击 `ChanPy_build\dist\ChanPy.exe`）。
启动器按「exe 所在目录的父目录」推导仓库根，推出来的目录里没有 `App\AppConfig.py`，报错后立即退出；
而错误分支当时没有停住窗口，控制台一闪就没了。

**修正**（`chanpy_launcher.py`，已包含在本包 `dist\ChanPy.exe` 里）：

1. **向上定位**：父目录没有 `App\AppConfig.py` 时，从 exe 目录逐级向上最多找 6 层，命中即用，
   并在控制台打印 `[信息]   注意：exe 不在 <仓库根>\Script\ 下，已向上定位到 <路径>`。
2. **窗口停住**：所有错误分支改为「打印原因 + 停住窗口」，按键立即关，最长 30 秒自动关
   （`CHANPY_PAUSE_TIMEOUT` 可调，设 0 关闭）；用 `msvcrt` 轮询，不会像 `input()` 那样永久阻塞。
3. **文案带指引**：找不到仓库时明确写出「本 exe 所在 / 按约定推导的仓库根 / 应放在哪里 / 怎么补救」。

**仍然建议**：exe 就放 `D:\CChan\Script\`，别塞进子目录——向上定位只是兜底，日志会多一行注意提示。
