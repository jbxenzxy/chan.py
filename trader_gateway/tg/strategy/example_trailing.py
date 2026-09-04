# -*- coding: utf-8 -*-
"""
示例：如何新增一个出场策略（移动止损）
======================================
这个文件存在的唯一目的是演示"换策略"到底要做多少事——答案就三步：

    ① 复制本文件，改类名与 name
    ② 实现 plan() 与 check()
    ③ config.json 里把 exit_policy.name 改成你的类名

**不需要**改 engine.py / brokers / sources 的任何一行。
文件放在 tg/strategy/ 下会被自动扫描注册。

用法
    python run_gateway.py --source replay --replay-dir ./replay_data \\
        --config ./config_trailing.json

或直接改 config.json：
    "exit_policy": {"name": "TrailingExitPolicy",
                    "params": {"take_profit_points": 12.0, "stop_points": 5.0,
                               "trail_start_points": 6.0, "trail_distance_points": 4.0}}

策略逻辑
    初始止损 = 入场价 ± stop_points（固定点数，不用信号极值）
    浮盈达到 trail_start_points 后，止损上移到 (当前收盘价 ∓ trail_distance_points)
    止损只朝有利方向移动，永不回撤
"""
from __future__ import annotations

from typing import Optional

from ..symbols import InstrumentSpec
from ..types import Bar, ExitPlan, Position, Side
from .base import ExitCheck, ExitPolicy, register_exit


@register_exit
class TrailingExitPolicy(ExitPolicy):
    name = "TrailingExitPolicy"

    def __init__(self, params=None):
        super().__init__(params)
        self.take_profit_points = float(self.params.get("take_profit_points", 12.0))
        self.stop_points = float(self.params.get("stop_points", 5.0))
        self.trail_start = float(self.params.get("trail_start_points", 6.0))
        self.trail_dist = float(self.params.get("trail_distance_points", 4.0))

    def plan(self, signal: Signal, entry_price: float, spec: InstrumentSpec) -> ExitPlan:
        is_long = signal.side is Side.LONG
        stop = entry_price - self.stop_points if is_long else entry_price + self.stop_points
        tp = entry_price + self.take_profit_points if is_long \
            else entry_price - self.take_profit_points
        stop = spec.round_price(stop, "up" if is_long else "down")
        tp = spec.round_price(tp, "down" if is_long else "up")
        return ExitPlan(name=self.name, stop_price=stop, tp_price=tp,
                        params=dict(self.params))

    def check(self, position: Position, bar: Bar, spec: InstrumentSpec,
              bars_held: int = 0) -> Optional[ExitCheck]:
        stop = position.exit_plan.stop_price
        tp = position.exit_plan.tp_price
        is_long = position.side is Side.LONG

        # 先判出场：同根 K 线同时触及止盈止损时按止损计（悲观假设）
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

        # 未出场 → 考虑移动止损（只更新计划，不登场）
        if is_long:
            fav = bar.close - position.entry_price
            if fav >= self.trail_start:
                new_stop = spec.round_price(bar.close - self.trail_dist, "up")
                if new_stop > stop:
                    return ExitCheck("trailing", 0.0, only_update=True,
                                    plan=ExitPlan(self.name, new_stop, tp,
                                                  dict(self.params)))
        else:
            fav = position.entry_price - bar.close
            if fav >= self.trail_start:
                new_stop = spec.round_price(bar.close + self.trail_dist, "down")
                if new_stop < stop:
                    return ExitCheck("trailing", 0.0, only_update=True,
                                    plan=ExitPlan(self.name, new_stop, tp,
                                                  dict(self.params)))
        return None
