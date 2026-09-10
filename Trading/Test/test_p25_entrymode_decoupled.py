# -*- coding: utf-8 -*-
"""
P25 EntryMode 解耦契约测试（2026-09-10 规则 ⑸ 改造后的防回潮护栏）
==================================================================
背景
----
规则 ⑸ 改造前，离场方式由 `entry_mode` 联动决定：

    OPEN_FIRST   → SOFT_EXIT → OrderIntent.LOCK  + 反向 side（锁仓软离场）
    UNLOCK_FIRST → HARD_EXIT → OrderIntent.CLOSE + pos.side （平仓硬离场）

该假定隐含"OPEN_FIRST 仓当日开、当日平"。实际"当日开仓、隔日才触发离场"时，
这笔仓物理上已是昨仓，却仍走 LOCK（开反向今仓）→ 多付一次开仓费，且次日
还要再平两笔，劣于直接平昨。2026-09-10 用户拍板：改为**按建仓日期判定**
（`Engine._exit_intent(pos, today)` 看 `Position.entry_date`）。

由此产生一个**语义残骸**：`EntryMode.OPEN_FIRST` / `UNLOCK_FIRST` 不再参与
任何决策，只剩审计标签作用（唯一有决策语义的是 `LOCKED`，9 处判定）。
名字字面义"入场来源"仍成立，但极易被后来人（或 AI）误读为"决定离场方式"，
重新写回 `if entry_mode is UNLOCK_FIRST: ...` —— 即回潮。

本测试把"已解耦"从注释升级为**可执行契约**，三层护栏：

  [1] 行为等价：entry_mode ∈ {OPEN_FIRST, UNLOCK_FIRST} × entry_date ∈ {今日, 跨日}
      四种组合下，_exit_intent 结果**只随 entry_date 变，不随 entry_mode 变**。
  [2] 源码契约：_exit_intent 的实现里不得出现 OPEN_FIRST / UNLOCK_FIRST 字样
      （inspect.getsource 直接读运行时源码，改实现即红）。
  [3] 全仓扫描：Trading/ 非测试代码中不得存在对这两个值的判定性比较
      （`is` / `is not` / `==` / `!=`）。允许赋值（贴标签）与 .value（序列化）。

另附 [4] 持久化往返：确保 state.db 里的 "open_first"/"unlock_first" 字符串
在 to_dict / from_dict 后不变（为将来可能的改名/合并保留兼容基线）。

跑法：python Trading/Test/test_p25_entrymode_decoupled.py
"""
from __future__ import annotations

import datetime as _dt
import inspect
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading.Engine.Engine import TradingEngine  # noqa: E402
    from Trading.Infra.Types import (  # noqa: E402
        EntryMode, ExitPlan, OrderIntent, Position, Side,
    )
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    raise SystemExit(2)

_PASS = 0
_FAIL = 0

_CN = _dt.timezone(_dt.timedelta(hours=8))
_TODAY = _dt.datetime.now(_CN).date().isoformat()
_YESTERDAY = (_dt.datetime.now(_CN).date() - _dt.timedelta(days=1)).isoformat()


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def mk_pos(side=Side.LONG, entry_mode=EntryMode.OPEN_FIRST, entry_date=_TODAY):
    return Position(symbol="CFFEX.IF2609", side=side, volume=2,
                    entry_price=4000.0, entry_at="", entry_bar_ts=0,
                    signal_key="k", open_order_id="o",
                    exit_plan=ExitPlan(name="x", stop_price=3990.0),
                    entry_mode=entry_mode, entry_date=entry_date)


# ════════════════════════════════════════════════════════════════
print("\n[1] 行为等价矩阵：离场方式只随 entry_date 变，不随 entry_mode 变")
# ════════════════════════════════════════════════════════════════
_MODES = (EntryMode.OPEN_FIRST, EntryMode.UNLOCK_FIRST)

# 1a. 今日单：两种来源都必须 LOCK + 反向 side
for m in _MODES:
    i, s = TradingEngine._exit_intent(mk_pos(entry_mode=m, entry_date=_TODAY), _TODAY)
    check("今日单 / {} → LOCK + SHORT".format(m.value), (i, s),
          (OrderIntent.LOCK, Side.SHORT))

# 1b. 跨日单：两种来源都必须 CLOSE + 原 side
for m in _MODES:
    i, s = TradingEngine._exit_intent(mk_pos(entry_mode=m, entry_date=_YESTERDAY), _TODAY)
    check("跨日单 / {} → CLOSE + LONG".format(m.value), (i, s),
          (OrderIntent.CLOSE, Side.LONG))

# 1c. 同一 entry_date 下两种来源结果必须逐字节相同（核心契约）
for d, label in ((_TODAY, "今日"), (_YESTERDAY, "跨日")):
    a = TradingEngine._exit_intent(mk_pos(entry_mode=EntryMode.OPEN_FIRST, entry_date=d), _TODAY)
    b = TradingEngine._exit_intent(mk_pos(entry_mode=EntryMode.UNLOCK_FIRST, entry_date=d), _TODAY)
    check("{}：OPEN_FIRST 与 UNLOCK_FIRST 结果一致".format(label), a, b)

