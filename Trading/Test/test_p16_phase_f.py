# -*- coding: utf-8 -*-
"""
对账 + 卡单复核拆除后的形态（原 Phase F）
=============================================
2026-09-17 拍板：例外 → 弹窗 → 用户干预，引擎不自动兜底。
原 F1 卡单复核（_close_in_flight / _check_close_stuck / _validate_close_in_flight）
整体删除 —— FOK/FAK 笔笔有终态，"无终态挂死"场景按设计不存在；
终态万一丢失由对账同步 + broker 终态看门狗兜住。

本测试现在锁死：
  ① broker.trade_confirmed(intent, signal_key) 接口保留（诊断用途）：
      base 默认 True / dry_run 重写 True / 自定义 broker 可重写返回 False
  ② 卡单复核已删除 —— 引擎无 _check_close_stuck / _close_in_flight /
      _validate_close_in_flight，无 close_confirmed / close_stuck_* 事件；
      store 里遗留的 _close_in_flight kv 被忽略（不致命）。
  ③ F2（_restore 末尾首拉真实持仓，source="restore"）保留不变：
      · 无持仓 → 不报错；broker 无 real_position → skip
      · real_vol == engine_vol → skip
      · real_vol < engine_vol → FIFO 部分平（reason=reconcile_external_partial）
      · real_vol == 0 → 全平；real_vol > engine_vol → 告警不接管
      · broker.real_position 抛异常 → 写 restore_reconcile_failed，不阻断启动
      · restore 路径不拦 bars_held < 1"""
from __future__ import annotations

import json
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
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p16_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading.Broker.Base import Broker, OrderIntent, register_broker  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Engine.PositionBook import PositionBook  # noqa: E402
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Strategy.Entry import EntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402
from dataclasses import replace as _dc_replace  # noqa: E402

# 单笔手数的唯一来源 = 品种执行策略表第 3 列（原 `risk.max_volume`
# 已删除）。本文件的场景按「一笔 1 手」构造（多笔叠加才是观察对象），故这里直接
# 改**表值** —— 只把 IF 档案的执行策略第 3 列换成 1，"改表即生效"正是它的口径。
# ⚠️ 引擎的有效档案来自 `broker.state`（合并），本文件所有
#    `Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF)` 都被这一处覆盖，不会出现"cfg 说 1 手、broker 说 2 手"。
_IF = _dc_replace(PRODUCT_PROFILES["IF"],
                  exec_policy=_dc_replace(PRODUCT_PROFILES["IF"].exec_policy,
                                          lots_per_order=1))
from Trading.Infra.Records import AccountState, Bar, EngineState, ExitPlan, Position, Side


_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_truthy(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_in(name, got, container):
    """检查 got 是否在 container 中（dict/list/set/tuple 任意）。"""
    global _PASS, _FAIL
    ok = got in container
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r} not in container".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


# ════════════════════════════════════════════════════════════════
# Mock Broker：可注入 trade_confirmed 返回值 + real_position 模拟
# ════════════════════════════════════════════════════════════════
class ControlledTradeConfirmedBroker(DryRunBroker):
    """DryRunBroker 子类：可注入 trade_confirmed 返回值 + 可选 raise。

    参数：
      · trade_confirmed_value: True / False，控制 trade_confirmed 返回
      · raise_on_trade_confirmed: 若 True，trade_confirmed 抛 RuntimeError
      · real_longs / real_shorts: 模拟外部真实持仓（用于 F2 集成）
    """
    name = "controlled_tc"

    def __init__(self, spec, params=None, *,
                 trade_confirmed_value=True,
                 raise_on_trade_confirmed=False,
                 real_longs=None, real_shorts=None):
        super().__init__(spec, params)
        self._tc_value = trade_confirmed_value
        self._tc_raise = raise_on_trade_confirmed
        self._real_longs = real_longs
        self._real_shorts = real_shorts
        self.tc_calls: list = []   # 记录 (intent, signal_key) 调用对

    def trade_confirmed(self, intent, signal_key=""):
        self.tc_calls.append((intent.value, signal_key))
        if self._tc_raise:
            raise RuntimeError("simulated trade_confirmed failure")
        return self._tc_value

    def real_position(self, side):
        if side is Side.LONG:
            return self._real_longs
        if side is Side.SHORT:
            return self._real_shorts
        return None


