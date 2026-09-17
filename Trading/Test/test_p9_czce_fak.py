# -*- coding: utf-8 -*-
"""
P9 郑商所 CZCE · FOK→FAK + OPEN 手数（执行策略表第 5 列） 契约测试
=================================================
背景（设计，见自动下单重构-分析与实施计划 v1.3 ；用户拍板方案）：
  郑商所（CZCE）不支持 FOK（tqsdk 限价+FOK 仅拒郑商所期货）。用户拍板：
  对 CZCE 期货，**一次信号只报 1 笔、该笔固定挂 1 手、指令属性 FOK→FAK**。
  因为单笔 1 手，FAK 在 1 手下要么成 1、要么成 0，与 FOK 完全同构（FAK(1)≡FOK(1)），
  所以：
    · 报单填充三态（待报/全成/全撤）不变量 4 保持 —— 无需扩展状态机、无需补簿；
    · 账户三态只看净敞口，不受影响；
    · 平仓手数 = 目标仓单全额（不受单笔手数旋钮影响）。
  其余品种完全不变：1 笔 N 手（N = 品种执行策略表第 3 列，IF = 2）FOK。

本测试锁死用户的三条断言：
  [1] Instrument.effective_order_advanced()：CZCE→"FAK"，其余→"FOK"（含配置覆盖；
      报单属性真值源 = 品种档案；删 exchange 字段后交易所彻底出代码）
  [2] Engine._decide_action OPEN 手数：按表第 3 列（TA=1 / IF=2，无拆单）
  [3] Broker submit 报文：CZCE 两个 insert_order 站点（OPEN/CLOSE/离场）advanced 均 "FAK"；
      CFFEX 回归 "FOK"
  [4] 端到端（Engine + DryRunBroker）：CZCE 信号 → 恰好 1 笔 1 手、net 正确、
      终态只可能是 filled/rejected（无部分成交态、无补簿拆单）
  [5] 端到端 advanced 透传（Engine + SimNow broker + MockApi）：CZCE 信号 → 1 笔 1 手
      advanced="FAK"、net=1

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p9_czce_fak.py
"""
from __future__ import annotations

import itertools
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.Base import OrderIntent  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, RiskConfig, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import (Instrument,  # noqa: E402
                                      InstrumentConfig)
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402

