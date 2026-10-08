# -*- coding: utf-8 -*-
"""
Test/test_undefined_global_guard.py —— 未定义全局名静态护栏（防复潮）
=======================================================================
背景（2026-10-08 IM 选点 NameError 事故）：`App/AppSSE.futures_manual_select_point`
在 db6f886（2026-10-03）把「定位」段的
    config = _make_chan_config()
    chan, kl_type = _build_futures_chan(..., config=config, src=src)
换成了 `init_chan_symbol(...)`，局部变量 `config` 随之消失；而下方 Step 4 的
    chan2, _ = _build_futures_chan(records2, symbol, freq_sec, config=config, src=src2)
留在原地 → 调用即 `NameError: name 'config' is not defined`。

为什么门禁全绿也拦不住：`NameError` 只在**真正跑到那一行**时才抛。该函数体
前半段要连天勤拉历史、建链、定位左肩，静态单测与桩驱动都进不到 Step 4；而
`config` 是一个合法标识符，`py_compile` / AST 语法检查一律放过——门禁里
（含 `compileall` 类组件）根本没有「未定义全局名」这一维度。

本组件补上该维度，判据（可执行，不读注释）：
  对仓库内每个 `.py`，取编译后的每个 code object 的字节码，凡是
  `LOAD_GLOBAL <name>` 且 `<name>` 既不在**模块级绑定名**集合、也不在内置名
  集合 → 记一处「未定义全局名」，命中即红。

要点：模块级绑定名只从 `tree.body` 顶层（含 if/try/for/while/with 的模块级
语句）收集，**不进入函数/类体**——否则某函数里的局部 `config` 会把真缺陷
掩盖掉（本组件实现过程中就踩过这个坑，见 `test_detector_no_false_negative`）。

自证（变异敏感，两条）：
  ① 把历史的 `config=config` 片段喂给检测器 → 必须命中（否则检测器是摆设）；
  ② 对「局部变量与全局同名」的样本必须**不**误报，且**不得**把局部名算进
     模块级绑定名（防有人把收集范围放宽成 `ast.walk(tree)` 后恒绿）。

运行：python Test/test_undefined_global_guard.py
"""
import ast
import builtins
import dis
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)

SKIP_PARTS = (".venv", "__pycache__", "node_modules", "/build/", "/dist/")

_BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__builtins__", "__loader__", "__debug__",
}

# PEP 649（Python 3.14+ 惰性注解）由**解释器**注入的内部名，不出现在源码里。
# 3.14 起带注解的模块会生成 `__annotate__` 函数；当注解值形态需要惰性求值时，
# 编译器额外绑定 `__conditional_annotations__` 单元格，并在 `__annotate__` 内
# 以 `LOAD_GLOBAL __conditional_annotations__` 读回 ⇒ 被本检测器当成
# 「未定义全局名」。实测（2026-10-08，CPython 3.14.7）该名字**不携带任何源码
# 缺陷**：`DataAPI/TqSdkAPI.py` 的 `_futures_lookback_config: dict = {}` 处
# 命中的这一个名字，import 该模块成功、`__annotate__(1)` 正常返回
# `{'_futures_lookback_config': dict}`，运行时不抛 `NameError`。
# 故按「解释器内建」对待，与本文件既有的 `__name__/__file__` 一族同列。
# 这不放宽缺陷检测面：全仓扫描命中集仅此一名（见自证③）。
_INTERPRETER_INJECTED = {
    "__conditional_annotations__",
}

_BUILTINS |= _INTERPRETER_INJECTED

# 事故锚点：`futures_manual_select_point` 的 Step 4 调用点符号（防回潮点名）
ANCHOR_FUNC = "futures_manual_select_point"
ANCHOR_SYMBOL = "config"


def module_level_names(tree):
    """收集**模块级**绑定名（不进入函数/类体）。

    只认模块作用域真正会绑定名字的语句：import / def / class / 赋值 /
    for 目标 / with as 目标 / except as 名 / global 声明；这些语句出现在
    模块级 if/try/for/while/with 里时同样递归收取。函数与类**只收名字本身**，
    其函数体一律不入——这是本检测器判别力的来源。
    """
    names = set()

    def add_target(target):
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                names.add(node.id)

    def scan_body(stmts):
        for node in stmts:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    add_target(target)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                add_target(node.target)
            elif isinstance(node, ast.For):
                add_target(node.target)
                scan_body(node.body)
                scan_body(node.orelse)
            elif isinstance(node, (ast.While, ast.If)):
                scan_body(node.body)
                scan_body(node.orelse)
            elif isinstance(node, ast.Try):
                scan_body(node.body)
                scan_body(node.orelse)
                scan_body(node.finalbody)
                for handler in node.handlers:
                    if handler.name:
                        names.add(handler.name)
                    scan_body(handler.body)
            elif isinstance(node, ast.With):
                for item in node.items:
                    if item.optional_vars is not None:
                        add_target(item.optional_vars)
                scan_body(node.body)
            elif isinstance(node, ast.Global):
                names.update(node.names)

    scan_body(tree.body)
    return names


