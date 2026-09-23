# -*- coding: utf-8 -*-
"""P63 盈亏比「文档不复述取值」护栏
==================================
背景（2026-09-23 用户拍板）：
    改某个品种的盈亏比时，不该被迫同步改一堆注释 / README 文案 / 测试描述 ——
    那些复述出来的倍数（形如「比例 + 倍数」的写法）是同一事实的**第二份副本**，
    改一处漏一处，文档就会与代码互相矛盾。**具体倍数只许写在档案条目上。**

判据（同一行内「语境词」与「档位形态」同现才算违规）：
  · 载体：**注释**（`#`）与**字符串常量**（docstring / check 描述 / README 文案，经 tokenize；
    docstring 是单个跨行 token，故按行拆开逐行判）。
  · 语境词 = `win_loss_ratio` / `盈亏比` / `L3`。
  · 档位形态 = 比例式（两侧都不得与数字相邻，避开 `09:41:33` 这类时间戳）｜
    `统一|全为|均为|同在` + 倍数｜`倍数 + 启动|达标`｜`win_loss_ratio=小数`。
  · **不算违规**（刻意保留、可执行或与档案无关）：
      ① 各品种条目的 `win_loss_ratio=` 实参 —— 唯一真值源（可执行代码，不属注释/字符串）；
      ② 钉住口径的测试**期望值**（如 `test_period_profile` 的集合断言）—— 改档位时本就该改；
      ③ 与档案无关的倍数：L1 的「止盈距 : 止损距」比例、用例自注入的探针档位、
         `win_loss_ratio=99` 哨兵、`pl_ratio` 那个同名的「盈亏比(赔率)」。
  · **已知边界**：孤立写「1:2」而不提语境词的注释本护栏不覆盖（提不出可达路径的形态
    一律不猜）；判据是**形态**而非绑死数值 ⇒ 档位改成 2.5 / 4.0 不必改本文件。
  · 本文件 [3] 组的「诱饵」**必须**是违规样本，故用 `p63-scan-off … p63-scan-on`
    标记豁免该区间；[1] 组断言该标记在全仓只出现在本文件，防止被用来绕过护栏。

范围：`Trading/` `App/` `Frontend/` `Test/` 的 .py .js ＋ `Trading/README.md`。
    **不扫 `Docs/**`**（审计快照性质，历史失真不清算 —— 与术语护栏同口径）。

跑法：python Trading/Test/test_p63_wlr_doc_guard.py
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import tokenize

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_root() -> str:
    """仓库根 = Trading 包的父目录。"""
    d = _HERE
    for _ in range(6):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return os.path.dirname(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_root()
if not _ROOT:
    print("✗ 找不到仓库根（Trading 包），请把本文件放在 Trading/Test/ 下。")
    raise SystemExit(2)

SCAN_DIRS = ("Trading", "App", "Frontend", "Test")
SCAN_EXTS = (".py", ".js")
EXTRA_FILES = ("Trading/README.md",)
SKIP_DIRS = {"__pycache__", ".venv", ".git", "State", "replay_data", "Docs", "node_modules"}

# ① 语境词：出现它才可能是在复述「品种盈亏比」这件事
#    `L3` 加词边界 —— 否则 `文档 L332` / `L4` 之类的行会被卷进来（实测误伤 SimNow.py）。
_CTX = re.compile(r"(win_loss_ratio|盈亏比|(?<![A-Za-z0-9])L3(?![0-9]))")

# ② 档位形态（任一条命中即算复述）
_FORMS = (
    (re.compile(r"(?<![0-9])1\s*[:：]\s*[23](?![0-9])"), "比例式（1:2 / 1:3）"),
    (re.compile(r"(?<![0-9])[23]\s*[:：]\s*1(?![0-9])"), "比例式（2:1 / 3:1）"),
    (re.compile(r"(?:统一|全为|均为|同在)\s*[0-9](?:\.0)?"), "「统一/全为」+ 倍数"),
    (re.compile(r"[0-9](?:\.0)?\s*R\s*(?:启动|达标)"), "倍数 + 启动/达标"),
    (re.compile(r"win_loss_ratio\s*[=＝]\s*[0-9]\.[0-9]"), "win_loss_ratio + 具体取值"),
    (re.compile(r"集合\s*==?\s*\{?[0-9]"), "描述里复述期望值集合"),
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("" if ok else "  -> {}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _violations_of_text(text):
    """一段（单行）注释 / 字符串是否违规 —— 返回命中的形态标签列表。"""
    if not _CTX.search(text):
        return []
    return [label for pat, label in _FORMS if pat.search(text)]


def _iter_files():
    for d in SCAN_DIRS:
        base = os.path.join(_ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith(SCAN_EXTS):
                    fp = os.path.join(dirpath, fn)
                    yield os.path.relpath(fp, _ROOT).replace("\\", "/"), fp
    for rel in EXTRA_FILES:
        fp = os.path.join(_ROOT, rel)
        if os.path.isfile(fp):
            yield rel, fp


_STR_TYPES = {tokenize.STRING}
for _n in ("FSTRING_MIDDLE",):
    _t = getattr(tokenize, _n, None)
    if _t is not None:
        _STR_TYPES.add(_t)

# 自证诱饵区豁免标记（只允许出现在本文件；[1] 组有断言守住这一点）
_OFF, _ON = "p63-scan-off", "p63-scan-on"


def _off_ranges(src):
    """返回 [(start, end)] —— 被豁免标记包住的行区间（含标记行本身）。"""
    ranges, start = [], None
    for i, line in enumerate(src.splitlines(), 1):
        if _OFF in line:
            start = i
        elif _ON in line and start is not None:
            ranges.append((start, i))
            start = None
    return ranges


def _texts_of_py(path):
    """产出 (行号, 载体类型, 该行文本) —— 只取注释与字符串常量（含 docstring，按行拆）。

    落在 `p63-scan-off … p63-scan-on` 区间内的产物被跳过（仅用于本文件的自证诱饵）。
    """
    src = open(path, "r", encoding="utf-8", errors="replace").read()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    off = _off_ranges(src)

    def _skipped(ln):
        return any(a <= ln <= b for a, b in off)

    for tk in toks:
        if tk.type == tokenize.COMMENT:
            if not _skipped(tk.start[0]):
                yield tk.start[0], "注释", tk.string
        elif tk.type in _STR_TYPES:
            for i, part in enumerate(tk.string.splitlines()):
                ln = tk.start[0] + i
                if not _skipped(ln):
                    yield ln, "字符串", part


def _texts_of_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            yield i, "文档", line


def scan():
    """返回违规清单 [(rel, lineno, kind, labels, snippet)]。"""
    hits = []
    for rel, fp in _iter_files():
        gen = _texts_of_py(fp) if rel.endswith(".py") else _texts_of_text(fp)
        for lineno, kind, text in gen:
            labels = _violations_of_text(text)
            if labels:
                hits.append((rel, lineno, kind, labels, text.strip()[:110]))
    return hits


def main():
    print("[1] 扫描范围与文件数")
    files = list(_iter_files())
    check("扫到文件数 > 100（范围没写错）", len(files) > 100, True)
    check("范围含 Trading/Infra/Product.py",
          any(r == "Trading/Infra/Product.py" for r, _ in files), True)
    check("范围含 Trading/README.md",
          any(r == "Trading/README.md" for r, _ in files), True)
    check("范围不含 Docs/（审计快照不清算）",
          any(r.startswith("Docs/") for r, _ in files), False)

    print("\n[1b] 豁免标记只许出现在本文件（否则等于给护栏开后门）")
    _SELF = "Trading/Test/test_p63_wlr_doc_guard.py"
    marked = [rel for rel, fp in files
              if _OFF in open(fp, "r", encoding="utf-8", errors="replace").read()]
    check("含豁免标记的文件", marked, [_SELF])
    check("本文件的豁免区间恰好 1 个",
          len(_off_ranges(open(os.path.join(_ROOT, _SELF), encoding="utf-8").read())), 1)

    print("\n[2] 违规扫描：注释 / 文档字符串里不得复述盈亏比档位")
    hits = scan()
    for rel, lineno, kind, labels, snip in hits:
        print("   ✗ {}:{} [{}|{}] {}".format(rel, lineno, kind, "/".join(labels), snip))
    check("注释 / 文档字符串 / README 里零复述", hits, [])

    print("\n[3] 反向自证 A：真违规必须拦住（8 条伪造样本逐条命中）")
    # p63-scan-off（以下诱饵故意含违规写法，本区间豁免）
    decoys = [
        "# 8 个品种统一 1" + ":2（L3 在 2R 启动）",
        'note="盈亏比 1' + ':3、乘数 200 元/点"',
        'check("IF resolved win_loss_ratio' + '=2.0", x, 2.0)',
        "# 全品种统一 2" + ".0，L3 在 2R 达标",
        "# 盈亏比默认 1" + ":2 的止盈宽度",
        "# 每品种盈亏比 3" + ":1，见档案",
        "# L3 的触发阈值 = win_loss_ratio" + "=2.5",
        'check("8 品种 win_loss_ratio 集合 == {2' + '.0}", got, [2.0])',
    ]
    # p63-scan-on
    for i, text in enumerate(decoys, 1):
        labels = _violations_of_text(text)
        check("伪造样本 {}：命中（{}）".format(i, "/".join(labels) or "无"), bool(labels), True)

    print("\n[4] 反向自证 B：正当写法不得误伤（9 条）")
    legit = [
        'check("TA win_loss_ratio 跟随档案", p.win_loss_ratio, 2.0)',
        'check("多单 名义止盈距=2×止损距（_tp_nominal 落盘、1:2 比例不变）", a, True)',
        'check("阈值2R high 2.01R 启动", _l3_started(pol_r2, 120.1, 2715), True)',
        'check("8 品种 win_loss_ratio 集合（统一口径，无品种分档）", got, [3.0])',
        "# R=10、入场 4000 → 保本阈值 4010（+1R）。win_loss_ratio=99 关掉跟踪。",
        '"[E3] 全亏：盈亏比 = 0.0（0÷亏损 = 0，有定义）"',
        "# 港股 4 位补零（通达信文件统一 5 位）",
        "# 跟踪缓冲 = 0.5 × R；盈亏比由档案给（见 Infra/Product.py 各品种条目）",
        "背景（2026-09-18 当日 IF 案件）：09:41:33 引擎 FOK 报空 2 手",
    ]
    for i, text in enumerate(legit, 1):
        labels = _violations_of_text(text)
        check("正当样本 {}：零命中（{}）".format(i, "/".join(labels) or "无"), labels, [])

    print("\n[5] 反向自证 C：可执行取值不受扫描（档案实参是代码、不是注释/字符串）")
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "Sample.py")
        with open(fp, "w", encoding="utf-8") as f:
            f.write('PROFILES = {"IM": Product(product="IM", win_loss_ratio=' + '2.0,\n'
                    '                              note="盈亏比见 win_loss_ratio（L3 启动阈值）；")}\n')
        found = [(ln, k, _violations_of_text(t)) for ln, k, t in _texts_of_py(fp)]
        bad = [x for x in found if x[2]]
        check("含 win_loss_ratio 实参 + 合规 note 的样本文件：零命中", bad, [])
        check("样本文件的字符串确实被读到（不是空扫描）", len(found) >= 1, True)

    print("\n" + "=" * 60)
    print("通过 {} / 失败 {}".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
