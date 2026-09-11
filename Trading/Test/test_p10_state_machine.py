# -*- coding: utf-8 -*-
"""
P10 引擎态 / 账户态 / 信号门 单元测试
====================================
2026-09-11 Phase 7 改写。旧版锁死的是 Phase A/B 的**旧**状态机，其中三样已删除：
  · `PositionOrigin` / `ExitMode` 两个枚举 —— 随"持仓来源 / 出场模式"概念删除；
  · `Position.origin` 字段 —— 同上，仓单不再记出身；
  · `OrderIntent` 的 4 值（OPEN / LOCK / UNLOCK / CLOSE）→ 收敛为 **2 值**（OPEN / CLOSE）；
  · `Engine._close_position` / `_open_position` —— Phase 4 删除，统一走
    `_decide_action` / `_decide_exit` → `_execute`。

新旧口径的关键差别（本测试正面钉死）
    ① "引擎态" `_state` 不再有独立口径 —— 它是 `account_state()`（净敞口三态）的
       **派生镜像**：RUNNING → IN_TRADE，FLAT / LOCKED → IDLE。
       `EngineState.OPENING` / `EXITING` 两个瞬态值保留在枚举里（留给二期异步 broker），
       但**同步 broker 下引擎自己永远不会进入它们** —— 旧版那两条转移断言已不成立。
    ② 信号门不再看"持仓方向是否相反"，而是看账户三态（转移表 ①②③）：
       RUNNING → 一律忽略（含反向）；FLAT → ① 开仓；当日 LOCKED → ② 开仓；
       跨日 LOCKED → ③ 平掉反向最早的那一笔。
    ③ "平仓"的落账路径按今 / 昨仓分流：今仓 → 转移 ④ 反向 OPEN（留下双向持仓 = LOCKED，
       净敞口归零、PnL 不兑现）；跨日仓 → 转移 ⑤ CLOSE（记 Trade、离场兑现）。

硬性要求（本测试锁死）
    [1] 枚举：OrderIntent 2 值 / AccountState 3 值 / EngineState 4 值；PositionOrigin
        与 ExitMode **不存在**。
    [2] `Position` 无 origin 字段；to_dict 不含；from_dict 宽容忽略多余键；
        而"旧库拒绝启动"的闸门在 Engine（G1）。
    [3] `_restore` 的初始态推断（4 种持仓形态）。
    [4] 开仓成功 IDLE→IN_TRADE（账户态 RUNNING），且**不经过 OPENING**。
    [5] 开仓被拒 → 回 IDLE / FLAT。
    [6] 离场分流：今仓 → ④ LOCKED；跨日仓 → ⑤ CLOSE。
    [7] 信号门 RUNNING + 同向 → skip，不调 entry_policy。
    [8] 信号门 RUNNING + 反向 → 同样忽略（Q1=B：离场只由 L1-L3 负责）。
    [9] 信号门 FLAT + 信号 → 正常开仓（反向也不反手）。
    [10] 瞬态门：手工置 OPENING / EXITING → 信号标 in_flight；同步路径不产生瞬态。
    [11] 事件埋点：open 事件含 side/volume/entry_date/transition，**不含 origin**。
    [12] `_decide_action` 转移表入口（纯函数）：① / ② / ③ 与 RUNNING → None。

不需要真实 tqsdk / 网络；用 dry_run broker + 真实 Store(sqlite tempfile) 验证。
跑法：python Trading/Test/test_p10_state_machine.py
"""
from __future__ import annotations

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
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p10_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
import Trading.Infra.Types as _T  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, EngineState, ExitPlan, OrderIntent, Position, Side, Signal,
)

