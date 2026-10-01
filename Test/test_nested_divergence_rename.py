# -*- coding: utf-8 -*-
"""
区间套判据改名护栏：`check_nested_diver` → `check_nesting_divergence`（2026-10-01）
================================================================================
为什么要有它
    改名本身是一次**纯标识符替换**（34 处，零行数漂移、零逻辑改动），做完当天必然全绿。
    真正的风险在以后：这个方法还被另外 8 个文件以注释的方式点到（`App/` 四处、
    `Chan.py`、`KLine/KLine_List.py`、`Test/` 两处），注释里留着旧名 = 挂着 8 个
    **悬空符号** —— 照着 grep 一无所获、照着抄就是 AttributeError。同类教训在这个仓库
    发生过两次（2026-09-10 术语「腿」清理后回潮 15 处，才有 p26 术语护栏）。
    护栏比「下次记住」可靠：写回旧名的那一刻，门禁就红。

第三处拼写
    仓库里原本还散着 `check_nested_divergence`（`Chan.py` 与 `KLine/KLine_List.py`
    的注释），它既不等于旧名、也从未指向任何真实符号（一直是悬空的）。本次一并收敛到
    新名，故**两个旧拼写都在禁止之列**。

刻意不改：`Docs/chan-arch-concurrency-v7.0.html`
    带版本号的历史架构快照 = 当时的交付记录，改它就是篡改历史（§2 明列 `Docs/**`
    不进术语扫描范围）。但「豁免」必须可见 —— 那处残留挂在 [3] 的定额之下，
    哪天真改了那份文档，[3a] 会红提醒同步，不会无声无息。

为什么不顺便验行为
    判据本体（笔 / 中枢 / MACD 背驰那一套计算）由快照类回归组件盯着；本文件只钉
    「名字这一层契约」：**类上有、形参对、调用点在、旧名零残留**。行为不变是这次
    改名的**前提**（零逻辑改动），不是本护栏要再证一遍的东西。

用法：python Test/test_nested_divergence_rename.py（退出码 0 = 通过）
"""
import ast
import io
import inspect
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
THIS = os.path.abspath(__file__)

NEW = "check_nesting_divergence"
OLD_SHORT = "check_nested_diver"
OLD_VARIANT = "check_nested_divergence"

# 负向断言：OLD_SHORT 不得是更长标识符的一部分（否则 OLD_VARIANT 会被重复计一次）
PAT_SHORT = re.compile(r"(?<![A-Za-z_])" + OLD_SHORT + r"(?![A-Za-z_])")

SOURCE = "BuySellPoint/BSPointList.py"
HOLDER_CLASS = "CMyBSPointList"

# 改名当时实际动过的文件（[3b] 覆盖面自检用：有人把扫描范围改窄 ⇒ 护栏静默失效）
TOUCHED = [
    "App/AppConfig.py",
    "App/AppData.py",
    "App/AppEngine.py",
    "App/AppSSE.py",
    "BuySellPoint/BSPointList.py",
    "Chan.py",
    "KLine/KLine_List.py",
    "Test/snapshot_runner.py",
    "Test/test_futures_sub_key.py",
]

# [3] 定额豁免：有些文件**必须**提到旧名 —— 本文件自己就是禁用词的定义处、
#     门禁注册注释要写清映射关系、`Docs/` 版本号快照刻意不改。豁免是**定额**而非
#     整文件放行：多写一处也红，少写了（文档被改过、注解被删）同样红 —— 免得豁免
#     悄悄过期。形态：`相对路径 -> (短旧名处数, 第三拼写处数)`
EXPECTED = {
    "Test/run_all.py": (1, 0),
    "Docs/chan-arch-concurrency-v7.0.html": (1, 0),
}

SCAN_EXT = (".py", ".md", ".js", ".html", ".json", ".txt", ".yml", ".yaml", ".cfg")
SKIP_DIR = {".git", ".venv", "__pycache__", ".idea", "Image", "node_modules",
            ".pytest_cache", "State", "Trading/State"}

_passed = 0
_failed = 0


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  \u2713 %s" % name)
    else:
        _failed += 1
        print("  \u2717 %s -> got %r, want %r" % (name, got, want))


def read(rel):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        return None
    return io.open(p, encoding="utf-8", errors="ignore").read()


