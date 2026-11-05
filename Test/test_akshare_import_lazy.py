"""AKShare 数据源「顶层零重依赖」防回潮护栏（2026-11-05）。

背景：`DataAPI/AkshareAPI.py` 顶层曾 `import akshare`，而服务启动链
（`FrontAPI → App/AppOrch → AppRefresh / TdxAPI`）只消费 `fetch_index_cons`
与两个映射常量。样本值（作者机器 2026-11-05，Python 3.14.7 / akshare 1.20.27，
暖盘）：启动链 `import App.AppOrch` 1537 ms 里 561 ms 花在 akshare 上（36%）；
本模块导入 1.37 s → 0.05 s；扫描池 spawn worker 首次任务 1.60~1.80 s
→ 1.09~1.15 s（默认 workers ≤ 4，逐个重付）；模块数 −509、RSS −20 MB/进程。

改动把 akshare / pandas 下沉为函数内惰性 import（`_ak()` 统一入口），文件头
docstring 写下这条约定。本护栏让**约定本身可执行**：既有的 phase4 / phase5
import 门禁只审「消费侧 import 了哪些 `DataAPI.*`」与「DataAPI 是否反向依赖
App」，都不审某个 DataAPI 模块自身的顶层 import 清单 ⇒ 谁把 `import akshare`
加回顶层，门禁不会红，这条约定等于没有。

五项判据：
  ① 模块级 import 集合**恰好**是白名单（logging / Common + 相对
     .CommonStockAPI）—— 顶层新增任何 import 都会红，须回来说明理由再同步白名单；
  ② 全文件 `import akshare` **恰好 1 处**，且只在 `_ak()` 函数体内；
  ③ 三个消费方 `fetch_index_cons` / `get_kl_data` / `do_init` 一律 `ak = _ak()`：
     `_ak()` 调用**恰好 1 次**、必须就是绑定 `ak` 的那一次，且函数内不存在
     `__import__` / `importlib` / `sys.modules` / `import akshare` 等绕过路径；
  ④ `create_item_dict` 的 pandas 是函数内 import（局部依赖，不靠模块全局 pd）；
  ⑤ 运行时硬证据：全新子进程 `import DataAPI.AkshareAPI` 后 `akshare` 不在
     `sys.modules`。未装 akshare 的环境**降级为 SKIP**：那时根本没有东西可加载，
     不能把 `AK_LOADED=False` 读成「惰性生效」；①②③④ 静态判据仍生效。

「模块级」的判定口径：`ast` 中未被 `FunctionDef / AsyncFunctionDef / Lambda`
包住的 `Import` / `ImportFrom` 都算 —— 含 `try:` / `if:` 块内的，以及类体内的
（类体在类创建期执行，同样是导入期成本）。原始写法 `try: import akshare /
except ImportError: ak = None` 正是靠这个口径才抓得到。

③ 为什么不能只判「函数里出现过 _ak()」：消费方写成
`if False: _ak()` + `ak = __import__("akshare")` 时，死代码满足了「出现过」，
真取数却绕过了唯一入口 —— 顶层零重依赖与启动期收益都还在，但「以后加版本
校验 / 缓存 / 遥测只改一处」的约定已经失效。故 ③ 改判「恰好一次绑定」。
静态黑名单不可能穷尽所有反射手法，这一条的目标是把**已知绕过形态**钉死，
不是在数学意义上证明不存在绕过。

变异自证：`python Test/test_akshare_import_lazy.py --self-check`
  M1 顶层加回 import akshare          → ①②⑤
  M2 fetch_index_cons 改回直连 import  → ②③
  M3 删 create_item_dict 局部 pandas   → ④
  M4 顶层加 import pandas              → ①
  M5 import akshare 藏进类体            → ①②⑤
  M6 消费方 __import__ 绕过 + 死 _ak()  → ③
  M7 _ak() 改名、消费方仍调旧名          → ②③
  M8 DataAPI/__init__.py 顶层 import    → ⑤
  M9 顶层 try/except 容错（1105 原形）   → ①②⑤
逐组注入 → 单跑护栏 → 比对红集 → 逐字节还原并校验 sha256。
`--self-check` 会临时改写源文件，只在手工核对时用，不进门禁。
"""
import ast
import hashlib
import os
import re
import subprocess
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TARGET_REL = os.path.join("DataAPI", "AkshareAPI.py")
_TARGET = os.path.join(_REPO_ROOT, _TARGET_REL)
_INIT_REL = os.path.join("DataAPI", "__init__.py")

# ① 模块级 import 白名单：顶层包名；相对导入记为 ".模块名"
_ALLOWED_MODULE_IMPORTS = {"logging", "Common", ".CommonStockAPI"}

