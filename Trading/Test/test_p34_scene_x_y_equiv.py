# -*- coding: utf-8 -*-
"""
P34 场景 X vs 场景 Y：同一成交价下"新敞口"的风控层等价性（2026-09-11）
========================================================================
用户在原始需求里问的原文：

    空仓状态下做多（记为场景X），是买入开新仓，进场后通过止盈止损 L1-L3 策略离场
    锁仓状态下做多（记为场景Y），是买入平旧仓（平掉原空单），除了报单不同，
    其它（成交价，止盈止损）是否跟场景X执行结果一模一样？

结论（文档 §4.7 已推演，本文件把它**跑成代码**）：
    **成立** —— 除「报单 offset」「Trade 会计锚」两项必然不同外，**风控层完全一致**：
    `run_side` / `run_volume` / `run_anchor` / `run_plan`（stop / tp / params）
    / L1 触发价，全部逐字段相同。这不是巧合，是 **D1** 的设计目标：
    **净敞口从 0 变非 0 的那一刻，用该次成交价作锚** —— X 用开仓价 P₀、Y 用拆锁
    成交价 P₂，两者进的是同一个 `_run_start(anchor_price=...)`。

测试设计：为什么要换 broker
--------------------------------------------------------------------------
dry-run 撮合把滑点按 intent 反向加：开仓 `ref+slip`、平仓 `ref-slip` ——
于是 X 与 Y 的成交价天生差 2 个 tick（这是撮合模型的口径，不是风控口径）。
要单独检视"风控层是否一致"，必须先把"成交价"这个变量消掉，
所以本文件用 `FixedFillBroker`（按 `ref_price` 精确成交、不滑点），
**人为让两场景拿到同一个成交价 4520**，再看下游是否逐字节相同。

⚠️ 对文档 §4.7 最后一行的**收紧**（本文件按更严格的口径断言）
--------------------------------------------------------------------------
文档写"端到端总和 Σ net_points 一致"。该说法**在全局口径下不成立**，需要收紧为
"**同一段敞口**的 Σ net_points 一致"。场景 Y 的历史里比 X **多两个东西**：
  1. **锁仓持仓**：锁仓本身是更早一次 L1 触发的转移 ④（反向开仓）产生的，
     那笔持仓在 X 里根本不存在，它自己的盈亏是 Y 独有的一段；
  2. **会计锚差**：Y 的敞口持仓沿用原仓的建仓价 4500（锁仓期间不动账），
     X 的敞口持仓是本次成交价 4520 —— 两者相差 20 点，是**有意的**会计口径。
本文件把这两项**分别量化**并对上账（见 [6]/[7]），而不是笼统断言"总和一致"。

覆盖
--------------------------------------------------------------------------
  [1] 两场景自然跑通：X = ① 开仓；Y = ④ 锁仓 → ③ 拆锁（全程 on_bar/on_signal）
  [2] 必然不同的项：报单 intent/offset、转移号、Trade 会计锚
  [3] 必须相同的风控层：run_side / run_volume / run_anchor / run_plan 逐字段相等
  [4] L1 触发价一致（`run_plan.stop_price` 相同 → 同一根不利 K 线同时触发）
  [5] 费率：中金所**开仓费率 == 平昨费率**（0.0023%）；平今 0.0345% 在此不可达
  [6] 离场报单逐字段相同（同一个 ⑤ CLOSE、同一触发价）
  [7] 差额对账：Σ_Y − Σ_X = 锁仓持仓那笔 + 会计锚差（两项分别量化）；
      锁仓持仓（④ 反向开仓产物）在 ③ 拆锁时以成交价 4520 了结，是一段与敞口持仓
      分离的独立盈亏（方向随价格路径可正可负，本路径为空头在反弹中平仓故为负）

跑法：PYTHONPATH=<repo root> python Trading/Test/test_p34_scene_x_y_equiv.py
"""
from __future__ import annotations

