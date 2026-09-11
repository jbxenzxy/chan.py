# -*- coding: utf-8 -*-
"""
P33 转移表 ①~⑤ 逐条契约 + "第 6 种情况"的构建期闸门（2026-09-11，Phase 7）
==========================================================================
本文件钉死的是**状态机本体**：五条转移各自的进入条件与产出动作。
判据本身（三态怎么来）在 p32；各调用点行为在 p27；离场口径在 p30。

转移表（全表，无第 6 条）
--------------------------------------------------------------------------
    触发源        前置状态            动作 / 意图           转移号
    ─────────────────────────────────────────────────────────────────────
    交易信号      FLAT                OPEN  (信号方向)       ①
    交易信号      LOCKED 且 D_last≥今天 OPEN  (信号方向)       ②
    交易信号      LOCKED 且 D_last<今天 CLOSE (反向, 目标=FIFO最早反向仓) ③
    离场(bar)     净敞口≠0 且 D_last≥今天 OPEN (净敞口反向)    ④
    离场(bar)     净敞口≠0 且 D_last<今天 CLOSE(净敞口方向, 目标=FIFO最早同向仓) ⑤
    ─────────────────────────────────────────────────────────────────────
    交易信号      RUNNING             —（忽略，规则 ⑶）
    离场(bar)     净敞口=0           —（无动作）

⚠️ 关于"第 6 种情况"（对文档 §7.3 的修正）
--------------------------------------------------------------------------
文档 §7.3 的 p33 条目写的是"断言第 6 种情况**会写 `state_machine_violation`**"。
**该写法与代码不符，本文件按代码改写，并在此说明理由**：

  `state_machine_violation` 这个事件在代码里**根本不存在**（全仓检索无写入方，
  也无消费方）。它只出现在文档 §4.4 / §7.3 的叙述里。原因不是"漏实现"，而是
  **这条路径被构造性消除了**：

    · `_decide_action` / `_decide_exit` 是**纯函数**，值域由返回语句穷举决定；
    · 非穷举的分支一律 `return None`（RUNNING 收信号 → None；净敞口 0 → None；
      锁仓态找不到对冲目标 → None + `signal_no_close_target` 事件）。
    · "先算动作、后执行"两段之间没有任何可以"违反状态机"的窗口。

  所以正确的护栏不是**运行时事件**，而是**构建期闸门**：用 AST 断言这两个
  函数里出现的 `transition=` 常量集合恰好是 {1,2,3,4,5}，且不存在第 6 个。
  这样"新增第 6 条转移却不更新本契约"会**在测试期就红**，比运行时事件更早、
  更硬，也不需要在热路径上塞防御代码。

覆盖
--------------------------------------------------------------------------
  [1] 构建期闸门：转移表恰好 5 条（AST 穷举 `_Action(... transition=N)`）
  [2] 转移 ①：FLAT + 信号 → OPEN（信号方向）/ is_exit=False / target=None
  [3] 转移 ②：LOCKED(当日锁) + 信号 → OPEN（信号方向），不动锁仓仓单
  [4] 转移 ③：LOCKED(跨日锁) + 信号 → CLOSE（反向）/ 目标 = FIFO 最早反向仓
  [5] 转移 ③ 的兜底：簿非空但找不对冲目标 → None + `signal_no_close_target`
  [6] 转移 ④：今仓离场 → 反向 OPEN / is_exit=True / transition=4
  [7] 转移 ⑤：跨日仓离场 → CLOSE / 目标 = FIFO 最早同向仓 / transition=5
  [8] 两个"无动作"分支：RUNNING 收信号 → None；净敞口 0 离场 → None
  [9] 值域穷举：两个决策函数的返回值**只能是** None 或上述 5 条转移之一

跑法：PYTHONPATH=<repo root> python Trading/Test/test_p33_state_machine_table.py
"""
from __future__ import annotations

import ast
import copy
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
_REPO_ROOT = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO_ROOT)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, ExitPlan, OrderIntent, Position, Side, Signal,
)
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_MAYBE_FAIL = 0
TODAY = "2026-09-02"
YESTERDAY = "2026-09-01"


