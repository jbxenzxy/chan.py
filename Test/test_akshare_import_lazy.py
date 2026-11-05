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

六项判据：
  ① 模块级 import 集合**恰好**是白名单（logging / Common + 相对
     .CommonStockAPI）—— 顶层新增任何 import 都会红，须回来说明理由再同步白名单；
  ② 全文件 `import akshare` **恰好 1 处**，且只在 `_ak()` 函数体内；
  ③ 三个消费方 `fetch_index_cons` / `get_kl_data` / `do_init` 一律 `ak = _ak()`：
     `_ak()` 调用**恰好 1 次**、必须就是绑定 `ak` 的那一次，且函数内不存在
     `__import__()` / `*.import_module()` / `sys.modules` / `import akshare`，也没有
     `from importlib import import_module [as x]` 后再调 x()、`exec()` / `eval()`
     字面量里带导入语义串（`exec('ak = __import__("akshare")')`）这类绕过；
  ④ `create_item_dict` 的 pandas 是函数内 import（局部依赖，不靠模块全局 pd）；
  ⑤ 运行时硬证据：全新子进程 `import DataAPI.AkshareAPI` 后 `akshare` 不在
     `sys.modules`。未装 akshare 的环境**降级为 SKIP**：那时根本没有东西可加载，
     不能把 `AK_LOADED=False` 读成「惰性生效」；①②③④ 静态判据仍生效。
  ⑥ 运行时连线 + **对象身份**：全新子进程把 `sys.modules["akshare"]` 换成桩，
     `_ak` 被包成「取桩 → 返回带唯一标记的代理」，然后**真实调用**三个消费方 ——
     `fetch_index_cons("000300")` 必须返回桩里那两只成分股、`CAkshare("sh600000")
     .get_kl_data()` 必须产出 2 根 K 线（close 10.5 / 11.0）、`CAkshare.do_init()`
     必须通过。身份判据三条：`_ak()` 恰好被调 3 次；每次返回的代理都带同一标记
     （即三个消费方拿到的是同一个 `_ak()` 返回值）；**桩自身被直接访问 0 次** ——
     后者是「绕过唯一入口自己取数」的运行期硬证据（桩的每个属性都记账，不靠
     AST 形态）。未装 pandas 时降级 SKIP（桩的夹具是真实 DataFrame）。

「模块级」的判定口径：`ast` 中未被 `FunctionDef / AsyncFunctionDef / Lambda`
包住的 `Import` / `ImportFrom` 都算 —— 含 `try:` / `if:` 块内的，以及类体内的
（类体在类创建期执行，同样是导入期成本）。原始写法 `try: import akshare /
except ImportError: ak = None` 正是靠这个口径才抓得到。

③ 为什么不能只判「函数里出现过 _ak()」：消费方写成
`if False: _ak()` + `ak = __import__("akshare")` 时，死代码满足了「出现过」，
真取数却绕过了唯一入口 —— 顶层零重依赖与启动期收益都还在，但「以后加版本
校验 / 缓存 / 遥测只改一处」的约定已经失效。故 ③ 改判「恰好一次绑定」。

③ 的静态黑名单为什么只认语义形态（2026-11-05 第四轮收窄）：早先任何
`.modules` 属性都判红，于是消费方里出现一个与 akshare 无关的 `df.modules`
也会红（评审 N5 实测误伤）。现在只认 `sys.modules`、`__import__()`、
`*.import_module()` 三种**确属动态导入**的形态；`importlib` 这个名字本身不再
单独判红（`import importlib` 而不调用没有任何绕过能力）。

③ 第五轮补的两类形态（评审 Q2/Q3 实测放行后补）：`from importlib import
import_module as _im` 再调 `_im("akshare")`（别名绕开 `.import_module()` 属性判据）、
`exec("ak = __import__('akshare')")`（导入语义在字符串里，AST 只见字面量）。
两类的判别口径仍是「有绕过能力」而非「名字难看」：只 import 不调用、
`exec("print(1)")` 这类照旧放行。

