# -*- coding: utf-8 -*-
"""静态 import 审计：检查全仓 `from X import a, b` 中的 a/b 是否在 X 里真实存在。

用途：抓「改名/删除只改一半」造成的失效引用（本次 P0 的形态）。
方法：AST 提取每个模块的顶层可导出名，再逐个校验 import 目标。
"""
import os
import sys
import ast

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "build", "dist",
             ".pytest_cache", ".mypy_cache", "Image", "Docs"}


def collect_py(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".py"):
                out.append(os.path.join(dp, fn))
    return out


def top_level_names(tree):
    """模块顶层可被 import 的名字。"""
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
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Try):          # try/except ImportError 里的兜底定义
            for sub in (node.body + [x for h in node.handlers for x in h.body]):
                if isinstance(sub, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
        elif isinstance(node, ast.If):           # if TYPE_CHECKING: / if not hasattr(...)
            for sub in node.body + node.orelse:
                if isinstance(sub, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
    # 动态导出（__all__ / globals().update / setattr）会让静态判断失真 → 标记
    return names


def has_star_or_dynamic(tree):
    """模块是否含 *导入 或 __all__ 或 getattr 动态导出，用于降低误报。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return True
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    return True
    src = ast.dump(tree)
    return ("globals()" in src) or ("setattr" in src)


def module_index(root, files):
    """模块名 -> (定义集合, 是否动态)"""
    idx = {}
    for fp in files:
        rel = os.path.relpath(fp, root).replace("\\", "/")
        if rel.endswith("/__init__.py"):
            mod = rel[:-len("/__init__.py")].replace("/", ".")
        elif rel == "__init__.py":
            mod = ""
        else:
            mod = rel[:-3].replace("/", ".")
        try:
            tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read(), filename=rel)
        except SyntaxError as e:
            idx[mod] = (set(), True, "SYNTAX_ERROR: {}".format(e))
            continue
        idx[mod] = (top_level_names(tree), has_star_or_dynamic(tree), None)
    return idx


def resolve(root, files, idx, from_mod, level, cur_file):
    """解析 `from X import` 的 X 对应哪个模块名。"""
    if level == 0:
        return from_mod if from_mod else None
    # 相对导入：以当前文件所在包为基准
    rel = os.path.relpath(cur_file, root).replace("\\", "/")
    parts = rel.split("/")[:-1]
    if rel.endswith("__init__.py"):
        pass
    else:
        parts = parts  # 当前文件的包 = 所在目录
    if not parts:
        return from_mod
    base = parts[:len(parts) - (level - 1)] if level > 1 else parts
    prefix = ".".join(base)
    return (prefix + "." + from_mod) if from_mod else prefix


def main():
    root = sys.argv[1]
    files = collect_py(root)
    idx = module_index(root, files)

    problems = []
    checked = 0
    for fp in files:
        rel = os.path.relpath(fp, root).replace("\\", "/")
        try:
            tree = ast.parse(open(fp, encoding="utf-8", errors="replace").read(), filename=rel)
        except SyntaxError:
            problems.append((rel, 0, "文件语法错误，无法解析"))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = resolve(root, files, idx, node.module or "", node.level, fp)
            if not mod:
                continue
            if mod not in idx:
                # 可能是第三方库或未下载的包
                if mod.split(".")[0] in {p.split(".")[0] for p in idx}:
                    problems.append((rel, node.lineno,
                                     "`from {} import ...` → 模块 `{}` 在本仓库内找不到".format(mod, mod)))
                continue
            defs, dynamic, err = idx[mod]
            if err:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                checked += 1
                if a.name in defs or dynamic:
                    continue
                # 合法形态之二：a 本身是 mod 下的子模块（from App import AppEngine）
                if "{}.{}".format(mod, a.name) in idx:
                    continue
                problems.append((rel, node.lineno,
                                 "`from {} import {}` → `{}` 在 `{}` 中既非定义、也非子模块".format(
                                     mod, a.name, a.name, mod)))

    print("扫描 {} 个 .py，校验 {} 条 import".format(len(files), checked))
    print("=" * 78)
    if not problems:
        print("✅ 未发现失效 import")
    else:
        print("⚠ 发现 {} 处疑点：".format(len(problems)))
        for rel, ln, msg in problems:
            print("  {}:{}".format(rel, ln))
            print("      {}".format(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
