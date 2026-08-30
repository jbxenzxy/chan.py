# -*- coding: utf-8 -*-
"""锁覆盖**完整性**静态守护（无第三方依赖，纯 ast 扫描）

为什么需要它
------------
`test_lock_v5_guards.py` 用计数包装锁断言「这些入口持锁」——它问的是
**「这扇门有没有锁」**，没问**「是不是所有门都上了锁」**。审计 v6 里
`_scan_skip_log` 整个逃出登记表、`replace_names` / `get_annotated_codes`
无锁遍历，都是靠这个盲区一路溜过去的。

本用例反向做：从**受保护字段**出发，遍历 AppData 的每一个方法，凡是
引用了受保护字段却没持对应锁的，一律报告。新增方法若忘了加锁会立刻
失败，而不是等线上 500。

判定口径（函数级，非节点级）
----------------------------
一个方法引用受保护字段时，满足任一条件即视为已保护：
  ① 方法体内存在 `with self.<对应锁>:`（含嵌套 with）；
  ② 方法名以 `_locked` 结尾——沿用项目既有约定「内部方法，调用方须持锁」
     （如 `_cache_put_locked` / `_evict_dual_overflow_locked`）；
  ③ 在 `ALLOWLIST` 中显式登记并写明理由。

已知近似：判定是**函数级**的，若某方法在 A 分支持锁、在 B 分支访问字段，
本用例会判为已保护。这比节点级判定宽松，但足以拦住「整段无锁」这类真实
缺陷；如需更严，可改成节点级（需处理嵌套 with 与 try/finally）。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))            # .../Test
_REPO_ROOT = os.path.dirname(_HERE)                            # 仓库根

# ── 受保护字段 → 保护它的锁 ──────────────────────────────────────────
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

# ── 显式白名单：方法名 → 理由 ────────────────────────────────────────
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


def _scan_class(path, class_name):
    """返回 [(方法名, [未持锁的字段…])]"""
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


def _check_scan_skip_log():
    """_scan_skip_log 只能经加锁访问器碰（审计 P1-5）"""
    path = os.path.join(_REPO_ROOT, "App", "AppScan.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    # 访问器内部允许直接操作列表本体
    accessors = {"append_scan_skip", "clear_scan_skip",
                 "scan_skip_snapshot", "scan_skip_count"}
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in accessors:
            continue
        for sub in ast.walk(node):
            # _scan_skip_log.append(...) / .clear() / 直接下标 / 直接遍历
            if (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "_scan_skip_log"):
                bad.append((node.name, sub.attr))
    return sorted(set(bad))


def main():
    results = []

    # ① AppData 锁覆盖完整性
    viol = _scan_class(os.path.join(_REPO_ROOT, "App", "AppData.py"), "AppData")
    if viol:
        for name, fields in viol:
            results.append(f"[FAIL] AppData.{name} 无锁访问 {fields}")
    else:
        results.append("[PASS] AppData 全部方法：受保护字段均持对应锁（或已白名单）")

    # ② _scan_skip_log 不被裸碰
    bad = _check_scan_skip_log()
    if bad:
        results.append(f"[FAIL] _scan_skip_log 被直接操作（应经访问器）: {bad}")
    else:
        results.append("[PASS] _scan_skip_log 仅经加锁访问器操作")

    # ③ 白名单不能被偷偷扩大：受白名单保护的方法必须仍在 ALLOWLIST 里
    #    （上面 ① 已隐含校验；这里断言 ALLOWLIST 每条都带理由）
    no_reason = [k for k, v in ALLOWLIST.items() if not v or len(v) < 4]
    if no_reason:
        results.append(f"[FAIL] ALLOWLIST 缺少理由: {no_reason}")
    else:
        results.append(f"[PASS] ALLOWLIST {len(ALLOWLIST)} 条均登记理由")

    print("\n".join(results))
    failed = [r for r in results if r.startswith("[FAIL]")]
    print(f"\n合计: {len(results) - len(failed)} 通过 / {len(failed)} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