判据之间的分工（六项判据合起来也不是完备性证明）：①②③④ 只看 AST 形态，
⑤ 只看「import 后 akshare 是否进了 sys.modules」，⑥ 才真跑消费方。前四轮
靠 ⑥ 咬住的三类断链是 ——
  · 作用域：`_ak()` 被挪进类体、模块级消失 → 消费方 NameError；
  · 返回值契约：`_ak()` 保留 `import akshare` 但 `return None` → `fetch_index_cons`
    静默返回 []、`get_kl_data` AttributeError；
  · 绑定分支：`ak = _ak()` 挪进 `except ImportError` 分支 → 正常路径根本不执行
    那一行，`fetch_index_cons` 静默返回 []（③ 仍判「恰好一次绑定」而放行）。
第五轮补的第四类更隐蔽：**取数旁路藏在辅助函数里**（消费方老实写 `ak = _ak()`
却让另一条路径去取数，③ 只扫三个消费方故全绿、输出也正确）—— 只有 ⑥ 的
「桩被直接访问次数 = 0」能咬住（夹具 R1）。

仍然不受静态判据保护的：用拼接/编码构造的动态导入串（`exec("__im" + "port__…")`）
与 `builtins.__dict__["__import__"]` 一类反射（夹具 R1/R2 走的就是这条路径：
`import builtins` + 下标取 `__import__`，③ 的三条形态一条都不命中）。它们的
**取数行为**仍落在 ⑥ 的记账口径里（任何直接访问桩的调用都会记一笔；R2 那种
「主路径照旧走代理、只多打一发直连」也照样红）；真正漏掉的只有
「反射取到 akshare 但没有调用本文件用到的三个接口」—— 例如只读
`akshare.__version__`，无语义影响，故不再为它加判据。

变异自证：`python Test/test_akshare_import_lazy.py --self-check`
  默认在**临时副本树**里做（不碰工作树），`--in-place` 才就地改源文件；
  缺 akshare / pandas 时 `--self-check` 硬失败 rc=2，门禁组件入口带
  `--allow-skip` 才降级为 `[SKIP]` rc=0（最小环境里这两项红集本就无法成立）。
  M1 顶层加回 import akshare          → ①②⑤
  M2 fetch_index_cons 改回直连 import  → ②③⑥
  M3 删 create_item_dict 局部 pandas   → ④⑥
  M4 顶层加 import pandas              → ①
  M5 import akshare 藏进类体            → ①②⑤
  M6 消费方 __import__ 绕过 + 死 _ak()  → ③⑥
  M7 _ak() 改名、消费方仍调旧名          → ②③⑥
  M8 DataAPI/__init__.py 顶层 import    → ⑤
  M9 顶层 try/except 容错（1105 原形）   → ①②⑤
  N5 消费方出现无关的 `.modules` 属性    → （全绿；③ 收窄后不再误伤）
  N6 _ak() 挪进类体（作用域断）          → ⑥
  N7 _ak() 返回 None（返回值契约断）     → ⑥
  N8 ak = _ak() 挪进 except 分支（绑定分支断）→ ⑥
  N9 死代码里 sys.modules["akshare"]     → ③（收窄后仍咬得住语义绕过）
  N10 死代码里 importlib.import_module   → ③
  Q1 消费方 import importlib 但不调用     → （全绿；不得误伤合规写法）
  Q2 exec("ak = __import__('akshare')")  → ③
  Q3 from importlib import import_module 别名调用 → ③
  R1 取数旁路藏进辅助函数（消费方仍写 ak = _ak()）→ ⑥（桩被直接访问 > 0）
  R2 额外用反射直连桩取一次（主路径仍走代理）→ ⑥（只有 direct 计数能咬）
逐组注入 → 单跑护栏 → 比对红集 → 逐字节还原并校验 sha256。
本护栏按「仓库根」定位目标文件，可用环境变量 `AKSHARE_GUARD_TREE` 指向别的树
（`--self-check` 的隔离模式就是这么复用的）。
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_DEFAULT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_TREE_ENV = "AKSHARE_GUARD_TREE"
# 允许把本护栏指向另一棵树（自证隔离模式 / 手工核对某份拷贝）
_REPO_ROOT = os.path.abspath(os.environ.get(_TREE_ENV) or _DEFAULT_ROOT)
_TARGET_REL = os.path.join("DataAPI", "AkshareAPI.py")
_TARGET = os.path.join(_REPO_ROOT, _TARGET_REL)
_INIT_REL = os.path.join("DataAPI", "__init__.py")

