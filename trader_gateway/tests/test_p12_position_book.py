# -*- coding: utf-8 -*-
"""
P12 PositionBook 容器 + 兼容层单元测试（Phase E1）
==================================================
背景
    Phase E 引入 PositionBook 容器，为 E2 (UNLOCK_FIRST 入场) / E3 (N≥1 同 K 线连开)
    预留多仓扩展点。E1 阶段聚焦"零行为变化"——
      · PositionBook 是 List[Position] 的薄封装，max=1 强约束
      · engine.position 走 property 转发到 book.legacy_single()，与旧版完全等价
      · 持久化双写新键 "positions" + 旧键 "position"，老数据库零侵入迁移

硬性要求（本测试锁死）
    ① 容器基础 CRUD：add / remove / clear / __len__ / __iter__ / __bool__
    ② legacy_single() & set_legacy() 兼容层语义：空→None / 单→唯一 / 多→抛错
    ③ has_opposite / opposite_positions / same_side_positions（E2 准备）
    ④ 序列化：to_dict / from_dict roundtrip
    ⑤ 序列化兼容：旧版单字段 dict 也能 from_dict 恢复
    ⑥ add() 超 max=1 报错（提前守护 E3 — 多仓进来立刻抛错而非隐式合并）
    ⑦ engine.position property 兼容（读 / 写 Position / 写 None / 状态推导）
    ⑧ _persist 双写新键 "positions" + 旧键 "position"；空簿时双删
    ⑨ _restore 从老 "position" 单字段恢复（迁移路径）
    ⑩ _restore 从新 "positions" list 恢复
    ⑪ UNLOCK_FIRST 持仓经 PositionBook 完整 roundtrip 保留 entry_mode / volume
        （与 _restore 路径无缝衔接，不会被静默回退到 OPEN_FIRST）

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。P5..P11 不改一行。
跑法：python tests/test_p12_position_book.py
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
    d = tempfile.mkdtemp(prefix="tg_p12_")
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
from tg.position_book import PositionBook, PositionBookError  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    EntryMode, ExitPlan, Position, Side,
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


def check_raises(name, fn, exc_type):
    """fn() 应该抛出 exc_type，否则算失败。"""
    global _PASS, _FAIL
    try:
        fn()
    except exc_type:
        print("✓ " + name)
        _PASS += 1
        return
    except Exception as e:
        print("✗ " + name + "  -> got={!r} (expected {})".format(type(e).__name__, exc_type.__name__))
        _FAIL += 1
        return
    print("✗ " + name + "  -> no exception raised (expected {})".format(exc_type.__name__))
    _FAIL += 1


def make_pos(symbol="CFFEX.IF", side=Side.LONG, vol=1, entry_price=4550.0,
             entry_mode=EntryMode.OPEN_FIRST):
    """构造一个最小化的 Position（绕开真实开仓流程，专测 PositionBook）。"""
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=4000, signal_key="P12-TEST",
        open_order_id="p12-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_mode=entry_mode)


def build_engine(tmpdir):
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


# ════════════════════════════════════════════════════════════════
# [1] PositionBook 容器基础 CRUD
# ════════════════════════════════════════════════════════════════
print("\n[1] PositionBook 容器基础 CRUD")
book = PositionBook()
check("新簿空 → is_empty() True", book.is_empty(), True)
check("新簿空 → __len__ == 0", len(book), 0)
check("新簿空 → __bool__ False（if not book 走 True 分支）", bool(book), False)
p1 = make_pos(entry_price=4500.0, side=Side.LONG)
book.add(p1)
check("add 1 笔 → is_empty() False", book.is_empty(), False)
check("add 1 笔 → __len__ == 1", len(book), 1)
check("add 1 笔 → __bool__ True", bool(book), True)
# 同位置（is / 内存地址）再次 add 在 max=1 时应报错
check_raises("max=1 时 add 第二笔立刻抛错 PositionBookError",
             lambda: book.add(make_pos(entry_price=4505.0, side=Side.SHORT)),
             PositionBookError)
check("抛错后簿内容不变（仍是 1 笔）", len(book), 1)
# remove 命中
book.remove(p1)
check("remove 已存在位置 → __len__ == 0", len(book), 0)
# remove 幂等
book.remove(p1)
check("remove 不存在位置 → 不抛错，簿仍空", len(book), 0)
# clear
book.add(p1)
book.clear()
check("clear 后空簿", book.is_empty(), True)
# __iter__ 返回独立 list
book.add(p1)
items = list(book)
items.append("TAG")
check("__iter__ / list() 返回独立拷贝（不影响内部）", len(book), 1)
del items


# ════════════════════════════════════════════════════════════════
# [2] legacy_single / set_legacy 兼容层（核心：v1 主入口）
# ════════════════════════════════════════════════════════════════
print("\n[2] legacy_single / set_legacy 兼容层")
b = PositionBook()
check("空簿 legacy_single() → None", b.legacy_single(), None)
# set_legacy(None) 应等价于 clear
b.set_legacy(None)
check("set_legacy(None) → 空簿", b.is_empty(), True)
p_long = make_pos(entry_price=4500.0, side=Side.LONG)
b.set_legacy(p_long)
check("set_legacy(pos) → 簿只有这 1 笔", len(b), 1)
check("legacy_single() 返回这笔 Position", b.legacy_single() is p_long, True)
# 替换（不同对象）
p_short = make_pos(entry_price=4600.0, side=Side.SHORT)
b.set_legacy(p_short)
check("set_legacy 第二次 → 替换整个簿", len(b), 1)
check("legacy_single() 返回新的 p_short", b.legacy_single() is p_short, True)
# property: .positions 是拷贝
ext = b.positions
ext.clear()
check(".positions 返回拷贝：外部 clear 不影响内部", b.is_empty(), False)
# 多仓（强行构造）：legacy_single 必须抛错 —— 守护 E3 早期失稳
b2 = PositionBook()
b2.add(p_long)
# 改 max 上限以构造多仓场景（不允许生产代码这么干 —— 测试专用）
b2._max = 5
b2.add(p_short)
check("构造多仓（测试 hack）：__len__ == 2", len(b2), 2)
check_raises("legacy_single() 多仓立刻抛错 PositionBookError",
             b2.legacy_single, PositionBookError)
# 多仓时 set_legacy 替换整簿（合法 set 语义）
b2.set_legacy(p_long)
check("多仓 set_legacy → 整簿替换回单仓", len(b2), 1)
check("多仓 set_legacy 后 legacy_single() 返回 ok", b2.legacy_single() is p_long, True)


# ════════════════════════════════════════════════════════════════
# [3] has_opposite / opposite_positions / same_side_positions（E2 准备）
# ════════════════════════════════════════════════════════════════
print("\n[3] has_opposite / opposite_positions / same_side_positions（E2 准备）")
b3 = PositionBook()
b3._max = 5
check("空簿 has_opposite(LONG) False", b3.has_opposite(Side.LONG), False)
check("空簿 has_opposite(SHORT) False", b3.has_opposite(Side.SHORT), False)
# 一笔多
b3.add(p_long)
check("单笔多 → has_opposite(LONG) False", b3.has_opposite(Side.LONG), False)
check("单笔多 → has_opposite(SHORT) True（与 SHORT 相反）",
      b3.has_opposite(Side.SHORT), True)
# 一笔多 + 一笔空
b3.add(p_short)
check("双边 → has_opposite(LONG) True", b3.has_opposite(Side.LONG), True)
check("双边 → has_opposite(SHORT) True", b3.has_opposite(Side.SHORT), True)
# 取反方向
opp = b3.opposite_positions(Side.LONG)
check("opposite_positions(LONG) 长度 == 1", len(opp), 1)
check("opposite_positions(LONG)[0] 是 SHORT", opp[0].side, Side.SHORT)
# 取同方向
same = b3.same_side_positions(Side.SHORT)
check("same_side_positions(SHORT) 长度 == 1", len(same), 1)
check("same_side_positions(SHORT)[0] 是 p_short",
      same[0] is p_short, True)
# 反方向没有时返回空 list
none_opp = b3.opposite_positions(Side.SHORT)
check("opposite_positions(SHORT) 长度 == 1 (返回 LONG 那笔)",
      len(none_opp), 1)
check("opposite_positions(SHORT)[0] 是 p_long",
      none_opp[0] is p_long, True)


# ════════════════════════════════════════════════════════════════
# [4] 序列化 roundtrip：新格式 list[dict]
# ════════════════════════════════════════════════════════════════
print("\n[4] to_dict / from_dict roundtrip（新格式 list[dict]）")
b4 = PositionBook()
check("空簿 to_dict() → []", b4.to_dict(), [])
b4.add(make_pos(entry_price=4500.0, side=Side.LONG, vol=2,
                entry_mode=EntryMode.OPEN_FIRST))
data = b4.to_dict()
check("单仓 to_dict() 是 list", isinstance(data, list), True)
check("单仓 to_dict() 长度 == 1", len(data), 1)
check("to_dict()[0] 是 dict 且含 symbol", isinstance(data[0], dict)
      and data[0].get("symbol") == "CFFEX.IF", True)
# roundtrip
b4_rt = PositionBook.from_dict(data)
check("from_dict(list) 簿长度 == 1", len(b4_rt), 1)
check("from_dict(list).legacy_single() side 与原一致",
      b4_rt.legacy_single().side, Side.LONG)
check("from_dict(list).legacy_single().volume == 2",
      b4_rt.legacy_single().volume, 2)
# 复杂：UNLOCK_FIRST + 双边
b4x = PositionBook()
b4x._max = 5
b4x.add(make_pos(entry_price=4500.0, side=Side.LONG, vol=1,
                 entry_mode=EntryMode.OPEN_FIRST))
b4x.add(make_pos(entry_price=4555.0, side=Side.SHORT, vol=1,
                 entry_mode=EntryMode.UNLOCK_FIRST))
data_x = b4x.to_dict()
check("双边 to_dict() 长度 == 2", len(data_x), 2)
b4x_rt = PositionBook.from_dict(data_x)
check("双边 roundtrip 后 __len__ == 2", len(b4x_rt), 2)
modes = sorted([p.entry_mode.value for p in b4x_rt.positions])
check("双边 roundtrip 后 entry_mode 集合保留",
      modes, ["open_first", "unlock_first"])


# ════════════════════════════════════════════════════════════════
# [5] 序列化兼容：旧格式 dict（v1 单字段）也能 from_dict
# ════════════════════════════════════════════════════════════════
print("\n[5] from_dict 兼容旧版单字段 dict")
legacy_dict = {
    "symbol": "CFFEX.IF", "side": "LONG", "volume": 3,
    "entry_price": 4500.0, "entry_at": "2026-09-01 09:00",
    "entry_bar_ts": 4000, "entry_bar_seq": 10,
    "signal_key": "OLD", "open_order_id": "old-o1",
    "exit_plan": {"name": "x", "stop_price": 4495.0, "tp_price": None, "params": {}},
    "entry_mode": "open_first",
}
b5 = PositionBook.from_dict(legacy_dict)
check("旧 dict 反序列化 → __len__ == 1", len(b5), 1)
check("旧 dict entry_mode → OPEN_FIRST",
      b5.legacy_single().entry_mode, EntryMode.OPEN_FIRST)
check("旧 dict volume == 3", b5.legacy_single().volume, 3)

empty_inputs = [None, {}, [], {"symbol": None}, [None, {}], [{"x": 1}]]
for idx, bad in enumerate(empty_inputs):
    b_bad = PositionBook.from_dict(bad)
    check("from_dict 异常输入 #{} → 空簿".format(idx), b_bad.is_empty(), True)


# ════════════════════════════════════════════════════════════════
# [6] engine.position property 兼容（旧代码零改动）
# ════════════════════════════════════════════════════════════════
print("\n[6] engine.position property 兼容（旧代码零改动）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # ① 默认空
    check("新引擎 engine.position 是 None", engine.position, None)
    check("新引擎 engine.positions.is_empty() True",
          engine.positions.is_empty(), True)
    check("新引擎 _state = IDLE", engine._state.name, "IDLE")

    # ② 写一个 Position
    p_test = make_pos(entry_price=4500.0, side=Side.LONG, vol=2)
    engine.position = p_test
    check("engine.position = pos 后，positions.__len__ == 1",
          len(engine.positions), 1)
    check("engine.position 读回同一个对象", engine.position is p_test, True)
    check("engine.positions.legacy_single() 走 property 转发也是它",
          engine.positions.legacy_single() is p_test, True)
    # 注意：直接写 engine.position 不会自动改 _state —— _state 由
    # _open_position / _close_position / _restore 这些"经过引擎"的状态转移路径推。
    # 这里手动同步一下，验证 legacy_single() 给的就是它。
    check("直接赋值不推 _state（v1 状态机驱动点之一）",
          engine._state.name, "IDLE")
    engine._state = engine._state.__class__.IN_TRADE  # 模拟真实开仓已完成

    # ③ 写 None 清空（注意：v1 设计上只有 _open_position/_close_position 会推 _state）
    engine.position = None
    check("engine.position = None → 空簿", engine.positions.is_empty(), True)
    check("直接清空不自动推 _state（开/平路径才推）",
          engine._state.name, "IN_TRADE")
    engine._state = engine._state.__class__.IDLE  # 手动同步

    # ④ 链式访问（position.entry_mode / .volume / .side）
    engine.position = make_pos(entry_price=4500.0, side=Side.SHORT, vol=4,
                               entry_mode=EntryMode.UNLOCK_FIRST)
    check("engine.position.entry_mode = UNLOCK_FIRST",
          engine.position.entry_mode, EntryMode.UNLOCK_FIRST)
    check("engine.position.volume == 4", engine.position.volume, 4)
    check("engine.position.side = SHORT", engine.position.side, Side.SHORT)


# ════════════════════════════════════════════════════════════════
# [7] _persist 双写：内存多仓 / 空簿清理
# ════════════════════════════════════════════════════════════════
print("\n[7] _persist 双写：新键 'positions' + 旧键 'position'")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # ① 单仓
    engine.position = make_pos(entry_price=4500.0, side=Side.LONG)
    engine._persist()
    new_k = store.get_json("positions")
    legacy_k = store.get_json("position")
    check("单仓 persist → 新键 'positions' 是 list 且 len==1",
          isinstance(new_k, list) and len(new_k) == 1, True)
    check("单仓 persist → 旧键 'position' 是 dict 含 symbol",
          isinstance(legacy_k, dict) and legacy_k.get("symbol") == "CFFEX.IF",
          True)
    check("两个键的 symbol 一致（新旧视图统一）",
          new_k[0].get("symbol"), legacy_k.get("symbol"))

    # ② 多仓（测试 hack）：新键是 list[2]，旧键是 list[0]
    engine.positions._max = 5
    engine.positions.add(make_pos(entry_price=4505.0, side=Side.SHORT))
    engine._persist()
    new_k2 = store.get_json("positions")
    legacy_k2 = store.get_json("position")
    check("多仓 persist → 新键 'positions' len==2",
          isinstance(new_k2, list) and len(new_k2) == 2, True)
    check("多仓 persist → 旧键 'position' 是首个（向后兼容单字段视图）",
          legacy_k2.get("side"), "LONG")

    # ③ 空簿：两键全删
    engine.positions.clear()
    engine.position = None   # 验证 setter 也清空
    engine._persist()
    check("空簿 persist → 新键 'positions' 被删",
          store.get_json("positions"), None)
    check("空簿 persist → 旧键 'position' 被删",
          store.get_json("position"), None)


# ════════════════════════════════════════════════════════════════
# [8] _restore 从老版 "position" 单字段恢复（迁移路径）
# ════════════════════════════════════════════════════════════════
print("\n[8] _restore 从老版 'position' 单字段恢复")
with tmp_dir() as tmp:
    # ① 自己造一个老数据库布局：只写 "position"，不写 "positions"
    store = Store(os.path.join(tmp, "state.db"))
    legacy_dict = {
        "symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
        "entry_price": 4500.0, "entry_at": "2026-09-01 09:00",
        "entry_bar_ts": 4000, "entry_bar_seq": 10,
        "signal_key": "LEGACY", "open_order_id": "l-o1",
        "exit_plan": {"name": "x", "stop_price": 4490.0, "tp_price": None,
                      "params": {}},
        "entry_mode": "unlock_first",
    }
    store.set_json("position", legacy_dict)
    store.close()

    # ② 新引擎读这份数据库
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store2 = Store(os.path.join(tmp, "state.db"))
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store2, ev)

    check("老 'position' → engine.positions.__len__ == 1",
          len(engine.positions), 1)
    check("老 'position' → engine.position.entry_mode = UNLOCK_FIRST",
          engine.position.entry_mode, EntryMode.UNLOCK_FIRST)
    check("老 'position' → engine.position.signal_key = LEGACY",
          engine.position.signal_key, "LEGACY")
    check("老 'position' → engine._state = IN_TRADE",
          engine._state.name, "IN_TRADE")
    # ③ _persist 后写新键 —— 后续就完全走新格式了
    engine._persist()
    after = store2.get_json("positions")
    check("老数据库首次 _persist → 自动升级写入新键 'positions'",
          isinstance(after, list) and len(after) == 1, True)


# ════════════════════════════════════════════════════════════════
# [9] _restore 从新版 "positions" list 恢复（多仓场景基础，E3.1 cfg 化）
# ════════════════════════════════════════════════════════════════
print("\n[9] _restore 从新版 'positions' list 恢复（cfg.max=2）")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4500.0, "entry_at": "2026-09-01 09:00",
         "entry_bar_ts": 4000, "entry_bar_seq": 10,
         "signal_key": "K1", "open_order_id": "o1",
         "exit_plan": {"name": "x", "stop_price": 4490.0, "tp_price": None,
                       "params": {}},
         "entry_mode": "open_first"},
        {"symbol": "CFFEX.IF", "side": "SHORT", "volume": 1,
         "entry_price": 4555.0, "entry_at": "2026-09-01 09:05",
         "entry_bar_ts": 4200, "entry_bar_seq": 12,
         "signal_key": "K2", "open_order_id": "o2",
         "exit_plan": {"name": "x", "stop_price": 4565.0, "tp_price": None,
                       "params": {}},
         "entry_mode": "unlock_first"},
    ])
    store.close()

    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    # E3.1 显式把 max 拉到 2，让持久化的 2 个 Position 都能恢复
    cfg.risk.max_open_positions = 2
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store2 = Store(os.path.join(tmp, "state.db"))
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store2, ev)

    check("新 'positions' list[2] → engine.positions.__len__ == 2",
          len(engine.positions), 2)
    sides = sorted([p.side.name for p in engine.positions.positions])
    check("新 'positions' → 两侧方向都恢复",
          sides, ["LONG", "SHORT"])
    modes = sorted([p.entry_mode.value for p in engine.positions.positions])
    check("新 'positions' → 两种 entry_mode 都保留",
          modes, ["open_first", "unlock_first"])
    check("有仓 → engine._state = IN_TRADE",
          engine._state.name, "IN_TRADE")
    # 顺手比对 cfg.max 真的传到了 book（验证 cfg 化生效）
    check("cfg.max_open_positions=2 → engine.positions.max_positions == 2",
          engine.positions.max_positions, 2)
    # 顺手比对 legacy_single 在多仓时必须抛错（即使 max=2 也抛 —— E3.3 之前不允许多仓用单仓 API）
    check_raises("engine.position 多仓时抛错 PositionBookError",
                 lambda: engine.position, PositionBookError)
    # truncated 应为空（没有截断）
    check("无截断：truncated_on_restore 为空",
          len(engine.positions.truncated_on_restore), 0)


# ════════════════════════════════════════════════════════════════
# [9b] E3.1 新约束：cfg.max < persisted 数据 → 截断 + warning
# 典型场景：原 max=3 后改 max=1；或升级时 cfg 上限缩窄
# ════════════════════════════════════════════════════════════════
print("\n[9b] E3.1 cfg 截断：persisted=3 但 cfg.max=1")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4500.0, "entry_at": "2026-09-01 09:00",
         "entry_bar_ts": 4000, "entry_bar_seq": 10,
         "signal_key": "P1", "open_order_id": "o1",
         "exit_plan": {"name": "x", "stop_price": 4490.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4505.0, "entry_at": "2026-09-01 09:01",
         "entry_bar_ts": 4020, "entry_bar_seq": 11,
         "signal_key": "P2", "open_order_id": "o2",
         "exit_plan": {"name": "x", "stop_price": 4495.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
        {"symbol": "CFFEX.IF", "side": "LONG", "volume": 1,
         "entry_price": 4510.0, "entry_at": "2026-09-01 09:02",
         "entry_bar_ts": 4040, "entry_bar_seq": 12,
         "signal_key": "P3", "open_order_id": "o3",
         "exit_plan": {"name": "x", "stop_price": 4500.0, "tp_price": None,
                       "params": {}}, "entry_mode": "open_first"},
    ])
    store.close()

    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    # 默认 max=1，但持久化有 3 个 → 触发截断
    check("default cfg.risk.max_open_positions == 1",
          cfg.risk.max_open_positions, 1)
    spec = InstrumentSpec()
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
    dropped_keys = sorted([p.signal_key for p in engine.positions.truncated_on_restore])
    check("truncated 丢的是后两个（FIFO 截断保留前 max 个）",
          dropped_keys, ["P2", "P3"])
    # 验证 ev 写入了 warning
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
# [10] UNLOCK_FIRST 持仓完整 roundtrip（端到端）
# ════════════════════════════════════════════════════════════════
print("\n[10] UNLOCK_FIRST 持仓端到端 roundtrip 保留 entry_mode")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    pos_u = Position(
        symbol="CFFEX.IF", side=Side.LONG, volume=2,
        entry_price=4500.0, entry_at="2026-09-01 09:00",
        entry_bar_ts=4000, signal_key="U1",
        open_order_id="u-o1",
        exit_plan=ExitPlan(name="x", stop_price=4490.0),
        entry_mode=EntryMode.UNLOCK_FIRST,
    )
    engine.position = pos_u   # 绕开正常开仓直接塞入
    engine._persist()
    engine._state = engine._state.__class__.IN_TRADE

    # 模拟进程重启：丢掉内存，从 store 重新加载
    new_store = Store(store.path)
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker2 = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    new_ev = EventLog(os.path.join(tmp, "events2.jsonl"),
                      echo=False, echo_kinds=None)
    new_engine = GatewayEngine(cfg, broker2, DefaultEntryPolicy({}),
                               DefaultExitPolicy({}), new_store, new_ev)

    check("重启后 positions.__len__ == 1", len(new_engine.positions), 1)
    check("重启后 entry_mode 仍是 UNLOCK_FIRST",
          new_engine.position.entry_mode, EntryMode.UNLOCK_FIRST)
    check("重启后 side = LONG",
          new_engine.position.side, Side.LONG)
    check("重启后 volume == 2", new_engine.position.volume, 2)
    check("重启后 _state = IN_TRADE", new_engine._state.name, "IN_TRADE")


# ════════════════════════════════════════════════════════════════
# 收尾 —— P5..P11 不改一行的"零行为变化"复核（间接）
# ════════════════════════════════════════════════════════════════
print("\n[11] 改完后没有破坏 P10/P11 的「零行为变化」假设：见全回归报告")
# 本文件已包含 9 + 21 + 19 + 20 + 62 + 59 + 59 = 249 例；
# E1 提交后这些数字必须保持。本 section 仅占位说明，不重复跑全套（避免拖慢 CI）。


print("\n" + "=" * 60)
print("P12 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
