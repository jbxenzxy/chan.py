---
name: chanpy-trading-engine-verify
description: 验证 + 最小可替换单元交付（**已合并吸收 `minimal-patch-delivery`**）。用真实 Trading 引擎 + dry_run broker 构建**对照实验**来核对/验证 chan.py 自动下单规格（或任何"读代码看不出结论"的引擎语义问题），量化候选补丁的**波及面**，并把结论落成**用户能自己合并、也能一键回退**的交付包。当用户说「核对一下这个功能实现是否符合我的描述」「XX 这条到底怎么跑的」「这两个场景结果是否一样」「这个 bug 改完会影响哪些测试」，或「改完后打包给我」「按目录层级打包」「出个最小补丁」「改的文件打包交付」「覆盖即用、删掉即回退」时使用。产出：沙盒对照实验脚本 + 原始输出 + 波及面量化表 + zip（保留仓库真实目录层级 + overlay/patch + 变更说明 md+HTML + evidence 原始日志）+ 清房复验结论。
agent_created: true
---

> **本技能是合并体**：2026-09-13 把 `minimal-patch-delivery`（最小补丁包交付）整体并入本技能 ——
> 两者的「沙盒改 + A/B 实证 + 打包交付」是同一套流程，拆成两个技能会导致触发词打架、
> 且交付纪律散落两处。
>
> **怎么读**：
> - **通用层**（任何仓库都适用）：§0 铁律、§1 拉取并留存原版、§2 测试环境 + **§2.2 验收四连**、
>   §5 **§5.1 清房复验与多向回环（三向 / 四向）**、§7 坑点、§8 交付包结构、§9 报告与变更说明结构。
> - **chan.py 专属层**：§3 引擎实验脚手架、§3.5 自然流程 vs 构造场景、§3.6 根因定位、
>   §4 判据速查、§6 防回潮护栏，以及 §7 里带项目名的条目。
> - 触发词两套都接：「核对/验证」走实验路线，「打包/交付」走交付路线，多数任务两者都要。

# chan.py 交易引擎：对照实验验证 + 波及面量化 + 最小补丁交付

目标：交付一个**用户能自己合并、也能一键回退**的改动单元。
核心不是"改对"，而是**让用户有证据相信改对了**，且**改动波及面肉眼可数**。

## 何时用

- 要核对"实现是否符合某条规格"，而**读代码得不出确定结论**（判定式分散、缺省分支互相矛盾、隐式不变量）
- 要回答"场景 X 和场景 Y 执行结果是否一样"这类**必须实测**的问题
- 有一个已确认的 bug，要评估**候选修法**会连带影响哪些测试（选最小波及面变体）
- 用户要求"沙盒改、打包给我自己合并"（→ 交付纪律见 §0 铁律 6~9、§5.1、§8、§9.1）
- 要判断"远端某分支合入到哪了 / 一期到底做完了没有"这类**需要逐项核对 + 量化**的问题

**不要用**：纯 README/文档类问题；单文件一眼能看完的小改动。

**外观/尺寸类需求换技能**：凡是「改为跟 XX 一样大 / 对齐 / 间距不对」这类**像素级**诉求，
用 **`frontend-pixel-verify`**（无头 Edge 量真实盒模型 + 渲染候选给用户挑 + 改前改后对比图
+ 多宽度回归）。别在本技能里靠读 CSS 猜尺寸。

---

## 0. 铁律

1. **绝不改用户的真实项目目录**（`C:\my_chan_project`）。全部在
   `C:\Users\river\WorkBuddy\<session>\sandbox\<name>\` 里做，最后打包 zip。
2. **先沙盒全量落地仓库**，再动手。不要只下 `Trading/`——跑测试会缺 `App/`。
3. **交付前跑全量回归**（脚本式，看 exit code），并在干净目录解压复验。
4. **交付结构（2026-09-15 用户裁决，已改）**：`overlay/`（覆盖即用，**必给**） +
   `evidence/`（脚本+原始输出） + `reports/`（md+HTML）。
   **⚠️ `patch/`（unified diff）默认不再产出** —— 用户 2026-09-15 拍板：「以后每个阶段完成后，
   把修改的文件，按目录层级，打包给我就行，无需出补丁」。
   只有当次**明确要求 diff** 才补（且必须用 git 原生 `git diff` 生成 + `git apply --check` 干跑，见 §7.6/坑 98）。
   注意：内部做 A/B 反向对照实验时**仍需要** diff（那是验证手段，不是交付物），只是不进 zip。
5. **⚠️ 绝不用「人工注入的构造场景」推断「运行时行为」**（本项目最大的翻车点）。
   `positions.add(...)` 塞仓能造出任意簿面，但那只证明"引擎循环是逐笔的（设计如此）"，
   **不能**证明自然流程会出现这种簿面。任何靠手工塞仓得到的结论，必须再用纯
   `on_bar + on_signal` 驱动复现一遍，才能作为行为证据；否则显式标注"构造场景 / 不可达"。
   → 详见 §3.5。
6. **改动面必须可数**。动手前先跟用户确认"改哪几个文件、几处"，写进交付文档。
   若某处修复会牵连测试文件或公共签名 → **降级为注释修正**，并把"彻底做法"列成独立的后续项，
   不要静默扩大改动面。
7. **先存原版再改**。改之前把原文件按**相同相对路径**下到 `sandbox/orig/`。
   否则事后做不了 diff、打不了 patch、也做不了"改前"对照。
8. **每处改动都要真实 A/B**（见 §5）。方法：把 `orig/` 的原文件临时换回去、或对改动做
   **机械化反向补丁**，跑**同一个**探针/回放，把 `rc`、断言差异、产物有无**落成日志**。
   理论推断（"这里应该会抛错"）不算证据。
9. **清房复验不能省**。见 §5.1。

### 0.1 铁律 1 的唯一例外：用户点名要求改的文件

铁律 1 是"不擅自改用户项目目录"。**若用户明确点名了某个文件/目录**
（例："并同步更新 `C:\my_chan_project\Docs\<某文档>`"），那就是**显式授权**，可以直接就地改。

⚠️ 但**就地改会带来一个交付侧的副作用**：磁盘上只剩一个文件，用户**无法比对**
（"你给我的和我本地的一模一样，你是不是没更新？" —— 2026-09-13 真实踩到）。
所以就地改之后**必须补一份可比对物**：

1. 把改动**逐条反向撤销**，重建一份"改前基线"（重建脚本必须带**自校验**，见 §5.2），
2. 产出标准 `unified diff`，
3. 连同改后全文一起打包交付。

判据：用户拿到包后能自己 `diff` 出与交付包内 `.diff` 相同的结果。

---

## 1. 拉取仓库（GitHub 本机可行路径）

`git clone` 在本机走 schannel 会失败（`CRYPT_E_NO_REVISION_CHECK` / 挂死）；
tarball（codeload）会**固定卡在 7,687,103 字节**（exit 28，且服务端不支持 Range）。
**唯一可靠路径 = GitHub API 文件树 + raw 逐个取 blob**（秒级、可断点、0 失败）：

```bash
# 1) 文件树
curl -k -sL -o tree.json \
  "https://api.github.com/repos/<owner>/<repo>/git/trees/<branch>?recursive=1"

# 2) 逐个 blob 下载（见 scripts/fetch_repo.py，用 urllib + 未校验 ssl context，每个重试 3 次）
#    下载范围要覆盖跑测试所需的前置目录，否则测试会从 82 项掉到 40 项

# 3) 判断"合到哪个 commit"（中文 commit message 常全是"更新"，只能靠时间戳）
curl -k -sL "https://api.github.com/repos/<owner>/<repo>/commits?sha=<branch>&per_page=15"
```

**比对远端 vs 本地务必忽略 CRLF**：`diff --strip-trailing-cr`，否则本地 CRLF 造成"全文件 differ"假象。

### 1.1 留存原版 + 量化改动行数（铁律 7 的落地）

- 把要改的文件同时下到 `orig/`（**相同相对路径**），之后用 `difflib.unified_diff`
  生成 `patches.diff`。
- **改动行数（`+N -M`）写进交付文档** —— 这是"波及面可控"最直观的证据。
- **新增文件的 diff 要按 `/dev/null` 全文列出**，并在统计里把"修改文件 +N/-M"与
  "新增文件 +K（全文）"**分开报**。混在一起会让"代码改动量"看起来虚高
  （实测：修改 3 文件 +87/−33，新增 1 文件 +423 行）。
- 多阶段连续交付时，`orig/` 必须是**用户当前的仓库状态**（= 上一阶段交付包的内容），
  **不是远端仓库最新版**。取错的话 diff 里会混进前几个阶段的全部改动，
  用户看不出这一包到底改了什么，"波及面可数"当场失效。

### 1.2 改动本身的约定（沙盒内）

- **只改确认过的行**；改动的注释要说明**为什么**（并带日期），让后人不必考古。
- 多行注释要对齐原文件风格；**能一个字符搞定的别写三行**（diff 越小越可信）。
- 改完立刻 `python -m py_compile <file>` 逐个过语法（= §2.2 验收四连的 ①）。
- **"我自作主张的附加"要单独成节**：交付说明里写清"为什么加 / 只影响什么（日志 or 行为）/
  要拿掉得删哪几行"。用户的默认反应是"我没批这个"，先答比被问好。

---

## 2. 测试环境

```
Python: C:\Users\river\.workbuddy\binaries\python\envs\default\Scripts\python.exe
        （已含 pydantic / pandas / numpy / chinese_calendar / tqsdk / fastapi，依赖最全）
跑法:   cd <repo root> && export PYTHONPATH=. && python Trading/Test/test_pXX_*.py
        脚本式（内部 sys.exit(1)），不是 pytest；判定看 exit code
全量:   for t in Trading/Test/test_p*.py; do python "$t"; echo "$?"; done
```

**跑完必须清运行产物**，否则混进交付包 / 产生假差异：
`__pycache__/`、`_verify_A/`、`_verify_B/`、`Trading/State/state.db`、`*.dbtmp`。

### 2.1 本机跑全套的两个必踩坑（2026-09-13 实测，都花了轮次）

1. **单次 PowerShell 调用约 60–85s 会被截断**：表现为**无任何输出、exit≠0、子进程被杀**，
   而且被杀的进程**还没来得及写结果文件**（于是你以为"runner 没生成 `_out.txt`"，
   误判成脚本坏了）。⇒ **跑全套必须分块**（单次调用预算 ≤45s），
   且 runner **每跑完一条立刻落盘 + flush**，这样被截断也能看到进度。
2. **`test_p20_phase_i1.py` 在本机退出时永不退出**：它的断言全过
   （`P20 Phase I1 结果: 108 通过 / 0 失败`），但随后 tqsdk 的 asyncio 残留任务刷
   `Task was destroyed but it is pending` / `Event loop is closed`，进程挂死不退。
   ⇒ 它**混进批量会拖死整批**（表现就是上面第 1 条的"被截断"）。
   **必须单独跑并给超时**，判定以"**结果行已打印**"为准（不能只看 exit code）。
   自写 runner 的判据：超时（`TimeoutExpired`）时若 stdout 里已有 `...结果: N 通过 / M 失败`
   行，就按 PASS 记，并在行尾标注"结果行已打印，进程 teardown 挂死"。

**可靠 runner 模板**（替代 `Trading/Test/_tool.py runall`）：
`python runner.py <repo_root> [test1.py ...]`，要点：
`os.chdir` 不要用（用 `cwd=`）；`env["PYTHONPATH"]=W` + `env["PYTHONIOENCODING"]="utf-8"`；
`subprocess.run(..., capture_output=True, timeout=<可从环境变量配>)`；
每条结果**立即 `f.write(...); f.flush()`**；失败时把 `✗` 行或末尾 N 行一并落盘。

**另注**：PowerShell `*>` 重定向出来的日志是 **UTF-16（含 BOM）**，`Read` 会直接报
"binary file"；要落 utf-8 就让 **Python** 写文件再读。

### 2.2 验收四连（全绿才继续）

| 顺序 | 项目 | 说明 |
|---|---|---|
| ① | `py_compile` | 逐个改动文件过语法（改完立刻做，别等运行才发现） |
| ② | **仓库自带护栏/契约测试** | 若仓库有可执行的术语/风格护栏，**必须把交付的探针脚本一起放进去扫**（护栏常把仓库根的 `verify_*.py` 纳入扫描范围） |
| ③ | 仓库自带全套测试 | 脚本式测试注意 `PYTHONPATH=<repo root>` 且 `cwd=repo root`。**⚠️ 数字要按环境分开报**：无 `.env` 的空环境 vs 用户项目（带本机 `.env`）结果可能不同，见 §5.1 与坑点 93 |
| ④ | 交付探针 | 与 ② 同处仓库根时再跑一次，证明共存无冲突 |

---

## 3. 引擎实验脚手架（核心）

用真实引擎 + `DryRunBroker` 造场景，比读代码可信得多。

```python
from Trading import Broker                      # 注册 dry_run
from Trading.Broker.DryRun import DryRunBroker
from Trading.Config import DEFAULT_CONFIG, TradingConfig
from Trading.Engine.Engine import TradingEngine
from Trading.Infra.EventLog import EventLog
from Trading.Infra.InstrumentSpec import InstrumentSpec
from Trading.Infra.Store import Store
from Trading.Infra.Types import (Bar, Signal, Position, ExitPlan, Side,
                                 PositionOrigin, EngineState, AccountState)
from Trading.Strategy.Entry import DefaultEntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy

def build(tmpdir, tag):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = 2          # 一笔挂 N 手
    cfg.risk.max_open_positions = 1
    cfg.risk.unlock_no_new_open = True
    cfg.exit_params.use_atr = False
    cfg.exit_params.min_r_points = 3.0          # 让止损距离可控、可预测
    cfg.exit_params.use_trailing = True
    spec = InstrumentSpec()
    return TradingEngine(
        cfg, DryRunBroker(spec, {}), DefaultEntryPolicy({}),
        LayeredExitPolicy(cfg.exit_params.model_dump()),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None)), spec
```

### 关键调用序列

```python
eng.on_bar(mkbar(D1, "09:30", close=4500, high=4505, low=4495, ts=1))   # 先喂 bar
eng.on_signal(mksig(D1, "09:30", is_buy=True, price=4500,
                    frac_low=4490, frac_high=4520, ts=1))                # 再喂信号
eng.on_bar(mkbar(D1, "09:35", close=4488, high=4490, low=4485, ts=2))   # 砸穿止损 → 触发
eng.shutdown_and_lock_all()                                              # 关闭路径
```

- **时序不能反**：`on_bar` 先结算已有持仓，`on_signal` 才开仓。反了就是"同一根 K 线内既开又平"的作弊。
- **入场那根 K 线不参与出场判定**（`bar.timestamp <= pos.entry_bar_ts` 会 skip）→ 触发用的 bar 要给更大 `ts`。
- **信号几何决定止损**：`R = max(0.2, min_r_points=3.0)`；设 `frac_low=信号价` 可得 `stop = 信号价 - 3.0`，于是"喂 low 低于该值"必定触发。
- **dry_run 撮合价**：`filled = 信号价 ± 0.2`（买 +0.2 / 卖 -0.2），别当成真实滑点。

### 读结果

```python
broker.orders          # list[Order]；o.meta["intent"] ∈ {open,close,lock,unlock}
                       #                o.meta["offset"] ∈ {OPEN,CLOSE}  ← CTP 报文层只有这两种
eng.positions.positions  # 簿面；看 p.origin / p.entry_date / p.lock_pair_id /
                         #              p.exit_plan.name / p.exit_plan.params["risk_anchor"]
