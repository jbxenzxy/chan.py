# -*- coding: utf-8 -*-
"""实验侧出场策略：LayeredExitPolicy 的等价复刻 + 可注入的额外触发条件。

★ 为什么必须"复刻"而不是"包装"
------------------------------------------------------------------
`Runner.run()` 在函数体内 `from Trading.Strategy.Exit import LayeredExitPolicy`，
因此可以在实验侧把模块属性替换成子类 ⇒ 回测的**信号、状态机、拒收逻辑全部走
真实路径**，只有 check() 换掉。这样 A/B 才包含"出场变早 → 持仓期拒收信号变化"
的二阶效应；用离线重放（replay.py）是拿不到这一层的。

★ 等价性由 `verify_equiv.py` 钉死
------------------------------------------------------------------
在默认配置（所有实验开关 = None）下，本类的 check() 与原实现必须逐笔产出
完全相同的 (exit_date, exit_price, exit_reason)。原文照抄 + 只在"额外条件"
处插入分支，是为了让这个断言有机会成立；任何一处抄错都会让它变红。
"""
from __future__ import annotations

from typing import Optional

from Trading.Strategy.Exit import LayeredExitPolicy
from Trading.Infra.Records import ExitPlan, Side


class ExpExitPolicy(LayeredExitPolicy):
    """实验开关（类属性，实验前统一设置；None = 关闭该条）"""

    # ⓪ 纯百分比替换模式：把 L3 的两层（保本/跟踪）整层从 R 口径换成百分比口径，
    #    原 R 口径的两条判定（breakeven_trigger_r / win_loss_ratio）**整层停用**。
    #    （PCT_ONLY=False 时，下面的 PCT_* 是"附加条件"，与原 R 层并存、取更靠有利侧者。）
    PCT_ONLY: bool = False
    #    边界口径：False = 严格 >（与真实实现一致）；True = ≥（用户需求原文写法）
    PCT_GE: bool = False
    # ① 「百分比保本」：浮盈 / 入场价 > 阈值 ⇒ 保护价抬到 entry×(1+buffer)
    PCT_BE_TRIGGER: Optional[float] = None      # 如 0.10 = 10%
    PCT_BE_BUFFER: float = 0.0                  # 保本落点相对入场的百分比缓冲
    # ② 「百分比跟踪」：浮盈 / 入场价 > 阈值 ⇒ 保护价 = best×(1−dist)
    PCT_TRAIL_TRIGGER: Optional[float] = None
    PCT_TRAIL_DIST: float = 0.10
    # ③ 覆盖原有 R 口径阈值（None = 沿用 params）
    OVR_BE_TRIGGER_R: Optional[float] = None
    OVR_BE_BUFFER_R: Optional[float] = None
    OVR_TRAIL_DIST_R: Optional[float] = None
    OVR_WIN_LOSS_RATIO: Optional[float] = None
    # ④ 时间兜底：持仓超过 N 根且仍处在 sl 层 ⇒ 强制离场
    TIME_STOP_BARS: Optional[int] = None
    # ⑤ 多级阶梯（替代"单一保本层"）。元素 = (触发口径, 触发值, 落点口径, 落点值)
    #    口径 "r" = 相对 R 的倍数；"pct" = 相对入场价的百分比
    #    例：[("r",0.5,"r",0.0), ("r",1.0,"r",0.5)] ⇒ 0.5R 抬到保本、1R 抬到 +0.5R
    #    取**所有已达标台阶中最靠有利侧**的落点；None ⇒ 走内置单一保本层
    STEPS: Optional[list] = None

    def check(self, position, bar, state, bars_held: int = 0):
        from Trading.Strategy.Exit import ExitCheck
        plan = position.exit_plan
        stop = plan.stop_price
        is_long = position.side is Side.LONG
        # 持仓根数自计数：Runner 调 check() 时不传 bars_held（恒 0），
        # 时间兜底只能自己数。key 用 signal_key + entry_price（持仓期内唯一）。
        if self.TIME_STOP_BARS is not None:
            if not hasattr(self, "_hold"):
                self._hold = {}
            _k = (getattr(position, "signal_key", None),
                  float(position.entry_price), int(is_long))
            bars_held = self._hold.get(_k, 0) + 1
            self._hold[_k] = bars_held
        ra = plan.params.get("risk_anchor")
        entry = ra if ra else position.entry_price
        R = plan.params.get("R")
        R = float(R) if R is not None else None
        close = bar.close
        stop_reason = self._phase_reason(plan)
        updated: Optional[ExitPlan] = None

        # PCT_ONLY ⇒ R 口径两层整层停用（触发值置 0，两处 `> 0` 判定自然为假）
        _off = self.PCT_ONLY
        be_trig = 0.0 if _off else (
            self.OVR_BE_TRIGGER_R if self.OVR_BE_TRIGGER_R is not None
            else self.breakeven_trigger_r)
        be_buf = (self.OVR_BE_BUFFER_R if self.OVR_BE_BUFFER_R is not None
                  else self.breakeven_buffer_r)
        trail_d = (self.OVR_TRAIL_DIST_R if self.OVR_TRAIL_DIST_R is not None
                   else self.trailing_trigger_r)
        wlr = 0.0 if _off else (
            self.OVR_WIN_LOSS_RATIO if self.OVR_WIN_LOSS_RATIO is not None
            else self.win_loss_ratio)

        def _hit(v, thr):
            """百分比阈值比较：PCT_GE=False 走严格 >（与真实实现一致）。"""
            return (v >= thr) if self.PCT_GE else (v > thr)

        if R is not None and R > 0:
            best = float(plan.params.get("_trail_best", entry))
            prev_best = best
            _ext = self._fav_extreme(bar, is_long)
            best = max(best, _ext) if is_long else min(best, _ext)
            fav_profit = (best - entry) * position.side.sign
            fav_pct = (fav_profit / entry) if entry else 0.0
            tracking_started = (wlr > 0 and fav_profit > wlr * R)
            new_stop = stop
            r_be_hit = False

            # ── 多级阶梯（实验新增；置位时替代上面的单一保本层）──
            if not _off and self.STEPS:
                for tk, tv, bk, bv in self.STEPS:
                    hit = (fav_profit > tv * R) if tk == "r" else (fav_pct > tv)
                    if not hit:
                        continue
                    r_be_hit = True
                    cand = ((entry + bv * R) if is_long else (entry - bv * R)) \
                        if bk == "r" else \
                        (entry * (1 + bv) if is_long else entry * (1 - bv))
                    cand = state.round_price(cand, "up" if is_long else "down")
                    if (is_long and cand > new_stop) or (not is_long and cand < new_stop):
                        new_stop = cand
            elif not _off:
                # ── 保本：R 口径 ──
                if be_trig and be_trig > 0 and fav_profit > be_trig * R:
                    r_be_hit = True
                    be = (entry + be_buf * R) if is_long else (entry - be_buf * R)
                    be = state.round_price(be, "up" if is_long else "down")
                    if (is_long and be > new_stop) or (not is_long and be < new_stop):
                        new_stop = be
            # ── 保本：百分比口径 ──
            #    触发值取 K 线有利侧极值（做多 high），落点取入场价 ± buffer；
            #    PCT_ONLY=False 时与上面 R 口径并存，取两者更靠有利侧的落点。
            pct_be_hit = (self.PCT_BE_TRIGGER is not None
                          and _hit(fav_pct, self.PCT_BE_TRIGGER))
            if pct_be_hit:
                cand = entry * (1 + (self.PCT_BE_BUFFER if is_long
                                     else -self.PCT_BE_BUFFER))
                cand = state.round_price(cand, "up" if is_long else "down")
                if (is_long and cand > new_stop) or (not is_long and cand < new_stop):
                    new_stop = cand
            # ── 跟踪：R 口径 ──
            if tracking_started:
                tgt = (best - trail_d * R) if is_long else (best + trail_d * R)
                tgt = state.round_price(tgt, "up" if is_long else "down")
                if (is_long and tgt > new_stop) or (not is_long and tgt < new_stop):
                    new_stop = tgt
            # ── 跟踪：百分比口径 ──
            #    触发值取有利侧极值；落点 = 至今最好极值 × (1 ∓ dist)（从最好价回撤）。
            pct_tr_hit = (self.PCT_TRAIL_TRIGGER is not None
                          and _hit(fav_pct, self.PCT_TRAIL_TRIGGER))
            if pct_tr_hit:
                d = self.PCT_TRAIL_DIST
                tgt = (best * (1 - d)) if is_long else (best * (1 + d))
                tgt = state.round_price(tgt, "up" if is_long else "down")
                if (is_long and tgt > new_stop) or (not is_long and tgt < new_stop):
                    new_stop = tgt

            if tracking_started or pct_tr_hit:
                phase = "trailing"
            elif r_be_hit or pct_be_hit:
                phase = "breakeven"
            else:
                phase = ""

            # 回写条件：原实现两条 + 实验新增一条 —— 纯百分比模式下 `best` 是跟踪落点
            #   的锚，必须"每次创新高都落盘"，否则下一根读到陈旧锚会把回撤价算低
            #   （R 口径下无此问题：`tracking_started` 一旦为真即恒真，原条件已覆盖）。
            _persist_best = ((tracking_started and best != prev_best)
                             or (_off and best != prev_best))
            if new_stop != stop or _persist_best:
                params = dict(plan.params)
                params["_trail_best"] = best
                params["_phase"] = phase
                updated = ExitPlan(self.name, new_stop, params=params)
                stop = new_stop
                if phase in ("breakeven", "trailing"):
                    stop_reason = phase

        # ② 触发判定
        if is_long:
            if stop and close < stop:
                return ExitCheck(stop_reason, stop, fill_price=close, plan=updated)
        else:
            if stop and close > stop:
                return ExitCheck(stop_reason, stop, fill_price=close, plan=updated)

        # ④ 时间兜底（实验新增）：只在仍未抬过价（sl 层）时生效
        if (self.TIME_STOP_BARS is not None and updated is None
                and bars_held >= self.TIME_STOP_BARS):
            return ExitCheck("time", stop or close, fill_price=close, plan=None)

        if updated is not None:
            return ExitCheck(stop_reason, 0.0, only_update=True, plan=updated)
        return None


def install():
    """把 Runner 会 import 到的 LayeredExitPolicy 换成实验子类。"""
    import Trading.Strategy.Exit as mod
    mod.LayeredExitPolicy = ExpExitPolicy
    return ExpExitPolicy


def uninstall():
    import Trading.Strategy.Exit as mod
    mod.LayeredExitPolicy = LayeredExitPolicy


def configure(**kw):
    for k, v in kw.items():
        setattr(ExpExitPolicy, k, v)


def reset():
    ExpExitPolicy.PCT_ONLY = False
    ExpExitPolicy.PCT_GE = False
    ExpExitPolicy.PCT_BE_TRIGGER = None
    ExpExitPolicy.PCT_BE_BUFFER = 0.0
    ExpExitPolicy.PCT_TRAIL_TRIGGER = None
    ExpExitPolicy.PCT_TRAIL_DIST = 0.10
    ExpExitPolicy.OVR_BE_TRIGGER_R = None
    ExpExitPolicy.OVR_BE_BUFFER_R = None
    ExpExitPolicy.OVR_TRAIL_DIST_R = None
    ExpExitPolicy.OVR_WIN_LOSS_RATIO = None
    ExpExitPolicy.TIME_STOP_BARS = None
    ExpExitPolicy.STEPS = None
