# -*- coding: utf-8 -*-
"""锁覆盖**完整性**静态守护（无第三方依赖，纯 ast 扫描）

为什么需要它
------------
`test_lock_v5_guards.py` 用计数包装锁断言「这些入口持锁」——它问的是
**「这扇门有没有锁」**，没问**「是不是所有门都上了锁」**。审计 v6 里
`_scan_skip_log` 整个逃出登记表、`replace_names` / `get_annotated_codes`
无锁遍历，都是靠这个盲区一路溜过去的。

本用例反向做：从**受保护字段**出发，凡是引用了受保护字段却没持对应锁的，
一律报告。新增代码若忘了加锁会立刻失败，而不是等线上 500。

════════════════════════════════════════════════════════════════════
三种形态（指导书 §8.3：扫描器的覆盖面就是盲区）
════════════════════════════════════════════════════════════════════
v6 版只认形态 ①，另外两种整片在视野外——AppRefresh.py 那段无锁全表迭代
（`set(_pe_ttm_cache.keys())`）就是这么溜过去的：它用的是**模块级别名**，
不带 `self.`，形态 ① 的扫描器根本看不见。

  ① 实例字段      `self._names`                  + `with self._meta_cache_lock:`
  ② 模块级别名    `_pe_ttm_cache = app_data.pe_cache`
                  之后全程不带 `self.`，绕开形态 ①
  ③ 跨模块守卫    `with app_data.stocks_sub_chan_guarded(...):`
                  锁由 AppData 侧的 @contextmanager 持有，本模块看不见
                  任何 `with ...lock`，形态 ① 会误报为「无锁」

三者必须都覆盖，漏一种就有系统性盲区。

判定口径（函数级，非节点级）
----------------------------
引用受保护字段时，满足任一条件即视为已保护：
  ① 处于 `with <任一受认可的持锁表达式>:` 体内（含嵌套 with）；
  ② 方法名以 `_locked` 结尾——沿用项目既有约定「内部方法，调用方须持锁」
     （如 `_cache_put_locked` / `_evict_dual_overflow_locked`）；
  ③ 在 `ALLOWLIST` 中显式登记并写明理由。

「非原子使用」的口径（形态 ② 专用）
------------------------------------
别名指向的是**共享可变容器**。CPython GIL 下以下操作是原子的，不报警：
    X[k]（只读下标）、X.get(k)、k in X、if X:（真值测试）
以下**不是**原子的组合操作，必须持对应锁：
    迭代（for / 推导式 / *X 解包）
    .keys() / .values() / .items() / .copy() / .clear() / .update()
    / .pop() / .popitem() / .setdefault()
    len(X) / list(X) / set(X) / sorted(X) / dict(X) / tuple(X)
    X[k] = v（写）——单条 STORE_SUBSCR 本身原子，但与写者的
    clear()+update() 组合会丢失更新，故一并纳入
    del X[k]

已知近似：判定是**函数级**的，若某函数 A 分支持锁、B 分支访问字段，本用例
会判为已保护。这比节点级判定宽松，但足以拦住「整段无锁」这类真实缺陷。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))            # .../Test
_REPO_ROOT = os.path.dirname(_HERE)                            # 仓库根

_APPDATA = os.path.join(_REPO_ROOT, "App", "AppData.py")

# ── 形态 ①：受保护字段 → 保护它的锁 ─────────────────────────────────
# 与 AppData.__init__ 中「锁集合」的注释一一对应；新增共享字段必须登记。
FIELD_LOCK = {
    # _meta_cache_lock（v5 漏登记，v6 补上）
    "_names": "_meta_cache_lock",
    "_names_loaded": "_meta_cache_lock",
    "_pe": "_meta_cache_lock",
    "_pe_loaded": "_meta_cache_lock",
    "_belong": "_meta_cache_lock",
    "_belong_loaded": "_meta_cache_lock",
    # _user_store_lock
    "_annotations": "_user_store_lock",
    "_annotations_loaded": "_user_store_lock",
    "_saved_point_times": "_user_store_lock",
    "_float_mc": "_user_store_lock",
    "_float_mc_loaded": "_user_store_lock",
    "_float_mc_saved_at": "_user_store_lock",
    # _stocks_cache_lock
    "_stocks_analysis_cache": "_stocks_cache_lock",
    "_stocks_sub_chan_cache": "_stocks_cache_lock",
    # _futures_cache_lock
    "_futures_analysis_cache": "_futures_cache_lock",
    "_futures_chan_locks": "_futures_cache_lock",
}

# ── 形态 ① 白名单：方法名 → 理由 ────────────────────────────────────
# 每条都必须写明**为什么这里无锁是安全的**。新增条目视为设计决策，需评审。
ALLOWLIST = {
    "__init__": "单例构造，import 期单线程执行，不存在并发",

    # 9 个「裸出口」property：下游已绑成模块级别名（AppEngine/AppRefresh/
    # AppScan），**不能**改成返回快照——那会破坏刷新线程 replace_names
    # 「同对象清空+灌入」的零漂移语义（别名将永远读到旧表）。
    # 契约：点查（.get / `in`）在 CPython 下原子，允许；**遍历必须**改用
    # names_snapshot() / pe_snapshot() / belong_snapshot() / annotations
    # 快照。审计 v6 §根因一。
    "stocks_analysis_cache": "裸出口 property：仅点查用，遍历须走快照方法",
    "futures_analysis_cache": "裸出口 property：仅点查用，遍历须走快照方法",
    "names_cache": "裸出口 property：仅点查用，遍历须走 names_snapshot()",
    "pe_cache": "裸出口 property：仅点查用，遍历须走 pe_snapshot()",
    "belong_cache": "裸出口 property：仅点查用，遍历须走 belong_snapshot()",
    "float_mc_cache": "裸出口 property：仅点查用",
    "float_mc_loaded": "裸出口 property：标量读，CPython 下原子",
    "saved_point_times": "裸出口 property：仅点查用（读走 get_saved_point_time）",
    "annotations_cache": "裸出口 property：仅点查用，遍历走 get_annotated_codes",

    # 点查（dict.get）在 CPython 下是原子的，无需加锁
    "get_stock_name": "self._names.get() 点查，原子",
    "get_pe_ttm": "self._pe.get() 点查，原子",
    "get_index_belong": "self._belong.get() 点查，原子",
    "get_float_mc_from_cache": "self._float_mc.get() 点查，原子",
    "get_annotated_codes": "self._names.get() 点查（标注表本身是锁内快照）",

    # 标量读
    "float_mc_cache_stale": "读 _float_mc_saved_at 标量，原子",
}

# ── 形态 ②③：跨模块扫描的靶文件 ────────────────────────────────────
SCAN_MODULES = [
    "App/AppData.py",
    "App/AppEngine.py",
    "App/AppRefresh.py",
    "App/AppScan.py",
    "App/AppScanPool.py",
    "App/AppChart.py",
    "App/AppSSE.py",
    "App/AppOrch.py",
    "FrontAPI.py",
]

# ── 形态 ③：AppData 的 @contextmanager 守卫方法 → 它在内部持有的锁 ────
# 调用方写 `with app_data.xxx_guarded(...):` 时锁在 AppData 内部，
# 本模块看不到任何 `with ...lock`，形态 ① 会误报为无锁。
GUARDED_METHODS = {
    "futures_sub_chan_guarded": "_futures_cache_lock",
    "futures_sub_chan_guarded_by_key": "_futures_cache_lock",
    "stocks_sub_chan_guarded": "_stocks_cache_lock",
}

# ── 形态 ② 白名单：(模块, 函数名) → 理由 ─────────────────────────────
ALIAS_ALLOWLIST = {}


# ══════════════════════════════════════════════════════════════════════
# 从 AppData.py 自动推导「property 出口 → 受保护字段 / 锁字段」
# 新增裸出口 property 会**自动**纳入形态 ② 的扫描，无需手工同步。
# ══════════════════════════════════════════════════════════════════════
def _parse_appdata_exports():
    """返回 (property→字段, 锁property→锁字段)"""
    tree = ast.parse(open(_APPDATA, encoding="utf-8").read())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "AppData"), None)
    if cls is None:
        raise AssertionError("AppData.py 中找不到 AppData 类")

    prop_field, lock_prop = {}, {}
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not any(isinstance(d, ast.Name) and d.id == "property"
                   for d in fn.decorator_list):
            continue
        for st in fn.body:
            if isinstance(st, ast.Return) and isinstance(st.value, ast.Attribute):
                field = st.value.attr
                if field in FIELD_LOCK:
                    prop_field[fn.name] = field
                elif field.endswith("_lock"):
                    lock_prop[fn.name] = field
    return prop_field, lock_prop


PROP_FIELD, LOCK_PROP = _parse_appdata_exports()

# 非原子操作：会触发全表扫描 / 结构变更的容器方法
_NON_ATOMIC_METHODS = {
    "keys", "values", "items", "copy", "clear", "update", "pop", "popitem",
    "setdefault", "popitem", "__iter__",
}
# 原子点查：允许无锁
_ATOMIC_METHODS = {"get"}
# 会触发全表扫描的内建函数
_SCAN_BUILTINS = {"len", "list", "set", "sorted", "dict", "tuple",
                  "sum", "any", "all", "max", "min", "frozenset", "iter"}


def _shared_key(node, alias_names):
    """把「共享容器引用」归一成一个可比较的 key

    两种写法都算：
      · 模块级别名               `_pe_ttm_cache`
      · 直接点 app_data 的出口    `app_data.pe_cache`
    只认前者会漏掉「不绑别名、就地用」的写法。
    """
    if isinstance(node, ast.Name) and node.id in alias_names:
        return node.id
    if (isinstance(node, ast.Attribute) and node.attr in PROP_FIELD
            and isinstance(node.value, ast.Name)
            and node.value.id in ("app_data", "_ad")):
        return f"app_data.{node.attr}"
    return None


def _field_of(key, alias_names, alias_field):
    """key → 受保护字段名"""
    if key in alias_field:
        return alias_field[key]
    return PROP_FIELD.get(key.split(".", 1)[-1])


def _dangerous_alias_use(node, alias_names):
    """若 node 是对某个共享容器的**非原子**使用，返回 (key, 原因)；否则 None"""
    # X.method(...)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        key = _shared_key(node.func.value, alias_names)
        if key is not None:
            m = node.func.attr
            if m in _ATOMIC_METHODS:
                return None
            if m in _NON_ATOMIC_METHODS:
                return key, f".{m}()"
            # 未知方法：保守报警（新增容器方法多半也是全表或变更）
            return key, f".{m}()（未知方法，保守判定为全表/变更）"

    # X[k] 读 / 写 / 删
    if isinstance(node, ast.Subscript):
        key = _shared_key(node.value, alias_names)
        if key is not None:
            if isinstance(node.ctx, ast.Store):
                return key, "下标写入"
            if isinstance(node.ctx, ast.Del):
                return key, "下标删除"
            return None  # 只读下标，原子

    # len(X) / list(X) / set(X) ...
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _SCAN_BUILTINS:
        for a in node.args:
            tgt = a.value if isinstance(a, ast.Starred) else a
            key = _shared_key(tgt, alias_names)
            if key is not None:
                suffix = " 全表解包" if isinstance(a, ast.Starred) else " 全表扫描"
                return key, f"{node.func.id}(){suffix}"

    # 迭代：for y in X / [.. for y in X]
    if isinstance(node, (ast.For, ast.comprehension)):
        key = _shared_key(node.iter, alias_names)
        if key is not None:
            return key, ("for 迭代" if isinstance(node, ast.For) else "推导式迭代")

    return None


def _scan_class(path, class_name):
    """形态 ①：返回 [(方法名, [未持锁的字段…])]"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        raise AssertionError(f"{path} 中找不到类 {class_name}")

    def walk(node, held, used):
        # 先判节点自身是否为 with（否则嵌套 with 的内层不会被识别）
        if isinstance(node, ast.With):
            inner = set(held)
            for it in node.items:
                cx = it.context_expr
                if (isinstance(cx, ast.Attribute)
                        and isinstance(cx.value, ast.Name)
                        and cx.value.id == "self"):
                    inner.add(cx.attr)
                # 形态 ③：with app_data.xxx_guarded(...)
                elif (isinstance(cx, ast.Call)
                      and isinstance(cx.func, ast.Attribute)
                      and isinstance(cx.func.value, ast.Name)):
                    owner, m = cx.func.value.id, cx.func.attr
                    if owner == "self" and m in GUARDED_METHODS:
                        inner.add(GUARDED_METHODS[m])
            for b in node.body:
                walk(b, inner, used)
            return
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            used.setdefault(node.attr, set()).update(held)
        for ch in ast.iter_child_nodes(node):
            walk(ch, held, used)

    violations = []
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        used = {}
        walk(fn, set(), used)
        bad = sorted({f for f, held in used.items()
                      if f in FIELD_LOCK and FIELD_LOCK[f] not in held})
        if bad and not fn.name.endswith("_locked") and fn.name not in ALLOWLIST:
            violations.append((fn.name, bad))
    return violations


