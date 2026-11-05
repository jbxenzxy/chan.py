"""AKShare 数据源「顶层零重依赖」防回潮护栏（2026-11-05）。

背景：`DataAPI/AkshareAPI.py` 顶层曾 `import akshare`，而服务启动链
（`FrontAPI → App/AppOrch → AppRefresh / TdxAPI`）只消费 `fetch_index_cons`
与两个映射常量。样本值（作者机器 2026-11-05，Python 3.14.7 / akshare 1.20.27，
暖盘）：启动链 `import App.AppOrch` 1537 ms 里 561 ms 花在 akshare 上（36%）；
本模块导入 1.37 s → 下沉后 0.05 s；扫描池 spawn worker 首次任务 1.60~1.80 s
→ 1.09~1.15 s（默认 workers ≤ 4，逐个重付）；模块数 −509、RSS −20 MB/进程。

改动把 akshare / pandas 下沉为函数内惰性 import（`_ak()` 统一入口），文件头
docstring 写下这条约定。本护栏让**约定本身可执行**：既有的 phase4 / phase5
import 门禁只审「消费侧 import 了哪些 `DataAPI.*`」与「DataAPI 是否反向依赖
App」，都不审某个 DataAPI 模块自身的顶层 import 清单 ⇒ 谁把 `import akshare`
加回顶层，门禁不会红，这条约定等于没有。

五项判据：
  ① 模块级 import 集合**恰好**是白名单（logging / Common + 相对
     .CommonStockAPI）—— 顶层新增任何 import 都会红，须回来说明理由再同步白名单；
  ② 全文件 `import akshare` **恰好 1 处**，且只在 `_ak()` 函数体内（唯一入口）；
  ③ 三个消费方 `fetch_index_cons` / `get_kl_data` / `do_init` 都不自己 import，
     而是调用 `_ak()`（以后加版本校验 / 缓存 / 遥测只改一处）；
  ④ `create_item_dict` 的 pandas 是函数内 import（局部依赖，不靠模块全局 pd）；
  ⑤ 运行时硬证据：全新子进程 `import DataAPI.AkshareAPI` 后 `akshare` 不在
     `sys.modules` —— 静态判据抓不到「被别人的导入链带进来」，这条直接盯症状。

「模块级」的判定口径：`ast` 中未被 `FunctionDef / AsyncFunctionDef / Lambda`
包住的 `Import` / `ImportFrom` 都算 —— 含 `try:` / `if:` 块内的，以及类体内的
（类体在类创建期执行，同样是导入期成本）。原始写法 `try: import akshare /
except ImportError: ak = None` 正是靠这个口径才抓得到。

变异实证（2026-11-05 实测四组，改坏 → 跑 → 逐字节还原）：
  · 顶层加回 `import akshare` → ① ② ⑤ 红（⑤ 也红：真加载了）；
  · `fetch_index_cons` 改回 `import akshare as ak` → ② ③ 红；
  · 删掉 `create_item_dict` 的函数内 `import pandas` → ④ 红；
  · 顶层加 `import pandas as pd` → ① 红；
  · 还原后 5/5 绿（文件 md5 与改前一致）。
"""
import ast
import os
import subprocess
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TARGET_REL = os.path.join("DataAPI", "AkshareAPI.py")
_TARGET = os.path.join(_REPO_ROOT, _TARGET_REL)

# ① 模块级 import 白名单：顶层包名；相对导入记为 ".模块名"
_ALLOWED_MODULE_IMPORTS = {"logging", "Common", ".CommonStockAPI"}

# ③ 必须经 _ak() 取数、不得自己 import akshare 的消费方
_AK_CONSUMERS = ("fetch_index_cons", "get_kl_data", "do_init")

results = []


def rec(no, title, ok, detail=""):
    results.append((bool(ok), no, title, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + f"{no} {title}"
          + ("" if ok else f"    {detail}"))


def _module_level_imports(tree):
    """导入期会执行的 import（未被函数 / lambda 包住），含 try / if / 类体内的。"""
    out = []
    stack = [(tree, False)]
    while stack:
        node, in_func = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import) and not in_func:
                for a in child.names:
                    out.append((a.name.split(".")[0], child.lineno))
            elif isinstance(child, ast.ImportFrom) and not in_func:
                if child.level:
                    out.append(("." * child.level + (child.module or ""), child.lineno))
                else:
                    out.append(((child.module or "").split(".")[0], child.lineno))
            stack.append((child, in_func or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))))
    return out


