# -*- coding: utf-8 -*-
"""
test_apptrader_pid_alive_guards：AppTrader 存活探测「纯查询、零副作用」护栏
============================================================================
背景（2026-09-19 实测定性，非推测）
    Windows 上 `signal.CTRL_C_EVENT` 的值就是 **0**，CPython 的 `os.kill` 遇到
    0/1 会走 `GenerateConsoleCtrlEvent` 分支 —— 于是 `os.kill(pid, 0)` 这句
    "存活探测"的真实效果是**给该 pid 所在进程组投递一次 Ctrl+C**：

      · pid > 0：同组进程（含父进程、控制台宿主 shell）一起收 CTRL_C_EVENT，
        上层 Python 抛 KeyboardInterrupt 被打断；
      · pid == 0：直接**广播整个控制台**。
      · 发起者若由 CREATE_NEW_PROCESS_GROUP 启动（Ctrl+C 对该组禁用），它自己
        毫发无伤、测试照常 PASS —— 症状就是"某条用例一跑，跑测试 / 跑服务的
        那个进程莫名收到 Ctrl+C 并中断"。

    `_TraderProc.running` 被 `status()` / `stop()` 高频调用（`stop()` 每 0.3s
    轮询一次），而 `_TraderProc.pid` 允许为 0 → 在真实部署里足以把同控制台的
    api_server / gateway 一起打断。故存活探测必须走纯查询。

本文件锁死的不变量（6 层，全部可执行）
    [1] 源码护栏：`_pid_alive` 的 **Windows 可达路径** 上 0 处 `os.kill`；
        Windows 分支必须走 `OpenProcess` + `WaitForSingleObject` 纯查询；
        POSIX 分支保留原 `os.kill(pid, 0)` 写法；`pid <= 0` 早退；
        `_TraderProc.running` 必须委托 `_pid_alive`；
        `_send_signal_best_effort` 保留 Windows 上 0/1 拒绝。
    [2] 变异自证：把上述每条规则的旧写法**注入回源码副本**，断言护栏逐条报出来。
        （一条从未失败的护栏等于没有护栏 —— 本节是护栏自己的负控。）
    [3] 全仓护栏：`App/` + `Trading/` 下「字面量为 0/1 的 `os.kill`」只允许出现在
        带平台守卫的函数内；手工 `GenerateConsoleCtrlEvent` /
        `SetConsoleCtrlHandler` 调用 0 处。
    [4] 行为护栏：`_pid_alive` 返回值必须正确（活=真；死 / 0 / 负数=假）。
    [5] 零副作用护栏：把 `os.kill` 与 `GenerateConsoleCtrlEvent` 换成计数桩，
        跑遍 [4] 全部探测，断言**调用数 = 0**（仅 Windows：POSIX 上 os.kill
        就是正确的探测方式）。桩**不转发** Windows 上的 0/1 调用 —— 即使护栏
        失效，也不会真的往控制台投 Ctrl+C。
    [6] 调用链护栏：`_TraderProc.running` 真跑一遍，含 p20 的
        `_FakeProc.pid = os.getpid()`（"存活 pid 让 running 为 True"）场景，
        并断言整条链零 `os.kill`。

为什么不直接量「控制台事件个数」当断言
    `SetConsoleCtrlHandler` 的计数器只在**真控制台且本进程 Ctrl+C 处于启用态**
    时才会 +1。runner 用 CREATE_NEW_PROCESS_GROUP 起子进程，且该「忽略 Ctrl+C」
    状态会被孙进程继承 —— 此时计数恒为 0，拿它当断言就是**假绿**。[5] 的
    API 调用计数不依赖控制台，在任何环境都是决定性证据，故采用之。

免检标记
    [3] 允许用行内注释 `# guard-allow: <理由>` 豁免某一行（例如本文件 [5] 的
    正控需要真调一次 `os.kill`）。标记后必须写理由，否则视同未豁免。

运行方式（独立脚本，非 pytest）：
    python Trading/Test/test_apptrader_pid_alive_guards.py
    CHAN_REPO=<仓库根> python test_apptrader_pid_alive_guards.py   # 跨目录复核用
退出码 0 = 全部通过；非 0 = 有断言失败。
仓库无 App/ 包（Trading 单独解压）时，依赖 AppTrader 的 [1]/[2]/[4]/[5]/[6] 跳过，
[3] 仍对 Trading/ 生效。
"""
import ast
import os
import subprocess
import sys