# 1d. 反向仓（SHORT）同样成立
for m in _MODES:
    i, s = TradingEngine._exit_intent(
        mk_pos(side=Side.SHORT, entry_mode=m, entry_date=_TODAY), _TODAY)
    check("今日空单 / {} → LOCK + LONG".format(m.value), (i, s),
          (OrderIntent.LOCK, Side.LONG))

# 1e. 唯一例外：LOCKED 恒走 UNLOCK，与日期无关（防御分支）
for d in (_TODAY, _YESTERDAY, ""):
    i, s = TradingEngine._exit_intent(mk_pos(entry_mode=EntryMode.LOCKED, entry_date=d), _TODAY)
    check("LOCKED / entry_date={!r} → UNLOCK + LONG".format(d), (i, s),
          (OrderIntent.UNLOCK, Side.LONG))


# ════════════════════════════════════════════════════════════════
print("\n[2] 源码契约：_exit_intent 实现中不得出现 OPEN_FIRST / UNLOCK_FIRST")
# ════════════════════════════════════════════════════════════════
_src = inspect.getsource(TradingEngine._exit_intent)
# 只查**代码体**：docstring 里保留"为什么废弃旧 entry_mode 联动"的说明是必要的，
# 其中必然会提到这两个名字。用 AST 定位 docstring 行区间并剔除，避免误报。
_doc_lines = set()
try:
    import ast
    import textwrap
    _tree = ast.parse(textwrap.dedent(_src))
    _n0 = _tree.body[0].body[0]
    if (isinstance(_n0, ast.Expr) and isinstance(_n0.value, ast.Constant)
            and isinstance(_n0.value.value, str)):
        _doc_lines = set(range(_n0.lineno, (_n0.end_lineno or _n0.lineno) + 1))
except Exception:
    _doc_lines = set()
_code = "\n".join(l for i, l in enumerate(_src.splitlines(), 1) if i not in _doc_lines)

check("_exit_intent 代码体不含 'OPEN_FIRST'", "OPEN_FIRST" in _code, False)
check("_exit_intent 代码体不含 'UNLOCK_FIRST'", "UNLOCK_FIRST" in _code, False)
check("_exit_intent 代码体含 'entry_date'（按日期判定的证据）", "entry_date" in _code, True)
check("docstring 已剔除（行数 {}）".format(len(_doc_lines)), len(_doc_lines) > 0, True)


# ════════════════════════════════════════════════════════════════
print("\n[3] 全仓扫描：非测试代码不得对 OPEN_FIRST / UNLOCK_FIRST 做判定性比较")
# ════════════════════════════════════════════════════════════════
# 判定性比较 = is / is not / == / != 后紧跟 EntryMode.OPEN_FIRST|UNLOCK_FIRST
# （反向形式 "EntryMode.X is ..." 一并覆盖）
_PAT = re.compile(
    r"(?:is\s+not\s+|\bis\s+|==|!=)\s*EntryMode\.(?:OPEN_FIRST|UNLOCK_FIRST)"
    r"|EntryMode\.(?:OPEN_FIRST|UNLOCK_FIRST)\s*(?:\bis\s+not\b|\bis\b|==|!=)")

_hits = []
for _dir in ("Broker", "Engine", "Infra", "Risk", "Source", "State", "Strategy", "Tool"):
    _d = os.path.join(_TG_ROOT, _dir)
    if not os.path.isdir(_d):
        continue
    for _root, _dirs, _files in os.walk(_d):
        _dirs[:] = [x for x in _dirs if x != "__pycache__"]
        for _fn in _files:
            if not _fn.endswith(".py"):
                continue
            _p = os.path.join(_root, _fn)
            try:
                _text = io.open(_p, encoding="utf-8").read()
            except Exception:
                continue
            for _ln, _line in enumerate(_text.splitlines(), 1):
                if _PAT.search(_line):
                    _hits.append("{}:{}: {}".format(
                        os.path.relpath(_p, _TG_ROOT), _ln, _line.strip()))

check("判定性比较命中数为 0（当前 {} 处）".format(len(_hits)), _hits, [])
for h in _hits:
    print("      命中 → {}".format(h))


# ════════════════════════════════════════════════════════════════
print("\n[4] 持久化往返：state.db 字符串值不变（改名/合并的兼容基线）")
# ════════════════════════════════════════════════════════════════
for m in (EntryMode.OPEN_FIRST, EntryMode.UNLOCK_FIRST, EntryMode.LOCKED):
    p = mk_pos(entry_mode=m, entry_date=_YESTERDAY)
    back = Position.from_dict(p.to_dict())
    check("{} 往返后 entry_mode 不变".format(m.value), back.entry_mode, m)
    check("{} 序列化值 = {!r}".format(m.value, m.value), p.to_dict()["entry_mode"], m.value)

# 缺字段 / 坏值 → 回退 OPEN_FIRST（旧持仓记录兼容）
check("缺 entry_mode 字段 → OPEN_FIRST",
      Position.from_dict({k: v for k, v in mk_pos().to_dict().items()
                          if k != "entry_mode"}).entry_mode,
      EntryMode.OPEN_FIRST)
_bad = mk_pos().to_dict()
_bad["entry_mode"] = "not_a_mode"
check("坏值 entry_mode → OPEN_FIRST", Position.from_dict(_bad).entry_mode,
      EntryMode.OPEN_FIRST)


print("\n" + "=" * 60)
print("P25 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
