# -*- coding: utf-8 -*-
"""
P35 「今日单永不 CLOSE」（硬约束 · 契约测试，2026-09-11）
=========================================================
背景（文档 §5.8.5 行 14 / 不变量 6 / 引擎 _pre_trade_check 注释）
--------------------------------------------------------------------
中金所没有平今指令：对**今仓**发 CLOSE 会被交易所当平昨处理，并按**平今费率**
（0.0345%，是平昨的 15 倍）收费；上期所 / 能源中心则需要 CLOSETODAY 才平今。
规则 ⑷⑸⑹⑺ 保证正常路径下 ⑤ 平仓的目标必为**跨日仓**，因此 CLOSE 恒按平昨计费、
不会误收平今费（p34 [5] 已验证费率相等）。

但"正常路径不产生"不够——这是**全交易所安全**的最后一道闸，必须写成硬断言。
本测试从两层钉死「今日单永不 CLOSE」：

  [1] 引擎层硬约束：`_pre_trade_check` 对「CLOSE 目标 entry_date == 今日」返回
      `"close_target_is_today"` 并拒绝报单；对跨日目标放行。
  [2] 行为层：今仓 + 同根 K 线触发 L1 → 引擎走 ④ 反向开仓（intent=OPEN），
      **绝不**发 ⑤ CLOSE。净敞口归 0（LOCKED），且全程没有任何 intent=close 的报单。

覆盖
  [1] 单测 _pre_trade_check（转移到 ⑤ 的前置校验链，A3 唯一出口）
  [2] 端到端：今仓当日触发离场 → 必为 ④ 锁仓，无 ⑤ 平仓

跑法：python Trading/Test/test_p35_today_never_close.py
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

from Trading import Broker  # noqa: E402,F401
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import AccountState, Bar, OrderIntent, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"
P0 = 4520.0
P_EXIT = 4110.0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p35_%s_" % tag)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                                 "use_trailing": False})
    return TradingConfig.from_dict(base)


def make_bar(ts, date, o, h, l, c):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2", is_buy=is_buy,
                  price=price, high=price + 10.0, low=price - 10.0,
                  fractal_low=price - 12.0, fractal_high=price + 12.0)


def build_engine(tmpdir):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}),
        LayeredExitPolicy(make_cfg().exit_params.model_dump()),
        Store(os.path.join(tmpdir, "state.db")),
        EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                 echo_kinds=None))


print("\n[1] 引擎层硬约束：_pre_trade_check 拒绝「CLOSE 目标 = 今仓」")
eng = build_engine(tempfile.mkdtemp(prefix="tg_p35_gate_"))
# 目标仓：用最小对象承载 _pre_trade_check 读取的字段（entry_date / volume）
today_target = SimpleNamespace(entry_date=D1, volume=2, side=Side.LONG,
                               signal_key="X|buy|1")
past_target = SimpleNamespace(entry_date="2026-09-01", volume=2,
                               side=Side.LONG, signal_key="X|buy|1")
act_close_today = _Action(intent=OrderIntent.CLOSE, side=Side.SHORT,
                           volume=2, target=today_target, is_exit=True,
                           transition=5)
act_close_past = _Action(intent=OrderIntent.CLOSE, side=Side.SHORT,
                          volume=2, target=past_target, is_exit=True,
                          transition=5)
why_today = eng._pre_trade_check(act_close_today, D1, None)
why_past = eng._pre_trade_check(act_close_past, D1, None)
check("[1a] 今仓 CLOSE → 拒绝原因 'close_target_is_today'",
      why_today, "close_target_is_today")
check("[1b] 跨日仓 CLOSE → 通过（None）", why_past, None)
check("[1c] 今仓 CLOSE 不报单（exit 路径在 _execute 内被该闸拦下）",
      why_today is not None, True)


print("\n[2] 行为层：今仓当日触发 L1 → 必走 ④ 锁仓（OPEN），不发 ⑤ CLOSE")
with tmp_dir("beh") as tmp:
    eng = build_engine(tmp)
    # 同一交易日 D1 内：先开仓（entry_date=D1），再同根不利 K 线触发离场
    eng.on_bar(make_bar(1000, D1 + " 09:40", P0, P0 + 10, P0 - 10, P0))
    eng.on_signal(make_sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[2a] 开仓后 RUNNING", eng.account_state(), AccountState.RUNNING)
    eng.on_bar(make_bar(2000, D1 + " 14:55", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    # 离场应触发 → 净敞口归 0（LOCKED）
    check("[2b] 当日离场后净敞口归 0 → LOCKED（④ 反向开仓，非 ⑤ 平仓）",
          eng.account_state(), AccountState.LOCKED)
    intents = [o.meta.get("intent") for o in eng.broker.orders]
    check("[2c] 全程**没有** intent=close 的报单（今日单永不 CLOSE）",
          "close" not in intents, True)
    check("[2d] 离场那笔是 intent=open（④ 反向开仓锁仓）",
          intents.count("open") >= 2, True)


print("\n" + "=" * 60)
print("P35 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
