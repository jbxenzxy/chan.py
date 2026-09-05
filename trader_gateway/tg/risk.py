# -*- coding: utf-8 -*-
"""
风控闸门
========
与策略解耦：策略回答"要不要做"，风控回答"让不让你做"。
全部参数来自配置，改风控不需要碰代码。

当前闸门
    ① 交易时段（默认 IF：09:30-11:30 / 13:00-15:00，与商品期货不同，别照抄）
    ② 尾盘不开新仓（默认 14:50 之后）
    ③ 单笔/单日手数上限
    ④ 单日最大往返笔数
    ⑤ 单日最大净亏损（触达后当日停止开仓，已在持仓仍按原计划出场）
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .config import RiskConfig
from .symbols import InstrumentSpec
from .types import Side

DEFAULT_DAY_STATS: Dict[str, Any] = {"day": "", "trades": 0, "net_points": 0.0,
                                     "wins": 0, "losses": 0}


class RiskGate:
    def __init__(self, cfg: RiskConfig, spec: InstrumentSpec):
        self.cfg = cfg
        self.spec = spec
        self.day_stats: Dict[str, Any] = dict(DEFAULT_DAY_STATS)

    def roll_day(self, day: str) -> bool:
        """换日重置统计。返回 True 表示确实换日了。"""
        if self.day_stats.get("day") == day:
            return False
        self.day_stats = dict(DEFAULT_DAY_STATS)
        self.day_stats["day"] = day
        return True

    def check_open(self, side: Side, volume: int, bar_date: str,
                   max_volume: Optional[int] = None) -> Tuple[bool, str]:
        """开仓前检查。返回 (是否放行, 原因)。

        max_volume: 手数上限。省略时用 RiskConfig.max_volume。
            开启仓位管理（sizing）后由引擎传入 sizer 的有效上限 —— 否则会出现
            「sizing 算出 4 手、风控却按 risk.max_volume=1 拦截」的死角。
            sizing 关闭时 sizer.max_volume 就等于 risk.max_volume，行为不变。
        """
        cfg = self.cfg
        if cfg.enforce_session and not self.spec.in_session(bar_date):
            return False, "非交易时段"
        if cfg.no_open_after:
            parts = (bar_date or "").split()
            if len(parts) >= 2 and parts[1][:5] >= cfg.no_open_after:
                return False, "已过尾盘开仓截止时间 {}".format(cfg.no_open_after)
        if volume <= 0:
            return False, "手数非法"
        limit = int(cfg.max_volume if max_volume is None else max_volume)
        if volume > limit:
            return False, "单笔手数 {} 超过上限 {}".format(volume, limit)
        if self.day_stats.get("trades", 0) >= cfg.max_trades_per_day:
            return False, "当日成交笔数已达上限 {}".format(cfg.max_trades_per_day)
        if cfg.block_on_daily_loss:
            limit = cfg.max_daily_loss_points
            if limit > 0 and self.day_stats.get("net_points", 0.0) <= -abs(limit):
                return False, "当日净亏 {:.2f} 点已达上限 {:.2f}".format(
                    self.day_stats["net_points"], limit)
        return True, "ok"

    def on_trade_closed(self, net_points: float, volume: int = 1) -> None:
        """平仓后累计当日统计。"""
        self.day_stats["trades"] = self.day_stats.get("trades", 0) + 1
        self.day_stats["net_points"] = self.day_stats.get("net_points", 0.0) + net_points * volume
        if net_points >= 0:
            self.day_stats["wins"] = self.day_stats.get("wins", 0) + 1
        else:
            self.day_stats["losses"] = self.day_stats.get("losses", 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        return {"cfg": {"max_volume": self.cfg.max_volume,
                        "max_trades_per_day": self.cfg.max_trades_per_day,
                        "max_daily_loss_points": self.cfg.max_daily_loss_points,
                        "enforce_session": self.cfg.enforce_session,
                        "no_open_after": self.cfg.no_open_after},
                "day": dict(self.day_stats)}

    def restore(self, snap: Optional[Dict[str, Any]]) -> None:
        if isinstance(snap, dict) and isinstance(snap.get("day"), dict):
            self.day_stats = dict(snap["day"])
