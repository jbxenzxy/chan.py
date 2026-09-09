# -*- coding: utf-8 -*-
"""
品种档案（Trading/Infra/ProductProfile.py）
=====================================
本模块是 Trading 侧**所有"合约品种（IF/IH/IC/IM）"参数差异**的唯一事实源。

背景（与周期档案 Infra/PeriodProfile.py 成对出现）
----------------------------------------------------
Step 2.1 引入了「周期档案」PeriodProfile：按 source.freq 把周期敏感参数
差异收口。但参数里还有一类差异**不随周期变化、而随合约品种变化**：

  · `min_r_points`（R 下限）：IF/IH 波动率较低，3.0 点足够兜底；IC/IM 波动
    更大，3.0 点会被极端横盘+极窄分型轻易击穿，需放大到 5.0 点；
  · `r_multiple_tp`（止盈盈亏比）：IF/IH 惯用 1:2；IC/IM 波动大、趋势性弱，
    1:3 的盈亏比更合适；
  · `multiplier`（合约乘数）：IF/IH = 300 元/点，IC/IM = 200 元/点；
  · `breakeven_buffer_ticks`（保本缓冲）：保本出场只保"价差为零"，往返手续费+
    滑点仍会让净收益为负，故保本位 = 入场价 ± 此 tick 数。IF/IH 滑点小取 2 tick，
    IC/IM 波动大、冲击成本高取 3 tick。

这些差异与周期无关（4 个品种在 4 个周期下都应保持各自的 R 下限/盈亏比/乘数/保本缓冲），
因此**不放 PeriodProfile**，而单独成立本模块的 `ProductProfile`。

为什么 Trading 自持一份品种表，而不是 import 主程序
------------------------------------------------------
  与 PeriodProfile 同理：Trading/ 对 chan.py 零侵入、零 import，只通过 HTTP/SSE
  取数。品种乘数/盈亏比这类执行层参数由网关自持，避免把 chan.py 依赖树拖进来。

  代价是两表可能漂移 → 后续可仿照 period_consistency 增加品种对账测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

# ══════════════════════════════════════════════════════════════════
# 品种档案（与 PeriodProfile 平行的"随品种可变参数"归总）
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ProductProfile:
    """一个合约品种的全部品种相关设定。

    覆盖四个「随品种可变」的 flat 字段（详见模块 docstring）：
      min_r_points             R 下限（点数），防极端横盘+极窄分型
      r_multiple_tp            止盈盈亏比（r_multiple_tp × R）
      multiplier               合约乘数（元/点）
      breakeven_buffer_ticks    保本位缓冲 tick（覆盖往返手续费+滑点，真正"不亏钱"）
    """
    product: str
    min_r_points: float
    r_multiple_tp: float
    multiplier: float
    breakeven_buffer_ticks: float = 2.0    # 保本位缓冲 tick（覆盖往返手续费+滑点）
    note: str = ""                              # 调参记录 / 数据来源 / 标定状态

    @property
    def label(self) -> str:
        return self.product


# 4 个品种的档案（中金所股指期货：IF/IH 一组、IC/IM 一组）。
# min_r_points / r_multiple_tp / multiplier / breakeven_buffer_ticks 显式给真值；note 标定状态。
PRODUCT_PROFILES: Dict[str, ProductProfile] = {
    "IF": ProductProfile(
        product="IF", min_r_points=3.0, r_multiple_tp=2.0, multiplier=300.0,
        breakeven_buffer_ticks=2.0,
        note="IF/IH 基线：波动较低，R 下限 3.0 点、盈亏比 1:2、保本缓冲 2 tick"),
    "IH": ProductProfile(
        product="IH", min_r_points=3.0, r_multiple_tp=2.0, multiplier=300.0,
        breakeven_buffer_ticks=2.0,
        note="IF/IH 基线：波动较低，R 下限 3.0 点、盈亏比 1:2、保本缓冲 2 tick"),
    "IC": ProductProfile(
        product="IC", min_r_points=5.0, r_multiple_tp=3.0, multiplier=200.0,
        breakeven_buffer_ticks=3.0,
        note="IC/IM 调整：波动较大，R 下限 5.0 点、盈亏比 1:3、乘数 200 元/点、保本缓冲 3 tick"),
    "IM": ProductProfile(
        product="IM", min_r_points=5.0, r_multiple_tp=3.0, multiplier=200.0,
        breakeven_buffer_ticks=3.0,
        note="IC/IM 调整：波动较大，R 下限 5.0 点、盈亏比 1:3、乘数 200 元/点、保本缓冲 3 tick"),
}


def parse_product(signal_symbol: str) -> str:
    """从缠论分析合约代码提取品种代码。

    例："KQ.m@CFFEX.IF" → "IF"（取最后一个 '.' 之后的片段）。
    无 '.' 或缺失时返回 ""（= 未知品种，不套用任何品种档案）。
    """
    s = str(signal_symbol or "").strip()
    return s.split(".")[-1] if "." in s else ""


def profile_for(signal_symbol: str) -> Optional[ProductProfile]:
    """signal_symbol 对应的品种档案（未知品种返回 None）。"""
    return PRODUCT_PROFILES.get(parse_product(signal_symbol))