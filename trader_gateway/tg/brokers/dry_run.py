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
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List

from ..symbols import InstrumentSpec
from ..types import Order, Side, now_cn
from .base import Broker, register_broker


@register_broker
class DryRunBroker(Broker):
    name = "dry_run"

    def __init__(self, spec: InstrumentSpec, params=None):
        super().__init__(spec, params)
        self._seq = itertools.count(1)
        self.orders: List[Order] = []

    def submit(self, action: str, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        spec = self.spec
        sign = side.sign

        slip = spec.slippage_ticks * spec.price_tick
        if action == "open":
            slipped = ref_price + sign * slip
        else:
            slipped = ref_price - sign * slip
        aligned = spec.align_entry(slipped, sign) if action == "open" \
            else spec.align_exit(slipped, sign)

        o = Order(
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=spec.trade_symbol, side=side,
            action=action, volume=int(volume), price=aligned,
            req_price=float(ref_price), filled_price=aligned,
            status="filled", created_at=now_cn(), broker=self.name, note=note,
            meta={"dry_run": True, "slippage_ticks": spec.slippage_ticks},
        )
        self.orders.append(o)
        return o

    def stats(self) -> Dict[str, Any]:
        return {"broker": self.name, "orders": len(self.orders)}
