# memory-env.md —— 本机环境与工具坑（Bash / PowerShell / Edit / grep / 打包）


> 本文件由 `~/.workbuddy/MEMORY.md` 于 2026-09-15 拆分而来，**不常驻上下文**。
> 命中 MEMORY.md 索引表的主题词时，用 Read / Grep 按需读取本文件。
> 内容原样搬运未删减；拆分前全文备份：`MEMORY.full-2026-09-15.md`。

## ⚠️ Bash 里 `python -c "..."` 的反引号会被 shell 当命令替换吃掉（2026-09-27 实测）
- **坑**：命令体里的**反引号**被 bash 解析成命令替换，被替换成空 → 写出的文件里反引号包裹的内容
  **整段消失**（实测 header 里两处路径 `` `...` `` 全丢，只剩空白）。
- **最阴的地方**：**退出码仍是 0**，Python 打印的"written chars"也正常，
  只有 stderr 刷一堆 `xxx: command not found` —— 不看 stderr、不读回文件，完全发现不了。
- **判据**：stderr 报了某命令 not found，但你命令里根本没写过它 → 就是被 shell 重解析了。
- **绕法**（按优先级）：① 含反引号的内容**改用 Write / Edit 工具写**，别走 shell；
  ② 必须跑脚本 → 先写到沙盒 `.py` 文件再 `python script.py`；③ 真要 inline → 用 `chr(96)` 拼。
- **校验**：脚本写完**必须 Read 回来确认反引号内容还在**，只看退出码不算数。

## ⚠️ 脚本写文件必须「原子写」（2026-09-23 实测：app.js 被清空、整套 r7 改动丢失重放）
- **坑**：`io.open(path, "wb").write(f(x))` 这种一行式写法 —— `open("wb")` **先截断文件**，
  随后 `f(x)` 参数求值若抛异常（如 bytes 误调 `.encode`），`write` 永不执行 ⇒ **文件留在 0 字节**。
  实证：mutate driver 对 50 万字节 app.js 触发此坑，r7 全部前端改动丢失，靠「基底副本 + 行号证据
  重放 + 护栏测试当裁判」才恢复。
- **铁律**：任何脚本改写既有文件一律用 `_atomic_write`：先写 `path + ".tmp"` 全量落盘，
  再 `os.replace(tmp, path)`（同盘原子）。变异/还原类 driver 在**改前**先留 sha256，
  改完跑完再还原并比对 sha。
- **沙盒工作树是唯一副本，改前先备份**：对大文件动刀前 `cp work/Frontend/app.js /tmp/app.js.bak`
  （或 sha 记录），副本来源（历史快照 / 用户项目）提前想好 —— 本轮恢复全靠
  `sandbox/orig`、`sandbox/internal/remote_*`、用户项目三路副本互证。

## ⚠️ 会话级「批量删除拦截」会给仓库测试造**假红**（2026-09-23 实测，先看这条再判红）
- 本工具会话往**每个子进程**注入一组 `CODEBUDDY_SAFE_DELETE_*` 变量，且把 shim 目录塞进
  **`PYTHONPATH`**（实测 `PYTHONPATH=E:\...\cli\vendor\shim`，因此它在 `sys.path[1]`，
  venv python 每次启动都会加载）。阈值 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=50`，计数 scope = **turn**。
- 命中时子进程 stdout 打出 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":3800,
  "threshold":50,"scope":"turn","targets":[...]}`，**删除被拒绝** ⇒ 用例 rc=1。
- 2026-09-23 实证：门禁 120 条跑出 **117/120**，三个红（`app_amo_behavior` / `lock_completeness` /
  `aol_ledger_display`）全部是这个拦截造成的（它们各自清自己的几千个临时 fixture，如
  `Test/fixtures/_amo*`、`Test/_selftest_prob*`）。**清掉环境后逐个重跑 rc=0**：
  `env -u PYTHONPATH -u CODEBUDDY_SAFE_DELETE_ENABLED -u CODEBUDDY_SAFE_DELETE_BULK_GUARD \
   -u CODEBUDDY_SAFE_DELETE_BIN_DIR -u CODEBUDDY_SAFE_DELETE_BROKER_DELETE \
   -u CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD -u CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR \
   -u CODEBUDDY_SAFE_DELETE_REPORT_PATH <venv>\Scripts\python.exe Test/xxx.py`
