# -*- coding: utf-8 -*-
"""
出场策略合集（Exit.py）
=======================
集中全部出场策略实现，可插拔（经 Base.py 注册表按 name 构造）：

    DefaultExitPolicy    默认止盈/止损/时间（用户当前规则）
    LayeredExitPolicy    标准分层组合出场 L1-L4

三个刻意保留的保守设定（Default 沿用）
    ① 同根 K 线同时触及止盈与止损 → 按止损计（不猜盘中先后顺序）
    ② 价格对齐一律往"对自己不利"的方向取整（止损更易触发、止盈更晚更少）
    ③ 出场计划里带上参数快照，落盘后可做事后参数敏感性分析
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from ..Infra.Config import DEFAULT_CONFIG
from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Bar, ExitPlan, Position, Side, Signal
from .Base import ExitCheck, ExitPolicy, register_exit


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


# T4（2026-09-05）：参数默认值单一事实源 —— 无参构造的默认值一律回落
# config.py DEFAULT_CONFIG["exit_policy"]["params"]，不在策略里再养一套数字。
_DEF_EXIT_PARAMS: dict = dict(DEFAULT_CONFIG["exit_policy"]["params"])


@register_exit
class LayeredExitPolicy(ExitPolicy):
    name = "LayeredExitPolicy"

    # ---------- 参数 ----------
    def __init__(self, params=None):
        super().__init__(params)
        # L1 R 倍数定基线
        self.stop_at_signal_extreme = bool(self.params.get("stop_at_signal_extreme", _DEF_EXIT_PARAMS["stop_at_signal_extreme"]))
        self.stop_buffer_ticks = float(self.params.get("stop_buffer_ticks", _DEF_EXIT_PARAMS["stop_buffer_ticks"]) or 0.0)
        self.r_multiple_tp = float(self.params.get("r_multiple_tp", _DEF_EXIT_PARAMS["r_multiple_tp"]))
        self.min_r_points = float(self.params.get("min_r_points", _DEF_EXIT_PARAMS["min_r_points"]))
        # L2 波动率(ATR)定宽窄
        self.use_atr = bool(self.params.get("use_atr", _DEF_EXIT_PARAMS["use_atr"]))
        self.atr_period = int(self.params.get("atr_period", _DEF_EXIT_PARAMS["atr_period"]))
        self.atr_sl_multiple = float(self.params.get("atr_sl_multiple", _DEF_EXIT_PARAMS["atr_sl_multiple"]))
        # L3 移动/保本锁利
        self.use_trailing = bool(self.params.get("use_trailing", _DEF_EXIT_PARAMS["use_trailing"]))
        self.breakeven_trigger_r = float(self.params.get("breakeven_trigger_r", _DEF_EXIT_PARAMS["breakeven_trigger_r"]))
        self.breakeven_buffer_ticks = float(self.params.get("breakeven_buffer_ticks", _DEF_EXIT_PARAMS["breakeven_buffer_ticks"]) or 0.0)
        self.trailing_trigger_r = float(self.params.get("trailing_trigger_r", _DEF_EXIT_PARAMS["trailing_trigger_r"]))
        self.trailing_atr_multiple = float(self.params.get("trailing_atr_multiple", _DEF_EXIT_PARAMS["trailing_atr_multiple"]))
        self.trailing_distance_points = float(self.params.get("trailing_distance_points", _DEF_EXIT_PARAMS["trailing_distance_points"]) or 0.0)
        # L4 时间/收盘兜底
        self.max_hold_bars = int(self.params.get("max_hold_bars", _DEF_EXIT_PARAMS["max_hold_bars"]) or 0)
        self.session_end_hhmm = str(self.params.get("session_end_hhmm", _DEF_EXIT_PARAMS["session_end_hhmm"]) or "")
        # T3（2026-09-05）：EOD 按"bar 结束时刻"判定所需的 bar 间隔（秒），
        # 由 on_bar 用相邻两根闭合 K 线推断；未知时退回旧口径（起点判定）保底。
        self._bar_secs: Optional[int] = None

        # ATR 历史缓冲（on_bar 维护，平着也收）
        self._bars: "deque" = deque(maxlen=self.atr_period + 2)

    # ---------- 钩子：每根 K 线（无论持仓与否）都会调用 ----------
    def on_bar(self, bar: Bar, spec: InstrumentSpec) -> None:
        prev_ts = self._bars[-1].timestamp if self._bars else 0
        self._bars.append(bar)
        # T3：由相邻两根闭合 K 线推断 bar 间隔（1 分钟 ~ 4 小时视为有效），
        # 供 EOD "bar 结束时刻" 判定使用；跳变（隔夜/休市）不影响——
        # EOD 判定只发生在尾盘连续段，此时相邻间隔就是标准 bar 周期。
        if prev_ts and bar.timestamp > prev_ts:
            secs = int(bar.timestamp - prev_ts)
            if 60 <= secs <= 14400:
                self._bar_secs = secs

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
        """对外暴露当前 ATR（点数）。仓位管理 atr_risk 模式用作止损距离兜底。

        样本不足（on_bar 缓冲未攒够 atr_period+1 根）时返回 None，
        调用方（PositionSizer）会自行回退，不会因此崩。
        """
        return self._atr()

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

        # ④ L4 时间/收盘兜底（硬上限，优先于跟踪）
        if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
            return ExitCheck("time", bar.close)
        if self.session_end_hhmm:
            # T3（2026-09-05）：以 bar 结束时刻 ≥ 阈值判定。bar 闭合即推送，
            # 5m 下 14:50 起点的 bar 在 14:55 到达触发 = 14:55 发单，留足 5 分钟缓冲；
            # 旧口径（bar 起点判定）会让 14:55-15:00 那根在收盘后才触发，发单零缓冲。
            # _bar_secs 未知（仅首根/异常流）时退回起点判定保底，不丢兜底。
            thr = int(self.session_end_hhmm[:2]) * 60 + int(self.session_end_hhmm[3:5])
            s = self._bar_time(bar)
            start_min = int(s[:2]) * 60 + int(s[3:5])
            if self._bar_secs:
                end_min = start_min + max(1, int(round(self._bar_secs / 60)))
                hit = end_min >= thr
            else:
                hit = s >= self.session_end_hhmm
            if hit:
                return ExitCheck("eod_time", bar.close)

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


