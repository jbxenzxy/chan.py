# -*- coding: utf-8 -*-
"""
P27 账户三态 SSOT 契约测试（P1 整改，2026-09-10）
================================================
背景
----
用户口径 ⑴：账户只有三种状态 ——
    · 空仓状态（簿内无仓单，盈亏恒 0）
    · 锁仓状态（多/空互锁，净敞口 0，盈亏锁定）
    · 运行状态（盈亏随行情实时变动）

改造前，该判定式在 Engine 内**内联重复 4 处**，且正写反写混用：

    _restore            : is_empty() or all(SOFT_EXIT_LOCK)     → IDLE
    on_signal           : any(not SOFT_EXIT_LOCK)               → 运行态，忽略信号
    _close_positions 尾  : is_empty() or all(SOFT_EXIT_LOCK)     → IDLE
    _unlock_position 尾  : any(not SOFT_EXIT_LOCK)               → IN_TRADE else IDLE

4 份写法互为逆否，"口径一致"完全靠人肉维持；任一处漂移即让
"运行态忽略信号" 与 "解锁后回 IN_TRADE" 互相打架。且判定值
`EngineState.IDLE` **同时覆盖空仓与锁仓**，App / 前端拿不到"空仓 vs 锁仓"。

本测试把"已收口"从注释升级为可执行契约，四层护栏：

  [1] 判定正确性：三种簿面 → AccountState.{FLAT, LOCKED, RUNNING}
      （含"今日锁"与"昨日锁"都必须是 LOCKED）
  [2] 与旧内联口径等价：新旧判定在全部簿面下逐一相等（证明是纯重构、零行为变化）
  [3] 源码契约：Engine 中不得再出现内联的账户态判定式
      （inspect.getsource 读运行时源码，只允许 account_state() 自身一处）
  [4] 对外可见 + 调用点行为：auto_order_status() 暴露三态；
      on_signal 运行态忽略 / 软离场后回 IDLE / _restore 推断 三个调用点仍正确

跑法：python Trading/Test/test_p27_account_state_ssot.py
"""
from __future__ import annotations

import inspect
import os
import re
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

from Trading import Broker  # noqa: E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, EngineState, ExitPlan, Position, PositionOrigin, Side, Signal,
)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

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


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p27_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    d = {
        "risk": {"max_volume": 2, "max_open_positions": 1, "unlock_no_new_open": True},
        "exit_params": {"use_atr": False, "min_r_points": 3.0},
    }
    import copy
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"].update(d["risk"])
    base["exit_params"].update(d["exit_params"])
    return TradingConfig.from_dict(base)


def make_bar(ts, o=4500.0, h=4510.0, l=4490.0, c=4505.0, date="2026-09-02 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(side=Side.LONG, vol=2, entry_price=4500.0, origin=PositionOrigin.SIGNAL_OPEN,
             key="P27", entry_bar_seq=1, entry_date="2026-09-02", lock_pair_id=""):
    return Position(
        # 必须等于 InstrumentSpec.trade_symbol（CFFEX.IF2609）—— _restore 按此过滤，
        # 用别的合约代码注入的持仓会在恢复时被当作"旧合约外部平仓"丢弃（[4g] 会假红）。
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=key, open_order_id="p27-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq, origin=origin, entry_date=entry_date,
        lock_pair_id=lock_pair_id)


def build_engine(tmpdir, tag="a"):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag), echo=False, echo_kinds=None))


def old_predicate(engine) -> str:
    """改造前 4 处内联判定式的等价合并（逆否归一后）。"""
    poss = engine.positions.positions
    if not poss:
        return "flat"
    if all(p.origin is PositionOrigin.SOFT_EXIT_LOCK for p in poss):
        return "locked"
    return "running"


