---
name: git-ref-behavior-diff-review
description: 对比同一仓库两个 git ref（分支/tag/commit）之间的**行为差异**并评审改动质量。当用户提出「拉取 X 分支跟 Y 比对」「看修改了哪些功能」「改得是否OK/是否引入问题」「这两个版本有什么行为变化」时使用。产出：改动清单 + 行为差异实证 + 引入问题分级 + A/B 回归结论。适用于任何语言的仓库。
agent_created: true
---

# 两 git ref 行为差异评审

目标不是复述 diff，而是回答三个问题：**改了什么功能 / 改得对不对 / 有没有引入问题**。
核心手段：**A/B 对照**。任何"测试失败"或"输出变化"都必须在新旧两版上各跑一次，
只有**失败项集合不一致**才能归因到本次改动。

## 铁律

1. **先辨明 ref 类型**。`git branch -r` 与 `git tag -l` 都要查——`/tree/xxx` 在 GitHub 上
   分支和 tag 都这么显示。tag 不是分支，无法 `git checkout` 成跟踪分支。
2. **不归因未 A/B 过的失败**。仓库常有一批"环境性"失败（缺依赖、缺 gitignore 的本地缓存）。
   基线跑一遍，失败项集合相同 → 明确写"既有环境问题，非本次引入"。
3. **区分源码改动与生成物**。快照/锁文件/产物 JSON 的重冻结不算逻辑改动，
   但它们**记录了行为变化**，是定位语义改动的最快入口。
4. **数字说话**。不要写"可能会影响"，要写"29 次调用中 6 次踩空，其中 3 处判定翻转"。

## 流程

### ⓵ 拉取与关系判定

```bash
git clone <url> repo && cd repo
git fetch origin <refA> <refB>
git branch -r | head -40          # 是分支吗
git tag -l                        # 是 tag 吗
git merge-base <A> <B>            # 共同祖先
git log --oneline --graph --format='%h | %ad | %an | %s' --date=format:'%m-%d %H:%M' <A>..<B>
git log --oneline <B>..<A>        # 反向：A 独有的提交（分叉回退）
```

只有 `<A>..<B>` 非空、`<B>..<A>` 为空 → B 是 A 的快进，改动范围就是那几个提交。

**⚠️ `git clone` 不可用时的替代通道（先试再决定，别在 clone 上耗时间）**

本机（Windows / Git Bash）实测：`git clone` 走 schannel 报
`CRYPT_E_NO_REVOCATION_CHECK`；`curl` 下 codeload tarball 会在 **7.6MB 处卡死**
（`curl -C -` 续传返回 **exit 33**，服务端不支持 Range），加 `-k` / 换 sslBackend 都无效。

**可用方案（按推荐顺序）**：

```bash
# ① 文件树（一次拿到全仓清单 + 每个 blob 的 sha/size）
curl -k -sL -o tree.json \
  "https://api.github.com/repos/<owner>/<repo>/git/trees/<branch>?recursive=1"

# ② 提交列表（判断"合到哪个 commit"、比对本轮改动的时间边界）
curl -k -sL -o commits.json \
  "https://api.github.com/repos/<owner>/<repo>/commits?sha=<branch>&per_page=15"

# ③ 按 tree 清单逐个取 blob（raw 通道最稳，74 个文件 0 失败）
curl -k -sL -o <local> "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>"
```

**⚠️ raw 通道的 ref 只能是 commit sha / branch / tag，不能是 blob sha。**
实测：为了按内容去重，用 `raw/.../<blob_sha>/<path>` 去下，**344 个文件全部 404**。
正确做法是 `raw/.../<commit_sha>/<path>`（commit sha 就是 tree API 用的那个 ref），
本地缓存仍可按 blob sha 命名去重——只是 URL 里的 ref 必须是 commit/branch/tag。

**先查 ref 类型再动手**（比 clone 快，且 clone 在本机本来就不可用）：
```bash
curl -k -sL "https://api.github.com/repos/<o>/<r>/branches?per_page=100"   # 分支
curl -k -sL "https://api.github.com/repos/<o>/<r>/tags?per_page=100"       # tag
curl -k -sL "https://api.github.com/repos/<o>/<r>/compare/<A>...<B>"       # ahead_by/behind_by + files 清单
```
实测形态：`/tree/09132` 看着像分支，实际是 **tag**（`branches` 只有 custom-dev/main）；
而 `custom-dev` 的 HEAD 同时又打了 tag `0914` —— 用 tags 表能直接把"分支 HEAD"和"tag"对上。

写个小脚本遍历 `tree.json` 批量下载（**只取 blob，跳过 `__pycache__` / 产物**），
每个请求重试 3 次即可；`urllib` + `ssl._create_unverified_context()` 与 curl 等效。
**别只下"我关心的那几个文件"** —— 复验需要能跑的完整树（含测试、App/、根入口）。

**⚠️ 300 个 blob 的下拉必须放后台跑，且脚本必须支持续传。** 本机实测：前台跑约 260 个文件后
被 **SIGTERM 掐断（exit 1、stdout 全空）**，看起来像"脚本报错了"，其实只是跑太久。
**做法**：① 脚本开头 `if os.path.exists(dst) and os.path.getsize(dst) > 0: return "skip"`
（幂等跳过已下好的，重跑即续传）；② 用 `run_in_background` 跑，剩余 25 个文件 19 秒补齐。
判据仍是最后的 `FAIL = 0`；`fetch.py` 打完要能反复重跑而不重复下载。

**⚠️ raw 通道的路径必须 URL 编码，否则非 ASCII 文件全军覆没** —— 实测：`urllib` 直接拼
中文路径会抛 `'ascii' codec can't encode characters in position 40-43`，导致 **Docs 下所有
中文名文件（`止盈止损/*.html`、`六家期货交易所_*.html`…）100% 下载失败**，而英文路径全部成功，
于是你会在不知情的前提下做缺文件的比对。**写法**：`urllib.parse.quote(path)` 只作用于 URL，
本地落地路径仍用原始串。判断是否补齐：最后 "[done] ok/fail" 的 **fail 必须为 0**。

**⚠️ `compare` API 的 `patch` 字段可能静默截断 —— 绝不能只凭它下结论。**
实测：某次 `compare/A...B` 返回的 `Trading/Config.py` 只有 2 个 hunk（+6/-9），
而真实文件差异里还有一个**删掉 3 个配置字段**的 hunk 没返回。若只看 API patch，
会漏掉"入场过滤字段被删"这条主线。
**判据**：拿到 tree 后**用真实文件逐文件重算 diff**（脚本遍历比较，
`open(...).read().replace("\r\n","\n")` 归一化），把 API 的 `files` 清单只当**范围提示**。
命名建议：`treediff.py` 输出「删除 / 新增 / 修改」三段 + 总文件数，与 API 的 `files.length` 对得上才算范围正确。

**⚠️ `git apply` 在 Windows 上不认 `/c/Users/...` 这种 MSYS 路径** → 报
`can't open patch '...': No such file or directory`，而**同一命令里 bash 是能解析该路径的**，
于是你会以为补丁打过了、后面所有冒烟测试其实都跑在**未打补丁**的树上（实测踩到）。
**判据**：`git apply` 一律用 `C:/Users/...` 正斜杠 Windows 路径；打完先跑一条
「应用前红、应用后绿」的差异断言自证补丁真的生效了。

**⚠️ 只下想要的目录会漏掉跑测试所需的前置**（踩过：只取 `Trading/` → P20 从 82 项掉到 40 项，
因为它探测 `App/` 是否存在来决定测试规模）。先看清"测试入口依赖哪些顶层目录"再定下载范围。

**⚠️ 测试跑完会在工作树里生成运行时产物**（如 `_verify_A/`、`_verify_B/` 下的 `state.db`、
`__pycache__/`）。打包前必须清掉，否则会混进交付包；比对"远端 vs 本地"时也要先排除它们，
否则会看到一批假差异。

### ⓶ 拆分改动

```bash
git diff --stat <A> <B>                    # 看分布：源码 vs 快照/产物
git diff <A> <B> -- <源码文件>              # 只读源码 diff
```

按目录/扩展名把文件分成「源码」「生成物」「测试」。源码通常只有几个文件；
生成物（snapshots、*.lock、generated）的变化是**行为变化的指纹**，留到 ⓷。

### ⓷ 生成物逐字段 diff（最快定位语义改动）

