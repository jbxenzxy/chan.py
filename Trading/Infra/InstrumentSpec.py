# -*- coding: utf-8 -*-
"""
合约规格 / 价格对齐 / 成本模型
==============================
角色定位（2026-09-14 双轴声明）：本模型的静态规格默认值经
TradingConfig.instrument（Trading/Config.py）挂载进配置树，属部署配置的
一部分；与 Config.py 的分区关系（消费层 × 变异维度两把尺子）见该文件
模块 docstring 的「双轴声明」。

静态规格 × 运行时状态（Phase 3 · Fix B · 2026-09-14）
------------------------------------------------------
本文件从此承载**两个生命周期完全不同**的模型，这是刻意的：

  · `InstrumentSpec`（pydantic，**静态**）—— 部署资产 / 领域注册表。
    字段是"启动前就写死、启动后不变"的值：symbol / exchange / slippage /
    order_advanced / 涨跌停幅度 / 最后交易日 等。
    它经 `TradingConfig.instrument` 挂进配置树 —— 正因如此，**它必须只读**：
    TradingConfig 是配置对象，构造后不该有任何字段被行情推着改。

  · `InstrumentState`（普通类，**运行时**）—— 行情 / 成交回报回填的活状态。
    有效价 tick / 乘数、当日涨跌停区间、A′ 的 verified+source 都在这里。
    它还承载**订单定价与成本口径**（round_price / align_* / cost_cash /
    points_to_cash）—— 成本读"有效乘数 × 品种档案费率"，天然属运行时。

  分工改写历史（乱源②）：Phase 8~12 把行情回填的运行时字段直接挂在了
  InstrumentSpec 上（`instrument_verified` / `instrument_source` /
  `upper_limit` / `lower_limit`，以及被 apply_quote 就地覆盖的 price_tick /
  multiplier），于是同一份模型同时是"配置"、"状态容器"和"计算服务"。
  现在拆开：**配置类只存配置，运行时状态持 spec 引用**，Engine 建一份 state
  交给 Broker 写、自己读。

  所有权规则（务必遵守，否则 A′ fail-closed 会失效）：
      一次运行**只有一份** InstrumentState —— main.py 建好后同时交给
      `Broker.build_broker(..., state=state)` 与 `TradingEngine(..., state=state)`。
      Broker 写（apply_quote），Engine 读（闸门 / 漂移对账 / 成本）。
      若两边各建一份，SimNow 置的 verified 引擎永远看不见
      → 闸门恒拒单（且看不出原因）。

v1 的合约映射用**配置表**（Trading/Config.py 的 instrument.trade_symbol）。
M2 接 tqsdk 后换成 `quote.underlying_symbol` 动态解析，接口不变——
这是刻意留的替换点，不要把这层的调用散到引擎里。

成本口径（重要，P-A · 2026-09-15 改"元"）
    - 费率真值源 = 品种档案 ProductProfile 的 Fee 两档（开仓 / 平今），静态、启动即确定；
    - 手续费按 `Fee.cash(price, multiplier)` 折算为**每手元**：rate 档 = 万分比 × 价 × 乘数，
      per_lot 档 = 固定元/手 —— "点数"口径无法表达 per_lot 档（10 元/手 ÷ 乘数再乘回，
      中间还过一次浮点），故成本记账**统一在元上做**（cost_cash）；
    - 盈亏毛值仍以"点"记账（gross_points），净值为元（net_cash = 毛利元 − 成本元）；
    - 滑点**不计入** cost_cash，而是体现在成交价上（见 dry_run broker 的让价）
      否则同一笔滑点会被算两次，回测虚高、实盘对不上
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


class InstrumentSpec(BaseModel):
    """合约规格 · **静态**部分（2026-09-07 改为 pydantic；2026-09-14 Phase 3 拆出运行时状态）。

    未知键（拼错字段）直接报错，缺字段用下面的默认值 —— 合约规格的默认值
    只在本模型维护。

    ⚠️ 本模型是 `TradingConfig.instrument`，**构造后必须只读**。
      凡"行情 / 成交回报会回填"的值一律属于 `InstrumentState`，不要往这里加。
      唯一例外是离线兜底种子（price_tick / multiplier）：
      它们在**构造时**作种子，之后由 state 持有有效值，本模型自身不再变。

    2026-09-15 P-A 删除三档费率字段（open/close/closetoday_fee_rate）：
      费率真值源 = 券商费率表 → 归位到品种档案 ProductProfile 的 Fee 两档
      （静态、启动即确定），不再经"成交回报反推 / TqSim 查询"运行时通道，
      也不再需要 fee_source 来源标记 —— "取不到费率"这个状态彻底消失。
    """
    model_config = ConfigDict(extra="forbid")

    signal_symbol: str = "KQ.m@CFFEX.IF"            # 缠论分析用的主连
    trade_symbol: str = "CFFEX.IF2609"              # 实际下单的月份合约
    # —— 离线兜底种子（在线路径由行情原子覆盖，见 InstrumentState.apply_quote）——
    price_tick: float = 0.2                    # IF 最小变动价位
    multiplier: float = 300.0                  # 合约乘数（元/点）
    slippage_ticks: float = 1.0                # 单边滑点（tick 数）
    # 报单 advanced 指令（A2，2026-09-11）：一处配置，供所有 insert_order 调用点读取。
    #   "FOK"  全成或全撤 —— 中金所支持，本系统默认依赖它（无部分成交幽灵）
    #   "FAK"  部分成交后撤余量 —— **郑商所只支持 FAK**，二期上 CZCE 必须切这个
    #   二期按交易所切换时改这一个字段即可，不要把值写死在 Broker/ 里。
    order_advanced: str = "FOK"
    closetoday_first: bool = True                   # 今仓成本开关（2026-09-10 更正注释：**不是**
                                                    #   "平仓优先平今"）。实际语义 = 是否允许按持仓
                                                    #   entry_date 把"今仓"判成平今费率；规则 ⑸ 下
                                                    #   OrderIntent.CLOSE 只用于跨日单，正常流程
                                                    #   恒走平昨费率，置 False 可整体关闭今仓判定。
    # 价格笼子band（§5.8.5 D12 落地项，2026-09-12 补字段位）。
    #   含义：限价单相对最新价的**最大偏离点数**；超出即被交易所拒（中金所的
    #   "价格保护带"、上期所的"涨跌停/限价距离"都归这一类）。
    #   ⚠️ **一期不消费** —— 只是把字段位占住，避免二期加价格笼子护栏时又去
    #   改一遍合约规格模型（届时只需在 Broker 的报单前校验里读它）。
    price_band_points: float = 0.0       # 0 = 不限制（一期的唯一合法值）

    # ════════════════════════════════════════════════════════════════
    # Phase 8（二期 · D20）：品种参数自动获取的字段位（§5.6 六个缺字段在此兑现）
    # ════════════════════════════════════════════════════════════════
    exchange: str = ""                        # 交易所：CFFEX/SHFE/INE/DCE/CZCE/GFEX。
                                              #   A5 预留的字段位在此兑现；Phase 9（FOK/FAK
                                              #   切换）正式消费，Phase 8 只填充不分支。
    # 2026-09-14 删除：原 `max_order_volume` 字段（"交易所单笔报单上限（手）"）自 Phase 8
    #   加入起**从未被任何代码消费**（全仓只有字段定义一处，0 处读取）。单笔手数的唯一
    #   来源是 risk.max_volume（Config.py → Engine.lots_per_signal → Engine._open_volume），
    #   CZCE 钉 1 手则按交易所直接硬编码在 Engine._open_volume 里 —— 与本字段无关。
    #   死字段留着会误导（看起来像"手数上限在这里配"），故删除。
    #   Q9（中金所限价单单笔上限究竟是 20 还是 5000）**仍未决**；将来真要落地该校验时，
    #   按当时的真实需求重新设计（很可能是"每交易所/每品种的上限表"），**不要**靠恢复
    #   这个字段了事。防回潮断言见 Trading/Test/test_p50_review_fixes.py [8]。
    limit_up_pct: float = 0.0                 # 涨跌停板幅度（%，如 10.0）。仅作档案记录：
                                              #   区间真值是绝对价 upper/lower_limit（随日结算价变），
                                              #   从行情取，见 InstrumentState（Phase 3 迁出）。
    limit_down_pct: float = 0.0
    last_trade_date: str = ""                 # 最后交易日 YYYY-MM-DD（阻塞点 4 交割月护栏，Phase 11 消费）
    night_session: bool = False               # 是否有夜盘。字段位保留但**不消费**：
                                              #   交易时段护栏（阻塞点 6 · Q5）已于 2026-09-14
                                              #   判定为**不需要**（纯 K 线推送架构下非交易时段
                                              #   无 K 线 → 无信号 → 无报单，无从拦截）。
    #
    # 2026-09-14 Phase 3 迁出（→ InstrumentState）：
    #   `upper_limit` / `lower_limit`（当日涨跌停绝对价，唯一真值来源是行情）、
    #   `instrument_verified` / `instrument_source`（A′ fail-closed 闸门与来源标记）。
    #   它们是**运行时状态**，不是配置。
    #   2026-09-15 P-A：三档费率字段与 fee_source 直接删除（费率归位品种档案）。

    # ════════════════════════════════════════════════════════════════
    # 品种档案显式播种（Phase 3 · Fix B · 2026-09-14）
    #   取代原 Config.py 的 `model_validator(mode="after")` + `model_fields_set` 注入：
    #   那一套是"模型构造的副作用"（看不见、要读源码才知道字段被改了），
    #   现在播种是一条**看得见的调用**，落在启动路径上（main.py）。
    # ════════════════════════════════════════════════════════════════
    @classmethod
    def for_product(cls, profile: Optional["ProductProfile"],
                    **overrides: Any) -> "InstrumentSpec":
        """按**品种档案**播种一份合约规格（唯一默认值来源 = 档案）。

        播种规则（一次成型，无 model_fields_set 判据，无 force 双语义）：

          · `profile` 非空 → `price_tick` / `multiplier` **强制**取档案值。
            这两个字段随品种变，档案是它们唯一的真值来源；显式传进来的
            `price_tick=` / `multiplier=` 会被忽略。这是刻意的 —— 与 Fix A
            把 r_multiple_tp 从 ExitConfig 删掉、只留档案件是**同一个决策**（D1：放弃 .env 覆盖
            品种相关参数的能力；调参 = 改档案 = git 评审 + 对账测试守护）。

          · 其余字段逐项取 `overrides`（signal_symbol / trade_symbol / exchange /
            slippage_ticks / order_advanced / last_trade_date …），
            缺省回落到模型默认值。调用方通常传
            `**cfg.instrument.model_dump(exclude={"price_tick", "multiplier"})`
            把用户已经配好的静态项带过来。

          · `profile` 为空（未标定品种）→ **不播种**，只用 overrides 构造。
            离线场景仍可启动；引擎侧白名单闸门（ProductProfile.assert_product_allowed）
            与 resolved_exit_params() 会在启动期拒绝并给出完整文案。
        """
        d = {k: v for k, v in overrides.items()
             if k not in ("price_tick", "multiplier")}
        if profile is not None:
            d["price_tick"] = profile.price_tick
            d["multiplier"] = profile.multiplier
        return cls(**d)

    # ---------- 换日 ----------
    def is_new_day(self, prev_date: str, cur_date: str) -> bool:
        """两个日期串是否跨自然日（纯静态判定，不读任何运行时状态）。"""
        return (prev_date or "")[:10] != (cur_date or "")[:10]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstrumentSpec":
        """从配置 dict 构造（未知键报错，缺键用模型默认值）。"""
        return cls(**(d or {}))

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def effective_order_advanced(self) -> str:
        """Phase 9（FOK/FAK 按交易所切换）：返回实际报单用的 advanced 属性。

        郑商所（CZCE）是唯一不支持 FOK 的交易所（tqsdk 限价+FOK 仅拒郑商所期货），
        故 CZCE **强制**切 FAK，忽略 `order_advanced` 配置。其余交易所沿用
        `order_advanced`（默认 "FOK"）。

        为什么要集中到 spec 而不是在 Broker/ 里判 exchange：A2 既定"报单属性一处
        配置、所有 insert_order 调用点读取"，加交易所分支也只改这一处，避免
        Broker/ 里散落 `if exchange=="CZCE"`。

        配套约束（见 Engine._decide_action / _open_volume）：CZCE 的 OPEN 手数钉死 1
        —— 单笔 1 手下 FAK ≡ FOK（没有"剩余"可撤），报单填充三态（待报/全成/全撤）
        不变量 4 天然保持，无需扩展状态机、无需补簿。

        （Phase 3：**留 spec** —— 纯静态判定，读 exchange / order_advanced 两个
          启动后不变的字段。）
        """
        if self.exchange == "CZCE":
            return "FAK"
        return self.order_advanced

    @property
    def supports_closetoday(self) -> bool:
        """本品种所在交易所是否支持**平今指令**（CLOSETODAY offset）。

        Phase 10（D6 · 2026-09-14）：六家交易所里**只有上期所（SHFE）与
        上期能源（INE）**有 CLOSETODAY 平今指令，其余四家（CFFEX/DCE/CZCE/GFEX）
        传平今会直接报错。平今分支（转移④ + _pre_trade_check 双处消费）
        以本属性为唯一守卫 —— "走不走平今"由费率派生（P-A 起在
        ProductProfile.prefer_closetoday 单源派生），"能不能走平今"由本属性守卫，
        两者缺一不可，**本闸门永不可删**。

        exchange 可能为 ""（离线配置未填充 / Phase 8 之前）→ 一律 False，
        保守侧：宁可继续锁仓，也不生成一张会被拒的平今单。

        （Phase 3：**留 spec** —— 纯静态判定。）
        """
        return str(self.exchange or "").upper() in ("SHFE", "INE")

    def delivery_guard_blocked(self, today: str, threshold_days: int = 1) -> bool:
        """交割月护栏判定（Phase 11 · 阻塞点 4 · D8）。返回 True = 距最后交易日不足
        `threshold_days` 个交易日（含当天）→ 本护栏命中（具体拦哪个动作由 Engine
        按账户三态×意图再判，本方法只回答"是否在禁用窗内"）。

        语义（2026-09-14 拍板，见实施计划 §6.2 Phase 11 行）：
          · 判据 =「剩余交易日 **<** threshold_days 即拦」；
          · **护栏对象 = 现行主力 `trade_symbol`** 的 last_trade_date —— 主连换月
            （IF2609→IF2610）时 trade_symbol 更新、判定随之解除；
          · 三态分发在 Engine._pre_trade_check：空仓态拦【开仓】、锁仓态拦【平仓/
            解锁】、运行态不拦（Principle：交割月附近不让新进裸仓，也不让解锁成
            裸仓，已运行仓位可正常交易/锁仓）；
          · `last_trade_date` 未知（离线 dry_run / 行情未取到）→ 返回 False，
            与涨跌停护栏同哲学（"不校验未知的东西"）。

        天数口径：只数工作日（Mon-Fri），不计法定节假日 —— 节假日需交易日历，
        未引入（一期不消费），文档已注明 N=1 ≈ 仅最后交易日当天拦。

        例：today=2026-09-18、last_trade_date=2026-09-18 → 剩 0 日 < 1 → 拦；
            today=2026-09-17（=N 前一天）→ 剩 1 日 < 1 不拦 → 正常可开。

        （Phase 3：**留 spec** —— last_trade_date 是行情回填的静态元数据
          （换月前不变），判定本身不依赖任何有效值。）
        """
        if not self.last_trade_date:
            return False
        rem = _weekdays_between(today, self.last_trade_date)
        return rem < int(threshold_days)


# ════════════════════════════════════════════════════════════════════
# InstrumentState —— 合约规格的**运行时状态**（Phase 3 · Fix B · 2026-09-14）
#   为什么是普通类而不是 pydantic：它是**可变状态**，且与 spec 是"种子 + 视图"
#   关系；pydantic 的 model_fields_set / validate_assignment 语义在这里只会
#   重新引入 Phase 3 要消掉的那种隐式行为。
#   为什么不从 spec 继承：两者生命周期正交（配置 vs 会话运行期）。持有引用
#   比继承更能表达"静态项归 spec、有效值归 state"。
# ════════════════════════════════════════════════════════════════════
class InstrumentState:
    """合约规格的运行时状态（Phase 3 · Fix B）：有效值 + 行情/回报来源标记 + 定价与成本。

    构造：`InstrumentState(spec)` —— 从静态规格**播种**有效值
    （price_tick / multiplier）。之后只有一条路径能改写它：
      · 行情路径：`apply_quote()`（tick / 乘数 / 涨跌停）
      · 离线显式声明：`mark_config_offline()`
      （2026-09-15 P-A 删除费率路径：费率归位品种档案，无运行时回填）

    归属判据（本类字段 vs InstrumentSpec 字段）：
      · 启动后**会变** → 本类（有效 tick/乘数、涨跌停、verified/source）
      · 启动后**不变** → InstrumentSpec（symbol / exchange / slippage / order_advanced /
        closetoday_first / 涨跌停幅度 / last_trade_date / 离线种子）

    定价与成本（round_price / align_* / slip_price / cost_cash / points_to_cash）
    也放在本类：成本读"有效乘数 × 品种档案费率"，属运行时语义。
    """

    # ── 参数来源标记的合法值 ──
    # instrument 参数来源：apply_quote / mark_config_offline 维护，外部只读比较。
    SOURCE_QUOTE: ClassVar[str] = "QUOTE"
    SOURCE_CONFIG_OFFLINE: ClassVar[str] = "CONFIG_OFFLINE"
    # 2026-09-14 评审 P1-3：quote_partial 档（自研/第三方在线通道逃生舱）的来源标记。
    #   tick + 乘数来自行情，但**涨跌停区间缺失** → band 护栏已降级为不校验。
    #   留这个独立标记是为了诊断时能一眼看出"verified 为真但护栏是降级的"。
    SOURCE_QUOTE_PARTIAL: ClassVar[str] = "QUOTE_PARTIAL"

    # apply_quote 回填的行情字段 → state 字段映射（tqsdk quote 字段名 → 本类字段名）
    _QUOTE_FIELD_MAP: ClassVar[Tuple[Tuple[str, str], ...]] = (
        ("price_tick", "price_tick"),
        ("volume_multiple", "multiplier"),
        ("upper_limit", "upper_limit"),
        ("lower_limit", "lower_limit"),
    )

    def __init__(self, spec: InstrumentSpec):
        self.spec: InstrumentSpec = spec
        # —— 有效值：构造时从静态规格播种（离线兜底；在线路径由行情原子覆盖）——
        #   （2026-09-15 P-A：费率不再经 state —— 成本计算直接读品种档案 Fee 两档）
        self.price_tick: float = float(spec.price_tick)
        self.multiplier: float = float(spec.multiplier)
        # —— 运行时采集：行情 / 离线显式声明写入 ——
        #   当日涨跌停区间（绝对价）。**唯一真值来源是行情**（apply_quote 回填），
        #     配置里的 limit_up_pct/limit_down_pct 只是档案，换算不出当日绝对价。
        #     0 = 未知（离线模式未取到）→ 涨跌停护栏对未知区间不校验（不校验未知的东西）。
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

    def __repr__(self) -> str:
        return ("InstrumentState(trade_symbol={!r}, price_tick={!r}, multiplier={!r}, "
                "band=({!r}, {!r}), verified={!r}, source={!r})"
                .format(self.spec.trade_symbol, self.price_tick, self.multiplier,
                        self.lower_limit, self.upper_limit,
                        self.verified, self.source))

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

        配置值只允许在离线模式生效，且必须能自证来源 —— 启动横幅 + WARN
        写明"tick/乘数取自配置，回测结果不可直接外推实盘"由调用方（main.py）负责。
        """
        self.source = self.SOURCE_CONFIG_OFFLINE

    # 2026-09-15 P-A 删除（Phase 12 三档费率回填 + 平今经济性判定三件套，共 102 行）：
    #   apply_fee_rates / mark_fee_config / evaluate_closetoday_economy / SOURCE_FEE_*。
    #   费率真值源 = 品种档案 ProductProfile 的 Fee 两档（静态）；平今取舍 =
    #   ProductProfile.prefer_closetoday（3× 口径纯派生）。
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

        滑点**幅度**是静态项（spec.slippage_ticks，不随品种变），
        换算用的 tick 是**有效值**（本类）—— 所以本方法属运行时语义。
        """
        tick = self.spec.slippage_ticks * self.price_tick
        return price + side_sign * tick if for_open else price - side_sign * tick

    # ---------- 成本（P-A · 2026-09-15：读品种档案 Fee 两档 + 有效乘数，统一在元上算） ----------
    def cost_cash(self, product: "ProductProfile", entry_price: float,
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
    """
    raw = str(symbol or "").strip()
    if "." not in raw:
        return ""
    s = raw.split("@", 1)[1] if "@" in raw else raw
    head = s.split(".", 1)[0].strip().upper()
    return head
