# -*- coding: utf-8 -*-
"""
两态状态机（Backtest/State.py）
================================
设计文档 §5.3（Q11 / 第 ⑸ 项）：股票回测只有 **两个**状态 ——

    FLAT（空仓）  ⇄  IN_TRADE（持仓）

| 转移 | 条件 | 依据 |
|---|---|---|
| `FLAT + 买点`      | → 开多 | 第三轮 ⑵ |
| `FLAT + 卖点`      | → 开空 | 第三轮 ⑵ + §4.6（双向） |
| `IN_TRADE + 任何新信号` | → **拒收** | 第五轮 ⑴（Q13，`reverse_on_opposite_signal` 默认 False） |
| `IN_TRADE + 出场触发`   | → 平仓，回 `FLAT` | 每根判一次，触发即以该根 close 成交 |
| 同根平仓后再遇信号 | → **允许开新仓** | 第四轮 Q11 定案（不复用 `Trading/Engine` 的瞬态） |

为什么不复用 `Trading/Engine/Engine.py` 的 `EngineState`
---------------------------------------------------------------------
`EngineState` 是 `IDLE / OPENING / IN_TRADE / EXITING` 四态，其中
`OPENING` / `EXITING` 是"下单到成交之间"的瞬态；回测里撮合是瞬时的
⇒ 两个瞬态退化，剩下的正是 `IDLE ⇄ IN_TRADE` 两态。

但 Engine 还耦合了 `PositionBook`、对账、资金闸门、CTP 报文，且其账户三态里的
**LOCKED（锁仓）是期货专有**（多空互锁），股票不存在 ⇒ 复用不划算，
本模块自写一个约 30 行的两态机（设计文档 §5.3 定案：同构但不复用）。
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    """回测持仓状态（两态，穷举）。继承 `str` 便于落盘 / 打印。"""
    FLAT = "flat"            # 空仓：等待入场信号
    IN_TRADE = "in_trade"    # 持仓：只做出场判定，拒收一切新信号


def next_state(cur: State, *, entry: bool = False, exit_: bool = False) -> State:
    """两态转移（**唯一**改写点）。

    非法组合（同时 entry 与 exit_、或方向与当前态不匹配）直接抛错 ——
    两态机的全部价值就在"不可能的状态转移不可能表达"，静默兜底会把它变成四态机。
    """
    if entry and exit_:
        raise ValueError("State: 同一根既开又平（entry 与 exit_ 互斥）")
    if cur is State.FLAT:
        if exit_:
            raise ValueError("State: FLAT 态不可能触发出场")
        return State.IN_TRADE if entry else State.FLAT
    if entry:
        raise ValueError("State: IN_TRADE 态不得开新仓（持仓期拒收信号，Q13）")
    return State.FLAT if exit_ else State.IN_TRADE