- 结论：**门禁出现"某个用例 0.x 秒就红、且输出里有 safe-delete 字样"时，先当环境拦截处理**，
  清环境重跑该条确认；不要当代码回归报。另：`BASH_FUNC_rm%%`/`unlink` 也被包装成 shim，
  所以 `rm` 的行为与直觉不同（走 broker，超阈值需确认）。

## ⚠️ Playwright 无头浏览器：「捆绑 chromium」收尾会挂死，改用本机 Chrome（2026-09-18 实测）
- 症状：`browser.close()` **永不返回**（用例挂死，最长观察 8 分钟），最终报
  `Exception: Browser.close: Connection closed while reading from the driver`。
  `tasklist` 查 chrome / headless 进程为 **0**（chromium 其实已退出，是 Python 侧调用不返回）。
- 与页面、路由无关：同一探针在**干净基线**上同样复现；把 `page.route` 收窄、
  `page.unroute_all()`、`goto("about:blank")` 均不能解决。
- ✅ **修法**：`pw.chromium.launch(channel="chrome")`（本机 Chrome）—— launch ≈2.5s、close 正常返回。
  优先序写成本机 Chrome → 捆绑 chromium → `ms-playwright` 缓存路径。
  探针脚本：`<sandbox>/probe_pw_close.py`（同一脚本换 launch 方式对照即可定位）。
- 配套：给用例加**总时长看门狗**（守护线程 sleep 后 `os._exit(1)`），任何收尾挂起都不会拖死整轮。


## 本机两套 Python 解释器「各管一半」，跑测试前先认准（2026-09-18 实测）
- `~/.workbuddy/binaries/python/envs/chanenv/Scripts/python.exe`（`default` 同）——
  有 **pandas / fastapi / pydantic**，**跑仓库 `Test/run_all.py` 这类集成测试必须用它**。
- `~/.workbuddy/binaries/python/versions/3.13.12/python.exe`（managed）—— 有 **playwright**，
  但**没有 pandas / fastapi**；用它跑 run_all 会大面积
  `ModuleNotFoundError: No module named 'pandas' / 'fastapi'` 的**假红**（看着像代码坏了）。
- 本机 `Python314`（系统）有 pydantic，可跑 `Trading/Test/test_p59_*.py`。
- 快速认环境：`"<python>" -c "import pandas,fastapi,pydantic"` / `-c "import playwright"`。


## 长命令的输出与存活（2026-09-18 实测）
- Bash 工具里跑长任务（含浏览器 / 安装 / 测试）易被 SIGTERM 杀掉且 stdout 为空 →
  改 `run_in_background=true`，让任务独立存活。
- 输出**重定向到文件时 Python 是块缓冲**，看不到进度 → 加 `-u`（无缓冲），
  再 Read 文件即可实时看进度、判断卡在哪一步。
- TaskStop 只清任务本身，**不会**回收它派生的浏览器 / 子进程；
  排查"卡死"时先 `tasklist` 看有没有残留。


## 跑仓库测试的 PYTHONPATH：**必须 Windows 风格**（2026-09-17 实测）
- 有些测试脚本（如 `Trading/Test/test_p56_exchange_today_exit.py`）会 `import Trading.xxx`
  但**自己不插 `sys.path`** —— 它们依赖调用者把仓库根给进去。
- ⚠️ **在 git-bash 里 `PYTHONPATH=/c/Users/x/repo` 无效**：那是 POSIX 路径，Windows 版 Python 认不出，
  症状是 `ModuleNotFoundError: No module named 'Trading'` —— 看着像代码/目录没了，其实是变量格式问题。
  **必须写 `C:\Users\x\repo`**：
  `PYTHONPATH='C:\Users\river\my_repo' /c/my_repo/.venv/Scripts/python.exe Trading/Test/xxx.py`
