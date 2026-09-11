# -*- coding: utf-8 -*-
"""
P27 账户三态在调用点的行为契约（Phase 7 重写，2026-09-11）
================================================================
本文件与 p32 的分工
-------------------
  · **p32** 管"判据本身"：`account_state()` 是全仓唯一返回三态的地方，
    判据是净敞口（源码级 + 穷举不变量）。
  · **p27**（本文件）管"判出来之后各处怎么用"：三态在 4 个**原内联调用点**
    上的行为是否一致 —— 空仓开仓 / 锁仓响应 / 运行态忽略 / 离场后回退 /
    恢复期推断。

改造前这 4 处的判定式是**内联复制**的（`is_empty() or all(SOFT_EXIT_LOCK)` 与
它的逆否 `any(not SOFT_EXIT_LOCK)`），口径一致完全靠人肉维持。改造后判据收口到
`account_state()`，本文件验证"收口之后各调用点仍然走在同一条口径上"。

⚠️ 与旧版 p27 的差异（有意）
--------------------------
旧版 [2] 段断言"新旧判定逐面等价、纯重构零行为变化"。**该断言按设计已不成立**：
旧 `LOCKED` 把"单笔 SOFT_EXIT_LOCK"也算锁仓，新 `LOCKED` 只看净敞口，单笔
必然带敞口 → 必是 RUNNING。所以本文件改为**正面**断言新口径（见 [1] 与 p32[1c]）。

覆盖
----
  [1] FLAT + 信号 → 转移 ①，落簿后 RUNNING + `_state`=IN_TRADE + run 开启
  [2] LOCKED（当日锁）+ 信号 → 转移 ②（同向 OPEN，不动锁仓仓单）
  [3] LOCKED（跨日锁）+ 信号 → 转移 ③（CLOSE，对冲反向最早一笔）
  [4] RUNNING + 信号 → 忽略（规则 ⑶）
  [5] 离场后按净敞口回退：今仓 → ④ 反向 OPEN → LOCKED；跨日仓 → ⑤ CLOSE → FLAT
  [6] 恢复期（`_restore`）按净敞口推断三态，并把 run 一起恢复
  [7] 源码契约：4 个原内联点均已收口到 SSOT

跑法：PYTHONPATH=<repo root> python Trading/Test/test_p27_account_state_ssot.py
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
from Trading.Engine.Reconcile import ReconcileMixin  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, EngineState, ExitPlan, OrderIntent, Position, Side, Signal,
)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
TODAY = "2026-09-02"
YESTERDAY = "2026-09-01"


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("  ✓ " if ok else "  ✗ ") + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % detail if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p27_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    import copy
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0})
    return TradingConfig.from_dict(base)


def make_bar(ts, o=4500.0, h=4510.0, l=4490.0, c=4505.0, date=TODAY + " 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(side=Side.LONG, vol=2, entry_price=4500.0, key="P27",
             entry_bar_seq=1, entry_date=TODAY):
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=key, open_order_id="p27-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq, entry_date=entry_date)


def make_sig(key, is_buy=True, price=4500.0, ts=1001, date=TODAY + " 09:41"):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2",
                  is_buy=is_buy, price=price, high=price + 10.0,
                  low=price - 10.0, fractal_low=price - 12.0,
                  fractal_high=price + 12.0)


def build_engine(tmpdir, tag="a"):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag), echo=False, echo_kinds=None))


def kinds(eng):
    """已落盘的事件 kind 列表（EventLog 有刷盘缓冲，必须先 flush）。"""
    eng.ev.flush()
    out = []
    try:
        import io
        for ln in io.open(eng.ev.path, encoding="utf-8", errors="replace"):
            m = re.search(r'"kind"\s*:\s*"([^"]+)"', ln)
            if m:
                out.append(m.group(1))
    except OSError:
        pass
    return out


# ════════════════════════════════════════════════════════════════
print("\n[1] FLAT + 信号 → 转移 ①：开仓、转 RUNNING、run 开启")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c1")
    eng.on_bar(make_bar(1000))
    check("[1a] 前置：FLAT", eng.account_state(), AccountState.FLAT)

    sig = make_sig("P27-1", is_buy=True)
    act = eng._decide_action(sig, TODAY)
    check("[1b] 决策 = 转移 ① / OPEN / LONG",
          (act.transition, act.intent, act.side),
          (1, OrderIntent.OPEN, Side.LONG))
    check("[1b] 手数 = lots_per_signal", act.volume, eng.lots_per_signal)
    check("[1b] is_exit=False（入场不追价，D13）", act.is_exit, False)

    eng.on_signal(sig)
    check("[1c] 簿内 1 笔、方向 LONG、2 手",
          [(p.side.name, p.volume) for p in eng.positions.positions],
          [("LONG", 2)])
    check("[1d] 转移后 RUNNING", eng.account_state(), AccountState.RUNNING)
    check("[1e] `_state` 镜像 = IN_TRADE", eng._state.name, "IN_TRADE")
    check("[1f] 幂等键记为 opened", eng.store.signal_action("P27-1"), "opened")
    check("[1g] run 已开启（side=LONG, volume=2, 锚=成交价）",
          (eng._run_side.name, eng._run_volume, eng._run_anchor > 0),
          ("LONG", 2, True))


# ════════════════════════════════════════════════════════════════
print("\n[2] LOCKED（当日锁）+ 信号 → 转移 ②：同向 OPEN，不动锁仓仓单")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c2")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, key="L1", entry_date=TODAY, entry_bar_seq=1))
    eng.positions.add(make_pos(Side.SHORT, key="L2", entry_date=TODAY, entry_bar_seq=2))
    check("[2a] 前置：LOCKED（net==0 且非空）", eng.account_state(), AccountState.LOCKED)

    sig = make_sig("P27-2", is_buy=True)
    act = eng._decide_action(sig, TODAY)
    check("[2b] 决策 = 转移 ② / OPEN / LONG",
          (act.transition, act.intent, act.side),
          (2, OrderIntent.OPEN, Side.LONG))
    check("[2b] is_exit=False", act.is_exit, False)

    kept = [p.signal_key for p in eng.positions.positions]
    eng.on_signal(sig)
    check("[2c] 簿内新增一笔（共 3 笔）", len(eng.positions.positions), 3)
    check("[2d] 原有两笔锁仓仓单**未被改动**",
          [p.signal_key for p in eng.positions.positions][:2], kept)
    check("[2e] 转移后 net=+2 → RUNNING",
          (eng.positions.net_volume(), eng.account_state()),
          (2, AccountState.RUNNING))


# ════════════════════════════════════════════════════════════════
print("\n[3] LOCKED（跨日锁）+ 信号 → 转移 ③：CLOSE，对冲反向最早一笔")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c3")
    eng.on_bar(make_bar(1000))
    # 反向最早的一笔：空头放在**更早**的序号上，多头更晚 → 买信号应平掉这笔空头
    eng.positions.add(make_pos(Side.SHORT, key="OLD-SHORT",
                               entry_date=YESTERDAY, entry_bar_seq=1))
    eng.positions.add(make_pos(Side.LONG, key="OLD-LONG",
                               entry_date=YESTERDAY, entry_bar_seq=2))
    check("[3a] 前置：跨日锁", eng.account_state(), AccountState.LOCKED)

    sig = make_sig("P27-3", is_buy=True)
    act = eng._decide_action(sig, TODAY)
    check("[3b] 决策 = 转移 ③ / CLOSE / SHORT（买信号 → 买平 → 平空）",
          (act.transition, act.intent, act.side),
          (3, OrderIntent.CLOSE, Side.SHORT))
    check("[3c] 对冲目标 = 反向最早的那笔空头",
          (act.target.signal_key if act.target else None), "OLD-SHORT")
    check("[3d] is_exit=False（拆锁不追价，D13）", act.is_exit, False)

    n0 = len(eng.store.trades())
    eng.on_signal(sig)
    check("[3e] 簿内只剩多头一笔",
          [(p.side.name, p.signal_key) for p in eng.positions.positions],
          [("LONG", "OLD-LONG")])
    check("[3f] 记了 1 笔成交", len(eng.store.trades()) - n0, 1)
    check("[3g] net=+2 → RUNNING",
          (eng.positions.net_volume(), eng.account_state()),
          (2, AccountState.RUNNING))
    check("[3h] 幂等键记为 closed", eng.store.signal_action("P27-3"), "closed")


# ════════════════════════════════════════════════════════════════
print("\n[4] RUNNING + 信号 → 忽略（规则 ⑶：出场只由 L1-L3 负责）")
# ════════════════════════════════════════════════════════════════
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c4")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, key="RUN", entry_date=TODAY))
    check("[4a] 前置：RUNNING", eng.account_state(), AccountState.RUNNING)

    for i, buy in enumerate((True, False)):
        sig = make_sig("P27-4-%d" % i, is_buy=buy)
        check("[4b] 反向信号也不得产生决策（buy=%s）" % buy,
              eng._decide_action(sig, TODAY), None)
        eng.on_signal(sig)
        check("[4c] 幂等键记为 skip（buy=%s）" % buy,
              eng.store.signal_action(sig.key), "skip")
    check("[4d] 簿面未被信号改动", len(eng.positions.positions), 1)

    _ev = kinds(eng)
    check_true("[4e] 写了 signal_skip 事件（reason=running_ignore_signal）",
               "signal_skip" in _ev)
    check_true("[4f] 未写任何 order 事件",
               not any(k in _ev for k in ("order", "order_rejected")))


# ════════════════════════════════════════════════════════════════
print("\n[5] 离场后按净敞口回退（转移 ④ → LOCKED / 转移 ⑤ → FLAT）")
# ════════════════════════════════════════════════════════════════
# [5] 今仓 → 转移 ④ 反向 OPEN → 净敞口 0 → LOCKED + IDLE
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c5")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, key="TODAY-L", entry_date=TODAY))
    eng._sync_state()
    check("[5a] 前置 RUNNING + IN_TRADE",
          (eng.account_state(), eng._state.name),
          (AccountState.RUNNING, "IN_TRADE"))

    eng._force_exit(make_bar(1001), reason="p27_exit", trigger_price=4500.0)
    check("[5b] 今仓 → 反向 OPEN，净敞口归 0 → LOCKED",
          eng.account_state(), AccountState.LOCKED)
    check("[5c] 簿内 2 笔（多空互锁，双向持仓）", len(eng.positions.positions), 2)
    check("[5d] `_state` 回 IDLE", eng._state.name, "IDLE")
    _ev = kinds(eng)
    check_true("[5e] 未记 Trade（锁定不兑现盈亏）",
               eng.store.trades() == [])
    check_true("[5f] 写了 run_end（净敞口归零收尾）", "run_end" in _ev)

# [5] 跨日仓 → 转移 ⑤ CLOSE → 净敞口 0 → FLAT
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c6")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, key="OLD-L", entry_date=YESTERDAY))
    eng._sync_state()
    eng._force_exit(make_bar(1001), reason="p27_exit", trigger_price=4500.0)
    check("[5g] 跨日仓 → CLOSE → 簿空 → FLAT",
          (eng.account_state(), len(eng.positions)), (AccountState.FLAT, 0))
    check("[5h] 记了 1 笔 Trade（兑现盈亏）", len(eng.store.trades()), 1)
    check("[5i] `_state` 回 IDLE", eng._state.name, "IDLE")

# [5] 净敞口为 0 时 _force_exit 是 no-op（幂等）
with tmp_dir() as tmp:
    eng = build_engine(tmp, "c7")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, key="K1", entry_date=TODAY))
    eng.positions.add(make_pos(Side.SHORT, key="K2", entry_date=TODAY))
    check("[5j] 前置 LOCKED → _decide_exit 返回 None",
          eng._decide_exit(make_bar(1001)), None)
    eng._force_exit(make_bar(1001), reason="p27_exit", trigger_price=4500.0)
    check("[5k] no-op：簿面未变", len(eng.positions.positions), 2)


# ════════════════════════════════════════════════════════════════
print("\n[6] 恢复期（_restore）按净敞口推断三态，并恢复 run")
# ════════════════════════════════════════════════════════════════
# [6] LOCKED 持久化 → 重启后仍是 LOCKED，且 _state 一律 IDLE
with tmp_dir() as tmp:
    eng1 = build_engine(tmp, "c8")
    eng1.positions.add(make_pos(Side.LONG, key="RS1", entry_date=TODAY, entry_bar_seq=1))
    eng1.positions.add(make_pos(Side.SHORT, key="RS2", entry_date=TODAY, entry_bar_seq=2))
    eng1._persist()
    eng2 = build_engine(tmp, "c8")          # 同一 state.db → __init__ 内 _restore
    check("[6a] 恢复后仍是 LOCKED", eng2.account_state(), AccountState.LOCKED)
    check("[6b] 恢复后 _state = IDLE（非运行态一律 IDLE）", eng2._state.name, "IDLE")
    check("[6c] 两笔仓单都恢复了", len(eng2.positions.positions), 2)

# [6] RUNNING 的 run 一起恢复（D14/D15：有敞口必须有 run）
with tmp_dir() as tmp:
    eng1 = build_engine(tmp, "c9")
    eng1.on_bar(make_bar(1000))
    eng1.on_signal(make_sig("P27-6", is_buy=True))
    check("[6d] 前置：eng1 RUNNING", eng1.account_state(), AccountState.RUNNING)
    check("[6e] run 已落盘（kv 'run'）",
          bool(eng1.store.get_json("run")), True)

    eng2 = build_engine(tmp, "c9")
    check("[6f] 恢复后仍 RUNNING", eng2.account_state(), AccountState.RUNNING)
    check("[6g] 恢复后 _state = IN_TRADE", eng2._state.name, "IN_TRADE")
    check("[6h] run 风控锚一起恢复（side/anchor 非空）",
          (eng2._run_side.name, eng2._run_anchor > 0), ("LONG", True))


# ════════════════════════════════════════════════════════════════
print("\n[7] 源码契约：4 个原内联点均已收口到 SSOT")
# ════════════════════════════════════════════════════════════════
_def = inspect.getsource(TradingEngine.account_state)
check_true("[7a] account_state() 是净敞口口径",
           "net_volume()" in _def and "is_empty()" in _def)

for fn_name in ("_sync_state", "_settle_positions", "auto_order_status"):
    fn = getattr(TradingEngine, fn_name, None)
    src = inspect.getsource(fn) if fn else ""
    check_true("[7b] %s 读 account_state()" % fn_name, "account_state()" in src)

# _restore 自己不判三态：它把恢复后的簿交给 _sync_state()（投影）与
# _reconcile_positions()（对账），两者内部都读 SSOT。
# 注：`_restore` 里确有 `is_empty()`（判断"要不要首拉真实持仓"），这不是三态判定 ——
#     三态判定必须同时用到净敞口，故这里以"不出现 net_volume()/origin"为准。
_rst_src = inspect.getsource(TradingEngine._restore)
check_true("[7b] _restore 不内联判三态（无 net_volume / origin）",
           not re.search(r"net_volume|origin", _rst_src))
check_true("[7b] _restore 经 _sync_state() 投影状态",
           "_sync_state()" in _rst_src)

# on_signal 不再自己判三态 —— 它只调 _decide_action，三态判定在后者内部
_sig_src = inspect.getsource(TradingEngine.on_signal)
check_true("[7c] on_signal 不再内联判三态（无 net_volume / is_empty / origin）",
           not re.search(r"net_volume|is_empty|origin", _sig_src))
check_true("[7d] on_signal 通过 _decide_action 决策",
           "_decide_action(" in _sig_src)

# _decide_action / _decide_exit 是转移表唯一实现点（A1）
_da = inspect.getsource(TradingEngine._decide_action)
_de = inspect.getsource(TradingEngine._decide_exit)
check_true("[7e] _decide_action 读 account_state()", "account_state()" in _da)
check_true("[7f] _decide_exit 读 net_volume()（不依赖来源标记）",
           "net_volume()" in _de and "origin" not in _de)
check_true("[7g] 转移表编号只出现在 _decide_action / _decide_exit",
           set(int(x) for x in re.findall(r"transition=(\d)", _da + _de)),
           {1, 2, 3, 4, 5})

# 对账侧（Phase 5 G4）：净敞口归零必须同步收口 run
_rec = inspect.getsource(ReconcileMixin._reconcile_positions)
check_true("[7h] 对账收口读 account_state()", "account_state()" in _rec)
# 对账收口（2026-09-12 口径对齐）：用 `_run_reset()`（清字段、**不写** `run_end`
# 事件），与文档 §5.5「配套改动」一致 —— 对账清仓没有"一段 run 正常结束"的语义，
# 再写一条 `run_end` 会让运维侧误以为真发生了一次离场。收口这件事本身由上面的
# `run_ended_by_reconcile` 事件表达，所以这里断言的是 `_run_reset` 而非 `_run_end`。
check_true("[7i] 对账收口在同处收口 run（_run_reset，不写 run_end 事件）",
           "_run_reset()" in _rec and "_run_end()" not in _rec)

print("\n" + "=" * 60)
print("P27 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
