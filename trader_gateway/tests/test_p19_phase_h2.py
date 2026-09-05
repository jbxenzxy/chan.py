# -*- coding: utf-8 -*-
"""
P19 Phase H2：批次解锁入场（解锁优先）单元测试
===============================================
背景（用户拍板设计）
    仓位管理模式（N≥2）下入场优先用解锁：前日锁 3 单、今日算出 N=4
    → 先解锁 3 单再新开 1 单。解锁 N 单看成一个整体批次：
      · 串行提交（先解锁后新开）
      · 批次截止 = 最后一张提交 + 5s（墙钟）
      · 到期逐一复核：只撤卡单；撤单后 trade_records 复核防竞速
      · k=0 整批放弃回 IDLE / 0<k<N 用 N-k 新开补齐 / 纯解锁
    batch_count==1（默认配置）⇒ 走原 E2 单笔路径，零行为变化。

硬性要求（本测试锁死）
    [1] N=1 兼容：单笔路径（sub_key 无后缀 + F1 in-flight + 无批次事件）
    [2] 纯批次解锁：opp=2,N=2 → k=2 → 簿清空回 IDLE
    [3] 混合解锁+新开：opp=3,N=4 → 解锁 3 + 新开 1 → IN_TRADE
    [4] opp>N：opp=3,N=2 → 只解锁 2，剩余 1 笔锁仓留簿
    [5] k=0 整批放弃：卡单全部恢复回簿、零新开、回 IDLE（含窗口未到不复核）
    [6] 0<k<N 补齐：k=1 → 新开 N-k=3（planned 2 + 补齐 1）
    [7] 持久化重启：批次 in-flight 恢复 + 续跑复核（state=EXITING）
    [8] per_batch=0：解锁照走、新开跳过（sizing_zero_volume）
    [9] sizing 异常：退回 E2 单笔解锁（解锁不依赖 sizing 健康度）
    [10] 簿容量 headroom 截断：留簿锁仓占位，新开被截断并写 max_open_cap

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python tests/test_p19_phase_h2.py
"""
from __future__ import annotations

import copy
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
    d = tempfile.mkdtemp(prefix="tg_p19_")
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
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
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


class BatchStuckBroker(DryRunBroker):
    """submit 照常同步撮合；trade_confirmed 对指定 sub_key 恒 False（模拟卡单）；
    cancel_pending 记录调用并返回 0（撤单也没救回来 = 真卡单）。"""

    def __init__(self, spec, params, stuck_keys=()):
        super().__init__(spec, params)
        self.stuck_keys = set(stuck_keys)
        self.cancel_calls = []

    def trade_confirmed(self, intent, signal_key=""):
        if intent == OrderIntent.UNLOCK and signal_key in self.stuck_keys:
            return False
        return True

    def cancel_pending(self, signal_key=""):
        self.cancel_calls.append(signal_key)
        return 0


class BrokenSizer:
    """没有 size_batch 的坏 sizer（模拟 sizing 通道异常）。"""

    def size(self, **kw):
        return 0, "broken"

    @property
    def max_volume(self):
        return 1

    def describe(self):
        return {"name": "broken"}


def make_cfg(max_pos=1, batch_open=1, sizing_overrides=None):
    d = copy.deepcopy(DEFAULT_CONFIG)
    d["risk"]["max_open_positions"] = max_pos
    if sizing_overrides:
        d["sizing"].update(sizing_overrides)
    d["sizing"]["batch_open"] = batch_open
    return GatewayConfig.from_dict(d)


def make_signal(is_buy, price=4500.0, high=4552.0, low=4548.0,
                date="2026-09-03 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    return Signal(key=sig_key, symbol="CFFEX.IF", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-03 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(symbol="CFFEX.IF", side=Side.LONG, vol=1, entry_price=4500.0,
             entry_mode=EntryMode.LOCKED, signal_key="P19-LOCK",
             entry_bar_seq=0):
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-02 09:00",
        entry_bar_ts=4000, signal_key=signal_key,
        open_order_id="p19-legacy-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq,
        entry_mode=entry_mode)


