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

# ── 建仓日 / 交易日（SSOT，2026-09-10 立）─────────────────────────────
# 规则 ⑸「今仓 → 反向开仓锁仓 / 昨仓 → 平仓」以及平今•平昨费率口径，
# 全部只认 trading_day_of_ms() 一个函数。权威输入是**毫秒时间戳**
# （bar.timestamp / Signal.timestamp），不是格式化字符串 —— 字符串是展示产物，
# 一旦为空就会被下游解释成一个合法业务语义（"很久以前" = 昨仓），错误无法暴露。
#
# 夜盘起点（北京时间整点）：>= 该时刻的成交归属【次一交易日】。
# 期货夜盘最早 21:00 开盘，取 20:00 留边界余量（20:00-21:00 是休市静默段）。
NIGHT_SESSION_START_HOUR = 20
# 派生日期可信下限：低于它的派生结果说明该"时间戳"不是真实毫秒
# （如测试夹具用 4000 = 序号），必须拒绝采用，避免算成 1970-01-01。
PLAUSIBLE_DATE_MIN = "2020-01-01"


def now_cn() -> str:
    """北京时间 ISO 字符串（秒精度）。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def now_ms() -> int:
    """当前毫秒时间戳（墙钟）。"""
    return int(datetime.now(CN_TZ).timestamp() * 1000)


def _ms_to_dt(ts_ms: int) -> Optional[datetime]:
    """毫秒时间戳 → 北京时间 datetime。无效（None/0/负/越界）→ None。"""
    try:
        ts = int(ts_ms)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000.0, CN_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def date_of_ms(ts_ms: int) -> str:
    """毫秒时间戳 → 自然日 'YYYY-MM-DD'（北京时间）。无效 → ''。"""
    dt = _ms_to_dt(ts_ms)
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


def trading_day_of_ms(ts_ms: int,
                      night_start_hour: int = NIGHT_SESSION_START_HOUR) -> str:
    """毫秒时间戳 → 所属【交易日】'YYYY-MM-DD'（北京时间）。无效 → ''。

    为什么不能直接用自然日
      夜盘的成交属于**次一交易日**。用自然日会漏判：夜盘 21:00 建的仓，
      自然日是 9/9，而 9/10 日盘平它实为【平今】，自然日口径却判成"昨仓"
      → 规则 ⑸ 发 CLOSE 平昨 → 中金所拒单（平今/平昨报错）→ 连锁 phantom 清仓。
      日盘品种（如 IF）两者恒等，所以此坑只在夜盘品种（au/ag/螺纹/原油）暴露。

    规则
      北京时间 hour >= night_start_hour（默认 20）→ 归属次一自然日；否则归属当日。
      周末/节假日不做精细处理：**所有调用方共用本函数**，只要口径一致，
      "是否同一交易日"的判定就正确 —— 例如周五夜盘 → 周六，与下周一的日盘
      不等，判为跨交易日，符合事实（周五夜盘的仓到下周一确实是昨仓）。
      真正需要"精确交易日历"的场景（如对账按日切片）不在本函数职责内。
    """
    dt = _ms_to_dt(ts_ms)
    if dt is None:
        return ""
    if dt.hour >= night_start_hour:
        dt = dt + timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def trading_day_from_clock(clock: str) -> str:
    """墙钟字符串（`entry_at` / `now_cn()` 产物）→ 交易日 'YYYY-MM-DD'。无效 → ''。

    仅作**第三兜底**：既无 entry_date 又无有效 entry_bar_ts 时才用。
    刻意不做夜盘偏移 —— 墙钟在回放/补录场景下未必等于 K 线的交易日，
    把它当权威会引入比"缺失"更隐蔽的错误。这里只负责"能读出个像样的日期"。
    """
    if not clock or len(clock) < 10:
        return ""
    head = clock[:10]
    if head[4] != "-" or head[7] != "-":
        return ""
    if not (head[:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit()):
        return ""
    if head < PLAUSIBLE_DATE_MIN:
        return ""
    return head


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
#   ⚠️ 以下枚举均继承 (str, Enum)：**成员定义顺序无语义**，不得据此做任何判定/分支。
#      序列化只取 .value（字符串）、比较按字符串，成员排列仅服务于可读性分组，
#      可以自由调整 —— P10 的成员断言已与顺序解耦（见 test_p10_state_machine.py [1]）。
# ─────────────────────────────────────────────
class OrderIntent(str, Enum):
    """订单意图 —— 唯一决定 CTP 报文 offset 的来源（映射见 Broker/Base.INTENT_TO_OFFSET）。

    只有两个值，一一对应需求 ⑵ 的两种操作：
      OPEN   开仓：买信号 → 买开，卖信号 → 卖开   → offset=OPEN
      CLOSE  平仓：买信号 → 买平，卖信号 → 卖平   → offset=CLOSE

    「买还是卖」**不在本枚举里**：方向由调用方按信号方向给出 `side`，
    broker 只负责把 (side, offset) 翻译成 CTP 报文。

    为什么没有"锁仓 / 解锁"这两个意图
      历史上这里有过 LOCK / UNLOCK 两个成员，但它们与 OPEN / CLOSE 在
      **CTP 报文层面完全等价**（LOCK 就是反向 OPEN，UNLOCK 就是 CLOSE），
      只是引擎内部的记账标签。围着标签长出来的"出身判定 / 配对 / 升级"
      是复杂度的主要来源，2026-09-11 按需求方口径整体删除。
      现在"这笔仓是开出来的还是锁出来的"**不是一个需要记录的属性** ——
      账户长什么样只取决于净敞口（见 `AccountState`）。

    不变量（规则 ⑹/⑺ 的推论，硬断言在 `Engine._pre_trade_check`）
      CLOSE 的目标恒定是**跨日仓** → 中金所下恒为平昨，**不存在平今分支**。
      这条不变量是全品种安全性的基础：它让本系统永远不需要 CLOSETODAY 指令，
      从而天然绕开六家交易所平今/平昨的指令差异（仅上期所/能源中心有该指令，
      其余四家传平今会直接报错）。**不要为"平今"开任何口子。**
    """
    OPEN = "open"     # 开仓 → offset=OPEN
    CLOSE = "close"   # 平仓 → offset=CLOSE（中金所下恒为平昨）


class AccountState(str, Enum):
    """账户三态（需求 ⑴）—— 由持仓簿**派生**的只读状态，不是独立状态机。

    与 `EngineState` 的关系：`EngineState` 是引擎的 4 值**过程**状态机
    （IDLE / OPENING / IN_TRADE / EXITING，含两个下单瞬态）；本枚举是账户的
    3 值**结果**状态（用户视角的"账户长什么样"），两者正交：
      · OPENING / EXITING 是瞬态，期间账户态不变
      · EngineState 的 IDLE 同时覆盖本枚举的 FLAT 与 LOCKED（引擎在两者都"等信号"）

    三态定义（需求 ⑴ 字面口径，**按净敞口判定，与"这笔仓什么出身"无关**）：
      FLAT     空仓态：簿内无任何仓单，净敞口 = 0，盈亏恒 0。
      LOCKED   锁仓态：有仓单但净敞口 = 0（多空互锁），盈亏锁定。
      RUNNING  运行态：净敞口 ≠ 0，盈亏随行情实时变动。

    判定式（唯一实现点 `TradingEngine.account_state()`，`PositionBook.net_volume()`）：
      net = 0 且簿空 → FLAT；net = 0 且簿非空 → LOCKED；net ≠ 0 → RUNNING。

    ⚠️ 除 `account_state()` 外**不得**再写第二处 `if net == 0` 之外的三态判定
    （架构约束 A1）。历史上这里曾按"簿内是否存在特定出身的仓"判定，
    那是本次重构要消除的耦合。
    """
    FLAT = "flat"        # 空仓态
    LOCKED = "locked"    # 锁仓态
    RUNNING = "running"  # 运行态


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
        """从 chan.py SSE 快照的 bsp 条目构造信号。

        F3（2026-09-10）：`date` / `timestamp` 缺失 → **抛 ValueError**，不再静默补空串。
          理由：`date` 同时是规则 ⑸ 的建仓日判据与幂等键的组成部分。若容忍缺失，
          `make_key` 会退化成 `'|1|B'` —— 所有同类信号**撞同一个键**，除第一条外
          全部被引擎判为重复丢弃。这是比"日期为空"更早发作的静默失效。
          上游 App/AppSSE.py 生成 bsp 时是字面量赋值 `"date": klu.time.toFmtStr(...)`，
          两个键必然存在；缺失即契约被破坏，必须暴露。
          调用方（Source/SSE.py）逐条捕获后丢弃该 bsp 并告警，不会炸掉整条连接。
        """
        date = str(b.get("date") or "")
        ts = int(b.get("timestamp") or 0)
        if not date or ts <= 0:
            raise ValueError(
                "bsp 缺 date/timestamp，拒绝构造 Signal（否则幂等键退化为 '|{}|{}' "
                "并让同类信号互相去重）: date={!r} timestamp={!r} type={!r} is_buy={!r}"
                .format(str(b.get("type", "")), "B" if b.get("is_buy") else "S",
                        b.get("date"), b.get("timestamp"), b.get("type"),
                        b.get("is_buy")))
        btype = str(b.get("type", ""))
        is_buy = bool(b.get("is_buy"))
        return cls(
            key=cls.make_key(date, btype, is_buy),
            symbol=symbol, freq=freq, date=date,
            timestamp=ts,
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
    action: str                 # 下单动作 = OrderIntent.value ∈ open|close
                                #   方向由 side 给出，两者合起来唯一确定 CTP 报文
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
    """单笔持仓（一笔报单的产物，一笔挂 N 手，N ≥ 1）。

    簿内可同时存在多笔：同一交易日连开数笔，以及锁仓留下的双向持仓。
    **仓单之间没有配对关系** —— 簿只是一个按时间先后（FIFO）排列的序列。
    平仓时与"序列中反向最早的一笔"对冲（由 `PositionBook.oldest_opposite` 选出），
    不需要、也不应该记录"这两笔是一对"。

    记录单位是**笔**（每笔 N 手），不是手：交易所/CTP 只按手聚合
    （合约+方向+昨今，无"笔"概念，平仓只减手数），但"平反向最早那一撮"
    的 FIFO 顺序柜台不替我们记，所以本地必须多留这一层。
    前提：N 恒定（当前 2），否则一次 CLOSE 会跨笔 —— 由 `Engine._pre_trade_check` 断言。
    """
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
    # 建仓所属【交易日】（YYYY-MM-DD），由 trading_day_of_ms() 派生（含夜盘归属次日）。
    #   规则 ⑸ 判定"当日/跨日"的唯一依据 → 决定离场走 LOCK（今仓锁仓）还是 CLOSE（昨仓平仓）。
    #   F4（2026-09-10）：本字段**不得为空**。它是派生字段，缺失时由 __post_init__
    #   依次从 entry_bar_ts → entry_at 精确重建；三者全空则保持空串，由调用方
    #   fail-fast（建仓期拒绝建仓 / 恢复期拒绝启动），**绝不由下游把空串解释成"昨仓"**。
    entry_date: str = ""

    def __post_init__(self) -> None:
        """不变量：entry_date 必须能解析出，不得靠"默认空串 + 下游解释"存在。

        F4（2026-09-10）。此前的实现是 `entry_date: str = ""` + 判定式 `"" < today`
        恒真 → 空串被**静默解释成"昨仓"** → 对今仓发 CLOSE 平今 → CTP 拒单 →
        连锁到 phantom 清仓（簿面清空但实盘仍有仓，不可逆）。

        构造期按**权威性递减**依次重建，能算就算，绝不猜：
          ① entry_date 已显式给出 → 原样保留（调用方口径优先）
          ② entry_bar_ts 是真实毫秒时间戳，且派生交易日 >= PLAUSIBLE_DATE_MIN
             → 采用（权威来源：建仓 K 线的时间戳；旧 state.db 一定有它，
               因为 entry_bar_ts 比 entry_date 更早引入）
          ③ entry_at 墙钟字符串能读出日期 → 采用（兜底）
        三者都不成立 → 保持空串，交由调用方 fail-fast：
          · 建仓期 Engine._pre_trade_check → 拒绝建仓（no_time_anchor，不报单）
          · 恢复期 Engine._restore         → 拒绝启动
        """
        if self.entry_date:
            return
        derived = trading_day_of_ms(self.entry_bar_ts)
        if derived and derived >= PLAUSIBLE_DATE_MIN:
            self.entry_date = derived
            return
        self.entry_date = trading_day_from_clock(self.entry_at)

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
            "entry_date": self.entry_date,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Position":
        return cls(symbol=d["symbol"], side=Side[d["side"]], volume=int(d["volume"]),
                   entry_price=float(d["entry_price"]), entry_at=d.get("entry_at", ""),
                   entry_bar_ts=int(d.get("entry_bar_ts") or 0),
                   entry_bar_seq=int(d.get("entry_bar_seq") or 0),
                   signal_key=d.get("signal_key", ""),
                   open_order_id=d.get("open_order_id", ""),
                   exit_plan=ExitPlan.from_dict(d.get("exit_plan") or {}),
                   entry_date=d.get("entry_date", ""))


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
