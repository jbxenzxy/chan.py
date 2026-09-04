# -*- coding: utf-8 -*-
"""
Broker 接口（可插拔点 ③）
=========================
引擎只认这个接口，不认 SimNow / 创元 / dry-run。
接真实账户时新建一个类实现 `submit()`，在 config.json 里换 name 即可，
引擎与策略层一行都不用改。

submit() 被设计成**同步返回 Order**，是为了让 dry-run 与真实 CTP 语义统一：
真实 CTP 是异步回执，届时在 broker 内部用 wait_update 阻塞到终态再返回，
对外仍是同步的。这样引擎的状态机不用为异步改写成回调地狱。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from ..symbols import InstrumentSpec
from ..types import Order, Side

BROKERS: Dict[str, Type["Broker"]] = {}


def register_broker(cls: Type["Broker"]) -> Type["Broker"]:
    BROKERS[cls.name] = cls
    return cls


def build_broker(name: str, spec: InstrumentSpec,
                 params: Optional[Dict[str, Any]] = None) -> "Broker":
    if name not in BROKERS:
        raise KeyError("未注册的 broker: {}（已注册: {}）".format(name, list(BROKERS)))
    return BROKERS[name](spec, params or {})


class Broker(ABC):
    name: str = "base"

    def __init__(self, spec: InstrumentSpec, params: Optional[Dict[str, Any]] = None):
        self.spec = spec
        self.params: Dict[str, Any] = dict(params or {})

    @abstractmethod
    def submit(self, action: str, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        """提交委托并等待终态。

        action: "open" | "close"
        ref_price: 策略参考价（开仓=信号K线收盘价；平仓=触发价）
        """
        raise NotImplementedError

    def pulse(self) -> None:
        """心跳（可选实现）。引擎每处理一根 K 线调一次。

        真实 CTP 通道（如 SimNow）需要在长连接空闲期定期收发数据，否则会被
        判为"用户不活跃"而断连。离线通道（dry_run）无需实现。
        """

    def real_position(self, side: "Side") -> Optional[int]:
        """真实持仓查询（可选实现）。引擎持仓对账（增强 B）用。

        返回该方向当前真实持仓手数；不支持/未知返回 None（引擎跳过对账）。
        默认实现返回 None（如 dry_run 离线通道，没有真实账户可查）。
        """
        return None

    def close(self) -> None:
        pass
