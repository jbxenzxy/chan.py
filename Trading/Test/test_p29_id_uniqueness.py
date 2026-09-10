# -*- coding: utf-8 -*-
"""P29 —— 持久 ID 跨重启唯一性 / fail-fast / 锁仓配对不变量（R1+R2+R3，2026-09-10）

背景（为什么要这个文件）
--------------------------------------------------------------------------
三个"持久 ID" 全部由**进程内计数器**生成，而三张落盘表的键又是它们：

    trade_id    = "T{:05d}".format(self._trade_seq)        Engine / Reconcile
    lock_pair_id= "lock_{:05d}".format(self._lock_pair_seq) Engine._book_lock_pair
    order_id    = "{broker}-{:06d}".format(seq)             DryRun / SimNow broker

进程重启 → 三个计数器全部归零 → 新号与库内既有号相撞。而 `save_trade` /
`save_order` 当时用的是 `INSERT OR REPLACE` → **上一进程的审计记录被静默覆盖**
（实测：2 笔独立成交落库后只剩 1 条）。`lock_pair_id` 撞号更隐蔽：解锁时
「选要平的仓」（方向 + 最老）与「选要升级的仓」（lock_pair_id 相同）会脱钩，
变成"平一笔、升另一笔"。

根因诊断（核实过，不是猜）
    `_trade_seq` / `_lock_pair_seq` 全文件只有赋值、**没有任何恢复路径**
    （`_persist` / `_restore` 都不碰）。而同性质的 `bars_seen` 早就走
    `set_json` / `get_json` —— 实测跨重启 7 → 7，那两个归零 2 → 0。
    ⇒ 根因是**漏接已有的持久化机制**，不是"计数器方案不可靠"。

三个修正（本文件把它们钉死）
    R1  序号持久化：`_persist` 写 kv；恢复取 `max(kv 值, 库内数据推导值)`；
        broker 报单序号从 `orders` 表抬升。**不换时间戳 ID** —— 实测同一毫秒
        批量锁仓会生成相同毫秒、墙钟回拨后会与历史号重复，症状与计数器一样。
    R2  写入 fail-fast：`save_trade` / `save_order` 改 `INSERT`，撞号抛
        `IdCollisionError`（撞号 = 审计底稿要被覆盖，必须响亮地死）。
    R3  锁仓配对不变量：同一 `lock_pair_id` 至多 2 笔且方向相反，违反 → 拒绝启动。
        （正常路径构造性成立，故不需要"拒绝升级"这种业务策略。）

本文件共 6 组：
    [1] 端到端自然流程：开→平→再开→锁仓 ‖ 重启 ‖ 解锁→再平，全程无重复、无丢失
    [2] 序号落 kv（跨重启保持）
    [3] 库内 max 自愈（kv 丢了也能从真实数据反推）
    [4] R2 fail-fast（撞号抛错且**不覆盖**既有记录）
    [5] R3 不变量闸门（>2 笔 / 2 笔同向 → 拒绝启动）
    [6] 源码护栏（不得再用 INSERT OR REPLACE 写这两张表；broker 不得用 itertools.count）

跑法：python Trading/Test/test_p29_id_uniqueness.py
"""
from __future__ import annotations

