# -*- coding: utf-8 -*-
"""
合约规格 / 价格对齐 / 成本模型
==============================
v1 的合约映射用**配置表**（`config.json` 的 instrument.trade_symbol）。
M2 接 tqsdk 后换成 `quote.underlying_symbol` 动态解析，接口不变——
这是刻意留的替换点，不要把这层的调用散到引擎里。

成本口径（重要）
    - 手续费按比率折算成"点数"：费 = 价格 × 费率，单位就是指数点
    - 滑点**不计入** cost_points，而是体现在成交价上（见 dry_run broker 的让价）
      否则同一笔滑点会被算两次，回测虚高、实盘对不上
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class InstrumentSpec:
    signal_symbol: str = "KQ.m@CFFEX.IF"      # 缠论分析用的主连
    trade_symbol: str = "CFFEX.IF2609"        # 实际下单的月份合约
    price_tick: float = 0.2                   # IF 最小变动价位
    multiplier: float = 300.0                 # 合约乘数（元/点）
    open_fee_rate: float = 0.000023           # 开仓 0.0023%
    close_today_fee_rate: float = 0.000345    # 平今 0.0345%（中金所，期指很贵）
    close_fee_rate: float = 0.000023          # 平昨 0.0023%
    slippage_ticks: float = 1.0               # 单边滑点（tick 数）
    overprice_points: float = 0.6             # 超价点数：下单价 = 实时对手价 ± overprice_points（朝成交方向取整到 tick）；IF tick=0.2 时 0.6 恰好 3 tick
    close_today_first: bool = True            # 平仓优先平今
    sessions: List[str] = field(default_factory=lambda: ["09:30-11:30", "13:00-15:00"])

    # ---------- 价格对齐 ----------
    def round_price(self, price: float, mode: str = "nearest") -> float:
        """把价格对齐到 price_tick。mode: nearest / up / down。"""
        tick = self.price_tick
        if not tick or tick <= 0:
            return price
        raw = price / tick
        if mode == "up":
            n = math.ceil(raw - 1e-9)
        elif mode == "down":
            n = math.floor(raw + 1e-9)
        else:
            n = math.floor(raw + 0.5)
        return round(n * tick, 10)

    def align_entry(self, price: float, side_sign: int) -> float:
        """开仓价对齐：买向上、卖向下（让价方向 = 不利方向，保守）。"""
        return self.round_price(price, "up" if side_sign > 0 else "down")

    def align_exit(self, price: float, side_sign: int) -> float:
        """平仓价对齐：平多＝卖出向下，平空＝买入向上。"""
        return self.round_price(price, "down" if side_sign > 0 else "up")

    def slip_price(self, price: float, side_sign: int, for_open: bool) -> float:
        """叠加滑点。开仓时顺着不利方向推，平仓同理。"""
        return price + side_sign * self.slippage_ticks * self.price_tick \
            if for_open else price - side_sign * self.slippage_ticks * self.price_tick

    # ---------- 成本 ----------
    def cost_points(self, entry_price: float, exit_price: float,
                    close_today: bool = True) -> float:
        """往返手续费，折算成点数。滑点不在此处计（见模块 docstring）。"""
        rate = self.close_today_fee_rate if close_today else self.close_fee_rate
        return entry_price * self.open_fee_rate + exit_price * rate

    def points_to_cash(self, points: float, volume: int = 1) -> float:
        return points * self.multiplier * volume

    # ---------- 交易时段 ----------
    def in_session(self, date_str: str) -> bool:
        """date_str 形如 "2026-09-01 09:35"，只取时间部分判断。"""
        parts = (date_str or "").split()
        if len(parts) < 2:
            return True
        hm = parts[1][:5]
        for seg in self.sessions:
            if "-" not in seg:
                continue
            a, b = seg.split("-", 1)
            if a <= hm <= b:
                return True
        return False

    def is_new_day(self, prev_date: str, cur_date: str) -> bool:
        return (prev_date or "")[:10] != (cur_date or "")[:10]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstrumentSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})