对 JSON 快照类文件，写脚本做结构化 diff，把列表下标折叠掉后统计"哪些字段变了"：

```python
# 递归 walk(dict/list)，收集 (归一化路径, 旧值, 新值)
# 归一化：re.sub(r"\[\d+\]", "[]", path)
# 然后按归一化路径 Counter 聚合 → 一眼看出「只有坐标字段变了」还是「业务字段也变了」
```

**关键判据**：如果变化只落在"输出坐标/时间戳"类字段 → 纯展示变化；
如果落在"结果/计数/判定"类字段（如 bsps、count、verdict）→ 有实质行为变化，必须深挖。

### ⓸ 调用点全量核查

对改动的每个函数/符号：

```bash
grep -rn "<symbol>" --include="*.py" .     # 或 Grep 工具
```

列出全部调用点，逐个判断语义影响，**并显式列出"已核查但不受影响"的路径**
（排除嫌疑和发现问题同等重要，尤其当仓库里存在名字相似的其他实现时）。
同时检查**前端/其他语言侧是否有同一逻辑的第二份实现**——若前后端各算一遍，
只改后端会造成错位。

**⚠️ 改名类改动必须做 AST 级扫描，`grep` 会漏 —— 一条符号能在四个地方"活"着：**

```python
import ast, os, io            # Name(A 处) / Attribute(B 处) / ImportFrom(C 处) / Import(别名)
for dp, dns, fns in os.walk(ROOT):
    dns[:] = [d for d in dns if d not in {"__pycache__",".git",".venv","Image","Docs","Frontend"} and not d.startswith(".")]
    for fn in fns:
        if not fn.endswith(".py"): continue
        fp = os.path.join(dp, fn); rel = os.path.relpath(fp, ROOT).replace("\\","/")
        try: t = ast.parse(io.open(fp, encoding="utf-8", errors="replace").read(), filename=rel)
        except SyntaxError: continue
        for n in ast.walk(t):
            if isinstance(n, ast.Name) and n.id == SYM:      hits.append(f"{rel}:{n.lineno} Name")
            elif isinstance(n, ast.Attribute) and n.attr == SYM: hits.append(f"{rel}:{n.lineno} Attr")
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == SYM: hits.append(f"{rel}:{n.lineno} ImportFrom")
```

**`ImportFrom` 是最常漏的一路**（本次实测：`DefaultEntryPolicy` 改名后残留
**48 处活引用 / 21 个文件**，含**生产入口** `Trading/main.py`；而仓库里的静态守卫
只检查了 `ast.Name`/`ast.Attribute`，**完全没查 `ImportFrom`** —— 于是守卫对自己
覆盖的 3 个符号做了"判别力自检"、对本次改的符号却全程沉默）。
§⓸ 的判据是：**改名后 `grep -c` 与 AST 计数都应为 0，且两个数要能解释彼此的差**
（差的通常是注释/docstring，可接受）。若相差几十，就是漏改。

**⚠️ 同一次提交新增/改动的"防回潮护栏"要专门审它覆盖了哪些符号** ——
最常见的形态是：守卫覆盖的是**上一轮**删的符号，而本轮改的符号不在清单里。
**判据**：把本轮改动的符号逐个代入守卫的集合，确认每个都在里面；不在就是盲点
（且要给一组"注入 → 变红"的反向验证，见下条）。

**⚠️ 文本/子串型护栏会被"前缀"骗过 —— 这是最隐蔽的一类假绿。**
实测：某守卫断言 `"EntryPolicy(cfg.entry_params" in main_py_text`，而 `main.py`
里写的是 `DefaultEntryPolicy(cfg.entry_params.model_dump())` ——
**前者是后者的子串 → 断言 True → 测试全绿，而生产入口其实 ImportError**。
**修法**：静态文本护栏一律用**行首锚定 + 词边界**的正则，别用裸 `in`：
```python
re.search(r"^\s*entry\s*=\s*EntryPolicy\(", txt, re.M)   # 对；前缀骗不过
"\bEntryPolicy(cfg.entry_params" in txt                   # 仍不够（\b 在 D 后不成立？实测能挡，但行首锚定最稳）
```
**反向自检**：改完守卫后，把**旧名**手动塞回源码跑一次，确认它**必须变红** ——
和"注入 1 处遗漏 → 护栏变红 → 撤销 → 复绿"是同一个套路，没有这一步就不算护栏有效。

**⚠️ 「断言跟着实现一起改」是最强的伪装 —— 全绿 ≠ 无回归。** 当改动同时改了解释行为的断言
（把原本的"防止退化"翻转成"确认退化"），A/B 双绿只是说明**两边自洽**，什么都没证明。
本次真实例子：`R=max(A,B,min_r_points)` 删掉 `min_r_points` 后，`test_p8` 的
「无 ATR 回退 → 止损=98」被改写成「R=0 → 止损=99.8（P2 边界保护）」，于是
「R=0 时 L3 保本/跟踪整层被 `if use_trailing and R:` 静默跳过」这条 P1 全仓无人报警。
**判据**：diff 里凡是「同一行/同一节断言的**期望值被改写**」的，都标记为「断言已翻转」，
必须另写独立探针在两树上**直接取值**对比 value_new vs value_old（如逐个 0.1 点扫描阈值、
打印 R / 止损距 / 锁利落点），用数字而不是套件颜色来回答"行为变了多少"。

**⚠️ 行为探针优先"复用该模块的测试构造器"** —— 别自己 Fake 对象。
写法：`importlib.util.spec_from_file_location` 加载新版/旧版各自的
`<套件>.py`，取出里面的 `make_*` 构造器与被测类，两版共用同一套输入口径。
比手写桩可靠，也比重跑整个套件快一个量级（本次 6 个场景 2 版 ≈ 秒级）。

**⚠️ 计数脚本要同时认多种打印风格** —— 实测 `[PASS]` 计数对部分文件为 0（该仓用 `✓/✗`），
只看 `[PASS]` 会误判"这个文件没跑"。**做法**：`✓|✔|[PASS]`、`✗|✘|[FAIL]` 都算，
但仍然**以退出码为主判据**。

**⚠️ 「同时改了源码和测试」的提交，要在报告里显式列出断言翻转清单** ——
逐条写清「文件:行 + 旧期望 → 新期望」，让用户能一眼复核这是"想要的语义变更"
还是"仅仅把测试改绿"。本次正是靠这张清单才让 P1 浮出水面。

### ⓹ A/B 回归（归因的唯一依据）

```bash
git worktree add ../repo_base <A>     # 用绝对路径，Windows 上相对路径会生成 C:\c\... 怪路径
```

**⚠️ 走 API/raw 通道（clone 不可用）时，用"旧树重建法"而不是把两棵完整树都下一遍：**

`tree_new` / `tree_old` 两份 JSON 已给出每个 blob 的 sha，所以只有"sha 不同"的文件才需要旧版。
做法：`copytree(new_dir, old_full)` → 只把**改动过的**文件用旧版覆盖 → 删掉**新增的**文件。
成本直接砍半，且能保证两棵树"只在改动处不同"。重建完**必须**校验指纹并写成断言：

```python
# 期望：非改动文件 SAME，改动文件 DIFF —— 两个数都要与预测一致才叫重建成功
# 实测：SAME=264 DIFF=17  预期 DIFF=17   → 一致
same = diff = 0
for p in sorted(N):                       # N = 新版全量 blob 清单
    a, b = os.path.join("new", p), os.path.join("old_full", p)
    if not (os.path.exists(a) and os.path.exists(b)):
        if p in added: continue           # 新增文件在旧树里不存在 = 正常
        bad.append(p); continue           # 其余缺失 = 下载漏了
    if md5(a) == md5(b): same += 1
    else:
        diff += 1
        if p not in modified: bad.append(p)   # 非改动文件却不同 = 重建错了
```

判据：`bad == []` 且 `diff == len(modified)`。**没打印这两个数就等于没验证**（本地 CRLF、
下载漏文件都会让 DIFF 数悄悄变大而你看不见）。缺的文件若只落在 `Docs/`、`Image/` 等
与测试无关的目录，可以接受，但要在报告里写明"跳过范围"。

