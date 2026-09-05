# -*- coding: utf-8 -*-
"""
P14a Phase E3.1: cfg.risk.max_open_positions 配置化 + PositionBook 多仓容器
=========================================================================
E3.1 的范围（本次提交）
  ① cfg.risk.max_open_positions 字段（默认=1，向后兼容）
  ② PositionBook.__init__(max=N) / set_max(N) / max_positions property
  ③ PositionBook 真支持多仓（add 在 len<max 时成功，>max 时抛错）
  ④ legacy_single 在多仓时仍抛守护错（E3.3 之前不允许单仓 API 操作多仓）
  ⑤ set_legacy 保留 E1 行为：max 不论，整簿替换
  ⑥ replace_with：引擎 _restore 内部用；persisted 数据超出 cfg max 时截断 + 写 truncated
  ⑦ engine 构造时 cfg.risk.max_open_positions 传到 PositionBook
  ⑧ _restore 用 cfg 上限 + 写 positions_truncated_on_restore warning（若有截断）

E3.2 / E3.3 在 E3.1 容器基础上扩展（sizer batch + settle/close loop）

硬性要求
  · 默认 max_open_positions=1 → 所有现存测试（P5..P13）零行为变化
  · 测试显式传 max_open_positions=N 才能进入多仓路径
  · 现有所有单仓断言（legacy_single / set_legacy / etc）继续通过
  · 多仓 throw 守护（legacy_single + set_legacy 多仓覆盖）继续通过

跑法：python tests/test_p14a_posbook_cfg.py
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


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p14a_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig, RiskConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.position_book import PositionBook, PositionBookError  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    EntryMode, ExitPlan, Position, Side,
)

_PASS = 0
_FAIL = 0


def check(name: str, actual, expected) -> None:
    global _PASS, _FAIL
    ok = actual == expected
    if ok:
        _PASS += 1
        print("✓ {}".format(name))
    else:
        _FAIL += 1
        print("✗ {}  -> got={!r} expected={!r}".format(name, actual, expected))


def check_raises(name: str, fn, exc_type) -> None:
    global _PASS, _FAIL
    try:
        fn()
        _FAIL += 1
        print("✗ {}  -> no exception raised (expected {})".format(name, exc_type.__name__))
    except exc_type:
        _PASS += 1
        print("✓ {}".format(name))
    except Exception as e:
        _FAIL += 1
        print("✗ {}  -> got {} (expected {})".format(name, type(e).__name__, exc_type.__name__))


def make_pos(side: Side, entry_price: float = 4500.0, vol: int = 1,
             signal_key: str = "test_key") -> Position:
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:30:00",
        entry_bar_seq=10, entry_bar_ts=4000,
        signal_key=signal_key, open_order_id="dry_run-test",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 5.0, tp_price=None, params={}),
        entry_mode=EntryMode.OPEN_FIRST,
    )


# ════════════════════════════════════════════════════════════════
# [1] cfg.risk.max_open_positions 字段读取与默认值
# ════════════════════════════════════════════════════════════════
print("\n[1] cfg.risk.max_open_positions 字段读取与默认值")
risk = RiskConfig()
check("RiskConfig() 默认 max_open_positions == 1（向后兼容）",
      risk.max_open_positions, 1)

risk2 = RiskConfig(max_open_positions=3)
check("RiskConfig(max_open_positions=3) 显式赋值 ok",
      risk2.max_open_positions, 3)

risk3 = RiskConfig(max_open_positions=5)
check("RiskConfig(max_open_positions=5) 显式赋值 ok",
      risk3.max_open_positions, 5)

# 字段在 known fields 内（from_dict 不会丢）
risk4 = RiskConfig.from_dict({"max_open_positions": 7, "max_volume": 2})
check("RiskConfig.from_dict({max_open_positions:7, max_volume:2}) → 7",
      risk4.max_open_positions, 7)
check("RiskConfig.from_dict 不影响 max_volume",
      risk4.max_volume, 2)

# DEFAULT_CONFIG 中也要有 max_open_positions（默认=1）
cfg0 = GatewayConfig.from_dict(DEFAULT_CONFIG)
check("DEFAULT_CONFIG.risk.max_open_positions == 1",
      cfg0.risk.max_open_positions, 1)


# ════════════════════════════════════════════════════════════════
# [2] PositionBook.__init__(max=N) / set_max / max_positions
# ════════════════════════════════════════════════════════════════
print("\n[2] PositionBook.__init__(max=N) / set_max / max_positions")

# 默认 max=1
b1 = PositionBook()
check("PositionBook() 默认 max=1", b1.max_positions, 1)

# 显式 max=5
b5 = PositionBook(max_positions=5)
check("PositionBook(max=5).max_positions == 5", b5.max_positions, 5)

# max<1 应抛错
check_raises("PositionBook(max=0) 抛错", lambda: PositionBook(max_positions=0),
             PositionBookError)
check_raises("PositionBook(max=-1) 抛错", lambda: PositionBook(max_positions=-1),
             PositionBookError)

# set_max 动态调整
b1.set_max(3)
check("set_max(3) 后 max_positions == 3", b1.max_positions, 3)

# set_max 不能缩到现存数以下
b1.add(make_pos(Side.LONG))
b1.add(make_pos(Side.SHORT))
check("add 第二笔后 __len__ == 2", len(b1), 2)
check_raises("set_max(0) 不能缩（簿非空）", lambda: b1.set_max(0),
             PositionBookError)
check_raises("set_max(1) 不能缩（2>1）", lambda: b1.set_max(1),
             PositionBookError)

# set_max 等量放小允许（len=2, max=2 → max=2 不抛）
b1.set_max(2)
check("set_max(2) 等量（簿满）允许", b1.max_positions, 2)

# set_max 放大允许
b1.set_max(5)
check("set_max(5) 放大允许", b1.max_positions, 5)

# set_max 不能缩到 < 1
check_raises("set_max(0) 抛错", lambda: b1.set_max(0),
             PositionBookError)


# ════════════════════════════════════════════════════════════════
# [3] PositionBook.add 多仓行为（max=1 仍 throw, max=N 可累加）
# ════════════════════════════════════════════════════════════════
print("\n[3] PositionBook.add 多仓行为")

# max=1: add 第二笔 throw
b_max1 = PositionBook(max_positions=1)
b_max1.add(make_pos(Side.LONG, signal_key="K1"))
check_raises("max=1 时 add 第二笔 throw",
             lambda: b_max1.add(make_pos(Side.SHORT, signal_key="K2")),
             PositionBookError)

# max=3: 可累加 3 笔
b_max3 = PositionBook(max_positions=3)
p1 = make_pos(Side.LONG, signal_key="K1")
p2 = make_pos(Side.SHORT, signal_key="K2")
p3 = make_pos(Side.LONG, signal_key="K3")
b_max3.add(p1)
b_max3.add(p2)
b_max3.add(p3)
check("max=3 三笔 add 后 __len__ == 3", len(b_max3), 3)
check("max=3 顺序 FIFO：positions[0] == p1", b_max3.positions[0] is p1, True)
check("max=3 顺序 FIFO：positions[2] == p3", b_max3.positions[2] is p3, True)

# max=3: 第 4 笔 throw
check_raises("max=3 add 第 4 笔 throw",
             lambda: b_max3.add(make_pos(Side.SHORT, signal_key="K4")),
             PositionBookError)


# ════════════════════════════════════════════════════════════════
# [4] legacy_single 多仓仍抛守护（E3.3 之前不允许）
# ════════════════════════════════════════════════════════════════
print("\n[4] legacy_single 多仓仍抛守护")

# max=1 + 单仓：正常
b_solo = PositionBook(max_positions=1)
b_solo.add(make_pos(Side.LONG, signal_key="K1"))
check("max=1 单仓：legacy_single() 返回那笔",
      b_solo.legacy_single() is b_solo.positions[0], True)

# max=3 + 多仓：legacy_single 必须 throw（E3.3 之前不允许单仓 API 操作多仓）
b_multi = PositionBook(max_positions=3)
b_multi.add(make_pos(Side.LONG, signal_key="K1"))
b_multi.add(make_pos(Side.SHORT, signal_key="K2"))
check("多仓 len == 2", len(b_multi), 2)
check_raises("多仓 legacy_single() 抛守护错",
             b_multi.legacy_single, PositionBookError)


# ════════════════════════════════════════════════════════════════
# [5] set_legacy 保留 E1 行为：max 不论，整簿替换
# ════════════════════════════════════════════════════════════════
print("\n[5] set_legacy 保留 E1 行为（兼容层）")

# max=1: 单仓时正常替换
b_solo = PositionBook(max_positions=1)
b_solo.set_legacy(make_pos(Side.LONG, signal_key="A"))
b_solo.set_legacy(make_pos(Side.SHORT, signal_key="B"))
check("max=1 set_legacy 替换整簿", b_solo.legacy_single().signal_key, "B")

# max>1: 多仓时 set_legacy 仍允许（E1 兼容，doc 警告"会丢仓"）
b_multi = PositionBook(max_positions=3)
b_multi.add(make_pos(Side.LONG, signal_key="A"))
b_multi.add(make_pos(Side.SHORT, signal_key="B"))
b_multi.set_legacy(make_pos(Side.LONG, signal_key="C"))
check("max>1 多仓 set_legacy 仍整簿替换（E1 兼容）",
      len(b_multi), 1)
check("整簿替换后唯一仓位 signal_key == 'C'",
      b_multi.legacy_single().signal_key, "C")


# ════════════════════════════════════════════════════════════════
# [6] replace_with 引擎内部用：persisted > cfg.max 时截断
# ════════════════════════════════════════════════════════════════
print("\n[6] replace_with 持久化数据 > cfg max 时截断")

# 簿 max=1，从 max=3 的另一簿 swap（典型场景：cfg 缩窄）
src = PositionBook(max_positions=3)
src.add(make_pos(Side.LONG, signal_key="A"))
src.add(make_pos(Side.LONG, signal_key="B"))
src.add(make_pos(Side.LONG, signal_key="C"))

dst = PositionBook(max_positions=1)
dst.replace_with(src)
check("replace_with: 截断到 max=1", len(dst), 1)
check("replace_with: 保留前 max 个（FIFO）", dst.positions[0].signal_key, "A")
check("replace_with: truncated 字段含后 2 个",
      len(dst.truncated_on_restore), 2)
truncated_keys = sorted([p.signal_key for p in dst.truncated_on_restore])
check("replace_with: truncated 是 B + C（FIFO 截断后丢弃）",
      truncated_keys, ["B", "C"])

# 簿 max=3，从 max=3 swap：全部接受
src2 = PositionBook(max_positions=3)
src2.add(make_pos(Side.LONG, signal_key="X"))
src2.add(make_pos(Side.LONG, signal_key="Y"))
src2.add(make_pos(Side.LONG, signal_key="Z"))

dst2 = PositionBook(max_positions=3)
dst2.replace_with(src2)
check("replace_with 等量 swap：3 笔全恢复", len(dst2), 3)
check("replace_with 等量 swap：无截断",
      len(dst2.truncated_on_restore), 0)

# 簿 max=5，从 max=3 swap：3 笔全接受，无截断
dst3 = PositionBook(max_positions=5)
dst3.replace_with(src2)
check("replace_with 放大 swap：3 笔全接受", len(dst3), 3)
check("replace_with 放大 swap：无截断",
      len(dst3.truncated_on_restore), 0)


# ════════════════════════════════════════════════════════════════
# [7] engine 构造时 cfg.risk.max_open_positions → PositionBook
# ════════════════════════════════════════════════════════════════
print("\n[7] engine 构造时 cfg 化传递")

with tmp_dir() as tmp:
    # 默认 cfg: max=1
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = cfg.instrument
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store = Store(os.path.join(tmp, "state.db"))
    store.wipe_runtime_state()
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    check("默认 cfg.max=1 → engine.positions.max_positions == 1",
          engine.positions.max_positions, 1)

    # cfg.max=3
    cfg.risk.max_open_positions = 3
    store2 = Store(os.path.join(tmp, "state2.db"))
    store2.wipe_runtime_state()
    ev2 = EventLog(os.path.join(tmp, "events2.jsonl"), echo=False, echo_kinds=None)
    engine2 = GatewayEngine(cfg, broker, entry, exitp, store2, ev2)
    check("cfg.max=3 → engine.positions.max_positions == 3",
          engine2.positions.max_positions, 3)


# ════════════════════════════════════════════════════════════════
# [8] _restore 截断时 ev 写 warning
# ════════════════════════════════════════════════════════════════
print("\n[8] _restore 截断时 ev 写 warning")

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4500.0, "entry_at": "2026-09-01 09:00",
         "entry_bar_ts": 4000, "entry_bar_seq": 10,
         "signal_key": "PA", "open_order_id": "o1",
         "exit_plan": {"name": "x", "stop_price": 4490.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4505.0, "entry_at": "2026-09-01 09:01",
         "entry_bar_ts": 4020, "entry_bar_seq": 11,
         "signal_key": "PB", "open_order_id": "o2",
         "exit_plan": {"name": "x", "stop_price": 4495.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4510.0, "entry_at": "2026-09-01 09:02",
         "entry_bar_ts": 4040, "entry_bar_seq": 12,
         "signal_key": "PC", "open_order_id": "o3",
         "exit_plan": {"name": "x", "stop_price": 4500.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
    ])
    store.close()

    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    check("默认 cfg.max=1", cfg.risk.max_open_positions, 1)
    spec = cfg.instrument
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store2 = Store(os.path.join(tmp, "state.db"))
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store2, ev)

    check("persisted=3 但 cfg.max=1 → engine.positions.__len__ == 1",
          len(engine.positions), 1)
    check("truncated_on_restore 含 2 个被丢弃",
          len(engine.positions.truncated_on_restore), 2)

    ev.flush()
    events_log = os.path.join(tmp, "events.jsonl")
    has_warning = False
    if os.path.isfile(events_log):
        with open(events_log, "r", encoding="utf-8") as fh:
            for line in fh:
                if "positions_truncated_on_restore" in line:
                    has_warning = True
                    break
    check("ev 写了 positions_truncated_on_restore warning",
          has_warning, True)


# ════════════════════════════════════════════════════════════════
# [9] 默认 cfg 不变：所有现存测试场景行为不变（端到端冒烟）
# ════════════════════════════════════════════════════════════════
print("\n[9] 默认 cfg 不变：端到端冒烟（一开一平）")

with tmp_dir() as tmp:
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = cfg.instrument
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store = Store(os.path.join(tmp, "state.db"))
    store.wipe_runtime_state()
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)

    # 制造一个 bar + 信号
    from tg.types import Bar, Signal, now_cn
    bar = Bar(timestamp=4000, date="2026-09-02 09:30",
              open=4500, high=4505, low=4498, close=4503, vol=100)
    engine.on_bar(bar)

    sig = Signal(key="K_open", date="2026-09-02 09:30",
                 timestamp=4000, bsp_type="1", is_buy=True,
                 price=4503, high=4505, low=4498,
                 symbol="CFFEX.IF2609", freq="5m", extra={})
    engine.on_signal(sig)

    check("开仓后 positions.__len__ == 1", len(engine.positions), 1)
    check("开仓后 max_positions 仍 == 1（默认 cfg）",
          engine.positions.max_positions, 1)
    check("开仓后 _state == IN_TRADE",
          engine._state.value, "in_trade")

    # 反向信号触发平仓
    sig2 = Signal(key="K_close", date="2026-09-02 09:35",
                  timestamp=4300, bsp_type="2", is_buy=False,
                  price=4510, high=4512, low=4508,
                  symbol="CFFEX.IF2609", freq="5m", extra={})
    bar2 = Bar(timestamp=4300, date="2026-09-02 09:35",
               open=4505, high=4512, low=4505, close=4510, vol=100)
    engine.on_bar(bar2)
    engine.on_signal(sig2)

    check("反向信号后 positions 全为 LOCKED 锁仓（H1 落簿）",
          all(p.entry_mode.value == "locked" for p in engine.positions.positions), True)
    check("反向信号后 _state 回 IDLE",
          engine._state.value, "idle")
    check("反向信号后 _trade_seq == 1",
          engine._trade_seq, 1)


# ════════════════════════════════════════════════════════════════
# [10] cfg.max>1 端到端：手动 add 多笔 + 检查多仓 throw 守护
# ════════════════════════════════════════════════════════════════
print("\n[10] cfg.max=3 端到端：多仓 throw 守护")

with tmp_dir() as tmp:
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = 3
    spec = cfg.instrument
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store = Store(os.path.join(tmp, "state.db"))
    store.wipe_runtime_state()
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    check("cfg.max=3 → engine.positions.max_positions == 3",
          engine.positions.max_positions, 3)

    # 通过 property（兼容层）写入两笔——但 set_legacy 单仓语义会让第二笔覆盖第一笔
    p_a = make_pos(Side.LONG, signal_key="A")
    engine.position = p_a
    check("engine.position = p_a 后 __len__ == 1", len(engine.positions), 1)

    # 通过 book.add 直接追加（绕过 property 测试多仓 API）
    p_b = make_pos(Side.SHORT, signal_key="B")
    engine.positions.add(p_b)
    check("positions.add(p_b) 后 __len__ == 2", len(engine.positions), 2)

    check_raises("engine.position 多仓抛守护错",
                 lambda: engine.position, PositionBookError)

    # 清理后恢复单仓 API
    engine.positions.clear()
    check("clear 后 __len__ == 0", len(engine.positions), 0)
    check("清空后 engine.position = None",
          engine.position, None)
    check("清空后 _state = IDLE",
          engine._state.value, "idle")


print()
print("=" * 60)
print("P14a 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
if _FAIL:
    raise SystemExit(1)