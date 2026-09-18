# -*- coding: utf-8 -*-
"""
信号源接口（双通道：实时 SSE / 离线回放）
=========================================
两个源对外产出完全相同的事件流：("bar", Bar) 与 ("signal", Signal)。
引擎不区分自己是在跑实盘还是在重放昨天的录制数据——这是能"用历史数据
秒级验证新策略"的前提。

新增数据源（比如接 tqsdk 直连行情）只需实现 events()，其余不动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Type

from ..Infra.Instrument import Instrument
from ..Infra.Records import Bar, Signal

SOURCES: Dict[str, Type["Source"]] = {}

Event = Tuple[str, Any]     # ("bar", Bar) | ("signal", Signal)


def register_source(cls: Type["Source"]) -> Type["Source"]:
    SOURCES[cls.name] = cls
    return cls


def build_source(name: str, params: Dict[str, Any], state: "Instrument") -> "Source":
    if name not in SOURCES:
        raise KeyError("未注册的信号源: {}（已注册: {}）".format(name, list(SOURCES)))
    return SOURCES[name](params or {}, state)


class Source(ABC):
    name: str = "base"

    # P68k：空闲回调（可选）。实时源每帧（含心跳注释帧）在帧处理之前调用
    # 一次（若已注入）——心跳帧经 iter_sse 产出空 data、随后被丢弃，不会被
    # "处理"，故挂点必须排在丢弃之前；先泵后交帧是有意的（见 SSE.py 注释）。
    # 用于驱动 broker 的回报泵（tqsdk wait_update 单线程，见 SimNow.pulse）。
    # 由装配方（main.py）注入，Source 不感知 broker —— 维持单向依赖。
    on_idle: Optional[Callable[[], None]] = None

    def __init__(self, params: Dict[str, Any], state: "Instrument"):
        # D-C：属性名统一为 state（原 self.spec 别名已删，
        #   与 Broker / Engine 同名，全仓只有"运行时对象"这一个说法）。
        self.params = dict(params or {})
        self.state = state

    @abstractmethod
    def events(self) -> Iterator[Event]:
        raise NotImplementedError

    def close(self) -> None:
        pass