eng._state             # EngineState（4 值过程态）
eng.account_state()    # AccountState（3 值结果态，P1 引入）
eng.store.trades()     # 已实现 Trade
eng.store.signal_action(key)   # 信号的处置：opened / skip / rejected / unlock
open(os.path.join(tmp,"events_x.jsonl")).read()   # 事件日志（grep 关键 kind）
```

**净敞口**：`sum((1 if p.side is Side.LONG else -1) * p.volume for p in eng.positions.positions)`

---

## 3.5 自然流程 vs 构造场景（必读）

**问题**：`positions.add()` 能让簿里出现任何东西（3 笔同向、半配对、单边锁仓……），
但引擎有门控，这些簿面在自然流程里**可能根本不可达**。用它们推断运行时行为 = 结论无效。

**本项目已确认的门控（都会拦住你臆想出来的多笔敞口）**：

| 门 | 位置 | 效果 |
|---|---|---|
| 运行态忽略信号 | `on_signal` 顶部 `account_state() is RUNNING → skip` | 有非锁仓持仓时，**任何**新信号被忽略 |
| 瞬态忽略信号 | `on_signal`：`_state in (OPENING, EXITING) → in_flight` | 下单过程中不收信号 |
| ~~同向上限~~ | ~~`_open_position`~~：~~`same_side_n >= cfg.risk.max_open_positions → open_silenced`~~ | ❌ **已删除，见下方更正** |
| ~~漏单补开~~ | ~~`_unlock_position`~~：~~受 `unlock_no_new_open` 管~~ | ❌ **已删除，见下方更正** |

> #### ⚠️ 2026-09-13 更正：上面两行门**已不存在**，"运行态恒 1 笔"的结论作废
>
> 2026-09-11 的 Phase 5/7 重构把「持仓来源」概念整体删除：`PositionOrigin` /
> `SOFT_EXIT_LOCK` / `_open_position` / `_unlock_position` **全仓已不存在**
> （见 `test_p10_state_machine.py` 文件头）。同时 `Config.py:285-286` 明文记载
> `max_open_positions`（同时持仓笔数上限 D2）与 `unlock_no_new_open` **已删除** ——
> `RiskConfig` 现在只剩 `max_volume` 一个字段。**运行态不再有任何笔数上限。**
>
> **更正后的推论**：运行态**可以**自然出现多笔。唯一入口：
>
> ```
> ①开仓（运行态）→ ④反向开仓（锁仓态）→ ②今锁 + 信号 开新仓（回到运行态）
> 簿面 = 「多、空、多」三笔，net = ±2
> ```
>
> 实测可复现（2026-09-13，远端 `8e34e75f`）：`②` 那步真的成交
> （报单 `OPEN/OPEN x2`，簿 2 笔 → 3 笔）。**这是 ⑤ 号转移落到锁仓态的唯一自然入口**
> —— 见 §7.2 坑点 76。
>
> ⚠️ **读这份 skill 时先看 `Config.py` 的 `RiskConfig` 还剩哪些字段**：这个项目的
> 风控闸门删得很快，任何"有上限 / 有开关"的结论都会在下一个 Phase 失效。
> "运行时xx不可能出现"这类断言，**一律先 grep 配置字段是否还在**再下结论。

**写脚本的规矩**：
- 主体用 `Driver` 类：`on_bar` / `on_signal` 两个方法 + 一个**按当前持仓止损价动态构造触发 bar** 的
  `trigger()`（硬编码价格一定会砸穿失败 —— 止损价是按 fractal 算的，不是固定 R）。
- 人工注入的对照组**单列一节**，标题里就写「⚠ 自然流程不可达」，避免日后被自己误读。
- 每个场景跑完打印 `account_state() / 簿内笔数 / 非锁仓笔数`，并**断言不变量**
  （如"运行态时非锁仓笔数 ≤ 1"），把违规集中列在末尾。

> 实战教训（2026-09-10）：我曾用"注入 3 笔同向敞口"报出"出场一次触发会报 3 笔"，
> 结论被用户当场推翻 —— 运行态压根不接收新信号，3 笔敞口不可能出现。
> 改用 5 场景自然流程重测后：默认配置恒 1 笔（结论反转）。
>
> **2026-09-13 再更正**：末句"默认配置恒 1 笔"**已作废** —— `max_open_positions` 于
> 2026-09-11 随重构删除（见 §3.5 更正块）。教训本身仍然成立、而且更值钱了：
> **同一类问题反转了两次** —— 第一次错在"方法"（拿构造场景当运行时行为），
> 第二次错在"时效"（结论被后续重构推翻）。⇒ 写结论时**必须标注它依赖哪个配置字段 / 哪个版本**，
> 否则半年后没法判断它还算不算数。


---

## 3.6 根因定位法：拿仓库里已有的**同构实现**做对照

用户的口头禅是「先把根因找到，不要兜底」。"某处坏了"与"某处**天生**不可靠"是两个不同结论，
必须分开证明。判据很省事：**在同一个仓库里找一个性质完全相同、但工作正常的同类实现做对照。**

实战（2026-09-10，ID 撞号）：

| | `bars_seen` | `_trade_seq` / `_lock_pair_seq` |
|---|---|---|
| 性质 | 进程内自增整数 | 进程内自增整数（**完全同类**） |
| 接持久化？ | `_persist:406` 写 / `_restore:344` 读 | **不写不读** |
| 跨重启实测 | 保持（7 → 7） | 归零（2 → 0） |

⇒ 根因是「**漏接已有的持久化机制**」，不是「计数器这个方案不可靠」。
修法只需在既有 `kv` 机制里加一行 `set_json` —— 不需要新表、不需要改 schema、
**更不需要换掉 ID 的生成方式**。

**推论（很容易做错的一步）**：根因修掉后，那些"为了防它"而存在的防御分支会变成**不可达**。
此时**不要给它引入新的业务行为策略** —— 不要用一个业务动作，去兜一个代码里本不该出现的状态。
应降级为**不变量断言**（报警 + 拒绝启动）。区分方法一句话：
「这个分支触发时，需要一个人类做**业务决策**吗？」需要 = 真策略；不需要 = 该断言。

**顺带自查**：`grep -n "set_json\|get_json" Trading/Engine/Engine.py` 一眼看清哪些状态接了持久化。
**同一批 `self._xxx = 0` 的兄弟变量里，如果有的一接一没接，那几乎必然是漏接（bug），不是设计意图。**

> 反面教训（同一天）：我把"自己 R1 方案的残余复用洞"说成了"系统的第二个根因"，
> 被用户当场要求「先把不唯一的根因找到」。**自己方案的缺陷 ≠ 系统的根因**，两者必须分清。

### 3.7 核对「某处写了 X 字段」型断言：键命中 ≠ 语义命中（2026-09-21 实战，铁律级）

评审/方案里常见这类旁证：「A 处和 B 处都已写 `meta["offset"]`，所以已有数据源」。
**只 grep 键名命中就判它成立，会漏掉"键对了、值不是那个语义"的情况。**

实战案例：`SimNow.py:1381` 写的是 `"offset": action`，键是 `offset`，
但值是局部变量 `action` —— 而 `action` 来自 `_finalize(...)` 的**第 3 形参**，
两个调用点**硬编码**传字符串字面量（`_submit_open` 传 `"open"`、`_submit_close` 传 `"close"`），
**与同函数里写 `meta["intent"] = intent_str`（走第 2 形参 `intent.value`）是两个完全不同的字段**。
⇒ 同名字段在两个 broker 里语义不等价：DryRun 写 `INTENT_TO_OFFSET[intent]`
= `{OPEN,CLOSE,CLOSETODAY}`；SimNow 写 `{open,close}`（小写标签，**永不含 CLOSETODAY**）。

**SOP（三步，缺一不可）**：
1. **定位赋值行** → 看右侧**变量名**，不要只看左侧键名。
2. **追变量来源** → 若是函数形参，去**所有调用点**看实参；若是局部量，继续向上追。
   `grep -n "def 该函数" ` + `grep -n "该函数(" ` 两把抓完。
3. **比对两个写方的值域** → 逐个列出可能取值，**看是否真的同域**。
   大小写不同、枚举 vs 字面量、少一个取值 —— 都是"不等价"。

**为什么这类问题特别值得查**：它**不会有测试报红** —— 因为测试用的是另一个 broker。
本例全树 `meta["offset"]` 的读者只有 `trading/Test/test_p30_shutdown_exit_mode.py`，
而它的 `build()` 只造 `DryRunBroker`，期望值是大写 `"OPEN"`，恰好对上。
**"某一侧无测试覆盖"正是这类分歧的温床。**

**推论**：引用旁证时，写清"**哪一侧、哪个字段、取值域是什么**"，
不要写"A、B 都有" —— 后者字面为真、语义误导，比不写更危险。

### 3.8 做「全树搜索」前，先声明并断言扫描基（2026-09-21 实战，铁律级，我自己踩过）

沙盒里只下载了**跑测试所需的前置文件**时，`os.walk` 会**静默地只覆盖子集**，
而人（包括我）会把它当成"全树"。**范围缺失不报错 —— 这是最危险的地方。**

实战翻车：我曾断言「`meta["offset"]` 的读者只有 3 处、全在 `test_p30`」，
被用户纠正为 **3 文件 7 处** —— 漏掉的 `test_p34` / `test_p51` **当时根本不在沙盒树里**。

**SOP（三选一，必须做其一）**：
1. **先 `assert os.path.exists(候选文件)`** 再搜；候选从 `git ls-files` / GitHub tree API 取，
   **不是**从 `os.walk` 取（否则同样受限于已下载集）。
2. 结论里**显式写明扫描范围**：「扫描范围 = 已下载的 N 个文件（列表见 X）」——
   把"我没搜到的部分"变成读者可见的边界，而不是隐含假设。
3. 用**仓库全量文件列表**驱动搜索：先取 tree，再 `if not exists: 当场下载`，
   最后 `assert 已下载数 == 期望数`。

**自检问句**：「我说'全树/所有/唯一'时，凭据是**仓库**还是**我手上已有的文件**？」
—— 这两者不相等，而**后者不会报错**。

### 3.9 核对基线必须锚定 commit sha，不要复用「目录名相近」的旧 tree（2026-09-21 实战，铁律级）

**做「新树 vs 基线」的改动面核对时，第一步不是 diff，是确认基线到底是哪个 commit。**

实战翻车（本轮验收 run 级配对会计）：沙盒里同时躺着
`tree.json`（更早 HEAD）/ `tree_base.json`（真基线）/ `tree_new.json`（新树），
以及 `head_base/` `head_new/` `head_4pf/` 几个目录 —— **blobs 数都接近（367/367/368）、文件名极像**。
我随手用 `tree.json` 做基线 → 报出「**21 个文件改动**」；用 `head_4pf/` 做内容 diff →
报出「`Base.py`/`SimNow.py`/`main.py` **零变化**」这个**假结论**
—— 因为 `head_4pf/` 是**混合态目录**（其 `Base.py` 的 sha 已是新版本），根本不是一个干净快照。

改用 `tree_base.json`(b4880c6fdb) 重算后，真实改动面 = **12 改 + 1 增 + 0 删**，与用户文档「13 文件」自洽。

**SOP**：
1. **先取 commit sha，再对文件名**：`curl -k -sL ".../commits?sha=<branch>&per_page=5"` 拿时间戳 + sha，
   在报告开头**明写**「基线 = `<sha>` / 新树 = `<sha>`」。
2. **一个目录 = 一个快照**：目录内必须**全部文件**来自同一个 commit。
   凡是"部分文件已更新"的目录（混合态），**降级为参考、绝不作基线** —— 它给出的"零变化"结论是假的。
3. **用 blobs 数做互证**：基线 367 / 新树 368 ⇒ 差 1（正是新增的 p52）。
   数量对不上就说明拿错了 tree。
4. **目录命名带 sha 短号**（如 `head_b4880c6f/`），杜绝"哪个 tree 是哪个"的口头记忆。

**自检问句**：「我这个基线目录里的**每一个文件**，都是同一个 commit 的吗？」
—— 答不上来就别开始 diff。

### 3.10 独立「重放器」的配对规则必须与引擎语义一致；否则优先改用「归属判据」（2026-09-21 实战）

为了验证恒等式（`Σ run.net_cash ≡ Σ 全部真实成交现金流`），我写了一个**独立 FIFO 重放器**
（自己按同向 FIFO 把入场笔和离场笔配对，再累加现金流），结果报出
「恒等式破裂 **56,231 元**」—— 而重放器自己也报「残留入场笔 2 手」。
**两个自相矛盾的信号同时在，正确结论是「重放器不适配」，不是「引擎有 bug」。**

根因：引擎的配对规则不是"同向 FIFO"。它有一条**当日锁 + 信号 → OPEN 新开**的转移
（不动锁仓仓单），会产生「同侧多笔、run 锚 ≠ 最先入场笔」的簿面；
重放器没实现这条，于是把本不相关的笔配到一起。

**SOP —— 优先用「归属判据」替代易错重放器**：
- **归属判据 = 字段级一致性**：拿一个**引擎已经算好的实体**（如对账产生的 Trade）
  的 `entry` 字段，**直接与它声称归属的那张仓单**的 `entry` 比对。
  两边一一对应 = 正确；不一致 = 错配，且差异值可量化（本例 100 点）。
- 它的好处：**不引入第二套配对逻辑**，因此不存在"重放器自己错了"的可能。
- **反例纪律**：当"独立实现"与"被测实现"结论冲突时，先质疑独立实现 ——
  **越是自己现写的、越容易把语义差异当成缺陷**。要下"引擎错了"的结论，必须能
  **逐条说出引擎的配对规则**，并证明重放器实现了同一条。

**配套**：一旦发现某条生产可达路径（本例 = 转移② 当日锁 + 信号再开仓）不在候选补丁的验证矩阵里，
**必须把它补进验证矩阵**再宣布补丁"已验证"。

### 3.11 「某测试能否区分两版实现」：断言名/括注 ≠ 实际断言（2026-09-21 实战，铁律级）

**判定一个测试能不能拦住某个改动，不能读它的断言文字，必须读它的 `check()` 参数。**

实战翻车（第十一轮复核 P0-1 裁决）：裁决书的核心论据是
「候选补丁会**翻转** `test_p15b` [5.3d] 钉死的语义（`两笔 Trade 的 entry 均 = run 锚 4545`），
所以补丁不能用」。我把补丁真打进 HEAD 副本跑 p15b —— **136 通过 / 0 失败，与原树逐字相同**。

**根因（三条，缺一不可地查）**：

1. **穷举该文件里针对该字段的全部断言**。p15b 全文件对 `Trade.entry_price` 只有 **1 处**
   （`:629` `[4.5e]`），而那条场景走 `_force_exit`（`_execute` 路径）—— **根本不经过被改的 Reconcile**。
2. **核对每个断言场景是否真经过被测改动的那条代码路径**。断言存在 ≠ 断言能碰到改动。
3. **看断言对象是谁**。`[5.3d]` 的实参是 `[d.get("signal_key") for d in _ev53]`，
   `_ev53` 来自 `read_events(kinds={"position_externally_closed"})` ⇒ 断的是**事件流**的字段。
   「Trade.entry 均 = run 锚」只出现在**断言名的文字描述里**，既没被 `check()` 断言，
   也和 `signal_key` 无关。补丁只改 `Trade.entry_price` ⇒ 碰不到被断言的字段。

**SOP**：
1. `grep -n '<字段>' <test>.py` 穷举，**逐条看 `check(名字, got, expected)` 的第 2/3 实参**，
   把"名字里提到 X"的行全部剔除 —— **括注/名字是文档，不是契约**。
2. 对每条真实断言，追它引用的对象从哪来（`read_events` 的 kinds？`store.trades()`？`broker.orders`？）。
3. **最省事也最硬的办法：把候选实现真打进去跑一遍**（本例 `run_patched/` 副本 + 原树对照跑同一测试）。
   静态分析只用来解释"为什么不红"，不用来断言"会不会红"。

**并行纪律**：这条与 §3.7「键命中 ≠ 语义命中」是同构的 ——
**§3.7 说"字段名对了不代表值语义对"；§3.11 说"断言名说了不代表真的断了"**。
两者共用一条元规则：**只认可执行语句，不认文字描述**。

### 3.12 评审措辞纪律：「没被护栏拦下」≠「会被护栏拦下」（2026-09-21 实战）

这两种说法**运维含义相反**，写评审/裁决时混用会造成实质危害：

| 说法 | 真实含义 | 应衍生的动作 |
|---|---|---|
| 「补丁会翻红既有护栏」 | 护栏在起作用，改动被拦 | 拒绝改动（尊重既有护栏） |
| 「补丁**不会**翻红，因为该路径零断言」 | **护栏有洞** | **顺势提出补护栏**（这是新发现的待办） |

本轮真实案例：裁决把「Reconcile 路径的 `Trade.entry` 口径**全树零断言**」
写成了「p15b 会拦下这个补丁」。后果是双重的 ——
① 后来者（含我自己）会**误信已有保护**而不去补 `[S6-d]`；
② 同一补丁再被提出时，会被这句错误依据反复驳回。

**SOP**：写「某测试会拦住 X」之前，**必须**先做 §3.11 的三步核对；
如果三步做完发现"其实拦不住"，那么结论应当是
「**该语义目前无护栏，建议补断言**」，而不是「已有护栏，故无需动作」。
**把盲区说成护栏，比不写更危险。**

**同类教训的判据**：任何形如「只有 N 处」「唯一」「所有」的全称量词，
给出前必须先证明**搜索空间完整**（见铁律 6.5 的"全称量词给出前先穷举"）。

### 3.13 「护栏已补」的验收要**实证判别力**，不能只看它绿（2026-09-21 最终验收实战，铁律级）

用户补了护栏后说"看起来没问题"，你要回答"是否无需再改"，**不能只跑一遍看全绿**
—— 全绿只证明"当前实现满足断言"，**不证明"断言能拦住错误实现"**。
必须做**反向注入实验**：把被拒/错误口径打进一份**副本树**，断言那几条**必须翻红**。

本轮实证（`test_p52` 新增 `[S6-d]`，钉死 Reconcile 路径 `Trade.entry == run 锚`）：

```
CLEAN  树 (87b850d)          PASS=63 FAIL=0   [S6-d2] ✓(4510,4520)  [S6-d4] ✓(20,10)
PATCHED树 (候选口径打进入场锚) PASS=61 FAIL=2   [S6-d2] ✗(4500,4520)  [S6-d4] ✗(20,20)
```

**只有 PATCHED 翻红，才算"这条护栏有牙"。** 只有 CLEAN 绿 = 可能只是装饰。

**反向注入的做法**（最小、可逆、不污染干净树）：
1. `copytree(SRC, DST)` 出副本 —— ⚠️ 沙盒会拦 `rmtree`（>50 文件），用**改名不删**
   （`os.rename(DST, DST+".stale_HHMMSS")`）腾位再 `copytree(dirs_exist_ok=True)`。
2. 在 DST 里做**语义等价**的最小替换。本轮 = 在 `_settle_run` 调用前后
   临时把 `self._run_anchor` 换成 `pos.entry_price`（模拟"Trade.entry 取被删仓单"）：
   ```python
   _saved_anchor = self._run_anchor
   self._run_anchor = pos.entry_price
   _t = self._settle_run(...)
   self._run_anchor = _saved_anchor
   ```
   替换前 `assert src.count(anchor_line) == 1`（**断言命中次数**，防锚点漂移）。
3. **隔离性自证**：跑完比对 `sha(SRC文件) == sha(下载件)`，并断言注入标记
   （`_saved_anchor`）**只在 DST 出现、SRC 中不存在**。
4. 两棵树跑同一测试，比对断言级输出（不只比对 rc）。
5. **注入树必须"红"，不能"崩"**（2026-09-22 二次验收踩到）：新写的断言若直接取
   `chk.plan.stop_price`、`_attrs_in(_method(...))` 这类**可能为 None 的深层字段**，
   负控树里会 `AttributeError` → rc=2 + Traceback，**计数行打不出来**，
   证据从"✗ 30/6 可引用"退化成"崩了"。凡断言里出现"先取子对象、再取它的属性"的地方一律判空：
   ```python
   chk.attr if chk is not None else None            # 对象本身
   chk.plan.stop_price if chk.plan is not None else None   # 子对象
   _attrs_in(node) if node is not None else set()          # AST 扫描基
   ```
   收尾判据加一条：**注入树的 stdout 里不得出现 `Traceback`**，且必须有 `NN passed, MM failed` 计数行。

**收尾判据**：当"拒绝某补丁"的理由是"补丁会被测试拦下"时，
**必须出示 PATCHED 翻红的实测**；给不出 = 该理由不成立（承接 §3.12）。

### 3.14 探针/验收脚本的运行时与沙盒三坑（2026-09-21 最终验收，都踩过）

1. **必须用 venv python**：`C:\Users\river\.workbuddy\binaries\python\envs\default\Scripts\python.exe`。
   用 bare managed python（`versions\3.13.12\python.exe`）跑 Trading 测试会
   `ModuleNotFoundError: No module named 'pydantic'`（`Infra/Instrument.py:71` 依赖 pydantic）。
   ⇒ **规则**：凡是要 import Trading 包的脚本，一律走 venv；`versions\` 那个只跑纯标准库工具
   （如 diff/报告生成器）。
2. **沙盒拦 `rmtree`**：`SAFE_DELETE_BULK_CONFIRM_REQUIRED`（count>50）。
   ⇒ 一切"重建目录"改用**改名不删**；这也符合用户"复验目录用改名不删"的既有约定。
3. **有些测试文件无 `__main__` 守卫**（如 `test_p52_run_accounting.py`）→
   `import` 它等于跑全套、输出被淹没。要拿断言输出就
   `subprocess.run([PY, "-X", "utf8", rel_test], cwd=tree, env=...)` 捕 stdout，
   **别 import**。

**这三条合起来的验收脚本骨架**（可直接复用）：
`copytree→改名不删` → `assert count(anchor)==1` → 注入 → 两树各 `subprocess.run` →
grep 断言名逐行比对 → 打印 `RESULT` 前缀便于外层 grep。

---

### 3.15 判「某状态是否可达」：不要拿"看着严重"当缺陷报（2026-09-22 实战，B4-1/B4-2 一轮出结论）

用户级铁律 6.6（`~/.workbuddy/MEMORY.md`）：**报问题先给背景**，第一句必须写"什么条件下才会发生"。
本条是把该铁律落成可执行三步法 —— 适用于任何"某字段为空 / 某分支走错档 / 某静默回落"式候选缺陷。

**三步（按顺序做，任一步判"不可达"即可收工）**

1. **枚举赋值点，看是否成对 + 有无守卫**
   `grep -rn "self\._the_field = " Trading/ | grep -v /Test/` → 列出全部赋值点，
   逐个看：与它"同时才合法"的兄弟字段是否**同处赋值**？（`_run_side` 只在 `_restore_run:698`
   与 `_run_start:1753` 赋值，`_run_entry_at` 就在 `:715`/`:1761` **成对** → 有 run 必有 entry_at）
   再看写盘口是否加了守卫（`_persist_run:643-646` 在 `_run_plan`/`_run_side` 为 None 时**删键**、
   根本不写 → "半截记录"进不了库）。
2. **逐提交枚举该字段的"出现史"，判"半新半旧"的库是否存在**
   持久化字段的空值来源几乎总是"旧版写入的库"。别猜：`commits?path=<file>&per_page=12`
   → 逐 sha 拉 raw → 解析持久化 payload 的键集，做一张"版本 × 字段有无"表。
   本轮实测 `entry_offset` 与 `entry_at` **同一提交 `ba7926c` 同时进 `_persist_run`**
   （之前两者全无、之后两者全有）⇒「有 offset 无 at」的库**不存在** —— 这一步直接结案。
3. **用真实引擎做两个实验收口**
   ① **round-trip**：`_run_start(...)` → `_persist()` → 新引擎 `_restore_run` → 断言字段非空且值一致；
   ② **老格式库**：把 payload 换回旧字段集写进 state.db → 断言启动被 **fail-fast 拒绝**
   （若是拒绝启动，则"空值被读到"这一状态在真实库里不可达）。脚本骨架见 `sandbox/verify_b4.py`。

**坑**：`_persist_run()` 只落 run 那条 KV，**持仓簿不落** → 新引擎恢复时走"有 run、无敞口 →
清残留"分支，字段变空串，看起来像"值真丢了"。要复现真恢复必须用 **`_persist()`**（含持仓簿）。

**结论写法（本轮用户认可的口径）**：裁定为非缺陷后，从文档的未决项表 / 场景节里**删掉**，
但在原地留一段**可达性裁定说明**（触发条件 + 判定链 + 实证脚本名），避免下一轮复核"重新发现"一遍。

## 4. 判据速查（chan.py 自动下单语义）

| 概念 | 落点 |
|---|---|
> **⚠️ 本节已按 2026-09-14 基线（`custom-dev` HEAD `16a2ca4d41`）整体校正。**
> 2026-09-11 重构 + 二期 Phase 8~12 之后，下面**加删除线**的条目全部作废 ——
> 它们描述的是"重构前"的引擎，读到时不要再当现状引用。
>
> **⚠️ 2026-09-22 二次校正（基线 `custom-dev` HEAD `58680f67`）——两条被改写的判断：**
> ①「止盈止损·触发口径」的依据**由根内极值反转为收盘价**（下表中已就地改写，
>   旧答案见该行括注）；② 新增「播报里的 1R 量值」一行（R 的绝对量 + 报价单位）。
> **凡在 2026-09-22 之前读过本节、记得"用 low/high 判触发"的，必须以本行为准。**

| 概念 | 落点（**2026-09-14 基线**） |
|---|---|
| 账户三态 | `account_state()` → `FLAT` / `LOCKED` / `RUNNING`；**SSOT，别内联重写** |
| 引擎过程态 | `_state` → IDLE / OPENING / IN_TRADE / EXITING（`IDLE` 同时覆盖空仓与锁仓） |
| **转移表（信号侧）** | `_decide_action(sig, today)` → `_Action`，**纯函数**：FLAT→① OPEN / LOCKED+今锁→② OPEN / LOCKED+跨日→③ CLOSE |
| **转移表（离场侧）** | `_decide_exit(bar)` → `_Action`，**纯函数**：RUNNING+今仓→④ 反向 OPEN（**或 CLOSETODAY**，见下）/ RUNNING+跨日→⑤ CLOSE |
| **止盈止损·触发口径**（**2026-09-22 已反转**，基线 `58680f67`，用户拍板） | **判定依据**与**判定时刻**必须分开答，混一句就是错答：<br>①依据 = **本根 K 线收盘价 `bar.close`**，**不再读 `bar.low`/`bar.high`** —— `Strategy/Exit.py` `check()` :340-350（多仓 `close = bar.close; if stop and close <= stop: return ExitCheck("sl", stop)`；空仓镜像 `close >= stop`）。<br>&nbsp;&nbsp;理由 = 判定的**依据**要和判定的**时刻**同源：时刻是"这根闭合之后"，依据就不能是"盘中到过多少"（那是已消失的价）。代价 = 止损更晚更深、不被插针扫；止盈/抬损也迟一根。<br>&nbsp;&nbsp;唯一豁免 `_atr()`（`Exit.py:146`）继续读 high/low —— TR 是波动率**度量**，不参与是否触发。<br>&nbsp;&nbsp;连带作废：「同根 K 线既破止损又破止盈 → 按止损计（悲观）」**已不可达**（一个 close 不可能同时 ≥tp 且 ≤stop），规则随口径删除；`close<=stop` 仍写在 `close>=tp` 前面，只是畸形计划的确定性兜底，**不是**悲观规则。<br>②时刻 = **该 bar 闭合之后** —— `Engine.on_bar`(:748) → `_settle_positions`(:797) → `check_with`(:818) ⇒ 盘中跌破**不会**即时触发。入场那根不判：`Engine.py:814` `if bar.timestamp <= run.entry_bar_ts: return`<br>③ L3 浮盈基准同口径：`best = max(best, close)`（`Exit.py:366`）—— 盘中冲高、收盘回落的那根**不再算达标**。<br>④ **改动边界 —— 只有三处判据 + 1 行取价**：可执行行净变 6 行，**全在 `check()`**（`:340` `close = bar.close`；`:342/:347` 止损判据、`:344/:349` 止盈判据、`:366` 1R/2R/3R 浮盈判据）。AST 去 docstring 比对 `Exit.py` 全部 9 个函数 —— `_initial_r()` / `plan()` / `_atr()` / `on_bar()` / `check_with()` **可执行语义零改动**（其余 diff 全是 docstring/注释）。<br>&nbsp;&nbsp;**结构止损点仍取分型极值，没跟着换成收盘价**：`A = entry − signal.fractal_low`（`:215`，空仓镜像 `:220`），`R = max(A, B)`（`:249`）中 A 的分型来源是 **`App/AppSSE.py:1405-1406` `f_klu.low` / `f_klu.high`**（`f_klu = bsp.bi.get_end_klu()`，即该分型 K 线的**极值**，不是它的 `close`）。`plan()` 签名 `(signal, entry_price, state, anchor)` **没有 `Bar` 入参** ⇒ 止损生成路径物理上读不到任何 K 线收盘价；长仓止损价 = `min(分型极值价, 入场−2×ATR)`。<br>🔒 护栏 `Trading/Test/test_p61_exit_close_only.py`：`ast.walk` 扫源码，`check()` 内出现 `.high/.low` 即红；同时断言 `_atr()` **仍**在读 high/low（否则"豁免"退化成"悄悄换口径也没人知道"）。<br>⚠️ **历史答案（2026-09-22 之前）**是「依据 = 根内极值 `bar.low <= stop`，根内到过即认定触及」。被问到旧版本 / 旧 state.db 数据时按旧口径答，被问"现在"一律按 close。 |
| **引擎天然滞后一根 · 无交易所侧条件单** | `Source/SSE.py:74` `bar_mode` 默认 `"confirmed"`（`Config.py:236`），`_on_frame` 只在 `klines[-1]` 时间戳变化时才把上一根发出去（:124-134）；服务端再叠 `App/AppSSE.py` `BAR_COMPLETION_BUFFER = 1.0`（:419）⇒ 引擎最晚可用的信息 = **闭合后 ~1s + 帧往返**。<br>grep `条件单|预埋|stop_order|conditional` **零命中** ⇒ 纯软件止损：触发后按**当时盘口超价追价**挂限价单（`SimNow._submit_close` + `_overprice_limit`），`ref_price`(=触发价) 只在取不到行情时兜底 ⇒ **成交价 ≠ 触发价**，跳空/闪崩全吃滑点。<br>dry_run 相反，按触发价 ∓1 tick（`Broker/DryRun.py:62-68`）—— 别拿模拟盘成交价解释实盘。<br>L3 跟踪/保本的最大浮盈同样**只在闭合时更新**（`only_update=True`）且自 2026-09-22 起按**收盘价**累计（`best = max(best, close)`，`Exit.py:366`）→ 跟踪止损同样滞后一根 |
| **播报里的 1R 量值**（2026-09-22 新增） | 三条 toast 由 `TradingEngine._notify_open` / `_notify_run_phase`（`Trading/Engine/Engine.py:1706` / `:1753`）生成：`_r_label(N, R)` → `1R（= 5 点）`。单位来自 **`Product.quote_unit`**（`Trading/Infra/Product.py` 档案字段，**只进文案不进计算**；8 品种按交易所标准合约文本标定：IF/IH/IC/IM=点、AU=元/克、AG=元/千克、CU/TA=元/吨）。<br>⚠️ `R` 取自该段计划 `params["R"]`，定义 `R = max(分型极值距离, atr_sl_multiple×ATR)` ⇒ **run 级、随每段入场位置与当时 ATR 变，不是品种常量**（别答成"IF 的 1R 恒等于 5 点"）。<br>两条退化：R 缺失 → 只写 `1R`（不编数字）；品种未标定 `quote_unit` → `1R（= 5）`（给数不给单位）。<br>开仓文案**不再**写 `止损(1R) = 4010`（把距离当价格），改为 `止损 = 4010（距入场 1R = 5 点）` |
| **唯一报单出口** | `_execute(act, ref_price, ...)`；前置校验链 `_pre_trade_check(act, today, sig, ref_price)`（A3） |
| ~~离场方式~~ | ~~`_exit_intent(pos, today)`~~ ❌ **已删**，被 `_decide_action` / `_decide_exit` 取代 |
| ~~锁仓~~ | ~~`_book_lock_pair` / `lock_pair_id` / `soft_exit_lock`~~ ❌ **全仓已删**（2026-09-11） |
| ~~解锁~~ | ~~`_unlock_position` / `_upgrade_lock_pair` / `PositionOrigin` / `ExitMode`~~ ❌ **全仓已删** |
| 运行态忽略信号 | `_decide_action` 顶部：`account_state() is RUNNING → return None`（规则 ⑶） |
| 运行态敞口笔数 | 无上限（`max_open_positions` 已删）→ **可自然出现多笔**，见 §3.5 更正块 |
| **平今** | ⚠️ **`CLOSETODAY` 报文现在存在了**（Phase 10 · D6，2026-09-14）：`OrderIntent.CLOSETODAY` → `INTENT_TO_OFFSET` 第三项；**仅 SHFE/INE**（`spec.supports_closetoday` 守卫），由品种开关 `prefer_lock_over_closetoday=False` 触发。CFFEX/DCE/CZCE/GFEX 传平今会直接报错，故默认恒走锁仓 |
| **FOK / FAK** | `spec.effective_order_advanced()`：CZCE 强制 FAK，其余读 `order_advanced`（默认 FOK）。**Broker 正文里没有交易所分支**，别去 `SimNow.py` 里找 |
| **报单手数** | `Engine._open_volume()`：CZCE 钉 1 手（FAK(1)≡FOK(1)），其余 `risk.max_volume`。⚠️ 交易所名硬编码在引擎正文，与 A2 有张力（见坑点 91） |
| **能看 vs 能下单（两个数字别混）** | **看行情 = 83 个别名**，事实源 `DataAPI/TqSdkAPI.FUTURES_ALIASES`（**手写累加表**，`:148-185`）；**能自动下单 = 8 品种**，事实源 `Trading/Infra/ProductProfile.PRODUCT_PROFILES`（IF/IH/IC/IM/AU/AG/CU/TA）。**83 = 81 唯一合约 + 2 组重复别名**（TA/PTA、A50/CN）；仅有 CFFEX 是完整的。⇒ 用户 2026-09-14 拍板**彻底解耦**：看行情侧不做任何品种过滤，下单侧靠闸门拦 |
| **下单闸门是【两层·同源】** | ①前置检查 `AppTrader.check_symbol_allowed()` → `GET /api/trader/product-check` → 前端 alert「不支持交易」+ 开关回弹（管**体验**，不发启动请求）；②权威闸门 `ProductProfile.assert_product_allowed()`（管**权威**：回放/CLI/异常路径）。**①直接调② → 永不漂移**（`p20[9x]` 逐例一致钉死）。**降级方向 = 放行**（查询接口挂了不能让人开不了单，交引擎兜底报错） |
| **会话期间换不了品种** | 前端 `app.js` 的 `autoOrderRunning` guard：引擎运行中，`loadStock` / `switchFreq` **早退 + alert「请先关闭自动下单」** ⇒ **不存在"开着自动下单切合约"的场景**，所以上面两层闸门无需处理运行时换品种 |
| **品种白名单** | `ProductProfile.assert_product_allowed(signal_symbol)`（唯一实现）；当前档案 8 个：IF/IH/IC/IM/AU/AG/CU/TA。未标定品种 → 引擎**拒绝启动** |
| **合约参数来源** | 实盘：`spec.apply_quote(quote)` 从真实月份合约行情**原子回填** + `instrument_verified=True`；取不到 → `_pre_trade_check` 拒单（A′ fail-closed）。离线：`mark_config_offline()` → `CONFIG_OFFLINE` |
| **费率来源** | `spec.apply_fee_rates(...)` 原子回填 + `fee_source ∈ {FEE_QUOTE, FEE_TRADE, FEE_CONFIG}`；空串 → 经济性判定 fail-closed |
| **交割月护栏** | `spec.delivery_guard_blocked(today, threshold_days=risk.delivery_guard_days)`（默认 1）；`_pre_trade_check` 里按三态分发：FLAT 拦 OPEN / LOCKED 拦 CLOSE·CLOSETODAY / RUNNING 不拦 |
| run 风控锚（D14/D15） | 只在 `_execute` 里「净敞口 0 → 非 0」时建立（`_run_start`）。**三处绕过 `_execute` 直接改簿的正向突变不会建 run**：`_note_close_rejected` 清幻影仓、`_reconcile_positions` 移除锁仓单侧、`_check_close_stuck` 重建 target → 留下「RUNNING 但 run=None」⇒ L1-L3 `_run_view()` 返回 None（**止损静默失效**）+ 下次重启触发 G2 拒启动。且 `_run_start` **必须传 `sig`**（`exit_policy.plan(sig, ...)` 要信号形态数据），对账/清幻影时没有信号 → 无法静默重建，正确处置是复用 `_run_start` 的 `sig is None` 分支（`run_start_incomplete` + SEVERE 告警），而不是什么都不做。⚠️ 该分支**只告警、不设 `_run_side` / `_run_plan`**（`Engine.py:1104-1116` 早退）
⇒ 它是「让它可见」方案，**不是**「补一个锚」方案，别误读成"传 `sig=None` 就能建 run"。
`_run_plan` 必须靠 `exit_policy.plan(sig, ...)`（`Engine.py:1118`）生成，缺信号形态数据就生成不出来
⇒ 这是该洞"设计上没法补"的根源。三个方案：①只告警（保守，推荐先做）；
②净敞口→0 时暂存 run 快照、回到非 0 且方向一致时**恢复**（零猜测，语义正确，需多存一份 kv）；
③猜锚（**不可行**：要改 `exit_policy.plan()` 签名，波及面远超预期，已撤回）。
**✅ 2026-09-14 二次复核（`custom-dev` HEAD `16a2ca4d41`）：「0 → 非 0」边三处已全部补上，本条**已修**，
不许再当"已知未修"或"新发现"报。** 落点：`_check_run_anchor(source)` 挂在
① `Engine._sync_state()`（:781，覆盖 `_note_close_rejected` 与 `_check_close_stuck` 两条尾路径）
② `Reconcile._reconcile_positions()` 尾部**无条件**一行（:144，"持仓对账"）——
这正是坑点 43 说的"别只挂 `_sync_state`"的落地形态。判据是 `_run_ready` 门（`_restore` 末尾才置 True），
故恢复期的瞬时假象不会误报；同一次异常由 `_run_missing_notified` 锁只报一次。 |
| 关闭自动下单·**离场口径** | ~~`_lock_remaining_positions`~~ → ❌ 已删，现为 `Engine._force_exit(bar, reason, force=)`：内部就是 `_decide_exit` + `_execute`（**与正常离场共用同一张转移表**），今仓 ④ 反向 OPEN / 跨日 ⑤ CLOSE。`force_lock` 覆盖开关已于 2026-09-10 删除（P4 变体A），别再假设"关闭 = 一律锁仓"。⚠️ **铺簿后想跑离场用它**，别用 `_settle_positions`（后者要求先有 run，见坑点 79） |
| 关闭自动下单·**账户归宿** | **冻结**（用户 2026-09-10 拍板甲方案）：留下的锁对**无自动出口** —— 信号门已关 / 关闭态不跑 settle / 重启也不变。需人工平仓或重新开启后由对向信号走 UNLOCK。关闭时写一条 `account_frozen` 事件使其可见（护栏 `test_p30_shutdown_exit_mode.py`） |

---

## 5. 波及面量化（用户最在意的一步）

用户极其在意**变更波及面可控、可一键回退**。给一个候选修法前，必须量化：

1. **独立草稿副本**：`cp -r chanpy_review p4_draft`，在主沙盒**之外**试改。
2. **跑全量回归**，逐条列出 break 的断言（`grep "✗"`）。
3. **区分两类破坏**：
   - **断言写错**（语义真的变了）→ 需要改测试语义，**重**
   - **夹具缺字段**（如 `make_pos()` 没给 `entry_date`，默认 `""` → `"" < today` 恒真 → 被当昨仓）
     → 补夹具即可，**断言一条都不用改，轻**
4. **列变体对照表**：`变体 | 关键分支取值 | 全量回归 x/y | 需改测试`，然后推荐。
5. 给出**预演后的行为输出**（不只是"应该会变成 X"，而是真的贴出新的报单序列）。

> 本轮实战：P4 三变体 —— A（严格按日期）+ 补夹具 ⇒ 25/25 全绿、零断言改写（推荐）；
> A′ 不补夹具 ⇒ p20 破 10 条；B（缺失即锁）⇒ p11 破 1 条（要改语义断言）。结论靠数据，不靠直觉。
>
> F1-F4 实战（2026-09-10，改"日期派生"）：首轮回归 20/26，
> **6 个失败全是"夹具时间数据不真实"**（timestamp 当序号用），不是断言错。
> 加"可信度降级链"后回到 26/26，**零断言改写**。
> 另一类是"假通过"：`test_p11 [6]` 注释写"**昨日**锁仓遗留"但把 bar 放在同一天 ——
> 旧实现靠 `entry_date=""` + `"" < today` 恒真歪打正着走了 CLOSE。
> **这类假通过只有在你拿掉隐式默认值之后才会暴露**，同样必须改夹具而非改断言。
> 经验：改"时间/日期派生"类逻辑时，**先跑一遍全量回归**看哪些夹具用了假 timestamp，
> 比逐个静态推演快得多（本轮 6 个失败一次定位）。

### 真实 A/B：用「反向补丁」重建 BEFORE 树（2026-09-10 实践，最推荐的做法）

用户要的不是"读代码看起来对了"，而是**同一段流程、两份代码、可对照的输出**。
最省事又最硬的做法：**不要去找历史快照，直接对改动文件做机械化反向补丁。**

```python
def _replace_once(path, old, new, tag):   # 锚点命中次数必须精确 == 1，否则 SystemExit
    src = io.open(path, encoding="utf-8").read()
    if src.count(old) != 1:
        raise SystemExit("[反向补丁失败] {}：命中 {} 次".format(tag, src.count(old)))
    ...
