# -*- coding: utf-8 -*-
"""
P11 broker intent + exit_mode 联动单测（Phase C + D 合并）
=========================================================
背景
    Phase C 把 broker.submit() 改用 OrderIntent 显式表达意图（OPEN/UNLOCK/CLOSE/LOCK），
    intent → CTP OpenCloseType 由 INTENT_TO_OFFSET 权威表映射。dry_run 同样遵守便于审计。
    Phase D 按 pos.entry_mode 硬规则决定离场方式：
        OPEN_FIRST   → LOCK_SOFT  → OrderIntent.LOCK  + 反向 side
        UNLOCK_FIRST → CLOSE_HARD → OrderIntent.CLOSE + pos.side
    没有 entry_mode 字段的旧持仓（默认 OPEN_FIRST）也走 LOCK_SOFT。

硬性要求（本测试锁死）
    ① INTENT_TO_OFFSET 表 4 个键值对正确。
    ② broker._resolve_intent 兼容旧 action 字符串（"open"/"close"/"lock"/"unlock"）。
    ③ dry_run.submit 各 intent 写入 Order.meta.{intent,offset}；LOCK 报为 "open" 但 meta.intent="lock"。
    ④ engine._exit_intent：
        OPEN_FIRST → (OrderIntent.LOCK, 反向 side)
        UNLOCK_FIRST → (OrderIntent.CLOSE, pos.side)
        默认 / 无 entry_mode → (OrderIntent.LOCK, 反向 side)
    ⑤ end-to-end：dry_run broker，OPEN_FIRST 持仓 SL 触发 → broker 收到 LOCK + 反向 side。
    ⑥ end-to-end：dry_run broker，UNLOCK_FIRST 持仓 SL 触发 → broker 收到 CLOSE + pos.side。
    ⑦ events.jsonl 里 close 事件 exit_mode 字段正确（lock / close）。
    ⑧ 反向信号触发"只离场"时，离场方式也按 entry_mode 走（不反手开新仓）。

跑法：python tests/test_p11_intent_exitmode.py
"""
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
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p11_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg import brokers  # noqa: E402  注册 dry_run / simnow
from tg.brokers.base import INTENT_TO_OFFSET, Broker  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EntryMode, ExitPlan, OrderIntent, Position, Side, Signal,
)

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