# ③ 必须经 _ak() 取数、不得自己 import akshare 的消费方
_AK_CONSUMERS = ("fetch_index_cons", "get_kl_data", "do_init")

# ③ 已知绕过动态导入的名字（静态黑名单，目标是把已知形态钉死）
_DYNAMIC_NAMES = {"__import__", "importlib"}
_DYNAMIC_ATTRS = {"import_module", "modules"}

results = []


def rec(no, title, ok, detail=""):
    results.append(("PASS" if ok else "FAIL", no, title, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + f"{no} {title}"
          + ("" if ok else f"    {detail}"))


def rec_skip(no, title, detail=""):
    results.append(("SKIP", no, title, detail))
    print(f"  [SKIP] {no} {title}    {detail}")


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


def _target_names(node):
    """赋值目标里的 Name 集合（覆盖 Assign / 循环 / with / 海象 / 推导式）。"""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        out = set()
        for e in node.elts:
            out |= _target_names(e)
        return out
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return set()


def _ak_bindings(fn, callee="_ak"):
    """(把 callee() 的返回值绑给 ak 的行号, 其它把 ak 绑到别处的行号)。"""
    good, other = [], []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            names = set()
            for t in n.targets:
                names |= _target_names(t)
            val = n.value
            if (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
                    and val.func.id == callee and names == {"ak"}):
                good.append(n.lineno)
            elif "ak" in names:
                other.append(n.lineno)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            if "ak" in _target_names(n.target):
                other.append(n.lineno)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            if "ak" in _target_names(n.target):
                other.append(n.lineno)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None and \
                        "ak" in _target_names(item.optional_vars):
                    other.append(n.lineno)
    return good, other


def _ak_calls(fn, callee="_ak"):
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == callee]


def _dynamic_hits(fn):
    """函数体内的动态导入 / 反射取模块痕迹。"""
    hits = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "__import__":
            hits.append(f"__import__@{n.lineno}")
        elif isinstance(n, ast.Name) and n.id in _DYNAMIC_NAMES:
            hits.append(f"{n.id}@{getattr(n, 'lineno', '?')}")
        elif isinstance(n, ast.Attribute) and n.attr in _DYNAMIC_ATTRS:
            hits.append(f".{n.attr}@{n.lineno}")
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) \
                else [n.module or ""]
            if any(m.split(".")[0] in ("importlib", "akshare") for m in mods):
                hits.append(f"import:{','.join(mods)}@{n.lineno}")
    return hits


def _check_consumer(name, fn, ak_defined):
    """③ 单个消费方的违规说明；返回 None 表示合规。"""
    if fn is None:
        return f"{name}: 函数未找到"
    if not ak_defined:
        return f"{name}: 本模块未定义 _ak()（入口被改名或删除）"
    if _has_import(fn, "akshare"):
        return f"{name}: 内部直接 import akshare"
    hits = _dynamic_hits(fn)
    if hits:
        return f"{name}: 动态导入 / 反射绕过 _ak() {hits}"
    calls = _ak_calls(fn)
    good, other = _ak_bindings(fn)
    if len(calls) != 1:
        return f"{name}: _ak() 调用 {len(calls)} 次（要求恰好 1 次）行={calls}"
    if good != calls:
        return f"{name}: _ak() 未绑定给 ak（要求 `ak = _ak()`）调用={calls} 绑定={good}"
    if other:
        return f"{name}: ak 另有绑定行={other}（只允许 `ak = _ak()` 这一处）"
    return None


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

    # ── ③ 三个消费方一律 `ak = _ak()`，无绕过 ───────────────────────
    fns = _funcs(tree, set(_AK_CONSUMERS) | {"_ak", "create_item_dict"})
    ak_defined = fns.get("_ak") is not None
    bad = [msg for msg in (_check_consumer(n, fns.get(n), ak_defined)
                           for n in _AK_CONSUMERS) if msg]
    rec("③", f"{_AK_CONSUMERS} 均以 `ak = _ak()` 绑定且恰好一次、无动态导入绕过",
        not bad, "; ".join(bad))

    # ── ④ create_item_dict 的 pandas 是函数内 import ────────────────
    cid = fns.get("create_item_dict")
    rec("④", "create_item_dict 内部 import pandas（不依赖模块级 pd）",
        cid is not None and _has_import(cid, "pandas"),
        "create_item_dict 未找到或无函数内 pandas import")

    # ── ⑤ 运行期硬证据：新进程 import 后 akshare 不在 sys.modules ────
    probe = (
        "import sys\n"
        "try:\n"
        "    import importlib.util as _u\n"
        "    _installed = _u.find_spec('akshare') is not None\n"
        "except Exception:\n"
        "    _installed = False\n"
        "print('AK_INSTALLED=%s' % _installed)\n"
        "import DataAPI.AkshareAPI\n"
        "print('AK_LOADED=%s' % ('akshare' in sys.modules))\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT,
                              capture_output=True, text=True, env=env, timeout=120)
        out = proc.stdout or ""
        err = (proc.stderr or "").strip()[-200:]
        if proc.returncode == 0 and "AK_INSTALLED=False" in out:
            rec_skip("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
                     "akshare 未安装：无东西可加载，本判据降级（不能据此判定惰性生效）；"
                     "①②③④ 静态判据仍生效")
        else:
            rec("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
                proc.returncode == 0 and "AK_LOADED=False" in out
                and "AK_INSTALLED=True" in out,
                f"rc={proc.returncode} out={out.strip()!r} err={err!r}")
    except Exception as e:  # noqa: BLE001 —— 护栏自身不得因探针异常而 traceback
        rec("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
            False, f"{type(e).__name__}: {e}")

    print("=" * 68)
    n_pass = sum(1 for r in results if r[0] == "PASS")
    n_skip = sum(1 for r in results if r[0] == "SKIP")
    n_fail = sum(1 for r in results if r[0] == "FAIL")
    if n_skip:
        print(f"合计 {len(results)} 项，通过 {n_pass}，跳过 {n_skip}，失败 {n_fail}")
    else:
        print(f"合计 {len(results)} 项，通过 {n_pass}，失败 {n_fail}")
    for st, no, title, _d in results:
        if st != "PASS":
            print(f"  {st} {no} {title}")
    return 1 if n_fail else 0