def _scan_module_aliases(rel_path):
    """形态 ②③：返回 [(函数名, 别名, 原因, 行号)]"""
    path = os.path.join(_REPO_ROOT, *rel_path.split("/"))
    if not os.path.exists(path):
        return []
    return _scan_module_aliases_abs(path)


def _scan_module_aliases_abs(path):
    """（内部）形态 ②③ 的实际实现，接受绝对路径（自证用例也走同一条路径）"""
    tree = ast.parse(open(path, encoding="utf-8").read())

    # ① 收集模块级别名：`X = app_data.<property>`
    alias_field, lock_alias = {}, {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if not (isinstance(val, ast.Attribute)
                and isinstance(val.value, ast.Name)
                and val.value.id == "app_data"):
            continue
        for tg in node.targets:
            if not isinstance(tg, ast.Name):
                continue
            if val.attr in PROP_FIELD:
                alias_field[tg.id] = PROP_FIELD[val.attr]
            elif val.attr in LOCK_PROP:
                lock_alias[tg.id] = LOCK_PROP[val.attr]
    # 注意：**不能**因为本模块没绑别名就跳过——`app_data.pe_cache` 这类
    # 「不绑别名、就地用」的写法同样要扫（_shared_key 认这两种形态）。

    alias_names = set(alias_field)

    def walk(node, held, out, fn_name):
        # with <持锁表达式>:
        if isinstance(node, ast.With):
            inner = set(held)
            for it in node.items:
                cx = it.context_expr
                # with <锁别名>:
                if isinstance(cx, ast.Name) and cx.id in lock_alias:
                    inner.add(lock_alias[cx.id])
                # with app_data.<锁property>:
                elif (isinstance(cx, ast.Attribute)
                      and isinstance(cx.value, ast.Name)
                      and cx.value.id == "app_data"
                      and cx.attr in LOCK_PROP):
                    inner.add(LOCK_PROP[cx.attr])
                # 形态 ③：with app_data.xxx_guarded(...)
                elif (isinstance(cx, ast.Call)
                      and isinstance(cx.func, ast.Attribute)
                      and isinstance(cx.func.value, ast.Name)
                      and cx.func.value.id == "app_data"
                      and cx.func.attr in GUARDED_METHODS):
                    inner.add(GUARDED_METHODS[cx.func.attr])
            for b in node.body:
                walk(b, inner, out, fn_name)
            return

        hit = _dangerous_alias_use(node, alias_names)
        if hit is not None:
            key, reason = hit
            # 注意：held 集合里装的是**锁名**，`_field_of` 给的是**字段名**，
            # 必须经 FIELD_LOCK 换算后才能比较（否则必然恒为「未持锁」）。
            field = _field_of(key, alias_names, alias_field)
            if field is not None and FIELD_LOCK[field] not in held:
                out.append((fn_name, key, reason, node.lineno))

        for ch in ast.iter_child_nodes(node):
            walk(ch, held, out, fn_name)

    violations = []
    # 模块级代码（顶层语句；函数体由下方单独走一遍，此处跳过以免重复报告）
    mod_out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        walk(node, set(), mod_out, "<module>")
    violations.extend(mod_out)
    # 各函数（含嵌套函数；ast.walk 会逐层展开）
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (os.path.basename(path), node.name) in ALIAS_ALLOWLIST:
                continue
            if node.name.endswith("_locked"):
                continue
            out = []
            for st in node.body:
                walk(st, set(), out, node.name)
            violations.extend(out)
    return violations


# ── 裸出口 property 的「返回活对象」契约基线 ──────────────────────────
# property 名 → 必须返回的字段。
#
# 为什么必须是**固定清单**而不是从代码自动推导：
#   自动推导的判据是「函数体为 `return self._字段`」。一旦有人把某个出口
#   改成返回副本，它就**不再满足判据、直接从被检查集合里消失**，检查结果
#   只会从 12 条静默缩到 11 条——检查本身被绕过了。（这是自证用例实测出来
#   的：把 pe_cache 改成 `return dict(self._pe)` 后，本检查反而"通过"了。）
#   故这里钉死一份基线，双向比对：少一个、多一个、内容不对，都算失败。
#
# 语义：这些出口的存在理由见 ALLOWLIST——下游把它们绑成模块级别名，靠
# 「同对象清空+灌入」做到零漂移。**不能**改成返回快照，否则别名永远读到
# 旧表，而刷新线程 replace_names 的零漂移语义随之失效。这类改动不报错，
# 只会让数据静默过期，属于最难查的那一类。
PROP_CONTRACT = {
    "stocks_analysis_cache": "_stocks_analysis_cache",
    "futures_analysis_cache": "_futures_analysis_cache",
    "names_cache": "_names",
    "pe_cache": "_pe",
    "belong_cache": "_belong",
    "float_mc_cache": "_float_mc",
    "float_mc_loaded": "_float_mc_loaded",
    "saved_point_times": "_saved_point_times",
    "annotations_cache": "_annotations",
    "stocks_cache_lock": "_stocks_cache_lock",
    "futures_cache_lock": "_futures_cache_lock",
    "user_store_lock": "_user_store_lock",
}

# 允许违反契约的例外：property 名 → 理由（新增须评审）
PROP_LIVE_EXEMPT = {}


def _check_bare_property_contract():
    """裸出口 property 的「返回活对象」契约（审计 P2：从注释升级为机器校验）"""
    tree = ast.parse(open(_APPDATA, encoding="utf-8").read())
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "AppData"), None)
    if cls is None:
        return ["AppData 类缺失"], [], []

    props = {}
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not any(isinstance(d, ast.Name) and d.id == "property"
                   for d in fn.decorator_list):
            continue
        # 去掉 docstring / 纯字符串表达式后，函数体必须是且仅是一句
        # `return self.<字段>`
        body = [s for s in fn.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant))]
        if (len(body) == 1 and isinstance(body[0], ast.Return)
                and isinstance(body[0].value, ast.Attribute)
                and isinstance(body[0].value.value, ast.Name)
                and body[0].value.value.id == "self"):
            props[fn.name] = body[0].value.attr
        else:
            props[fn.name] = None          # 有这个出口，但形态不对

    missing = [k for k in PROP_CONTRACT if k not in props]
    broken = [f"{k}（应为 return self.{v}，实际 {props[k] or '非 return self.字段'}）"
              for k, v in PROP_CONTRACT.items()
              if k in props and props[k] != v and k not in PROP_LIVE_EXEMPT]
    extra = [k for k in props
             if k not in PROP_CONTRACT and k in (PROP_FIELD | LOCK_PROP)]
    return sorted(missing), sorted(broken), sorted(extra)


