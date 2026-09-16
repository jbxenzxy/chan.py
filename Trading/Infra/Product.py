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
    （运行时对象在 Infra/Instrument.py 的 Instrument）；
  · symbol 只是代码字符串（signal_symbol / trade_symbol），不是粒度概念。
  · parse_product（保月份，认定到合约）与 parse_product_key（剥月份，查品种档案）
    这对函数的分工就是两个粒度的代码体现。

变更纪律（§5.4 · 取代旧「Profile=标定 / Spec=事实」后缀规则）
----------------------------------------------------
文件名只回答「这是 per-product 粒度的东西」；「该不该走 git 评审」用两个正交手段表达：
  · 标定值（r_multiple_tp）：改 = 改代码资产，走 git 评审 + 对账测试（test_p50）；
  · **费率：不再是手抄值** —— 真值源 = 券商费率表 `Docs/手续费标准-.xlsx`，
    经 `Tool/GenFeeTable.py` 刷新**本文件内的 GENERATED 标记区块**（机器生成、
    禁止手改），费率数字在本文件的手写部分里**一个字面量都没有**（D-B · 2026-09-15）。
    对账测试：`Trading/Test/test_product_fee_table.py`（生成区块 ⇄ 档案 ⇄ xlsx 三方一致）。
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
from typing import ClassVar, Dict, List, Optional, Tuple


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


def _freeze_fee(t: Tuple[str, float]) -> Fee:
    """费率区块里的 (kind, value) 元组 → Fee 对象（唯一转换点）。"""
    return Fee(t[0], t[1])


# 覆盖档条目 = (标签, 开仓档, 平今档)。见 Product.fee_overrides。
OverrideItem = Tuple[str, Fee, Optional[Fee]]


# ══════════════════════════════════════════════════════════════════
# 费率数据区（D-B · 生成式 SSOT）—— **机器生成，禁止手工编辑**
# ══════════════════════════════════════════════════════════════════
# 真值源 = `Docs/手续费标准-.xlsx`。区块内是纯数据元组，与本文件的手写内容严格分离，
# 分离手段不是"文件边界"而是"标记 + 逐字节护栏"（为什么不用独立文件：见
# `Tool/GenFeeTable.py` 模块 docstring —— 交接文档 §5.1 把 Infra/ 定死成 7 个模块）：
#   · 生成器只重写两块标记之间的文本，**区块外逐字节不动**；
#   · 有人在区块里手改一个数字 → `Tool/GenFeeTable.py --check` 与
#     `Test/test_product_fee_table.py [3f]`（逐字节比对）立刻变红；`[3g]-[3i]`
#     还会自检"这道闸门不是恒返回 0"。
# 改费率 = 改 xlsx（或改生成器解析规则）→ 重跑：
#
#     python Trading/Tool/GenFeeTable.py
#
# >>> GENERATED: FeeTable（禁止手工编辑；改费率请改 xlsx 后重跑生成器）
# 纯数据元组（kind, value）：
#   kind="rate"    → value = 万分之几（xlsx 里打印的 "0.23%%" 就存 0.23）
#   kind="per_lot" → value = 元/手
FeeTuple = Tuple[str, float]
Pair = Tuple[FeeTuple, Optional[FeeTuple]]   # (开仓档, 平今档; None=同开仓档)

# 源表品种名（备案：便于人工回查 xlsx 行）
BASE_LABELS: Dict[str, str] = {
    'AG': '白银',
    'AU': '黄金',
    'CU': '铜',
    'IC': '中证500',
    'IF': '沪深300指数',
    'IH': '上证50',
    'IM': '中证1000',
    'TA': 'PTA',
}

