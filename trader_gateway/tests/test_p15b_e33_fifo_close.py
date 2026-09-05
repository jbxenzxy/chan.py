# -*- coding: utf-8 -*-
"""
P15b FIFO 出场 + 多仓对账 + for-each settle（Phase E3.3）
=========================================================
背景
    Phase E3.3 在 E3.2 (PositionBook + _open_positions 批次开仓) 基础上加入多仓平仓
    与对账：
      · engine._close_positions(positions, ...) 多仓 FIFO 出场版（按 entry_bar_seq ASC）
      · engine._close_position / _settle_position / _reconcile_position 兼容壳
      · engine._settle_positions for-each 多仓逐笔判 exit，触发的仓位一次性 FIFO 平仓
      · engine._reconcile_positions 多仓版：每侧独立对账
          · real_vol < engine_vol → FIFO 部分平最早仓位
          · real_vol == 0 → 清空同侧全部仓位 + 写 position_externally_closed_summary
          · real_vol > engine_vol → 仅告警 position_mismatch，不自动接管
          · broker 不支持 real_position（默认 None） → 跳过对账

硬性要求（本测试锁死）
    ① _close_positions 直接调用：
        · 单笔 = E3.1 _close_position 等价
        · 多笔 FIFO：按 entry_bar_seq ASC 逐笔平仓
        · 每笔独立 ev.write(order fifo_index=idx) + ev.write(close fifo_index)
        · 每笔独立 Trade + risk.on_trade_closed
        · 第一笔拒单整批停 + cooldown（保持 E3.1 行为）
        · 后续笔拒单：保留剩余仓位继续（部分成交）
        · 全部成交：簿 0 + state IDLE
        · 部分成交：簿剩余 + state EXITING
    ② _settle_positions 多仓 for-each：
        · 3 仓同时触发 TP → 一次性 FIFO 平仓
        · 各仓独立 trade（独立 trade_id + signal_key）
        · 各仓独立 exit_plan 更新
    ③ _reconcile_positions 多仓版：
        · real_vol < engine_vol FIFO 部分平（生成 reconcile_external_partial trade）
        · real_vol == 0 清同侧全部
        · real_vol > engine_vol 仅告警
        · broker 不支持（None）跳过
    ④ 兼容壳：_close_position / _settle_position / _reconcile_position 单仓调通
    ⑤ on_bar 全流程：多仓 + on_bar → 对账 → settle → FIFO 平仓

不需要真实 tqsdk / 网络；纯单测 + RealPositionBroker mock 测对账路径。
跑法：python tests/test_p15b_e33_fifo_close.py
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
    print("\u2717 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)


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


from tg import brokers  # noqa: E402  注册 dry_run
from tg.brokers.base import OrderIntent  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.position_book import PositionBook  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EngineState, EntryMode, ExitPlan, Position, Side,
)


_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_truthy(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


# ════════════════════════════════════════════════════════════════
# Mock Broker：可注入 real_position 返回值模拟对账
# ════════════════════════════════════════════════════════════════
class RealPositionBroker(DryRunBroker):
    """DryRunBroker 子类：可注入 real_longs / real_shorts 模拟外部真实持仓。"""
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


def make_engine(tmpdir, *, max_open_positions=3, batch_open=1,
                cfg_risk_max_volume=10, tp_points=5.0, stop_points=10.0,
                close_before_session_end=False, broker=None):
    """构造引擎：默认 batch=1 + sizing 关闭（fixed_volume=1）。"""
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = max_open_positions
    cfg.risk.max_volume = cfg_risk_max_volume
    cfg.risk.enforce_session = False
    cfg.risk.close_before_session_end = close_before_session_end
    cfg.sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    cfg.sizing["enabled"] = False
    cfg.sizing["fixed_volume"] = 1
    cfg.sizing["batch_open"] = batch_open

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    # ExitPolicy: TP = tp_points 上方（多）/ 下方（空），给 settle 触发用
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": tp_points,
                                "stop_loss_points": stop_points})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    eng = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return eng


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000):
    """构造 Bar。默认 ts=5000（大于所有 make_position 的 entry_bar_ts），
    避免触发 _settle_positions 的「入场那根 K 线不参与出场判定」跳过逻辑。
    """
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST",
                   tp_offset=5.0, sl_offset=10.0):
    """构造一个手动 Position（不走 _open_positions），用于测试 settle / close / reconcile。

    tp_offset / sl_offset 是相对 entry_price 的偏移量：
      · 多仓：tp = entry + tp_offset，stop = entry - sl_offset
      · 空仓：tp = entry - tp_offset，stop = entry + sl_offset
    """
    from tg.types import now_cn
    if side is Side.LONG:
        tp = entry_price + tp_offset
        stop = entry_price - sl_offset
    else:
        tp = entry_price - tp_offset
        stop = entry_price + sl_offset
    return Position(
        symbol="KQ.m@CFFEX.IF", side=side, volume=vol,
        entry_price=entry_price, entry_at=now_cn(),
        entry_bar_ts=4000 + entry_bar_seq * 100,
        entry_bar_seq=entry_bar_seq,
        signal_key=signal_key, open_order_id="manual",
        exit_plan=ExitPlan(name="tp_sl", stop_price=stop, tp_price=tp,
                            params={"take_profit_points": tp_offset,
                                    "stop_loss_points": sl_offset}),
        entry_mode=EntryMode.OPEN_FIRST)


def read_events(eng, kinds=None, tail_n=200):
    """读 events.jsonl，返回事件列表；可选 kinds 过滤。"""
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


# ════════════════════════════════════════════════════════════════
# [1] _close_positions 直接调用 —— FIFO 出场
# ════════════════════════════════════════════════════════════════
print("\n[1] _close_positions 直接调用 FIFO 出场")

# 1.1 单笔 = E3.1 _close_position 等价
with tmp_dir() as td:
    eng = make_engine(td)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-1")
    eng.positions.add(pos)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_positions([pos], "manual", 4555.0, eng.last_bar, signal_key="P15B-1-1")
    check("单笔：簿 1 仓（LOCKED 锁仓，H1 落簿）", len(eng.positions), 1)
    check("单笔：锁仓 entry_mode=LOCKED", eng.positions.positions[0].entry_mode, EntryMode.LOCKED)
    check("单笔：state IDLE", eng._state, EngineState.IDLE)
    check("单笔：broker 1 单", len(eng.broker.orders), 1)
    trades = eng.store.trades()
    check("单笔：1 条 Trade", len(trades), 1)
    check("单笔：Trade.signal_key 对应", trades[0]["signal_key"], "P15B-1-1")

# 1.2 多笔同向 FIFO：按 entry_bar_seq ASC 平仓
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    # 故意乱序加入（FIFO 排序由 _close_positions 内部保证）
    p2 = make_position(Side.LONG, 1, 4547.0, 5, signal_key="P15B-1-2-B")
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-2-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-1-2-M")
    eng.positions.add(p2)
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_positions([p2, p0, p1], "manual", 4555.0, eng.last_bar)
    check("FIFO：簿 3 仓（3 笔 LOCKED 锁仓，H1 落簿）", len(eng.positions), 3)
    check("FIFO：全部 entry_mode=LOCKED",
          all(p.entry_mode is EntryMode.LOCKED for p in eng.positions.positions), True)
    check("FIFO：broker 3 单", len(eng.broker.orders), 3)
    trades = eng.store.trades()
    check("FIFO：3 条 Trade", len(trades), 3)
    # trade signal_key 顺序：建仓顺序（A → M → B）
    keys_in_order = [t["signal_key"] for t in trades]
    check("FIFO：trade 按建仓顺序 A→M→B",
          keys_in_order,
          ["P15B-1-2-A", "P15B-1-2-M", "P15B-1-2-B"])

# 1.3 ev.write(order fifo_index) + ev.write(close fifo_index) 字段
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-3-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-1-3-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_positions([p0, p1], "manual", 4555.0, eng.last_bar)
    order_evs = read_events(eng, kinds={"order"})
    fifo_idx = [e.get("fifo_index") for e in order_evs]
    batch_sz = [e.get("batch_size") for e in order_evs]
    check("order ev：fifo_index=[0,1]", fifo_idx, [0, 1])
    check("order ev：batch_size=[2,2]", batch_sz, [2, 2])
    close_evs = read_events(eng, kinds={"close"})
    close_idx = [e.get("fifo_index") for e in close_evs]
    check("close ev：fifo_index=[0,1]", close_idx, [0, 1])

# 1.4 第一笔拒单整批停
with tmp_dir() as td:
    # E3.3 内联 RejectDryBroker（避免跨测试文件 import 触发 P15a 顶层代码执行）
    class RejectDryBroker(DryRunBroker):
        """DryRunBroker 子类，可指定拒单次数。0=全过、1=首笔拒、-1=全拒。"""
        def __init__(self, spec, params=None, *, reject_first_n=0):
            super().__init__(spec, params)
            self.reject_first_n = reject_first_n
            self._calls = 0

        def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
            self._calls += 1
            if self._calls <= self.reject_first_n:
                from tg.types import Order
                o = Order(
                    order_id="reject-{:06d}".format(self._calls),
                    signal_key=signal_key, symbol=self.spec.trade_symbol,
                    side=side, action="open", volume=int(volume),
                    price=0.0, req_price=float(ref_price),
                    filled_price=None, status="rejected",
                    created_at="2026-09-01 09:30", broker=self.name, note=note,
                    meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                          "reject_reason": "test_reject"})
                self.orders.append(o)
                return o
            return super().submit(intent, side, volume, ref_price, signal_key, note)

    broker = RejectDryBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              reject_first_n=1)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-4-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-1-4-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_positions([p0, p1], "manual", 4555.0, eng.last_bar)
    check("首笔拒单：簿 2 仓（未平）", len(eng.positions), 2)
    check("首笔拒单：broker 只提交 1 单", len(broker.orders), 1)
    check("首笔拒单：state EXITING", eng._state, EngineState.EXITING)
    trades = eng.store.trades()
    check("首笔拒单：0 条 Trade", len(trades), 0)
    rejected_evs = read_events(eng, kinds={"order_rejected"})
    check("首笔拒单：order_rejected 事件", len(rejected_evs) >= 1, True)

# 1.5 第二笔拒单部分成交（第一笔成功 + 第二笔拒单）
with tmp_dir() as td:
    # P15b 内联 broker（不依赖 P15a import）
    # reject_first_n=1: 第 1 单拒，后续都过
    # 但 P15a RejectDryBroker 是 _calls <= reject_first_n 时拒单
    # 这里我们要「第 2 单拒」→ 用 _calls=2 时拒
    # 改写：直接构造新 broker
    class SkipSecondBroker(DryRunBroker):
        """第 2 笔拒单（前面所有都过）。"""
        def __init__(self, spec, params=None):
            super().__init__(spec, params)
            self._calls = 0

        def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
            self._calls += 1
            if self._calls == 2:
                from tg.types import Order
                o = Order(
                    order_id="skip-{:06d}".format(self._calls),
                    signal_key=signal_key, symbol=self.spec.trade_symbol,
                    side=side, action="open", volume=int(volume),
                    price=0.0, req_price=float(ref_price),
                    filled_price=None, status="rejected",
                    created_at="2026-09-01 09:30", broker=self.name, note=note,
                    meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                          "reject_reason": "test_skip_second"})
                self.orders.append(o)
                return o
            return super().submit(intent, side, volume, ref_price, signal_key, note)

    broker = SkipSecondBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0})
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-5-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-1-5-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_positions([p0, p1], "manual", 4555.0, eng.last_bar)
    check("第2笔拒单：簿 2 仓（剩 p1 + p0 锁仓，H1 落簿）", len(eng.positions), 2)
    check("第2笔拒单：broker 2 单", len(broker.orders), 2)
    check("第2笔拒单：state EXITING（仍有仓位）", eng._state, EngineState.EXITING)
    trades = eng.store.trades()
    check("第2笔拒单：1 条 Trade（p0 成交）", len(trades), 1)
    check("第2笔拒单：Trade.signal_key == A", trades[0]["signal_key"], "P15B-1-5-A")

# 1.6 空列表快速返回
with tmp_dir() as td:
    eng = make_engine(td)
    eng.last_bar = make_bar()
    eng._close_positions([], "manual", 4555.0, eng.last_bar)
    check("空列表：簿 0", len(eng.positions), 0)
    check("空列表：broker 0 单", len(eng.broker.orders), 0)
    check("空列表：state IDLE（未变）", eng._state, EngineState.IDLE)

# 1.7 cooldown 中快速返回（沿用 _close_retry_bars 字段，默认 5）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-1-7-A")
    eng.positions.add(p0)
    eng.last_bar = make_bar(close=4555.0, ts=5000)
    eng.bars_seen = 10
    # 差值 = 5000 - 4996 = 4 <= _close_retry_bars(5) → cooldown 中
    eng._last_close_failed_bar_ts = 4996
    eng._close_positions([p0], "manual", 4555.0, eng.last_bar)
    check("cooldown：簿 1 仓（未平）", len(eng.positions), 1)
    check("cooldown：broker 0 单", len(eng.broker.orders), 0)
    check("cooldown：state EXITING", eng._state, EngineState.EXITING)


# ════════════════════════════════════════════════════════════════
# [2] _settle_positions 多仓 for-each
# ════════════════════════════════════════════════════════════════
print("\n[2] _settle_positions 多仓 for-each")

# 2.1 多仓触发 TP：一次性 FIFO 平仓
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0)
    # 3 笔多仓：entry_price 全部在 4545（TP=4555 触发）
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-2-1-A")
    p1 = make_position(Side.LONG, 1, 4545.0, 3, signal_key="P15B-2-1-B")
    p2 = make_position(Side.LONG, 1, 4545.0, 5, signal_key="P15B-2-1-C")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.positions.add(p2)
    eng.last_bar = make_bar(close=4558.0)  # 触发 TP
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("3仓TP：簿 3（3 笔 LOCKED 锁仓，H1 落簿）", len(eng.positions), 3)
    check("3仓TP：broker 3 单", len(eng.broker.orders), 3)
    trades = eng.store.trades()
    check("3仓TP：3 条 Trade", len(trades), 3)
    keys = [t["signal_key"] for t in trades]
    check("3仓TP：FIFO 顺序 A→B→C", keys,
          ["P15B-2-1-A", "P15B-2-1-B", "P15B-2-1-C"])
    check("3仓TP：state IDLE", eng._state, EngineState.IDLE)

# 2.2 仅 1 仓触发 TP（其他未触发）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-2-2-A")  # TP 触发
    p1 = make_position(Side.LONG, 1, 4560.0, 3, signal_key="P15B-2-2-B")  # 不触发（已是 TP 上方）
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4558.0)
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("1仓触发：簿 2 仓（剩 p1 + p0 锁仓，H1 落簿）", len(eng.positions), 2)
    check("1仓触发：broker 1 单", len(eng.broker.orders), 1)
    check("1仓触发：剩 p1.signal_key", eng.positions.positions[0].signal_key,
          "P15B-2-2-B")
    trades = eng.store.trades()
    check("1仓触发：1 条 Trade", len(trades), 1)
    check("1仓触发：Trade.signal_key == A", trades[0]["signal_key"], "P15B-2-2-A")

# 2.3 入场 K 线跳过（bar.timestamp <= entry_bar_ts 不判 exit）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-2-3-A")
    eng.positions.add(p0)
    # entry_bar_ts = 4100, ts=4100 → 等于 entry_bar_ts 跳过
    eng.last_bar = make_bar(close=4558.0, ts=4100)
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("入场K线：簿 1 仓（不判）", len(eng.positions), 1)
    check("入场K线：broker 0 单", len(eng.broker.orders), 0)
    check("入场K线：state IDLE（settle 未触发，不改 state）",
          eng._state, EngineState.IDLE)

# 2.4 多仓触发 SL（stop_loss）：一次性 FIFO 平仓
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0, stop_points=5.0)
    # SL offset = 5.0 → SL = 4545 - 5 = 4540；bar.low=4538 触发
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-2-4-A",
                       tp_offset=10.0, sl_offset=5.0)
    p1 = make_position(Side.LONG, 1, 4545.0, 3, signal_key="P15B-2-4-B",
                       tp_offset=10.0, sl_offset=5.0)
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4538.0)  # 触发 SL
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("2仓SL：簿 2（2 笔 LOCKED 锁仓，H1 落簿）", len(eng.positions), 2)
    check("2仓SL：broker 2 单", len(eng.broker.orders), 2)
    trades = eng.store.trades()
    check("2仓SL：2 条 Trade", len(trades), 2)
    keys = [t["signal_key"] for t in trades]
    check("2仓SL：FIFO 顺序 A→B", keys,
          ["P15B-2-4-A", "P15B-2-4-B"])

# 2.5 空簿 settle 无事发生
with tmp_dir() as td:
    eng = make_engine(td)
    eng.last_bar = make_bar(close=4558.0)
    eng.bars_seen = 10
    eng._settle_positions(eng.last_bar)
    check("空簿：broker 0 单", len(eng.broker.orders), 0)
    check("空簿：state IDLE", eng._state, EngineState.IDLE)


# ════════════════════════════════════════════════════════════════
# [3] _reconcile_positions 多仓版
# ════════════════════════════════════════════════════════════════
print("\n[3] _reconcile_positions 多仓版")

# 3.1 real_vol == engine_vol：无操作（仅告警 all_cleared 路径）
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=2, real_shorts=0)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-1-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-3-1-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("一致：簿仍 2 仓", len(eng.positions), 2)
    check("一致：broker 0 单（对账不下单）", len(eng.broker.orders), 0)
    trades = eng.store.trades()
    check("一致：0 条 Trade", len(trades), 0)

# 3.2 real_vol < engine_vol：FIFO 部分平（最早建仓）
with tmp_dir() as td:
    # 引擎 3 仓各 1 手 → engine_vol=3；real_longs=2 → 差 1 手 → 平最早 1 笔
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=2, real_shorts=None)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    # 故意乱序加入
    p2 = make_position(Side.LONG, 1, 4547.0, 5, signal_key="P15B-3-2-C")
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-2-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-3-2-B")
    eng.positions.add(p2)
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("部分平FIFO：簿 2 仓", len(eng.positions), 2)
    # 平掉的是最早建仓的 p0（entry_bar_seq=1）
    keys_left = sorted([p.signal_key for p in eng.positions.positions])
    check("部分平FIFO：剩 B+C（最早 A 被平）",
          keys_left, ["P15B-3-2-B", "P15B-3-2-C"])
    trades = eng.store.trades()
    check("部分平FIFO：1 条 Trade（p0）", len(trades), 1)
    check("部分平FIFO：Trade.reason == 'reconcile_external_partial'",
          trades[0]["reason"], "reconcile_external_partial")
    check("部分平FIFO：Trade.signal_key == A",
          trades[0]["signal_key"], "P15B-3-2-A")
    check("部分平FIFO：broker 0 单（对账不下单）", len(eng.broker.orders), 0)
    # 注：_reconcile_positions 不主动改 state（与 E3.1 一致）——
    # 若簿仍有仓位，state 维持原值（初始 IDLE → 仍 IDLE）。
    # _close_positions 才设 EXITING。

# 3.3 real_vol == 0：清空同侧全部仓位
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=0, real_shorts=None)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-3-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-3-3-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("real=0：簿 0 仓", len(eng.positions), 0)
    check("real=0：state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("real=0：2 条 Trade（p0 + p1）", len(trades), 2)
    keys = sorted([t["signal_key"] for t in trades])
    check("real=0：FIFO 顺序 A→B", keys,
          ["P15B-3-3-A", "P15B-3-3-B"])
    summary_evs = read_events(eng, kinds={"position_externally_closed_summary"})
    check("real=0：summary 事件", len(summary_evs) >= 1, True)

# 3.4 real_vol > engine_vol：仅告警
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=5, real_shorts=None)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-4-A")
    eng.positions.add(p0)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("real>engine：簿仍 1 仓", len(eng.positions), 1)
    check("real>engine：broker 0 单", len(eng.broker.orders), 0)
    trades = eng.store.trades()
    check("real>engine：0 条 Trade（不接管）", len(trades), 0)
    mismatch_evs = read_events(eng, kinds={"position_mismatch"})
    check("real>engine：mismatch 事件", len(mismatch_evs), 1)

# 3.5 broker 不支持 real_position：跳过
with tmp_dir() as td:
    # DryRunBroker 默认 real_position 返回 None → 跳过
    eng = make_engine(td, max_open_positions=3)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-5-A")
    eng.positions.add(p0)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_positions()
    check("不支持：簿仍 1 仓", len(eng.positions), 1)
    check("不支持：broker 0 单", len(eng.broker.orders), 0)
    trades = eng.store.trades()
    check("不支持：0 条 Trade", len(trades), 0)

# 3.6 入场 K 线跳过（min_bars_held < 1 → skip）
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=0, real_shorts=None)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-3-6-A")
    eng.positions.add(p0)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 1  # 入场即下一根，bars_held=0 → < 1 跳过
    eng._reconcile_positions()
    check("入场K线：簿仍 1 仓", len(eng.positions), 1)
    trades = eng.store.trades()
    check("入场K线：0 条 Trade", len(trades), 0)


# ════════════════════════════════════════════════════════════════
# [4] on_bar 全流程：多仓 + on_bar → 对账 → settle → FIFO 平仓
# ════════════════════════════════════════════════════════════════
print("\n[4] on_bar 全流程")

# 4.1 on_bar 触发 settle + reconcile
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-4-1-A")
    p1 = make_position(Side.LONG, 1, 4545.0, 3, signal_key="P15B-4-1-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    bar = make_bar(close=4558.0)  # 触发 TP
    eng.on_bar(bar)
    check("on_bar TP：簿 2 仓（2 笔 LOCKED 锁仓，H1 落簿）", len(eng.positions), 2)
    check("on_bar TP：broker 2 单", len(eng.broker.orders), 2)
    trades = eng.store.trades()
    check("on_bar TP：2 条 Trade", len(trades), 2)
    keys = [t["signal_key"] for t in trades]
    check("on_bar TP：FIFO 顺序 A→B", keys,
          ["P15B-4-1-A", "P15B-4-1-B"])
    check("on_bar TP：state IDLE", eng._state, EngineState.IDLE)


# ════════════════════════════════════════════════════════════════
# [5] 兼容壳保留（_close_position / _settle_position / _reconcile_position）
# ════════════════════════════════════════════════════════════════
print("\n[5] 兼容壳保留")

# 5.1 _close_position 单仓兼容壳
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-1")
    eng.positions.add(pos)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._close_position("manual", 4555.0, eng.last_bar, signal_key="P15B-5-1")
    check("兼容_close：簿 1 仓（LOCKED 锁仓，H1 落簿）", len(eng.positions), 1)
    check("兼容_close：state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("兼容_close：1 条 Trade", len(trades), 1)
    check("兼容_close：Trade.signal_key 对应",
          trades[0]["signal_key"], "P15B-5-1")

# 5.2 _settle_position 单仓兼容壳（forward 到 _settle_positions）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, tp_points=10.0)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-2")
    eng.positions.add(pos)
    eng.last_bar = make_bar(close=4558.0)  # TP 触发
    eng.bars_seen = 10
    eng._settle_position(eng.last_bar)
    check("兼容_settle：簿 1（LOCKED 锁仓，H1 落簿）", len(eng.positions), 1)
    check("兼容_settle：state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("兼容_settle：1 条 Trade", len(trades), 1)

# 5.3 _reconcile_position 单仓兼容壳
with tmp_dir() as td:
    broker = RealPositionBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                                 real_longs=0, real_shorts=None)
    eng = make_engine(td, max_open_positions=3, broker=broker)
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-3")
    eng.positions.add(pos)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    eng._reconcile_position()
    check("兼容_reconcile：簿 0", len(eng.positions), 0)
    check("兼容_reconcile：state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("兼容_reconcile：1 条 Trade", len(trades), 1)
    check("兼容_reconcile：Trade.reason",
          trades[0]["reason"], "reconcile_external_partial")

# 5.4 兼容壳多仓行为：_close_position 在多仓时抛 PositionBookError（property 守护）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    p0 = make_position(Side.LONG, 1, 4545.0, 1, signal_key="P15B-5-4-A")
    p1 = make_position(Side.LONG, 1, 4546.0, 3, signal_key="P15B-5-4-B")
    eng.positions.add(p0)
    eng.positions.add(p1)
    eng.last_bar = make_bar(close=4555.0)
    eng.bars_seen = 10
    raised = False
    try:
        eng._close_position("manual", 4555.0, eng.last_bar)
    except Exception:
        raised = True
    check("兼容壳多仓：property 抛错（预期）", raised, True)


# ════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P15b 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