class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类：所有 submit 都拒单（用于测试拆锁 CLOSE 拒单不设 in-flight）。"""
    name = "reject_dry"

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        from Trading.Infra.Records import Order
        from Trading.Infra.Clock import now_cn
        o = Order(
            order_id="{}-REJ".format(self.name), signal_key=signal_key,
            symbol=self.state.trade_symbol, side=side,
            action="close", volume=int(volume), price=float(ref_price),
            req_price=float(ref_price), filled_price=None, status="rejected",
            created_at=now_cn(), broker=self.name, note=note,
            meta={"reject_reason": "simulated reject",
                  "reject_class": "not_tradable", "intent": intent.value},
        )
        self.orders.append(o)
        return o


def seed_run(store, side="LONG", volume=1, anchor=4545.0, signal_key="seed-run"):
    """预置运行态风控锚。

    D15 / G2：净敞口 ≠ 0 却没有 run → `_restore` **拒绝启动**
    （缺风控锚等于让敞口在没有止损的状态下运行）。旧版没有 run 概念，
    故凡手工把持仓写进 state.db 的地方都要配套写 run。
    """
    store.set_json("run", {
        "side": side, "anchor": anchor, "volume": volume,
        "bar_ts": 4100, "bar_seq": 1, "signal_key": signal_key,
        "entry_offset": "OPEN", "entry_at": "2026-09-01 09:00",
        "plan": {"name": "seed_plan", "stop_price": anchor - 10.0,
                 "params": {}},
    })


def make_engine(tmpdir, *, split_positions=1,
                close_before_session_end=False, broker=None):
    """构造引擎：每笔手数 = 1（= 品种执行策略表第 3 列，见 `_IF` 改写说明）。"""
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    # 同向笔数上限已在删除（D2）：簿容器不限容量，同向可叠加

    spec = Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF)
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return eng


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000):
    """构造 Bar。默认 ts=5000（大于所有 make_position 的 entry_bar_ts=4100+），避免 _settle_positions 跳过。"""
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST",
                  sl_offset=10.0, entry_date=""):
    """构造手动 Position（不走 _open_positions），用于直接构造 portfolio 状态。"""
    from Trading.Infra.Clock import now_cn
    if side is Side.LONG:
        stop = entry_price - sl_offset
    else:
        stop = entry_price + sl_offset
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at=now_cn(),
        entry_bar_ts=4000 + entry_bar_seq * 100,
        entry_bar_seq=entry_bar_seq,
        signal_key=signal_key, open_order_id="manual",
        exit_plan=ExitPlan(name="tp_sl", stop_price=stop,
                            params={"stop_loss_points": sl_offset}),
        entry_date=entry_date)


def make_signal(key, side, price=4550.0, date="2026-09-01 09:35", bsp_type="buy"):
    """构造手动 Signal（不走 chan.py 上游）。"""
    from Trading.Infra.Records import Signal
    return Signal(key=key, symbol="CFFEX.IF2609", freq="5m",
                  timestamp=5000, date=date, bsp_type=bsp_type,
                  is_buy=(side is Side.LONG), price=price,
                  high=price + 5.0, low=price - 5.0)


def read_events(eng, kinds=None, tail_n=200):
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-tail_n:]:
            try:
                rec = json.loads(line)
                if kinds is None or rec.get("kind") in kinds:
                    out.append(rec)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def find_event(events, kind, **must_match):
    """在 events 列表里找第一条 kind 匹配且所有 must_match 字段相等的记录。"""
    for ev in events:
        if ev.get("kind") != kind:
            continue
        ok = True
        for k, v in must_match.items():
            if ev.get(k) != v:
                ok = False
                break
        if ok:
            return ev
    return None


# ════════════════════════════════════════════════════════════════
# [1] broker.trade_confirmed 接口
# ════════════════════════════════════════════════════════════════
print("\n[1] broker.trade_confirmed 接口")

# 1.1 base.Broker 默认返回 True
class BareBroker(Broker):
    name = "bare"
    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        raise NotImplementedError

base_default = Broker.trade_confirmed(None, "")
check("1.1 base.Broker 默认 trade_confirmed=True", base_default, True)

# 1.2 dry_run.DryRunBroker 重写返回 True
dry_b = DryRunBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF))
check("1.2 dry_run.DryRunBroker trade_confirmed=True",
      dry_b.trade_confirmed(OrderIntent.CLOSE, "x"), True)

# 1.3 自定义 broker 可重写返回 False
ctl = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), trade_confirmed_value=False)
check("1.3 自定义 broker trade_confirmed=False",
      ctl.trade_confirmed(OrderIntent.CLOSE, "x"), False)

# 1.4 自定义 broker 可抛异常
ctl_raise = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF),
                                           raise_on_trade_confirmed=True)
raised = False
try:
    ctl_raise.trade_confirmed(OrderIntent.CLOSE, "x")
except RuntimeError:
    raised = True
check_truthy("1.4 自定义 broker trade_confirmed 抛异常被捕获", raised)

# 1.5 intent / signal_key 参数被接收并记录
ctl.tc_calls = []
ctl.trade_confirmed(OrderIntent.CLOSE, "sig-key-1")
ctl.trade_confirmed(OrderIntent.OPEN, "sig-key-2")
check("1.5a 记录 intent=CLOSE 调用",
      (ctl.tc_calls[0][0], ctl.tc_calls[0][1]), ("close", "sig-key-1"))
check("1.5b 记录 intent=OPEN 调用",
      (ctl.tc_calls[1][0], ctl.tc_calls[1][1]), ("open", "sig-key-2"))


# ════════════════════════════════════════════════════════════════
# [2] F1 _check_close_stuck 直接调用
# ════════════════════════════════════════════════════════════════
# [2] F1 卡单复核已整体删除（2026-09-17 拍板）—— 漂移护栏
# ════════════════════════════════════════════════════════════════
print("\n[2] 卡单复核 _check_close_stuck 已整体删除")
with tmp_dir() as td:
    eng = make_engine(td)
    check("2.1 引擎无 _check_close_stuck", hasattr(eng, "_check_close_stuck"), False)
    check("2.2 引擎无 _close_in_flight", hasattr(eng, "_close_in_flight"), False)
    check("2.3 引擎无 _validate_close_in_flight",
          hasattr(eng, "_validate_close_in_flight"), False)
    check("2.4 broker 接口 trade_confirmed 仍保留（诊断用途）",
          callable(getattr(eng.broker, "trade_confirmed", None)), True)
# ════════════════════════════════════════════════════════════════
print("\n[3] F2 _restore 末尾首拉真实持仓")

# 3.1 无持仓 → 不报错但不触发 reconcile
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=0, real_shorts=0)
    # 注意：直接 make_engine 走 _restore 会调用 reconcile
    # 无持仓时不应触发任何 reconcile 事件
    eng = make_engine(td, broker=ctl_b)
    evs = read_events(eng, kinds={"position_externally_closed_summary",
                                  "position_mismatch", "restore_reconcile_failed"})
    check("3.1 无持仓 → 无 reconcile 事件", len(evs), 0)

# 3.2 有持仓 + broker 无 real_position → skip（默认 DryRunBroker real_position=None）
#     已经在 P14a 测试过；此处快速回归一下
with tmp_dir() as td:
    spec = Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF)
    dry_b = DryRunBroker(spec)  # 默认 real_position=None
    # 手动构造 engine 前先把 store 写入持仓
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(td, "state.db"))
    # 写入持仓
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-test").to_dict()
    store.set_json("positions", [pos_dict])
    store.set_json("bars_seen", 5)
    seed_run(store)
    ev = EventLog(os.path.join(td, "events.jsonl"), echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, dry_b, entry, exitp, store, ev)
    # broker 无 real_position → skip reconcile → portfolio 保留
    check("3.2 broker 无 real_position → portfolio 保留", len(eng.positions), 1)

# 3.3 有持仓 + real_vol == engine_vol → skip
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-3").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=1, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)   # 触发 _restore
    check("3.3 一致 → portfolio 保留", len(eng.positions), 1)
    evs = read_events(eng, kinds={"position_externally_closed_summary",
                                  "position_mismatch", "restore_reconcile_failed"})
    check("3.3b 一致 → 无 reconcile 事件", len(evs), 0)

# 3.4 有持仓 + real_vol < engine_vol → FIFO 部分平（trade reason=reconcile_external_partial）
#     构造：2 仓各 2 手（engine_vol=4），real_longs=2（差 2 手）→ 平最早仓 p_a 整笔 2 手，剩 p_b
with tmp_dir() as td:
    pos_a = make_position(Side.LONG, 2, 4545.0, 1, signal_key="restore-3-4-A").to_dict()
    pos_b = make_position(Side.LONG, 2, 4547.0, 2, signal_key="restore-3-4-B").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_a, pos_b])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=2, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("3.4a 部分平后剩 1 仓", len(eng.positions), 1)
    check("3.4b 保留最晚建仓的 (entry_bar_seq=2，FIFO 平最早)",
          eng.positions.positions[0].signal_key, "restore-3-4-B")
    trades = eng.store.trades()
    check("3.4c 1 条 reconcile_external_partial trade", len(trades), 1)
    if trades:
        check("3.4d Trade.signal_key = run 的 key（v3.1 run 级会计，"
              "被平仓单身份看 position_externally_closed 事件）",
              trades[0]["signal_key"], "seed-run")
        check("3.4e trade reason", trades[0]["reason"], "reconcile_external_partial")
    evs = read_events(eng, kinds={"position_externally_closed_summary"})
    check("3.4f 写 reconcile_partial summary", len(evs) >= 1, True)
    if evs:
        check("3.4g summary 带 source=restore",
              evs[0].get("source"), "restore")

# 3.5 有持仓 + real_vol == 0 → 全平
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-5").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("3.5a real_vol==0 → 全平", len(eng.positions), 0)
    check("3.5b state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("3.5c 1 条 reconcile_external_partial trade", len(trades), 1)

# 3.6 有持仓 + real_vol > engine_vol → 告警不接管
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-6").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=5, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("3.6a real_vol>engine_vol → portfolio 保留", len(eng.positions), 1)
    evs = read_events(eng, kinds={"position_mismatch"})
    check("3.6b 写 position_mismatch 告警", len(evs) >= 1, True)
    if evs:
        check("3.6c 告警带 source=restore",
              evs[0].get("source"), "restore")

# 3.7 broker.real_position 抛异常 → 写 restore_reconcile_failed，不阻断启动
class RaisingRealPosBroker(DryRunBroker):
    name = "raising_rp"
    def real_position(self, side):
        raise RuntimeError("simulated real_position failure")

with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-7").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    rp_b = RaisingRealPosBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF))
    eng = make_engine(td, broker=rp_b)
    check("3.7a 异常被吞 → 引擎能启动（portfolio 保留）", len(eng.positions), 1)
    evs = read_events(eng, kinds={"restore_reconcile_failed"})
    check("3.7b 写 restore_reconcile_failed", len(evs) >= 1, True)

# 3.8 source="on_bar" vs "restore" 差异：on_bar 拦截 bars_held<1，restore 不拦
with tmp_dir() as td:
    # 建一个仓后 bars_seen=1（同侧 bars_held=0）→ on_bar 应拦截，restore 不拦
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-8").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 1)  # bars_held = 1 - 1 = 0
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    # restore 路径不拦 → 应该全平
    check("3.8 restore 路径不拦 bars_held<1 → 全平", len(eng.positions), 0)


# ════════════════════════════════════════════════════════════════
# [4] F1+F2 集成
# ════════════════════════════════════════════════════════════════
# [4] F1 删除后的兼容回归
# ════════════════════════════════════════════════════════════════
print("\n[4] F1 删除后的兼容回归")

# 4.1 拆锁成交（dry_run 同步撮合）→ 目标仓被平；不再挂 in-flight、不再写复核事件
with tmp_dir() as td:
    eng = make_engine(td)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="yesterday-4-1",
                        entry_date="2026-08-31")
    eng.positions.add(pos)
    eng.positions.add(make_position(Side.SHORT, 1, 4549.0, 2,
                                    signal_key="yesterday-4-1-b",
                                    entry_date="2026-08-31"))
    eng.last_bar = make_bar()
    eng.bars_seen = 5
    sig = make_signal("close-sig-4-1", Side.SHORT)
    eng.on_signal(sig)
    check("4.1a 拆锁成交 → 目标多头被平（只剩那笔空头）",
          [p.signal_key for p in eng.positions.positions], ["yesterday-4-1-b"])
    check("4.1b 引擎无 _close_in_flight（复核机制已删）",
          hasattr(eng, "_close_in_flight"), False)
    evs = read_events(eng, kinds={"close_confirmed", "close_stuck_recovered",
                                  "close_stuck_confirmed"})
    check("4.1c 无卡单复核类事件", len(evs), 0)

# 4.2 F2 清幽灵 + 新信号正常开仓（不依赖 F1，保留）
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="ghost-pos").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    seed_run(pre_store)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("4.2a F2 清掉幽灵后 portfolio 空", len(eng.positions), 0)
    check("4.2b F2 清掉幽灵后 state IDLE", eng._state, EngineState.IDLE)
    eng.last_bar = make_bar()
    sig = make_signal("new-sig-4-2", Side.SHORT)
    eng.on_signal(sig)
    check("4.2c 新 SHORT 信号 → 空仓态正常开空仓", len(eng.positions), 1)
    check("4.2d 开空仓 side 正确",
          eng.positions.positions[0].side, Side.SHORT)

# 4.3 旧 kv 残留不致命：store 里遗留 _close_in_flight 时，重启引擎直接忽略
with tmp_dir() as td:
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("_close_in_flight", {"signal_key": "legacy"})
    pre_store.close()
    eng = make_engine(td)
    check("4.3a 重启后不加载遗留 in-flight（属性已不存在）",
          hasattr(eng, "_close_in_flight"), False)
    check("4.3b 引擎正常可用", eng.bars_seen >= 0, True)
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P16 Phase F 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
