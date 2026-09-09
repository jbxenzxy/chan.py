# -*- coding: utf-8 -*-
"""
P18 Phase H1：LOCK 反向仓落簿（lock_booked）单元测试
=====================================================
背景（评审议题 S1 修复）
    此前 LOCK 软离场成交后，broker 端真实存在的反向锁仓仓**不落簿**
    （_close_positions 直接 remove）→ 次日簿空 → has_opposite 恒 False
    → E2 的 UNLOCK（解锁入场）在纯自动流程中不可达。

Phase H1 改动
    ① types.EntryMode 新增 LOCKED —— LOCK 成交后反向仓作为 Position 落簿
    ② _close_positions：intent==LOCK 成交 → add 反向 Position + lock_booked 事件
    ③ _settle_positions：跳过 LOCKED（锁仓不走 TP/SL/EOD，唯一离场 = 次日 UNLOCK）
    ④ _close_positions 收尾 + _restore 状态推导：簿内只剩 LOCKED → IDLE
    ⑤ _exit_intent：LOCKED → (UNLOCK, pos.side)（防御分支，正常不可达）

硬性要求（本测试锁死）
    [1] E2E：开多 → 反向信号 → SOFT_EXIT 成交 → 反向仓落簿（LOCKED/SHORT/#lock 键）
    [2] settle 跳过：任意出场策略都不会把 LOCKED 仓平掉（对照：OPEN_FIRST 会被平）
    [3] 次日对向信号 → E2 UNLOCK 路径解锁（CloseYesterday + unlock_against_signal）
    [4] 持久化重启：LOCKED 仓恢复 + state=IDLE + 解锁仍可达
    [5] 同向信号：IDLE + 簿内锁仓 → skip idle_with_same_side，簿不变
    [6] 批量 LOCK：2 笔多单 FIFO 全 LOCK → 2 笔锁仓落簿 + state=IDLE + 多反向 warning
    [7] _exit_intent(LOCKED) → (UNLOCK, pos.side)
    [8] 对账接管：外部手工平掉锁仓 → 下一根 bar reconcile 清簿

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python tests/test_p18_phase_h1.py
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
    d = tempfile.mkdtemp(prefix="tg_p18_")
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
from Trading.Strategy.Base import ExitCheck, ExitPolicy  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    Bar, EntryMode, EngineState, ExitPlan, OrderIntent, Position, Side, Signal,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


class AlwaysExitPolicy(ExitPolicy):
    """测试 [2] 专用：对任何仓位任何 bar 都返回出场触发。
    用于证明 _settle_positions 的 LOCKED 跳过是模式级而非策略级。"""
    name = "AlwaysExitPolicy"

    def plan(self, sig, entry_price, spec):
        stop = entry_price - 5.0 if sig.is_buy else entry_price + 5.0
        return ExitPlan(name="always", stop_price=stop)

    def check(self, position, bar, spec, bars_held=0):
        return ExitCheck("always", bar.close)


class RealPosBroker(DryRunBroker):
    """测试 [8] 专用：dry_run + 可编程 real_position（模拟券商真实持仓）。"""

    def __init__(self, spec, params):
        super().__init__(spec, params)
        self._real = {Side.LONG: 0, Side.SHORT: 0}

    def real_position(self, side):
        return self._real.get(side, 0)


def make_signal(is_buy, price=4500.0, high=4552.0, low=4548.0,
                date="2026-09-02 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    return Signal(key=sig_key, symbol="CFFEX.IF2609", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-02 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(symbol="CFFEX.IF2609", side=Side.LONG, vol=1, entry_price=4500.0,
             entry_mode=EntryMode.OPEN_FIRST, signal_key="P18-LEGACY",
             entry_bar_seq=0):
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=4000, signal_key=signal_key,
        open_order_id="p18-legacy-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq,
        entry_mode=entry_mode)


def build_engine(tmpdir, exit_policy=None, broker=None, cfg=None,
                 store=None, ev=None):
    cfg = cfg or TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = broker or DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = exit_policy or LayeredExitPolicy()
    store = store or Store(os.path.join(tmpdir, "state.db"))
    ev = ev or EventLog(os.path.join(tmpdir, "events.jsonl"),
                        echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def event_kinds(ev_path):
    kinds = []
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


def n_orders(broker):
    return len(broker.orders)


# ════════════════════════════════════════════════════════════════
# [1] E2E：开多 → 反向信号 → SOFT_EXIT 成交 → 反向仓落簿
# ════════════════════════════════════════════════════════════════
print("\n[1] E2E：BUY 开仓 → LOCK → 留双仓（原仓+反向 LOCKED，不兑现 PnL）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_signal(make_signal(is_buy=True, price=4500.0))
    check("[1a] 开多成功", len(engine.positions), 1)
    check("[1b] state=IN_TRADE", engine._state.name, "IN_TRADE")
    open_price = engine.positions.positions[0].entry_price  # P₀ 会计锚
    n_before = n_orders(broker)

    # v1.3（Q1=B）：反向信号在运行态被忽略；LOCK 软离场改由出场层触发
    # （_settle_positions → _exit_intent(OPEN_FIRST)=LOCK）。此处直接调 _close_positions
    # 模拟出场层触发，验证留双仓落簿链路。
    engine._close_positions([engine.positions.positions[0]], "lock_test", 4550.0, None)
    check("[1c] LOCK 后留双仓（原仓 + 反向）", len(engine.positions), 2)
    legs = engine.positions.positions
    orig = next(p for p in legs if p.side is Side.LONG)
    lock = next(p for p in legs if p.side is Side.SHORT)
    check("[1d] 原仓 side=LONG 保留", orig.side, Side.LONG)
    check("[1e] 原仓 entry_mode=LOCKED", orig.entry_mode, EntryMode.LOCKED)
    check("[1f] 原仓 entry_price=P₀ 不变（会计锚）", orig.entry_price, open_price)
    lock_order = [o for o in broker.orders
                  if o.meta.get("intent") == "lock"][-1]
    check("[1g] 反向仓 entry_price=LOCK 报单成交价",
          lock.entry_price, lock_order.filled_price)
    check("[1h] 反向仓 signal_key 以 #lock 结尾", lock.signal_key.endswith("#lock"), True)
    check("[1i] 反向仓 entry_mode=LOCKED", lock.entry_mode, EntryMode.LOCKED)
    check("[1j] 两笔 volume 与原仓一致（2 手）", orig.volume == lock.volume == 2, True)
    check("[1k] 两笔共享 lock_pair_id 且非空",
          bool(orig.lock_pair_id) and orig.lock_pair_id == lock.lock_pair_id, True)
    check("[1l] state 回 IDLE", engine._state.name, "IDLE")
    check("[1m] broker 多出 1 笔 LOCK 报", n_orders(broker) - n_before, 1)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[1n] 事件流含 lock_booked", "lock_booked" in kinds, True)
    check("[1o] 原仓 PnL 不兑现（trade 不落盘）", len(store.trades()), 0)
    check("[1p] 双仓已持久化（2 条 locked）",
          [p["entry_mode"] for p in (store.get_json("positions") or [])],
          ["locked", "locked"])


# ════════════════════════════════════════════════════════════════
# [2] settle 跳过 LOCKED：再激进的出场策略也不能平锁仓
# ════════════════════════════════════════════════════════════════
print("\n[2] settle 跳过 LOCKED（对照：OPEN_FIRST 会被 AlwaysExit 平掉）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, exit_policy=AlwaysExitPolicy({}))
    # 对照组：OPEN_FIRST 仓位在下一根 bar 被 AlwaysExit 平掉（LOCK 落簿）
    engine.positions.add(make_pos(signal_key="P18-CTL", entry_price=4500.0))
    engine._state = EngineState.IN_TRADE
    engine.on_bar(make_bar(5000, 4500, 4510, 4490, 4505))
    check("[2a] 对照组 OPEN_FIRST 被 settle 平掉（留双仓全 LOCKED）",
          [p.entry_mode for p in engine.positions.positions],
          [EntryMode.LOCKED, EntryMode.LOCKED])
    n_mid = n_orders(broker)

    # 实验组：簿内只剩 LOCKED（双仓），任何 bar 都不应触发出场
    engine.on_bar(make_bar(5300, 4505, 4590, 4500, 4585))   # 对 SHORT 极不利
    engine.on_bar(make_bar(5600, 4585, 4595, 4580, 4590,
                           date="2026-09-02 15:00"))         # 收盘根（EOD）
    check("[2b] 锁仓双仓仍在簿", len(engine.positions), 2)
    check("[2c] 锁仓 entry_mode 仍=LOCKED",
          all(p.entry_mode is EntryMode.LOCKED for p in engine.positions.positions), True)
    check("[2d] bar 期间零报单（settle 被跳过）", n_orders(broker), n_mid)
    check("[2e] state 保持 IDLE", engine._state.name, "IDLE")


# ════════════════════════════════════════════════════════════════
# [3] 次日对向信号 → E2 UNLOCK 路径解锁
# ════════════════════════════════════════════════════════════════
print("\n[3] LOCK 落簿后，对向信号触发 UNLOCK（平反向仓 + 升级原仓）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_signal(make_signal(is_buy=True, price=4500.0))
    # v1.3（Q1=B）：反向信号在运行态被忽略；LOCK 软离场改由出场层触发
    # （_settle_positions → _exit_intent(OPEN_FIRST)=LOCK）。此处直接调 _close_positions
    # 模拟出场层触发，验证留双仓落簿链路。
    engine._close_positions([engine.positions.positions[0]], "lock_test", 4550.0, None)
    check("[3a] 锁仓双仓已落簿", len(engine.positions), 2)
    n_before = n_orders(broker)

    engine.on_signal(make_signal(is_buy=True, price=4520.0,
                                 date="2026-09-03 09:35", bsp_type="1"))
    check("[3b] 解锁后剩 1 笔（升级的原仓，非清空）", len(engine.positions), 1)
    check("[3c] 剩余持仓 entry_mode=UNLOCK_FIRST",
          engine.positions.positions[0].entry_mode, EntryMode.UNLOCK_FIRST)
    check("[3d] 剩余持仓 side=LONG（原仓方向）",
          engine.positions.positions[0].side, Side.LONG)
    check("[3e] broker 收到 UNLOCK 报",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), True)
    check("[3f] UNLOCK 是本轮唯一新报单", n_orders(broker) - n_before, 1)
    trades = store.trades()
    check("[3g] 解锁 Trade 落盘 reason=unlock_against_signal",
          [t["reason"] for t in trades][-1], "unlock_against_signal")
    check("[3h] state 回 IN_TRADE（剩单边敞口）", engine._state.name, "IN_TRADE")


# ════════════════════════════════════════════════════════════════
# [4] 持久化重启：LOCKED 恢复 + state=IDLE + 解锁可达
# ════════════════════════════════════════════════════════════════
print("\n[4] 持久化重启后 LOCKED 恢复且解锁路径可达")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_signal(make_signal(is_buy=True, price=4500.0))
    # v1.3（Q1=B）：反向信号在运行态被忽略；LOCK 软离场改由出场层触发
    # （_settle_positions → _exit_intent(OPEN_FIRST)=LOCK）。此处直接调 _close_positions
    # 模拟出场层触发，验证 lock_booked 落簿链路。
    engine._close_positions([engine.positions.positions[0]], "lock_test", 4550.0, None)

    # 重启：同目录新 engine（新 store/ev 实例，同一磁盘文件；构造函数内 _restore）
    ev.flush()
    ev2 = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    store2 = Store(os.path.join(tmp, "state.db"))
    engine2, _, broker2, _ = build_engine(tmp, store=store2, ev=ev2)
    # 直接断言恢复结果：
    check("[4a] 重启后簿内 2 笔", len(engine2.positions), 2)
    check("[4b] 双仓 entry_mode=LOCKED",
          all(p.entry_mode is EntryMode.LOCKED for p in engine2.positions.positions), True)
    check("[4c] 重启后 state=IDLE（不再推 IN_TRADE）",
          engine2._state.name, "IDLE")
    ids = {p.lock_pair_id for p in engine2.positions.positions}
    check("[4c2] lock_pair_id 配对关系恢复（共享非空 id）",
          len(ids) == 1 and ids != {""}, True)

    engine2.on_signal(make_signal(is_buy=True, price=4520.0,
                                  date="2026-09-03 09:35", bsp_type="1"))
    check("[4d] 重启后对向信号走 UNLOCK 解锁",
          any(o.meta.get("intent") == "unlock" for o in broker2.orders), True)
    check("[4e] 解锁后剩 1 笔 UNLOCK_FIRST（非空）",
          len(engine2.positions) == 1
          and engine2.positions.positions[0].entry_mode is EntryMode.UNLOCK_FIRST, True)


# ════════════════════════════════════════════════════════════════
# [5] 同向信号：IDLE + 簿内锁仓 → skip，簿不变
# ════════════════════════════════════════════════════════════════
print("\n[5] 同向信号（锁仓持仓同向）→ 开新仓，不动锁仓持仓")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_signal(make_signal(is_buy=True, price=4500.0))
    # v1.3（Q1=B）：反向信号在运行态被忽略；LOCK 软离场改由出场层触发
    # （_settle_positions → _exit_intent(OPEN_FIRST)=LOCK）。此处直接调 _close_positions
    # 模拟出场层触发，验证 lock_booked 落簿链路。
    engine._close_positions([engine.positions.positions[0]], "lock_test", 4550.0, None)
    n_before = n_orders(broker)
    # SELL 信号与锁仓持仓（SHORT）同向 → v1.3：开新仓，不提前平锁仓持仓（规则 ⑸-① 同向版）
    engine.on_signal(make_signal(is_buy=False, price=4540.0,
                                 date="2026-09-02 10:05", bsp_type="2"))
    check("[5a] 锁仓双仓仍在（2 笔 LOCKED）",
          sum(1 for p in engine.positions.positions
              if p.entry_mode is EntryMode.LOCKED), 2)
    check("[5b] 新开一笔 SHORT（簿内共 3 笔）", len(engine.positions), 3)
    check("[5c] broker 收到 1 笔新 OPEN 报", n_orders(broker) - n_before, 1)
    check("[5d] signal_action=opened（同向开新仓）",
          store.signal_action("2026-09-02 10:05|2|S"), "opened")


# ════════════════════════════════════════════════════════════════
# [6] 批量 LOCK：2 笔多单 FIFO 全 LOCK → 2 笔锁仓落簿 + 多反向 warning
# ════════════════════════════════════════════════════════════════
print("\n[6] 批量 LOCK 落簿（留双仓）+ 次日单笔解锁边界")
with tmp_dir() as tmp:
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = 2
    engine, store, broker, ev = build_engine(tmp, cfg=cfg)
    p1 = make_pos(signal_key="P18-BA", entry_bar_seq=1)
    p2 = make_pos(signal_key="P18-BB", entry_bar_seq=2)
    engine.positions.add(p1)
    engine.positions.add(p2)
    engine._persist()
    engine._state = EngineState.IN_TRADE

    engine._close_positions([p1, p2], "lock_test", 4550.0, None,
                            signal_key="P18-BATCH")
    check("[6a] 2 笔多单留双仓（共 4 笔）", len(engine.positions), 4)
    check("[6b] 全部 entry_mode=LOCKED",
          all(p.entry_mode is EntryMode.LOCKED
              for p in engine.positions.positions), True)
    check("[6c] side 分布：2 LONG + 2 SHORT",
          [sum(1 for p in engine.positions.positions if p.side is Side.LONG),
           sum(1 for p in engine.positions.positions if p.side is Side.SHORT)],
          [2, 2])
    check("[6d] 两个 lock_pair_id（2 个锁对）",
          len({p.lock_pair_id for p in engine.positions.positions}), 2)
    check("[6e] state=IDLE（全 LOCKED）", engine._state.name, "IDLE")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[6f] lock_booked ×2", kinds.count("lock_booked"), 2)

    # 次日对向信号：单笔解锁（平 1 反向仓 + 升级 1 原仓）
    engine.on_signal(make_signal(is_buy=True, price=4520.0,
                                 date="2026-09-03 09:35", bsp_type="1"))
    check("[6g] 本轮解锁 1 锁对（剩 3 笔）", len(engine.positions), 3)
    check("[6h] 升级 1 笔为 UNLOCK_FIRST",
          sum(1 for p in engine.positions.positions
              if p.entry_mode is EntryMode.UNLOCK_FIRST), 1)
    check("[6h2] 剩余 2 笔仍 LOCKED",
          sum(1 for p in engine.positions.positions
              if p.entry_mode is EntryMode.LOCKED), 2)
    ev.flush()
    kinds2 = event_kinds(os.path.join(tmp, "events.jsonl"))
    # Phase H2：多反向仓在 N=1 时走 E2 单笔解锁是合法边界（不再是异常告警），
    # 批量解锁由 H2 批次路径（split_positions≥2）承接 —— 旧 unlock_partial_warning 已移除
    check("[6i] 多反向仓不再告警（H2 批次路径承接）",
          "unlock_partial_warning" in kinds2, False)
    check("[6j] 单笔解锁事件已写",
          "unlock" in kinds2, True)
    check("[6k] 解锁升级事件已写",
          "unlock_pair_upgraded" in kinds2, True)


# ════════════════════════════════════════════════════════════════
# [7] _exit_intent(LOCKED) 防御分支
# ════════════════════════════════════════════════════════════════
print("\n[7] _exit_intent：LOCKED → (UNLOCK, pos.side)")
with tmp_dir() as tmp:
    engine, _, _, _ = build_engine(tmp)
    got = engine._exit_intent(make_pos(side=Side.SHORT,
                                       entry_mode=EntryMode.LOCKED))
    check("[7a] intent=UNLOCK", got[0], OrderIntent.UNLOCK)
    check("[7b] side=仓自身方向 SHORT", got[1], Side.SHORT)
    got2 = engine._exit_intent(make_pos(side=Side.LONG,
                                        entry_mode=EntryMode.LOCKED))
    check("[7c] 多头锁仓 → (UNLOCK, LONG)", got2, (OrderIntent.UNLOCK, Side.LONG))


# ════════════════════════════════════════════════════════════════
# [8] 对账接管：外部手工平掉锁仓 → 下一根 bar reconcile 清簿
# ════════════════════════════════════════════════════════════════
print("\n[8] 锁仓被外部（快期3）手工平仓 → reconcile 自动接管")
with tmp_dir() as tmp:
    rb = RealPosBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0})
    engine, store, broker, ev = build_engine(tmp, broker=rb)
    engine.on_signal(make_signal(is_buy=True, price=4500.0))
    # v1.3（Q1=B）：反向信号在运行态被忽略；LOCK 软离场改由出场层触发
    # （_settle_positions → _exit_intent(OPEN_FIRST)=LOCK）。此处直接调 _close_positions
    # 模拟出场层触发，验证 lock_booked 落簿链路。
    engine._close_positions([engine.positions.positions[0]], "lock_test", 4550.0, None)
    check("[8a] 锁仓双仓已落簿", len(engine.positions), 2)

    # bar1：真实持仓与簿一致（LONG=2 + SHORT=2 双边锁仓）→ 对账无动作
    rb._real[Side.LONG] = 2
    rb._real[Side.SHORT] = 2
    engine.on_bar(make_bar(5000, 4550, 4560, 4540, 4555))
    check("[8b] 一致时簿不变", len(engine.positions), 2)

    # bar2：真实持仓被外部清零 → reconcile 清簿
    rb._real[Side.LONG] = 0
    rb._real[Side.SHORT] = 0
    engine.on_bar(make_bar(5300, 4555, 4565, 4550, 4560))
    check("[8c] 外部平仓后簿清空", engine.positions.is_empty(), True)
    check("[8d] state 回 IDLE", engine._state.name, "IDLE")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[8e] 事件流含 position_externally_closed",
          "position_externally_closed" in kinds, True)


# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P18 Phase H1 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
if _FAIL:
    raise SystemExit(1)