# 基准档：品种键 → (开仓/平昨档, 平今档)。平今 None = xlsx 无独立平今行（同开仓档）。
BASE: Dict[str, Pair] = {
    'AG': (('rate', 0.1), None),
    'AU': (('per_lot', 10.0), ('per_lot', 0.0)),
    'CU': (('rate', 0.5), ('rate', 1.0)),
    'IC': (('rate', 0.23), ('rate', 2.3)),
    'IF': (('rate', 0.23), ('rate', 2.3)),
    'IH': (('rate', 0.23), ('rate', 2.3)),
    'IM': (('rate', 0.23), ('rate', 2.3)),
    'TA': (('per_lot', 3.0), ('per_lot', 0.0)),
}

# 覆盖档（合约月份差异化费率）：品种键 → ((标签, 开仓档, 平今档), …)
# ⚠️ 当前**不消费**（Product.fee_overrides 字段位保留、默认空）。
#    AU/AG 主力滚到覆盖档合约时若不启用本表，回测成本会静默低估；
#    启用方式见 Product.fee_overrides 字段注释（一处收口，勿在别处另开分支）。
OVERRIDES: Dict[str, List[OverrideItem]] = {
    'AG': [('6、12合约&2607、2608、2609、2610合约', ('rate', 0.5), None)],
    'AU': [('6、12合约&2607、2608、2609、2610合约', ('per_lot', 20.0), ('per_lot', 0.0))],
}
# <<< END GENERATED


# ══════════════════════════════════════════════════════════════════
# 品种执行策略表（2026-09-16 用户拍板）—— 今仓离场 / 报单属性 / 每笔手数
# ══════════════════════════════════════════════════════════════════
# 这三件事原先散落在三处（交易所能力闸门 + 费率 3× 判据 + 交易所 FOK/FAK 分支），
# 现在收成**一张 8 行的表**，代码只读不推。
#
#   close_mode      今仓离场用哪个 offset ——
#                     "R-OPEN"     → 反向开仓锁仓（三态机：会进锁仓态，次日拆锁）
#                     "CLOSETODAY" → 直接平今（两态机：平完即回空仓，锁仓态不可达）
#   order_advanced  报单属性 —— "FOK"（全成全撤）/ "FAK"（部分成交后撤余量）
#   lots_per_order  一笔挂几手
#
# ⚠️ 为什么要有这张表（用户原话）：「让程序按交易费率计算是否平今，代码逻辑复杂
#    （有些品种还涉及两种交易费率）。用这张表，相当于用户基于费率已算好了是否
#    要平今，无需代码去计算，代码只需读这个表格即可。」
#    —— 所以**决策侧零费率引用**；费率数据保留给会计侧 `cost_cash` 用。
#
# ⚠️ close_mode 是「我们的选择」，不是「交易所的能力」：上期所虽然支持平今指令，
#    但并非每个品种平今都便宜 —— 只有用户算出来平今便宜的才填 CLOSETODAY。
#    **代码里禁止再用交易所名字判断任何走向**（用户第 3 轮 ⑵ 明令，第 2 列纯注释）。
#
# ⚠️ CLOSETODAY ≠ 永发 CLOSETODAY：昨仓离场仍走 CLOSE（平昨）。
#    本表第 1 列只约束**今仓**怎么离场。
#
# 硬断言（构造期拒绝启动，不留静默默认值）：
#   · 三列取值合法（close_mode / order_advanced / lots_per_order ≥ 1）；
#   · **FAK ⟹ lots_per_order == 1**（用户第 2 轮 ⑵⑶ 定为硬断言）——
#     FAK 允许部分成交后撤余量，只有"一笔 1 手、没有余量可撤"时 FAK 才等价于
#     FOK；放开 N 会破坏「待报 / 全成 / 全撤」三态不变量。
R_OPEN = "R-OPEN"
CLOSETODAY = "CLOSETODAY"
FOK = "FOK"
FAK = "FAK"