# `Product.exchange` 字段整体删除后，原先的
#   `_prod(ex) = _dc_replace(_IF, exchange=ex)`（"换交易所造一份现场档案"）
#   在结构上无法表达 —— 连能填错的地方都没有了，故连同 `_dc_replace`
#   导入一并删除（本文件其余地方不再需要它）。
_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.Records import AccountState, Bar, EngineState, Signal, Side
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("" if ok else "  -> got={!r}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p9_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────
# mock 对象（复用 test_p23 的 MockApi 记录完整 insert_order 报文）
# ───────────────────────────────────────────────────────────────────
class MockQuote:
    def __init__(self, ask=None, bid=None):
        self.ask_price1 = ask
        self.bid_price1 = bid


class MockPos:
    pos_long_today = 0
    pos_long_his = 2
    pos_short_today = 0
    pos_short_his = 0


class MockRawOrder:
    def __init__(self, oid):
        self.order_id = oid
        self.status = "ALIVE"
        self.volume_left = 1
        self.trade_price = None
        self.trade_records = {}
        self.last_msg = "Submitted"


class FilledRawOrder:
    def __init__(self, oid, volume, price):
        self.order_id = oid
        self.status = "FINISHED"
        self.volume_left = 0
        self.trade_price = price
        self.trade_records = {"t-{}".format(oid): {"volume": volume, "price": price}}
        self.last_msg = "全部成交报单"


class MockApi:
    """最小 api：记录 insert_order 的完整报文（含 advanced），默认返回 ALIVE 单。"""

    def __init__(self, ask=4565.0, bid=4560.0, pos=None, filled=False,
                 fill_volume=1, fill_price=4560.0):
        self._quote = MockQuote(ask=ask, bid=bid)
        self._pos = pos
        self._filled = filled
        self._fill_volume = fill_volume
        self._fill_price = fill_price
        self.inserted = []
        self.cancelled = []

    def get_quote(self, symbol):
        return self._quote

    def wait_update(self, deadline=None):
        return True

    def get_position(self, sym=None):
        return self._pos

    def get_order(self, oid):
        return None

    def insert_order(self, symbol, direction, offset, volume, limit_price, advanced=None):
        self.inserted.append({"symbol": symbol, "direction": direction,
                              "offset": offset, "volume": volume,
                              "limit_price": limit_price, "advanced": advanced})
        if self._filled:
            return FilledRawOrder("raw-{}".format(len(self.inserted)),
                                  volume, self._fill_price)
        return MockRawOrder("raw-{}".format(len(self.inserted)))

    def cancel_order(self, oid):
        self.cancelled.append(oid)


def make_broker(api=None, params=None):
    """用 object.__new__ 绕过 __init__（避免真实 _connect 连 SimNow）。"""
    b = object.__new__(SimNowBroker)
    b.state = Instrument(None, _IF)
    b.params = dict(DEFAULT_CONFIG["broker_params"], **(params or {}))
    b._api = api
    b._trade_symbol = b.state.trade_symbol
    b._seq = itertools.count(1)
    b.orders = []
    b._sig_orders = {}
    b._conn_error = None
    return b


def make_czce_broker(api=None, params=None):
    """make_broker 的 CZCE 版：报单属性归品种档案 —— 直接用 TA 档案
    构造 Instrument，trade_symbol 用郑商所月份合约（运行时可写身份字段）。"""
    b = make_broker(api=api, params=params)
    b.state = Instrument(
        InstrumentConfig(signal_symbol="KQ.m@CZCE.TA", trade_symbol="CZCE.TA501"),
        PRODUCT_PROFILES["TA"])
    b._trade_symbol = "CZCE.TA501"
    return b


def build_engine(tmpdir, *, code="TA", broker=None):
    """构造引擎（TA / IF 两份档案二选一）。

    Engine 与 broker 共用同一份 Instrument（合并），
    报单属性由**品种档案**给定，端到端验证 FOK/FAK 切换。
    单笔手数不再由配置给（`risk.max_volume` 已删），
    真值源 = 品种执行策略表第 3 列（TA → 1 / IF → 2）。
    形参由 `exchange="CZCE"/"CFFEX"` 改为 `code="TA"/"IF"`。
    本参数**从来就只是"选哪份档案"**（CZCE/CFFEX 是当时能想到的场景名），
    改用品种键才与代码真正读的东西（`PRODUCT_PROFILES[code]`）对得上；
    何况 `Product.exchange` 字段已删除，交易所名再无字段可承载。
    """
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    # frozen 配置不可就地改写 ——
    #   按品种键取档案；Instrument 单例整体替换进 cfg，
    #   并同时注入 broker（Engine 读 self.state = broker.state = 同一对象）。
    is_ta = code == "TA"
    sym = "KQ.m@CZCE.TA" if is_ta else "KQ.m@CFFEX.IF"
    tsym = "CZCE.TA501" if is_ta else "CFFEX.IF2609"
    cfg.instrument = InstrumentConfig(signal_symbol=sym, trade_symbol=tsym)
    spec = Instrument(None, PRODUCT_PROFILES[code])
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    else:
        broker.state = spec          # 注入目标档案 spec 到传入 broker（如 SimNow+MockApi）
        broker._state = spec
        broker._trade_symbol = tsym
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def make_sig(key="P9-TEST|0|0", is_buy=True, price=4550.0, low=4540.0, high=4560.0,
             symbol="KQ.m@CZCE.TA"):
    return Signal(
        key=key, symbol=symbol, freq="5m",
        date="2026-09-01 09:30", timestamp=4000,
        bsp_type="B" if is_buy else "S", is_buy=is_buy,
        price=price, high=high, low=low, extra={})


def make_bar(date="2026-09-01 09:30", close=4550.0):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=4000, vol=0)


_FAST = {"fill_timeout_open": 0.05, "fill_timeout_close": 0.05,
         "close_max_chase": 2, "chase_interval": 0.01}


# ════════════════════════════════════════════════════════════════
# [1] Instrument.effective_order_advanced() 契约
# ════════════════════════════════════════════════════════════════
print("\n[1] Instrument.effective_order_advanced()：唯一口径 = 品种执行策略表第 2 列"
      "（2026-09-16 起不再按交易所名字分支）")
