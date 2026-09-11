# -*- coding: utf-8 -*-
"""
P12 PositionBook 容器 + 兼容层单元测试
=====================================
2026-09-11 Phase 7 改写。旧版测的三件事里有两件已经不存在了：
  · `Position.origin` / `PositionOrigin.SIGNAL_OPEN|UNLOCK_UPGRADE` —— 随
    "持仓来源"概念一起删除（Phase 1-4）。仓单现在**不记出身**，只记
    symbol / side / volume / entry_price / entry_date / entry_bar_seq。
  · `cfg.risk.max_open_positions` 作为容器容量来源 —— D2 删除。
    `PositionBook.DEFAULT_MAX` 现在是 **None（不限容量）**，"资金是唯一闸门"。
  · 多仓不再需要 `_max = 5` 这种测试 hack —— 默认就是不限容量。

新口径（本测试锁死）
    [1] 容器基础 CRUD；默认不限容量；显式限容（max_positions=N）时超限报错
    [2] legacy_single() & set_legacy() 兼容层：空→None / 单→唯一 / 多→抛错
    [3] has_opposite / opposite_positions / same_side_positions
    [4] to_dict / from_dict roundtrip（新格式 list[dict]，含 entry_date）
    [5] from_dict 兼容旧版单字段 dict + 异常输入 → 空簿
    [5b] 三态判定与选仓的容器侧：net_volume / latest / oldest_opposite
    [6] engine.position property 兼容（读 / 写 Position / 写 None）
    [7] _persist 双写新键 "positions" + 旧键 "position"；空簿双删
    [8] _restore 从老 "position" 单字段恢复（+ G2 的 run 锚种子）
    [9] _restore 从新 "positions" list 恢复（双向 → LOCKED → _state=IDLE）
    [9b] 不限容量：persisted=3 → 全量恢复、不截断、无 warning
    [10] 持仓端到端 roundtrip（跨"进程重启"）
    [11] G1 旧 schema 闸门：持仓记录带 origin/lock_pair_id → 拒绝启动
    [12] 切合约隔离：restore 只加载当前 trade_symbol，persist 分片合并

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python Trading/Test/test_p12_position_book.py
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
    d = tempfile.mkdtemp(prefix="tg_p12_")
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
from Trading.Engine.PositionBook import PositionBook, PositionBookError  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, ExitPlan, Position, Side,
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


def check_raises(name, fn, exc_type):
    """fn() 应该抛出 exc_type，否则算失败。"""
    global _PASS, _FAIL
    try:
        fn()
    except exc_type:
        print("\u2713 " + name)
        _PASS += 1
        return
    except Exception as e:
        print("\u2717 " + name + "  -> got={!r} (expected {})".format(
            type(e).__name__, exc_type.__name__))
        _FAIL += 1
        return
    print("\u2717 " + name + "  -> no exception raised (expected {})".format(
        exc_type.__name__))
    _FAIL += 1


def make_pos(symbol=_SYM, side=Side.LONG, vol=1, entry_price=4550.0,
             signal_key="P12-TEST", entry_bar_seq=1, entry_date="2026-09-01"):
    """构造一个最小化的 Position（绕开真实开仓流程，专测 PositionBook）。

    2026-09-11：**不再传 origin**（字段已删）。显式给 entry_date，
    免得依赖 entry_bar_ts=4000 派生（那是序号不是真实毫秒）。
    """
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=4000, entry_bar_seq=entry_bar_seq,
        signal_key=signal_key,
        open_order_id="p12-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_date=entry_date)


def seed_run(store, side="LONG", anchor=4500.0, volume=1):
    """G2/D15：净敞口 ≠ 0 的库**必须**带 kv `run`，否则引擎拒绝启动。

    本测试大量手写持仓记录来模拟重启 —— 不是通过真实开仓路径产生的，
    所以要手动补上这份运行态记录，否则会撞上"缺少运行态记录，拒绝启动"。
    """
    store.set_json("run", {
        "side": side, "anchor": anchor, "volume": volume,
        "bar_ts": 4000, "bar_seq": 10, "signal_key": "SEED",
        "plan": {"name": "run_managed", "stop_price": 0.0,
                 "tp_price": None, "params": {}},
    })


def build_engine(tmpdir, *, store=None, ev_name="events.jsonl"):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = LayeredExitPolicy()
    st = store if store is not None else Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, ev_name), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, st, ev)
    return engine, st, broker, ev


def pos_dict(side="LONG", volume=1, entry_price=4500.0, signal_key="K",
             entry_bar_seq=10, entry_bar_ts=4000, entry_date="2026-09-01",
             symbol=_SYM):
    """手写一条**新 schema** 持仓记录（不带 origin / lock_pair_id）。"""
    return {
        "symbol": symbol, "side": side, "volume": volume,
        "entry_price": entry_price, "entry_at": "2026-09-01 09:00",
        "entry_bar_ts": entry_bar_ts, "entry_bar_seq": entry_bar_seq,
        "signal_key": signal_key, "open_order_id": "o-" + signal_key,
        "exit_plan": {"name": "x", "stop_price": entry_price - 10.0,
                      "tp_price": None, "params": {}},
        "entry_date": entry_date,
    }


# ════════════════════════════════════════════════════════════════
# [1] PositionBook 容器基础 CRUD
# ════════════════════════════════════════════════════════════════
print("\n[1] PositionBook 容器基础 CRUD")
book = PositionBook()
check("[1a] 新簿空 → is_empty() True", book.is_empty(), True)
check("[1b] 新簿空 → __len__ == 0", len(book), 0)
check("[1c] 新簿空 → __bool__ False（if not book 走 True 分支）", bool(book), False)
check("[1d] DEFAULT_MAX is None（D2：不限容量，资金是唯一闸门）",
      PositionBook.DEFAULT_MAX, None)
check("[1e] 默认簿 max_positions is None", book.max_positions, None)
p1 = make_pos(entry_price=4500.0, side=Side.LONG)
book.add(p1)
check("[1f] add 1 笔 → is_empty() False", book.is_empty(), False)
check("[1g] add 1 笔 → __len__ == 1", len(book), 1)
check("[1h] add 1 笔 → __bool__ True", bool(book), True)
# 默认不限容量 → 第二笔照收（旧版此处 max=1 会抛错，D2 后不再有静默门）
p_short = make_pos(entry_price=4600.0, side=Side.SHORT)
book.add(p_short)
check("[1i] 默认不限容量 → add 第二笔成功（旧版 max=1 会抛错）", len(book), 2)
check("[1j] 不限容量：net_volume 多空对消 = 0", book.net_volume(), 0)
# 显式限容时才报错（容器能力仍在，只是不再由 cfg 驱动）
capped = PositionBook(max_positions=1)
capped.add(p1)
check_raises("[1k] 显式 max_positions=1 时 add 第二笔抛 PositionBookError",
             lambda: capped.add(p_short), PositionBookError)
check("[1l] 抛错后簿内容不变（仍是 1 笔）", len(capped), 1)
check_raises("[1m] max_positions=0 → 构造期报错",
             lambda: PositionBook(max_positions=0), PositionBookError)
# set_max：只能放大 / 等量，不能缩到现存数以下
capped.set_max(2)
capped.add(p_short)
check("[1n] set_max(2) 后第二笔可加入（len==2）", len(capped), 2)
check_raises("[1o] set_max(1)（缩到现存数以下）→ 报错，不允许隐式丢失持仓",
             lambda: capped.set_max(1), PositionBookError)
check("[1o-2] 报错后上限未被改动（仍为 2）", capped.max_positions, 2)
capped.set_max(None)
check("[1p] set_max(None) → 恢复不限容量", capped.max_positions, None)
# remove 命中 / 幂等
capped.remove(p1)
check("[1q] remove 已存在位置 → __len__ == 1", len(capped), 1)
capped.remove(p1)
check("[1r] remove 不存在位置 → 不抛错，簿仍 1 笔", len(capped), 1)
# clear
capped.clear()
check("[1s] clear 后空簿", capped.is_empty(), True)
# __iter__ 返回独立 list
capped.add(p1)
items = list(capped)
items.append("TAG")
check("[1t] __iter__ / list() 返回独立拷贝（不影响内部）", len(capped), 1)
del items
# positions property 是浅拷贝
ext = capped.positions
ext.clear()
check("[1u] .positions 返回拷贝：外部 clear 不影响内部", capped.is_empty(), False)


# ════════════════════════════════════════════════════════════════
# [2] legacy_single / set_legacy 兼容层
# ════════════════════════════════════════════════════════════════
print("\n[2] legacy_single / set_legacy 兼容层")
b = PositionBook()
check("[2a] 空簿 legacy_single() → None", b.legacy_single(), None)
b.set_legacy(None)
check("[2b] set_legacy(None) → 空簿", b.is_empty(), True)
p_long = make_pos(entry_price=4500.0, side=Side.LONG)
b.set_legacy(p_long)
check("[2c] set_legacy(pos) → 簿只有这 1 笔", len(b), 1)
check("[2d] legacy_single() 返回这笔 Position", b.legacy_single() is p_long, True)
# 替换（不同对象）
p_short2 = make_pos(entry_price=4600.0, side=Side.SHORT)
b.set_legacy(p_short2)
check("[2e] set_legacy 第二次 → 替换整个簿", len(b), 1)
check("[2f] legacy_single() 返回新的 p_short2", b.legacy_single() is p_short2, True)
# 多仓：legacy_single 必须抛错（守护"多仓误用单仓 API"）
b2 = PositionBook()          # 默认不限容量，无需 hack 即可构造多仓
b2.add(p_long)
b2.add(p_short2)
check("[2g] 不限容量下构造多仓：__len__ == 2", len(b2), 2)
check_raises("[2h] legacy_single() 多仓立刻抛错 PositionBookError",
             b2.legacy_single, PositionBookError)
# 多仓时 set_legacy 替换整簿（合法 set 语义）
b2.set_legacy(p_long)
check("[2i] 多仓 set_legacy → 整簿替换回单仓", len(b2), 1)
check("[2j] 多仓 set_legacy 后 legacy_single() 返回 ok",
      b2.legacy_single() is p_long, True)


# ════════════════════════════════════════════════════════════════
# [3] has_opposite / opposite_positions / same_side_positions
# ════════════════════════════════════════════════════════════════
print("\n[3] has_opposite / opposite_positions / same_side_positions")
b3 = PositionBook()
check("[3a] 空簿 has_opposite(LONG) False", b3.has_opposite(Side.LONG), False)
check("[3b] 空簿 has_opposite(SHORT) False", b3.has_opposite(Side.SHORT), False)
b3.add(p_long)
check("[3c] 单笔多 → has_opposite(LONG) False", b3.has_opposite(Side.LONG), False)
check("[3d] 单笔多 → has_opposite(SHORT) True（与 SHORT 相反）",
      b3.has_opposite(Side.SHORT), True)
b3.add(p_short2)
check("[3e] 双边 → has_opposite(LONG) True", b3.has_opposite(Side.LONG), True)
check("[3f] 双边 → has_opposite(SHORT) True", b3.has_opposite(Side.SHORT), True)
opp = b3.opposite_positions(Side.LONG)
check("[3g] opposite_positions(LONG) 长度 == 1", len(opp), 1)
check("[3h] opposite_positions(LONG)[0] 是 SHORT", opp[0].side, Side.SHORT)
same = b3.same_side_positions(Side.SHORT)
check("[3i] same_side_positions(SHORT) 长度 == 1", len(same), 1)
check("[3j] same_side_positions(SHORT)[0] 是 p_short2", same[0] is p_short2, True)
none_opp = b3.opposite_positions(Side.SHORT)
check("[3k] opposite_positions(SHORT) 返回 LONG 那笔", len(none_opp), 1)
check("[3l] opposite_positions(SHORT)[0] 是 p_long", none_opp[0] is p_long, True)


# ════════════════════════════════════════════════════════════════
# [4] 序列化 roundtrip：新格式 list[dict]
# ════════════════════════════════════════════════════════════════
print("\n[4] to_dict / from_dict roundtrip（新格式 list[dict]）")
b4 = PositionBook()
check("[4a] 空簿 to_dict() → []", b4.to_dict(), [])
b4.add(make_pos(entry_price=4500.0, side=Side.LONG, vol=2))
data = b4.to_dict()
check("[4b] 单仓 to_dict() 是 list", isinstance(data, list), True)
check("[4c] 单仓 to_dict() 长度 == 1", len(data), 1)
check("[4d] to_dict()[0] 是 dict 且含 symbol",
      isinstance(data[0], dict) and data[0].get("symbol") == _SYM, True)
check("[4e] 序列化**不含** origin / lock_pair_id（新 schema）",
      sorted(k for k in ("origin", "lock_pair_id", "exit_mode", "entry_mode")
             if k in data[0]), [])
check("[4f] 序列化含 entry_date（规则 ⑷⑸ 的今/昨仓依据）",
      data[0].get("entry_date"), "2026-09-01")
b4_rt = PositionBook.from_dict(data)
check("[4g] from_dict(list) 簿长度 == 1", len(b4_rt), 1)
check("[4h] from_dict(list).legacy_single() side 与原一致",
      b4_rt.legacy_single().side, Side.LONG)
check("[4i] from_dict(list).legacy_single().volume == 2",
      b4_rt.legacy_single().volume, 2)
check("[4j] from_dict(list) entry_date 保留",
      b4_rt.legacy_single().entry_date, "2026-09-01")
check("[4k] from_dict(list) entry_bar_seq 保留",
      b4_rt.legacy_single().entry_bar_seq, 1)
# 复杂：双边
b4x = PositionBook()
b4x.add(make_pos(entry_price=4500.0, side=Side.LONG, vol=1,
                 signal_key="L1", entry_bar_seq=1))
b4x.add(make_pos(entry_price=4555.0, side=Side.SHORT, vol=3,
                 signal_key="S1", entry_bar_seq=2))
data_x = b4x.to_dict()
check("[4l] 双边 to_dict() 长度 == 2", len(data_x), 2)
b4x_rt = PositionBook.from_dict(data_x)
check("[4m] 双边 roundtrip 后 __len__ == 2", len(b4x_rt), 2)
check("[4n] 双边 roundtrip 后方向集合保留",
      sorted(p.side.name for p in b4x_rt.positions), ["LONG", "SHORT"])
check("[4o] 双边 roundtrip 后手数保留（净敞口 = 1 - 3 = -2）",
      b4x_rt.net_volume(), -2)
# 容量与多仓的交互（诚实记录 + 守护）
b4x_capped = PositionBook.from_dict(data_x, max_positions=1)
check("[4p] from_dict 不走 add → 不受 max_positions 截断（诚实记录现状）",
      len(b4x_capped), 2)
check_raises("[4q] 该簿 legacy_single() 抛错（多仓）",
             b4x_capped.legacy_single, PositionBookError)
_cap1 = PositionBook(max_positions=1)
_cap1.add(b4x.positions[0])
check_raises("[4r] 显式限容 1 的簿 add 第二笔 → 抛错",
             lambda: _cap1.add(b4x.positions[1]), PositionBookError)
del b4x_capped, _cap1


# ════════════════════════════════════════════════════════════════
# [5] 序列化兼容：旧格式 dict（v1 单字段）也能 from_dict
# ════════════════════════════════════════════════════════════════
print("\n[5] from_dict 兼容旧版单字段 dict + 异常输入")
legacy_dict = pos_dict(side="LONG", volume=3, signal_key="OLD",
                       entry_bar_seq=10, entry_date="2026-09-01")
b5 = PositionBook.from_dict(legacy_dict)
check("[5a] 旧 dict 反序列化 → __len__ == 1", len(b5), 1)
check("[5b] 旧 dict volume == 3", b5.legacy_single().volume, 3)
check("[5c] 旧 dict side == LONG", b5.legacy_single().side, Side.LONG)
check("[5d] 旧 dict entry_date 保留", b5.legacy_single().entry_date, "2026-09-01")

empty_inputs = [None, {}, [], {"symbol": None}, [None, {}], [{"x": 1}]]
for idx, bad in enumerate(empty_inputs):
    b_bad = PositionBook.from_dict(bad)
    check("[5e] from_dict 异常输入 #{} → 空簿".format(idx), b_bad.is_empty(), True)
# 单条坏数据不影响整体（symbol 合法但 side 非法 → 跳过该条，其余保留）
mixed = PositionBook.from_dict([pos_dict(side="LONG", signal_key="OK"),
                                {"symbol": _SYM, "side": "NOPE", "volume": 1,
                                 "entry_price": 1.0}])
check("[5f] list 内单条坏数据被跳过，其余保留", len(mixed), 1)
check("[5g] 保留的是那条合法记录", mixed.legacy_single().signal_key, "OK")


# ════════════════════════════════════════════════════════════════
# [5b] 三态判定与选仓的容器侧（A1 的 SSOT 基座）
# ════════════════════════════════════════════════════════════════
print("\n[5b] net_volume / latest / oldest_opposite")
nb = PositionBook()
check("[5b-1] 空簿 net_volume == 0", nb.net_volume(), 0)
check("[5b-2] 空簿 latest() is None", nb.latest(), None)
check("[5b-3] 空簿 oldest_opposite(LONG) is None",
      nb.oldest_opposite(Side.LONG), None)
# FIFO：entry_bar_seq 递增
pl1 = make_pos(side=Side.LONG, vol=2, signal_key="L-1", entry_bar_seq=1)
pl2 = make_pos(side=Side.LONG, vol=1, signal_key="L-2", entry_bar_seq=5)
ps1 = make_pos(side=Side.SHORT, vol=2, signal_key="S-1", entry_bar_seq=3)
for _p in (pl1, ps1, pl2):
    nb.add(_p)
check("[5b-4] 净敞口 = 2 - 2 + 1 = 1", nb.net_volume(), 1)
check("[5b-5] latest() 取 entry_bar_seq 最大者", nb.latest().signal_key, "L-2")
check("[5b-6] oldest_opposite(LONG) 取**反向最早**一笔 → S-1",
      nb.oldest_opposite(Side.LONG).signal_key, "S-1")
check("[5b-7] oldest_opposite(SHORT) 取**反向最早**一笔 → L-1",
      nb.oldest_opposite(Side.SHORT).signal_key, "L-1")
# 同序号（同根 K 线内多笔）→ latest 取最后追加的一笔
nb2 = PositionBook()
nb2.add(make_pos(side=Side.LONG, signal_key="A", entry_bar_seq=7))
nb2.add(make_pos(side=Side.LONG, signal_key="B", entry_bar_seq=7))
check("[5b-8] 同 entry_bar_seq → latest() 取最后追加的一笔", nb2.latest().signal_key, "B")
# 反向对消 → LOCKED 基座
nb3 = PositionBook()
nb3.add(make_pos(side=Side.LONG, vol=3, signal_key="L-1", entry_bar_seq=1))
nb3.add(make_pos(side=Side.SHORT, vol=3, signal_key="S-1", entry_bar_seq=2))
check("[5b-9] 多空等量 → net_volume == 0（LOCKED 基座）", nb3.net_volume(), 0)
check_true("[5b-10] net==0 但簿非空 → 不是 FLAT 而是 LOCKED 的判据成立",
           nb3.net_volume() == 0 and not nb3.is_empty())
# FIFO 出场顺序 = 添加顺序（append）
check("[5b-11] positions 顺序即添加顺序（FIFO）",
      [p.signal_key for p in nb3.positions], ["L-1", "S-1"])
del pl1, pl2, ps1, nb, nb2, nb3


# ════════════════════════════════════════════════════════════════
# [6] engine.position property 兼容（旧代码零改动）
# ════════════════════════════════════════════════════════════════
print("\n[6] engine.position property 兼容")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    check("[6a] 新引擎 engine.position 是 None", engine.position, None)
    check("[6b] 新引擎 engine.positions.is_empty() True",
          engine.positions.is_empty(), True)
    check("[6c] 新引擎 _state = IDLE", engine._state.name, "IDLE")
    check("[6d] 新引擎 account_state = FLAT", engine.account_state(), AccountState.FLAT)

    p_test = make_pos(entry_price=4500.0, side=Side.LONG, vol=2)
    engine.position = p_test
    check("[6e] engine.position = pos 后，positions.__len__ == 1",
          len(engine.positions), 1)
    check("[6f] engine.position 读回同一个对象", engine.position is p_test, True)
    check("[6g] engine.positions.legacy_single() 走 property 转发也是它",
          engine.positions.legacy_single() is p_test, True)
    # 直接写 engine.position 不会推 account_state/_state —— 那是 _sync_state 的职责
    check("[6h] 直接赋值不推 _state（仍是 IDLE）", engine._state.name, "IDLE")
    check("[6i] 但 account_state 是**即算**的 → 已是 RUNNING（net=2）",
          engine.account_state(), AccountState.RUNNING)
    engine._sync_state()
    check("[6j] _sync_state() 后 _state 跟上 → IN_TRADE",
          engine._state.name, "IN_TRADE")

    engine.position = None
    check("[6k] engine.position = None → 空簿", engine.positions.is_empty(), True)
    check("[6l] 清空后 account_state 立即回 FLAT",
          engine.account_state(), AccountState.FLAT)
    check("[6m] 未调 _sync_state → _state 仍停在 IN_TRADE（派生镜像滞后）",
          engine._state.name, "IN_TRADE")
    engine._sync_state()
    check("[6n] _sync_state() 后 _state 回 IDLE", engine._state.name, "IDLE")

    # 链式访问（position.side / .volume / .entry_price）
    engine.position = make_pos(entry_price=4500.0, side=Side.SHORT, vol=4)
    check("[6o] engine.position.side = SHORT", engine.position.side, Side.SHORT)
    check("[6p] engine.position.volume == 4", engine.position.volume, 4)
    check("[6q] engine.position.entry_price == 4500.0",
          engine.position.entry_price, 4500.0)
    check_true("[6r] engine.position 无 origin 属性（字段已删）",
               not hasattr(engine.position, "origin"))


# ════════════════════════════════════════════════════════════════
# [7] _persist 双写：内存多仓 / 空簿清理
# ════════════════════════════════════════════════════════════════
print("\n[7] _persist 双写：新键 'positions' + 旧键 'position'")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.position = make_pos(entry_price=4500.0, side=Side.LONG)
    engine._persist()
    new_k = store.get_json("positions")
    legacy_k = store.get_json("position")
    check("[7a] 单仓 persist → 新键 'positions' 是 list 且 len==1",
          isinstance(new_k, list) and len(new_k) == 1, True)
    check("[7b] 单仓 persist → 旧键 'position' 是 dict 含 symbol",
          isinstance(legacy_k, dict) and legacy_k.get("symbol") == _SYM, True)
    check("[7c] 两个键的 symbol 一致（新旧视图统一）",
          new_k[0].get("symbol"), legacy_k.get("symbol"))

    # 多仓（默认不限容量，不需要 _max hack）
    engine.positions.add(make_pos(entry_price=4605.0, side=Side.SHORT))
    engine._persist()
    new_k2 = store.get_json("positions")
    legacy_k2 = store.get_json("position")
    check("[7d] 多仓 persist → 新键 'positions' len==2",
          isinstance(new_k2, list) and len(new_k2) == 2, True)
    check("[7e] 多仓 persist → 旧键 'position' 是首个（向后兼容单字段视图）",
          legacy_k2.get("side"), "LONG")

    engine.positions.clear()
    engine.position = None   # 验证 setter 也清空
    engine._persist()
    check("[7f] 空簿 persist → 新键 'positions' 被删",
          store.get_json("positions"), None)
    check("[7g] 空簿 persist → 旧键 'position' 被删",
          store.get_json("position"), None)


# ════════════════════════════════════════════════════════════════
# [8] _restore 从老版 "position" 单字段恢复（迁移路径）
# ════════════════════════════════════════════════════════════════
print("\n[8] _restore 从老版 'position' 单字段恢复")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("position", pos_dict(side="LONG", volume=1,
                                       signal_key="LEGACY"))
    seed_run(store, side="LONG")      # net=1 → RUNNING → 必须有 run（G2）
    store.close()

    store2 = Store(os.path.join(tmp, "state.db"))
    engine, _st, _bk, ev = build_engine(tmp, store=store2)
    check("[8a] 老 'position' → engine.positions.__len__ == 1",
          len(engine.positions), 1)
    check("[8b] 老 'position' → signal_key = LEGACY",
          engine.position.signal_key, "LEGACY")
    check("[8c] 老 'position' → entry_date = 2026-09-01",
          engine.position.entry_date, "2026-09-01")
    check("[8d] 老 'position' → _state = IN_TRADE（net=1 → RUNNING）",
          engine._state.name, "IN_TRADE")
    check("[8e] 老 'position' → account_state = RUNNING",
          engine.account_state(), AccountState.RUNNING)
    # _persist 后写新键 —— 后续就完全走新格式了
    engine._persist()
    after = store2.get_json("positions")
    check("[8f] 老数据库首次 _persist → 自动升级写入新键 'positions'",
          isinstance(after, list) and len(after) == 1, True)
    check("[8g] 升级写入的记录不含 origin（新 schema）",
          "origin" in after[0], False)


# ════════════════════════════════════════════════════════════════
# [9] _restore 从新版 "positions" list 恢复（双向 → LOCKED）
# ════════════════════════════════════════════════════════════════
print("\n[9] _restore 从新版 'positions' list 恢复（双向 → LOCKED）")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        pos_dict(side="LONG", volume=1, entry_price=4500.0, signal_key="K1",
                 entry_bar_seq=10),
        pos_dict(side="SHORT", volume=1, entry_price=4555.0, signal_key="K2",
                 entry_bar_seq=12),
    ])
    # 净敞口 = 0（LOCKED）→ **不需要** run；这正是"锁仓态不受风控锚约束"的实证
    store.close()

    store2 = Store(os.path.join(tmp, "state.db"))
    engine, _st, _bk, ev = build_engine(tmp, store=store2)
    check("[9a] 新 'positions' list[2] → engine.positions.__len__ == 2",
          len(engine.positions), 2)
    check("[9b] 新 'positions' → 两侧方向都恢复",
          sorted(p.side.name for p in engine.positions.positions),
          ["LONG", "SHORT"])
    check("[9c] 两笔 entry_date 都保留",
          sorted(p.entry_date for p in engine.positions.positions),
          ["2026-09-01", "2026-09-01"])
    check("[9d] 双向对消 → account_state = LOCKED",
          engine.account_state(), AccountState.LOCKED)
    check("[9e] LOCKED → _state = IDLE（EngineState.IDLE 同时覆盖 FLAT 与 LOCKED）",
          engine._state.name, "IDLE")
    check("[9f] 不限容量：engine.positions.max_positions is None",
          engine.positions.max_positions, None)
    check_raises("[9g] engine.position 多仓时抛错 PositionBookError",
                 lambda: engine.position, PositionBookError)
    check("[9h] 无截断：truncated_on_restore 为空",
          len(engine.positions.truncated_on_restore), 0)


# ════════════════════════════════════════════════════════════════
# [9b] 不限容量：persisted=3 → 全量恢复、不截断、无 warning
# ════════════════════════════════════════════════════════════════
print("\n[9b] 不限容量：persisted=3 → 全量恢复不截断")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [
        pos_dict(side="LONG", volume=1, entry_price=4500.0, signal_key="P1",
                 entry_bar_seq=10),
        pos_dict(side="LONG", volume=1, entry_price=4505.0, signal_key="P2",
                 entry_bar_seq=11),
        pos_dict(side="LONG", volume=1, entry_price=4510.0, signal_key="P3",
                 entry_bar_seq=12),
    ])
    seed_run(store, side="LONG", volume=3)   # net=3 → RUNNING → 需 run
    store.close()

    store2 = Store(os.path.join(tmp, "state.db"))
    engine, _st, _bk, ev = build_engine(tmp, store=store2)
    check("[9b-1] persisted=3 → engine.positions.__len__ == 3（不截断）",
          len(engine.positions), 3)
    check("[9b-2] 净敞口 = 3", engine.positions.net_volume(), 3)
    check("[9b-3] truncated_on_restore 为空", len(engine.positions.truncated_on_restore), 0)
    check("[9b-4] 三笔 signal_key 全在",
          sorted(p.signal_key for p in engine.positions.positions),
          ["P1", "P2", "P3"])
    check("[9b-5] net=3 → _state = IN_TRADE", engine._state.name, "IN_TRADE")
    ev.flush()
    _log = os.path.join(tmp, "events.jsonl")
    _has_warn = False
    if os.path.isfile(_log):
        with open(_log, "r", encoding="utf-8") as fh:
            _has_warn = any("positions_truncated_on_restore" in ln for ln in fh)
    check("[9b-6] 不限容量下不写 positions_truncated_on_restore warning",
          _has_warn, False)


# ════════════════════════════════════════════════════════════════
# [10] 持仓端到端 roundtrip（跨"进程重启"）
# ════════════════════════════════════════════════════════════════
print("\n[10] 持仓端到端 roundtrip")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    pos_u = make_pos(side=Side.LONG, vol=2, signal_key="U1")
    engine.position = pos_u   # 绕开正常开仓直接塞入
    # 绕开 _execute 就要手动把 run 立起来（真实路径由 _execute 的净敞口变化触发）——
    #   否则 _persist 会按"无运行态"把 kv run 删掉，重启时撞 G2 拒绝启动。
    engine._run_side = Side.LONG
    engine._run_anchor = 4500.0
    engine._run_volume = 2
    engine._run_plan = ExitPlan(name="run_managed", stop_price=0.0)
    engine._run_signal_key = "U1"
    engine._persist()
    engine._sync_state()

    check("[10a] 持久化后 _state = IN_TRADE", engine._state.name, "IN_TRADE")

    new_store = Store(store.path)
    new_engine, _st2, _bk2, _ev2 = build_engine(
        tmp, store=new_store, ev_name="events2.jsonl")

    check("[10b] 重启后 positions.__len__ == 1", len(new_engine.positions), 1)
    check("[10c] 重启后 side = LONG", new_engine.position.side, Side.LONG)
    check("[10d] 重启后 volume == 2", new_engine.position.volume, 2)
    check("[10e] 重启后 signal_key = U1", new_engine.position.signal_key, "U1")
    check("[10f] 重启后 entry_date 保留",
          new_engine.position.entry_date, "2026-09-01")
    check("[10g] 重启后 entry_bar_seq 保留（FIFO 顺序不漂移）",
          new_engine.position.entry_bar_seq, 1)
    check("[10h] 重启后 _state = IN_TRADE", new_engine._state.name, "IN_TRADE")
    check("[10i] 重启后 run（风控锚）也接续上了",
          new_engine._run_side, Side.LONG)


# ════════════════════════════════════════════════════════════════
# [11] G1 旧 schema 闸门：带 origin / lock_pair_id 的库 → 拒绝启动
# ════════════════════════════════════════════════════════════════
print("\n[11] G1 旧 schema 闸门（origin / lock_pair_id）")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    _d = pos_dict(side="LONG", volume=1, signal_key="OLD1")
    _d["origin"] = "signal_open"          # ← 旧版本写的"来源"标记
    store.set_json("positions", [_d])
    store.close()
    _store2 = Store(os.path.join(tmp, "state.db"))
    check_raises("[11a] 持仓记录含 origin → 拒绝启动 RuntimeError",
                 lambda: build_engine(tmp, store=_store2), RuntimeError)

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    _d = pos_dict(side="LONG", volume=1, signal_key="OLD2")
    _d["lock_pair_id"] = "lock_00001"
    store.set_json("positions", [_d])
    store.close()
    _store2 = Store(os.path.join(tmp, "state.db"))
    check_raises("[11b] 持仓记录含 lock_pair_id → 拒绝启动 RuntimeError",
                 lambda: build_engine(tmp, store=_store2), RuntimeError)

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [pos_dict(side="LONG", volume=1, signal_key="OK1")])
    store.set_json("lock_pair_seq", 7)     # ← 旧版本写在 kv 上的配对序号
    seed_run(store, side="LONG")
    store.close()
    _store2 = Store(os.path.join(tmp, "state.db"))
    check_raises("[11c] kv 表残留 lock_pair_seq → 拒绝启动 RuntimeError",
                 lambda: build_engine(tmp, store=_store2), RuntimeError)

with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [pos_dict(side="LONG", volume=1, signal_key="OK2")])
    seed_run(store, side="LONG")
    store.close()
    _store2 = Store(os.path.join(tmp, "state.db"))
    try:
        _e = build_engine(tmp, store=_store2)[0]
        _err = None
    except Exception as e:
        _e, _err = None, type(e).__name__
    check("[11d] 干净的新 schema 库 → 正常启动", _err, None)
    check("[11e] 干净库恢复出 1 笔持仓",
          (len(_e.positions) if _e else None), 1)

# G2：净敞口 ≠ 0 但缺 run → 拒绝启动（与 G1 同性质的"不猜"闸门）
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    store.set_json("positions", [pos_dict(side="LONG", volume=1, signal_key="NORUN")])
    store.close()                          # 故意不写 run
    _store2 = Store(os.path.join(tmp, "state.db"))
    check_raises("[11f] G2：净敞口≠0 但无 run → 拒绝启动 RuntimeError",
                 lambda: build_engine(tmp, store=_store2), RuntimeError)


# ════════════════════════════════════════════════════════════════
# [12] 切合约隔离：restore 只加载当前 trade_symbol，persist 保留它合约持仓
# ════════════════════════════════════════════════════════════════
print("\n[12] 切合约隔离：restore 按 trade_symbol 过滤 + persist 分片合并")
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state.db"))
    # 库里同时有 IF（当前合约）与 IM（其它合约）持仓
    store.set_json("positions", [
        pos_dict(side="LONG", volume=1, signal_key="IF-L", entry_bar_seq=10,
                 symbol="CFFEX.IF2609"),
        pos_dict(side="SHORT", volume=1, signal_key="IM-S", entry_bar_seq=12,
                 entry_price=6000.0, symbol="CFFEX.IM2609"),
    ])
    seed_run(store, side="LONG")   # 当前合约 IF 净敞口 = 1 → 需 run
    store.close()

    _store2 = Store(os.path.join(tmp, "state.db"))
    engine, _st, _bk, _ev = build_engine(tmp, store=_store2)

    check("[12a] restore 只加载 IF 持仓（IM 被过滤）", len(engine.positions), 1)
    check("[12b] 加载的持仓 symbol == trade_symbol",
          engine.positions.positions[0].symbol, "CFFEX.IF2609")
    check("[12c] 加载的是 IF 仓（signal_key=IF-L）",
          engine.positions.positions[0].signal_key, "IF-L")

    # persist 分片合并：IF 全平（簿空）后，IM 持仓仍保留在库里
    engine.positions.clear()
    engine._persist()
    after = _store2.get_json("positions")
    check("[12d] IF 全平 persist 后 IM 持仓仍保留",
          isinstance(after, list) and len(after) == 1, True)
    check("[12e] 保留的是 IM 仓", after[0].get("symbol"), "CFFEX.IM2609")
    legacy = _store2.get_json("position")
    check("[12f] 旧键 'position' 退回它合约首仓（IM）",
          legacy.get("symbol") if legacy else None, "CFFEX.IM2609")


print("\n" + "=" * 60)
print("P12 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