# ── 定位仓库根（含 Trading/ 的目录）：可用 CHAN_REPO 覆盖 ──────────────────
_REPO_ENV = (os.environ.get("CHAN_REPO") or "").strip()


def _locate_repo(start: str) -> str:
    p = os.path.abspath(start)
    for _ in range(8):
        if os.path.isdir(os.path.join(p, "Trading")):
            return p
        nxt = os.path.dirname(p)
        if nxt == p:
            break
        p = nxt
    return ""


_REPO = os.path.abspath(_REPO_ENV) if _REPO_ENV else _locate_repo(
    os.path.dirname(os.path.abspath(__file__)))
_APP_TRADER = os.path.join(_REPO, "App", "AppTrader.py") if _REPO else ""
_HAS_APP = bool(_REPO) and os.path.isfile(_APP_TRADER)
if _REPO and _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_PASS = 0
_FAIL = 0
_SKIP = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("  [PASS] " if ok else "  [FAIL] ") + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    return ok


def check_true(name, got):
    return check(name, bool(got), True)


def skip(name):
    global _SKIP
    _SKIP += 1
    print("  [SKIP] " + name)


# ══════════════════════════════════════════════════════════════════════════
# AST 工具（纯文本 → 结论；不看注释、不猜语义）
# ══════════════════════════════════════════════════════════════════════════
_NT_LITERALS = ("nt", "win32", "cygwin", "msys")
_POSIX_LITERALS = ("posix", "linux", "darwin", "freebsd")


def _dotted(node) -> str:
    """把 Call.func / 表达式还原成 'os.kill' 这类点号名；非名字表达式返回 ''。"""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _str_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _int_literal(node):
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool) \
            and isinstance(node.value, int):
        return node.value
    return None


def _platform_test_kind(test) -> str:
    """判定条件是不是平台守卫：'posix_only' / 'nt_only' / ''。"""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return ""
    left, op, right = test.left, test.ops[0], test.comparators[0]
    for a, b in ((left, right), (right, left)):
        dotted = _dotted(a)
        lit = _str_literal(b)
        if dotted not in ("os.name", "sys.platform") or not lit:
            continue
        lit = lit.lower()
        if isinstance(op, ast.NotEq) and lit in _NT_LITERALS:
            return "posix_only"
        if isinstance(op, ast.Eq) and lit in _POSIX_LITERALS:
            return "posix_only"
        if isinstance(op, ast.Eq) and lit in _NT_LITERALS:
            return "nt_only"
        if isinstance(op, ast.NotEq) and lit in _POSIX_LITERALS:
            return "nt_only"
    return ""


def _collect(nodes):
    out = []
    for n in nodes:
        out.extend(ast.walk(n))
    return out


def _split_platform_regions(func):
    """按平台守卫把函数体节点分成 (windows_ids, posix_ids)。

    两种写法都支持：
      A) `if os.name != "nt": <POSIX>...`  → 分支内算 POSIX，其余全算 Windows 可达（保守）
      B) `if os.name == "nt": <Windows> else: <POSIX>`
    找不到守卫 → (set(), set())，即"处处都算 Windows 可达"。
    """
    guard = None
    for st in ast.walk(func):
        if isinstance(st, ast.If):
            kind = _platform_test_kind(st.test)
            if kind:
                guard = (st, kind)
                break
    if guard is None:
        return set(), set()
    st, kind = guard
    win, posix = set(), set()
    if kind == "posix_only":
        posix.update(id(n) for n in _collect(st.body))
    else:
        win.update(id(n) for n in _collect(st.body))
        posix.update(id(n) for n in _collect(st.orelse))
    for n in _collect(func.body):
        if id(n) not in posix:
            win.add(id(n))
    return win, posix