**⚠️ 判据要写成 `diff == len(modified ∩ 两树都在的)`，并显式排除"仓库里被跟踪的运行时产物"。**
实测两次踩到：① 仓库把测试跑完写出的报告（如 `Test/report.json`）也跟踪了，两棵树各自跑完
测试后它必然不同 → 白送 1 处 DIFF；② 更阴的是**这个噪声数会和预期数撞上**——本次
`DIFF=45` 恰好等于 `len(modified)=45`，若不逐条核对就会误判"全部对应"；真相是
`44 个改动文件 + 1 个 report.json`，另有 1 个改动文件（37MB 二进制）两树皆缺。
**做法**：重建校验脚本里维护一个 `RUNTIME = {"Test/report.json"}`，DIFF 计数时把它单列，
并**打印「改动文件中两树皆缺」的清单**（本次正是靠它发现 `.docx` 未覆盖）。

两个工作树各跑一次回归，逐项比对：

```bash
cd repo      && python <test_entry> 2>&1 | grep -E "^\[(PASS|FAIL)\]|^====="
cd repo_base && python <test_entry> 2>&1 | grep -E "^\[(PASS|FAIL)\]|^====="
```

失败项 → 单独跑看真实原因（多为 `ModuleNotFoundError`）。**两版失败集合相同即放行。**

**⚠️ 每轮跑之前必须打印状态指纹，否则 A/B 结论可能是假的：**

```bash
git status --short          # 必须为空（干净基线）／必须有改动（打补丁版）
md5sum <被测的关键文件>       # 两轮的指纹必须不同，否则跑的是同一份代码
```

踩过的坑：`git checkout -- . && git clean -fdq` 之后工作树又被污染，两轮实际跑了同一份代码，
"输出完全一致 → 无回归"是空结论。加指纹校验后重做才拿到真结论。**指纹不同才算做了对照。**

**⚠️ 套件级失败数不能直接当回归依据 —— 必须对每个失败项单独重跑复核。**
实测遇到过：某组件在套件里 FAIL、单独跑却 7/7 全通过，原因是**沙箱/环境策略**拦住了它
清理临时 fixture（如 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`，删除计数超阈值），
断言本身全绿、只是收尾清理失败导致退出码非 0。
**判据**：把失败组件单独重跑；若全绿，则失败归因于环境而非代码。同理，套件里的
间歇性失败项（快照夹具漂移等）在不同轮次会漂移，导致"43/46 vs 44/46"这种假差异。

依赖装进隔离 venv，不要污染全局：
```bash
<managed-python> -m venv <env-dir>
<env-dir>/Scripts/pip install -q <deps>
```

**⚠️ 判据用「退出码」，不要用输出里的标记计数。** 同一套脚本式测试里可能混着
多种打印风格（`[PASS]` / `✓` / `ok`），用 `^\[PASS\]` 统计会把一批文件数成 `0P 0F`
并误判为"没跑"。**做法**：判 `returncode == 0`；标记计数只作参考。
另外 `0P 0F 且 rc=0` 是个红旗 —— 说明你的正则没匹配上该文件的风格。

**⚠️ 写一个批量 runner，别手动逐个跑。** 42 个文件 × 3 个版本 = 126 次执行，
手跑必漏。runner 要：`subprocess.run([py, path], cwd=repo, env={PYTHONPATH: repo}, timeout=300)`
→ 存 `{rc, pass_cnt, fail_cnt, log}` 到 JSON → 再写一个差分脚本算
「A 绿 B 红 / A 红 B 绿 / 两版都红 / 新增文件」四类。**这三份 JSON 就是 A/B 结论的原始证据**，
交付时一并给出。

**⚠️ 一定要有一条「独立复现路径」把"只是测试写错"和"生产真的坏了"分开。**
本次关键佐证来自 `test_p21_stop_e2e.py` —— 它**真实 `Popen` 拉起生产入口 `main.py`**，
首条断言 `子进程启动并落盘 start 事件` 失败。这条从**进程级**证明网关起不来，
而其余 22 个失败都只是 `ImportError`（可被辩解为"测试没跟上改名"）。
**判据**：找/写一条不经过测试自身 import 链的端到端路径（拉子进程 / 跑 CLI /
`python <entry> --help`）。本次 `python Trading/main.py --help` 在打补丁前返回
ImportError、打补丁后返回 0 —— 一句话就是最硬的证据。

**⚠️ 交付前必须"从交付物本身"复验，不要拿工作副本的结果顶替。**
把补丁**打进 zip、再把 zip 解压到全新目录**，在那里 `git apply` + 跑回归。
实测教训：一次 `git apply` 因 `/c/...` 路径失败（见 ⓵），后续冒烟全跑在未打补丁的树上，
结论完全无效却看起来"全绿"。**判据**：应用后必须看到"应用前红 → 应用后绿"的
同一命令差异，否则等于没验证。

**⚠️ 交付补丁用原生 `git diff`，并做双向验证。**
流程：干净树复制 → `git init && git add -A && git commit`（建立基线）→ 改 → `git diff > fix.patch`。
然后三步验证，缺一不可：
1. `git apply --check --ignore-whitespace fix.patch`（打到干净基线）；
2. `git apply` 后逐文件 md5 比对编辑树（**忽略 CRLF**），确认"补丁 == 我改的东西"；
3. 反向 `git apply -R`，确认 9 个文件回到原版 md5 —— **可一键回退**是交付的硬要求。

**⚠️ Edit 插入代码块容易缩进错位（一个空格就能让整文件 IndentationError）。**
从 Read 输出复制时会带上不可见的缩进偏差（同时本地套 4 空格、我却贴成 5 空格），
而这 Januar 却成功写入、下一次运行才报 `IndentationError: unexpected indent`。
**做法**：插完立刻扫一遍异常缩进再跑：
`python -c "print([l[:60] for l in open(f) if l.startswith('     ') and l.strip()])"`，
或对每个待插入行先 `repr()` 核对。**每插入一段就跑一次语法检查**，不要攒到最后。

### ⓺ 埋点定位根因（快照说不清"为什么"时用）

当知道"结果变了"但不知道"哪一步变了"，在关键函数上打 wrapper，输出决策轨迹，
两版各跑一次再 `diff`：

```python
_orig = mod.func
def _wrapped(*a, **kw):
    r = _orig(*a, **kw)
    LOG.append((inputs, r))      # 记录输入与输出
    return r
mod.func = _wrapped
# 跑目标用例 → 打印 LOG → 两版 diff
```

埋点选在**判定分叉点**（如"找到/没找到"、"返回 True/False"），不要埋在叶子函数。
轨迹 diff 能直接指出"哪一步的输入变了 → 导致哪个分支被走"。

### ⓻ 量化影响

把轨迹解析成结构化记录，统计：
- 各分支被走中的次数（新旧对比）
- 判定翻转的方向与根因（每个翻转都要能说出"因为什么从 X 变成 Y"）
- 边界条件的覆盖率变化（如"内缩/外扩"的笔数）

**⚠️ A/B 探针必须每棵树起一个独立进程。** 两棵树的顶层包名相同（如都是 `Trading/`），
在一个进程里先 import A 再 import B，**B 会命中 `sys.modules` 里 A 的缓存**，
输出的却是"A 的结果两次"——表现为「两版完全一致 → 无回归」的假结论。
实测踩过：同一进程跑的 `probe_rfloor.py`，B 版给出的 R 值与 A 版一模一样，
改成 `python probe.py <treeA> > A.json` / `<treeB> > B.json` 两个进程后立刻出现差异。
**判据**：探针输出里必须能看到两棵树的指纹差异（如某个已知改动的字段值不同），否则先怀疑进程污染。

**⚠️ 改动若**改名/删字段**，探针不能硬编码参数键名 —— 要按"参数意图"构造，兼容两版键名。**
本轮的实例：`breakeven_buffer_ticks` → `breakeven_buffer_r`，同一个"保本缓冲"意图在两版叫不同名字。
直接写 `LayeredExitPolicy({"breakeven_buffer_r": 0.5})` 去跑旧树会因 `extra="forbid"` **直接抛
ValidationError**，于是旧树那一格变成 error、对照表缺一半。做法：

```python
def make_policy(**kw):
    want_buffer = kw.pop("_buffer", None)        # 用"意图"作为入参，不用字段名
    if want_buffer is None:
        return LayeredExitPolicy(kw)
    try:
        return LayeredExitPolicy(dict(kw, breakeven_buffer_r=want_buffer))   # 新版键名
    except Exception:
        return LayeredExitPolicy(dict(kw, breakeven_buffer_ticks=want_buffer))  # 旧版键名