def _cut_span(path, start, end, new, tag): ...   # 整段删除（如新增的持久化段、闸门调用）
```

要求：

1. **锚点必须断言命中次数**（`== 1` / `== 2`）。不写断言的补丁会静默失效，
   于是你跑出来的"BEFORE"其实还是 AFTER —— A/B 变成自欺。
2. **反向补丁必须覆盖全部改动文件**。只回退"调用点"而不回退"被调用的新方法"，
   会让"改动量"统计出现 `+0/-0`（明明改了 30 行却报 0），报告直接失真。
3. **每个 stage 用独立进程**（`subprocess.run([sys.executable, scenario, "--stage", s1a])`），
   不要在同一进程里 `new` 一个引擎对象 —— 那不算重启，读者会质疑。
4. 场景脚本对**新 API** 要用 `getattr(x, "order_seq", None)` 之类安全读取，
   否则 BEFORE 树一跑就 `AttributeError` 崩溃，「预期失败」变成「无输出」，
   证据强度大打折扣。**崩溃 ≠ 断言失败**，要让它在断言层失败并给出计数。
5. 最后做一条**跨代码桥接**：把 BEFORE 产出的 `state.db` 交给 AFTER 代码加载 →
   若被拒绝启动，就证明"旧代码确实会造出坏数据，而新护栏真的能拦住它"。
6. **BEFORE 树里要【保留】本次新增的测试文件**（不要删掉）。删了的话跑它只会得到
   `can't open file ...`（rc=2），**证明不了测试能抓到这个问题**；保留才是真断言失败
   （rc=1 + 一串 ✗ 行 + `29 通过 / 12 失败` 这种可引用的计数）。
   配套：新测试里凡"可能取不到元素"的地方（`ev_list[0]`）都要写成
   `ev0 = ev_list[0] if ev_list else {}`，否则 BEFORE 树上直接 `IndexError` 崩掉，
   证据从"红"退化成"崩"。

> 实战结果格式（报告里直接贴）：
> `修复前 trades 2 行（应 3）、T00001 内容被替换；修复后 3 行完好`
> `修复前 lock_pair_id 分组 {'lock_00001': 4} ⚠；修复后 {'lock_00001': 2, 'lock_00002': 2}`
>
> 清房复验三件套（必须同时满足才算过）：
> ① 修复前代码树跑新测试 → **必失败**（证明测试真能抓到 bug）
> ② 解压交付 zip 覆盖该副本 → 新测试 + 邻接回归 **必全绿**
> ③ zip 内路径/内容与工作树 **逐文件 sha256 一致**（忽略 CRLF）

### 5.1 清房复验与「多向回环」（三向 = §5.1.6；四向 = §5.1.7）

1. `cp -r <work> clean`，并清掉**运行时产物**（`__pycache__/`、`State/`、`state.db*`、
   测试生成的 `_verify_*/`）。用 `copytree(src, dst, dirs_exist_ok=True)` **只覆盖不整目录删**
   （`shutil.rmtree` 删几百个文件会触发批量删除保护 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。
2. 用 `orig/` 把改动过的文件**还原成原版** → 模拟"用户当前的干净仓库"。
3. 解压交付 zip 覆盖上去。
   **⚠️ 复验基座必须是「完整仓库树 + zip 覆盖」，不能是「只解压 zip」**（2026-09-21 踩到，
   白跑一轮）：交付 zip 只含**改动的那几个文件**，单独解压出来是一棵**残缺树**
   （缺 `Trading/__init__.py`、`Broker/Base.py`、`Infra/` 等依赖）→ `import Trading.*`
   立刻 `ModuleNotFoundError: No module named 'Trading.Broker.Base'` → 全部用例报"找不到包"。
   **这是复验脚本的错，不是包的错**，但它长得像"包坏了"，极易误判。
   正确顺序＝先把完整仓库 `copytree` 到干净目录，**再**把 zip 覆盖上去，然后在新树上跑全量。
   跑全量直接用仓库自带的统一入口（如 `run_all_tests.py --only Trading/Test`）——
   它自带凭据隔离与 env 约定，别手搓 `PYTHONPATH`。
   顺带：`os.remove(zip)` 在打包脚本里是多余的（`ZipFile(path,"w")` 本就截断重写），
   写了反而会被批量删除保护拦下。
4. **逐字节比对**：改动文件应与工作副本一致；全树 diff 应只剩交付包新增物 + 运行时产物。
5. 在新树里**重跑 §2.2 的验收四连**。任一项不过 → 包不能用。
6. **三向回环（比字节比对强，强烈建议写进 `cleanroom_*.py`）**：
   单靠"逐字节比对"证明不了 `orig/` 真是改动前的状态 —— 万一打包时手滑把改后文件也塞进
   `orig/`，比对会照样全绿，而用户拿它回退时才发现回退不了。做法：
   - **A** 在干净树里用 `orig/` 覆盖 → 跑探针/自验**必须红**（尽量贴出具体报错，
     如 `AttributeError: 'TradingEngine' object has no attribute 'alert'`）→ 证 `orig/` 确是改动前；
   - **B** 在 A 的树上跑交付的 `patch_*.py` → 探针**必须绿**，且产物与 zip 的 `new/`
     **逐字节一致** → 证补丁脚本可从零复现交付物。
   任一条不成立，说明 `orig/` / `new/` / 补丁脚本三者至少有一个在撒谎。
7. **四向回环 —— 把「护栏有没有判别力」也从"相信"变成"证明"**（2026-09-22 定型，
   适用于「这次改动动了某条**口径**，并同步改了护栏」的场景，如出场判定判据、字段来源）：
   前面第 6 条只验 `orig/` 是不是真·改动前；**它证明不了护栏本身有牙** ——
   如果新旧护栏都恒绿，`orig/` 比对照样全绿，改动等于没被任何测试约束。
   做法：建**四棵独立树**，一次跑完贴四个计数：

   | 树 | 生产代码 | 护栏 | 期望 | 证明什么 |
   |---|---|---|---|---|
   | `AFTER` | 改后 | 改后 | **绿** | 新代码 + 新护栏自洽 |
   | `BEFORE` | 原版 | 原版 | **绿** | 旧代码 + 旧护栏自洽（且三文件 == `orig/` 自证） |
   | `MIX1` | 原版 | 改后 | **红** | 新护栏能抓住**旧口径** |
   | `MIX2` | 改后 | 原版 | **红** | 旧护栏能拒绝**新口径**（两向都咬得住） |

   - `MIX2` 是最容易被跳过、也最有价值的一条：只做 `MIX1` 时，"新护栏很强"这句话
     成立，但"旧护栏本来就很弱、随便改都不红"这个可能**没被排除**。四向补齐才算闭环。
   - 断言写成一次性布尔（`四向回环成立 = True`）+ 四个计数（如 `32/0 · 25/0 · 19/6 · rc=1`），
     贴进交付报告比"我加了护栏"这句话硬得多。
   - `BEFORE` 树要顺带自证 `hash` 等于 `orig/` —— 否则 `BEFORE` 绿可能只是"没换成原版"。
   - 与第 6 条不重复：第 6 条管**原版快照真伪**，本条管**护栏判别力**，两条都要做。

**两个定义陷阱（都实测踩过）**：

- **基线的定义是「用户项目当前状态」，不是「项目最初基线」**。若拿最初那棵树做清房，
  历轮**新增**的文件（如某轮才加的 `DataAPI/XxxAPI.py`）在新树里根本不存在，
  于是文档引用 / import 校验会报「文件不存在」的**假失败**，白白耗掉一轮排查。
  清房前先确认基线树里**包含用户实际拥有的全部新增文件**。
- **排除规则必须精确到"运行时产物"，不能按扩展名一刀切**。实测：用
  `tar --exclude='*.json'` 复制基线副本，会把 `Test/fixtures/*.json`（K 线输入）
  与 `Test/snapshots/*.json`（快照期望基线）一起排掉 → 一次跑出 **5 个假失败**，
  看上去像"基线比我改的还差"。正确写法是**只排明确的运行时产物**：
  `__pycache__`、`*.pyc`、`Test/report.json`、`state.db`、`*.log`，其余一律保留。
- **拷贝基线别忘了排 `.venv` / `node_modules`**（2026-09-14 踩到）。它们体积大且
  沙箱会**拒写其中个别文件**（实测 `WinError 5 拒绝访问` 终止整个 `copytree`，
  还会连带触发 safe-delete 拦截）。用 `ignore=` 回调按目录名 + 后缀双过滤，
  把 `.venv/ venv/ .git/ node_modules/ __pycache__/ .pytest_cache/ State/ _verify_*/`
  和 `*.pyc *.pyo *.log *.jsonl` 一并排除，比事后删**更安全**（事后删文件就是上面那条批量删除坑）。
- **⚠️ 本机 `.env` 会让清房全套测试「多出红灯」，必须单独归因，否则会误判成本批改坏**
  （2026-09-14 实测，值一轮排查）。本项目实例：用户项目带 `TRADING_BROKER=simnow`，
  而 `test_step2_smoke_freq.py` 断言的是**离线默认** `broker.name == "dry_run"`
  → 稳定 4 红，且构造 simnow broker 走网络，该测试由 1.4s 拖到 ~197s
  （表现为"全套突然从几分钟变十几分钟"，很容易误以为是死循环）。
  > **✅ 2026-09-14 二次实测：此干扰已修，在 `custom-dev` HEAD 上不再复现。**
  > 修法是**隔离配置来源**（`isolated_trading_env()` 把 broker 钉成 `dry_run`），它已随
  > 第 6 批进入基线，故**旧的原版仓库也不复现**。判据（本轮实测）：
  > `PYTHONPATH=. TRADING_BROKER=simnow python Trading/Test/test_step2_smoke_freq.py`
  > → **48 通过 / 0 失败，3 秒**（不是 4 红 + 197s）。
  > ⇒ 下面这套"三组对照定位法"仍是**方法论资产**（换仓库/换配置来源时照用），
  > 但**别再把它当成"本仓库当前会发生的红灯"来预告用户** —— 先跑一遍再下结论。
  **三组对照定位法**（便宜且结论硬）：
  | 对照 | 目录 | 期望 |
  |---|---|---|
  | C0 | 基线树 + 新批，**带 `.env`** | 与 C2 相同 |
  | C1 | 基线树 + 新批，**去掉 `.env`** | 转绿 ⇒ `.env` 是唯一变量 |
  | C2 | 基线树 + **还原本批改前**，带 `.env` | 与 C0 逐条相同 ⇒ 与批次无关 |
  报告里**必须把"C0 ≡ C2 且 C1 转绿"写清楚**，并明确"无 `.env` 环境（CI）= 全绿"，
  不要让用户以为是自己改坏的。判据：**只要把 `.env` 一移就走/一来就红，就与本批无关。**

- **⚠️ 基线必须"逐文件核验版本"，不能把整个目录当成一个基线**（2026-09-14 实测，差点让清房
  变成走过场）。本轮复用的 `clean8`（= 用户项目 + 第五批）里，那份
  `Trading/Test/test_step2_smoke_freq.py` **已被提前同步成第六批版本**（182 行、已含
  `isolated_trading_env`）—— 于是"改前"对照组也跑出 `51 passed / 0 failed`，
  **把本批正要证明的 `.env` 冲突整个掩盖掉**，而脚本还很"绿"地判了个通过。
  识别信号：**跑完"改前"发现"预期该红的没红"** → 立刻 grep 本批特征标记在基线树里出现几次
  （应当为 **0**）。修法：该文件改从**用户项目**取真基线，并加一条
  `assert "<本批特征标记>" not in 基线文件内容` 把这件事**钉死在脚本里**。
  ⇒ 一般化：**基线里每个"本批会改的文件"，都要有一条"基线版不含本批标记"的断言。**
- **"改前"组的期望值要显式写进脚本，并逐项核对**（别只打印出来看着顺眼）。
  写法示例：`改前期望 = {verify_doc: 6, verify_doc2: 26, grey_js: 1 失败, 其余: 0}`，
  跑完逐项对上才继续；对不上就**先修基线**再往下走。不做这一步的"改前"等于没跑 ——
  它唯一的用处就是**证明本批改动确实产生了差异**，对不上就等于没证明。

### 5.2 「就地改」时如何造出可比对的改前基线

场景：用户点名要求就地改某个文件（§0.1），磁盘上没有改前副本 → 做不了 diff。

做法：**把本轮施加的每一处编辑逐条反向撤销**，重建「改前」文本。关键是**必须有自校验**，
否则撤销清单漏一条就会产出"看着像 diff、其实在撒谎"的东西：

```python
# 自校验判据（两条都要）：实测过，靠它们拦下过两次遗漏
# ① 行数：重建结果的换行符数 == 开工时实测值（文件末尾换行差异允许 ±1，需显式说明）
# ② 残留扫描：重建结果里不得含任何"本轮新增"的标记串
#    ⚠️ 选标记串要挑「本轮唯一引入」的；用到了项目里本来就有的词（如某测试名）
#       会假报残留 —— 本轮就把 p15b 误当残留，白绕一轮。
if lines_old not in (EXPECT, EXPECT + 1): raise SystemExit("撤销清单有遗漏")
if [t for t in LEFTOVER if t in old]: raise SystemExit("重建结果仍含新增标记")
```

另两个要点：
- **反向替换的锚点必须断言命中次数 == 1**，多一处少一处都直接报错退出。
- 逐条替换时**打印每步的行数增减**，一眼定位是哪一条改了行数（本轮据此快速发现
  "§7.3 里还新增了一行 p15b 定义行"没被撤销）。
- 交付时把**改后全文 + 重建的改前基线 + unified diff + 变更说明**一起打包
  （用户能自己 diff 出同样结果）；文档类改动**单独打包**（`Docs_*.zip`），
  与代码补丁**解耦**，各自独立可还原。

---

### 5.3 核对「挂点/回调挪位置」型改动：假上游 + 真模块 A/B（2026-09-18 实战）

场景：用户丢一个 zip（往往只含 1 个文件）问「这个改法对不对」。**不跑引擎、不接网络**也能给出决定性证据。

做法：
1. 建两棵树：A = 基线文件（`sandbox/repo`），B = A 的 `copytree` + `copyfile` 覆盖补丁文件。
2. 只换外部 IO，不动被测代码：对 `Trading/Source/SSE.py` 就是
   `SSE.urllib.request.urlopen = lambda req, timeout=None: FakeResp(chunks)`，
   假的 `FakeResp` 只需 `read1()` / `__enter__` / `__exit__`。
3. 场景至少两个：① **改动声称的路径**（只喂 `b": heartbeat\n\n"`，数 `on_idle` 调用次数：0 → 5 即证）；
   ② **改动波及的顺序**（心跳/数据帧交替，打印 `泵 / 事件` 轨迹，一眼看出挂点跑到事件之前还是之后）。
4. **收口坑（实测踩过）**：假上游耗尽后 `iter_sse` 正常 `return`，外层 `while self._running:` 会**立刻重连且无退避**
   → 死循环。让假 `urlopen` 在**第 2 次调用**时 `src._running = False` 再抛异常（`events()` 的 `except` 会干净退出）。

判据（**频率型改动专属**，比"改完还会不会被调用"强得多）：
**新增调用的那个状态下的「调用频率上界」** vs **单次调用的「代价上界」**，两个数摆出来比。
- 代价上界从代码取：如 `pulse()` → `wait_update(deadline=now+keepalive_wait)`，`keepalive_wait=0.2s`
  （tqsdk `api.py:_fetch_msg` 是 `while not self._pending_diffs: await recv()` → 无数据时必耗满窗口）。
- 频率上界从上游循环取：如心跳是每轮无条件 `yield`，循环里唯一等待是 `wait_update(+100ms)` → 0.1s/帧。
- 消费侧一旦落后就不再 `read1`，积压落在 `iter_sse` 的 `buf`（`buf += chunk` 无上限）→ **不会自愈**。
本次就是靠这个不等式（5 帧/s < 10 帧/s）发现"挂点前移"会把 25~55s 的**回报滞后**换成**行情滞后**。

顺带一条通用技巧：全树哈希比对「沙盒 vs 用户项目」时，**先做"仅换行差异"归一化**
（`a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")`），否则会把 49/51 个文件误报成内容差异。

#### 5.3.1 二阶探针：把"代价"改成"不等待"之后，必须再量"单次放行几格"（2026-09-18 P68k v2 实战）

用户按上一轮意见把空闲泵改成非阻塞（`pulse(0)` → `wait_update(deadline=now)`）后再来复核。
**只量"代价从 0.2s 降到 ~0"不算验完** —— 被等待方若是**有队列的服务者**，还要问它每次被放行推进了几格。

直接调用真实实现（不做假上游），把 `tqsdk` 的方法**绑到 Stub 上**即可，量三个数：
1. **空转代价**：`deadline=now` → **0.0001–0.0012 s**、事件循环 generation **1**；`now+0.2` → **0.2034–0.2087 s**、
   generation **57699**。→ 证实"改动的收益"真实存在。
2. **放行格数**：预置 N 个 `_wait_idle_list` future，数 `pulse(0)` 后 pop 掉几个 —— 恒 **1**；0.2 s 窗口能弹满。
   源码对得上：`baseApi.py:96-98` 的 `_run_until_idle` 每次 `pop(0)` 一次。
3. **消费者拓扑**：谁在消费那个队列？`connect.py:187-191`（ws 收包循环）每收一包前 `await _wait_until_idle()`
   → **一次 `pulse(0)` 只放行 ≤1 个 ws 报文**；且 md 与 trade 共用同一条 md ws（`api.py:3720` →
   `multiaccount.py:132-144` 串行）→ **整连接吞吐上界 ≈ 帧率**（≈10/s）。

**判据模板（三问，缺一即未验证）**：① 这次调用把服务方队列推进**几格**？② 那个数是**固定值**还是**按剩余量弹满**
（决定"够不够"能否靠提高频率补）？③ 服务方**独占**还是**多消费者共用**（共用时单消费者的频率上界 = 全局上界）。
报告里只写"代价降到 0、所以没问题"会被打回。

**探针本机两个坑（都踩过）**：
- `asyncio.new_event_loop()` 在 Windows 返回 **`ProactorEventLoop`**，`_loop_self_reading` 靠 future 回调自续，
  每次 `stop()` `_event_rev+1` → tqsdk `_run_until_idle` 的 `while _check_rev != _event_rev` **永不收敛**
  （实测 `now+0.2` 空转 57699 次、`now` 直接卡死；`faulthandler.dump_traceback_later` 抓到 `_check_rev=5999/_event_rev=6003`）。
  **探针里显式 `asyncio.SelectorEventLoop()`**（与 `baseApi.py:25` 一致），否则测出的"慢"是本机 loop 的假象。
- 跑探针用**项目 venv** `C:\my_chan_project\.venv\Scripts\python.exe`。用 workbuddy 的 python 3.13 装 cp314 的 numpy
  会 `ImportError: numpy._core._multiarray_umath`。

---

### 5.4 消除"数字哨兵"：把同一概念的多个表示收敛成一个（2026-09-21 实战）

**信号**：接口用**数值**表达"读不到 / 不可信" —— `-1`、`(-1, -1)`、
或"异常时 `return 0`"（`0` 常常是**可信读数**的语义，于是"不知道"被冒充成"确实是 0"）。
一个概念有两种表示，调用方就永远有**漏挡一边**的空间。

**为什么必须收敛（把事故算式重放一遍）**：数值哨兵是**合法整数**，漏挡的调用方会把它当业务量算下去：

```
engine_vol = 2 ; real_vol = -1            # 读数不可信时的旧值
n_to_close = engine_vol - real_vol = 2 - (-1) = 3
# 3 > engine_vol ⇒ 被当成"柜台比账本少" ⇒ 账本该侧仓单整笔删除 + 补记虚构平仓盈亏，且无任何告警
```

换成 `None`：同一个漏挡动作直接 `TypeError` —— **"宁可炸，不可错"**。
收敛的收益不是"更优雅"，是把**静默的错误**变成**响亮的异常**。

**收敛口径（写进基类 docstring 当 SSOT）**：`非负 int` = 可信（`0` 是"柜台确实无仓"的**可信语义**）；
`None` = 不可信（唯一）。**不许有第三个值**。四个失败成因（未连接 / 通道不稳 / 抛异常 /
形态或取值认不出）**同值**。顺带一条：把"接口不支持该功能"（dry_run）与"读数失败"**共用同一个 `None`** ——
两者对调用方的含义相同（没有可用读数），**刻意不引入第二个哨兵**。

**收敛时必查的"配套口子"（一个都不能漏，否则等于没收敛）**：
1. 该概念的**所有上游出口**（本例 5 个：`_position_split` / `_position_total` /
   `_take_baseline` / `_take_baseline_split` / `real_position`）；
2. **值域校验**：让"返回非 `None` ⇒ 一定是可信的非负值"成为不变量，否则下游无法只判 `None`
   （本例：`if today < 0 or his < 0: return None`）；
3. **诊断/日志文案**：基线是 `None` 时要记"本次**未做**校验"，**不许**用哨兵值顶替后落进
   "未按预期变化 / 昨仓没减 / 今仓没减"这类分支 —— 那是把**"没读到"报成"读数没动"**（同一类假信息）；
4. **静态护栏**：AST 扫 `return -1` / `return (-1, …)` **回流**比 grep 稳
   （grep 会误伤注释与 docstring）；
5. **消费方守卫"保留、只改口径"**：跨模块边界（如 `Engine/Reconcile.py` 消费 `Base` 级接口）
   的 `if real_vol is None or real_vol < 0:` **不要因为上游收敛就删** —— 边界仍需自守（纵深防御），
   只更新注释说明"`None` = 唯一信号；负数 = 实现违约的畸形"。

**咬人自检的坑：同名内外两层守卫必须"整层回退"。**
纵深防御常写成"调用方有一处守卫 + 被调方有一处**同名**守卫"。只回退内层**不改变可观测行为** →
自检报 `SLIPPED`，你会误以为"护栏是摆设"，其实是**自检没有判别力**。
正确做法：那条 CASE **同时改两处**（整层回退），并**两个方向各测一次**
（"只挡 `None`、不挡负数" vs "只挡负数、不挡 `None`"）—— 两个方向都被抓才叫护栏完备。

**用新用例当探针，允许它咬出计划外的缺口（本轮真咬出一个）。**
写"收敛后不可信只能是 `None`"这条用例时它**红了**，暴露出 `_position_split` 处理
**账户视图里「单个值」** 时形态判据只认"属性"、不认"键空间"：值若不是 `Entity`（没有属性）
但字段在键里（普通 dict），会被判"认不出"→ 走字段读取函数**静默读成 0**，
而 `0` 是"柜台确实无仓"的可信语义 → **一个假的"无仓"覆盖掉真仓**。
这与本轮要杀的是**同一个病的内层版本**（外层容器已判、内层值没判）。

> **判据（可直接复用）**：凡是"先认容器形态、再从容器里取值"的函数，
> **取到值以后必须再判一次形态** —— 只有一层形态校验的读取函数，永远留着一个内层缺口。

---

## 6. 防回潮护栏（改完必补）

仓库有「契约测试」传统。做 SSOT 收口时，同时补一个 `Trading/Test/test_pXX_*.py`，
用 `inspect.getsource` 做**源码扫描**，把"已收口"从注释升级为可执行契约：

```python
import inspect, re
from Trading.Engine.Engine import TradingEngine
_src = inspect.getsource(TradingEngine)
check("内联 all(SOFT_EXIT_LOCK) 判定式仅 SSOT 1 处",
      len(re.findall(r"all\(\s*p\.origin\s+is\s+PositionOrigin\.SOFT_EXIT_LOCK", _src)), 1)
check("内联 any(not SOFT_EXIT_LOCK) 判定式 0 处",
      len(re.findall(r"any\(\s*p\.origin\s+is\s+not\s+PositionOrigin\.SOFT_EXIT_LOCK", _src)), 0)
```

另加"与旧口径等价"组（7 种簿面逐一比对 新 == 旧）来证明**纯重构零行为变化**。

### ⚠️ 源码护栏的五个陷阱（都踩过）

1. **不要用"某字符串不存在"当护栏** —— 注释、docstring、报错文案里为例举旧写法
   而写的 `INSERT OR REPLACE` 会让 `"INSERT OR REPLACE" not in body` 假红。
   正确做法是**扫语义载体**（SQL 字面量 / 函数名）：
   ```python
   check("save_trade 的 SQL 是裸 INSERT",
         "INSERT INTO trades VALUES" in _body(src, "save_trade")
         and "INSERT OR REPLACE INTO trades" not in _body(src, "save_trade"))
   ```
2. **取函数体要用"下一个 `\n    def `"做终点，并给 fallback**；取不到终点会一路吃到 EOF，
   把后面别的函数（比如 `set_json` 的 `INSERT OR REPLACE INTO kv`）算进来 → 假红。
3. 只剥注释再判"是否还在用某模块"：`"\n".join(l.split("#")[0] for l in src.splitlines())`
   —— 否则注释里的"（原 itertools.count(1)）"会把"已弃用"判成"仍在用"。