def find_undefined_globals(source, filename="<src>"):
    """返回 [(行号, 名字, code object 路径)]；空列表 = 无未定义全局名。"""
    tree = ast.parse(source, filename)
    defined = module_level_names(tree) | _BUILTINS
    code = compile(tree, filename, "exec")

    hits = []

    def walk(co, path):
        for ins in dis.get_instructions(co):
            if ins.opname == "LOAD_GLOBAL" and ins.argval not in defined:
                line = ins.positions.lineno if ins.positions else None
                hits.append((line, ins.argval, path))
        for const in co.co_consts:
            if hasattr(const, "co_code"):
                walk(const, path + "/" + (const.co_name or "?"))

    walk(code, "<module>")
    return hits


def iter_repo_py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = dirpath.replace("\\", "/")
        if any(part in rel_dir for part in SKIP_PARTS):
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in (".venv", "__pycache__", "node_modules")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name), rel_dir


def test_repo_has_no_undefined_global():
    """全仓扫描：任何一处未定义全局名即红，并点名文件:行:名字。"""
    bad = []
    n_files = 0
    for path, _rel_dir in iter_repo_py_files(REPO_ROOT):
        rel = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                source = fh.read().replace("\r\n", "\n")
            hits = find_undefined_globals(source, rel)
        except SyntaxError as e:
            bad.append((rel, e.lineno, f"SyntaxError: {e.msg}"))
            continue
        n_files += 1
        for line, name, scope in hits:
            bad.append((rel, line, f"{name}  ← {scope}"))
    assert not bad, (
        "存在未定义全局名（运行时必抛 NameError）：\n    "
        + "\n    ".join(f"{f}:{ln}: {msg}" for f, ln, msg in bad)
    )
    assert n_files > 50, f"扫描文件数异常（{n_files}），疑似根目录解析错误"
    print(f"[PASS] 全仓 {n_files} 个 .py 无未定义全局名")


def test_anchor_call_site_has_no_config_kwarg():
    """点名锚点：Step 4 的 `_build_futures_chan` 调用不得再出现 `config=config`。

    与上一条互补——全仓扫描防一切形态，本条钉死事故点，红的时候能一眼认出
    「就是当年那一行又回来了」。锚点用函数名 + 调用目标（稳定锚点，不用行号）。
    """
    path = os.path.join(REPO_ROOT, "App", "AppSSE.py")
    with open(path, encoding="utf-8", newline="") as fh:
        source = fh.read().replace("\r\n", "\n")
    tree = ast.parse(source, path)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == ANCHOR_FUNC:
            target = node
            break
    assert target is not None, f"未找到 {ANCHOR_FUNC}（锚点失效，请更新锚点）"
    offenders = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if not (isinstance(callee, ast.Name) and callee.id == "_build_futures_chan"):
            continue
        for kw in node.keywords:
            if kw.arg == ANCHOR_SYMBOL and isinstance(kw.value, ast.Name) \
                    and kw.value.id == ANCHOR_SYMBOL:
                offenders.append(node.lineno)
    assert not offenders, (
        f"{ANCHOR_FUNC} 内 _build_futures_chan({ANCHOR_SYMBOL}={ANCHOR_SYMBOL}) "
        f"复潮，行 {offenders}（局部变量已不存在 → NameError）")
    print(f"[PASS] {ANCHOR_FUNC} 无 config=config 自引用")


def test_detector_catches_regression_snippet():
    """自证①：当年的事故片段喂进来必须命中。"""
    snippet = (
        "def f(records2, src2):\n"
        "    chan2, _ = _build_futures_chan(records2, 'X', 60, config=config, src=src2)\n"
        "    return chan2\n"
    )
    hits = find_undefined_globals(snippet, "<regression-snippet>")
    names = {name for _ln, name, _scope in hits}
    # 片段里 _build_futures_chan 也是未定义名（此处不关心），只钉 config 必须被命中
    assert "config" in names, f"自证失败：检测器未抓到 config（命中 {hits}）"
    print("[PASS] 检测器自证①: 事故片段 config=config 被抓到")


