# -*- coding: utf-8 -*-
"""
P40 D19 平仓冷却 / 连续被拒清幻影仓（契约测试，2026-09-11）
=============================================================
背景（文档 §5.2.2 / 引擎 _note_close_rejected / auto_order_status.close_cooldown）
------------------------------------------------------------------------------------
CLOSE（转移 ⑤）被拒分两类处理：
  · 追价无用类（资金不足 / 非交易时段）→ 立即停追（D10）；
  · 价格不可达（FOK 全撤 / 涨跌停）→ 继续追，但同一笔平仓刚被拒过就先别每根 bar
    都砸单 —— 进入 **CLOSE 冷却**（close_retry_bars 根），冷却期内 _execute 直接跳过
    ⑤ 报单（D19）。
  · 连续被拒达上限（close_max_streak）→ 认定该仓在柜台不存在（幻影仓），从簿中清除
    + 发 **severe 告警** `close_repeatedly_rejected`（D11 触发源 ③）。

本测试钉死 D19 的三条不变量：
  [1] 首次 CLOSE 被拒 → 进入冷却（_in_close_cooldown()=True，status.close_cooldown
      可见），streak=1，仓仍在簿（不误清）；
  [2] 冷却窗=1 时每根 bar 重新尝试；连拒达上限 → 清掉该仓、net 归 0、streak 归零；
  [3] 清仓同时发 severe 告警 close_repeatedly_rejected，且 status.alerts 可见、
       簿内无残留幻影仓。

覆盖
  [1]~[3] 如上（用「开仓成交 / 平仓拒单」broker 驱动，不依赖真实柜台）

跑法：python Trading/Test/test_p40_close_cooldown_streak.py
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from contextlib import contextmanager

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
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import AccountState, Bar, OrderIntent, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"
D2 = "2026-09-03"
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
    d = tempfile.mkdtemp(prefix="tg_p40_%s_" % tag)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


class CloseRejectBroker(DryRunBroker):
    """开仓（OPEN）正常成交；平仓（CLOSE）一律拒单（模拟盘口深度不足 FOK 全撤）。"""

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        o = super().submit(intent, side, volume, ref_price, signal_key,
                            note=note, entry_date=entry_date, is_exit=is_exit)
        if intent is OrderIntent.CLOSE:
            o.status = "rejected"
            o.filled_price = None
            o.meta["reject_reason"] = "depth"
            o.meta["reject_class"] = "price"
        return o


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                                 "use_trailing": False})
    # 冷却窗 = 1（每根 bar 重新尝试），连拒上限 = 2（两根即清幻影仓）
    base["engine"]["close_retry_bars"] = 1
    base["engine"]["close_max_streak"] = 2
    return TradingConfig.from_dict(base)


def make_bar(ts, date, o, h, l, c):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2", is_buy=is_buy,
                  price=price, high=price + 10.0, low=price - 10.0,
                  fractal_low=price - 12.0, fractal_high=price + 12.0)


print("\n[1] 首次 CLOSE 被拒 → 进入冷却、streak=1、仓仍在簿（不误清）")
with tmp_dir("cool") as tmp:
    spec = InstrumentSpec()
    eng = TradingEngine(
        make_cfg(), CloseRejectBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}),
        LayeredExitPolicy(make_cfg().exit_params.model_dump()),
        Store(os.path.join(tmp, "state.db")),
        EventLog(os.path.join(tmp, "events.jsonl"), echo=False,
                 echo_kinds=None))
    eng.on_bar(make_bar(1000, D1 + " 09:40", P0, P0 + 10, P0 - 10, P0))
    eng.on_signal(make_sig("X|buy|1", D1 + " 09:40", 1000, P0, True))
    check("[1a] 开仓后 RUNNING", eng.account_state(), AccountState.RUNNING)
    # D2 不利 K 线 → ⑤ CLOSE（被拒）
    eng.on_bar(make_bar(3000, D2 + " 14:55", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    check("[1b] 首次被拒后净敞口仍在 → RUNNING（未误清）",
          eng.account_state(), AccountState.RUNNING)
    check_true("[1c] 进入 CLOSE 冷却（_in_close_cooldown）",
               eng._in_close_cooldown(), eng._in_close_cooldown())
    st = eng.auto_order_status()
    check("[1d] status.close_cooldown.active 可见", st["close_cooldown"]["active"],
          True)
    check("[1e] 连拒计数 streak = 1", st["close_cooldown"]["streak"], 1)


    print("\n[2] 冷却窗=1 时每根 bar 重试；连拒达上限 → 清幻影仓、net 归 0")
    # 再喂一根 D2 不利 K 线 → 重新尝试 ⑤ → 再次被拒 → streak 达 2 → 清仓
    eng.on_bar(make_bar(4000, D2 + " 14:56", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    check("[2a] 连拒达上限后净敞口归 0 → FLAT", eng.account_state(),
          AccountState.FLAT)
    check("[2b] 清仓后 streak 归零", eng._close_fail_streak, 0)
    check("[2c] 簿内无残留幻影仓（持仓数 = 0）",
          len(eng.positions.positions), 0)
    st2 = eng.auto_order_status()
    codes = {a["code"] for a in st2["alerts"]}
    check_true("[2d] 清仓同时发 severe 告警 close_repeatedly_rejected",
               "close_repeatedly_rejected" in codes, codes)


print("\n" + "=" * 60)
print("P40 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