def check(name, got, expected):
    global _PASS, _MAYBE_FAIL
    ok = got == expected
    print(("  ✓ " if ok else "  ✗ ") + name
          + ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _MAYBE_FAIL += 1


def check_true(name, cond, detail=""):
    # detail 可能是 tuple/list（如 kinds 列表）→ 必须 str() 后再拼。
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag="a"):
    d = tempfile.mkdtemp(prefix="tg_p33_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0})
    return TradingConfig.from_dict(base)


def make_bar(ts, o=4500.0, h=4510.0, l=4490.0, c=4505.0, date=TODAY + " 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(side=Side.LONG, vol=2, entry_price=4500.0, key="P33",
             entry_bar_seq=1, entry_date=TODAY):
    return Position(
        symbol="CFFEX.IF2609", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=key, open_order_id="p33-o1",
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
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def kinds(eng):
    eng.ev.flush()
    out = []
    try:
        for ln in open(eng.ev.path, encoding="utf-8", errors="replace"):
            m = re.search(r'"kind"\s*:\s*"([^"]+)"', ln)
            if m:
                out.append(m.group(1))
    except OSError:
        pass
    return out


def seed_lock(eng, today=True, vol=2):
    """塞一个真锁仓态（多空各一笔，净敞口 0）。"""
    ed = TODAY if today else YESTERDAY
    eng.positions.add(make_pos(Side.LONG, vol, 4500.0, "P33-L", 1, ed))
    eng.positions.add(make_pos(Side.SHORT, vol, 4510.0, "P33-S", 2, ed))
    return eng


# ══════════════════════════════════════════════════════════════
print("\n[1] 构建期闸门：转移表恰好 5 条（AST 穷举 transition 常量）")
# ══════════════════════════════════════════════════════════════
_SRC = open(os.path.join(_TG_ROOT, "Engine", "Engine.py"),
            encoding="utf-8").read()
_TREE = ast.parse(_SRC)
_FNS = {n.name: n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}


def _transitions(fn):
    """该函数体里出现的 transition= 关键字常量值（AST，注释/字符串不算）。"""
    out = set()
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_Action":
            for k in c.keywords:
                if k.arg == "transition" and isinstance(k.value, ast.Constant):
                    out.add(k.value.value)
    return out


_t_decide = _transitions(_FNS["_decide_action"])
_t_exit = _transitions(_FNS["_decide_exit"])
check("[1a] `_decide_action` 只产出转移 ①②③", sorted(_t_decide), [1, 2, 3])
check("[1b] `_decide_exit` 只产出转移 ④⑤", sorted(_t_exit), [4, 5])
check("[1c] 全表恰好 5 条，**没有第 6 条**",
      sorted(_t_decide | _t_exit), [1, 2, 3, 4, 5])

# "第 6 种情况"的护栏 = 构建期闸门（见文件头说明），不是运行时事件
check_true("[1d] 代码里**不存在** state_machine_violation 事件（文档 §7.3 需修正）",
           "state_machine_violation" not in _SRC, "源码中无该字符串")
_prod_hits = []
for _dp, _ds, _fs in os.walk(_TG_ROOT):
    if "__pycache__" in _dp or os.sep + "Test" in _dp:
        continue
    for _f in _fs:
        if not _f.endswith(".py"):
            continue
        _t = open(os.path.join(_dp, _f), encoding="utf-8",
                  errors="replace").read()
        if "state_machine_violation" in _t:
            _prod_hits.append(_f)
check("[1e] 生产代码全域亦无该事件（含 App/）", _prod_hits, [])

# 两个决策函数必须是纯函数：只读状态、只返回 _Action / None
check_true("[1f] `_decide_action` 源码里无 `self._persist` / `_execute` 调用（纯函数）",
           all(k not in ast.dump(_FNS["_decide_action"])
               for k in ("_persist", "_execute", "_submit")))
check_true("[1g] `_decide_exit` 源码里无 `self._persist` / `_execute` 调用（纯函数）",
           all(k not in ast.dump(_FNS["_decide_exit"])
               for k in ("_persist", "_execute", "_submit")))


# ══════════════════════════════════════════════════════════════
print("\n[2] 转移 ①：FLAT + 信号 → OPEN（信号方向）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t1") as tmp:
    eng = build_engine(tmp, "t1")
    eng.on_bar(make_bar(1000))
    check("[2a] 前置 FLAT", eng.account_state(), AccountState.FLAT)

    act = eng._decide_action(make_sig("P33-2-B", is_buy=True), TODAY)
    check("[2b] 买信号 → ① / OPEN / LONG",
          (act.transition, act.intent, act.side),
          (1, OrderIntent.OPEN, Side.LONG))
    check("[2c] 无对冲目标（新建仓）", act.target, None)
    check("[2d] is_exit=False（入场不追价，D13）", act.is_exit, False)
    check("[2e] 手数 = lots_per_signal", act.volume, eng.lots_per_signal)

    act_s = eng._decide_action(make_sig("P33-2-S", is_buy=False), TODAY)
    check("[2f] 卖信号 → ① / OPEN / SHORT",
          (act_s.transition, act_s.intent, act_s.side),
          (1, OrderIntent.OPEN, Side.SHORT))
    check("[2g] 决策本身不改状态（纯函数）：簿仍空",
          len(eng.positions.positions), 0)


# ══════════════════════════════════════════════════════════════
print("\n[3] 转移 ②：LOCKED(当日锁) + 信号 → OPEN（同向，不动锁仓仓单）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t2") as tmp:
    eng = build_engine(tmp, "t2")
    eng.on_bar(make_bar(1000))
    seed_lock(eng, today=True)
    check("[3a] 前置 LOCKED（净敞口 0 且簿非空）",
          eng.account_state(), AccountState.LOCKED)

    act = eng._decide_action(make_sig("P33-3-B", is_buy=True), TODAY)
    check("[3b] 当日锁 + 买信号 → ② / OPEN / LONG",
          (act.transition, act.intent, act.side),
          (2, OrderIntent.OPEN, Side.LONG))
    check("[3c] 手数 = lots_per_signal（与 ① 同口径）",
          act.volume, eng.lots_per_signal)
    check("[3d] 无对冲目标（② 是加仓不是对冲）", act.target, None)
    check("[3e] is_exit=False", act.is_exit, False)
    check("[3f] 纯函数：簿仍 2 笔（决策不动锁仓仓单）",
          len(eng.positions.positions), 2)


# ══════════════════════════════════════════════════════════════
print("\n[4] 转移 ③：LOCKED(跨日锁) + 信号 → CLOSE（反向，FIFO 最早反向仓）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t3") as tmp:
    eng = build_engine(tmp, "t3")
    eng.on_bar(make_bar(1000))
    seed_lock(eng, today=False)
    check("[4a] 前置 LOCKED（跨日）", eng.account_state(), AccountState.LOCKED)

    act = eng._decide_action(make_sig("P33-4-B", is_buy=True), TODAY)
    check("[4b] 跨日锁 + 买信号 → ③ / CLOSE / SHORT（买平 = 平空）",
          (act.transition, act.intent, act.side),
          (3, OrderIntent.CLOSE, Side.SHORT))
    check("[4c] 目标 = 簿内那笔空头（FIFO 最早反向仓）",
          act.target is eng.positions.oldest_opposite(Side.LONG), True)
    check("[4d] 目标方向 = SHORT", act.target.side.name, "SHORT")
    check("[4e] 量 = min(lots_per_signal, 目标手数)",
          act.volume, min(eng.lots_per_signal, act.target.volume))
    check("[4f] is_exit=False（拆锁不追价，D13 的另一半）", act.is_exit, False)

    act_s = eng._decide_action(make_sig("P33-4-S", is_buy=False), TODAY)
    check("[4g] 跨日锁 + 卖信号 → ③ / CLOSE / LONG（卖平 = 平多）",
          (act_s.transition, act_s.intent, act_s.side),
          (3, OrderIntent.CLOSE, Side.LONG))
    check("[4h] 目标方向 = LONG", act_s.target.side.name, "LONG")


# ══════════════════════════════════════════════════════════════
print("\n[5] 转移 ③ 的兜底：簿非空但找不对冲目标 → None + 事件")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t3b") as tmp:
    eng = build_engine(tmp, "t3b")
    eng.on_bar(make_bar(1000))
    # 人为构造"净敞口 0 但只有一个方向"的非法簿（正常路径不可达）
    eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "P33-5-L", 1, YESTERDAY))
    eng.positions.add(make_pos(Side.LONG, 2, 4510.0, "P33-5-L2", 2, YESTERDAY))
    check("[5a] 前置：净敞口 0（多 +4 / 空 0 → 但两笔同向）",
          eng.positions.net_volume(), 4)
    # 用真锁仓 + 清掉空头，制造"锁仓态但无反向仓"
    eng.positions.remove(eng.positions.positions[-1])
    eng.positions.add(make_pos(Side.SHORT, 4, 4500.0, "P33-5-S", 3, YESTERDAY))
    eng.positions.remove(eng.positions.positions[-1])
    eng.positions.add(make_pos(Side.LONG, 2, 4520.0, "P33-5-L3", 4, YESTERDAY))
    # 现在：LONG 2 + LONG 2 - 之前留下的? → 直接重建一个确定性簿
    eng.positions.clear()
    eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "P33-5-A", 1, YESTERDAY))
    eng.positions.add(make_pos(Side.SHORT, 2, 4510.0, "P33-5-B", 2, YESTERDAY))
    check("[5b] 重建后 LOCKED", eng.account_state(), AccountState.LOCKED)

    # 掉包 oldest_opposite 强制返回 None，走兜底分支
    _orig = eng.positions.oldest_opposite
    eng.positions.oldest_opposite = lambda side: None
    try:
        act = eng._decide_action(make_sig("P33-5-B", is_buy=True), TODAY)
    finally:
        eng.positions.oldest_opposite = _orig
    check("[5c] 找不对冲目标 → 返回 None（不猜、不开新仓）", act, None)
    check_true("[5d] 写 `signal_no_close_target` 事件（不静默）",
               "signal_no_close_target" in kinds(eng), kinds(eng))
    check("[5e] 兜底分支不改状态（簿仍 2 笔）",
          len(eng.positions.positions), 2)


