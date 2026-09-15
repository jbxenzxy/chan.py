# -*- coding: utf-8 -*-
"""
品种档案（Trading/Infra/Product.py）
=====================================
本模块是 Trading 侧**所有"合约品种（IF/IH/IC/IM 期指 + AU/AG/CU 上期所金属 + PTA 郑商所）"参数差异**的唯一事实源。

术语锚点（§附F.2 · 2026-09-15 P-D 落位）
----------------------------------------------------
  · product（品种）= IF / IH / AU / PTA 这类「品种族」，一行档案管全族合约
    （IF2509 / IF2512 / IF2603…换月不换档案）；
  · instrument（合约）= IF2509 这类「一张具体合约」，每张一份
    （运行时对象在 Infra/InstrumentSpec.py 的 Instrument）；
  · symbol 只是代码字符串（signal_symbol / trade_symbol），不是粒度概念。
  · parse_product（保月份，认定到合约）与 parse_product_key（剥月份，查品种档案）
    这对函数的分工就是两个粒度的代码体现。

变更纪律（§5.4 · 取代旧「Profile=标定 / Spec=事实」后缀规则）
----------------------------------------------------
文件名只回答「这是 per-product 粒度的东西」；「该不该走 git 评审」用两个正交手段表达：
  · 标定值（r_multiple_tp / 两档费率）：改 = 改代码资产，走 git 评审 + 对账测试
    （test_product_fee_table.py 快照对账 / test_p50）；
  · 合约事实（price_tick / multiplier）：随交易所/券商公告人工同步
    （实盘由行情 apply_quote 原子覆盖，档案值仅离线兜底）。

角色定位（2026-09-14 双轴声明）：本档案按**变异维度（随品种变）**分区，
是领域注册表（凭交易经验标定的代码资产，git 评审 + 对账测试守护），
不是部署配置入口 —— **不按消费层挪入 Trading/Config.py**；
两轴关系见 Config.py 模块 docstring 的「双轴声明」。
App/AppTrader 只 import 本模块纯函数（白名单闸门），不得反向依赖 Config.py。

背景（与周期档案 Infra/Period.py 成对出现）
----------------------------------------------------
Period 承载周期的时间语义（freq / bar_secs）；参数里另有一类差异
**不随周期变化、而随合约品种变化**：

  · `r_multiple_tp`（止盈盈亏比 / L3 启动阈值）：IF/IH 惯用 1:2（L3 在 2R 启动）；
    IC/IM 波动大、趋势性弱，1:3 的盈亏比更合适（L3 在 3R 启动）；
  · 保本/锁利层的"缓冲"已改为**全局比例** `breakeven_buffer_r`（在 Trading/Config.py
    的 ExitConfig，默认 0.5R，跨品种跨周期统一），不再随品种变 —— 故本档案不再含该字段。
  · `price_tick` / `multiplier`（最小变动价位 / 合约乘数）：IF/IH = 0.2 点 / 300 元/点，
    IC/IM = 0.2 点 / 200 元/点 —— 合约事实，实盘以行情为准（详见类 docstring）。

这些差异与周期无关（各品种在 4 个周期下都应保持各自的盈亏比/乘数），
因此**不放 Period**，而单独成立本模块的 `Product`。

为什么 Trading 自持一份品种表，而不是 import 主程序
------------------------------------------------------
  与 Period 同理：Trading/ 对 chan.py 零侵入、零 import，只通过 HTTP/SSE
  取数。品种乘数/盈亏比这类执行层参数由网关自持，避免把 chan.py 依赖树拖进来。

  代价是两表可能漂移 → 后续可仿照 period_consistency 增加品种对账测试。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


# ══════════════════════════════════════════════════════════════════
# Fee —— 一档费率（P-A 费率静态化 · 2026-09-15，Infra划分治理 §6.3-a）
# ══════════════════════════════════════════════════════════════════
# 交易所基准表就两种计价方式混排，单一 float 表达不了（P-A 之前 PTA=3 元/手、
# 黄金=10 元/手 只能"按当时价折成一个 rate"，价格一变就失真 → 回测净盈亏错）。
# value 存"万分之几"而不是小数：档案里的数必须能直接对上 xlsx 打印出来的字
# （xlsx 写 0.23%%，档案就存 0.23），对账退化成一次肉眼比对；
# 万分比 → 小数的换算只在 cash() 里做一次。
@dataclass(frozen=True)
class Fee:
    """一档费率。kind = "rate"（按成交额比例，value = 万分之几）
    | "per_lot"（每手固定元，value = 元/手）。"""
    kind: str
    value: float

    @staticmethod
    def free() -> "Fee":
        """平今免收：显式，不靠 0 兼表"免"和"未填"。"""
        return Fee("per_lot", 0.0)

    def cash(self, price: float, multiplier: float) -> float:
        """折算为**每手成本（元）** —— 所有费率比较与成本记账都在这一个单位上做
        （per_lot 档在"点数"口径下无法无损表达，统一在元上做）。"""
        if self.kind == "rate":
            return self.value * 1e-4 * price * multiplier
        return self.value

    def describe(self) -> str:
        """横幅 / 报表用的单档文案（与 xlsx 字面同构，可肉眼对账）。"""
        if self.kind == "rate":
            return "万{:.2f}".format(self.value)
        if self.value == 0.0:
            return "免收"
        return "{:.2f} 元/手".format(self.value)


# ══════════════════════════════════════════════════════════════════
# 品种档案（与 Period 平行的"随品种可变参数"归总）
# ══════════════════════════════════════════════════════════════════
# kw_only（2026-09-14 评审 P2-2）：强制关键字构造。
#   price_tick 是 Phase 8 后加的字段；位置参数构造 Product(...) 会把实参
#   静默错位到错误字段 —— dataclass 不做类型校验，不报错。
#   加 kw_only=True 后位置构造直接 TypeError，把静默错位变成启动期硬失败。
#   （改动前已核查：全仓 8 处构造全部是关键字参数，故无调用点需要改。）
@dataclass(frozen=True, kw_only=True)
class Product:
    """一个合约品种的全部品种相关设定。

    字段分三组（2026-09-13 用户定序 + 2026-09-15 P-A 费率归位）——
      【策略标定值（随经验调，放前面）】
      r_multiple_tp            止盈盈亏比（r_multiple_tp × R），同时是 L3 启动阈值（品种级）
                               （保本/锁利缓冲 breakeven_buffer_r 已改为全局比例，见 ExitConfig）
      【费率（P-A 静态化 · 2026-09-15）：真值源 = 券商费率表（Docs/手续费标准-.xlsx）】
      open_fee                 开仓费率。xlsx 只有两个口径：交易（= 开仓 = 平昨）与平今
                               —— 全表 90 条带独立平今行的基准条目里没有一行把"平昨"
                               单独列出来（§6.4），故**平昨档 = 开仓档，不设第三档**。
      closetoday_fee           平今费率；None = 与开仓同档（如 AG 无独立平今行）；
                               Fee.free() = 平今免收（如 AU/TA）。
      exchange                 交易所（CFFEX/SHFE/INE/DCE/CZCE/GFEX）。
                               supports_closetoday（平今指令能力闸门）读它判定。
      【合约事实（交易所定，几乎不变；实盘以行情 apply_quote 为准，此处仅离线兜底）】
      price_tick               最小变动价位 —— Phase 8（D20）新增，**仅作离线模式
                               （dry_run/replay）兜底**：实盘按 A′ 必须从行情取
                               （apply_quote），配置值不会被采用。
      multiplier               合约乘数（元/点）

    注：下方**声明顺序**受 dataclass 规则约束（无默认值字段必须在前），
    与上述概念分组不同属有意为之；书写/阅读以各档案条目的实参顺序为准。
    """
    product: str
    r_multiple_tp: float
    multiplier: float
    open_fee: Fee
    closetoday_fee: Optional[Fee] = None    # None = 同开仓档（xlsx 无独立平今行）
    price_tick: float = 0.2                # 最小变动价位（离线兜底；中金所四品种均 0.2）
    exchange: str = ""                     # 交易所：平今能力闸门 / FOK-FAK 语义的档案侧来源
    note: str = ""                              # 调参记录 / 数据来源 / 标定状态

    # 锁仓路径**比平今路径多出来的**开仓档成交笔数（P-A · §6.5.1 实测，非推理）：
    #   平今路径 = 2 笔：开 A + 平今 A
    #   锁仓路径 = 4 笔：开 A + 反向开 B + 平 A + 平 B
    #              （两笔平仓建仓于前一日，按**平昨**计；xlsx 已证 平昨 ≡ 开仓）
    #   ⇒ 4X > X + Y  ⟺  Y < 3X → 走平今（阈值 = 3 × 开仓费）。
    #   这 4 笔全部由现有转移表自动走出（③ 拆锁 + ⑤ 出场），不需要新路径。
    LOCK_PATH_MULT: int = 3

    @property
    def label(self) -> str:
        return self.product

    @property
    def supports_closetoday(self) -> bool:
        """本品种所在交易所是否支持**平今指令**（CLOSETODAY offset）。

        六家交易所里只有上期所（SHFE）与上期能源（INE）有 CLOSETODAY，
        其余四家（CFFEX/DCE/CZCE/GFEX）传平今会直接报错。
        ⚠️ 引擎路径真正的守卫是 `Instrument.supports_closetoday`
        （运行时有效 exchange，19 处消费点，双处消费不变）；本档案侧属性
        供纯函数派生（prefer_closetoday）与对账测试使用。
        """
        return str(self.exchange or "").strip().upper() in ("SHFE", "INE")

    def fee_pair(self) -> Tuple[Fee, Fee]:
        """返回（开仓费, 平今费）两档。closetoday_fee=None（无独立平今行）→ 回开仓档。

        P-A 用户拍板（2026-09-15）：**不做合约月份覆盖档**（fee_overrides 不实现，
        连字段位也不留）——有覆盖档的品种（AU 6/12 合约 20 元/手、AG 6/12 万0.5）
        一律按第一个基准档计，覆盖档信息只留在 note 里备查。
        `trade_symbol` 参数随覆盖档一并取消，签名只留 ref_price。
        """
        return self.open_fee, (self.open_fee if self.closetoday_fee is None
                               else self.closetoday_fee)

    def prefer_closetoday(self, ref_price: float) -> bool:
        """今仓离场：直接平今还是反向锁仓？**单源派生，无手写布尔**（P-A · §6.5）。

        返回 True = 走 CLOSETODAY 平今；False = 反向开仓锁仓（现状流程）。

        两层判定，缺一不可：
          ① 能力闸门（交易所事实）：无 CLOSETODAY 指令 → 结构上发不出去 → 锁仓。
             ⚠️ 引擎路径（Engine._decide_exit）在调本方法**之前**已按
             `spec.supports_closetoday`（运行时有效 exchange）闸过一次；
             本方法内的闸门读档案 exchange，供纯函数独立使用（费率表对账 /
             启动横幅）时兜底。
          ② 费率会计：比「平今路径 2 笔」vs「锁仓路径 4 笔」→ 阈值 = LOCK_PATH_MULT × 开仓费。

        ⚠️ **不要复用已删除的 `evaluate_closetoday_economy`**：它是 1× 口径
        （只比平今 vs 平昨**单笔**），漏算锁仓路径多出的两笔平仓 → 判据错
        （分歧区间 X < Y < 3X，CU 的 Y=2X 正落在这里）。本方法为 3× 口径新写。

        ⚠️ **"手数 N / 价格自动约掉"是架构不变量，不是数学恒等式**：
        前提 = 一次信号只报一笔（无拆单）+ FOK/FAK 全成全撤（无部分成交）
        → 每笔手数恒等（Engine._open_volume / 转移③⑤ 的 min() 都取等）。
        且本表 8 个品种两档**计价方式恒相同**（全 rate 或全 per_lot），
        比较式两边同乘 price × multiplier 后价格约掉 —— `ref_price` 仅在
        将来出现"混合计价"（一档 rate 一档 per_lot）时才真正参与比较；
        传入当前持仓入场价或最近 bar 收盘价均可。
        若未来引入差异化手数 / 部分成交 / 混合计价，本判定需重新推导。
        """
        # ① 能力闸门：无 CLOSETODAY 指令 → 只能锁仓（早退，不看费率）
        if not self.supports_closetoday:
            return False
        # ② 费率会计：平今路径（NX + NY） vs 锁仓路径（4NX，两笔平仓按平昨 ≡ 开仓档）
        open_fee, ct_fee = self.fee_pair()
        o = open_fee.cash(ref_price, self.multiplier)
        ct = ct_fee.cash(ref_price, self.multiplier)
        # Y < 3X → 平今；相等归锁仓（判据是 4X > X + Y 严格大于才改走平今）
        return ct < self.LOCK_PATH_MULT * o

    def exit_overrides(self) -> Dict[str, float]:
        """品种相关的出场参数（Fix A · 2026-09-14 策略参数单源化）。

        仅 r_multiple_tp **只存在于本档案**（它同时是 L3 启动阈值，品种级；
        D1 拍板：放弃 .env 覆盖能力，调参 = 改档案 = git 评审 + 对账测试守护）。
        min_r_points（R 下限）、breakeven_buffer_ticks 已于 2026-09-14 删除：
        R 改为纯自适应 max(A, 2×ATR)，保本缓冲改为全局比例 breakeven_buffer_r（ExitConfig）。
        Trading/Config.py 的 resolved_exit_params() 是唯一合并点 —— 把本返回值
        合到品种无关的 ExitConfig 上，组装出 LayeredExitPolicy 的完整参数。
        """
        return {
            "r_multiple_tp": self.r_multiple_tp,
        }


# 8 个品种的档案（中金所股指期货 IF/IH/IC/IM + 上期所金属 AU/AG/CU + 郑商所 PTA）。
# 条目实参顺序：策略标定值在前（r_multiple_tp），费率与合约事实在后；
# note 同序。显式给真值；note 标定状态。
#
# ⚠️ 手续费（P-A · 2026-09-15 归位）：费率真值源 = 券商费率表 Docs/手续费标准-.xlsx，
#   **静态档案，启动即确定**（取代已删除的"成交回报反推 / TqSim 查询"两条运行时通道）。
#   数值由解析脚本从 xlsx 派生（Parsing 规则见对账测试 test_product_fee_table.py 的快照注释），
#   平今口径分歧（如 CFFEX 万2.3 vs 代码旧值万3.45 = 万2.3 × 1.5）已按用户拍板"以 xlsx 为准"落值；
#   若实盘成交单显示券商按 1.5 倍加收，改对应品种的 Fee 数值一行即可。
#   对账守护：Trading/Test/test_product_fee_table.py（xlsx 派生快照 vs 本表逐项断言）。
#
# ⚠️ price_tick / multiplier 是**离线兜底值，无自动对账，需人工维护**（2026-09-14 评审 P2-3）
#   —— 半句都不能省的背景：
#     · 实盘（simnow/live）：由 SimNow._apply_instrument_quote 从**真实月份合约行情**
#       原子覆盖这两个字段（`InstrumentState.apply_quote`，Phase 3 起运行时有效值归
#       InstrumentState），本表的取值**在实盘不被采用**；
#     · 离线（dry_run/replay）：没有行情可比，state 的有效值就是本表播种的配置值
#       （Phase 3 起播种发生在启动路径：main._seed_instrument → InstrumentSpec.for_product）
#       —— 本表过期 = 回测/模拟成交**静默用错规格**（tick 错 → 限价口径错；乘数错 → PnL 错）。
#   维护口径：每次品种合约参数调整（交易所公告换月/改乘数）后，同步改本表并跑
#   Trading/Test/test_p50_review_fixes.py 的对账用例。
#
# ⚠️ 覆盖档（合约月份差异化费率）**不做**（P-A 用户拍板 2026-09-15）：AU / AG 的
#   "6、12 合约 & 2607-2610"档只记录在 note 里备查，费率一律按基准档。
PRODUCT_PROFILES: Dict[str, Product] = {
    "IF": Product(
        product="IF", r_multiple_tp=2.0,
        exchange="CFFEX",
        open_fee=Fee("rate", 0.23), closetoday_fee=Fee("rate", 2.3),
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IF：盈亏比 1:2（L3 在 2R 启动）；"
             "费率 xlsx：交易万0.23 / 平今万2.3（另有交割万0.5，本系统不参与交割不消费）；"
             "CFFEX 无平今指令 → 派生恒走锁仓（能力闸门短路）"),
    "IH": Product(
        product="IH", r_multiple_tp=2.0,
        exchange="CFFEX",
        open_fee=Fee("rate", 0.23), closetoday_fee=Fee("rate", 2.3),
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IH：盈亏比 1:2（L3 在 2R 启动）；费率同 IF（交易万0.23/平今万2.3）"),
    "IC": Product(
        product="IC", r_multiple_tp=3.0,
        exchange="CFFEX",
        open_fee=Fee("rate", 0.23), closetoday_fee=Fee("rate", 2.3),
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IC：盈亏比 1:3（L3 在 3R 启动）、乘数 200 元/点；费率同 IF"),
    "IM": Product(
        product="IM", r_multiple_tp=3.0,
        exchange="CFFEX",
        open_fee=Fee("rate", 0.23), closetoday_fee=Fee("rate", 2.3),
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IM：盈亏比 1:3（L3 在 3R 启动）、乘数 200 元/点；费率同 IF"),
    # ── 上期所金属（Tier 1 商品：流动性 + 趋势 + 形态干净，缠论画段体验好）──
    # 商品档盈亏比暂统一 1:2（L3 在 2R 启动），与 IF/IH 一致；IC/IM 因波动大、趋势性弱
    #   用 1:3。R 下限已删除（R = max(A, 2×ATR) 纯自适应），不再有"点数地板"。
    "AU": Product(
        product="AU", r_multiple_tp=2.0,
        exchange="SHFE",
        open_fee=Fee("per_lot", 10.0), closetoday_fee=Fee.free(),
        price_tick=0.02, multiplier=1000.0,
        note="上期所 SHFE 沪金：盈亏比 1:2（L3 在 2R 启动）、趋势强可上探 1:3；"
             "乘数 1000(元/克)、tick 0.02；费率 xlsx：开仓 10 元/手 / 平今免收"
             "（覆盖档：6、12 合约 & 2607-2610 = 20 元/手，本期不消费按基准档）；"
             "平今免收 → 派生走平今（今仓离场直接平今，不走锁仓）"),
    "AG": Product(
        product="AG", r_multiple_tp=2.0,
        exchange="SHFE",
        open_fee=Fee("rate", 0.1), closetoday_fee=None,
        price_tick=1.0, multiplier=15.0,
        note="上期所 SHFE 沪银：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 15(元/kg)、tick 1；费率 xlsx：交易万0.1（xlsx 基准档，2026-09-15 用户确认），"
             "无独立平今行 → 平今=开仓（closetoday_fee=None）"
             "（覆盖档：6、12 合约 & 2607-2610 = 万0.5，本期不消费按基准档）；"
             "平今不贵 → 派生走平今（省一次开仓 + 跨日平仓）"),
    "CU": Product(
        product="CU", r_multiple_tp=2.0,
        exchange="SHFE",
        open_fee=Fee("rate", 0.5), closetoday_fee=Fee("rate", 1.0),
        price_tick=10.0, multiplier=5.0,
        note="上期所 SHFE 沪铜：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 5(元/吨)、tick 10；费率 xlsx：开/平昨万0.5 / 平今万1.0；"
             "Y=2X 落在 3× 判据的分歧区（Y < 3X）→ 派生走**平今**"
             "（P-A 唯一行为变更品种：旧手写开关为锁仓，3× 经济账更正为平今，§6.5.5）"),
    # ── 郑商所 PTA（Tier 2 能源化工：成交额常年前三、随原油联动趋势明确）──
    # 键名 = 天勤符号末段："KQ.m@CZCE.TA" → parse_product() = "TA"（PTA 是俗名，
    #   符号代码是 TA）。注意：PTA 走 **CZCE 报单语义**（Phase 9）——
    #   exchange="CZCE" 时报单属性 FOK→FAK（InstrumentSpec.effective_order_advanced）、
    #   OPEN 手数钉 1 手（Engine._open_volume），档案只管品种参数、不管报单属性。
    "TA": Product(
        product="TA", r_multiple_tp=2.0,
        exchange="CZCE",
        open_fee=Fee("per_lot", 3.0), closetoday_fee=Fee.free(),
        price_tick=2.0, multiplier=5.0,
        note="郑商所 CZCE PTA(精对苯二甲酸)：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 5(元/吨)、tick 2；费率 xlsx：开仓 3 元/手 / 平今免收；"
             "郑商所品种报单走 FAK + OPEN 钉 1 手；CZCE 无平今指令 → 派生恒走锁仓；"
             "偶发装置/政策消息急拉急跌"),
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
        `"CFFEX.IF2609"` → `"IF2609"`（test_period_profile.py:133 明确断言此行为，
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
        "Trading/Infra/Product.py 的 PRODUCT_PROFILES 中"
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