```

**判据**：探针两版都 `exit=0` 且 JSON 里**没有 `error` 字段**。出现 `error` 就是键名撞墙，
先修构造器再对比，别把 `error` 当成"行为差异"写进报告。

**⚠️ 审"新增校验"时，必须对它的**声称**做反向构造 —— 把参数推到校验刚通过的边界。**
本项目实例：新增 `model_validator` 断言 `breakeven_buffer_r < breakeven_trigger_r`，
docstring 声称这能防止"保本止损被抬到市价之上，下一根 bar 立刻被硬止损出场"。
实测把参数取到**刚好通过**的边界（buffer=0.99 / trigger=1.0）——**故障照样复现**：
落点 110.0 而当时收盘 100.5，下一根判 `sl`。原因：落点只与 R 比较、从不与收盘价比较，
而触发判定用的是**根内有利极值** best，两者之间天生留有空隙。
**判据/话术**：新增校验一律写成"**必要但不充分**"，并给出"边界取值的反例"；
报告里要区分"这是校验没做到它声称的事"（实现缺口）与"这是口径变更的自然后果"（需回测）。

**⚠️ 定级前先把"合取条件"拆干净 —— 别拿必要条件当充要条件。**
典型翻车：断言「R=0 会让某层静默失效」被用户一句"有分型才有买卖点，入场时 R 必然已算好"反驳。
核查后真相是：`R = max(A, B)` 里 A、B 是两条**独立**的腿，用户的链只覆盖了其中一条。
**做法**：给每个输入单独列「归零/失效条件表」（含出处行号），再等级 ——
「两个条件的合取」要比「单条件」低一级，且必须在报告里写明合取关系，不能写成常态路径。
同一案例还给出了更有价值的那一条：**删掉某个"下限"参数时，真正丢的往往是"有下限"这件事本身**，
而不是那个极端值是否可达——用探针扫一档值（如 `A ∈ {0, 0.2, 0.4, 1, 2, 3, 5, 8}`）
就能把"什么时候起差异"画成表，比争"会不会发生"有用得多。

**⚠️ 用户说"这不会发生"时，最合适的收尾是可观测性，不是继续论证。**
如果对方对本项目的领域判断（如"有分型才有买卖点 → A 不会为 0"）成立，
正确做法是：**保留他的口径（取值一字不改）+ 在该分支加一条带现场信息的 WARNING**，
并明确"只在配置预期之外的场景下才打，避免刷屏"。
这样既尊重判断，又留下取证入口；跑一段若从未出现，就是他判断的实证。
同时**撤销自己上一轮为此加的字段**——用 `grep -rn <符号>` 全仓确认残留 0，
包括文档与测试断言（否则会留下"配置里有但没人用"的新漂移）。

**⚠️ 改完必须跑全量，别只跑你改的文件 —— 项目里可能有"守卫型"测试。**
实测：新增日志文案里写了「结构**腿**归零」，触发 `test_p26_terminology_guard.py`
（全仓中文术语守卫，禁用词含 `腿/双仓/丢仓/腿态/挑腿` 及 `leg/legs`）→ 全量 `nonzero_rc=1`，
而单跑被改的那个文件全绿。**任何新增字符串（日志文案 / 注释 / 断言描述）都要先过这类守卫。**
交付前固定动作：全量回归 `nonzero_rc == 0`，且断言数只允许**净增加**（新增断言 OK，
旧断言被改期望值或删除必须逐条列进报告）。

### ⓼ 合入后复审（post-merge re-audit）

**触发**：你交付过补丁/文件包，对方合入后又让你"再看一遍有没有遗漏"。
此时判据和首次评审完全不同 —— 首次是**找问题**，这次是**验证合入 + 兜底找漏**。

> 现成脚本（本 skill 自带，直接跑）：
> `scripts/postmerge/audit_imports.py <树>` —— 失效 import 审计；
> `scripts/postmerge/audit_removed.py <旧树> <新树>` —— 已删符号的活引用残留。
> 下文的判据逻辑都已实现在里面，遇到误报先按 ② 的两条合法形态检查它。

三步，缺一不可：

**① 三方树逐文件 md5（合入完整性）**

```python
# 需要三棵树：before(合入前) / delivered(你交付的改后树) / merged(合入后)
# 结论要看两组：
#   merged vs delivered → 差异应为 0（你给的东西一字不差进去了）
#   merged vs before 的文件集 == delivered vs before 的文件集
#     （既无"合入里有、我没给"的多余改动，也无"我给了、合入里没有"的遗漏）
```

别忘了**忽略 CRLF** 再比（`.replace(b"\r\n", b"\n")`），否则满盘假差异。

**② 静态 import 审计（全仓，扫"失效引用"）**

对每个 `from X import a, b` 校验 `a`/`b` 在 `X` 里真实存在（AST 提取各模块顶层定义）。
这正是"改名/删除只改一半"的通用检出器，比 grep 精确得多。

**必须处理这两个合法形态，否则误报会淹掉真信号**：
- `from 包 import 子模块`（`from App import AppEngine` 里 AppEngine 是 `App/AppEngine.py`，
  不是 `App/__init__.py` 的顶层定义）→ 判据要加 `"{}.{}".format(mod, name) in module_index`
- 模块含 `import *` / `__all__` / `globals()` / `setattr` → 静态不可判，整模块跳过

**③ 已删符号的"双层"扫描（活引用 + 文字残留）**

```python
# 层 1：求差集 —— 旧版有、新版无的顶层符号
removed = {d for d in old_defs if d not in new_defs}   # 跳过核心库目录，噪音大

# 层 2A：活引用（AST）—— 扫 Name / Attribute / ImportFrom，字符串与注释不算
# 层 2B：文字残留（词边界 grep）—— 专抓 docstring / 注释里的过时引用
pat = re.compile(r"(?<![A-Za-z_])(" + "|".join(map(re.escape, removed)) + r")(?![A-Za-z_])")
```

> **关键：这两层必须都做。** AST 活引用审计**天然抓不到 docstring/注释漂移**
> （那正是"注释不参与求值"的刻意设计，为的是不误伤解释性注释）。
> 实践中经常出现"活引用 0 处，但 docstring 还在写已删的类名"这种漏网之鱼。

**层 2B 的命中必须逐条看上下文语义**，不能按计数报警：
- ✅ 应保留：护栏/守卫测试的**自检样本**、说明「**删除原 X**」的历史记录、p26 类术语解释
- ❌ 该修：docstring 描述**当前行为**时用了已删/改名的符号
（本次 16 个已删符号里 15 处命中属前者、仅 1 处属后者 —— 只看计数会得出完全相反的结论。）

### ⓽ 定位「某功能是否曾进入历史」，并把丢掉的功能装回来

用户说「这功能以前做过，是不是被后面的改动冲掉了？能从 GitHub 找回来吗」
时，先用**全历史字符串搜索**把"有没有进过库"钉死，再决定用哪条恢复路径。

```bash
git log --all --oneline -S'_volDisplayMode'   # 全 ref、全历史，出现次数变化即命中
#   空输出 = 该字符串在任何提交的任何版本里都不存在 → 从未提交过，远端拿不回来
#   只命中 1 次 = 不是"先加后删"，而是"压根没加进被跟踪的文件"（若加了又被删，必有两个提交）

# 逐 ref 落文件核（-S 命中的可能是**别的文件**带来的，别被误导）
for r in <旧基线的短sha> <中间某提交> origin/<分支>; do
  printf "%-12s app.js=%s run_all=%s test=%s\n" "$r" \
    "$(git show $r:Frontend/app.js | grep -c '_volDisplayMode')" \
    "$(git show $r:Test/run_all.py | grep -c 'vol_macd_mode')" \
    "$(git ls-tree $r Test/test_vol_macd_mode.py | wc -l)"
done