def _find_func(tree, name, cls=None):
    for n in ast.walk(tree):
        if cls is not None:
            if isinstance(n, ast.ClassDef) and n.name == cls:
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and m.name == name:
                        return m
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _calls_in(func, region_ids):
    """区域内所有 Call 的点号名（如 'os.kill'）。"""
    out = []
    for n in ast.walk(func):
        if isinstance(n, ast.Call) and id(n) in region_ids:
            d = _dotted(n.func)
            if d:
                out.append(d)
    return out


def _names_in(func, region_ids):
    """区域内引用到的标识符名（如 'WAIT_TIMEOUT'）。"""
    out = []
    for n in ast.walk(func):
        if isinstance(n, ast.Name) and id(n) in region_ids:
            out.append(n.id)
    return out


def _returns_false(body) -> bool:
    return any(isinstance(s, ast.Return) and isinstance(s.value, ast.Constant)
               and s.value.value is False for s in body)


def _has_pid_nonpositive_early_return(func) -> bool:
    """`pid <= 0 → return False` 早退（0 是"广播整个控制台"，必须直接判死）。"""
    for st in func.body:
        if not isinstance(st, ast.If):
            continue
        if _platform_test_kind(st.test):
            continue
        t = st.test
        for cmp_node in (t.values if isinstance(t, ast.BoolOp) else [t]):
            if not isinstance(cmp_node, ast.Compare) or len(cmp_node.ops) != 1:
                continue
            if _dotted(cmp_node.left) != "pid":
                continue
            val = _int_literal(cmp_node.comparators[0])
            if val is None or val > 0:
                continue
            if isinstance(cmp_node.ops[0], (ast.Lt, ast.LtE)) and _returns_false(st.body):
                return True
            if isinstance(cmp_node.ops[0], ast.Eq) and val == 0 and _returns_false(st.body):
                return True
    return False


def _has_sig01_reject(func) -> bool:
    """`if os.name == "nt" and sig in (0, 1): return False` 护栏（结构比对，非文本）。"""
    for n in ast.walk(func):
        if not isinstance(n, ast.If) or not _returns_false(n.body):
            continue
        vals = n.test.values if isinstance(n.test, ast.BoolOp) else [n.test]
        has_nt = has_in01 = False
        for v in vals:
            if not isinstance(v, ast.Compare) or len(v.ops) != 1:
                continue
            if _dotted(v.left) == "os.name" and isinstance(v.ops[0], ast.Eq) \
                    and (_str_literal(v.comparators[0]) or "").lower() in _NT_LITERALS:
                has_nt = True
            if _dotted(v.left) == "sig" and isinstance(v.ops[0], ast.In):
                elts = getattr(v.comparators[0], "elts", [])
                ints = [x for x in (_int_literal(e) for e in elts) if x is not None]
                if sorted(ints) == [0, 1]:
                    has_in01 = True
        if has_nt and has_in01:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# 规则本体：纯函数（只吃源码文本），供 [1] 正检与 [2] 变异负控共用