4. **最彻底的做法是 `ast` 解析，而不是字符串/剥注释** —— 适用于三类断言：
   - 「某参数已被删除」：`"force_lock" not in [a.arg for a in fn.args.args]`
   - 「某调用不再传某关键字」：`[k.arg for c in calls for k in c.keywords] == []`
   - 「某判据函数仍在被调用」：`c.func.attr == "_exit_intent"`
   好处：**注释里刻意保留的历史说明**（"旧实现用 `force_lock=True`，已删除"）
   完全不影响判定，不用跟注释斗智斗勇。缺点：要写几行小工具函数
   （`_arg_names(fn)` / `_arg_default(fn, name)`，后者要按
   `base = len(args) - len(fn.args.defaults)` 对齐默认值）。
   实测：同一条护栏用"字符串不存在"，BEFORE 树假红/假绿都出现过；换 ast 后两边都准。
   **实战补强（2026-09-21，正则版第一跑就假红）**：当护栏要扫的是**另一个文件**
   （如冒烟脚本 `smoke_*.py`）时，同样必须用 ast。当时先写的是
   `re.findall(r"real_position\([^)]*\)\s*or\s*0", src)`，结果被**我自己刚写的那段解释性
   docstring**（里面正举着 `real_position(...) or 0` 当反例）命中 → 假红。
   改成 ast 遍历 `BoolOp(op=Or)`、找 `left` 为 `…real_position(…)` 的 `Call`
   且 `right` 为 `Constant(0)` 后立刻干净。
   **同时补两条自证**，否则这条护栏可能只是恒真的摆设：
   · 正向：把 `"lp = b.real_position(Side.LONG) or 0"` 单独 `ast.parse` 后**必须命中**；
   · 反向：只出现在 docstring 里的同形文本**必须不命中**（"注释不算证据"落到可执行断言）。

参考现成护栏：`test_p25_origin_decoupled.py`（来源标签解耦）、
`test_p26_terminology_guard.py`（中文术语）、`test_p27_account_state_ssot.py`（三态 SSOT）、
`test_p29_id_uniqueness.py`（持久 ID 唯一性 + fail-fast + 配对不变量）。

### 6.1 「误伤型护栏」的修复 SOP（2026-09-14 实战，铁律级）

**症状**：护栏本身没写错意图，但**判据太宽**，把无罪的新代码判成违规 →
二期每加一个功能就撞一次，最后变成"已知红"被无视。chan.py 真实案例两个（都在 2026-09-14 修掉）：

| 红灯 | 误伤原因 | 正解 |
|---|---|---|
| `p15a [1b]` | 断言把**字段集合写死** `sorted(...) == ["max_volume"]` → Phase 11 加 `delivery_guard_days` 即失配 | 拆成「**已删项不在** + **不超出受控清单**（新增须同步该行）+ 关键项仍在」。写死集合 = 任何有意新增都误报 |
| `p28 [6c]` | 判据 = "出现 `timedelta(days=1)` 即违规" → 误伤 Phase 11 的 `_weekdays_between()`（**数工作日**，纯日历算术） | **按语义收窄**：同一代码块（函数体/模块顶层）内**同时**出现 `timedelta(days=1)` **与**夜盘信号（`NIGHT_SESSION_START_HOUR`，或 `.hour` 与 0/20/21/23 比较）才算违规 |

**🚫 绝对禁止的修法：加白名单 / 加排除目录让它闭嘴。**
`p28` 若改成"把 `InstrumentSpec` 加进白名单"，会连带放过该文件里**将来真出现**的手写夜盘偏移
—— 那是**削弱护栏**，不是修 bug。

**✅ 必须同时交出的两组反测证据**（只收窄不验证 = 无法证明"没变松"）：

1. **判据自测**（合成代码片段，直接测判据函数本身，不依赖仓库状态）：
   无罪样例不报 ✅ / 有罪样例必报 ✅（覆盖：函数体内、模块顶层、引用常量名、字面量比较 ≥2 种形态）
2. **注入式反测**（端到端，证明在**真实目标文件**上仍有效）：
   复制仓库 → 往目标文件追加真违规代码 → **护栏必须变红且指名该文件** → 移除 → 恢复绿。

> ⚠️ 注入式反测的**副作用**：注入后的行号是**临时状态**，**绝不要写进文档/交付说明**
> （正常状态文件行数不同 → 会触发文档自带的"行号越界"检查，本轮真踩到）。

**修完必须复跑全套并更新基线数字**：2026-09-14 修完后 `p15a` 110→**113**、`p28` 47+1红→**52**，
全套 **51 passed / 0 failed**（此前 49 绿 / 2 红）。
**→ 这两个红灯已修，不许再当"已知红"报；"全绿门禁"自 2026-09-14 起成立。**

> ⚠️ **"全绿"的口径是【无 `.env` 的空环境】+【20:00 之前】—— 两个维度都要写进报告**
> （2026-09-14 补充实测，第二轮修订）。
>
> ① **`.env` 维度**：本机 `.env`（`TRADING_BROKER=simnow`）曾让 `test_step2_smoke_freq.py`
>    稳定 4 红 + 慢到 ~197s。**该问题已随第 6 批修入基线**（`isolated_trading_env()`），
>    2026-09-14 复测为 48 通过 / 0 失败 / 3 秒 ⇒ **此维度当前不分叉**（详见 §5.1 的更正块）。
> ② **时间维度（本轮新发现，真实分叉）**：`test_p24_close_offset.py` 的日期锚是
>    **自然日**（`datetime.now(CN).date()`），而引擎按**交易日**比较；
>    `Types.py` 的 `NIGHT_SESSION_START_HOUR = 20` ⇒ **20:00 之后跑必红 5 条**（④ 变 ⑤）。
>    实测同一份**未改动**的基线树：19:12/19:38/19:54 三次 51/51，20:02 起跑 50/51。
>    ⇒ 报数字必须**同时标注跑的时刻**；只在晚上验证过的"全绿"是假结论。
>
> **判据（本轮定型的写法）**：结论一律写成**矩阵**而非单值 ——
> `{无 .env, .env} × {20:00 前, 20:00 后}`，并逐格标注哪些格子是"本批引起"、哪些是"既有缺陷"。
> 只说"51/0"或"50/1"都会被用户一跑就抓到不符。

---

### 6.9 新增测试的登记 + 「读投影字段」的写法（2026-09-23 定型）

- **新写的测试不会自动进门禁**：必须手加 `Test/run_all.py` 的 `COMPONENTS` 一行
  （`("名字", [sys.executable, os.path.join("Trading","Test","test_xxx.py")])`）；
  加完顺手 grep 同步 `run_all_tests.py` docstring 里写死的组件数（陈旧且**没人会红**）。
- **读投影 / 嵌套字段一律走 `.get()`，数值再包一层 `float()` 兜底**：
  用例里写 `rv["phase"]`，一旦该键被删（正是要防的那种回归），用例在**第一个**断言处
  `KeyError` 中断 —— 后面几十项**一条都看不到**，门禁只留一个 Traceback，
  "护栏红在哪"当场丢失。正确写法：
  ```python
  def g(d, k):  return (d or {}).get(k)              # 缺键 → None（报红，不中断）
  def num(d, k):
      try:    return float(g(d, k))
      except (TypeError, ValueError): return float("nan")   # 比较恒 False → 报红
  ```
- **JS 取回的字段先归一**：`undefined` 会被 `JSON.stringify` **丢掉**该键 ⇒ Python 侧
  `o["disp"]` 又是 `KeyError` 中断。harness 里写 `x === undefined ? null : x`，
  Python 侧一律 `o.get(...)`。
- **stub 元素要预置哨兵值**（如 `title: 'STALE'`），否则"该清没清"的残留分支测不出来
  （例：无运行段时徽标必须把旧价 tooltip 清掉）。

### 6.10 「同一份视图、两处投影」必须同构（2026-09-23，差点交付半残）

**背景**：chan.py 的自动下单跑在**子进程**里。前端轮询的 `GET /api/trader/auto-order/status`
走 `FrontAPI` → `App/AppTrader._read_engine_switch(out_dir)`，它**读 state.db 的 kv 自己重新投影
一遍**（API 侧刻意不 import 引擎 —— 进程托管层不与状态机耦合），**不**调用
`Engine.auto_order_status()`。⇒ **同一个 `run` 视图有两份独立的投影代码。**

**后果（可达路径明确）**：只在引擎侧加了字段 ⇒ 前端拿到的字段恒 `undefined` ⇒
功能静默半残，而**所有静态断言仍然全绿**。

**三层护栏（本次定型，照抄即可）**：
1. **键集合**：用 `ast` 各抓两处的 dict 字面量（判据 = 同时含 `stop` 与 `anchor` 两个键），
   断言 key 列表**逐字相等**。
2. **取值来源**：断言三项取自 `plan.params` 的**同名字面键**（`_phase` / `R` / `_tp_nominal`）。
   ⚠️ **不能用子串匹配** —— 引擎侧写的是 `str(self._run_plan.params.get("_phase") or "")`、
   API 侧是 `str(prm.get("_phase") or "")`，`"params" in expr` 这种写法会直接失效；
   正确做法是 `ast.walk` 收集表达式里所有 `x.get('K')` 的**常量键名**。
3. **行为层（最强）**：真实 `state.db` 上两处**逐字段相等**。步骤 = 真实引擎
   `on_bar/on_signal` 推进 → `_persist()` → **`store.close()`（WAL 未 checkpoint 时另一连接读不到）**
   → 调 API 侧函数 → 比对**初始 / 保本 / 跟踪三个时点**的整个 dict。
   附带自检：断言 kv 文档的键集合 == `_persist_run` 源码里 dict 字面量的键集合
   （防止组装 kv 时少写/多写字段，让"投影照样对"变成假绿）。

**反测（判别力）**：把 API 侧的 `_phase` 改成 `phase`（模拟键名分叉）⇒ 第 2、3 层必须同时变红。

### 6.11 门禁里的【已知红】清单（报数字前先判是不是它）

- ⚠️ **`fixtures_integrity` 在 Windows 工作树上恒红（预存在，2026-09-23 定性）**：
  git checkout 把 `Test/fixtures/*.json` 落成 **CRLF**，而 `Test/gen_fixtures.py` 显式写 **LF**，
  `--check` 用 `filecmp` 做**字节**比对 ⇒ 6 条 `CHECK-FAIL` 恒红，真漂移被淹没在恒红里。
  **判据**：在真实项目目录跑 `Test/gen_fixtures.py --check`（**只读**，只写系统临时目录）
  得到**同样那 6 条** ⇒ 与本次改动无关。`gen_fixtures.py` 只 import `json/math/os/random/sys/datetime`
  （`tempfile/filecmp` 在 `__main__` 块内联 import），压根不引用引擎 / 前端。
  ⇒ 报门禁数字时写成 **`120/121`（唯一红 = fixtures_integrity，预存在）**，别写成"全绿"或"我改红了 1 条"。
- ⚠️ **在工作树里跑 `Test/run_all.py` 会改写 4 个未跟踪的本地数据文件**：
  `App/stock_float_mc.json`、`App/text_annotation.json`（**手工标注数据**）、
  `App/double_click_dt.csv`、`App/last_code_freq.json`（`AppConfig.app_data_dir` 指向 `App/`，
  相关测试未重定向到临时目录）。两条纪律：
  ① 在**真实仓库**里跑门禁前先备份这 4 个；
  ② 做交付时这 4 个（连同 `Test/report.json`）**绝不能进 zip** —— 否则解压覆盖会抹掉用户真实数据。
  **自检手法**：打包前拿真实项目目录逐文件 md5 比对，差异清单里出现这 4 个 = 打包范围写错了。

---

### 6.12 「某参数怎么算的 / 跟 X 有没有关系」的溯源与敏感度核验（2026-09-23 定型）

用户问「成交价怎么定？跟 R 有没有关系？」这类**参数溯源 + 敏感度**问题时，**只贴代码链不够** ——
要把「唯一的赋值链」+「实盘数值闭合」+「排他性自洽」+「分段斜率」四件一起给。可离线做（纯函数，不接网络、不重跑引擎）：

1. **赋值链每一步带 `文件:行`**：从报单 → 成交回报 → 成交价 → 锚 → 参数。链上任何一环用"读不出结论"的注释
   都不算证据（本例：`Exit.py` 模块 docstring 的假设会把 A/B 主导判反）。
2. **用实盘三值做闭合校验**：把实盘事件里的三个数（本例 `anchor=7584.6` / `R=7.8` / `stop=7576.8`）
   拿代码纯函数重跑，要求**逐位复现**。跑不齐 → 说明某一环的输入取值错了（见第 3 点），**先修取值再谈结论**。
3. **排他性论证（多解释时最有力）**：当同一组实盘数有 ≥2 种解释（本例 `R = 7.8` 可能是 A 主导也可能是 B 主导），
   找出**一个能证伪的离散取值**：本例把 ATR 按两个时点截断各算一遍 —— 截止 10:10:45 → `B=7.8`（= 实盘 R，自洽）；
   截止 10:11:00 → `B=8.343`（`max(A,B) ≥ 8.343 ≠ 7.8`，与实盘**矛盾**）。矛盾那支直接排除 ⇒ 唯一解。
   ⚠️ 取值的依据必须是**引擎实际看到的状态**：`Engine.py:760` 在 **bar 闭合后**才喂 `on_bar` ⇒ 成交发生在某根
   bar 中途时，**那根尚未闭合的 bar 不在 ATR 缓冲里**。取错截断 = 整条结论错。
4. **分段斜率，别只给"有关系"**：`max(A, B)` 型参数要按分支各给一张斜率表 ——
   A 主导段 `ΔR/Δ成交价 = 1` 且止损不动；B 主导段 `ΔR/Δ成交价 = 0` 且止损整体平移；并给出分界条件。
   再补**时点敏感度**（本例同一信号晚 15s 成交，B 从 7.8 跳到 8.3429）。
5. **顺手给"退化窗口"**：本例 ATR 未就绪（`B=0`、R 只剩 A）有两处 —— 进程重启后前 `atr_period+1` 根 bar
   （缓冲是内存态）、每交易日开盘后前 15 根（`on_bar` 跨日 `clear`）。这类窗口是"参数缩水"的高发段，用户最需要。
6. 产物：一个只调纯函数的 `probe_*.py` + 分段输出日志；脚本按惯例进证据目录、**不随交付包发**。

### 6.13 变异自证脚本：还原必须进 `try/finally`，否则变异会留在工作树上（2026-09-23 踩到）

变异脚本的形态是「快照 → 改文件 → 跑用例 → **还原**」。**还原那一步必须受 `try/finally` 保护**：

- 症状（本轮实测）：脚本里 `print("...集合 == {2.0} ...".format(a, b))` 的花括号没双写 → `IndexError` 抛在
  `restore()` **之前** ⇒ rc=1、前半段输出正常（变异也确实被拦住了），但**工作树上 `Product.py` 的
  `win_loss_ratio` 仍是变异值 `3.0`**。
- 后果：紧接着跑的门禁 / 打包会**以变异版本为准**，那一轮结果全部作废。
- 做法：① 先 `snaps = {rel: io.open(p,"rb").read()}` 快照全部被改性文件；
  ② 「变异 + 跑用例」整段包进 `try:`，`finally: for rel,raw in snaps.items(): write(raw)`；
  ③ 收尾断言 `io.open(p,"rb").read() == snaps[rel]` 并**打印结论**（别只 assert，看不见）；
  ④ 脚本里凡 `print` 出现的**字面花括号**（`{2.0}` / `{}`）一律双写 —— 与 `.format()` 混用必炸。
- 补救：一旦中招，先手工把文件改回（**逐条列出改回什么**），再核对行数 / 关键行，然后才重跑门禁。

### 6.14 门禁必须在「文件静止」窗口里跑（2026-09-23，第二次踩）

门禁是**判决器**，读的是工作树**当前**内容。启动后若又去写文件 —— 哪怕只是跑一个变异脚本（它会临时改写
`index.html` / `Product.py`）—— 门禁读到的就是**变异版本**，整轮结果无意义。

- 纪律：`跑门禁 → 期间零写入`；中途动过任何文件 ⇒ **作废重跑**，并在报告里写明"上一轮因编辑期间跑而作废"。
- 实操：① 用 `python -u -X utf8 Test/run_all.py`（无缓冲）启动，日志实时可见 ——
  不必靠"日志文件 0 字节"去猜是卡住了还是块缓冲（重定向到文件时 Python 默认 8KB 块缓冲，跑完才 flush）；
  ② 启动前 `find "$W" -name __pycache__ -prune -exec rm -rf {} +` 清一遍；
  ③ `.env` 先移开（避免真实凭据参与）；④ 等待期间只做**不写 `sandbox/work`** 的事：
  写报告、写记忆、整理 `deliver*/` 目录都可以。

---

## 7. 坑点清单（都踩过）

> **编号规则（2026-09-14 整理）**：本清单是**一条连续编号的列表** —— 文档顺序 = 编号顺序。
> **新增坑点一律接在全文末尾**，编号 = 上一条 +1；**不要在任何子节里重新起号**（历史上 §7.2 曾从 68 重新起号，造成两个 68、其后全部少 1）。
> 引用别处条目一律写「见坑点 N」；改号后**必须全文件 grep 同步引用**：`grep -n "坑点 [0-9]\+" SKILL.md`。
>
> **历史编号对照（2026-09-14 重排前 → 重排后；未列出的号不变）**：`34→51、35→52、36→53、37→58、38→54、39→55、40→56、41→57、42→34、43→35、44→36、45→37、46→38、47→39、48→40、49→41、50→42、51→43、52→44、53→45、54→59、55→60、56→61、57→62、58→63、59→64、60→65、61→66、62→67、63→68、64→69、65→70、66→71、67→72、68→73、69→75、70→76、71→77、72→78、73→79、74→80、75→81、76→82、77→83、78→84、79→85、80→86、81→87、82→88、83→89、84→90、85→91`
> 旧稿里的引用（例如指向「CZCE 手数硬编码」的那条）请按上表换算；
> §7.1 起的分块乱序（42-53 排在 29-41 之前）已一并理顺。

1. **`test_p26_terminology_guard` 会扫仓库根目录的 `verify_*.py`**，禁用词
   「腿 / 双仓 / 丢仓 / 腿态 / 挑腿」一个都不能出现 —— 写验证脚本时就要避开。
2. **`_restore` 按 `spec.trade_symbol` 过滤持仓**：测试注入的 `Position.symbol` 必须等于
   `InstrumentSpec.trade_symbol`（= `CFFEX.IF2609`），否则恢复时被当"旧合约外部平仓"丢弃（假红）。
3. **`Position.__init__` 必填 `open_order_id` 与 `exit_plan`**（漏了直接 TypeError）。
4. **`PositionBook(max_positions=None)` 是生产配置**（不限容量）→ `add` 永不抛错 →
   任何"落簿失败"防御分支都是**死代码**，别把它当活缺陷报。
5. **Windows 临时目录清理会因文件句柄占用报 `WinError 32`**：
   `tempfile.TemporaryDirectory(prefix=..., ignore_cleanup_errors=True)`。
6. **不要硬编码 Unix 时间戳**：需要时用 `date` / PowerShell 取。
7. **`position` property 是单仓兼容层**，多仓场景会抛 `PositionBookError` —— 用 `positions.positions`。
8. 报告里**不要写"应该会"**；每个结论都要有脚本输出行作为证据。
9. **硬编码触发价必踩坑**：止损价由信号 fractal 距离决定（`frac_low` / `frac_high`），
   不是固定 `min_r_points`。写"砸穿"的 bar 前先读 `p.exit_plan.stop_price` 现算。
10. **`spec.trade_symbol` = `CFFEX.IF2609`**，而行情代码是 `KQ.m@CFFEX.IF`；
    人工注入 Position 时写错 symbol → `_restore` 静默过滤掉（簿面变空，看起来像"没恢复"）。
11. **⚠️ 测试夹具的 `timestamp` 是"递增序号"（1,2,3…），不是真实毫秒**
    （`make_bar(5000, ...)`、`entry_bar_ts=4000`、`entry_bar_seq*1000`）。
    一旦你的改动涉及"从 timestamp 派生日期/时间"，这些夹具会算出 **1970-01-01**，
    与夹具自己声明的 `date="2026-09-02 09:35"` 互相矛盾 → **批量假红**。
    正确解法不是改断言，而是给解析函数加**可信度判据**：
    ```python
    # timestamp 派生出的日期 >= PLAUSIBLE_DATE_MIN(2020-01-01) 才采信，
    # 否则回退用该锚的 date 字符串。生产路径永远走 timestamp 分支（App 是 int(ts*1000)）。
    d = trading_day_of_ms(ts)
    if d and d >= PLAUSIBLE_DATE_MIN: return d
    return trading_day_from_clock(anchor.date)
    ```
12. **冗余副本要用"可信度降级链"，不要"默认值兜底"**。当同一事实有两个来源
    （`entry_bar_ts` 毫秒 vs `entry_date` 字符串；`timestamp` vs `date`），
    缺失时应**回源头重算**，而不是给默认值 —— 默认值若落在合法语义域内
    （如 `""` 在 `"" < today` 里恒真 = "很久以前" = 昨仓），错误就被静默消化了。
13. **`EventLog` 有 64 条 / 1 秒缓冲，读它的 jsonl 文件取证前必须 `eng.ev.flush()`**，
    否则刚写完的事件读出来是**空**（会误判成"事件没写"）。
    事件结构：`{"at": <ISO>, "kind": "<事件名>", ...其余为 payload}`。
14. **持久化标识绝不能由"进程内易失计数器"生成** —— 这是本项目反复出现的**同一类根因**：
    ```python
    self._trade_seq = 0            # __init__
    self._lock_pair_seq = 0
    self._seq = itertools.count(1) # broker
    ```
    三处都是"重启即归零"，而它们分别被用作 `trades.trade_id`、`positions.lock_pair_id`、
    `orders.order_id` —— 全是**持久化标识**。叠加
    `INSERT OR REPLACE`（`Store.save_trade` / `save_order`）→ **静默覆盖，不报错**。
    自查方法（拿到任何交易类仓库都先做）：
    ```bash
    # ① 找出所有"看起来唯一"的标识，看生成方式
    grep -rn "itertools.count\|_seq\b\|_seq = 0\|{:0[0-9]d}" --include=*.py Trading/
    # ② 看这些 id 是否进 PRIMARY KEY 且用 INSERT OR REPLACE
    grep -n "PRIMARY KEY" -B3 -A3 Trading/Infra/Store.py; grep -n "INSERT OR REPLACE" Trading/Infra/Store.py
    # ③ 看 _persist / _restore 有没有把计数器带上
    grep -n "_seq" Trading/Engine/Engine.py | grep -i "persist\|restore\|set_json\|get_json"
    ```
    实测危害分两级：`trade_id` / `order_id` **立即发作**（主键直接冲突 → 后者覆盖前者，
    库里只剩"最后一个进程生命周期"的记录）；`lock_pair_id` 是**潜伏**的 ——
    自然流程下 FIFO 顺序（同组原仓恒在反向仓前、更老组恒在前）恰好掩盖撞号，
    只有出现"单边残留"时才串台（升级错误的仓 + 本该升级的仓永久滞留）。
    **根因与修法见 §3.6**：不是"计数器不可靠"，是这三个计数器**漏接**已有的
    `kv` 持久化（`bars_seen` 就是同构的正确对照）→ 修法 = 加 `set_json` 一行，不是换 ID 方案。
15. **定级"严重性"前先跑一遍自然流程**：同 3.5 节。构造场景能复现 ≠ 生产可达。
    正确姿势是**两个都做**：构造场景证明"机制上确实会错"，自然流程证明"当前是否可达"，
    据此把缺陷分成「立即发作」与「潜伏（被某个顺序巧合掩盖）」——
    用户的决策依赖这个区分，不能混为一谈。
    判据链必须显式写出来，且每级都要有"可信性门槛"。参见本轮
    `Position.__post_init__`（bar_ts → entry_at）与 `_day_of_anchor`（ts → date）。
16. **改"日期/时间判定"前先 grep 全部判定点**：本项目同一口径曾散在 4 处
    （`on_signal` / `_close_positions` / `_exit_intent` / **`Reconcile.py` 对账成本**），
    漏掉任何一处都会造成口径分叉。`grep -rn "date\[:10\]\|now_cn()\[:10\]"` 是入口，
    但要注意 `Reconcile.py` 这种 **Mixin 文件**容易被主文件的 grep 范围漏掉。
17. **夜盘**：`entry_date` 必须是**交易日**（夜盘 21:00 后归属次一自然日），
    不是自然日。日盘品种（IF）两者恒等，所以此坑只在夜盘品种暴露 ——
    测日盘夹具**永远测不出来**，必须单写一组时间换算断言（参见 P28 [1a]-[1k]）。
18. **有人会提议"用 unix 时间戳生成唯一 ID"—— 要能立刻反驳，且必须给可复现证据。**
    它确实解决了"重启归零"，但**不能保证唯一**，两条可复现的失效路径：
    - **同一毫秒批量生成**：`_lock_remaining_positions`（Engine.py:1263-1273）把 `remaining`
      当**列表**交给 `_close_positions` → 循环里逐笔调 `_book_lock_pair`。
      实测同一次批量锁仓内两次 `now_ms()` **完全相同**（1789034252388 == 1789034252388）。
    - **墙钟回拨**：`now_ms()` 读墙钟（Engine.py:974/976 已在用）。NTP 校时 / VM 快照回滚 /
      改系统时间都会让它往回走；**钟再次经过回拨前用过的时刻**时，生成出与历史**完全相同**的 ID
      （实测：T0 → 回拨 2 分钟 → 钟走回 T0，第 3 个 ID == 第 1 个 ID）→ 配合 `INSERT OR REPLACE` 直接覆盖审计记录。
    单调计数器不依赖墙钟，两条都不会犯。所以**正确解是"计数器 + 持久化"，不是时间戳**。
19. **时间戳不能代替 `entry_bar_seq`**（会有人这么提，理由听起来很顺）：
    - `entry_bar_seq` 读写的 `bars_seen` **本来就已经持久化** → 跨重启单调，**没有"最老判错"的 bug**；
    - 它的语义是**根数**不是时刻：`bars_held = bars_seen - entry_bar_seq`（Engine.py:505/902/1134）。
      换时间戳就要除以周期 → 引入周期依赖，与 Engine.py:90 注释里刻意的设计
      （"与周期、与时间戳单位都无关"）相悖；
    - "跳过入场当根 K 线"那一处**本来就在用时间戳**：`bar.timestamp <= pos.entry_bar_ts`（Engine.py:502）。
    结论：**不要动它**。先把"它坏了"证伪，再谈改。
20. **⚠️ 把 `INSERT OR REPLACE` 改成 `INSERT`（fail-fast）之前，必须先把"ID 生成端"接上持久化**，
    否则等于把"静默覆盖"换成"**每次重启后第一单必崩**"，比原 bug 更糟。
    而且 ID 生成端往往**不在引擎里**：`order_id` 由 broker 造
    （`Broker/Base.py` 的统一入口 `_next_order_id` + `DryRun` / `SimNow` 各自的调用点），
    引擎只能在 `_restore` 里把它抬升（`seed_order_seq(max(kv, 库内 max))`）。
    **三个 ID 是一组，缺一个都不成立**：只播种 `_trade_seq` 而留着
    `itertools.count(1)`，播种无效；只 fail-fast 而不抬升 broker 序号，则重启必崩。
21. **`wipe_runtime_state`（`--fresh` 回放重跑）里，序号要按"配套表是否被保留"分别处理**：
    它清 `trades` / `positions` / `processed_signals` → 所以 `trade_seq` / `lock_pair_seq`
    应当**一并清**（让重跑 ID 逐轮一致、可比对）；但 `orders` 表是**刻意保留**的审计底稿 →
    `order_seq` **只增不减**，否则重跑会与历史委托号相撞（fail-fast 后直接抛错）。
    **判据：序号与它服务的表，要么一起清、要么一起留。**
22. **一处行为出现两套判据时，正确解是"删掉覆盖开关"，不是给两套打补丁**。
    实例：离场方式本应由规则 ⑸（建仓日期 → 今仓 LOCK / 昨仓 CLOSE）唯一决定，
    但"自动下单关闭"路径带了个 `force_lock=True`，直接短路掉 `_exit_intent`
    → 昨仓被白锁一次（多付开仓费、次日还要再平两笔）。
    修法是把调用点改成恒走 `_exit_intent` **并把 `force_lock` 参数整个删掉**
    —— 只要参数还在，下一个人就会再用它。
