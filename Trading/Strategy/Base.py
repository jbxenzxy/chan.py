# -*- coding: utf-8 -*-
"""
策略层协议（出场 ExitPolicy / 入场 EntryPolicy 两个接口）
=========================================================
管道（engine/broker/source）不感知任何具体止盈止损逻辑，只调用这两个接口。

2026-09-08 精简：取消「策略选择器」抽象。
    生产环境入场只有一个策略 DefaultEntryPolicy、出场只有一个策略
    LayeredExitPolicy（L1-L4 分层），用户明确不会增加第二种，因此不再有
    注册表 / @register / build_*_policy 这类路由机制。main.py 与测试直接
    实例化这两个类即可。

出场判定返回 ExitCheck；若策略想顺带更新止盈止损（跟踪止损、移动止盈），
在 ExitCheck.plan 里带上新的 ExitPlan，引擎会持久化。
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Type

from ..Infra.InstrumentSpec import InstrumentSpec
from ..Infra.Types import Bar, Decision, Position, Side, Signal, ExitPlan


def _accepts_held_secs(fn: Any) -> bool:
    """策略的 check() 是否接受 held_secs 关键字（新签名）。"""
    try:
        return "held_secs" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


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
              bars_held: int = 0,
              held_secs: Optional[float] = None) -> Optional[ExitCheck]:
        """每根 K 线闭合后判定是否出场。返回 None = 继续持有。

        bars_held   已持有 K 线**根数** —— 跨周期不可比（30 根在 30m 下是
                    15 小时、在 15s 下是 7.5 分钟），只可用于计数/诊断，
                    **禁止**用于任何时间类兜底判定。
        held_secs   已持有**秒数** —— 跨周期可比，时间止损等一律用这个。
        """
        raise NotImplementedError

    # ---------- 引擎唯一调用入口（兼容旧签名策略） ----------
    def check_with(self, position: Position, bar: Bar, spec: InstrumentSpec,
                   bars_held: int = 0,
                   held_secs: Optional[float] = None) -> Optional["ExitCheck"]:
        """引擎应始终走这里，而不是直接调 check()。

        尚未升级的第三方/测试策略可能还是旧签名 `check(pos, bar, spec, bars_held)`，
        直接传 held_secs 会 TypeError。这里按签名自适应：
        支持 held_secs 就传（拿到跨周期可比的时间口径），不支持就退回只传根数
        ——宁可让老策略拿不到新能力，也不能让它直接崩在实盘路径上。
        """
        if _accepts_held_secs(type(self).check):
            return self.check(position, bar, spec, bars_held=bars_held,
                              held_secs=held_secs)
        return self.check(position, bar, spec, bars_held)

    # ---------- 可选钩子：周期注入 ----------
    def set_bar_secs(self, bar_secs: int) -> None:
        """引擎启动时把"当前周期一根 bar 多少秒"注入策略。

        默认实现只把值存下来；不依赖周期语义的策略无需重写。

        为什么要有这个钩子
            历史 bug：策略在 on_bar 里靠相邻 bar 的 timestamp 差**推断**
            bar 间隔，而 timestamp 单位不统一（SSE 毫秒 / 回放秒），
            推断代码把毫秒当秒比较 → 4 个周期全部推断失败、静默降级。
            周期是启动期就已知的确定信息，不该靠运行时推断。
        """
        self._injected_bar_secs = int(bar_secs or 0)

    def on_bar(self, bar: Bar, spec: InstrumentSpec) -> None:
        """可选钩子：引擎每根 K 线（无论是否持仓）都会调用一次。

        默认空实现。需要历史行情（如 ATR、跟踪极值）的策略可在此维护自己的
        缓冲——这样即使当前空仓，策略也能持续积累 bar，开仓瞬间就有足够历史。
        不属于出场判定的强制接口，子类按需重写。
        """
        return None


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
