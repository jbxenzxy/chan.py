# -*- coding: utf-8 -*-
"""
核心数据类型
============
设计原则：
  ① 纯数据 + 无业务逻辑，方便序列化（sqlite / jsonl / 回放）
  ② 幂等键 `Signal.make_key` 必须与 M0 录制器 `bsp_key()` 完全一致，
     否则回放源与实时源会产生不同的去重结果（这是最容易埋雷的地方）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, Optional

CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> str:
    """北京时间 ISO 字符串（秒精度）。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


class Side(Enum):
    """持仓/信号方向。value 即符号，可直接参与盈亏乘算。"""
    LONG = 1
    SHORT = -1

    @property
    def sign(self) -> int:
        return self.value

    @classmethod
    def from_is_buy(cls, is_buy: bool) -> "Side":
        return cls.LONG if is_buy else cls.SHORT

    def __str__(self) -> str:
        return "多" if self is Side.LONG else "空"


class DecisionType(Enum):
    """入场策略给出的动作类型。"""
    OPEN = "open"                    # 开仓
    CLOSE_AND_HOLD = "close_hold"    # 只平仓，不反手（反向信号默认走这条）
    CLOSE_AND_REVERSE = "reverse"    # 平仓并反手开新仓
    SKIP = "skip"                    # 忽略


# ─────────────────────────────────────────────
# Phase A 命名固化（2026-09-05）
# ─────────────────────────────────────────────
# 4 种订单意图、2 种入场模式、2 种离场方式、4 态引擎状态机 —— 全部用枚举固化。
# 引入后默认行为零变化（Position.entry_mode 默认 OPEN_FIRST，与旧持仓记录兼容）；
# 真正的报文区分（Phase C）和入场方式决定离场方式（Phase D）后续 phase 落地。
class OrderIntent(str, Enum):
    """订单意图（CTP OpenCloseType 映射的源头）。"""
    OPEN = "open"        # 首次开仓        → CTP OpenCloseType=Open
    UNLOCK = "unlock"    # 解锁（只平昨）  → CTP OpenCloseType=CloseYesterday
    CLOSE = "close"      # 平仓           → 按 spec.close_today_first 决定 CloseToday/Auto
    LOCK = "lock"        # 锁仓（开反向）  → CTP OpenCloseType=Open（与 OPEN 报文相同但语义不同）


class EntryMode(str, Enum):
    """入场模式（决定了离场方式）。"""
    OPEN_FIRST = "open_first"          # 今日新开 → 离场用 LOCK_SOFT（避免平今 15× 费率）
    UNLOCK_FIRST = "unlock_first"      # 解锁昨日锁仓 → 离场用 CLOSE_HARD（平昨无费率问题）


class ExitMode(str, Enum):
    """离场方式（Phase D 由 entry_mode 自动决定，不留配置开关）。"""
    CLOSE_HARD = "close_hard"          # 平仓硬离场
    LOCK_SOFT = "lock_soft"            # 锁仓软离场（开反向同手数）


class EngineState(str, Enum):
    """引擎状态机（4 态，Phase A）。"""
    IDLE = "idle"              # 0. 无持仓，等待入场信号
    OPENING = "opening"        # 1. 正在开仓（瞬态：下单到成交之间）
    IN_TRADE = "in_trade"      # 2. 已持仓，等待离场条件
    EXITING = "exiting"        # 3. 正在离场（瞬态：下单到成交之间）


