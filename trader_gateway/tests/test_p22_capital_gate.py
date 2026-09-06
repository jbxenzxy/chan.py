# -*- coding: utf-8 -*-
"""
P22 资金闸门（capital gate）单元测试 · 审计 P2-8 / P2-9
======================================================
背景（2026-09-06）
    开仓前用"账户可用资金"判门槛：
      K = 一手保证金 + 一手名义价值 × risk_unit_pct      （开 1 手的最低门槛）
      X = floor(可用资金 / K)                            （资金允许的最多手数上限）
      cap_per_batch = floor(X / batch_count)；可为 0。

    审计 P2-8 发现：旧实现 `cap = max(1, X//bc)` 在
      equity=3K（X=3）+ batch_count=5 时 → cap=1 → 5 批各开 1 手 = 共 5 手 > 3 手，
    批次总手数超资金上限。本测试锁死修复后的 floor 语义：
      · cap 可为 0（X < batch_count）→ 调用方拒开本批，绝不超资金上限
      · 每笔超 cap → 截断（capital_capped）
    P2-9：资金闸门是实盘资金安全功能，须补上专门测试。

本测试不连 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
    A 段 —— 直连 `engine._capital_gate`，验证边界语义（含 P2-8 场景）。
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
    d = tempfile.mkdtemp(prefix="tg_p22_")
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
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402


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
    from tg.types import Signal
    return Signal(key=sig_key, symbol="CFFEX.IF", freq="5m",
                  date="2026-09-06 09:35", timestamp=0, bsp_type="3",
                  is_buy=is_buy, price=price, high=price + 2.0, low=price - 2.0)


def build_engine(tmpdir, sim_equity=1_000_000.0):
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": sim_equity})
    entry = DefaultEntryPolicy({})
    exitp = DefaultExitPolicy({})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
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
    """可控每手保证金 / 批次 / 上限的 sizer stub。"""
    def __init__(self, margin=150_000.0, risk_unit=0.01, max_vol=10,
                 src="available"):
        self._margin = float(margin)
        self.risk_unit_pct = float(risk_unit)
        self.max_volume = int(max_vol)
        self.equity_source = src
        self._per = 1
        self._bc = 1

    def per_lot_margin(self, price):
        return self._margin

    def size(self, **kw):
        return self._per, "stub"

    def size_batch(self, **kw):
        return self._per, self._bc, "stub+batch={}".format(self._bc)


# ════════════════════════════════════════════════════════════════
# A 段 —— `engine._capital_gate` 边界语义（直连）
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
    blocked, cap = engine._capital_gate(sig, 1)
    check("A1 equity<K -> blocked=True", blocked, True)

    # A2：equity = 3K，batch_count=5 → cap = floor(3/5) = 0（P2-8 核心）
    gb = EqBroker(k * 3)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig, 5)
    check("A2 X=3,bc=5 -> blocked=False (拒开由调用方)",
          (blocked, cap), (False, 0))

    # A3：equity = 3K，batch_count=1 → cap = X = 3（默认路径不破坏）
    blocked, cap = engine._capital_gate(sig, 1)
    check("A3 X=3,bc=1 -> cap == 3", cap, 3)

    # A4：equity = 7K，batch_count=3 → cap = floor(7/3) = 2
    gb = EqBroker(k * 7)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig, 3)
    check("A4 X=7,bc=3 -> cap == 2", cap, 2)

    # A5：equity <= 0 → 不拦（(False,None)），由其他风控兜底
    gb = EqBroker(0)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig, 1)
    check("A5 equity<=0 -> 不拦 (False,None)", (blocked, cap), (False, None))

    # A6：equity 未知（None，非 dry_run）→ 不拦（保守放行）
    gb = EqBroker(None)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig, 1)
    check("A6 equity=None(非dry_run) -> 不拦 (False,None)",
          (blocked, cap), (False, None))

    # A7：batch_count≤0 防御 → 等价 batch=1
    gb = EqBroker(k * 5)
    engine.broker = gb
    blocked, cap = engine._capital_gate(sig, 0)
    check("A7 bc=0 -> 走 bc=1 (cap==X==5)", cap, 5)


# ════════════════════════════════════════════════════════════════
# B 段 —— on_signal 集成：拒开 / 截断 / dry_run 虚拟资金
# ════════════════════════════════════════════════════════════════
print("\n[B] on_signal 集成")

# B1：X < batch_count（cap=0）→ 拒开本批，事件 batch_unaffordable，无下单
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    margin = 150_000.0
    price = 4500.0
    k = margin + spec.points_to_cash(price, 1) * 0.01
    gb = EqBroker(k * 3)          # X=3
    gs = StubSizer(margin=margin, max_vol=10)
    gs._per, gs._bc = 1, 5        # per=1, batch=5
    engine.broker, engine.sizer = gb, gs
    sig = make_signal(price=price)
    engine.on_signal(sig)
    check("B1 cap=0 -> signal_action=risk_block",
          store.signal_action(sig.key), "risk_block")
    check("B1 cap=0 -> broker 零下单",
          sum(1 for o in gb.orders), 0)
    check("B1 cap=0 -> state=IDLE（未入场）",
          engine._state.name, "IDLE")

# B2：per_batch > cap → 截断为 cap，事件 capital_capped，下单手数=cap
with tmp_dir() as tmp:
    engine, store, broker0 = build_engine(tmp)
    spec = engine.spec
    margin = 150_000.0
    price = 4500.0
    k = margin + spec.points_to_cash(price, 1) * 0.01
    gb = EqBroker(k * 3, delegate=broker0)   # X=3 → cap=3（bc=1）
    gs = StubSizer(margin=margin, max_vol=10)
    gs._per, gs._bc = 9, 1        # per=9 超 cap=3
    engine.broker, engine.sizer = gb, gs
    sig = make_signal(price=price)
    engine.on_signal(sig)
    vols = [o.volume for o in gb.orders]
    check("B2 per=9>cap=3 -> 下单手数=3（截断）", vols, [3])
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
    blocked, cap = engine._capital_gate(sig, 1)
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