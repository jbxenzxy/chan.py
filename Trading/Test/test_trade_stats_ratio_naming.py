# -*- coding: utf-8 -*-
"""成交统计「盈亏比 / 盈利因子」命名与口径契约。

用户拍板的口径（2026-09-17）
------------------------------------------------------------
    平均每笔盈利 ÷ 平均每笔亏损  →  中文「盈亏比」    字段 `pl_ratio`
    总盈 ÷ 总亏                  →  中文「盈利因子」  字段 `profit_factor`

这两个口径**必须可区分**：它们名字相近、数值量级可能差一倍以上，
一旦有人的实现或文案把两者混起来（历史真事：`Analyze.py` 里叫
`profit_factor` 的字段实际算的是「盈亏比」口径），面板就会出现
"两个都叫盈亏比、数却差一倍"的读不懂现象。

本文件把它钉死。三层：
  [A] 行为：同一组样本下两个字段分别命中均值口径 / 总量口径，且互不相等
  [B] 契约：返回键集合、空样本键集合、源码（剥 docstring）不含旧字段名
  [C] 全仓：生产代码 / 前端不得再出现旧字段名与旧标签（防回潮）

为什么要有 [C]：清理只写进文档 = 必然回潮。后来人（或 AI）新写代码时
没有任何信号提醒他这个名字已废。可执行断言才是那个信号。

跑法：python Trading/Test/test_trade_stats_ratio_naming.py
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
_REPO = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO)

from Trading.Infra.TradeStats import (  # noqa: E402
    _empty_stats, compute_trade_stats)

_PASS = 0
_FAIL = 0

# 旧名 —— 只在本文件里作为「检测器样本」出现，生产代码里必须为零。
_LEGACY_FIELD = "avg_pl_ratio"
_LEGACY_LABEL = "盈亏比 PF"

# 新名（带词边界：pl_ratio 是 avg_pl_ratio 的子串，不加边界会误判为"新名还在"）
_RE_NEW_PL = re.compile(r"(?<![A-Za-z0-9_])pl_ratio(?![A-Za-z0-9_])")
_RE_LEGACY = re.compile(r"(?<![A-Za-z0-9_])" + _LEGACY_FIELD + r"(?![A-Za-z0-9_])")


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


def strip_docstrings(src: str) -> str:
    """源码剥掉 docstring 后再比对。

    ⚠️ 不剥会误报：docstring 里解释"为什么不再叫旧名"时**必然提到旧名**。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    drop = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            drop.append(first)
    if not drop:
        return src
    lines = src.splitlines(keepends=True)
    drop_lines = set()
    for node in drop:
        for ln in range(node.lineno, node.end_lineno + 1):
            drop_lines.add(ln)
    return "".join(l for i, l in enumerate(lines, 1) if i not in drop_lines)


def mk(net_cash, i):
    return {
        "trade_id": "T%d" % i, "symbol": "CFFEX.IF2609", "side": "long",
        "volume": 1, "entry_price": 4500.0, "exit_price": 4500.0,
        "entry_at": "2026-09-01 09:00", "exit_at": "2026-09-0%d 10:00" % (i % 9 + 1),
        "reason": "tp" if net_cash > 0 else "sl",
        "gross_points": float(net_cash), "cost_cash": 0.0,
        "net_cash": float(net_cash), "bars_held": 3,
        "exit_plan_name": "run_managed", "exit_plan_params": "{}",
    }


def iter_sources():
    """生产代码 + 前端（排除测试目录、本文件自身、历史文档）。"""
    skip_dirs = {"__pycache__", ".git", ".venv", "Docs", "State", "replay_data"}
    self_abs = os.path.abspath(__file__)
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rel_root = os.path.relpath(root, _REPO).replace("\\", "/")
        # 测试目录：允许保留旧名作为"历史对照样本"，不参与防回潮扫描
        if rel_root == "Test" or rel_root.startswith("Test/") \
                or rel_root == "Trading/Test" or rel_root.startswith("Trading/Test/"):
            continue
        for f in files:
            if not f.endswith((".py", ".js")):
                continue
            p = os.path.join(root, f)
            if os.path.abspath(p) == self_abs:
                continue
            yield p, os.path.relpath(p, _REPO).replace("\\", "/")


