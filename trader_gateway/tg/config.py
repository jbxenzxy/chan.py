# -*- coding: utf-8 -*-
"""
配置加载
========
用 JSON 而非 YAML——零第三方依赖，随项目拷走就能跑。
策略参数以 dict 形式原样传给对应 Policy 类，新增参数不需要改这里。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict

from .symbols import InstrumentSpec


@dataclass
class RiskConfig:
    max_volume: int = 2                      # 单笔手数上限（也用于开仓手数；非仓位管理下=每次入场固定手数）
    max_open_positions: int = 1              # Phase E3（2026-09-05）：同时持仓数上限；N≥1 同 K 线连开场景下 >1
                                             #   1 = 单仓（v1 行为，向后兼容）
                                             #   N = 一次入场可连开 N 单（broker 收 N 笔独立报单）
                                             # 注意：实际单笔手数仍由 max_volume 限制 —— max_open_positions 决定"几笔"，
                                             # max_volume 决定"每笔几手"。两者独立，可分别 cfg 化。
    initial_cash: float = 10000000.0         # 虚拟初始资金（dry_run 无真实账户时用），资金闸门定 X=floor(available/K) 用
    max_trades_per_day: int = 20             # 每日最大往返笔数
    max_daily_loss_points: float = 60.0      # 每日最大净亏（点数），触达后停止开仓
    enforce_session: bool = True             # 只在交易时段内开仓
    no_open_after: str = "14:50"             # 尾盘不再开新仓（空串=不限制）
    close_before_session_end: bool = True    # 收盘前强平（由引擎在时段外收到 bar 时处理）
    block_on_daily_loss: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RiskConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class GatewayConfig:
    instrument: InstrumentSpec = field(default_factory=InstrumentSpec)  # 合约规格：标的、tick、乘数、费率、交易时段
    risk: RiskConfig = field(default_factory=RiskConfig)                # 风控参数：手数/日笔数/日亏上限/时段限制
    entry_policy: Dict[str, Any] = field(default_factory=dict)          # 开仓策略参数（原样传给 Policy 类）
    exit_policy: Dict[str, Any] = field(default_factory=dict)           # 出场策略参数（止盈/止损/最长持仓）
    source: Dict[str, Any] = field(default_factory=dict)                # 行情源：replay 回放 / sse 实时
    broker: str = "dry_run"                                             # 执行通道：dry_run(离线模拟) / simnow(仿真) / live(实盘CTP)
    broker_params: Dict[str, Any] = field(default_factory=dict)         # broker 专属参数（超价/超时/追价等，见 DEFAULT_CONFIG）
    sizing: Dict[str, Any] = field(default_factory=dict)                # 仓位管理参数（手数定档，默认关闭=固定手数，见 tg/sizing.py）
    state_dir: str = "./state"                                          # 运行时状态目录（state.db / events.jsonl / orders.jsonl）

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GatewayConfig":
        d = d or {}
        return cls(
            instrument=InstrumentSpec.from_dict(d.get("instrument") or {}),
            risk=RiskConfig.from_dict(d.get("risk") or {}),
            entry_policy=d.get("entry_policy") or {},
            exit_policy=d.get("exit_policy") or {},
            source=d.get("source") or {},
            broker=d.get("broker") or "dry_run",
            broker_params=d.get("broker_params") or {},
            sizing=d.get("sizing") or {},
            state_dir=d.get("state_dir") or "./state",
        )

    @classmethod
    def load(cls, path: str) -> "GatewayConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save_example(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


DEFAULT_CONFIG: Dict[str, Any] = {
    # 执行通道："dry_run"=本地模拟撮合（离线可跑，不连 CTP）；"simnow"=SimNow 仿真真实下单；
    #           "live"=实盘 CTP（如创元期货，需 broker_params.tq_market=期货公司名 +
    #           confirm_live_trading=true 双确认）
    "broker": "dry_run",
    # broker 专属参数（仅 simnow/live 生效；dry_run 忽略）
    "broker_params": {
        "overprice_points": 1.0,      # 超价点数：下单价 = 实时对手价(买=ask/卖=bid) ± 此值，朝成交方向取整到 tick。
                                      # IF 每 tick 0.2 点：1.0 = 让 5 个 tick。
                                      # 全部报单都是 FOK —— 限价内盘口累计深度 ≥ 报单手数才全成，
                                      # 超价越厚、能吃到的价位档越多，全成概率越高。四类报单共用此值。
        # ── 报单类型（2026-09-06 全 FOK 重构）──
        #   开仓 OPEN / 解锁 UNLOCK / 锁仓 LOCK / 平仓 CLOSE 四类报单**全部 FOK**：
        #   限价立即全部成交否则全部撤销，由交易所撮合引擎强制执行，无部分成交残留。
        #   入场（OPEN/UNLOCK）不成交 → 整笔作废，不追价，等下一信号；
        #   离场（LOCK/CLOSE）不成交 → 立即按最新对手价重报，直到成交（见 close_max_chase）。
        #   注意：郑商所期货不支持 FOK（tqsdk 直接抛异常，暂不处理）；
        #         中金所 IF/IH/IC/IM 限价+FOK 官方支持，本项目只交易中金所金融期货。
        "fill_timeout_open": 5.0,     # 入场报单等待终态秒数。FOK 下交易所瞬间给终态，
                                      # 本值退化为通道异常兜底 watchdog（回报丢失时防 submit 永久挂死）
        "fill_timeout_close": 5.0,    # 离场报单每轮等待终态秒数；本轮未成交则立即重报追价
        "close_max_chase": 20,        # 离场追价最大轮数：每轮都按"最新对手价 ± overprice"重新定价，
                                      # 直到成交或用尽轮数（引擎还会跨 K 线继续重试，实际=直到成交）
        "close_chase_ticks": 2,       # 离场追价兜底步长：仅在行情临时取不到时，在上一笔限价基础上朝成交方向推几跳
        "chase_interval": 1.0,        # 离场追价重报间隔秒数：FOK 全撤后隔此间隔再报下一轮，
                                      # 防报撤单频率超限（CTP 对高频报撤有监控阈值）
        "connect_retries": 3,         # 登录重试次数（CTP 对短连接敏感，"用户不活跃"时重试通常能连上）
        "connect_backoff": 5.0,       # 登录失败后的首轮退避秒数（每轮 ×1.5：5s → 7.5s → 11.25s）
        # ↑ 单一事实源：broker 参数默认值只在本表维护，tg/brokers/simnow.py 不再自带兜底
        # ── 账户选择（2026-09-05）：SimNow 仿真 ↔ 实盘 CTP ──
        "tq_market": "simnow",        # 天勤 TqAccount 接入市场：simnow=仿真；实盘填期货公司名（如"创元期货"）
        "confirm_live_trading": False,# 实盘安全闸门：broker="live" 或 tq_market≠simnow 时必须显式 true，否则拒绝启动
        "live_account": "",           # 实盘资金账号（也可环境变量 LIVE_ACCOUNT）
        "live_password": "",          # 实盘资金密码（也可环境变量 LIVE_PASSWORD；建议仅用环境变量，不落盘）
        # 仿真账号沿用 sn_account/sn_password（环境变量 SN_ACCOUNT/SN_PASSWORD），
        # 天勤账号 tq_account/tq_password（环境变量 TQ_ACCOUNT/TQ_PASSWORD）两种模式共用。
    },
    # 仓位管理（手数定档）。默认 enabled=False —— 开几手完全沿用 risk.max_volume，
    # 与加这个模块之前的行为逐字节一致；不查账户、不联网，零风险引入。
    # 想"全仓开满"或"按风险预算定手数"时再把 enabled 打开，详见 tg/sizing.py。
    # ⚠ 一笔报单最多 20 手（中金所限价单硬性上限，IF/IH/IC/IM 同）：
    #   无论哪种模式，一个信号一次报出去的手数都不能超过 20，超了交易所直接拒单
    #   （代码也会在开仓前拦一道：signal_action=rejected / reason=over_exchange_limit）。
    "sizing": {
        "enabled": False,                # 总开关：False=关闭仓位管理（固定手数）；True=按下面的 mode 定手数
        "mode": "fixed",                 # 手数定档模式：fixed 固定手数 | capital_pct 按保证金占比 | atr_risk 按风险敞口
        "fixed_volume": 0,               # 【一笔挂多少手】fixed 模式下一个信号一次报单的手数。
                                         #   0=沿用 risk.max_volume（默认 2 手）。⚠ 不能超过 20（见上方说明）
        "capital_pct": 0.50,             # capital_pct 模式：这笔仓位最多占用权益的比例（0.50=50%）
        "risk_per_trade_pct": 0.01,      # atr_risk 模式：这笔最多亏掉权益的比例（0.01=1%，固定分数法）
        "margin_rate": 0.15,             # 保证金率，用于 capital_pct 折算每手占用（IF 一般 12%-15%）
        "risk_unit_pct": 0.01,           # 资金门槛缓冲：每手名义价值预留的波动比例。
                                         #   开 1 手最低门槛 K = 一手保证金 + 名义价值×risk_unit_pct；
                                         #   X = floor(可用资金/K) 为资金闸门定的最多可开手数（见 engine._capital_gate）
        "max_volume": 0,                 # 仓位管理算法结果的截断上限；0=默认中金所单笔上限 20（无需显式设）
        "min_volume": 1,                 # 手数下限：算出来不足时提升到该值（1=信号来了就至少开 1 手；设 0 则真的不开）
        "fallback_volume": 1,            # 权益/ATR 取不到时的回退手数（保守值，避免因查询失败而乱开仓）
        "equity_source": "available",    # 权益口径：available=可用资金（已扣保证金占用）| balance=总资产权益
        "unlock_no_new_open": True,      # 解锁昨仓后是否补开今仓的缺额。
                                         #   True =绝不补开（默认）：只解锁昨仓、缺口放弃，
                                         #        规避金融期货"平今"高手续费坑
                                         #   False=补开：昨日锁 3 手、今日信号算 5 手
                                         #        → 解锁 3 手后再开 2 手，净敞口到 5 手
                                         #   两种情况下解锁本身照常执行（解锁是减风险动作）
    },
    # 运行时状态目录：state.db（信号去重/持仓/日统计）、events.jsonl、orders.jsonl 都落在这里
    "state_dir": "./state",
    # 合约规格
    "instrument": {
        "signal_symbol": "KQ.m@CFFEX.IF",   # 缠论分析用的主连合约（KQ.m@ 自动映射主力）
        "trade_symbol": "CFFEX.IF2609",     # 实际下单的月份合约（主连解析失败时兜底用）
        "price_tick": 0.2,                  # 最小变动价位（IF=0.2 点），所有挂单价都取整到此粒度
        "multiplier": 300.0,                # 合约乘数（元/点），点数盈亏 × 此值 = 金额盈亏
        "open_fee_rate": 0.000023,          # 开仓手续费率（占成交金额比例），折算进成本点数
        "close_today_fee_rate": 0.000345,   # 平今手续费率（中金所期指很贵，是开仓的 15 倍）
        "close_fee_rate": 0.000023,         # 平昨手续费率
        "slippage_ticks": 1.0,              # 单边滑点（tick 数）：仅 dry_run 模拟成交与成本展示用，不参与实盘定价
        "overprice_points": 0.6,            # （已废弃，仅为向后兼容保留）超价默认值统一在 broker_params.overprice_points，simnow 不再读本字段
        "close_today_first": True,          # 平仓优先平今仓（True=先平当日仓，受平今费率影响时关注）
        "sessions": ["09:30-11:30", "13:00-15:00"],  # 交易时段（风控 enforce_session 与收盘强平都按此判断）
    },
    # 风控参数
    "risk": {
        "max_volume": 2,                    # 单笔委托手数上限（也是开仓手数；非仓位管理下=每次入场固定手数）
        "max_trades_per_day": 20,           # 每日最大往返笔数，超过后当日不再开新仓
        "max_daily_loss_points": 60.0,      # 每日最大净亏（点数），触达后当日停止开仓（平仓不受限）
        "enforce_session": True,            # 只在 instrument.sessions 时段内开仓（按 K 线时间判断，非墙钟）
        "no_open_after": "14:50",           # 该时刻后不再开新仓（避免尾盘开仓来不及出场；空串=不限制）
        "close_before_session_end": True,   # 收盘前强平：时段外收到 K 线时若仍持仓则市况平仓
        "block_on_daily_loss": True,        # 日亏触达 max_daily_loss_points 后是否真的拦截开仓
    },
    # 开仓策略参数（params 原样传给 DefaultEntryPolicy）
    "entry_policy": {
        "name": "DefaultEntryPolicy",
        "params": {
            "reverse_on_opposite_signal": False,     # 持仓时出现反向信号是否反手（True=平仓并反向开仓）
            "max_signal_range_points": 0.0,          # 信号 K 线最大振幅（点数）过滤，0=不过滤（振幅过大不开仓）
            "min_stop_distance_points": 0.0,         # 止损位与入场价最小距离（点数），0=不校验
        },
    },
    # 出场策略参数（params 原样传给对应 Policy 类；切换策略只需改 name）
    #   DefaultExitPolicy   = 简单固定点数/信号极值（无需历史，零依赖）
    #   LayeredExitPolicy   = 标准分层组合：R 倍数基线 → ATR 宽窄 → 保本/跟踪 → 时间/收盘兜底
    # 下面默认启用 LayeredExitPolicy（每层都可单独开关做 A/B，详见 tg/strategy/layered_exit.py）
    "exit_policy": {
        "name": "LayeredExitPolicy",
        "params": {
            # ---- L1 R 倍数定基线 ----
            "initial_risk_points": 10.0,       # 固定初始风险（点数）；ATR 不可用时作为兜底基线
            "stop_at_signal_extreme": True,    # True=用信号 K 线极值作结构止损（多=信号K最低价）；False=用上面固定点数
            "stop_buffer_ticks": 0.0,          # 止损位额外让出的 tick 缓冲，0=严格按基线
            "r_multiple_tp": 2.0,              # 止盈 = 入场价 ± r_multiple_tp × R（默认 1:2 盈亏比）
            "min_r_points": 2.0,               # R 下限（点数），防止极端行情下止损过窄
            # ---- L2 波动率(ATR)定宽窄 ----
            "use_atr": True,                   # True=用 ATR 自适应止损/止盈宽度（行情宽则宽、窄则窄）
            "atr_period": 14,                  # ATR 计算周期
            "atr_sl_multiple": 2.0,           # 初始止损距离 = atr_sl_multiple × ATR
            # ---- L3 移动/保本锁利 ----
            "use_trailing": True,              # True=启用保本 + 跟踪止损
            "breakeven_trigger_r": 1.0,        # 浮盈 ≥ 此倍数 × R 时，止损抬至保本（入场价±缓冲）
            "breakeven_buffer_ticks": 0.0,     # 保本位缓冲 tick，0=严格保本
            "trailing_trigger_r": 2.0,         # 浮盈 ≥ 此倍数 × R 时，启动 ATR 跟踪止损
            "trailing_atr_multiple": 1.5,      # 跟踪止损距离 = trailing_atr_multiple × ATR
            "trailing_distance_points": 0.0,   # ATR 不可用时的跟踪兜底距离（点数），0=无兜底则不做跟踪
            # ---- L4 时间/收盘兜底 ----
            "max_hold_bars": 30,               # 最长持仓 K 线数，超时强制平仓，0=不限时长
            "session_end_hhmm": "14:55",       # 该时刻及之后强制平仓（""=不启用）；引擎另有收盘强平兜底
        },
    },
    # 行情源
    "source": {
        "type": "replay",                            # "replay"=回放本地 K 线（离线测试）；"sse"=实时订阅
        "replay_dir": "./replay_data",               # replay 模式的 K 线数据目录
        "sse_base": "http://127.0.0.1:18081",        # sse 模式的行情服务地址
        "symbol": "KQ.m@CFFEX.IF",                   # 订阅的合约（与 signal_symbol 一致）
        "freq": "5m",                                # K 线周期（5 分钟）
    },
}
