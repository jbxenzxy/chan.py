# -*- coding: utf-8 -*-
"""兜底扫描：找出「旧版有、新版已删」的顶层符号，检查新版里是否还有活引用。

覆盖 import 之外的引用形态（属性访问、裸名字），与 audit_imports.py 互补。
"""
import os
import sys
import ast
from collections import defaultdict

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "build", "dist",
             ".pytest_cache", ".mypy_cache", "Image"}

# 这些目录的符号多为框架/库性质，改名噪音大，单独标注
CORE_HINT = ("Bi", "Seg", "ZS", "BuySellPoint", "Combiner", "Common", "KLine", "Math", "Plot")


def collect(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".py"):
                out.append(os.path.join(dp, fn))
    return out


def defs_of(fp):
    """模块顶层定义名（class/def/大写常量/ClassVar）。"""
    try:
        tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read(), filename=fp)
    except SyntaxError:
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def live_refs(root):
    """新版全仓的活引用：名字求值 + import + 属性尾名。"""
    hits = defaultdict(list)
    for fp in collect(root):
        rel = os.path.relpath(fp, root).replace("\\", "/")
        try:
            tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read(), filename=rel)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                hits[n.id].append((rel, n.lineno, "Name"))
            elif isinstance(n, ast.Attribute):
                hits[n.attr].append((rel, n.lineno, "Attribute"))
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    hits[a.name].append((rel, n.lineno, "ImportFrom"))
            elif isinstance(n, ast.Import):
                for a in n.names:
                    hits[a.name.split(".")[-1]].append((rel, n.lineno, "Import"))
    return hits


def main():
    old_root, new_root = sys.argv[1], sys.argv[2]

    old_defs = {}
    for fp in collect(old_root):
        rel = os.path.relpath(fp, old_root).replace("\\", "/")
        # 只关注应用层（核心缠论模块是外部库，改名噪音大）
        if rel.startswith(CORE_HINT):
            continue
        for d in defs_of(fp):
            old_defs.setdefault(d, []).append(rel)

    new_defs = set()
    for fp in collect(new_root):
        new_defs |= defs_of(fp)

    removed = {d: v for d, v in old_defs.items() if d not in new_defs}
    print("旧版顶层符号 {} 个，新版缺失（删除/改名）{} 个".format(len(old_defs), len(removed)))

    hits = live_refs(new_root)

    # 过滤噪声：单词太短、纯下划线、常见局部名
    NOISE = {"x", "y", "z", "i", "j", "k", "n", "m", "t", "s", "p", "q", "a", "b", "c",
             "e", "f", "g", "d", "v", "h", "w", "r", "o", "self", "cls", "args", "kwargs",
             "key", "value", "item", "data", "result", "config", "cfg", "name", "path",
             "code", "date", "time", "price", "count", "idx", "index", "row", "rows",
             "df", "np", "pd", "os", "sys", "re", "json", "time_", "log", "logger"}

    suspicious = []
    for d, where in sorted(removed.items()):
        if d in NOISE or len(d) < 3:
            continue
        if d.startswith("_") and len(d) < 5:
            continue
        if d not in hits:
            continue
        refs = hits[d]
        # 只保留「跨文件」引用（同一文件内自引不算遗漏）——但符号已从全仓消失，
        # 故任何引用都值得看；此处按引用文件去重
        files = sorted({r[0] for r in refs})
        suspicious.append((d, where[:3], files, refs))

    print("=" * 78)
    if not suspicious:
        print("✅ 未发现已删符号的残留活引用")
    else:
        print("⚠ 以下 {} 个已删/改名符号在新版中仍有引用：".format(len(suspicious)))
        for d, where, files, refs in suspicious:
            print("\n  ▸ {}   （旧定义于 {}）".format(d, ", ".join(where)))
            print("      引用文件 {} 个：".format(len(files)))
            for rel, ln, kind in refs[:12]:
                print("        {}:{}  [{}]".format(rel, ln, kind))
            if len(refs) > 12:
                print("        ... 另有 {} 处".format(len(refs) - 12))
    return 0


if __name__ == "__main__":
    sys.exit(main())