def _check_scan_skip_session():
    """审计 X3：跳过记录必须每次扫描私有，且只能经 token 感知的访问器碰

    v6 版断言的是「_scan_skip_log 只能经加锁访问器碰」。X3 之后该列表已
    下沉为 `_ScanSession.skip_log`（每次扫描私有），故断言升级为：
      ① 模块级 `_scan_skip_log` 全局**不得**复活（复活 = 跨页串批）；
      ② `.skip_log` 只能由四个访问器内部触碰，其他任何地方直接碰即失败。
    """
    # 四个 token 感知访问器 + 会话类自身（它拥有这份列表，理应直接碰）
    accessors = {"append_scan_skip", "clear_scan_skip",
                 "scan_skip_snapshot", "scan_skip_count"}
    owner_class = "_ScanSession"
    bad = []
    for rel in ("App/AppScan.py", "App/AppScanPool.py"):
        path = os.path.join(_REPO_ROOT, *rel.split("/"))
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        # ① 模块级全局不得复活
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tg in node.targets:
                    if isinstance(tg, ast.Name) and tg.id == "_scan_skip_log":
                        bad.append((rel, node.lineno, "模块级 _scan_skip_log 复活"))
        # ② 直接触碰 .skip_log（访问器与会话类除外）
        for cls in [n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == owner_class]:
            for fn in cls.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    accessors.add(fn.name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in accessors:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "skip_log":
                    bad.append((rel, sub.lineno, f"{node.name} 直接碰 .skip_log"))
    return sorted(set(bad))


def main():
    results = []

    # ① 形态 ①：AppData 实例字段锁覆盖完整性
    viol = _scan_class(_APPDATA, "AppData")
    if viol:
        for name, fields in viol:
            results.append(f"[FAIL] AppData.{name} 无锁访问 {fields}")
    else:
        results.append("[PASS] 形态① 实例字段：AppData 受保护字段均持对应锁（或已白名单）")

    # ② 形态 ②③：跨模块别名 + 守卫方法
    alias_viol = []
    covered = []
    for rel in SCAN_MODULES:
        v = _scan_module_aliases(rel)
        alias_viol.extend((rel,) + tuple(x) for x in v)
        if _module_has_alias(rel):
            covered.append(rel)
    if alias_viol:
        for rel, fn, alias, reason, lineno in alias_viol:
            results.append(
                f"[FAIL] 形态② {rel}:{lineno} {fn} 对共享别名 {alias} "
                f"做非原子操作（{reason}）且未持锁")
    else:
        results.append(
            f"[PASS] 形态②③ 模块别名 + 跨模块守卫："
            f"{len(covered)} 个模块、{len(PROP_FIELD)} 个共享出口，无非原子裸用")

    # ③ 扫描跳过记录的归属（X3）
    bad = _check_scan_skip_session()
    if bad:
        for rel, lineno, why in bad:
            results.append(f"[FAIL] 扫描跳过记录 {rel}:{lineno} {why}")
    else:
        results.append("[PASS] 扫描跳过记录已下沉为每次扫描私有会话，无进程级全局")

    # ④ 白名单不能被偷偷扩大：每条都必须带理由
    no_reason = [k for k, v in ALLOWLIST.items() if not v or len(v) < 4]
    if no_reason:
        results.append(f"[FAIL] ALLOWLIST 缺少理由: {no_reason}")
    else:
        results.append(f"[PASS] ALLOWLIST {len(ALLOWLIST)} 条均登记理由")

    # ⑤ 裸出口 property 的「返回活对象」契约（P2：从注释升级为机器校验）
    missing, broken, extra = _check_bare_property_contract()
    if missing or broken or extra:
        if missing:
            results.append(
                f"[FAIL] 裸出口 property 缺失/改名（契约基线里有、代码里没有）: {missing}")
        if broken:
            results.append(
                f"[FAIL] 裸出口 property 不再返回字段本体（会静默破坏零漂移语义）: "
                f"{broken}——如确需改动，须在 PROP_LIVE_EXEMPT 登记理由")
        if extra:
            results.append(
                f"[FAIL] 新增了裸出口 property 但未登记契约: {extra}"
                f"——须先加进 PROP_CONTRACT 并说明理由")
    else:
        results.append(
            f"[PASS] 裸出口 property 契约：{len(PROP_CONTRACT)} 个出口"
            f"均返回活对象，且无遗漏/无新增未登记（零漂移语义未被破坏）")

    # ⑤ 自证：扫描器能认出三种形态（防「扫描器自身失灵」的假阴性）
    selfcheck = _selfcheck()
    for ok, msg in selfcheck:
        results.append(f"[{'PASS' if ok else 'FAIL'}] 扫描器自证：{msg}")

    print("\n".join(results))
    failed = [r for r in results if r.startswith("[FAIL]")]
    print(f"\n合计: {len(results) - len(failed)} 通过 / {len(failed)} 失败")
    return 1 if failed else 0


def _module_has_alias(rel):
    path = os.path.join(_REPO_ROOT, *rel.split("/"))
    if not os.path.exists(path):
        return False
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute) \
                and isinstance(node.value.value, ast.Name) \
                and node.value.value.id == "app_data" \
                and node.value.attr in PROP_FIELD:
            return True
    return False


