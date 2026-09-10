# -*- coding: utf-8 -*-
"""
P25 PositionOrigin 解耦契约测试（2026-09-10 规则 ⑸ 改造后的防回潮护栏）
==================================================================
背景
----
规则 ⑸ 改造前，离场方式由「持仓来源标签」联动决定：

    SIGNAL_OPEN   → SOFT_EXIT → OrderIntent.LOCK  + 反向 side（锁仓软离场）
    UNLOCK_UPGRADE → HARD_EXIT → OrderIntent.CLOSE + pos.side （平仓硬离场）

该假定隐含"SIGNAL_OPEN 仓当日开、当日平"。实际"当日开仓、隔日才触发离场"时，
这笔仓物理上已是昨仓，却仍走 LOCK（开反向今仓）→ 多付一次开仓费，且次日
还要再平两笔，劣于直接平昨。2026-09-10 用户拍板：改为**按建仓日期判定**
（`Engine._exit_intent(pos, today)` 看 `Position.entry_date`）。

由此产生一个**语义残骸**：`PositionOrigin.SIGNAL_OPEN` / `UNLOCK_UPGRADE` 不再参与
任何决策，只剩审计标签作用（唯一有决策语义的是 `SOFT_EXIT_LOCK`）。
名字字面义"入场来源"仍成立，但极易被后来人（或 AI）误读为"决定离场方式"，
重新写回 `if origin is UNLOCK_UPGRADE: ...` —— 即回潮。

本测试把"已解耦"从注释升级为**可执行契约**，三层护栏：

  [1] 行为等价：origin ∈ {SIGNAL_OPEN, UNLOCK_UPGRADE} × entry_date ∈ {今日, 跨日}
      四种组合下，_exit_intent 结果**只随 entry_date 变，不随 origin 变**。
  [2] 源码契约：_exit_intent 的实现里不得出现 SIGNAL_OPEN / UNLOCK_UPGRADE 字样
      （inspect.getsource 直接读运行时源码，改实现即红）。
  [3] 全仓扫描：Trading/ 非测试代码中不得存在对这两个值的判定性比较
      （`is` / `is not` / `==` / `!=`）。允许赋值（贴标签）与 .value（序列化）。

另附 [4] 持久化往返：确保 state.db 里的 "signal_open"/"unlock_upgrade" 字符串
在 to_dict / from_dict 后不变（为将来可能的改名/合并保留兼容基线）。

另附 [5] 旧 schema 闸门：改名把持久化键从 entry_mode 换成 origin，
旧库静默回退会把锁仓持仓恢复成 SIGNAL_OPEN（敞口持仓）→ 纳入 L1-L3 会发错单。
本组断言 `Engine._legacy_position_records` 能准确挑出旧键记录（供 _restore 拒绝启动）。

跑法：python Trading/Test/test_p25_origin_decoupled.py
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
        PositionOrigin, ExitPlan, OrderIntent, Position, Side,
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


def mk_pos(side=Side.LONG, origin=PositionOrigin.SIGNAL_OPEN, entry_date=_TODAY):
    return Position(symbol="CFFEX.IF2609", side=side, volume=2,
                    entry_price=4000.0, entry_at="", entry_bar_ts=0,
                    signal_key="k", open_order_id="o",
                    exit_plan=ExitPlan(name="x", stop_price=3990.0),
                    origin=origin, entry_date=entry_date)


# ════════════════════════════════════════════════════════════════
print("\n[1] 行为等价矩阵：离场方式只随 entry_date 变，不随 origin 变")
# ════════════════════════════════════════════════════════════════
_MODES = (PositionOrigin.SIGNAL_OPEN, PositionOrigin.UNLOCK_UPGRADE)

# 1a. 今日单：两种来源都必须 LOCK + 反向 side
for m in _MODES:
    i, s = TradingEngine._exit_intent(mk_pos(origin=m, entry_date=_TODAY), _TODAY)
    check("今日单 / {} → LOCK + SHORT".format(m.value), (i, s),
          (OrderIntent.LOCK, Side.SHORT))

# 1b. 跨日单：两种来源都必须 CLOSE + 原 side
for m in _MODES:
    i, s = TradingEngine._exit_intent(mk_pos(origin=m, entry_date=_YESTERDAY), _TODAY)
    check("跨日单 / {} → CLOSE + LONG".format(m.value), (i, s),
          (OrderIntent.CLOSE, Side.LONG))

# 1c. 同一 entry_date 下两种来源结果必须逐字节相同（核心契约）
for d, label in ((_TODAY, "今日"), (_YESTERDAY, "跨日")):
    a = TradingEngine._exit_intent(mk_pos(origin=PositionOrigin.SIGNAL_OPEN, entry_date=d), _TODAY)
    b = TradingEngine._exit_intent(mk_pos(origin=PositionOrigin.UNLOCK_UPGRADE, entry_date=d), _TODAY)
    check("{}：SIGNAL_OPEN 与 UNLOCK_UPGRADE 结果一致".format(label), a, b)

# 1d. 反向仓（SHORT）同样成立
for m in _MODES:
    i, s = TradingEngine._exit_intent(
        mk_pos(side=Side.SHORT, origin=m, entry_date=_TODAY), _TODAY)
    check("今日空单 / {} → LOCK + LONG".format(m.value), (i, s),
          (OrderIntent.LOCK, Side.LONG))

# 1e. 唯一例外：SOFT_EXIT_LOCK 恒走 UNLOCK，与日期无关（防御分支）
for d in (_TODAY, _YESTERDAY, ""):
    i, s = TradingEngine._exit_intent(mk_pos(origin=PositionOrigin.SOFT_EXIT_LOCK, entry_date=d), _TODAY)
    check("SOFT_EXIT_LOCK / entry_date={!r} → UNLOCK + LONG".format(d), (i, s),
          (OrderIntent.UNLOCK, Side.LONG))


# ════════════════════════════════════════════════════════════════
print("\n[2] 源码契约：_exit_intent 实现中不得出现 SIGNAL_OPEN / UNLOCK_UPGRADE")
# ════════════════════════════════════════════════════════════════
_src = inspect.getsource(TradingEngine._exit_intent)
# 只查**代码体**：docstring 里保留"为什么废弃旧「来源决定离场」联动"的说明是必要的，
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

check("_exit_intent 代码体不含 'SIGNAL_OPEN'", "SIGNAL_OPEN" in _code, False)
check("_exit_intent 代码体不含 'UNLOCK_UPGRADE'", "UNLOCK_UPGRADE" in _code, False)
check("_exit_intent 代码体含 'entry_date'（按日期判定的证据）", "entry_date" in _code, True)
check("docstring 已剔除（行数 {}）".format(len(_doc_lines)), len(_doc_lines) > 0, True)


# ════════════════════════════════════════════════════════════════
print("\n[3] 全仓扫描：非测试代码不得对 SIGNAL_OPEN / UNLOCK_UPGRADE 做判定性比较")
# ════════════════════════════════════════════════════════════════
# 判定性比较 = is / is not / == / != 后紧跟 PositionOrigin.SIGNAL_OPEN|UNLOCK_UPGRADE
# （反向形式 "PositionOrigin.X is ..." 一并覆盖）
_PAT = re.compile(
    r"(?:is\s+not\s+|\bis\s+|==|!=)\s*PositionOrigin\.(?:SIGNAL_OPEN|UNLOCK_UPGRADE)"
    r"|PositionOrigin\.(?:SIGNAL_OPEN|UNLOCK_UPGRADE)\s*(?:\bis\s+not\b|\bis\b|==|!=)")

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
for m in (PositionOrigin.SIGNAL_OPEN, PositionOrigin.UNLOCK_UPGRADE, PositionOrigin.SOFT_EXIT_LOCK):
    p = mk_pos(origin=m, entry_date=_YESTERDAY)
    back = Position.from_dict(p.to_dict())
    check("{} 往返后 origin 不变".format(m.value), back.origin, m)
    check("{} 序列化值 = {!r}".format(m.value, m.value), p.to_dict()["origin"], m.value)

# 缺字段 / 坏值 → 回退 SIGNAL_OPEN（容错，避免一条脏记录阻断整簿恢复）
check("缺 origin 字段 → SIGNAL_OPEN",
      Position.from_dict({k: v for k, v in mk_pos().to_dict().items()
                          if k != "origin"}).origin,
      PositionOrigin.SIGNAL_OPEN)
_bad = mk_pos().to_dict()
_bad["origin"] = "not_a_mode"
check("坏值 origin → SIGNAL_OPEN", Position.from_dict(_bad).origin,
      PositionOrigin.SIGNAL_OPEN)


# ════════════════════════════════════════════════════════════════
print("\n[5] 旧 schema 闸门：拒绝启动而不是静默把锁仓持仓当敞口持仓")
# ════════════════════════════════════════════════════════════════
# 改名后 key 由 entry_mode → origin。旧库静默回退会把 "locked" 持仓恢复成
# SIGNAL_OPEN（敞口持仓）→ 被纳入 L1-L3，可能对锁仓持仓发平仓单。
check("旧键 entry_mode 记录被识别",
      len(TradingEngine._legacy_position_records(
          [{"entry_mode": "locked", "symbol": "X"}])), 1)
check("新键 origin 记录不被误判",
      TradingEngine._legacy_position_records(
          [{"origin": "soft_exit_lock", "symbol": "X"}]), [])
check("混合列表只挑旧键记录",
      len(TradingEngine._legacy_position_records(
          [{"origin": "signal_open"}, {"entry_mode": "locked"}])), 1)
check("非法条目（None / 非 dict）被忽略",
      TradingEngine._legacy_position_records([None, "x", 3]), [])


class _EvStub:
    def __init__(self):
        self.events = []

    def write(self, kind, **kw):
        self.events.append(kind)


class _GuardStub:
    """只为验证 _reject_legacy_state 的抛错路径（不需要真引擎）。"""
    _LEGACY_POSITION_KEYS = TradingEngine._LEGACY_POSITION_KEYS
    # 注意：从类上取 staticmethod 拿到的是裸函数，直接赋成类属性会变成实例方法
    # （self 被当第一个参数）→ 必须再包一层 staticmethod。
    _legacy_position_records = staticmethod(TradingEngine._legacy_position_records)
    _reject_legacy_state = TradingEngine._reject_legacy_state

    def __init__(self):
        self.ev = _EvStub()


_g = _GuardStub()
_raised = None
try:
    _g._reject_legacy_state([{"entry_mode": "locked", "symbol": "X"}])
except RuntimeError as e:
    _raised = e
check("旧 schema → 抛 RuntimeError 拒绝启动", isinstance(_raised, RuntimeError), True)
check("错误信息给出可执行处理方式",
      "state.db" in str(_raised) if _raised else False, True)
check("抛错前写了 state_schema_incompatible 事件",
      _g.ev.events, ["state_schema_incompatible"])

_g2 = _GuardStub()
_g2._reject_legacy_state([{"origin": "soft_exit_lock", "symbol": "X"}])
check("新 schema → 不抛错、不写事件", _g2.ev.events, [])


print("\n" + "=" * 60)
print("P25 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
