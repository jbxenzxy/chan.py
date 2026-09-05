# -*- coding: utf-8 -*-
"""
仓位管理（手数定档）
====================
回答"这一笔开几手"，与风控回答"让不让你开"解耦。

设计原则
    ① **默认关闭**：`enabled=False` 时行为与加这个模块之前完全一致
       （固定手数 = sizing.fixed_volume，若为 0 则沿用 risk.max_volume），
       不查账户、不联网、dry_run 也无需任何改动 —— 零风险引入。
    ② **永不放大风险**：三种模式算出的手数都要再过一遍
       `min_volume`（下限提升）与 `max_volume`（硬上限截断），
       最终手数还要交给 `RiskGate.check_open` 再校验一次。
    ③ **取不到数据就保守**：权益 / 止损距离 / ATR 任一取不到时，
       按 `fallback_volume` 回退并写事件，绝不因为查询失败就乱开仓。

三种模式（mode）
    fixed        固定手数。等于关闭，但会走一遍上下限截断逻辑。
    capital_pct  按保证金占比：手数 = 权益 × capital_pct / (现价 × 乘数 × 保证金率)
                 语义 = "我愿意让这笔仓位占用多少比例的资金"。
                 例：权益 100 万、pct=0.5、IF@4550×300、保证金 15%
                     → 50 万 / 20.475 万 = 2.44 → 2 手
    atr_risk     按风险敞口（固定分数法）：手数 = 权益 × risk_pct / (止损距离 × 乘数)
                 语义 = "这笔最多亏掉权益的百分之几"。
                 例：权益 100 万、risk_pct=1%、止损距离 8 点
                     → 1 万 / (8×300=2400) = 4.16 → 4 手
                 止损距离优先用信号自身极值距离（与 LayeredExitPolicy 的 R 一致），
                 取不到才回落 ATR。

术语（与 CTP 对齐，避免歧义）
    开多 (BUY, OPEN) / 开空 (SELL, OPEN) / 平多 (SELL, CLOSE) / 平空 (BUY, CLOSE)
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from .symbols import InstrumentSpec

_VALID_MODES = ("fixed", "capital_pct", "atr_risk")
_VALID_EQUITY_SRC = ("available", "balance")


class PositionSizer:
    """按配置把"要不要开"翻译成"开几手"。

    纯函数式（不持有状态、不碰 IO），便于单测。权益由外部（broker.equity()）传入。
    """

    def __init__(self, params: Optional[Dict[str, Any]] = None,
                 spec: Optional[InstrumentSpec] = None,
                 risk_max_volume: int = 1):
        p = params or {}
        self.spec = spec or InstrumentSpec()
        self.enabled = bool(p.get("enabled", False))

        mode = str(p.get("mode", "fixed") or "fixed").strip().lower()
        self.mode = mode if mode in _VALID_MODES else "fixed"

        # fixed_volume=0 表示"沿用 risk.max_volume"
        self.fixed_volume = int(p.get("fixed_volume", 0) or 0)
        self.capital_pct = float(p.get("capital_pct", 0.0) or 0.0)
        self.risk_per_trade_pct = float(p.get("risk_per_trade_pct", 0.0) or 0.0)
        self.margin_rate = float(p.get("margin_rate", 0.0) or 0.0)

        # 硬上限：0=沿用 risk.max_volume；显式配置则用配置值
        cfg_max = int(p.get("max_volume", 0) or 0)
        self.max_volume = cfg_max if cfg_max > 0 else max(0, int(risk_max_volume or 0))
        # 下限：算出来小于它时提升到它（默认 1，保证"信号来了就交易"的历史行为）
        self.min_volume = int(p.get("min_volume", 1) or 0)
        # 权益/参数取不到时的回退手数
        self.fallback_volume = int(p.get("fallback_volume", 1) or 0)

        src = str(p.get("equity_source", "available") or "available").strip().lower()
        self.equity_source = src if src in _VALID_EQUITY_SRC else "available"

        # ── Phase E3.2（2026-09-05）：每信号批次内开几笔 ──
        #   含义：缠论同 K 线上可能同时给 N 个入仓信号，本参数决定这 N 个信号每个
        #   自己内部再拆出几笔独立 Position（每笔独立 sub_key、独立 exit_plan）。
        #   默认 1 ⇒ 与 E2 完全等价（每信号只开 1 笔），零行为变化。
        #   ≥ 2 ⇒ 实现"加仓金字塔"语义：同根 K 线的同一信号也能分散到 N 个 Position 上，
        #   出场时按 FIFO / 优先最新仓位等规则处理（具体 FIFO 由 E3.3 接入）。
        cfg_batch_raw = p.get("batch_open", 1)
        try:
            cfg_batch = int(cfg_batch_raw)
        except (TypeError, ValueError):
            cfg_batch = 1
        self.batch_open = max(1, cfg_batch)

    # ---------------- 对外主入口 ----------------
    def size(self, *, equity: Optional[float] = None,
             price: float = 0.0,
             stop_distance_points: Optional[float] = None,
             atr_points: Optional[float] = None) -> Tuple[int, str]:
        """返回 (手数, 原因)。手数 ≤ 0 表示不要开仓（调用方应拦下）。

        equity     账户权益（可用资金或总资产，取决于 equity_source）
        price      拟开仓价格（信号 K 线收盘价或最新价）
        stop_dist  这笔的止损距离（点数），atr_risk 模式用
        atr        ATR（点数），stop_dist 取不到时的兜底
        """
        # ① 模块关闭：固定手数，不查账户、不多做任何计算
        if not self.enabled:
            vol = self.fixed_volume if self.fixed_volume > 0 else self.max_volume
            return max(0, int(vol)), "sizing:disabled(fixed)"

        # ② 权益缺失：保守回退，并告知调用方（调用方负责写事件）
        eq = self._clean_float(equity)
        if eq is None or eq <= 0:
            return max(0, self.fallback_volume), "sizing:no_equity(fallback)"

        # ③ 按模式算原始手数
        if self.mode == "capital_pct":
            raw, why = self._by_capital(eq, price)
        elif self.mode == "atr_risk":
            raw, why = self._by_risk(eq, stop_distance_points, atr_points)
        else:
            raw = float(self.fixed_volume if self.fixed_volume > 0 else self.max_volume)
            why = "sizing:fixed"

        # ④ 硬上限截断
        if self.max_volume > 0 and raw > self.max_volume:
            return self.max_volume, why + "+capped(max_volume=%d)" % self.max_volume

        vol = int(math.floor(raw)) if raw > 0 else 0

        # ⑤ 下限提升（算出来 0 手时，若允许最少 1 手则提升）
        if vol < self.min_volume:
            if self.min_volume > 0 and self.max_volume >= self.min_volume:
                return self.min_volume, why + "+raised(min_volume=%d)" % self.min_volume
            return 0, why + "+below_min(no_open)"

        return vol, why

    # ---------------- E3.2 批次拆分 ----------------
    def size_batch(self, *, equity: Optional[float] = None,
                   price: float = 0.0,
                   stop_distance_points: Optional[float] = None,
                   atr_points: Optional[float] = None) -> Tuple[int, int, str]:
        """E3.2：把单笔"开几手"扩展为"每笔几手 × 开几笔"。

        返回 (per_batch, batch_count, reason)
          per_batch    每笔的手数（与 size() 同源）
          batch_count  批次内开几笔（来自 self.batch_open 配置）
          reason       决策原因（带 +batch=N 后缀便于审计）

        调用方拿到 per_batch + batch_count 后：
          · 校验 per_batch > 0 且 batch_count > 0
          · 校验总手数 per_batch * batch_count 是否过 risk / sizer 上限
          · 校验现存同向仓数 + batch_count 是否过 cfg.max_open_positions（截断由引擎）
          · 然后在循环里逐笔调 broker.submit(...) 建独立 Position

        batch_count=1（默认）⇒ 与 size() 完全等价，零行为变化
        """
        per, why = self.size(equity=equity, price=price,
                             stop_distance_points=stop_distance_points,
                             atr_points=atr_points)
        return per, self.batch_open, why + "+batch={}".format(self.batch_open)

    # ---------------- 各模式计算 ----------------
    def _by_capital(self, equity: float, price: float) -> Tuple[float, str]:
        """按保证金占比：手数 = 权益 × pct / 每手保证金。"""
        budget = equity * self.capital_pct
        per_lot_margin = self.per_lot_margin(price)
        if self.capital_pct <= 0 or per_lot_margin <= 0:
            return float(self.fallback_volume), "sizing:capital_pct:bad_param"
        return budget / per_lot_margin, "sizing:capital_pct(%.3f)" % self.capital_pct

    def _by_risk(self, equity: float, stop_dist: Optional[float],
                 atr: Optional[float]) -> Tuple[float, str]:
        """按风险敞口：手数 = 权益 × risk_pct / (止损距离 × 乘数)。"""
        budget = equity * self.risk_per_trade_pct
        per_lot_risk = self.per_lot_risk(stop_dist, atr)
        if self.risk_per_trade_pct <= 0 or per_lot_risk <= 0:
            return float(self.fallback_volume), "sizing:atr_risk:bad_param"
        return budget / per_lot_risk, "sizing:atr_risk(%.4f)" % self.risk_per_trade_pct

    # ---------------- 单手换算 ----------------
    def per_lot_margin(self, price: float) -> float:
        """每手占用保证金（元）= 价格 × 乘数 × 保证金率。"""
        rate = self.margin_rate if self.margin_rate > 0 else 0.15
        px = self._clean_float(price) or 0.0
        if px <= 0:
            return 0.0
        return px * float(self.spec.multiplier) * rate

    def per_lot_risk(self, stop_dist: Optional[float],
                     atr: Optional[float]) -> float:
        """每手风险金额（元）= 止损距离（点）× 乘数。

        止损距离优先用信号极值距离；取不到（None/0/NaN）时回落到 ATR。
        """
        d = self._clean_float(stop_dist)
        if d is None or d <= 0:
            d = self._clean_float(atr)
        if d is None or d <= 0:
            return 0.0
        return d * float(self.spec.multiplier)

    @staticmethod
    def _clean_float(v: Any) -> Optional[float]:
        """把任意输入安全地转成正 float；NaN/inf/负数/非数字一律 None。"""
        if v is None or isinstance(v, bool):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f

    # ---------------- 观测 ----------------
    def describe(self) -> Dict[str, Any]:
        """给日志/事件流用的快照，便于事后复盘"为什么开这么多手"。"""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "fixed_volume": self.fixed_volume,
            "capital_pct": self.capital_pct,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "margin_rate": self.margin_rate,
            "min_volume": self.min_volume,
            "max_volume": self.max_volume,
            "fallback_volume": self.fallback_volume,
            "equity_source": self.equity_source,
            "batch_open": self.batch_open,                    # Phase E3.2
        }