# ① 模块级 import 白名单：顶层包名；相对导入记为 ".模块名"
_ALLOWED_MODULE_IMPORTS = {"logging", "Common", ".CommonStockAPI"}

# ③ 必须经 _ak() 取数、不得自己 import akshare 的消费方
_AK_CONSUMERS = ("fetch_index_cons", "get_kl_data", "do_init")

# ③ 已知绕过动态导入的**语义形态**（不是名字黑名单：名字可随便起，形如
#    `importlib.import_module(...)` / `__import__(...)` / `sys.modules` 才有绕过能力）
_DYNAMIC_IMPORT_ATTRS = {"import_module", "__import__"}
# ③ 导入语义藏在字符串里的形态：exec / eval 的字面量参数含这些词就判红
_EXEC_CALLS = {"exec", "eval"}
_EXEC_NEEDLES = ("import", "akshare", "modules")

# ⑥ 桩数据与期望值（桩是确定的，故断言可精确到字段）
_RUNTIME_EXPECT_CODES = ["600000", "000001"]
_RUNTIME_EXPECT_CLOSE = [10.5, 11.0]
# 绑定关系：「三个消费方各调一次」——fetch_index_cons / get_kl_data / do_init。
# 改了取数结构（例如给 _ak() 加模块级缓存、三处共用一次取数）必须同步改这个
# 数字 **以及** _MUTATIONS 里各组的期望红集，否则 ⑥ 会把「实现变了但链路正确」
# 判成红（评审 P3-4 的口径提醒）。
_RUNTIME_EXPECT_AK_CALLS = 3
# 取数必须发生在 `_ak()` 返回的代理上：桩自身被直接访问次数 = 0
_RUNTIME_EXPECT_VIA = {"index_stock_cons_csindex": 1, "stock_zh_a_hist": 1}

_RUNTIME_PROBE = r'''
import json
import sys
import types

try:
    import pandas as pd
except Exception as e:
    print("PANDAS_MISSING=%s" % type(e).__name__)
    raise SystemExit(0)

rows = [
    {"日期": "2024-01-02", "开盘": 10.0, "最高": 11.0, "最低": 9.5,
     "收盘": 10.5, "成交量": 1000, "成交额": 10500.0, "换手率": 1.2},
    {"日期": "2024-01-03", "开盘": 10.5, "最高": 11.5, "最低": 10.0,
     "收盘": 11.0, "成交量": 1200, "成交额": 13200.0, "换手率": 1.4},
]
cons_rows = [
    {"成分券代码": "600000.SH", "交易所": "上海证券交易所"},
    {"成分券代码": "000001.SZ", "交易所": "深圳证券交易所"},
]
FX = {
    "stock_zh_a_hist": lambda **kw: pd.DataFrame(rows),
    "stock_zh_index_daily": lambda **kw: pd.DataFrame(rows),
    "index_stock_cons_csindex": lambda **kw: pd.DataFrame(cons_rows),
}
MARK = "akshare-proxy-2026-11-05"
via, direct = [], []


def _caller(name, bucket):
    def _call(**kw):
        bucket.append(name)
        return FX[name](**kw)
    return _call


class _Proxy(object):
    """_ak() 的返回值：带唯一标记。刻意与 sys.modules["akshare"] 不是同一对象。"""

    __akshare_proxy__ = MARK

    def __getattr__(self, name):
        if name in FX:
            return _caller(name, via)
        raise AttributeError(name)


proxy = _Proxy()

fake = types.ModuleType("akshare")


def _stub_getattr(name):
    # 模块级 __getattr__：任何绕过 _ak() 的直接取数都会在这里记一笔
    if name in FX:
        return _caller(name, direct)
    raise AttributeError(name)


fake.__getattr__ = _stub_getattr
sys.modules["akshare"] = fake

import DataAPI.AkshareAPI as M

out = {"ak_attr": hasattr(M, "_ak"), "mark": MARK}
calls, marks = [], []
if out["ak_attr"]:
    _orig = M._ak

    def _wrapped():
        calls.append(1)
        got = _orig()
        if got is not fake:      # 契约被改（None / 别的对象）：原样透出，让断链显形
            marks.append("<not-stub:%s>" % type(got).__name__)
            return got
        marks.append(MARK)
        return proxy

    M._ak = _wrapped
try:
    cons = M.fetch_index_cons("000300")
    out["fetch"] = cons if isinstance(cons, list) else repr(cons)[:120]
except Exception as e:
    out["fetch_error"] = type(e).__name__ + ": " + str(e)[:160]
try:
    units = list(M.CAkshare("sh600000").get_kl_data())
    out["kl"] = [float(u.close) for u in units]
except Exception as e:
    out["kl_error"] = type(e).__name__ + ": " + str(e)[:160]
try:
    M.CAkshare.do_init()
    out["do_init"] = "ok"
except Exception as e:
    out["do_init"] = type(e).__name__ + ": " + str(e)[:160]
out["ak_calls"] = len(calls)
out["marks"] = marks
out["via"] = via
out["direct"] = direct
print("RUNTIME_JSON " + json.dumps(out, ensure_ascii=False, sort_keys=True))
'''

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


