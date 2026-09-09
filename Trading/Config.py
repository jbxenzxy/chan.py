# -*- coding: utf-8 -*-
"""
Trading/Config.py —— 自动下单配置的**唯一总入口**（SSOT = Single Source Of Truth）
=================================================================================
2026-09-08 配置层按「六层架构」重排 + 周期敏感配置归总：
    · 顶层根配置 `TradingConfig` 置于文件最前（它是整个模块的根，其余各层 section
      模型都是它的嵌套字段）；其各层字段按下层顺序组织：
        ① 信号源层     → SourceConfig
        ② 信号适配层   → （无独立配置模型，仅解析/去重行为）
        ③ 策略层       → Entry/Exit 参数（单一策略，无选择器）
        ④ 风控层       → RiskConfig（开仓手数/持仓上限/补开开关）
        ⑤ 执行层       → （无独立配置模型，状态机/对账行为）
        ⑥ Broker 适配器层 → BrokerConfig
    · 周期敏感配置（freq ...）统一收口到文末 `PERIOD_SENSITIVE_FIELDS` 归总，
      作为 Step 2 调参单一入口。（2026-09-08：L4 时间兜底与风控五道硬闸门删除、
      signal_max_age_minutes 改为 K 线相对容差后，周期敏感项已只剩 freq。）

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
    ③ 策略层**没有「策略选择」这一层**。生产环境入场只有一个策略 `DefaultEntryPolicy`、
    出场只有一个策略 `LayeredExitPolicy`（L1-L3 分层出场，2026-09-08 已删 L4），
    用户明确不会增加第二种，因此不再需要 `name` 字段、注册表、或 build_*_policy 路由。

    配置直接持有参数模型：
      · `entry_params: EntryConfig`   —— DefaultEntryPolicy 的可调数值（默认值唯一来源）
      · `exit_params: ExitConfig`     —— LayeredExitPolicy（L1-L3）的可调数值

    main.py 直接实例化，不再经选择器：
        entry = DefaultEntryPolicy(cfg.entry_params.model_dump())
        exitp = LayeredExitPolicy(cfg.exit_params.model_dump())
    原 `DefaultExitPolicy`（简单固定点数出场）已删除——它是可选的「第二种」出场策略，
    生产从不选用，保留它只会让配置与测试多一套无用的选择分支。

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
    · 组件（ExitPolicy / EntryPolicy / SimNow / Broker）只接受配置模型
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

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .Infra.InstrumentSpec import InstrumentSpec
from .Infra.PeriodProfile import PERIOD_PROFILES, PeriodProfile, SUPPORTED_FREQS
from .Infra.ProductProfile import PRODUCT_PROFILES, ProductProfile, parse_product

__all__ = [
    # 顶层根配置（横切·基础设施）—— 置于最前，是整个配置树的根
    "TradingConfig", "default_config", "DEFAULT_CONFIG",
    # 各层 section 模型（按下层顺序声明，见文件内 banner）
    "SourceConfig",
    "EntryConfig", "ExitConfig",
    "RiskConfig",
    "BrokerConfig",
    "ChannelTimingConfig",
    "EngineConfig",
    # 周期敏感配置归总（Step 2 调参单一入口）
    "PERIOD_SENSITIVE_FIELDS", "period_sensitive_fields",
    "period_sensitive_summary",
]

# 仓库根（Trading/ 的上一级）—— 与 App/AppConfig.py 同一份 .env
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_REPO_ROOT, ".env")


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
    instrument: InstrumentSpec = Field(default_factory=InstrumentSpec)

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

    # ── 周期档案注入（Step 2.1：周期敏感参数入 PeriodProfile）──
    # 按 source.freq 选档案，把 6 项周期敏感参数「影子覆盖」进 flat 字段：
    #   · 仅当 flat 字段仍是模型默认值时填入（用户显式 JSON/环境变量覆盖的字段不动）；
    #   · 未知 freq 直接跳过——TradingConfig 也用于 AppTrader 等非引擎场景，那里
    #     freq 可能只是透传（如 test_p20 的 "15m"）；真正的 fail-fast 在 main.py。
    # bar_secs 不在此 reconcile —— 它走引擎 bar_secs_for(freq) 自动推导
    # （PeriodProfile.bar_secs 已是推导源），flat 的 bar_secs=0 保留「手动覆盖」语义。
    @model_validator(mode="after")
    def _reconcile_period_profile(self) -> "TradingConfig":
        self._apply_profile_values()
        return self

    def _apply_profile_values(self) -> None:
        # 2026-09-08：原对 source.signal_max_age_minutes 的周期敏感影子覆盖，随该字段
        #   退役（改为 signal_k_tol_bars 的「按 K 线相对根数容差」，非周期敏感项）后已
        #   无目标字段。保留本方法仅为兼容 main.py 的 apply_period_profile 调用约定，
        #   视为幂等 no-op（freq/bar_secs 本就透传自 Profile，不需落到 flat 字段）。
        return

    def apply_period_profile(self) -> "TradingConfig":
        """CLI 覆盖 source.freq 后重新对齐周期档案（main.py 在 --freq 之后调用）。

        构造期由 _reconcile_period_profile 自动对齐；此后若手动改了 source.freq
        （CLI --freq），再调本方法让 flat 字段跟随新周期。基线值下是幂等 no-op。
        """
        self._apply_profile_values()
        return self

    @property
    def period_profile(self) -> Optional["PeriodProfile"]:
        """当前 source.freq 对应的周期档案（只读视图；未知 freq 返回 None）。"""
        return PERIOD_PROFILES.get(self.source.freq)

    # ── 品种档案注入（2026-09-09：随品种可变参数入 ProductProfile）──
    # 按 instrument.signal_symbol 选品种，把 min_r_points / r_multiple_tp /
    # multiplier / breakeven_buffer_ticks 四个「随品种可变」的 flat 字段强制覆盖
    # （品种档案是真值来源）：
    #   · 未知品种（signal_symbol 不含 '.' 或不在 4 个品种表内）时跳过，保留 flat 默认。
    # 与周期档案「仅当仍是模型默认值才覆盖」的语义不同——品种字段没有跨越 IF 基线的
    #   「通用默认值」，flat 默认本身就是 IF 基线，故这里整体覆盖、语义更直白。
    @model_validator(mode="after")
    def _reconcile_product_profile(self) -> "TradingConfig":
        self._apply_product_profile_values()
        return self

    def _apply_product_profile_values(self) -> None:
        product = parse_product(self.instrument.signal_symbol)
        profile = PRODUCT_PROFILES.get(product)
        if profile:
            self.exit_params.min_r_points = profile.min_r_points
            self.exit_params.r_multiple_tp = profile.r_multiple_tp
            self.exit_params.breakeven_buffer_ticks = profile.breakeven_buffer_ticks
            self.instrument.multiplier = profile.multiplier

    @property
    def product_profile(self) -> Optional["ProductProfile"]:
        """当前 instrument.signal_symbol 对应的品种档案（只读视图；未知品种返回 None）。"""
        return PRODUCT_PROFILES.get(parse_product(self.instrument.signal_symbol))


# ════════════════════════════════════════════════════════════════════
# ① 信号源层（Signal Source）配置
#    行情来源 / 周期 / 信号新鲜度过滤。全部字段与「周期选择」相关，
#    但语义上属于「信号源」这一层；其中 freq 是周期敏感项（见文末
#    PERIOD_SENSITIVE_FIELDS 归总）；signal_k_tol_bars 是「按 K 线相对根数」
#    的容差、**不随周期改变**，属非周期敏感项，不入归总。
# ════════════════════════════════════════════════════════════════════
class SourceConfig(BaseModel):
    """行情源：replay 回放本地 K 线 / sse 实时订阅。"""
    model_config = ConfigDict(extra="forbid")

    type: str = "replay"                      # replay=回放（离线测试）；sse=实时订阅
    replay_dir: str = "./replay_data"         # replay 模式的 K 线数据目录
    sse_base: str = "http://127.0.0.1:18081"  # sse 模式的行情服务地址
    symbol: str = "KQ.m@CFFEX.IF"             # 订阅合约（与 instrument.signal_symbol 一致）
    freq: str = "5m"                          # K 线周期（周期只做字符串透传，不参与分钟换算）
    speed: float = 0.0                        # replay 每根 K 线间隔秒数（0=尽快）
    bar_mode: str = "confirmed"               # confirmed=只取已闭合 K 线；last=含未闭合
    only_alive: bool = False                  # 只处理存活（未到期）合约
    # 信号新鲜度过滤 —— K 线位置口径（2026-09-08 取代原 signal_max_age_minutes）：
    #   chan.py SSE 首连会 replay 一批历史 bsp。每个买卖点信号的 timestamp =
    #   它所在分型右肩 K 的时间戳；快照最后一根 K 即「当前最新 K」。
    #   这里按「信号归属K 距最新K 的根数」判新旧：距最新 K > N 根 → 视为历史
    #   残留丢弃。N 是「距最终K的相对根数」，**不随周期改变**（非周期敏感项，
    #   故不入文末 PERIOD_SENSITIVE_FIELDS）。0 = 必须正好是最右一根 K 才处理。
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
#    入场 / 出场各只有一个策略（DefaultEntryPolicy / LayeredExitPolicy），
#    配置层不再有「策略选择」抽象——直接持有参数模型 entry_params / exit_params。
# ════════════════════════════════════════════════════════════════════
class EntryConfig(BaseModel):
    """DefaultEntryPolicy 参数（唯一入场策略，在此加字段，别处不再写默认值）。"""
    model_config = ConfigDict(extra="forbid")

    reverse_on_opposite_signal: bool = False  # 持仓时出现反向信号是否反手
    max_signal_range_points: float = 0.0      # 信号 K 线最大振幅（点数）过滤，0=不过滤
    min_stop_distance_points: float = 0.0     # 止损位与入场价最小距离（点数），0=不校验
    max_stop_distance_points: float = 0.0     # 止损位与入场价最大距离（点数），0=不校验


class ExitConfig(BaseModel):
    """出场参数（单一模型，LayeredExitPolicy 即 L1-L3 分层出场，2026-09-08 精简）。

    LayeredExitPolicy（L1-L3 分层出场）：L1 R 倍数定基线 → L2 ATR 定宽窄 →
    L3 保本/跟踪锁利。原 L4 时间/收盘兜底已删除（含引擎侧收盘前强平）。
    R 的产生见 Strategy/Exit.py：R = max(A, 2×ATR, min_r_points)，其中
      A = 结构止损（分型极值距离），2×ATR = 波动率止损，min_r_points = R 下限地板。

    （2026-09-08：原独立的 DefaultExitParamsConfig 已并入本模型，统一为单一出场参数模型；
     可选的第二套出场 DefaultExitPolicy 一并删除——生产只用 L1-L3，不再保留无用选择分支。）
    """
    model_config = ConfigDict(extra="forbid")

    # ---- L1 R 倍数定基线 ----
    stop_at_signal_extreme: bool = True         # True=用分型极值作结构止损；False=只靠 2×ATR 与 min_r_points
    stop_buffer_ticks: float = 0.0         # 止损位额外让出的 tick 缓冲
    r_multiple_tp: float = 2.0             # 盈亏比。止盈 = 入场价 ± r_multiple_tp × R（默认 1:2，品种档案可覆盖）
    min_r_points: float = 3.0              # R 下限（点数），防极端横盘+极窄分型（品种档案可覆盖）
    # ---- L2 波动率(ATR)定宽窄 ----
    use_atr: bool = True                        # 用 ATR 自适应止损/止盈宽度
    atr_period: int = 14                        # ATR 计算周期
    atr_sl_multiple: float = 2.0           # 初始止损距离 = atr_sl_multiple × ATR
    # ---- L3 移动/保本锁利 ----
    use_trailing: bool = True                   # 启用保本 + 跟踪止损
    breakeven_trigger_r: float = 1.0       # 浮盈 ≥ 此倍数×R 时，止损抬至保本
    breakeven_buffer_ticks: float = 0.0    # 保本位缓冲 tick（覆盖往返手续费+滑点；品种档案 IF/IH=2、IC/IM=3）
    trailing_trigger_r: float = 2.0        # 浮盈 ≥ 此倍数×R 时，启动 ATR 跟踪止盈
    trailing_atr_multiple: float = 1.0     # 跟踪缓冲 = trailing_atr_multiple × ATR（R 含 2×ATR，最坏回吐 = 此值/2 × R = 0.5R）
    trailing_distance_points: float = 0.0  # ATR 不可用时的跟踪兜底距离（点数），0=不做跟踪
                                                #   注：B 方案（use_trailing=True）下 r_multiple_tp 仅作"名义盈亏比"
                                                #   （期望值口径），不生成硬止盈单；止盈交给 L3 ATR 跟踪兑现


# ════════════════════════════════════════════════════════════════════
# ④ 风控层（Risk Gate）配置
#    2026-09-08 二次精简：删除整条"仓位管理（手数定档）"通道（PositionSizing /
#      SizingConfig）。开仓手数直接由本层 max_volume 决定。
#    已删除：原五道硬闸门（enforce_session / no_open_after / max_trades_per_day /
#      max_daily_loss_points / block_on_daily_loss）与 RiskGate 类本身；
#      资金闸门（initial_cash）；仓位管理动态计算字段（capital_pct / atr_risk /
#      risk_unit_pct / equity_source / fixed_volume / fallback_volume …）。
#    保留：max_volume（每个买卖点一笔挂 N 手）、max_open_positions（同时持仓笔数上限）、
#      unlock_no_new_open（解锁昨仓后是否补开今仓）。
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
    max_open_positions: int = 1      # 同时持仓笔数上限（1=单仓；N=一次可连开 N 笔）
    unlock_no_new_open: bool = True  # 解锁昨仓后是否补开今仓缺额。
                                     #   True = 绝不补开（默认）：只解锁昨仓、缺口放弃，
                                     #         规避金融期货"平今"高手续费坑
                                     #   False = 补开：昨锁 3 手、今信号 5 手
                                     #           → 解锁 3 后再开 2，净敞口到 5
                                     #
                                     #   两种情况（True/False）下解锁本身照常执行
                                     #   （解锁 = 减风险动作，永不因补开开关而放弃）。

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


# ════════════════════════════════════════════════════════════════════
# ⑥ Broker 适配器层（Broker，可替换）配置
#    下单/成交复核/撤单（报单升级 FOK）。账号/密码不在此处。
#    （类名 2026-09-08 由 BrokerConfig 改为 BrokerConfig，与「每层一个 *Config」约定对齐；
#      字段名 broker_params 不变。）
# ════════════════════════════════════════════════════════════════════
class BrokerConfig(BaseModel):
    """broker 专属参数（仅 simnow/live 生效；dry_run 忽略）。

    全部报单都是 FOK（中金所 IF/IH/IC/IM 支持；郑商所不支持 FOK）：
      开仓 OPEN / 解锁 UNLOCK / 锁仓 LOCK / 平仓 CLOSE 四类共用 overprice_points。
      入场不成交 → 整笔作废等下一信号；离场不成交 → 立即按最新对手价重报直到成交。

    单一事实源：broker 参数默认值只在本模型维护，Broker/SimNow.py 不再自带兜底。
    账号/密码不在此处 —— 见模块 docstring「凭据（安全）」。
    """
    model_config = ConfigDict(extra="forbid")

    overprice_points: float = 1.0    # 超价点数：下单价 = 对手价 ± 此值并取整到 tick（IF tick=0.2 → 5 tick）
    fill_timeout_open: float = 5.0   # 入场报单等待终态秒数（FOK 下退化为通道异常 watchdog）
    fill_timeout_close: float = 5.0  # 离场报单每轮等待终态秒数；未成交则立即重报追价
    close_max_chase: int = 20             # 离场追价最大轮数（引擎还会跨 K 线继续重试，实际=直到成交）
    close_chase_ticks: int = 2            # 离场追价兜底步长（仅在行情临时取不到时，在上一笔限价基础上推几跳）
    chase_interval: float = 1.0      # 离场追价重报间隔秒数（防 CTP 高频报撤监控）
    connect_retries: int = 3              # 登录重试次数（CTP 对短连接敏感，"用户不活跃"时重试通常能连上）
    connect_backoff: float = 5.0     # 登录失败后首轮退避秒数（每轮 ×1.5）
    tq_market: str = "simnow"             # 天勤接入市场：simnow=仿真；实盘填期货公司名（如"创元期货"）
    confirm_live_trading: bool = False    # 实盘安全闸门：broker=live 或 tq_market≠simnow 时必须显式 true
    # —— 通道时序（Step 2.3 归一，见 ChannelTimingConfig docstring）——
    channel: ChannelTimingConfig = Field(default_factory=lambda: ChannelTimingConfig())


class EngineConfig(BaseModel):
    """引擎时序参数（Step 2.2：原 Engine.__init__ 硬编码常量归一）。

    三项都是「根数」口径（单位 = K 线根数，与墙钟时间无关）：
      语义上周期敏感（15s 的 5 根 = 75 秒；30m 的 5 根 = 2.5 小时），
      当前按跨周期不变值放本模型（用户拍板 F1）；2.7+ 若差异化标定，
      候选迁入 PeriodProfile（见 PERIOD_SENSITIVE_FIELDS 索引表注记）。

    单一事实源：默认值只在本模型维护，Engine/Reconcile 不再自带兜底数字。
    """
    model_config = ConfigDict(extra="forbid")

    close_retry_bars: int = 5    # close 被拒后冷却多少根 bar 再试（防每根 bar 重复平仓死循环）
    close_max_streak: int = 20   # 连续失败这么多根后认定幻影持仓，强制清除
    unlock_stuck_bars: int = 5   # UNLOCK 报单后多少根 bar 触发二次确认复核（Reconcile 消费）


# ════════════════════════════════════════════════════════════════════
# 周期敏感配置归总（Step 2 调参单一入口 / SSOT 索引）
# ------------------------------------------------------------------
# 2026-09-08 Step 2.1 起：这些项的**值**已收口到 Infra/PeriodProfile.py 的
#   PERIOD_PROFILES（每周期一份，含 note 标定记录），TradingConfig 构造期按
#   source.freq 把周期敏感值「影子覆盖」进 flat 字段。
#   本表仍保留作静态说明（path/layer/kind/step2 的「是什么/为什么」）；
#   运行时每周期的实际值见 `period_sensitive_summary()` 派生视图。
#
# 字段说明：
#   path   : 字段在配置树中的点分路径（与 DEFAULT_CONFIG 对应）
#   layer  : 所属六层架构层级
#   default: BASELINE 默认值（= PeriodProfile 各周期占位值）
#   kind   : 量纲 / 含义
#   step2  : Step 2 调参关注点
# ════════════════════════════════════════════════════════════════════
PERIOD_SENSITIVE_FIELDS: List[Dict[str, Any]] = [
    {
        "path": "source.freq",
        "layer": "① 信号源层",
        "default": "5m",
        "kind": "周期本身（字符串透传，不参与分钟换算）",
        "step2": "Step 2 在 15s/1m/5m/30m 间切换；非标周期须先确认主程序 FREQ_TABLE/"
                 "FREQ_SEC_MAP 已注册（Infra/PeriodProfile 已对账）",
    },
    # （2026-09-08：原 source.signal_max_age_minutes 周期敏感条目已退役，改为
    #   signal_k_tol_bars（按 K 线相对根数的容差）——该新字段**非周期敏感**，
    #   不随周期改变，故不入本归总表。）
    # （2026-09-08：原 exit_params.max_hold_bars / max_hold_seconds /
    #   eod_lead_bars / bar_secs / session_end_hhmm（L4 时间/收盘兜底）与
    #   risk.max_trades_per_day（风控五道硬闸门）的周期敏感条目已随功能删除。）
]


def period_sensitive_fields() -> List[Dict[str, Any]]:
    """返回周期敏感配置归总的副本（防止调用方改到模块级常量）。"""
    return [dict(f) for f in PERIOD_SENSITIVE_FIELDS]


def period_sensitive_summary() -> List[Dict[str, Any]]:
    """4 周期 × 周期敏感参数的**派生视图**（值来自 PeriodProfile，Step 2.7+ 调参一眼对比）。

    这是 `PERIOD_SENSITIVE_FIELDS`（静态说明：path/layer/kind/step2）的运行时补充：
    前者说「哪几项是周期敏感的、为什么」，本函数给出「这些项在每个周期下的实际值」。
    """
    rows: List[Dict[str, Any]] = []
    for freq in SUPPORTED_FREQS:
        p = PERIOD_PROFILES[freq]
        rows.append({
            "freq": freq,
            "bar_secs": p.bar_secs,
            "note": p.note,
        })
    return rows


# ════════════════════════════════════════════════════════════════════
# 模块收尾：TradingConfig 声明在所有 section 模型之前（根配置置顶），其嵌套字段的
# 注解是「前向引用」（文件顶部 `from __future__ import annotations` 已把注解转字符串）。
# 必须等全部 section 类定义完毕，再调用 model_rebuild() 让 pydantic 解析这些前向引用，
# 之后才能实例化。顺序：TradingConfig(顶) → 各 section 类 → model_rebuild → 默认配置快照。
# ════════════════════════════════════════════════════════════════════
TradingConfig.model_rebuild()
# 注：2.0.3 起已删除 *PolicyConfig 选择器抽象，配置层直接持有 entry_params / exit_params
# 两个参数模型，不再有需单独 model_rebuild 的 *PolicyConfig 类。


def default_config() -> TradingConfig:
    """取一份「模型默认值 + 环境变量覆盖」后的配置（不含 CLI 覆盖）。"""
    return TradingConfig()


# 默认配置的 dict 快照（供测试与文档对照；源仍是上面的模型字段，不是第二套默认值）
DEFAULT_CONFIG: Dict[str, Any] = default_config().model_dump()