def _akshare_imports(tree):
    """全文件每一处 `import akshare`：(所属函数名 | None, 行号)。"""
    out = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Import) and any(
                    a.name.split(".")[0] == "akshare" for a in child.names):
                out.append((func, child.lineno))
            elif isinstance(child, ast.ImportFrom) and (
                    child.module or "").split(".")[0] == "akshare":
                out.append((func, child.lineno))
            walk(child, name)

    walk(tree, None)
    return out


def _funcs(tree, names):
    found = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            found.setdefault(n.name, n)
    return found


def _has_import(node, top):
    for n in ast.walk(node):
        if isinstance(n, ast.Import) and any(
                a.name.split(".")[0] == top for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == top:
            return True
    return False


def _calls(node, name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == name for n in ast.walk(node))


def main():
    print("AKShare 数据源「顶层零重依赖」防回潮护栏")
    print("=" * 68)
    print(f"目标文件：{_TARGET_REL}")

    if not os.path.isfile(_TARGET):
        rec("①", "目标文件存在", False, _TARGET)
        print("=" * 68)
        print("合计 1 项，通过 0，失败 1")
        return 1

    with open(_TARGET, encoding="utf-8", newline="") as f:
        src = f.read()
    tree = ast.parse(src)

    # ── ① 模块级 import 恰好是白名单 ────────────────────────────────
    top = _module_level_imports(tree)
    got = {name for name, _ln in top}
    rec("①", f"模块级 import 集合 == 白名单 {sorted(_ALLOWED_MODULE_IMPORTS)}",
        got == _ALLOWED_MODULE_IMPORTS,
        f"got={sorted(got)} extra={sorted(got - _ALLOWED_MODULE_IMPORTS)} "
        f"missing={sorted(_ALLOWED_MODULE_IMPORTS - got)} "
        f"行号={[ln for _n, ln in top]}")

    # ── ② `import akshare` 恰好 1 处且只在 _ak() 内 ─────────────────
    aks = _akshare_imports(tree)
    rec("②", "全文件 import akshare 恰好 1 处，且只在 _ak() 函数体内",
        len(aks) == 1 and aks[0][0] == "_ak",
        f"命中={aks}（要求仅 1 处且所属函数为 _ak）")

    # ── ③ 三个消费方都经 _ak()，不自己 import ───────────────────────
    fns = _funcs(tree, set(_AK_CONSUMERS) | {"create_item_dict", "_ak"})
    bad = []
    for name in _AK_CONSUMERS:
        fn = fns.get(name)
        if fn is None:
            bad.append(f"{name}: 函数未找到")
        elif _has_import(fn, "akshare"):
            bad.append(f"{name}: 内部直接 import akshare")
        elif not _calls(fn, "_ak"):
            bad.append(f"{name}: 未调用 _ak()")
    rec("③", f"{_AK_CONSUMERS} 均经 _ak() 取 akshare，无人自己 import",
        not bad, "; ".join(bad))

    # ── ④ create_item_dict 的 pandas 是函数内 import ────────────────
    cid = fns.get("create_item_dict")
    rec("④", "create_item_dict 内部 import pandas（不依赖模块级 pd）",
        cid is not None and _has_import(cid, "pandas"),
        "create_item_dict 未找到或无函数内 pandas import")

    # ── ⑤ 运行期硬证据：新进程 import 后 akshare 不在 sys.modules ────
    probe = ("import sys; import DataAPI.AkshareAPI; "
             "print('AK_LOADED=%s' % ('akshare' in sys.modules))")
    env = os.environ.copy()
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT,
                              capture_output=True, text=True, env=env, timeout=120)
        ok5 = proc.returncode == 0 and "AK_LOADED=False" in proc.stdout
        detail = (f"rc={proc.returncode} out={proc.stdout.strip()!r} "
                  f"err={(proc.stderr or '').strip()[-200:]!r}")
    except Exception as e:  # noqa: BLE001 —— 护栏自身不得因探针异常而 traceback
        ok5, detail = False, f"{type(e).__name__}: {e}"
    rec("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
        ok5, detail)

    print("=" * 68)
    failed = [r for r in results if not r[0]]
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for _ok, no, title, _d in failed:
        print(f"  FAIL {no} {title}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
