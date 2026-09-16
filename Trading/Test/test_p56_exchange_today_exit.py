# -*- coding: utf-8 -*-
"""
P56 交易所字段删除 + today_exit 改名（2026-09-16 · B 批 ⑴+⑶-c）
=====================================================================
原始主张（用户 2026-09-16，逐字保留）
--------------------------------------------------------------
⑴ 「删掉 `Product.exchange` 字段（与 EXEC_POLICY 行尾注释、note 文案是同一事实的
   三份副本；把「零判断」从纪律降级为结构）」
   —— 另经用户拍板：删掉之后**启动横幅干脆不打交易所**。

⑶-c「`close_mode` 改名回 `today_exit`（一半的行干的是 open，文档 §5.1 原本就叫
     today exit，§15 反而改差了）」

这两件事为什么不是"洁癖"
--------------------------------------------------------------
· `exchange`：字段本身不参与判断，但它**能被读**。"交易所不参与任何判断"原先靠
  纪律（谁都不许读）；删掉字段后只剩注释形态，注释**无法参与分支** —— 纪律变成结构。
· `close_mode`：8 行里 **5 行**取值 `R-OPEN`，那一支发的是**反向 OPEN**，属于开仓；
  字段名却宣称自己在描述"平仓方式"。名字错 ≠ 行为错，但错名会让人读错状态机：
  "今仓走 close_mode" 会让人以为永远平仓，从而认为锁仓态不可达 —— 对 IF/IH/IC/IM/TA
  恰好相反。

覆盖
--------------------------------------------------------------------------
  [0] 元护栏：先证明**检测器本身有效**（能抓真违规、不误伤 `exchange_symbol`
      之类的邻居词），否则下面那些"零命中"可能只是检测器瞎了
  [1] `Product` 无 `exchange` 字段；`Instrument` 无 `exchange` 属性；
      `__repr__` 不依赖它
  [2] ★ 生产代码（Trading/ + App/ + Frontend/，Test/ 同样计入）AST 零命中：
      无 `.exchange` 属性读、无 `exchange=` 关键字实参、无结构化 `"exchange"`
      字符串常量。**唯一豁免 = `_REMOVED_KEYS` 的旧键指路**（删了字段反而要留着
      它，才能给写旧键的人报错指路）
  [3] `ExecPolicy` 字段名 = `today_exit`（`close_mode` 不再是字段）；值域不变
  [4] ★ 生产代码零 `close_mode` 标识符（**剥掉注释/docstring 后**再判 ——
      改名史必然写在注释里，那是留档不是残留）
  [5] 行为锚点：改名不动判据。`Instrument → exec_policy → today_exit` 取值链路
      与 8 行表逐项一致；引擎两处判定点仍在（转移④ / 两态守卫）
      （转移④ 的**分支行为**由 test_p51 [3] 覆盖，本文件只钉"名字→取值"这一跳）
  [6] 覆盖面自检：关键文件必须在扫描集合里（防有人把范围改窄 → 护栏静默失效）
  [7] 变异注入（在内存里造样本喂检测器）→ 必须报红

跑法：python Trading/Test/test_p56_exchange_today_exit.py
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = (got == want)
    print(("  \u2713 " if ok else "  \u2717 ") + name +
          ("" if ok else "  -> got={!r}, want={!r}".format(got, want)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, cond):
    check(name, bool(cond), True)


def check_contains(name, hay, needle):
    check(name + "  [含 " + needle[:40] + "]", needle in hay, True)


# ══════════════════════════════════════════════════════════════════
# 工具：剥注释 / docstring（**位置等长掩码**，行号列号不变）
# ══════════════════════════════════════════════════════════════════
def mask_prose(src: str) -> str:
    """把 COMMENT 与 STRING 的字符原地换成空格。

    位置精确对齐是刻意的：这样 `文件:行号` 报告才可信（早前一版按 token
    重建文本、重复计了换行，行号虚高近一倍 —— 已修）。
    """
    import tokenize as _tk
    buf = list(src)
    lines = src.split("\n")
    offs, acc = [], 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln) + 1

    def put(row, col):
        if 1 <= row <= len(lines):
            i = offs[row - 1] + col
            if 0 <= i < len(buf):
                buf[i] = " "

    try:
        toks = list(_tk.generate_tokens(io.StringIO(src).readline))
    except Exception:                                          # noqa: BLE001
        return src
    for t in toks:
        if t.type not in (_tk.COMMENT, _tk.STRING):
            continue
        (sr, sc), (er, ec) = t.start, t.end
        if sr == er:
            for c in range(sc, ec):
                put(sr, c)
        else:
            for c in range(sc, len(lines[sr - 1])):
                put(sr, c)
            for r in range(sr + 1, er):
                for c in range(len(lines[r - 1])):
                    put(r, c)
            for c in range(0, ec):
                put(er, c)
    return "".join(buf)


def ast_hits(src: str, rel: str, names):
    """AST 口径命中：属性读取 / 关键字实参 / (非 docstring) 字符串常量。"""
    out = []
    tree = ast.parse(src)
    ds = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ds.add(id(body[0].value))
    pat = re.compile(r"\.(?:%s)\b" % "|".join(names))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            out.append("%s:%d 属性 .%s" % (rel, node.lineno, node.attr))
        if isinstance(node, ast.keyword) and node.arg in names:
            out.append("%s:%d 关键字实参 %s=" % (rel, node.lineno, node.arg))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in ds and pat.search(node.value):
            out.append("%s:%d 字符串常量 %r" % (rel, node.lineno,
                                              node.value[:40]))
    return out


_SELF = os.path.abspath(__file__)


def iter_py():
    """遍历待扫的 .py（**排除本护栏文件自身**）。

    护栏文件必然含禁用词字面量 —— 它就是定义处（元护栏的植入样本、
    各条检查的中文描述里都写着 `.exchange` / `close_mode`）。不排除自己
    就会"自己扫自己"，恒红。这与全仓术语护栏 `test_p26` 的处理一致。
    """
    for root in ("Trading", "App", "Frontend"):
        base = os.path.join(_REPO, root)
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d != "__pycache__"]
            for f in sorted(fn):
                if f.endswith(".py") and os.path.abspath(
                        os.path.join(dp, f)) != _SELF:
                    yield os.path.join(dp, f)


# ══════════════════════════════════════════════════════════════════
# [0] 元护栏
# ══════════════════════════════════════════════════════════════════
print("\n[0] 元护栏：先证明检测器本身有效（否则「零命中」可能是假绿）")

# 0a：掩码必须真把注释/docstring 抹掉
_m = mask_prose('x = 1  # .exchange\n')
check("[0a] 掩码抹掉行尾注释里的 .exchange", ".exchange" in _m, False)
check_true("[0a] 掩码保住代码本体（x = 1 还在）", "x = 1" in _m)
_m2 = mask_prose('"""doc .close_mode"""\ny = 2\n')
check("[0b] 掩码抹掉 docstring 里的 .close_mode", ".close_mode" in _m2, False)

# 0c：AST 检测器抓得到真违规
_planted = 'Product(exchange="SHFE")\nz = p.exchange\nq = pol.close_mode\n'
_h = ast_hits(_planted, "<样本>", ("exchange", "close_mode"))
check("[0c] 检测器抓到植入的 3 处违规（exchange= / .exchange / .close_mode）",
      len(_h), 3)

# 0d：不误伤邻居词
_ok_src = ('a = getattr(v, "exchange_symbol", None)\n'
           'b = "legal"\nc = "privilege"\nd = "exchange_symbol"\n')
check("[0d] 不误伤 exchange_symbol / legal / privilege",
      ast_hits(_ok_src, "<样本>", ("exchange", "close_mode")), [])

# 0e：close_mode 的 `\b` 边界（close_mode_x 不算命中）
check("[0e] `close_mode_x` 不算命中（词边界正确）",
      ast_hits("k = d.close_mode_x\n", "<样本>", ("exchange", "close_mode")), [])


# ══════════════════════════════════════════════════════════════════
# [1] 字段 / 属性层
# ══════════════════════════════════════════════════════════════════
print("\n[1] `Product.exchange` 字段与 `Instrument.exchange` 属性均已删除")

from Trading.Infra.Instrument import Instrument            # noqa: E402
from Trading.Infra.Product import (CLOSETODAY, ExecPolicy,  # noqa: E402
                                   FOK, PRODUCT_PROFILES, R_OPEN)

check("[1a] ★ Product 全部 8 档均无 exchange 字段",
      [k for k in PRODUCT_PROFILES if hasattr(PRODUCT_PROFILES[k], "exchange")],
      [])
check("[1b] ★ Product dataclass 字段表里没有 exchange",
      "exchange" in getattr(PRODUCT_PROFILES["IF"], "__dataclass_fields__", {}),
      False)
check("[1c] ★ Instrument 实例没有 exchange 属性",
      hasattr(Instrument(None, PRODUCT_PROFILES["IF"]), "exchange"), False)
check("[1d] 未标定 Instrument 也没有 exchange 属性",
      hasattr(Instrument(None, None), "exchange"), False)
check("[1e] __repr__ 不打交易所",
      "exchange" in repr(Instrument(None, PRODUCT_PROFILES["IF"])), False)
check_true("[1f] __repr__ 仍打品种相关值（tick / multiplier 还在）",
           "price_tick" in repr(Instrument(None, PRODUCT_PROFILES["IF"])))


# ══════════════════════════════════════════════════════════════════
# [2] 全仓 AST 零命中（唯一豁免 = _REMOVED_KEYS 旧键指路）
# ══════════════════════════════════════════════════════════════════
print("\n[2] ★ 全仓 AST：`.exchange` / `exchange=` / `\"…exchange…\"` 零命中")

_all_hits = []
_scanned = []
for p in iter_py():
    rel = os.path.relpath(p, _REPO).replace("\\", "/")
    _scanned.append(rel)
    src = io.open(p, encoding="utf-8").read()
    try:
        _all_hits += ast_hits(src, rel, ("exchange",))
    except SyntaxError as e:
        _all_hits.append("%s 语法错误：%s" % (rel, e))

check("[2a] ★ 全仓 .exchange / exchange= / 结构化字符串 零命中", _all_hits, [])

# 反向：旧键指路必须**留着** —— 删字段不等于假装它从未存在过。
# 老配置里写了 `exchange=` 的人，需要一句明确的"它已整体删除"报错，
# 而不是一个含义不明的 extra-forbid 报错。
from Trading.Infra.Instrument import _REMOVED_KEYS            # noqa: E402
check("[2b] _REMOVED_KEYS 仍保留 exchange 旧键指路（删字段 ≠ 抹掉历史）",
      "exchange" in _REMOVED_KEYS, True)
check_true("[2b2] 指路文案说的是「已整体删除」，不是「已归位」",
           "已整体删除" in _REMOVED_KEYS.get("exchange", ""))


# ══════════════════════════════════════════════════════════════════
# [3] ExecPolicy 字段名
# ══════════════════════════════════════════════════════════════════
print("\n[3] `ExecPolicy` 第 1 字段 = `today_exit`（不再是 `close_mode`）")

_fields = list(getattr(ExecPolicy, "__dataclass_fields__", {}))
check("[3a] ExecPolicy 字段序 = (today_exit, order_advanced, lots_per_order)",
      _fields, ["today_exit", "order_advanced", "lots_per_order"])
check("[3b] close_mode 不再是字段", "close_mode" in _fields, False)
check("[3c] 值域不变：{R_OPEN, CLOSETODAY}",
      sorted({p.exec_policy.today_exit for p in PRODUCT_PROFILES.values()}),
      sorted({R_OPEN, CLOSETODAY}))
check("[3d] 8 行齐 + 与档案同源",
      sorted(k for k, v in PRODUCT_PROFILES.items()
             if v.exec_policy.today_exit in (R_OPEN, CLOSETODAY)),
      sorted(PRODUCT_PROFILES))
def _rejects(v):
    """构造非法值 → 必须被 ExecPolicy.__post_init__ 拒绝。"""
    try:
        ExecPolicy(v, FOK, 2)
        return False
    except ValueError:
        return True


check("[3e] 构造期仍拒绝非法值、仍接受合法值（改名不动校验）",
      [_rejects("CLOSEX"), _rejects(R_OPEN), _rejects(CLOSETODAY)],
      [True, False, False])


# ══════════════════════════════════════════════════════════════════
# [4] 全仓零 `close_mode`（剥注释 / docstring 后）
# ══════════════════════════════════════════════════════════════════
print("\n[4] ★ 剥注释/docstring 后：生产代码零 `close_mode` 标识符")

_cm_hits, _cm_prose = [], []
for p in iter_py():
    rel = os.path.relpath(p, _REPO).replace("\\", "/")
    src = io.open(p, encoding="utf-8").read()
    code = mask_prose(src)
    for i, line in enumerate(code.split("\n"), 1):
        if re.search(r"(?<![\w.])close_mode\b", line):
            _cm_hits.append("%s:%d  %s" % (rel, i, line.strip()[:80]))
    if "close_mode" in src:
        _cm_prose.append(rel)

check("[4a] ★ 实代码零 `close_mode`（注释里的改名史不算残留）", _cm_hits, [])
check_true("[4b] 改名史留档在注释/docstring 里（说明这次改名有据可查）",
           len(_cm_prose) >= 1)
check_true("[4c] `today_exit` 在实代码里确实被用起来（不是只改了字段名）",
           len([1 for p in iter_py()
                if "today_exit" in mask_prose(
                    io.open(p, encoding="utf-8").read())]) >= 3)


# ══════════════════════════════════════════════════════════════════
# [5] 行为锚点：名字 → 取值的链路
# ══════════════════════════════════════════════════════════════════
print("\n[5] 行为锚点：`Instrument → exec_policy → today_exit` 取值链路")

check("[5a] AU 档案（CLOSETODAY 档）→ 走平今",
      Instrument(None, PRODUCT_PROFILES["AU"]).exec_policy.today_exit,
      CLOSETODAY)
check("[5b] IF 档案（R-OPEN 档）→ 反向锁仓",
      Instrument(None, PRODUCT_PROFILES["IF"]).exec_policy.today_exit, R_OPEN)

_eng = io.open(os.path.join(_REPO, "Trading/Engine/Engine.py"),
               encoding="utf-8").read()
# 取值点共 4 处（改名必须一处不漏，否则会出现"半新半旧"的读法）：
#   ① _decide_exit 转移④      → `== CLOSETODAY`
#   ② _check_two_state_invariant → `!= CLOSETODAY`
#   ③ 同上，事件 payload        → `today_exit=self._today_exit()`
#   ④ _pre_trade_check 兜底校验 → `!= CLOSETODAY`
check("[5c] 引擎取值点 4 处（转移④ / 两态守卫 / 事件 payload / 兜底校验）",
      _eng.count("self._today_exit()"), 4)
check("[5c2] 判定点分解：== CLOSETODAY 一处、!= CLOSETODAY 两处",
      (_eng.count("if self._today_exit() == CLOSETODAY:"),
       _eng.count("if self._today_exit() != CLOSETODAY:")), (1, 2))
check_contains("[5d] 判定点①（转移④ 平今分支）", _eng,
               "if self._today_exit() == CLOSETODAY:")
check_contains("[5e] 两态守卫调用点（_restore / _book_open 各一次）", _eng,
               "_check_two_state_invariant(")
check("[5f] 事件 payload 键同步改名（close_mode → today_exit，避免新旧字段混库）",
      "today_exit=self._today_exit()" in _eng, True)

_main = io.open(os.path.join(_REPO, "Trading/main.py"), encoding="utf-8").read()
check_contains("[5g] 启动横幅不打交易所（格式化串里没有 exchange 位）", _main,
               '"[gw] 品种 {}：开仓 {} ｜ 平昨 {} ｜ 平今 {}"')


# ══════════════════════════════════════════════════════════════════
# [6] 覆盖面自检
# ══════════════════════════════════════════════════════════════════
print("\n[6] 覆盖面自检：关键文件必须在扫描集合里（防范围被改窄）")

_MUST = ["Trading/Infra/Product.py", "Trading/Infra/Instrument.py",
         "Trading/Engine/Engine.py", "Trading/main.py", "Trading/Config.py",
         "Trading/Broker/SimNow.py", "Trading/Broker/Base.py",
         "Trading/Infra/Records.py"]
check("[6a] 关键生产文件全部在扫描集合内",
      [m for m in _MUST if m not in _scanned], [])
check_true("[6b] 扫描到的 .py 文件数 ≥ 100（范围没被砍）+ 实得 {}".format(
    len(_scanned)), len(_scanned) >= 100)


# ══════════════════════════════════════════════════════════════════
print("\n============================================================")
print("P56 交易所字段删除 + today_exit 改名 结果: {} 通过 / {} 失败".format(
    _PASS, _FAIL))
print("============================================================")
raise SystemExit(1 if _FAIL else 0)