def _import_module_aliases(fn):
    """`from importlib import import_module [as x]` 在函数内绑定的本地名。"""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "importlib":
            for a in n.names:
                if a.name == "import_module":
                    out.add(a.asname or a.name)
    return out


def _exec_has_import(call):
    """exec()/eval() 的字面量参数里是否带导入语义（拼接串看不见，见 docstring 边界）。"""
    args = list(call.args) + [k.value for k in call.keywords]
    for a in args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            low = a.value.lower()
            if any(nd in low for nd in _EXEC_NEEDLES):
                return True
    return False


def _dynamic_hits(fn):
    """函数体内的动态导入 / 反射取模块痕迹（只认语义形态，见模块 docstring）。"""
    hits = []
    aliases = _import_module_aliases(fn)
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "__import__":
                hits.append(f"__import__()@{n.lineno}")
            elif isinstance(f, ast.Name) and f.id in aliases:
                hits.append(f"import_module 别名 {f.id}()@{n.lineno}")
            elif isinstance(f, ast.Name) and f.id in _EXEC_CALLS and _exec_has_import(n):
                hits.append(f"{f.id}(含导入语义的字符串)@{n.lineno}")
            elif isinstance(f, ast.Attribute) and f.attr in _DYNAMIC_IMPORT_ATTRS:
                hits.append(f".{f.attr}()@{n.lineno}")
        elif isinstance(n, ast.Attribute) and n.attr == "modules" \
                and isinstance(n.value, ast.Name) and n.value.id == "sys":
            hits.append(f"sys.modules@{n.lineno}")
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


def _child_env(root):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    env[_TREE_ENV] = root
    return env


def _runtime_probe():
    """⑥ 打桩 akshare 后真跑三个消费方。返回 (payload|None, 说明, 是否降级)。"""
    try:
        proc = subprocess.run([sys.executable, "-c", _RUNTIME_PROBE], cwd=_REPO_ROOT,
                              capture_output=True, text=True,
                              env=_child_env(_REPO_ROOT), timeout=180)
    except Exception as e:  # noqa: BLE001 —— 护栏自身不得因探针异常而 traceback
        return None, f"{type(e).__name__}: {e}", False
    out = proc.stdout or ""
    err = (proc.stderr or "").strip()[-200:]
    if "PANDAS_MISSING=" in out:
        return None, ("pandas 未安装：桩的夹具是真实 DataFrame，本判据降级"
                      "（⑤ 与 ①②③④ 仍生效）"), True
    m = re.search(r"RUNTIME_JSON (\{.*\})", out)
    if not m:
        return None, (f"rc={proc.returncode} 探针无输出 "
                      f"out={out.strip()[-200:]!r} err={err!r}"), False
    return json.loads(m.group(1)), "", False