23. **改"日期/时间"类判据之前，先检查测试夹具的日期锚会不会退化到墙钟**。
    实例：某测试夹具用 `entry_bar_ts = entry_bar_seq * 1000`（序号当毫秒 → 1970），
    且调 shutdown 前**不喂 bar** → `_current_trading_day()` 只能退化到**墙钟（真实运行日）**
    → 夹具被新判据判成"昨仓" → 整组断言红。
    这类红**不是代码错**，是夹具口径没跟上：要么给夹具显式 `entry_date`，
    要么关闭前喂一根 bar 把 `last_bar` 立到"今天"。
    **先跑一遍全量回归看哪组红，比逐个静态推演快得多。**
24. **用户选了"保持现状 / 冻结 / 不自动处理"类方案时，必须把该状态做成【可见】**。
    本次：关闭自动下单后今仓被 LOCK 成锁对，而信号门已关、关闭态不跑 settle、
    重启也不改变 → 账户**永久停在双向持仓**，且前端只显示 `enabled=false`。
    做法：补一条一次性事件（`account_frozen`，带 `frozen_n` / `lock_pair_ids` /
    `net_exposure`），**只记日志、不改交易行为**；并在交付说明里**单独成节**标注
    "这是我自作主张的附加，可这样剥离"（列出去掉它要删哪几行）。
    「有意选择的静默」和「漏掉没做」在代码里长一样，交付时要说清是哪一种。
25. **`Store.wipe_runtime_state()` 曾漏清 `positions`（复数）—— 2026-09-10 已修**。
    修前它只清 kv 的 `position / day_stats / bars_seen / trade_seq / lock_pair_seq`
    （`Store.py:244`），而 `--fresh`（replay 源默认）走的就是它 → **上一轮的持仓整对残留**，
    下一轮 `_restore`（优先读复数字键）把幽灵持仓当真：`account_state()` 按残留判 LOCKED、
    新信号被当"锁仓态"处理。现已在清理列表补上 `"positions"`（`order_seq` 仍刻意不清）。
    核"清房重跑是否真干净"仍要 dump `kv` 键清单实测，**不要信 docstring**。
    且注意：**`--fresh` ≠ 删库文件** —— `--fresh` 保留 `orders` 审计底稿（故 `order_seq` 不清），
    要绝对干净只能删 `state.db`（三件：`-wal` / `-shm` 一起）。
26. **判"某异常状态能否由历史版本自然产生"要用提交考古，不要在代码里找"可能"**：
    `GET /repos/{owner}/{repo}/commits?path=<file>&sha=<branch>&per_page=40`
    拿逐版本文件（raw.githubusercontent 按 sha 取），再 grep 关键键/写入点。
    本次据此证明「`origin` 键」与「`lock_pair_id`」是**同一提交**引入、且所有能写锁仓记录的
    版本都给两笔都写 id → "有 origin 但缺/空 lock_pair_id"的记录**无自然产出者**，
    此前仅凭代码推演的"旧库入口"实际只能由**外部污染**产生。
27. **`on_signal` 的解锁门只解"昨仓锁"**：`opp[0].entry_date < today` 才走 UNLOCK；
    同日锁（`entry_date == today`）走的是**开新仓**（规则 ⑸-①）。
    做"解锁 → 再锁"这类实验必须**跨日**喂 bar，否则会把自己写的同日剧本误判成缺陷。
28. **同一文件不要在同一条消息里并行发多个 Edit**（后写的会覆盖先写的，前一个改动静默丢失）。
    改完 grep 一次确认落盘。
29. **⚠️「删库重启会自动对账」是错的（2026-09-10 实测 17/17）**。
    库被删 ⇒ 簿为空 ⇒ `_restore` 末尾那段首拉对账被 `if not self.positions.is_empty():`
    （`Engine.py`）**整段跳过**，一个事件都不写；而且对账**单向**——`_reconcile_side`
    只在 `real < engine` 时清 phantom，`real > engine` 时只写 `position_mismatch` 告警、
    **不接管**未知真实持仓。后果：实盘有仓 + 库被删 → 引擎当自己空仓照常开新仓 →
    敞口静默叠加，孤儿仓永不被跟踪、永不平仓。
    ⇒ 任何"删库 / wipe 重置"方案的安全前提**只有一条：账户已无未了结持仓**
    （项目既有闸门错误信息里早就写着这句，新增闸门照抄同一措辞）。
    验证这类主张要写实验（空簿 + 假 `real_position` 读数 + 对照"簿非空时对账确实会跑"），
    别用直觉答；用户会对"删库就完事"这类简化主张要证据。
30. **`str(Side.LONG)` 在本项目渲染为「多」**：脚本里用 `{"LONG": 2}` 这类字符串键
    模拟 broker 读数会**静默取不到**（`real_position` 返回 0）→ 反而触发 phantom 清仓，
    把实验结论带偏（本次先踩后修）。夹具里用 **Side 枚举对象**做键，并在读数函数里
    同时兼容枚举与 `str(side)`。同理簿面断言里的方向字段是 `多` / `空`。
31. **A/B 的 BEFORE 树里，新测试必须能跑到"行为级失败"**：若新测试第一句就是
    `TradingEngine._新方法`（BEFORE 树没有）→ `AttributeError` 直接崩，A/B 证据退化成
    "崩了"而不是"断言失败"（与"文件不存在 rc=2"同级）。做法：`getattr(X, "_m", None)`
    兜底把第 [1] 组判失败但不中断，`src.index(...)` 类护栏包一层 `_safe_body()` 返回空串
    → 失败清单里就会长出 `[2a] 抛出 RuntimeError -> None`、`[6f] 簿面仍残留整对锁仓`
    这类行为级证据（本次 BEFORE 树 PASS=17/FAIL=21，很有说服力）。
32. **P26 术语护栏会扫新写的注释与测试字符串**：本次「平错腿/升错腿」（源码注释）与
    `open_first`（测试里构造旧键值）各被记 1 次红。写注释/夹具字符串时要过术语表；
    构造旧 schema 记录时键值可用 `locked` 之类未被禁的值（闸门只查**键是否存在**）。
33. **"闸门"类改动的固定形状（本项目已 4 处同款，新增时照抄）**：
    `_restore` 装载进簿**之前** → 纯函数挑记录（便于单测，`@staticmethod`）→
    `self.ev.write("<kind>", n_..., detail={...})` → `raise RuntimeError(...)`
    错误信息四段式：**现象 / 后果 / 原因（为什么自然流程写不出来）/ 处理**
    （处理统一为"确认账户无未了结持仓后，删除 Trading/State/state.db 再启动"）。
    判据必须在错误信息里说清"构造性保证"，否则下一个人会以为是误报。
    配套测试至少 4 组：污染记录拒启动（含 `n≥1` 边界）、**不误伤**（合法形态逐一放行）、
    自然流程产物放行、源码护栏。**闸门收紧时同步改老测试的旧契约断言**
    （本次 P29 [5g]「空 id 一律合法」拆成 [5g] 拒 / [5h] 放行，A/B 里它必须变红）。

34. **复现「对账 / 卡单」类路径：子类化 `DryRunBroker` 覆写读数，别去改引擎**。
    ```python
    class FakeBroker(DryRunBroker):
        armed = False                       # 开关：未武装时读数返回 None，引擎跳过对账
        def real_position(self, side):      # 只有 armed 后才给真实读数
            if not self.armed: return None
            return 2 if side is Side.LONG else 0
        def trade_confirmed(self, intent, signal_key=None):   # 造 CLOSE 卡单
            if self.armed and str(intent).upper().endswith("CLOSE"): return False
            return True
    ```
    两个时序坑（都踩过）：
    - **卡单窗口按根数**：`bars_elapsed = bars_seen - submit_bar_seq`，默认
      `close_stuck_bars=5`。要造"复核时刚好 armed"，必须喂 **`close_stuck_bars - 1`** 根平静
      bar 再武装、再喂 1 根 —— 喂满 5 根的话，第 5 根就在未武装状态下被判 confirmed 清掉了，
      表现为 `in_flight=False`、场景跑空。
    - **`real_position` 返回 `None` ≠ 0**：前者让引擎整段 skip 对账，后者才是"柜台无此仓"。
      用错会把实验做成"什么都没发生"。
    - 键用 **`Side` 枚举对象**，不要用 `{"LONG": 2}` 字符串键（见坑点 30）。
35. **「术语 / 残留 / 注释过时」类结论必须先全仓扫描再下笔**。
    2026-09-12 教训：我在报告里写"用户手工平掉一腿"被当场反问 —— 全仓扫描后确认
    代码与文档**零残留**，那 31 处"腿"全在 `test_p26_terminology_guard.py` 的**禁用词清单本体**
    （护栏自己）。扫描时注意：**护栏文件必然包含被禁词，命中它不算残留**，要排除后再判。
    同一条：**自己报告里的措辞也会被 P26 护栏扫**，写成脚本/注释前先过术语表（坑点 32）。
36. **下结论前先查 skill / 项目 memory，别把"已知未修"当"新发现"报**；
    **出报告前把 §7 这份坑点清单当 checklist 逐条过一遍**，别只靠现场扫描。
    2026-09-12 连踩两次：
    - 我花两轮复现出"run 风控锚三处绕过点"，最后才发现 §4 的「run 风控锚」条目里
      早已记下同一结论（含三处位置与正确修法）；
    - 拿他方审查报告做交叉核对时，对方报的 `dropped_legacy_keys` ClassVar（坑点 55）
      与 §7.3 p33 失效条目（坑点 57）**全在我自己的坑点清单里**，而我上一版报告漏了两条。
    ⇒ 正确顺序：现场扫描 → `Grep` 本 skill §7 + `.workbuddy/memory/` →
    若已记录，报的时候明确写「**已知未修**（此前已记录），本轮复核远端仍如此」；
    若清单里有而报告里没有，**补进去再交付**。
    用户会对"同一件事被当新东西报"和"清单里有的却漏报"两种都敏感。
37. **"计数器/状态机"类缺陷要查【重置点全不全】，而不是只看累加点对不对**。
    2026-09-12 在他方报告里发现（我自己两轮都没看出）：`_close_fail_streak`
    全仓 6 处引用，只有"达上限"清零，**成功 CLOSE 路径从不清零** → 语义是"连续被拒"
    实现成"累计被拒" → 跨 run 累积，把引擎自己刚开的**真仓**当幻影清掉
    （复现见 `repro_streak_accum.py`）。
    ⇒ 通用查法：`Grep` 该字段全部引用 → 列出「+1 点 / 清零点 / 判据点」三栏表 →
    问"每一个语义上的'这一段结束了'是否有对应清零？"（成交成功 / 对账全清 / 重启 /
    换 run / 方向切换）。**清零点缺失 = 语义漂移，且往往是两个缺陷的叠加点**
    （这里它同时是 §4 run 缺口路径 C 的触发源）。
38. **"取 min/max 截断"类写法必查下游是否按【截断前】的语义记账**。
    `min(lots, target.volume)`（Engine.py:859/881）本身不破坏不变量（我据此降过级，
    没错），但下游 `_book_close`（:1072/1080）用 `pos.volume` **整笔**记 Trade +
    整笔 remove → **部分平仓 = 账实不符**。
    ⇒ 查法：看到 `min(` / 截断，**顺着 act.volume 追到 `_book_*`**，确认记账与
    remove 用的是 `act.volume` 还是 `pos.volume`。
    对称性佐证：`_pre_trade_check` 有 `close_volume_exceeds_target` 却没有
    `close_volume_below_target` → 只挡一边本身就是信号。
39. **"前端读得到吗"要一路追到 API 侧的实际返回键，不能只看引擎有没有这个字段**。
    `auto_order_status()`（Engine.py:1440）有 `run` / `close_cooldown`，前端
    `app.js:7458` 也读 —— 但子进程架构下 API 走的是 `_read_engine_switch`
    （AppTrader.py:841-848），返回键里**两个都没有** → tooltip 永远死数据。
    ⇒ 查法：`Grep` 前端读取键 → `Grep` API 侧 `return {...}` 的字面键 → 两者求差。
    kv 没持久化的字段（如 `close_cooldown`）即使想补也补不出来，要一并点名。
40. **多人评审时，把"对方有我没有"做成显式表格进报告**（用户会拿来做交叉核对）。
    2026-09-12 连做两份他方报告核对，各补进 2-4 项我自己没有的。
    同时要敢报**判断分歧**（如对方判"一期已全部做完"、我判"未全做完" ——
    分歧根源是她没逐项核对 §5.8.5 的 D12 清单），不要为了一致而抹平。
41. **⚠️ 跑批脚本别把仓库根目录写死 —— 否则"干净目录复验"会得到假绿**。
    2026-09-12：我做 overlay 复验时，跑批脚本里 `ROOT = <沙盒已改动的副本>`，
    于是"基线"和"打补丁后"跑的是**同一份已改动代码**，两次都是 38/38，
    看起来通过、实际什么都没验到（后来发现基线里新测试本该红却全绿才起疑）。
    ⇒ 跑批脚本必须 `ROOT = sys.argv[1]`；复验脚本再补一条**自检断言**
    （如 `assert "_check_run_anchor" not in 干净目录/Engine.py`），
    确认"干净目录真的是打补丁前的状态"再跑。
42. **新增契约测试前先查编号占用**。
    2026-09-12 我先起名 `test_p42_audit_fixes.py`，后来才发现文档 §7.3 已把
    `p42_price_from_quote` 预留给二期 D20 → 只能改名 p43（连文件内 4 处引用一起改）。
    ⇒ 查法：`Grep "p4[0-9]" Docs/自动下单重构-分析与实施计划.md`。
43. **"自检/告警"类新钩子的挂点必须覆盖到所有改簿路径，别默认 `_sync_state()` 万能**。
    `_sync_state()` 看似是所有改簿路径的公共收口，但 `Reconcile._reconcile_positions`
    里的 `_sync_state()` 只在 `all_cleared` 块和 G4 块内调用 —— **对账只清一侧时
    两块都不进**，钩子根本不触发（我第一次就是把 `_check_run_anchor` 只挂在
    `_sync_state` 里，测试直接暴露没告警）。
    ⇒ 查法：列出所有 `positions.add/remove` 的位置，逐个确认**到 `_sync_state()` 的
    路径无条件可达**；不可达就在该函数尾部**无条件**再挂一次。
44. **`EventLog` 是按秒 / 按 64 条缓冲写的**，测试里直接读 `events.jsonl` 数条数
    必须先 `eng.ev.flush()`，否则恒为 0 → 假红（2026-09-12 p43 首次跑 4 条假红）。
45. **`shutil.rmtree` 删几百个文件会触发批量删除保护**（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。
    做"干净目录"复验时用 `copytree(src, dst, dirs_exist_ok=True)` 只覆盖、不整目录删。


46. **`EventLog.write(kind, **payload)` 落盘的 JSON 键是 `kind`**（不是 `type` / `event`）
    —— `EventLog.py:45` 构造 `{"at": ..., "kind": kind}`。断言事件流必须
    `[e.get("kind") for e in read_events(ev)]`；用 `e.get("type")` 会全拿到 `None`，
    极易误判成"这条事件根本没写"（本轮就踩了：把"有事件"读成"静默"）。
    `EventLog` 是**带缓冲**的（满 64 条 或 隔 flush_interval 才落盘）→ 断言前必须 `ev.flush()`。
47. **判定"某异常状态自然是否可达"，最有力的证据是找到它的**唯一自然入口**并否掉它**：
    半配对（单笔 `soft_exit_lock`）的唯一自然入口是 `_book_lock_pair` 里
    `positions.add(lock_pos)` 抛 `PositionBookError`；而 `PositionBook(max_positions=None)`
    （`Engine.py:68`）与 `restore_max = None`（`Engine.py:438`）都硬编码、
    `add` 的唯一 `raise` 是容量校验 → `_max` 恒 `None` → **永不抛** → 半配对自然不可达。
    ⇒ 结论从"理论上可能"变成"只有外力改簿可达"，处置建议随之完全不同。
48. **用 `str.replace` 删脚本里的带缩进代码块，锚点必须连**行首缩进**一起写**：
    只匹配 `check(...)` 而留下 4 个空格，会生成"只含空白的行" → 下一行报
    `IndentationError: unexpected indent`（行号还指向无辜的下一行，很费轮次）。
    同类：改完用 `ast.parse()` 做语法自检，别等运行才发现。

49. **`changes.diff` 的基准不能一律用 BEFORE 树** —— 本项目 A/B 的惯例是
    「BEFORE 树里**刻意保留**新测试，让它真断言失败」，于是**测试文件在两棵树里完全相同**
    → 以 BEFORE 树为基准生成的 diff 里，**整个测试文件的改动会消失**
    （本轮第一版 diff 只剩 `Engine.py` 的 14 行，测试新增的 [8] 组 100+ 行不见踪影）。
    正解：diff 基准取**「上一版交付的 zip 内同一文件」**（= 用户手上那一版），
    这样 diff 恰好表达"从上一版到这一版改了什么"；并加一条自检 ——
    **每个改动文件在 diff 里必须恰好出现 1 个 `+++ b/<path>`**。
50. **从 zip / 二进制通道读出的文本要归一化 CRLF 再喂 `difflib`** ——
    `z.read(rel).decode()` 得到的可能是 `

`，与工作树的 `
` 逐行比对会产出
    **"全文 differ"的假差异**（本轮实测 4275 行 → 归一化后 170 行）。
    这与"远端 vs 本地比对必须忽略 CRLF"是同一个坑，只是这次踩在 diff 通道上。
    自检办法：diff 行数应该与"肉眼估计的改动规模"同量级，几千行必然是假差异。

51. **判断"某个静默 return 是不是唯一的静默点"要数全分支** ——
    `_upgrade_lock_pair` 有 4 个防御分支（缺 id / 同 id 多候选 / 配对不在簿 /
    配对 origin 异常），逐个 grep `ev.write` 才发现只有第一个不写事件。
    补事件时**保持行为零变化**（仍 `return`），并在测试里同时钉两件事：
    ①事件写出来了（新）；②簿面/三态与修复前完全一致（防"补事件顺手改了行为"）。

52. **审核"远端分支是否合入某批改动"用三层证据（缺一层就有盲区）**：
    ① **全仓逐文件比对**——按远端文件树逐个拉取，与本地工作树做 **CRLF 归一化**后比内容。
       一次暴露所有差异（含你没预期到的）；副产品是能证明"远端没有对手改的代码"。
    ② **AST 符号核验**——对每个改动项，用 `ast` 在远端文件上验证符号/形参/调用点。
    ③ **在远端副本上跑全量测试**——证明合入的不只是文本，而是能跑通的行为。
    ⚠️ 只做 ②③ 会漏掉"本地新建但没推送"的文件；只做 ① 无法证明行为可用。
53. **判"某开关/参数是否已删除"必须用 AST，不能用字符串 `in`** ——
    远端注释里**刻意保留**的历史说明（例："2026-09-10 P4 变体A 之前存在 `force_lock=True` 短路"）
    是**审计资产**，不是残留。`"force_lock" not in src` 会把它误判成"没删干净"（本轮首版据此误报 FAIL）。
    正确判据：`ast.walk` 找 `Call.keywords` 里 `kw.arg == "force_lock"` + `FunctionDef.args` 里
    `a.arg == "force_lock"` → 远端命中 0 处。
54. **⚠️ 沙盒副本跑测试前必须先排掉本机 `.env`（否则假红，2026-09-11 实测）**：
    远端仓库**不含 `.env`（只有 `.env.example`）**；若沙盒副本是从用户本地目录 `cp -r` 来的，
    会带上 `TRADING_BROKER=simnow` → `test_step2_smoke_freq.py` 断言
    `engine.broker.name == 'dry_run'` 直接红，**37/37 掉成 36/37**，看起来像代码缺陷。
    处置：跑前 `rm -f <repo>/.env`（或先确认 `env | grep TRADING_` 为空），
    并在报告里把"环境干扰"与"真缺陷"分开说 —— 这类红**不是代码问题**，
    但如果不排掉，第 ① 层证据就不可信。
55. **用 `ClassVar[List]` 当"可见性记号"会污染后续所有实例（实测）**：
    `RiskConfig.dropped_legacy_keys: ClassVar[List[str]] = []` + 在 `model_validator` 里
    `cls.dropped_legacy_keys.append(k)` → ①列表跨实例无界累积；②告警条件是
    `if cls.dropped_legacy_keys:`，而它一旦非空就**永不清零** → 之后每次加载
    **干净配置也误报**"配置文件含已删除的配置项"（探针实测：构造干净 dict 仍打 WARNING；
    `clear()` 后同一代码不再告警 ⇒ 证明判据挂在共享变量上而非本次输入）。
    危害是**运维级**的：真出问题时这条告警已经喊了几百遍，失去信号价值。
    ⇒ 正确形状是让记号**随实例走**（`PrivateAttr` / 放进实例字段）或改成布尔标志。
    审核同类"仅为可见性"的附加字段时，先问一句：它是实例态还是类态？
56. **审核"某测试是否存在"不能只看 `Trading/Test/` 目录清单** ——
    文档 §7.3 注册的契约测试可能**从未实现**，而它的**断言内容早已被别的老测试覆盖**。
    本轮实例：§7.3 的 `p37`（FOK 全撤 / 部分成交）在目录里不存在，但
    ①全撤追价轮数与每轮重定价 → `test_p23_fok.py` [5][6][7]；
    ②部分成交不得判成交 → `test_p6_fix.py` 场景 C「1/2 手 → 判未成交」；
    ③FOK 全撤终态（FINISHED + `volume_left=0` + `trade_records` 空）→ 同文件场景 A「ghost」。
    ⇒ 结论要分两层报：**"覆盖"已达成**，**"按 D12 立独立契约测试"未达成**。
    反之 `p38`（CLOSE 前校验 `pos_long_his>0`）真的没落地：
    `_wait_position_ok` 只校验 `today+his >= volume`，`_verify_position_delta` 也只测**总量**增量
    → 昨/今仓拆分**毫无校验**；引擎侧只由 `p35` 的 `close_target_is_today` 闸门兜（≠ 同一件事）。
    危害链条要写全：引擎认为"跨日"但柜台无昨仓 → CLOSE 被 CTP 拒 →
    `_note_close_rejected` **不按拒单类别区分**，累积到 `close_max_streak` 就清掉该仓
    → 真仓还在、簿里没了 = 账实不符（正是当初 P0 要修的形态）。
57. **文档内部自相矛盾要单独列一条**（用户会把"文档准不准"也纳入验收）：
    本轮实例（同一份文档、同一节）：§6.1 进度表 `Phase 7 测试改造 | ⬜ 未开始`，
    而紧随其后的引用块写 `✅ Phase 7 已落地 … 37/37 通过`；§7.3 的 `p33` 条目写
    "断言第 6 种情况会写 `state_machine_violation`"，而该事件**全仓不存在**
    （`test_p33` 自己就在 [1d] 断言它不存在，并注明"文档 §7.3 需修正"）。
    处理口径：**代码为准**，把文档条目标记为"需修正/待同步"，不要反过来去补一个不存在的事件。

58. **反向扫描"本地有、远端无"必须全盘 `os.walk` 本地仓库**，不能只遍历远端 tree 的路径列表 ——
    后者永远发现不了"本地新建但从未推送"的文件。本轮据此捞出漏提交的 `test_p27_account_state_ssot.py`
    （P1 契约测试 25 项；源码改动全在远端、该测试在远端代码上 25/25 全绿 ⇒ 纯漏提交、无功能缺失）。
    报告漏项时要把"功能缺失"与"护栏缺失"分开说：本次风险是后者（防回潮断言在远端失效）。

59. **为「尚未存在」的功能写护栏测试：正确形状是「若存在则约束 + 全仓字面量扫描」，不是空断言**
    （2026-09-13 实践）。场景：用户的决策已拍板（"开关只留 strict/off，删掉宽容档 prefer"），
    但开关本体要到下一个 Phase 才被创建 → 仓库里**没有可删的对象**。
    这时**不要说"没得改"就收工**，也不要去把开关提前建出来（那是下一 Phase 的范围），
    而是把决策落成**代码级护栏**：
    ```python
    # [1] 若出现该开关（字段名正则匹配）→ 档位集合必须 ⊆ {strict, off}、默认必须是 strict
    #     （用 typing.get_origin(ann) is typing.Literal → get_args；Enum → 各 .value）
    #     当前未建档 → 只打印提示并通过；建档那天自动生效
    # [2] 全仓源码（.py/.js）不得出现宽容档【字面量】"prefer"/"quote_first"/...
    # [3] 既有安全闸门（如 confirm_live_trading）仍在且默认值未被削弱
    ```
    两个必须注意的点：
    - **[2] 必须用「带引号的完整字面量」匹配**（`["'](prefer|...)["']`）。裸词 `prefer` 会命中
      **别的开关** `prefer_lock_over_close_today`（平今成本开关，与取值策略无关）→ 误报"没改干净"。
    - **[2] 必须跳过测试文件自身**（它要写这些字面量当数据），并只扫 `.py`/`.js`
      （`.css`/`.html` 会引入无关误报）。把这条边界写进 docstring，同时也要报给用户——
      "将来某个测试把该词写进术语黑名单会误报，届时加豁免清单"。
    - 交付时**明确说清第 [1] 项现在是"待生效"而非"已验证通过"**（别让假绿蒙过用户）。

60. **判"这个决定属于哪个 Phase"要分「决策层 / 编码层」两问，答案常常不同**
    （2026-09-13 教训）。用户问"删掉 prefer 这个属于 Phase 8 吗？如果不属于就先改这个"。
    我上一轮笼统答"不属于 Phase 8（是规格层）"→ 用户据此认为"那就该现在改代码"，
    结果在仓库里找不到对象，白绕一圈。
    ⇒ 正确答法：**决策层（留哪几档）不属于该 Phase（它只是规格，已写进计划文档）；
    但编码层（那个开关本体）属于该 Phase —— 因为开关是那时才被创建的。**
    两问分开答，然后据此说明"现在能做什么、不能做什么"。用户会追问"到底属于不属于"，
    不要给一个笼统的"属于/不属于"。

### 7.1 通用交付坑（合并自 `minimal-patch-delivery`）

61. **`grep -E "(?<!...)"` 查禁用词 = 假绿**：GNU grep -E 不支持反向断言 → 报错非零退出 →
    被 `|| echo 未命中` 吞掉 → 打印"干净"。用 `grep -P` 或按护栏规则用 Python 复刻正则。
62. **护栏词表可能有盲区**：某类旧名称（废弃功能名）可能整个不在词表里，于是长期存活。
63. **删表/改数据前，必须先搜 `Test/` 里被冻结的旧事实**（真实翻车）。
    仓库常把"行数 / 内容哈希 / 枚举值计数"**写死进测试或快照文件**，例如
    `len(tdxhy2)==125`、`sha256` 存进 `Test/snapshots/*.json`。删掉内嵌表后这些测试**必红，
    且与改动正确与否无关**，表现为"用户跑测试直接挂"——最伤交付信任。
    → 动手前跑：`grep -rnE 'sha256|hashlib|==\s*(125|315|470)\b' Test/`，
    把"冻结旧事实"的断言重写成**契约式断言**（校验"从哪读 / 最少多少行 / 禁止旧实现回潮"）。
    → 并且**回归时要 A/B 各跑一遍全套测试**：只在打补丁的树上跑，看不出"是我改红的还是本来就红"。
64. **A/B 回归的判据是「共有组件的 ok 标志全等」，不是「通过数相同」**。
    两个数字可能恰好相等而失败项不同（完全不同的两件事）；反过来，**组件总数会因新增测试而变**
    （实测 46 → 47），此时"通过数 44 vs 45"看着不一致，实际是**零回归**。做法：
    把两棵树的 `report.json` 读成 `{组件名: ok}` 字典，`diff` 键集合 →
    得到「共有 / 仅新增 / 仅缺失」，再断言**共有部分标志全等**。对残余失败要**定性到根因**，
    且**不要在本任务的包里顺手修无关的既有失败**（会污染"波及面可数"）。