_PASS = 0
_FAIL = 0
_SYM = "CFFEX.IF2609"


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def make_signal(is_buy, price=4550.0, high=4552.0, low=4548.0,
                date="2026-09-01 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or Signal.make_key(date, bsp_type, is_buy)
    return Signal(key=sig_key, symbol=_SYM, freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-01 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def build_engine(tmpdir, *, ev_name="events.jsonl"):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, ev_name), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker


def make_pos(side=Side.LONG, vol=1, entry_price=4550.0, signal_key="MP",
             entry_bar_seq=1, entry_date="2026-09-01"):
    """直接构造一笔仓单（绕开开仓流程，用于摆出指定持仓形态）。"""
    return Position(
        symbol=_SYM, side=side, volume=vol, entry_price=entry_price,
        entry_at="2026-09-01 09:00", entry_bar_ts=4000,
        entry_bar_seq=entry_bar_seq, signal_key=signal_key,
        open_order_id="o-" + signal_key,
        exit_plan=ExitPlan(name="run_managed", stop_price=0.0),
        entry_date=entry_date)


def seed_run(store, side="LONG", anchor=4550.0, volume=1):
    """G2：净敞口 ≠ 0 的库必须带 kv run，否则引擎拒绝启动。"""
    store.set_json("run", {
        "side": side, "anchor": anchor, "volume": volume,
        "bar_ts": 4000, "bar_seq": 10, "signal_key": "SEED",
        "plan": {"name": "run_managed", "stop_price": 0.0,
                 "tp_price": None, "params": {}},
    })


# ════════════════════════════════════════════════════════════════
# [1] 枚举：收敛后的值集合 + 已删枚举的缺席
# ════════════════════════════════════════════════════════════════
print("\n[1] 枚举值集合（OrderIntent 收敛为 2 值）")
check("[1a] OrderIntent 只剩 open / close（LOCK/UNLOCK 已删）",
      sorted(e.value for e in OrderIntent), ["close", "open"])
check("[1b] OrderIntent.OPEN 值", OrderIntent.OPEN.value, "open")
check("[1c] OrderIntent.CLOSE 值", OrderIntent.CLOSE.value, "close")
check("[1d] AccountState 3 个值（三态唯一来源）",
      sorted(e.value for e in AccountState), ["flat", "locked", "running"])
check("[1e] EngineState 4 个值（OPENING/EXITING 保留给二期异步 broker）",
      sorted(e.value for e in EngineState), ["exiting", "idle", "in_trade", "opening"])
check("[1f] PositionOrigin 已从 Types 删除", hasattr(_T, "PositionOrigin"), False)
check("[1g] ExitMode 已从 Types 删除", hasattr(_T, "ExitMode"), False)


# ════════════════════════════════════════════════════════════════
# [2] Position 不再记"出身"；旧键的闸门在 Engine 侧
# ════════════════════════════════════════════════════════════════
print("\n[2] Position 无 origin；序列化不含旧键")
p_default = make_pos()
check("[2a] Position 无 origin 属性（字段已删）", hasattr(p_default, "origin"), False)
check("[2b] Position 无 lock_pair_id 属性", hasattr(p_default, "lock_pair_id"), False)
_d = p_default.to_dict()
check("[2c] to_dict 不含 origin", "origin" in _d, False)
check("[2d] to_dict 不含 lock_pair_id", "lock_pair_id" in _d, False)
check("[2e] to_dict 不含 exit_mode / entry_mode",
      [k for k in ("exit_mode", "entry_mode") if k in _d], [])
check("[2f] to_dict 含 entry_date（今/昨仓依据）", _d.get("entry_date"), "2026-09-01")
check("[2g] to_dict 含 entry_bar_seq（FIFO 选仓依据）", _d.get("entry_bar_seq"), 1)
# from_dict 是**宽容**的：多余键直接忽略（旧键的拒绝闸门不在这里，在 Engine G1）
_old = dict(_d, origin="signal_open", lock_pair_id="lock_00001")
_p_old = Position.from_dict(_old)
check("[2h] from_dict 忽略多余旧键（宽容，不抛错）", _p_old.side, Side.LONG)
check("[2i] from_dict roundtrip 保留 side/volume/entry_date",
      (_p_old.side, _p_old.volume, _p_old.entry_date),
      (Side.LONG, 1, "2026-09-01"))


# ════════════════════════════════════════════════════════════════
# [3] 状态机初始推断（_restore 路径）
# ════════════════════════════════════════════════════════════════
print("\n[3] 初始态推断（空 / 单边 / 双向 / 多笔）")

with tmp_dir() as tmp:
    engine, store, _ = build_engine(tmp)
    check("[3a] 空 store + 空持仓 → _state IDLE", engine._state, EngineState.IDLE)
    check("[3b] 空 store + 空持仓 → account_state FLAT",
          engine.account_state(), AccountState.FLAT)
    check("[3c] 空 store + 空持仓 → position is None", engine.position, None)

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("position", make_pos(side=Side.LONG, vol=1).to_dict())
    seed_run(store, side="LONG")     # net=1 → 需要 run
    store.close()
    engine, _st, _bk = build_engine(tmp)
    check("[3d] 单边持仓恢复 → _state IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[3e] 单边持仓恢复 → account_state RUNNING",
          engine.account_state(), AccountState.RUNNING)
    check("[3f] 恢复后 signal_key 保留", engine.position.signal_key, "MP")

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        make_pos(side=Side.LONG, vol=2, entry_bar_seq=1).to_dict(),
        make_pos(side=Side.SHORT, vol=2, entry_bar_seq=2).to_dict(),
    ])
    # net = 0（LOCKED）→ 不需要 run
    store.close()
    engine, _st, _bk = build_engine(tmp)
    check("[3g] 双向对消恢复 → account_state LOCKED",
          engine.account_state(), AccountState.LOCKED)
    check("[3h] LOCKED → _state IDLE（IDLE 同时覆盖 FLAT 与 LOCKED）",
          engine._state, EngineState.IDLE)

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        make_pos(side=Side.LONG, vol=1, entry_bar_seq=1).to_dict(),
        make_pos(side=Side.LONG, vol=1, entry_bar_seq=2).to_dict(),
        make_pos(side=Side.LONG, vol=1, entry_bar_seq=3).to_dict(),
    ])
    seed_run(store, side="LONG", volume=3)
    store.close()
    engine, _st, _bk = build_engine(tmp)
    check("[3i] 同向 3 笔（net=3）→ RUNNING → _state IN_TRADE",
          engine._state, EngineState.IN_TRADE)
    check("[3j] 同向 3 笔 → 净敞口 3", engine.positions.net_volume(), 3)
    # 单笔也算 RUNNING（不再有"单笔 = 软锁仓"的旧口径，D2 后的收窄）
    check("[3k] 口径收窄实证：单笔持仓是 RUNNING 而非 LOCKED",
          engine.positions.net_volume() != 0 and len(engine.positions) == 3, True)