def _check_runtime(r):
    """⑥ 把探针结果翻成违规说明列表（空表示合规）。"""
    bad = []
    if r.get("ak_attr") is not True:
        bad.append("模块级无 _ak()（入口缺失或作用域被挪）")
    cons = r.get("fetch")
    if not isinstance(cons, list):
        bad.append(f"fetch_index_cons 非列表：{r.get('fetch_error', cons)!r}")
    else:
        codes = [c.get("code") for c in cons if isinstance(c, dict)]
        markets = [c.get("market") for c in cons if isinstance(c, dict)]
        if codes != _RUNTIME_EXPECT_CODES or not all(m in ("sh", "sz") for m in markets):
            bad.append(f"fetch_index_cons 成分股不符：{cons!r}")
    kl = r.get("kl")
    if not isinstance(kl, list) or len(kl) != len(_RUNTIME_EXPECT_CLOSE) or any(
            abs(a - b) > 1e-9 for a, b in zip(kl, _RUNTIME_EXPECT_CLOSE)):
        bad.append(f"get_kl_data 产出不符：{r.get('kl_error', kl)!r}"
                   f"（期望 {_RUNTIME_EXPECT_CLOSE}）")
    if r.get("do_init") != "ok":
        bad.append(f"do_init 失败：{r.get('do_init')!r}")
    if r.get("ak_calls") != _RUNTIME_EXPECT_AK_CALLS:
        bad.append(f"_ak() 实际被调 {r.get('ak_calls')} 次（要求 "
                   f"{_RUNTIME_EXPECT_AK_CALLS}：三个消费方各一次）")
    # ── 对象身份：三个消费方必须拿到 _ak() 返回的那个代理，而不是自己去取桩 ──
    marks = r.get("marks")
    mark = r.get("mark")
    if isinstance(marks, list) and any(m != mark for m in marks):
        bad.append(f"消费方拿到的不是 _ak() 返回的代理对象：marks={marks}")
    direct = r.get("direct")
    if direct:
        bad.append(f"有取数绕过了 _ak() 返回的对象：桩被直接访问 {direct}"
                   "（这些调用没经过唯一入口）")
    via = r.get("via")
    got_via = {}
    for name in (via if isinstance(via, list) else [repr(via)]):
        got_via[name] = got_via.get(name, 0) + 1
    if got_via != _RUNTIME_EXPECT_VIA:
        bad.append(f"经代理的取数次数不符：{got_via}（期望 {_RUNTIME_EXPECT_VIA}）")
    return bad


def main():
    print("AKShare 数据源「顶层零重依赖」防回潮护栏")
    print("=" * 68)
    print(f"目标文件：{_TARGET_REL}")
    print(f"仓库根：{_REPO_ROOT}")

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
    try:
        proc = subprocess.run([sys.executable, "-c", probe], cwd=_REPO_ROOT,
                              capture_output=True, text=True,
                              env=_child_env(_REPO_ROOT), timeout=120)
        out = proc.stdout or ""
        err = (proc.stderr or "").strip()[-200:]
        if proc.returncode == 0 and "AK_INSTALLED=False" in out:
            rec_skip("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
                     "akshare 未安装：无东西可加载，本判据降级（不能据此判定惰性生效）；"
                     "①②③④⑥ 仍生效")
        else:
            rec("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
                proc.returncode == 0 and "AK_LOADED=False" in out
                and "AK_INSTALLED=True" in out,
                f"rc={proc.returncode} out={out.strip()!r} err={err!r}")
    except Exception as e:  # noqa: BLE001 —— 护栏自身不得因探针异常而 traceback
        rec("⑤", "全新子进程 import DataAPI.AkshareAPI 后 akshare 未进 sys.modules",
            False, f"{type(e).__name__}: {e}")

    # ── ⑥ 运行时连线：打桩后三个消费方真实走 _ak() 且取数成功 ────────
    title6 = ("打桩 akshare 后三个消费方真实走 _ak() 返回的对象（取数成功、桩零直接访问）")
    payload, why, skip = _runtime_probe()
    if skip:
        rec_skip("⑥", title6, why)
    elif payload is None:
        rec("⑥", title6, False, why)
    else:
        bad6 = _check_runtime(payload)
        rec("⑥", title6, not bad6, "; ".join(bad6) + f"    payload={payload}")

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
_FETCH_TRY = ('    try:\n        ak = _ak()\n    except ImportError:\n'
              '        log.info("[AKShare] 未安装')
