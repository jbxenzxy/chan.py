# memory-github.md —— GitHub 拉取 · diff · 代码评审


> 本文件由 `~/.workbuddy/MEMORY.md` 于 2026-09-15 拆分而来，**不常驻上下文**。
> 命中 MEMORY.md 索引表的主题词时，用 Read / Grep 按需读取本文件。
> 内容原样搬运未删减；拆分前全文备份：`MEMORY.full-2026-09-15.md`。

## ⚠️ 拉取方式 · 2026-09-18 二次踩坑（放最前面，别再试错）

**不要再试这两条路**（本次实测各白烧 8~9 分钟）：
- `codeload.github.com/.../legacy.tar.gz/<sha>` —— 跑了 8m22s 仍无输出，杀掉。
- `git clone --depth 1 --branch <br> --single-branch` —— 跑了 9m33s，**产物损坏**：
  只剩 `.git`（16M），`git log` 报 `your current branch appears to be broken`，
  `git ls-tree -r HEAD` 只返回 1 条。**别指望用它兜底。**

**唯一正解（实测 245 个 .py 约 8 秒，0 失败）**：
1. `api.github.com/repos/<o>/<r>/git/trees/<sha>?recursive=1` → 全量 path/size 清单；
2. 按目录前缀 + 扩展名筛（如只要 `.py`）；
3. `raw.githubusercontent.com/<o>/<r>/<sha>/<path>` + `ThreadPoolExecutor(max_workers=8)` 并发拉；
   跳过已存在且 size>0 的文件做增量。
   ⚠️ **该 skip 逻辑只比 size**（2026-09-22 踩到）：同 size 的内容改动会被静默漏拉。
   **要跟远端最新比对时，拉到新目录（`repo_latest`）再逐文件 md5 diff，绝不覆盖自己的工作树**
   （否则本地改动被冲掉、且无法判断差异来源）。脚本见沙盒 `_pull2.py` + `_diff_latest.py`。
4. 比差异用 `api.github.com/repos/<o>/<r>/compare/<base>...<head>` → `files[].patch` 直接是 diff，
   按文件名存成 `.patch` 再读，比拉两份全文比对快得多。

**⚠️ 下载过滤别用"目录前缀白名单"**（2026-09-23 踩到）：按 `Trading/ App/ Frontend/ Test/ Docs/` 过滤会
  漏掉**仓库根**的 `run_all_tests.py` / `.env.example` / `Chan.py` / `ChanConfig.py` / `main.py`，
  以及 `Bi/ BuySellPoint/ ChanModel/ Combiner/ Common/ DataAPI/ KLine/ Math/ Plot/ Seg/ ZS/` 这些
  根目录包 —— 前者导致"全量入口不存在"，后者让依赖链测试挂。
  **正确过滤 = 只排除 `Image/`**（+ 保留 `.env.example`/`.gitignore`），实测 262 个文本文件约 16s 下完。
- **全量测试规模已涨到 117 个**（2026-09-23 实测，`run_all_tests.py --list`；旧记录写的 111 已过时）。
  一轮 5~7 分钟。**A/B 评审的正确姿势**：拉两份树（`repo_latest` / `repo_base`）各跑一轮再比失败清单，
  不要凭单轮失败就判回归 —— 本次基线反而多红 1 个（phase7 两条是已知偶发）。

**跑测试时的依赖链坑**：只下 `Trading/ App/ Frontend/` 会让 4 个测试挂（都是沙盒缺文件，不是真失败）：
`test_p47` → `DataAPI.TqSdkAPI` → `CommonStockAPI` → `KLine` → `Math`；
`test_p56` 需 `PYTHONPATH=<repo根>`（它 `from Trading.Infra.Instrument import ...` 但没自己插 sys.path）；
`test_p58` 需根目录 `.env.example`；`test_trade_stats_ratio_naming` 需根目录 `FrontAPI.py`。
→ **直接下全量 `.py`（245 个，8 秒）+ 补 `.env.example`/`FrontAPI.py`/`Frontend/app.css`，一劳永逸。**