65. **大删改之后，验收标准是"功能零变化"而非"测试绿"**：先证明新实现的**行为摘要与旧实现逐条一致**
    （行数 / 分层计数 / 内容 digest），再谈测试。否则测试全绿也可能只是断言被改宽了。
66. **名称/字段"看起来像残留"≠"真的没用"**：判断某标识是否死代码，必须全仓 grep 用法与调用方，
    别信注释、别信文档、别信记忆。真正死的往往是**同名形参**而不是字段本体。
67. **运行时产物混进包**：`state.db`、`State/`、`__pycache__/`、测试生成的临时目录、`*.jsonl`
    → 打包前清理（排除规则见 §5.1 的陷阱）。
68. **Windows 路径坑**：Git Bash 会把 `--out /tmp/x` 交给原生程序时转成 `C:\tmp\x`。
    程序"没输出"时先 `ls /c/tmp/` 找产物，别以为没跑。
69. **自检项要选"会变红"的**：验收脚本本身要先反向验证一次（故意放一个违规/坏数据 → 应当失败），
    否则你不知道它是真的在检查还是一直绿。
70. **大块文本替换脚本（`rep(t, start, end, new)` 这类）三个坑**：
    + `new` 的**尾部换行必须显式写出来**。写 `"...）"` 会把它与 `end` 之后残留的下一行
      粘成同一行（`from x import yfrom z import w`），语法错但报错位置很误导。
      表格行的替换同理：`new` 末尾要带 `\n\n`，否则表格最后一行会和后面的段落/引用块粘连。
    + **`end` 锚点别把下一个标题一起吃进去**：若写成 `end="\n---\n\n## 7. 测试策略"` 而
      `new` 里又带了一遍 `## 7. 测试策略`，结果会拼出**两个同名标题**。凡是"替换一整节"的脚本，
      改完必须数一遍小节标题出现次数（期望恒为 1）。
    + **脚本要先能一键回滚**：动手前 `cp` 一份 `.bak`，跑挂了就"还原 → 改脚本 → 重跑"，
      别在已经改脏的文件上做二次替换（会命中不到锚点，或命中到刚写进去的新文本）。
71. **打包脚本会把编辑残留打进包**：文档升级脚本留下的 `*.pre_vN.bak` / `*.bak`
    会被朴素的 `z.write()`（或 `os.listdir` 全量收集）一起打进去 → 打包前显式过滤
    `f.endswith(".bak")` / `".pre_v" in f`，打完用 `zipfile.namelist()` 回读确认条目清单。
72. **中文文件名必须显式设置 zip 的 UTF-8 标志位**，否则 Windows 与部分 `unzip` 会显示成
    `README_ÕÅÿµø¦Þ»¦µÿÄ.md` 这类乱码（解压出来还是烂名）。用 `zipfile` 逐条写入时：
    ```python
    zi = zipfile.ZipInfo(rel, date_time=(2026, 9, 13, 7, 40, 0))
    zi.external_attr = 0o644 << 16          # 权限位，避免解压后属性怪异
    if any(ord(c) > 127 for c in rel):
        zi.flag_bits |= 0x800               # bit 11 = UTF-8 文件名
    ```
    打包后**回读验证**：`zipfile.ZipFile(z).namelist()` 里的中文条目应可正常打印，
    且解压后 `os.path.exists()` 为真。**不要**图省事把中文名改成拼音/英文 ——
    用户看的是文件名，乱码的文件名比英文名更糟。顺带：固定 `date_time` 可让同内容的
    两次打包字节一致，便于比对。
73. **多轮交付：开工前必须先判定"用户是否已合并上一批"** —— 判错则基线选错，diff 与回退点全废。
    2026-09-14 实测可用的三条判据（当时差点以旧原版为基线做第二轮 diff）：
    + **mtime == 上一批打包时写死的 `date_time`**（实测该批文件 mtime 全为 `09-14 12:00:00`，
      正是 `zipfile.ZipInfo(date_time=(2026,9,14,12,0,0))` 的值）→ **铁证是解压产物**；
    + **"定义形态"grep**（如 `^\s*max_order_volume\s*:` 命中 0、`def get_tradable_futures_aliases` 命中 1）；
    + **文档行数/字节数**与上一批交付的全文一致（如 1919 行 / 215719 字节）。
    ⚠️ **别用"关键字是否存在"当判据**：删除处留的**说明性注释**里仍含该关键字（如 `max_order_volume`），
    会误判成"未合并"。必须用**定义形态**正则。
    判定后：新一批的 **baseline 改为"用户项目当前状态"**（不是更早的原版），且
    **baseline 文件名换一个**（如 `Docs_before_round2.md`），避免覆盖用户已有的回退点；
    对照复验也跟着换成"用户当前 → 解压新包 → 断言全绿"。

### 7.2 本轮新增（2026-09-13）

74. **⚠️ 契约测试里"故障注入集合的范围"必须 ≥ "驱动循环的轮数"，否则多余的那次调用会走成功路径**
    （本轮最费轮次的一个坑）。实测：`RejectBroker(reject_calls=range(1, streak+1))`
    只拒前 20 次，而驱动循环跑了 `streak+2` 轮 → **第 21 次 submit 不在拒单集里，
    被 `DryRunBroker` 当成成交** → 走 `_book_close` 把仓移出簿面 → 表现为
    **"没写 `position_drop` 事件，但簿却空了"**，看着像"新逻辑有 bug"，实则是测试自身的洞。
    ⇒ 规矩：① 故障注入集合放宽到 `range(1, n+5)`；② **驱动轮数精确等于预期轮数**
    （多一轮就会踩这个坑）；③ 断言"簿面/事件/计数"三者**互相印证**，不一致时先怀疑测试。
    同源规矩：**mock broker 的拒单必须带上业务标签**（本项目 = `reject_class`），
    否则新加的类别门槛会让旧断言整体走另一条分支而全崩。
75. **交付文档前跑一遍「引用完整性」扫描**。本轮实测：正文引用 `§12 / §12.1 / §12.2`，
    而文档**根本没有 §12**（章节止于 §11 附录）—— 是上一轮改动留下的悬空引用，
    用户读的时候会直接踩空。做法：把所有 `§x.y` 引用抓出来，与"实际存在的标题编号"求差集。
    同类：正文写死"37/37"而实测表写"41/41"（自相矛盾）→ 口径类数字**只留一处权威来源**，
    其余位置改成"以 §X 表为准"，不写死。

76. **核对「状态机图 / 流程图」类文档，必须核对【转移的落点状态】，不能只核对转移号与触发条件。**
    实例（2026-09-13）：`Docs/自动下单三态迁移与CTP报文对应关系.html` 把 ⑤（跨日仓离场）
    画成"运行态 → 空仓态"，而实际 ⑤ 的落点是 `_book_close` 之后**由簿面重新派生**的：
    簿平光 → 空仓态；**残留反向仓 → 锁仓态**（实测簿面「多、空、多」即可复现）；
    **残留同向仓 → 仍为运行态**（第三种，见坑点 78 —— 别只验两格就下"二元"结论）。
    要害是：`test_p33` 这类契约测试钉的是转移号语义（intent / is_exit / target / volume），
    **落点状态它抓不到** ⇒ 图与代码不一致时全套测试仍然全绿，只能靠实测发现。
    ⇒ 查法：对图上每条边问三句 ——
    ①触发条件（读决策函数）②执行体（读 `_execute` / `_book_*`）
    ③执行后**状态怎么重算**（读 `account_state()` 的判据，看它依赖哪些字段）。
    落点若由**派生量**决定（净敞口 / 簿是否空 / 最新一笔的建仓日），就必然存在**多落点**，
    图必须画成分支而不是单箭头。
    附：**别把"一个触发器"当"一条边"**。本项目离场有两个调用方（`_settle_positions` 与
    关闭收尾），共用同一个 `_decide_exit`，图上通常只画了前一个。
77. **⚠️ 别用 PowerShell 重定向捕获 Python 的中文输出 —— 它会双重编码，既乱码又是有损的。**
    实例（2026-09-13）：`& $py repro.py > out.txt 2>&1` 在 PS 5.1 下这样走：
    python 吐 **UTF-8 字节** → PS 按**本机 OEM 代码页（GBK）**解码成字符串 → 再按
    `>` 的默认编码（UTF-16LE）存盘。结果：`纯函数矩阵` 变成 `绾嚱鏁扮煩闃`，
    且**字符数变了**（3283 vs 正确的 3044）⇒ **有损**，不是"显示问题"，内容真的坏了。
    更坑的是：拿 `Get-Content` 去读这两份文件做比对时，一个带 UTF-8 BOM、一个带 UTF-16LE BOM，
    PS 按各自 BOM 解码 → **乱码那份被尝出了"内容相同"**，一度得出错误结论。
    ⇒ 正确做法（本机已验证）：
    ```python
    # 让 Python 自己写文件，全程不过 shell
    import io, runpy, sys
    buf = io.StringIO(); old = sys.stdout; sys.stdout = buf
    try: runpy.run_path(target, run_name="__main__")
    finally: sys.stdout = old
    open(out, "w", encoding="utf-8", newline="\n").write(buf.getvalue())
    # 见 sandbox/evidence/run_capture.py
    ```
    比对同理：**用 Python 显式 `open(p,'rb')` → 剥 BOM → `decode('utf-8')` → 比对**，
    不要经过 `Get-Content` / `Select-String`（后者在本机会按 GBK 读 UTF-8 文件，中文正则**永远匹配不到**）。
    一句话：**只要产出含中文，就绕开 PowerShell 做管道与比对。**
78. **核"派生状态"的落点时，要穷举 ValueDomain，别只验两条路径就下"二元"结论。**
    实例（2026-09-13，同一天第二次更正）：`account_state()` 是**派生量**
    （`net != 0 → RUNNING`；`net == 0 and is_empty() → FLAT`；否则 `LOCKED`），
    所以 ④⑤ 这类"开/平一笔"之后的落点，取决于**平仓后的簿面**。
    上一轮只验了"多2"与"多2+空2+多2"两格，就此写成"⑤ 落点只有空仓态/锁仓态两种"，
    这一轮补第 3 格「多2 + 多4」实测出：**CLOSE/CLOSE x2 之后 net = 4 ≠ 0 → 仍停在运行态**。
    ⇒ 判据口诀：**凡是 `_decide_*` 只返回"做什么"，落点另由 `account_state()` 派生时，
    必须按 (净敞口符号, 簿是否空, 是否残留同类仓) 把格子铺满再下结论。**
    同时要问"这一格可达吗"：第 3 格需要簿内**各笔手数不一致**（同一会话 `lots_per_signal`
    恒定，且 ④ 开仓手数 = `abs(net)` ⇒ 同向笔数−反向笔数 恒 ∈{0,1} ⇒ 同会话必然清零）；
    只有**跨会话调大 `risk.max_volume`** 后带旧仓重启才达得到。
    镜像情形（调小）被 `_pre_trade_check` 的 `act.volume == act.target.volume` 挡掉
    （`close_volume_below_target`）—— **可达性也要实测，不能只靠推理**。
79. **⚠️ 给 `_settle_positions` 铺簿（注入 Position）是驱动不起来的 —— 它要求先有 run。**
    实测（2026-09-13）：想在 C 段穷举 ⑤ 落点，于是 `on_bar` 换日 → 注入跨日腿 → 再喂砸穿 bar，
    结果**四个格子全是"(无报单)"**。原因是 `_settle_positions` 先 `run = self._run_view()`，
    而 run 锚（`_run_anchor`）只在"净敞口从 0 变非 0 的那次成交"里写 —— 铺簿绕过了记账 →
    `_run_view()` 返回 None → 直接 return。
    ⇒ 要"铺簿 + 真实执行"跑离场，改用 **`_force_exit(bar, reason=...)`**：
    它是"关闭自动下单"的生产路径，内部就是 `_decide_exit` + `_execute`，
    **不需要 run**，落点逻辑与正常离场完全一致（`Engine.py:1285-1307` 注释写明两调用方共用同一张表）。
80. **图/表类文档交付前必须做「版式自检」，用脚本量，不要靠眼看。**
    实例（2026-09-13）：状态机图要拆成"双向各两条边"，中文标签普遍 200–300px，
    而两条竖线之间的通道只有 260–310px ⇒ **单行标签必然压线**。
    手写坐标版自查出 3 处压线；改成双行标签后归零。
    ⇒ 做法：`evidence/check_svg_layout.py` —— **直接解析 SVG 文件**（不是手抄坐标，
    否则图改了脚本没改，校验就成了自我安慰），按字符类型估宽
    （全角/CJK 按 1.0em、其余 0.55em）算包围盒，检查
    ①出画布 ②压连线 ③压状态框 ④标签互压 ⑤正交折线交叉点。
    三个必踩点：
    · **`<defs>` 里的箭头 marker 也是 `<path>`**，不剥掉会被当成连线；
    · **状态框内部文字不是"压框"**，要单独走"是否溢出框"的检查；
    · **`viewBox` 是 `"min-x min-y width height"`**，前两个是原点不是宽高 ——
      第一版就错在这里，把 680×582 读成 0×0，整张图被判"全部出画布"。
    · 用一张**人造坏图**（`selftest_bad.svg`）自测一次，确认检查器不是"永远通过"。
81. **中文路径不要经 shell 传参**。实测（2026-09-13）：
    `& python check.py "C:\...\自动下单....html"` → 被调脚本收到乱码路径 →
    `FileNotFoundError`；`Copy-Item "中文路径" ...` 同样失败。
    ⇒ 方案：① 路径写进 **Python 源码字面量**（源文件是 UTF-8，安全）；
    ② 或先复制成 **ASCII 文件名**再处理。
    另注：`C:\my_chan_project`（用户项目目录）在本机**从 shell 侧不可访问**
    （`os.path.exists()` 返回 False），但 Read/Write 工具能读写 —— 拿它做只读快照要绕开 shell。

### 7.3 实施型任务（把拍板规格写成代码）新增（2026-09-13）

82. **fail-closed 语义的契约测试，主体必须是【反例】；正例只用来证明反例有判别力**
    （2026-09-13 Phase 8 实践，用户认可的写法）。原因：**行情一直正常时，"静默回退配置值"
    的实现与"fail-closed"的实现跑出来一模一样** —— 正例对它俩零判别力。
    所以这类测试的骨架是：
    ```
    [反例矩阵] 每一种"取不到正确值"的形态 × 每一项必须保持不变的断言
      ⇒ nan / 0 / 字段缺失 / 越界 / 区间反转 …… 每一种都要"拒 + 告警 + 零副作用"
      ⇒ 并且断言**配置值保持原样** —— 注意"不回退" ≠ "被写坏成 0/None"
    [正例对照] 同一入口、行情正常 ⇒ 必须放行 + 真的发出 1 笔
      ⇒ 这条不是"顺便测一下成功路径"，而是证明上面的反例**不是"恒拒"的假绿**
    ```
    实测产出：`p42` 158 项，其中 `[3]` 段 44 项全是反例；`[3l]~[3n]` 3 项是与反例**同一路径**的正例对照。
    另注：断言"零副作用"要选**可外部观测的**那个量 —— 这里是 `api.inserted == []`
    （一笔都没发到柜台），比"返回了 rejected"强得多。

83. **`nan` 是 truthy —— 判空必须用 `isfinite and > 0`，不能用 `if not v`**。
    `bool(float('nan')) is True` ⇒ `not nan` 是 `False` ⇒ `if not v: return` **漏过** nan，
    随后 `math.ceil(nan)` 抛 `ValueError`。传参校验统一形式：
    ```python
    def _finite_positive(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)): return False
        f = float(v)
        return math.isfinite(f) and f > 0
    ```
    三个必测的反例：`nan` / `0` / **属性缺失**（`getattr(q, "f", None) → None`）。
    ⚠️ "缺失"与"值为 0"必须能区分（前者报 `missing_field`，后者报 `not_positive`），
    否则线上分不清"行情字段名改了"还是"合约真的没报价"。契约测试里 `[0]` 段先单独立一条
    `assert bool(nan) is True` —— 它是后面所有判据的**理由**，写出来比注释有用。

84. **写改动到 Python 源码时，中文文案里的内层引号会截断字符串字面量**（本轮踩两次）。
    `check("[3d] 没被 nan 写坏、也没被"修好"")` → `SyntaxError`（两个字符串相邻）。
    同一坑还出现在文档表格的 `…写"42/42"…`、打包脚本的 `"若与上表"基线"列不一致"`。
    ⇒ 规矩：**中文文案里的引号一律用 `「」` / `『』`**，ASCII `"` 只留给语法。
    自检：改完立刻 `python -m py_compile <file>`（本轮两次都是编译期就抓到，代价 1 轮）。

85. **"成功路径恒发一条 info 通知"会污染"无告警"类断言 —— 必须按 code 精确断言**。
    本轮：`_ensure_instrument_spec` 成功时恒发 `info/instrument_spec_from_quote`，
    我按"反例：四项全一致 ⇒ 告警列表为空"写，得到 `got ['instrument_spec_from_quote']` **假红**。
    ⇒ 正确写法是断言 code 集合的**精确值**（`== ["instrument_spec_from_quote"]`），
    或断言"某个特定 code 不在集合里"。**"列表为空"这种断言只在你能保证零通知时才用。**
    （顺带：这条 info 本身是设计意图 —— 成功也要留痕，否则日志里只有失败，看起来像从没成功过。）

86. **对"按顺序覆盖多个字段"的接口，断言变更清单的【顺序】必须用独立对象**。
    本轮：`apply_quote` 返回"被覆盖的字段名列表"，顺序 = 映射表顺序 + 附加字段追加在末尾。
    我第一版复用了**已经被改写过的 spec** 去测这个顺序 → 第二次调用时值已相同，
    返回空列表 → 断言天然失败。⇒ 用**全新构造的对象**跑，并把"覆盖前的快照"也一起断言
    （`price_field_snapshot()`），这样"改了哪几个 + 改之前是什么"一次说清。

87. **清房复验要做【两步】，只跑测试会漏掉"解压漏了文件"**（2026-09-13 定型）。
    ```
    ① 逐文件 md5（CRLF 归一化）比对「解压产物」vs「工作副本」 —— 12/12 一致
    ② 在该干净树里跑全套测试 —— 42/42 全绿
    ```
    只做 ② 的话，如果打包时漏了一个文件、而恰好那个文件不影响测试结果，就静默通过了。
    同时拿 `pull/repo`（**拉取时留下的远端原样副本**）当①的基线拷贝来源与②的干净种子 ——
    不必另造基线树，也不会有"基线被我改过"的风险。
    配套：交付包里的 `baseline/` 要再做一次自校验 —— **它的 md5 必须等于用户本地原文件**
    （本轮 2/2 一致）。这就是用户要的「可比对物」，缺了会被问"你是不是没更新"。

88. **【改动清单】让脚本从基线扫出来，别凭记忆列**。
    写一个 `_diffscan.py`：`os.walk` 基线树与工作副本 → 按相对路径做 md5（CRLF 归一化）
    求差 → 分「修改 / 新增 / 缺失」三栏，并把**运行期产物**（`state.db` / `_verify_*` / `*.log`）
    过滤掉。三个好处：
    ① 交付包的文件清单由它生成，**不会漏也不会多**；
    ② 顺手证明"我没碰别的文件"（本轮 336 文件里恰好 11 改 1 增，其余 325 个字节相同）；
    ③ 过滤规则显式写出来，避免"因为产物混进来而多打一个文件"。
    ⚠️ 反向也要看一眼「缺失」栏 —— 本轮是 0，若不为 0 说明工作副本里少文件（多半是打包前清理误删）。

89. **文档内部版本号与磁盘文件名不一致时：标注，不要擅自改名**。
    本轮实测：磁盘文件名是 `…v1.2.md`，而正文自我声明是 **v1.0**（且正文明确写"不再有 v3.x 后续版本"）
    ⇒ 我这一轮**不能**续用 v3.14（与正文自相矛盾），改用 **v1.1**；同时在头部加一段
    「📌 文件名说明」写明"文件名是用户侧命名，正文版本号是权威"，并留一句"若希望内部号跟文件名走，
    改标题 + 附录 C 行标签即可"。
    ⇒ 通用规矩：**版本号只留一处权威来源（正文），别的地方改成"以 §X 为准"；
    发现不一致时先标注再问，不要静默改文件名的口气（用户可能有意为之）。**
    同理适用：正文写死"42/42"而实测表写"41/41"这类口径矛盾 → 与坑点 75 同一条纪律。

90. **核验脚本读【本轮新增的 API】必须先 `hasattr` 探测**（2026-09-14 踩到，很关键）。
    清房复验的核心证据是"**基线树上 FAILS 恰好等于改动落点**"这句话。但如果脚本直接
    `trader.check_symbol_allowed(...)`，在基线树上会先抛 `AttributeError` 把脚本**整只崩掉** ——
    你看到的是 traceback，不是 FAILS 计数，等于**丢掉了唯一的改动点证据**。
    ⇒ 写法固定：
    ```python
    if not hasattr(trader, "check_symbol_allowed"):
        check_true("[B1] check_symbol_allowed 存在", False, "MISSING_API（基线树预期）")
    else:
        ...normal assertions...
    ```
    方法（`hasattr(obj, "m")`）、漏斗函数（`getattr(orch, "funnel", None)`）、前端 token
    （`if _TOK in js: js.index(_TOK)` —— 直接 `.index()` 会 `ValueError`）三处都要护。
    **判据**：脚本在基线树与改动树上**都必须 exit 0 且只输出 FAILS=N**，不允许出现 traceback。
    这条同样适用于"删除型改动"的反向断言（如 `get_tradable_futures_aliases` 已被删 →
    基线树上"存在"是**预期**的，别写成 `hasattr(...) is False` 就完事，要能区分"预期存在"与"误删"）。

91. **用户会【推翻上轮已交付的决策】—— 撤销型改动要整条链路一起收干净**（2026-09-14 实例）。
    上轮按用户字面要求把搜索收敛到 8 品种（新增 `AppSSE.get_tradable_futures_aliases()` +
    `AppChart.search_stocks` 过滤）；本轮用户拍板解耦 → 该限制**整条撤销**。撤销时别只改回
    调用点，**要把失去调用方的新增函数一并删掉**，否则留下"看着像还在用"的死函数。
    配套三个动作（缺一个都会留下新的不一致）：
    ① **函数删**（`get_tradable_futures_aliases` 全仓 0 引用后删除）；
    ② **docstring 反向写纪律**（`get_futures_aliases()` 里明写"**不要再在搜索/解析侧做品种过滤**"，
       否则下一个人会把过滤再加回来）；
    ③ **核验脚本同步反转**（旧脚本断言"搜不到 RB/M/SC" → 本轮必须断言"**搜得到**"，
       否则交付包里躺着一个会误报的脚本）。本次同步反转了 `verify_entry.py` / `verify_p12.py` /
       `verify_doc.py` / `verify_doc2.py` 四个脚本的断言集。
    ⚠️ 推论：**"拒绝型断言"比"存在型断言"更容易过期** —— 凡是 `assert not found` 形式的检查，
    都应在脚本头部注释里写清"这条断言属于哪个决策；决策反转时必须改这里"。

---


### 7.4 需求实现层（2026-09-14 新增）

92. **缩范围要改【数据源】，不要在消费端加过滤 —— 否则一定留下绕过点**
    （2026-09-14 正反两次实测，本类需求的定型解法）。
    需求形如"界面只让看 / 只让操作 N 个东西"，有两种做法：

      ❌ **在消费端过滤**（在搜索函数 / 下拉候选里加白名单）→ 加了几处就有几条绕过路径。
        本项目实例：把搜索候选收敛到 8 个品种后，**输入框直敲回车 / 历史记录 /
        刷新会话恢复 / 直调 HTTP API** 四条路径照样能进（实测矩阵见交付报告）。
        当天即被用户撤销。

      ✅ **改数据源本身**（改那张 dict / 表）→ 所有入口自然一致，**结构上不存在绕过点**。
        本项目实例：把 `FUTURES_ALIASES` 由 83 条收窄为 16 条，四个入口零改动。

    ⇒ 判据：**如果"加过滤"这个动作需要在多处重复，就说明选错了层。**

    配套两条（缺一条就会长出新洞）：

      ① **同源的硬编码副本必须一起改** —— 本项目后端 `FUTURES_ALIASES`（Python dict）与
         前端 `FUTURES_ALIAS_KEYS`（JS Set）各一份；只改一边会出现
         "前端认作期货 → 后端解析不出代码"的空转路径。修法是**加契约测试逐键比对**
         （前后端两张表键集必须相等），而不是靠自觉。

      ② **"能看"与"能操作"是两个数字，别混** —— 收窄后本项目是"能看 16 / 能下单 8"；
         且**可操作集合必须是可看集合的子集**（否则出现"能下单却打不开界面"的死角）。
         本项目用一条断言钉死：`set(PRODUCT_PROFILES) - 可看集合 == []`。

93. **报告里的测试结论必须标注【哪个环境跑的】—— 开发树全绿 ≠ 用户机器全绿**
    （2026-09-14 实测，差点交出一份"说谎"的报告）。
    开发树是**刻意裁过的**（不含 `.env`、不含运行时数据），用户项目是**活的**。
    同一个 `runtests.py`：开发树 51/0，用户项目 50/1。若只报前者，
    用户合并后自己一跑发现红灯，会立刻怀疑"你是不是没验证过"。
    ⇒ 规矩：**测试结论一律写成两行（开发树 / 你的项目）**，差异逐条归因；
    归因不了的**不许写"全绿"**，改成"待归因"并附上排查入口。
    与铁律「不许说'看着对'」同源 —— **测量环境的差异本身也是必须报告的事实。**
    **第六批补充**：正确修法是**隔离配置来源**（环境变量优先级 > `.env`，
    故显式钉 `TRADING_BROKER=dry_run` 并把 4 处 `build_runtime` 包进
    `isolated_trading_env()` 上下文管理器，退出恢复），**不是删 `.env`、也不是放宽断言**；
    并且必须**补"隔离机制自证"断言**（环境变量能压过 `.env` / 隔离后 == 字段默认 /
    退出后环境恢复），否则以后 pydantic 写法一变，测试会**静默**退回随机器漂移。
    实测：带 `.env` 41/4 → 48/0，无 `.env` 45/0 → 48/0，单测 **197s → 1.26s**。

94. **清房基线要"逐文件核验版本"，且"改前"组的期望值必须写进脚本逐项核对**
    （2026-09-14 实测）。见 §5.1 末两条：基线里某个文件被提前同步过下一批改动 →
    "改前"也全绿 → **本批改动的存在性没被证明**。信号是"预期该红的没红"。
    配套：给基线里每个"本批会改的文件"加 `assert 本批特征标记 not in 基线文件`。

95. **解析汇总行 `N 通过 / M 失败` 时，`split("通过")[0]` 拿到的是 N 不是 M**
    （2026-09-14 踩到，逻辑直接判反）。本轮把 `23 通过 / 0 失败` 里的 **23** 当成"23 失败"，
    于是"本批改完"被判成不达标、脚本 `exit 1`。
    ⇒ 规矩：解析汇总行用**两个不同变量**分别接"通过/失败"并**两个都打印出来**，
    判据只看失败数。正确写法：
    `pf = int(seg.split("通过")[1].split("/")[1].strip().split()[0])`。
    同理适用于 `FAILS = n` 之外的任何自定义汇总格式 —— **自己写的脚本，
    自己先跑一遍基线确认它报的是"坏"，再跑改后确认它报的是"好"。**

