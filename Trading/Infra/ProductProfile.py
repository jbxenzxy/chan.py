# -*- coding: utf-8 -*-
"""
品种档案（Trading/Infra/ProductProfile.py）
=====================================
本模块是 Trading 侧**所有"合约品种（IF/IH/IC/IM 期指 + AU/AG/CU 上期所金属 + PTA 郑商所）"参数差异**的唯一事实源。

角色定位（2026-09-14 双轴声明）：本档案按**变异维度（随品种变）**分区，
是领域注册表（凭交易经验标定的代码资产，git 评审 + 对账测试守护），
不是部署配置入口 —— **不按消费层挪入 Trading/Config.py**；
两轴关系见 Config.py 模块 docstring 的「双轴声明」。
App/AppTrader 只 import 本模块纯函数（白名单闸门），不得反向依赖 Config.py。

背景（与周期档案 Infra/PeriodProfile.py 成对出现）
----------------------------------------------------
PeriodProfile 承载周期的时间语义（freq / bar_secs）；参数里另有一类差异
**不随周期变化、而随合约品种变化**：

  · `min_r_points`（R 下限）：IF/IH 波动率较低，3.0 点足够兜底；IC/IM 波动
    更大，3.0 点会被极端横盘+极窄分型轻易击穿，需放大到 5.0 点；
  · `breakeven_buffer_ticks`（保本缓冲）：保本出场只保"价差为零"，往返手续费+
    滑点仍会让净收益为负，故保本位 = 入场价 ± 此 tick 数。IF/IH 滑点小取 2 tick，
    IC/IM 波动大、冲击成本高取 3 tick；
  · `r_multiple_tp`（止盈盈亏比）：IF/IH 惯用 1:2；IC/IM 波动大、趋势性弱，
    1:3 的盈亏比更合适；
  · `price_tick` / `multiplier`（最小变动价位 / 合约乘数）：IF/IH = 0.2 点 / 300 元/点，
    IC/IM = 0.2 点 / 200 元/点 —— 合约事实，实盘以行情为准（详见类 docstring）。

这些差异与周期无关（4 个品种在 4 个周期下都应保持各自的 R 下限/盈亏比/乘数/保本缓冲），
因此**不放 PeriodProfile**，而单独成立本模块的 `ProductProfile`。

为什么 Trading 自持一份品种表，而不是 import 主程序
------------------------------------------------------
  与 PeriodProfile 同理：Trading/ 对 chan.py 零侵入、零 import，只通过 HTTP/SSE
  取数。品种乘数/盈亏比这类执行层参数由网关自持，避免把 chan.py 依赖树拖进来。

  代价是两表可能漂移 → 后续可仿照 period_consistency 增加品种对账测试。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict

# ══════════════════════════════════════════════════════════════════
# 品种档案（与 PeriodProfile 平行的"随品种可变参数"归总）
# ══════════════════════════════════════════════════════════════════
# kw_only（2026-09-14 评审 P2-2）：强制关键字构造。
#   price_tick 是 Phase 8 后加的字段，插在 breakeven_buffer_ticks 与 note 之间；
#   位置参数构造 ProductProfile("IF", 3.0, 2.0, 300.0, 2.0, "note") 会把第 6 个
#   实参（note）静默落进 price_tick —— dataclass 不做类型校验，不报错。
#   加 kw_only=True 后位置构造直接 TypeError，把静默错位变成启动期硬失败。
#   （改动前已核查：全仓 8 处构造全部是关键字参数，故无调用点需要改。）
@dataclass(frozen=True, kw_only=True)
class ProductProfile:
    """一个合约品种的全部品种相关设定。

    字段分两组（2026-09-13 用户定序：**各档案条目按此顺序书写**）——
      【策略标定值（随经验调，放前面）】
      min_r_points             R 下限（点数），防极端横盘+极窄分型
      breakeven_buffer_ticks    保本位缓冲 tick（覆盖往返手续费+滑点，真正"不亏钱"）
      r_multiple_tp            止盈盈亏比（r_multiple_tp × R）
      prefer_lock_over_closetoday  D6 平今开关（Phase 10，默认 True = 锁仓优先）。
                               True：今仓离场走转移 ④ 反向开仓锁仓（现状）；
                               False：今仓离场直接平今（offset=CLOSETODAY），
                               仅当交易所支持平今（SHFE/INE）时生效 ——
                               "平今免收/平今便宜"的品种（如沪金 AU、部分农产品、
                               原油 SC）关掉锁仓可省一次开仓费 + 一次跨日平仓费。
      【合约事实（交易所定，几乎不变；实盘以行情 apply_quote 为准，此处仅离线兜底）】
      price_tick               最小变动价位 —— Phase 8（D20）新增，**仅作离线模式
                               （dry_run/replay）兜底**：实盘按 A′ 必须从行情取
                               （apply_quote），配置值不会被采用。
      multiplier               合约乘数（元/点）

    注：下方**声明顺序**受 dataclass 规则约束（无默认值字段必须在前），
    与上述概念分组不同属有意为之；书写/阅读以各档案条目的实参顺序为准。
    """
    product: str
    min_r_points: float
    r_multiple_tp: float
    multiplier: float
    breakeven_buffer_ticks: float = 2.0    # 保本位缓冲 tick（覆盖往返手续费+滑点）
    price_tick: float = 0.2                # 最小变动价位（离线兜底；中金所四品种均 0.2）
    prefer_lock_over_closetoday: bool = True   # D6 平今开关（Phase 10）：默认锁仓优先
    note: str = ""                              # 调参记录 / 数据来源 / 标定状态

    @property
    def label(self) -> str:
        return self.product

    def exit_overrides(self) -> Dict[str, float]:
        """品种相关的出场参数三件套（Fix A · 2026-09-14 策略参数单源化）。

        min_r_points / r_multiple_tp / breakeven_buffer_ticks **只存在于本档案**
        （D1 拍板：放弃 .env 覆盖能力，调参 = 改档案 = git 评审 + 对账测试守护）。
        Trading/Config.py 的 resolved_exit_params() 是唯一合并点 —— 把本返回值
        合到品种无关的 ExitConfig 上，组装出 LayeredExitPolicy 的完整参数。
        """
        return {
            "min_r_points": self.min_r_points,
            "r_multiple_tp": self.r_multiple_tp,
            "breakeven_buffer_ticks": self.breakeven_buffer_ticks,
        }


# 8 个品种的档案（中金所股指期货 IF/IH/IC/IM + 上期所金属 AU/AG/CU + 郑商所 PTA）。
# 条目实参顺序（2026-09-13 用户定序）：策略标定值在前（min_r_points → breakeven_buffer_ticks
# → r_multiple_tp），合约事实在后（price_tick → multiplier）；note 同序。显式给真值；note 标定状态。
# ⚠️ 手续费（open/close/closetoday_fee_rate）**不在此处** —— 它们随 broker 加收变化，
#   属 InstrumentSpec 配置项，由用户在 instrument 配置里按实际账户填写（交易所基准见各 note）。
#
# ⚠️⚠️ price_tick / multiplier 是**离线兜底值，无自动对账，需人工维护**（2026-09-14 评审 P2-3）
#   —— 半句都不能省的背景：
#     · 实盘（simnow/live）：由 SimNow._apply_instrument_quote 从**真实月份合约行情**
#       原子覆盖这两个字段（InstrumentSpec.apply_quote），本表的取值**在实盘不被采用**；
#     · 离线（dry_run/replay）：没有行情可比，spec 用的就是本表注入的配置值 ——
#       本表过期 = 回测/模拟成交**静默用错规格**（tick 错 → 限价口径错；乘数错 → PnL 错）。
#   原实现里 Engine._check_spec_drift 只在 instrument_verified=True 时才比对（即只在实盘
#   生效），离线路径永远不查 —— 兜底值过期在离线模式完全不可见。现已补齐离线对账：
#   见 Engine._check_spec_drift 的 offline 分支（比对 spec 实际值 vs 本档案值，warn 提示）。
#   维护口径：每次品种合约参数调整（交易所公告换月/改乘数）后，同步改本表并跑
#   Trading/Test/test_p50_review_fixes.py 的对账用例。
PRODUCT_PROFILES: Dict[str, ProductProfile] = {
    "IF": ProductProfile(
        product="IF", min_r_points=3.0, breakeven_buffer_ticks=2.0, r_multiple_tp=2.0,
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IF：波动较低，R 下限 3.0 点；保本缓冲 2 tick；盈亏比 1:2"),
    "IH": ProductProfile(
        product="IH", min_r_points=3.0, breakeven_buffer_ticks=2.0, r_multiple_tp=2.0,
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IH：波动较低，R 下限 3.0 点；保本缓冲 2 tick；盈亏比 1:2"),
    "IC": ProductProfile(
        product="IC", min_r_points=5.0, breakeven_buffer_ticks=3.0, r_multiple_tp=3.0,
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IC：波动较大，R 下限 5.0 点；保本缓冲 3 tick；盈亏比 1:3；乘数 200 元/点"),
    "IM": ProductProfile(
        product="IM", min_r_points=5.0, breakeven_buffer_ticks=3.0, r_multiple_tp=3.0,
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IM：波动较大，R 下限 5.0 点；保本缓冲 3 tick；盈亏比 1:3；乘数 200 元/点"),
    # ── 上期所金属（Tier 1 商品：流动性 + 趋势 + 形态干净，缠论画段体验好）──
    # min_r_points 是 R 下限地板，单位 = 品种报价点数（IF=指数点、商品=元/克·元/kg·元/吨）。
    # 2026-09-13 用户拍板：商品档 min_r_points **暂全部用默认 3 点** —— IF/IH=3、
    #   IC/IM=5 是交易经验标定，商品侧尚无同等经验积累，先用默认值跑，待回测/实盘
    #   积累后再手工调（此前按 tick 数推 10~25 的标定已废弃：tick 粒度是交易所报价
    #   惯例，不是波动尺度）。
    #   手续费（open/close/closetoday_fee_rate）交易所基准：AU 平今免收、开平昨固定约万1(¥10/手)；
    #   AG 开平昨/平今均万0.5；CU 开平昨万0.5、平今万1.0 —— 在 InstrumentSpec 配置里按实际 broker 填写。
    "AU": ProductProfile(
        product="AU", min_r_points=3.0, breakeven_buffer_ticks=2.0, r_multiple_tp=2.0,
        prefer_lock_over_closetoday=False,   # Phase 10（D6）：沪金平今免收 → 今仓离场直接平今
        price_tick=0.02, multiplier=1000.0,
        note="上期所 SHFE 沪金：R 下限暂用默认 3 点(待经验积累后手工调)；趋势强、盈亏比可上探 1:3；"
             "乘数 1000(元/克)、tick 0.02；平今免收(手续费 InstrumentSpec 配)；"
             "prefer_lock_over_closetoday=False(平今免收→今仓直接平今，不走锁仓)"),
    "AG": ProductProfile(
        product="AG", min_r_points=3.0, breakeven_buffer_ticks=2.0, r_multiple_tp=2.0,
        prefer_lock_over_closetoday=False,   # Phase 10（D6）：沪银平今=平昨费率 → 平今不贵于锁仓
        price_tick=1.0, multiplier=15.0,
        note="上期所 SHFE 沪银：R 下限暂用默认 3 点(待经验积累后手工调)；"
             "乘数 15(元/kg)、tick 1；开平昨/平今均万0.5(手续费 InstrumentSpec 配)；"
             "prefer_lock_over_closetoday=False(平今不贵→今仓直接平今，省一次开仓+跨日平)"),
    "CU": ProductProfile(
        product="CU", min_r_points=3.0, breakeven_buffer_ticks=3.0, r_multiple_tp=2.0,
        price_tick=10.0, multiplier=5.0,
        note="上期所 SHFE 沪铜：R 下限暂用默认 3 点(待经验积累后手工调)；"
             "乘数 5(元/吨)、tick 10；平今万1.0/开平昨万0.5(手续费 InstrumentSpec 配)；"
             "平今=锁仓后再平昨的总费、不占便宜 → prefer_lock_over_closetoday 保持默认 True"),
    # ── 郑商所 PTA（Tier 2 能源化工：成交额常年前三、随原油联动趋势明确）──
    # 键名 = 天勤符号末段："KQ.m@CZCE.TA" → parse_product() = "TA"（PTA 是俗名，
    #   符号代码是 TA）。注意：PTA 走 **CZCE 报单语义**（Phase 9）——
    #   exchange="CZCE" 时报单属性 FOK→FAK（InstrumentSpec.effective_order_advanced）、
    #   OPEN 手数钉 1 手（Engine._open_volume），档案只管品种参数、不管报单属性。
    "TA": ProductProfile(
        product="TA", min_r_points=3.0, breakeven_buffer_ticks=2.0, r_multiple_tp=2.0,
        price_tick=2.0, multiplier=5.0,
        note="郑商所 CZCE PTA(精对苯二甲酸)：R 下限暂用默认 3 点(待经验积累后手工调)；"
             "乘数 5(元/吨)、tick 2；"
             "郑商所品种报单走 FAK + OPEN 钉 1 手；"
             "偶发装置/政策消息急拉急跌；手续费固定值以交易所最新公示为准(InstrumentSpec 配)"),
}


def parse_product(signal_symbol: str) -> str:
    """从缠论分析合约代码提取品种代码。

    例："KQ.m@CFFEX.IF" → "IF"（取最后一个 '.' 之后的片段）。
    无 '.' 或缺失时返回 ""（= 未知品种，不套用任何品种档案）。

    大小写归一为**大写**（2026-09-13）：前端别名表（DataAPI/TqSdkAPI.py
    FUTURES_ALIASES）把 SHFE/DCE 品种解析成 tqsdk 惯例的**小写**主连
    （如 AU → "KQ.m@SHFE.au"、RB → "KQ.m@SHFE.rb"），若按原文取末段
    会得到 "au"/"rb"，与档案键（大写）匹配不上——已标定品种反而被当成
    未知品种。档案键统一大写，故在此归一，调用方无需各自 upper()。
    """
    s = str(signal_symbol or "").strip()
    if "." not in s:
        return ""
    return s.split(".")[-1].upper()


# 合约月份后缀：主连（KQ.m@CFFEX.IF）没有；真实月份合约（CFFEX.IF2609）末段是
# 「品种代码 + 3~4 位年月数字」。商品 3 位（au2512 → AU2512 剥 4 位数字 = 2512）、
# 期指 4 位（IF2609）。注意不能无脑剥数字：品种代码本身不含数字（IF/TA/CU…），
# 故 `\d+$` 只吃尾部连续数字是安全的。
_MONTH_SUFFIX_RE = re.compile(r"\d+$")


def parse_product_key(signal_symbol: str) -> str:
    """**档案 / 白名单查询专用**的品种键：在 parse_product 结果上再剥合约月份。

    与 parse_product 的分工（2026-09-14 评审 P1-1）：
      · parse_product   —— 语义 =「取符号末段」，保留月份。
        `"CFFEX.IF2609"` → `"IF2609"`（test_period_profile.py:114 明确断言此行为，
        不能改，改了会把已标定的既有用例打红）。
      · parse_product_key —— 语义 =「查档案用的键」，剥掉月份。
        `"CFFEX.IF2609"` → `"IF"`；`"KQ.m@CFFEX.IF"` → `"IF"`（主连本来就没月份，
        不受影响）。

    为什么必须分两个函数：白名单（Engine._restore / AppTrader / Config 注入）
    拿到的 symbol 可能是**真实月份合约写法**（CLI 直启 `--symbol CFFEX.IF2609`、
    回放、或 trade_symbol 直接当 signal_symbol 用），而档案键是 `"IF"`。
    原实现直接拿 parse_product 的 `"IF2609"` 查档案 → 命中不了 → 抛
    「品种 'IF2609' 不在支持清单」—— 已标定的 IF 被误杀，且报错文案极具误导性
    （会让人以为 IF 没标定）。

    例：
        parse_product_key("CFFEX.IF2609")    -> "IF"
        parse_product_key("SHFE.au2512")     -> "AU"
        parse_product_key("KQ.m@CZCE.TA")    -> "TA"
        parse_product_key("KQ.m@SHFE.rb")    -> "RB"（未标定 → 仍会被白名单拦）
        parse_product_key("")                -> ""
    """
    s = str(signal_symbol or "").strip()
    if "." not in s:
        return ""
    return _MONTH_SUFFIX_RE.sub("", s.split(".")[-1]).upper()


def describe_unknown_product(signal_symbol: str) -> str:
    """未知品种的统一文案（单一事实源，2026-09-14 评审 P2-4）。

    原实现有两份文案（Config 打 WARN + Engine._restore 抛 ValueError），且解析口径
    不一致（一个用末段、一个剥月份），日志里同一件事出现两次、措辞还不一样。
    现在谁要报「品种未标定」都调这里，保证：① 只解析一次口径；② 文案一致。
    """
    raw = str(signal_symbol or "").strip()
    key = parse_product_key(raw)
    # 措辞约束（两条既有契约同时成立，改文案前先看这里）：
    #   · 含连续子串 "拒绝启动交易引擎" —— test_p20_phase_i1 的 [9w3] 断言
    #     （App/AppTrader 前端前置拦截路径）；
    #   · 含连续子串 "禁止启动"        —— test_p47 的 [3] 断言
    #     （Engine._restore 引擎侧权威闸门路径）。
    #   两份旧文案分别是"已拒绝…"/"禁止…"，收敛成一句时必须两个都命中。
    return (
        "品种 {} 不在自动下单支持清单（{}）中：执行参数未标定，"
        "已拒绝启动交易引擎（清单外品种禁止启动）。请更换品种，或在 "
        "Trading/Infra/ProductProfile.py 的 PRODUCT_PROFILES 中"
        "标定后再试。（原始符号={!r}，解析品种键={!r}）"
        .format(key or raw, "/".join(sorted(PRODUCT_PROFILES)), raw, key))


def assert_product_allowed(signal_symbol: str) -> str:
    """品种白名单硬约束的**唯一实现**（2026-09-14 评审 P1-1 + P2-1）。

    返回解析出的品种键；不在 PRODUCT_PROFILES 中则抛 ValueError（文案由
    describe_unknown_product 统一给出）。

    为什么收敛到一处：原先 App/AppTrader.py 与 Engine._restore 各写一遍
    `if _product not in PRODUCT_PROFILES` —— 两处文案不同（"已拒绝" / "禁止"），
    且白名单一旦加档（如按交易所放宽）极易改一处漏一处 → 前端放行、引擎自杀
    的行为分叉。现在：
      · App 侧：try: assert_product_allowed(...) except ValueError → AppError(400)；
      · 引擎侧：_restore 直接调，ValueError 冒泡 → 子进程退出。
    """
    key = parse_product_key(signal_symbol)
    if key not in PRODUCT_PROFILES:
        raise ValueError(describe_unknown_product(signal_symbol))
    return key