check("[1a] 无档案 Instrument → FOK（回落部署配置 order_advanced）",
      Instrument(None).effective_order_advanced(), "FOK")
check("[1b] IF 档案 → FOK",
      Instrument(None, PRODUCT_PROFILES["IF"]).effective_order_advanced(), "FOK")
check("[1c] ★ TA 档案 → FAK（表第 2 列；与交易所名字无关）",
      Instrument(None, PRODUCT_PROFILES["TA"]).effective_order_advanced(), "FAK")
check("[1d] ★ TA 档案 + 配置 order_advanced='FOK' → 仍 FAK（表覆盖配置）",
      Instrument(InstrumentConfig(order_advanced="FOK"),
                 PRODUCT_PROFILES["TA"]).effective_order_advanced(), "FAK")
check("[1e] ★ IF 档案 + 配置 order_advanced='FAK' → 仍 FOK（表覆盖配置）",
      Instrument(InstrumentConfig(order_advanced="FAK"),
                 PRODUCT_PROFILES["IF"]).effective_order_advanced(), "FOK")
for _k in ("AU", "AG", "CU"):
    check("[1f] {} 档案 → FOK（表第 2 列）".format(_k),
          Instrument(None, PRODUCT_PROFILES[_k]).effective_order_advanced(), "FOK")
# [1g] 原为「配错交易所不改表结论：TA 档案改 exchange='SHFE' → 仍 FAK」。
#      删除 `Product.exchange` 字段后，这条从**行为断言**升级为
#      **结构断言**：交易所连"能被填错的地方"都不存在了 —— 比"填错也不影响"更强，
#      因为它不再依赖"有人记得去测配错这个动作"。
check("[1g] ★ 交易所彻底出代码：8 档均无 exchange 字段（2026-09-16 B 批删除）",
      [k for k in PRODUCT_PROFILES if hasattr(PRODUCT_PROFILES[k], "exchange")],
      [])
check("[1h] 表第 3 列：TA 一笔 1 手 / IF 一笔 2 手",
      (PRODUCT_PROFILES["TA"].exec_policy.lots_per_order,
       PRODUCT_PROFILES["IF"].exec_policy.lots_per_order), (1, 2))


# ════════════════════════════════════════════════════════════════
# [2] Engine._decide_action OPEN 手数契约（FLAT→OPEN 转移① / 同日锁→OPEN 转移②）
# ════════════════════════════════════════════════════════════════
print("\n[2] Engine._decide_action OPEN 手数：读执行策略表第 5 列（TA=1，无拆单）")
with tmp_dir() as td:
    eng = build_engine(td, code="TA")
    check("[2a] CZCE 引擎初始 account_state=FLAT", eng.account_state(), AccountState.FLAT)
    sig = make_sig(key="P9-2a")
    act = eng._decide_action(sig, sig.date)
    check("[2b] ★ CZCE 转移① OPEN 手数 = 1（钉死，无拆单）", act.volume, 1)
    check("[2c] CZCE 转移① intent=OPEN", act.intent, OrderIntent.OPEN)
    check("[2d] CZCE 转移① transition=1", act.transition, 1)

with tmp_dir() as td:
    eng = build_engine(td, code="IF")
    check("[2e] CFFEX 引擎初始 account_state=FLAT", eng.account_state(), AccountState.FLAT)
    act = eng._decide_action(make_sig(key="P9-2e", symbol="KQ.m@CFFEX.IF"), "2026-09-01 09:30")
    check("[2f] CFFEX 转移① OPEN 手数 = 表第 3 列(=2)", act.volume, 2)
    check("[2g] CFFEX 转移① intent=OPEN", act.intent, OrderIntent.OPEN)

# 转移②（当日锁→OPEN）与 ① 共用 self._open_volume() —— 直接钉死 helper 即覆盖两者
with tmp_dir() as td:
    eng = build_engine(td, code="TA")
    check("[2h] ★ CZCE 引擎 _open_volume()=1（转移①/② 共用，钉死 1 手）",
          eng._open_volume(), 1)
    eng2 = build_engine(td, code="IF")
    check("[2i] CFFEX 引擎 _open_volume()=表第 3 列(=2)（其余所不变）",
          eng2._open_volume(), 2)