# ══════════════════════════════════════════════════════════════
print("\n[0] 元护栏：先证明检测器有效，否则「零命中」可能是假绿")
# ══════════════════════════════════════════════════════════════
check_true("[0a] 旧字段名检测器能命中样本", bool(_RE_LEGACY.search(
    'stats["%s"]' % _LEGACY_FIELD)))
check_true("[0b] 新名检测器带词边界 → 不会把旧名误判成新名",
           not _RE_NEW_PL.search(_LEGACY_FIELD))
check_true("[0c] 新名检测器能命中真正的新名", bool(_RE_NEW_PL.search('s["pl_ratio"]')))
check_true("[0d] 旧字段名检测器不误伤 pl_ratio", not _RE_LEGACY.search("pl_ratio"))


# ══════════════════════════════════════════════════════════════
print("\n[1] 口径行为：同一组样本，两个字段必须落进各自的算法")
# ══════════════════════════════════════════════════════════════
# 样本刻意拉大「单笔典型值」与「总量」的差距，让两种口径不可能撞在一起：
#   盈利 5 笔合计 3400（均值 680）；亏损 2 笔合计 -1000（均值 -500）
#     盈亏比（均值口径）  = 680 / 500 = 1.36
#     盈利因子（总量口径）= 3400 / 1000 = 3.4
_NETS = [100.0, 100.0, 100.0, 100.0, 3000.0, -500.0, -500.0]
_s = compute_trade_stats([mk(n, i) for i, n in enumerate(_NETS)])

check("[1a] pl_ratio = 平均每笔盈利 ÷ 平均每笔亏损",
      _s.get("pl_ratio"), 1.36)
check("[1b] profit_factor = 总盈 ÷ 总亏",
      _s.get("profit_factor"), 3.4)
check_true("[1c] ★ 两个口径必须可区分（实现串了就红）",
           _s.get("pl_ratio") != _s.get("profit_factor"),
           "%s vs %s" % (_s.get("pl_ratio"), _s.get("profit_factor")))

# 反向样本：把"少数大亏 / 少数大赚"对调，两口径继续各走各的
_NETS2 = [800.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0, -100.0]
_s2 = compute_trade_stats([mk(n, i) for i, n in enumerate(_NETS2)])
check("[1d] 反向样本 pl_ratio（均值口径）", _s2.get("pl_ratio"), 8.0)
check("[1e] 反向样本 profit_factor（总量口径）", _s2.get("profit_factor"), 0.8889)
check_true("[1f] ★ 反向样本下两口径符号倾向相反（一个 >1 一个 <1）",
           (_s2.get("pl_ratio") > 1) != (_s2.get("profit_factor") > 1),
           "pl_ratio=%s profit_factor=%s" % (_s2.get("pl_ratio"), _s2.get("profit_factor")))


# ══════════════════════════════════════════════════════════════
print("\n[2] 返回键集合：新名在、旧名不在（含空样本分支）")
# ══════════════════════════════════════════════════════════════
check_true("[2a] 正常样本含 pl_ratio", _RE_NEW_PL.search(
    " ".join(_s.keys())), sorted(_s.keys())[:6])
check_true("[2b] 正常样本含 profit_factor", "profit_factor" in _s)
check_true("[2c] 正常样本**不含**旧字段名", _LEGACY_FIELD not in _s)
_e = _empty_stats()
check_true("[2d] 空样本同样含 pl_ratio（否则前端读 undefined）",
           "pl_ratio" in _e)
check("[2e] 空样本 pl_ratio = None（无亏损 = 无定义，不是 0）", _e.get("pl_ratio"), None)
check("[2f] 空样本 profit_factor = None", _e.get("profit_factor"), None)
check_true("[2g] 空样本不含旧字段名", _LEGACY_FIELD not in _e)
check("[2h] 两个键的集合在 正常/空 两条路径上一致",
      sorted(k for k in _s if k in ("pl_ratio", "profit_factor")),
      sorted(k for k in _e if k in ("pl_ratio", "profit_factor")))


