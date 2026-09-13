# -*- coding: utf-8 -*-
"""
入场策略（Entry.py）
====================
入场策略（EntryPolicy，唯一入场策略，无"默认"之分）
    买点 → 开多；卖点 → 开空。
    已有持仓时：
      - 反向信号 → 只平不反手（reverse_on_opposite_signal=False，用户当前选择）
      - 同向信号 → 忽略
    无持仓时按信号方向开仓。
    信号质量过滤（振幅 / 止损距离上下限）已移除：是否值得开仓由缠论分析引擎
    在产生买卖点信号时判定，Trading 层只忠实执行已确认信号（仅保留信号价格
    无效的数据兜底，见 decide）。
"""

from __future__ import annotations

from typing import Optional

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Decision, DecisionType, Position, Signal
from ..Config import EntryConfig


class EntryPolicy:
    name = "EntryPolicy"

    def __init__(self, params=None):
        # 严格模式（2026-09-07）：参数由 EntryConfig 校验，缺省键用模型
        # 默认值（唯一来源在 Trading/Config.py），拼错的键立即报错。
        self.params = dict(params or {})
        p = EntryConfig(**self.params)
        self.p = p
        self.reverse = p.reverse_on_opposite_signal

    def describe(self) -> str:
        return "{}({})".format(self.name, self.params)

    def decide(self, signal: Signal, position: Optional[Position],
               spec: InstrumentSpec) -> Decision:
        if signal.price <= 0 or signal.high <= 0 or signal.low <= 0:
            return Decision(DecisionType.SKIP, reason="信号价格无效")

        if position is None:
            # 信号质量过滤（振幅 / 止损距离上下限）已移除：是否值得开仓由缠论
            # 分析引擎在产生买卖点信号时判定，Trading 层只忠实执行已确认信号。
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
