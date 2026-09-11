# -*- coding: utf-8 -*-
"""P29 —— 持久 ID 跨重启唯一性 / fail-fast / 旧 schema 闸门（R1+R2，2026-09-10）
==============================================================================
2026-09-11 Phase 7 改写。**R1（序号持久化）与 R2（撞号 fail-fast）原样保留** ——
它们与本次重构无关，且是真实事故（2 笔成交落库只剩 1 条）的护栏。
被删掉的是 **R3（锁仓配对不变量）**：`lock_pair_id` / `_lock_pair_seq` /
`_assert_lock_pair_invariant()` 随"配对"概念一并删除。旧版 R3 那段
（"同一 lock_pair_id 至多 2 笔且反向"）在新模型里**不可表达也无法违反**：
仓单之间没有配对关系，簿只是一个 FIFO 序列，"锁仓"完全由净敞口表达。
R3 的继任者是**旧 schema 闸门（G1）**：库里出现 `origin` / `lock_pair_id` /
`exit_mode` / `entry_mode` 这些键 → 直接拒绝启动（不猜、不迁移）。

背景（为什么要这个文件）
--------------------------------------------------------------------------
持久 ID 由**进程内计数器**生成，而落盘表的键又是它们：

    trade_id = "T{:05d}".format(self._trade_seq)      Engine / Reconcile
    order_id = "{broker}-{:06d}".format(seq)          DryRun / SimNow broker

进程重启 → 计数器归零 → 新号与库内既有号相撞。而 `save_trade` / `save_order`
当时用的是 `INSERT OR REPLACE` → **上一进程的审计记录被静默覆盖**
（实测：2 笔独立成交落库后只剩 1 条）。

根因诊断（核实过，不是猜）
    `_trade_seq` 全文件只有赋值、**没有任何恢复路径**（`_persist` / `_restore`
    都不碰）。而同性质的 `bars_seen` 早就走 `set_json` / `get_json` ——
    实测跨重启 7 → 7，那个归零 2 → 0。
    ⇒ 根因是**漏接已有的持久化机制**，不是"计数器方案不可靠"。

两个修正（本文件把它们钉死）
    R1  序号持久化：`_persist` 写 kv；恢复取 `max(kv 值, 库内数据推导值)`；
        broker 报单序号从 `orders` 表抬升。**不换时间戳 ID** —— 实测同一毫秒
        批量离场会生成相同毫秒、墙钟回拨后会与历史号重复，症状与计数器一样。
    R2  写入 fail-fast：`save_trade` / `save_order` 改 `INSERT`，撞号抛
        `IdCollisionError`（撞号 = 审计底稿要被覆盖，必须响亮地死）。
    （原 R3 锁仓配对不变量 → 已被 G1 旧 schema 闸门取代，见 §5）

本文件共 6 组：
    [1] 端到端自然流程：开→平→再开→④ 锁仓 ‖ 重启 ‖ ③ 拆锁→再平，全程无重复、无丢失
    [2] 序号落 kv（跨重启保持）
    [3] 库内 max 自愈（kv 丢了也能从真实数据反推）
    [4] R2 fail-fast（撞号抛错且**不覆盖**既有记录）
    [5] 旧 schema 闸门（origin / lock_pair_id 记录、lock_pair_seq kv → 拒绝启动）
    [6] 源码护栏（不得再用 INSERT OR REPLACE 写这两张表；broker 不得用 itertools.count）

跑法：python Trading/Test/test_p29_id_uniqueness.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.DryRun import DryRunBroker            # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine           # noqa: E402
from Trading.Infra.EventLog import EventLog               # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec   # noqa: E402
from Trading.Infra.Store import IdCollisionError, Store   # noqa: E402
from Trading.Infra.Types import (                         # noqa: E402
    Bar, ExitPlan, Order, Position, Side, Signal, Trade,
)
from Trading.Strategy.Entry import DefaultEntryPolicy     # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy       # noqa: E402

CN_TZ = timezone(timedelta(hours=8))
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


def check_true(name, cond, detail=""):
    check(name + ("  — " + detail if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p29_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=CN_TZ).timestamp() * 1000)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"].update({"max_volume": 2})
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0})
    return TradingConfig.from_dict(base)


def build(tmp, tag="a"):
    """在 tmp 下用**固定文件名**建引擎 —— 同 tag 第二次调用 = 模拟进程重启。"""
    cfg = make_cfg()
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {})
    store = Store(os.path.join(tmp, "state_%s.db" % tag))
    ev = EventLog(os.path.join(tmp, "events_%s.jsonl" % tag),
                  echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, DefaultEntryPolicy({}),
                        LayeredExitPolicy(cfg.exit_params.model_dump()),
                        store, ev)
    return eng, broker


def db_rows(store, sql):
    return [dict(r) for r in store.conn.execute(sql).fetchall()]


def event_kinds(path):
    """读 events.jsonl 的 kind 序列（文件不存在 → 空列表）。"""
    kinds = []
    if not os.path.isfile(path):
        return kinds
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                kinds.append(json.loads(line).get("kind"))
            except ValueError:
                pass
    return kinds


def make_bar(ts, date, o, h, l, c):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def _oseq(brk):
    """读 broker 报单序号。缺该 API（= R1 未落地）时返回 None → 断言失败而非崩溃。"""
    fn = getattr(brk, "order_seq", None)
    return int(fn()) if callable(fn) else None


def _next_oid(brk):
    fn = getattr(brk, "_next_order_id", None)
    return fn() if callable(fn) else None


def make_sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type=("1" if is_buy else "2"),
                  is_buy=is_buy, price=price, high=price + 5.0, low=price - 5.0,
                  fractal_low=price - 30.0, fractal_high=price + 30.0)


# ════════════════════════════════════════════════════════════════════
print("\n[1] 端到端自然流程：跨重启全链路无重复 / 无丢失")
# ════════════════════════════════════════════════════════════════════
# 剧本（全部走 on_bar / on_signal 正常入口，不注入持仓）：
#   进程 A：T1 开多 → T2 跨日止损（转移 ⑤ CLOSE，成交 T00001）
#           T3 再开多 → T4 当日止损（转移 ④ 反向 OPEN → 双向持仓、net 0）
#   ↻ 重启（同一 state.db）↻
#   进程 B：T5 反向信号 → 转移 ③ 拆锁平仓（成交 T00002）
#           T6 跨日止损（转移 ⑤ CLOSE，成交 T00003）
T1 = (ms(2026, 9, 2, 9, 40), "2026-09-02 09:40")
T2 = (ms(2026, 9, 3, 9, 40), "2026-09-03 09:40")
T3 = (ms(2026, 9, 3, 9, 45), "2026-09-03 09:45")
T4 = (ms(2026, 9, 3, 9, 50), "2026-09-03 09:50")
T5 = (ms(2026, 9, 7, 9, 40), "2026-09-07 09:40")
T6 = (ms(2026, 9, 8, 9, 40), "2026-09-08 09:40")

with tmp_dir() as tmp:
    eng_a, brk_a = build(tmp, "e2e")

    # ── T1：开多 ────────────────────────────────────────────────
    eng_a.on_bar(make_bar(T1[0], T1[1], 4500, 4510, 4490, 4505))
    eng_a.on_signal(make_sig("A|buy|1", T1[1], T1[0], 4505.0, True))
    check("[1a] T1 开仓 → 簿内 1 笔", len(eng_a.positions.positions), 1)
    check("[1b] T1 开仓 → 1 条 order",
          len(db_rows(eng_a.store, "SELECT order_id FROM orders")), 1)

    # ── T2：跨日止损 → 转移 ⑤ CLOSE ──────────────────────────────
    eng_a.on_bar(make_bar(T2[0], T2[1], 4400, 4410, 3000, 3050))
    check("[1c] T2 跨日止损 → 成交 1 笔（T00001）",
          [t["trade_id"] for t in eng_a.store.trades()], ["T00001"])
    check("[1d] T2 平仓后簿空", len(eng_a.positions.positions), 0)

    # ── T3：再开多（同一交易日）──────────────────────────────────
    eng_a.on_bar(make_bar(T3[0], T3[1], 4500, 4510, 4490, 4505))
    eng_a.on_signal(make_sig("A|buy|2", T3[1], T3[0], 4505.0, True))
    check("[1e] T3 再开仓 → 簿内 1 笔", len(eng_a.positions.positions), 1)

    # ── T4：当日止损 → 转移 ④ 反向 OPEN（双向持仓，net 0）────────
    eng_a.on_bar(make_bar(T4[0], T4[1], 4400, 4410, 3000, 3050))
    check("[1f] T4 当日止损 → ④ 反向开仓（留下双向持仓）",
          sorted(p.side.name for p in eng_a.positions.positions),
          ["LONG", "SHORT"])
    check("[1g] ④ 后簿内 2 笔（多空互锁）",
          len(eng_a.positions.positions), 2)
    check("[1g2] 净敞口归零 → LOCKED（无 lock_pair_id，不记配对）",
          eng_a.account_state().value, "locked")
    check("[1h] ④ 不记 Trade（PnL 未兑现）",
          len(eng_a.store.trades()), 1)
    check("[1i] 进程 A 结束时 _trade_seq=1", eng_a._trade_seq, 1)
    orders_a = db_rows(eng_a.store, "SELECT order_id FROM orders")
    eng_a.store.close()

    # ── ↻ 重启（同一 state.db）────────────────────────────────────
    eng_b, brk_b = build(tmp, "e2e")
    check("[1j] 重启后 _trade_seq 保持 1（R1）", eng_b._trade_seq, 1)
    check("[1k] 重启后持仓 2 笔（双向持仓跨重启保持）",
          len(eng_b.positions.positions), 2)
    check("[1k2] 重启后 account_state 仍 LOCKED",
          eng_b.account_state().value, "locked")

    # ── T5：反向信号 → 转移 ③ 拆锁平仓 ──────────────────────────
    eng_b.on_bar(make_bar(T5[0], T5[1], 4500, 4510, 4490, 4505))
    eng_b.on_signal(make_sig("B|sell|1", T5[1], T5[0], 4505.0, False))
    check("[1m] T5 拆锁 → 新成交号是 T00002（**不是 T00001**）",
          sorted(t["trade_id"] for t in eng_b.store.trades()),
          ["T00001", "T00002"])
    check("[1n] 拆锁后簿内 1 笔（多头那笔被平掉，只剩空头）",
          len(eng_b.positions.positions), 1)
    check("[1n2] 剩余的是 SHORT",
          eng_b.positions.positions[0].side.name, "SHORT")

    # ── T6：跨日止损 → 转移 ⑤ CLOSE ─────────────────────────────
    eng_b.on_bar(make_bar(T6[0], T6[1], 4600, 6000, 4590, 5990))
    trades_final = eng_b.store.trades()
    check("[1o] T6 平仓 → 三个成交号全不同",
          sorted(t["trade_id"] for t in trades_final),
          ["T00001", "T00002", "T00003"])
    check("[1p] 三笔成交全部保留（无被覆盖）", len(trades_final), 3)

    oids = [r["order_id"] for r in db_rows(eng_b.store,
                                           "SELECT order_id FROM orders")]
    check_true("[1q] 委托号无重复（重启前后不撞号）",
               len(oids) == len(set(oids)),
               "n={} uniq={}".format(len(oids), len(set(oids))))
    check_true("[1r] 重启前写下的委托号仍在库里（未被覆盖）",
               all(r["order_id"] in oids for r in orders_a),
               "before={}".format([r["order_id"] for r in orders_a]))
    check_true("[1s] 委托记录数 ≥ 5（没有被覆盖合并）",
               len(oids) >= 5, "n={}".format(len(oids)))
    eng_b.store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[2] 序号落 kv（R1：跨重启保持，不依赖数据反推）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng, _ = build(tmp, "kv")
    eng._trade_seq = 7
    _seed = getattr(eng.broker, "seed_order_seq", None)
    if callable(_seed):
        _seed(11)
    eng._persist()
    check("[2a] kv 写入 trade_seq", eng.store.get_json("trade_seq"), 7)
    check("[2b] kv 写入 order_seq", eng.store.get_json("order_seq"), 11)
    # 2026-09-11：lock_pair_seq 已随配对概念删除 —— 它现在属于**旧 schema 键**，
    # 库里出现它会让引擎拒绝启动（见 [5c]），更不该由 _persist 写出来。
    check("[2c] _persist 不再写 lock_pair_seq（键已随概念删除）",
          eng.store.get_json("lock_pair_seq"), None)

    eng2, brk2 = build(tmp, "kv")
    check("[2d] 重启后 _trade_seq 恢复", eng2._trade_seq, 7)
    check("[2e] 重启后 broker 序号抬升", _oseq(brk2), 11)
    check("[2f] 下一条委托号 = 12",
          _next_oid(brk2), "dry_run-000012")
    # 新号绝不能落在已用集合里
    check_true("[2g] 抬升后的号与历史号无交集",
               (_oseq(brk2) or 0) > 11, "seq={}".format(_oseq(brk2)))
    check("[2h] 重启后 _lock_pair_seq 属性已不存在（概念删除）",
          hasattr(eng2, "_lock_pair_seq"), False)
    eng.store.close()
    eng2.store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[3] 库内 max 自愈（R1 第二道保险：kv 丢了也能从真实数据反推）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state_heal.db"))
    for i in (3, 5, 12):                       # 刻意留下空洞，验证取 max 而非 count
        store.save_trade(Trade(
            trade_id="T{:05d}".format(i), symbol="CFFEX.IF2609", side=Side.LONG,
            volume=2, entry_price=4500.0, exit_price=4510.0,
            entry_at="2026-09-01 09:00", exit_at="2026-09-02 09:00",
            reason="sl", gross_points=10.0, cost_points=1.0, net_points=9.0,
            net_cash=180.0, bars_held=3, signal_key="K{}".format(i),
            exit_plan_name="x", exit_plan_params={}))
    for i in (4, 20):
        store.save_order(Order(order_id="dry_run-{:06d}".format(i),
                               signal_key="K{}".format(i), symbol="CFFEX.IF2609",
                               side=Side.LONG, action="open", volume=2,
                               price=4500.0, status="filled", broker="dry_run"))
    check("[3a] max_trade_seq 取最大号（不是行数）", store.max_trade_seq(), 12)
    check("[3b] max_order_seq 取最大号", store.max_order_seq("dry_run"), 20)
    check("[3c] 别的 broker 前缀不混入", store.max_order_seq("simnow"), 0)

    # 模拟"kv 被外部清掉 / 换过库文件"：只留数据，不留 kv
    store.set_json("trade_seq", 0)
    store.set_json("order_seq", 0)
    # 只放一笔**新 schema** 持仓（无 origin / lock_pair_id）
    store.set_json("positions", [Position(
        symbol="CFFEX.IF2609", side=Side.LONG, volume=2, entry_price=4500.0,
        entry_at="2026-09-01 09:00", entry_bar_ts=ms(2026, 9, 1, 9, 35),
        signal_key="LK", open_order_id="o",
        exit_plan=ExitPlan(name="x", stop_price=0.0),
        entry_bar_seq=1, entry_date="2026-09-01").to_dict()])
    # 净敞口 ≠ 0 → 必须补一份 run，否则撞 G2（本组只测序号自愈，不测闸门）
    store.set_json("run", {
        "side": "LONG", "anchor": 4500.0, "volume": 2,
        "bar_ts": ms(2026, 9, 1, 9, 35), "bar_seq": 1, "signal_key": "LK",
        "plan": {"name": "x", "stop_price": 0.0, "tp_price": None, "params": {}}})
    eng3, brk3 = build(tmp, "heal")
    check("[3d] kv=0 时 _trade_seq 从 trades 表自愈", eng3._trade_seq, 12)
    check("[3e] kv=0 时 broker 序号从 orders 表自愈", _oseq(brk3), 20)
    check_true("[3f] 自愈出的号严格大于库内历史最大号",
               eng3._trade_seq >= 12 and (_oseq(brk3) or 0) >= 20)
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[4] R2 fail-fast：撞号抛 IdCollisionError，且**不覆盖**既有记录")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state_ff.db"))
    store.save_trade(Trade(
        trade_id="T00001", symbol="CFFEX.IF2609", side=Side.LONG, volume=2,
        entry_price=4500.0, exit_price=4510.0, entry_at="A", exit_at="B",
        reason="sl", gross_points=10.0, cost_points=1.0, net_points=9.0,
        net_cash=180.0, bars_held=3, signal_key="ORIG",
        exit_plan_name="x", exit_plan_params={}))

    raised = None
    try:
        store.save_trade(Trade(
            trade_id="T00001", symbol="CFFEX.IF2609", side=Side.SHORT, volume=9,
            entry_price=1.0, exit_price=2.0, entry_at="C", exit_at="D",
            reason="IMPOSTOR", gross_points=1.0, cost_points=0.0, net_points=1.0,
            net_cash=1.0, bars_held=0, signal_key="IMPOSTOR",
            exit_plan_name="y", exit_plan_params={}))
    except IdCollisionError as e:
        raised = e
    check_true("[4a] trades 撞号 → 抛 IdCollisionError", raised is not None,
               repr(raised))
    row = dict(store.conn.execute(
        "SELECT * FROM trades WHERE trade_id='T00001'").fetchone())
    check("[4b] 原记录**未被覆盖**（signal_key 仍是 ORIG）",
          row["signal_key"], "ORIG")
    check("[4c] 原记录 volume 未被覆盖", row["volume"], 2)
    check("[4d] 重复写同一 trade_id 不会新增行",
          len(db_rows(store, "SELECT * FROM trades")), 1)

    store.save_order(Order(order_id="dry_run-000001", signal_key="ORIG",
                           symbol="CFFEX.IF2609", side=Side.LONG, action="open",
                           volume=2, price=4500.0, status="filled",
                           broker="dry_run"))
    raised = None
    try:
        store.save_order(Order(order_id="dry_run-000001", signal_key="IMPOSTOR",
                               symbol="CFFEX.IF2609", side=Side.SHORT,
                               action="close", volume=9, price=1.0,
                               status="filled", broker="dry_run"))
    except IdCollisionError as e:
        raised = e
    check_true("[4e] orders 撞号 → 抛 IdCollisionError", raised is not None,
               repr(raised))
    orow = dict(store.conn.execute(
        "SELECT * FROM orders WHERE order_id='dry_run-000001'").fetchone())
    check("[4f] 原委托记录未被覆盖", orow["signal_key"], "ORIG")
    check("[4g] orders 行数仍为 1",
          len(db_rows(store, "SELECT * FROM orders")), 1)
    check_true("[4h] 错误信息里带得上 order_id / trade_id（可定位）",
               "dry_run-000001" in str(raised), str(raised)[:120])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[5] G1 旧 schema 闸门（带旧键的库 → 拒绝启动，不猜不迁移）")
# ════════════════════════════════════════════════════════════════════
# 本组取代旧 R3（"同一 lock_pair_id 至多 2 笔且反向"）。
# R3 在新模型里**不可表达也无法违反** —— 仓单之间没有配对关系，簿只是一个
# FIFO 序列，"锁仓"完全由净敞口表达，所以"配对不变量"这个概念本身消失了。
# 继任者 G1 处理真正的新风险：**旧版本的库**不能直接恢复 —— 旧记录用"来源标记"
# 区分锁仓仓 / 敞口仓，而当前口径只看净敞口，直接恢复会让引擎对已对冲的仓
# 发平仓单（账实不符）。口径：一律严格拒绝，不做"自动剥离 + 继续跑"的迁移。
_LEGACY_POS_KEYS = ("origin", "lock_pair_id", "entry_mode", "exit_mode")
_LEGACY_KV_KEYS = ("lock_pair_seq", "unlock_in_flight")


def _pos(side, key, entry_date="2026-09-01", seq=1):
    """新 schema 持仓：无 origin / lock_pair_id / entry_mode / exit_mode。"""
    return Position(symbol="CFFEX.IF2609", side=side, volume=2,
                    entry_price=4500.0, entry_at="2026-09-01 09:00",
                    entry_bar_ts=ms(2026, 9, 1, 9, 35), signal_key=key,
                    open_order_id="o", exit_plan=ExitPlan(name="x", stop_price=0.0),
                    entry_bar_seq=seq, entry_date=entry_date)


_RUN_KV = {
    "side": "LONG", "anchor": 4500.0, "volume": 2,
    "bar_ts": ms(2026, 9, 1, 9, 35), "bar_seq": 1, "signal_key": "A",
    "plan": {"name": "x", "stop_price": 0.0, "tp_price": None, "params": {}}}


def _seed_positions(tmp, tag, recs, kv_extra=None):
    """把 recs 写进 tag 对应的库，并补一份 run（避免本组误撞 G2）。

    net≠0 而无 run → 引擎拒绝启动（G2）。本组只测 G1，故凡是要"能启动"的
    格子都必须带上 run；凡是期望被 G1 拦住的格子（G1 在 run 检查之前触发，
    见 _restore 里 _reject_legacy_state 的调用位置）带不带都无所谓，统一带上
    以便把两个闸门解耦。
    """
    st = Store(os.path.join(tmp, "state_%s.db" % tag))
    st.set_json("positions", recs)
    st.set_json("run", _RUN_KV)
    for k, v in (kv_extra or {}).items():
        st.set_json(k, v)
    st.close()


def _try_build(tmp, tag):
    """启动引擎；成功返回 None，抛 RuntimeError 则返回该异常。"""
    try:
        build(tmp, tag)
        return None
    except RuntimeError as e:
        return e


with tmp_dir() as tmp:
    _seed_positions(tmp, "g1ok", [_pos(Side.LONG, "A").to_dict()])
    eng, _ = build(tmp, "g1ok")
    check("[5a] 全新 schema（无任何旧键）→ 正常启动",
          [p.side.name for p in eng.positions.positions], ["LONG"])

# 四个旧键各自单独出现都要被拦（逐个覆盖，不漏一个）
for _tag, _key in (("org", "origin"), ("lpid", "lock_pair_id"),
                   ("entrym", "entry_mode"), ("exitm", "exit_mode")):
    with tmp_dir() as tmp:
        _rec = _pos(Side.LONG, "A").to_dict()
        _rec[_key] = "soft_exit_lock"                 # 旧版本写下的来源标记
        _seed_positions(tmp, _tag, [_rec])
        _err = _try_build(tmp, _tag)
        check_true("[5b] 持仓记录含旧键 {} → 拒绝启动".format(_key),
                   _err is not None, repr(_err)[:80])
        check_true("[5c] 错误信息点出旧键 {}（可定位）".format(_key),
                   _key in str(_err), str(_err)[:120])
        check_true("[5d] 错误信息给出受影响合约（{}）".format("CFFEX.IF2609"),
                   "CFFEX.IF2609" in str(_err), str(_err)[:160])
        check_true("[5e] 错误信息解释后果 + 给出处理办法（删库）",
                   "账实不符" in str(_err) and "state.db" in str(_err),
                   str(_err)[:200])

# kv 表残留旧键 → 同样拒绝（它已无读取方，存在即说明库是旧版本写的）
for _tag, _kv in (("kvlps", "lock_pair_seq"), ("kvufi", "unlock_in_flight")):
    with tmp_dir() as tmp:
        _seed_positions(tmp, _tag, [_pos(Side.LONG, "A").to_dict()],
                        kv_extra={_kv: 3})
        _err = _try_build(tmp, _tag)
        check_true("[5f] kv 残留 {} → 拒绝启动".format(_kv),
                   _err is not None, repr(_err)[:80])
        check_true("[5g] 错误信息点出 kv 键 {}（可定位）".format(_kv),
                   _kv in str(_err), str(_err)[:160])

# 闸门必须留痕：写 state_schema_incompatible 事件（供人查"为什么启不来"）
with tmp_dir() as tmp:
    _rec = _pos(Side.LONG, "A").to_dict()
    _rec["origin"] = "signal_open"
    _seed_positions(tmp, "g1ev", [_rec])
    _err = _try_build(tmp, "g1ev")
    _ks = event_kinds(os.path.join(tmp, "events_g1ev.jsonl"))
    check_true("[5h] 拒绝时写 state_schema_incompatible 事件",
               "state_schema_incompatible" in _ks, str(_ks))
    check("[5i] 事件只写一次（不每次启动都追加）",
          _ks.count("state_schema_incompatible"), 1)

# 纯函数 `_legacy_position_records` 的边界：**新增字段不算旧键**
check("[5j] _legacy_position_records 纯函数：新 schema 记录 → 空",
      TradingEngine._legacy_position_records(
          [_pos(Side.LONG, "A").to_dict()]), [])
check("[5k] _legacy_position_records 纯函数：含 origin → 命中 1 条",
      len(TradingEngine._legacy_position_records(
          [dict(_pos(Side.LONG, "A").to_dict(), origin="x")])), 1)
check("[5l] 非 dict 项（脏数据）不参与判定，也不炸",
      TradingEngine._legacy_position_records([None, 42, "x"]), [])

# ════════════════════════════════════════════════════════════════════
print("\n[6] 源码护栏（防回潮）")
# ════════════════════════════════════════════════════════════════════


def read(rel):
    with open(os.path.join(os.path.dirname(_TG_ROOT), rel), encoding="utf-8") as f:
        return f.read()


store_src = read("Trading/Infra/Store.py")
# 只摘 save_order / save_trade 两个函数体
def _body(src, name):
    i = src.index("def {}(".format(name))
    j = src.find("\n    def ", i + 10)
    return src[i:j if j > 0 else len(src)]


def _code_only(src):
    """剥掉每行 # 之后的内容（注释里的举例不算"还在用"）。"""
    return "\n".join(line.split("#")[0] for line in src.splitlines())


