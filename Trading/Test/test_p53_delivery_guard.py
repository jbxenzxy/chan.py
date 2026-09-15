# -*- coding: utf-8 -*-
"""
P53 交割月护栏（Phase 11 · 阻塞点 4 · D8）契约（2026-09-14）
=============================================================
设计前提（拍板，见实施计划 §6.2 Phase 11 行）：
  · **护栏对象 = 现行主力 `trade_symbol`** 的 last_trade_date —— 主连换月
    （IF2609→IF2610）时 trade_symbol 更新，判定随之解除；
  · **判据 = 剩余交易日 < N 即拦**，N 默认 1（仅最后交易日当天拦）；
  · 只拦**开新仓**（_pre_trade_check 的 OPEN 分支消费）；**平旧仓/离场永不拦**；
  · last_trade_date **未知**（离线 dry_run / 行情未取到）→ 不拦
    （"不校验未知的东西"，与涨跌停护栏同哲学）。
  · 天数口径只数工作日（Mon-Fri，不含节假日）—— 跨越周末不计入剩余交易日，
    使「最后交易日为周一、今天上周五」剩 1 日，N=1 不拦。

关键数据：2026-09-14 周一，2026-09-18 周五。

覆盖：
  [1] `delivery_guard_blocked`（N=1）：
        a. last_trade_date 未知 → False（降级不拦）
        b. last_trade_date == today → True（剩 0 < 1 拦）
        c. 前一天（工作日，剩 1 日）→ False（不拦）
        d. N=2、我晓3 日前 → True（剩 2 < 3 拦）
        e. 跨周末：last=周一、today=上周五 → 剩 1 日 → N=1 不拦
  [2] `_weekdays_between` 边界（同 Date，含 end / 跨周末计数）
  [3] Engine._pre_trade_check 挂点：
        a. OPEN + today=最后交易日 → 拒 "delivery_guard_blocked"
        b. OPEN + 现行主力换了远月（last 在 10 月）→ 放行（None）
        c. CLOSE + 今天=最后交易日 → **不**被交割月护栏拦
  [4] 配置旋钮 `risk.delivery_guard_days` 生效（配 2 → 距 2 日也拦，配 0 → 恒放行）

跑法：python Trading/Test/test_p53_delivery_guard.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

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

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import (  # noqa: E402
    Instrument, _weekdays_between)
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402


_IF = PRODUCT_PROFILES["IF"]


def _inst(ltd=""):
    """现场 Instrument + 指定最后交易日（P-B：last_trade_date 是运行时字段）。"""
    ins = Instrument(None, _IF)
    ins.last_trade_date = ltd
    return ins
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.Records import Bar, ExitPlan, OrderIntent, Position, Side, Signal
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"
D_MAIN = "2026-09-18"     # 现行主力（IF2609）最后交易日 = 周五
D_MON = "2026-09-21"      # 换月后新主力（IF2610）最后交易日（设在周一，探跨周末）
D_FAR = "2026-10-16"      # 远月（10 月交割，远大于 N）


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p53_%s_" % tag)
    try:
        yield d
    finally:
        pass


def make_bar(ts, date=D1 + " 09:40", o=4500.0, h=4510.0, l=4490.0, c=4505.0):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(price=4500.0, ts=1001):
    return Signal(key="p53", symbol="KQ.m@CFFEX.IF", freq="5m", date=D1 + " 09:41",
                  timestamp=ts, bsp_type="1", is_buy=True, price=price,
                  high=price + 10.0, low=price - 10.0,
                  fractal_low=price - 12.0, fractal_high=price + 12.0)


def make_pos(entry_date=D1, vol=2, side=Side.LONG):
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol, entry_price=4500.0,
        entry_at="2026-09-01 09:00", entry_bar_ts=1000,
        signal_key="p53", open_order_id="p53-o1",
        exit_plan=ExitPlan(name="x", stop_price=4490.0),
        entry_bar_seq=1, entry_date=entry_date)


def build_engine(tmpdir, cfg, spec, tag="a"):
    return TradingEngine(
        cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        EntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def make_cfg(guard_days=1):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["instrument"]["signal_symbol"] = "KQ.m@CFFEX.IF"
    # P-B：配置不再携带 exchange（归品种档案；写旧键会触发 _check_removed_keys）
    base["risk"]["max_volume"] = 2
    base["risk"]["delivery_guard_days"] = guard_days
    base["exit_params"].update({"use_atr": False,
                                "use_trailing": False})
    return TradingConfig.from_dict(base)


# ════════════════════════════════════════════════════════════════
print("\n[1] Instrument.delivery_guard_blocked（N=1 默认；P-B：last_trade_date 为运行时字段）")
# ════════════════════════════════════════════════════════════════
check("[1a] last_trade_date 未知 → 不拦（降级）",
      _inst("").delivery_guard_blocked(D_MAIN), False)
check("[1b] last_trade_date == today（IF2609 交割日当天）→ 拦",
      _inst(D_MAIN).delivery_guard_blocked(D_MAIN), True)
check("[1c] 前一天（9/17 周四，剩 1 个交易日）→ 不拦",
      _inst(D_MAIN).delivery_guard_blocked("2026-09-17"), False)
check("[1d] N=2、仅剩 1 个交易日（9/17→9/18）→ 拦",
      _inst(D_MAIN).delivery_guard_blocked(
          "2026-09-17", threshold_days=2), True)
check("[1e] N=2、剩 2 个交易日（9/16→9/18）=N → 不拦（rem<N 才拦）",
      _inst(D_MAIN).delivery_guard_blocked(
          "2026-09-16", threshold_days=2), False)
check("[1f] N=3、剩 2 个交易日 → 拦",
      _inst(D_MAIN).delivery_guard_blocked(
          "2026-09-16", threshold_days=3), True)
check("[1g] N=3、剩 4 个交易日（9/14→9/18）→ 不拦",
      _inst(D_MAIN).delivery_guard_blocked(
          "2026-09-14", threshold_days=3), False)
check("[1h] 跨周末：last=周一 9/21、today=周五 9/18 → 剩 1 日 → N=1 不拦",
      _inst(D_MON).delivery_guard_blocked("2026-09-18"), False)
check("[1i] 跨周末：last=周一 9/21、today=周四 9/17 → 剩 2 日 → N=3 拦",
      _inst(D_MON).delivery_guard_blocked(
          "2026-09-17", threshold_days=3), True)
check("[1j] 已过期（today > last_trade_date）→ 拦（剩 0）",
      _inst(D1).delivery_guard_blocked(D_MAIN), True)

# ════════════════════════════════════════════════════════════════
print("\n[2] _weekdays_between 边界（区间 (start, end]，仅工作日）")
# ════════════════════════════════════════════════════════════════
check("[2a] 同日期 → 0", _weekdays_between(D_MAIN, D_MAIN), 0)
check("[2b] 前一天（周四→周五）→ 1", _weekdays_between("2026-09-17", D_MAIN), 1)
check("[2c] 前两日（周三→周五）→ 2", _weekdays_between("2026-09-16", D_MAIN), 2)
check("[2d] end<start → 0", _weekdays_between(D_MAIN, "2026-09-14"), 0)
check("[2e] 跨周末（周五→下周一）→ 1（只计周一）",
      _weekdays_between(D_MAIN, D_MON), 1)
check("[2f] 非法/空串 → 0", _weekdays_between("", D_MAIN), 0)

# ════════════════════════════════════════════════════════════════
print("\n[3] Engine._pre_trade_check 挂点 —— 三态×意图（dry_run；dry_run 是 offline → A′ 闸门放行）")
print("    空仓态: 今天=交割日 → 拦【开仓】；锁仓态: → 拦【平仓】；运行态: → 都不拦")
# ════════════════════════════════════════════════════════════════
#   注：Engine.state = cfg.instrument，交割日直接对 eng.state 赋值（模拟行情回填）。
#   账户态由持仓派生：无仓=FLAT；净敞口≠0=RUNNING；净敞口0且簿非空=LOCKED。
# 3a：空仓态(FLAT) + OPEN + today=最后交易日 → 拦（不让新进裸仓）
with tmp_dir("t3a") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF), "a")
    eng.state.last_trade_date = D_MAIN
    eng.on_bar(make_bar(1000))
    act = _Action(OrderIntent.OPEN, Side.LONG, 2)
    why = eng._pre_trade_check(act, D_MAIN, make_sig(), ref_price=4500.0)
    check("[3a] 空仓态 OPEN + 交割日 → 拦", why, "delivery_guard_blocked")

# 3b：运行态(RUNNING) + today=交割日 → 开(④锁仓)/平(⑤) 都不拦
with tmp_dir("t3b") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF), "b")
    eng.state.last_trade_date = D_MAIN
    eng.positions.add(make_pos(entry_date=D1, vol=2, side=Side.LONG))  # → RUNNING
    eng.on_bar(make_bar(1000))
    check("[3b0] 前置：pn 已增持 → RUNNING", eng.account_state().value, "running")
    why_open = eng._pre_trade_check(_Action(OrderIntent.OPEN, Side.SHORT, 2),
                                    D_MAIN, make_sig(), ref_price=4500.0)
    check("[3b1] 运行态 OPEN(④锁仓) + 交割日 → 不拦", why_open, None)
    why_close = eng._pre_trade_check(
        _Action(OrderIntent.CLOSE, Side.LONG, 1, target=make_pos(entry_date=D1)),
        D_MAIN, make_sig(), ref_price=4500.0)
    check("[3b2] 运行态 CLOSE(⑤) + 交割日 → 不拦（≠ delivery_guard_blocked）",
          why_close != "delivery_guard_blocked", True)

# 3c：锁仓态(LOCKED) + today=交割日 → 拦【平仓/解锁】；但 OPEN(②) 不拦
with tmp_dir("t3c") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF), "c")
    eng.state.last_trade_date = D_MAIN
    eng.positions.add(make_pos(entry_date=D1, vol=2, side=Side.LONG))
    eng.positions.add(make_pos(entry_date=D1, vol=2, side=Side.SHORT))
    eng.on_bar(make_bar(1000))
    check("[3c0] 前置：pn 多空对锁 → LOCKED", eng.account_state().value, "locked")
    act_close = _Action(OrderIntent.CLOSE, Side.LONG, 1,
                        target=make_pos(entry_date=D1, side=Side.LONG))
    why_close = eng._pre_trade_check(act_close, D_MAIN, make_sig(), ref_price=4500.0)
    check("[3c1] 锁仓态 CLOSE(③解锁) + 交割日 → 栏", why_close, "delivery_guard_blocked")
    why_open = eng._pre_trade_check(_Action(OrderIntent.OPEN, Side.LONG, 2),
                                    D_MAIN, make_sig(), ref_price=4500.0)
    check("[3c2] 锁仓态 OPEN(②) + 交割日 → 不拦", why_open, None)

# 3d：空仓态 + 换月远月（last=FAR）→ OPEN 放行（换月自动解除）
with tmp_dir("t3d") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF), "d")
    eng.state.last_trade_date = D_FAR
    eng.on_bar(make_bar(1000))
    act = _Action(OrderIntent.OPEN, Side.LONG, 2)
    why = eng._pre_trade_check(act, D_MAIN, make_sig(), ref_price=4500.0)
    check("[3d] 空仓态 OPEN + 换月远月 → 放行 None", why, None)

# ════════════════════════════════════════════════════════════════
print("\n[4] 配置旋钮 risk.delivery_guard_days")
# ════════════════════════════════════════════════════════════════
# 4a：guard_days=2 → 空仓态仅剩 1 个交易日（9/17→9/18）也拦
with tmp_dir("t4a") as tmp:
    eng = build_engine(tmp, make_cfg(guard_days=2), Instrument(None, _IF), "a")
    eng.state.last_trade_date = D_MAIN
    eng.on_bar(make_bar(1000))
    act = _Action(OrderIntent.OPEN, Side.LONG, 2)
    why = eng._pre_trade_check(act, "2026-09-17", make_sig(), ref_price=4500.0)
    check("[4a] guard_days=2 + 空仓态 OPEN 前一天 → 拦", why, "delivery_guard_blocked")

# 4b：guard_days=0 → 恒放行（关闭护栏）—— 空仓态 OPEN 交割日当天也放行
with tmp_dir("t4b") as tmp:
    eng = build_engine(tmp, make_cfg(guard_days=0), Instrument(None, _IF), "b")
    eng.state.last_trade_date = D_MAIN
    eng.on_bar(make_bar(1000))
    act = _Action(OrderIntent.OPEN, Side.LONG, 2)
    why = eng._pre_trade_check(act, D_MAIN, make_sig(), ref_price=4500.0)
    check("[4b] guard_days=0 → 空仓态 OPEN 交割日当天也放行 None", why, None)

print("\nPASS:{} FAIL:{}".format(_PASS, _FAIL))
sys.exit(1 if _FAIL else 0)