# ══════════════════════════════════════════════════════════════════════════
def audit_apptrader(src: str):
    """AppTrader.py 源码 → 违规清单 [(规则码, 说明)]，空 = 合规。"""
    bad = []
    tree = ast.parse(src)

    f = _find_func(tree, "_pid_alive")
    if f is None:
        return [("G1", "_pid_alive 不存在（存活探测入口被删/改名）")]
    win, posix = _split_platform_regions(f)
    if not posix:
        bad.append(("G1b", "_pid_alive 缺平台守卫：无法区分 Windows / POSIX 路径"))
    win_calls = _calls_in(f, win)
    posix_calls = _calls_in(f, posix)
    if "os.kill" in win_calls:
        bad.append(("G2", "_pid_alive 的 Windows 可达路径上出现 os.kill"
                          "（Windows 上 0/1 = 投递 Ctrl+C，pid=0 = 广播整控制台）"))
    if "os.kill" not in posix_calls:
        bad.append(("G5", "_pid_alive 的 POSIX 分支丢了 os.kill(pid, 0)（原语义被误删）"))
    k32 = sorted(x for x in set(win_calls) if x.startswith("kernel32."))
    if k32 != ["kernel32.CloseHandle", "kernel32.OpenProcess",
               "kernel32.WaitForSingleObject"]:
        bad.append(("G3", "Windows 分支不是 OpenProcess/WaitForSingleObject 纯查询，"
                          "实际 kernel32.* 调用 = %r" % (k32,)))
    if "WAIT_TIMEOUT" not in _names_in(f, win):
        bad.append(("G3b", "Windows 分支未用 WAIT_TIMEOUT 判定存活"))
    if not _has_pid_nonpositive_early_return(f):
        bad.append(("G4", "_pid_alive 缺 `pid <= 0 → return False` 早退"))

    run = _find_func(tree, "running", cls="_TraderProc")
    if run is None:
        bad.append(("G6b", "_TraderProc.running 不存在"))
    else:
        run_calls = [d for n in ast.walk(run) if isinstance(n, ast.Call)
                     for d in [_dotted(n.func)] if d]
        if "_pid_alive" not in run_calls:
            bad.append(("G6", "_TraderProc.running 未委托 _pid_alive"))
        if "os.kill" in run_calls:
            bad.append(("G6c", "_TraderProc.running 自身直接调用 os.kill"))

    sig = _find_func(tree, "_send_signal_best_effort")
    if sig is None or not _has_sig01_reject(sig):
        bad.append(("G7", "_send_signal_best_effort 缺 Windows 上 sig in (0, 1) 拒绝"
                          "（0/1 不是普通信号，是 CTRL_C / CTRL_BREAK）"))
    return bad


_SELF_NAME = os.path.basename(os.path.abspath(__file__))
_ALLOW_MARK = "guard-allow:"


def audit_kill_calls(src: str, path: str):
    """单文件通用护栏：字面量 0/1 的 os.kill 必须在带平台守卫的函数内；
    不得手工调用控制台事件 API。返回 [(规则码, 说明)]。"""
    bad = []
    if os.path.basename(path) == _SELF_NAME:
        # 本护栏文件自身的 [5] 正控需要真调一次 os.kill；该行已加 guard-allow 标记，
        # 故这里照常扫描，不做文件级豁免。
        pass
    lines = src.splitlines()
    tree = ast.parse(src)
    parents = {}
    for n in ast.walk(tree):
        for child in ast.iter_child_nodes(n):
            parents[child] = n

    def line_of(node):
        return lines[node.lineno - 1] if 1 <= node.lineno <= len(lines) else ""

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        dotted = _dotted(n.func)
        if dotted.endswith("GenerateConsoleCtrlEvent") or \
                dotted.endswith("SetConsoleCtrlHandler"):
            if _ALLOW_MARK in line_of(n):
                continue
            bad.append(("G9", "%s:%d 手工控制台事件调用 %s（应由存活探测改用纯查询）"
                        % (path, n.lineno, dotted)))
            continue
        if dotted != "os.kill" or len(n.args) < 2:
            continue
        if _int_literal(n.args[1]) not in (0, 1):
            continue
        if _ALLOW_MARK in line_of(n):
            continue
        # 找最近的函数上下文，判定是否落在平台守卫的 POSIX 分支里
        cur, func = n, None
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = cur
                break
        if func is not None:
            _win, posix = _split_platform_regions(func)
            if id(n) in posix:
                continue
            where = "函数 %s 内但不在平台守卫分支里" % func.name
        else:
            where = "不在任何函数内（模块级）"
        bad.append(("G8", "%s:%d os.kill(..., %d)：%s —— Windows 上会投递 Ctrl+C"
                    % (path, n.lineno, _int_literal(n.args[1]), where)))
    return bad