def build_engine(tmpdir, exit_policy=None, broker=None, cfg=None,
                 store=None, ev=None):
    cfg = cfg or GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = broker or DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy
    entry = DefaultEntryPolicy({})
    exitp = exit_policy or DefaultExitPolicy({})
    store = store or Store(os.path.join(tmpdir, "state.db"))
    ev = ev or EventLog(os.path.join(tmpdir, "events.jsonl"),
                        echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def event_kinds(ev_path):
    kinds = []
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


def unlock_orders(broker):
    return [o for o in broker.orders if o.meta.get("intent") == "unlock"]


def open_orders(broker):
    return [o for o in broker.orders if o.meta.get("intent") == "open"]


# ════════════════════════════════════════════════════════════════
# [1] N=1 兼容：走原 E2 单笔路径，零行为变化
# ════════════════════════════════════════════════════════════════
print("\n[1] N=1 兼容：单笔路径（无 #u 后缀 + F1 in-flight + 无批次事件）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.positions.add(make_pos(signal_key="P19-1L", entry_bar_seq=1))
    sig = make_signal(is_buy=False, sig_key="P19-1|2|S")

    engine.on_signal(sig)
    uos = unlock_orders(broker)
    check("[1a] 只报 1 笔 UNLOCK", len(uos), 1)
    check("[1b] sub_key 无 #u 后缀（E2 兼容）",
          uos[0].signal_key if uos else None, sig.key)
    check("[1c] F1 单笔 in-flight 已设", engine._unlock_in_flight is not None, True)
    check("[1d] 批次 in-flight 未设", engine._unlock_batch_in_flight, None)
    check("[1e] 簿清空", engine.positions.is_empty(), True)
    check("[1f] state=IDLE", engine._state.name, "IDLE")
    check("[1g] signal_action=unlock", store.signal_action(sig.key), "unlock")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[1h] 无批次事件", any(k.startswith("unlock_batch") for k in kinds), False)
    check("[1i] unlock 事件已写", "unlock" in kinds, True)


# ════════════════════════════════════════════════════════════════
# [2] 纯批次解锁：opp=2,N=2 → k=2 → 簿清空回 IDLE
# ════════════════════════════════════════════════════════════════
print("\n[2] 纯批次解锁 opp=2,N=2（dry_run 即时确认，无批次窗口）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(max_pos=2, batch_open=2))
    engine.positions.add(make_pos(signal_key="P19-2A", entry_bar_seq=1))
    engine.positions.add(make_pos(signal_key="P19-2B", entry_bar_seq=2))
    sig = make_signal(is_buy=False, sig_key="P19-2|1|S")

    engine.on_signal(sig)
    uos = unlock_orders(broker)
    check("[2a] 串行报 2 笔 UNLOCK", len(uos), 2)
    check("[2b] sub_key = #u0/#u1",
          [o.signal_key for o in uos],
          [sig.key + "#u0", sig.key + "#u1"])
    check("[2c] 簿清空", engine.positions.is_empty(), True)
    check("[2d] state=IDLE", engine._state.name, "IDLE")
    check("[2e] 批次 in-flight 未设（全部即时确认）",
          engine._unlock_batch_in_flight, None)
    check("[2f] 2 笔 unlock Trade 落盘",
          sum(1 for t in store.trades() if t["reason"] == "unlock_against_signal"), 2)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[2g] unlock_batch 起始事件", "unlock_batch" in kinds, True)
    check("[2h] unlock_batch_result action=pure_unlock",
          [k for k in kinds if k == "unlock_batch_result"], ["unlock_batch_result"])
    check("[2i] 无批次挂起事件", "unlock_batch_pending" in kinds, False)
    check("[2j] 无新开报单", len(open_orders(broker)), 0)


# ════════════════════════════════════════════════════════════════
# [3] 混合解锁+新开：opp=3,N=4 → 解锁 3 + 新开 1
# ════════════════════════════════════════════════════════════════
print("\n[3] 解锁优先：前日锁 3 单、N=4 → 先解锁 3 单再新开 1 单")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(max_pos=4, batch_open=4))
    for i, k in enumerate(["P19-3A", "P19-3B", "P19-3C"], start=1):
        engine.positions.add(make_pos(signal_key=k, entry_bar_seq=i))
    sig = make_signal(is_buy=False, sig_key="P19-3|1|S")

    engine.on_signal(sig)
    check("[3a] 解锁 3 笔", len(unlock_orders(broker)), 3)
    check("[3b] 新开 1 笔", len(open_orders(broker)), 1)
    check("[3c] 簿内 1 笔（新开今仓）", len(engine.positions), 1)
    pos = engine.positions.positions[0]
    check("[3d] 新仓 side=SHORT（信号方向）", pos.side, Side.SHORT)
    check("[3e] 新仓 entry_mode=OPEN_FIRST", pos.entry_mode, EntryMode.OPEN_FIRST)
    check("[3f] state=IN_TRADE", engine._state.name, "IN_TRADE")
    check("[3g] 3 笔解锁 Trade 落盘",
          sum(1 for t in store.trades() if t["reason"] == "unlock_against_signal"), 3)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[3h] unlock_batch_result ×1",
          kinds.count("unlock_batch_result"), 1)
    check("[3i] unlock 事件 ×3", kinds.count("unlock"), 3)
    check("[3j] open 事件 ×1", kinds.count("open"), 1)


# ════════════════════════════════════════════════════════════════
# [4] opp>N：opp=3,N=2 → 只解锁 2，剩余 1 笔锁仓留簿
# ════════════════════════════════════════════════════════════════
print("\n[4] opp>N：unlock_count=min(opp,N)=2，剩余锁仓留簿")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(max_pos=4, batch_open=2))
    for i, k in enumerate(["P19-4A", "P19-4B", "P19-4C"], start=1):
        engine.positions.add(make_pos(signal_key=k, entry_bar_seq=i))
    sig = make_signal(is_buy=False, sig_key="P19-4|1|S")

    engine.on_signal(sig)
    check("[4a] 只解锁 2 笔（N=2）", len(unlock_orders(broker)), 2)
    check("[4b] 簿内剩 1 笔", len(engine.positions), 1)
    check("[4c] 剩余是 FIFO 最晚的锁仓",
          engine.positions.positions[0].signal_key, "P19-4C")
    check("[4d] 剩余 entry_mode=LOCKED",
          engine.positions.positions[0].entry_mode, EntryMode.LOCKED)
    check("[4e] state=IDLE（全 LOCKED）", engine._state.name, "IDLE")
    check("[4f] 零新开", len(open_orders(broker)), 0)