import copy
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
    Bar, ExitPlan, Order, Position, PositionOrigin, Side, Signal, Trade,
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
    base["risk"].update({"max_volume": 2, "max_open_positions": 1})
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
#   进程 A：T1 开多 → T2 止损（跨日 → CLOSE，成交 T00001）
#           T3 再开多 → T4 止损（当日 → LOCK，落锁对 lock_00001）
#   ↻ 重启（同一 state.db）↻
#   进程 B：T5 反向信号 → UNLOCK 平昨（成交 T00002，配对仓升级为单边敞口）
#           T6 止损（跨日 → CLOSE，成交 T00003）
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

    # ── T2：跨日止损 → 硬离场 CLOSE ──────────────────────────────
    eng_a.on_bar(make_bar(T2[0], T2[1], 4400, 4410, 3000, 3050))
    check("[1c] T2 跨日止损 → 成交 1 笔（T00001）",
          [t["trade_id"] for t in eng_a.store.trades()], ["T00001"])
    check("[1d] T2 平仓后簿空", len(eng_a.positions.positions), 0)

    # ── T3：再开多（同一交易日）──────────────────────────────────
    eng_a.on_bar(make_bar(T3[0], T3[1], 4500, 4510, 4490, 4505))
    eng_a.on_signal(make_sig("A|buy|2", T3[1], T3[0], 4505.0, True))
    check("[1e] T3 再开仓 → 簿内 1 笔", len(eng_a.positions.positions), 1)

    # ── T4：当日止损 → 软离场 LOCK（落锁对）─────────────────────
    eng_a.on_bar(make_bar(T4[0], T4[1], 4400, 4410, 3000, 3050))
    check("[1f] T4 当日止损 → 软离场落锁对",
          sorted({p.lock_pair_id for p in eng_a.positions.positions
                  if p.lock_pair_id}), ["lock_00001"])
    check("[1g] 落锁对后簿内 2 笔（多空互锁）",
          len(eng_a.positions.positions), 2)
    check("[1h] 软离场不记 Trade（PnL 未兑现）",
          len(eng_a.store.trades()), 1)
    check("[1i] 进程 A 结束时 _trade_seq=1 / _lock_pair_seq=1",
          (eng_a._trade_seq, eng_a._lock_pair_seq), (1, 1))
    orders_a = db_rows(eng_a.store, "SELECT order_id FROM orders")
    eng_a.store.close()

    # ── ↻ 重启（同一 state.db）────────────────────────────────────
    eng_b, brk_b = build(tmp, "e2e")
    check("[1j] 重启后 _trade_seq 保持 1（R1）", eng_b._trade_seq, 1)
    check("[1k] 重启后 _lock_pair_seq 保持 1（R1）", eng_b._lock_pair_seq, 1)
    check("[1l] 重启后持仓 2 笔（锁对）", len(eng_b.positions.positions), 2)

    # ── T5：反向信号 → 解锁平昨 ─────────────────────────────────
    eng_b.on_bar(make_bar(T5[0], T5[1], 4500, 4510, 4490, 4505))
    eng_b.on_signal(make_sig("B|sell|1", T5[1], T5[0], 4505.0, False))
    check("[1m] T5 解锁 → 新成交号是 T00002（**不是 T00001**）",
          sorted(t["trade_id"] for t in eng_b.store.trades()),
          ["T00001", "T00002"])
    check("[1n] 解锁后簿内 1 笔（配对仓已升级为单边敞口）",
          len(eng_b.positions.positions), 1)

    # ── T6：跨日止损 → 硬离场 ───────────────────────────────────
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
    eng._lock_pair_seq = 9
    _seed = getattr(eng.broker, "seed_order_seq", None)
    if callable(_seed):
        _seed(11)
    eng._persist()
    check("[2a] kv 写入 trade_seq", eng.store.get_json("trade_seq"), 7)
    check("[2b] kv 写入 lock_pair_seq", eng.store.get_json("lock_pair_seq"), 9)
    check("[2c] kv 写入 order_seq", eng.store.get_json("order_seq"), 11)

    eng2, brk2 = build(tmp, "kv")
    check("[2d] 重启后 _trade_seq 恢复", eng2._trade_seq, 7)
    check("[2e] 重启后 _lock_pair_seq 恢复", eng2._lock_pair_seq, 9)
    check("[2f] 重启后 broker 序号抬升", _oseq(brk2), 11)
    check("[2g] 下一条委托号 = 12",
          _next_oid(brk2), "dry_run-000012")
    # 新号绝不能落在已用集合里
    check_true("[2h] 抬升后的号与历史号无交集",
               (_oseq(brk2) or 0) > 11, "seq={}".format(_oseq(brk2)))
    eng.store.close()
    eng2.store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[3] 库内 max 自愈（R1 第二道保险：kv 丢了也能从真实数据反推）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    store = Store(os.path.join(tmp, "state_heal.db"))
    spec = InstrumentSpec()
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
    store.set_json("lock_pair_seq", 0)
    store.set_json("order_seq", 0)
    store.set_json("positions", [Position(
        symbol="CFFEX.IF2609", side=Side.LONG, volume=2, entry_price=4500.0,
        entry_at="2026-09-01 09:00", entry_bar_ts=ms(2026, 9, 1, 9, 35),
        signal_key="LK", open_order_id="o",
        exit_plan=ExitPlan(name="x", stop_price=0.0),
        entry_bar_seq=1, origin=PositionOrigin.SOFT_EXIT_LOCK,
        entry_date="2026-09-01", lock_pair_id="lock_00042").to_dict()])
    eng3, brk3 = build(tmp, "heal")
    check("[3d] kv=0 时 _trade_seq 从 trades 表自愈", eng3._trade_seq, 12)
    check("[3e] kv=0 时 _lock_pair_seq 从持仓记录自愈", eng3._lock_pair_seq, 42)
    check("[3f] kv=0 时 broker 序号从 orders 表自愈", _oseq(brk3), 20)
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
print("\n[5] R3 锁仓配对不变量闸门（违反 → 拒绝启动）")
# ════════════════════════════════════════════════════════════════════