# ══════════════════════════════════════════════════════════════════════════
print("== test_apptrader_pid_alive_guards ==")
print("仓库根  : %s" % (_REPO or "<未定位到>"))
print("被测源码: %s%s" % (_APP_TRADER or "<无 App/ 包>",
                          "" if _HAS_APP else "  （不存在 → 依赖它的段落跳过）"))

_SRC = ""
if _HAS_APP:
    with open(_APP_TRADER, encoding="utf-8") as f:
        _SRC = f.read()

# ── [1] 源码护栏 ─────────────────────────────────────────────────────────
print("\n[1] 源码护栏：_pid_alive 在 Windows 上必须是纯查询")
if not _HAS_APP:
    skip("[1] 无 App/AppTrader.py（Trading 单独解压运行）")
else:
    _viol = audit_apptrader(_SRC)
    check("[1z] 现行源码零违规", [c for c, _ in _viol], [])
    for code, msg in _viol:
        print("        · %s %s" % (code, msg))
    tree = ast.parse(_SRC)
    f = _find_func(tree, "_pid_alive")
    win, posix = _split_platform_regions(f)
    check("[1a] 平台守卫可识别（os.name/sys.platform 判定）", bool(posix), True)
    check("[1b] Windows 可达路径 0 处 os.kill", _calls_in(f, win).count("os.kill"), 0)
    check_true("[1c] POSIX 分支保留 os.kill 存活探测（原语义不变）",
               "os.kill" in _calls_in(f, posix))
    check("[1d] Windows 分支走 OpenProcess/WaitForSingleObject 纯查询",
          sorted(set(x for x in _calls_in(f, win) if x.startswith("kernel32."))),
          ["kernel32.CloseHandle", "kernel32.OpenProcess",
           "kernel32.WaitForSingleObject"])
    check_true("[1e] pid <= 0 直接判死（0 = 广播整个控制台）",
               _has_pid_nonpositive_early_return(f))
    run = _find_func(tree, "running", cls="_TraderProc")
    run_calls = [d for n in ast.walk(run) if isinstance(n, ast.Call)
                 for d in [_dotted(n.func)] if d]
    check_true("[1f] _TraderProc.running 委托 _pid_alive", "_pid_alive" in run_calls)
    check("[1g] running 自身零 os.kill 调用", run_calls.count("os.kill"), 0)
    check_true("[1h] _send_signal_best_effort 保留 Windows 0/1 拒绝",
               _has_sig01_reject(_find_func(tree, "_send_signal_best_effort")))

# ── [2] 变异自证（护栏的负控） ───────────────────────────────────────────
print("\n[2] 变异自证：把旧写法注入回源码副本，护栏必须逐条报出来")
if not _HAS_APP:
    skip("[2] 无 App/AppTrader.py")