@dataclass(frozen=True)
class ExecPolicy:
    """一个品种的执行侧三件事。**不可变**，构造期自校验（见上方硬断言）。"""
    close_mode: str
    order_advanced: str
    lots_per_order: int

    def __post_init__(self) -> None:
        if self.close_mode not in (R_OPEN, CLOSETODAY):
            raise ValueError("close_mode 必须是 {} / {}，收到 {!r}".format(
                R_OPEN, CLOSETODAY, self.close_mode))
        if self.order_advanced not in (FOK, FAK):
            raise ValueError("order_advanced 必须是 {} / {}，收到 {!r}".format(
                FOK, FAK, self.order_advanced))
        if int(self.lots_per_order) < 1:
            raise ValueError("lots_per_order 必须 ≥ 1，收到 {!r}".format(
                self.lots_per_order))
        if self.order_advanced == FAK and int(self.lots_per_order) != 1:
            raise ValueError(
                "FAK 品种一笔必须挂 1 手（收到 {}）：FAK 允许部分成交后撤余量，"
                "只有 N=1 时 FAK 才等价于 FOK，放开 N 会破坏「待报 / 全成 / 全撤」"
                "三态不变量。".format(self.lots_per_order))


# 8 个品种的执行策略（与 PRODUCT_PROFILES 同键）。
#   行尾注释的交易所只是**备案**，不参与任何判断 —— 代码只读后三列。
EXEC_POLICY: Dict[str, ExecPolicy] = {
    "IF": ExecPolicy(R_OPEN, FOK, 2),        # CFFEX
    "IH": ExecPolicy(R_OPEN, FOK, 2),        # CFFEX
    "IC": ExecPolicy(R_OPEN, FOK, 2),        # CFFEX
    "IM": ExecPolicy(R_OPEN, FOK, 2),        # CFFEX
    "AU": ExecPolicy(CLOSETODAY, FOK, 2),    # SHFE
    "AG": ExecPolicy(CLOSETODAY, FOK, 2),    # SHFE
    "CU": ExecPolicy(CLOSETODAY, FOK, 2),    # SHFE
    "TA": ExecPolicy(R_OPEN, FAK, 1),        # CZCE
}


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
      【费率（P-A 静态化 · D-B 生成式 · 2026-09-15）：真值源 = 券商费率表
        （Docs/手续费标准-.xlsx → Tool/GenFeeTable.py → 本文件 GENERATED 区块）】
      open_fee                 开仓费率。xlsx 只有两个口径：交易（= 开仓 = 平昨）与平今
                               —— 全表 90 条带独立平今行的基准条目里没有一行把"平昨"
                               单独列出来（§6.4），故**平昨档 = 开仓档，不设第三档**。
      closetoday_fee           平今费率；None = 与开仓同档（如 AG 无独立平今行）；
                               Fee.free() = 平今免收（如 AU/TA）。
      fee_overrides            **覆盖档**（合约月份差异化费率）字段位，默认空 = 不消费。
                               来源 = 费率区块的 OVERRIDES（AU 6/12 合约 20 元/手、
                               AG 6/12 合约 万0.5）。留位理由见字段处注释。
      exec_policy              执行侧三件事（今仓离场 offset / 报单属性 / 一笔挂几手）。
                               唯一事实源 = 本模块上方 `EXEC_POLICY` 表 —— **代码只读不推**：
                               不读费率、不看交易所名字（2026-09-16 用户拍板）。
      exchange                 交易所（CFFEX/SHFE/INE/DCE/CZCE/GFEX）。
                               ⚠️ **纯备案/注释**：不参与任何判断，也没有任何代码读它
                               去做分支（用户第 3 轮 ⑵⑶ 明令清掉按交易所名字的判断）。
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
    exec_policy: ExecPolicy                 # 执行策略表行（今仓离场/报单属性/每笔手数）
    closetoday_fee: Optional[Fee] = None    # None = 同开仓档（xlsx 无独立平今行）
    price_tick: float = 0.2            # 最小变动价位（离线兜底；中金所四品种均 0.2）
    exchange: str = ""                      # 交易所：**纯备案**，零判断（见字段 docstring）
    note: str = ""                          # 调参记录 / 数据来源 / 标定状态
    # 覆盖档字段位（R3 · 2026-09-15，交接文档 §6.3-b 明令"字段位必须留"）。
    #   ⚠️ **当前不消费**：`fee_pair()` 一律读基准档。
    #   后果（量化）：AU 主力滚到 6/12 合约时基准档 10 元/手 vs 覆盖档 20 元/手
    #   → 回测成本低估 2.0×；AG 万0.1 vs 万0.5 → 低估 5.0×。
    #   **决策侧完全不受影响** —— 2026-09-16 起"走不走平今"由 `EXEC_POLICY` 表
    #   第 1 列直接给定，决策侧**根本不读费率**（用户第 4 轮 ⑴ 拍板：人算 → 改表 →
    #   启动），受影响的只有会计侧 `cost_cash`。
    #   启用方式：把 `effective_fee_override()` 接进 `fee_pair()`，并在
    #   `Instrument.cost_cash` 传 trade_symbol —— 一处收口，勿在别处另开分支。
    fee_overrides: Tuple[OverrideItem, ...] = ()

    @property
    def label(self) -> str:
        return self.product

    def fee_pair(self) -> Tuple[Fee, Fee]:
        """返回（开仓费, 平今费）两档**基准档**。closetoday_fee=None → 回开仓档。

        P-A 用户拍板（2026-09-15）：**不做合约月份覆盖档** —— 有覆盖档的品种
        （AU 6/12 合约 20 元/手、AG 6/12 万0.5）一律按第一个基准档计。
        R3（2026-09-15）：**字段位已留**（`fee_overrides`，数据由费率区块提供、
        构造期填入），但本方法仍只读基准档 ——
        字段位存在 ≠ 已消费，启用方式见 `fee_overrides` 的注释。

        ⚠️ 2026-09-16 起本方法**只服务会计侧**（`cost_cash` 回测/记账）。
        "走不走平今"由 `EXEC_POLICY` 表第 1 列直接给定，决策侧不读费率。
        """
        return self.open_fee, (self.open_fee if self.closetoday_fee is None
                               else self.closetoday_fee)

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


