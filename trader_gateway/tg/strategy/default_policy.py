# -*- coding: utf-8 -*-
"""
默认策略实现（用户当前规则）
============================
入场（DefaultEntryPolicy）
    买点 → 开多；卖点 → 开空。
    已有持仓时：
      - 反向信号 → 只平不反手（reverse_on_opposite_signal=False，用户当前选择）
      - 同向信号 → 忽略
    无持仓时按信号方向开仓。

出场（DefaultExitPolicy）
    多头：止盈 = 入场价 + N 点；止损 = 信号 K 线最低价
    空头：完全镜像，止损 = 信号 K 线最高价
    把 stop_at_signal_extreme 置 false 可切换为固定点数止损，便于对比两类止损。

三个刻意保留的保守设定
    ① 同根 K 线同时触及止盈与止损 → **按止损计**（不猜盘中先后顺序）
    ② 价格对齐一律往"对自己不利"的方向取整（止损更易触发、止盈更晚更少）
    ③ 出场计划里带上参数快照，落盘后可做事后参数敏感性分析
"""
from __future__ import annotations

from typing import Optional

from ..symbols import InstrumentSpec
from ..types import Bar, Decision, DecisionType, ExitPlan, Position, Side, Signal
from .base import EntryPolicy, ExitCheck, ExitPolicy, register_entry, register_exit


@register_exit
class DefaultExitPolicy(ExitPolicy):
    name = "DefaultExitPolicy"

    def __init__(self, params=None):
        super().__init__(params)
        self.take_profit_points = float(self.params.get("take_profit_points", 10.0))
        self.stop_at_signal_extreme = bool(self.params.get("stop_at_signal_extreme", True))
        self.stop_points = float(self.params.get("stop_points", 5.0) or 0.0)
        self.stop_buffer_ticks = float(self.params.get("stop_buffer_ticks", 0.0) or 0.0)
        self.max_hold_bars = int(self.params.get("max_hold_bars", 0) or 0)

    def plan(self, signal: Signal, entry_price: float, spec: InstrumentSpec) -> ExitPlan:
        buf = self.stop_buffer_ticks * spec.price_tick
        is_long = signal.side is Side.LONG
        min_gap = spec.price_tick  # 至少 1 个 tick 间距（防止 stop==entry 立即触发）

        # 止损基准：默认信号K线极值（结构止损）；关掉则改固定点数，方便 A/B 对比
        if self.stop_at_signal_extreme:
            base_stop = signal.low if is_long else signal.high
        else:
            base_stop = entry_price - self.stop_points if is_long \
                else entry_price + self.stop_points

        if is_long:
            raw_stop = base_stop - buf
            raw_tp = entry_price + self.take_profit_points
            stop = spec.round_price(raw_stop, "up")     # 止损往上靠 → 更容易触发（保守）
            tp = spec.round_price(raw_tp, "down")       # 止盈往下靠 → 更晚更少（保守）
        else:
            raw_stop = base_stop + buf
            raw_tp = entry_price - self.take_profit_points
            stop = spec.round_price(raw_stop, "down")
            tp = spec.round_price(raw_tp, "up")

        # P2 修复：保证 stop 严格在 entry 的"不利侧"且至少 1 tick 间距。
        # 历史场景：信号较老（chan.py SSE 推陈旧信号），行情已下跌，
        # 限价让价后 entry < signal.low。如果还把 stop 设在 signal.low 上方，
        # 就成了"开仓即触发止盈"的反向单——逻辑完全错乱。
        # 修正策略：buy 止损必须在 entry 下方；short 止损必须在 entry 上方。
        if is_long:
            if stop is not None and stop >= entry_price - min_gap:
                # 信号极值已不可信（< entry），改用 entry 下方固定距离止损
                stop = spec.round_price(entry_price - max(self.stop_points, min_gap), "down")
        else:
            if stop is not None and stop <= entry_price + min_gap:
                stop = spec.round_price(entry_price + max(self.stop_points, min_gap), "up")

        return ExitPlan(name=self.name, stop_price=stop, tp_price=tp,
                        params=dict(self.params))

    def check(self, position: Position, bar: Bar, spec: InstrumentSpec,
              bars_held: int = 0) -> Optional[ExitCheck]:
        plan = position.exit_plan
        stop = plan.stop_price
        tp = plan.tp_price
        is_long = position.side is Side.LONG

        if is_long:
            if stop and bar.low <= stop:
                return ExitCheck("sl", stop)
            if tp is not None and bar.high >= tp:
                return ExitCheck("tp", tp)
        else:
            if stop and bar.high >= stop:
                return ExitCheck("sl", stop)
            if tp is not None and bar.low <= tp:
                return ExitCheck("tp", tp)

        if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
            return ExitCheck("time", bar.close)
        return None


@register_entry
class DefaultEntryPolicy(EntryPolicy):
    name = "DefaultEntryPolicy"

    def __init__(self, params=None):
        super().__init__(params)
        self.reverse = bool(self.params.get("reverse_on_opposite_signal", False))
        self.max_range = float(self.params.get("max_signal_range_points", 0.0) or 0.0)
        self.min_stop_dist = float(self.params.get("min_stop_distance_points", 0.0) or 0.0)
        self.max_stop_dist = float(self.params.get("max_stop_distance_points", 0.0) or 0.0)

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
