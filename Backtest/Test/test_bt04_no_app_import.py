# -*- coding: utf-8 -*-
"""
分层单向依赖契约（Backtest/Test/test_bt04_no_app_import.py）
=============================================================
把设计文档 §5.1 层表 + §5.9 R31 从**文字约定**变成**可执行断言**。

§5.1 给 `Backtest/` **生产代码**的允许依赖是**闭集**：

    Chan / Common / DataAPI / BuySellPoint / ChanConfig
    Trading.Strategy / Trading.Infra
    （`Backtest/` 自身）

**禁**：
  · `App*`（R31：`Backtest/` 不得 import App，AST 契约测试钉死）
  · `Frontend`（前端资源不属于离线回测层）
  · `Test/`（生产代码依赖测试树 ⇒ 一是违反本表，二是交付出去的
    `Backtest/` 单独一份**跑不起来**）
  · `Trading` 下除 `Strategy` / `Infra` 之外的子模块（`Engine` 是 App 的下一层）

`Backtest/Test/` 与 `Backtest/Exp/` **豁免**（§5.1 的适用范围是**生产代码**）：
  · `Test/` —— 比对测试必须 import `App` 才做得了（见 bt02 / bt03 / bt05）；
  · `Exp/` —— **离线回测实验脚本层**。它存在的意义就是"与页面同源"，因此必然
    要现读 App 层配置 SSOT（`stocks_lookback_config` / `full_data_mode` /
    `saved_point_file` / `forward_adjust_enabled`）。为了躲开本契约而把这几个数字
    **抄一份进自己的模块**确实能让这里变绿，但那是**第二个 SSOT** —— App 层改一次
    就静默漂移，回测照跑、数字照出、序列却已不是页面那条，比"实验脚本 import App"
    危险得多。⇒ 本契约管的是**交付出去能独立运行的回测内核**，不是"实验层不许
    看 App"；豁免 Expr的同时，由 `Backtest/Exp/common/tdx_source.py` 的模块注释
    钉住"不许抄数字、取不到就报错"。

为什么用 AST 而不是 grep
---------------------------------------------------------------------
`import App` 可能写在**函数体内**（本包与仓库的惰性导入风格就是函数内 import），
也可能是别名 `import App.AppEngine as AE`，还可能出现在 docstring / 字符串里
（**不该**算违规）。AST 精确定位 `Import` / `ImportFrom` 节点并给出 `文件:行`。

⚠ Windows 大小写不敏感：本仓根目录**真的存在** `Math/` 目录 ⇒ 用
  `os.path.isdir(ROOT + "/math")` 判断"`math` 是不是本地模块"会**假阳性**（stdlib
  `math` 被误判成本地）。所以本地模块集合必须按 `os.listdir` 的**原始大小写**
  精确匹配。

覆盖
---------------------------------------------------------------------
  ① 零 `App` 依赖（R31 硬约束）
  ② 零 `Frontend` 依赖
  ③ 零 `Test/` 依赖（生产代码不得依赖测试树）
  ④ 全部 repo 本地顶层依赖 ∈ 层表白名单（拦住"新加一个上层依赖"）
  ⑤ `Trading` 只许 `Strategy` / `Infra`（前缀匹配，非精确等值）
  ⑥ 扫描器判别力自证：喂含 `App` / `Test`（别名 / 函数体内 / `from` 形式）的源码
     必须被精确标出；喂合法源码必须零报警 —— 证明④不是恒真式
  ⑦ 豁免范围**只有** `Backtest/Test/` 与 `Backtest/Exp/`（两者都不是交付内核），
     豁免表里写的目录必须在磁盘上**真实存在**（键悬空 ⇒ 读者误以为被豁免），
     且被扫文件集非空（防空扫全绿）；
     `Exp/` 的豁免另由 ⑦d/⑦e 钉成**有界登记**（白名单现在只有 `tdx_source.py`，
     实验层再引一处就红 ⇒ 逼你去查它是不是顺手抄了第二份 SSOT）
  ⑧ 实际本地依赖集非空并逐一打印披露

跑法：`python Backtest/Test/test_bt04_no_app_import.py`（退出码 0/1 即判决）
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)

# 豁免目录（相对 `Backtest/` 的**首层**目录名）。R31 的适用范围是**生产代码**：
#   Test —— 比对测试层（bt02 / bt03 / bt05 必须 import App 才做得了，§5.1 v1.14）；
#   Exp  —— 离线回测实验脚本层（见 docstring：宁可豁免 import，**不许**为躲契约
#           把 App 层的数字抄一份进实验代码）。
EXEMPT_REL_DIRS = {"Test", "Exp"}

# 层表白名单：Backtest/ 生产代码允许依赖的**仓库本地**顶层模块（§5.1 闭集）
LAYER_ALLOW = {
    "Backtest",        # 本包自身
    "Chan", "Common", "DataAPI", "BuySellPoint", "ChanConfig",
    "Trading",         # 仅 Strategy / Infra —— 见 ⑤
}
# `Trading` 下的二级白名单（**前缀**匹配）
TRADING_ALLOW_PREFIX = ("Trading", "Trading.Strategy", "Trading.Infra")

# 明令禁止的上层/测试树依赖 → 各自的断言编号与说明
FORBIDDEN = (
    ("App",      "①", "R31：Backtest/ 不得 import App（§5.1 层表 / §5.9）"),
    ("Frontend", "②", "前端资源目录不属于离线回测层"),
    ("Test",     "③", "生产代码不得依赖测试树（违反 §5.1 闭集，且交付件将无法独立运行）"),
)

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[PASS] %s" % name)
    else:
        FAIL += 1
        print("[FAIL] %s" % name)
        if detail:
            print("        " + str(detail).replace("\n", "\n        "))


# ─────────────────────────────────────────────────────────────────────
# 仓库本地顶层模块集合（**按 os.listdir 原始大小写**，见 docstring 的大小写警告）
# ─────────────────────────────────────────────────────────────────────
def _repo_local_tops():
    tops = set()
    for name in os.listdir(ROOT):
        if name.startswith((".", "_")):
            continue
        p = os.path.join(ROOT, name)
        if os.path.isdir(p):
            tops.add(name)                       # Math/ ◀ 大写，不会撞 stdlib math
        elif name.endswith(".py"):
            tops.add(name[:-3])
    return tops


LOCAL_TOPS = None          # 惰性（首次 scan 时填）


def is_repo_local(top):
    global LOCAL_TOPS
    if LOCAL_TOPS is None:
        LOCAL_TOPS = _repo_local_tops()
    return top in LOCAL_TOPS


# ─────────────────────────────────────────────────────────────────────
# 扫描器（纯函数：源码字符串 → [(lineno, dotted_module)])
# ─────────────────────────────────────────────────────────────────────
def scan_source(src):
    """抽出所有 import 的**绝对**模块名（相对导入 level>0 跳过，它不出仓库）。"""
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                out.append((node.lineno, al.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue                      # 相对导入（本包内）
            if node.module:
                out.append((node.lineno, node.module))
    return out


def _prod_files():
    """Backtest/ 下的生产代码（.py，排除豁免目录与缓存目录）。"""
    files = []
    for dirpath, dirnames, filenames in os.walk(BACKTEST_DIR):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        rel = os.path.relpath(dirpath, BACKTEST_DIR).replace("\\", "/")
        if rel != "." and rel.split("/")[0] in EXEMPT_REL_DIRS:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    return sorted(files)


def main():
    print("=" * 68)
    print("分层单向依赖契约（Backtest/Test/test_bt04_no_app_import.py）")
    print("=" * 68)

    # ── ⑥ 判别力自证（先证扫描器不是恒真式）──────────────────────
    probe_bad = (
        "import os\n"
        "import App.AppEngine as AE\n"          # 别名 import
        "from App.AppTPSL import x\n"           # from 形式
        "from Test.gen_fixtures import y\n"     # 测试树
        "import Frontend.app\n"                 # 前端
        "def f():\n"
        "    import App.Orch\n"                  # 函数体内
        "    from .Local import z\n"             # 相对导入 —— 不该标
    )
    hits = sorted(m for _, m in scan_source(probe_bad)
                  if m.split(".")[0] in {t for t, _, _ in FORBIDDEN})
    check("⑥a 扫描器能标出 App / Frontend / Test（别名 / 函数体内 / from 形式）",
          hits == ["App.AppEngine", "App.AppTPSL", "App.Orch",
                   "Frontend.app", "Test.gen_fixtures"],
          "got=%r" % hits)

    probe_good = (
        "import os\n"
        "import math\n"                          # ◀ stdlib，不得误判为本地 Math/
        "from typing import Any\n"
        "from Chan import CChan\n"
        "from Trading.Strategy.Exit import P\n"
        "from .ExitParams import X\n"
        "def f():\n"
        "    from Common.CEnum import KL_TYPE\n"
    )
    bad_mods = [m for _, m in scan_source(probe_good)
                if m.split(".")[0] in {t for t, _, _ in FORBIDDEN}]
    bad_local = [m for _, m in scan_source(probe_good)
                 if is_repo_local(m.split(".")[0])
                 and m.split(".")[0] not in LAYER_ALLOW]
    check("⑥b 扫描器对合法源码零报警（相对导入 / stdlib / 白名单内不算违规）",
          not bad_mods and not bad_local,
          "bad_mods=%r bad_local=%r" % (bad_mods, bad_local))

    # ── ⑦ 豁免范围与扫描面 ───────────────────────────────────────
    files = _prod_files()
    check("⑦a 生产代码文件集非空（防空扫全绿）", bool(files),
          "扫到 %d 个 .py" % len(files))
    leaked = []
    for p in files:
        r = os.path.relpath(p, BACKTEST_DIR).replace("\\", "/")
        if r.split("/")[0] in EXEMPT_REL_DIRS:
            leaked.append(r)
    check("⑦b 豁免目录的文件不在生产扫描面内", not leaked,
          "漏进扫描面：%r" % leaked)
    missing = sorted(d for d in EXEMPT_REL_DIRS
                     if not os.path.isdir(os.path.join(BACKTEST_DIR, d)))
    check("⑦c 豁免目录在磁盘上真实存在（键不悬空）", not missing,
          "豁免表里写了不存在的目录：%r" % missing)

    # ── ⑦d Exp 层豁免是**有界**的：每处上层依赖都必须显式登记 ──────
    # 豁免 ≠ 放任。R31 真正要防的不是"实验脚本 import App"，而是"为了躲契约把 App 的
    # 数字抄一份到实验代码里"。所以这里反过来钉：Exp 下凡是引了 App / Frontend / Test
    # 的文件，都必须在 `EXP_APP_ALLOW` 里点名登记 ⇒ 新增实验脚本再引一处就红，
    # 逼着先看一眼它有没有顺手抄第二个 SSOT（见 tdx_source.py 的模块注释）。
    EXP_APP_ALLOW = {"Exp/common/tdx_source.py"}
    exp_hits = []
    exp_dir = os.path.join(BACKTEST_DIR, "Exp")
    if os.path.isdir(exp_dir):
        for _dp, _dns, _fns in os.walk(exp_dir):
            _dns[:] = [d for d in _dns if d not in ("__pycache__", ".pytest_cache")]
            for fn in sorted(_fns):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(_dp, fn)
                rel = os.path.relpath(p, BACKTEST_DIR).replace("\\", "/")
                with open(p, "r", encoding="utf-8") as f:
                    for lineno, mod in scan_source(f.read()):
                        if mod.split(".")[0] in {t for t, _, _ in FORBIDDEN}:
                            exp_hits.append("%s:%d  %s" % (rel, lineno, mod))
        unregistered = sorted({h.split(":")[0] for h in exp_hits} - EXP_APP_ALLOW)
        check("⑦d Exp 层的上层依赖全在登记白名单内（新增再引一处即红）",
              not unregistered,
              "未登记：%r\n命中：%s" % (unregistered, "\n".join(exp_hits)))
        hit_files = {h.split(":")[0] for h in exp_hits}
        # 悬空 = 登记了既没命中、磁盘上也不存在的文件（意味着该条例已经名存实亡）
        dangling = sorted(
            rel for rel in EXP_APP_ALLOW
            if rel not in hit_files
            and not os.path.isfile(os.path.join(BACKTEST_DIR, rel)))
        check("⑦e 白名单条目不悬空（登记的条例要么正在生效、要么文件已被删）",
              not dangling,
              "登记了但既无命中、文件也不存在：%r" % dangling)
        print("     Exp 层登记的上层依赖（%d 处）：%s"
              % (len(exp_hits), ", ".join(exp_hits) or "无"))
    prod_names = [os.path.relpath(p, BACKTEST_DIR).replace("\\", "/") for p in files]
    print("     生产代码扫描面（%d）：%s" % (len(files), ", ".join(prod_names)))

    # ── 逐文件扫描 ───────────────────────────────────────────────
    banned_hits = {top: [] for top, _, _ in FORBIDDEN}
    local_uses = set()
    trading_hits = []
    for path in files:
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for lineno, mod in scan_source(src):
            top = mod.split(".")[0]
            if top in banned_hits:
                banned_hits[top].append("%s:%d  %s" % (rel, lineno, mod))
            if is_repo_local(top):
                local_uses.add(top)
                if top == "Trading" and not mod.startswith(TRADING_ALLOW_PREFIX):
                    trading_hits.append("%s:%d  %s" % (rel, lineno, mod))

    # ── ①②③ 逐类禁止项 ─────────────────────────────────────────
    for top, no, why in FORBIDDEN:
        hits = banned_hits[top]
        check("%s 生产代码零 %s 依赖（%s）" % (no, top, why), not hits,
              "命中：\n" + "\n".join(hits))

    # ── ④ 全部本地依赖 ∈ 层表白名单 ──────────────────────────────
    outside = sorted(local_uses - LAYER_ALLOW)
    check("④a repo 本地顶层依赖全部 ∈ 层表白名单", not outside,
          "越界：%r（白名单 %r）" % (outside, sorted(LAYER_ALLOW)))

    # ── ⑤ Trading 只许 Strategy / Infra ──────────────────────────
    check("⑤ Trading 只许 Strategy / Infra（不许 Engine / App*）", not trading_hits,
          "命中：\n" + "\n".join(trading_hits))

    # ── ⑧ 披露实际依赖集（非空）─────────────────────────────────
    check("⑧ 实际用到的本地依赖集非空（证明④a 非恒真）", bool(local_uses),
          "local_uses=%r" % sorted(local_uses))
    print("     实际本地依赖集：%s" % ", ".join(sorted(local_uses)))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