_FETCH_DOC_TAIL = ('    各自持有超时策略，这里只做纯取数、不再 import akshare 于调用方。\n'
                   '    """\n    try:')
_FETCH_BODY = '        df = ak.index_stock_cons_csindex(symbol=index_code)\n'
_CLS_HEAD = 'class CAkshare(CCommonStockApi):\n    """使用 akshare 获取A股数据"""\n'


def _dead_stmt(stmt, mid):
    """把 stmt 塞进 fetch_index_cons 开头的 `if False:` 里（死代码，只供 AST 判据看）。"""
    new = _FETCH_DOC_TAIL.replace(
        '    """\n    try:',
        '    """\n    if False:\n        ' + stmt + f'  # MUTATION-{mid}\n    try:')
    return (_TARGET_REL, _FETCH_DOC_TAIL, new)

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
        "edits": [(_TARGET_REL, _FETCH_TRY,
                   '    try:\n        import akshare as ak  # MUTATION-M2\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("②", "③", "⑥"),
    },
    {
        "id": "M3",
        "label": "删掉 create_item_dict 内的 import pandas",
        "edits": [(_TARGET_REL,
                   '    """将DataFrame行转换为K线单元所需的字典格式"""\n'
                   '    import pandas as pd\n',
                   '    """将DataFrame行转换为K线单元所需的字典格式"""\n')],
        "red": ("④", "⑥"),
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
        "edits": [(_TARGET_REL, _CLS_HEAD,
                   _CLS_HEAD + '    import akshare  # MUTATION-M5\n')],
        "red": ("①", "②", "⑤"),
    },
    {
        "id": "M6",
        "label": "消费方 __import__ 绕过 _ak()，只留死代码 _ak() 调用",
        "edits": [(_TARGET_REL, _FETCH_TRY,
                   '    try:\n        if False:\n            _ak()\n'
                   '        ak = __import__("akshare")  # MUTATION-M6\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("③", "⑥"),
    },
    {
        "id": "M7",
        "label": "_ak() 改名，消费方仍调旧名",
        "edits": [(_TARGET_REL, "def _ak():\n", "def _ak_alt():\n")],
        "red": ("②", "③", "⑥"),
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
    {
        "id": "N5",
        "label": "消费方出现无关的 `.modules` 属性（③ 收窄后不得误伤）",
        "edits": [_dead_stmt("_unrelated = (1).modules", "N5")],
        "red": (),
    },
    {
        "id": "N6",
        "label": "_ak() 挪进类体、模块级删掉（作用域断）",
        "edits": [(_TARGET_REL,
                   'def _ak():\n'
                   '    """惰性获取 akshare（本文件唯一入口；未安装时抛 ImportError，由调用方处置）"""\n'
                   '    import akshare\n    return akshare\n\n\n', ''),
                  (_TARGET_REL, _CLS_HEAD,
                   _CLS_HEAD + '    def _ak(self):\n        import akshare  # MUTATION-N6\n'
                   '        return akshare\n')],
        "red": ("⑥",),
    },
    {
        "id": "N7",
        "label": "_ak() 保留 import akshare 但 return None（返回值契约断）",
        "edits": [(_TARGET_REL, "    import akshare\n    return akshare\n",
                   "    import akshare\n    return None  # MUTATION-N7\n")],
        "red": ("⑥",),
    },
    {
        "id": "N8",
        "label": "ak = _ak() 挪进 except 分支（绑定分支断）",
        "edits": [(_TARGET_REL, _FETCH_TRY,
                   '    try:\n        pass\n    except ImportError:\n'
                   '        ak = _ak()  # MUTATION-N8\n'
                   '        log.info("[AKShare] 未安装')],
        "red": ("⑥",),
    },
    {
        "id": "N9",
        "label": "死代码里 sys.modules['akshare']（③ 收窄后仍须红）",
        "edits": [_dead_stmt('_unrelated = sys.modules["akshare"]', "N9")],
        "red": ("③",),
    },
    {
        "id": "N10",
        "label": "死代码里 importlib.import_module('akshare')（③ 收窄后仍须红）",
        "edits": [_dead_stmt('_unrelated = importlib.import_module("akshare")', "N10")],
        "red": ("③",),
    },
    {
        "id": "Q1",
        "label": "消费方 import importlib 但不调用（合规写法，不得误伤）",
        "edits": [_dead_stmt("import importlib", "Q1")],
        "red": (),
    },
    {
        "id": "Q2",
        "label": "exec 字面量里 __import__ 二次绑定（导入语义藏在字符串里）",
        "edits": [(_TARGET_REL, _FETCH_TRY,
                   '    try:\n        ak = _ak()\n'
                   '        exec("ak = __import__(\'akshare\')")  # MUTATION-Q2\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("③",),
    },
    {
        "id": "Q3",
        "label": "from importlib import import_module as _im 再调 _im()",
        "edits": [_dead_stmt('from importlib import import_module as _im\n'
                             '        _unrelated = _im("akshare")', "Q3")],
        "red": ("③",),
    },
    {
        "id": "R1",
        "label": "取数旁路藏进辅助函数（③ 只扫消费方，靠 ⑥ 的 direct 计数抓）",
        "edits": [
            (_TARGET_REL, "def fetch_index_cons(index_code):",
             'def _fetch_cons_direct(symbol):\n'
             '    import builtins  # MUTATION-R1\n'
             '    _b = builtins.__dict__["__import__"]  # MUTATION-R1\n'
             '    return _b("akshare").index_stock_cons_csindex(symbol=symbol)\n\n\n'
             'def fetch_index_cons(index_code):'),
            (_TARGET_REL, _FETCH_BODY,
             '        df = _fetch_cons_direct(index_code)  # MUTATION-R1\n'),
        ],
        "red": ("⑥",),
    },
    {
        "id": "R2",
        "label": "消费方额外用反射直连桩取一次（主路径仍走代理 ⇒ 只有 direct 能咬）",
        "edits": [(_TARGET_REL, _FETCH_TRY,
                   '    try:\n        ak = _ak()\n'
                   '        import builtins  # MUTATION-R2\n'
                   '        _b = builtins.__dict__["__import__"]  # MUTATION-R2\n'
                   '        _warm = _b("akshare").index_stock_cons_csindex('
                   'symbol=index_code)  # MUTATION-R2\n'
                   '    except ImportError:\n        log.info("[AKShare] 未安装')],
        "red": ("⑥",),
    },
]

_MUTATION_FILES = sorted({e[0] for m in _MUTATIONS for e in m["edits"]})

# 建隔离树时跳过的目录：全是与护栏无关的体积（Docs 50MB / Test 快照 9MB）
_COPY_IGNORE = ("Docs", "Image", "Test", ".git", "__pycache__", ".venv", "*.pyc", "*.log")


def _read_bytes(root, rel):
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def _write_bytes(root, rel, data):
    p = os.path.join(root, rel)
    tmp = p + ".selfcheck.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


def _drop_pyc(root, rel):
    d = os.path.join(root, os.path.dirname(rel), "__pycache__")
    stem = os.path.splitext(os.path.basename(rel))[0]
    if not os.path.isdir(d):
        return
    for n in os.listdir(d):
        if n.startswith(stem + "."):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass


def _apply(root, rel, old, new):
    raw = _read_bytes(root, rel)
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
    _write_bytes(root, rel, out.replace("\n", eol).encode("utf-8"))
    _drop_pyc(root, rel)


def _run_guard(root):
    env = _child_env(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    guard = os.path.join(root, "Test", os.path.basename(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, guard], cwd=root, env=env,
                          capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    red = tuple(sorted(set(re.findall(r"\[FAIL\]\s*([①②③④⑤⑥])", out))))
    return proc.returncode, red, out.strip()[-400:]


def _make_isolated_tree():
    """复制一棵临时树（跳过 Docs/Test/Image），并把本护栏放到 Test/ 下。"""
    root = tempfile.mkdtemp(prefix="akshare_guard_tree_")
    shutil.copytree(_DEFAULT_ROOT, root, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_COPY_IGNORE))
    dst = os.path.join(root, "Test", os.path.basename(os.path.abspath(__file__)))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(os.path.abspath(__file__), dst)
    return root


def _self_check(isolated=True, allow_skip=False):
    import importlib.util
    print("AKShare 护栏变异自证（M1~M9 + N5~N10 + Q1~Q3 + R1/R2，"
          "逐组注入 → 单跑 → 逐字节还原）")
    print("=" * 68)

    for mod, why in (("akshare", "M1/M5/M8/M9 期望 ⑤ 变红"),
                     ("pandas", "N6/N7/N8 期望 ⑥ 变红")):
        try:
            missing = importlib.util.find_spec(mod) is None
            detail = f"未安装 {mod}：{why}无法成立"
        except Exception as e:  # noqa: BLE001
            missing = True
            detail = f"探测 {mod} 失败：{type(e).__name__}: {e}"
        if missing:
            if allow_skip:
                # 门禁组件入口：最小环境（系统 Python）里没有这些依赖，
                # 与其让整个门禁常红，不如像 ⑤/⑥ 一样明示降级、不给结论。
                print(f"[SKIP] {detail}；本机降级，自证不给出结论"
                      f"（去掉 --allow-skip 可要求硬失败）")
                return 0
            print(f"[ABORT] {detail}；请先 pip install 后再自证")
            return 2

    root = _REPO_ROOT
    if isolated:
        root = _make_isolated_tree()
        if os.path.abspath(root) == os.path.abspath(_DEFAULT_ROOT):
            print("[ABORT] 隔离树与工作树同路径，拒绝就地改写")
            return 2
    print(f"{'隔离副本树' if isolated else '工作树（--in-place）'}：{root}")
    print(f"目标文件：{', '.join(_MUTATION_FILES)}")

    try:
        return _run_self_check(root)
    finally:
        if isolated:
            shutil.rmtree(root, ignore_errors=True)


def _run_self_check(root):
    backup = {rel: _read_bytes(root, rel) for rel in _MUTATION_FILES}
    sha0 = {rel: hashlib.sha256(b).hexdigest() for rel, b in backup.items()}

    rc, red, tail = _run_guard(root)
    print(f"基线：rc={rc} 红集={list(red)}")
    if rc != 0 or red:
        print(f"[ABORT] 基线不绿，自证无意义：{tail}")
        return 1

    rows, bad = [], []
    try:
        for m in _MUTATIONS:
            try:
                for rel, old, new in m["edits"]:
                    _apply(root, rel, old, new)
            except AssertionError as e:
                for rel in _MUTATION_FILES:
                    _write_bytes(root, rel, backup[rel])
                    _drop_pyc(root, rel)
                rows.append((m["id"], m["label"], m["red"], "替换点未命中", False))
                bad.append(f"{m['id']} 替换点未命中: {e}")
                continue
            try:
                rc, red, _tail = _run_guard(root)
            finally:
                for rel in _MUTATION_FILES:
                    _write_bytes(root, rel, backup[rel])
                    _drop_pyc(root, rel)
            # 期望「全绿」的组（N5）必须 rc=0，其余必须 rc≠0
            ok = red == m["red"] and (rc == 0 if not m["red"] else rc != 0)
            rows.append((m["id"], m["label"], m["red"], red, ok))
            if not ok:
                bad.append(f"{m['id']} 期望 {m['red']} 实测 {red} rc={rc}")
    finally:
        for rel in _MUTATION_FILES:
            _write_bytes(root, rel, backup[rel])
            _drop_pyc(root, rel)

    print("-" * 68)
    for mid, label, want, got, ok in rows:
        print(f"  {'OK  ' if ok else 'BAD '} {mid:<4} {label:<44} "
              f"期望 {'+'.join(want) if want else '(全绿)':<10} "
              f"实测 {'+'.join(got) if isinstance(got, tuple) else got}")
    print("-" * 68)

    same = all(hashlib.sha256(_read_bytes(root, rel)).hexdigest() == sha0[rel]
               for rel in _MUTATION_FILES)
    rc, red, tail = _run_guard(root)
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
        sys.exit(_self_check(isolated="--in-place" not in sys.argv[1:],
                             allow_skip="--allow-skip" in sys.argv[1:]))
    sys.exit(main())
