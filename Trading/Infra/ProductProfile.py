# -*- coding: utf-8 -*-
"""
品种档案（Trading/Infra/ProductProfile.py）
=====================================
本模块是 Trading 侧**所有"合约品种（IF/IH/IC/IM 期指 + AU/AG/CU 上期所金属 + PTA 郑商所）"参数差异**的唯一事实源。

背景（与周期档案 Infra/PeriodProfile.py 成对出现）
----------------------------------------------------
PeriodProfile 承载周期的时间语义（freq / bar_secs）；参数里另有一类差异
**不随周期变化、而随合约品种变化**：

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
from typing import Dict

# ══════════════════════════════════════════════════════════════════
# 品种档案（与 PeriodProfile 平行的"随品种可变参数"归总）
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ProductProfile:
    """一个合约品种的全部品种相关设定。

    覆盖五个「随品种可变」的 flat 字段（详见模块 docstring）：
      min_r_points             R 下限（点数），防极端横盘+极窄分型
      r_multiple_tp            止盈盈亏比（r_multiple_tp × R）
      multiplier               合约乘数（元/点）
      breakeven_buffer_ticks    保本位缓冲 tick（覆盖往返手续费+滑点，真正"不亏钱"）
      price_tick               最小变动价位 —— Phase 8（D20）新增，**仅作离线模式
                               （dry_run/replay）兜底**：实盘按 A′ 必须从行情取
                               （apply_quote），配置值不会被采用。
    """
    product: str
    min_r_points: float
    r_multiple_tp: float
    multiplier: float
    breakeven_buffer_ticks: float = 2.0    # 保本位缓冲 tick（覆盖往返手续费+滑点）
    price_tick: float = 0.2                # 最小变动价位（离线兜底；中金所四品种均 0.2）
    note: str = ""                              # 调参记录 / 数据来源 / 标定状态

    @property
    def label(self) -> str:
        return self.product


# 8 个品种的档案（中金所股指期货 IF/IH/IC/IM + 上期所金属 AU/AG/CU + 郑商所 PTA）。
# min_r_points / r_multiple_tp / multiplier / breakeven_buffer_ticks / price_tick 显式给真值；note 标定状态。
# ⚠️ 手续费（open/close/close_today_fee_rate）**不在此处** —— 它们随 broker 加收变化，
#   属 InstrumentSpec 配置项，由用户在 instrument 配置里按实际账户填写（交易所基准见各 note）。
PRODUCT_PROFILES: Dict[str, ProductProfile] = {
    "IF": ProductProfile(
        product="IF", min_r_points=3.0, r_multiple_tp=2.0, multiplier=300.0,
        breakeven_buffer_ticks=2.0, price_tick=0.2,
        note="IF/IH 基线：波动较低，R 下限 3.0 点、盈亏比 1:2、保本缓冲 2 tick"),
    "IH": ProductProfile(
        product="IH", min_r_points=3.0, r_multiple_tp=2.0, multiplier=300.0,
        breakeven_buffer_ticks=2.0, price_tick=0.2,
        note="IF/IH 基线：波动较低，R 下限 3.0 点、盈亏比 1:2、保本缓冲 2 tick"),
    "IC": ProductProfile(
        product="IC", min_r_points=5.0, r_multiple_tp=3.0, multiplier=200.0,
        breakeven_buffer_ticks=3.0, price_tick=0.2,
        note="IC/IM 调整：波动较大，R 下限 5.0 点、盈亏比 1:3、乘数 200 元/点、保本缓冲 3 tick"),
    "IM": ProductProfile(
        product="IM", min_r_points=5.0, r_multiple_tp=3.0, multiplier=200.0,
        breakeven_buffer_ticks=3.0, price_tick=0.2,
        note="IC/IM 调整：波动较大，R 下限 5.0 点、盈亏比 1:3、乘数 200 元/点、保本缓冲 3 tick"),
    # ── 上期所金属（Tier 1 商品：流动性 + 趋势 + 形态干净，缠论画段体验好）──
    # min_r_points 是 R 下限地板，单位 = 品种价格单位（元/克·元/kg·元/吨），**不是指数点**；
    #   按各品种 price_tick 量级给 ~10~25 tick 地板，防极端横盘+极窄分型把 R 压到无意义。
    #   下列 R 下限 / 盈亏比 / 保本缓冲为**起始标定值，需回测确认**（参照 IF/IH/IC/IM 同款 note 惯例）。
    #   手续费（open/close/close_today_fee_rate）交易所基准：AU 平今免收、开平昨固定约万1(¥10/手)；
    #   AG 开平昨/平今均万0.5；CU 开平昨万0.5、平今万1.0 —— 在 InstrumentSpec 配置里按实际 broker 填写。
    "AU": ProductProfile(
        product="AU", min_r_points=0.5, r_multiple_tp=2.0, multiplier=1000.0,
        breakeven_buffer_ticks=2.0, price_tick=0.02,
        note="SHFE 沪金：乘数 1000(元/克)、tick 0.02；R 下限 0.5 元/克(25 tick)；"
             "趋势强、盈亏比可上探 1:3；平今免收(手续费 InstrumentSpec 配)；需回测标定"),
    "AG": ProductProfile(
        product="AG", min_r_points=20.0, r_multiple_tp=2.0, multiplier=15.0,
        breakeven_buffer_ticks=2.0, price_tick=1.0,
        note="SHFE 沪银：乘数 15(元/kg)、tick 1；R 下限 20 元/kg(20 tick)；"
             "开平昨/平今均万0.5(手续费 InstrumentSpec 配)；需回测标定"),
    "CU": ProductProfile(
        product="CU", min_r_points=100.0, r_multiple_tp=2.0, multiplier=5.0,
        breakeven_buffer_ticks=3.0, price_tick=10.0,
        note="SHFE 沪铜：乘数 5(元/吨)、tick 10；R 下限 100 元/吨(10 tick)；"
             "平今万1.0/开平昨万0.5(手续费 InstrumentSpec 配)；需回测标定"),
    # ── 郑商所 PTA（Tier 2 能源化工：成交额常年前三、随原油联动趋势明确）──
    # 键名 = 天勤符号末段："KQ.m@CZCE.TA" → parse_product() = "TA"（PTA 是俗名，
    #   符号代码是 TA）。注意：PTA 走 **CZCE 报单语义**（Phase 9）——
    #   exchange="CZCE" 时报单属性 FOK→FAK（InstrumentSpec.effective_order_advanced）、
    #   OPEN 手数钉 1 手（Engine._open_volume），档案只管品种参数、不管报单属性。
    "TA": ProductProfile(
        product="TA", min_r_points=30.0, r_multiple_tp=2.0, multiplier=5.0,
        breakeven_buffer_ticks=2.0, price_tick=2.0,
        note="CZCE PTA(精对苯二甲酸)：乘数 5(元/吨)、tick 2；R 下限 30 元/吨(15 tick)；"
             "郑商所品种报单走 FAK + OPEN 钉 1 手；"
             "偶发装置/政策消息急拉急跌，假突破多于金属；"
             "手续费固定值以交易所最新公示为准(InstrumentSpec 配)；需回测标定"),
}


def parse_product(signal_symbol: str) -> str:
    """从缠论分析合约代码提取品种代码。

    例："KQ.m@CFFEX.IF" → "IF"（取最后一个 '.' 之后的片段）。
    无 '.' 或缺失时返回 ""（= 未知品种，不套用任何品种档案）。
    """
    s = str(signal_symbol or "").strip()
    return s.split(".")[-1] if "." in s else ""