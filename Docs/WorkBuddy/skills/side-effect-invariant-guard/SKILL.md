---
name: side-effect-invariant-guard
description: 把「某个 API / 某条代码路径绝不允许产生副作用」这类不变量钉成**可执行且能自证**的护栏。核心手法：① 用 AST 把被测函数按**平台守卫**切成「Windows 可达 / POSIX 可达」两区，断言危险 API 不出现在错误那一侧（典型：Windows 上 `os.kill(pid, 0)` 不是存活探测，`signal.CTRL_C_EVENT == 0` 会让它变成给进程组投 Ctrl+C）；② 把危险 API 换成**调用计数桩**（不转发）—— 比"量事件个数"可靠，因为无控制台 / 忽略 Ctrl+C 的进程里事件计数恒为 0，会假绿；③ 护栏自证：源码级把旧写法**注入回副本**、运行时把模块属性**换回旧实现**，两路都必须报出来。当用户说「补一条『XX 不得产生副作用』的护栏断言」「这个探测/查询会不会有副作用」「只判断返回值抓不到这个 bug」「护栏写了但不知道有没有用」时使用。产出：独立脚本式护栏测试 + 变异自证矩阵 + 按仓库层级的 zip 交付包。
agent_created: true
---

# 零副作用不变量护栏（AST 分支划分 + 计数桩 + 变异自证）

适用症状：代码里某句"只是查一下"的调用，在某个平台上其实是**有副作用的动作**；
或者你只知道"应该没副作用"，但**没有任何可执行的证据**。

## 何时用 / 不用

**用**：
- 用户点名「补一条 XX 不得产生副作用的护栏断言」；
- 一个 bug 修完，它的形态是"某个查询/探测/判断动作夹带了副作用"；
- 你发现**只看返回值抓不到这个 bug**（旧代码返回值恰好也是对的）；
- 平台差异类危险 API：`os.kill(pid, 0/1)`、`TerminateProcess`、`GenerateConsoleCtrlEvent`、
  序列化/落盘、关闭句柄、日志雪崩、全局状态污染。

**不用**：纯逻辑分支覆盖 —— 那用普通单测就够了，不需要 AST 和桩。

## 铁律

1. **先证明"旧写法真的会产生副作用"，再写护栏。** 拿不到"旧实现被计数桩抓到"的
   证据，护栏就是猜的。同一句代码在不同环境（有无控制台、Ctrl+C 是否启用、
   是否新进程组）行为不同 —— 必须把这几种环境分别说清。
2. **别用"事件个数"当断言。** `SetConsoleCtrlHandler` 计数器只在**真控制台且
   本进程 Ctrl+C 启用**时才 +1；`CREATE_NEW_PROCESS_GROUP` 起的进程（含其孙进程，
   该"忽略 Ctrl+C"状态会继承）计数恒为 0 → **假绿**。改用 **API 调用计数**。
3. **桩不许转发危险调用。** 桩先记账再决定是否转发；Windows 上的 0/1 信号
   **绝不转发**。这样即使护栏失效，也不会真去投 Ctrl+C 打断跑测试的那个进程。
4. **护栏必须自证（变异测试），否则等于没写。** 两路都要做：
   源码级（把旧写法字符串注回副本 → 断言护栏报出对应规则码）、
   运行时（把被测模块的同名属性换成旧实现 → 断言计数桩抓到调用）。
   变异还要断言"确实改变了源码"，否则变异没生效会假装通过。
5. **只用 AST，不用正则。** 注释和字符串里出现 `os.kill(pid, 0)` 不代表代码里有；
   反之正则也容易漏。按节点判定"这条调用落在哪个分支"。
6. **平台守卫的划分要保守。** 找不到守卫 → **处处都算 Windows 可达**（宁可误报）。
   支持两种写法：`if os.name != "nt": <POSIX>`（其后语句算 Windows）与
   `if os.name == "nt": <Win> else: <POSIX>`。
7. **豁免要显式、要带理由。** 用行内标记 `# guard-allow: <理由>`，理由为空视同未豁免。
8. **不改用户仓库。** 护栏文件写到沙盒，按仓库原目录层级打包 zip；验证时用
   `CHAN_REPO=<用户仓库>` 环境变量把被测源码指过去（读源码 + import 都不需要写权限）。
   **新增测试文件前先 `ls` 目标目录，照抄大小写风格与行尾**（本案例多数派是纯 LF）。

## 标准流程

1. **定位危险点**：AST 扫全仓，列出危险 API 的**每一处**调用及其实参
   （是常量 0/1 还是变量？在哪个函数？有没有平台守卫？）。有白名单才好定红线。
2. **建正控条件**：找到能真实触发副作用的场景（真控制台 / 特定进程组 / 活进程 pid），
   先手动复现一次，把"副作用的可观测形式"确定下来（是事件、是计数、是被打断的父进程）。
3. **写规则函数（纯函数）**：`audit_xxx(src) -> [(规则码, 说明)]`，只吃源码文本。
   这样 [正检] 和 [变异负控] 能共用同一套规则。