- 等价绕法：`python -m Trading.Test.xxx`（cwd 会进 `sys.path`）。
- ⚠️ 批量跑测试用 `subprocess.run(..., env=dict(os.environ, PYTHONPATH=WIN_STYLE))` 最保险：
  若只依赖继承的环境，那个 POSIX 值的 `PYTHONPATH` 会把路径污染（实测一次误报 2 项 FAIL，
  以为是自己改坏了代码，实为环境）。


## 本机 Bash 工具 PATH 故障（2026-09-14 实测，高频）
- Bash 工具启动的 shell 里 `ls` / `dirname` / `find` 会直接 `command not found`
  （报错来自 `app.asar.unpacked/cli/vendor/shim/shell-runtime-bash-env.sh`，
  它自己也因 `dirname: command not found` 而 `cd: null directory`）。
- **修法：每条命令开头加** `export PATH="/usr/bin:/bin:/usr/local/bin:$PATH"`。
  加完 `ls/curl/git/which` 全恢复（实测）。需要 managed python/node 时再追加其绝对路径。
- 症状变体：命令静默返回空 + exit 0 / 127。看到这种输出先修 PATH 再重试，别怀疑文件不存在。


## ⚠️ Bash 工具 PATH 可能整体损坏（2026-09-11 实测，第一个要试的绕法）
- 症状：任何 Bash 命令都报 `shim/shell-runtime-bash-env.sh: line 3: dirname: command not found`
  + `cd: null directory`，随后 `mkdir / ls / grep` 等**全部** `command not found`（exit 127）。
  不是个别命令缺失，是整个 PATH 没注入。
- **绕法 A（最省事，2026-09-11 复验有效）**：命令最前面加一行重新注入 PATH，之后就恢复正常 shell：
  `export PATH="/usr/bin:/bin:/c/Windows/System32:$PATH"; ...`
  实测 `ls / head / wc / curl / python` 全部可用，且**不会**再出现 `command not found`。
  （可用 `;` 串联全部命令，一个 Bash 调用里只注入一次即可。）
- **绕法 B（兜底）**：直接调 managed Python 绝对路径跑脚本，把 shell 要干的活
  （建目录 / 列目录 / grep / 下载 / 跑测试）都写进 .py 里：
  `C:\Users\river\.workbuddy\binaries\python\envs\default\Scripts\python.exe xxx.py`
  注意：即便如此，stderr 仍会打印上面那两行 shim 报错，**但 stdout 与 exit code 正常** ——
  别被 stderr 误导以为命令失败。
- **Bash 与 PowerShell 都可考虑**：本轮 PowerShell 工具（`Get-ChildItem` 等）返回 exit 0 但 **stdout 为空**，
  即 PowerShell 通道也不可靠。**优先 Bash + 绕法 A**。
- 判断依据：先跑一次 `python -c "print('ok')"`；能出 `ok` 就走 Python 路线，别再折腾 shell。
- ⚠️ **管道也会挂**：PATH 坏时 `| tail` / `| head` 同样 `command not found`。
  要截断输出就在 Python 脚本里做（`lines[:40]`），别指望 shell 管道。


## 编辑工具同文件并行 Edit 的 EBUSY 竞态（2026-09-14 实测）
- 在**同一条消息里对同一个文件发多个 Edit 调用**，会因文件锁竞争（`EBUSY: resource busy or locked`）
  导致部分编辑"报成功但落到旧快照"——文件里仍残留未改的旧代码/旧注释，grep 才暴露。
- **规律：同一文件要改多处，必须逐条串行提交（每条 Edit 一个独立消息），绝不在一条消息里并行 Edit 同文件。**
  每条 Edit 后若担心，用 Grep 确认无残留旧符号（如已删除的字段名/函数名）。