补充（2026-09-22 实测，只下 .py 仍会假红 4 个）：必须再补 **`Frontend/app.js`、`Frontend/index.html`** ——
`test_p26`（术语守卫覆盖面自检要扫 app.js）、`test_p47`、`test_p59`（读 index.html 核前端勾选框）、
`test_trade_stats_ratio_naming`（读 app.js）缺它们直接 FileNotFoundError；补齐后 24/0、83/0、57/0、37/0 全绿。
另：`test_p60_trade_toasts_reconcile_gap.py` 批量串行跑可能因 tqsdk websockets 任务残留返回 exit=1（耗时 ~159s），
单独跑 exit=0 且 ✗ 计数 0 → 判定为环境噪音，别当真红。
**补充（2026-09-22 晚复测）**：若**裸跑脚本**（不经过 `run_all_tests.py`）会**挂死到超时 rc=124**，日志显示真去连 CTP 登录并抛 `TqTimeoutError`
  —— 因为 `run_all_tests.py` 会**清空 `SN_*`/`TQ_*` 凭据**，裸跑环境里凭据还在 → 走真实登录分支。
  判据统一为「**看它是否在连 CTP/天勤**」：连 → 环境噪音；不连且断言红 → 才可能是真红。该用例在改后全量回归里 PASS(1.15s)。
跑法：`cwd=<repo根>` 且 `PYTHONPATH=<repo根>`，58/58 可全绿。

**⚠️ 重建旧树时「先下新树 → copytree → 用同一个 fetch 脚本补旧版文件」会被 skip 逻辑吃掉**
（2026-09-23 踩到）：脚本里的 `if os.path.exists(dst) and size>0: return "skip"` 会把
"从新树拷过来的旧树文件"当成已下载 → **52 个旧版一个都没下**，而校验脚本给出的却是
`SAME=329 DIFF=0、unexpected=[]` 这种**完美假象**（因为确实 0 处非预期差异）。
**判据**：重建后 `DIFF` 必须等于 `len(modified)` 且 >0 —— `DIFF=0` 就是没下。
**做法**：旧版一律先下到独立目录（`_old_raw`），再 `shutil.copyfile` 覆盖进旧树。

补充（2026-09-22 下午实测，跑**全仓 111 个测试文件**时的假红清单）：
只下 `.py` + 上述 4 个前端文件仍会假红 5 个（`test_determinism` / `test_lookback_truncation` /
`test_trigger_step_replay` 缺 `Test/fixtures/stock_day.json` 等、`test_phase2_guards` 缺
`Test/fixtures/futures_15s.json`、`test_vol_macd_mode` 缺 `Test/snapshots/*.json` 且先报
`UnboundLocalError: got` 再崩）→ **再补 `Test/fixtures/` 与 `Test/snapshots/` 两个目录**后
111/111 全绿。判假红套路：失败栈是 `FileNotFoundError` 且路径指向沙盒内 `fixtures/`、
`snapshots/` → 补文件复跑，别当真红。

---

## GitHub 拉取方式（2026-09-10 更新，Windows 本机）
- **`git clone` 在本机走 schannel 会失败**：`fatal: ... schannel: next InitializeSecurityContext failed: CRYPT_E_NO_REVOCATION_CHECK (0x80092012)`；
  加 `GIT_SSL_NO_VERIFY=1` / `-c http.sslBackend=openssl` 也不行（会挂到超时）。
- **⚠️ tarball 通道已不可靠（2026-09-10 实测）**：`curl -k -sL -o x.tar.gz https://codeload.github.com/<owner>/<repo>/tar.gz/refs/heads/<branch>`
  **固定卡在 7,687,103 字节处**（chan.py），`--max-time` 到点返回 **exit 28**；
  加 `-C -` 想续传返回 **exit 33**（服务端不支持 Range）。**别再在这条路上耗轮次。**