def test_detector_no_false_negative():
    """自证②：函数内局部变量与全局同名时，必须仍报未定义全局名。

    若有人把模块级绑定名收集放宽成 `ast.walk(tree)`（把函数局部也算进来），
    本样本会变绿——故本用例是「收集范围」的棘轮。
    """
    snippet = (
        "def a():\n"
        "    config = 1\n"          # 另一处函数的局部变量，不得污染模块级判定
        "    return config\n"
        "\n"
        "def futures_manual_select_point(records2, src2):\n"
        "    chan2, _ = _build_futures_chan(records2, 'X', 60, config=config, src=src2)\n"
        "    return chan2\n"
    )
    defined = module_level_names(ast.parse(snippet))
    assert "config" not in defined, "自证失败：函数局部名被误收为模块级绑定名"
    hits = find_undefined_globals(snippet, "<scope-sample>")
    names = {name for _ln, name, _scope in hits}
    # 片段里 _build_futures_chan 同为未定义名（此处不关心），关键在 config 不得被掩盖
    assert "config" in names, \
        f"自证失败：同名局部变量掩盖了缺陷（命中 {hits}）"
    print("[PASS] 检测器自证②: 同名局部不掩盖（收集范围未被放宽）")


def test_whitelist_is_exactly_pep649_and_does_not_widen():
    """自证③：白名单恰为「PEP 649 解释器内部名」这一族，不得被放宽。

    钉两条（缺一不可）：
      ① 白名单集合必须**恰好**是已登记的那几个名字 —— 有人往里塞任意名字
         （例如把某个真缺陷名加进来「修绿」）时立刻红；
      ② 真缺陷名**不在**白名单里，且注入该名的等价源码仍被命中 —— 证明
         放行的这一族与「缺陷名」互不相干。

    ①的判别力：白名单是硬编码集合，若被扩成 `_BUILTINS |= {"config"}` 之类，
    断言 ① 立即失败。
    ②的判别力：若有人把真缺陷名（或它的别名）混进 `_INTERPRETER_INJECTED`，
    断言 ② 的第一条失败；若有人把 `module_level_names` 放宽成 `ast.walk`，
    本文件的 `test_detector_no_false_negative` 会先红。

    说明：白名单的语义就是「这些名字一律视为已定义」，故**源码里显式写它**同样
    不报 —— 这不是漏洞，而是 `_BUILTINS` 与 `__name__`/`__file__` 一族的既定
    口径。真正要防的是「把非注入名塞进白名单」，故断言落在集合本身。
    """
    expected = {"__conditional_annotations__"}
    # 若解释器升级后内部名集合变化，此断言会红 —— 那时须人工确认后再更新本集合。
    assert _INTERPRETER_INJECTED == expected, (
        f"白名单集合被改动：{sorted(_INTERPRETER_INJECTED)} ≠ {sorted(expected)}；"
        "该集合只应登记 PEP 649 解释器注入名，新增前须人工确认它不是真源码符号")

    # ②a 事故锚点符号不得出现在白名单里（真缺陷不得被「修绿」）
    assert ANCHOR_SYMBOL not in _INTERPRETER_INJECTED, (
        f"事故锚点符号 {ANCHOR_SYMBOL!r} 被塞进白名单 —— 检测器对本类缺陷已失效")
    # ②b 注入名之外的普通未定义名仍必须被命中（口径未被整体放宽）
    snippet = (
        "def f():\n"
        "    return __undefined_name_probe__\n"
        "\n"
        "f()\n"
    )
    hits = find_undefined_globals(snippet, "<whitelist-sample>")
    names = {name for _ln, name, _scope in hits}
    assert "__undefined_name_probe__" in names, (
        "自证失败：注入名之外的普通未定义名未被命中 —— 口径被整体放宽"
        f"（命中 {hits}）")

    # ③ 真实注解写法必须干净（与 TqSdkAPI.py:212 同形），证明该项放行是必要的
    annotated = (
        "from typing import Optional\n"
        "_cfg: dict = {}\n"
        "_x: Optional[int] = None\n"
    )
    assert not find_undefined_globals(annotated, "<annotated-sample>"), (
        "自证失败：正常的模块级注解写法被判为未定义全局名")
    print("[PASS] 检测器自证③: 白名单恰为 PEP 649 内部名，未放宽检测口径")


def main():
    test_repo_has_no_undefined_global()
    test_anchor_call_site_has_no_config_kwarg()
    test_detector_catches_regression_snippet()
    test_detector_no_false_negative()
    test_whitelist_is_exactly_pep649_and_does_not_widen()
    print("ALL 未定义全局名静态护栏 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