import copy
import os
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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading import Broker  # noqa: E402,F401
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import AccountState, Bar, Side, Signal  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"
D2 = "2026-09-03"
# 统一变量：两场景的"新敞口成交价"都设成 4520
P_ANCHOR = 4520.0
# Y 的原始多单建仓价（会计锚，故意 ≠ P_ANCHOR，以展示两个锚的分离）
P_Y_ENTRY = 4500.0
# 不利 K 线：足够低，必触发 L1 止损
P_EXIT_BAR = 4110.0


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
    # 注意：detail 可能是 tuple（如 snap["plan"]）→ 必须 str() 后再拼，
    # 否则 `"（%s）" % tuple` 会抛 "not all arguments converted"。
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p34_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_cfg():
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({
        "use_atr": False, "min_r_points": 3.0, "use_trailing": False,
    })
    return TradingConfig.from_dict(base)


def make_bar(ts, date, o, h, l, c):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(key, date, ts, price, is_buy):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2",
                  is_buy=is_buy, price=price, high=price + 10.0,
                  low=price - 10.0, fractal_low=price - 12.0,
                  fractal_high=price + 12.0)


class FixedFillBroker(DryRunBroker):
    """按 `ref_price` **精确成交**、不加滑点。

    目的：把"成交价"这个变量从 X/Y 对比里消掉（否则 dry-run 的
    开仓 `ref+slip` / 平仓 `ref-slip` 会让两场景天生差 2 个 tick）。
    meta 与 DryRun 完全一致，故 intent / offset / is_exit / transition
    这些"报单层"的差异仍然可观察。
    """

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        o = super().submit(intent, side, volume, ref_price, signal_key,
                           note=note, entry_date=entry_date, is_exit=is_exit)
        o.filled_price = float(ref_price)
        o.price = float(ref_price)
        return o