# ══════════════════════════════════════════════════════════════
print("\n[6] 转移 ④：今仓离场 → 反向 OPEN（is_exit=True）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t4") as tmp:
    eng = build_engine(tmp, "t4")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, 3, 4500.0, "P33-6", 1, TODAY))
    check("[6a] 前置 RUNNING（单边敞口）", eng.account_state(),
          AccountState.RUNNING)

    act = eng._decide_exit(eng.last_bar)
    check("[6b] 今仓 → ④ / OPEN / SHORT（净敞口反向）",
          (act.transition, act.intent, act.side),
          (4, OrderIntent.OPEN, Side.SHORT))
    check("[6c] 量 = |净敞口| = 3", act.volume, 3)
    check("[6d] 无 target（④ 不指定被平仓单）", act.target, None)
    check("[6e] is_exit=True（离场必须追价，D13）", act.is_exit, True)

    # 空头同理
    eng.positions.clear()
    eng.positions.add(make_pos(Side.SHORT, 3, 4500.0, "P33-6b", 1, TODAY))
    act_s = eng._decide_exit(eng.last_bar)
    check("[6f] 今空仓 → ④ / OPEN / LONG",
          (act_s.transition, act_s.intent, act_s.side),
          (4, OrderIntent.OPEN, Side.LONG))


