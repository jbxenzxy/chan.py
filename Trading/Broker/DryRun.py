# -*- coding: utf-8 -*-
"""
dry-run 撮合
============
不发真实委托，但完整走完"价格对齐 + 滑点让价 + 生成回执"的全部语义。
目的是让**除撮合之外的一切**（合约映射、风控、状态机、幂等、落盘、统计）
都能在不需要账号、不受交易时段限制的情况下被验证。

撮合假设（刻意保守）
    - 开仓成交价 = 参考价 往不利方向推 slippage_ticks 个 tick，再对齐 price_tick
    - 平仓成交价 = 触发价 往不利方向推 slippage_ticks 个 tick，再对齐 price_tick
    - 不做成交量/排队假设：一律视为立即全部成交
    （真实 CTP 会有部分成交与排队，M3 接实盘时这里要换成真实回执）

撮合语义（2026-09-11 重构）
---------------------------
按 intent 决定让价方向：
  - OPEN   开仓：往不利方向让 slippage_ticks 个 tick，再按 align_entry 对齐
  - CLOSE  平仓：往不利方向让 slippage_ticks 个 tick，再按 align_exit 对齐
`is_exit`（是否离场）对 dry-run 无影响 —— 本 broker 一律立即全部成交，
不存在追价。保留该参数只为与 Broker 接口签名一致。

meta 里记录 intent / offset 供日志审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Order, OrderIntent, Side, now_cn
from .Base import INTENT_TO_OFFSET, Broker, register_broker


@register_broker
class DryRunBroker(Broker):
    name = "dry_run"

    def __init__(self, spec: InstrumentSpec, params=None):
        super().__init__(spec, params)
        # R1（2026-09-10）：报单序号改由 Base 的自增整数提供（原 itertools.count(1)
        #   是进程内计数器，重启归零 → order_id 与上一进程相撞 → 审计记录被覆盖）。
        #   序号由引擎 `_restore` 从 state.db 抬升（seed_order_seq）。
        self.orders: List[Order] = []

    def submit(self, intent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "",
               entry_date: str = "", is_exit: bool = False) -> Order:
        intent = self._resolve_intent(intent, side)
        spec = self.spec
        sign = side.sign

        is_open_like = intent is OrderIntent.OPEN
        slip = spec.slippage_ticks * spec.price_tick
        if is_open_like:
            slipped = ref_price + sign * slip
        else:
            slipped = ref_price - sign * slip
        aligned = spec.align_entry(slipped, sign) if is_open_like \
            else spec.align_exit(slipped, sign)

        offset_str = INTENT_TO_OFFSET[intent]
        action_str = "open" if is_open_like else "close"

        o = Order(
            order_id=self._next_order_id(),
            signal_key=signal_key, symbol=spec.trade_symbol, side=side,
            action=action_str, volume=int(volume), price=aligned,
            req_price=float(ref_price), filled_price=aligned,
            status="filled", created_at=now_cn(), broker=self.name, note=note,
            meta={
                "dry_run": True,
                "slippage_ticks": spec.slippage_ticks,
                "intent": intent.value,            # 记账：开 / 平
                "offset": offset_str,              # 记账：CTP 报文类型
                "offset_close_yesterday_first": bool(
                    spec.close_today_first),       # 成本计算时按此选平今/平昨费率
                "entry_date": entry_date,          # 2026-09-10：被平持仓建仓日（今/昨仓审计用）
            },
        )
        self.orders.append(o)
        return o

    def stats(self) -> Dict[str, Any]:
        return {"broker": self.name, "orders": len(self.orders)}

    def trade_confirmed(self, intent, signal_key: str = "") -> bool:
        """dry_run 撮合是同步的，submit 返回 filled 即视为真实成交。

        不需要二次复核。返回 True 让引擎的卡单复核直接通过。
        """
        return True