- 同理，`git apply` 不认 difflib 手工拼的 unified diff（全部 hunk 起始行失败）→ 生成补丁一律用
  **git 原生 `git diff`**（临时代管仓：基线 commit → 覆盖改动文件 → `git diff`），再 `git apply --check` 验证。


## ⚠️ 改 CRLF 仓库的文件：Edit 工具整块替换会失配（2026-09-11 实测）
- 症状：`Edit` 报 "String to replace not found"，但你刚 `Read` 出来的内容看着一模一样
  —— 因为仓库文件是 **CRLF**，Read 展示的是去掉 `\r` 的文本，整块匹配按 LF 比对就失配。
- **绕法（已验证）**：写一次性 patch 脚本，用 Python 做替换并**显式处理换行符**，
  最后只在有 CRLF 时再整体转回，避免把整个文件改成 LF（那会造成"全文件 differ"）：
  ```python
  src = io.open(P, encoding="utf-8", newline="").read()
  crlf = "\r\n" in src
  work = src.replace("\r\n", "\n")
  ...  # 一系列 sub(old, new)
  if crlf: work = work.replace("\n", "\r\n")
  io.open(P, "w", encoding="utf-8", newline="").write(work)
  ```
- **两个必须加的保险**：① 每个 `sub` 前先 `assert old in work`，缺一个就 `sys.exit(2)`
  **在写盘之前**退出（否则会落一个改了一半的文件）；② 逐段 `sub` 而不是一次性重写整个文件，
  失配时能立刻定位是哪一段。
- 单行级替换 Edit 工具通常没问题，出问题的主要是**多行/整块**（尤其含 docstring 的块）。


## ⚠️ Edit 报「成功」但文件其实没变（假成功，2026-09-19 实测 2 次）
- 症状：`Edit` 返回 `Successfully edited file`，但下一次 `grep` 该文件时**旧内容原样还在**、
  新内容一次都没出现。两次都发生在「一次消息里连发多条 Edit」的场景（同一条消息 2-3 个 Edit，
  不同文件或同文件）。**没有报错、没有 EBUSY，纯静默丢改动。**
- 判据：改完立刻用 Grep/Read 核对**新标记是否存在**（不能只看工具返回的那句 success）。
  本次是靠 patch 脚本里的 `hit()` 断言 + `grep -n "再缀\|再写"` 才发现漏改。
- ✅ 纪律：**一条消息只发一个 Edit**；改完同一轮内用 Grep 复核新字符串命中数 ≥1、旧字符串 =0。
  批量改动仍然走「Python 脚本 + 命中次数断言」那条路（见上节），一次写盘、一次复核。


## ❌ 假绿陷阱：`grep -E` 不支持反向断言（2026-09-10 血泪）
- 用 `grep -nE "...|(?<![A-Za-z])legs?(?![A-Za-z])"` 查禁用词 → **GNU grep -E 不支持 `(?<!...)`**，
  报 `Invalid preceding regular expression` 并以非零退出 → 被 `|| echo "✓ 干净"` 吞掉 → **打印"干净"但根本没检测**。
  本次 6 处 `run_leg` 就是这样被漏掉的（后来用 Python 才查出来）。
- ✅ 正确做法：`grep -P`（PCRE），或**用 Python 复刻护栏的 `re` 规则**（推荐，与护栏逻辑一致）：
  `re.compile(r"(?<![A-Za-z])legs?(?![A-Za-z])", re.I)`。
- 配套纪律：**凡断言"某概念已不存在"，只看代码正文** —— 不看注释、不看 Docs、不看记忆
  （本次"L4 / 腿"两次误判均源于把文档或记忆的印象当成代码现状）。


## ❌ `grep "A\|B\|C"` 多分支会静默丢分支（2026-09-15 实测）
- 用 `grep -n "fee_overrides:\|def __post_init__\|^BASE:\|GENERATED\|def fee_pair"` 查行号，
  **只回了 5 条命中，其中 `GENERATED`/`^BASE:`/`fee_overrides:` 全都没匹配上**（逐个单独查时全部命中）。