# ════════════════════════════════════════════════════════════════
print("\n[1] 判定正确性：三种簿面 → FLAT / LOCKED / RUNNING")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c1")
    check("[1a] 空簿 → FLAT", eng.account_state(), AccountState.FLAT)

    eng.positions.add(make_pos(key="A", origin=PositionOrigin.SOFT_EXIT_LOCK,
                              entry_date="2026-09-02", lock_pair_id="lock_00001"))
    check("[1b] 单笔 SOFT_EXIT_LOCK（今日锁）→ LOCKED",
          eng.account_state(), AccountState.LOCKED)

    eng.positions.add(make_pos(side=Side.SHORT, key="A#lock",
                              origin=PositionOrigin.SOFT_EXIT_LOCK,
                              entry_date="2026-09-02", lock_pair_id="lock_00001"))
    check("[1c] 一锁对（多空互锁，净敞口 0）→ LOCKED",
          eng.account_state(), AccountState.LOCKED)

    eng.positions.add(make_pos(key="B", origin=PositionOrigin.SIGNAL_OPEN,
                              entry_date="2026-09-02"))
    check("[1d] 混入 1 笔真实敞口 → RUNNING", eng.account_state(), AccountState.RUNNING)

with tmp_dir() as tmp:
    eng = build_engine(tmp, "c2")
    eng.positions.add(make_pos(key="Y", origin=PositionOrigin.SOFT_EXIT_LOCK,
                              entry_date="2026-09-01", lock_pair_id="lock_00009"))
    check("[1e] 昨日锁（entry_date < today）也必须是 LOCKED",
          eng.account_state(), AccountState.LOCKED)

with tmp_dir() as tmp:
    eng = build_engine(tmp, "c3")
    eng.positions.add(make_pos(key="U", origin=PositionOrigin.UNLOCK_UPGRADE,
                              entry_date="2026-09-01"))
    check("[1f] 解锁升级仓（单边敞口）→ RUNNING",
          eng.account_state(), AccountState.RUNNING)


# ════════════════════════════════════════════════════════════════
print("\n[2] 与改造前内联口径等价（纯重构、零行为变化）")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c4")
    cases = [
        ("空簿", []),
        ("1 锁", [(Side.LONG, PositionOrigin.SOFT_EXIT_LOCK)]),
        ("1 锁对", [(Side.LONG, PositionOrigin.SOFT_EXIT_LOCK),
                    (Side.SHORT, PositionOrigin.SOFT_EXIT_LOCK)]),
        ("2 锁对", [(Side.LONG, PositionOrigin.SOFT_EXIT_LOCK),
                    (Side.SHORT, PositionOrigin.SOFT_EXIT_LOCK),
                    (Side.LONG, PositionOrigin.SOFT_EXIT_LOCK),
                    (Side.SHORT, PositionOrigin.SOFT_EXIT_LOCK)]),
        ("1 敞口", [(Side.LONG, PositionOrigin.SIGNAL_OPEN)]),
        ("1 敞口+1 锁对", [(Side.LONG, PositionOrigin.SIGNAL_OPEN),
                           (Side.LONG, PositionOrigin.SOFT_EXIT_LOCK),
                           (Side.SHORT, PositionOrigin.SOFT_EXIT_LOCK)]),
        ("1 升级仓", [(Side.LONG, PositionOrigin.UNLOCK_UPGRADE)]),
    ]
    all_eq = True
    for i, (label, spec) in enumerate(cases):
        eng.positions.clear()
        for k, (sd, og) in enumerate(spec):
            eng.positions.add(make_pos(side=sd, origin=og, key="E%d_%d" % (i, k)))
        new, old = eng.account_state().value, old_predicate(eng)
        if new != old:
            all_eq = False
            print("      不等: %s  new=%s old=%s" % (label, new, old))
    check("7 种簿面下 新判定 == 旧内联判定", all_eq, True)

    # 旧内联判定只区分 running / 非running；新判定把"非 running"细分成 flat / locked
    eng.positions.clear()
    check("细分关系：新 flat ⊂ 旧非running", eng.account_state(),
          AccountState.FLAT)
    eng.positions.add(make_pos(origin=PositionOrigin.SOFT_EXIT_LOCK))
    check("细分关系：新 locked ⊂ 旧非running", eng.account_state(),
          AccountState.LOCKED)


# ════════════════════════════════════════════════════════════════
print("\n[3] 源码契约：Engine 内不得再有内联账户态判定式")
# ════════════════════════════════════════════════════════════════
_src = inspect.getsource(TradingEngine)
_inline_all = re.findall(r"all\(\s*p\.origin\s+is\s+PositionOrigin\.SOFT_EXIT_LOCK", _src)
_inline_any = re.findall(r"any\(\s*p\.origin\s+is\s+not\s+PositionOrigin\.SOFT_EXIT_LOCK", _src)
check("[3a] 内联 all(SOFT_EXIT_LOCK) 判定式仅 SSOT 1 处", len(_inline_all), 1)
check("[3b] 内联 any(not SOFT_EXIT_LOCK) 判定式 0 处", len(_inline_any), 0)