# ════════════════════════════════════════════════════════════════
# [4] 开仓成功：IDLE → IN_TRADE（不经 OPENING）
# ════════════════════════════════════════════════════════════════
print("\n[4] 开仓成功 IDLE→IN_TRADE（同步 broker 无 OPENING 瞬态）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    check("[4a] 初始 _state IDLE", engine._state, EngineState.IDLE)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    sig = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig)
    check("[4b] 开仓成功后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[4c] 开仓成功后 position 非空", engine.position is not None, True)
    check("[4d] 新开仓 side LONG", engine.position.side, Side.LONG)
    check("[4e] 新开仓 volume 2（默认 max_volume=2）", engine.position.volume, 2)
    check("[4f] 新开仓 entry_date = 信号交易日",
          engine.position.entry_date, "2026-09-01")
    check("[4g] 开仓后 account_state RUNNING", engine.account_state(), AccountState.RUNNING)
    check("[4h] 开仓后 signal_action=opened", store.signal_action(sig.key), "opened")
    check("[4i] 开仓后 broker 恰好 1 单", len(engine.broker.orders), 1)


# ════════════════════════════════════════════════════════════════
# [5] 开仓被拒：回 IDLE / FLAT
# ════════════════════════════════════════════════════════════════
print("\n[5] 开仓被拒 IDLE→IDLE")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    def boom(intent, side, volume, ref_price, signal_key="", note="",
             entry_date="", is_exit=False):
        from Trading.Infra.Types import Order
        return Order(order_id="x", signal_key=signal_key, symbol=_SYM,
                     side=side, action="open", volume=int(volume),
                     price=0.0, status="rejected",
                     meta={"reject_reason": "test_boom"})

    engine.broker.submit = boom

    sig = make_signal(is_buy=True, date="2026-09-01 09:36")
    engine.on_signal(sig)
    check("[5a] 开仓被拒后 _state 回 IDLE", engine._state, EngineState.IDLE)
    check("[5b] 开仓被拒后 account_state FLAT",
          engine.account_state(), AccountState.FLAT)
    check("[5c] 开仓被拒后 position 仍为 None", engine.position, None)
    check("[5d] 开仓被拒后 signal_action=rejected",
          store.signal_action(sig.key), "rejected")


# ════════════════════════════════════════════════════════════════
# [6] 离场分流：今仓 → ④ 反向 OPEN（LOCKED）；跨日仓 → ⑤ CLOSE
# ════════════════════════════════════════════════════════════════
print("\n[6] 离场分流（今仓锁仓 / 跨日平仓）")

# 6A 今仓 → 转移 ④：反向 OPEN，净敞口归零，留双向持仓 = LOCKED，PnL 不兑现
with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    engine.on_signal(make_signal(is_buy=True, date="2026-09-01 09:35"))
    check("[6A-1] 开多后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[6A-2] 开多后 trades=0", len(store.trades()), 0)

    # 当日离场（同一交易日）
    engine._force_exit(engine.last_bar, "test_today_exit", trigger_price=4560.0)
    check("[6A-3] 今仓离场 → 簿内 2 笔（留下双向持仓）", len(engine.positions), 2)
    check("[6A-4] 双向：LONG + SHORT 各一笔",
          sorted(p.side.name for p in engine.positions.positions), ["LONG", "SHORT"])
    check("[6A-5] 净敞口归零", engine.positions.net_volume(), 0)
    check("[6A-6] account_state LOCKED", engine.account_state(), AccountState.LOCKED)
    check("[6A-7] _state 回 IDLE", engine._state, EngineState.IDLE)
    check("[6A-8] trades 仍 0（锁仓不兑现 PnL）", len(store.trades()), 0)
    engine.ev.flush()
    import json as _j
    _evs_a = [_j.loads(l) for l in open(engine.ev.path, encoding="utf-8") if l.strip()]
    _ord_a = [d for d in _evs_a if d.get("kind") == "order"]
    check("[6A-9] 最后一笔报单 transition=4（今仓反向开仓）",
          (_ord_a[-1].get("transition") if _ord_a else None), 4)
    check("[6A-10] 该报单 intent=open（④ 是「反向开仓」而非平仓）",
          (_ord_a[-1].get("intent") if _ord_a else None), "open")
    check("[6A-11] 该报单 is_exit=True（离场动作，D13）",
          (_ord_a[-1].get("is_exit") if _ord_a else None), True)

# 6B 跨日仓 → 转移 ⑤：CLOSE，记 Trade，簿清空
with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550, date="2026-09-01 09:40"))
    engine.on_signal(make_signal(is_buy=True, date="2026-09-01 09:35"))
    check("[6B-1] 开多 entry_date=2026-09-01",
          engine.position.entry_date, "2026-09-01")

    # 次一交易日的 bar → 该仓变"昨仓" → 转移 ⑤ CLOSE
    _bar2 = make_bar(2, 4560, 4565, 4555, 4560, date="2026-09-02 09:40")
    engine._force_exit(_bar2, "test_crossday_exit", trigger_price=4560.0)
    check("[6B-2] 跨日离场 → 簿清空", engine.positions.is_empty(), True)
    check("[6B-3] 跨日离场 → 净敞口 0", engine.positions.net_volume(), 0)
    check("[6B-4] 跨日离场 → account_state FLAT",
          engine.account_state(), AccountState.FLAT)
    check("[6B-5] 跨日离场 → _state IDLE", engine._state, EngineState.IDLE)
    check("[6B-6] 跨日 CLOSE → 记 1 笔 Trade（PnL 兑现）", len(store.trades()), 1)
    _ks2 = []
    engine.ev.flush()
    import json as _j2
    _ks2 = [_j2.loads(l) for l in open(engine.ev.path, encoding="utf-8") if l.strip()]
    _orders = [d for d in _ks2 if d.get("kind") == "order"]
    check("[6B-7] 最后一笔报单 transition=5",
          (_orders[-1].get("transition") if _orders else None), 5)
    check("[6B-8] 报单 is_exit=True（D13：离场由 is_exit 判定，不看 intent）",
          (_orders[-1].get("is_exit") if _orders else None), True)