# ══════════════════════════════════════════════════════════════
print("\n[3] 源码契约：实现里不许再出现旧字段名（剥 docstring 后判）")
# ══════════════════════════════════════════════════════════════
_src = inspect.getsource(compute_trade_stats)
check_true("[3a] compute_trade_stats 源码（剥 docstring）不含旧字段名",
           not _RE_LEGACY.search(strip_docstrings(_src)))
check_true("[3b] compute_trade_stats 源码含新字段名",
           bool(_RE_NEW_PL.search(strip_docstrings(_src))))

# 中文口径：docstring 里的中文名必须各就各位（名字对了、中文名错了同样误读）
_doc = (compute_trade_stats.__doc__ or "")
_pl_line = [l for l in _doc.splitlines() if _RE_NEW_PL.search(l)]
_pf_line = [l for l in _doc.splitlines() if "profit_factor" in l]
check_true("[3c] docstring 中 pl_ratio 一行写着「盈亏比」",
           bool(_pl_line) and "盈亏比" in _pl_line[0], _pl_line[:1])
check_true("[3d] docstring 中 profit_factor 一行写着「盈利因子」",
           bool(_pf_line) and "盈利因子" in _pf_line[0], _pf_line[:1])

# Analyze.py：它的字段实际算的是「盈亏比」口径 → 必须叫 pl_ratio
_an = os.path.join(_REPO, "Trading", "Tool", "Recorder", "Analyze.py")
_an_src = open(_an, encoding="utf-8").read()
_an_code = strip_docstrings(_an_src)
check_true("[3e] Analyze.py 的盈亏比字段已叫 pl_ratio",
           bool(_RE_NEW_PL.search(_an_code)))
check_true("[3f] ★ Analyze.py 不再有 profit_factor（它与总量口径同名不同义）",
           "profit_factor" not in _an_code)


# ══════════════════════════════════════════════════════════════
print("\n[4] 全仓防回潮：生产代码 / 前端不得出现旧字段名")
# ══════════════════════════════════════════════════════════════
_hits = []
_scanned = []
for p, rel in iter_sources():
    _scanned.append(rel)
    try:
        t = open(p, encoding="utf-8").read()
    except Exception:
        continue
    for m in _RE_LEGACY.finditer(t):
        _hits.append("%s:%d" % (rel, t[:m.start()].count("\n") + 1))

check("[4a] 旧字段名零命中", _hits, [])
# 覆盖面自检：防有人把扫描范围改窄 → 护栏静默失效
for _must in ("Trading/Infra/TradeStats.py", "Frontend/app.js",
              "Trading/Tool/Recorder/Analyze.py", "FrontAPI.py"):
    check_true("[4b] 扫描覆盖到 " + _must, _must in _scanned)
check_true("[4c] 扫描规模合理（>50 个源文件）",
           len(_scanned) > 50, len(_scanned))


# ══════════════════════════════════════════════════════════════
print("\n[5] 前端契约：字段名 + 中文标签")
# ══════════════════════════════════════════════════════════════
_js = open(os.path.join(_REPO, "Frontend", "app.js"), encoding="utf-8").read()
check_true("[5a] 前端读的是 d.pl_ratio", "d.pl_ratio" in _js)
check_true("[5b] 前端仍读 profit_factor", "d.profit_factor" in _js)
check_true("[5c] 面板有「盈亏比」标签（核心三格）",
           '<span class="stats-label">盈亏比</span>' in _js)
check_true("[5d] 面板有「盈利因子」标签（明细行）",
           '<span class="stats-label">盈利因子</span>' in _js)
check_true("[5e] ★ 旧标签「%s」已不存在" % _LEGACY_LABEL, _LEGACY_LABEL not in _js)
check_true("[5f] 前端不含旧字段名", _LEGACY_FIELD not in _js)


print("\n" + "=" * 62)
print("成交统计命名契约 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
if _FAIL:
    raise SystemExit(1)