def _fee_kw(code: str) -> Dict[str, object]:
    """从**本模块的生成区块**取费率 → Product 构造参数（两档费率 + 覆盖档）。

    D-B（2026-09-15）唯一手抄消除点：本函数是费率数据进入档案的**唯一入口**，
    Product 条目里不再出现任何费率数字。区块由 `Tool/GenFeeTable.py` 机器生成、
    就写在**本文件上方**（`>>> GENERATED` … `<<< END GENERATED`）—— 所以这里读的
    只是同模块的模块级常量，没有额外模块依赖。生成区块缺该品种时直接 KeyError
    （启动期硬失败，好过静默用错费率）。
    """
    o, ct = BASE[code]
    overrides: Tuple[OverrideItem, ...] = tuple(
        (label, _freeze_fee(bo), (_freeze_fee(bct) if bct is not None else None))
        for label, bo, bct in OVERRIDES.get(code, ()))
    return {
        "open_fee": _freeze_fee(o),
        "closetoday_fee": (_freeze_fee(ct) if ct is not None else None),
        "fee_overrides": overrides,
    }


def _exec_kw(code: str) -> Dict[str, object]:
    """从**本模块的执行策略表**取该品种的执行三件事 → Product 构造参数。

    与 `_fee_kw(code)` 完全同构（同一张表、同一处注入、同样的"缺行即炸"）：
    生成器/人工改 `EXEC_POLICY` 一处即可，Product 条目里**不重复写**这些值。
    表里缺该品种 → 直接 KeyError（启动期硬失败，好过静默走错执行策略）。
    """
    return {"exec_policy": EXEC_POLICY[code]}


