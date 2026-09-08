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

from ..Config import DefaultExitParamsConfig, ExitParamsConfig
from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.PeriodProfile import (bar_sec_of_day, eod_triggered,
                                   norm_delta_sec, parse_hhmmss, ts_scale)
from ..Infra.Types import Bar, ExitPlan, Position, Side, Signal
from .Base import ExitCheck, ExitPolicy, register_exit


@register_exit
class DefaultExitPolicy(ExitPolicy):
    name = "DefaultExitPolicy"

    def __init__(self, params=None):
        super().__init__(params)
        p = DefaultExitParamsConfig(**(self.params or {}))
        self.p = p
        self.take_profit_points = float(p.take_profit_points)
        self.stop_at_signal_extreme = p.stop_at_signal_extreme
        self.stop_points = float(p.stop_points or 0.0)
        self.stop_buffer_ticks = float(p.stop_buffer_ticks or 0.0)
        # 根数为主（bar 语义），秒为可选附加顶（默认 0=不启用）
        self.max_hold_bars = int(p.max_hold_bars or 0)
        self.max_hold_seconds = float(p.max_hold_seconds or 0.0)

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
              bars_held: int = 0,
              held_secs: Optional[float] = None) -> Optional[ExitCheck]:
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
        # 可选墙钟上限（默认 0=不启用）：与根数是"或"关系，谁先到谁生效
        if self.max_hold_seconds > 0 and held_secs is not None \
                and held_secs >= self.max_hold_seconds:
            return ExitCheck("time", bar.close)
        return None


# 参数默认值单一事实源（2026-09-07 严格模式）：
#   不再从 DEFAULT_CONFIG 抄一份 `_DEF_EXIT_PARAMS` 兜底，直接由 Trading/Config.py
#   的参数模型校验 —— 缺省键用模型字段的默认值，拼错的键（extra="forbid"）立即报错。


@register_exit
class LayeredExitPolicy(ExitPolicy):
    name = "LayeredExitPolicy"

    # ---------- 参数 ----------
    def __init__(self, params=None):
        super().__init__(params)
        p = ExitParamsConfig(**(self.params or {}))
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
        # L4 时间/收盘兜底
        # 主口径 = max_hold_bars（**K 线根数**，与周期无关，遵 L4 设计文档
        #          "N 根 K 线无进展 → 走"）；
        # 附加顶 = max_hold_seconds（墙钟秒，默认 0=不启用），二者取"或"。
        self.max_hold_bars = int(p.max_hold_bars or 0)
        self.max_hold_seconds = float(p.max_hold_seconds or 0.0)
        self.session_end_hhmm = str(p.session_end_hhmm or "")
        self.eod_lead_bars = int(p.eod_lead_bars or 0)
        # bar_secs 的三级来源（优先级从高到低）：
        #   ① 策略参数显式给（非 0）           —— 单测 / 非标周期
        #   ② 引擎 set_bar_secs 注入（source.freq 推导）—— 生产路径
        #   ③ on_bar 用相邻 bar 推断（单位嗅探）—— 兜底
        # 旧代码只有 ③，且推断时把毫秒当秒 → 四个周期全部推断失败且静默。
        self.bar_secs: Optional[int] = int(p.bar_secs or 0) or None
        self._inferred_bar_secs: Optional[int] = None
        # 跨日清空 ATR 缓冲用
        self._last_day: str = ""

        # ATR 历史缓冲（on_bar 维护，平着也收）
        self._bars: "deque" = deque(maxlen=self.atr_period + 2)

    # ---------- 有效 bar 秒数（三级来源归并） ----------
    @property
    def effective_bar_secs(self) -> Optional[int]:
        """当前生效的 bar 秒数；三级来源都拿不到时返回 None。"""
        if self.bar_secs:
            return self.bar_secs
        injected = int(getattr(self, "_injected_bar_secs", 0) or 0)
        if injected:
            return injected
        return self._inferred_bar_secs

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

        prev_ts = self._bars[-1].timestamp if self._bars else 0
        self._bars.append(bar)
        # 兜底推断：只在没拿到配置/注入值时才用。
        # norm_delta_sec 会嗅探毫秒/秒（阈值 1e5），不再像旧代码那样
        # 把毫秒差值直接当秒去比 60~14400 —— 那会让 4 个周期全部推断失败。
        if prev_ts and bar.timestamp > prev_ts:
            # 用**绝对值**判定单位（毫秒 ~1.7e12 / 秒 ~1.7e9），不用差值阈值：
            # 差值口径在"毫秒源 + 15s 周期"（15000）与"秒源 + 30m 周期"（1800）
            # 之间无法取到一个同时正确的分界。
            secs = (bar.timestamp - prev_ts) / ts_scale(bar.timestamp)
            if 1 <= secs <= 14400:
                self._inferred_bar_secs = int(round(secs))

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
        """【遗留】只取 HH:MM。15s 周期的 date 带秒，这里会丢秒 —— 新代码
        一律改走 `bar_sec_of_day()`（返回当日秒数，秒级精度）。"""
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
              bars_held: int = 0,
              held_secs: Optional[float] = None) -> Optional[ExitCheck]:
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

        # ④ L4 时间兜底（主口径）：max_hold_bars = **K 线根数**，与周期无关。
        #    判断依据是"多少根 bar 没走出来"，不是墙钟时间 —— 计量单位是
        #    结构信息量（一根 bar = 一份证据），所以 30 在任何周期下都是 30 根。
        #    （2026-09-08 更正：此前改成秒制是把 Step 2 标定问题误判成 Step 1
        #     缺陷，会在 15s/1m/30m 上静默改变策略行为，已撤回。）
        if self.max_hold_bars > 0 and bars_held >= self.max_hold_bars:
            return ExitCheck("time", bar.close)
        #    附加顶（可选）：max_hold_seconds 墙钟上限，默认 0=不启用。
        #    用途：粗周期上加一道"绝不过夜/绝不超时"硬顶。
        if self.max_hold_seconds > 0:
            hs = held_secs
            if hs is None:
                bs = self.effective_bar_secs
                hs = float(bars_held * bs) if (bs and bars_held) else None
            if hs is not None and hs >= self.max_hold_seconds:
                return ExitCheck("time", bar.close)

        # ⑤ L4 收盘兜底：统一走 eod_triggered（bar 结束 + lead×bar_secs ≥ 阈值）。
        #    旧逻辑有双重缺陷：
        #      a) 用 `_bar_time()` 取 HH:MM → 15s 的 date 带秒被截掉，最多偏 59 秒；
        #      b) bar 结束时判定 → 30m 最后一根 14:30-15:00 闭合时已收盘，永远平不掉。
        #    新逻辑提前 eod_lead_bars（默认 1）根 bar 判定，四个周期都能平掉。
        if self.session_end_hhmm:
            thr = parse_hhmmss(self.session_end_hhmm)
            start = bar_sec_of_day(bar)
            if thr is not None and start is not None:
                if eod_triggered(start, self.effective_bar_secs, thr,
                                 self.eod_lead_bars):
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


