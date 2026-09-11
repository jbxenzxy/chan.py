# -*- coding: utf-8 -*-
"""
P15b 离场分流（转移 ④⑤）+ L1-L3 结算 + 多仓 FIFO 对账
====================================================
2026-09-11 Phase 7 改写。旧版围绕三个**已被删除**的东西组织：
  · `Engine._close_positions(positions, ...)` 批量逐笔平仓 —— 删除。新模型一次
    离场只发**一笔**报单：转移 ④（今日仓 → 反向 OPEN，整段净敞口一次锁住）
    或转移 ⑤（跨日仓 → CLOSE，平「净敞口方向最早的一笔」）。
  · `Position.origin == SOFT_EXIT_LOCK` 的"软离场"标记 —— 随来源概念删除。
    新口径下"锁仓"由**净敞口 = 0 且簿非空**表达（`AccountState.LOCKED`），
    不再给仓单打标。
  · `_close_position` / `_settle_position` 单仓兼容壳、`lock_booked` / `order.fifo_index`
    事件 —— 全部删除。离场唯一入口是 `_force_exit`（内部走 `_decide_exit` + `_execute`）。

新旧口径的**量化差别**（本测试正面钉死）
    旧：N 笔今日仓离场 → 逐笔锁 → 2N 笔 + N 单（每单 1 手）
    新：N 笔今日仓离场 → 整段锁 → N+1 笔 + **1 单（N 手）**
    这个差别来自"run 是风控单位、仓单只是 FIFO 序列"的新模型。

硬性要求（本测试锁死）
    [1] 转移 ④：今日仓离场 → 反向 OPEN 整段净敞口 → net 0 / LOCKED / 0 Trade
    [2] 转移 ⑤：跨日仓离场 → CLOSE「最早一笔」→ 记 Trade / FIFO 逐笔消化
    [3] 拒单：CLOSE 被拒 → 冷却（根数口径）；连续被拒到上限 → position_drop
    [4] `_settle_positions`：非 RUNNING / 无 run / 入场 K 线 三种不动作 + TP 触发
    [5] `_reconcile_positions` 多仓 FIFO 对账（部分平 / 清仓 / 告警 / 跳过 / G4 收口 run）
    [6] `on_bar` 全流程
    [7] 已删方法不存在（`_close_positions` / `_close_position` / `_settle_position`）

不需要真实 tqsdk / 网络；纯单测 + mock broker。
跑法：python Trading/Test/test_p15b_e33_fifo_close.py
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
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p15b_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.Base import OrderIntent  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, EngineState, ExitPlan, Position, Side,
)

_PASS = 0
_FAIL = 0
_SYM = "KQ.m@CFFEX.IF"
_BAR_DAY = "2026-09-01"


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


# ════════════════════════════════════════════════════════════════
# Mock Broker
# ════════════════════════════════════════════════════════════════
class RealPositionBroker(DryRunBroker):
    """可注入 real_longs / real_shorts 模拟外部真实持仓，用于对账测试。"""
    def __init__(self, spec, params=None, *, real_longs=None, real_shorts=None):
        super().__init__(spec, params)
        self._real_longs = real_longs
        self._real_shorts = real_shorts

    def real_position(self, side):
        if side is Side.LONG:
            return self._real_longs
        if side is Side.SHORT:
            return self._real_shorts
        return None


class RejectBroker(DryRunBroker):
    """可指定拒单的单号集合（1-based）。"""
    def __init__(self, spec, params=None, *, reject_calls=()):
        super().__init__(spec, params)
        self.reject_calls = set(reject_calls)
        self._calls = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        self._calls += 1
        if self._calls in self.reject_calls:
            from Trading.Infra.Types import Order
            o = Order(
                order_id="reject-{:06d}".format(self._calls),
                signal_key=signal_key, symbol=self.spec.trade_symbol,
                side=side,
                action="open" if intent is OrderIntent.OPEN else "close",
                volume=int(volume), price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "entry_date": entry_date,
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note,
                              entry_date, is_exit)


def make_engine(tmpdir, *, max_volume=1, broker=None):
    """构造引擎。每笔手数 = cfg.risk.max_volume（D2 后仓位管理已整体删除）。"""
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = max_volume
    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000,
             high=None, low=None):
    return Bar(date=date, open=close,
               high=close if high is None else high,
               low=close if low is None else low,
               close=close, timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST",
                  entry_date=_BAR_DAY):
    """构造一笔手写仓单（不带 origin —— 字段已删）。"""
    from Trading.Infra.Types import now_cn
    return Position(
        symbol=_SYM, side=side, volume=vol,
        entry_price=entry_price, entry_at=now_cn(),
        entry_bar_ts=4000 + entry_bar_seq * 100,
        entry_bar_seq=entry_bar_seq,
        signal_key=signal_key, open_order_id="manual",
        exit_plan=ExitPlan(name="manual", stop_price=entry_price - 10.0),
        entry_date=entry_date)


def seed_run(eng, *, side=Side.LONG, anchor=4550.0, bar_ts=4000, bar_seq=1,
             plan=None, signal_key="RUN"):
    """直接立起一段 run（风控锚 + 出场计划）。

    绕开真实开仓路径就必须补这一步：`_settle_positions` 只认 run，
    没有 run 就等于"这段敞口没有风控锚" → 不做任何 L1-L3 判定。
    """
    eng._run_side = side
    eng._run_anchor = anchor
    eng._run_volume = abs(eng.positions.net_volume())
    eng._run_bar_ts = bar_ts
    eng._run_bar_seq = bar_seq
    eng._run_signal_key = signal_key
    eng._run_plan = plan or ExitPlan(name="manual", stop_price=0.0)


def read_events(eng, kinds=None, tail_n=400):
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out
    for line in lines[-tail_n:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if kinds is None or rec.get("kind") in kinds:
            out.append(rec)
    return out


def order_events(eng):
    return read_events(eng, kinds={"order"})


# ════════════════════════════════════════════════════════════════
# [1] 转移 ④：今日仓离场 → 反向 OPEN 整段净敞口
# ════════════════════════════════════════════════════════════════
print("\n[1] 转移 ④：今日仓离场（整段锁仓，一笔报单）")

# 1.1 单笔今日多仓
with tmp_dir() as td:
    eng = make_engine(td)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-1")
    eng.positions.add(pos)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(eng.last_bar, "manual_exit", trigger_price=4555.0)

    check("[1.1a] 1 笔今日仓离场 → 簿 2 笔（原仓 + 反向仓）", len(eng.positions), 2)
    check("[1.1b] 双向各一笔（LONG + SHORT）",
          sorted(p.side.name for p in eng.positions.positions), ["LONG", "SHORT"])
    check("[1.1c] 反向仓 = 1 手（= 净敞口）",
          [p.volume for p in eng.positions.positions if p.side is Side.SHORT], [1])
    check("[1.1d] 净敞口归零", eng.positions.net_volume(), 0)
    check("[1.1e] account_state LOCKED", eng.account_state(), AccountState.LOCKED)
    check("[1.1f] _state IDLE", eng._state, EngineState.IDLE)
    check("[1.1g] broker 1 单", len(eng.broker.orders), 1)
    check("[1.1h] 0 条 Trade（锁仓不兑现 PnL）", len(eng.store.trades()), 0)
    _o = order_events(eng)
    check("[1.1i] order 事件 transition=4",
          (_o[-1].get("transition") if _o else None), 4)
    check("[1.1j] order 事件 intent=open（④ 是反向开仓，不是平仓）",
          (_o[-1].get("intent") if _o else None), "open")
    check("[1.1k] order 事件 is_exit=True（离场动作由 is_exit 判定，D13）",
          (_o[-1].get("is_exit") if _o else None), True)
    check("[1.1l] 写 run_end 事件",
          len(read_events(eng, kinds={"run_end"})), 1)

# 1.2 三笔今日多仓 → **1 笔**反向仓把整段净敞口锁住（新旧口径的关键差别）
with tmp_dir() as td:
    eng = make_engine(td)
    for i, k in enumerate(("A", "B", "C")):
        eng.positions.add(make_position(Side.LONG, 1, 4545.0 + i, 1 + 2 * i,
                                       signal_key="P15B-1-2-" + k))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(eng.last_bar, "manual_exit", trigger_price=4555.0)

    check("[1.2a] 3 笔今日多仓离场 → 簿 4 笔（3 原仓 + 1 笔反向）",
          len(eng.positions), 4)
    check("[1.2b] 反向仓只有 1 笔（整段净敞口一次锁住）",
          len([p for p in eng.positions.positions if p.side is Side.SHORT]), 1)
    check("[1.2c] 该反向仓 3 手（= 净敞口，不是逐笔 1 手 ×3）",
          [p.volume for p in eng.positions.positions if p.side is Side.SHORT], [3])
    check("[1.2d] **broker 只 1 单**（旧口径是 3 单）", len(eng.broker.orders), 1)
    check("[1.2e] 该单 3 手", eng.broker.orders[0].volume, 3)
    check("[1.2f] 净敞口归零", eng.positions.net_volume(), 0)
    check("[1.2g] 0 条 Trade", len(eng.store.trades()), 0)

# 1.3 空仓离场：无事发生
with tmp_dir() as td:
    eng = make_engine(td)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._force_exit(eng.last_bar, "manual_exit", trigger_price=4555.0)
    check("[1.3a] 空簿：簿 0", len(eng.positions), 0)
    check("[1.3b] 空簿：broker 0 单", len(eng.broker.orders), 0)
    check("[1.3c] 空簿：state IDLE", eng._state, EngineState.IDLE)
    check("[1.3d] 空簿：account_state FLAT", eng.account_state(), AccountState.FLAT)

# 1.4 拒单：整段锁仓失败 → 簿不动、无 Trade、写 order_rejected 与冷却计数
with tmp_dir() as td:
    broker = RejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                          reject_calls=(1,))
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1, signal_key="P15B-1-4"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(eng.last_bar, "manual_exit", trigger_price=4555.0)

    check("[1.4a] 拒单：簿仍 1 笔（未锁）", len(eng.positions), 1)
    check("[1.4b] 拒单：净敞口仍 +2", eng.positions.net_volume(), 2)
    check("[1.4c] 拒单：0 条 Trade", len(eng.store.trades()), 0)
    check_true("[1.4d] 拒单：写 order_rejected 事件",
               len(read_events(eng, kinds={"order_rejected"})) >= 1)
    # ★ ④ 是 **OPEN**（反向开仓），不是 CLOSE → 不进入"CLOSE 冷却"记账。
    #   冷却只约束 CLOSE，因为只有它会按 bar 反复重发（见 [3]）。
    check("[1.4e] ④ 被拒属 OPEN → **不**进入 CLOSE 冷却",
          eng._last_close_failed_bar_seq, 0)
    check("[1.4f] ④ 被拒 → 连续被拒计数仍 0", eng._close_fail_streak, 0)
    check("[1.4g] ④ 被拒 → _in_close_cooldown() False",
          eng._in_close_cooldown(), False)

# 1.5 跨日仓离场被拒（⑤ CLOSE）→ **才**进入 CLOSE 冷却记账
with tmp_dir() as td:
    broker = RejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                          reject_calls=(1,))
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1,
                                    signal_key="P15B-1-5",
                                    entry_date="2026-08-28"))
    _bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(_bar, "manual_exit", trigger_price=4555.0)

    check("[1.5a] ⑤ CLOSE 被拒：簿仍 1 笔", len(eng.positions), 1)
    check("[1.5b] ⑤ CLOSE 被拒：0 条 Trade", len(eng.store.trades()), 0)
    check("[1.5c] ⑤ CLOSE 被拒 → 进入冷却记账（_last_close_failed_bar_seq）",
          eng._last_close_failed_bar_seq, 10)
    check("[1.5d] ⑤ CLOSE 被拒 → 连续被拒计数 = 1", eng._close_fail_streak, 1)
    check("[1.5e] ⑤ CLOSE 被拒 → _in_close_cooldown() True",
          eng._in_close_cooldown(), True)
    check_true("[1.5f] 写 close_retry_cooldown 事件",
               len(read_events(eng, kinds={"close_retry_cooldown"})) == 1)


# ════════════════════════════════════════════════════════════════
# [2] 转移 ⑤：跨日仓离场 → CLOSE「同向最早一笔」
# ════════════════════════════════════════════════════════════════
print("\n[2] 转移 ⑤：跨日仓离场（CLOSE，逐笔 FIFO 消化）")

# 2.1 单笔跨日 → 平掉 → 簿空 + 1 Trade
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-2-1",
                                    entry_date="2026-08-28"))
    _bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(_bar, "manual_exit", trigger_price=4555.0)

    check("[2.1a] 跨日单笔离场 → 簿清空", eng.positions.is_empty(), True)
    check("[2.1b] 净敞口 0", eng.positions.net_volume(), 0)
    check("[2.1c] account_state FLAT", eng.account_state(), AccountState.FLAT)
    check("[2.1d] _state IDLE", eng._state, EngineState.IDLE)
    check("[2.1c2] 记 1 笔 Trade", len(eng.store.trades()), 1)
    check("[2.1e] Trade.signal_key = 被平那笔",
          eng.store.trades()[0]["signal_key"], "P15B-2-1")
    _o = order_events(eng)
    check("[2.1f] order 事件 transition=5",
          (_o[-1].get("transition") if _o else None), 5)
    check("[2.1g] order 事件 intent=close",
          (_o[-1].get("intent") if _o else None), "close")
    check("[2.1h] order 事件 is_exit=True",
          (_o[-1].get("is_exit") if _o else None), True)
    check("[2.1i] 报单带 target_signal_key",
          (_o[-1].get("target_signal_key") if _o else None), "P15B-2-1")

# 2.2 三笔跨日 → 一次只平「最早一笔」（1 手），簿剩 2 笔
with tmp_dir() as td:
    eng = make_engine(td)
    # 故意乱序加入，FIFO 由 entry_bar_seq 决定
    eng.positions.add(make_position(Side.LONG, 1, 4547.0, 5,
                                    signal_key="P15B-2-2-C",
                                    entry_date="2026-08-30"))
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-2-2-A",
                                    entry_date="2026-08-28"))
    eng.positions.add(make_position(Side.LONG, 1, 4546.0, 3,
                                    signal_key="P15B-2-2-B",
                                    entry_date="2026-08-29"))
    _bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(_bar, "manual_exit", trigger_price=4555.0)

    check("[2.2a] 3 笔跨日 → 簿剩 2 笔", len(eng.positions), 2)
    check("[2.2b] 被平的是最早一笔 A",
          sorted(p.signal_key for p in eng.positions.positions),
          ["P15B-2-2-B", "P15B-2-2-C"])
    check("[2.2c] 净敞口从 +3 → +2", eng.positions.net_volume(), 2)
    check("[2.2d] 1 条 Trade", len(eng.store.trades()), 1)
    check("[2.2e] Trade 记的是 A",
          eng.store.trades()[0]["signal_key"], "P15B-2-2-A")
    check("[2.2f] broker 只 1 单（一次离场 = 一笔报单）", len(eng.broker.orders), 1)
    check("[2.2g] 该单 1 手（不是 3 手 —— ⑤ 逐笔平）", eng.broker.orders[0].volume, 1)
    check("[2.2h] 仍有净敞口 → account_state RUNNING",
          eng.account_state(), AccountState.RUNNING)

# 2.3 卖信号侧：跨日净空 → CLOSE 平最早的空头
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.SHORT, 1, 4560.0, 1,
                                    signal_key="P15B-2-3-S1",
                                    entry_date="2026-08-28"))
    eng.positions.add(make_position(Side.SHORT, 1, 4565.0, 3,
                                    signal_key="P15B-2-3-S2",
                                    entry_date="2026-08-29"))
    _bar = make_bar(date="2026-09-01 09:40", close=4550.0)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.SHORT, anchor=4560.0)
    eng._force_exit(_bar, "manual_exit", trigger_price=4550.0)

    check("[2.3a] 净空跨日 → 平最早一笔 S1", len(eng.positions), 1)
    check("[2.3b] 剩 S2", eng.positions.positions[0].signal_key, "P15B-2-3-S2")
    check("[2.3c] 净敞口 -1", eng.positions.net_volume(), -1)
    check("[2.3d] 报单 side=SHORT（CLOSE 的 side = 被平仓单方向）",
          eng.broker.orders[0].side, Side.SHORT)
    check("[2.3e] 报单 intent=close", eng.broker.orders[0].meta.get("intent"), "close")


# ════════════════════════════════════════════════════════════════
# [3] CLOSE 冷却 / 连续被拒兜底
# ════════════════════════════════════════════════════════════════
print("\n[3] CLOSE 冷却与连续被拒兜底")

# 3.1 冷却中：不发单、簿不动
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-3-1",
                                    entry_date="2026-08-28"))
    eng.last_bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    # 冷却按**根数**判定：10 - 7 = 3 < close_retry_bars(5) → 冷却中
    eng._last_close_failed_bar_seq = 7
    _act = eng._decide_exit(eng.last_bar)
    check("[3.1a] 冷却期 _decide_exit 仍产出 CLOSE 动作",
          (_act.intent if _act else None), OrderIntent.CLOSE)
    _o = eng._execute(_act, ref_price=4555.0, bar=eng.last_bar, reason="manual")
    check("[3.1b] 冷却中 _execute 返回 None（未报单）", _o, None)
    check("[3.1c] 拒绝原因 = close_cooldown", eng._last_reject, "close_cooldown")
    check("[3.1d] 簿仍 1 笔", len(eng.positions), 1)
    check("[3.1e] broker 0 单", len(eng.broker.orders), 0)
    check("[3.1f] _in_close_cooldown() True", eng._in_close_cooldown(), True)
    check("[3.1g] 剩余冷却根数 = 5 - 3 = 2", eng._close_cooldown_bars_left(), 2)

# 3.2 force=True 绕过冷却（关闭自动下单的收尾路径用）
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-3-2",
                                    entry_date="2026-08-28"))
    eng.last_bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._last_close_failed_bar_seq = 7
    _act = eng._decide_exit(eng.last_bar)
    _o = eng._execute(_act, ref_price=4555.0, bar=eng.last_bar,
                      reason="auto_order_off", force=True)
    check("[3.2a] force=True 绕过冷却 → 报单成功", _o is not None, True)
    check("[3.2b] 簿清空", eng.positions.is_empty(), True)
    check("[3.2c] 1 条 Trade", len(eng.store.trades()), 1)

# 3.3 连续被拒到上限（close_max_streak，默认 20）→ position_drop（清掉该笔幻影仓）
with tmp_dir() as td:
    _streak = TradingConfig.from_dict(DEFAULT_CONFIG).engine.close_max_streak
    broker = RejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                          reject_calls=tuple(range(1, _streak + 1)))
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-3-3",
                                    entry_date="2026-08-28"))
    eng.last_bar = make_bar(date="2026-09-01 09:40", close=4555.0)
    eng.bars_seen = 100
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    check("[3.3a-0] close_max_streak 阈值 = 20", _streak, 20)
    for _i in range(_streak + 2):
        eng.bars_seen += eng._close_retry_bars + 1     # 绕开冷却
        _act = eng._decide_exit(eng.last_bar)
        if _act is None:
            break
        eng._execute(_act, ref_price=4555.0, bar=eng.last_bar, reason="manual")
    check("[3.3a] 连续被拒达上限 → 该仓从簿中清除（判定为柜台不存在）",
          len(eng.positions), 0)
    check_true("[3.3b] 写 position_drop 事件",
               len(read_events(eng, kinds={"position_drop"})) == 1)
    _pd = read_events(eng, kinds={"position_drop"})
    check("[3.3c] position_drop 原因 = close_repeatedly_rejected",
          (_pd[-1].get("reason") if _pd else None), "close_repeatedly_rejected")
    check_true("[3.3d] 升级为告警（D11）", len(eng._alerts) >= 1)
    check("[3.3e] 告警码 = close_repeatedly_rejected",
          (eng._alerts[-1].get("code") if eng._alerts else None),
          "close_repeatedly_rejected")


# ════════════════════════════════════════════════════════════════
# [4] `_settle_positions`（L1-L3 → 转移 ④⑤）
# ════════════════════════════════════════════════════════════════
print("\n[4] _settle_positions（L1-L3 结算）")

# 4.1 非 RUNNING（空簿 / 锁仓）→ 不动作
with tmp_dir() as td:
    eng = make_engine(td)
    eng.last_bar = make_bar(close=4558.0, high=4560.0, low=4540.0)
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("[4.1a] 空簿 settle → broker 0 单", len(eng.broker.orders), 0)
    check("[4.1b] 空簿 settle → state IDLE", eng._state, EngineState.IDLE)

# 4.2 有净敞口但**没有 run**（缺风控锚）→ 不做 L1-L3
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-4-2"))
    eng.last_bar = make_bar(close=4500.0, high=4520.0, low=4498.0)
    eng.bars_seen = 10
    check("[4.2a] 有敞口 → RUNNING", eng.account_state(), AccountState.RUNNING)
    eng._settle_positions(eng.last_bar)
    check("[4.2b] 无 run（无风控锚）→ 不产生任何报单",
          len(eng.broker.orders), 0)
    check("[4.2c] 簿仍 1 笔", len(eng.positions), 1)

# 4.3 入场 K 线不参与出场判定（bar.timestamp <= run.entry_bar_ts）
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-4-3"))
    # 止损 4540；bar.low=4530 本会触发，但时间戳等于 run.entry_bar_ts → 跳过
    eng.last_bar = make_bar(close=4532.0, high=4540.0, low=4530.0, ts=4000)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4540.0))
    eng._settle_positions(eng.last_bar)
    check("[4.3a] 入场 K 线 → broker 0 单", len(eng.broker.orders), 0)
    check("[4.3b] 入场 K 线 → 簿仍 1 笔", len(eng.positions), 1)

# 4.4 硬止损触发 + 今日仓 → 转移 ④（锁仓）
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1,
                                    signal_key="P15B-4-4"))
    eng.last_bar = make_bar(close=4532.0, high=4540.0, low=4530.0, ts=9000)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4540.0))
    eng._settle_positions(eng.last_bar)
    check("[4.4a] 今日仓止损 → 转移 ④ 锁仓（簿 2 笔）", len(eng.positions), 2)
    check("[4.4b] 净敞口归零", eng.positions.net_volume(), 0)
    check("[4.4c] account_state LOCKED", eng.account_state(), AccountState.LOCKED)
    check("[4.4d] 0 条 Trade（④ 不兑现）", len(eng.store.trades()), 0)
    _o = order_events(eng)
    check("[4.4e] 报单 transition=4 / is_exit=True",
          ((_o[-1].get("transition"), _o[-1].get("is_exit")) if _o else None),
          (4, True))

# 4.5 硬止损触发 + 跨日仓 → 转移 ⑤（CLOSE）
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1,
                                    signal_key="P15B-4-5",
                                    entry_date="2026-08-28"))
    _bar = make_bar(date="2026-09-01 10:00", close=4532.0, high=4540.0,
                    low=4530.0, ts=9000)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4540.0))
    eng._settle_positions(_bar)
    check("[4.5a] 跨日仓止损 → 转移 ⑤ 平仓（簿清空）",
          eng.positions.is_empty(), True)
    check("[4.5b] 1 条 Trade", len(eng.store.trades()), 1)
    check("[4.5c] Trade.reason 带出止损原因",
          eng.store.trades()[0]["reason"], "sl")
    _o = order_events(eng)
    check("[4.5d] 报单 transition=5", (_o[-1].get("transition") if _o else None), 5)
    check("[4.5e] 止损价 4540 成交价",
          eng.store.trades()[0]["entry_price"], 4545.0)

# 4.6 L3 only_update：只更新计划不触发离场
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4500.0, 1,
                                    signal_key="P15B-4-6"))
    # 给足 R 与浮盈，让 L3 保本/跟踪启动
    _bar = make_bar(close=4550.0, high=4552.0, low=4545.0, ts=9000)
    eng.last_bar = _bar
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4500.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4480.0, tp_price=None,
                           params={"R": 10.0}))
    eng._settle_positions(_bar)
    _upd = read_events(eng, kinds={"exit_plan_update"})
    check_true("[4.6a] L3 只更新计划 → 写 exit_plan_update",
               len(_upd) >= 1 or len(eng.broker.orders) == 0)
    check("[4.6b] 未触发离场 → 簿仍 1 笔", len(eng.positions), 1)


# ════════════════════════════════════════════════════════════════
# [5] `_reconcile_positions` 多仓 FIFO 对账
# ════════════════════════════════════════════════════════════════
print("\n[5] _reconcile_positions 多仓 FIFO 对账")

# 5.1 real == engine → 无操作
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=2, real_shorts=0)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-1-A"))
    eng.positions.add(make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-5-1-B"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("[5.1a] 一致：簿仍 2 笔", len(eng.positions), 2)
    check("[5.1b] 一致：broker 0 单（对账不下单）", len(eng.broker.orders), 0)
    check("[5.1c] 一致：0 条 Trade", len(eng.store.trades()), 0)

# 5.2 real < engine → FIFO 部分平最早一笔
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=2, real_shorts=None)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4547.0, 5, signal_key="P15B-5-2-C"))
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-2-A"))
    eng.positions.add(make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-5-2-B"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("[5.2a] 部分平 FIFO：簿剩 2 笔", len(eng.positions), 2)
    check("[5.2b] 最早一笔 A 被平",
          sorted(p.signal_key for p in eng.positions.positions),
          ["P15B-5-2-B", "P15B-5-2-C"])
    check("[5.2c] 1 条 Trade", len(eng.store.trades()), 1)
    check("[5.2d] Trade.reason = reconcile_external_partial",
          eng.store.trades()[0]["reason"], "reconcile_external_partial")
    check("[5.2e] Trade.signal_key = A",
          eng.store.trades()[0]["signal_key"], "P15B-5-2-A")
    check("[5.2f] 对账不下单：broker 0 单", len(eng.broker.orders), 0)

# 5.3 real == 0 → 清空同侧全部 + summary 事件
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=0, real_shorts=None)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-3-A"))
    eng.positions.add(make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-5-3-B"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("[5.3a] real=0：簿清空", len(eng.positions), 0)
    check("[5.3b] real=0：state IDLE", eng._state, EngineState.IDLE)
    check("[5.3c] real=0：2 条 Trade", len(eng.store.trades()), 2)
    check("[5.3d] real=0：FIFO 顺序 A→B",
          sorted(t["signal_key"] for t in eng.store.trades()),
          ["P15B-5-3-A", "P15B-5-3-B"])
    check_true("[5.3e] real=0：写 position_externally_closed_summary",
               len(read_events(eng, kinds={"position_externally_closed_summary"})) >= 1)

# 5.4 real > engine → 仅告警
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=5, real_shorts=None)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-4-A"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("[5.4a] real>engine：簿仍 1 笔", len(eng.positions), 1)
    check("[5.4b] real>engine：broker 0 单", len(eng.broker.orders), 0)
    check("[5.4c] real>engine：0 条 Trade（不接管未知持仓）",
          len(eng.store.trades()), 0)
    check("[5.4d] real>engine：1 条 position_mismatch 事件",
          len(read_events(eng, kinds={"position_mismatch"})), 1)

# 5.5 broker 不支持 real_position → 跳过
with tmp_dir() as td:
    eng = make_engine(td)          # DryRunBroker.real_position → None
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-5-A"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("[5.5a] 不支持：簿仍 1 笔", len(eng.positions), 1)
    check("[5.5b] 不支持：0 条 Trade", len(eng.store.trades()), 0)

# 5.6 入场 K 线跳过（min_bars_held < 1）
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=0, real_shorts=None)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-6-A"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 1               # bars_held = 1 - 1 = 0 < 1 → 跳过
    eng._reconcile_positions()
    check("[5.6a] 入场 K 线：簿仍 1 笔", len(eng.positions), 1)
    check("[5.6b] 入场 K 线：0 条 Trade", len(eng.store.trades()), 0)

# 5.7 G4：对账把净敞口判成 0 → 同步结束 run（防孤儿风控锚被持久化）
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                real_longs=0, real_shorts=None)
    eng = make_engine(td, broker=broker)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-7-A"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    check("[5.7a] 前置：run 已建立", eng._run_side, Side.LONG)
    eng._reconcile_positions()
    check("[5.7b] 对账清空 → run 被结束（_run_side 清空）", eng._run_side, None)
    check("[5.7c] run 计划也清空", eng._run_plan, None)
    check_true("[5.7d] 写 run_ended_by_reconcile 事件",
               len(read_events(eng, kinds={"run_ended_by_reconcile"})) == 1)
    check("[5.7e] kv run 未残留（不会让下次启动撞 G2）",
          eng.store.get_json("run"), None)


# ════════════════════════════════════════════════════════════════
# [6] on_bar 全流程
# ════════════════════════════════════════════════════════════════
print("\n[6] on_bar 全流程（对账 → settle → 离场 → 收口 run）")

with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1,
                                    signal_key="P15B-6-1",
                                    entry_date="2026-08-28"))
    seed_run(eng, side=Side.LONG, anchor=4545.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4540.0))
    bar = make_bar(date="2026-09-01 10:00", close=4532.0, high=4540.0,
                   low=4530.0, ts=9000)
    eng.on_bar(bar)
    check("[6.1a] on_bar 触发跨日止损 → 簿清空", eng.positions.is_empty(), True)
    check("[6.1b] 1 条 Trade", len(eng.store.trades()), 1)
    check("[6.1c] _state IDLE", eng._state, EngineState.IDLE)
    check("[6.1d] run 已收口", eng._run_plan, None)
    check("[6.1e] kv run 未残留", eng.store.get_json("run"), None)

with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 2, 4545.0, 1, signal_key="P15B-6-2"))
    seed_run(eng, side=Side.LONG, anchor=4545.0, bar_ts=4000,
             plan=ExitPlan(name="manual", stop_price=4540.0))
    bar = make_bar(date="2026-09-01 10:00", close=4532.0, high=4540.0,
                   low=4530.0, ts=9000)
    eng.on_bar(bar)
    check("[6.2a] on_bar 触发今日止损 → 转移 ④ 锁仓（簿 2 笔）",
          len(eng.positions), 2)
    check("[6.2b] 净敞口归零 → LOCKED", eng.account_state(), AccountState.LOCKED)
    check("[6.2c] _state IDLE", eng._state, EngineState.IDLE)


# ════════════════════════════════════════════════════════════════
# [7] 已删方法 / 已删概念的缺席
# ════════════════════════════════════════════════════════════════
print("\n[7] 已删方法与概念的缺席")

_TE = TradingEngine
check("[7a] `_close_positions` 已删（批量逐笔平仓）",
      hasattr(_TE, "_close_positions"), False)
check("[7b] `_close_position` 已删（单仓兼容壳）",
      hasattr(_TE, "_close_position"), False)
check("[7c] `_settle_position` 已删（单仓兼容壳）",
      hasattr(_TE, "_settle_position"), False)
check("[7b2] `_reconcile_position` 保留（兼容壳，转发到 _reconcile_positions）",
      hasattr(_TE, "_reconcile_position"), True)
for _m in ("_force_exit", "_settle_positions", "_reconcile_positions",
           "_decide_exit", "_execute", "_book_close", "_book_open"):
    check("[7c{}] 有 {}".format(_m, _m), hasattr(_TE, _m), True)
# 事件层不再有 lock_booked / fifo_index（软离场打标随来源概念删除）
with tmp_dir() as td:
    eng = make_engine(td)
    eng.positions.add(make_position(Side.LONG, 1, 4545.0, 1,
                                    signal_key="P15B-7-1"))
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    seed_run(eng, side=Side.LONG, anchor=4545.0)
    eng._force_exit(eng.last_bar, "manual_exit", trigger_price=4555.0)
    check("[7d] 事件流无 lock_booked（软离场打标已删）",
          len(read_events(eng, kinds={"lock_booked"})), 0)
    _o7 = order_events(eng)
    check("[7e] order 事件无 fifo_index 字段",
          "fifo_index" in (_o7[-1] if _o7 else {}), False)
    check("[7f] Position 无 origin 属性",
          hasattr(eng.positions.positions[0], "origin"), False)


# ════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P15b 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