check_true("[6a] save_trade 的 SQL 是裸 INSERT（不带 OR REPLACE）",
           "INSERT INTO trades VALUES" in _body(store_src, "save_trade")
           and "INSERT OR REPLACE INTO trades"
           not in _body(store_src, "save_trade"))
check_true("[6b] save_order 的 SQL 是裸 INSERT（不带 OR REPLACE）",
           "INSERT INTO orders VALUES" in _body(store_src, "save_order")
           and "INSERT OR REPLACE INTO orders"
           not in _body(store_src, "save_order"))
check_true("[6c] 两函数都抛出 IdCollisionError",
           "IdCollisionError" in _body(store_src, "save_trade")
           and "IdCollisionError" in _body(store_src, "save_order"))
check_true("[6d] kv 表仍用 INSERT OR REPLACE（upsert 语义正确，别误改）",
           "INSERT OR REPLACE INTO kv" in store_src)

eng_src = read("Trading/Engine/Engine.py")
check_true("[6e] _persist 落 trade_seq / order_seq（R1）",
           all(k in eng_src for k in ('set_json("trade_seq"',
                                      'set_json("order_seq"')))
check_true("[6e2] _persist **不再**落 lock_pair_seq（键随概念删除）",
           'set_json("lock_pair_seq"' not in eng_src)