- **✅ 首选方案：GitHub API 文件树 + raw 通道逐个取 blob**（本轮 74 个文件 0 失败，秒级）：
  1. `curl -k -sL -o tree.json "https://api.github.com/repos/<owner>/<repo>/git/trees/<branch>?recursive=1"`
     → 拿到全仓 path/sha/size（chan.py 全仓 376 个 blob）；
  2. `curl -k -sL "https://api.github.com/repos/<owner>/<repo>/commits?sha=<branch>&per_page=15"`
     → 判断"合到哪个 commit"（chinese commit message 全是"更新"，只能靠时间戳定位）；
  3. 遍历 tree 里 type=blob 的条目，`https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>`
     逐个下载（urllib + 未校验 ssl context 等效；每个重试 3 次）。
  **下载范围要覆盖跑测试所需的前置目录**（只取 `Trading/` 会让 P20 从 82 项掉到 40 项 → 必须补 `App/`）。
- 拿最新 commit 列表也可用 WebFetch `https://github.com/<owner>/<repo>/commits/<branch>`（不必下载代码）。
- 比对远端 vs 本地务必**忽略 CRLF**（`.replace(b"\r\n", b"\n")` 或 `--strip-trailing-cr`），
  否则本地 CRLF 会造成"全文件 differ"假象。


## 拉取 GitHub 大仓库做代码评审的高效路径（2026-09-09 固化，实测有效）
- 背景：chan.py custom-dev 全量 zip 约 60MB，本机 `curl codeload` 需 15 分钟且常超时（exit 28）；
  `-C -` 断点续传**不被支持**（HTTP 33）；git clone 也失败。
- **正解（快且准）**：
  1. `curl -sS -k https://api.github.com/repos/<owner>/<repo>/commits?sha=<branch>&per_page=10`
     → 拿到最近提交 sha / 时间 / message，定位"上次评审快照之后的新提交"。
  2. `curl -sS -k https://api.github.com/repos/<owner>/<repo>/commits/<sha>` → 响应里
     `files[].patch` **直接就是完整 diff**，评审主要靠它，省去大部分文件读取。
  3. 需要跑测试/冒烟时，只抓变更文件：
     `https://raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>`
     覆盖到上次快照目录上即可重建 HEAD 树（Python `urllib` + `ssl.CERT_NONE` 可直连）。
  4. **交叉校验**：把重建树与后台慢慢下的全量包 `diff -rq` 对比，完全一致才证明没漏文件。
- 好处：评审从"等 15 分钟下载"变成"几十秒拿补丁"，且能精确界定"哪些文件变了"。
- ⚠️ **中文文件名必须 URL 编码（2026-09-11 踩坑）**：raw 通道对 `Docs/止盈止损/xxx.html` 这类路径
  会抛 `'ascii' codec can't encode characters in position ...`（17 个文件全挂）。
  必须 `urllib.parse.quote(path)` 后再拼 URL；**只对 path 编码，不要编码整个 URL**。
- 全量拉取用 `git/trees/<branch>?recursive=1` 拿 path/size/sha 清单，
  再按扩展名过滤（跳过 `Image/` 等资源目录），并用 `size` 做**断点续传式**跳过已下好的文件。
  328 blob / 61.6MB 的仓库，过滤后仅 282 个文本文件，1.5 分钟下完 0 失败。


## 评审 git ref 差异时的高效套路（已验证）
1. `api.github.com/repos/<o>/<r>/git/trees/<ref>?recursive=1` 拿两棵树的
   path+sha（**含 sha → 直接比 sha 就得到改动清单**，不用下全仓）；
2. 只下"改动的 + 新增的"文件（两 ref 各一份），本地 `difflib` 出 diff；
3. 需要跑测试时再下全量树（334 文件，8 线程约 2 分钟；中文名 Docs 会 404，可忽略）；
4. 定位被删功能：对 diff 里所有 `^-` 行 grep `def |class `，一眼看全删除清单。
5. Bash/PowerShell 的 PATH 偶发被沙箱破坏（`ls`/`head`/`tail` 都 not found）→
   直接改用绝对路径 Python 干所有事，别在 shell 上耗轮次。
