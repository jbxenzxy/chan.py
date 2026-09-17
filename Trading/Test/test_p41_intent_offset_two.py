# -*- coding: utf-8 -*-
"""
P41 报单意图 → CTP offset 只有三个值（三值化）
=============================================================================
背景（一期架构约束 A4 / 文档退役 p11）
-------------------------------------------------
`OrderIntent` 从旧版的 4 值（open / close / lock / unlock）收敛为 2 值
（OPEN / CLOSE）；（D6 平今开关）追加第 3 值 **CLOSETODAY**（平今）。
CTP 报文 offset 由 `Broker/Base.INTENT_TO_OFFSET`
**唯一**决定（见 `SimNow.submit` 注释："offset 由 INTENT_TO_OFFSET 决定"）。

本测试钉死这条映射：
  · 键集合恰为 {OPEN, CLOSE, CLOSETODAY} —— 不能多（没有 lock/unlock），
    也不能少；
  · 值集合恰为 {"OPEN", "CLOSE", "CLOSETODAY"} —— 都在 tqsdk 白名单内
    （api.py:1353 / lib/utils.py:39 三处硬校验）；
  · 每个 OrderIntent 都有映射（无遗漏 → 不会出现 None offset 把报单发飞）。

为什么可以断言：offset 是纯查表，无运行时分支；这是"净敞口模型"能成立的前提
（④ 锁仓与 ① 开仓都走 OPEN、⑤ 平仓走 CLOSE、④ 平今分支走 CLOSETODAY，
靠 is_exit / intent 区分语义而非靠不同的 offset）。若有人偷偷加回 lock/unlock，
本测试立即变红；CLOSETODAY 的**可用边界**由品种执行策略表第 1 列
（`ExecPolicy.close_mode`，2026-09-16 起取代原「交易所能力守卫」与
「费率 3× 派生开关」）守卫：引擎 _pre_trade_check + 转移④ 分支条件双处
消费，见 test_p51。

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
from Trading.Infra.Records import OrderIntent  # noqa: E402


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


print("\n[1] 映射表结构：键 = {OPEN, CLOSE, CLOSETODAY}，值 = {OPEN, CLOSE, CLOSETODAY}")
check("[1a] 键集合恰为 {OPEN, CLOSE, CLOSETODAY}",
      set(INTENT_TO_OFFSET.keys()),
      {OrderIntent.OPEN, OrderIntent.CLOSE, OrderIntent.CLOSETODAY})
check("[1b] 值集合恰为 {'OPEN', 'CLOSE', 'CLOSETODAY'}（无 lock/unlock 出口）",
      set(INTENT_TO_OFFSET.values()), {"OPEN", "CLOSE", "CLOSETODAY"})
check("[1c] 映射条目数 = 3（不多不少）", len(INTENT_TO_OFFSET), 3)


print("\n[2] 每个意图都有确定映射（无 None offset）")
for intent in (OrderIntent.OPEN, OrderIntent.CLOSE, OrderIntent.CLOSETODAY):
    off = INTENT_TO_OFFSET.get(intent)
    check("[2] {} -> offset 非空字符串".format(intent.name),
          isinstance(off, str) and off != "", True)


print("\n[3] 语义钉死：OPEN→OPEN，CLOSE→CLOSE（平昨），CLOSETODAY→CLOSETODAY（平今）")
check("[3a] OPEN 意图恒映射 offset=OPEN", INTENT_TO_OFFSET[OrderIntent.OPEN], "OPEN")
check("[3b] CLOSE 意图恒映射 offset=CLOSE（跨日仓恒平昨）",
      INTENT_TO_OFFSET[OrderIntent.CLOSE], "CLOSE")
check("[3c] CLOSETODAY 意图恒映射 offset=CLOSETODAY（表第 1 列 = CLOSETODAY）",
      INTENT_TO_OFFSET[OrderIntent.CLOSETODAY], "CLOSETODAY")


print("\n" + "=" * 60)
print("P41 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