_src_acct = inspect.getsource(TradingEngine.account_state)
_body = "\n".join(l for l in _src_acct.splitlines()
                  if not l.strip().startswith(("#",))).split('"""')
check("[3c] account_state 自身实现了 all(SOFT_EXIT_LOCK) 判定",
      "SOFT_EXIT_LOCK" in _src_acct, True)

_uses = re.findall(r"self\.account_state\(\)", _src)
check("[3d] account_state() 被调用 ≥ 4 处（4 个原内联点已收口）",
      len(_uses) >= 4, True)
for _site in ("_restore", "on_signal", "_close_positions", "_unlock_position"):
    _fn = getattr(TradingEngine, _site, None)
    _fs = inspect.getsource(_fn) if _fn else ""
    check("[3e] %s 已改用 account_state()" % _site, "account_state()" in _fs, True)


# ════════════════════════════════════════════════════════════════
print("\n[4] 对外可见 + 调用点行为")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c5")
    st = eng.auto_order_status()
    check("[4a] auto_order_status() 暴露 account_state=flat",
          st.get("account_state"), "flat")
    eng.positions.add(make_pos(origin=PositionOrigin.SOFT_EXIT_LOCK))
    check("[4b] 锁仓后 account_state=locked",
          eng.auto_order_status().get("account_state"), "locked")
    eng.positions.clear()
    eng.positions.add(make_pos(origin=PositionOrigin.SIGNAL_OPEN))
    check("[4c] 敞口后 account_state=running",
          eng.auto_order_status().get("account_state"), "running")

# [4d] on_signal：运行态忽略信号（调用点 2）
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c6")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(origin=PositionOrigin.SIGNAL_OPEN, key="RUN",
                              entry_bar_seq=1, entry_date="2026-09-02"))
    eng._state = EngineState.IN_TRADE
    sig = Signal(key="P27-RUN|1|S", symbol="KQ.m@CFFEX.IF", freq="5m",
                 date="2026-09-02 09:41", timestamp=1001, bsp_type="1",
                 is_buy=False, price=4500.0, high=4505.0, low=4495.0,
                 fractal_low=4490.0, fractal_high=4510.0)
    eng.on_signal(sig)
    check("[4d] 运行态 + 反向信号 → signal_action=skip/running_ignore_signal",
          eng.store.signal_action(sig.key), "skip")

# [4e] _close_positions 尾部：软离场落簿锁仓后必须回 IDLE（调用点 3）
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c7")
    eng.on_bar(make_bar(1000, date="2026-09-02 09:40"))
    p = make_pos(origin=PositionOrigin.SIGNAL_OPEN, key="LCK",
                 entry_bar_seq=1, entry_date="2026-09-02")
    eng.positions.add(p)
    eng._state = EngineState.IN_TRADE
    eng._close_positions([p], "p27_lock", 4500.0,
                         make_bar(1001, date="2026-09-02 09:41"),
                         signal_key="p27")
    check("[4e] 软离场（当日单 → LOCK）落簿后 state=IDLE",
          eng._state.name, "IDLE")
    check("[4f] 落簿后 account_state=locked", eng.account_state(), AccountState.LOCKED)

# [4g] _restore：从持久化恢复后按三态推断（调用点 1）
with tmp_dir() as tmp:
    eng1 = build_engine(tmp, "c8")
    eng1.positions.add(make_pos(origin=PositionOrigin.SOFT_EXIT_LOCK, key="RS",
                               lock_pair_id="lock_00001"))
    eng1._persist()
    eng2 = build_engine(tmp, "c8")   # 同一 store 文件 → __init__ 内 _restore 恢复
    check("[4g] 恢复后 account_state=locked", eng2.account_state(), AccountState.LOCKED)
    check("[4h] 恢复后 state=IDLE（非运行态一律 IDLE）", eng2._state.name, "IDLE")

print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (_PASS, _FAIL))
print("=" * 60)
if _FAIL:
    sys.exit(1)