def make_signal(is_buy, price=4550.0, high=4552.0, low=4548.0, date="2026-09-01 09:35",
                bsp_type="1", sig_key=None):
    sig_key = sig_key or Signal.make_key(date, bsp_type, is_buy)
    return Signal(key=sig_key, symbol="CFFEX.IF", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-01 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def build_engine(tmpdir, exit_plan_stop=4540.0):
    """构造最小可跑 GatewayEngine：dry_run broker + 默认策略 + 临时 sqlite"""
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    # 默认出场策略：固定 10 点止损（无 ATR），便于触发 SL
    exitp = DefaultExitPolicy({"stop_distance_points": 10.0,
                               "tp_distance_points": 0.0})
    store_path = os.path.join(tmpdir, "state.db")
    store = Store(store_path)
    event_path = os.path.join(tmpdir, "events.jsonl")
    ev = EventLog(event_path, echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


# ════════════════════════════════════════════════════════════════
# [1] INTENT_TO_OFFSET 权威映射表
# ════════════════════════════════════════════════════════════════
print("\n[1] INTENT_TO_OFFSET 4 个键值对")
check("OPEN → OPEN", INTENT_TO_OFFSET[OrderIntent.OPEN], "OPEN")
check("LOCK → OPEN (报文同 OPEN 但语义不同)",
      INTENT_TO_OFFSET[OrderIntent.LOCK], "OPEN")
check("CLOSE → CLOSE", INTENT_TO_OFFSET[OrderIntent.CLOSE], "CLOSE")
check("UNLOCK → CLOSEYESTERDAY (避开平今)",
      INTENT_TO_OFFSET[OrderIntent.UNLOCK], "CLOSEYESTERDAY")


# ════════════════════════════════════════════════════════════════
# [2] _resolve_intent 字符串兼容
# ════════════════════════════════════════════════════════════════
print("\n[2] Broker._resolve_intent 兼容旧 action 字符串")
check('"open" → OrderIntent.OPEN',
      Broker._resolve_intent("open", Side.LONG), OrderIntent.OPEN)
check('"close" → OrderIntent.CLOSE',
      Broker._resolve_intent("close", Side.LONG), OrderIntent.CLOSE)
check('"lock" → OrderIntent.LOCK',
      Broker._resolve_intent("lock", Side.LONG), OrderIntent.LOCK)
check('"unlock" → OrderIntent.UNLOCK',
      Broker._resolve_intent("unlock", Side.LONG), OrderIntent.UNLOCK)
check("OrderIntent 枚举原样返回",
      Broker._resolve_intent(OrderIntent.CLOSE, Side.LONG), OrderIntent.CLOSE)
check("None → 默认 OrderIntent.OPEN (防御兜底)",
      Broker._resolve_intent(None, Side.LONG), OrderIntent.OPEN)
# 坏值应抛 ValueError
try:
    Broker._resolve_intent("nonsense", Side.LONG)
    check("坏值抛 ValueError", False, True)
except ValueError:
    check("坏值抛 ValueError", True, True)


# ════════════════════════════════════════════════════════════════
# [3] dry_run 4 种 intent 报文字段
# ════════════════════════════════════════════════════════════════
print("\n[3] dry_run.submit 4 种 intent → Order.meta 字段")
spec = InstrumentSpec()
broker = DryRunBroker(spec, {})
side_l = Side.LONG
side_s = Side.SHORT

o_open = broker.submit(OrderIntent.OPEN, side_l, 1, 4550.0, "k1", "open1")
check("OPEN 报 action='open'", o_open.action, "open")
check("OPEN 报 meta.intent='open'", o_open.meta.get("intent"), "open")
check("OPEN 报 meta.offset='OPEN'", o_open.meta.get("offset"), "OPEN")

o_lock = broker.submit(OrderIntent.LOCK, side_s, 1, 4550.0, "k2", "lock1")
check("LOCK 报 action='open' (LOCK 走 Open 报文)",
      o_lock.action, "open")
check("LOCK 报 meta.intent='lock' (语义是锁仓)", o_lock.meta.get("intent"), "lock")
check("LOCK 报 meta.offset='OPEN'", o_lock.meta.get("offset"), "OPEN")
check("LOCK 报 side=SHORT (反向，由引擎填)",
      o_lock.side, Side.SHORT)

o_close = broker.submit(OrderIntent.CLOSE, side_l, 1, 4550.0, "k3", "close1")
check("CLOSE 报 action='close'", o_close.action, "close")
check("CLOSE 报 meta.intent='close'", o_close.meta.get("intent"), "close")
check("CLOSE 报 meta.offset='CLOSE'", o_close.meta.get("offset"), "CLOSE")

o_unlock = broker.submit(OrderIntent.UNLOCK, side_l, 1, 4550.0, "k4", "unlock1")
check("UNLOCK 报 action='close' (Unlock 走 CloseYesterday 报文)",
      o_unlock.action, "close")
check("UNLOCK 报 meta.intent='unlock'", o_unlock.meta.get("intent"), "unlock")
check("UNLOCK 报 meta.offset='CLOSEYESTERDAY'",
      o_unlock.meta.get("offset"), "CLOSEYESTERDAY")

# 字符串 action 兼容
o_legacy = broker.submit("close", side_l, 1, 4550.0, "k5", "legacy")
check('旧 "close" 字符串仍被接受 → meta.intent="close"',
      o_legacy.meta.get("intent"), "close")


# ════════════════════════════════════════════════════════════════
# [4] engine._exit_intent 联动规则
# ════════════════════════════════════════════════════════════════
print("\n[4] engine._exit_intent 联动硬规则")
p_open = Position(symbol="X", side=Side.LONG, volume=1, entry_price=100.0,
                  entry_at="", entry_bar_ts=0, signal_key="k", open_order_id="o",
                  exit_plan=ExitPlan(name="x", stop_price=99.0),
                  entry_mode=EntryMode.OPEN_FIRST)
intent, side = GatewayEngine._exit_intent(p_open)
check("OPEN_FIRST → OrderIntent.LOCK", intent, OrderIntent.LOCK)
check("OPEN_FIRST → 反向 side (LONG→SHORT)", side, Side.SHORT)

p_unlock = Position(symbol="X", side=Side.LONG, volume=1, entry_price=100.0,
                    entry_at="", entry_bar_ts=0, signal_key="k", open_order_id="o",
                    exit_plan=ExitPlan(name="x", stop_price=99.0),
                    entry_mode=EntryMode.UNLOCK_FIRST)
intent2, side2 = GatewayEngine._exit_intent(p_unlock)
check("UNLOCK_FIRST → OrderIntent.CLOSE", intent2, OrderIntent.CLOSE)
check("UNLOCK_FIRST → pos.side (LONG)", side2, Side.LONG)

# 默认 entry_mode（OPEN_FIRST）走 LOCK_SOFT
p_default = Position(symbol="X", side=Side.SHORT, volume=1, entry_price=100.0,
                     entry_at="", entry_bar_ts=0, signal_key="k", open_order_id="o",
                     exit_plan=ExitPlan(name="x", stop_price=99.0))
# entry_mode 默认 OPEN_FIRST
check("默认 entry_mode==OPEN_FIRST", p_default.entry_mode, EntryMode.OPEN_FIRST)
intent3, side3 = GatewayEngine._exit_intent(p_default)
check("默认 → OrderIntent.LOCK", intent3, OrderIntent.LOCK)
check("默认 + SHORT 持仓 → 反向 side (LONG)", side3, Side.LONG)


# ════════════════════════════════════════════════════════════════
# [5] end-to-end：OPEN_FIRST 持仓 SL → broker 收 LOCK + 反向
# ════════════════════════════════════════════════════════════════
print("\n[5] end-to-end: OPEN_FIRST 持仓 SL 触发 → LOCK 报文")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # 开多仓（OPEN_FIRST）
    sig = make_signal(is_buy=True, price=4550.0, high=4552.0, low=4548.0,
                      date="2026-09-01 09:35")
    engine.on_signal(sig)
    check("开仓后 state=IN_TRADE", engine._state.name, "IN_TRADE")
    check("开仓后 position.side=LONG", engine.position.side, Side.LONG)
    check("开仓后 position.entry_mode=OPEN_FIRST",
          engine.position.entry_mode, EntryMode.OPEN_FIRST)
    # 推一根 bar 让 SL 触发（low 触及 stop_price）
    bar = make_bar(5000, 4551.0, 4551.0, 4535.0, 4540.0, date="2026-09-01 09:40")
    engine.on_bar(bar)
    check("SL 触发后 state=IDLE (已离场)", engine._state.name, "IDLE")
    check("SL 触发后 position=None", engine.position, None)
    # 检查 broker 是否收到 LOCK 报
    lock_orders = [b for b in broker.orders
                   if b.meta.get("intent") == "lock"]
    check("broker 收到 1 笔 LOCK 报", len(lock_orders), 1)
    if lock_orders:
        lo = lock_orders[0]
        check("LOCK 报 side=SHORT (反向 LONG→SHORT)", lo.side, Side.SHORT)
        check("LOCK 报 action='open' (LOCK 用 Open 报文)",
              lo.action, "open")
        check("LOCK 报 offset='OPEN'", lo.meta.get("offset"), "OPEN")
        check("LOCK 报 status=filled (dry_run 撮合成功)",
              lo.status, "filled")
    # Trade.net_points 应该按 trigger_price 结算
    trades = store.trades()
    check("store 落了 1 笔 Trade", len(trades), 1)
    # events.jsonl 验证 close 事件带 exit_mode=lock
    ev.flush()  # 强制刷盘，否则 EventLog 的 1s 缓冲未到点
    ev_path = os.path.join(tmp, "events.jsonl")
    with open(ev_path, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    close_events = [e for e in lines if e.get("kind") == "close"]
    check("events.jsonl 至少 1 条 close 事件", len(close_events) >= 1, True)
    if close_events:
        check("close 事件 exit_mode=lock",
              close_events[0].get("exit_mode"), "lock")
        check("close 事件 entry_mode=open_first",
              close_events[0].get("entry_mode"), "open_first")


# ════════════════════════════════════════════════════════════════
# [6] end-to-end：UNLOCK_FIRST 持仓 SL → broker 收 CLOSE + pos.side
# ════════════════════════════════════════════════════════════════
print("\n[6] end-to-end: UNLOCK_FIRST 持仓 SL 触发 → CLOSE 报文")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # 直接构造一个 UNLOCK_FIRST 持仓（绕开入场逻辑，模拟"昨日锁仓遗留"）
    pos = Position(symbol="CFFEX.IF", side=Side.LONG, volume=1,
                   entry_price=4550.0, entry_at="2026-09-01 09:00",
                   entry_bar_ts=4000, signal_key="LEGACY_LOCK",
                   open_order_id="legacy-o1",
                   exit_plan=ExitPlan(name="x", stop_price=4540.0),
                   entry_mode=EntryMode.UNLOCK_FIRST)
    engine.position = pos
    engine._state = engine._state.__class__.IN_TRADE
    store.set_json("position", pos.to_dict())
    # 推一根 bar 让 SL 触发
    bar = make_bar(5000, 4551.0, 4551.0, 4535.0, 4540.0, date="2026-09-01 09:40")
    engine.on_bar(bar)
    check("UNLOCK_FIRST SL 触发 → state=IDLE", engine._state.name, "IDLE")
    # broker 应该收到 CLOSE 报（不是 LOCK）
    close_orders = [b for b in broker.orders
                    if b.meta.get("intent") == "close"]
    check("broker 收到 1 笔 CLOSE 报", len(close_orders), 1)
    if close_orders:
        co = close_orders[0]
        check("CLOSE 报 side=LONG (pos.side 不变)", co.side, Side.LONG)
        check("CLOSE 报 action='close'", co.action, "close")
    # events.jsonl 验证 close 事件带 exit_mode=close
    ev.flush()
    ev_path = os.path.join(tmp, "events.jsonl")
    with open(ev_path, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    close_events = [e for e in lines if e.get("kind") == "close"]
    if close_events:
        check("UNLOCK_FIRST close 事件 exit_mode=close",
              close_events[0].get("exit_mode"), "close")
        check("UNLOCK_FIRST close 事件 entry_mode=unlock_first",
              close_events[0].get("entry_mode"), "unlock_first")


# ════════════════════════════════════════════════════════════════
# [7] 反向信号触发"只离场"时，离场方式也按 entry_mode 走
# ════════════════════════════════════════════════════════════════
print("\n[7] 反向信号触发只离场：按 entry_mode 决定离场方式")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # 开多仓（OPEN_FIRST）
    sig_buy = make_signal(is_buy=True, price=4550.0, high=4552.0, low=4548.0,
                          date="2026-09-01 09:35")
    engine.on_signal(sig_buy)
    check("开仓后 state=IN_TRADE", engine._state.name, "IN_TRADE")
    # 推一根 bar（不要触发 SL，让持仓还在）
    bar = make_bar(5000, 4551.0, 4552.0, 4549.0, 4551.0, date="2026-09-01 09:40")
    engine.on_bar(bar)
    # 反向（卖出）信号：只离场
    sig_sell = make_signal(is_buy=False, price=4551.0, high=4553.0, low=4550.0,
                           date="2026-09-01 09:45", bsp_type="2")
    engine.on_signal(sig_sell)
    check("反向信号触发后 state=IDLE", engine._state.name, "IDLE")
    check("反向信号触发后 position=None", engine.position, None)
    # broker 应该收 1 笔 LOCK 报（OPEN_FIRST 持仓 → LOCK 离场）
    lock_orders = [b for b in broker.orders
                   if b.meta.get("intent") == "lock"]
    check("反向信号触发 LOCK 离场（OPEN_FIRST → LOCK）", len(lock_orders), 1)
    if lock_orders:
        check("LOCK 报 side=SHORT (反向)", lock_orders[0].side, Side.SHORT)
    # 反向信号不应触发新开仓
    open_orders = [b for b in broker.orders
                   if b.meta.get("intent") == "open"]
    check("反向信号没有再触发 OPEN 开仓", len(open_orders), 1)  # 只有最初那笔


# ════════════════════════════════════════════════════════════════
# [8] 持久化兼容：Position.entry_mode=UNLOCK_FIRST 也能 roundtrip
# ════════════════════════════════════════════════════════════════
print("\n[8] UNLOCK_FIRST 持久化兼容")
p = Position(symbol="X", side=Side.LONG, volume=1, entry_price=100.0,
            entry_at="t", entry_bar_ts=0, signal_key="k", open_order_id="o",
            exit_plan=ExitPlan(name="x", stop_price=99.0),
            entry_mode=EntryMode.UNLOCK_FIRST)
d = p.to_dict()
p2 = Position.from_dict(d)
check("UNLOCK_FIRST roundtrip", p2.entry_mode, EntryMode.UNLOCK_FIRST)


# ════════════════════════════════════════════════════════════════
# 总结
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"P11 结果: {_PASS} 通过 / {_FAIL} 失败")
print("=" * 60)
if _FAIL:
    raise SystemExit(1)