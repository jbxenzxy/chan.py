# -*- coding: utf-8 -*-
"""
出场策略（Exit.py）
====================
2026-09-08 精简：出场只有一个策略 `LayeredExitPolicy`（L1-L3 分层出场），
原可选的 `DefaultExitPolicy`（简单固定点数出场）与 L4 时间/收盘兜底均已删除。

三个刻意保留的保守设定（LayeredExitPolicy）
    ① 同根 K 线同时触及止盈与止损 → 按止损计（不猜盘中先后顺序）
    ② 价格对齐一律往"对自己不利"的方向取整（止损更易触发、止盈更晚更少）
    ③ 出场计划里带上参数快照，落盘后可做事后参数敏感性分析
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from ..Config import ExitConfig
from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Bar, ExitPlan, Position, Side, Signal
from .Base import ExitCheck, ExitPolicy


# 参数默认值单一事实源（2026-09-07 严格模式）：
#   不再从 DEFAULT_CONFIG 抄一份 `_DEF_EXIT_PARAMS` 兜底，直接由 Trading/Config.py
#   的参数模型校验 —— 缺省键用模型字段的默认值，拼错的键（extra="forbid"）立即报错。


class LayeredExitPolicy(ExitPolicy):
    name = "LayeredExitPolicy"

    # ---------- 参数 ----------
    def __init__(self, params=None):
        super().__init__(params)
        p = ExitConfig(**(self.params or {}))
        self.p = p
        # L1 R 倍数定基线
        self.stop_at_signal_extreme = p.stop_at_signal_extreme
        self.stop_buffer_ticks = float(p.stop_buffer_ticks or 0.0)
        self.r_multiple_tp = float(p.r_multiple_tp)
        self.min_r_points = float(p.min_r_points)
        # L2 波动率(ATR)定宽窄
        self.use_atr = p.use_atr
        self.atr_period = int(p.atr_period)
        self.atr_sl_multiple = float(p.atr_sl_multiple)
        # L3 移动/保本锁利
        self.use_trailing = p.use_trailing
        self.breakeven_trigger_r = float(p.breakeven_trigger_r)
        self.breakeven_buffer_ticks = float(p.breakeven_buffer_ticks or 0.0)
        self.trailing_trigger_r = float(p.trailing_trigger_r)
        self.trailing_atr_multiple = float(p.trailing_atr_multiple)
        self.trailing_distance_points = float(p.trailing_distance_points or 0.0)
        # 跨日清空 ATR 缓冲用
        self._last_day: str = ""

        # ATR 历史缓冲（on_bar 维护，平着也收）
        self._bars: "deque" = deque(maxlen=self.atr_period + 2)

    # ---------- 钩子：每根 K 线（无论持仓与否）都会调用 ----------
    def on_bar(self, bar: Bar, spec: InstrumentSpec) -> None:
        # 跨日清空 ATR 缓冲：昨收 → 今开的隔夜跳空会造出一个巨大 TR。
        # 30m 下一天只有 8 根 bar、缓冲要 atr_period+1=15 根，
        # 一个跳空能把近两天的 ATR 都顶高 → 止损/跟踪距离被系统性放大。
        day = (bar.date or "")[:10]
        if self._last_day and day and day != self._last_day:
            self._bars.clear()
        if day:
            self._last_day = day

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

    def current_atr(self) -> Optional[float]:
        """对外暴露当前 ATR（点数）。供外部观测/日志使用。

        （2026-09-08：原 atr_risk 仓位模式随动态定仓删除，ATR 仅保留观测用途。）
        样本不足（on_bar 缓冲未攒够 atr_period+1 根）时返回 None。
        """
        return self._atr()

    # ---------- R 计算（L1 + L2） ----------
    def _initial_r(self, signal, entry_price: float, spec: InstrumentSpec) -> float:
        """初始风险距离 R。use_atr 且有 ATR 时用 ATR 自适应宽度，否则回退 L1 基线。"""
        if self.use_atr:
            atr = self._atr()
            if atr:
                return max(self.atr_sl_multiple * atr, self.min_r_points)
        # L1 回退：结构止损（信号极值）；显式关闭极值止损时用 min_r_points 保底
        is_long = signal.side is Side.LONG
        if self.stop_at_signal_extreme:
            ext = signal.low if is_long else signal.high
            base = abs(entry_price - ext)
        else:
            base = self.min_r_points
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
        # R 快照缺失（旧版本 state.db 恢复的持仓）→ L3 跳过：保本/跟踪是 R 倍数语义，
        #   R 未知时激进触发反而危险；硬止损/止盈/时间兜底均不依赖 R，不受影响
        R = plan.params.get("R")
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

        # ③ L3 移动/保本锁利（只更新计划、不登场）
        if self.use_trailing and R:
            best = float(plan.params.get("_trail_best", entry))
            best = max(best, bar.high) if is_long else min(best, bar.low)
            # fav_profit 用"根内有利极值 best"而非收盘价衡量：
            #   允许 r_multiple_tp 与 trailing_trigger_r 解耦 —— 盘中冲高
            #   （如到 2R）即便收盘回落（如 1.2R），只要有意义浮盈达标仍会
            #   触发保本/跟踪，避免"盘中到过 2R 却因只看收盘而漏检"。
            #   注意 tp 极值判定（①）仍是硬离场，与 L3 不冲突。
            fav_profit = position.pnl_points(best)  # (best-entry)·sign
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