print("\n[0] 元护栏：先证明检测器有效（否则「零命中」可能只是假装看不见）")
check("[0a] PAT_SHORT 命中旧名样例",
      bool(PAT_SHORT.search("is_diver = self.check_nested_diver(bi_list, zs_list)")), True)
check("[0b] PAT_SHORT 不把第三种拼写当成短名的另一处（避免重复计数）",
      bool(PAT_SHORT.search("self.market_type = market_type  # → check_nested_divergence")), False)
check("[0c] 新名不被任一检测器命中",
      bool(PAT_SHORT.search(NEW)) or OLD_VARIANT in NEW, False)
check("[0d] 相邻单词不误伤（check_nested_diversified）",
      bool(PAT_SHORT.search("check_nested_diversified = 1")), False)

print("\n[1] 类层契约：持有类上有新名、形参不变；两个旧拼写都不是属性")
sys.path.insert(0, ROOT)
try:
    import BuySellPoint.BSPointList as mod
    cls = getattr(mod, HOLDER_CLASS, None)
    check("[1a] 持有类可导入", cls is not None, True)
    new_fn = getattr(cls, NEW, None)
    check("[1b] %s.%s 存在且可调用" % (HOLDER_CLASS, NEW), callable(new_fn), True)
    check("[1c] 形参仍是 (bi_list, zs_list)",
          [p for p in inspect.signature(new_fn).parameters if p != "self"]
          if callable(new_fn) else None,
          ["bi_list", "zs_list"])
    check("[1d] 旧名 %s 已不再是类属性" % OLD_SHORT, hasattr(cls, OLD_SHORT), False)
    check("[1e] 旧名 %s 已不再是类属性" % OLD_VARIANT, hasattr(cls, OLD_VARIANT), False)
except Exception as e:  # pragma: no cover
    check("[1x] import %s 失败" % SOURCE, "%s: %s" % (type(e).__name__, e), "no exception")

print("\n[2] 源码层：定义与调用点都用新名（防「只改定义、漏改调用」的半吊子改名）")
src = read(SOURCE) or ""
tree = ast.parse(src)
defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == NEW]
calls = [n.func.attr for n in ast.walk(tree)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and n.func.attr == NEW]
check("[2a] 定义处是新名", len(defs), 1)
check("[2b] 至少一处 self.%s(...) 调用点" % NEW, len(calls) >= 1, True)

print("\n[3] 全仓扫描：每个文件的旧名残留都等于定额（默认 0，豁免文件允许定额）")
scanned = []
counts = {}
for r, d, fs in os.walk(ROOT):
    rel_dir = os.path.relpath(r, ROOT).replace("\\", "/")
    if rel_dir == ".":
        rel_dir = ""
    d[:] = [x for x in d if x not in SKIP_DIR]
    for f in fs:
        if not f.endswith(SCAN_EXT):
            continue
        rel = (rel_dir + "/" + f) if rel_dir else f
        p = os.path.join(r, f)
        if os.path.abspath(p) == THIS:
            continue  # 本文件必然含禁用词（它就是禁则的定义处），跳过自身
        scanned.append(rel)
        t = io.open(p, encoding="utf-8", errors="ignore").read()
        n_short = len(PAT_SHORT.findall(t))
        n_var = len(re.findall(OLD_VARIANT, t))
        if n_short or n_var:
            counts[rel] = (n_short, n_var)

mismatch = []
for rel in sorted(set(counts) | set(EXPECTED)):
    got = counts.get(rel, (0, 0))
    want = EXPECTED.get(rel, (0, 0))
    if got != want:
        mismatch.append("%s got=%s want=%s" % (rel, got, want))
check("[3a] 无一文件偏离定额", mismatch, [])
check("[3b] 覆盖面自检：改过的 9 个文件都在扫描集合里",
      sorted(set(TOUCHED) - set(scanned)), [])
check("[3c] 覆盖面自检：扫描文件数 >= 100（范围没被偷偷改窄）", len(scanned) >= 100, True)

print("\n[4] 定额豁免的存在性：豁免文件仍在树里（否则说明豁免已过期，应删条目）")
check("[4] 豁免所指文件都存在", sorted(set(EXPECTED) - set(scanned)), [])

print("\n" + "=" * 60)
print("nested_divergence_rename: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