# ════════════════════════════════════════════════════════════════
# [5] k=0 整批放弃：卡单全部恢复回簿、零新开、回 IDLE
# ════════════════════════════════════════════════════════════════
print("\n[5] k=0 整批放弃（卡单 ×2 → 全部恢复 → 回 IDLE）+ 窗口未到不复核")
with tmp_dir() as tmp:
    sig = make_signal(is_buy=False, sig_key="P19-5|1|S")
    stuck_keys = {sig.key + "#u0", sig.key + "#u1"}
    broker = BatchStuckBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              stuck_keys=stuck_keys)
    engine, store, broker, ev = build_engine(
        tmp, broker=broker, cfg=make_cfg(max_pos=2, batch_open=2))
    engine.positions.add(make_pos(signal_key="P19-5A", entry_bar_seq=1))
    engine.positions.add(make_pos(signal_key="P19-5B", entry_bar_seq=2))

    engine.on_signal(sig)
    check("[5a] 乐观落账：簿暂空", engine.positions.is_empty(), True)
    check("[5b] state=EXITING（复核中）", engine._state.name, "EXITING")
    check("[5c] 批次 in-flight 已设", engine._unlock_batch_in_flight is not None, True)
    check("[5d] 批次 in-flight 已持久化",
          isinstance(store.get_json("_unlock_batch_in_flight"), dict), True)

    # 窗口未到：不复核
    engine.on_bar(make_bar(5000, 4500, 4510, 4490, 4505))
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[5e] 窗口未到不写 result",
          "unlock_batch_result" in kinds, False)
    check("[5f] 窗口未到 state 保持 EXITING", engine._state.name, "EXITING")

    # 回拨 deadline → 复核：撤单 ×2、复核仍 False → 全部恢复 → k=0 放弃
    engine._unlock_batch_in_flight["deadline_ts"] = 0.0
    engine.on_bar(make_bar(5300, 4505, 4515, 4495, 4510))
    check("[5g] 撤单调用 ×2（#u0,#u1）", broker.cancel_calls,
          [sig.key + "#u0", sig.key + "#u1"])
    check("[5h] 卡单目标全部恢复回簿", len(engine.positions), 2)
    check("[5i] 恢复的全是 LOCKED",
          all(p.entry_mode is EntryMode.LOCKED
              for p in engine.positions.positions), True)
    check("[5j] state=IDLE", engine._state.name, "IDLE")
    check("[5k] 零新开", len(open_orders(broker)), 0)
    check("[5l] 批次 in-flight 清掉", engine._unlock_batch_in_flight, None)
    check("[5m] signal_action=rejected（abandon）",
          store.signal_action(sig.key), "rejected")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[5n] unlock_batch_pending 已写", "unlock_batch_pending" in kinds, True)
    check("[5o] unlock_batch_result 已写",
          kinds.count("unlock_batch_result"), 1)
    check("[5p] unlock_batch_restored ×2",
          kinds.count("unlock_batch_restored"), 2)