else:
    _ANCHOR_K32 = "    import ctypes\n    SYNCHRONIZE"
    _ANCHOR_RUN = "        return _pid_alive(self.pid)"
    _ANCHOR_123 = "    if pid <= 0:\n        return False\n"
    _ANCHOR_SIG = '    if os.name == "nt" and sig in (0, 1):\n        return False\n'
    for anchor, label in ((_ANCHOR_K32, "Windows 分支锚点"),
                          (_ANCHOR_RUN, "running 委托锚点"),
                          (_ANCHOR_123, "pid<=0 早退锚点"),
                          (_ANCHOR_SIG, "_send_signal 护栏锚点")):
        check_true("[2z] %s 命中（变异可施加）" % label, _SRC.count(anchor) == 1)

    _muts = [
        # 规则 G2：Windows 可达路径重新出现 os.kill(pid, 0)
        ("G2 Windows 路径回潮 os.kill",
         _SRC.replace(_ANCHOR_K32, "    os.kill(pid, 0)\n" + _ANCHOR_K32, 1), "G2"),
        # 规则 G6：running 不再委托、自己直接 os.kill
        ("G6 running 直连 os.kill",
         _SRC.replace(_ANCHOR_RUN, "        os.kill(self.pid, 0)\n        return True", 1),
         "G6"),
        # 规则 G4：删掉 pid <= 0 早退
        ("G4 删掉 pid <= 0 早退",
         _SRC.replace(_ANCHOR_123, "", 1), "G4"),
        # 规则 G7：删掉 Windows 0/1 拒绝
        ("G7 删掉 sig in (0,1) 拒绝",
         _SRC.replace(_ANCHOR_SIG, "", 1), "G7"),
    ]
    for label, mutated, expect in _muts:
        check_true("[2m] 变异确实改变了源码（%s）" % label, mutated != _SRC)
        codes = [c for c, _ in audit_apptrader(mutated)]
        check_true("[2m] 护栏报出 %s（%s）" % (expect, label), expect in codes)
        if expect not in codes:
            print("        实际报出: %r" % (codes,))

    # 规则 G8：新增一个无平台守卫的函数里 os.kill(pid, 0)
    _g8_src = _SRC + ("\n\ndef _legacy_alive(pid):\n"
                      "    try:\n"
                      "        os.kill(pid, 0)\n"
                      "        return True\n"
                      "    except OSError:\n"
                      "        return False\n")
    _g8 = [c for c, _ in audit_kill_calls(_g8_src, "AppTrader.py")]
    check_true("[2m] 护栏报出 G8（新增无守卫函数的 os.kill(pid, 0)）", "G8" in _g8)
    if "G8" not in _g8:
        print("        实际报出: %r" % (_g8,))

    # 规则 G9：手工控制台事件调用
    _g9_src = _SRC + ("\n\ndef _broadcast():\n"
                      "    import ctypes\n"
                      "    ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0)\n")
    _g9 = [c for c, _ in audit_kill_calls(_g9_src, "AppTrader.py")]
    check_true("[2m] 护栏报出 G9（手工 GenerateConsoleCtrlEvent）", "G9" in _g9)
    if "G9" not in _g9:
        print("        实际报出: %r" % (_g9,))

    # 反向自证：现行源码在通用规则下也必须零违规（防 G8 误报正常写法）
    _g = [c for c, _ in audit_kill_calls(_SRC, _APP_TRADER)]
    check("[2n] 现行源码在通用规则下零违规（区分能力来自守卫，不是误报）",
          _g, [])

# ── [3] 全仓护栏 ─────────────────────────────────────────────────────────
print("\n[3] 全仓护栏：App/ + Trading/ 下字面量 0/1 的 os.kill 与手工控制台事件")
if not _REPO:
    skip("[3] 未定位到仓库根（可用 CHAN_REPO 指定）")
else:
    _scanned = 0
    _bad = []
    for _base in ("App", "Trading"):
        _dir = os.path.join(_REPO, _base)
        if not os.path.isdir(_dir):
            continue
        for _dp, _dn, _fn in os.walk(_dir):
            _dn[:] = [d for d in _dn if d not in ("__pycache__", ".venv")]
            for _f in _fn:
                if not _f.endswith(".py"):
                    continue
                _p = os.path.join(_dp, _f)
                try:
                    with open(_p, encoding="utf-8", errors="replace") as fh:
                        _txt = fh.read()
                    _bad.extend(audit_kill_calls(_txt, os.path.relpath(_p, _REPO)))
                    _scanned += 1
                except SyntaxError as e:
                    _bad.append(("G0", "%s 语法错误: %s"
                                 % (os.path.relpath(_p, _REPO), e)))
    print("  扫描 %d 个 .py（App/ + Trading/，不含 __pycache__）" % _scanned)
    check("[3a] 零违规", [c for c, _ in _bad], [])
    for code, msg in _bad:
        print("        · %s %s" % (code, msg))

# ── [4]/[5]/[6] 运行时护栏 ───────────────────────────────────────────────
if not _HAS_APP:
    print("\n[4][5][6] 运行时护栏")
    skip("[4] 无 App/AppTrader.py")
    skip("[5] 无 App/AppTrader.py")
    skip("[6] 无 App/AppTrader.py")