- 后果很阴：**看起来"查过了"，实际漏了大半**；若用它做"残留扫描"，会得出"已清干净"的假结论。
- ✅ 规避：① 多目标用 **`for p in ...; do grep -nE "$p" "$F"; done`** 逐个跑；
  ② 或直接 `grep -nE "A|B|C"`（`-E` 才用 `|` 而不是 `\|`）；③ 关键断言一律回落到 **Python 脚本**。
- 与上一条同源：**grep 的"没输出/少输出"永远不能直接当成"不存在"**，要换一种方式复验一次。


## 工具输出捕获经验（Windows PowerShell vs Bash）
- **PowerShell 工具**对 `Compress-Archive` / `Expand-Archive` / `Tee-Object` 等命令的进度条输出大量转义进 stdout，但**退出码和最终结果不可见**——必须用 `Get-ChildItem` / `Get-Content` 二次查询确认
- **Git Bash (Bash 工具)** 适合跑测试脚本 + grep + ls；Python 调用走 `/c/my_chan_project/.venv/Scripts/python.exe`（managed venv 含 pytest）
- **混用策略**：复制文件 + ls + grep + python 跑脚本 → Bash；打 zip / 解压 / 创建目录 → PowerShell


## PowerShell 沙箱坑（2026-09-08 三次踩坑，已锁定方案）
- `Compress-Archive` / `Expand-Archive` 在本机（WorkBuddy Windows）**多次静默失败**：
  - 第一次：解压进度条走完、无报错、文件没落盘（git status --short 无输出）
  - 第二次：打包退出码 0、但 mtime 是上一轮的，文件大小也对得上但内容是旧版
  - 第三次：调用完全没异常，但 `Test-Path` 检查的路径不存在
- **本机打包/解压一律用 Python `zipfile`**：
  - 打包：`zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED).write(p, relpath)`
  - 解压：`z.extractall('.')` 或 `z.extractall(target)`
- PowerShell 只用来跑 `Compress-Archive` 之外的文件操作（`Get-ChildItem` 之类）。


## 清理文件的教训（2026-09-04 踩坑）
**不要用宽泛 glob 删项目文件**。曾执行 `rm -rf trader_gateway/run_*` 想把临时运行目录清掉，
结果通配符把 `run_gateway.py` 一起删了。清理前务必先 `ls <glob>` 确认匹配范围，
或改用精确路径/显式列表。删完重要文件后，务必用新打的交付包**解压到干净目录复验一遍**。


## 文件切分类重构的两个新坑（2026-09-15 P-C 实测）
- **切分文件时，定义在截取标记之前的符号会被静默漏掉**（CN_TZ 在"# ── 建仓日"标记之前 → TradingClock.py 缺定义 → 30 个测试 NameError）。
  防法：切分后必须 ① py_compile（查不出 NameError）② 跑**调用路径**测试（import 冒烟也查不出，要真调 now_cn() 才炸）。
- **`cmd | head -30` 会吞 python 的退出码**（管道退出码 = head 的 0），失败被显示成 exit 0 假绿。
  判失败要看 stderr traceback 或跑完整命令不带管道。


## 批量改测试/源码注释的稳套路（2026-09-16 实测，比逐条 Edit 可靠）
场景：同一个文件要改 3 处以上分散的 docstring/注释，且仓库混着 CRLF 与 LF、
docstring 缩进对不齐（手写精确锚点十有八九失配）。
做法：**写一个 Python 补丁脚本**，每条替换带 `assert count == 1`，并且：
1. 读文件时 `io.open(..., newline="")` → 若含 `
` 先归一成 `
`，写回时再还原；
2. 锚点失配时降级为**行首空格数不敏感匹配**（把每行的前导空格换成 `[ ]*` 正则）；
3. **重跑保护**：若 old 命中 0 次但 new 已存在 1 次 → 跳过（脚本可以放心重跑）；
4. 拆批：一个脚本跑完就落盘，重复运行会因断言撞已落盘内容而失败 —— 第二批另开脚本。
另：把仓库复制到临时目录做复验时**一定要排除 `.venv`**（2.4 万文件），
否则复制几分钟后被超时 SIGTERM，且只复制完顶层、`Trading` 等子目录一个都没有（像“复制坏了”）。

