# -*- coding: utf-8 -*-
"""
标准分层组合出场策略（LayeredExitPolicy）
========================================
把"止盈止损"拆成四层、纵向叠加（非并列），每层独立可开关：

    L1  R 倍数定基线   —— 初始止损 = 1R，止盈 = r_multiple_tp × R（默认 1:2）
    L2  波动率(ATR)定宽窄 —— R = atr_sl_multiple × ATR，行情宽则宽、窄则窄
    L3  移动/保本锁利  —— 浮盈 ≥ breakeven_trigger_r·R 抬至保本；
                          浮盈 ≥ trailing_trigger_r·R 启动 ATR 跟踪止损
    L4  时间/收盘兜底  —— 持仓 ≥ max_hold_bars 强制平仓；或到点(session_end_hhmm)强平

设计要点
    ① 四层共用同一个 R：L1 算出 R，L2 只改 R 的"宽度"，L3 的触发阈值用 R 表达。
       这样"风险预算"一条线串到底，不会各层各算一套。
    ② ATR 需要历史 K 线：engine 每根 bar（无论是否持仓）都会调 on_bar() 钩子，
       策略在这里维护一个 rings buffer，所以开仓瞬间、重启后几根内 ATR 就有值；
       拿不到 ATR 时（首根/重连初期）自动回退到 L1 固定/结构基线。
    ③ 跟踪用的"最优极值"随 ExitPlan.params 落盘，重启不丢 trailing 状态。
    ④ 保守约定沿用 DefaultExitPolicy：同根 K 线同时触止盈止损→按止损计；
       初始止损朝"易触发"方向取整，止盈朝"难触发"方向取整。
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from ..symbols import InstrumentSpec
from ..types import Bar, ExitPlan, Position, Signal, Side
from .base import ExitCheck, ExitPolicy, register_exit


@register_exit
class LayeredExitPolicy(ExitPolicy):
    name = "LayeredExitPolicy"

    # ---------- 参数 ----------
    def __init__(self, params=None):
        super().__init__(params)
        # L1 R 倍数定基线
        self.initial_risk_points = float(self.params.get("initial_risk_points", 10.0))
        self.stop_at_signal_extreme = bool(self.params.get("stop_at_signal_extreme", True))
        self.stop_buffer_ticks = float(self.params.get("stop_buffer_ticks", 0.0) or 0.0)
        self.r_multiple_tp = float(self.params.get("r_multiple_tp", 2.0))
        self.min_r_points = float(self.params.get("min_r_points", 2.0))
        # L2 波动率(ATR)定宽窄
        self.use_atr = bool(self.params.get("use_atr", True))
        self.atr_period = int(self.params.get("atr_period", 14))
        self.atr_sl_multiple = float(self.params.get("atr_sl_multiple", 2.0))
        # L3 移动/保本锁利
        self.use_trailing = bool(self.params.get("use_trailing", True))
        self.breakeven_trigger_r = float(self.params.get("breakeven_trigger_r", 1.0))
        self.breakeven_buffer_ticks = float(self.params.get("breakeven_buffer_ticks", 0.0) or 0.0)
        self.trailing_trigger_r = float(self.params.get("trailing_trigger_r", 2.0))
        self.trailing_atr_multiple = float(self.params.get("trailing_atr_multiple", 1.5))
        self.trailing_distance_points = float(self.params.get("trailing_distance_points", 0.0) or 0.0)
        # L4 时间/收盘兜底
        self.max_hold_bars = int(self.params.get("max_hold_bars", 0) or 0)
        self.session_end_hhmm = str(self.params.get("session_end_hhmm", "14:55") or "")

        # ATR 历史缓冲（on_bar 维护，平着也收）
        self._bars: "deque" = deque(maxlen=self.atr_period + 2)

    # ---------- 钩子：每根 K 线（无论持仓与否）都会调用 ----------
    def on_bar(self, bar: Bar, spec: InstrumentSpec) -> None:
        self._bars.append(bar)

    # ---------- ATR ----------
    def _atr(self) -> Optional[float]:
        if len(self._bars) < self.atr_period + 1:
            return None
        bars = list(self._bars)
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < self.atr_period:
            return None
        return sum(trs[-self.atr_period:]) / self.atr_period

    # ---------- 时间解析：从 "2026-09-01 14:55" 取 "14:55" ----------
    @staticmethod
    def _bar_time(bar: Bar) -> str:
        s = bar.date
        if " " in s:
            s = s.split(" ", 1)[1]
        return s[:5]

    # ---------- R 计算（L1 + L2） ----------
    def _initial_r(self, signal, entry_price: float, spec: InstrumentSpec) -> float:
        """初始风险距离 R。use_atr 且有 ATR 时用 ATR 自适应宽度，否则回退 L1 基线。"""
        if self.use_atr:
            atr = self._atr()
            if atr:
                return max(self.atr_sl_multiple * atr, self.min_r_points)
        # L1 回退：结构止损（信号极值）或固定点数
        is_long = signal.side is Side.LONG
        if self.stop_at_signal_extreme:
            ext = signal.low if is_long else signal.high
            base = abs(entry_price - ext)
        else:
            base = self.initial_risk_points
        return max(base, self.min_r_points)

    # ---------- 开仓时生成出场计划 ----------
    def plan(self, signal: Signal, entry_price: float, spec: InstrumentSpec) -> ExitPlan:
        is_long = signal.side is Side.LONG
        min_gap = spec.price_tick
        R = self._initial_r(signal, entry_price, spec)
        stop_dist = R
        tp_dist = self.r_multiple_tp * R

        if is_long:
            raw_stop = entry_price - stop_dist - self.stop_buffer_ticks * spec.price_tick
            raw_tp = entry_price + tp_dist
            stop = spec.round_price(raw_stop, "up")      # 易触发（保守）
            tp = spec.round_price(raw_tp, "down")        # 难触发（保守）
        else:
            raw_stop = entry_price + stop_dist + self.stop_buffer_ticks * spec.price_tick
            raw_tp = entry_price - tp_dist
            stop = spec.round_price(raw_stop, "down")
            tp = spec.round_price(raw_tp, "up")

        # P2 防护：止损必须严格在 entry 的"不利侧"且至少 1 tick 间距，
        # 否则遇到陈旧信号（行情已走远）会变成"开仓即触发止盈"的反向单。
        if is_long:
            if stop is not None and stop >= entry_price - min_gap:
                stop = spec.round_price(entry_price - max(stop_dist, min_gap), "down")
        else:
            if stop is not None and stop <= entry_price + min_gap:
                stop = spec.round_price(entry_price + max(stop_dist, min_gap), "up")

        params = dict(self.params)
        params["R"] = R
        params["_trail_best"] = entry_price  # 跟踪极值初值 = 入场价
        return ExitPlan(name=self.name, stop_price=stop, tp_price=tp, params=params)

    # ---------- 每根 bar 闭合后判定 ----------
    def check(self, position: Position, bar: Bar, spec: InstrumentSpec,
              bars_held: int = 0) -> Optional[ExitCheck]:
        plan = position.exit_plan
        stop = plan.stop_price
        tp = plan.tp_price
        is_long = position.side is Side.LONG
        entry = position.entry_price
        R = float(plan.params.get("R", self.initial_risk_points))
        atr = self._atr()

        # ① 硬出场：同根 K 线同时触止盈止损 → 按止损计（悲观）
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

        # ④ L4 时间/收盘兜底（硬上限，优先于跟踪）
        if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
            return ExitCheck("time", bar.close)
        if self.session_end_hhmm and self._bar_time(bar) >= self.session_end_hhmm:
            return ExitCheck("eod_time", bar.close)

        # ③ L3 移动/保本锁利（只更新计划、不登场）
        if self.use_trailing:
            best = float(plan.params.get("_trail_best", entry))
            best = max(best, bar.high) if is_long else min(best, bar.low)
            fav_profit = position.pnl_points(bar.close)  # (close-entry)·sign
            new_stop = stop

            # 保本：浮盈 ≥ breakeven_trigger_r·R → 止损抬至保本
            if self.breakeven_trigger_r > 0 and fav_profit >= self.breakeven_trigger_r * R:
                be = (entry + self.breakeven_buffer_ticks * spec.price_tick) if is_long \
                    else (entry - self.breakeven_buffer_ticks * spec.price_tick)
                be = spec.round_price(be, "up" if is_long else "down")
                if (is_long and be > new_stop) or (not is_long and be < new_stop):
                    new_stop = be

            # 跟踪：浮盈 ≥ trailing_trigger_r·R → ATR 跟踪止损（只朝有利方向移动）
            if self.trailing_trigger_r > 0 and fav_profit >= self.trailing_trigger_r * R:
                trail_dist = (self.trailing_atr_multiple * atr) if (atr and self.trailing_atr_multiple > 0) \
                    else self.trailing_distance_points
                if trail_dist and trail_dist > 0:
                    tgt = (best - trail_dist) if is_long else (best + trail_dist)
                    tgt = spec.round_price(tgt, "up" if is_long else "down")
                    if (is_long and tgt > new_stop) or (not is_long and tgt < new_stop):
                        new_stop = tgt

            if new_stop != stop:
                params = dict(plan.params)
                params["_trail_best"] = best
                return ExitCheck("trailing", 0.0, only_update=True,
                                plan=ExitPlan(self.name, new_stop, tp, params))
        return None