else:
    from App import AppTrader as AT  # noqa: E402

    _FLAGS = 0
    if os.name == "nt":
        _FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) \
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _spawn(code, **extra):
        """起一个独立子进程：devnull 三件套（不占 runner 的管道）+
        新建进程组（免被 Ctrl+C 波及）。"""
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
        if _FLAGS:
            kw["creationflags"] = _FLAGS
        kw.update(extra)
        return subprocess.Popen([sys.executable, "-c", code], **kw)

    _live = _spawn("import time; time.sleep(60)")
    _dead = _spawn("pass")
    _dead.wait()

    print("\n[4] 行为护栏：_pid_alive 的返回值")
    _probes = [
        ("自己的 pid（活）", os.getpid(), True),
        ("存活子进程 pid（活）", _live.pid, True),
        ("已退出子进程 pid（死）", _dead.pid, False),
        ("pid=0（Windows 上=广播整个控制台）", 0, False),
        ("pid=-1", -1, False),
        ("不存在的 pid", 999999999, False),
    ]
    for _label, _pid, _want in _probes:
        try:
            _got = AT._pid_alive(_pid)
        except BaseException as e:                      # noqa: BLE001
            _got = "%s: %s" % (type(e).__name__, e)
        check("[4] %s → %r" % (_label, _want), _got, _want)

    print("\n[5] 零副作用护栏：os.kill / GenerateConsoleCtrlEvent 调用计数必须为 0")
    _hits = []

    class _Spy:
        def __init__(self):
            self._undo = []

        def install(self):
            _real_kill = os.kill
            _block = (os.name == "nt")

            def _kill(pid, sig=None, *a, **kw):
                _hits.append("os.kill(%r, %r)" % (pid, sig))   # guard-allow: 这是计数桩，转发前先记账
                if _block and sig in (0, 1):
                    return None          # 绝不转发：即使护栏失效也不投真 Ctrl+C
                return _real_kill(pid, sig, *a, **kw)
            _kill._guard_is_spy = True
            os.kill = _kill
            self._undo.append(lambda: setattr(os, "kill", _real_kill))

            self.gcce_pinned = False
            if os.name == "nt":
                import ctypes
                _k32 = ctypes.windll.kernel32
                _real_gcce = _k32.GenerateConsoleCtrlEvent

                def _gcce(sig, grp):
                    _hits.append("GenerateConsoleCtrlEvent(%r, %r)" % (sig, grp))
                    return 0
                _gcce._guard_is_spy = True
                _k32.GenerateConsoleCtrlEvent = _gcce
                self.gcce_pinned = True
                self._undo.append(
                    lambda: setattr(_k32, "GenerateConsoleCtrlEvent", _real_gcce))
            return self

        def uninstall(self):
            for fn in reversed(self._undo):
                fn()
            self._undo = []

    _spy = _Spy().install()
    try:
        del _hits[:]
        AT.os.kill(os.getpid(), 0)     # guard-allow: 正控需真调一次，验证桩在记账
        check("[5a] 计数桩生效（正控：手工调用被记 1 次）", len(_hits), 1)
        if os.name == "nt":
            import ctypes
            check_true("[5b] GenerateConsoleCtrlEvent 已被钉住（不会真广播）",
                       _spy.gcce_pinned
                       and getattr(ctypes.windll.kernel32.GenerateConsoleCtrlEvent,
                                   "_guard_is_spy", False))
        else:
            skip("[5b] 非 Windows：无控制台事件 API 需要钉住")

        del _hits[:]
        for _label, _pid, _want in _probes:
            AT._pid_alive(_pid)
        if os.name == "nt":
            check("[5c] 存活探测全程零 os.kill / 零控制台事件调用",
                  list(_hits), [])
        else:
            check("[5d] 非 Windows：仅在 pid > 0 的探测上走 os.kill(pid, 0)（POSIX 正解）",
                  sorted(_hits),
                  sorted("os.kill(%r, 0)" % p for _, p, w in _probes if p > 0))

        print("\n[6] 调用链护栏：_TraderProc.running（p20:577 的触发场景）")

        class _LiveProc:
            """复刻 p20 的 _FakeProc：pid 指向"活着的自己"。"""
            def __init__(self, pid):
                self.pid = pid
                self.args = []

            def poll(self):
                return None

            def send_signal(self, sig):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            def communicate(self, input=None, timeout=None):
                return (b"", b"")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            @property
            def returncode(self):
                return 0

        class _ExitedProc:
            def __init__(self, pid):
                self.pid = pid

            def poll(self):
                return 1     # 已退出 → poll 短路，不该再去做存活探测

        del _hits[:]
        _mk = dict(out_dir=os.path.join(_REPO, "Trading"),
                   started_at="2026-09-19 12:00:00", broker="dry_run",
                   symbol="KQ.m@CFFEX.IF", freq="5m", sse_base="")
        _tp_self = AT._TraderProc(_LiveProc(os.getpid()), **_mk)
        check_true("[6a] 自己的 pid → running=True（p20:577 的意图）",
                   _tp_self.running)
        _tp_live = AT._TraderProc(_LiveProc(_live.pid), **_mk)
        check_true("[6b] 存活子进程 → running=True", _tp_live.running)
        _tp_dead = AT._TraderProc(_LiveProc(_dead.pid), **_mk)
        check("[6c] 已退出子进程 → running=False", _tp_dead.running, False)
        _tp_zero = AT._TraderProc(_LiveProc(0), **_mk)
        check("[6d] pid=0 → running=False（旧写法此处广播整控制台）",
              _tp_zero.running, False)
        _tp_polled = AT._TraderProc(_ExitedProc(os.getpid()), **_mk)
        check("[6e] poll() 已返回 → running=False（短路，不再探测）",
              _tp_polled.running, False)
        if os.name == "nt":
            check("[6f] 整条调用链零 os.kill / 零控制台事件调用", list(_hits), [])
        else:
            skip("[6f] 非 Windows：POSIX 分支本就走 os.kill(pid, 0)")

        print("\n[7] 运行时变异自证：换回旧实现，[5]/[6] 的零副作用断言必须报出来")
        _old_ns = {"os": os}
        exec("def _old_pid_alive(pid):\n"
             "    if pid <= 0:\n"
             "        return False\n"
             "    try:\n"
             "        os.kill(pid, 0)\n"
             "        return True\n"
             "    except OSError:\n"
             "        return False\n", _old_ns)
        _old_alive = _old_ns["_old_pid_alive"]
        _saved_alive = AT._pid_alive
        try:
            AT._pid_alive = _old_alive
            del _hits[:]
            AT._pid_alive(os.getpid())
            check_true("[7a] 旧实现下 os.kill 被计数桩抓到（[5c] 会因此失败）",
                       len(_hits) >= 1)
            del _hits[:]
            _tp_old = AT._TraderProc(_LiveProc(os.getpid()), **_mk)
            _old_running = _tp_old.running
            check_true("[7b] 旧实现下 running 链路也会调 os.kill（[6f] 会因此失败）",
                       len(_hits) >= 1)
            print("        （参考值：本环境旧实现 running(自己的 pid) = %r，"
                  "真控制台里它返回 True 的同时会投出一次 Ctrl+C）" % (_old_running,))
            AT._pid_alive = _saved_alive
            del _hits[:]
            AT._pid_alive(os.getpid())
            check("[7c] 换回新实现后同一探测回到零调用（差异确由实现决定）",
                  list(_hits), [])
        finally:
            AT._pid_alive = _saved_alive
    finally:
        _spy.uninstall()
        for _p in (_live, _dead):
            try:
                if _p.poll() is None:
                    _p.kill()
                _p.wait()
            except Exception:                               # noqa: BLE001
                pass

print("\n" + "=" * 60)
print("test_apptrader_pid_alive_guards 结果: {} 通过 / {} 失败 / {} 跳过"
      .format(_PASS, _FAIL, _SKIP))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
