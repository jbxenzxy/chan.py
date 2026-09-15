# -*- coding: utf-8 -*-
"""
Trading/Config.py —— 自动下单配置的**唯一总入口**（SSOT = Single Source Of Truth）
=================================================================================
2026-09-08 配置层按「六层架构」重排：
    · 顶层根配置 `TradingConfig` 置于文件最前（它是整个模块的根，其余各层 section
      模型都是它的嵌套字段）；其各层字段按下层顺序组织：
        ① 信号源层     → SourceConfig
        ② 信号适配层   → （无独立配置模型，仅解析/去重行为）
        ③ 策略层       → Entry/Exit 参数（单一策略，无选择器）
        ④ 风控层       → RiskConfig（开仓手数/持仓上限/补开开关）
        ⑤ 执行层       → （无独立配置模型，状态机/对账行为）
        ⑥ Broker 适配器层 → BrokerConfig
    · 周期只作时间语义（freq → bar_secs），收口在 Infra/Period.py 的
      FREQ_SEC / PERIOD_PROFILES；品种相关的出场参数（r_multiple_tp，Fix A 单源化 + D1
      拍板）收口在 Infra/Product.py，品种无关项（breakeven_* / ATR / trailing）
      在本文件 ExitConfig 调；
      经本文件 resolved_exit_params() 合并成 LayeredExitPolicy 的完整参数。
    · 本文件**不含任何"构造后改字段"的副作用**（Phase 3 · Fix B · 2026-09-14；
      P-B · 2026-09-15 起配置类 frozen=True）：
      品种播种机制已消亡（P-B 删 for_product/_seed_instrument）—— tick/乘数/
      exchange/费率真值源 = 品种档案 Product，运行时对象 `Instrument`
      构造时直接取档案初值（见 Infra/Instrument.py）；
      合约参数的运行时状态（有效 tick/乘数、涨跌停区间、A′ verified、
      trade_symbol/last_trade_date 回填）收口在 `Instrument`，**不在本配置树上**。

2026-09-07 配置层归一：删掉 config.json / config_example.json 这条配置路径，
原来的 Trading/Infra/Config.py（dataclass + 裸 dict）上移并重写为本文件。

命名约定（2026-09-08）
------------------------------------------------------------------
    · 顶层根配置类 `TradingConfig`（模块由 trade_gateway/ 更名为 Trading/ 后同步改名）。
    · 引擎类 `TradingEngine`（同步改名）。
    · ⑥ 层配置类原名 `BrokerConfig` —— 与 ① 层 `SourceConfig` / ④ 层 `RiskConfig`
      的命名习惯不一致（那两层都叫 `*Config` 而不是 `*ParamsConfig`），故改为 `BrokerConfig`，
      与六层架构的「每层一个 *Config」约定对齐。字段名 `broker_params` 不变。
    · 凡引用处一律更新；旧名 `Gateway*` / `BrokerConfig` 不再存在于代码中。

策略层（2026-09-08 精简：取消策略选择器抽象）
------------------------------------------------------------------
    ③ 策略层**没有「策略选择」这一层**。生产环境入场只有一个策略 `EntryPolicy`、
    出场只有一个策略 `LayeredExitPolicy`（L1-L3 分层出场，2026-09-08 已删 L4），
    用户明确不会增加第二种，因此不再需要 `name` 字段、注册表、或 build_*_policy 路由。

    配置直接持有参数模型：
      · `entry_params: EntryConfig`   —— EntryPolicy 的可调数值（默认值唯一来源）
      · `exit_params: ExitConfig`     —— LayeredExitPolicy（L1-L3）的可调数值

    main.py 直接实例化，不再经选择器：
        entry = EntryPolicy(cfg.entry_params.model_dump())
        exitp = LayeredExitPolicy(resolved_exit_params(cfg))
    原 `DefaultExitPolicy`（简单固定点数出场）已删除——它是可选的「第二种」出场策略，
    生产从不选用，保留它只会让配置与测试多一套无用的选择分支。

双轴声明（2026-09-14 配置架构评审定稿）
------------------------------------------------------------------
    Trading 侧的配置存在**两把正交的分区尺子**，各管各的数据，不互相搬家：
      · 本文件按**消费层**分区（①③④⑥ 各层 *Config）—— 是**部署配置入口**：
        环境变量 / 命令行 / .env 可覆盖的运维参数。
      · Infra/Period.py / Infra/Product.py 按**变异维度**分区
        （随周期变 / 随品种变）—— 是**领域注册表**：凭交易经验标定的代码资产，
        进 git 评审 + 对账测试守护，不走 env 覆盖。
    两轴正交：品种/周期档案**不按消费层归入**本文件 —— 一张档案表里一行供
      ③策略层、一行供⑤执行层、一行供⑥Broker 层，无法唯一归属；且 App/AppTrader
      只 import 档案的纯函数（白名单闸门），挪进来会反向拉起整个 TradingConfig。
      `TradingConfig.period_profile / product_profile` property 是「入口聚合
      档案」的唯一形态。
    Infra/Instrument.py 的**部署级配置** `InstrumentConfig`（frozen=True）
      经 `TradingConfig.instrument` 字段挂载，属本配置树的一部分（角色定位见其
      模块 docstring）；同一文件里的 `Instrument`（唯一运行时对象）**不在**本
      配置树上 —— 它由 main.py 构造并注入 Engine / Broker（P-B · 2026-09-15）。

设计约定（与项目主线对齐：根 ChanConfig.py + App/AppConfig.py）
------------------------------------------------------------------
优先级（高 → 低）
    1. main.py 命令行参数（--symbol / --freq / --broker / --source ...）
    2. 环境变量 / 仓库根 `.env`
        前缀 `TRADING_`，嵌套用双下划线，如：
            TRADING_SOURCE__FREQ=15s
            TRADING_RISK__MAX_VOLUME=3
            TRADING_SIZING__ENABLED=true
    3. 本文件各模型字段的默认值 —— **默认值的唯一来源，别处不再写第二套**

严格模式（用户拍板：不要兜底）
------------------------------------------------------------------
    · 每个 section 都是 `extra="forbid"` 的 pydantic 模型：
      传入未知键、缺字段，一律立即抛异常（启动期 fail-fast），
      不再有组件内 `p.get(key, 兜底值)` 那种"第二套默认值"。
    · 组件（EntryPolicy / LayeredExitPolicy / SimNow / Broker）只接受配置模型
      或经模型校验的 dict，传错直接报错 —— 缺键/拼错在配置阶段就暴露，
      绝不带错误默认值悄悄跑。

凭据（安全）
------------------------------------------------------------------
    本文件**不落任何账号密码**（它入库）。账户信息一律走环境变量或仓库根 `.env`：
        SN_ACCOUNT / SN_PASSWORD         SimNow 仿真账号
        TQ_ACCOUNT / TQ_PASSWORD         天勤账号（两种模式共用）
        LIVE_ACCOUNT / LIVE_PASSWORD     实盘资金账号密码
    读取逻辑见 Broker/SimNow.py `_cred()`（env 优先；本文件没有这些字段）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import (BaseModel, ConfigDict, Field, field_validator,
                      model_validator)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .Infra.Instrument import InstrumentConfig
from .Infra.Period import PERIOD_PROFILES, Period
from .Infra.Product import PRODUCT_PROFILES, Product, describe_unknown_product, parse_product_key

__all__ = [
    # 顶层根配置（横切·基础设施）—— 置于最前，是整个配置树的根
    "TradingConfig", "default_config", "DEFAULT_CONFIG",
    # 出场参数合并点（Fix A：品种档案 + 品种无关项 → LayeredExitPolicy 完整参数）
    "resolved_exit_params",
    # 各层 section 模型（按下层顺序声明，见文件内 banner）
    "SourceConfig",
    "EntryConfig", "ExitConfig", "ExitPolicyParams",
    "RiskConfig",
    "BrokerConfig",
    "ChannelTimingConfig",
    "EngineConfig",
]

# 仓库根（Trading/ 的上一级）—— 与 App/AppConfig.py 同一份 .env
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_REPO_ROOT, ".env")
_log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 横切 · 基础设施（Infra）配置 —— 顶层根配置（置于最前）
#    字段按下层顺序组织：①信号源 → ③策略 → ④风控 → ⑥Broker → 横切三件套(broker/state_dir/instrument)。
#    嵌套字段的 default_factory 用 lambda 延迟绑定：本类定义在各 section 模型之前，
#    lambda 在实例化（TradingConfig()）时才解析，彼时所有类均已就绪（配合文件顶部
#    `from __future__ import annotations`，注解已是字符串、不会在定义期强求值）。
# ════════════════════════════════════════════════════════════════════
class TradingConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADING_",           # TRADING_BROKER / TRADING_RISK__MAX_VOLUME ...
        env_nested_delimiter="__",
        env_file=_ENV_FILE,              # 仓库根 .env（与 App/AppConfig.py 同一份）
        env_file_encoding="utf-8",
        extra="forbid",                  # 未知键（init kwargs / 配置 dict）直接报错
        protected_namespaces=(),
    )

    # —— ① 信号源层 ——
    source: SourceConfig = Field(default_factory=lambda: SourceConfig())
    # —— ③ 策略层（单一策略，无选择器）——
    entry_params: EntryConfig = Field(default_factory=lambda: EntryConfig())
    exit_params: ExitConfig = Field(default_factory=lambda: ExitConfig())
    # —— ④ 风控层 ——
    risk: RiskConfig = Field(default_factory=lambda: RiskConfig())
    # —— ⑥ Broker 适配器层 ——
    broker_params: BrokerConfig = Field(default_factory=lambda: BrokerConfig())
    # —— ⑦ 引擎时序参数（Step 2.2 归一：原 Engine.__init__ 硬编码常量收口到此）——
    engine: EngineConfig = Field(default_factory=lambda: EngineConfig())
    # —— 横切·基础设施三件套 ——
    broker: str = "dry_run"              # dry_run 离线模拟 / simnow 仿真 / live 实盘 CTP
    state_dir: str = "./State"           # 运行时状态目录（state.db / events.jsonl / orders.jsonl）
    # P-B（2026-09-15）：InstrumentSpec → InstrumentConfig（frozen=True）。
    #   旧配置若仍带 price_tick / multiplier / exchange / last_trade_date 键，
    #   构造期 ValueError 并指路（见 InstrumentConfig._REMOVED_KEYS）——
    #   显式报错而不是静默忽略（交接文档 §7.2）。
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TradingConfig":
        """从完整/部分配置 dict 构造（缺的键用模型默认值补齐，多出来的键报错）。"""
        return cls(**(d or {}))

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def redacted_dict(self) -> Dict[str, Any]:
        """对外展示/日志用快照（对齐 AppConfig.as_dict(redact=True) 的脱敏约定）。

        本文件不存凭据，redact 目前是空操作；保留接口是为了将来若引入敏感字段，
        调用方不必改代码。
        """
        return self.model_dump()

    @property
    def period_profile(self) -> Optional["Period"]:
        """当前 source.freq 对应的周期档案（只读视图；未知 freq 返回 None）。"""
        return PERIOD_PROFILES.get(self.source.freq)

    # ── 品种档案：只读视图（Phase 3 · Fix B · 2026-09-14 起不再有任何注入机制）──
    # Phase 2 之前这里是一整套"注入"逻辑（model_validator + _apply_product_profile_values
    #   + apply_product_profile，含 model_fields_set 的 user-explicit-wins 判据与
    #   --symbol 换品种的 force 双语义）。它的副作用是：TradingConfig 一构造就会
    #   改写 instrument 的字段 —— 配置对象因此**构造后可变**，且"哪些字段会被改"
    #   只能靠读源码得知。
    #
    # Phase 3 把整条通道删掉，换成两个显式、无状态的形态：
    #   · 播种（一次性、构造时）：`InstrumentSpec.for_product(profile, **overrides)`
    #     —— 唯一默认值来源是品种档案；启动路径上看得见地调用（main.py `_seed_instrument`）。
    #   · 读取（随时、只读）：本 property —— 引擎白名单闸门 / resolved_exit_params /
    #     _check_spec_drift 都只用它查档案，不再有任何写回。
    @property
    def product_profile(self) -> Optional["Product"]:
        """当前 instrument.signal_symbol 对应的品种档案（只读视图；未知品种返回 None）。

        2026-09-14 评审 P1-1：与播种路径同源用 parse_product_key（剥合约月份），
        否则 `--symbol CFFEX.IF2609` 时这里返回 None，而播种那边（改用 key 后）
        能命中 → 「参数播种了但 product_profile 查不到」的自相矛盾状态，
        连带 _check_spec_drift 的漂移校验被静默跳过。
        """
        return PRODUCT_PROFILES.get(parse_product_key(self.instrument.signal_symbol))


# ════════════════════════════════════════════════════════════════════
# ① 信号源层（Signal Source）配置
#    行情来源 / 周期 / 信号新鲜度过滤。全部字段与「周期选择」相关，
#    但语义上属于「信号源」这一层；其中 freq 是周期选择项（bar_secs 见
#    Infra/Period.py）；signal_k_tol_bars 是「按 K 线相对根数」
#    的容差、**不随周期改变**，属非周期敏感项。
# ════════════════════════════════════════════════════════════════════
class SourceConfig(BaseModel):
    """行情源：replay 回放本地 K 线 / sse 实时订阅。"""
    model_config = ConfigDict(extra="forbid")

    type: str = "replay"                      # replay=回放（离线测试）；sse=实时订阅
    replay_dir: str = "./replay_data"         # replay 模式的 K 线数据目录
    sse_base: str = "http://127.0.0.1:18081"  # sse 模式的行情服务地址
    symbol: str = "KQ.m@CFFEX.IF"             # 订阅合约（与 instrument.signal_symbol 一致）
    freq: str = "5m"                          # K 线周期（周期只做字符串透传，不参与分钟换算）
    speed: float = 0.0                   # replay 每根 K 线间隔秒数（0=尽快）
    bar_mode: str = "confirmed"               # confirmed=只取已闭合 K 线；last=含未闭合
    only_alive: bool = False                  # 只处理存活（未到期）合约
    # 信号新鲜度过滤 —— K 线位置口径（2026-09-08 取代原 signal_max_age_minutes）：
    #   chan.py SSE 首连会 replay 一批历史 bsp。每个买卖点信号的 timestamp =
    #   它所在分型右肩 K 的时间戳；快照最后一根 K 即「当前最新 K」。
    #   这里按「信号归属K 距最新K 的根数」判新旧：距最新 K > N 根 → 视为历史
    #   残留丢弃。N 是「距最终K的相对根数」，**不随周期改变**（非周期敏感项）。
    #   0 = 必须正好是最右一根 K 才处理。
    #   字段名说明：tol = tolerance 的缩写（容差 / 容错范围）。
    signal_k_tol_bars: int = 1                # N：信号归属K 距最新K 的容差根数（tol=tolerance 容差）

    @field_validator("signal_k_tol_bars")
    @classmethod
    def _check_signal_k_tol(cls, v: int) -> int:
        """容差根数必须 >= 0（0=严格只认最新一根 K），负值无意义，构造期 fail-fast。"""
        if v < 0:
            raise ValueError(
                "source.signal_k_tol_bars 不能为负（当前={}）；0=必须最右一根 K".format(v))
        return v

    # SSE 重连三参数（Step 2.5 收口，唯一事实源）：
    #   等待 = min(reconnect_wait × 连续失败次数, reconnect_wait_max) 线性退避。
    #   max_retry=0 表示无限重连；>0 时超过次数抛异常退出（由上层决定重启策略）。
    reconnect_wait: float = 5.0          # 重连基础间隔秒（×失败次数退避）
    reconnect_wait_max: float = 60.0     # 重连单次等待上限秒
    reconnect_max_retry: int = 0              # 最大重连次数，0=无限


# ════════════════════════════════════════════════════════════════════
# ③ 策略层（Strategy）配置
#    入场 / 出场各只有一个策略（EntryPolicy / LayeredExitPolicy），
#    配置层不再有「策略选择」抽象——直接持有参数模型 entry_params / exit_params。
# ════════════════════════════════════════════════════════════════════
class EntryConfig(BaseModel):
    """EntryPolicy 参数（唯一入场策略，在此加字段，别处不再写默认值）。"""
    model_config = ConfigDict(extra="forbid")

    reverse_on_opposite_signal: bool = False  # 持仓时出现反向信号是否反手


class ExitConfig(BaseModel):
    """出场参数（单一模型，LayeredExitPolicy 即 L1-L3 分层出场，2026-09-08 精简）。

    LayeredExitPolicy（L1-L3 分层出场）：L1 R 倍数定基线 → L2 ATR 定宽窄 →
    L3 保本/跟踪锁利。原 L4 时间/收盘兜底已删除（含引擎侧收盘前强平）。
    R 的产生见 Strategy/Exit.py：R = max(A, 2×ATR)，其中
      A = 结构止损（分型极值距离），2×ATR = 波动率止损（自适应下限，无绝对点数地板）。

    （2026-09-08：原独立的 DefaultExitParamsConfig 已并入本模型，统一为单一出场参数模型；
     可选的第二套出场 DefaultExitPolicy 一并删除——生产只用 L1-L3，不再保留无用选择分支。）

    品种相关字段单源化（Fix A · 2026-09-14 · D1 拍板）：r_multiple_tp **不在本模型** —— 它随品种变，
    唯一默认值来源是 Infra/Product.py 的品种档案；.env / 环境变量
    的覆盖能力已放弃（extra=forbid 下带旧键构造直接报错，这是刻意的：
    调参 = 改档案 = git 评审 + 对账测试守护）。组装 LayeredExitPolicy 的
    完整参数一律经 resolved_exit_params(cfg) —— 唯一合并点。
    """
    model_config = ConfigDict(extra="forbid")

    # ---- L1 R 倍数定基线 ----
    # 注：2026-09-15 已删除 `stop_at_signal_extreme` 开关。R 的口径唯一：
    #     R = max(分型极值距离 A, atr_sl_multiple × ATR)（取大，不是二选一）。
    #     信号未携带分型时 fractal ≤ 0（哨兵）→ A = 0 → R 自动退化为 2×ATR，
    #     「只靠 ATR」由数据缺失表达，无需配置项。
    stop_buffer_ticks: float = 0.0         # 止损位额外让出的 tick 缓冲
    # ---- L2 波动率(ATR)定宽窄 ----
    use_atr: bool = True                        # 用 ATR 自适应止损/止盈宽度
    atr_period: int = 14                        # ATR 计算周期
    atr_sl_multiple: float = 2.0           # 初始止损距离 = atr_sl_multiple × ATR
    # ---- L3 移动/保本锁利 ----
    use_trailing: bool = True                   # 启用保本 + 跟踪止损
    breakeven_trigger_r: float = 1.0       # 浮盈 ≥ 此倍数×R 时，启动保本/锁利层
    breakeven_buffer_r: float = 0.5       # 保本/锁利层落点 = 入场价 ± 此倍数×R（=0.5 即锁定半 R；=0 为真正保本）
    trailing_atr_multiple: float = 1.0     # 跟踪缓冲 = trailing_atr_multiple × ATR（R 含 2×ATR，最坏回吐 = 此值/2 × R = 0.5R）
    trailing_distance_points: float = 0.0  # ATR 不可用时的跟踪兜底距离（点数），0=不做跟踪
                                                #   注：跟踪止盈模式（use_trailing=True）下**不生成硬止盈单**，止盈完全交给
                                                #   L3 的 ATR 跟踪兑现；L3 启动阈值 = r_multiple_tp（品种档案，IC/IM=3R、
                                                #   其余=2R），即"盈利到 r_multiple_tp×R 时进 L3"——r_multiple_tp 由此从
                                                #   "名义盈亏比/死配置"变为 L3 触发的唯一真值源（品种级，不再有全局
                                                #   trailing_trigger_r）。

    @model_validator(mode="after")
    def _check_exit_param_order(self) -> "ExitConfig":
        """构造期 fail-fast：参数之间的**大小关系**（2026-09-15 评审补，P2）。

        **一条**不变式（违反即直接抛错，绝不带错值进实盘）：

        ① `breakeven_buffer_r < breakeven_trigger_r`（trigger > 0 时）
            保本层的语义是「浮盈到 breakeven_trigger_r×R → 把止损抬到入场价 +
            breakeven_buffer_r×R」。改成 R 的倍数之后，这两个参数第一次有了**可比较性**，
            但代码没做比较：buffer ≥ trigger 时保本位会落在**当前浮盈之上**（实测
            trigger=1.0 / buffer=1.5、R=10、浮盈 1.1R 时抬到入场价之上 15 点，市价
            4011 → 新止损 4015），下一根 bar 立刻被硬止损打掉。
            旧实现用 tick 计量（IF=2 tick=0.4 点），天然越不过 trigger，才一直没暴露。
            trigger=0（关闭保本层）时不校验 —— 那一层根本不跑。

        注：R 本身不设下限（2026-09-15 评审 · 采纳"有分型才有买卖点"的口径，
        删除 min_r_points 后不再补地板）。可观测性由 LayeredExitPolicy._initial_r()
        的两条 WARNING 负责，不在配置层拦：
          `[R 结构距离*]` —— A < LayeredExitPolicy.r_alert_a_floor
                              （默认 3.0 = 已删地板原值；IC/IM 原为 5.0）；
          `[R 归零]`      —— R = max(A, B) = 0。
        """
        if self.breakeven_trigger_r > 0 and \
                self.breakeven_buffer_r >= self.breakeven_trigger_r:
            raise ValueError(
                "exit_params: breakeven_buffer_r({}) 必须 < breakeven_trigger_r({}) —— "
                "缓冲 ≥ 触发时，保本止损会被抬到市价之上，下一根 bar 立刻被硬止损出场。"
                "（若确要「浮盈即锁利」，把 breakeven_trigger_r 设 0 关掉该层，"
                "或让 buffer 小于 trigger）".format(
                    self.breakeven_buffer_r, self.breakeven_trigger_r))
        return self


class ExitPolicyParams(ExitConfig):
    """LayeredExitPolicy 的运行时参数模型（Fix A · 2026-09-14 拆分）。

    继承 ExitConfig 的品种无关项（ATR / trailing / breakeven_*），另持品种相关参数 r_multiple_tp（它同时是 L3 启动阈值，品种级）。
    r_multiple_tp 的**权威默认值在 Product 档案**（生产路径经 resolved_exit_params()
    合并喂入，见 Exit.py 用法）；此处的默认值仅作无档案直连场景的兜底 ——
    如 test_p8 直接构造 policy、LayeredExitPolicy() 无参取默认等（= IF 档案基线）。
    """
    model_config = ConfigDict(extra="forbid")

    r_multiple_tp: float = 2.0             # 盈亏比。止盈 = 入场价 ± r_multiple_tp × R（默认 1:2）


# ════════════════════════════════════════════════════════════════════
# ④ 风控层（Risk Gate）配置
#    2026-09-08 二次精简：删除整条"仓位管理（手数定档）"通道（PositionSizing /
#      SizingConfig）。开仓手数直接由本层 max_volume 决定。
#    已删除：原五道硬闸门（enforce_session / no_open_after / max_trades_per_day /
#      max_daily_loss_points / block_on_daily_loss）与 RiskGate 类本身；
#      资金闸门（initial_cash）；仓位管理动态计算字段（capital_pct / atr_risk /
#      risk_unit_pct / equity_source / fixed_volume / fallback_volume …）。
#    保留：max_volume（每个买卖点一笔挂 N 手）。
#    2026-09-11 删除：max_open_positions（同时持仓笔数上限，D2 —— 资金是唯一闸门）、
#      unlock_no_new_open（解锁昨仓后是否补开今仓 —— "解锁"概念随重构删除）。
# ════════════════════════════════════════════════════════════════════
class RiskConfig(BaseModel):
    """风控参数（2026-09-08 二次精简）：只保留开仓手数与持仓笔数上限。

    手数语义（与"每个买卖点只开一笔"绑定）：
      · 每个买卖点信号触发时**只开一笔**，一笔挂 N 手，N = `max_volume`。
      · `max_volume` 即单笔手数上限，默认 2。默认不会配置超过中金所限价单
        单笔上限 20 手，故引擎不再另设交易所 20 手拦截。
    """
    model_config = ConfigDict(extra="forbid")

    max_volume: int = 2              # 每个买卖点一笔挂 N 手（= 单笔手数上限，默认 2）。
                                     #   中金所限价单单笔上限 20 手，配置不应超过。

    # 交割月护栏（Phase 11 · 阻塞点 4 · D8）：距最后交易日不足 `delivery_guard_days`
    #   个交易日时拒绝**开新仓**（fail-closed）。判据 = 剩余交易日 < N 即拦。
    #   默认 1 = 仅最后交易日当天拦（2026-09-14 拍板）。只拦开新仓，平旧仓永不拦；
    #   护栏对象 = 现行主力（随 `last_trade_date` 换月自动解除）。
    delivery_guard_days: int = 1

    dropped_legacy_keys: ClassVar[List[str]] = []
    # 2026-09-11 重构删除的两个键被 `_drop_legacy_keys` 丢弃时记在这里。
    # 只为"可见"服务 —— 静默丢弃会让"配置里还有这两行"永远不被发现。

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data: Any) -> Any:
        """丢弃 2026-09-11 重构删除的两个键 —— **但不静默**（Phase 5 G6）。

        RiskConfig 是 extra="forbid"，若不放行，老配置文件带上这两个键会
        **启动即报错**（pydantic 不认识的字段）。它们现在完全没有语义，
        丢弃比让服务起不来合适。

        ⚠️ 2026-09-12 修正：告警判据**只认本次输入的命中**（局部 `hits`），不再挂
        在 `cls.dropped_legacy_keys` 这份共享类变量上。旧实现的后果（实测）：
        进程内只要解析过**一次**带旧键的配置，此后**每次**加载——哪怕配置文件
        早已删干净——都会再喊一遍，告警因此失去信号价值（真出问题时已喊了几
        百遍）。`dropped_legacy_keys` 保留**累计记账**（诊断 / 测试用），但**不做
        判据**。
        """
        hits: List[str] = []
        if isinstance(data, dict):
            for k in ("max_open_positions", "unlock_no_new_open"):
                if k in data:
                    data.pop(k, None)
                    hits.append(k)
                    cls.dropped_legacy_keys.append(k)
        if hits:
            _log.warning(
                "配置文件含已删除的配置项 %s，已忽略（2026-09-11 重构："
                "「同时持仓笔数上限」「解锁后补开」随概念一并删除）。"
                "请从配置文件里删掉这几行。", sorted(set(hits)))
        return data

    @field_validator("max_volume")
    @classmethod
    def _check_max_volume(cls, v: int) -> int:
        """P1-1 启动期校验（2026-09-08）：越界 fail-fast。

        引擎不再做运行期单笔 20 手上限拦截（over_exchange_limit 已随
        PositionSizing 删除），合法性完全靠这里在配置构造时兜底，杜绝
        dry_run 无告警"照常成交超限手数"、实盘才被 CTP 拒单的行为分歧。
        """
        if not 1 <= v <= 20:
            raise ValueError(
                "risk.max_volume 必须落在 1..20（中金所限价单单笔上限 20 手），"
                "实际={}".format(v))
        return v


# ════════════════════════════════════════════════════════════════════
# ⑥b 引擎时序配置（Step 2.2 归一：原 Engine.__init__ 硬编码常量，根数口径）
#    与通道层（⑥a）/Broker 报单层（⑥）区分：本组是「执行层」的等待节奏。
# ════════════════════════════════════════════════════════════════════
class EngineConfig(BaseModel):
    """引擎时序参数（Step 2.2：原 Engine.__init__ 硬编码常量归一）。

    三项都是「根数」口径（单位 = K 线根数，与墙钟时间无关）：
      语义上周期敏感（15s 的 5 根 = 75 秒；30m 的 5 根 = 2.5 小时），
      当前按跨周期不变值放本模型（用户拍板 F1）。

    单一事实源：默认值只在本模型维护，Engine/Reconcile 不再自带兜底数字。
    """
    model_config = ConfigDict(extra="forbid")

    close_retry_bars: int = 5    # CLOSE 被拒后冷却多少根 bar 再试（防每根 bar
                                 #   重复报单 —— broker 内部每笔已追 close_max_chase 轮）
    close_max_streak: int = 20   # CLOSE 连续被拒这么多次 → 认定幻影仓，从簿中清除
                                 #   并升级为严重告警（D11，弹窗叫人核对实盘）
    close_stuck_bars: int = 5    # CLOSE 报单后多少根 bar 触发二次确认复核（Reconcile 消费）


# ════════════════════════════════════════════════════════════════════
# ⑥a 通道时序配置（Step 2.3 归一：原 Broker/SimNow.py 散落的超时/等待/退避因子）
#    仅 simnow/live 生效（dry_run 无 tqsdk 通道，不消费本组）。
#    全部默认值 == 原硬编码值（行为等价）。
# ════════════════════════════════════════════════════════════════════
class ChannelTimingConfig(BaseModel):
    """通道时序参数（Step 2.3：原 SimNow.py 散落的 10 处超时/等待收口）。

    语义分组：与 BrokerConfig 的**报单参数**（overprice/追价/fill_timeout）区分——
    本组是「通道层」的等待与重试节奏，报单语义的参数留在 BrokerConfig 平级字段。

    单一事实源：默认值只在本模型维护，SimNow.py 经 _timing() 严格读取（无兜底）。
    """
    model_config = ConfigDict(extra="forbid")

    quote_stale_seconds: float = 30.0     # 行情快照陈旧阈值（超过判陈旧→对账跳过该侧；原 _QUOTE_STALE_SECONDS）
    connect_backoff_factor: float = 1.5   # 登录失败退避增长因子（每轮 backoff ×此值；原硬编码 1.5）
    probe_alive_timeout: float = 8.0      # CTP"用户不活跃"探活窗口秒数（原 _probe_alive 默认）
    keepalive_wait: float = 0.2           # poll_market 心跳 wait_update 窗口秒数（引擎每 bar 调一次）
    baseline_settle_wait: float = 0.5     # 下单前持仓快照 settle：等 CTP 延迟回报同步（连调两次）
    recover_settle_wait: float = 5.0      # 恢复路径：给 CTP 推完未确认回报的窗口秒数（连调两次）
    position_ok_timeout: float = 10.0     # 平仓前等持仓回报可见秒数（CTP 看不到持仓会拒单）
    verify_delta_timeout: float = 5.0     # 持仓增量精确校验窗口秒数（_verify_position_delta 生产调用点）
    underlying_map_timeout: float = 20.0  # 主连→主力合约映射等待秒数（get_quote.underlying_symbol）
    cancel_settle_wait: float = 5.0       # 超时撤单后等最后一笔回报的窗口秒数
    # Phase 8.1（B-2）：合约参数（tick/乘数/涨跌停）就绪等待**独立**秒数。
    #   与 underlying_map_timeout 分开的原因：主连映射通常 <1s，而真实月份
    #   合约的静态字段在非交易时段可能 10~30s 才推齐，两者超时期望不同。
    #   默认 30s —— 取宽松端：超时不是终态（pulse 每根 bar 重试），宁可
    #   多等一轮也别在开盘阶段误触发 fail-closed（外部评审建议 10s 与其
    #   自己"10~30s 才到齐"的论证矛盾，不采纳）。
    instrument_fetch_timeout: float = 30.0


# ════════════════════════════════════════════════════════════════════
# ⑥ Broker 适配器层（Broker，可替换）配置
#    下单/成交复核/撤单（报单升级 FOK）。账号/密码不在此处。
#    （类名 2026-09-08 由 BrokerConfig 改为 BrokerConfig，与「每层一个 *Config」约定对齐；
#      字段名 broker_params 不变。）
# ════════════════════════════════════════════════════════════════════
class BrokerConfig(BaseModel):
    """broker 专属参数（仅 simnow/live 生效；dry_run 忽略）。

    报单方式（FOK / FAK）逐品种定，**唯一口径 = 品种执行策略表第 2 列**
    （`Infra/Product.py` 的 `EXEC_POLICY[code].order_advanced`）：
    IF/IH/IC/IM/AU/AG/CU = FOK，TA = FAK。此处不重复声明、也不按交易所推。
      开仓 OPEN / 解锁 UNLOCK / 锁仓 LOCK / 平仓 CLOSE 四类共用 overprice_ticks（默认 5 tick × 品种 price_tick，IF=1.0 点）。
      入场不成交 → 整笔作废等下一信号；离场不成交 → 立即按最新对手价重报直到成交。

    单一事实源：broker 参数默认值只在本模型维护，Broker/SimNow.py 不再自带兜底。
    账号/密码不在此处 —— 见模块 docstring「凭据（安全）」。
    """
    model_config = ConfigDict(extra="forbid")

    overprice_ticks: int = 5              # 超价 = overprice_ticks × 品种 tick（默认 5 tick；IF tick=0.2 → 1.0 点）。二期其它品种加载各自 price_tick 自动缩放。
    fill_timeout_open: float = 5.0   # 入场报单等待终态秒数（FOK 下退化为通道异常 watchdog）
    fill_timeout_close: float = 5.0  # 离场报单每轮等待终态秒数；未成交则立即重报追价
    close_max_chase: int = 20             # 离场追价最大轮数（引擎还会跨 K 线继续重试，实际=直到成交）
    close_chase_ticks: int = 2            # 离场追价兜底步长（仅在行情临时取不到时，在上一笔限价基础上推几跳）
    chase_interval: float = 1.0      # 离场追价重报间隔秒数（防 CTP 高频报撤监控）
    connect_retries: int = 3              # 登录重试次数（CTP 对短连接敏感，"用户不活跃"时重试通常能连上）
    connect_backoff: float = 5.0     # 登录失败后首轮退避秒数（每轮 ×1.5）
    tq_market: str = "simnow"             # 天勤接入市场：simnow=仿真；实盘填期货公司名（如"创元期货"）
    confirm_live_trading: bool = False    # 实盘安全闸门：broker=live 或 tq_market≠simnow 时必须显式 true
    # ── Phase 8（D20 · A′）：合约参数自动获取开关（§5.9.4 项 4）──
    #   **没有宽容档**（旧 A 案 prefer 已删除 —— 它就是"静默回退配置值"，
    #   需求方 2026-09-11 明确否决）：
    #     strict（默认）：实盘必须从行情取到并通过校验 price_tick / volume_multiple /
    #                    涨跌停，否则 Engine._pre_trade_check 拒单 + 严重告警（fail-closed）。
    #     off          ：只用配置值。**仅 dry_run/replay 离线模式生效** —— SimNow
    #                    （在线通道）下永不标记 verified → 闸门照样拒单（规则 4：
    #                    调试开关不得绕过 A′）。
    #     quote_partial：2026-09-14 评审 P1-3 新增的**逃生舱档**（面向不走 tqsdk 的
    #                    自研/第三方在线通道，如 CTP 直连）。语义：
    #                      · 只强制 price_tick + volume_multiple（缺一即 fail-closed，
    #                        这两个错 = 限价口径和 PnL 全错，绝不让步）；
    #                      · 涨跌停区间（upper/lower_limit）取不到时**允许放行**，
    #                        但必须把 band 护栏显式降级为不校验，并回一条 warn 告警
    #                        （code=instrument_band_degraded）让前端可见 —— 降级必须
    #                        出声，不能静默。
    #                    为什么需要这一档：Broker.is_offline 基类默认 False，
    #                    任何新通道不显式声明离线就 100% 拒单，而它未必能提供
    #                    tqsdk 那套涨跌停字段。没有中间档 = 新通道要么自欺欺人
    #                    声明离线，要么根本接不进来。
    instrument_fetch_policy: str = "strict"
    # —— 通道时序（Step 2.3 归一，见 ChannelTimingConfig docstring）——
    channel: ChannelTimingConfig = Field(default_factory=lambda: ChannelTimingConfig())

    @field_validator("instrument_fetch_policy")
    @classmethod
    def _validate_fetch_policy(cls, v: str) -> str:
        v = str(v).strip().lower()
        # 2026-09-14 评审 P1-3：新增 quote_partial 逃生舱档（valid 三档）。
        #   'prefer' 依旧非法（宽容档已按需求方 2026-09-11 拍板删除），
        #   故 test_p42 对 prefer / whatever 的断言仍然成立。
        if v not in ("strict", "off", "quote_partial"):
            raise ValueError(
                "instrument_fetch_policy 只允许 'strict'（默认，实盘必须行情取值）"
                " / 'off'（仅离线生效） / 'quote_partial'（只强制 tick+乘数，"
                "涨跌停缺失时降级为不校验并告警），得到 {!r} —— 宽容档 prefer "
                "已按需求方 2026-09-11 拍板删除".format(v))
        return v


# ════════════════════════════════════════════════════════════════════
# 模块收尾：TradingConfig 声明在所有 section 模型之前（根配置置顶），其嵌套字段的
# 注解是「前向引用」（文件顶部 `from __future__ import annotations` 已把注解转字符串）。
# 必须等全部 section 类定义完毕，再调用 model_rebuild() 让 pydantic 解析这些前向引用，
# 之后才能实例化。顺序：TradingConfig(顶) → 各 section 类 → model_rebuild → 默认配置快照。
# ════════════════════════════════════════════════════════════════════
TradingConfig.model_rebuild()
# 注：2.0.3 起已删除 *PolicyConfig 选择器抽象，配置层直接持有 entry_params / exit_params
# 两个参数模型，不再有需单独 model_rebuild 的 *PolicyConfig 类。


def resolved_exit_params(cfg: TradingConfig) -> Dict[str, Any]:
    """LayeredExitPolicy 完整出场参数的**唯一合并点**（Fix A · 2026-09-14 · D1 拍板）。

    合并两个来源：
      · 品种无关项 —— cfg.exit_params（ATR / trailing / 触发倍数等）
      · 品种相关项 —— cfg.product_profile.exit_overrides()（r_multiple_tp，唯一默认值来源是品种档案）
    档案值整块生效：本函数产出的 dict 才是 LayeredExitPolicy 的合法入参，
    直接传 cfg.exit_params.model_dump() 会缺品种参数 r_multiple_tp（构造期 AttributeError）。
    （2026-09-14 前档案提供 min_r_points / r_multiple_tp / breakeven_buffer_ticks 三个品种参数，
     现只剩 r_multiple_tp 一个 —— 另两个已分别删除 / 上移为全局比例。）

    未标定品种 → 抛 ValueError（describe_unknown_product 统一文案，与
    AppTrader / Engine._restore 白名单闸门同源同文案）—— 把「品种参数没标定」
    拦在启动期，与 main.py 的周期 fail-fast（bar_secs_for）同一纪律。

    用法：main.py `exitp = LayeredExitPolicy(resolved_exit_params(cfg))`；
    测试里需要覆盖品种值时，在本函数返回的 dict 上改再传 LayeredExitPolicy。
    """
    profile = cfg.product_profile
    if profile is None:
        raise ValueError(describe_unknown_product(cfg.instrument.signal_symbol))
    return {**cfg.exit_params.model_dump(), **profile.exit_overrides()}


def default_config() -> TradingConfig:
    """取一份「模型默认值 + 环境变量覆盖」后的配置（不含 CLI 覆盖）。"""
    return TradingConfig()


# 默认配置的 dict 快照（供测试与文档对照；源仍是上面的模型字段，不是第二套默认值）
DEFAULT_CONFIG: Dict[str, Any] = default_config().model_dump()