# ════════════════════════════════════════════════════════════════
# [6] 0<k<N 补齐：opp=2,N=4，1 笔卡单 → k=1 → 新开 N-k=3
# ════════════════════════════════════════════════════════════════
print("\n[6] 0<k<N 补齐：planned 2 + 补齐 1 = 新开 3，1 笔锁仓留簿")
with tmp_dir() as tmp:
    sig = make_signal(is_buy=False, sig_key="P19-6|1|S")
    broker = BatchStuckBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              stuck_keys={sig.key + "#u0"})
    engine, store, broker, ev = build_engine(
        tmp, broker=broker, cfg=make_cfg(max_pos=4, batch_open=4))
    engine.positions.add(make_pos(signal_key="P19-6A", entry_bar_seq=1))
    engine.positions.add(make_pos(signal_key="P19-6B", entry_bar_seq=2))

    engine.on_signal(sig)
    check("[6a] 解锁提交 2 笔", len(unlock_orders(broker)), 2)
    check("[6b] 乐观落账后簿空", engine.positions.is_empty(), True)

    engine._unlock_batch_in_flight["deadline_ts"] = 0.0
    engine.on_bar(make_bar(5000, 4500, 4510, 4490, 4505))
    check("[6c] 卡单目标恢复 1 笔回簿", len(engine.positions), 4)
    modes = sorted(p.entry_mode.value for p in engine.positions.positions)
    check("[6d] 簿内 = 1 LOCKED + 3 OPEN_FIRST", modes,
          ["locked", "open_first", "open_first", "open_first"])
    opens = open_orders(broker)
    check("[6e] 新开 3 笔", len(opens), 3)
    check("[6f] 新开 sub_key = #0/#1/#2",
          sorted(o.signal_key for o in opens),
          sorted([sig.key + "#0", sig.key + "#1", sig.key + "#2"]))
    check("[6g] state=IN_TRADE", engine._state.name, "IN_TRADE")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[6h] unlock_batch_result k=1/restored=1/new_open=3",
          kinds.count("unlock_batch_result"), 1)
    check("[6i] confirmed_after_cancel 未触发（撤单没救回）",
          "unlock_batch_confirmed_after_cancel" in kinds, False)


# ════════════════════════════════════════════════════════════════
# [7] 持久化重启：批次 in-flight 恢复 + 续跑复核
# ════════════════════════════════════════════════════════════════
print("\n[7] 引擎重启后批次复核上下文恢复（state=EXITING）并续跑")
with tmp_dir() as tmp:
    sig = make_signal(is_buy=False, sig_key="P19-7|1|S")
    stuck_keys = {sig.key + "#u0", sig.key + "#u1"}
    broker1 = BatchStuckBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                               stuck_keys=stuck_keys)
    engine1, store1, _, ev1 = build_engine(
        tmp, broker=broker1, cfg=make_cfg(max_pos=2, batch_open=2))
    engine1.positions.add(make_pos(signal_key="P19-7A", entry_bar_seq=1))
    engine1.positions.add(make_pos(signal_key="P19-7B", entry_bar_seq=2))
    engine1.on_signal(sig)
    check("[7a] 重启前：批次 in-flight 挂起",
          engine1._unlock_batch_in_flight is not None, True)
    ev1.flush()

    # 重启：同目录新 engine（新 store/ev 实例，同一磁盘文件）
    broker2 = BatchStuckBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                               stuck_keys=stuck_keys)
    engine2, store2, _, ev2 = build_engine(
        tmp, broker=broker2, cfg=make_cfg(max_pos=2, batch_open=2))
    check("[7b] 重启后 state=EXITING", engine2._state.name, "EXITING")
    check("[7c] 批次 in-flight 已恢复",
          engine2._unlock_batch_in_flight is not None, True)
    ev2.flush()
    kinds2 = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[7d] 恢复事件已写",
          "unlock_batch_restored_in_flight" in kinds2, True)

    engine2._unlock_batch_in_flight["deadline_ts"] = 0.0
    engine2.on_bar(make_bar(6000, 4500, 4510, 4490, 4505))
    check("[7e] 续跑复核：k=0 放弃 → 簿复原 2 笔", len(engine2.positions), 2)
    check("[7f] state=IDLE", engine2._state.name, "IDLE")
    check("[7g] 撤单 ×2", len(broker2.cancel_calls), 2)


