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
    max_volume: int = 1                      # 单笔手数上限（也用于开仓手数）
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
    broker: str = "dry_run"                                             # 执行通道：dry_run / simnow
    broker_params: Dict[str, Any] = field(default_factory=dict)         # broker 专属参数（超价/超时/追价等，见 DEFAULT_CONFIG）
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
    # 执行通道："dry_run"=本地模拟撮合（离线可跑，不连 CTP）；"simnow"=SimNow 仿真真实下单
    "broker": "dry_run",
    # broker 专属参数（仅 simnow 生效；dry_run 忽略）
    "broker_params": {
        "overprice_points": 0.6,      # 超价点数：下单价 = 实时对手价(买=ask/卖=bid) ± 此值，朝成交方向取整到 tick。0.6 = IF 3 个 tick
        "fill_timeout_open": 5.0,     # 开仓委托等待成交秒数，超时撤单 → 本笔作废，等下一信号（卡单保护）
        "fill_timeout_close": 5.0,    # 平仓委托每轮等待成交秒数，超时撤单 → 进入下一轮追价
        "close_max_chase": 20,        # 平仓追价最大轮数：每轮都按"最新对手价 ± overprice"重新定价，直到成交或用尽轮数
        "close_chase_ticks": 2,       # 平仓追价兜底步长：仅在行情临时取不到时，在上一笔限价基础上朝成交方向推几跳
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
        "overprice_points": 0.6,            # 超价点数默认值（broker_params 未配置时的兜底，建议与 broker_params 保持一致）
        "close_today_first": True,          # 平仓优先平今仓（True=先平当日仓，受平今费率影响时关注）
        "sessions": ["09:30-11:30", "13:00-15:00"],  # 交易时段（风控 enforce_session 与收盘强平都按此判断）
    },
    # 风控参数
    "risk": {
        "max_volume": 1,                    # 单笔委托手数上限（也是开仓手数）
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
