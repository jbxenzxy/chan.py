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
        ④ 风控层       → RiskConfig / SizingConfig
        ⑤ 执行层       → （无独立配置模型，状态机/对账行为；其消费的时序参数见 ExitConfig 的 L4 段）
        ⑥ Broker 适配器层 → BrokerConfig
    · 周期敏感配置（freq / max_hold_bars / eod_lead_bars / bar_secs / signal_max_age_minutes /
      max_trades_per_day ...）统一收口到文末 `PERIOD_SENSITIVE_FIELDS` 归总，作为 Step 2 调参单一入口。

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
    出场只有一个策略 `LayeredExitPolicy`（L1-L4 分层），用户明确不会增加第二种，
    因此不再需要 `name` 字段、注册表、或 build_*_policy 路由。

    配置直接持有参数模型：
      · `entry_params: EntryConfig`   —— DefaultEntryPolicy 的可调数值（默认值唯一来源）
      · `exit_params: ExitConfig`     —— LayeredExitPolicy（L1-L4）的可调数值

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
    · 组件（PositionSizer / ExitPolicy / EntryPolicy / SimNow）只接受配置模型
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
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .Infra.InstrumentSpec import InstrumentSpec

__all__ = [
    # 顶层根配置（横切·基础设施）—— 置于最前，是整个配置树的根
    "TradingConfig", "default_config", "DEFAULT_CONFIG",
    # 各层 section 模型（按下层顺序声明，见文件内 banner）
    "SourceConfig",
    "EntryConfig", "ExitConfig",
    "RiskConfig", "SizingConfig",
    "BrokerConfig",
    # 周期敏感配置归总（Step 2 调参单一入口）
    "PERIOD_SENSITIVE_FIELDS", "period_sensitive_fields",
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
    sizing: SizingConfig = Field(default_factory=lambda: SizingConfig())
    # —— ⑥ Broker 适配器层 ——
    broker_params: BrokerConfig = Field(default_factory=lambda: BrokerConfig())
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


# ════════════════════════════════════════════════════════════════════
# ① 信号源层（Signal Source）配置
#    行情来源 / 周期 / 信号新鲜度过滤。全部字段与「周期选择」相关，
#    但语义上属于「信号源」这一层；其中 freq / signal_max_age_minutes
#    是周期敏感项，见文末 PERIOD_SENSITIVE_FIELDS 归总。
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
    # 信号新鲜度过滤（分钟）：chan.py SSE 首连会 replay 一批历史 bsp，
    # "首次出现距今 > 本值"视为陈旧残留丢弃。15s 周期下 60 分钟 = 240 根 bar，
    # 建议按周期收紧（Step 2 调参项）。0=不过滤。
    signal_max_age_minutes: float = 60.0


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
    """出场参数（单一模型，LayeredExitPolicy 即 L1-L4 分层出场，2026-09-08 已删除 DefaultExitPolicy）。

    LayeredExitPolicy（L1-L4 分层出场）：L1 R 倍数定基线 → L2 ATR 定宽窄 →
    L3 保本/跟踪锁利 → L4 时间/收盘兜底。R 的产生与回退链见 Strategy/Exit.py：
    ATR×atr_sl_multiple → 信号极值 → min_r_points 地板。

    （2026-09-08：原独立的 DefaultExitParamsConfig 已并入本模型，统一为单一出场参数模型；
     可选的第二套出场 DefaultExitPolicy 一并删除——生产只用 L1-L4，不再保留无用选择分支。）
    """
    model_config = ConfigDict(extra="forbid")

    # ---- L1 R 倍数定基线 ----
    stop_at_signal_extreme: bool = True  # True=用信号 K 线极值作结构止损；False=用 min_r_points 保底
    stop_buffer_ticks: float = 0.0       # 止损位额外让出的 tick 缓冲
    r_multiple_tp: float = 2.0           # 止盈 = 入场价 ± r_multiple_tp × R（默认 1:2）
    min_r_points: float = 2.0            # R 下限（点数），防极端行情止损过窄
    # ---- L2 波动率(ATR)定宽窄 ----
    use_atr: bool = True                 # 用 ATR 自适应止损/止盈宽度
    atr_period: int = 14                 # ATR 计算周期
    atr_sl_multiple: float = 2.0         # 初始止损距离 = atr_sl_multiple × ATR
    # ---- L3 移动/保本锁利 ----
    use_trailing: bool = True            # 启用保本 + 跟踪止损
    breakeven_trigger_r: float = 1.0     # 浮盈 ≥ 此倍数×R 时止损抬至保本
    breakeven_buffer_ticks: float = 0.0  # 保本位缓冲 tick
    trailing_trigger_r: float = 2.0      # 浮盈 ≥ 此倍数×R 时启动 ATR 跟踪止损
    trailing_atr_multiple: float = 1.5   # 跟踪止损距离 = trailing_atr_multiple × ATR
    trailing_distance_points: float = 0.0  # ATR 不可用时的跟踪兜底距离（点数），0=不做跟踪
    # ---- L4 时间/收盘兜底（以下 5 项均为周期敏感，见文末 PERIOD_SENSITIVE_FIELDS）----
    # max_hold_bars：最长持仓 **K 线根数**，超时强平，0=不限。
    #   语义以《Docs/止盈止损/TP_SL_L4》为准 —— "N 根 K 线无进展 → 走"，
    #   计量单位是 **bar 不是时间**，因此 30 在任何周期下都是 30 根，与时间无关。
    #   （2026-09-08 更正：此前曾把它判为"跨周期语义漂移 120 倍"并改成秒，
    #    那是把 Step 2 的标定问题误当成 Step 1 的正确性缺陷，已撤回。）
    #   注意：默认 30 是按 5m 标定的（≈2.5 小时）。换周期后这数字是否仍合适，
    #   属 Step 2 调参 —— 尤其是 30m（一天仅 8 根，30 根跨 3.75 个交易日，
    #   实际永远轮不到它、由收盘强平接管）。
    max_hold_bars: int = 30
    # max_hold_seconds：可选的**墙钟**上限（秒），0=不启用。
    #   与 max_hold_bars 是"或"的关系，谁先到谁生效；默认 0 → 行为与基线完全一致。
    #   用途：想在粗周期上加一道"绝不过夜/绝不超时"的硬顶时再开。
    max_hold_seconds: float = 0.0
    session_end_hhmm: str = "14:55"  # 收盘前强平阈值时刻（""=不启用）
    # eod_lead_bars：提前几根 bar 的时长判定"该平了"。
    #   1（默认）= 下一根 bar 会跨过收盘 → 本根闭合即平（30m 也能平掉）；
    #   0 = 旧口径"bar 结束时刻 ≥ 阈值"，30m 下最后一根结束后已收盘，失效。
    #   语义见 Infra/PeriodProfile.eod_triggered。
    eod_lead_bars: int = 1
    # bar_secs：本周期一根 bar 的秒数。0=引擎按 source.freq 自动推导并注入；
    #   显式给非 0 值可覆盖（单测 / 非标周期用）。
    bar_secs: int = 0


# ════════════════════════════════════════════════════════════════════
# ④ 风控层（Risk Gate）配置
#    时段/手数/日亏 + 仓位管理（只删分仓）。
# ════════════════════════════════════════════════════════════════════
class RiskConfig(BaseModel):
    """风控参数：手数 / 日笔数 / 日亏上限 / 时段限制。"""
    model_config = ConfigDict(extra="forbid")

    max_volume: int = 2                      # 单笔手数上限（非仓位管理下 = 每次入场固定手数）
    max_open_positions: int = 1              # 同时持仓笔数上限（1=单仓；N=一次可连开 N 笔）
    initial_cash: float = 10000000.0         # 虚拟初始资金（dry_run 无真实账户时资金闸门用）
    max_trades_per_day: int = 20             # 每日最大往返笔数
    max_daily_loss_points: float = 60.0      # 每日最大净亏（点数），触达后停止开仓
    enforce_session: bool = True             # 只在交易时段内开仓
    no_open_after: str = "14:50"             # 尾盘不再开新仓（空串=不限制）
    close_before_session_end: bool = True    # 收盘前强平（引擎在时段外收到 bar 时处理）
    block_on_daily_loss: bool = True         # 日亏触达后是否真的拦截开仓


class SizingConfig(BaseModel):
    """仓位管理（手数定档）。默认 enabled=False —— 开几手完全沿用 risk.max_volume。

    ⚠ 一笔报单最多 20 手（中金所限价单硬性上限，IF/IH/IC/IM 同）：超过交易所直接拒单
      （代码也会在开仓前拦一道：signal_action=rejected / reason=over_exchange_limit）。
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False            # 总开关：False=关闭仓位管理（固定手数）
    mode: str = "fixed"              # fixed 固定手数 | capital_pct 按保证金占比 | atr_risk 按风险敞口
    fixed_volume: int = 0            # fixed 模式下一个信号一次报单的手数；0=沿用 risk.max_volume
    capital_pct: float = 0.50        # capital_pct 模式：这笔仓位最多占用权益的比例
    risk_per_trade_pct: float = 0.01 # atr_risk 模式：这笔最多亏掉权益的比例（固定分数法）
    margin_rate: float = 0.15        # 保证金率，capital_pct 折算每手占用（IF 一般 12%-15%）
    risk_unit_pct: float = 0.01      # 资金门槛缓冲：每手名义价值预留的波动比例
    max_volume: int = 0              # 算法结果的截断上限；0=默认中金所单笔上限 20
    min_volume: int = 1              # 手数下限：算出来不足时提升到该值（设 0 则真的不开）
    fallback_volume: int = 1         # 权益/ATR 取不到时的回退手数
    equity_source: str = "available" # 权益口径：available 可用资金 | balance 总资产权益
    unlock_no_new_open: bool = True  # 解锁昨仓后是否补开今仓缺额。
                                     #   True = 绝不补开（默认）：只解锁昨仓、缺口放弃，
                                     #         规避金融期货"平今"高手续费坑
                                     #   False = 补开：昨锁 3 手、今信号算 5 手
                                     #           → 解锁 3 后再开 2，净敞口到 5
                                     #   两种情况下解锁本身照常执行（解锁是减风险动作）


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
    close_max_chase: int = 20        # 离场追价最大轮数（引擎还会跨 K 线继续重试，实际=直到成交）
    close_chase_ticks: int = 2       # 离场追价兜底步长（仅在行情临时取不到时，在上一笔限价基础上推几跳）
    chase_interval: float = 1.0      # 离场追价重报间隔秒数（防 CTP 高频报撤监控）
    connect_retries: int = 3         # 登录重试次数（CTP 对短连接敏感，"用户不活跃"时重试通常能连上）
    connect_backoff: float = 5.0     # 登录失败后首轮退避秒数（每轮 ×1.5）
    tq_market: str = "simnow"        # 天勤接入市场：simnow=仿真；实盘填期货公司名（如"创元期货"）
    confirm_live_trading: bool = False  # 实盘安全闸门：broker=live 或 tq_market≠simnow 时必须显式 true


# ════════════════════════════════════════════════════════════════════
# 周期敏感配置归总（Step 2 调参单一入口 / SSOT 索引）
# ------------------------------------------------------------------
# 这些字段**物理上仍住在上面各层的 section 模型里**（每层默认值唯一），
# 此处只做"按周期归总"的只读索引，方便 Step 2 一眼看到所有需按 15s/1m/5m/30m
# 重新标定的项，避免遗漏。
#
# 字段说明：
#   path   : 字段在配置树中的点分路径（与 DEFAULT_CONFIG 对应）
#   layer  : 所属六层架构层级
#   default: 当前默认值
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
    {
        "path": "source.signal_max_age_minutes",
        "layer": "① 信号源层",
        "default": 60.0,
        "kind": "信号新鲜度过滤（分钟）",
        "step2": "15s 下 60min=240 根，必须按周期收紧；否则陈旧信号被误判为新鲜",
    },
    {
        "path": "exit_params.max_hold_bars",
        "layer": "③ 策略层 · L4",
        "default": 30,
        "kind": "最长持仓 K 线根数（量纲=bar，与周期无关）",
        "step2": "默认按 5m 标定(≈2.5h)；30m 下 30 根≈3.75 交易日、由收盘强平接管；"
                 "15s/1m 须重标",
    },
    {
        "path": "exit_params.max_hold_seconds",
        "layer": "③ 策略层 · L4",
        "default": 0.0,
        "kind": "可选墙钟硬顶（秒），0=不启用",
        "step2": "启用即周期敏感；用于粗周期加一道'绝不过夜/绝不超时'硬顶",
    },
    {
        "path": "exit_params.eod_lead_bars",
        "layer": "③ 策略层 · L4",
        "default": 1,
        "kind": "提前 N 根 bar 判定收盘",
        "step2": "1=下一根将跨收盘即平（30m 也能平）；0=旧口径失效",
    },
    {
        "path": "exit_params.bar_secs",
        "layer": "③ 策略层 · L4",
        "default": 0,
        "kind": "本周期一根 bar 秒数（0=引擎按 source.freq 自动推导注入）",
        "step2": "单测/非标周期显式覆盖；自动推导依赖 PeriodProfile.bar_secs_for 与主程序对账",
    },
    {
        "path": "exit_params.session_end_hhmm",
        "layer": "③ 策略层 · L4",
        "default": "14:55",
        "kind": "收盘前强平阈值时刻",
        "step2": "效应随 bar_secs 变化（与 eod_lead_bars 协同）",
    },
    {
        "path": "risk.max_trades_per_day",
        "layer": "④ 风控层",
        "default": 20,
        "kind": "每日最大往返笔数",
        "step2": "15s 一天 480×N 信号，20 笔极易耗尽；须按周期放大",
    },
]


def period_sensitive_fields() -> List[Dict[str, Any]]:
    """返回周期敏感配置归总的副本（防止调用方改到模块级常量）。"""
    return [dict(f) for f in PERIOD_SENSITIVE_FIELDS]


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