# ════════════════════════════════════════════════════════════════
# [8] per_batch=0：解锁照走、新开补齐跳过
# ════════════════════════════════════════════════════════════════
print("\n[8] sizing 定 0 手（无 equity + fallback_volume=0）：解锁照走、新开跳过")
with tmp_dir() as tmp:
    broker = DryRunBroker(InstrumentSpec(), {})   # 无 sim_equity → equity=None
    engine, store, broker, ev = build_engine(
        tmp, broker=broker,
        cfg=make_cfg(max_pos=2, batch_open=2,
                     sizing_overrides={"enabled": True, "fallback_volume": 0}))
    engine.positions.add(make_pos(signal_key="P19-8A", entry_bar_seq=1))
    sig = make_signal(is_buy=False, sig_key="P19-8|1|S")

    engine.on_signal(sig)
    check("[8a] 解锁 1 笔照常执行", len(unlock_orders(broker)), 1)
    check("[8b] 零新开", len(open_orders(broker)), 0)
    check("[8c] 簿清空", engine.positions.is_empty(), True)
    check("[8d] state=IDLE", engine._state.name, "IDLE")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[8e] sizing_zero_volume 已写", "sizing_zero_volume" in kinds, True)
    check("[8f] result action=pure_unlock",
          kinds.count("unlock_batch_result"), 1)


# ════════════════════════════════════════════════════════════════
# [9] sizing 异常：退回 E2 单笔解锁（不依赖 sizing 健康度）
# ════════════════════════════════════════════════════════════════
print("\n[9] sizer 损坏（无 size_batch）→ 回退单笔解锁")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(max_pos=2, batch_open=2))
    engine.sizer = BrokenSizer()
    engine.positions.add(make_pos(signal_key="P19-9A", entry_bar_seq=1))
    sig = make_signal(is_buy=False, sig_key="P19-9|1|S")

    engine.on_signal(sig)
    uos = unlock_orders(broker)
    check("[9a] 退回单笔：1 笔 UNLOCK", len(uos), 1)
    check("[9b] sub_key 无 #u 后缀", uos[0].signal_key if uos else None, sig.key)
    check("[9c] F1 单笔 in-flight 已设", engine._unlock_in_flight is not None, True)
    check("[9d] 簿清空", engine.positions.is_empty(), True)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[9e] 无批次事件", any(k.startswith("unlock_batch") for k in kinds), False)


# ════════════════════════════════════════════════════════════════
# [10] 簿容量 headroom 截断：留簿锁仓占位 → 新开被截断
# ════════════════════════════════════════════════════════════════
print("\n[10] headroom 截断：max=3, N=4, 卡单留 1 锁 → 新开 3→2")
with tmp_dir() as tmp:
    sig = make_signal(is_buy=False, sig_key="P19-10|1|S")
    broker = BatchStuckBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              stuck_keys={sig.key + "#u0"})
    engine, store, broker, ev = build_engine(
        tmp, broker=broker, cfg=make_cfg(max_pos=3, batch_open=4))
    engine.positions.add(make_pos(signal_key="P19-10A", entry_bar_seq=1))
    engine.positions.add(make_pos(signal_key="P19-10B", entry_bar_seq=2))

    engine.on_signal(sig)
    check("[10a] 解锁提交 2 笔（unlock_count=min(2,4)）",
          len(unlock_orders(broker)), 2)

    engine._unlock_batch_in_flight["deadline_ts"] = 0.0
    engine.on_bar(make_bar(5000, 4500, 4510, 4490, 4505))
    check("[10b] 卡单恢复 1 笔留簿", len(engine.positions), 3)
    modes = sorted(p.entry_mode.value for p in engine.positions.positions)
    check("[10c] 簿内 = 1 LOCKED + 2 OPEN_FIRST", modes,
          ["locked", "open_first", "open_first"])
    check("[10d] 新开 2 笔（3 被 headroom 截到 2）",
          len(open_orders(broker)), 2)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[10e] max_open_cap 已写", "max_open_cap" in kinds, True)


print("\n" + "=" * 60)
print("P19 Phase H2 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