def _selfcheck():
    """扫描器自证：喂进去三种形态的样本，确认都能被识别

    指导书 §8.4「警惕验证脚本自身的假阴性」——只保证「扫出问题」不够，
    还要保证「问题摆在面前时真能扫出来」。
    """
    out = []
    sample = """
import app_data
_A = app_data.names_cache          # 形态②：共享别名

def f_locked_ok():
    with app_data.stocks_sub_chan_guarded("a", "5m"):   # 形态③
        return len(_A)

def f_bare_iter():                  # 应被判违规
    return len(_A)
"""
    tree = ast.parse(sample)
    # 挑出 f_bare_iter 单独扫描
    target = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "f_bare_iter")
    out.append((_dangerous_alias_use(next(s for s in ast.walk(target)
                                          if isinstance(s, ast.Call)),
                                     {"_A"}) is not None,
                "形态② 裸 len(别名) 能被识别为非原子"))

    # 形态③：with app_data.xxx_guarded(...) 内的使用应被判为已持锁
    guarded = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "f_locked_ok")
    with_node = next(s for s in ast.walk(guarded) if isinstance(s, ast.With))
    cx = with_node.items[0].context_expr
    ok3 = (isinstance(cx, ast.Call) and cx.func.attr in GUARDED_METHODS)
    out.append((ok3, "形态③ with app_data.*_guarded(...) 能识别为持锁"))

    # 形态① + ②③ 端到端自证：写一个**真实文件**走完整扫描链路。
    # 只测「扫出问题」不够，还要证「问题摆在面前时真能扫出来」——
    # 否则扫描器一旦失灵，产出的是永假的 PASS（指导书 §8.4）。
    probe = os.path.join(_HERE, "_selftest_probe.py")
    src = '''# -*- coding: utf-8 -*-
"""自证探针（临时文件，扫完即删）——故意放三种形态的违规与正例"""
from App.AppData import app_data

_pe_alias = app_data.pe_cache          # 形态②：共享别名
_names_alias = app_data.names_cache
_cache_alias = app_data.stocks_analysis_cache
_ck_lock = app_data.stocks_cache_lock  # 锁别名


def bad_bare_iter():                  # 违规：无锁全表迭代
    return set(_pe_alias.keys())


def bad_bare_len():                   # 违规：无锁 len
    return len(_pe_alias)


def bad_bare_del():                   # 违规：无锁下标删除
    del _names_alias["sh600519"]


def bad_wrong_lock():                 # 违规：持了锁，但**不是这字段的锁**
    with _ck_lock:
        return len(_pe_alias)


def good_under_lock():                # 正例：锁别名持**对应**锁
    with _ck_lock:
        return len(_cache_alias)


def good_under_guard():               # 正例：形态③ 跨模块守卫方法
    with app_data.stocks_sub_chan_guarded("sh600519", "5m"):
        return len(_cache_alias)


def good_point_query():               # 正例：点查原子
    return _pe_alias.get("sh600519")


class Probe:
    def bad_field(self):              # 形态①违规：self._names 无锁
        return len(self._names)

    def good_field(self):             # 形态①正例
        with self._meta_cache_lock:
            return len(self._names)
'''
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(src)

        # ②③：应抓到 4 个违规（含「持错锁」），且不误报 3 个正例
        v = _scan_module_aliases_abs(probe)
        got = sorted(fn for fn, _a, _r, _l in v)
        want = sorted(["bad_bare_iter", "bad_bare_len", "bad_bare_del",
                       "bad_wrong_lock"])
        out.append((got == want,
                    f"形态②③ 端到端抓到 {got}（期望 {want}）"))

        # ①：应只抓到 Probe.bad_field
        v1 = _scan_class(probe, "Probe")
        got1 = sorted(n for n, _f in v1)
        out.append((got1 == ["bad_field"],
                    f"形态① 端到端抓到 {got1}（期望 ['bad_field']）"))
    finally:
        if os.path.exists(probe):
            os.remove(probe)
    return out


if __name__ == "__main__":
    sys.exit(main())
