# -*- coding: utf-8 -*-
"""
合约轴：部署配置 + 唯一运行时对象（合并）
==========================================================

术语锚点（§附F.1/F.2 · 2026-09-15 P-D 落位）
----------------------------------------------------
`Instrument` = 被交易的那张**具体合约**（IF2509 / AU2512），名字借自 CTP
柜台协议 `InstrumentField`（price_tick / multiplier / last_trade_date 一一对应；
其 exchange 一项在本仓**不再对应任何字段**）—— 不是自造词。
与相邻轴的分工：
  · `Product`（Infra/Product.py）：品种族档案（IF 全族一份），per-product；
  · `symbol`（signal_symbol / trade_symbol）：只是代码字符串，不是粒度概念；
  · 换月换的是本轴的 instrument 身份（trade_symbol / last_trade_date 行情回填），
    品种档案不动。已拍板：不改名为 ContractSpec（对齐 CTP 行业词，备选否决）。

合并（交接文档拍板"真合并"）
----------------------------------------------------
本文件承载**两个**模型，但语义与之前完全不同：

  · `InstrumentConfig`（pydantic，**frozen=True**）—— 部署级配置，6+3 字段。
    只放"启动前就定死、启动后不可变"的值：symbol / slippage / order_advanced /
    closetoday_first / 价格带字段位 / 涨跌停幅度档案位。
    **frozen 是断言级事实**：任何运行期写入直接 ValidationError（不再是
    docstring 里的一句话）——SimNow/main 里曾经的四处就地写入（trade_symbol /
    exchange / last_trade_date / signal_symbol）已随归位到运行时对象或
    构造期，本类被写即炸正是要的护栏。
    摘除的字段（旧键会构造期报错，见 _REMOVED_KEYS 提示）：
      price_tick / multiplier → 品种档案 `Product`（归位）
      exchange → **已整体删除**（交易所只作注释备案）
      last_trade_date         → `Instrument` 运行时身份（行情回填）
      open/close/closetoday_fee_rate → 品种档案 Fee 两档（已归位）

  · `Instrument`（普通类，**唯一一份运行时对象**）—— 原 InstrumentSpec 的
    合约身份 + InstrumentState 的运行时状态合并而成：
        ├ 静态身份（config 转发只读）：signal_symbol / slippage_ticks /
        │   order_advanced / closetoday_first / price_band_points
        ├ 品种派生（product 转发只读）：exec_policy（执行策略表）
        ├ 运行时身份（行情回填可写）：trade_symbol / last_trade_date
        ├ 有效值（SSOT=品种档案，构造期播种后不再改写）：price_tick /
        │   multiplier / verified / source
        └ 定价与成本：round_price / align_* / slip_price / cost_cash /
            points_to_cash（读有效 tick/乘数 × 档案 Fee 两档，属运行时语义）

  D-C：原 `instrument.spec` 兼容别名**已删除** —— 它让
  `instrument` / `.spec` / `.state` 三个名字指向同一块内存，把"概念更少"
  的合并收益吃回去一半；更麻烦的是 `instrument.spec.price_tick` 读起来像
  "静态规格"，实际读到的是**行情回填后的有效值**（歧义正是方案自己列为
  "唯一需要权衡"的那处）。现在两处说法各自名副其实：
    · 对象本身 `instrument`（或调用方持有的 `self.state`）—— 唯一运行时对象；
    · 持有者一律用 `.state` 这一个属性名（Broker / Engine / Source 同名）。

  所有权规则（不变，务必遵守）：一次运行**只有一份** Instrument ——
  main.py 建好后同时交给 `Broker.build_broker(..., state=instr)` 与
  `TradingEngine(..., state=instr)`。Broker 写（trade_symbol 主连映射 /
  last_trade_date 交割日回填 / verified 在线置位），Engine 读（闸门 / 对账 / 成本）。

成本口径（改"元"，不变）
    - 费率真值源 = 品种档案 Product 的 Fee 两档（开仓 / 平今），静态；
    - 手续费按 `Fee.cash(price, multiplier)` 折算为**每手元**，成本记账统一在元上做；
    - 盈亏毛值仍以"点"记账（gross_points），净值为元（net_cash = 毛利元 − 成本元）；
    - 滑点**不计入** cost_cash，而是体现在成交价上（见 dry_run broker 的让价）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from Trading.Infra.Product import ExecPolicy, Product


# 摘除的旧键 → 归属去向（extra=forbid 报错前的明确提示，交接文档）。
#   旧配置/旧测试若仍传这些键，构造期直接 ValueError 并指路 —— 不允许
#   "填了但被静默忽略"（-D 情形 A 的教训）。
_REMOVED_KEYS: Dict[str, str] = {
    "price_tick": "品种档案 Product.price_tick（P-B 归位，调参=改档案）",
    "multiplier": "品种档案 Product.multiplier（P-B 归位，调参=改档案）",
    "exchange": "已整体删除（2026-09-16 B 批）—— 交易所只作注释备案"
                "（EXEC_POLICY 行尾 + 各档案 note 散文）",
    "last_trade_date": "Instrument 运行时身份（行情回填，配置不再持有）",
    "open_fee_rate": "品种档案 Product.open_fee（P-A 归位）",
    "close_fee_rate": "品种档案 Product（平昨≡开仓档，P-A 删第三档）",
    "closetoday_fee_rate": "品种档案 Product.closetoday_fee（P-A 归位）",
    "instrument_verified": "Instrument.verified（Phase 3 已迁运行时）",
    "instrument_source": "Instrument.source（Phase 3 已迁运行时）",
    "max_order_volume": "已删除的死字段（从未被消费，见 test_p50 [8]）",
}


class InstrumentConfig(BaseModel):
    """合约轴的**部署级配置**（原 InstrumentSpec 收缩而来）。

    未知键（拼错字段）直接报错；**frozen=True**：构造后任何字段赋值直接
    ValidationError —— "配置不可变"从 docstring 声明升级为断言级事实。

    ⚠️ 凡"行情 / 成交回报会回填"的值一律属于 `Instrument`（本文件下方），
      不要往这里加。字段位保留但**不消费**的三个档案位（limit_up_pct /
      limit_down_pct / night_session）注释里写明缘由，删除前先过对账测试。
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_symbol: str = "KQ.m@CFFEX.IF"            # 缠论分析用的主连
    trade_symbol: str = ""                          # 初始月份合约：空 = 必须由主连映射
                                                    #   填充（SimNow._resolve_trade_symbol）；
                                                    #   映射失败 fail-fast，不再有默认月份兜底
    slippage_ticks: float = 1.0                # 单边滑点（tick 数）
    # 报单 advanced 指令（A2，2026-09-11）：一处配置，供所有 insert_order 调用点读取。
    #   "FOK"  全成或全撤 —— 本系统默认依赖它（无部分成交幽灵）
    #   "FAK"  部分成交后撤余量
    #   ⚠️：**已标定品种的实际生效值 = 品种执行策略表第 2 列**
    #   （见 Instrument.effective_order_advanced）；本字段只服务未标定品种的兜底。
    order_advanced: str = "FOK"
    closetoday_first: bool = True                   # 今仓成本开关（2026-09-10 更正注释：**不是**
                                                    #   "平仓优先平今"）。实际语义 = 是否允许按持仓
                                                    #   entry_date 把"今仓"判成平今费率；规则 ⑸ 下
                                                    #   OrderIntent.CLOSE 只用于跨日单，正常流程
                                                    #   恒走平昨费率，置 False 可整体关闭今仓判定。
    # 价格笼子band（D12 落地项，补字段位）。
    #   含义：限价单相对最新价的**最大偏离点数**；超出即被交易所拒。
    #   ⚠️ **一期不消费** —— 只是把字段位占住，避免二期加价格笼子护栏时又去
    #   改一遍合约模型（届时只需在 Broker 的报单前校验里读它）。
    price_band_points: float = 0.0       # 0 = 不限制（一期的唯一合法值）

    # ── 档案位（字段位保留，**当前不消费**）──
    limit_up_pct: float = 0.0            # 涨跌停板幅度（%，如 10.0）。仅作档案记录：
                                              #   涨跌停绝对价已随 2026-09-17 改造整体删除
                                              #   （报错价由交易所拒单 + 软件侧弹窗人工干预兜底）。
    limit_down_pct: float = 0.0
    night_session: bool = False               # 是否有夜盘。字段位保留但**不消费**：
                                              #   交易时段护栏（阻塞点 6 · Q5）
                                              #   判定为**不需要**（纯 K 线推送架构下非交易时段
                                              #   无 K 线 → 无信号 → 无报单，无从拦截）。
    #
    # 归位/迁出（含去向，详见模块 docstring）：
    #   price_tick / multiplier → Product（品种轴档案）
    #   exchange **整体删除**（不再有任何字段承载）
    #   last_trade_date → Instrument（运行时身份，行情回填）
    #   三档费率字段删除（费率归位品种档案 Fee 两档）。
    #   max_order_volume 死字段删除（0 消费，防回潮见 test_p50 [8]）。

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstrumentConfig":
        """从配置 dict 构造（未知键报错，缺键用模型默认值）。"""
        return cls(**(d or {}))

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    # ---------- 换日 ----------
    def is_new_day(self, prev_date: str, cur_date: str) -> bool:
        """两个日期串是否跨自然日（纯静态判定，不读任何运行时状态）。"""
        return (prev_date or "")[:10] != (cur_date or "")[:10]

    def _check_removed_keys(self, values: Dict[str, Any]) -> None:  # noqa: C901
        """被摘除键的**明确报错**（交接文档：不允许静默吞掉）。"""
        bad = {k: v for k, v in values.items() if k in _REMOVED_KEYS}
        if bad:
            hints = "；".join(
                "{} → 归属：{}".format(k, _REMOVED_KEYS[k]) for k in sorted(bad))
            raise ValueError(
                "InstrumentConfig 不再接受字段 [{}]（P-B 字段归位，2026-09-15）。{}；"
                "旧配置里的这些键不再被读取 —— 显式报错而不是静默忽略"
                "（交接文档 §7.2「静默吞掉」顺手处理）。".format(
                    ", ".join(sorted(bad)), hints))

    def __init__(self, **data: Any) -> None:  # noqa: D102
        self._check_removed_keys(data)
        super().__init__(**data)