96. **不要假设用户的 `C:\my_chan_project` 还停在"上一批之前"——用户会在两轮之间自行合并**
    （2026-09-14 实测）。本轮差点以"第五批未合并"为前提去设计基线；实测用户项目里
    `FUTURES_ALIAS_KEYS` 已是第五批的 **17 键收窄版** ⇒ **第五批已在用户项目里**，
    于是 `clean8` 的 `app.js` / `p47` 恰好就是"用户项目 + 第五批"的**合法基线**
    （用 `cmp` 逐字节验过 == 用户项目）。
    ⇒ 规矩：**动手前先 grep 上一批的特征标记**确认合并状态：
    在位 ⇒ 基线 = 用户项目本身；不在位 ⇒ 基线 = 用户项目 + 上一批包。
    凭记忆假设合并状态，会直接选错基线、diff 里混进上一批的改动（用户一合就重复）。

97. **⚠️「测试全绿」还有一个隐藏维度是【跑的时刻】—— 20:00 是硬分界线**
    （2026-09-14 实测，本类问题第一次被逮到）。
    现象：同一份**未改动**的基线树，**19:12 / 19:38 / 19:54 三次跑满 51/51**，
    **20:02 起跑的第 4 次变成 50/51**，红点恒为 `test_p24_close_offset.py` 的**同 5 条**断言。
    根因链（三段都要钉住才算查清）：
    ```
    ① Types.py:27  NIGHT_SESSION_START_HOUR = 20
       ⇒ trading_day_of_ms(now_ms()) 在 >=20:00 时返回【次日】
    ② test_p24:90-91  _TODAY = datetime.now(CN).date().isoformat()   ← 用的是【自然日】
    ③ Engine._current_trading_day()  无 bar 时走墙钟 ⇒ 拿到【交易日】
       ⇒ entry_date(=自然日) < today(=交易日) ⇒ 判成昨仓 ⇒ 期望 ④ 反向 OPEN，实得 ⑤ CLOSE
       ⇒ 报错形态：got 5, want 4 / got CLOSE, want OPEN / got LONG, want SHORT（共 5 条）
    ```
    **为什么生产不受影响**：生产路径的 `entry_date` 由引擎自己写
    `_current_trading_day(bar)`（`Engine.py:1547`）⇒ **恒为交易日**，
    只有**测试夹具手写自然日**才会分叉。⇒ 这是**夹具缺陷**，不是引擎缺陷
    （与坑点 11 / 23 同族：凡"从墙钟派生日期"的夹具，都要按**调用方口径**校准）。
    **归因动作（必须做，否则会被误判成"本批引入了回归"）**：
    在**未改动的基线树**上单跑该测试 —— 若报**完全相同的断言与计数**，即可判定"非本批所致"。
    本轮实测：改前树 / 改后树逐条相同（同 5 条），根因脚本在两棵树上输出完全一致 ⇒ 归因闭合。
    **不要**把这类红点含糊写成"疑似 flaky" —— flaky 与"时刻依赖"的处置完全不同。
    **处置纪律**：
    - **不在本批顺手修**（属测试夹具修正，与本次交付主题无关；修了会把"波及面可数"搞脏）→
      写进交付说明的**遗留项**（附精确位置 + 一行修法）交用户拍板；
    - 修法本身只有一行：`_TODAY = trading_day_of_ms(now_ms())`、`_YESTERDAY` 同理；
    - 报告数字一律**矩阵化**：`{20:00 前 / 20:00 后}`（与坑点 93 的 `{无 .env / .env}` 并列），
      并明写"哪格属既有缺陷、哪格属本批"。
    **顺带自查（拿到任何交易类测试套件都先做）**：
    `grep -n "datetime.now\|date.today\|now()" Trading/Test/*.py`
    —— 每个命中点都要问"这个日期锚是**自然日**还是**交易日**？调用方按哪个口径比较？"
    两者混用 = 定时炸弹（每天 20:00 后引爆，只有晚上跑 CI 的人才会看到）。

98. **⚠️ 手工拼接生成的 `changes.diff` 可能是【永远套不上】的坏补丁 —— 必须用 git 原生生成 + 干跑**
    （2026-09-14 实测，本类问题差点把坏补丁交出去）。
    事故形态：用 `difflib.unified_diff` 逐文件生成后**直接拼接**。本仓库有 **24 个文件末行没有换行符**
    （历史风格），其中 3 个在本批改动清单里 ⇒ 拼接处**上一个文件的末行与下一个文件的
    `--- a/...` 头粘成同一行**：
    ```
            }--- a/Trading/Engine/Reconcile.py      ← 真的长这样，肉眼极易放过
    ```
    症状：`git apply --check` → `error: patch fragment without header ... :742`（rc=128），
    **整份补丁作废**（不是少一个文件，是全份不可用）。
    **自校验不变量（选它，一眼就能看出）**：
    ```
    --- a/ 段数 == +++ b/ 段数 == diff --git 段数 == 改动文件数
    ```
    本次实测正是 `19 vs 20` 暴露了那个被吞掉的文件头（其它统计全都"看起来对"）。
    **正解写法**（git 原生，自动带 `\ No newline at end of file` 与 `index` 段）：
    ```python
    # ① copytree(基线) -> tmp/repo（用 ignore 排 .git/运行产物）
    # ② git init -q; git add -A; git -c user.email=.. -c user.name=.. commit -qm baseline --no-gpg-sign
    # ③ 用基线 vs 工作副本逐文件 sha256 求差 -> 覆盖 tmp/repo 里的改动文件
    # ④ git -c core.autocrlf=false diff --no-color --src-prefix=a/ --dst-prefix=b/  → 写盘
    ```
    **交付前必做（零成本，没理由跳过）**：
    ```
    git apply --check <diff>          # 干跑：rc 必须 0
    git apply <diff>                  # 真套
    # 套完与工作副本逐文件 sha256（CRLF 归一化）比对 → 必须 N/N 一致
    # 再跑 2-3 条关键契约测试 → 证明"套完不只是文本像，行为也对"
    ```
    本次据此拿到：`--check` rc=0 / apply rc=0 / **20/20 逐字节一致** / p42·p50·period_profile = 71·110·44 全绿
    ⇒ **patch 通道与 zip 通道等价**（两个通道都给用户，才算"覆盖即用 + 逐文件评审"都成立）。
    ⚠️ 附带效应：换生成方式会让**行数统计变几个**（git 对"无末换行"末行的处理与 difflib 不同；
    本次 `+963/−517` → `+957/−511`）。⇒ **行数口径以最终交付的那份 diff 为唯一权威**，
    改生成方式后**必须重算并同步文档里的每一处**，别让文档和新 diff 打架。

99. **⚠️ git 的"非 ASCII 路径 / 换行"两个配置陷阱 —— 它们让【验证步骤】假失败，不是真出错**
    （2026-09-14 新增批次实测，两处各踩一次。核心纪律：**"验证脚本报红"要先分清
    【被测对象错了】还是【验证工具有偏】** —— 凡是"全量、整齐、一致"的失败
    （N/N 全不同、全部 0 命中），先怀疑工具。）

    **(a) 路径含中文 → `git diff` 把路径做 C 转义，第 98 条的结构自检会假失败。**
    本批含 `Docs/…中文名.md`，`git diff` 默认输出
    `diff --git "a/Docs/\346\226\207..."`，于是 `l.startswith("--- a/")` 计数 **0**，
    "`--- a/` == `+++ b/` == `diff --git` == 改动文件数" 报**结构异常** ——
    真实原因是**路径被转义**，补丁本身完全正常。
    ```python
    git(["-c", "core.quotepath=false", "diff", ...])   # ← 有非 ASCII 路径就必须加
    ```
    判据：报"结构不一致"时，**先把 `diff --git` 那几行原样打出来**；
    若见引号 + `\nnn` 八进制转义，就是本条，别去改补丁。

    **(b) 用 `git apply` 做"字节级"复验 → 临时仓库必须关 `core.autocrlf`。**
    Windows 默认 `core.autocrlf=true`：`git apply` 把 LF 落成 CRLF，结果
    **`--check` rc=0、`apply` rc=0、但"套用后 vs 工作副本逐字节" 23/23 全报"内容不同"**
    （看起来像补丁彻底坏了）。修法：
    ```python
    git(["init", "-q"], tree)
    git(["config", "core.autocrlf", "false"], tree)   # ← 关键
    git(["config", "core.eol", "lf"], tree)
    git(["config", "core.safecrlf", "false"], tree)
    ```
    并给该检查加**"差异自解释"**（打印首个差异字节 offset + 两侧片段 + CRLF 计数），
    本次正是靠它一眼确认"是 CRLF、不是内容"：
    ```
    [诊断] Base.py：交付 4812 字节 / 套用后 4846 字节；首个差异字节 offset=63
           交付 : b'...\n    """\n'        套用 : b'...\r\n'
           CRLF 计数：交付=0  套用后=34
    ```

100. **交付"累积叠加包"时，逐文件标注【属于哪一批】，并在说明里写明与上一批的关系**（2026-09-14 实测）。
    场景：上一批补丁包已交付、但**用户可能还没合并**，本批又在其之上改了文件。两种打法：
    - **delta 包**（只装本批改的）：体积小，但用户若未合并上一批 → **拿到手跑不起来**；
    - **累积 overlay**（相对**原始基线**装全部改动文件）：用户无论合并与否都正确
      （已合并时内容相同，重复覆盖无害）。
    本次取累积 overlay，并在清单里逐文件打标（`Phase 3（Fix B）` / `Phase 4 · p24 日期锚` /
    `Phase 4 · P4.1 README` / `Phase 4 · P4.3 交接文档`），说明里明写
    "**本包相对 `<base-sha>` 累积**，含上一批 N 个文件" ⇒ 用户既能整包覆盖、也能只挑本批 3 个。
    ⚠️ 配套两条：
    - **代码与文档分两份 diff**（`changes_code.diff` / `changes_docs.diff`），
      否则"整包回退代码"会把文档一起回退掉，与用户"每项独立可还原"的要求打架；
    - **目录层级打包时，zip 内只放源文件**（`Trading/…`、`Docs/…`），说明/证据放 zip 外 ——
      解压到仓库根即覆盖，不会往仓库里塞垃圾文件。

101. **最小化修一个测试时，记得清掉它留下的【死代码】**（2026-09-14 实测，一行）。
    把 `test_p24` 的日期锚从自然日改成交易日锚后，原来只为自然日服务的
    `_CN = _dt.timezone(_dt.timedelta(hours=8))` 变成**零引用**的死变量。
    留着它有三个坏处：① 看 diff 像"改了一半"；② 它是**同类 bug 的诱因**
    （下一个人要写自然日锚时，手边正好有个现成时区常量）；③ 交付说明里得额外解释"这行为什么还在"。
    判据：**删掉某个赋值的唯一消费点后，回头 grep 一次该名字**（本次 grep 只剩定义行 ⇒ 删）。

102. **⚠️「回算一致」证明不了规则正确 —— 先算【判别样本数】，再枚举分歧区间**（2026-09-15 实测，用户当场推翻了上一轮结论）。
    场景：要定某派生规则的**阈值常数**（"平今 vs 锁仓"该比到哪一步）。8 个品种回算，
    `1×` / `2×` / `3×` 三套阈值里有**两套**都能凑出「8/8 与现值一致」。
    **⇒ 一致率对这个选择零判别力。**
    根因：8 个样本里只有 **1 个**（CU，`平今/开仓 = 2×`）靠近分界，其余全在"远侧"
    （IF 系列被交易所闸门**短路**、AU 是 `0×`、AG 是 `1×`）。**样本量 8 ≠ 判别力 8。**
    做法（四条，缺一条就会重蹈覆辙）：
    ```
    ① 对每个候选规则，显式枚举【它们结论不同的区间】，再逐样本代入，
       标出"该样本能否区分这两个候选" → 得到判别样本集合
    ② 报"一致率"时必须【同时报判别样本数】：「一致 8/8，其中判别样本 1/8」
    ③ 判别样本数 ≤ 1 时【不许用一致率下结论】，改去问"比较截止到哪个终态"
       这类【定义问题】—— 本例真答案是"阈值随终态变"，不是某个算出来的数
    ④ 独立复核现任值的【理由】而不只是数值：本例仓库现规则数值上等于 1× 口径，
       但它的参考量是"平昨费"—— 一个在离场决策路径里【根本不出现】的量。
       属"数值对上、理由错位"：换成 3× 口径它立刻全线判错。
    ```
    **配套（同源教训）**：这类"路径成本"问题**必须实测笔数，不能推理**。
    本轮先按推理写了 `2 × 开仓费`，实测（`DryRunBroker` + 同一触发剧本只换品种）笔数是
    **2 / 3 / 4**（到净敞口 0 / 到下一个持仓 / 到簿空），**没有一个是 2 笔的中间态** ——
    推错的根源是把"反向开仓"和"平掉原仓"当成同一条路径的两笔（后者在净敞口 0 之后
    **根本不会自动发生**）。⇒ 凡是"某条路径要花几笔/几步"的问题，写个对照实验数出来。

103. **⚠️ 用 `replace_between(start, end, new)` 做整节替换时，`end` 锚点必须被【吃掉】**（2026-09-15 踩到，见坑点 70 同族）。
    事故：实现写成 `src[:i] + new_block + src[j:]`（`j = find(end)`），**漏了 `j += len(end)`**，
    而 `new_block` 末尾又照惯例重写了一遍下一个标题 → 产出
    `### 6.6 xxx### 6.6 xxx`（两个标题粘连，肉眼扫一遍很容易放过）。
    ⇒ 两条纪律：
    - `j += len(end)` 写进函数体，并在注释里点明为什么（否则下个人重构时又漏）；
    - **改完立刻脚本化断言"重复标题 = 0 + 相邻重复行 = 0"**，别靠人工数
      （坑点 70 已要求"数一遍小节标题出现次数"，本轮就是没做成断言才漏过去的）：
      ```python
      h = [l for l in L if l.startswith("#")]
      assert [k for k, v in collections.Counter(h).items() if v > 1] == []
      assert [l for i, l in enumerate(L) if i and l == L[i-1] and len(l.strip()) > 8] == []
      ```

104. **⚠️ 读注释得出的"结构性结论"，必须回问一句「这段话的【语境】是什么」**（2026-09-15 实测，用户当场指出）。
    事故：`Engine.py:1652-1658` / `:1682-1690` 写着「引擎**无自动清仓路径**，需人工平仓或
    重新开启自动下单后由对向信号拆锁」。我把这句当成通用结论 → 得出
    "锁仓态回不了空仓、要取 X× 口径必须先给引擎补新功能" → 整个方案被带偏。
    **真相**：那段话住在 `shutdown_and_lock_all()` 里，语境是**关闭自动下单之后**
    （`on_signal` 顶部直接 `return`，等不到信号，所以没有自动出口）。
    **自动下单开启时**，锁仓态等得到信号 → 转移 ③ 拆锁 → 运行态 → L1-L3 → 转移 ⑤ → 空仓，
    全程走现有转移表，**一行新功能都不用加**。
    ⇒ 纪律：
    - 引用注释前先看它**住在哪个函数 / 哪条分支**里；函数名带条件（`shutdown_*` / `only_when_*`）就是强信号；
    - **"某条路径不存在"这类断言，必须用真实引擎实跑一次才算数**（本轮实跑：
      `on_bar`+`on_signal` 两个公开入口，不手工改簿 → 实测锁仓路径 4 笔自动回到空仓）；
    - 重读一个状态机时，**逐转移实跑一遍**（①~⑤ 各构造一次触发，打印 `intent/offset/is_exit/transition`
      + 每步 `account_state()` + 簿内容），比读十遍注释可靠 —— 本轮就是这么把 ①~⑤ 全覆盖掉的。

105. **重读状态机要用【公开入口】驱动，不要手写簿**（2026-09-15 实测）。
    反面做法：上一轮的对照实验用 `eng.positions.add(make_pos(...))` 直接塞仓单
    → 观察到的"锁仓态无动作""需要人工补路径"里，有一半是**自己塞出来的假象**
    （塞进去的仓单没有 run、没有正确的 `entry_bar_seq`）。
    正确做法：`on_bar(bar)` → `on_signal(sig)` 按剧本推进，只读
    `eng.broker.orders`（含 `meta.intent/offset/is_exit/transition`）与 `eng.account_state()`。
    照抄仓库现有夹具最省事：`Trading/Test/test_p51_closetoday_switch.py` 的
    `make_bar` / `make_sig` / `make_cfg` / `build_engine` 四个函数可直接借。

106. **核对"外部约束"类结论时，先分清是哪一层定的**（2026-09-15 实测，用户质疑触发）。
    三类主体常被混为一谈，结论的真假边界完全不同：
      · **交易所规则** —— "CZCE 限价指令无 FOK 属性"、"只有 SHFE/INE 有平今指令"；
      · **SDK / 通道实现** —— tqsdk「限价+FOK 仅拒郑商所期货」是 **SDK 的前置校验**，不等于交易所规则；
      · **本地代码的选择** —— `effective_order_advanced()` 对 CZCE **无条件**强制 FAK，
        这已是"我们的策略"，不是"交易所事实"（属**过度约束**：把"不能用 FOK"写成"必须用 FAK"）。
    两个高频误读：
    ① **把"不支持 X"读成"只能 Y"**：「CZCE 只吃 FAK」→ 实际是"CZCE 无 FOK 属性，
       普通限价单（GFD）与 FAK 都能用"。交易所极少"只支持一种指令"。
    ② **把"不能选 X"读成"X 不会发生"**：「其余四家无平今指令」→ 实际是"不能**主动选**平今"，
       但中金所按成交时序「先平当日新开仓」**自动认定**平今 → 当天开当天平照样收平今费。
       ⇒ 能力闸门的精确语义永远是「能否**主动选择**」，不是「会不会发生」。
    取证姿势（按硬度排序）：**交易所官网 / 官方文档 > SDK 官方文档 > 券商科普**。
    本仓最硬的一手证据是 tqsdk `insert_order.offset` 的字段说明（我们实际走这条通道）；
    中金所官网另有专门的"平今仓数量是如何计算的"答疑。**至少两源交叉**，
    并在报告里**标明每条结论的来源层级**（否则下一个人分不清哪句是规则、哪句是我们的选择）。
    附带发现：闸门对"平今免收"的品种（TA）是**真成本**，对"平今贵"的品种（IF）**零成本** ——
    评估一个新品种时要算这笔账。

107. **⚠️「被预校验挡下」≠「安全」—— 必须接着追三问：账户停在哪 / 能不能自愈 / 用户看不看得见**
    （2026-09-16 实测，核两份状态机文档时在**拒绝分支**上挖出静默卡死）。

    判据链只有一条，但必须走完：
    ```
    ① 拒绝点：_pre_trade_check 返回非 None → 写 order_rejected 事件 + _last_reject，直接 return
       ⚠️ 它【不】走 _note_close_rejected → 不累加 _close_fail_streak、不冷却、不告警
       （对照：CTP 拒单那条分支才调 _alert_on_reject + _note_close_rejected）
    ② 账户归宿：净敞口没变 → 状态原地不动（本例 RUNNING），持仓继续裸奔
    ③ 自愈：下一个触发器再进同一分支。**"关闭自动下单"也解不开**
       —— 它走同一条 _force_exit → _decide_exit → 撞同一个校验
    ④ 可见性：auto_order_status() 的返回字段里【没有】"上次拒绝原因"
       （只有 close_cooldown 是为"为什么半天不补单"专门加的）→ 前端零提示
       ⇒ 每根 bar 重试一次、每次写一条同名 order_rejected（刷屏），用户完全无感
    ```
    实例：`close_volume_below_target` —— 簿内**同向各笔手数不一致**时（如 `[多2, 空2, 多4]`），
    ⑤ 取 target = 同向最早一笔（4 手）而净敞口只有 2 ⇒ `min(2,4)=2 < 4` ⇒ 永久拦住离场。
    两条触发路径（共同前提 = 跨会话改表第 3 列后**又开过一次仓**）：
      · 调**大** N（2→4）：第一次 ⑤ 能成（净 ≥ 目标），落到第三格（运行态），**第二次**离场才被拦；
      · 调**小** N（4→2）：**第一次**离场就被拦（净 2 < 目标 4）。
    ⇒ 通用纪律：
      · 报告"某分支会被拒绝"时，**必须接着写出"拒绝之后账户长什么样"**，否则等于没报
        （"被挡下"读起来像保护，实际可能是永久卡死）；
      · **凡是"用户点一下就能收尾"的兜底动作（关闭自动下单 / 平仓 / 重启），都要实测它是否真能解**；
      · 预校验类拒绝是**引擎自己造的问题**，其可见性不能依赖"CTP 拒单告警"那条通道。

108. **⚠️ 「源码护栏」的抓法过窄 = 假绿 —— 写完必须做【变异自测】**
    （2026-09-16 实测：同一条 AST 护栏，第一版命中 0 处却"看起来通过"）
    （§6「源码护栏的五个陷阱」的第 6 条）

    实例：要证"运行期零次读 `cfg.product_profile`"，第一版只匹配
    `self.cfg.product_profile`，而真实代码写的是
    `Instrument(cfg.instrument, cfg.product_profile)` —— **裸形参名 `cfg`**，
    根本没有 `self.` 前缀 → 命中 **0** 处。
    这次是运气好：断言写成 `sorted(函数名) == ["__init__"]` 所以**当场红**了；
    若写成 `len(hits) == 0`（"运行期没有读取点"）就会**假绿**。

    纪律：
    · 判据别写"零命中"，写「**恰好只有白名单里的那一处**」—— 前者对抓法错误免疫，
      后者会把抓法错误暴露出来；
    · 命中集合**先打印再断言**（本例打印出 `[]` 与预期 `["__init__"]` 不符，
      才发现是抓法问题，而不是代码问题）；
    · 抓法本身也要有断言（命中的行号 > 0、函数名符合预期 —— 防"匹配了个空气"）；
    · 最后跑**变异自测**：故意把生产代码改回旧写法，确认护栏变红。
      本轮 5 个变异 **5 个被杀**（脚本见 §5 的"反向补丁"思路）：
      摘断言 / 调用加回旧参数 / 读法退回 getattr / 删早退分支 / 判据回潮换层。

    实例 B（2026-09-22，同一坑的第二个面孔 —— 这次栽在"节点类型"上）：
      要证「8 个品种档案里 `quote_unit`/`price_tick`/`multiplier` 的**书写顺序**」，
      第一版只判 `isinstance(node, ast.Assign)` ⇒ **命中 0 个档案**。
      原因：真值写作 `PRODUCT_PROFILES: Dict[str, Product] = {…}` —— **带注解的模块级赋值是
      `ast.AnnAssign`（字段是 `.target` 单数），不是 `Assign`（`.targets` 列表）**。
      ⚠️ 更险的是：这次**判别力自证仍然绿**（那条用的是无注解样本 → 走 Assign 分支），
      所以"自证"救不了它 —— 救场的是**"解析到 8 个档案"这条数量断言**。
      ⇒ 纪律（并入第 108 条）：
        · 遍历模块级常量时 **`Assign` 与 `AnnAssign` 都要收**（同理 `Terminated` 与
          `Try` 的 `orelse`、`ast.Str` 与 `ast.Constant` 这些 py3 版本迁移对）；
        · **"取到的对象数 == 预期数"必须单列一条断言**，别只断言"内容合规" ——
          `hits == {}` / `bad == []` 这类"空即通过"对抓法错误完全免疫（恒真的两种写法之一）。

109. **⚠️ 打桩要打在被读的【那一层】；替换对象后必须重设标志位（否则退化成恒真断言）**
    （2026-09-16 实测：一次改动连带弄红两个老测试，且其中一个差点变成假绿）

    场景：`_check_spec_drift` 的早退判据从 `cfg.product_profile` 改成
    `self.state.product` 之后 ——
      · 老测试的桩打在 `eng.cfg`（`class _CfgStub: product_profile = None`）
        → **再也触发不到早退分支**，继续往下走撞 `self.cfg.instrument`
        （stub 没有该属性）→ `AttributeError` 崩掉；
      · 把桩挪到 `eng.state = Instrument(None, None)` 是对的，但 **`verified` 必须
        在替换之后**再置位：新 Instrument 的 `verified` 初值是 False，
        沿用"先 `verified=True` 再替换 state"的顺序 → `if self.state.verified:`
        直接为假 → 用例**退化成恒真断言**（早退分支在不在都绿）。

    纪律：
    · 判据换层 ⇒ **桩跟着换层**；
    · 改测试时专门想一遍**语句顺序**类陷阱（"先设标志再换对象"= 把标志重置掉）；
    · 桩改层后回问一句：**这条分支在生产路径上还可达吗？**
      本例"品种在册却拿不到档案"已被启动期断言拦死 ⇒ 老测试测的是**不可达分支**；
      正确处置不是"把桩挪过去继续测"，而是**把断言升级成「启动期拒绝启动」**
      （不可达的分支留在测试里，只会让人误以为它还会发生）。

110. **「对比基线清单」的结构假设必须先验证：取值是标量还是嵌套 dict**
    （2026-09-16 实测，误报 314 个文件"已改动"）

    用 `{path: {"sha":…, "size":…}}` 型 manifest 生成"哪些文件被改过"清单时，
    第一版按扁平 `{path: sha}` 用（`now[p] != old[p]` 拿字符串比字典）→ **恒不等**
    → 报「314 改 / 1 增」，而真值是「3 改 / 1 增」。

    纪律：
    · 清单结果要**能质疑**：「314 个文件全改了」这句本身就该触发怀疑
      （改动手感只有 3 个文件），而不是直接写进报告；
    · 嵌套字段显式解包（`v["sha"]`），别靠宽松取值；
    · 最终以 **zip 条目 + 逐文件 sha** 为准交付 —— 那才是权威清单。

111. **生成的"含中文散文"的 Python 脚本里，ASCII 直引号会截断字符串**
    （2026-09-16 实测：报告生成脚本直接 `SyntaxError`）

    写「报告生成脚本」这类把中文成段塞进字符串字面量的代码时，中文语境里习惯性
    敲的 `"先前三处"` / `"拒绝启动"` 都是 **ASCII 0x22**，会把外层字符串截断
    → `SyntaxError: invalid syntax. Perhaps you forgot a comma?`。
    坑在于**报错行号常常指在下一行**（Python 把两段 `"…"` 视作隐式拼接，
    真正的断点在上面一行），照着报错行找是找不到的。

    纪律：
    · 中文引号一律用全角 `“ ”` / `「 」`，中文散文里别用 ASCII 引号；
    · 修的时候**不要**用正则"扫全部裸引号" —— 字符串边界本身就是引号，
      会刷出几十条误报（第一版兜底校验就是这样，把 `"结论**：` 之类也算了进去）。
      正确做法：`ast.parse` 后遍历 `ast.Constant`，只检查**常量内部**是否还有
      紧邻中文的 ASCII 引号（本例据此又扫出 1 处藏在 module docstring 里的）；
    · 定点替换要**逐条断言命中次数 == 1**，防漏改/防多改。

112. **交付报告渲染自检的两个盲区：底边探测要按底色、溢出文本只能靠眼睛**
    （2026-09-16 实测）

    · **内容底边探测**：页面背景是 `#f4f6f8` 这类**浅灰**时，用"是否纯白"判行会让
      **每一行**都算有内容 → 报"内容底边 = 图片高度"，看着像"截图被截断"，
      实际是判据错了。先取角落像素当底色，再按**颜色距离**判行。
    · **SVG 文本溢出**：`viewBox` 内 `font-size × 字数` 超过容器宽度时，
      浏览器**不报错**、DOM 结构检查也全绿（标签都合法），只有截图肉眼能看见。
      所以报告自检必须**真渲染 + 分段肉眼过一遍**，重点看"中英混排的长句方框"
      （本例 `决策（状态机/手数/报单）= 会计（成本/对账）= 同一个 Product`
      就溢出了，拆成两行才收进框内）。
    · **连字符折行**：markdown→HTML 转换器不产生 class，`⑶-b` 这类含连字符的
      编号会被浏览器在 `-` 后断成两行。只能靠 CSS `td:first-child{white-space:nowrap}`
      兜（加之前先确认该列最长内容仍在容器宽度内，否则会横向溢出）。