# ════════════════════════════════════════════════════════════════
# [7] 信号门：RUNNING + 同向信号 → skip
# ════════════════════════════════════════════════════════════════
print("\n[7] 信号门：RUNNING + 同向 → skip")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("[7a] 开多后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)

    called = {"n": 0}
    orig = engine.entry_policy.decide

    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)

    engine.entry_policy.decide = spy
    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40")
    engine.on_signal(sig2)
    check("[7b] RUNNING+同向 → 不调 entry_policy.decide", called["n"], 0)
    check("[7c] RUNNING+同向 → _state 仍 IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[7d] RUNNING+同向 → position 仍是原来那笔",
          engine.position.signal_key, sig1.key)
    check("[7e] RUNNING+同向 → signal_action 标 skip",
          store.signal_action(sig2.key), "skip")
    check("[7f] RUNNING+同向 → 簿仍 1 笔", len(engine.positions), 1)


# ════════════════════════════════════════════════════════════════
# [8] 信号门：RUNNING + 反向信号 → 一律忽略（Q1=B）
# ════════════════════════════════════════════════════════════════
print("\n[8] 信号门：RUNNING + 反向 → 忽略（离场只由 L1-L3 负责）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("[8a] 开多后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[8b] 开多后 trades=0", len(store.trades()), 0)

    called = {"n": 0}
    orig = engine.entry_policy.decide

    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)

    engine.entry_policy.decide = spy
    sig2 = make_signal(is_buy=False, date="2026-09-01 09:40")
    engine.on_signal(sig2)
    check("[8c] RUNNING+反向 → 不调 entry_policy.decide", called["n"], 0)
    check("[8d] RUNNING+反向 → _state 仍 IN_TRADE（不因信号离场）",
          engine._state, EngineState.IN_TRADE)
    check("[8e] RUNNING+反向 → 仍是原来那笔多头（未锁仓）",
          (engine.position.side, engine.position.signal_key),
          (Side.LONG, sig1.key))
    check("[8f] RUNNING+反向 → trades 增 0", len(store.trades()), 0)
    check("[8g] RUNNING+反向 → signal_action 标 skip",
          store.signal_action(sig2.key), "skip")
    _evs = []
    engine.ev.flush()
    import json as _j3
    _evs = [_j3.loads(l) for l in open(engine.ev.path, encoding="utf-8") if l.strip()]
    _skips = [d for d in _evs if d.get("kind") == "signal_skip"]
    check("[8h] 跳过原因 = running_ignore_signal",
          (_skips[-1].get("reason") if _skips else None), "running_ignore_signal")


