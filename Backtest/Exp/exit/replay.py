# -*- coding: utf-8 -*-
"""L1-L3 出场策略的独立重放器（不改仓库代码，纯实验侧）。

用途：给定 (entry_idx, entry_price, R, side, bars)，逐根重放 check() 的判定，
     记录 MFE / MAE / 是否进保本 / 是否进跟踪 / 离场原因。

为什么能精确复现仓库行为（已核对 Trading/Strategy/Exit.py）：
  · Instrument(product=None) ⇒ round_price 原样返回、price_tick 不影响
  · stop_buffer_ticks 默认 0 ⇒ 初始 stop = entry ∓ R
  · 达标判据 = 根内有利极值 best（单调），浮盈 = (best − entry)·sign
  · 触发判据 = 本根 close，严格不等
  · 保本：fav > be_trigger_r×R → stop = entry + be_buffer_r×R
  · 跟踪：fav > win_loss_ratio×R → stop = best − trailing_trigger_r×R
  · 顺序：先抬价、再判触发（当根可离场）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
# 优先级：显式 CHAN_REPO > 向上找仓库根 > 沙盒布局（<work>/wt_latest）
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))     # 共享层（tdx_source / exp_policy …）
sys.path.insert(0, HERE)
os.chdir(REPO)


@dataclass
class ReplayRow:
    i: int
    date: str
    open: float
    high: float
    low: float
    close: float
    best: float          # 至今最好极值（单调）
    fav: float           # (best − entry)·sign
    fav_pct: float       # fav / entry
    fav_r: float         # fav / R
    stop: float
    phase: str           # sl / breakeven / trailing
    exited: bool = False


@dataclass
class ReplayResult:
    entry: float
    r: float
    side: int            # +1 long / -1 short
    rows: List[ReplayRow] = field(default_factory=list)
    mfe: float = 0.0            # 最大有利偏移（点数，按 best）
    mfe_pct: float = 0.0
    mfe_r: float = 0.0
    mae: float = 0.0            # 最大不利偏移（点数，按 low/high）
    mae_pct: float = 0.0
    be_reached: bool = False    # 是否进过保本
    trail_reached: bool = False
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    net_pct: Optional[float] = None
    final_phase: str = "sl"


def replay(bars: List[Dict], entry_idx: int, entry_price: float, r: float,
           side: int, be_trigger_r: float = 1.0, be_buffer_r: float = 0.5,
           win_loss_ratio: float = 2.0, trailing_trigger_r: float = 1.0,
           # ── 实验开关：附加的「百分比保本」触发条件 ──
           pct_be_trigger: Optional[float] = None,
           pct_be_buffer: Optional[float] = None,
           # ── 实验开关：附加的「百分比跟踪」触发条件 ──
           pct_trail_trigger: Optional[float] = None,
           pct_trail_dist: Optional[float] = None,
           max_bars: int = 100000) -> ReplayResult:
    """bars: [{dt, open, high, low, close}, ...]；side: +1 多 / -1 空。"""
    sign = 1 if side > 0 else -1
    entry = float(entry_price)
    R = float(r)
    stop = entry - sign * R
    best = entry
    res = ReplayResult(entry=entry, r=R, side=sign)
    be_stop = entry + sign * be_buffer_r * R if be_trigger_r else None
    pct_be_stop = (entry * (1 + sign * pct_be_buffer)
                   if pct_be_trigger is not None else None)

    for i in range(entry_idx + 1, min(len(bars), entry_idx + 1 + max_bars)):
        b = bars[i]
        c, h, l = float(b["close"]), float(b["high"]), float(b["low"])
        ext = h if sign > 0 else l
        best = max(best, ext) if sign > 0 else min(best, ext)
        fav = (best - entry) * sign
        fav_pct = fav / entry if entry else 0.0
        new_stop = stop
        phase = "sl"

        # L3 保本（R 口径）
        if be_trigger_r and be_trigger_r > 0 and fav > be_trigger_r * R:
            cand = entry + sign * be_buffer_r * R
            if (sign > 0 and cand > new_stop) or (sign < 0 and cand < new_stop):
                new_stop = cand
                phase = "breakeven"
        # L3 保本（百分比口径，实验新增）
        if pct_be_trigger is not None and fav_pct > pct_be_trigger:
            cand = entry + sign * (pct_be_buffer or 0.0) * entry
            if (sign > 0 and cand > new_stop) or (sign < 0 and cand < new_stop):
                new_stop = cand
                phase = "breakeven"
        # L3 跟踪（R 口径）
        tracking = win_loss_ratio > 0 and fav > win_loss_ratio * R
        if tracking:
            cand = best - sign * trailing_trigger_r * R
            if (sign > 0 and cand > new_stop) or (sign < 0 and cand < new_stop):
                new_stop = cand
                phase = "trailing"
        # L3 跟踪（百分比口径，实验新增）
        if pct_trail_trigger is not None and fav_pct > pct_trail_trigger:
            cand = best - sign * (pct_trail_dist or 0.0) * best
            if (sign > 0 and cand > new_stop) or (sign < 0 and cand < new_stop):
                new_stop = cand
                phase = "trailing"

        if new_stop != stop:
            stop = new_stop
        if phase == "breakeven":
            res.be_reached = True
        if phase == "trailing":
            res.trail_reached = True
        res.final_phase = phase if phase != "sl" else res.final_phase

        res.mfe = max(res.mfe, fav)
        adv = (c - entry) * sign
        if adv < res.mae:
            res.mae = adv

        exited = (c < stop) if sign > 0 else (c > stop)
        res.rows.append(ReplayRow(i, str(b.get("dt", ""))[:10], float(b["open"]),
                                  h, l, c, best, fav, fav_pct,
                                  (fav / R) if R else 0.0, stop, phase, exited))
        if exited:
            res.exit_idx = i
            res.exit_price = c
            res.exit_reason = phase
            res.r_multiple = (c - entry) * sign / R if R else None
            res.net_pct = (c - entry) * sign / entry if entry else None
            res.mfe_pct = res.mfe / entry if entry else 0.0
            res.mfe_r = res.mfe / R if R else 0.0
            res.mae_pct = res.mae / entry if entry else 0.0
            return res

    res.mfe_pct = res.mfe / entry if entry else 0.0
    res.mfe_r = res.mfe / R if R else 0.0
    res.mae_pct = res.mae / entry if entry else 0.0
    return res
