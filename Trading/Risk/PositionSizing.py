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

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Config import SizingConfig

_VALID_MODES = ("fixed", "capital_pct", "atr_risk")
_VALID_EQUITY_SRC = ("available", "balance")

# 中金所限价单每次最大下单手数（IF/IH/IC/IM 同，交易所交易细则）。
# 仓位管理开启、sizing.max_volume 未显式配置（0）时的默认截断上限。
_CFFEX_SINGLE_ORDER_MAX = 20


class PositionSizer:
    """按配置把"要不要开"翻译成"开几手"。

    纯函数式（不持有状态、不碰 IO），便于单测。权益由外部（broker.equity()）传入。
    """

    def __init__(self, params: "SizingConfig",
                 spec: Optional[InstrumentSpec] = None,
                 risk_max_volume: int = 2):
        # 严格模式（2026-09-07）：只接受 SizingConfig 配置模型，不接受裸 dict。
        # 目的：拼错/缺失的键在 Trading/Config.py 构造时就抛异常（fail-fast），
        #      这里不再养第二套 `p.get(key, 兜底值)` 默认值。
        if isinstance(params, dict):
            raise TypeError(
                "PositionSizer 只接受 SizingConfig（严格模式不接受裸 dict）："
                "请用 SizingConfig(**d) 构造，缺键/拼错会在配置阶段暴露")
        if params is None:
            raise TypeError("PositionSizer 需要 SizingConfig 参数（传 None 无效）")
        p = params
        self.cfg = p
        self.spec = spec or InstrumentSpec()
        self.enabled = bool(p.enabled)

        mode = str(p.mode or "fixed").strip().lower()
        if mode not in _VALID_MODES:
            raise ValueError(
                "sizing.mode 非法: {!r}（可选: {}）".format(p.mode, _VALID_MODES))
        self.mode = mode

        # fixed_volume=0 表示"沿用 risk.max_volume"
        self.fixed_volume = int(p.fixed_volume or 0)
        self.capital_pct = float(p.capital_pct or 0.0)
        self.risk_per_trade_pct = float(p.risk_per_trade_pct or 0.0)
        self.margin_rate = float(p.margin_rate or 0.0)
        # 资金门槛缓冲：K = 一手保证金 + 名义价值×risk_unit_pct，
        # 由 engine._capital_gate 用于资金闸门（能不能开 / X 上限），这里只透传。
        self.risk_unit_pct = float(p.risk_unit_pct or 0.01)

        # 固定手数（非仓位管理 / fixed 模式的回落值）：
        # fixed_volume 显式 >0 用它，否则回落 risk.max_volume（默认 2）。
        self._fixed_volume = self.fixed_volume if self.fixed_volume > 0 \
            else max(0, int(risk_max_volume or 0))

        # 硬上限（仓位管理算法结果的截断上限）：
        # 未显式配置（0）时默认中金所单笔上限 20；显式配置则用配置值。
        cfg_max = int(p.max_volume or 0)
        self.max_volume = cfg_max if cfg_max > 0 else _CFFEX_SINGLE_ORDER_MAX
        # 下限：算出来小于它时提升到它（默认 1，保证"信号来了就交易"的历史行为）
        self.min_volume = int(p.min_volume or 0)
        # 权益/参数取不到时的回退手数
        self.fallback_volume = int(p.fallback_volume or 0)

        src = str(p.equity_source or "available").strip().lower()
        if src not in _VALID_EQUITY_SRC:
            raise ValueError(
                "sizing.equity_source 非法: {!r}（可选: {}）".format(
                    p.equity_source, _VALID_EQUITY_SRC))
        self.equity_source = src

        # ── 解锁后是否补开今仓 ──
        #   True  = 解锁昨仓后绝不新开今仓（默认，规避金融期货"平今高手续费"）。
        #           例：昨日锁 3 手、今日信号算 5 手 → 只解锁 3 手，缺的 2 手不补开。
        #   False = 解锁后按缺口补开（N - 已解锁手数），把净敞口补到目标手数（需显式关闭）。
        #   无论开关如何，解锁本身照常执行（解锁是减风险动作）。
        self.unlock_no_new_open = bool(p.unlock_no_new_open)

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
            vol = self._fixed_volume
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
            raw = float(self._fixed_volume)
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
            "risk_unit_pct": self.risk_unit_pct,
            "min_volume": self.min_volume,
            "max_volume": self.max_volume,
            "fallback_volume": self.fallback_volume,
            "equity_source": self.equity_source,
            "unlock_no_new_open": self.unlock_no_new_open,
        }


# ════════════════════════════════════════════════════════════════════
# 资金闸门（纯函数，2026-09-07 自 engine._capital_gate 平移，逻辑不变）
# ════════════════════════════════════════════════════════════════════
def capital_gate(sig, *, broker, sizer, spec, initial_cash, last_bar=None):
    """资金闸门（2026-09-06 用户拍板）：
    开仓前判断"账户可用资金"够不够开 1 手；够的话，资金允许开几手 X。

    K = 一手保证金 + 一手名义价值 × risk_unit_pct   （开 1 手的最低门槛）
    X = floor(可用资金 / K)                        （资金允许的最多手数上限）

    返回 (blocked, cap_lots)
      blocked   True = 可用资金连 1 手门槛都不够 → 拒绝入场
      cap_lots  资金允许的最多手数 X；None = 资金未知，不拦。
                sizer 算出的手数超 X 则由调用方截断，保证这笔报单 ≤ X。
    """
    equity = None
    fn = getattr(broker, "equity", None)
    if callable(fn):
        try:
            equity = fn(sizer.equity_source)
        except Exception:
            equity = None
    # dry_run 无真实账户：用 risk.initial_cash 作为虚拟资金（默认 1000 万）
    if equity is None and getattr(broker, "name", "") == "dry_run":
        equity = float(initial_cash or 0.0)

    if equity is None or equity <= 0:
        return False, None

    price = float(sig.price or 0.0)
    if price <= 0 and last_bar is not None:
        price = float(last_bar.close or 0.0)
    if price <= 0:
        return False, None

    per_lot = sizer.per_lot_margin(price)              # 一手保证金
    notional = spec.points_to_cash(price, 1)           # 一手名义价值
    risk_unit = float(getattr(sizer, "risk_unit_pct", 0.01) or 0.01)
    k = per_lot + notional * risk_unit
    if k <= 0:
        return False, None

    if equity < k:
        return True, None                                     # 连 1 手门槛都不够 → 拒开
    cap = int(equity // k)                                    # 资金允许最多 X 手
    return False, cap
