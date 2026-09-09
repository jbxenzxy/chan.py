# -*- coding: utf-8 -*-
"""
P19 解锁入场：一笔 FOK 整笔解锁 + 缺口补开（2026-09-06 全 FOK 重构后）
=====================================================================
背景
    重构后解锁模型归一（旧的 H2 批次轮次机制已整体删除）：
      · 一个反向信号只解一笔：对最老的一笔反向锁仓持仓发**一笔 FOK 解锁报单**，
        手数 = 该笔持仓的全部手数（全成或全撤）
      · 解锁成功后按缺口补开：new_lots = 今日信号想开手数 N − 已解锁手数 V
            N > V → 补开 N−V 手；N ≤ V → 纯解锁不补开
      · unlock_no_new_open=True → 缺口一律不补开（规避平今高手续费）
      · 不存在"轮次/批次"概念：无轮次窗口、无 in-flight 批次持久化、无三分支结算

硬性要求（本测试锁死）
    [1] 单笔解锁：1 笔锁仓 3 手 → 1 笔 UNLOCK 3 手 → 簿清空、signal_action=unlock
    [2] 多笔锁仓：只解最老一笔（entry_bar_seq 最小），其余留簿
    [3] 缺口补开（显式 unlock_no_new_open=False）：锁 3 手 + N=5 → 解锁 3 + 开 2 手 → IN_TRADE
    [4] 默认不补开（unlock_no_new_open 缺省=True）：锁 3 手 + N=5 → 只解锁 3 手、零补开 → IDLE
    [5] N ≤ V：锁 3 手 + N=2 → 纯解锁，零补开
    [6] 解锁拒单 → rejected、回 IDLE、锁仓保留在簿
    [7] 手数由 risk.max_volume 决定（仓位管理通道已整体删除，无 sizing/sizer）：
        解锁后 N ≤ V 且 unlock_no_new_open=False → 纯解锁不补开，
        且不再有 risk_block 事件（风控五道硬闸门已删）
    [8] H2 轮次机制已无残留（无 unlock_round* 事件、无批次 in-flight 字段）

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python tests/test_p19_unlock_lots.py
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
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


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


from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    Bar, EntryMode, EngineState, ExitPlan, OrderIntent, Position, Side, Signal,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


class RejectUnlockBroker(DryRunBroker):
    """UNLOCK 一律拒单（其它 intent 正常）。"""
    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        if intent == OrderIntent.UNLOCK:
            from Trading.Infra.Types import Order
            o = Order(
                order_id="reject-unlock", signal_key=signal_key,
                symbol=self.spec.trade_symbol, side=side, action="unlock",
                volume=int(volume), price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-03 09:40", broker=self.name, note=note,
                meta={"intent": "unlock", "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note)


def make_cfg(max_pos=3, risk_overrides=None):
    d = copy.deepcopy(DEFAULT_CONFIG)
    d["risk"]["max_open_positions"] = max_pos
    d["risk"]["max_volume"] = 20      # 默认每笔 N 手；场景用 risk_overrides 覆盖
    if risk_overrides:
        d["risk"].update(risk_overrides)
    return TradingConfig.from_dict(d)


def make_signal(is_buy, price=4500.0, high=4552.0, low=4548.0,
                date="2026-09-03 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    return Signal(key=sig_key, symbol="CFFEX.IF2609", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts=4000, o=4500.0, h=4500.0, l=4500.0, c=4500.0,
             date="2026-09-03 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(symbol="CFFEX.IF2609", side=Side.LONG, vol=1, entry_price=4500.0,
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


def build_engine(tmpdir, exit_policy=None, broker=None, cfg=None):
    cfg = cfg or make_cfg()
    spec = InstrumentSpec()
    broker = broker or DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    from Trading.Strategy.Entry import DefaultEntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy
    entry = DefaultEntryPolicy({})
    exitp = exit_policy or LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def event_kinds(ev_path):
    kinds = []
    try:
        with open(ev_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    kinds.append(json.loads(line).get("kind", ""))
    except FileNotFoundError:
        pass
    return kinds


def unlock_orders(broker):
    return [o for o in broker.orders if o.meta.get("intent") == "unlock"]


def open_orders(broker):
    return [o for o in broker.orders if o.meta.get("intent") == "open"]


# ════════════════════════════════════════════════════════════════
# [1] 单笔解锁：整笔全量手数一笔报单
# ════════════════════════════════════════════════════════════════
print("\n[1] 单笔解锁：一笔 FOK 挂全部手数")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(risk_overrides={"max_volume": 3}))
    engine.positions.add(make_pos(vol=3, signal_key="P19-1L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-1|2|S")
    engine.on_signal(sig)

    uos = unlock_orders(broker)
    check("[1a] 只报 1 笔 UNLOCK", len(uos), 1)
    check("[1b] 该笔 3 手（整笔全量）", uos[0].volume if uos else None, 3)
    check("[1c] sub_key = 信号 key（无 #u 后缀）",
          uos[0].signal_key if uos else None, sig.key)
    check("[1d] 解锁后簿清空", engine.positions.is_empty(), True)
    check("[1e] signal_action=unlock", store.signal_action(sig.key), "unlock")
    check("[1f] 解锁复核 in-flight 已设", engine._unlock_in_flight is not None, True)
    check("[1g] 无轮次 in-flight 字段", hasattr(engine, "_unlock_round_in_flight"), False)


# ════════════════════════════════════════════════════════════════
# [2] 多笔锁仓：只解最老一笔
# ════════════════════════════════════════════════════════════════
print("\n[2] 多笔锁仓：只解最老一笔，其余留簿")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(risk_overrides={"max_volume": 1}))
    engine.positions.add(make_pos(vol=3, signal_key="P19-2OLD", entry_bar_seq=1))
    engine.positions.add(make_pos(vol=2, signal_key="P19-2NEW", entry_bar_seq=5))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-2|2|S")
    engine.on_signal(sig)

    uos = unlock_orders(broker)
    check("[2a] 只报 1 笔 UNLOCK", len(uos), 1)
    check("[2b] 解的是最老一笔（3 手）", uos[0].volume if uos else None, 3)
    check("[2c] 簿内剩 1 笔", len(engine.positions), 1)
    check("[2d] 剩的是较新那笔（2 手 P19-2NEW）",
          (engine.positions.positions[0].signal_key,
           engine.positions.positions[0].volume), ("P19-2NEW", 2))


# ════════════════════════════════════════════════════════════════
# [3] 缺口补开（显式 unlock_no_new_open=False）：锁 3 手 + 今日 N=5 → 补开 2 手
# ════════════════════════════════════════════════════════════════
print("\n[3] 缺口补开：N=5、已解锁 3 手 → 补开 2 手（显式 unlock_no_new_open=False）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(risk_overrides={"max_volume": 5, "unlock_no_new_open": False}))
    engine.positions.add(make_pos(vol=3, signal_key="P19-3L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-3|2|S")
    engine.on_signal(sig)

    uos = unlock_orders(broker)
    ops = open_orders(broker)
    check("[3a] 1 笔 UNLOCK 3 手", (len(uos), uos[0].volume if uos else 0), (1, 3))
    check("[3b] 1 笔 OPEN 补开", len(ops), 1)
    check("[3c] 补开 2 手（N=5 − V=3）", ops[0].volume if ops else 0, 2)
    check("[3d] 补开方向与信号一致（卖信号 → SHORT）",
          ops[0].side if ops else None, Side.SHORT)
    check("[3e] 簿内 1 笔 2 手", (len(engine.positions),
                                 engine.positions.positions[0].volume), (1, 2))
    check("[3f] state=IN_TRADE", engine._state, EngineState.IN_TRADE)
    check("[3g] signal_action=unlock", store.signal_action(sig.key), "unlock")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[3h] 写 unlock_result(with_new_open)", "unlock_result" in kinds, True)


# ════════════════════════════════════════════════════════════════
# [4] 默认不补开（unlock_no_new_open 缺省=True）：缺口不补开
# ════════════════════════════════════════════════════════════════
print("\n[4] 默认不补开：只解锁，不补开")
with tmp_dir() as tmp:
    cfg = make_cfg(risk_overrides={"max_volume": 5})
    engine, store, broker, ev = build_engine(tmp, cfg=cfg)
    engine.positions.add(make_pos(vol=3, signal_key="P19-4L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-4|2|S")
    engine.on_signal(sig)

    check("[4a] 解锁照常（1 笔 3 手）",
          (len(unlock_orders(broker)),
           unlock_orders(broker)[0].volume if unlock_orders(broker) else 0), (1, 3))
    check("[4b] 零补开报单", len(open_orders(broker)), 0)
    check("[4c] 簿空", engine.positions.is_empty(), True)
    check("[4d] state=IDLE", engine._state, EngineState.IDLE)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[4e] 写 unlock_no_new_open 事件", "unlock_no_new_open" in kinds, True)


# ════════════════════════════════════════════════════════════════
# [5] N ≤ V：纯解锁，零补开
# ════════════════════════════════════════════════════════════════
print("\n[5] N ≤ V：纯解锁不补开")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(risk_overrides={"max_volume": 2}))
    engine.positions.add(make_pos(vol=3, signal_key="P19-5L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-5|2|S")
    engine.on_signal(sig)

    check("[5a] 解锁 3 手", unlock_orders(broker)[0].volume if unlock_orders(broker) else 0, 3)
    check("[5b] 零补开（N=2 ≤ V=3）", len(open_orders(broker)), 0)
    check("[5c] 簿空、state=IDLE",
          (engine.positions.is_empty(), engine._state), (True, EngineState.IDLE))


# ════════════════════════════════════════════════════════════════
# [6] 解锁拒单：回 IDLE、锁仓保留
# ════════════════════════════════════════════════════════════════
print("\n[6] 解锁拒单：整笔作废、锁仓留簿")
with tmp_dir() as tmp:
    bk = RejectUnlockBroker(InstrumentSpec(), {"sim_equity": 10_000_000.0})
    engine, store, broker, ev = build_engine(tmp, broker=bk,
                                             cfg=make_cfg(risk_overrides={"max_volume": 5}))
    engine.positions.add(make_pos(vol=3, signal_key="P19-6L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-6|2|S")
    engine.on_signal(sig)

    check("[6a] 解锁被拒 → 簿内锁仓保留", len(engine.positions), 1)
    check("[6b] 簿内仍是那笔 3 手", engine.positions.positions[0].volume, 3)
    check("[6c] 零补开（解锁没成就不入场）", len(open_orders(broker)), 0)
    check("[6d] signal_action=rejected", store.signal_action(sig.key), "rejected")
    check("[6e] state=IDLE", engine._state, EngineState.IDLE)
    check("[6f] 未设解锁复核 in-flight", engine._unlock_in_flight is None, True)


# ════════════════════════════════════════════════════════════════
# [7] N ≤ V（缺额 ≤0）：纯解锁不补开，且无 risk_block 事件
#     手数由 risk.max_volume 决定（仓位管理通道已删除）：
#     N=1 ≤ 已解锁 3 → want 不过 V，new_lots=0 → 纯解锁；五道硬闸门已删，无 risk_block
# ════════════════════════════════════════════════════════════════
print("\n[7] N=1 ≤ 已解锁 3：纯解锁不补开（无 risk_block 残留事件）")
with tmp_dir() as tmp:
    cfg = make_cfg(max_pos=3, risk_overrides={"max_volume": 1,
                                              "unlock_no_new_open": False})
    engine, store, broker, ev = build_engine(tmp, cfg=cfg)
    engine.positions.add(make_pos(vol=3, signal_key="P19-7LOCK", entry_bar_seq=1,
                                  side=Side.LONG))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-7|2|S")
    engine.on_signal(sig)

    check("[7a] 解锁照常（1 笔 3 手）",
          (len(unlock_orders(broker)),
           unlock_orders(broker)[0].volume if unlock_orders(broker) else 0), (1, 3))
    check("[7b] N=1 ≤ V=3 → 零补开", len(open_orders(broker)), 0)
    check("[7c] 解锁已生效（簿空）", engine.positions.is_empty(), True)
    check("[7d] signal_action 仍为 unlock", store.signal_action(sig.key), "unlock")
    ev.flush()
    recs = []
    with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    ures = [r for r in recs if r.get("kind") == "unlock_result"]
    check("[7e] new_lots=0（不足缺口）→ pure_unlock",
          ures[0].get("action") if ures else None, "pure_unlock")
    check("[7f] 不再产生 risk_block 事件",
          any(r.get("kind") == "risk_block" for r in recs), False)


# ════════════════════════════════════════════════════════════════
# [8] H2 轮次机制无残留
# ════════════════════════════════════════════════════════════════
print("\n[8] H2 轮次机制已无残留")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp, cfg=make_cfg(risk_overrides={"max_volume": 2}))
    engine.positions.add(make_pos(vol=2, signal_key="P19-8L", entry_bar_seq=1))
    engine.on_bar(make_bar())
    sig = make_signal(is_buy=False, sig_key="P19-8|2|S")
    engine.on_signal(sig)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[8a] 无 unlock_round 事件",
          any(k.startswith("unlock_round") for k in kinds), False)
    check("[8b] 无 unlock_round_pending 事件", "unlock_round_pending" in kinds, False)
    check("[8c] 无 unlock_round_result 事件", "unlock_round_result" in kinds, False)
    check("[8d] 无 _unlock_round_in_flight 属性",
          hasattr(engine, "_unlock_round_in_flight"), False)
    check("[8e] 无 _unlock_round_window 属性",
          hasattr(engine, "_unlock_round_window"), False)
    check("[8f] 无 _check_unlock_round 方法",
          hasattr(engine, "_check_unlock_round"), False)
    check("[8g] 无 _unlock_round_settle 方法",
          hasattr(engine, "_unlock_round_settle"), False)
    check("[8h] 解锁事件正常写出", "unlock" in kinds, True)


print("\n" + "=" * 60)
print("P19 解锁入场（一笔 FOK + 缺口补开）结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