## ⚠️ Windows：`os.kill(pid, 0)` **不是存活探测**，会投一次真实 Ctrl+C（2026-09-19 实证）

**结论**：`signal.CTRL_C_EVENT == 0`，CPython 的 `os.kill` 在 Windows 上遇到 0/1 走
`GenerateConsoleCtrlEvent`，所以 `os.kill(pid, 0)` 的实际效果是
**给该 pid 所在进程组投一次 CTRL_C**；`pid == 0` 时是**广播整个控制台**。
已装 C 级 `SetConsoleCtrlHandler` 计数实测（真控制台）：

| 输入 | 原写法 `os.kill(pid, 0)` | OpenProcess 版 |
|---|---|---|
| 自己的 pid | **False**（把活着的自己判成死） | True |
| 存活子进程 | True | True |
| **pid = 0** | **True + 控制台事件 +1（广播）** | False + 0 |
| 不存在的 pid | False | False |

**症状**：子进程（`CREATE_NEW_PROCESS_GROUP` → **该组 Ctrl+C 被禁用**）调用它毫发无伤、照常 PASS，
而**同一控制台里 Ctrl+C 启用的父进程（宿主/跑测试的进程）收到 KeyboardInterrupt 被中断** ——
表现成"跑到固定某一条，跑测试的进程莫名收到 Ctrl+C，日志里连那条用例的输出都没有"。

**复现条件缺一不可**（这是我一度误判"用户误触"的原因）：
1. **有真实控制台**（`GetConsoleWindow() != 0`）—— 否则 `GenerateConsoleCtrlEvent` 直接失败，
   被 `except OSError` 吞掉，一切正常；
2. **进程 Ctrl+C 处于启用态** —— 该状态**会继承**，也会被 `CREATE_NEW_PROCESS_GROUP` 设置。
   agent/CI 宿主演生的进程树通常全部是**禁用**态 → 事件投过来也无人响应 → **永远复现不出来**。
   ⇒ 造复现环境：中间加一层 stage1，`SetConsoleCtrlHandler(None, False)` 打开 Ctrl+C 后再
   `runpy.run_path(runner, run_name="__main__")`；外层用 `CREATE_NEW_CONSOLE` +
   `STARTF_USESHOWWINDOW/SW_HIDE`（真控制台但不弹窗）。

**修法**：Windows 用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(h,0) == WAIT_TIMEOUT`
纯查询，`pid <= 0` 直接 False；POSIX 保留 `os.kill(pid, 0)`。
**别**拿 `psutil.Process(p).send_signal(signal.CTRL_C_EVENT)` 替代 —— `psutil/_pswindows.py:850`
那分支就是 `os.kill(self.pid, sig)` 的包装，同样中招。

**排查手法**：装 C 级 `SetConsoleCtrlHandler` 计数器，对**同一组输入**跑"原实现 vs 候选修复"
比事件增量 —— 比读代码猜快得多，而且是硬证据。

**另注**：`GenerateConsoleCtrlEvent` 的组 id 语义不完全照文档走（实测把非组长的子进程 pid 当组 id
传进去，**同控制台的父进程也收到了事件**）。所以别只按"它只会影响目标组"来做安全推理。

## 内嵌可视化（show_widget）渲染失败（2026-09-18 实录）

- 本机会话内 show_widget 的 SVG 出现过**静默渲染失败**：宿主不回传任何错误，用户看到的是原始标记文本。SVG 内容本身合法（原样嵌入 HTML 文件后本地打开渲染正常），问题出在宿主内嵌通道。
- 处置：重要图示（用户要反复看、据以讨论的架构图等）**优先走自包含 HTML 文件交付**（铁律 10 套路：无外链、给本地路径），内嵌 widget 只做锦上添花，不做唯一通道。