# ════════════════════════════════════════════════════════════════
# [9] 信号门：FLAT + 信号 → 正常开仓（反向也不反手）
# ════════════════════════════════════════════════════════════════
print("\n[9] 信号门：FLAT + 信号 → 正常开仓（反向也不反手）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    sig1 = make_signal(is_buy=False, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("[9a] FLAT+卖信号 → 直接开空", engine.position.side, Side.SHORT)
    check("[9b] FLAT+卖信号 → _state IN_TRADE", engine._state, EngineState.IN_TRADE)

    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40")
    engine.on_signal(sig2)
    check("[9c] 之后的反向信号 → 忽略，trades 增 0", len(store.trades()), 0)
    check("[9d] 反向信号后 _state 仍 IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[9e] 反向信号后 position 仍是那笔空头（未反手）",
          (engine.position.side, engine.position.signal_key),
          (Side.SHORT, sig1.key))

# FLAT + 双向已平（LOCKED 且跨日）不在本段覆盖面内 —— 见 [12] 转移表单测


# ════════════════════════════════════════════════════════════════
# [10] 瞬态门：手工置 OPENING / EXITING → in_flight；同步路径不产生瞬态
# ════════════════════════════════════════════════════════════════
print("\n[10] 瞬态门（OPENING / EXITING）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 同步 broker 下引擎**不会**自己进入瞬态：报单那一刻 _state 仍是 IDLE
    captured = {"state_at_submit": None}
    orig_submit = engine.broker.submit

    def hook_submit(*a, **kw):
        captured["state_at_submit"] = engine._state
        return orig_submit(*a, **kw)

    engine.broker.submit = hook_submit
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("[10a] 报单瞬间 _state = IDLE（同步 broker 不产生 OPENING 瞬态）",
          captured["state_at_submit"], EngineState.IDLE)
    check("[10b] 开仓完成后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[10c] 全流程未曾出现 OPENING（_state 是派生镜像，不是过程机）",
          captured["state_at_submit"] is EngineState.OPENING, False)

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine._state = EngineState.OPENING          # 手工模拟"二期异步 broker 在途"
    engine.on_signal(sig1)
    check("[10d] OPENING 状态进信号 → 幂等键仍占位（in_flight）",
          store.signal_action(sig1.key), "in_flight")
    check("[10e] OPENING 状态进信号 → _state 保持 OPENING（没改）",
          engine._state, EngineState.OPENING)
    check("[10f] OPENING 状态进信号 → 未报单", len(engine.broker.orders), 0)

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    engine.on_signal(make_signal(is_buy=True, date="2026-09-01 09:35"))
    check("[10g] 开仓后 _state IN_TRADE", engine._state, EngineState.IN_TRADE)

    engine._state = EngineState.EXITING           # 手工模拟在途平仓
    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40")
    engine.on_signal(sig2)
    check("[10h] EXITING 状态进信号 → 幂等键仍占位（in_flight）",
          store.signal_action(sig2.key), "in_flight")
    check("[10i] EXITING 状态进信号 → _state 不变", engine._state, EngineState.EXITING)
    check("[10j] EXITING 状态进信号 → 未新增报单", len(engine.broker.orders), 1)


# ════════════════════════════════════════════════════════════════
# [11] 事件埋点：不含 origin，含 side/volume/entry_date/transition
# ════════════════════════════════════════════════════════════════
print("\n[11] 事件埋点（新字段口径）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))
    engine.on_signal(make_signal(is_buy=True, date="2026-09-01 09:35"))
    engine.ev.flush()
    import json as _j4
    _all = [_j4.loads(l) for l in open(engine.ev.path, encoding="utf-8") if l.strip()]
    _opens = [d for d in _all if d.get("kind") == "open"]
    check("[11a] 有 1 条 open 事件", len(_opens), 1)
    _o = _opens[0] if _opens else {}
    # ⚠️ 事件里的 side 走 `str(Side)` → 中文"多"/"空"（历史口径，本次重构未改）。
    #    注意它与 kv `positions` 里的 `side:"LONG"`（走 `.name`）**口径不同**：
    #    跨这两个数据源做 join 的人会踩坑。此处如实钉住现状，改动属另一项决策。
    check("[11b] open 事件 side 用中文标签（str(Side) 的历史口径）",
          _o.get("side"), "\u591a")
    check("[11b-2] 同一字段在 kv positions 里是 LONG（两处口径不一致，已知）",
          (store.get_json("positions") or [{}])[0].get("side"), "LONG")
    check("[11c] open 事件含 volume", _o.get("volume"), 2)
    check("[11d] open 事件含 entry_date", _o.get("entry_date"), "2026-09-01")
    check("[11e] open 事件含 transition=1", _o.get("transition"), 1)
    check("[11f] open 事件含 signal_key", _o.get("signal_key"),
          make_signal(is_buy=True, date="2026-09-01 09:35").key)
    # 全事件流不含已删术语
    _raw = open(engine.ev.path, encoding="utf-8").read()
    check("[11g] 事件流不含 origin", "origin" in _raw, False)
    check("[11h] 事件流不含 soft_exit_lock", "soft_exit_lock" in _raw, False)
    check("[11i] 事件流不含 lock_pair_id", "lock_pair_id" in _raw, False)


# ════════════════════════════════════════════════════════════════
# [12] `_decide_action` 转移表入口（纯函数：① / ② / ③ / RUNNING→None）
# ════════════════════════════════════════════════════════════════
print("\n[12] 转移表入口 `_decide_action`（纯函数）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    _bar = make_bar(1, 4550, 4552, 4548, 4550)
    engine.on_bar(_bar)
    today = engine._current_trading_day(_bar)
    check("[12a] 交易日 = bar 所属日", today, "2026-09-01")

    # 转移 ①：FLAT + 信号 → OPEN（信号方向）
    _buy = make_signal(is_buy=True, date="2026-09-01 09:35")
    _a1 = engine._decide_action(_buy, today)
    check("[12b] ① FLAT → intent OPEN", _a1.intent, OrderIntent.OPEN)
    check("[12c] ① FLAT → side = 信号方向", _a1.side, Side.LONG)
    check("[12d] ① FLAT → volume = lots_per_signal", _a1.volume, engine.lots_per_signal)
    check("[12e] ① FLAT → transition=1", _a1.transition, 1)
    check("[12f] ① FLAT → is_exit=False", _a1.is_exit, False)
    # 纯函数：不得改状态
    check("[12g] `_decide_action` 是纯函数（簿未被改动）",
          engine.positions.is_empty(), True)

    # RUNNING + 信号 → None（规则 ⑶）
    engine.positions.add(make_pos(side=Side.LONG, vol=2, entry_date=today))
    check("[12h] 有净敞口 → account_state RUNNING",
          engine.account_state(), AccountState.RUNNING)
    _a2 = engine._decide_action(_buy, today)
    check("[12i] RUNNING → _decide_action 返回 None", _a2, None)

    # 转移 ②：当日 LOCKED（net=0，最新一笔建仓日 = today）→ OPEN
    engine.positions.clear()
    engine.positions.add(make_pos(side=Side.LONG, vol=2, entry_date=today,
                                  entry_bar_seq=1))
    engine.positions.add(make_pos(side=Side.SHORT, vol=2, entry_date=today,
                                  entry_bar_seq=2))
    check("[12j] 双向且 net=0 → LOCKED", engine.account_state(), AccountState.LOCKED)
    _a3 = engine._decide_action(_buy, today)
    check("[12k] ② 当日锁 → intent OPEN", _a3.intent, OrderIntent.OPEN)
    check("[12l] ② 当日锁 → side = 信号方向（同向加仓，不动锁仓仓单）",
          _a3.side, Side.LONG)
    check("[12m] ② 当日锁 → transition=2", _a3.transition, 2)
    check("[12n] ② 当日锁 → is_exit=False", _a3.is_exit, False)

    # 转移 ③：跨日 LOCKED（最新一笔建仓日 < today）→ CLOSE（反向最早一笔）
    engine.positions.clear()
    engine.positions.add(make_pos(side=Side.LONG, vol=2, entry_date="2026-08-28",
                                  entry_bar_seq=1, signal_key="OLD-L"))
    engine.positions.add(make_pos(side=Side.SHORT, vol=2, entry_date="2026-08-29",
                                  entry_bar_seq=2, signal_key="OLD-S"))
    check("[12o] 跨日双向 → LOCKED", engine.account_state(), AccountState.LOCKED)
    _a4 = engine._decide_action(_buy, today)
    check("[12p] ③ 跨日锁 → intent CLOSE", _a4.intent, OrderIntent.CLOSE)
    check("[12q] ③ 跨日锁 → side = 反信号方向（买信号 → 卖平空头）",
          _a4.side, Side.SHORT)
    check("[12r] ③ 跨日锁 → target = 反向最早一笔",
          _a4.target.signal_key if _a4.target else None, "OLD-S")
    check("[12s] ③ 跨日锁 → transition=3", _a4.transition, 3)
    check("[12t] ③ 跨日锁 → is_exit=False（拆锁不是离场，D13）", _a4.is_exit, False)
    check("[12u] ③ 跨日锁 → volume = min(lots, target.volume)",
          _a4.volume, min(engine.lots_per_signal, 2))
    # 卖信号 → 平多头最早一笔
    _sell = make_signal(is_buy=False, date="2026-09-01 09:36")
    _a5 = engine._decide_action(_sell, today)
    check("[12v] ③ 卖信号 → target = 反向最早的多头",
          _a5.target.signal_key if _a5.target else None, "OLD-L")
    check("[12w] ③ 卖信号 → side = LONG", _a5.side, Side.LONG)


print()
print("=" * 60)
print("P10 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
if _FAIL:
    sys.exit(1)
