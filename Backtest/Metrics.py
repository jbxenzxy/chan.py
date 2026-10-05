# -*- coding: utf-8 -*-
"""
指标聚合（Backtest/Metrics.py）
================================
对齐 Trading 统计面板（设计文档 §5.4 / §5.4c）。

★ 胜负三分唯一定义（§5.4c 末，全文档共用）
---------------------------------------------------------------------
    每笔**净收益率 % > 0 = 胜**；`< 0 = 负`；`== 0 = 平`（平计入分母、不计胜、
    也不算亏损）。共用同一胜负集的指标：胜率 / 盈亏比 / 盈利因子 / `by_bsp_type` /
    `by_reason`。

    ⚠ 判据是**净**（扣完双边成本），不是"涨了"、也不是毛 `r_multiple > 0`。
      依据是 `Trading/Infra/TradeStats.py` 与 `Engine.py` 都按 `net_cash > 0/< 0/== 0`
      三分，且有常驻护栏 `Trading/Test/test_trade_stats_formulas.py`。
      股票侧不产出"元"口径（v1.11 定案）⇒ 落到**每笔净收益率 %**
      （`A > 0` ⇒ 与 `net_cash` 同号，判据等价）。

⚠ **两个指标故意用不同口径**（R30）：
    期望 R 倍数用**毛**（不扣成本）= 策略本身的质量，跨标的可比；
    平均净收益率 % 用**净** = 落到口袋的比例。别以为是笔误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .Runner import BtTrade, RunResult


def _mean(xs: List[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _bucket(trades: List[BtTrade], key_fn) -> Dict[str, Dict[str, Any]]:
    """按 `key_fn(trade)` 分组，逐组给出 `n / w / l / e / avg_net_return / expectancy_r`。

    分组只做**披露**（`by_bsp_type` / `by_reason`），不参与主口径 ——
    主口径永远是把所有笔混在一起算（`pooled`，§9.1）。
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        k = key_fn(t)
        g = groups.setdefault(k, {"n": 0, "w": 0, "l": 0, "e": 0,
                                  "net": [], "r": []})
        g["n"] += 1
        nr = t.net_return
        if nr is None:
            continue
        g["net"].append(nr)
        if nr > 0:
            g["w"] += 1
        elif nr < 0:
            g["l"] += 1
        else:
            g["e"] += 1
        if t.r_multiple is not None:
            g["r"].append(t.r_multiple)
    for g in groups.values():
        g["avg_net_return"] = _mean(g.pop("net"))
        g["expectancy_r"] = _mean(g.pop("r"))
    return groups


@dataclass
class Metrics:
    """一只票一轮回测的汇总指标（口径见模块 docstring）。"""
    n: int = 0                       # 已平仓笔数（状态机去重后，不是信号数）
    w: int = 0                       # 盈利笔
    l: int = 0                       # 亏损笔
    e: int = 0                       # 平笔
    u: int = 0                       # 未平仓笔数
    rej: int = 0                     # 拒收信号数
    filtered: int = 0                # 因类型过滤未放行的信号数
    bars_total: int = 0
    win_rate: Optional[float] = None
    profit_loss_ratio: Optional[float] = None
    profit_factor: Optional[float] = None
    avg_net_return: Optional[float] = None     # 等权算术平均（**不许**金额累加）
    expectancy_r: Optional[float] = None       # 毛 R 算术平均
    avg_bars_held: Optional[float] = None
    max_win_r: Optional[float] = None
    max_loss_r: Optional[float] = None
    by_bsp_type: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_reason: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def compute(result: RunResult) -> Metrics:
    """`RunResult` → `Metrics`（只吃**已平仓**笔；未平仓只计数）。"""
    closed = result.closed
    m = Metrics(
        n=len(closed),
        u=len(result.still_open),
        rej=result.signals_rejected,
        filtered=result.signals_filtered,
        bars_total=result.bars_total,
    )
    if not closed:
        return m

    nets = [t.net_return for t in closed if t.net_return is not None]
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    holds = [t.bars_held for t in closed if t.bars_held is not None]

    wins = [x for x in nets if x > 0]
    losses = [-x for x in nets if x < 0]
    m.w = len(wins)
    m.l = len(losses)
    m.e = len(nets) - m.w - m.l

    if nets:
        m.win_rate = m.w / len(nets)                   # 平计入分母、不计胜
        m.avg_net_return = _mean(nets)
    if wins and losses:
        # 盈亏比 = 盈利笔净收益率**均值** ÷ 亏损笔净收益率**均值**（比值的比）
        m.profit_loss_ratio = _mean(wins) / _mean(losses)
    if wins and losses:
        m.profit_factor = sum(wins) / sum(losses)      # 总盈 ÷ 总亏（无量纲）
    m.expectancy_r = _mean(rs)
    m.avg_bars_held = _mean([float(h) for h in holds])
    if rs:
        m.max_win_r = max(rs)
        m.max_loss_r = min(rs)

    m.by_bsp_type = _bucket(closed, lambda t: str(t.bsp_type))
    m.by_reason = _bucket(closed, lambda t: str(t.exit_reason or "-"))
    return m
