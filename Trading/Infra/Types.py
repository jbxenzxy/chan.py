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
# 枚举：订单意图 / 持仓来源 / 离场方式 / 引擎状态
# ─────────────────────────────────────────────
class OrderIntent(str, Enum):
    """订单意图 —— 唯一决定 CTP 报文 offset 的来源（映射见 Broker/Base.INTENT_TO_OFFSET）。"""
    OPEN = "open"        # 开仓（空仓新开 / 锁仓后补开）→ offset=OPEN
    UNLOCK = "unlock"    # 解锁：平掉反向昨仓          → offset=CLOSE（平昨）
    CLOSE = "close"      # 硬离场：平仓了结            → offset=CLOSE（平昨）
                         #   规则 ⑸ 保证 CLOSE 只用于跨日单，故恒为平昨，无平今分支
    LOCK = "lock"        # 软离场：反向开同手数锁仓     → offset=OPEN（与 OPEN 同报文、异语义）


class PositionOrigin(str, Enum):
    """持仓来源标记（**不参与任何交易决策**，仅作审计标签）。

    唯一有决策语义的值是 SOFT_EXIT_LOCK，判定集中在 Engine 的三类场合：
      · settle 跳过（锁仓腿不等止盈止损，等解锁）
      · 运行态判定（簿内存在非 SOFT_EXIT_LOCK 持仓 = 运行态 → 忽略信号）
      · _exit_intent 防御分支 / force_lock 过滤 / 事件统计
    SIGNAL_OPEN 与 UNLOCK_UPGRADE 只写事件日志与 state.db，代码中不存在对它们的
    判定性比较（新增 `origin is UNLOCK_UPGRADE` 之类的分支会破坏契约，
    由 Trading/Test/test_p25_origin_decoupled.py 的源码扫描拦截）。

    ⚠️ 离场方式**不**由本枚举决定，而是由 `Engine._exit_intent(pos, today)` 按
       `Position.entry_date` 判定：当日单 → LOCK（反向开仓锁仓），
       跨日单 → CLOSE（平昨）。同一笔仓当日平与隔日平的离场方式不同，
       别再假设"来源决定离场方式"。
    """
    SIGNAL_OPEN = "signal_open"        # 空仓状态下由买卖点信号开新仓入场
    UNLOCK_UPGRADE = "unlock_upgrade"  # 锁仓解锁时，同向配对腿升级而来（entry_date 保持原开仓日）
    SOFT_EXIT_LOCK = "soft_exit_lock"  # 软离场锁仓成交后落簿的反向腿
                                       #   唯一合法离场 = 对向信号触发 UNLOCK（平昨，次日语义）


class ExitMode(str, Enum):
    """离场方式，由 `Engine._exit_intent` 按**建仓日期**决定（规则 ⑸ 硬规则，不留配置开关）。"""
    HARD_EXIT = "hard_exit"            # 硬离场（平仓：真正了结，PnL 兑现）
    SOFT_EXIT = "soft_exit"            # 软离场（锁仓：开反向同手数，正反互锁等效离场，PnL 不兑现）


class EngineState(str, Enum):
    """引擎状态机。"""
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
    high: float          # 信号 K 线最高价（右肩 K 线）
    low: float           # 信号 K 线最低价（右肩 K 线）
    fractal_low: float = 0.0   # 底分型最低点 K 线最低价（做多结构止损参考 A）
    fractal_high: float = 0.0  # 顶分型最高点 K 线最高价（做空结构止损参考 A）
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
            fractal_low=float(b.get("fractal_low") or 0.0),
            fractal_high=float(b.get("fractal_high") or 0.0),
            extra={k: v for k, v in b.items()
                   if k not in ("date", "type", "is_buy", "price", "high", "low",
                                "timestamp", "fractal_low", "fractal_high")},
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
            fractal_low=float(final.get("fractal_low") or 0.0),
            fractal_high=float(final.get("fractal_high") or 0.0),
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
    action: str                 # 下单动作 = OrderIntent.value ∈ open|unlock|close|lock
                                #   （DryRun 只发 open/close；SimNow 四值都可能）
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
    """单笔持仓（一笔报单的产物）。簿内可同时存在多笔：同 K 线连开 N 笔、
    以及锁仓留下的双向双腿（原腿 + 反向腿，共享 lock_pair_id）。"""
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
    # 来源标记，**不参与任何交易决策**（语义与唯一例外见 PositionOrigin docstring）。
    origin: PositionOrigin = PositionOrigin.SIGNAL_OPEN
    # 入场交易日（YYYY-MM-DD）。取 bar.date[:10]，不能用 now_cn()。
    #   规则 ⑸ 判定"当日/跨日"的唯一依据 → 决定离场走 LOCK（今日单）还是 CLOSE（跨日单）。
    #   空串（记录缺失）= "" < today 恒真 → 保守当作昨仓 → 走 CLOSE 平昨。
    entry_date: str = ""
    # 锁仓配对 ID：1 个锁仓 = 原腿 + 反向腿，两腿共享同一 ID。
    #   解锁时据此找到"同锁的另一笔"升级为 UNLOCK_UPGRADE。空串 = 未配对。
    lock_pair_id: str = ""

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
            "origin": self.origin.value,
            "entry_date": self.entry_date,
            "lock_pair_id": self.lock_pair_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        # 缺字段 / 坏值 → SIGNAL_OPEN：不抛异常，避免一条脏记录阻断整簿恢复
        raw = d.get("origin", PositionOrigin.SIGNAL_OPEN.value)
        try:
            origin = PositionOrigin(raw) if isinstance(raw, str) else PositionOrigin.SIGNAL_OPEN
        except ValueError:
            origin = PositionOrigin.SIGNAL_OPEN
        return cls(symbol=d["symbol"], side=Side[d["side"]], volume=int(d["volume"]),
                   entry_price=float(d["entry_price"]), entry_at=d.get("entry_at", ""),
                   entry_bar_ts=int(d.get("entry_bar_ts") or 0),
                   entry_bar_seq=int(d.get("entry_bar_seq") or 0),
                   signal_key=d.get("signal_key", ""),
                   open_order_id=d.get("open_order_id", ""),
                   exit_plan=ExitPlan.from_dict(d.get("exit_plan") or {}),
                   origin=origin,
                   entry_date=d.get("entry_date", ""),
                   lock_pair_id=d.get("lock_pair_id", ""))


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
    reason: str                 # 离场原因：tp / sl（L1-L3 触发）、settle_exit（兜底）、
                                #   auto_order_off（关闭托管时锁仓）、manual（调用方自定）
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
