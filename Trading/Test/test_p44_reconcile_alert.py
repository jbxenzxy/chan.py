# -*- coding: utf-8 -*-
"""
P44 对账同步弹窗（2026-09-17 拍板新增，正向用例）
====================================================
钉死 §4.3 步骤 3 的**新行为**：对账发现账实不一致 → 弹窗先入队（内容如实、
含不一致详情）、同步随即同轮执行 —— 弹窗与同步是"告知 + 落实"，不是
"等确认才动手"的挂起机制。

  [1] 柜台少仓（多为手工平仓）→ severe 弹窗 `reconcile_externally_closed`
      + 账本 FIFO 删除 + 补记平仓盈亏 + 状态按剩余持仓转换
  [2] 柜台多仓（多为手工加仓）→ severe 弹窗 `position_mismatch`
      + 引擎不接管、账本不动
  [3] 柜台与账本一致 → 不弹窗、不动簿（安静路径不受影响）

跑法：python Trading/Test/test_p44_reconcile_alert.py
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


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p44_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from dataclasses import replace as _dc_replace  # noqa: E402

from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402
from Trading.Infra.Records import AccountState, Bar, ExitPlan, Position, Side  # noqa: E402
from Trading.Infra.StateDB import Store  # noqa: E402
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_IF = _dc_replace(PRODUCT_PROFILES["IF"],
                  exec_policy=_dc_replace(PRODUCT_PROFILES["IF"].exec_policy,
                                          lots_per_order=1))

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def read_events(eng, kinds=None, tail_n=200):
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
    except OSError:
        pass
    return out


def seed_run(store, side="LONG", volume=1, anchor=4545.0, signal_key="seed-run"):
    """D15 / G2：手工写持仓必须配套写 run，否则启动被拒。"""
    store.set_json("run", {
        "side": side, "anchor": anchor, "volume": volume,
        "bar_ts": 4100, "bar_seq": 1, "signal_key": signal_key,
        "plan": {"name": "seed_plan", "stop_price": anchor - 10.0,
                 "tp_price": anchor + 10.0, "params": {}},
    })


class RealPositionBroker(DryRunBroker):
    """柜台真实持仓可注入：real_longs / real_shorts（None = 未武装 → skip）。"""

    name = "real_position_p44"

    def __init__(self, spec, params=None, *, real_longs=None, real_shorts=None):
        super().__init__(spec, params or {"sim_equity": 1_000_000.0})
        self._real_longs = real_longs
        self._real_shorts = real_shorts

    def real_position(self, side):
        if side is Side.LONG:
            return self._real_longs
        return self._real_shorts


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST",
                  entry_date="2026-08-31"):
    if side is Side.LONG:
        tp = entry_price + 5.0
        stop = entry_price - 10.0
    else:
        tp = entry_price - 5.0
        stop = entry_price + 10.0
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price,
        entry_at=entry_date + " 09:30", entry_bar_ts=4100,
        signal_key=signal_key, open_order_id="p44-" + signal_key,
        exit_plan=ExitPlan(name="x", stop_price=stop, tp_price=tp),
        entry_bar_seq=entry_bar_seq, entry_date=entry_date)


def build(tmpd, broker, *, positions=(), bars_seen=10):
    """直接在引擎对象上构造账本状态（不经 on_signal），run 锚按净敞口补齐。"""
    eng = TradingEngine(
        TradingConfig.from_dict(DEFAULT_CONFIG), broker,
        EntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpd, "state.db")),
        EventLog(os.path.join(tmpd, "events.jsonl"), echo=False, echo_kinds=None))
    eng.bars_seen = bars_seen
    eng.last_bar = make_bar()
    for pos in positions:
        eng.positions.add(pos)
    if not eng.positions.is_empty():
        net = eng.positions.net_volume()
        side = "LONG" if net > 0 else "SHORT"
        seed_run(eng.store, side=side, volume=abs(net))
    return eng


# ════════════════════════════════════════════════════════════════
print("\n[1] 柜台少仓（手工平仓）→ 弹窗 + 同步同轮完成")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as td:
    broker = RealPositionBroker(
        Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF),
        real_longs=0, real_shorts=0)
    eng = build(td, broker, positions=[
        make_position(Side.LONG, 2, 4545.0, 1, signal_key="P44-1-L")])
    check("[1a] 前置：账本 1 笔多头 2 手", eng.positions.net_volume(), 2)

    eng._reconcile_positions(source="test")

    alerts = [a for a in eng._alerts
              if a.get("code") == "reconcile_externally_closed"]
    check_true("[1b] ★ severe 弹窗 reconcile_externally_closed 已入队",
               len(alerts) == 1)
    check("[1c] 弹窗级别 = severe",
          (alerts[-1].get("level") if alerts else None), "severe")
    _m1 = (alerts[-1].get("msg", "") if alerts else "")
    check_true("[1d] 弹窗内容含不一致详情（柜台/账本手数）与『已同步』",
               (alerts and "柜台" in _m1 and "2" in _m1 and "已同步" in _m1))
    check("[1e] ★ 账本已同步（2 手全删，与柜台一致）",
          eng.positions.is_empty(), True)
    check_true("[1f] 补记平仓盈亏（Trade 落账）",
               len(eng.store.trades()) == 1)
    check_true("[1g] 写 position_externally_closed 事件",
               len(read_events(eng, kinds={"position_externally_closed"})) >= 1)
    check("[1h] 状态按剩余持仓转换 → FLAT",
          eng.account_state(), AccountState.FLAT)

# ════════════════════════════════════════════════════════════════
print("\n[2] 柜台多仓（手工加仓）→ 弹窗不接管、账本不动")
# 注：对账只比对**账本已有侧**（Reconcile 对空侧 skip）——
#     "账本空 + 柜台多仓"按设计不可见，mismatch 的可观测形态是
#     账本记 1 手、柜台有 2 手（差额部分不接管）。
# ════════════════════════════════════════════════════════════════
with tmp_dir() as td:
    broker = RealPositionBroker(
        Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF),
        real_longs=2, real_shorts=0)
    eng = build(td, broker, positions=[
        make_position(Side.LONG, 1, 4545.0, 1, signal_key="P44-2-L")])
    check("[2a] 前置：账本 1 手、柜台 2 手", eng.positions.net_volume(), 1)

    eng._reconcile_positions(source="test")

    alerts = [a for a in eng._alerts if a.get("code") == "position_mismatch"]
    check_true("[2b] ★ severe 弹窗 position_mismatch 已入队",
               len(alerts) == 1)
    check("[2c] 弹窗级别 = severe",
          (alerts[-1].get("level") if alerts else None), "severe")
    _m2 = (alerts[-1].get("msg", "") if alerts else "")
    check_true("[2d] 弹窗内容含详情与人工处理指引",
               ("柜台" in _m2 and "不接管" in _m2))
    check("[2e] ★ 账本不动（只保留账本原有的 1 手，不接管多出的 1 手）",
          eng.positions.net_volume(), 1)
    check_true("[2f] 写 position_mismatch 事件",
               len(read_events(eng, kinds={"position_mismatch"})) == 1)
    check("[2g] 状态仍 RUNNING（原 1 手仍是净敞口）",
          eng.account_state(), AccountState.RUNNING)

# ════════════════════════════════════════════════════════════════
print("\n[3] 账实一致 → 不弹窗、不动簿")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as td:
    broker = RealPositionBroker(
        Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"), _IF),
        real_longs=2, real_shorts=0)
    eng = build(td, broker, positions=[
        make_position(Side.LONG, 2, 4545.0, 1, signal_key="P44-3-L")])

    eng._reconcile_positions(source="test")

    _rc_codes = {"reconcile_externally_closed", "position_mismatch"}
    check_true("[3a] 无对账类弹窗",
               len([a for a in eng._alerts if a.get("code") in _rc_codes]) == 0)
    check("[3b] 簿不动", eng.positions.net_volume(), 2)
    check("[3c] 状态仍 RUNNING", eng.account_state(), AccountState.RUNNING)

# ════════════════════════════════════════════════════════════════
print("\n结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
sys.exit(0 if _FAIL == 0 else 1)