# ══════════════════════════════════════════════════════════════
print("\n[7] 转移 ⑤：跨日仓离场 → CLOSE（FIFO 最早同向仓）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t5") as tmp:
    eng = build_engine(tmp, "t5")
    eng.on_bar(make_bar(1000))
    p_old = make_pos(Side.LONG, 2, 4500.0, "P33-7-old", 1, YESTERDAY)
    p_new = make_pos(Side.LONG, 3, 4520.0, "P33-7-new", 2, YESTERDAY)
    eng.positions.add(p_old)
    eng.positions.add(p_new)
    check("[7a] 前置：净敞口 5 / 都是跨日仓", eng.positions.net_volume(), 5)

    act = eng._decide_exit(eng.last_bar)
    check("[7b] 跨日仓 → ⑤ / CLOSE / LONG（净敞口方向）",
          (act.transition, act.intent, act.side),
          (5, OrderIntent.CLOSE, Side.LONG))
    check("[7c] 目标 = FIFO **最早**那笔同向仓（不是最近的）",
          act.target is p_old, True)
    check("[7d] 量 = min(|净敞口|, 目标手数)",
          act.volume, min(5, p_old.volume))
    check("[7e] is_exit=True", act.is_exit, True)

    # 净敞口方向由净手数决定：多 5 - 空 2 = 多 3 → ⑤ 仍平多，
    # 但量受**目标那一笔的手数**封顶（FIFO 最早那笔只有 2 手）
    eng.positions.add(make_pos(Side.SHORT, 2, 4510.0, "P33-7-s", 3, YESTERDAY))
    act2 = eng._decide_exit(eng.last_bar)
    check("[7f] 净敞口仍为多（5-2=3）→ ⑤ 平多，量被目标手数(2)封顶",
          (act2.side.name, act2.volume), ("LONG", 2))
    check("[7f2] 净敞口方向 ≠ 单笔手数时以 net 定方向、以 target 定量",
          (act2.target is p_old, eng.positions.net_volume()), (True, 3))