# 8 个品种的档案（中金所股指期货 IF/IH/IC/IM + 上期所金属 AU/AG/CU + 郑商所 PTA）。
# 条目实参顺序：策略标定值在前（r_multiple_tp），费率与合约事实在后；
# note 同序。显式给真值；note 标定状态。
#
# ⚠️ 手续费（D-B · 2026-09-15 生成式）：本段**不再手写任何费率数字** ——
#   费率经 `_fee_kw(code)` 从**本文件的 GENERATED 费率区块**（由 Tool/GenFeeTable.py
#   从 Docs/手续费标准-.xlsx 生成）注入。改费率 = 改 xlsx（或生成器解析规则）
#   + 重跑生成器，**不要在本文件里加数字**（加了就成了第二份事实源，且会被
#   `GenFeeTable.py --check` 当场抓出）。
#   ⚠️ 平今口径分歧（CFFEX 万2.3 vs 早期代码的万3.45 = 万2.3 × 1.5）已按
#   用户拍板"以 xlsx 为准"落值；若实盘成交单显示券商按 1.5 倍加收，
#   改 xlsx（或生成器）后重跑，而非改本文件。
#   对账守护：Trading/Test/test_product_fee_table.py（费率区块 ⇄ 档案 ⇄ xlsx 三方一致）。
#
# ⚠️ price_tick / multiplier 是**离线兜底值，无自动对账，需人工维护**（2026-09-14 评审 P2-3）
#   —— 半句都不能省的背景：
#     · 实盘（simnow/live）：由 SimNow._apply_instrument_quote 从**真实月份合约行情**
#       原子覆盖这两个字段（`Instrument.apply_quote`，Phase 3 起运行时有效值归
#       Instrument），本表的取值**在实盘不被采用**；
#     · 离线（dry_run/replay）：没有行情可比，有效值就是本表播种的配置值
#       （2026-09-15 P-B 起播种桥已删，Instrument 构造时直接取档案初值）
#       —— 本表过期 = 回测/模拟成交**静默用错规格**（tick 错 → 限价口径错；乘数错 → PnL 错）。
#   维护口径：每次品种合约参数调整（交易所公告换月/改乘数）后，同步改本表并跑
#   Trading/Test/test_p50_review_fixes.py 的对账用例。
#
# ⚠️ 覆盖档（合约月份差异化费率）**字段位已留但不消费**（R3 · 2026-09-15）：
#   费率区块 OVERRIDES 已收录 AU/AG 的 6、12 合约档并填入 `fee_overrides`，
#   但 `fee_pair()` 一律读基准档 —— 启用方式与量化后果
#   见 `Product.fee_overrides` 字段注释。
#   ⚠️ 决策侧（走不走平今）自 2026-09-16 起**根本不读费率**，只读 `EXEC_POLICY`。
PRODUCT_PROFILES: Dict[str, Product] = {
    "IF": Product(
        product="IF", r_multiple_tp=2.0,
        exchange="CFFEX",
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IF：盈亏比 1:2（L3 在 2R 启动）；"
             "费率 xlsx：交易万0.23 / 平今万2.3（另有交割万0.5，本系统不参与交割不消费）；"
             "今仓离场 = R-OPEN（反向锁仓）",
        **_fee_kw("IF"),
        **_exec_kw("IF"),
    ),
    "IH": Product(
        product="IH", r_multiple_tp=2.0,
        exchange="CFFEX",
        price_tick=0.2, multiplier=300.0,
        note="中金所 CFFEX IH：盈亏比 1:2（L3 在 2R 启动）；费率同 IF（交易万0.23/平今万2.3）",
        **_fee_kw("IH"),
        **_exec_kw("IH"),
    ),
    "IC": Product(
        product="IC", r_multiple_tp=3.0,
        exchange="CFFEX",
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IC：盈亏比 1:3（L3 在 3R 启动）、乘数 200 元/点；费率同 IF",
        **_fee_kw("IC"),
        **_exec_kw("IC"),
    ),
    "IM": Product(
        product="IM", r_multiple_tp=3.0,
        exchange="CFFEX",
        price_tick=0.2, multiplier=200.0,
        note="中金所 CFFEX IM：盈亏比 1:3（L3 在 3R 启动）、乘数 200 元/点；费率同 IF",
        **_fee_kw("IM"),
        **_exec_kw("IM"),
    ),
    # ── 上期所金属（Tier 1 商品：流动性 + 趋势 + 形态干净，缠论画段体验好）──
    # 商品档盈亏比暂统一 1:2（L3 在 2R 启动），与 IF/IH 一致；IC/IM 因波动大、趋势性弱
    #   用 1:3。R 下限已删除（R = max(A, 2×ATR) 纯自适应），不再有"点数地板"。
    "AU": Product(
        product="AU", r_multiple_tp=2.0,
        exchange="SHFE",
        price_tick=0.02, multiplier=1000.0,
        note="上期所 SHFE 沪金：盈亏比 1:2（L3 在 2R 启动）、趋势强可上探 1:3；"
             "乘数 1000(元/克)、tick 0.02；费率 xlsx：开仓 10 元/手 / 平今免收"
             "（覆盖档：6、12 合约 & 2607-2610 = 20 元/手，字段位已留未消费）；"
             "今仓离场 = CLOSETODAY（直接平今，两态机）",
        **_fee_kw("AU"),
        **_exec_kw("AU"),
    ),
    "AG": Product(
        product="AG", r_multiple_tp=2.0,
        exchange="SHFE",
        price_tick=1.0, multiplier=15.0,
        note="上期所 SHFE 沪银：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 15(元/kg)、tick 1；费率 xlsx：交易万0.1（xlsx 基准档，2026-09-15 用户确认），"
             "无独立平今行 → 平今=开仓（closetoday_fee=None）"
             "（覆盖档：6、12 合约 & 2607-2610 = 万0.5，字段位已留未消费）；"
             "今仓离场 = CLOSETODAY（直接平今，两态机）",
        **_fee_kw("AG"),
        **_exec_kw("AG"),
    ),
    "CU": Product(
        product="CU", r_multiple_tp=2.0,
        exchange="SHFE",
        price_tick=10.0, multiplier=5.0,
        note="上期所 SHFE 沪铜：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 5(元/吨)、tick 10；费率 xlsx：开/平昨万0.5 / 平今万1.0；"
             "今仓离场 = CLOSETODAY（用户按费率算定：平今万1.0 < 锁仓路径 4 笔共万2.0）",
        **_fee_kw("CU"),
        **_exec_kw("CU"),
    ),
    # ── 郑商所 PTA（Tier 2 能源化工：成交额常年前三、随原油联动趋势明确）──
    # 键名 = 天勤符号末段："KQ.m@CZCE.TA" → parse_product() = "TA"（PTA 是俗名，
    #   符号代码是 TA）。报单属性与每笔手数 = `EXEC_POLICY["TA"]`（FAK + 1 手），
    #   由 `**_exec_kw("TA")` 注入 —— 不再由交易所名字推导（2026-09-16）。
    "TA": Product(
        product="TA", r_multiple_tp=2.0,
        exchange="CZCE",
        price_tick=2.0, multiplier=5.0,
        note="郑商所 CZCE PTA(精对苯二甲酸)：盈亏比 1:2（L3 在 2R 启动）；"
             "乘数 5(元/吨)、tick 2；费率 xlsx：开仓 3 元/手 / 平今免收；"
             "报单 FAK + 一笔 1 手；今仓离场 = R-OPEN（反向锁仓）；"
             "偶发装置/政策消息急拉急跌",
        **_fee_kw("TA"),
        **_exec_kw("TA"),
    ),
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