113. **⚠️「元护栏」会被自己的扫描器抓成残留 —— 豁免要精确到【规则】，不能精确到【文件】**
    （2026-09-16 B 批实测：`b2_scan.py` 报"测试侧残留 11 处"，**全部**在护栏文件自己身上）

    场景：本轮新增元护栏 `test_p56`，它**必须**内嵌违规样本（否则证明不了检测器能抓东西）、
    断言标签也**必须**写清"检查的是 `.exchange`"。于是它自己就成了"全仓旧符号扫描"的
    命中目标 → 护栏一上线，"零残留"判据立刻恒红。
    这是坑点 108 / 111 的同族：**自指**。

    ❌ 一刀切做法：`SELF_EXEMPT = {护栏文件}`，整个文件跳过扫描。
      代价：以后有人真在护栏里写了 `.exchange` 的**用法**（不是样本），也没人发现
      —— 留了个后门。
    ✅ 正确做法：**豁免到"规则"粒度**。实测 11 处命中**清一色是「字符串常量」**这一条规则，
      而 `AST 属性` / `AST 关键字实参` 两条**命中 0**。
      ⇒ 只对护栏文件豁免"字符串常量"这**一条**规则，另两条**照扫**：

      ```python
      if is_self_guard:                     # 违规样本 / 断言标签 ≠ 用法
          exempt.append(...)                # 单独打印出来，看得见
      else:
          hits[bucket].append(...)
      ```
      ⇒ 报告里写清"豁免 N 处 + 理由 + 其余规则实得 0"，而不是含糊的"已豁免该文件"。

    **通用判据**：豁免的粒度要**细到能继续抓真问题**。凡是"整个文件/整个目录/整类符号
    都跳过"的豁免，都要再问一句——**跳过之后，我还能抓住什么？**

114. **内联图形（SVG / canvas）里不能写 markdown 语法 —— 检测手段是"生成后 grep 残留标记"**
    （2026-09-16 B 批实测：报告里的 SVG 会原样显示 `**问题**` 的星号）

    报告生成器的 md→HTML 转换只作用于**正文**；手写在 Python 里的 SVG 片段是**原样注入**的，
    `<text>` 不认识 `**`。本轮在 SVG 注释句里写了
    `这是**问题**；R-OPEN/CLOSETODAY 是**答案**` → 页面上真的显示两个星号。
    改法：`<tspan font-weight="700" fill="#12703a">问题</tspan>`。

    纪律：
    · SVG 内部文本一律**裸写**，要加粗用 `font-weight` / `tspan`；
    · 生成后加一条**残留自检**：整份 HTML 里 `re.findall(r"\*\*", html)` 必须为 **0**
      （正文里残留的 `**` = markdown 转换漏网，SVG 里残留 = 本条）。这一条同时覆盖两类 bug，
      成本一行，本轮正是靠它逮到的；
    · 同类：SVG 里的 `「」`、`✗`（U+2717）等字符在部分字体下会落到 fallback glyph
      （实测 `✗` 渲染成红色 X）→ **必须真渲染截图肉眼确认**，别只看 DOM 检查全绿（坑点 112）。

115. **负对照的"红因"要抓【全量输出】；"哪些文件【不该红】"是同等重要的证据**
    （2026-09-16 B 批实测：`tail[-600:]` 够判红没红，不够写报告）

    场景：本轮做了"新测试 vs 旧代码"的负对照，9 个文件 7 红 2 绿。
    第一版只存 `tail[-600:]` → 若干文件的失败行落在截断之外，
    报告表格只能写成千篇一律的"它是结构断言 → 必须红"，等于没证据。
    单开一个 `neg_probe.py` 抓全量 stdout/stderr 后，表格才写得出：
    `AttributeError: 'ExecPolicy' object has no attribute 'today_exit'`（3 个文件崩）、
    `TypeError: unexpected keyword argument 'today_exit'`（1 个）、
    `got=[8 档] expected=[]`（3 个断言红）。**红因本身才是"护栏有没有牙"的证据。**

    配套两条：
    · **"先红一片再崩"要两条都写**：`test_p56` 在旧代码上先红 9 条结构断言、
      再崩在 `line 262`。只写"崩了"会丢掉"它逐条咬住了什么"；
    · **绿的那 2 个文件是正面证据**：它们只改注释/文案，旧代码下照样绿
      ⇒ 证明"不是跟着实现一起改的假绿"。报告里要**明确写"不该红"**，
      否则读者会以为漏了 2 个。
    ⇒ 判据模板：`N 红（全部落在本批主题上）+ M 绿（原因=纯文案）`，
    而不是含糊的"负对照通过"。

116. **每次动手前的第一件事：复查远端 HEAD 是否已推进**（坑点 96 的强化版）
    （2026-09-16 B 批实测：HEAD 已从 `d58e483dba4e` → `3d5df296c800`，用户已合入上一批）

    本轮若不复查，就会拿"上一批的工作树"当基线打包 → 把用户**已合入**的改动
    再覆盖一遍（清单里混进上一批），并覆盖掉用户**自己改的**文件（本轮实测那 1 个
    `Docs/账户三态…html` 就是用户自行修的口径）。
    ⇒ 动作固定三步：① `commits?sha=<branch>&per_page=1` 取 HEAD；
    ② 若 ≠ 上批基线 → compare 出用户合入了什么；
    ③ **逐字节树对比**确认"我的上批工作树 vs 新 HEAD"的差集里**只有用户自己改的文件**
       （差集里出现我的文件 = 用户没全合，要重新判断基线）。

117. **迁移加列时别给 `DEFAULT`** —— `NULL` 与 `''` 的区别是"回填能否自愈"的判据
    （2026-09-16 C 批实测，本批唯一动 schema 的改动）

    `ALTER TABLE t ADD COLUMN k TEXT` 顺手写 `DEFAULT ''`，则新列上
    **`NULL`（没算过）与 `''`（算过、但解析不出）糊成一个** → 回填判据
    `WHERE k IS NULL` **永假** → 「上次回填中断」留下的半成品**再也补不回来**
    （表面功能正常，是静默的；这正是它危险的地方）。

    纪律：
    · 加列**不给 `DEFAULT`**（旧行留 `NULL`）；
    · 回填判据用 `IS NULL`（不是 `= ''`）；
    · 幂等写法固定两句：**列不存在 → 加列 + 全表回填；列已存在 → 只补 `IS NULL` 的行**；
    · 变异自测要**专门杀一次「加 `DEFAULT`」**（本轮 M7，被 P57 `[2c]`/`[2j]` 咬住）——
      否则这条静默失效没人知道。

118. **派生值从「查询时现算」搬到「写入时落列」—— 收益是判定时刻收敛，配套三件套**
    （2026-09-16 C 批 ⑶-d）

    同一件事原先有**两个口径来源**：写库那份原始字段（字面量）+ 查库时现算的派生值
    （每查一次算一次）。搬到写入侧后，行落库那一刻归属就定型，查询只做列相等。

    三件套（缺一不可）：
    1. 判据改**列相等**（`WHERE key = ?`），查询侧**不再解释**原始字段；
       解析不出时**一律 0 命中**，绝不退回「不过滤」（退回会把全库当成本次结果，是"有数据但串品种"的假象）；
    2. 归一函数**只留一处定义**，放**被依赖的底层模块**（`Infra/Product.py`），上层（`TradeStats`）转发；
       用 `A.f is B.f` 钉住转发关系（防再造第二份实现）；
    3. 写入侧 `INSERT` 改**显式列名** —— 位置绑定把「参数序 = 物理列序」变成隐式约定，
       加列要"新列在表尾 + 参数补末尾"两处同时对才不炸。

    护栏：写个扫描脚本数**写入点唯一 / 定义点唯一 / 查询侧 row 级现算为 0** ——
    注意**先剥注释**再数（见 119）。

119. **⚠️ 复现：「护栏被自己的注释蒙过」—— 修法是剥注释 + 把坑做成活体样本**
    （2026-09-16 C 批**再次**踩到；行 566/568 条的一般性在此得到完整复现）

    实例：`save_trade` 里写了「取代 `INSERT INTO trades VALUES (...)`」这类**留档注释**
    → 老护栏用朴素 `in "INSERT INTO trades VALUES"` 判定 → 注释里的举例让断言**恒真**，
    **真把代码改回 `OR REPLACE` 也照样绿**。二次踩中说明「写完护栏人肉复核判定文本」不靠谱。

    升格做法（比"下次小心"可靠）：在新护栏里**保留这个坑的活体样本** ——
    P57 `[0a]` 掩码前假通过 / `[0b]` 剥注释后正确判 False / `[0c]` 真形态可见。
    这样「必须剥注释」这件事本身被测试钉住：以后谁把 `_code_only()` 去掉，`[0b]` 立刻红。
    ⇒ 通用模式：**把「你踩过的坑」写成一条反例测试**，而不是只写进文档。

120. **对照树复验的 base 选法：别用旧 manifest，用「远端 commit 原文 + P0 双向自证」**
    （2026-09-16 C 批）

    拿早先留的 `manifest_*.json` 当 base 有**两个隐患**：① 它是**更早的快照**
    （可能来自上一批之前，与你本批的"之前"不是一回事）；② **口径可能不同** ——
    git blob sha 是 `sha1(b"blob %d\0" % len + data)`，与裸 `sha1(data)` **直接比会全不相等**
    （本轮第一版探针就这么被误导过）。

    改用两条自证代替"信任旧 manifest"：
    · **P0-a**：本批改动的文件「远端 HEAD 版 ≠ 本地版」→ **全 True** 才说明 HEAD 尚未含本批；
    · **P0-b**：取若干**未改动**文件「远端 HEAD 版 == 本地版」→ 说明本地无额外未提交改动；
    · **T1 树级 diff**：对照树 vs 工作树的差异**恰好等于**改动清单（多项 / 少项都算失败）——
      一步同时兜住「copytree 漏文件」「回退不忠实」两类意外；
    · **拉错 ref 的兜底信号**：T2 负对照若不红（新测试在旧代码上竟绿），说明拿到的其实是
      **含本批**的版本 —— 这是唯一能自动发现"基线拉错"的判据，务必把它设成硬失败。

121. **⚠️ 交付「下一版」前先查远端 HEAD —— 用户可能已把上一版合并推送了**
    （2026-09-21 实测，v1→v2 差一步就把 diff 基线算错）

    背景：10:19 交付 v1 的 7 文件包 → 10:27 用户已**合并并推送**（`85e8deea`）→
    10:56 我做 v2 时按"老 HEAD `7aea0e84`"当基线，第一版 diff 直接算错
    （SimNow 显示 +88/−35，看着像改动变少了；Reconcile/main 等 5 个文件显示 +0/−0
    "无改动"，实际是我改的上一版已在远端了）。**幸而导出一份新 tree 时顺手比了尺寸**
    才发现，否则报告里的基线、行号、改动清单会整份错。

    **开工前的三步 P-1 自证**（都很快，别省）：
    - **P-1a 取 commits**：`GET /repos/<o>/<r>/commits?sha=<branch>&per_page=8`，
      记下最新 sha 与时间。**与上次交付的时间对对**。
    - **P-1b 认领上一版**：`GET /repos/<o>/<r>/commits/<sha>` 看 `files[]` 的
      `filename / additions / deletions` —— 若与你上一版交付包的**文件清单 + 逐文件 +/− 逐项吻合**，
      即**已合并**。这是判定"基线是否前移"的唯一硬证据（比问用户快）。
    - **P-1c 决定包内容**：
      - 已合并 → **本版只装增量文件**（基线 = 用户那条新 HEAD），报告 §基线 必须写明
        「vN 已合并 @<sha>」并给出 P-1b 的吻合证据；另可留一个"超集包"备"万一还没合并"。
      - 未合并 → 按老规矩，包内含「上一版 + 本版」全部改动文件（用户拿到即自洽）。

    **同时更新的三处**（漏一处就是自相矛盾的文档）：
    ① 报告的基线块（sha + 时间 + "已合并"结论）；② 改动集合边界那一节
    （此时正确口径是"相对**新** HEAD 恰好 N 个文件尺寸不同"）；
    ③ 任何写死的行号引用 —— 基线前移后行号会平移，**引用前必须重新 grep**（见铁律 6.5）。

    ⇒ 一句话：**"上一版交出去之后有没有被合并"是个会变的状态，别假定它没变。**

122. **两类同源病：「形态判据写错容器类型」与「失败值被 `or` 抹平」——附自查命令**
    （2026-09-21，同日连中；两处都不是"读一眼就能看出"的）

    **(a) `isinstance(x, dict)` 判形态 —— 对非 dict 的 Mapping 恒 False。**
    tqsdk 的容器类 `Entity` 继承 `MutableMapping` 而**不是 dict 子类**
    （实测 `isinstance(Entity(), dict)` = False、`isinstance(Entity(), Mapping)` = True）。
    真正危险的是**fallback 腿**：`x.values() if isinstance(x, dict) else list(x)`
    —— 对 Mapping 走 `list(x)` 就是**把 Mapping 当序列迭代**，拿到的是**键**（`['t1']`），
    于是每个字段都取不到 → **静默**累计出 0 / 空。本项目已在三处同源命中：
    持仓读数（`_position_split`）、启动账户基线、独立强制平仓工具。
    自查：`grep -rn 'isinstance([^,)]*, *dict)' <pkg>` 逐个问一句
    「这个对象在生产里会不会是 Mapping 但不是 dict？」；需要判形态一律用
    `collections.abc.Mapping`。**改的时候务必保留序列分支** —— 仓库里真有测试喂 list
    （`Trading/Test/test_p6_fix.py:126`），一刀切会弄红它。
    ⚠️ 另一类安全的 `isinstance(x, dict)` 是**JSON 解析产物**（`json.load` → 真 dict）与
    仓库自己 `jsonl` 序列化出来的数据，那些不必改 —— 区分办法是**看它从哪来**，不是看写法。

    **(b) `x = f() or 0` 只在 `f()` 返回 falsy 时替换。**
    `None or 0` → `0`（被抹掉）；`-1 or 0` → `-1`（**truthy，不会被抹**）。
    所以「会把 -1 抹成 0」这种判断要先核对**失败谱**再下结论 —— 本项目实测
    `real_position` 的失败来源是 `_api is None` / `_channel_unstable()` → `None`，
    而 `get_position()` 抛的异常会被下游自己的 `except` 吞掉、仍返回 `-1`。
    危害也不在"抹成 0"，而在 **failure 与 failure 互相抵消**：
    同一断言前后各读一次、两边都失败 → `(0,0) == (0,0)` 恒真 → **空断言**，冒烟报绿而
    实际什么也没读到。自查：`grep -rn 'real_position(.*) or 0'`、`grep -rn '= .*() or 0$'`。
    修法不是"把失败值换成另一个合法值"，而是**让读数带上「是否可信」标志**
    （如返回 `(ok, long, short)`），断言里 `ok and ...` 一起判。


## 8. 交付包结构（模板）

> ⚠️ **2026-09-15 覆盖条款（优先级高于本节以下所有模板）**：下面两版模板里出现的
> `patch/`、`changes.diff`、`ab_*.py`、`evidence/` 等**均已不再默认交付**，以
> `memory-workflow.md` §交付格式（第 2/6/9/10 条）为准：
> - **不出 diff / patch**：既不进附件，也不进 zip，产物放交付区之外。
> - **`.md` 不进 zip**：报告只作独立附件，且**不要重复发两份**。
> - **实验脚本默认不发**：`exp_*.py` / `derive_*.py` / 探针脚本**既不进 zip，也不挂
>   `present_files` 附件** —— 只交付「结论 + 文档」，回复末尾一句「脚本在沙盒 `sandbox/`，
>   需要随时说」。内部仍要求**在沙盒保留脚本**（可复现性），只是不推送给用户。
>
> 本节保留旧模板仅作**结构参考**（overlay 覆盖思路、命名、打包坑点）。

```
deliverables/<date>_<topic>/
├── README.txt            使用说明 + 覆盖/回退方式 + 复现命令 + 坑点 + 待决策清单
├── overlay/Trading/...   改动后的**完整文件**，按目录层级放 → 覆盖即用、删掉即回退
├── patch/
│   ├── applied_<x>/      unified diff（对原始文件，diff -u --strip-trailing-cr）
│   └── proposed_<y>/     尚未套用的候选补丁
├── evidence/             验证脚本 + 原始输出 txt
└── reports/              报告 md + HTML 双格式（内容一致）
```

**纯代码修复的更精简版（2026-09-10 采用，用户认可）**：

```
deliverables/<date>_<主题>/
├── <name>_fix_<date>.zip     ← 交付包：**只装源文件，按仓库相对路径**
│                                （解压到仓库根即覆盖；不含 README/证据，避免污染仓库）
├── 交付说明.md / .html        ← 同内容双格式
├── changes.diff              ← 相对修复前的 unified diff
├── ab_id_fix.py / ab_scenario.py / *_output.txt   ← A/B 与清房复验脚本 + 原始输出
└── pack_and_verify.py / *_output.txt
```

关键约定：
- zip 内**只放仓库相对路径的源文件**；README 与证据放在 zip **外面** ——
  否则解压会把 `_evidence/` 之类目录落进用户仓库。
- 用 `zipfile` 显式列文件写入，别用 `shutil.make_archive` 打整个目录（会夹带 `__pycache__`）。
- 打包前清运行时残留：`find . -name "__pycache__" -prune -exec rm -rf {} +`、删 `_verify_*/`、`.db`、`.jsonl`。
- 报告里给**诚实的改动量**（相对修复前的 `+N/-M`）—— 反向补丁不完整会让它显示 `+0/-0`（见 §5）。
- **新增文件的 diff 要按 `/dev/null` 全文列出**，并在统计里把"修改文件 +N/-M"与
  "新增文件 +K（全文）"**分开报**。混在一起报会让"代码改动量"看起来虚高
  （本次：修改 3 文件 +87/−33，新增 P30 一文件 +423 行）。
- **`changes.diff` 必须用 git 原生生成，且交付前跑 `git apply --check` 干跑**（见坑点 98）。
  手工拼接（difflib）会在"上一文件无末换行"处把两段粘一起 ⇒ 整份补丁作废。
  自校验：`--- a/ == +++ b/ == diff --git == 改动文件数`。
- **"我自作主张的附加"要在交付说明里单独成节**，写清：为什么加、只影响什么（日志/行为）、
  要拿掉得删哪几行。用户的默认反应是"我没批这个"，先答比被问好。
- **中文文件名要设 zip UTF-8 标志位**（`zi.flag_bits |= 0x800`，见坑点 72）；
  打包后回读 `namelist()` 确认条目名不乱码。

**文档类改动解耦打包**：与代码补丁**分开**成包（`Docs_<主题>_<日期>.zip`），
各含「改后全文 + 改前基线 + unified diff + 变更说明」，彼此独立可还原。
若采用**就地改**（§0.1），这份文档包就是用户唯一的比对依据，**不能省**。

打包后**解压到干净目录复验**（三件套见 §5 末、流程见 §5.1）再交付。

---

## 9. 报告结构（用户偏好）

先 **Verdict 速览表**（每条问题一行结论 + 状态标签），再逐条展开；每条都要：
**一句话 / 代码路径（带行号）/ 实测证据 / 危害形态 / 建议修法（标注"已落地 or 待拍板"）**。

末尾固定两节：**交付物清单 + 复现 + 坑点**、**待你决策清单（编号 D1/D2/...，每条带我的建议）**。

若是"修正自己上一版报告的措辞"，**要显式标注"修正上一版报告"**并说明原措辞为何不准 —— 用户会看这个。

### 9.1 交付说明文档（md + HTML 双格式，固定结构，缺一不可）

评审报告之外，**每个交付包都要配一份变更说明**，固定九节：

1. **一句话结论**（改了几个文件、几处、多少行、可否整体回退）
2. **怎么用**：应用（覆盖 / patch）、**回退**（整包 + 单文件）、应用后自检命令
3. **改动清单表** + **改动行数**
4. **逐处说明**：位置 / 原代码 / 事实 / 后果 / 改后 / **影响面** / A/B 结果
5. **验证证据表**：每项的结论 + 对应 `evidence/*.log` 文件名
6. **明确没有改的东西**（列出未触及的模块/测试，这是"波及面可控"的正面证据）
7. **遗留项**（优先级 + 位置），并显式标注哪些**不在本包**
8. **复跑方法**（可整段复制执行）
9. 任何**修正前一版报告的错误说法** → 单独用醒目块写出来，别偷偷改掉

HTML 版：自包含、浅色主题（跟随 IDE 主题）、`<pre>` 里用配色区分 `+` / `-` 行、
A/B 用左右对照卡片。

**若某个文件是「就地改」的**（§0.1），说明里必须**单独一节**讲清
"为什么只给了一个文件、怎么自己比出改动"（给两版全文 + diff 的用法），
否则用户会得出"你没改"的结论（2026-09-13 真实踩到）。

**HTML 生成的机械坑（2026-09-16 C 批实测，务必加自检）**：把上一批的
`gen_report_*_html.py` 抄过来复用时，**容易只改路径常量、漏改内部循环** ——
本轮表格组装处 `for r in body`（`body` 已剥掉表头分隔行）被手误写成
`for r in rows[1:]` → 每张表的 `|---|---|` 分隔行被当成**数据行**渲染，
表格顶部多出一排 `---`。这类"结构行泄漏"在长报告里肉眼极易漏掉。

自检一行（加进报告生成脚本的收尾）：

```python
assert re.findall(r">---<", doc) == [], "表格分隔行泄漏"
assert "**" not in re.sub(r"<pre>.*?</pre>", "", doc, flags=re.S)   # markdown 残留
```

⇒ 通用纪律：**复用转换器 / 解析器时逐行核对**，别只改路径与标题常量。
（配套：HTML 必须真渲染截图肉眼确认 —— 结构自检全绿不等于渲染正确。）

### 7.5 「跨版本账本兼容性」核验（2026-09-23 定型：旧库 + 新代码明天还能不能跑）

用户问「昨天开的仓单还在账本里，代码升级了今天能不能继续跑」，不是读代码能答的 ——
持久化格式的兼容性藏在**反序列化白名单**和**启动闸门**里，必须三步走完。

**三步（缺一步都可能漏掉唯一那条真差异）**

1. **序列化层：看 `from_dict` 是白名单还是 `**kwargs`**。
   chan.py 的 `ExitPlan.from_dict`（`Records.py:315-319`）是**显式字段白名单**构造
   ⇒ 旧库里多出来的键（如已删的 `tp_price`）被**静默忽略**，不会报错。
   ⚠️ 别把「pydantic `extra="forbid"`」的结论套到 dataclass 上 —— 那只对**配置**（`.env`）生效，
   对 **state.db 的 kv** 不生效。两者必须分开判。
2. **闸门层：diff 两版的持久化字段集与"旧 schema 拒绝清单"**。
   `diff` 两个 commit 的 `_persist_run/_restore_run` 与 `_LEGACY_POSITION_KEYS`/`_LEGACY_KV_KEYS`
   —— 本次实测两版**逐字节相同** ⇒ 昨天写的库不会被 G1/G2 拒。
   若这里出现差异（新增必填字段 / 新增拒绝键），旧库才会真的起不来。
3. **行为层：旧代码造库 → 新代码读库，真实引擎跑一遍**；再拿**用户真实 state.db 的只读副本**
   在新代码上启动一次。脚本骨架（`xday_compat.py` / `boot_real_db.py`）：
   - stage1（旧树）：`on_bar → on_signal →` 触发离场/关闭自动下单 → 造出"昨天的库"；
   - stage2（新树）：构造引擎（**构造即 `_restore`**）→ 打印 `account_state/簿面/run/事件`
     → 喂今天的 bar + 信号 → 打印报单序列；
   - 两个 stage **必须独立进程 + 独立 db 副本**（见坑点 123）。

**本类问题的三个高频答案（本次实测）**
- 锁仓态**不跑** `_settle_positions`（`_settle_positions` 首行 `if account_state() is not RUNNING: return`）
  ⇒ 旧库里挂着的出场计划在锁仓期间**完全不参与判定**；拆锁后那笔净敞口是**用当天参数重算** plan。
  ⇒ 所以"旧止盈价失效"这类差异**只在「带净敞口过夜（run 未清）」时可达**，锁仓场景为零。
- 关闭自动下单导致的锁仓 = **冻结**（`auto_order_enabled` 持久化 False）：信号走 `signal_skip`，
  唯一自动出口是「重开开关 + 一个对向信号 → 转移③」。
- **一笔一轮回**：N 笔锁仓（多空各 N/2 笔）要清空需要 **N/2 轮**（1 信号拆 1 笔 → RUNNING
  → 运行态不收信号 → L1-L3 离场 → 回到 LOCKED）。用户账本里躺着 14 笔时，这句话必须说出来。

123. **⚠️ 两个进程并行跑同一份 state.db → `IdCollisionError: orders.order_id` 是【实验事故】，不是代码 bug**
   （2026-09-23 实测踩到，差点当成"新代码引入订单号冲突"报出去）。
   成因：两个进程各自 `_restore` 读到同一个 `order_seq`（如 2），broker 序号都从 3 开始
   ⇒ 第二次 `save_order` 撞主键 ⇒ R1 的 fail-fast **正常工作**（这正是它该有的行为）。
   ⇒ 判据：每次跑给**独立 db 副本**（`shutil.copytree` 出 `*_base` / `*_fix` 两份），**串行**执行；
   已被并发写脏的库要**重造**，不能接着用。
   附带好处：这条反过来证明「序号跨重启 + fail-fast」这一组护栏是真在工作的。

124. **改 `cfg.instrument` 要先 `from_dict`：`InstrumentConfig` 是 `frozen=True`**
   （2026-09-23 实测）。`cfg.instrument.signal_symbol = "KQ.m@CFFEX.IM"` 直接抛
   `ValidationError: Instance is frozen`。正确写法：
   ```python
   d = json.loads(json.dumps(DEFAULT_CONFIG)); d["instrument"]["signal_symbol"] = "KQ.m@CFFEX.IM"
   cfg = TradingConfig.from_dict(d)
   spec = Instrument(InstrumentConfig(signal_symbol="KQ.m@CFFEX.IM", trade_symbol=SYM), PRODUCT_PROFILES["IM"])
   ```
   配套：引擎启动期有 `_assert_product_ssot` —— **cfg 的 signal_symbol 与 `state.product` 必须同一品种**，
   只改 `spec` 不改 `cfg` 会在这里炸。而 `TradingEngine.state` 取自 `broker.state`，
   所以**给 `DryRunBroker` 传正确的 spec 就够了**，`eng.state = spec` 那句是同对象赋值。

125. **「旧库能不能被新代码加载」的最后一关是【合约一致性】，不是字段兼容**
   （2026-09-23 实测：换 `trade_symbol` 启动同一份账本 → 加载 **0 笔**、`account_state=flat`）。
   `_restore` 按 `self.state.trade_symbol` 分片过滤持仓（v1.4 切合约隔离）
   ⇒ 用户今天订阅的合约与库里不一致时，旧持仓**留在库里但不进簿**，引擎当自己空仓，
   而柜台真实持仓还在 —— 账实不符且无任何告警。
   ⇒ 报兼容性结论时必须**同时核对用户账本里的 `symbol 集`**：
   ```python
   sqlite3.connect('file:'+db+'?mode=ro', uri=True)   # kv 表列名是 k / v 两列
   json.loads(cur.execute("SELECT v FROM kv WHERE k='positions'").fetchone()[0])
   ```
   并对每个 `d.get("symbol")` 求集合 —— 有 2 个以上就说明历史跨过合约，要单独提醒。
   **只读**核对用户 `C:\my_chan_project\Trading\State\state.db` 是允许的（不写即不违反沙盒铁律），
   它给出的证据比任何推演都硬（本次据此发现用户实际是 14 笔 IM2612，不是 IF）。