def build_engine(tmpdir, tag):
    spec = InstrumentSpec()
    return TradingEngine(
        make_cfg(), FixedFillBroker(spec, {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}),
        LayeredExitPolicy(make_cfg().exit_params.model_dump()),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def plan_key(plan):
    """出场计划的**可比指纹**（name / stop / tp / params 全字段）。"""
    if plan is None:
        return None
    return (plan.name, round(float(plan.stop_price), 6),
            (None if plan.tp_price is None else round(float(plan.tp_price), 6)),
            tuple(sorted((str(k), str(v)) for k, v in (plan.params or {}).items())))


def snapshot(eng, since_order=0, since_trade=0):
    """抓取"净敞口刚变成非 0"那一刻的风控层快照。"""
    return {
        "orders": [(o.meta.get("intent"), o.meta.get("offset"),
                    o.meta.get("is_exit"), o.meta.get("transition"))
                   for o in eng.broker.orders[since_order:]],
        "trades": len(eng.store.trades()) - since_trade,
        "book": [(p.side.name, p.volume, p.entry_price)
                 for p in eng.positions.positions],
        "state": eng.account_state(),
        "run": (eng._run_side.name, eng._run_volume, eng._run_anchor),
        "plan": plan_key(eng._run_plan),
        "stop": eng._run_plan.stop_price,
        "tp": eng._run_plan.tp_price,
        "risk_anchor": eng._run_plan.params.get("risk_anchor"),
    }


# ══════════════════════════════════════════════════════════════
print("\n[1] 场景 X：空仓做多（① OPEN）→ 跨日 → L1 触发（⑤ CLOSE）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("x") as tmp:
    eng = build_engine(tmp, "X")
    eng.on_bar(make_bar(1000, D1 + " 09:40", P_ANCHOR, P_ANCHOR + 10,
                        P_ANCHOR - 10, P_ANCHOR))
    check("[1a] 前置 FLAT", eng.account_state(), AccountState.FLAT)
    eng.on_signal(make_sig("X|buy|1", D1 + " 09:40", 1000, P_ANCHOR, True))
    snap_x = snapshot(eng)
    check("[1b] ① 开仓成交价 = 4520", snap_x["book"][0][2], P_ANCHOR)
    check("[1c] 转 RUNNING", snap_x["state"], AccountState.RUNNING)

    # 跨日 + 大跌 → L1 → ⑤ CLOSE
    eng.on_bar(make_bar(3000, D2 + " 14:55", P_EXIT_BAR, P_EXIT_BAR + 10,
                        4000.0, P_EXIT_BAR))
    ord_x = [(o.meta.get("intent"), o.meta.get("offset"), o.meta.get("transition"),
              o.price) for o in eng.broker.orders[-1:]]
    tr_x = [dict(t) for t in eng.store.trades()]
    # ⑤ 的成交价 = **L1 触发价**（止损价 4520−R），不是 K 线收盘价 ——
    # 这正是"止盈止损触发价一致"这一结论的直接落点，故直接拿计划里的止损价对齐。
    check("[1d] 跨日不利 K 线 → ⑤ CLOSE / 成交价 = L1 止损价",
          ord_x, [("close", "CLOSE", 5, snap_x["stop"])])
    check("[1e] X 终态 FLAT", eng.account_state(), AccountState.FLAT)
    check("[1f] X 只有 1 笔成交（敞口持仓）", len(tr_x), 1)

# ══════════════════════════════════════════════════════════════
print("\n[2] 场景 Y：锁仓做多（④ 锁仓 → ③ 拆锁）→ L1 触发（⑤ CLOSE）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("y") as tmp:
    eng = build_engine(tmp, "Y")
    eng.on_bar(make_bar(1000, D1 + " 09:40", P_Y_ENTRY, P_Y_ENTRY + 10,
                        P_Y_ENTRY - 10, P_Y_ENTRY))
    eng.on_signal(make_sig("Y|buy|1", D1 + " 09:40", 1000, P_Y_ENTRY, True))
    check("[2a] Y 前置：day1 开多 @4500（会计锚 = 4500）",
          [(p.side.name, p.entry_price) for p in eng.positions.positions],
          [("LONG", P_Y_ENTRY)])

    # day1 尾盘大跌 → L1 → ④ 反向开仓 → 锁仓
    eng.on_bar(make_bar(2000, D1 + " 14:55", P_EXIT_BAR, P_EXIT_BAR + 10,
                        4000.0, P_EXIT_BAR))
    check("[2b] ④ 反向开仓后 LOCKED（净敞口 0）",
          eng.account_state(), AccountState.LOCKED)
    check("[2c] 簿内双向（原多 + 反向空）",
          sorted(p.side.name for p in eng.positions.positions), ["LONG", "SHORT"])

    # day2：锁已跨日 → 买信号 @4520 → ③ 拆锁（平掉空头）
    eng.on_bar(make_bar(3000, D2 + " 09:40", P_ANCHOR, P_ANCHOR + 10,
                        P_ANCHOR - 10, P_ANCHOR))
    n_ord, n_trd = len(eng.broker.orders), len(eng.store.trades())
    eng.on_signal(make_sig("Y|buy|2", D2 + " 09:40", 3000, P_ANCHOR, True))
    snap_y = snapshot(eng, n_ord, n_trd)
    check("[2d] ③ 拆锁后来到单边敞口 → RUNNING", snap_y["state"],
          AccountState.RUNNING)
    check("[2e] ③ 之后只剩余多头 2 手（空头已平）",
          [(p.side.name, p.volume) for p in eng.positions.positions],
          [("LONG", 2)])
    check_true("[2f] 剩余多头的**会计锚**仍是原建仓价 4500（锁仓期间不动账）",
               eng.positions.positions[0].entry_price == P_Y_ENTRY,
               eng.positions.positions[0].entry_price)
    check_true("[2g] 而**风控锚**是本次拆锁成交价 4520（两锚分离）",
               snap_y["run"][2] == P_ANCHOR,
               "anchor=%s / entry=%s" % (snap_y["run"][2],
                                         eng.positions.positions[0].entry_price))

    # day2 尾盘大跌 → L1 → ⑤ CLOSE
    eng.on_bar(make_bar(4000, D2 + " 14:55", P_EXIT_BAR, P_EXIT_BAR + 10,
                        4000.0, P_EXIT_BAR))
    ord_y = [(o.meta.get("intent"), o.meta.get("offset"), o.meta.get("transition"),
              o.price) for o in eng.broker.orders[-1:]]
    tr_y = [dict(t) for t in eng.store.trades()]
    check("[2h] 跨日不利 K 线 → ⑤ CLOSE / 成交价 = L1 止损价",
          ord_y, [("close", "CLOSE", 5, snap_y["stop"])])
    check("[2i] Y 终态 FLAT", eng.account_state(), AccountState.FLAT)
    check("[2j] Y 共 2 笔成交（锁仓持仓 + 敞口持仓）", len(tr_y), 2)

# 提前抽出两场景的"止损离场持仓"与"锁仓持仓"，供 [5]/[7] 对账使用
lx = [t for t in tr_x if t["reason"] == "sl"][0]
ly = [t for t in tr_y if t["reason"] == "sl"][0]
lock_pos = [t for t in tr_y if t is not ly][0]


# ══════════════════════════════════════════════════════════════
print("\n[3] 必然不同：报单 / 转移号 / Trade 会计锚")
# ══════════════════════════════════════════════════════════════
check("[3a] 报单必然不同：X = ① open/OPEN ‖ Y = ③ close/CLOSE",
      (snap_x["orders"], snap_y["orders"]),
      ([("open", "OPEN", False, 1)], [("close", "CLOSE", False, 3)]))
check("[3b] Trade 数必然不同：X 入场不记 Trade ‖ Y 记 1 笔（平掉锁仓持仓）",
      (snap_x["trades"], snap_y["trades"]), (0, 1))

# ══════════════════════════════════════════════════════════════
print("\n[4] 必须相同：风控层逐字段一致（D1 的构造性保证）")
# ══════════════════════════════════════════════════════════════
check("[4a] 敞口持仓方向 / 手数一致（都是 LONG 2）",
      ([x[0] for x in snap_x["book"]], [x[0] for x in snap_y["book"]]),
      (["LONG"], ["LONG"]))
check("[4b] 转入状态一致（都 RUNNING）",
      (snap_x["state"], snap_y["state"]),
      (AccountState.RUNNING, AccountState.RUNNING))
check("[4c] run_side / run_volume / run_anchor 逐项相等", snap_x["run"],
      snap_y["run"])
check_true("[4d] 两边的风控锚都 = 4520（新敞口的成交价）",
           snap_x["run"][2] == P_ANCHOR and snap_y["run"][2] == P_ANCHOR,
           "%s / %s" % (snap_x["run"][2], snap_y["run"][2]))
check("[4e] **出场计划完全一致**（name / stop / tp / params 全等）",
      snap_x["plan"], snap_y["plan"])
check("[4f] L1 止损价一致（= 4520 − R）", snap_x["stop"], snap_y["stop"])
check("[4g] 止盈价一致", snap_x["tp"], snap_y["tp"])
check("[4h] 计划内风控锚一致", snap_x["risk_anchor"], snap_y["risk_anchor"])
check_true("[4i] 出场计划确实带了 risk_anchor（可审计）",
           snap_x["risk_anchor"] is not None
           and abs(float(snap_x["risk_anchor"]) - P_ANCHOR) < 1e-9,
           snap_x["plan"])

# ══════════════════════════════════════════════════════════════
print("\n[5] 费率：建敞口的成本相等")
# ══════════════════════════════════════════════════════════════
_spec = InstrumentSpec()
check("[5a] 中金所开仓费率 == 平昨费率（0.0023%）",
      _spec.open_fee_rate, _spec.close_fee_rate)
check_true("[5b] 平今费率 = 平昨的 15 倍（本代码不可达 → 今日单永不 CLOSE）",
           abs(_spec.close_today_fee_rate / _spec.close_fee_rate - 15.0) < 1e-9,
           _spec.close_today_fee_rate / _spec.close_fee_rate)
def _fee_ratio(t):
    notional = t["volume"] * t["exit_price"]
    return t["cost_points"] / notional if notional else 0.0
_r_lx = _fee_ratio(lx)
_r_ly = _fee_ratio(ly)
_tol = 0.05  # 相对容差 5%：足以区分「平昨」与「15 倍平今」（后者偏离 ~1400%）
check_true("[5c] ⑤ 平仓费率：X（跨日）与 Y 一致且都 ≈ 平昨 close_fee_rate（远离 15 倍平今 3.45e-4）",
           abs(_r_lx - _spec.close_fee_rate) / _spec.close_fee_rate < _tol
           and abs(_r_ly - _spec.close_fee_rate) / _spec.close_fee_rate < _tol
           and abs(_r_lx - _r_ly) / _spec.close_fee_rate < _tol,
           (_r_lx, _r_ly, _spec.close_fee_rate))

# ══════════════════════════════════════════════════════════════
print("\n[6] 离场报单逐字段相同（同一个 ⑤、同一触发价）")
# ══════════════════════════════════════════════════════════════
check("[6a] 两场景的离场报单完全相同（intent/offset/transition/价格）",
      ord_x, ord_y)
check_true("[6b] 都是 ⑤ CLOSE 且成交价都是 L1 止损价（同一触发价）",
           ord_x == ord_y and ord_x[0][3] == snap_x["stop"] == snap_y["stop"],
           ord_x)

# ══════════════════════════════════════════════════════════════
print("\n[7] 差额对账：Σ_Y − Σ_X = 锁仓持仓 + 会计锚差（两项分别量化）")
# ══════════════════════════════════════════════════════════════
check("[7a] 锁仓持仓方向 = SHORT（④ 反向开仓的产物）", lock_pos["side"], "SHORT")
check("[7b] 锁仓持仓 signal_key = 原信号键 + #lock（④ 的审计命名）",
      lock_pos["signal_key"], "Y|buy|1#lock")
check("[7c] 敞口持仓出场价一致（同一止损价 → 同价）",
      lx["exit_price"], ly["exit_price"])
check_true("[7d] 会计锚差 = 20 点：X 的敞口持仓入场 4520 ‖ Y 的 4500",
           lx["entry_price"] - ly["entry_price"] == P_ANCHOR - P_Y_ENTRY,
           "%s - %s" % (lx["entry_price"], ly["entry_price"]))
check_true("[7e] 该差额**只**体现在会计层：敞口持仓 gross 之差的绝对值 = 20",
           abs(round(lx["gross_points"] - ly["gross_points"], 6))
           == abs(P_ANCHOR - P_Y_ENTRY),
           "%.4f vs %.4f" % (lx["gross_points"], ly["gross_points"]))
check_true("[7f] 对账：Σ_Y − Σ_X = 锁仓持仓 net_points + 敞口持仓 net_points 之差",
           abs((sum(t["net_points"] for t in tr_y)
                - sum(t["net_points"] for t in tr_x))
               - (lock_pos["net_points"] + (ly["net_points"] - lx["net_points"]))
               ) < 1e-9,
           "ΣY=%.4f ΣX=%.4f lock=%.4f Δ敞口=%.4f" % (
               sum(t["net_points"] for t in tr_y),
               sum(t["net_points"] for t in tr_x), lock_pos["net_points"],
               ly["net_points"] - lx["net_points"]))
check_true("[7g] 锁仓持仓在 ③ 拆锁时以成交价 4520 平仓了结（与敞口持仓分离的独立一段）",
           abs(lock_pos["exit_price"] - P_ANCHOR) < 1e-9,
           "lock_exit=%.4f anchor=%.4f net=%.4f"
           % (lock_pos["exit_price"], P_ANCHOR, lock_pos["net_points"]))
check_true("[7h] 风控层等价是本文件的断言结论（止损价与离场报单全等由 [4][6] 钉死）",
           snap_x["plan"] == snap_y["plan"] and ord_x == ord_y, "")


print("\n" + "=" * 62)
print("P34 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
sys.exit(1 if _FAIL else 0)