# ─────────────────────────── 变异自证 ───────────────────────────
# 每组：备份 → 注入 → 单跑本护栏 → 比对红集 → 逐字节还原并校验 sha256。
# 替换键带上下文且断言命中 1 次：源码一变动，自证会立刻报「替换点未命中」而不是静默放行。
_MUTATIONS = [
    {
        "id": "M1",
        "label": "顶层加回 import akshare",
        "edits": [(_TARGET_REL, "import logging\n\nfrom Common.CEnum",
                   "import logging\nimport akshare  # MUTATION-M1\n\nfrom Common.CEnum")],
        "red": ("①", "②", "⑤"),
    },
    {
        "id": "M2",
        "label": "fetch_index_cons 改回直接 import akshare",
        "edits": [(_TARGET_REL,
                   '    try:\n        ak = _ak()\n    except ImportError:\n'
                   '        log.info("[AKShare] 未安装',
                   '    try:\n        import akshare as ak  # MUTATION-M2\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("②", "③"),
    },
    {
        "id": "M3",
        "label": "删掉 create_item_dict 内的 import pandas",
        "edits": [(_TARGET_REL,
                   '    """将DataFrame行转换为K线单元所需的字典格式"""\n'
                   '    import pandas as pd\n',
                   '    """将DataFrame行转换为K线单元所需的字典格式"""\n')],
        "red": ("④",),
    },
    {
        "id": "M4",
        "label": "顶层加 import pandas",
        "edits": [(_TARGET_REL, "import logging\n\nfrom Common.CEnum",
                   "import logging\nimport pandas as pd  # MUTATION-M4\n\nfrom Common.CEnum")],
        "red": ("①",),
    },
    {
        "id": "M5",
        "label": "import akshare 藏进类体（模块级口径）",
        "edits": [(_TARGET_REL,
                   'class CAkshare(CCommonStockApi):\n    """使用 akshare 获取A股数据"""\n',
                   'class CAkshare(CCommonStockApi):\n'
                   '    """使用 akshare 获取A股数据"""\n'
                   '    import akshare  # MUTATION-M5\n')],
        "red": ("①", "②", "⑤"),
    },
    {
        "id": "M6",
        "label": "消费方 __import__ 绕过 _ak()，只留死代码 _ak() 调用",
        "edits": [(_TARGET_REL,
                   '    try:\n        ak = _ak()\n    except ImportError:\n'
                   '        log.info("[AKShare] 未安装',
                   '    try:\n        if False:\n            _ak()\n'
                   '        ak = __import__("akshare")  # MUTATION-M6\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("③",),
    },
    {
        "id": "M7",
        "label": "_ak() 改名，消费方仍调旧名",
        "edits": [(_TARGET_REL, "def _ak():\n", "def _ak_alt():\n")],
        "red": ("②", "③"),
    },
    {
        "id": "M8",
        "label": "DataAPI/__init__.py 顶层 import akshare（被别人带进来）",
        "edits": [(_INIT_REL, None, "import akshare  # MUTATION-M8\n")],
        "red": ("⑤",),
    },
    {
        "id": "M9",
        "label": "顶层 try/except 容错写法（1105 原始形态）",
        "edits": [(_TARGET_REL, "import logging\n\nfrom Common.CEnum",
                   "import logging\ntry:\n    import akshare\n    ak = None  # MUTATION-M9\n"
                   "except ImportError:\n    ak = None\n\nfrom Common.CEnum")],
        "red": ("①", "②", "⑤"),
    },
]

_MUTATION_FILES = sorted({e[0] for m in _MUTATIONS for e in m["edits"]})


def _read_bytes(rel):
    with open(os.path.join(_REPO_ROOT, rel), "rb") as f:
        return f.read()


def _write_bytes(rel, data):
    p = os.path.join(_REPO_ROOT, rel)
    tmp = p + ".selfcheck.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


def _drop_pyc(rel):
    d = os.path.join(_REPO_ROOT, os.path.dirname(rel), "__pycache__")
    stem = os.path.splitext(os.path.basename(rel))[0]
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        if n.startswith(stem + "."):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def _apply(rel, old, new):
    raw = _read_bytes(rel)
    text = raw.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    lf = text.replace("\r\n", "\n")
    if old is None:
        out = new + lf
    else:
        hits = lf.count(old)
        if hits != 1:
            raise AssertionError(f"{rel} 替换点命中 {hits} 次（要求 1 次）：{old!r}")
        out = lf.replace(old, new, 1)
    _write_bytes(rel, out.replace("\n", eol).encode("utf-8"))
    _drop_pyc(rel)


def _run_guard():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                          cwd=_REPO_ROOT, env=env, capture_output=True,
                          text=True, timeout=300)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    red = tuple(sorted(set(re.findall(r"\[FAIL\]\s*([①②③④⑤])", out))))
    return proc.returncode, red, out.strip()[-400:]


def _self_check():
    print("AKShare 护栏变异自证（M1~M9，逐组注入 → 单跑 → 逐字节还原）")
    print("=" * 68)
    print(f"目标文件：{', '.join(_MUTATION_FILES)}")

    try:
        import importlib.util
        if importlib.util.find_spec("akshare") is None:
            print("[ABORT] 未安装 akshare：M1/M5/M8/M9 期望 ⑤ 变红无法成立；"
                  "请先 pip install akshare 再自证")
            return 2
    except Exception as e:  # noqa: BLE001
        print(f"[ABORT] 探测 akshare 失败：{type(e).__name__}: {e}")
        return 2

    backup = {rel: _read_bytes(rel) for rel in _MUTATION_FILES}
    sha0 = {rel: hashlib.sha256(b).hexdigest() for rel, b in backup.items()}

    rc, red, tail = _run_guard()
    print(f"基线：rc={rc} 红集={list(red)}")
    if rc != 0 or red:
        print(f"[ABORT] 基线不绿，自证无意义：{tail}")
        return 1

    rows, bad = [], []
    try:
        for m in _MUTATIONS:
            try:
                for rel, old, new in m["edits"]:
                    _apply(rel, old, new)
            except AssertionError as e:
                rows.append((m["id"], m["label"], m["red"], "替换点未命中", False))
                bad.append(f"{m['id']} 替换点未命中: {e}")
                continue
            try:
                rc, red, _tail = _run_guard()
            finally:
                for rel in _MUTATION_FILES:
                    _write_bytes(rel, backup[rel])
                    _drop_pyc(rel)
            ok = red == m["red"] and rc != 0
            rows.append((m["id"], m["label"], m["red"], red, ok))
            if not ok:
                bad.append(f"{m['id']} 期望 {m['red']} 实测 {red} rc={rc}")
    finally:
        for rel in _MUTATION_FILES:
            _write_bytes(rel, backup[rel])
            _drop_pyc(rel)

    print("-" * 68)
    for mid, label, want, got, ok in rows:
        print(f"  {'OK  ' if ok else 'BAD '} {mid:<3} {label:<34} "
              f"期望 {'+'.join(want) if want else '(全绿)':<10} "
              f"实测 {'+'.join(got) if isinstance(got, tuple) else got}")
    print("-" * 68)

    same = all(hashlib.sha256(_read_bytes(rel)).hexdigest() == sha0[rel]
               for rel in _MUTATION_FILES)
    rc, red, tail = _run_guard()
    print(f"还原校验：sha256 一致={same}；基线复跑 rc={rc} 红集={list(red)}")

    ok_all = not bad and same and rc == 0 and not red
    print(f"合计 {len(rows)} 组变异，符合预期 {sum(1 for r in rows if r[4])} 组，"
          f"不符 {len(bad)} 组")
    for b in bad:
        print(f"  BAD {b}")
    print("自证通过" if ok_all else f"自证失败：{tail}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        sys.exit(_self_check())
    sys.exit(main())
