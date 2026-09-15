# -*- coding: utf-8 -*-
"""
合约轴：部署配置 + 唯一运行时对象（P-B · 2026-09-15 合并）
==========================================================

术语锚点（§附F.1/F.2 · 2026-09-15 P-D 落位）
----------------------------------------------------
`Instrument` = 被交易的那张**具体合约**（IF2509 / AU2512），名字借自 CTP
柜台协议 `InstrumentField`（price_tick / multiplier / exchange / last_trade_date
一一对应）—— 不是自造词。与相邻轴的分工：
  · `Product`（Infra/Product.py）：品种族档案（IF 全族一份），per-product；
  · `symbol`（signal_symbol / trade_symbol）：只是代码字符串，不是粒度概念；
  · 换月换的是本轴的 instrument 身份（trade_symbol / last_trade_date 行情回填），
    品种档案不动。已拍板：不改名为 ContractSpec（对齐 CTP 行业词，备选否决）。

P-B 合并（交接文档 §4.3 · 2026-09-15 拍板"真合并"）
----------------------------------------------------
本文件承载**两个**模型，但语义与 P-B 之前完全不同：

  · `InstrumentConfig`（pydantic，**frozen=True**）—— 部署级配置，6+3 字段。
    只放"启动前就定死、启动后不可变"的值：symbol / slippage / order_advanced /
    closetoday_first / 价格带字段位 / 涨跌停幅度档案位。
    **frozen 是断言级事实**：任何运行期写入直接 ValidationError（不再是
    docstring 里的一句话）——SimNow/main 里曾经的四处就地写入（trade_symbol /
    exchange / last_trade_date / signal_symbol）已随 P-B 归位到运行时对象或
    构造期，本类被写即炸正是 P-B 要的护栏。
    摘除的字段（旧键会构造期报错，见 _REMOVED_KEYS 提示）：
      price_tick / multiplier → 品种档案 `Product`（P-B 归位）
      exchange                → 品种档案 `Product`（P-B 归位）
      last_trade_date         → `Instrument` 运行时身份（行情回填）
      open/close/closetoday_fee_rate → 品种档案 Fee 两档（P-A 已归位）

  · `Instrument`（普通类，**唯一一份运行时对象**）—— 原 InstrumentSpec 的
    合约身份 + InstrumentState 的运行时状态合并而成：
        ├ 静态身份（config 转发只读）：signal_symbol / slippage_ticks /
        │   order_advanced / closetoday_first / price_band_points
        ├ 品种派生（product 转发只读）：exchange / supports_closetoday
        ├ 运行时身份（行情回填可写）：trade_symbol / last_trade_date
        ├ 有效值（行情回填可写）：price_tick / multiplier / 涨跌停区间 /
        │   verified / source   ← 初值直接取 Product 档案（播种桥已消亡）
        └ 定价与成本：round_price / align_* / slip_price / cost_cash /
            points_to_cash（读有效 tick/乘数 × 档案 Fee 两档，属运行时语义）

  兼容视图：`instrument.spec` **返回 instrument 自身** —— 合并后"静态规格"
  与"运行时状态"是同一个对象，历史上 `state.spec.xxx` / `b.spec.xxx` 的
  只读引用继续工作（读到的是运行时有效值 + 转发项，语义比旧版更一致）。

  所有权规则（不变，务必遵守）：一次运行**只有一份** Instrument ——
  main.py 建好后同时交给 `Broker.build_broker(..., state=instr)` 与
  `TradingEngine(..., state=instr)`。Broker 写（apply_quote / trade_symbol /
  last_trade_date 回填），Engine 读（闸门 / 对账 / 成本）。

成本口径（P-A · 2026-09-15 改"元"，不变）
    - 费率真值源 = 品种档案 Product 的 Fee 两档（开仓 / 平今），静态；
    - 手续费按 `Fee.cash(price, multiplier)` 折算为**每手元**，成本记账统一在元上做；
    - 盈亏毛值仍以"点"记账（gross_points），净值为元（net_cash = 毛利元 − 成本元）；
    - 滑点**不计入** cost_cash，而是体现在成交价上（见 dry_run broker 的让价）。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from Trading.Infra.Product import Product


# P-B 摘除的旧键 → 归属去向（extra=forbid 报错前的明确提示，交接文档 §7.2）。
#   旧配置/旧测试若仍传这些键，构造期直接 ValueError 并指路 —— 不允许
#   "填了但被静默忽略"（§3.4-D 情形 A 的教训）。
_REMOVED_KEYS: Dict[str, str] = {
    "price_tick": "品种档案 Product.price_tick（P-B 归位，调参=改档案）",
    "multiplier": "品种档案 Product.multiplier（P-B 归位，调参=改档案）",
    "exchange": "品种档案 Product.exchange（P-B 归位）",
    "last_trade_date": "Instrument 运行时身份（行情回填，配置不再持有）",
    "open_fee_rate": "品种档案 Product.open_fee（P-A 归位）",
    "close_fee_rate": "品种档案 Product（平昨≡开仓档，P-A 删第三档）",
    "closetoday_fee_rate": "品种档案 Product.closetoday_fee（P-A 归位）",
    "instrument_verified": "Instrument.verified（Phase 3 已迁运行时）",
    "instrument_source": "Instrument.source（Phase 3 已迁运行时）",
    "max_order_volume": "已删除的死字段（从未被消费，见 test_p50 [8]）",
}


class InstrumentConfig(BaseModel):
    """合约轴的**部署级配置**（P-B · 2026-09-15；原 InstrumentSpec 收缩而来）。

    未知键（拼错字段）直接报错；**frozen=True**：构造后任何字段赋值直接
    ValidationError —— "配置不可变"从 docstring 声明升级为断言级事实。

    ⚠️ 凡"行情 / 成交回报会回填"的值一律属于 `Instrument`（本文件下方），
      不要往这里加。字段位保留但**不消费**的三个档案位（limit_up_pct /
      limit_down_pct / night_session）注释里写明缘由，删除前先过对账测试。
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_symbol: str = "KQ.m@CFFEX.IF"            # 缠论分析用的主连
    trade_symbol: str = "CFFEX.IF2609"              # 初始月份合约（运行时由行情刷新）
    slippage_ticks: float = 1.0                # 单边滑点（tick 数）
    # 报单 advanced 指令（A2，2026-09-11）：一处配置，供所有 insert_order 调用点读取。
    #   "FOK"  全成或全撤 —— 中金所支持，本系统默认依赖它（无部分成交幽灵）
    #   "FAK"  部分成交后撤余量 —— **郑商所只支持 FAK**
    #   实际生效值经 Instrument.effective_order_advanced（CZCE 强制 FAK）。
    order_advanced: str = "FOK"
    closetoday_first: bool = True                   # 今仓成本开关（2026-09-10 更正注释：**不是**
                                                    #   "平仓优先平今"）。实际语义 = 是否允许按持仓
                                                    #   entry_date 把"今仓"判成平今费率；规则 ⑸ 下
                                                    #   OrderIntent.CLOSE 只用于跨日单，正常流程
                                                    #   恒走平昨费率，置 False 可整体关闭今仓判定。
    # 价格笼子band（§5.8.5 D12 落地项，2026-09-12 补字段位）。
    #   含义：限价单相对最新价的**最大偏离点数**；超出即被交易所拒。
    #   ⚠️ **一期不消费** —— 只是把字段位占住，避免二期加价格笼子护栏时又去
    #   改一遍合约模型（届时只需在 Broker 的报单前校验里读它）。
    price_band_points: float = 0.0       # 0 = 不限制（一期的唯一合法值）

    # ── 档案位（字段位保留，**当前不消费**）──
    limit_up_pct: float = 0.0                 # 涨跌停板幅度（%，如 10.0）。仅作档案记录：
                                              #   区间真值是绝对价 upper/lower_limit（随日结算价变），
                                              #   从行情取，写在 Instrument 上。
    limit_down_pct: float = 0.0
    night_session: bool = False               # 是否有夜盘。字段位保留但**不消费**：
                                              #   交易时段护栏（阻塞点 6 · Q5）已于 2026-09-14
                                              #   判定为**不需要**（纯 K 线推送架构下非交易时段
                                              #   无 K 线 → 无信号 → 无报单，无从拦截）。
    #
    # 2026-09-15 P-B 归位/迁出（含去向，详见模块 docstring）：
    #   price_tick / multiplier / exchange → Product（品种轴档案）
    #   last_trade_date → Instrument（运行时身份，行情回填）
    #   2026-09-15 P-A：三档费率字段删除（费率归位品种档案 Fee 两档）。
    #   2026-09-14：max_order_volume 死字段删除（0 消费，防回潮见 test_p50 [8]）。

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
        """被摘除键的**明确报错**（交接文档 §7.2：不允许静默吞掉）。"""
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
# Instrument —— 合约轴的**唯一一份运行时对象**（P-B · 2026-09-15）
#   原 InstrumentSpec（静态身份）+ InstrumentState（运行时状态）合并而成。
#   为什么是普通类而不是 pydantic：它是**可变状态**；pydantic 的
#   model_fields_set / validate_assignment 语义在这里只会重新引入 Phase 3
#   要消掉的那种隐式行为。配置侧的不可变性由 InstrumentConfig.frozen 负责，
#   两侧各司其职。
# ════════════════════════════════════════════════════════════════════
class Instrument:
    """合约运行时对象（P-B 合并）：静态身份转发 + 运行时有效值 + 定价与成本。

    构造：`Instrument(config, product)`
      · config  — InstrumentConfig（frozen 部署配置），缺省 = 默认 IF 配置
      · product — Product 品种档案（tick/乘数/exchange/费率的真值源）；
        None = 未标定品种（有效值取 0 → 定价/对齐 fail-closed；启动期会被
        白名单闸门拒绝，这里允许 None 只为离线探针/测试便利）。

    有效值初值**直接取 Product 档案**（§5.1：播种桥 for_product/_seed_instrument
    已消亡）—— 离线（dry_run/replay）下这就是运行值；实盘由行情
    `apply_quote()` 原子覆盖。

    只有一条改写有效值的路径：apply_quote()（tick/乘数/涨跌停）+
    mark_config_offline()（离线显式声明来源）。
    trade_symbol / last_trade_date 由 SimNow 从**真实月份合约行情**回填。
    """

    # ── 参数来源标记的合法值 ──
    # instrument 参数来源：apply_quote / mark_config_offline 维护，外部只读比较。
    SOURCE_QUOTE: ClassVar[str] = "QUOTE"
    SOURCE_CONFIG_OFFLINE: ClassVar[str] = "CONFIG_OFFLINE"
    # 2026-09-14 评审 P1-3：quote_partial 档（自研/第三方在线通道逃生舱）的来源标记。
    #   tick + 乘数来自行情，但**涨跌停区间缺失** → band 护栏已降级为不校验。
    #   留这个独立标记是为了诊断时能一眼看出"verified 为真但护栏是降级的"。
    SOURCE_QUOTE_PARTIAL: ClassVar[str] = "QUOTE_PARTIAL"

    # apply_quote 回填的行情字段 → 本类字段映射（tqsdk quote 字段名 → 本类字段名）
    _QUOTE_FIELD_MAP: ClassVar[Tuple[Tuple[str, str], ...]] = (
        ("price_tick", "price_tick"),
        ("volume_multiple", "multiplier"),
        ("upper_limit", "upper_limit"),
        ("lower_limit", "lower_limit"),
    )

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
        # —— 有效值：初值直接取品种档案（§5.1；在线路径由行情原子覆盖）——
        self.price_tick: float = float(product.price_tick) if product is not None else 0.0
        self.multiplier: float = float(product.multiplier) if product is not None else 0.0
        # —— 运行时采集：行情 / 离线显式声明写入 ——
        #   当日涨跌停区间（绝对价）。**唯一真值来源是行情**（apply_quote 回填）。
        #     0 = 未知（离线模式未取到）→ 涨跌停护栏对未知区间不校验。
        self.upper_limit: float = 0.0
        self.lower_limit: float = 0.0
        # ── A′ fail-closed 闸门（§5.9.3）──
        # verified: 行情参数（tick/乘数/涨跌停）已取到并通过校验。
        #   实盘（非离线 broker）未置 True → Engine._pre_trade_check 拒单 + 严重告警。
        #   **代码里不得存在"取不到就回退配置值下单"的分支**（p42 用例③ 钉死）。
        self.verified: bool = False
        # source: 当前参数来源标记 —— "QUOTE"（行情，实盘唯一合法来源）
        #   / "QUOTE_PARTIAL"（tick+乘数来自行情，涨跌停护栏降级）
        #   / "CONFIG_OFFLINE"（dry_run/replay 离线兜底）/ ""（尚未定）。
        self.source: str = ""

    # ---------- 兼容视图（P-B）----------
    @property
    def spec(self) -> "Instrument":
        """合并后"静态规格"与"运行时状态"是同一个对象 —— 返回自身。

        历史上 `state.spec.xxx` / `broker.spec.xxx` / `eng.spec.xxx` 的只读
        引用因此继续工作：读到的是运行时有效值（price_tick / trade_symbol /
        last_trade_date）或转发项（signal_symbol / exchange / supports_closetoday
        …），语义比旧版（种子值 vs 有效值两套）更一致。
        新代码请直接用 `instrument.xxx`，不要再写 `.spec`。
        """
        return self

    @property
    def config(self) -> InstrumentConfig:
        """部署级配置（frozen）。新代码读 slippage/order_advanced 等走本属性。"""
        return self._config

    @property
    def product(self) -> Optional["Product"]:
        """品种档案（exchange / Fee 两档 / prefer_closetoday 的真值源）。"""
        return self._product

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

    # —— product 只读转发（P-B：exchange 归品种档案，§5.3）——
    @property
    def exchange(self) -> str:
        """交易所代码（CFFEX/SHFE/INE/DCE/CZCE/GFEX）。真值源 = 品种档案；
        未标定 → ""（保守侧：能力派生恒 False）。"""
        return str(self._product.exchange or "") if self._product is not None else ""

    @property
    def supports_closetoday(self) -> bool:
        """本品种所在交易所是否支持**平今指令**（CLOSETODAY offset）。

        Phase 10（D6 · 2026-09-14）：六家交易所里**只有上期所（SHFE）与
        上期能源（INE）**有 CLOSETODAY 平今指令，其余四家（CFFEX/DCE/CZCE/GFEX）
        传平今会直接报错。平今分支（转移④ + _pre_trade_check 双处消费）
        以本属性为能力守卫 —— "走不走平今"由费率派生（Product.
        prefer_closetoday 单源派生），"能不能走平今"由本属性守卫，
        两者缺一不可，**本闸门永不可删**。

        P-B 起 exchange 真值源 = 品种档案（P-A 已把档案侧同名派生铺好）；
        SimNow 的「月份合约 → exchange」推导降级为对账告警（档案与行情
        推导不一致时 warn，不再写任何对象）。
        无档案 → 一律 False，保守侧：宁可继续锁仓，也不生成会被拒的平今单。
        """
        if self._product is None:
            return False
        return self._product.supports_closetoday

    def __repr__(self) -> str:
        return ("Instrument(trade_symbol={!r}, exchange={!r}, price_tick={!r}, "
                "multiplier={!r}, band=({!r}, {!r}), verified={!r}, source={!r})"
                .format(self.trade_symbol, self.exchange, self.price_tick,
                        self.multiplier, self.lower_limit, self.upper_limit,
                        self.verified, self.source))

    # ---------- 报单属性 / 交割护栏（原 InstrumentSpec 静态判定，P-B 迁入）----------
    def effective_order_advanced(self) -> str:
        """Phase 9（FOK/FAK 按交易所切换）：返回实际报单用的 advanced 属性。

        郑商所（CZCE）是唯一不支持 FOK 的交易所（tqsdk 限价+FOK 仅拒郑商所期货），
        故 CZCE **强制**切 FAK，忽略 `order_advanced` 配置。其余交易所沿用
        配置值（默认 "FOK"）。

        为什么要集中到这里而不是在 Broker/ 里判 exchange：A2 既定"报单属性一处
        配置、所有 insert_order 调用点读取"，加交易所分支也只改这一处，避免
        Broker/ 里散落 `if exchange=="CZCE"`。

        配套约束（见 Engine._decide_action / _open_volume）：CZCE 的 OPEN 手数钉死 1
        —— 单笔 1 手下 FAK ≡ FOK（没有"剩余"可撤），报单填充三态（待报/全成/全撤）
        不变量 4 天然保持，无需扩展状态机、无需补簿。
        """
        if self.exchange == "CZCE":
            return "FAK"
        return self.order_advanced

    def delivery_guard_blocked(self, today: str, threshold_days: int = 1) -> bool:
        """交割月护栏判定（Phase 11 · 阻塞点 4 · D8）。返回 True = 距最后交易日不足
        `threshold_days` 个交易日（含当天）→ 本护栏命中（具体拦哪个动作由 Engine
        按账户三态×意图再判，本方法只回答"是否在禁用窗内"）。

        语义（2026-09-14 拍板）：
          · 判据 =「剩余交易日 **<** threshold_days 即拦」；
          · **护栏对象 = 现行主力 `trade_symbol`** 的 last_trade_date —— 主连换月
            （IF2609→IF2610）时 trade_symbol 更新、判定随之解除；
          · 三态分发在 Engine._pre_trade_check：空仓态拦【开仓】、锁仓态拦【平仓/
            解锁】、运行态不拦；
          · `last_trade_date` 未知（离线 dry_run / 行情未取到）→ 返回 False，
            与涨跌停护栏同哲学（"不校验未知的东西"）。

        天数口径：只数工作日（Mon-Fri），不计法定节假日 —— 节假日需交易日历，
        未引入（一期不消费），文档已注明 N=1 ≈ 仅最后交易日当天拦。

        P-B：last_trade_date 是行情回填的**运行时身份字段**（原挂在 spec 上被
        SimNow 就地改写，§4.2 例 1）—— 合并后本判定读自身字段，归属更直白。
        """
        if not self.last_trade_date:
            return False
        rem = _weekdays_between(today, self.last_trade_date)
        return rem < int(threshold_days)

    # ---------- Phase 8：行情参数回填（A′） ----------
    def apply_quote(self, quote: Any, require_band: bool = True) -> List[str]:
        """从行情 quote 回填合约参数（§5.9.4 项 2 · D20）。返回**值发生变化**的字段名列表。

        纯数据方法：鸭子类型读 quote 的四个字段，**不 import tqsdk**（便于单测）。
        原子性：先对全部待填值校验，任一不过 → 抛 ValueError 且**一个字段都不改**
        （半新半旧的一组参数比全旧更危险）。

        校验清单（§5.9.3，任一不过即 fail）：
          · price_tick / multiplier(volume_multiple)：isfinite 且 > 0
            —— tqsdk 取不到的字段返回 **nan 而不是 None**，nan 是 truthy，
            `if not v` 判空会漏过 nan → 必须 math.isfinite；
          · upper_limit / lower_limit：isfinite 且 > 0，且 lower < upper（区间自洽）；
          · 行情必须是**真实月份合约**的（订阅目标 trade_symbol）—— 由调用方
            （SimNow）保证订阅对象，本方法只验数值。

        require_band（2026-09-14 评审 P1-3，默认 True = 既有语义不变）：
          · True  —— 四字段全强制（strict 档）：涨跌停缺失/不自洽 → ValueError。
          · False —— 只强制 price_tick + volume_multiple（quote_partial 档）：
                    涨跌停**可用且自洽**就照样填；不可用（nan / 0 / 区间不自洽）
                    则**跳过不填**（保留原值 0 = 未知），由 Engine 侧
                    _ref_price_out_of_band 对未知区间自然降级为不校验。
                    调用方（SimNow）必须就此回一条 warn 告警，降级不能静默。
        """
        vals = {}
        for q_attr, s_field in self._QUOTE_FIELD_MAP:
            if not require_band and s_field in ("upper_limit", "lower_limit"):
                # 部分档：band 是"有就填、没有就不填"，先跳过，下面单独处理。
                continue
            try:
                raw = getattr(quote, q_attr)
            except AttributeError:
                raise ValueError("行情缺少字段 {!r}（合约参数校验失败）".format(q_attr))
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise ValueError("行情字段 {}={!r} 不是数值（合约参数校验失败）".format(q_attr, raw))
            if not math.isfinite(v) or v <= 0:
                raise ValueError(
                    "行情字段 {}={!r} 非法（要求 isfinite 且 > 0；tqsdk 取不到时是 nan）"
                    .format(q_attr, raw))
            vals[s_field] = v

        if require_band:
            if vals["lower_limit"] >= vals["upper_limit"]:
                raise ValueError(
                    "涨跌停区间不自洽: lower_limit={!r} >= upper_limit={!r}"
                    .format(vals["lower_limit"], vals["upper_limit"]))
        else:
            # 部分档：band 单独取（允许取不到）。取到就用，取不到/不自洽就整个跳过
            # —— 绝不允许只填 upper 不填 lower（半边区间会让护栏按错误的界校验）。
            try:
                hi = float(getattr(quote, "upper_limit"))
                lo = float(getattr(quote, "lower_limit"))
            except (TypeError, ValueError, AttributeError):
                hi = lo = float("nan")
            if (math.isfinite(hi) and math.isfinite(lo) and hi > 0 and lo > 0
                    and hi > lo):
                vals["upper_limit"], vals["lower_limit"] = hi, lo

        changed = []
        for s_field, v in vals.items():
            old = float(getattr(self, s_field))
            setattr(self, s_field, v)
            if abs(old - v) > 1e-12:
                changed.append(s_field)
        return changed

    def mark_config_offline(self) -> None:
        """离线模式（dry_run/replay）显式降级标记（§5.9.3 规则 2）。

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

    # ---------- 成本（P-A · 2026-09-15：读品种档案 Fee 两档 + 有效乘数，统一在元上算） ----------
    def cost_cash(self, product: "Product", entry_price: float,
                  exit_price: float, closetoday: bool, volume: int = 1) -> float:
        """往返手续费，**元**口径（每手元 × 手数）。

        closetoday=False → 平昨档；True → 平今档。平昨 ≡ 开仓（xlsx 全表没有
        一行把"平昨"单独列出来，见交接文档 §6.4）→ 两种情形都是
        「开仓档成交一笔 + 相应离场档成交一笔」。

        滑点不在此处计（见模块 docstring）。
        ⚠️ 取代已删除的 `cost_points`（点数口径）：per_lot 档（如黄金 10 元/手、
        PTA 3 元/手）在点数口径下无法无损表达（10 ÷ 乘数再乘回，中间还过一次
        浮点），成本记账统一在**元**上做；毛盈亏仍以点记（gross_points），
        净值 = 毛利元 − 成本元（net_cash）。
        """
        open_fee, ct_fee = product.fee_pair()
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


def derive_exchange(symbol: str) -> str:
    """从合约/主连 symbol 推导交易所代码（Phase 8.1 · O-1）。

    支持两种形态（tqsdk 惯例）：
      · 真实月份合约："CFFEX.IF2609" → "CFFEX"、"SHFE.au2608" → "SHFE"
      · 天勤主连：    "KQ.m@CZCE.TA"  → "CZCE"（取 "@" 后段的交易所前缀）
    解析不出（空串 / 不含 "." 分隔）→ 返回 ""（不猜 —— exchange 是 Phase 9
    FOK/FAK 分支的判据，宁缺勿错）。结果统一大写。

    P-B（2026-09-15）：exchange 真值源 = 品种档案（Product.exchange）。
    本函数降级为**对账工具**：SimNow / main 把行情推导值与档案值比对，
    不一致 → warn 告警，**不再写任何对象**（原 spec.exchange = derive_exchange(…)
    的就地写入随 P-B 删除）。
    """
    raw = str(symbol or "").strip()
    if "." not in raw:
        return ""
    s = raw.split("@", 1)[1] if "@" in raw else raw
    head = s.split(".", 1)[0].strip().upper()
    return head