check_true("[6f] _restore 恢复序号且带库内 max 自愈（R1 第二道保险）",
           "max_trade_seq()" in eng_src and "max_order_seq(" in eng_src)
check_true("[6f2] 锁仓序号的库内自愈辅助函数已删除",
           "_max_lock_pair_seq_in_state" not in eng_src)
check_true("[6g] G1 闸门在 _restore 中被调用（旧库拒绝启动）",
           "_reject_legacy_state(" in eng_src)
check_true("[6g2] 旧 schema 键清单仍完整（含改名前的 entry_mode）",
           all(k in eng_src for k in ("_LEGACY_POSITION_KEYS",
                                      '"entry_mode"', '"origin"',
                                      '"lock_pair_id"', '"exit_mode"')))
check_true("[6g3] 旧 kv 键清单在（lock_pair_seq / unlock_in_flight）",
           "_LEGACY_KV_KEYS" in eng_src and '"lock_pair_seq"' in eng_src
           and '"unlock_in_flight"' in eng_src)
# 配对时代的三个方法必须**连名带体**消失，不只是调用点被删（防半途回潮）
for _m in ("_book_lock_pair", "_assert_lock_pair_invariant", "_upgrade_lock_pair"):
    check_true("[6h] 配对方法 {} 已从引擎删除".format(_m),
               "def {}(".format(_m) not in eng_src)
check_true("[6h2] 引擎不再持有 _lock_pair_seq 计数器",
           "_lock_pair_seq" not in eng_src)

for rel in ("Trading/Broker/DryRun.py", "Trading/Broker/SimNow.py"):
    src = read(rel)
    check_true("[6i] {} 代码里不再用 itertools 生成报单号".format(rel),
               "itertools" not in _code_only(src))
    check_true("[6j] {} 用 Base._next_order_id()".format(rel),
               "_next_order_id()" in src)

# ════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P29 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
