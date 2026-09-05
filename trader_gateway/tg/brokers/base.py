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

Phase C（2026-09-05）
---------------------
submit() 改用 OrderIntent 显式表达意图（OPEN/UNLOCK/CLOSE/LOCK）。
旧调用方（action="open"|"close"）通过本类的 _resolve_intent() 自动映射到
对应 intent，行为不变；新接入的锁仓/解锁路径直接传 OrderIntent 即可。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from ..symbols import InstrumentSpec
from ..types import Order, OrderIntent, Side

BROKERS: Dict[str, Type["Broker"]] = {}


def register_broker(cls: Type["Broker"]) -> Type["Broker"]:
    BROKERS[cls.name] = cls
    return cls


def build_broker(name: str, spec: InstrumentSpec,
                 params: Optional[Dict[str, Any]] = None) -> "Broker":
    if name not in BROKERS:
        raise KeyError("未注册的 broker: {}（已注册: {}）".format(name, list(BROKERS)))
    return BROKERS[name](spec, params or {})


# intent → CTP OpenCloseType 的权威表（Phase C）
# 真实下单（simnow）按这张表映射；dry_run 同样遵守，便于回放日志审计。
INTENT_TO_OFFSET: Dict[OrderIntent, str] = {
    OrderIntent.OPEN: "OPEN",              # 首次开仓
    OrderIntent.UNLOCK: "CLOSEYESTERDAY",  # 解锁：只平昨仓（自动避开平今）
    OrderIntent.CLOSE: "CLOSE",            # 平仓：按 spec.close_today_first 决定 CloseToday / CloseAny
    OrderIntent.LOCK: "OPEN",              # 锁仓：与 OPEN 报文相同，但方向相反（净额对冲）
}


class Broker(ABC):
    name: str = "base"

    def __init__(self, spec: InstrumentSpec, params: Optional[Dict[str, Any]] = None):
        self.spec = spec
        self.params: Dict[str, Any] = dict(params or {})

    @abstractmethod
    def submit(self, intent: OrderIntent, side: Side, volume: int, ref_price: float,
               signal_key: str = "", note: str = "") -> Order:
        """提交委托并等待终态。

        intent: 订单意图（OrderIntent）
          - OPEN     顺势开仓
          - UNLOCK   解锁（只平昨）
          - CLOSE    平仓
          - LOCK     锁仓（开反向同手数）
        ref_price: 策略参考价（开仓=信号K线收盘价；平仓=触发价；锁仓/解锁同 CLOSE）
        """
        raise NotImplementedError

    @staticmethod
    def _resolve_intent(intent, side: Side,
                        close_today_first: bool = True) -> OrderIntent:
        """旧 action="open"|"close" 字符串 → OrderIntent 兼容层。

        新代码一律传 OrderIntent；旧代码（如外部脚本/老测试）传字符串 action 也照常工作：
          - "open"  → OrderIntent.OPEN
          - "close" → OrderIntent.CLOSE
          - None    → 默认按 OPEN（防御性兜底，理论上不应发生）
          - 已是 OrderIntent → 原样返回
        """
        if intent is None:
            return OrderIntent.OPEN
        if isinstance(intent, OrderIntent):
            return intent
        s = str(intent).strip().lower()
        if s == "open":
            return OrderIntent.OPEN
        if s in ("close", "close_hard"):
            return OrderIntent.CLOSE
        if s == "unlock":
            return OrderIntent.UNLOCK
        if s == "lock":
            return OrderIntent.LOCK
        raise ValueError("未知的 broker.submit intent/action: {!r}".format(intent))

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

    def trade_confirmed(self, intent: "OrderIntent", signal_key: str = "") -> bool:
        """真实成交是否到位（Phase F：UNLOCK 卡单检测）。

        用于引擎 UNLOCK 卡单 5 bars 后复核：CTP broker 在 submit 返回 filled 后，
        实际可能未成交（tqsdk 持仓缓存乐观 / CTP 通道异常）。这里要求 broker
        给出基于真实成交明细（如 ``order.trade_records``）的二次判定。

        默认 True（基类兜底：撮合同步的 broker 直接信 submit 返回）。
        dry_run 重写显式 True；SimNow 重写查 tqsdk order.trade_records 累计成交 ≥ volume。
        """
        return True

    def equity(self, source: str = "available") -> Optional[float]:
        """账户权益查询（可选实现）。仓位管理（PositionSizer）用。

        source: "available"=可用资金（已扣保证金占用与挂单冻结）
                "balance"  =总资产权益（含浮盈、未扣占用）
        返回 None 表示取不到（离线通道 / 未登录 / 查询失败），
        此时 PositionSizer 会按 fallback_volume 保守回退，不会乱开仓。
        """
        return None

    def close(self) -> None:
        pass
