# -*- coding: utf-8 -*-
"""
策略层协议（可插拔点 ①②）
==========================
这里是**唯一**需要为了"换策略"而改代码的地方。管道（engine/broker/source）不感知
任何具体止盈止损逻辑，只调用这两个接口。

换策略的正确姿势
    新建一个 py 文件 → 继承 ExitPolicy / EntryPolicy → 用 @register 装饰
    → config.json 里把 name 改成你的类名。其余代码一行不动。

出场判定返回 ExitCheck；若策略想顺带更新止盈止损（跟踪止损、移动止盈），
在 ExitCheck.plan 里带上新的 ExitPlan，引擎会持久化。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Type

from ..symbols import InstrumentSpec
from ..types import Bar, Decision, Position, Side, Signal, ExitPlan

EXIT_POLICIES: Dict[str, Type["ExitPolicy"]] = {}
ENTRY_POLICIES: Dict[str, Type["EntryPolicy"]] = {}


def register_exit(cls: Type["ExitPolicy"]) -> Type["ExitPolicy"]:
    EXIT_POLICIES[cls.name] = cls
    return cls


def register_entry(cls: Type["EntryPolicy"]) -> Type["EntryPolicy"]:
    ENTRY_POLICIES[cls.name] = cls
    return cls


def build_exit_policy(name: str, params: Optional[Dict[str, Any]] = None) -> "ExitPolicy":
    if name not in EXIT_POLICIES:
        raise KeyError(
            "未注册的出场策略: {}（已注册: {}）".format(name, list(EXIT_POLICIES)))
    return EXIT_POLICIES[name](params or {})


def build_entry_policy(name: str, params: Optional[Dict[str, Any]] = None) -> "EntryPolicy":
    if name not in ENTRY_POLICIES:
        raise KeyError(
            "未注册的入场策略: {}（已注册: {}）".format(name, list(ENTRY_POLICIES)))
    return ENTRY_POLICIES[name](params or {})


@dataclass
class ExitCheck:
    """出场判定结果。price 是"触发价"，不是最终成交价（成交价由 broker 决定）。

    only_update=True 表示"只更新出场计划、不登场"——移动止损/跟踪止盈走这条路。
    此时 plan 必须给，price 无意义。
    """
    reason: str                       # tp / sl / time / trailing / custom
    price: float
    plan: Optional[ExitPlan] = None   # 非空则替换持仓的出场计划
    only_update: bool = False


class ExitPolicy(ABC):
    """出场策略接口。"""
    name: str = "base"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params: Dict[str, Any] = dict(params or {})

    def describe(self) -> str:
        return "{}({})".format(self.name, self.params)

    @abstractmethod
    def plan(self, signal: Signal, entry_price: float,
             spec: InstrumentSpec) -> ExitPlan:
        """开仓时生成出场计划（止损价 / 止盈价）。"""
        raise NotImplementedError

    @abstractmethod
    def check(self, position: Position, bar: Bar, spec: InstrumentSpec,
              bars_held: int = 0) -> Optional[ExitCheck]:
        """每根 K 线闭合后判定是否出场。返回 None = 继续持有。"""
        raise NotImplementedError


class EntryPolicy(ABC):
    """入场策略接口。"""
    name: str = "base"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.params: Dict[str, Any] = dict(params or {})

    def describe(self) -> str:
        return "{}({})".format(self.name, self.params)

    @abstractmethod
    def decide(self, signal: Signal, position: Optional[Position],
               spec: InstrumentSpec) -> Decision:
        """收到新信号时决定动作。"""
        raise NotImplementedError
