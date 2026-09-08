# -*- coding: utf-8 -*-
"""
入场策略（Entry.py）
====================
默认入场策略（DefaultEntryPolicy，用户当前规则）
    买点 → 开多；卖点 → 开空。
    已有持仓时：
      - 反向信号 → 只平不反手（reverse_on_opposite_signal=False，用户当前选择）
      - 同向信号 → 忽略
    无持仓时按信号方向开仓，可选三道过滤（默认 0=关闭）：
      信号K线振幅上限 / 止损距离下限 / 止损距离上限
"""

from __future__ import annotations

from typing import Optional

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Bar, Decision, DecisionType, ExitPlan, Position, Side, Signal
from ..Config import EntryConfig
from .Base import EntryPolicy, ExitCheck, ExitPolicy


class DefaultEntryPolicy(EntryPolicy):
    name = "DefaultEntryPolicy"

    def __init__(self, params=None):
        super().__init__(params)
        # 严格模式（2026-09-07）：参数由 EntryConfig 校验，缺省键用模型
        # 默认值（唯一来源在 Trading/Config.py），拼错的键立即报错。
        p = EntryConfig(**(self.params or {}))
        self.p = p
        self.reverse = p.reverse_on_opposite_signal
        self.max_range = float(p.max_signal_range_points or 0.0)
        self.min_stop_dist = float(p.min_stop_distance_points or 0.0)
        self.max_stop_dist = float(p.max_stop_distance_points or 0.0)

    def decide(self, signal: Signal, position: Optional[Position],
               spec: InstrumentSpec) -> Decision:
        if signal.price <= 0 or signal.high <= 0 or signal.low <= 0:
            return Decision(DecisionType.SKIP, reason="信号价格无效")

        if position is None:
            rng = signal.high - signal.low
            if self.max_range > 0 and rng > self.max_range:
                return Decision(DecisionType.SKIP,
                                reason="信号K线振幅 {:.2f} 超过上限 {:.2f}".format(
                                    rng, self.max_range))
            stop_dist = (signal.price - signal.low) if signal.is_buy \
                else (signal.high - signal.price)
            if self.min_stop_dist > 0 and stop_dist < self.min_stop_dist:
                return Decision(DecisionType.SKIP,
                                reason="止损距离 {:.2f} 小于下限 {:.2f}".format(
                                    stop_dist, self.min_stop_dist))
            if self.max_stop_dist > 0 and stop_dist > self.max_stop_dist:
                return Decision(DecisionType.SKIP,
                                reason="止损距离 {:.2f} 超过上限 {:.2f}".format(
                                    stop_dist, self.max_stop_dist))
            return Decision(DecisionType.OPEN, side=signal.side,
                            reason="无持仓，按{}点信号开{}".format(
                                "买" if signal.is_buy else "卖", signal.side))

        if position.side is not signal.side:
            if self.reverse:
                return Decision(DecisionType.CLOSE_AND_REVERSE, side=signal.side,
                                reason="反向信号，平仓并反手")
            return Decision(DecisionType.CLOSE_AND_HOLD, side=None,
                            reason="反向信号，只平今不反手")

        return Decision(DecisionType.SKIP, reason="同向信号，已有持仓，忽略")