# ════════════════════════════════════════════════════════════════════
# EffectiveSpec —— 合约有效参数（SSOT = 品种档案，D-D · 2026-09-17 修订）
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EffectiveSpec:
    """一张合约的**有效参数**（tick / 乘数），不可变值对象。

    真值源 = 品种档案 `Product`（2026-09-17 拍板：最小变动价位与合约乘数从
    档案获取，**没有任何信息需要从行情获取**）。构造期一次性播种进
    `Instrument._effective`，运行期不再改写 —— 原子性诉求（半新半旧）
    随行情回填通道一并消亡，保留 frozen 值对象只为维持"整体一份、只读
    转发"的形状。
    """
    price_tick: float
    multiplier: float


# ════════════════════════════════════════════════════════════════════
# Instrument —— 合约轴的**唯一一份运行时对象**
#   原 InstrumentSpec（静态身份）+ InstrumentState（运行时状态）合并而成。
#   为什么是普通类而不是 pydantic：它是**可变状态**；pydantic 的
#   model_fields_set / validate_assignment 语义在这里只会重新引入
#   要消掉的那种隐式行为。配置侧的不可变性由 InstrumentConfig.frozen 负责，
#   两侧各司其职。
# ════════════════════════════════════════════════════════════════════
class Instrument:
    """合约运行时对象（合并）：静态身份转发 + 运行时有效值 + 定价与成本。

    构造：`Instrument(config, product)`
      · config  — InstrumentConfig（frozen 部署配置），缺省 = 默认 IF 配置
      · product — Product 品种档案（tick/乘数/费率的真值源）；
        None = 未标定品种（有效值取 0 → 定价/对齐 fail-closed；启动期会被
        白名单闸门拒绝，这里允许 None 只为离线探针/测试便利）。

    有效值**SSOT = Product 档案**（2026-09-17 拍板：没有任何信息需要从
    行情获取）—— 构造期播种，在线 / 离线的运行值相同，运行期不再改写。

    改写运行时字段的路径只有三条：verified / source 由 SimNow._connect
    （在线置位 source=CONFIG）或 mark_config_offline()（离线声明）写；
    trade_symbol / last_trade_date 由 SimNow 从**真实月份合约行情**回填。
    """

    # ── 参数来源标记的合法值 ──
    # instrument 参数来源：SimNow._connect / mark_config_offline 置位，外部只读比较。
    #   CONFIG：在线通道（simnow/live）连接成功即置位 —— 合约参数 SSOT=品种档案，
    #   无任何信息需要从行情获取，verified 的语义即"在线通道已连通"。
    SOURCE_CONFIG: ClassVar[str] = "CONFIG"
    SOURCE_CONFIG_OFFLINE: ClassVar[str] = "CONFIG_OFFLINE"

    def __init__(self, config: Optional[InstrumentConfig] = None,
                 product: Optional["Product"] = None):
        self._config: InstrumentConfig = config if config is not None else InstrumentConfig()
        self._product = product
        # —— 运行时身份：初值取 config，行情路径回填（SimNow）——
        #   trade_symbol 可写：SimNow._resolve_trade_symbol 用真实月份合约
        #   （quote.underlying_symbol）刷新（主连 → 月份合约）。
        self.trade_symbol: str = self._config.trade_symbol
        #   last_trade_date：最后交易日 YYYY-MM-DD，SimNow 从行情回填；
        #   0 值（""）= 未知 → 交割月护栏对未知不校验（不校验未知的东西）。
        self.last_trade_date: str = ""
        # —— 有效值：SSOT = 品种档案，构造期一次性播种 ——
        #   2026-09-17 拍板：最小变动价位与合约乘数从品种档案获取，
        #   **没有任何信息需要从行情获取**。EffectiveSpec 仍是不可变值对象
        #   （整体一份、只读转发），构造后不再改写。同名 property 只读转发。
        self._effective: EffectiveSpec = EffectiveSpec(
            price_tick=float(product.price_tick) if product is not None else 0.0,
            multiplier=float(product.multiplier) if product is not None else 0.0,
        )
        # ── A′ 在线闸门──
        # verified: 在线通道已连通（SimNow._connect 成功分支置 True）；
        #   未置 True 的在线状态 → Engine._pre_trade_check 拒单 + 严重告警。
        #   离线 broker（dry_run/replay）不置位，由 mark_config_offline 声明来源。
        self.verified: bool = False
        # source: 当前参数来源标记 —— "CONFIG"（在线连接成功）
        #   / "CONFIG_OFFLINE"（dry_run/replay 离线声明）/ ""（尚未定）。
        self.source: str = ""

    # D-C：这里原有 `spec` 兼容 property（返回 self 自身），
    #   已删除 —— 见模块 docstring「D-C」段。取有效值请直接用 `instrument.xxx`。

    @property
    def config(self) -> InstrumentConfig:
        """部署级配置（frozen）。新代码读 slippage/order_advanced 等走本属性。"""
        return self._config

    @property
    def product(self) -> Optional["Product"]:
        """品种档案（Fee 两档 / exec_policy / 策略标定值的真值源）。"""
        return self._product

    # —— 有效值转发（不可变 EffectiveSpec，只读）——
    #   SSOT=品种档案，构造期播种后不再改写。刻意**不给 setter** ——
    #   有效值在运行期没有任何合法的改写路径。
    @property
    def price_tick(self) -> float:
        """有效最小变动价位（对齐 / 滑点口径都读它）。"""
        return self._effective.price_tick

    @property
    def multiplier(self) -> float:
        """有效合约乘数（元/点；points_to_cash / cost_cash 读它）。"""
        return self._effective.multiplier

    # —— config 只读转发（替代旧 InstrumentSpec 的静态字段读点）——
    @property
    def signal_symbol(self) -> str:
        return self._config.signal_symbol

    @property
    def slippage_ticks(self) -> float:
        return self._config.slippage_ticks

    @property
    def order_advanced(self) -> str:
        return self._config.order_advanced

    @property
    def closetoday_first(self) -> bool:
        return self._config.closetoday_first

    @property
    def price_band_points(self) -> float:
        return self._config.price_band_points

    # —— product 只读转发 ——
    #   原 `exchange` 只读属性随 `Product.exchange` 字段一并删除：
    #   字段没了，"转发它"就没有意义；交易所从此只以**注释**形态存在。

    @property
    def exec_policy(self) -> Optional["ExecPolicy"]:
        """品种执行策略三件事（今仓离场 offset / 报单属性 / 一笔挂几手）。

        真值源 = `Product.EXEC_POLICY` 表（构造期由 `_exec_kw()` 注入
        `Product.exec_policy` 字段），**代码只读不推** —— 不读费率、不看交易所名字。

        ⚠️ 2026-09-16 收紧（跨文件 fallback 清理）：
          · 类型 `Optional[Any]` → `Optional[ExecPolicy]`：`Any` 会把
            `pol.today_exit` 这类拼写错误从"静态可查"降级成"运行时才发现"；
          · **去掉 `getattr(p, "exec_policy", None)`** —— `exec_policy` 是
            `Product` 的**必填 dataclass 字段**（无默认值），那段 fallback 在
            正常路径上**不可达**；它唯一的实际作用是"哪天字段被删/改名了也
            静默给 None，然后引擎按保守侧悄悄换一套执行策略" —— 正是本仓
            禁止的跨文件静默降级。现在直接属性访问：字段没了就是 AttributeError。

        未标定品种（`product is None`，仅离线探针/测试便利）→ `None`，调用方按
        保守侧处理（报单 FOK、今仓走 R-OPEN 反向锁仓）。

        **已标定品种恒非 None** —— 这条不是靠本方法自觉，而是由
        `Engine._restore` 启动期硬断言守住（`_assert_product_ssot`）：品种在册
        却没有执行策略行 → 拒绝启动，而不是静默降级。
        """
        p = self._product
        return p.exec_policy if p is not None else None

    def __repr__(self) -> str:
        return ("Instrument(trade_symbol={!r}, price_tick={!r}, "
                "multiplier={!r}, verified={!r}, source={!r})"
                .format(self.trade_symbol, self.price_tick,
                        self.multiplier, self.verified, self.source))

    # ---------- 报单属性 / 交割护栏（原 InstrumentSpec 静态判定，迁入）----------
    def effective_order_advanced(self) -> str:
        """返回实际报单用的 advanced 属性（FOK / FAK）。

        2026-09-16 起**唯一口径 = 品种执行策略表第 2 列**（`EXEC_POLICY`），
        取代原「CZCE 强制 FAK」交易所分支 —— 用户明令：代码里不得
        出现按交易所名字判断走向的逻辑，只看品种、只看表。

        未标定品种（无档案 / 档案无策略行）→ 回落部署配置 `order_advanced`
        （默认 "FOK"，保守侧）。

        ⚠️ 配套不变式：**FAK 的品种一笔必须挂 1 手** —— 单笔 1 手下 FAK ≡ FOK
        （没有"剩余"可撤），报单填充三态（待报/全成/全撤）不变量 4 天然保持，
        无需扩展状态机、无需补簿。该不变式由 `ExecPolicy.__post_init__`
        在构造期硬断言（改表即炸，不留静默默认值）。
        """
        pol = self.exec_policy
        if pol is not None:
            return str(pol.order_advanced)
        return self.order_advanced

    def delivery_guard_blocked(self, today: str, threshold_days: int = 1) -> bool:
        """交割月护栏判定（阻塞点 4 · D8）。返回 True = 距最后交易日不足
        `threshold_days` 个交易日（含当天）→ 本护栏命中（具体拦哪个动作由 Engine
        按账户三态×意图再判，本方法只回答"是否在禁用窗内"）。

        语义（拍板）：
          · 判据 =「剩余交易日 **<** threshold_days 即拦」；
          · **护栏对象 = 现行主力 `trade_symbol`** 的 last_trade_date —— 主连换月
            （IF2609→IF2610）时 trade_symbol 更新、判定随之解除；
          · 三态分发在 Engine._pre_trade_check：空仓态拦【开仓】、锁仓态拦【平仓/
            解锁】、运行态不拦；
          · `last_trade_date` 未知（离线 dry_run / 行情未回填）→ 返回 False
            （"不校验未知的东西"）。

        天数口径：只数工作日（Mon-Fri），不计法定节假日 —— 节假日需交易日历，
        未引入（一期不消费），文档已注明 N=1 ≈ 仅最后交易日当天拦。

        last_trade_date 是行情回填的**运行时身份字段**（原挂在 spec 上被
        SimNow 就地改写，例 1）—— 合并后本判定读自身字段，归属更直白。
        """
        if not self.last_trade_date:
            return False
        rem = _weekdays_between(today, self.last_trade_date)
        return rem < int(threshold_days)

    def mark_config_offline(self) -> None:
        """离线模式（dry_run/replay）显式降级标记。

        档案值只允许在离线模式生效，且必须能自证来源 —— 启动横幅 + WARN
        写明"tick/乘数取自品种档案，回测结果不可直接外推实盘"由调用方
        （main.py）负责。
        """
        self.source = self.SOURCE_CONFIG_OFFLINE

    # ---------- 价格对齐（读**有效 tick**） ----------
    def round_price(self, price: float, mode: str = "nearest") -> float:
        """把价格对齐到**有效** price_tick。mode: nearest / up / down。"""
        tick = self.price_tick
        if not tick or tick <= 0:
            return price
        raw = price / tick
        if mode == "up":
            n = math.ceil(raw - 1e-9)
        elif mode == "down":
            n = math.floor(raw + 1e-9)
        else:
            n = math.floor(raw + 0.5)
        return round(n * tick, 10)

    def align_entry(self, price: float, side_sign: int) -> float:
        """开仓价对齐：买向上、卖向下（让价方向 = 不利方向，保守）。"""
        return self.round_price(price, "up" if side_sign > 0 else "down")

    def align_exit(self, price: float, side_sign: int) -> float:
        """平仓价对齐：平多＝卖出向下，平空＝买入向上。"""
        return self.round_price(price, "down" if side_sign > 0 else "up")

    def slip_price(self, price: float, side_sign: int, for_open: bool) -> float:
        """叠加滑点。开仓时顺着不利方向推，平仓同理。

        滑点**幅度**是静态项（config.slippage_ticks，不随品种变），
        换算用的 tick 是**有效值**（本类）—— 所以本方法属运行时语义。
        """
        tick = self._config.slippage_ticks * self.price_tick
        return price + side_sign * tick if for_open else price - side_sign * tick

    # ---------- 成本（读品种档案 Fee 两档 + 有效乘数，统一在元上算） ----------
    def cost_cash(self, entry_price: float, exit_price: float,
                  closetoday: bool, volume: int = 1) -> float:
        """往返手续费，**元**口径（每手元 × 手数）。

        closetoday=False → 平昨档；True → 平今档。平昨 ≡ 开仓（xlsx 全表没有
        一行把"平昨"单独列出来，见交接文档）→ 两种情形都是
        「开仓档成交一笔 + 相应离场档成交一笔」。

        滑点不在此处计（见模块 docstring）。
        ⚠️ 取代已删除的 `cost_points`（点数口径）：per_lot 档（如黄金 10 元/手、
        PTA 3 元/手）在点数口径下无法无损表达（10 ÷ 乘数再乘回，中间还过一次
        浮点），成本记账统一在**元**上做；毛盈亏仍以点记（gross_points），
        净值 = 毛利元 − 成本元（net_cash）。

        ⚠️ 2026-09-16 去掉 `product` 入参（会计侧 SSOT 收敛）：
          原签名 `cost_cash(product, entry_price, …)` 允许调用方把**自己查到的**
          品种档案传进来 —— 而两个调用点（`Engine._book_close` /
          `Reconcile`）查的是 `cfg.product_profile`（**实时按
          `cfg.instrument.signal_symbol` 查表**），`Instrument` 持有的却是
          **构造期冻结**的那份（`self._product`）。两者只在"`--symbol` 变更
          发生在 Instrument 构造之前"这个**约定**下恒等 —— 一旦不等，
          就是「按 AU 决策、按 IF 记账」：成本算错且完全静默。
          品种来源收敛到 `self._product` 后，"传进来的档案与决策用的不是同一份"
          在**结构上**不再可能（连入口都没有了）。

        未标定品种（`self._product is None`）→ 0.0：与调用方原写法
        `… if p is not None else 0.0` 逐字等价（无档案即不计成本），
        只是把这条判据从**每个调用点**收回本对象一处。
        """
        p = self._product
        if p is None:
            return 0.0
        open_fee, ct_fee = p.fee_pair()
        exit_fee = ct_fee if closetoday else open_fee
        return (open_fee.cash(entry_price, self.multiplier)
                + exit_fee.cash(exit_price, self.multiplier)) * volume

    def points_to_cash(self, points: float, volume: int = 1) -> float:
        return points * self.multiplier * volume


def _weekdays_between(start: str, end: str) -> int:
    """统计 (start, end] 区间内的**工作日**（Mon-Fri）数，供交割月护栏用。

    仅数工作日不算节假日：start 到 end 跨越周末时，周末不计入剩余交易日
    —— 这使「最后交易日为周一、今天是上周五」时剩 1 日（周一），N=1 不拦，
    与"只拦最后交易日当天"语义一致。

    返回 0 的情形：start >= end（已过期 / 同日，即最后交易日当天）或
    start/end 解析失败（空串 / 非 YYYY-MM-DD）。
    """
    try:
        s = date.fromisoformat(str(start or "")[:10])
        e = date.fromisoformat(str(end or "")[:10])
    except (ValueError, TypeError):
        return 0
    if e <= s:
        return 0
    n, d = 0, s
    while True:
        d += timedelta(days=1)
        if d > e:
            break
        if d.weekday() < 5:
            n += 1
    return n
