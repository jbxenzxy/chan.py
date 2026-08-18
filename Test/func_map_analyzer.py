# -*- coding: utf-8 -*-
"""
阶段 2.6：App/AppEngine.py 函数级迁移映射 —— AST 静态分析器
=====================================================================
对 App/AppEngine.py 做纯静态分析（不 import、零副作用）：
（阶段 10.1：my_chan_main.py 已删除，引擎迁入 App/AppEngine.py，
  分析目标随之下移；DataAPI/ElTdxAPI.py 由 func_map_check 合并扫描）

  ① 全部顶层函数：名称/行区间/参数/首行 docstring
  ② 全部模块级状态：模块级赋值目标（排除 import 与函数定义）
  ③ 每个函数的依赖边：
       - 调用了哪些同模块顶层函数（Call.func 为 Name 且命中函数表）
       - 读/写了哪些模块级状态（Load/Store 于模块级状态名，
         含 global 声明）
  ④ 反向依赖（谁依赖我）+ 被依赖数
  ⑤ 纯函数判定：不读写任何模块级状态且不调用非纯函数

输出 JSON：函数表 + 状态表 + 边表（供映射文档生成与同步校验用）。
"""
import ast
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO_ROOT, "App", "AppEngine.py")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "func_map_auto.json")


def _ann_str(node):
    """参数注解 → 字符串（无注解返回 None）"""
    try:
        return ast.unparse(node)
    except Exception:
        return None


def analyze(path=TARGET):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    # ── ① 顶层函数 ──
    funcs = {}          # name -> info
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            args = node.args
            pos = [a.arg for a in args.posonlyargs + args.args]
            defaults = [None] * (len(pos) - len(args.defaults)) + \
                       [ast.unparse(d) for d in args.defaults]
            params = [f"{p}={d}" if d is not None else p
                      for p, d in zip(pos, defaults)]
            params += [f"*{a.arg}" for a in args.vararg] if args.vararg else []
            params += [f"{a.arg}={ast.unparse(d)}" if d else a.arg
                       for a, d in zip(args.kwonlyargs, args.kw_defaults)]
            params += [f"**{args.kwarg.arg}"] if args.kwarg else []
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            funcs[node.name] = {
                "line_start": node.lineno,
                "line_end": node.end_lineno,
                "n_lines": node.end_lineno - node.lineno + 1,
                "params": params,
                "doc": doc[0][:80] if doc else "",
            }

    # ── ② 模块级状态（赋值目标；import 名与 __all__ 等除外）──
    states = set()
    state_info = {}
    for node in tree.body:
        tgts = []
        if isinstance(node, ast.Assign):
            tgts = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            tgts = [node.target.id]
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            tgts = [node.target.id]        # 模块级 += 等（罕见）
        for t in tgts:
            if t.startswith("__"):
                continue
            states.add(t)
            state_info[t] = {
                "line": node.lineno,
                "kind": ("mutated" if isinstance(node, ast.AugAssign) else "init"),
                "value_hint": _value_hint(node),
            }

    # 排除与函数同名的名字（不会被当作状态）
    states -= set(funcs)
    state_info = {k: v for k, v in state_info.items() if k in states}

    # ── ③ 每函数依赖边 ──
    for name, info in funcs.items():
        fn_node = _find_func(tree, name)
        calls, reads, writes, global_decl = set(), set(), set(), set()
        for n in ast.walk(fn_node):
            if isinstance(n, ast.Global):
                global_decl.update(n.names)
            elif isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name) and f.id in funcs and f.id != name:
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                        and f.value.id in funcs and f.id in {"append", "extend", "update"}:
                    calls.add(f.value.id)   # 对函数返回容器继续操作（信息性）
            elif isinstance(n, ast.Name):
                if n.id in states:
                    (writes if isinstance(n.ctx, ast.Store) else reads).add(n.id)
        # global 声明但未直接 Store 的（global x 后 x=... 是 Store，已覆盖）
        info["calls"] = sorted(calls - {name})
        info["reads"] = sorted(reads)
        info["writes"] = sorted(writes)
        info["global_decl"] = sorted(global_decl)

    # ── ④ 反向依赖 ──
    for name in funcs:
        funcs[name]["depended_by"] = sorted(
            other for other in funcs if name in funcs[other]["calls"])

    # ── ⑤ 纯函数判定（递归闭包：不触状态，且调用的也都是纯函数）──
    def _pure(name, seen=None):
        seen = seen or set()
        if name in seen:
            return True                      # 环：按纯处理，交由人工复核
        seen.add(name)
        i = funcs[name]
        if i["reads"] or i["writes"] or i["global_decl"]:
            return False
        return all(_pure(c, seen) for c in i["calls"])

    for name in funcs:
        funcs[name]["pure"] = _pure(name)

    return {
        "file": os.path.basename(path),
        "n_funcs": len(funcs),
        "n_states": len(state_info),
        "funcs": funcs,
        "states": state_info,
    }


def _find_func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _value_hint(node):
    """模块级赋值的值提示（截断）"""
    v = getattr(node, "value", None)
    if v is None:
        return ""
    try:
        s = ast.unparse(v)
        return s[:60] + ("…" if len(s) > 60 else "")
    except Exception:
        return type(v).__name__


if __name__ == "__main__":
    result = analyze()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, sort_keys=True)
    fs, ss = result["funcs"], result["states"]

    print(f"顶层函数 {len(fs)} 个，模块级状态 {len(ss)} 个 → {OUT}\n")

    print("── 被依赖最多的 15 个函数（迁移优先级输入）──")
    for name, i in sorted(fs.items(), key=lambda kv: -len(kv[1]["depended_by"]))[:15]:
        print(f"  {len(i['depended_by']):3d} ← {name}")

    print("\n── 纯函数（无状态依赖，可直接先行迁移）──")
    pures = [n for n, i in fs.items() if i["pure"]]
    print(f"  共 {len(pures)} 个: {', '.join(sorted(pures)[:30])}{'…' if len(pures) > 30 else ''}")

    print("\n── 被读写的状态（按读写函数数排序，前 15）──")
    rank = []
    for s in ss:
        r = [n for n, i in fs.items() if s in i["reads"]]
        w = [n for n, i in fs.items() if s in i["writes"]]
        rank.append((len(r) + len(w), s, len(r), len(w)))
    for tot, s, nr, nw in sorted(rank, reverse=True)[:15]:
        print(f"  {tot:3d} 次  {s}  (读{nr}/写{nw})")
