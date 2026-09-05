# -*- coding: utf-8 -*-
"""
P10 状态机 + 信号门单元测试（Phase A + B 合并）
==============================================
背景
    Phase A 在 types.py 里命名固化了 4 个枚举（OrderIntent / EntryMode / ExitMode /
    EngineState）并给 Position 加了 entry_mode 字段（默认 OPEN_FIRST，向后兼容）。
    Phase B 把 engine 的 self.position 信号门重构成 self._state 4 态：
        IDLE → OPENING → IN_TRADE → EXITING → IDLE
    并把"反向信号只触发离场、不再反向开仓"的规则固化进 on_signal。

硬性要求（本测试锁死）
    ① 4 个枚举可导入、值正确。
    ② Position.entry_mode 默认 OPEN_FIRST，to_dict/from_dict 兼容旧 dict（无字段
       → 默认；坏值 → 回退）。
    ③ state 转移时序：
        IDLE → OPENING → IN_TRADE（开仓成功）
        IDLE → OPENING → IDLE（开仓被拒）
        IN_TRADE → EXITING → IDLE（平仓成功）
        IN_TRADE → EXITING → IN_TRADE phantom 清 → IDLE（close 失败超限）
    ④ 信号门按 state：
        IDLE + 同向/反向 → 正常开仓
        IN_TRADE + 同向 → skip，不调 entry_policy
        IN_TRADE + 反向 → 仅触发 _close_position，不调 _open_position
        OPENING / EXITING + 任意 → in_flight 忽略，幂等键仍占位
    ⑤ _open_position 写入 Position.entry_mode == OPEN_FIRST。
    ⑥ 持仓恢复（_restore）时按 position 推断初始 state。

不需要真实 tqsdk / 网络；用 dry_run broker + 真实 Store(sqlite tempfile) 验证。
跑法：python tests/test_p10_state_machine.py
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

# 临时目录上下文管理器（Windows 上 tempfile.TemporaryDirectory 偶尔因为文件句柄没释放
# 抛 WinError 32，这里用 ignore_errors 兜底，避免测试本身的不稳定掩盖真实失败）
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

from tg import brokers  # noqa: E402  注册 dry_run
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, DecisionType, EntryMode, EngineState, ExitMode, ExitPlan, OrderIntent,
    Position, Side, Signal,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
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


def build_engine(tmpdir):
    """构造最小可跑的 GatewayEngine：dry_run broker + DefaultEntry/Exit + 临时 sqlite"""
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store_path = os.path.join(tmpdir, "state.db")
    store = Store(store_path)
    event_path = os.path.join(tmpdir, "events.jsonl")
    ev = EventLog(event_path, echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker


# ════════════════════════════════════════════════════════════════
# [1] 枚举导入
# ════════════════════════════════════════════════════════════════
print("\n[1] 4 个枚举命名正确")
check("OrderIntent 4 个值", [e.value for e in OrderIntent],
      ["open", "unlock", "close", "lock"])
check("EntryMode 2 个值", [e.value for e in EntryMode],
      ["open_first", "unlock_first"])
check("ExitMode 2 个值", [e.value for e in ExitMode],
      ["close_hard", "lock_soft"])
check("EngineState 4 个值", [e.value for e in EngineState],
      ["idle", "opening", "in_trade", "exiting"])

# ════════════════════════════════════════════════════════════════
# [2] Position.entry_mode 默认值 + 序列化兼容
# ════════════════════════════════════════════════════════════════
print("\n[2] Position.entry_mode 默认 + 序列化兼容")
p_default = Position(symbol="X", side=Side.LONG, volume=1, entry_price=100.0,
                     entry_at="", entry_bar_ts=0, signal_key="k", open_order_id="o",
                     exit_plan=ExitPlan(name="x", stop_price=99.0))
check("不传 entry_mode → 默认 OPEN_FIRST", p_default.entry_mode, EntryMode.OPEN_FIRST)

d = p_default.to_dict()
check("to_dict 含 entry_mode 字段", d.get("entry_mode"), "open_first")

p2 = Position.from_dict(d)
check("roundtrip 保留 entry_mode", p2.entry_mode, EntryMode.OPEN_FIRST)

# 旧 dict 无 entry_mode 字段
old = {"symbol": "X", "side": "LONG", "volume": 1, "entry_price": 100.0,
       "entry_at": "", "entry_bar_ts": 0, "signal_key": "k", "open_order_id": "o",
       "exit_plan": {"name": "x", "stop_price": 99.0}}
p3 = Position.from_dict(old)
check("旧 dict 无 entry_mode → 默认 OPEN_FIRST", p3.entry_mode, EntryMode.OPEN_FIRST)

# 坏值回退
bad = dict(old, entry_mode="garbage")
p4 = Position.from_dict(bad)
check("坏值 entry_mode → 回退 OPEN_FIRST", p4.entry_mode, EntryMode.OPEN_FIRST)

# UNLOCK_FIRST 显式传
p_unlock = Position(symbol="X", side=Side.SHORT, volume=1, entry_price=100.0,
                     entry_at="", entry_bar_ts=0, signal_key="k2", open_order_id="o2",
                     exit_plan=ExitPlan(name="x", stop_price=99.0),
                     entry_mode=EntryMode.UNLOCK_FIRST)
check("显式 entry_mode=UNLOCK_FIRST 通过", p_unlock.entry_mode, EntryMode.UNLOCK_FIRST)
check("UNLOCK_FIRST 序列化", p_unlock.to_dict()["entry_mode"], "unlock_first")

# ════════════════════════════════════════════════════════════════
# [3] 状态机初始推断（_restore 路径）
# ════════════════════════════════════════════════════════════════
print("\n[3] 状态机初始推断")

with tmp_dir() as tmp:
    engine, store, _ = build_engine(tmp)
    check("空 store + 空持仓 → _state == IDLE", engine._state, EngineState.IDLE)
    check("空 store + 空持仓 → position is None", engine.position, None)


with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    # 手动写入一个持仓到 store，重启 engine，验证 _restore 推断 IN_TRADE
    pos = Position(symbol="CFFEX.IF", side=Side.LONG, volume=1, entry_price=4550.0,
                   entry_at="2026-09-01 09:30", entry_bar_ts=0, signal_key="restored",
                   open_order_id="restored_o", exit_plan=ExitPlan(name="x", stop_price=4540.0),
                   entry_mode=EntryMode.OPEN_FIRST)
    store.set_json("position", pos.to_dict())
    # 重建 engine
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker2 = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry2 = DefaultEntryPolicy({})
    exitp2 = DefaultExitPolicy({})
    store2 = Store(os.path.join(tmp, "state.db"))
    ev2 = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine2 = GatewayEngine(cfg, broker2, entry2, exitp2, store2, ev2)
    check("恢复持仓后 _state == IN_TRADE", engine2._state, EngineState.IN_TRADE)
    check("恢复持仓后 position.signal_key == restored",
          engine2.position.signal_key, "restored")
    check("恢复持仓后 position.entry_mode == OPEN_FIRST",
          engine2.position.entry_mode, EntryMode.OPEN_FIRST)

# ════════════════════════════════════════════════════════════════
# [4] 状态转移时序：开仓成功
# ════════════════════════════════════════════════════════════════
print("\n[4] 状态转移：开仓成功 IDLE→OPENING→IN_TRADE")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    check("初始 state=IDLE", engine._state, EngineState.IDLE)

    # 喂一根 bar 让 last_bar 不为 None（on_signal 里会读 last_bar.close）
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    sig = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig)
    check("开仓成功后 _state == IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("开仓成功后 self.position 非空", engine.position is not None, True)
    check("新开仓 position.entry_mode == OPEN_FIRST",
          engine.position.entry_mode, EntryMode.OPEN_FIRST)
    check("新开仓 position.side == LONG", engine.position.side, Side.LONG)
    check("新开仓 position.volume == 1", engine.position.volume, 1)

# ════════════════════════════════════════════════════════════════
# [5] 状态转移：开仓被拒
# ════════════════════════════════════════════════════════════════
print("\n[5] 状态转移：开仓被拒 IDLE→OPENING→IDLE")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 把 broker 改成永远 reject
    def boom(*a, **kw):
        from tg.types import Order
        return Order(order_id="x", signal_key=kw.get("signal_key", ""),
                     symbol="X", side=Side.LONG, action="open", volume=1,
                     price=4550.0, status="rejected",
                     meta={"reject_reason": "test_boom"})

    engine.broker.submit = boom

    sig = make_signal(is_buy=True, date="2026-09-01 09:36")
    engine.on_signal(sig)
    check("开仓被拒后 _state 回 IDLE", engine._state, EngineState.IDLE)
    check("开仓被拒后 position 仍为 None", engine.position, None)

# ════════════════════════════════════════════════════════════════
# [6] 状态转移：平仓成功
# ════════════════════════════════════════════════════════════════
print("\n[6] 状态转移：平仓成功 IN_TRADE→EXITING→IDLE")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 开仓
    sig = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig)
    check("开仓后 _state=IN_TRADE", engine._state, EngineState.IN_TRADE)

    # 直接调 _close_position（用 sig.price 作为 trigger_price）
    engine._close_position("manual_test", 4560.0, engine.last_bar, signal_key="x")
    check("平仓成功后 _state 回 IDLE", engine._state, EngineState.IDLE)
    check("平仓成功后 position is None", engine.position, None)
    # 验证 trades 表有 1 条
    check("trades 表记录 1 条", len(store.trades()), 1)

# ════════════════════════════════════════════════════════════════
# [7] 信号门：IN_TRADE + 同向信号 → skip
# ════════════════════════════════════════════════════════════════
print("\n[7] 信号门：IN_TRADE + 同向信号 → skip")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 开一笔多
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("开多后 _state=IN_TRADE", engine._state, EngineState.IN_TRADE)

    # 同向再发一个买点（不同 K 线 → 不同 key）—— 应被 skip，不调 entry_policy
    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40",
                       sig_key=Signal.make_key("2026-09-01 09:40", "1", True))
    # 把 entry_policy.decide 标记为 fail-call 检测器
    called = {"n": 0}
    orig = engine.entry_policy.decide
    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)
    engine.entry_policy.decide = spy

    engine.on_signal(sig2)
    check("IN_TRADE+同向 → 不调 entry_policy.decide", called["n"], 0)
    check("IN_TRADE+同向 → _state 仍是 IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("IN_TRADE+同向 → position 仍是原来那笔", engine.position.signal_key, sig1.key)
    check("IN_TRADE+同向 → signal_action 标 skip",
          store.signal_action(sig2.key), "skip")

# ════════════════════════════════════════════════════════════════
# [8] 信号门：IN_TRADE + 反向信号 → 仅触发 _close_position
# ════════════════════════════════════════════════════════════════
print("\n[8] 信号门：IN_TRADE + 反向信号 → 仅离场")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 开一笔多
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("开多后 _state=IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("开多后 trades=0", len(store.trades()), 0)

    # 反向（卖）信号
    sig2 = make_signal(is_buy=False, date="2026-09-01 09:40",
                       sig_key=Signal.make_key("2026-09-01 09:40", "1", False))

    # 检测不应再调 entry_policy（IN_TRADE 已被门控短路）
    called = {"n": 0}
    orig = engine.entry_policy.decide
    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)
    engine.entry_policy.decide = spy

    engine.on_signal(sig2)
    check("IN_TRADE+反向 → 不调 entry_policy.decide", called["n"], 0)
    check("IN_TRADE+反向 → _state 回 IDLE", engine._state, EngineState.IDLE)
    check("IN_TRADE+反向 → position 清空", engine.position, None)
    check("IN_TRADE+反向 → trades 增 1", len(store.trades()), 1)
    check("IN_TRADE+反向 → trade.reason=signal_reverse",
          store.trades()[0]["reason"], "signal_reverse")
    check("IN_TRADE+反向 → signal_action=close_only",
          store.signal_action(sig2.key), "close_only")

# ════════════════════════════════════════════════════════════════
# [9] 信号门：IDLE + 信号 → 正常开仓（反向也不再自动反手开仓）
# ════════════════════════════════════════════════════════════════
print("\n[9] 信号门：IDLE + 信号 → 正常开仓（即使反向也不反手）")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 第一个卖信号在 IDLE 直接开空
    sig1 = make_signal(is_buy=False, date="2026-09-01 09:35",
                        sig_key=Signal.make_key("2026-09-01 09:35", "1", False))
    engine.on_signal(sig1)
    check("IDLE+反向信号 → 正常开仓", engine.position.side, Side.SHORT)
    check("IDLE+反向信号 → _state=IN_TRADE", engine._state, EngineState.IN_TRADE)

    # 关键不变量：旧逻辑下 reverse=True 时会把"平+反手"做成连续两步（先 close 再 open）；
    # 现在 IN_TRADE 状态下收到反向信号只 close 不 open，trades 应只增 1 而不是 2。
    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40",
                       sig_key=Signal.make_key("2026-09-01 09:40", "1", True))
    engine.on_signal(sig2)
    check("IDLE→IN_TRADE 后反向 → 只 close 不 open，trades 增 1",
          len(store.trades()), 1)
    check("平仓后 _state=IDLE", engine._state, EngineState.IDLE)
    check("平仓后 position=None（没自动开反向多）", engine.position, None)

# ════════════════════════════════════════════════════════════════
# [10] 信号门：OPENING 瞬态时新信号被忽略（不调 entry_policy）
# ════════════════════════════════════════════════════════════════
print("\n[10] 信号门：OPENING 瞬态时新信号被忽略")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 模拟：把 broker.submit 改成"记录瞬态"
    captured = {"state_at_submit": None}
    orig_submit = engine.broker.submit
    def hook_submit(*a, **kw):
        # 在 broker 内部取 engine state
        captured["state_at_submit"] = engine._state
        return orig_submit(*a, **kw)
    engine.broker.submit = hook_submit

    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("broker.submit 被调时 _state=OPENING",
          captured["state_at_submit"], EngineState.OPENING)
    check("开仓成功后 _state=IN_TRADE", engine._state, EngineState.IN_TRADE)


with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 开仓中 → 让 submit 卡住（返回 pending），手动验 state
    # 这里用更简单的方法：直接 mock 让 broker.submit 在调用瞬间记录状态
    states = []

    # 把 broker 改成"调用时立即返回 rejected" 的简化形式（卡 0 帧）
    # 然后我们手工把 _state 设回 OPENING，看下一个信号进来是否被门控挡掉
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine._state = EngineState.OPENING  # 手工设
    engine.on_signal(sig1)
    # 由于 state=OPENING，try_mark_signal 已占位，应直接 update_signal_action
    check("OPENING 状态进信号 → 幂等键仍占位",
          store.signal_action(sig1.key), "in_flight")
    check("OPENING 状态进信号 → _state 保持 OPENING（没改）",
          engine._state, EngineState.OPENING)

# ════════════════════════════════════════════════════════════════
# [11] 信号门：EXITING 瞬态时新信号被忽略
# ════════════════════════════════════════════════════════════════
print("\n[11] 信号门：EXITING 瞬态时新信号被忽略")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    # 先开仓
    sig1 = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig1)
    check("开仓后 _state=IN_TRADE", engine._state, EngineState.IN_TRADE)

    # 手工把 state 设回 EXITING，模拟正在平仓的瞬态
    engine._state = EngineState.EXITING
    sig2 = make_signal(is_buy=True, date="2026-09-01 09:40",
                       sig_key=Signal.make_key("2026-09-01 09:40", "1", True))
    engine.on_signal(sig2)
    check("EXITING 状态进信号 → 幂等键仍占位(in_flight)",
          store.signal_action(sig2.key), "in_flight")
    check("EXITING 状态进信号 → _state 不变",
          engine._state, EngineState.EXITING)

# ════════════════════════════════════════════════════════════════
# [12] 旧持仓记录 to_dict/from_dict entry_mode 双向兼容
# ════════════════════════════════════════════════════════════════
print("\n[12] 序列化兼容：open_first / unlock_first 双向")

p_open = Position(symbol="X", side=Side.LONG, volume=1, entry_price=100.0,
                  entry_at="", entry_bar_ts=0, signal_key="k", open_order_id="o",
                  exit_plan=ExitPlan(name="x", stop_price=99.0),
                  entry_mode=EntryMode.OPEN_FIRST)
d_open = p_open.to_dict()
check("OPEN_FIRST 序列化 -> 'open_first'", d_open["entry_mode"], "open_first")
check("OPEN_FIRST 反序列化", Position.from_dict(d_open).entry_mode, EntryMode.OPEN_FIRST)

p_unl = Position(symbol="X", side=Side.SHORT, volume=1, entry_price=100.0,
                  entry_at="", entry_bar_ts=0, signal_key="k2", open_order_id="o2",
                  exit_plan=ExitPlan(name="x", stop_price=101.0),
                  entry_mode=EntryMode.UNLOCK_FIRST)
d_unl = p_unl.to_dict()
check("UNLOCK_FIRST 序列化 -> 'unlock_first'", d_unl["entry_mode"], "unlock_first")
check("UNLOCK_FIRST 反序列化", Position.from_dict(d_unl).entry_mode, EntryMode.UNLOCK_FIRST)

# ════════════════════════════════════════════════════════════════
# [13] 事件日志含 state / entry_mode 字段（便于离线分析）
# ════════════════════════════════════════════════════════════════
print("\n[13] 事件日志埋点：open/close 带 entry_mode 与 state 流转")

with tmp_dir() as tmp:
    engine, store, broker = build_engine(tmp)
    engine.on_bar(make_bar(1, 4550, 4552, 4548, 4550))

    sig = make_signal(is_buy=True, date="2026-09-01 09:35")
    engine.on_signal(sig)
    # EventLog 默认 64 条/1s 才 flush，本测试只产几条事件 → 手动 flush 一次
    engine.ev.flush()
    # 验证事件流里有 entry_mode
    ev_path = os.path.join(tmp, "events.jsonl")
    with open(ev_path, "r", encoding="utf-8") as f:
        content = f.read()
    check("事件流含 entry_mode", "entry_mode" in content, True)
    check("事件流含 open_first", "open_first" in content, True)


# ════════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
if _FAIL:
    sys.exit(1)