# ════════════════════════════════════════════════════════════════
# [3] Broker submit 报文契约（TA 的表第 4 列 = FAK，覆盖 OPEN/CLOSE/离场 三个 insert_order 站点）
# ════════════════════════════════════════════════════════════════
print("\n[3] Broker submit 报文：CZCE advanced='FAK'（OPEN/CLOSE/离场），CFFEX 回归 'FOK'")
# OPEN 站点（SimNow._submit_open:912）
b = make_czce_broker(api=MockApi(), params=_FAST)
b.submit(OrderIntent.OPEN, Side.LONG, 1, 4550.0, "k-czce-open")
check("[3a] ★ CZCE OPEN 报单 advanced='FAK'", b._api.inserted[0]["advanced"], "FAK")
check("[3b] CZCE OPEN 报单 volume=1（1 笔 1 手）", b._api.inserted[0]["volume"], 1)
# CLOSE 站点（SimNow._submit_close:1016）
b = make_czce_broker(api=MockApi(pos=MockPos()), params=_FAST)
b.submit(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-czce-close")
check("[3c] ★ CZCE CLOSE 报单 advanced='FAK'（覆盖 1016 站点）",
      b._api.inserted[0]["advanced"], "FAK")
# 离场（is_exit=True，转移④/⑤ 也走同一 advanced 通道）
b = make_czce_broker(api=MockApi(), params=_FAST)
b.submit(OrderIntent.OPEN, Side.SHORT, 1, 4550.0, "k-czce-exit", is_exit=True)
check("[3d] ★ CZCE 离场（is_exit=True）报单 advanced='FAK'",
      b._api.inserted[0]["advanced"], "FAK")
# 回归：CFFEX 仍 FOK（两条站点都不应被 CZCE 逻辑误伤）
b = make_broker(api=MockApi(), params=_FAST)
b.submit(OrderIntent.OPEN, Side.LONG, 2, 4550.0, "k-cffex-open")
check("[3e] 回归：CFFEX OPEN advanced='FOK'", b._api.inserted[0]["advanced"], "FOK")
b = make_broker(api=MockApi(pos=MockPos()), params=_FAST)
b.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-cffex-close")
check("[3f] 回归：CFFEX CLOSE advanced='FOK'", b._api.inserted[0]["advanced"], "FOK")


# ════════════════════════════════════════════════════════════════
# [4] 端到端（Engine + DryRunBroker）：CZCE 信号 → 1 笔 1 手、net 正确、无部分成交态
# ════════════════════════════════════════════════════════════════
print("\n[4] 端到端 Engine + DryRunBroker：CZCE 1 笔 1 手、net 正确、终态 only filled/rejected")

# 全成路径：CZCE 信号 → 恰好 1 单 1 手、net=1、RUNNING、status filled
with tmp_dir() as td:
    eng = build_engine(td, code="TA")
    eng.on_bar(make_bar())
    sig = make_sig(key="P9-4-ok")
    eng.on_signal(sig)
    check("[4a] ★ CZCE 全成：broker 恰好 1 单（不是拆成多笔）",
          len(eng.broker.orders), 1)
    check("[4b] ★ CZCE 全成：该单 1 手", eng.broker.orders[0].volume, 1)
    check("[4c] CZCE 全成：净敞口 = 1", eng.positions.net_volume(), 1)
    check("[4d] CZCE 全成：account_state=RUNNING", eng.account_state(), AccountState.RUNNING)
    check("[4e] CZCE 全成：signal_action=opened",
          eng.store.signal_action(sig.key), "opened")
    check("[4f] ★ CZCE 全成：该单终态 filled（报单填充三态，无部分成交态）",
          eng.broker.orders[0].status, "filled")
    check_true("[4g] ★ CZCE 全成：所有报单终态 ∈ {filled,rejected}（无 partial）",
               all(o.status in ("filled", "rejected") for o in eng.broker.orders))

# 全撤（拒单）路径：CZCE 信号 → 恰好 1 单 1 手、net=0、FLAT、status rejected（无幻影）
with tmp_dir() as td:
    broker = DryRunBroker(Instrument(None, _IF), {"sim_equity": 10_000_000.0})

    class RejectDryBroker(DryRunBroker):
        def submit(self, intent, side, volume, ref_price, signal_key="", note="",
                   entry_date="", is_exit=False):
            from Trading.Infra.Records import Order
            o = Order(
                order_id="reject-000001", signal_key=signal_key,
                symbol=self.state.trade_symbol, side=side,
                action="open" if intent is OrderIntent.OPEN else "close",
                volume=int(volume), price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o

    # exchange 归档案 —— CZCE 语义直接用 TA 档案表达（frozen 配置不可改写）
    rb = RejectDryBroker(
        Instrument(InstrumentConfig(trade_symbol="CZCE.TA501"),
                   PRODUCT_PROFILES["TA"]),
        {"sim_equity": 10_000_000.0})
    eng = build_engine(td, code="TA", broker=rb)
    eng.on_bar(make_bar())
    sig = make_sig(key="P9-4-rej")
    eng.on_signal(sig)
    check("[4h] ★ CZCE 全撤：broker 恰好 1 单（不补簿、不拆单）",
          len(eng.broker.orders), 1)
    check("[4i] ★ CZCE 全撤：该单 1 手", eng.broker.orders[0].volume, 1)
    check("[4j] CZCE 全撤：净敞口 0（无幻影持仓）", eng.positions.net_volume(), 0)
    check("[4k] CZCE 全撤：account_state=FLAT", eng.account_state(), AccountState.FLAT)
    check("[4l] CZCE 全撤：signal_action=rejected",
          eng.store.signal_action(sig.key), "rejected")
    check("[4m] ★ CZCE 全撤：该单终态 rejected（报单填充三态，无部分成交态）",
          eng.broker.orders[0].status, "rejected")
    check_true("[4n] ★ CZCE 全撤：所有报单终态 ∈ {filled,rejected}（无 partial）",
               all(o.status in ("filled", "rejected") for o in eng.broker.orders))

# 回归：CFFEX 全成仍是 N=2 手（确认 TA 的表第 5 列 = 1 不影响其它品种）
with tmp_dir() as td:
    eng = build_engine(td, code="IF")
    eng.on_bar(make_bar())
    sig = make_sig(key="P9-4-cffex", symbol="KQ.m@CFFEX.IF")
    eng.on_signal(sig)
    check("[4o] 回归：CFFEX 全成 该单 2 手（未误钉成 1）",
          eng.broker.orders[0].volume, 2)
    check("[4p] 回归：CFFEX 全成 净敞口 = 2", eng.positions.net_volume(), 2)


# ════════════════════════════════════════════════════════════════
# [5] 端到端 advanced 透传（Engine + SimNow broker + MockApi）：CZCE 信号 → FAK 透出
# ════════════════════════════════════════════════════════════════
print("\n[5] 端到端 Engine + SimNow broker + MockApi：CZCE 信号 → 1 笔 1 手 advanced='FAK'、net=1")
with tmp_dir() as td:
    api = MockApi(filled=True, fill_volume=1, fill_price=4561.2)
    bk = make_czce_broker(api=api, params=_FAST)
    # 单元测试无真实行情：放开 A′ fail-closed 闸门（模拟 dry_run 的 is_offline 放行），
    # 否则 _pre_trade_check 会直接 instrument_unverified 拒单、insert_order 不会被调用。
    bk.is_offline = True
    # A′ 的 verified 是**运行时状态**，落在 broker 的 state 上
    # （Engine 通过 broker.state 读同一份 —— 不是 spec）。
    bk.state.verified = True
    eng = build_engine(td, code="TA", broker=bk)
    eng.on_bar(make_bar())
    sig = make_sig(key="P9-5-e2e")
    eng.on_signal(sig)
    check("[5a] ★ CZCE 端到端：insert_order 恰好 1 次（无拆单/补簿）",
          len(api.inserted), 1)
    msg = api.inserted[0]
    check("[5b] ★ CZCE 端到端：报文 advanced='FAK'", msg["advanced"], "FAK")
    check("[5c] ★ CZCE 端到端：报文 volume=1", msg["volume"], 1)
    check("[5d] CZCE 端到端：净敞口 = 1", eng.positions.net_volume(), 1)
    check("[5e] CZCE 端到端：account_state=RUNNING", eng.account_state(), AccountState.RUNNING)
    check("[5f] CZCE 端到端：成交价取 CTP 明细价 4561.2",
          eng.broker.orders[0].filled_price if eng.broker.orders else None, 4561.2)


print("\n" + "=" * 60)
print("P9 郑商所 FOK→FAK + OPEN 手数（表第 5 列） 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
