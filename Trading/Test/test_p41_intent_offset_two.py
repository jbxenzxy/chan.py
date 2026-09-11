# -*- coding: utf-8 -*-
"""
P41 报单意图 → CTP offset 只有两个值（2026-09-11）
=====================================================
背景（一期架构约束 A4 / 文档 §5.2 / 退役 p11）
-------------------------------------------------
`OrderIntent` 从旧版的 4 值（open / close / lock / unlock）收敛为 2 值
（OPEN / CLOSE）。CTP 报文 offset 由 `Broker/Base.INTENT_TO_OFFSET` **唯一**
决定（见 `SimNow.submit` 注释："offset 由 INTENT_TO_OFFSET 决定"）。

本测试钉死这条映射：
  · 键集合恰为 {OPEN, CLOSE} —— 不能多（没有 lock/unlock/CLOSETODAY 出口），
    也不能少；
  · 值集合恰为 {"OPEN", "CLOSE"} —— 中金所下 CLOSE 恒为平昨（无平今分支），
    一期不做 CLOSETODAY（A4 二期预留，故意不放）；
  · 每个 OrderIntent 都有映射（无遗漏 → 不会出现 None offset 把报单发飞）。

为什么可以断言：offset 是纯查表，无运行时分支；这是"净敞口模型"能成立的前提
（④ 锁仓与 ① 开仓都走 OPEN、⑤ 平仓走 CLOSE，靠 is_exit 区分语义而非靠
不同的 offset）。若有人偷偷加回 lock/unlock 或 CLOSETODAY，本测试立即变红。

跑法：python Trading/Test/test_p41_intent_offset_two.py
"""
from __future__ import annotations

import os
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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.Base import INTENT_TO_OFFSET  # noqa: E402
from Trading.Infra.Types import OrderIntent  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


print("\n[1] 映射表结构：键 = {OPEN, CLOSE}，值 = {OPEN, CLOSE}")
check("[1a] 键集合恰为 {OPEN, CLOSE}",
      set(INTENT_TO_OFFSET.keys()), {OrderIntent.OPEN, OrderIntent.CLOSE})
check("[1b] 值集合恰为 {'OPEN', 'CLOSE'}（无 lock/unlock/CLOSETODAY 出口）",
      set(INTENT_TO_OFFSET.values()), {"OPEN", "CLOSE"})
check("[1c] 映射条目数 = 2（不多不少）", len(INTENT_TO_OFFSET), 2)


print("\n[2] 每个意图都有确定映射（无 None offset）")
for intent in (OrderIntent.OPEN, OrderIntent.CLOSE):
    off = INTENT_TO_OFFSET.get(intent)
    check("[2] {} -> offset 非空字符串".format(intent.name),
          isinstance(off, str) and off != "", True)


print("\n[3] 语义钉死：OPEN→OPEN（开仓/反向开仓锁仓），CLOSE→CLOSE（平仓）")
check("[3a] OPEN 意图恒映射 offset=OPEN", INTENT_TO_OFFSET[OrderIntent.OPEN], "OPEN")
check("[3b] CLOSE 意图恒映射 offset=CLOSE（中金所平昨，无平今分支）",
      INTENT_TO_OFFSET[OrderIntent.CLOSE], "CLOSE")


print("\n" + "=" * 60)
print("P41 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