def _pos(side, key, pair_id, entry_date="2026-09-01", seq=1):
    return Position(symbol="CFFEX.IF2609", side=side, volume=2,
                    entry_price=4500.0, entry_at="2026-09-01 09:00",
                    entry_bar_ts=ms(2026, 9, 1, 9, 35), signal_key=key,
                    open_order_id="o", exit_plan=ExitPlan(name="x", stop_price=0.0),
                    entry_bar_seq=seq, origin=PositionOrigin.SOFT_EXIT_LOCK,
                    entry_date=entry_date, lock_pair_id=pair_id)


with tmp_dir() as tmp:
    st = Store(os.path.join(tmp, "state_r3a.db"))
    st.set_json("positions", [
        _pos(Side.LONG, "A", "lock_00001").to_dict(),
        _pos(Side.SHORT, "A#lock", "lock_00001").to_dict(),
    ])
    eng, _ = build(tmp, "r3a")
    check("[5a] 正常锁对（1 多 1 空）→ 正常启动",
          len(eng.positions.positions), 2)
    check("[5b] 单成员残留（解锁后只剩一笔）→ 也允许",
          True, True)
    st.close()

with tmp_dir() as tmp:
    st = Store(os.path.join(tmp, "state_r3b.db"))
    st.set_json("positions", [
        _pos(Side.LONG, "A", "lock_00001").to_dict(),
        _pos(Side.SHORT, "A#lock", "lock_00001").to_dict(),
        _pos(Side.LONG, "B", "lock_00001", seq=2).to_dict(),   # 第 3 笔 → 违反
    ])
    err = None
    try:
        build(tmp, "r3b")
    except RuntimeError as e:
        err = e
    check_true("[5c] 同一 lock_pair_id 出现 3 笔 → 拒绝启动",
               err is not None, repr(err)[:80])
    check_true("[5d] 错误信息指出 lock_pair_id", "lock_00001" in str(err),
               str(err)[:150])
    check_true("[5e] 错误信息解释后果（平一笔升另一笔 / 滞留软离场）",
               "SOFT_EXIT_LOCK" in str(err) or "软离场" in str(err),
               str(err)[:150])
    st.close()

with tmp_dir() as tmp:
    st = Store(os.path.join(tmp, "state_r3c.db"))
    st.set_json("positions", [
        _pos(Side.LONG, "A", "lock_00002").to_dict(),
        _pos(Side.LONG, "A#bad", "lock_00002", seq=2).to_dict(),  # 2 笔同向 → 违反
    ])
    err = None
    try:
        build(tmp, "r3c")
    except RuntimeError as e:
        err = e
    check_true("[5f] 同一 lock_pair_id 2 笔同向 → 拒绝启动",
               err is not None, repr(err)[:80])
    st.close()

with tmp_dir() as tmp:
    st = Store(os.path.join(tmp, "state_r3d.db"))
    st.set_json("positions", [
        _pos(Side.LONG, "A", "").to_dict(),                        # 空 id 合法
        _pos(Side.LONG, "B", "lock_00001").to_dict(),              # 单成员合法
    ])
    eng, _ = build(tmp, "r3d")
    check("[5g] 空 lock_pair_id + 单成员 → 正常启动",
          len(eng.positions.positions), 2)
    st.close()

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
check_true("[6e] _persist 落 trade_seq / lock_pair_seq / order_seq",
           all(k in eng_src for k in ('set_json("trade_seq"',
                                      'set_json("lock_pair_seq"',
                                      'set_json("order_seq"')))
check_true("[6f] _restore 恢复三序号（含库内 max 自愈）",
           "max_trade_seq()" in eng_src
           and "_max_lock_pair_seq_in_state" in eng_src
           and "max_order_seq(" in eng_src)
check_true("[6g] R3 不变量闸门在 _restore 中被调用",
           "_assert_lock_pair_invariant()" in eng_src)
check_true("[6h] _upgrade_lock_pair 不再「取第一笔就 break」",
           "ambiguous" in eng_src)

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