git show --stat <命中提交>          # 看那次提交到底带了什么：常见是"只带了新测试文件"
```

**「测试在、实现不在」是功能被冲掉的典型指纹**：`Test/test_x.py` 是新文件（untracked
→ 怎样都留得下），而 `Frontend/app.js` 是被跟踪文件（被丢弃/被整包覆盖就无声消失）。
两个信号同时出现就该往"找回"走：① 守卫/回归里那个组件一直红；② 实现侧的关键符号
在 `git log --all -S` 里搜不到。

**正向确证「本轮恢复」的最硬证据 = 测试文件两版 `md5` 相同 + 两版都注册同一组件 + A 红 B 绿。**
实测（chan.py `custom-dev` vs tag `09181`）：`Test/test_vol_macd_mode.py` 两版
`md5sum` 完全一致、`run_all.py` 里都注册为第 30 号组件，OLD 侧 FAIL 报
`Page.click: Timeout … waiting for locator("input[name=\"vol-display-mode\"][value=\"macd\"]")`
（HTML 里根本没这个控件），NEW 侧 PASS 6.01s —— 实现被装回来了，且**测试从未被动过**。
报告里要写这三条，不要只写"测试从红变绿"（那可能只是测试被改绿）。

**恢复路径选型**（按可用性从上往下）：

1. **交付包 / 工作树副本**——功能当初是以 zip 或未提交改动形式存在的，先在本地找回来
   （`git diff <基线> -- <文件>` 看未提交改动够不够；`git show <ref>:<file>` 取干净基线）。
2. **不要直接拿旧文件覆盖**——后续几轮的成果会一起没。走三方合并，让 git 自己判定：

   ```bash
   # base=当初做该功能时的干净版本, mine=当前工作树, theirs=含功能的旧版本
   git merge-file -p mine.js base.js theirs.js > merged.js   # exit=0 且无冲突标记 = 干净
   ```
   `merge-file` 的行数校验：`mine + (theirs − base)` 应等于合并结果行数（本次 8436+190=8626）。
3. **正则在源码里重打补丁**（最后手段，且必须带命中次数断言）。

**装回去之后必须做「恢复精度」自检**——证明这次只装回那一个功能，没夹带/没多删：

```python
from collections import Counter
added = Counter(after) - Counter(before)      # 逐行**多重集**差（不是集合差！）
gone  = Counter(before) - Counter(after)
assert added == Counter(补丁的 '+' 行) and gone == Counter(补丁的 '-' 行)
```

- **必须用 `Counter` 不能用 `set`**：补丁新增的行里有些文本在别处早已存在（单独一行的 `}`、
  `ctx.fillStyle = COLORS.textLight;`），集合差会把它们误判成"没装上"，得到假红。
- 空行无区分力，比对前把空行从两侧一并剔除。
- 顺手把这次恢复的**精度数字**写进交付说明（"新增 186 行 == 补丁 + 侧 186 行 / 消失 9 行 ==
  补丁 − 侧 9 行"），比"我装回来了"可复核得多。

**顺带产出（用户最需要的）**：交付时逐文件比对"我的树 vs 对方仓库 HEAD"，只打真正不同的
文件——本次是 2 个（`Frontend/app.js`/`index.html`），其余 8 个 md5 一致不用重发。
另外把"那个一直红的组件"的根因写清楚（本次：`vol_macd_mode` 恒红 = 测试在、实现不在），
它往往就是用户察觉功能丢失的那条线索。

## 输出结构（报告模板）

1. **总体结论** —— 表格：改动范围 / 方向是否正确 / 是否引入问题 / 测试结论 / **可用性判断**
   （"当前状态不可合并"这类明确表态比"存在一些问题"有用得多）
2. **改了什么** —— 新旧对照表 + 影响面（全部调用点，含"已核查无影响"的那些）
3. **改得对的部分（实证）** —— 每条都要有数据支撑
4. **引入/放大的问题** —— 按 P0/P1/P2/P3 分级，每条写清：机制 → 实测数字 → **为什么护栏没拦住**
   → 修复。P0 必须配**独立于测试 import 链的复现路径**（子进程 / CLI）
5. **回归验证（A/B 对照表）** —— 基线 / 目标 / 打补丁后 三列并列，用退出码；
   另附「新引入失败 / 本次修复 / 新增且通过 / 两版都失败」四类归因
6. **未受影响（已核查）** —— 逐条说明为何排除
7. **建议** —— 按优先级，每条给出具体改法与代价；**能直接给可 apply 的补丁就给**
   （附 `git apply --check` 通过 + 应用后回归数字）
8. **附：复现步骤与脚本清单** —— 把 runner / 差分 / 探针脚本和它们的 JSON 结果一并交付
9. **交付物清单 + 应用/回退方法** —— 交付前从 zip 解压到干净目录复验过（见 ⓹）

> 报告里每个"引入问题"都要能回答："**哪条命令、什么输出**"。没有命令的结论不要写。

## 常见陷阱

- **门禁入口与「全量发现式」入口的组件集不同，两套必须都跑，结论不可互相替代** →
  实测：门禁 52 个组件里含 3 个「带参数调用的脚本」（`gen_fixtures.py --check`、
  `snapshot_runner.py`、`func_map_check.py`），而全量发现式 runner 只枚举
  `test_*` / `repro_*` / `smoke_*` 形态的文件 → 这 3 个组件**不在**发现式结果里。
  于是会出现「门禁 OLD 7 FAIL / 发现式 OLD 6 FAIL」的差值，且**差值不是噪声而是真实组件**。
  报告里要用**门禁**回答"门禁是否变绿"，用**发现式**回答"有没有用例从未进过门禁"，两列并列。
- **注释里的数字不可信，冻结基线必须解析字面量再做差集** → 本仓两处：phase6 的
  `WINDOW_BASELINE` 注释写"现回归为 62 个"、实际字面量 61；phase3 的 `EXPECTED_ROUTES`
  注释写"31 条"、实际字面量 27。**只读注释会得出与事实相反的结论。**
  做法：用 `re` 抽出 `{…}` 块里的字符串集合，**两版集合做差集**，
  `OLD-only == [] and NEW-only == []` 才算"基线未被弱化"（本次实测两版逐字相同，
  仅注释数字被修正）。同型：注释里的"共 N 项""断言数""路由数"一律复算。
- **两端 A/B 看不见中间态 → 凡「严格更优 / 单调更差」这类判断，必须逐提交核对** →
  `compare` 只给 A、B 两个端点的净差异，而中间提交可能**存在过**一个"收益来源与结论不同"的状态。
  实测：本仓 16 个提交里 `Trading/Source/SSE.py` 被改 3 次，其中 `4f493dd491`（**只改这一个文件**）
  把回调挂点前移到帧前、但消费窗口仍是 0.2s —— 该中间态在 10 帧/s 的帧率下每 1 s 墙钟要付出
  2 s 阻塞（消费跟不上生产、积压反灌 buf），正是下一个提交 `387cdec5c7` 改成非阻塞窗口的动机。
  若只比两端，会写出"相对旧方案延迟严格更优"这种**把中间态代价抹掉**的结论（正确说法：
  收益是"非阻塞 + 不积压"，单条时延相对该中间态是互有胜负）。
  **做法**：先按 `per_commit.json` 找出"同一文件被哪些提交碰过"（GitHub compare 的
  `files[i]` 是 `[status, path, adds, dels]`，按下标取 path，别按 `[0]`），只要被 ≥2 个提交碰过，
  就按 sha 取每个中间版本的**可执行行**比对（`raw.githubusercontent.com/<repo>/<sha>/<path>`），
  再给单调性结论；不许用"我印象里只改过一次"代替核对。
- **给"旧态是红的"定性前，先读失败日志把红因落到具体断言行** → 别把"门禁红"直接等同于
  "我关心的那条覆盖从未生效"。实测：旧树 `snapshot_regression` 9/9 全红，日志里展示的首个差异却是
  期货基线缺 `fractal_high/low`（基线陈旧），与"股票回看窗口绑宿主机配置"是**两件独立的事**；
  混为一谈就会写出"那条覆盖本来就恒红、不构成有效覆盖"——而真相是**该分支确实执行过**，
  只是结果随宿主配置漂移、无法用于判读。正确写法："本次把旧的非确定覆盖主动让位成了零覆盖"。
- **把 `red` 当分支** → 先 `git tag -l` 确认。
- **Windows `git worktree add ../x`** → 会生成 `C:\c\Users\...`，务必传绝对路径。
- **在工作树未切换时 grep** → clone 后 HEAD 在默认分支，diff 出来的符号在源码里搜不到。
  先 `git checkout -B <branch> origin/<branch>` 再搜。
- **只看 diff 不看快照** → 快照里往往藏着真正的业务行为变化（如买卖点数量变化）。
- **把"兜底返回"当成正常判定** → 特别警惕 `if not found: return True` 这类默认放行，
  输入分布一变，兜底命中率飙升，等于悄悄放宽了门槛。
- **把并发进程的日志交错当成行为差异** → 用 ProcessPool/多进程跑的测试，worker 日志
  （含"读取 N 条 / 截断 N→M / 查询完成"这类**行为指纹**）在汇总输出里的顺序和归属
  是不确定的，两版对拍时会出现"同名字段值不同"的假差异。
  **判据**：把该用例单独重跑 2 次×两版，若各自稳定且两版一致 → 日志交错，非回归。
- **Windows 上 `timeout` 不是 GNU timeout**（是 `TIMEOUT.EXE`）→ `timeout 90 python -c "..."`
  会报"无效语法"并返回非 0，制造**冒烟失败的假象**。命令行探针/冒烟脚本别用 `timeout`。
- **沙盒 Bash 里 `find` / `wc -l` 偶发返回 0**（PATH 被 shell shim 干扰）→ 统计文件数、
  列目录改用 Python；或每条命令前重新 `export PATH=...`。
- **交付目录会被中间产物污染** → 生成脚本别把辅助 json/临时文件写进交付根目录；
  打包后**核对 zip 内文件数**是否等于预期（本次正是靠这个发现多出来的 1 个文件）。
- **评审用的 diff 是内部产物，绝不能跟着交付走** → 本技能的产物天然是"diff/patch 视角"
  （`diff_all.txt`、逐文件 `.diff`），很容易在交付阶段被"顺手"带上，而用户的交付格式要求
  往往是「**只要 zip，不出补丁**」。本次踩到两层：
  ① 交付附件里多给了 2 个 `patch_*.diff`；② 更隐蔽的 —— diff 被我放在了 `reports/` 下，
  而打包规则是"整份 `reports/` 进 zip"，于是 **4 个 diff 产物被打进交付包**，用户解压覆盖时
  会把 `.diff` 落进仓库目录树（还有一份经模拟树解压后残留在 `verify/reports/`）。
  **规则**：① 交付前**重新对照交付格式铁律逐项过一遍**，不因"文件在 reports/ 里"就默认可交付；
  ② diff 一律写到**交付区之外**（如 `internal/`）；③ 交付只给 **zip + 报告**，
  `present_files` 的附件列表同样受此约束；④ 已泄漏就用**带断言的重打包脚本**收口，
  再解压做 md5 往返校验（与已跑过测试的那棵树比 md5 相等 ⇒ 免重跑回归）。
- **粗采样网格会「伪造」阈值** → 行为探针若只在 2.5R / 3.5R 两个点取样，输出
  「2.5R 未启动 + 3.5R 已启动」会被**自动读成"阈值在 3.5R"**，而代码判据可能是
  `fav >= k × R`，即精确 3.0R —— 真阈值落在两个采样点**之间**。
  本次实测踩到：探针**和单元测试**都只采了这两个点，于是"3.5R 才启动"被写进报告，
  被用户直接质疑「不应该是 3R 吗？为何 3.5R？」。
  **规则**：① 报告里写"阈值 = X"之前，必须用**小步长扫过**（如 0.01 单位）量出转折点，
  不许从采样点外推；② 把边界（`X−ε` 不触发 / `X` 恰好触发 / `X+ε` 触发）写进
  **单元测试**，否则该语义在测试里从未被钉住 —— 粗网格的测试会反过来"授权"错误读法；
  ③ 见到"3.5R""大约 2 倍"这类数字，先自问"这是量出来的，还是我采样点之间的距离"。
- **被质疑时先回读上一轮的原始产物，别靠回忆** → 本次「3.5R」的来源一眼就能在
  `probe.py` 的循环字面量（`[(125.0,"best=2.5R"), (135.0,"best=3.5R")]`）里看到。
  **结论会漂移，脚本不会**：先 `Read` 回探针源码/输出，比回忆"我当时怎么测的"快且准。
  更正时要在报告里**明确写"我上一轮错了"**并给出精确重测，不要悄悄改口。
- **清理遗留术语时，别把「术语沿革」一并删掉** → 全量删光后，后来人翻旧提交/旧文档
  看到废弃叫法，会以为代码里真有两套实现却无处可查。正确做法：**所有在用位置**改成新词，
  只在**一处**（模块 docstring 顶部）留一段"旧名 → 新名 + 废弃日期"映射；
  并在回复里**明确点出哪几处是刻意保留的**，否则用户 grep 到会以为没清干净。
  另外先做**误伤排除**：同名字符串可能属于完全无关的模块（本次 `Test/test_phase6_guards.py`
  里的"A 方案"是**前端 AppState 合并方案**，与出场策略无关，动了就是引入错误）。
- **改「观测口径/告警阈值」前，先查被它替代的那个值在各分组上的真实取值** →
  本次把 `A <= 0` 放宽成 `A < 3.0`，3.0 取自"被删的 `min_r_points` 地板原值"；
  回查旧档案发现 **IF/IH=3.0、IC/IM=5.0、商品=3.0** —— 单一常量对 IC/IM 松了一档。
  处置：按用户指定值实现，但做成**单一可调类属性** + docstring 写明分组差异，
  把事实报给用户，而不是自行按品种分档（用户偏好"不增加复杂度"）。
- **按行号替换前先 `repr()` 打印目标行** —— 中文全角括号、有无反引号、行尾空白都会让
  子串匹配静默失败；用 `assert old in line` 让它**响亮地失败**，别用 `replace` 后不校验。
- **Edit 报「Found N matches」是重复实现的信号，不是要你加上下文** → 同一段逻辑在文件里
  存在多份拷贝（典型：函数内联一份 + 模块级独立函数一份）。此时正确做法不是分别改 N 处，
  而是**先把其中 N-1 处改成委托调用，建立单一事实源**，再改那唯一一处。
  改完加一条**源码级守卫断言**（如"源码中不得再出现 `def _bucket_30min`"）防回潮。
  同型病还包括同名常量的多份副本（`INTRADAY_FREQS`）——作者改一份漏一份就是 bug 来源。
- **顺手修既有 bug 时，必须先证明"未改变无关行为"** → 例：把手写分支法换成锚点法修港股分桶，
  要先跑「A股逐根新旧分桶对比 = 0 处差异」才能说零回归，再断言港股恰好只修正了应修的 N 根。
- **回滚验证是断言有效性的唯一证明** → 每加一条断言，都把对应修复撤掉跑一次，
  确认测试**必须变红**。没变红说明断言空转。撤销范围要精确（用 `cp` 备份 + `git show HEAD:<file>`
  单文件回滚，别用 `git stash push -- <file>`，工作树有其它未跟踪改动时它会报
  `Entry not uptodate. Cannot merge.` 直接失败）。
  不要拿全量 run_all 的日志行直接下结论。
- **只看"改了的文件"找第二份实现** → 同名常量/函数常在同一仓库里有多份副本
  （如 `INTRADAY_FREQS` 同时存在于 `App/utils.py` 与 `BuySellPoint/BSPointList.py`）。
  改了其中一份后，必须 `grep -rn "<符号名>"` 全仓确认**所有副本**都同步，
  尤其是"改 A 文件、但另一条调用链用的是 B 文件副本"的情况——这会产生
  「同一张图，展示层对、判定层错」的隐蔽不一致。
- **跨进程语义必须端到端实测，桩测不出来** → 若改动涉及「父进程发信号 → 子进程收尾」
  （如 `Popen.send_signal(SIGTERM)` 触发子进程 handler 做清理落盘），
  只测被调用函数本身（in-process 直调 + `_FakeProc` 桩）永远全绿，
  而真实链路可能是断的。**判据**：真实 Popen 拉起 → 真实 stop → 断言**落盘结果**
  （state/db 字段值、事件文件条数、日志关键字），而不是断言"函数被调用了"。
  实测案例：某仓库 679 行新测试 77/0 全绿，但 `grep SIGTERM` 0 命中，
  真实关闭链路因平台语义差异完全失效。
- **Windows 上 `os.kill(pid, SIGTERM)` = `TerminateProcess`，不执行任何 handler/atexit** →
  凡是"发信号让子进程优雅收尾"的设计在 Windows 上 100% 空转，且退出码变成 1。
  跨平台可用替代（均已实测可行）：flag 文件哨兵（最稳，与 `CREATE_NO_WINDOW` 兼容）、
  `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`（Python 映射 SIGBREAK=21）、Job Object。
- **`os.kill(pid, 0)` 在 Windows 上对「已退出但 Popen 句柄未关」的进程仍返回成功** →
  不能单独当存活判据（会把死进程判成活的）。用 `proc.poll() is not None` 或 `tasklist` 复核。
- **Windows virtualenv 的 `Scripts\python.exe` 是 shim** → 用 `sys.executable` 拉子进程时
  `Popen.pid` 是 shim 的 pid，真正跑业务的是它的子进程（`os.getppid()` 可证）。
  按 pid 发信号 / 记录 pid 恢复，都会打在 shim 上。改用不依赖 pid 的协议，
  或解析 `sys._base_executable` / `pyvenv.cfg` 的 `base-executable`。
- **「flag 文件」停止协议一定要处理残留** → 父进程写 `{out}/.stop_request`、子进程轮询消费
  这类设计，若**没人删 flag**，下一次启动的新进程会立刻读到上次的停止请求 →
  **秒退 / 开不起来**（实测：第二次 start 4s 内退出，且又写了一遍收尾事件）。
  **判据**：`grep -rn "<flag名>"` 全仓，确认有且仅有一处**删除**逻辑，且删在
  「拉起子进程之前、持锁内」（放在子进程启动时删会有 start/stop 并发丢请求的竞态）。
  更稳的替代：flag 带时间戳，子进程只响应自身启动之后创建的 flag。
- **flag 文件残留：CLI 入口也要自清，不止编排层** → "写于 stop / 删于 start" 的协议，
  若**删除只发生在编排层（如 `AppTrader.start()`），而 CLI 直启入口（如 `run_gateway.py`）
  不自清**，则任何"不走编排层的直启/重启路径"（进程崩溃后手动重启、CI 复跑、直接 CLI 拉起）
  一启动就会被残留 flag 看护线程立刻关停（实测：同 `out_dir` 第二轮 CLI 直启仅存活 1.0s）。
  **修法**：在 CLI 入口 `run()` 拿到 `out` 后、看护/轮询线程启动之前，幂等 `if os.path.exists(flag): os.remove(flag)`，
  与编排层的删 flag 双保险。**测试回放类进程必须注意无限数据 vs 有限数据**：若停止链路测试靠
  回放源"持续活着"，而回放源是有限数据（每根 bar `time.sleep(speed)` 后播完即自然退出且不锁仓），
  第二轮复跑同一份数据时会在数秒内自然死、且不会产生"关闭"事件 → 误判。务必让第二轮用
  更低速（更长播放）或更多数据，使"存活断言窗口"远小于自然播完时长，才能区分"残留 flag 误杀"与"数据播完"。
- **「按结果判定成功」别扫全量历史** → 用"事件文件里是否出现 X 事件"来判定本次操作成功时，
  若事件文件是**追加写**的，上一轮的历史事件会让本轮失败也判成功
  （例如：被残留 flag 误杀的子进程仍会写"shutdown 完成"事件，[e2e-9]"≥2 条 off"在误杀下也过）。
  **修法**：要么只认"本轮新增"（记条数基线/时间戳基线），要么**新增独立判据从不同维度互证**
  （如事件时戳间隔 ≥N 秒 vs 探针侧 wallclock 实时感知），任一维度被破坏另一维度仍能拦。
- **秒精度时间戳比较的脆弱性** → `datetime.now().isoformat(timespec='seconds')` 给的是秒级字符串。
  用"A 秒 > B 秒"做判据时，**只要真实时间差 ≥1.1s 跨秒，判定结果就翻转**。
  在误杀场景（~1s）下"看似区分"只是偶然同秒，**稳健做法是用 wallclock `time.time()` 做差值比较**
  （毫秒精度），或用事件差值（如两轮 shutdown 事件间隔）而非单事件的"是否晚于另一事件"。
  （实测：本轮强杀未收尾，却因历史 `auto_order_off` 被判 graceful=True）。
  **判据**：记录操作前的条数/最后时间戳，只认**新增**；或按请求时间戳过滤。
- **别信 `graceful`/`success` 这类自报字段** → 它们常只表示"在超时前退出了"，
  不代表"清理真的执行了"。判据要落到**可观测副作用**（落盘值 / 事件 / 退出码）。
- **「只是调了下顺序」是最容易被误判为无风险的一类改动 —— 必须专门核查** →
  用户常把重新排列声明（枚举成员、常量、列表项、字典键、字段）当成纯可读性整理。
  生产逻辑往往真的零依赖，但**断言里常常是按定义顺序写的**，一调序就红。
  实测案例：`(str, Enum)` 的三个枚举各挪了一个成员，生产代码全仓零顺序依赖，但
  `test_p10` 里 `[e.value for e in OrderIntent]` 这种按定义顺序的断言直接 3 条失败
  （55 通过/3 失败，全量 30 绿/1 红）。

  **核查清单**（对每个被动过的顺序逐一跑）：
  ```bash
  # 序敏感用法：迭代、`__members__`、下标、排序键、位掩码、auto()
  grep -rnE "list\((EnumA|EnumB)\)|__members__|for .* in (EnumA|EnumB)|\.index\(" --include=*.py .
  grep -rnE "\[.*\.value for .* in [A-Z][A-Za-z]*\]" --include=*.py .   # 最常漏的断言写法
  grep -rnE "auto\(\)" --include=*.py .                                  # 隐式序号，最危险
  ```
  **判据**：`(str, Enum)` 继承 `str` → 比较按字符串值、序列化取 `.value`，
  顺序**无语义**；顺序性只活在"按定义顺序遍历"的地方（绝大多数是测试断言）。
  **修法**：让断言与顺序解耦（`sorted(e.value for e in E)`），而不是把源码顺序改回去 ——
  否则下一次纯可读性调序又会打红同一条测试。
  **反向确认**：改完要能回答"生产代码里 0 处顺序依赖"，并有全量回归（调序前 30绿/1红 → 调序后 31绿/0红）作证。

- **调序会带走注释的归属** → 成员带续行注释（第二行缩进对齐）时，移动成员会让续行留在原位，
  语义被挂到邻居头上。实测：`SOFT_EXIT_LOCK` 挪走后，其第二行说明
  `唯一合法离场 = 对向信号触发 UNLOCK` 变成 `UNLOCK_UPGRADE` 的注释。
  审查顺序改动时，除了查断言，还要**逐条核对"每行注释是否仍紧贴它描述的对象"**。

- **改名类改动的验收要扫描"输出文案"，不止标识符** → 枚举值/字段改名后，标识符很容易清干净，
  但**测试打印的标签字符串**常被漏掉，留下 `check("事件流含 open_first", ... "signal_open" in content)`
  这种"标签是旧名、断言是新值"的自相矛盾日志 —— 功能全绿，但排障时会被日志误导。
  扫描时同时 grep 旧名的 **snake_case 形态**（`open_first`）而不只是常量形态（`OPEN_FIRST`）。

- **评审"修复提交"时要专门找「修复引入的新路径」** → 上一轮的问题修好不等于没有新问题。
  重点查新增协议/新增文件的**生命周期是否闭合**（谁创建、谁消费、谁删除），
  以及新增宽限/超时值是否放大了既有阻塞路径（如 `_STOP_TIMEOUT` 20→150 让服务关闭卡 150s）。
- **「缩短超时」是高风险改动** → 它会把原本"几乎不走的兜底分支"变成常走路径，从而暴露该分支里
  的隐藏 bug。本仓曾见：把服务退出超时 150→30 后，最坏锁仓 ~100s > 30s → 强杀兜底高频触发，
  撞出 "Windows 无 `signal.SIGKILL`" 的崩溃（见下条）。缩短任何 timeout 都要把对应兜底分支完整跑一遍。
- **Windows 信号常量陷阱（高频踩坑点）** → `signal` 模块在 Windows 上**没有 `SIGKILL`**
  （只有 SIGINT/SIGILL/SIGFPE/SIGSEGV/SIGTERM/SIGBREAK/SIGABRT）。任何
  `_send_signal_best_effort(handle, signal.SIGKILL)` 在 Windows 上**参数求值即抛 AttributeError**，
  函数体内的 try/except 来不及生效，且后续依赖该调用之后的兜底（如 `taskkill /F /T`）成死代码、
  永不执行。审查"停止/强杀/超时兜底"代码时务必：`grep -rn "SIGKILL"` 全仓，
  对 Windows 用 `getattr(signal, "SIGKILL", signal.SIGTERM)` 退化或直走 `taskkill`。
- **测试里全局 monkeypatch `subprocess.Popen` 会污染 `subprocess.run`（跨平台测试黑洞）** →
  若测试用 `module.subprocess.Popen = _FakeProc` 全局替换，则同模块里 `subprocess.run(...)`
  内部复用模块级 `Popen`，**也返回 `_FakeProc`**，且 `run` 会把返回值当上下文管理器 `with process:`。
  于是：被测试代码只要在某条**平台分支**（如 `if os.name=="nt": _taskkill()`）里调用
  `subprocess.run`，该测试在**该平台必崩**（`_FakeProc` 无 `__enter__/__exit__`），在另一平台因分支
  跳过而"绿"。后果：CI 在 Linux 报 82/0，用户 Windows 实盘环境跑 pytest 直接 abort——
  **"X/0 全绿"是平台假象**。审查测试改动时：`grep -rn "subprocess.run" <被测模块>`，确认它只被
  非平台分支调用，或把 monkeypatch 收窄为只替换"启动子进程那一次 Popen"，或给 `_FakeProc` 补
  `__enter__/__exit__`。验证"跨平台修复"时**必须在本机真实 OS 跑对应测试**，不能只看 CI 平台。
  另：同一份代码在 **Linux 跑测试全绿、Windows 实盘崩溃** 是常态——CI 若在 Linux，必须单独对
  Windows 路径（或显式 `os.name=="nt"` 分支）做端到端验证，不能只信 CI 绿。

- **别用 shell `printf` / `echo` 往源码里注入测试代码（本机 Git Bash shim 会吃掉反斜杠）** →
  实测：`printf '\nif False:\n    x = 1\n' >> f.py` 写出来的是一行
  `if False:/n    x = 1` —— `\n` 变成字面 `/n` → **语法错误文件**。
  而这类文件若被 AST 扫描/守卫用 `except SyntaxError: continue` 处理，就**被静默跳过**，
  于是"注入后护栏没变红"被误读成"护栏没有判别力"（实测踩到，白跑一轮）。
  **做法**：文件注入/改写一律用 **Python**（`open(..., "w", newline="").write(...)`），
  shell 只用来跑命令。

- **AST 扫描器/守卫的 `except SyntaxError: continue` 是静默盲区** →
  语法坏掉的文件不会被扫、也不会被报告。实测：注入一行语法坏代码后，扫描数
  225 → 224，护栏照旧全绿。**修法**：把跳过数当断言 ——
  `check("跳过的文件数 == 0", skipped, 0)`，别让"扫不到"伪装成"扫过且干净"。

- **修"同类缺陷"时只修眼皮底下那一处 = 治标未治本，要扫全量同型** →
  本次修掉了一条恒真断言 `check(x, x)`，但 AST 扫描显示同一模式**基线 16 处 →
  改动后 15 处**（只修了 1 处）。**判据**：修完一类缺陷后，用 AST 把该模式
  **全仓重扫一遍**并把数字写进报告（"16 → 1"才叫修完，"16 → 15"要明说）。
  同型：`check(a, b)` 且 `ast.dump(a)==ast.dump(b)`；`subprocess` 桩缺
  `__enter__`；`return True` 兜底；同名常量多副本。

- **改动把"本地方案未确认"升级成"外部权威结论"时要专项审** →
  本次把 broker 侧"等待持仓更新超时"（**本地**判定，成因含"柜台真的没这仓"与
  "行情字段还没同步"两种）打上 `REJECT_POSITION` 标签，而该标签的语义被上层定义为
  "柜台明确答复无此仓"，且是**清除簿面仓单的唯一准入门槛** → 一个含歧义的本地超时
  获得了"删真仓"的资格。**判据**：凡是"分类标签被用作**不可逆动作**的门槛"，
  必须逐成因核对标签语义是否**单义**；有歧义就拆成新类别，而不是复用最方便的那个。
  量化式表述："需 20 次连续拒单 × 5 根 bar 冷却 ≈ 100 根 bar，属长尾风险，非日常故障"
  —— 别把它写成"随时会炸"。

- **「创建了组件却没接线」是最容易全绿的一类 P0 —— 静态源码护栏天然抓不到，必须做接线级探针** →
  实测（chan.py `custom-dev` vs tag `poll_worker`）：前端把自动下单轮询移进 Web Worker，
  app.js 里 `new Worker(Blob(AO_WORKER_SRC))` + `onmessage`/`onerror` 都齐了，
  但**主线程从未 `w.postMessage({type:'start'})`**，而 Worker 侧的定时器只在收到 `start`
  时才建立；`fallback()` 又只在 `typeof Worker === 'undefined'` / 构造抛错 / `onerror`
  三条路径触发，Blob Worker 语法正确 → **新旧两条轮询路径同时归零**（旧版主线程
  `setInterval` 反而正常）。新增护栏 `test_p66` 的 [2] 组只钉"创建 Worker / onmessage
  接线 / onerror 回退 / 主线程 setInterval 只剩 fallback"，[3] 组用 node 跑 Worker 源码时
  是探针自己调 `self.onmessage({type:'start'})` —— 于是**补上这一行前后，p66 都是
  20 passed / 0 failed**，门禁 125/127 全绿而运行时功能全死。
  **做法**：对"新建对象/新加通道"类改动，写**接线级探针** ——
  ① 从源码里定位初始化段（如 `(function startXxx() { ... })();`）用 `indexOf` 切出片段；
  ② `new Function(...keys, snippet)` 注入桩（假 `Worker` 记录 `postMessage`、假 `setInterval/
  clearInterval`、假 `fetch`、假 `document.getElementById`）执行；
  ③ 断言"消息真的发出去了 / 定时器真的建了"，而不是断言"创建调用存在"。
  **反向自检（不可省）**：把缺的那一行补回去 → 探针必须从 `POSTMESSAGE_COUNT=0` 变 1、
  且桩 fetch 被真实调用；同时确认**原有护栏仍然全绿**（若原护栏也绿，说明它没有判别力，
  要在报告里明写"该护栏对这条缺陷判别力为零"并给出补判据的具体写法）。
  同型风险面：`new Worker/Process/Thread` 建了不 start、注册了回调但没人触发、
  生产者建了但消费者没订阅、`addEventListener` 写错事件名 —— 共性都是
  "创建侧与驱动侧分离"，只审创建侧一定漏。

- **A/B runner 别擅自补环境变量，口径要与真实门禁一致** → 实测：runner 里顺手设了
  `TRADER_GATEWAY_HOME=<tree>/Trading`，而 `Trading/Test/*` 用它拼自身路径，于是拼出
  `<tree>/Trading/Trading/Test/...` → `FileNotFoundError`（p63 假红）。真实门禁
  `Test/run_all.py` 只设 `PYTHONIOENCODING`。**判据**：A/B runner 的 env 逐项对照门禁源码
  里的 env 设置（`grep -n 'env\[' <gate>.py`），只补 PATH/PYTHONIOENCODING 这类无语义的。
  另一条：Windows 上 `subprocess` 的 `PYTHONPATH` / `cwd` 必须传 **abspath**（相对路径会
  按父进程 cwd 解析 → 一堆 `No module named 'Trading'` 的假红）。

- **顺序型源码判据：定位子串必须取"调用形态"，否则 find 命中定义处 → 判据恒真** →
  实测（chan.py p70 补 `_write_ready_flag` 调用点顺序判据）：写
  `src.find("build_runtime(args)")` 定位调用点，实际命中的是 `def build_runtime(args)`
  **定义处**（在文件更前面）→ 无论被测调用挪到哪里判据都绿。改为
  `src.find("= build_runtime(args)")` 才落在调用行。
  **做法**：顺序判据一律用带上下文的调用形态（`= f(`、`out = f(`、`f(out)`），
  写完**必须反向自检**（把调用挪到锚点另一侧 → 必须变红）；反向自检的注入点也要
  确认真的越过了锚点（插到 `def run(...)` 之后一行 ≠ 在 build_runtime 之前，若锚点
  正是下一行则没越过）。同类坑：`find("X(")` 命中 `def X(` / 注释里的 `X(`。

- **评审完要"把问题修掉"时：修复后跑**真实门禁**（不是自己的 A/B runner）+ 每条新判据反向自检** →
  实测：A/B runner 报 125/127（p20/p60 红），改完直接跑 `python Test/run_all.py`
  （cwd=仓库根）是 **127/127** —— 那 2 个红是 runner 环境差异的**假红**，不是回归。
  **结论**：A/B runner 用于跨 ref 对照，判定"本机是否干净"只认真实门禁退出码。
  新加的每条护栏判据都要给出"删掉/挪动后必红"的实测输出（两组数字：正常态 passed/failed
  与破坏态 passed/failed），写进报告 —— 只写"已补判据"没有判别力证据等于没补。
