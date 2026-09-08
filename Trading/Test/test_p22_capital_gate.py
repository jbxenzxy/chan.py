# -*- coding: utf-8 -*-
"""
P22 资金闸门（capital gate）单元测试 · 审计 P2-8 / P2-9
======================================================
背景（2026-09-06 全 FOK + 删除分仓改造后）
    一笔信号 = 一笔报单（≤20 手），资金闸门不再除以持仓笔数：
      K = 一手保证金 + 一手名义价值 × risk_unit_pct      （开 1 手的最低门槛）
      X = floor(可用资金 / K)                            （资金允许的最多手数上限）
    语义（比旧版更简单，P2-8 的"批次总手数超上限"问题随分仓删除自然消失）：
      · equity < K        → blocked=True，拒开本笔（risk_block）
      · sizer 手数 > X    → 截断为 X（capital_capped），这笔报单总手数 ≤ X
      · equity 未知/≤0    → 不拦（(False, None)），由其他风控兜底
      · dry_run 无权益    → 回退 risk.initial_cash 虚拟资金

    P2-9：资金闸门是实盘资金安全功能，须有专门测试。

本测试不连 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
    A 段 —— 直连 `engine._capital_gate`，验证边界语义。
    B 段 —— 走 `on_signal` 集成，验证"拒开 / 截断 / dry_run 虚拟资金"落地。
跑法：python tests/test_p22_capital_gate.py
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
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p22_")
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
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402


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


def make_signal(is_buy=True, price=4500.0, sig_key="P22-OPEN"):
    from Trading.Infra.Types import Signal
    return Signal(key=sig_key, symbol="CFFEX.IF", freq="5m",
                  date="2026-09-06 09:35", timestamp=0, bsp_type="3",
                  is_buy=is_buy, price=price, high=price + 2.0, low=price - 2.0)


def build_engine(tmpdir, sim_equity=1_000_000.0):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": sim_equity})
    entry = DefaultEntryPolicy({})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker


class EqBroker:
    """可控 equity 的 broker stub：equity(src) 返回预设值或 None。

    需要真实撮合（走 on_signal 开仓）时传 delegate=内部 DryRunBroker，
    submit 委托给它；纯测 _capital_gate 时可只传 eq。
    """
    name = "eq_stub"

    def __init__(self, eq, delegate=None):
        self._eq = eq
        self._delegate = delegate
        self.orders = []

    def equity(self, source="available"):
        return self._eq

    def submit(self, *a, **k):
        o = self._delegate.submit(*a, **k)
        self.orders.append(o)
        return o


class StubSizer:
    """可控每手保证金 / 手数 / 上限的 sizer stub（单笔模型：只有 size，无分仓）。"""
    def __init__(self, margin=150_000.0, risk_unit=0.01, max_vol=10,
                 src="available"):
        self._margin = float(margin)
        self.risk_unit_pct = float(risk_unit)
        self.max_volume = int(max_vol)
        self.equity_source = src
        self._per = 1
        self.unlock_no_new_open = True   # 严格模式：引擎直接读该属性

    def per_lot_margin(self, price):
        return self._margin

    def size(self, **kw):
        return self._per, "stub"


# ════════════════════════════════════════════════════════════════
# A 段 —— `engine._capital_gate` 边界语义（直连，单笔模型）
# ════════════════════════════════════════════════════════════════
print("\n[A] _capital_gate 边界语义")
# K = margin + notional*risk_unit，equity=X*K → X 取整。全用 spec 自洽推导。
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    margin = 150_000.0
    risk_unit = 0.01
    price = 4500.0
    notional = spec.points_to_cash(price, 1)
    k = margin + notional * risk_unit
    sig = make_signal(price=price)

    # A1：equity < K（连 1 手门槛都不够）→ blocked
    gb = EqBroker(k * 0.5)
    gs = StubSizer(margin=margin, risk_unit=risk_unit)
    engine.broker, engine.sizer = gb, gs
    blocked, cap = engine._capital_gate(sig)
    check("A1 equity<K -> (True, None)", (blocked, cap), (True, None))

    # A2：equity = 3K → cap = X = 3（单笔模型：不除以持仓笔数）
    gb = EqBroker(k * 3)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig)
    check("A2 equity=3K -> (False, 3)", (blocked, cap), (False, 3))

    # A3：equity = 7.5K → cap = floor(7.5) = 7
    gb = EqBroker(k * 7.5)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig)
    check("A3 equity=7.5K -> cap == 7", (blocked, cap), (False, 7))

    # A4：equity <= 0 → 不拦（(False,None)），由其他风控兜底
    gb = EqBroker(0)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig)
    check("A4 equity<=0 -> 不拦 (False,None)", (blocked, cap), (False, None))

    # A5：equity 未知（None，非 dry_run）→ 不拦（保守放行）
    gb = EqBroker(None)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig)
    check("A5 equity=None(非dry_run) -> 不拦 (False,None)",
          (blocked, cap), (False, None))


# ════════════════════════════════════════════════════════════════
# B 段 —— on_signal 集成：拒开 / 截断 / dry_run 虚拟资金
# ════════════════════════════════════════════════════════════════
print("\n[B] on_signal 集成")

# B1：equity < K → 拒开本笔，事件 risk_block(capital_insufficient)，无下单
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    margin = 150_000.0
    price = 4500.0
    k = margin + spec.points_to_cash(price, 1) * 0.01
    gb = EqBroker(k * 0.5)          # 连 1 手都不够
    gs = StubSizer(margin=margin, max_vol=10)
    engine.broker, engine.sizer = gb, gs
    sig = make_signal(price=price)
    engine.on_signal(sig)
    check("B1 equity<K -> signal_action=risk_block",
          store.signal_action(sig.key), "risk_block")
    check("B1 equity<K -> broker 零下单",
          sum(1 for o in gb.orders), 0)
    check("B1 equity<K -> state=IDLE（未入场）",
          engine._state.name, "IDLE")

# B2：sizer 手数 > cap → 截断为 cap，事件 capital_capped，下单手数=cap
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    margin = 150_000.0
    price = 4500.0
    k = margin + spec.points_to_cash(price, 1) * 0.01
    gb = EqBroker(k * 3, delegate=broker0)   # cap = 3
    gs = StubSizer(margin=margin, max_vol=10)
    gs._per = 9                    # sizer 算 9 手，超 cap=3
    engine.broker, engine.sizer = gb, gs
    sig = make_signal(price=price)
    engine.on_signal(sig)
    vols = [o.volume for o in gb.orders]
    check("B2 per=9>cap=3 -> 单笔下单手数=3（截断）", vols, [3])
    check("B2 截断后 signal_action=opened",
          store.signal_action(sig.key), "opened")
    check("B2 截断后 state=IN_TRADE", engine._state.name, "IN_TRADE")

# B3：dry_run 无 sim_equity → 回退 risk.initial_cash（虚拟资金）
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp, sim_equity=None)
    # sim_equity=None ⇒ broker.equity() 返回 None ⇒ 回退 initial_cash（默认 1000 万）
    gs = StubSizer(margin=150_000.0, max_vol=1000)
    engine.sizer = gs
    sig = make_signal(price=4500.0)
    blocked, cap = engine._capital_gate(sig)
    check("B3 dry_run无权益 -> 回退 initial_cash(不拒开)", blocked, False)
    check("B3 回退虚拟资金后 cap>0", cap is not None and cap > 0, True)

# B4：equity 未知且非 dry_run → 不拦，正常开仓（保守放行）
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    sig = make_signal(price=4500.0)
    gb = EqBroker(None, delegate=broker0)
    gs = StubSizer(margin=150_000.0, max_vol=10)
    engine.broker, engine.sizer = gb, gs
    engine.on_signal(sig)
    check("B4 equity=None(非dry_run) -> 正常开仓",
          store.signal_action(sig.key), "opened")


print("\n" + "=" * 60)
print("P22 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
