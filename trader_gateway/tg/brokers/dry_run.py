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

Phase C（2026-09-05）
---------------------
submit() 接受 OrderIntent，按 intent 决定报文与撮合语义：
  - OPEN     同旧 "open"
  - UNLOCK   "unlock" = 平昨 → 与 CLOSE 同路径；偏移方向=平旧仓方向，cost 计算用平昨费率
  - CLOSE    同旧 "close"
  - LOCK     "lock"   = 开反向同手数 → side 已由引擎填为 pos.side 的反向，
            撮合方向按"开仓"算（往不利方向让 slippage_ticks tick）
记录 offset_meta 字段（OPEN/CLOSEYESTERDAY/CLOSE）供日志审计。
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

from ..symbols import InstrumentSpec
from ..types import Order, OrderIntent, Side, now_cn
from .base import INTENT_TO_OFFSET, Broker, register_broker


@register_broker
class DryRunBroker(Broker):
    name = "dry_run"

    def __init__(self, spec: InstrumentSpec, params=None):
        super().__init__(spec, params)
        self._seq = itertools.count(1)
        self.orders: List[Order] = []

    def submit(self, intent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        intent = self._resolve_intent(intent, side)
        spec = self.spec
        sign = side.sign

        # LOCK 是"开反向同手数"——和 OPEN 一样按开仓语义撮合（往不利方向让滑点）
        is_open_like = intent in (OrderIntent.OPEN, OrderIntent.LOCK)
        slip = spec.slippage_ticks * spec.price_tick
        if is_open_like:
            slipped = ref_price + sign * slip
        else:
            # CLOSE / UNLOCK：平仓语义，往不利方向让价
            slipped = ref_price - sign * slip
        aligned = spec.align_entry(slipped, sign) if is_open_like \
            else spec.align_exit(slipped, sign)

        offset_str = INTENT_TO_OFFSET[intent]
        action_str = "open" if is_open_like else "close"

        o = Order(
            order_id="{}-{:06d}".format(self.name, next(self._seq)),
            signal_key=signal_key, symbol=spec.trade_symbol, side=side,
            action=action_str, volume=int(volume), price=aligned,
            req_price=float(ref_price), filled_price=aligned,
            status="filled", created_at=now_cn(), broker=self.name, note=note,
            meta={
                "dry_run": True,
                "slippage_ticks": spec.slippage_ticks,
                "intent": intent.value,            # Phase C：记账 intent
                "offset": offset_str,              # Phase C：记账 CTP 报文类型
                "offset_close_yesterday_first": bool(
                    spec.close_today_first),       # 成本计算时按此选平今/平昨费率
            },
        )
        self.orders.append(o)
        return o

    def stats(self) -> Dict[str, Any]:
        return {"broker": self.name, "orders": len(self.orders)}

    def equity(self, source: str = "available") -> Optional[float]:
        """离线模拟权益。仅在 broker_params 里显式配了 sim_equity 时才返回非 None。

        用途：不开真户也能在 dry_run 下验证仓位管理（sizing）算得对不对。
        例：broker_params={"sim_equity": 1000000} 表示按 100 万本金定手数。
        未配置则返回 None —— 此时 PositionSizer 按 fallback_volume 保守回退，
        dry_run 的默认行为（固定 1 手）不受影响。
        """
        v = self.params.get("sim_equity")
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f <= 0:
            return None
        return f