@dataclass
class Signal:
    """缠论买卖点信号。字段与 chan.py SSE 快照中的 bsps[] 一一对应。"""
    key: str
    symbol: str
    freq: str
    date: str            # 信号 K 线时间，如 "2026-09-01 09:35"
    timestamp: int       # 毫秒时间戳
    bsp_type: str        # 买卖点类型 "1" "2" "3" "0"
    is_buy: bool
    price: float         # 信号 K 线收盘价（= 入场价）
    high: float          # 信号 K 线最高价（空头止损位）
    low: float           # 信号 K 线最低价（多头止损位）
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> Side:
        return Side.from_is_buy(self.is_buy)

    @staticmethod
    def make_key(date: str, bsp_type: str, is_buy: bool) -> str:
        """幂等键。刻意不含价格——笔端点位移会让 date 变化，此时视为新信号。"""
        return "{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S")

    @classmethod
    def from_bsp(cls, b: Dict[str, Any], symbol: str, freq: str) -> "Signal":
        date = str(b.get("date", ""))
        btype = str(b.get("type", ""))
        is_buy = bool(b.get("is_buy"))
        return cls(
            key=cls.make_key(date, btype, is_buy),
            symbol=symbol, freq=freq, date=date,
            timestamp=int(b.get("timestamp") or 0),
            bsp_type=btype, is_buy=is_buy,
            price=float(b.get("price") or 0.0),
            high=float(b.get("high") or 0.0),
            low=float(b.get("low") or 0.0),
            extra={k: v for k, v in b.items()
                   if k not in ("date", "type", "is_buy", "price", "high", "low", "timestamp")},
        )

    @classmethod
    def from_m0_record(cls, rec: Dict[str, Any]) -> "Signal":
        """从 M0 录制器 signals.json 的单条记录还原（取 final 快照）。"""
        final = rec.get("final") or {}
        return cls(
            key=rec["key"], symbol=rec.get("symbol", ""), freq=rec.get("freq", ""),
            date=rec.get("date", ""), timestamp=int(rec.get("timestamp") or 0),
            bsp_type=str(rec.get("type", "")), is_buy=bool(rec.get("is_buy")),
            price=float(final.get("price") or 0.0),
            high=float(final.get("high") or 0.0),
            low=float(final.get("low") or 0.0),
            extra={"disappear_count": rec.get("disappear_count", 0),
                   "revisions": len(rec.get("revisions") or [])},
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Bar:
    """已闭合的 K 线。出场判定只信任 high/low 极值，不猜盘中路径。"""
    timestamp: int
    date: str
    open: float
    high: float
    low: float
    close: float
    vol: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bar":
        return cls(
            timestamp=int(d.get("timestamp") or 0), date=str(d.get("date", "")),
            open=float(d.get("open") or 0.0), high=float(d.get("high") or 0.0),
            low=float(d.get("low") or 0.0), close=float(d.get("close") or 0.0),
            vol=d.get("vol"),
        )


@dataclass
class Order:
    """委托单。dry-run 下撮合是同步的，但字段按真实 CTP 回执的形状设计。"""
    order_id: str
    signal_key: str
    symbol: str
    side: Side
    action: str                 # "open" | "close"
    volume: int
    price: float                # 委托价（已对齐 price_tick）
    req_price: float = 0.0      # 策略原始价（未对齐）
    filled_price: Optional[float] = None
    status: str = "pending"     # pending / filled / rejected
    created_at: str = ""
    broker: str = ""
    note: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.name
        return d


@dataclass
class ExitPlan:
    """出场计划。由 ExitPolicy.plan() 生成，随持仓持久化。

    params 会原样落盘——将来做参数敏感性分析时，
    只看事件日志就能知道"这一笔当时用的是哪套止盈止损"。
    """
    name: str
    stop_price: float
    tp_price: Optional[float] = None
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExitPlan":
        return cls(name=d.get("name", "unknown"),
                   stop_price=float(d.get("stop_price") or 0.0),
                   tp_price=d.get("tp_price"),
                   params=d.get("params") or {})


@dataclass
class Position:
    """单策略实例的持仓。v1 只支持"一个实例最多一手"，不做锁仓。"""
    symbol: str
    side: Side
    volume: int
    entry_price: float
    entry_at: str
    entry_bar_ts: int
    signal_key: str
    open_order_id: str
    exit_plan: ExitPlan
    entry_bar_seq: int = 0        # 入场时的 bar 序号（计算持有根数、跳过入场K线）
    # Phase A：入场模式，默认 OPEN_FIRST —— 旧持仓记录无此字段也能正常反序列化
    # Phase D 由 entry_mode 自动决定 exit_mode（OPEN_FIRST→LOCK_SOFT / UNLOCK_FIRST→CLOSE_HARD）
    # str Enum 单例不可变，可直接做 dataclass default（不像 list/dict 需要 default_factory）
    entry_mode: EntryMode = EntryMode.OPEN_FIRST

    def pnl_points(self, price: float) -> float:
        """未扣成本的毛盈亏（点数）。"""
        return (price - self.entry_price) * self.side.sign

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side.name, "volume": self.volume,
            "entry_price": self.entry_price, "entry_at": self.entry_at,
            "entry_bar_ts": self.entry_bar_ts,
            "entry_bar_seq": self.entry_bar_seq,
            "signal_key": self.signal_key,
            "open_order_id": self.open_order_id,
            "exit_plan": self.exit_plan.to_dict(),
            "entry_mode": self.entry_mode.value,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        em_raw = d.get("entry_mode", EntryMode.OPEN_FIRST.value)
        # EntryMode 接受同模块字符串值；缺/坏值回退到 OPEN_FIRST（向后兼容旧持仓记录）
        try:
            entry_mode = EntryMode(em_raw) if isinstance(em_raw, str) else EntryMode.OPEN_FIRST
        except ValueError:
            entry_mode = EntryMode.OPEN_FIRST
        return cls(symbol=d["symbol"], side=Side[d["side"]], volume=int(d["volume"]),
                   entry_price=float(d["entry_price"]), entry_at=d.get("entry_at", ""),
                   entry_bar_ts=int(d.get("entry_bar_ts") or 0),
                   entry_bar_seq=int(d.get("entry_bar_seq") or 0),
                   signal_key=d.get("signal_key", ""),
                   open_order_id=d.get("open_order_id", ""),
                   exit_plan=ExitPlan.from_dict(d.get("exit_plan") or {}),
                   entry_mode=entry_mode)


@dataclass
class Trade:
    """一笔完整往返交易（开 + 平）。落盘用于统计与参数敏感性分析。"""
    trade_id: str
    symbol: str
    side: Side
    volume: int
    entry_price: float
    exit_price: float
    entry_at: str
    exit_at: str
    reason: str                 # tp / sl / signal_reverse / eod / manual
    gross_points: float
    cost_points: float
    net_points: float
    net_cash: float
    bars_held: int
    signal_key: str
    exit_plan_name: str
    exit_plan_params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.name
        return d


@dataclass
class Decision:
    """入场策略的决策结果。"""
    type: DecisionType
    side: Optional[Side] = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.type in (DecisionType.OPEN, DecisionType.CLOSE_AND_HOLD,
                             DecisionType.CLOSE_AND_REVERSE)
