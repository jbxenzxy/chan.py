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
    instrument: InstrumentSpec = field(default_factory=InstrumentSpec)
    risk: RiskConfig = field(default_factory=RiskConfig)
    entry_policy: Dict[str, Any] = field(default_factory=dict)
    exit_policy: Dict[str, Any] = field(default_factory=dict)
    source: Dict[str, Any] = field(default_factory=dict)
    broker: str = "dry_run"
    broker_params: Dict[str, Any] = field(default_factory=dict)
    state_dir: str = "./state"

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
    "broker": "dry_run",
    "state_dir": "./state",
    "instrument": {
        "signal_symbol": "KQ.m@CFFEX.IF",
        "trade_symbol": "CFFEX.IF2609",
        "price_tick": 0.2,
        "multiplier": 300.0,
        "open_fee_rate": 0.000023,
        "close_today_fee_rate": 0.000345,
        "close_fee_rate": 0.000023,
        "slippage_ticks": 1.0,
        "close_today_first": True,
        "sessions": ["09:30-11:30", "13:00-15:00"],
    },
    "risk": {
        "max_volume": 1,
        "max_trades_per_day": 20,
        "max_daily_loss_points": 60.0,
        "enforce_session": True,
        "no_open_after": "14:50",
        "close_before_session_end": True,
        "block_on_daily_loss": True,
    },
    "entry_policy": {
        "name": "DefaultEntryPolicy",
        "params": {
            "reverse_on_opposite_signal": False,
            "max_signal_range_points": 0.0,
            "min_stop_distance_points": 0.0,
        },
    },
    "exit_policy": {
        "name": "DefaultExitPolicy",
        "params": {
            "take_profit_points": 10.0,
            "stop_at_signal_extreme": True,
            "stop_buffer_ticks": 0.0,
            "max_hold_bars": 0,
        },
    },
    "source": {
        "type": "replay",
        "replay_dir": "./replay_data",
        "sse_base": "http://127.0.0.1:18081",
        "symbol": "KQ.m@CFFEX.IF",
        "freq": "5m",
    },
}