# ══════════════════════════════════════════════════════════════
print("\n[8] 两个「无动作」分支（这不是第 6 条转移，是穷举的剩余项）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t6") as tmp:
    eng = build_engine(tmp, "t6")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "P33-8", 1, TODAY))
    check("[8a] 前置 RUNNING", eng.account_state(), AccountState.RUNNING)
    check("[8b] RUNNING 收买信号 → None（规则 ⑶：运行态忽略信号）",
          eng._decide_action(make_sig("P33-8-B", is_buy=True), TODAY), None)
    check("[8c] RUNNING 收卖信号 → None（反向信号也不拆）",
          eng._decide_action(make_sig("P33-8-S", is_buy=False), TODAY), None)
    check("[8d] 运行态净敞口≠0 → 离场有动作（不是 No-op）",
          eng._decide_exit(eng.last_bar) is not None, True)

    eng.positions.clear()
    check("[8e] 空仓离场 → None（净敞口 0，无仓可离）",
          eng._decide_exit(eng.last_bar), None)
    seed_lock(eng, today=True)
    check("[8f] 锁仓态离场 → None（净敞口 0，⑤ 的进入条件不成立）",
          eng._decide_exit(eng.last_bar), None)


# ══════════════════════════════════════════════════════════════
print("\n[9] 值域穷举：决策函数的返回**只能是** None 或 ①②③④⑤")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t7") as tmp:
    seen = set()
    # 把 5 种簿面 × 2 种信号方向 × 2 个触发器全跑一遍，收集 transition 取值。
    # 5 种簿面必须全覆盖，否则漏掉 ①（需要空簿）或 ⑤（需要跨日单边敞口）。
    for _mode in ("flat", "lock_today", "lock_yday", "run_today", "run_yday"):
        for _is_buy in (True, False):
            eng = build_engine(tmp, "t7_%s_%s" % (_mode, _is_buy))
            eng.on_bar(make_bar(1000))
            if _mode == "lock_today":
                seed_lock(eng, today=True)
            elif _mode == "lock_yday":
                seed_lock(eng, today=False)
            elif _mode == "run_today":
                eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "K", 1, TODAY))
            elif _mode == "run_yday":
                eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "K", 1, YESTERDAY))
            a1 = eng._decide_action(make_sig("S", is_buy=_is_buy), TODAY)
            a2 = eng._decide_exit(eng.last_bar)
            for a in (a1, a2):
                seen.add(None if a is None else a.transition)
            eng.store.close()
    check("[9a] 穷举得到的 transition 集合 = {None,1,2,3,4,5}（五条全可达）",
          sorted(seen, key=lambda x: (x is not None, x)),
          [None, 1, 2, 3, 4, 5])
    check("[9b] 全集里确实出现了 None（说明存在无动作分支，非强凑）",
          None in seen, True)

    # 每条转移的 (intent, is_exit) 组合也必须是**固定的**，不能混
    eng = build_engine(tmp, "t7_cmb")
    eng.on_bar(make_bar(1000))
    combos = {}
    seed = [("flat", None), ("lock_today", True),
            ("lock_yday", False), ("run_today", "run_t"), ("run_yday", "run_y")]
    for name, mode in seed:
        eng.positions.clear()
        if mode is True:
            seed_lock(eng, today=True)
        elif mode is False:
            seed_lock(eng, today=False)
        elif mode == "run_t":
            eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "R1", 1, TODAY))
        elif mode == "run_y":
            eng.positions.add(make_pos(Side.LONG, 2, 4500.0, "R2", 1, YESTERDAY))
        for trig in ("sig", "exit"):
            a = (eng._decide_action(make_sig("X", is_buy=True), TODAY)
                 if trig == "sig" else eng._decide_exit(eng.last_bar))
            if a is not None:
                combos[a.transition] = (a.intent.value, a.is_exit)
    check("[9c] 转移→(intent,is_exit) 是常量映射（① ② 入场 / ③ 拆锁 / ④ ⑤ 离场）",
          combos,
          {1: ("open", False), 2: ("open", False), 3: ("close", False),
           4: ("open", True), 5: ("close", True)})
    eng.store.close()


print("\n" + "=" * 62)
print("P33 结果: {} 通过 / {} 失败".format(_PASS, _MAYBE_FAIL))
print("=" * 62)
sys.exit(1 if _MAYBE_FAIL else 0)