4. **六层护栏**（按需裁剪）：
   - [1] 源码：AST 断言危险 API 不在错误分支、正确分支保留了原写法、早退守卫在位、
         上层调用方必须委托到下层的安全实现；
   - [2] 源码变异自证：旧写法注回 → 每条规则码都被报出；现行源码 → 零违规；
   - [3] 全仓扫描：危险调用清单 == 白名单（含 `# guard-allow` 豁免）；
   - [4] 行为：真调一遍，断言返回值正确（活/死/0/负数/不存在）；
   - [5] 零副作用：装计数桩跑遍 [4]，断言**调用数 == 0**（仅在平台相关的方向上断言，
        非 Windows 上 `os.kill(pid, 0)` 本来就是正解，要 SKIP 而不是 FAIL）；
   - [6] 调用链：把真实调用方（含用户在 issue 里给的那个场景）跑一遍，断言整条链零调用；
   - [7] 运行时变异自证：换回旧实现 → 桩抓到；换回新实现 → 回到 0。
5. **落交付**：独立脚本（`sys.exit(1 if _FAIL else 0)`，非 pytest），
   仓库缺被测包时打 `[SKIP]` 并正常退出（不要 FAIL 也不要假 PASS）。
6. **进套件**：确认它被 runner 发现（`--only <dir> --filter <kw> --stream`），
   再看门禁（`Test/run_all.py` 之类）是**显式注册表**还是目录发现 ——
   注册表形态的要在说明里给出"加哪一行"，但**不擅自改门禁基线**。

## 关键代码骨架

**平台分支划分**（保守：非 POSIX 分支的全算 Windows 可达）

```python
def _split_platform_regions(func):
    guard = next((s for s in ast.walk(func)
                  if isinstance(s, ast.If) and _platform_test_kind(s.test)), None)
    if guard is None:
        return set(), set()            # 找不到守卫 → Windows 可达 = 空集反义，见下
    st, kind = guard
    win, posix = set(), set()
    if kind == "posix_only":
        posix.update(id(n) for n in _collect(st.body))
    else:
        win.update(id(n) for n in _collect(st.body))
        posix.update(id(n) for n in _collect(st.orelse))
    for n in _collect(func.body):      # 不在 POSIX 分支里的，一律算 Windows 可达
        if id(n) not in posix:
            win.add(id(n))
    return win, posix
```
> 调用侧：`if not posix: 报 G1b「缺平台守卫」`，把"找不到守卫"当成违规而不是放行。

**计数桩**（记账 + 拦危险调用 + 可 uninstall）

```python
_real = os.kill
def _kill(pid, sig=None, *a, **kw):
    hits.append("os.kill(%r, %r)" % (pid, sig))   # 先记账
    if os.name == "nt" and sig in (0, 1):
        return None                               # 绝不转发
    return _real(pid, sig, *a, **kw)
os.kill = _kill
# 别忘了同时钉住 ctypes.windll.kernel32.GenerateConsoleCtrlEvent（赋值可覆盖 __getattr__）
```
> **桩装好要先做正控**：手工调一次，断言 hits 恰好 +1 —— 否则"桩没装上"会表现为全绿。

**运行时变异自证**

```python
exec("def _old_pid_alive(pid):\n    ...os.kill(pid, 0)...\n", ns)
_saved = AT._pid_alive
AT._pid_alive = ns["_old_pid_alive"]      # 上层是模块级名字查找，运行时会取到新值
# 断言 hits >= 1（证明 [5c]/[6f] 真有区分力）
AT._pid_alive = _saved
# 断言 hits 回到 0（证明差异由实现决定，不是环境噪声）
```

## 报告里必须回答的三件事

1. **副作用长什么样**：可观测形式 + 为什么"受害者"往往是**另一个进程**
   （发起方被 `CREATE_NEW_PROCESS_GROUP` 保护住，返回值和测试都是绿的）。
2. **为什么以前的测试/评审抓不到**：把"只看返回值也是对的"这条实测数据摆出来。
3. **护栏的区分力证据**：变异矩阵（注入了什么 → 报出哪个规则码）。

## 坑

- **`os.kill(pid, 0)` 在 Windows 上不是存活探测**：`signal.CTRL_C_EVENT == 0`，
  CPython 走 `GenerateConsoleCtrlEvent`，pid 是**进程组 id**，`pid == 0` 更是
  **广播整个控制台**。探活要用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(h, 0) == WAIT_TIMEOUT`。
- **"忽略 Ctrl+C"状态会继承**：父进程 `CREATE_NEW_PROCESS_GROUP` → 子、孙进程都免疫。
  所以"在 runner 里测不到副作用"是**环境**问题，不是代码没问题。
- **`ctypes.windll.kernel32` 是缓存对象**，属性赋值能覆盖 `__getattr__`，
  桩恢复时记得删/还原；不要重复 `windll.kernel32` 取新对象以为能绕过缓存。
- **"已退出进程判死"有 pid 复用假红风险**：`wait()` 之后立刻探测，并在注释里说明。
- **桩的卸载要放进 `finally`**，且**先卸载再清理子进程**（`Popen.kill` 在 Windows 走
  `TerminateProcess`、在 POSIX 走 `os.kill`，别让清理动作污染计数）。
- **子进程用 `stdin/stdout/stderr=DEVNULL` 起**：否则会占住跑测试那个进程的管道，
  触发 runner 的"孤儿管道"兜底逻辑。
- 被测源码路径用**被测模块自己的 `__file__`** 推，别硬编码；再给 `CHAN_REPO` 之类的
  环境变量覆盖口，才能在不改用户仓库的前提下复核真实文件。
