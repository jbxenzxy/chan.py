# -*- coding: utf-8 -*-
"""
P52 run 级配对会计护栏（设计文档 v3.1 §6：[S1]-[S10] + [S7b]）
================================================================
背景：Trade 的记账口径从「仓单级」（每笔平今/平昨成交 = 一笔交易）改为
「run 级」（一笔 Trade = 一次 run = 一对成交：入场 fill + 离场 fill）。
离场路径（今仓反向开 / 昨仓平 / shutdown 强平 / on_bar retry / 对账强平）
全部收敛到 `_settle_run` 唯一出口；费率各按自己成交的 offset 档
（`Instrument.single_fee`，SSOT 仍在品种档案 Fee）。

剧本与**真实转移表**对齐（Engine._decide_action / _decide_exit）：
  · 当日锁 + 信号 → 转移 ②：OPEN 新开（不是拆锁！拆锁只属于跨日锁）；
  · 跨日锁 + 信号 → 转移 ③：CLOSE 平反向最早一笔（run 以拆锁 fill 入场）；
  · 今仓离场 → 转移 ④（IF=R-OPEN / AU=CLOSETODAY）；
  · 跨日仓离场 → 转移 ⑤：CLOSE 平昨。

本文件锁死（对照设计文档 v3.1 §6）：
  [S1] 四步例子端到端：4 笔 Trade 的 gross / cost / 胜率 / by_reason；
       equity_curve 末值 == total_net（防"曲线仍按旧口径"）
  [S2] 恒等式：Σ run.net_cash ≡ Σ 全部成交现金流（独立 FIFO 重放器，
       按同向配对），全平仓后比对；不把"两侧不等"本身当断言
  [S3] FIFO 无关性：拆锁场景 run 的 entry = 拆锁 fill 价，
       ≠ 被平仓单自己的 entry_price（run 会计只认 fill）
  [S4] 费率档：拆锁入场按平昨档（CLOSE→开仓档）、R-OPEN 离场按开仓档；
       TA（FAK/1 手）与 AU（CLOSETODAY 两态机）各覆盖一轮
  [S5] run 中重启：entry_offset / entry_at 从 state.db 恢复，离场后 Trade 完整
  [S6] 对账强平：run 对应仓单被删 → 以 ref_price 就地结算（触发点 = 删除
       那一刻）；部分删除 run 仍在途；锁仓侧删除不触 run（零 Trade）。
       [S6-d] 判别用例：同侧多笔不同价 + anchor ≠ 被删仓单 entry，
       钉死 Reconcile 路径 Trade.entry = run 锚（复核报告唯一实质漏项）。
       镜像走两阶段（先"见过"再"消失"），过 P64 证据门
  [S7] shutdown_and_lock_all → Trade reason=auto_order_off
  [S7b] auto_order_off_retry 单列（首拒 + 下一根 K 线补平），
        不落入 sl/tp 桶
  [S9] 在途 run 零写入：开仓后不触发离场 → trades 表 0 行；离场 → 恰 1 行
  [S10] events.jsonl 中每个非空 trade_id 都能在 trades 表命中；
        拆锁入场的 close 事件 trade_id 留空（无 run 结算，不悬空）

全部走 on_bar / on_signal 正常入口（对账用例沿用 p44 的注入式构造）。
跑法：PYTHONPATH=. python Trading/Test/test_p52_run_accounting.py
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
for _p in (os.path.dirname(_TG_ROOT), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig, resolved_exit_params  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402
from Trading.Infra.Records import (AccountState, Bar, ExitPlan, Position,  # noqa: E402
                                   Side, Signal)
from Trading.Infra.StateDB import Store  # noqa: E402
from Trading.Infra.TradeStats import compute_trade_stats  # noqa: E402
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0

D1, D2, D3 = "2026-09-02", "2026-09-03", "2026-09-04"


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓ " if ok else "✗ ") + name
          + ("" if ok else "   -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, ok, detail=""):
    global _PASS, _FAIL
    ok = bool(ok)
    print(("✓ " if ok else "✗ ") + name + ("" if ok else "   -> " + str(detail)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_close(name, got, expected, tol=0.01):
    global _PASS, _FAIL
    ok = abs(float(got) - float(expected)) <= tol
    print(("✓ " if ok else "✗ ") + name
          + ("" if ok else "   -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="p52_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def ms(y, mo, d, h, mi):
    import datetime
    return int(datetime.datetime(y, mo, d, h, mi,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


def make_bar(date, hhmm, close, high, low, ts):
    return Bar(timestamp=ts, date="%s %s" % (date, hhmm), open=close,
               high=high, low=low, close=close)


def make_sig(date, hhmm, is_buy, price, ts, key=None):
    t = "B" if is_buy else "S"
    return Signal(key=key or "%s %s|1|%s" % (date, hhmm, t),
                  symbol="CFFEX.IF2609", freq="5m", date="%s %s" % (date, hhmm),
                  timestamp=ts, bsp_type="1", is_buy=is_buy, price=price,
                  high=price + 5, low=price - 5,
                  fractal_low=price - 15, fractal_high=price + 15)


def build(tmpdir, tag="a", profile=None, trade_symbol="CFFEX.IF2609",
          store_path=None):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    sig = "KQ.m@{}.{}".format(trade_symbol.split(".")[0],
                              (profile or PRODUCT_PROFILES["IF"]).product)
    if sig != cfg.instrument.signal_symbol:
        # 白名单闸门按 cfg.instrument.signal_symbol 放行，_assert_product_ssot
        # 要求它与 Instrument 持有的档案同源 —— 非默认品种必须构造期重建
        # instrument 配置（frozen=True 不可就地改，与 main.py build_runtime 同法）。
        cfg = cfg.model_copy(update={"instrument": cfg.instrument.model_copy(
            update={"signal_symbol": sig})})
    cfg.exit_params.use_atr = False
    cfg.exit_params.use_trailing = True
    cfg.exit_params.breakeven_trigger_r = 1.0
    cfg.exit_params.trailing_distance_points = 10.0
    spec = Instrument(InstrumentConfig(trade_symbol=trade_symbol,
                                       signal_symbol=sig),
                      profile or PRODUCT_PROFILES["IF"])
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    db = store_path or os.path.join(tmpdir, "state_%s.db" % tag)
    store = Store(db)
    ev = EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                  echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, EntryPolicy({}),
                        LayeredExitPolicy(resolved_exit_params(cfg)), store, ev)
    eng.state = spec
    return eng, store, broker, ev, db


def event_dicts(eng, tmp, tag="a"):
    eng.ev.flush()
    path = os.path.join(tmp, "events_%s.jsonl" % tag)
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def replay_fifo_cash(eng, broker):
    """[S2] 独立重放器：按 FIFO 把全部成交配成"仓单级"往返（**按同向**配对：
    close 报单的 side = 被平仓单方向（p15b [2.3d] 口径），side=SHORT 的平仓单
    平的就是空头持仓 → 对消簿内同号入场笔），返回 Σ(配对毛利 × 乘数 × 手数 − 费)。
    与引擎的 run 级账本完全独立（不读 store.trades / 不读 run 元数据），
    全平仓后二者必须相等（telescoping 恒等式）。费档读 DryRun meta["offset"]
    （= INTENT_TO_OFFSET 契约，测试正是要验证该契约语义）。"""
    book = []           # [price, side_sign, vol]（仅 intent=open 的入场笔）
    cash = 0.0
    mult = eng.state.multiplier
    for o in broker.orders:
        if o.status != "filled" or o.filled_price is None:
            continue
        intent = o.meta.get("intent")
        offset = o.meta.get("offset")
        fee = eng.state.single_fee(o.filled_price, offset, o.volume)
        if intent == "open":
            book.append([o.filled_price, o.side.sign, o.volume])
            cash -= fee
        else:                       # close：side=被平仓单方向 → 对消**同号**入场笔
            need = o.volume
            while need > 0:
                idx = next((i for i, b in enumerate(book)
                            if b[1] == o.side.sign), None)
                if idx is None:
                    raise AssertionError("重放器：平仓无足够反向持仓可对消")
                b = book[idx]
                m = min(need, b[2])
                cash += (o.filled_price - b[0]) * b[1] * mult * m
                cash -= fee * (m / o.volume)
                need -= m
                b[2] -= m
                if b[2] == 0:
                    book.pop(idx)
    if book:
        raise AssertionError("重放器：全平仓前提下簿内仍有残留入场笔")
    return cash


def read_events_trade_ids(eng, tmp, tag):
    ids = []
    for d in event_dicts(eng, tmp, tag):
        tid = d.get("trade_id")
        if tid:
            ids.append((d.get("kind"), tid))
    return ids


# ════════════════════════════════════════════════════════════════════
print("\n[T1] [S1]+[S3]+[S4]+[S2]+[S10] 四步例子端到端（IF，2 手，R-OPEN 两态）")
# ════════════════════════════════════════════════════════════════════
# 剧本（8 步 = 4 段 run，与转移表逐条对齐）：
#   ① D1 买信号       → 转移① 开多 A（run1 入场，OPEN 档）
#   ② D1 _force_exit  → 转移④ R-OPEN 空 B（run1 结算 = T1，离场 OPEN 档）→ 当日锁
#   ③ D2 买信号       → 转移③ 跨日拆锁：CLOSE 平空 B（run2 入场，CLOSE 档）
#   ④ D2 _force_exit  → 转移⑤ CLOSE 平昨 A（run2 结算 = T2，离场 CLOSE 档）→ FLAT
#   ⑤ D2 卖信号       → 转移① 开空 C（run3 入场，OPEN 档）
#   ⑥ D2 _force_exit  → 转移④ R-OPEN 多 D（run3 结算 = T3，离场 OPEN 档）→ 当日锁
#   ⑦ D3 卖信号       → 转移③ 跨日拆锁：CLOSE 平多 D（run4 入场，CLOSE 档）
#   ⑧ D3 _force_exit  → 转移⑤ CLOSE 平昨 C（run4 结算 = T4，离场 CLOSE 档）→ FLAT
# 中间 bar 全部落在"止损与保本触发带之外"，保证离场只由 _force_exit 发生。
with tmp_dir("four") as tmp:
    eng, store, broker, ev, _ = build(tmp, "a")
    # ── ① D1 信号1：空仓开多 A ──
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    check("[T1-a1] 开多成交，run1 入场（OPEN 档）",
          (len(eng.positions.positions), eng._run_entry_offset), (1, "OPEN"))
    entry_at_a = eng._run_entry_at
    check_true("[T1-a2] run 元数据 entry_at 已记（≠空）", bool(entry_at_a))
    # ── ② D1 触发 tp：今仓 → 反向 OPEN（R-OPEN 锁仓，T1 结算）──
    eng.on_bar(make_bar(D1, "09:45", 4512, 4514, 4502, ms(2026, 9, 2, 9, 45)))
    eng._force_exit(eng.last_bar, reason="tp")
    trades = store.trades()
    check("[T1-a3] T1 已收口（1 笔 Trade，reason=tp）",
          [(t["reason"], t["volume"]) for t in trades], [("tp", 2)])
    check("[T1-a4] R-OPEN 后停在锁仓态",
          eng.account_state().value, "locked")
    # ── ③ D2 信号：跨日锁 → 转移③ 拆锁平空 B（T2 入场，CLOSE 档）──
    eng.on_bar(make_bar(D2, "09:40", 4515, 4525, 4505, ms(2026, 9, 3, 9, 40)))
    eng.on_signal(make_sig(D2, "09:40", True, 4510.0, ms(2026, 9, 3, 9, 40)))
    check("[T1-b1] 拆锁成交 → run2 入场（entry_offset=CLOSE）",
          (eng.account_state().value, eng._run_entry_offset), ("running", "CLOSE"))
    check("[T1-b1b] 拆锁只平反向最早一笔（B），A 仍在 → 净敞口 +2",
          [(p.side.name, p.volume) for p in eng.positions.positions],
          [("LONG", 2)])
    # ── ④ D2 触发 tp：A 已跨日 → 转移⑤ CLOSE 平昨（T2 结算）→ FLAT ──
    eng.on_bar(make_bar(D2, "09:45", 4525, 4528, 4518, ms(2026, 9, 3, 9, 45)))
    eng._force_exit(eng.last_bar, reason="tp")
    check("[T1-b2] T2 已收口（2 笔）且账户 FLAT",
          (len(store.trades()), eng.account_state().value), (2, "flat"))
    # ── ⑤ D2 信号2：空仓开空 C（T3 入场，OPEN 档）──
    eng.on_signal(make_sig(D2, "10:20", False, 4480.0, ms(2026, 9, 3, 10, 20)))
    check("[T1-c1] 空开 → run3 入场（OPEN 档，side=SHORT）",
          (eng._run_side.name, eng._run_entry_offset), ("SHORT", "OPEN"))
    # ── ⑥ D2 触发 tp：今仓 → R-OPEN 多 D（T3 结算，OPEN 档）→ 当日锁 ──
    eng.on_bar(make_bar(D2, "10:25", 4475, 4485, 4470, ms(2026, 9, 3, 10, 25)))
    eng._force_exit(eng.last_bar, reason="tp")
    check("[T1-c2] T3 已收口（3 笔）且停在锁仓态",
          (len(store.trades()), eng.account_state().value), (3, "locked"))
    # ── ⑦ D3 信号：跨日锁 → 转移③ 拆锁平多 D（T4 入场，CLOSE 档）──
    eng.on_bar(make_bar(D3, "09:40", 4470, 4480, 4460, ms(2026, 9, 4, 9, 40)))
    eng.on_signal(make_sig(D3, "09:40", False, 4465.0, ms(2026, 9, 4, 9, 40)))
    check("[T1-d1] 拆锁平多 → run4 入场（CLOSE 档，side=SHORT）",
          (eng._run_side.name, eng._run_entry_offset), ("SHORT", "CLOSE"))
    # ── ⑧ D3 触发 tp：C 已跨日 → 转移⑤ CLOSE 平昨 C（T4 结算）→ FLAT ──
    eng.on_bar(make_bar(D3, "09:45", 4452, 4458, 4448, ms(2026, 9, 4, 9, 45)))
    eng._force_exit(eng.last_bar, reason="tp")
    trades = store.trades()
    check("[T1-d2] 四步剧本结束：4 笔 Trade、账户 FLAT、簿空",
          ([t["trade_id"] for t in trades], eng.account_state().value,
           len(eng.positions)),
          (["T{:05d}".format(i) for i in range(1, 5)], "flat", 0))

    # [S3] FIFO 无关性：T2 的 entry = 拆锁平空 B 的 fill 价，
    #      ≠ 被平仓单 A 自己的 entry（A 是 D1 开仓 fill 价）
    t1, t2, t3, t4 = trades
    close_orders = [o for o in broker.orders if o.meta.get("intent") != "open"]
    open_orders = [o for o in broker.orders if o.meta.get("intent") == "open"]
    check_true("[S3] T2 entry = 拆锁 fill，≠ A 仓单自己的 entry",
               t2["entry_price"] != open_orders[0].filled_price
               and t2["entry_price"] == close_orders[0].filled_price,
               (t2["entry_price"], open_orders[0].filled_price,
                close_orders[0].filled_price))
    check_true("[S3b] T4 entry = 拆锁平多 D 的 fill（同口径第二证）",
               t4["entry_price"] == close_orders[2].filled_price
               and t4["entry_price"] != open_orders[2].filled_price)

    # [S4] 费率档：每行 cost == 单边(入场, 入场档) + 单边(离场, 离场档)。
    #      入场档 = run_start 事件 entry_offset；离场档 = 离场委托 meta["offset"]
    #      （R-OPEN 离场是 intent=open 的委托，CLOSE 离场是 intent=close）。
    evs = event_dicts(eng, tmp, "a")
    run_starts = [d for d in evs if d.get("kind") == "run_start"]
    check("[S4-e1] 4 段 run 的 entry_offset 序列（OPEN/CLOSE/OPEN/CLOSE）",
          [d.get("entry_offset") for d in run_starts],
          ["OPEN", "CLOSE", "OPEN", "CLOSE"])
    # 离场委托按时间序：R-OPEN B（T1）、CLOSE A（T2）、R-OPEN D（T3）、CLOSE C（T4）
    exit_offsets = [open_orders[1].meta.get("offset"),
                    close_orders[0].meta.get("offset"),
                    open_orders[3].meta.get("offset"),
                    close_orders[2].meta.get("offset")]
    for i, (t, eo, xo) in enumerate(zip(trades,
                                        ["OPEN", "CLOSE", "OPEN", "CLOSE"],
                                        exit_offsets), 1):
        want = (eng.state.single_fee(t["entry_price"], eo, t["volume"])
                + eng.state.single_fee(t["exit_price"], xo, t["volume"]))
        check_close("[S4-e2] T%d cost = 单边(入场,%s) + 单边(离场,%s)"
                    % (i, eo, xo), t["cost_cash"], want, tol=0.02)
    check("[S4-e3] T2/T4 离场是平昨（CLOSE→开仓档 ≡ 平昨档）",
          (exit_offsets[1], exit_offsets[3]), ("CLOSE", "CLOSE"))

    # [S1] 统计量 + equity_curve 末值 == total_net
    rep = compute_trade_stats(trades)
    check("[S1-f1] 四笔全胜：win_rate=1.0",
          (rep["count"], rep["wins"], round(rep["win_rate"], 4)), (4, 4, 1.0))
    check_true("[S1-f2] 每笔 gross 同号（多头 exit>entry / 空头 exit<entry）",
               all(t["gross_points"] > 0 for t in trades),
               [t["gross_points"] for t in trades])
    check("[S1-f3] equity_curve 末值 cumulative == total_net",
          rep["equity_curve"][-1]["cumulative"], rep["total_net"])
    check("[S1-f4] by_reason 只含 tp（不污染）",
          sorted(rep["by_reason"].keys()), ["tp"])

    # [S2] 恒等式：Σ run.net_cash ≡ Σ 全部成交现金流（独立 FIFO 重放）
    check_close("[S2-g1] Σrun.net_cash ≡ 重放现金流（全平仓）",
                sum(t["net_cash"] for t in trades),
                replay_fifo_cash(eng, broker), tol=0.02)
    check("[S2-g2] 结构性门禁：全平仓后簿空（无在途 run 前提成立）",
          (len(eng.positions), eng._run_side is None), (0, True))

    # [S10] events.jsonl 无悬空 trade_id
    tids = {t["trade_id"] for t in trades}
    used = read_events_trade_ids(eng, tmp, "a")
    dangling = [x for x in used if x[1] not in tids]
    check_true("[S10-h1] 事件里每个非空 trade_id 都在 trades 表",
               dangling == [], dangling)
    check_true("[S10-h2] close 事件带回了 run 结算的 trade_id（回填）",
               any(k == "close" and tid in tids for k, tid in used), used[:6])
    empty_close_tids = [d for d in evs if d.get("kind") == "close"
                        and d.get("trade_id") == ""]
    check_true("[S10-h3] 拆锁入场 close 事件 trade_id 留空（无结算可回填）",
               len(empty_close_tids) == 2, len(empty_close_tids))
    # P1-2 护栏（验收报告）：close 事件必须在 _run_end() 之前写，此时
    # `_run_plan` 未清，exit_policy 取 run 真实计划名；若回归到
    # `_run_reset` 之后写，会退化成占位名 'run_managed'。
    close_evs = [d for d in evs if d.get("kind") == "close"]
    check("[S10-h4] close 事件 exit_policy = run 真实计划名（非 run_managed）",
          sorted({d.get("exit_policy") for d in close_evs}),
          ["LayeredExitPolicy"])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T2] [S9] 在途 run 零写入")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("inflight") as tmp:
    eng, store, broker, ev, _ = build(tmp, "a")
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.on_bar(make_bar(D1, "09:45", 4502, 4512, 4492, ms(2026, 9, 2, 9, 45)))
    eng.on_bar(make_bar(D1, "09:50", 4504, 4514, 4494, ms(2026, 9, 2, 9, 50)))
    check("[S9-a] 开仓 + 2 根 bar（未触发离场）→ trades 0 行",
          len(store.trades()), 0)
    check_true("[S9-b] 持仓非空（run 在途）", len(eng.positions) == 1)
    eng._force_exit(eng.last_bar, reason="tp")
    check("[S9-c] 触发离场 → 恰好新增 1 笔", len(store.trades()), 1)
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T3] [S7] shutdown → reason=auto_order_off")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("shutdown") as tmp:
    eng, store, broker, ev, _ = build(tmp, "a")
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()
    trades = store.trades()
    check("[S7-a] shutdown 今仓 R-OPEN → 1 笔 Trade，reason=auto_order_off",
          [(t["reason"], t["volume"]) for t in trades], [("auto_order_off", 2)])
    check("[S7-b] 强平离场档 = OPEN（今仓反向开）",
          trades[0]["exit_price"] == broker.orders[-1].filled_price
          and broker.orders[-1].meta["offset"], "OPEN")
    check_true("[S7-c] 强平不回落 run 计划名（reason 单列）",
               trades[0]["reason"] not in ("tp", "sl", "trailing"))
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T4] [S7b] auto_order_off_retry 单列（首轮离场被拒 + 下一根 K 线补平）")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("retry") as tmp:
    class RejectOnceBroker(DryRunBroker):
        """首轮离场委托被拒（模拟价格不可达），其余正常。"""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.reject_left = 1

        def submit(self, intent, side, volume, ref_price, signal_key="",
                   note="", entry_date="", is_exit=False):
            o = super().submit(intent, side, volume, ref_price, signal_key,
                               note, entry_date, is_exit)
            if self.reject_left > 0 and is_exit:
                self.reject_left -= 1
                o.status = "rejected"
                o.filled_price = None
                o.meta["reject_reason"] = "价格不可达（测试注入）"
                o.meta["reject_class"] = "price"
            return o

    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.exit_params.use_atr = False
    spec = Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"),
                      PRODUCT_PROFILES["IF"])
    broker = RejectOnceBroker(spec, {"sim_equity": 1_000_000.0})
    store = Store(os.path.join(tmp, "state_a.db"))
    ev = EventLog(os.path.join(tmp, "events_a.jsonl"), echo=False, echo_kinds=None)
    eng = TradingEngine(cfg, broker, EntryPolicy({}),
                        LayeredExitPolicy(resolved_exit_params(cfg)), store, ev)
    eng.state = spec
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    eng.shutdown_and_lock_all()
    check("[S7b-a] 首轮离场被拒 → residual（RUNNING），trades 0 行",
          (len(store.trades()), eng.account_state().value), (0, "running"))
    # 下一根 K 线：关闭态 on_bar 补做 → reason=auto_order_off_retry
    eng.on_bar(make_bar(D1, "09:45", 4502, 4512, 4492, ms(2026, 9, 2, 9, 45)))
    trades = store.trades()
    check("[S7b-b] retry 补平成交 → 1 笔 Trade，reason=auto_order_off_retry",
          [(t["reason"], t["volume"]) for t in trades],
          [("auto_order_off_retry", 2)])
    rep = compute_trade_stats(trades)
    check_true("[S7b-c] retry 不落入 sl/tp 桶（by_reason 单列）",
               set(rep["by_reason"].keys()) == {"auto_order_off_retry"},
               rep["by_reason"])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T5] [S5] run 中重启：entry_offset / entry_at 从 state.db 恢复")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("restart") as tmp:
    db = os.path.join(tmp, "state.db")
    ev_path = os.path.join(tmp, "events.jsonl")
    eng, store, broker, ev, _ = build(tmp, "a", store_path=db)
    eng.on_bar(make_bar(D1, "09:40", 4500, 4510, 4490, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 4505.0, ms(2026, 9, 2, 9, 40)))
    anchor0, eo0, ea0 = eng._run_anchor, eng._run_entry_offset, eng._run_entry_at
    store.close()
    ev.flush()
    # 重启（同一 store）
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = Instrument(InstrumentConfig(trade_symbol="CFFEX.IF2609"),
                      PRODUCT_PROFILES["IF"])
    store2 = Store(db)
    ev2 = EventLog(ev_path, echo=False, echo_kinds=None)
    eng2 = TradingEngine(cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
                         EntryPolicy({}),
                         LayeredExitPolicy(resolved_exit_params(cfg)),
                         store2, ev2)
    eng2.state = spec
    check("[S5-a] 重启后 run 元数据完整恢复",
          (round(eng2._run_anchor, 3), eng2._run_entry_offset,
           eng2._run_entry_at == ea0),
          (round(anchor0, 3), "OPEN", True))
    eng2.on_bar(make_bar(D1, "09:45", 4502, 4512, 4492, ms(2026, 9, 2, 9, 45)))
    eng2._force_exit(eng2.last_bar, reason="tp")
    trades = store2.trades()
    check("[S5-b] 重启后离场 → Trade 完整（entry=重启前锚，entry_at=恢复值）",
          (len(trades), round(trades[0]["entry_price"], 3),
           trades[0]["entry_at"] == ea0),
          (1, round(anchor0, 3), True))
    store2.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T6] [S6] 对账强平并入 run 结算（p44 注入式构造 + 两阶段镜像过证据门）")
# ════════════════════════════════════════════════════════════════════


def inject_run(eng, side, anchor=4500.0, seq=100, key="T6"):
    eng._run_side = side
    eng._run_anchor = anchor
    eng._run_volume = 2
    eng._run_bar_ts = 0
    eng._run_bar_seq = seq
    eng._run_signal_key = key
    eng._run_plan = ExitPlan(name="run_managed", stop_price=0.0)
    eng._run_entry_offset = "OPEN"
    eng._run_entry_at = "2026-09-02 09:40:00"


def make_pos(eng, side, vol, price, entry_date, seq):
    return Position(symbol=eng.state.trade_symbol, side=side, volume=vol,
                    entry_price=price, entry_at="2026-09-02 09:40:00",
                    entry_bar_ts=0, entry_bar_seq=seq,
                    signal_key="T6", open_order_id="",
                    exit_plan=ExitPlan(name="run_managed", stop_price=0.0),
                    entry_date=entry_date)


class TwoPhaseRealBroker(DryRunBroker):
    """柜台镜像可注入（None = 该侧未武装）。P64 证据门要求镜像**先见过**
    这笔仓（读到 ≥ 账本量）才有资格说它消失 —— 故每个场景先以"一致"读数
    跑一轮对账（prime），再切到目标读数（act）。"""

    def __init__(self, spec, params=None):
        super().__init__(spec, params or {"sim_equity": 1_000_000.0})
        self._rv = {}

    def set_real(self, side: str, vol):
        self._rv[side] = vol

    def real_position(self, side):
        return self._rv.get("LONG" if side is Side.LONG else "SHORT")


with tmp_dir("rec1") as tmp:
    # 场景 A：RUNNING（run 在途）→ 柜台全平 → run 以 ref_price 就地结算
    eng, store, broker, ev, _ = build(tmp, "a")
    broker = eng.broker = TwoPhaseRealBroker(eng.state, {"sim_equity": 1_000_000.0})
    eng.on_bar(make_bar(D1, "09:40", 4520, 4530, 4510, ms(2026, 9, 2, 9, 40)))
    eng.positions.add(make_pos(eng, Side.LONG, 2, 4500.0, D1, 100))
    inject_run(eng, Side.LONG, anchor=4500.0)
    broker.set_real("LONG", 2)          # prime：镜像先"见过"这 2 手
    eng._reconcile_positions("probe")
    check("[S6-a0] prime 轮：读数一致 → 簿不动、零 Trade",
          (len(eng.positions), len(store.trades())), (1, 0))
    broker.set_real("LONG", 0)          # act：柜台全平
    eng._reconcile_positions("probe")
    trades = store.trades()
    check("[S6-a1] run 侧仓单被对账删除 → 就地结算 1 笔",
          [(t["reason"], t["volume"]) for t in trades],
          [("reconcile_external_partial", 2)])
    t = trades[0]
    check("[S6-a2] entry = run 锚，exit = 对账参考价（last_bar.close）",
          (round(t["entry_price"], 3), t["exit_price"]), (4500.0, 4520.0))
    check("[S6-a3] 结算后 run 已终结、账户 FLAT",
          (eng._run_side is None, eng.account_state().value), (True, "flat"))
    check("[S6-a4] 不写 run_end 事件（对账清仓无正常离场语义）",
          sum(1 for d in event_dicts(eng, tmp, "a")
              if d.get("kind") == "run_end"), 0)
    check_true("[S6-a5] position_externally_closed 事件带回 trade_id",
               any(d.get("kind") == "position_externally_closed"
                   and d.get("trade_id") in {x["trade_id"] for x in trades}
                   for d in event_dicts(eng, tmp, "a")))
    # 仓单级盈亏快照（自查新发现修复护栏）：删除事件必须携带被删仓单
    # 自身的段盈亏 —— 单笔场景下仓单级 == run 级（entry 即 run 锚）。
    _ev = [d for d in event_dicts(eng, tmp, "a")
           if d.get("kind") == "position_externally_closed"][0]
    check("[S6-a6] 删除事件带仓单级 gross_points（4520−4500=20 点）",
          _ev["gross_points"], 20.0)
    check_true("[S6-a7] 事件 net_cash 与 run 级 Trade 成本口径同源",
               abs(_ev["net_cash"] - t["net_cash"]) < 1e-6,
               (_ev["net_cash"], t["net_cash"]))
    store.close()

with tmp_dir("rec2") as tmp:
    # 场景 B：部分删除（柜台少 1 手）→ 结算被删手数，run 仍在途
    eng, store, broker, ev, _ = build(tmp, "a")
    broker = eng.broker = TwoPhaseRealBroker(eng.state, {"sim_equity": 1_000_000.0})
    eng.on_bar(make_bar(D1, "09:40", 4520, 4530, 4510, ms(2026, 9, 2, 9, 40)))
    eng.positions.add(make_pos(eng, Side.LONG, 1, 4500.0, D1, 100))
    eng.positions.add(make_pos(eng, Side.LONG, 1, 4505.0, D1, 101))
    inject_run(eng, Side.LONG, anchor=4500.0)
    broker.set_real("LONG", 2)          # prime
    eng._reconcile_positions("probe")
    broker.set_real("LONG", 1)          # act：柜台少 1 手
    eng._reconcile_positions("probe")
    trades = store.trades()
    check("[S6-b1] 部分删除 → 结算被删手数（1 手，FIFO 最早一笔）",
          [(t["reason"], t["volume"]) for t in trades],
          [("reconcile_external_partial", 1)])
    check("[S6-b2] run 仍在途（净敞口 1，继续管剩余）",
          (eng._run_side.name, eng.account_state().value), ("LONG", "running"))
    store.close()

with tmp_dir("rec3") as tmp:
    # 场景 C：LOCKED 双侧同清 → 均为锁仓侧删除 → 零 Trade（run 已随锁仓 fill 收口）
    eng, store, broker, ev, _ = build(tmp, "a")
    broker = eng.broker = TwoPhaseRealBroker(eng.state, {"sim_equity": 1_000_000.0})
    eng.on_bar(make_bar(D1, "09:40", 4520, 4530, 4510, ms(2026, 9, 2, 9, 40)))
    eng.positions.add(make_pos(eng, Side.LONG, 2, 4500.0, D1, 100))
    eng.positions.add(make_pos(eng, Side.SHORT, 2, 4510.0, D1, 101))
    check("[S6-c0] 预置锁仓态（净 0）", eng.account_state().value, "locked")
    broker.set_real("LONG", 2)          # prime 两侧
    broker.set_real("SHORT", 2)
    eng._reconcile_positions("probe")
    broker.set_real("LONG", 0)          # act：两侧同清
    broker.set_real("SHORT", 0)
    eng._reconcile_positions("probe")
    check("[S6-c1] 双侧同清、锁仓侧删除不触 run → 零 Trade",
          (len(store.trades()), len(eng.positions), eng.account_state().value),
          (0, 0, "flat"))
    # 锁仓侧删除的可见性护栏（自查新发现修复）：该场景 trades 零行，
    # 删除事件的仓单级盈亏是唯一载体 —— 修复前双层不可见。
    _evs = [d for d in event_dicts(eng, tmp, "a")
            if d.get("kind") == "position_externally_closed"]
    check("[S6-c2] 锁仓侧删除事件带仓单级 gross（LONG +20 / SHORT −10 点）",
          sorted((d["side"], d["gross_points"]) for d in _evs),
          sorted([(str(Side.LONG), 20.0), (str(Side.SHORT), -10.0)]))
    check_true("[S6-c3] 锁仓侧删除事件 net_cash 有值（不回退为缺字段）",
               all(isinstance(d.get("net_cash"), (int, float)) for d in _evs),
               [d.get("net_cash") for d in _evs])
    store.close()

with tmp_dir("rec4") as tmp:
    # 场景 D（[S6-d]，复核报告 ⑵ 唯一实质漏项）：部分强平 × 同侧多笔不同价，
    # 且 run 锚 ≠ 被删仓单 entry —— 判别用例。FIFO 删最早一笔（4500），
    # run 锚取 4510（= 簿内另一笔的 entry）：若把 Reconcile 路径的
    # Trade.entry 改成被删仓单 entry（P0-1 候选补丁口径），[S6-d2] 立即翻红。
    # 分层口径（与 Reconcile.py 事件注释同源）：删除事件 gross = 被删仓单
    # 身份，Trade gross = run 身份（run 锚口径），两者并存不是矛盾。
    eng, store, broker, ev, _ = build(tmp, "a")
    broker = eng.broker = TwoPhaseRealBroker(eng.state, {"sim_equity": 1_000_000.0})
    eng.on_bar(make_bar(D1, "09:40", 4520, 4530, 4510, ms(2026, 9, 2, 9, 40)))
    eng.positions.add(make_pos(eng, Side.LONG, 1, 4500.0, D1, 100))
    eng.positions.add(make_pos(eng, Side.LONG, 1, 4510.0, D1, 101))
    inject_run(eng, Side.LONG, anchor=4510.0)
    broker.set_real("LONG", 2)          # prime
    eng._reconcile_positions("probe")
    broker.set_real("LONG", 1)          # act：柜台少 1 手 → FIFO 删 4500 仓单
    eng._reconcile_positions("probe")
    trades = store.trades()
    check("[S6-d1] 部分删除 → 结算被删手数（1 手，FIFO 最早 4500 笔）",
          [(t["reason"], t["volume"]) for t in trades],
          [("reconcile_external_partial", 1)])
    check("[S6-d2] Trade.entry = run 锚 4510（≠ 被删仓单 4500）、exit = ref 4520",
          (round(trades[0]["entry_price"], 3), trades[0]["exit_price"]),
          (4510.0, 4520.0))
    check("[S6-d3] run 仍在途（净敞口 1，继续管剩余）",
          (eng._run_side.name, eng.account_state().value), ("LONG", "running"))
    _evd = [d for d in event_dicts(eng, tmp, "a")
            if d.get("kind") == "position_externally_closed"]
    check("[S6-d4] 分层：事件 gross = 被删仓单段盈亏 20 点（4500→4520），"
          "Trade gross = run 锚段盈亏 10 点（4510→4520）",
          (_evd[0]["gross_points"], trades[0]["gross_points"]), (20.0, 10.0))
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T7] [S4b] TA（FAK / 1 手）费率档")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("ta") as tmp:
    eng, store, broker, ev, _ = build(tmp, "a", profile=PRODUCT_PROFILES["TA"],
                                      trade_symbol="CZCE.TA2601")
    eng.on_bar(make_bar(D1, "09:40", 5200, 5210, 5190, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 5195.0, ms(2026, 9, 2, 9, 40)))
    check("[T7-a] TA FAK 1 手成交", (len(eng.positions), eng.positions.positions[0].volume),
          (1, 1))
    eng._force_exit(eng.last_bar, reason="tp")
    trades = store.trades()
    eo = [d for d in event_dicts(eng, tmp, "a")
          if d.get("kind") == "run_start"][0]["entry_offset"]
    xo = broker.orders[-1].meta["offset"]
    want = (eng.state.single_fee(trades[0]["entry_price"], eo, 1)
            + eng.state.single_fee(trades[0]["exit_price"], xo, 1))
    check("[T7-b] TA cost = 单边(入场,%s) + 单边(离场,%s)" % (eo, xo),
          [(t["reason"], round(t["cost_cash"], 2)) for t in trades],
          [("tp", round(want, 2))])
    store.close()

# ════════════════════════════════════════════════════════════════════
print("\n[T8] [S8] 两态机 AU（CLOSETODAY）：今仓离场走平今档")
# ════════════════════════════════════════════════════════════════════
with tmp_dir("au") as tmp:
    eng, store, broker, ev, _ = build(tmp, "a", profile=PRODUCT_PROFILES["AU"],
                                      trade_symbol="SHFE.AU2602")
    eng.on_bar(make_bar(D1, "09:40", 780, 782, 778, ms(2026, 9, 2, 9, 40)))
    eng.on_signal(make_sig(D1, "09:40", True, 779.0, ms(2026, 9, 2, 9, 40)))
    eng._force_exit(eng.last_bar, reason="tp")
    trades = store.trades()
    xo = broker.orders[-1].meta["offset"]
    check("[S8-a] AU 今仓离场 → CLOSETODAY 意图（两态机）", xo, "CLOSETODAY")
    want = (eng.state.single_fee(trades[0]["entry_price"], "OPEN", 2)
            + eng.state.single_fee(trades[0]["exit_price"], "CLOSETODAY", 2))
    check_close("[S8-b] cost = 单边(入场,OPEN) + 单边(离场,CLOSETODAY 平今档)",
                trades[0]["cost_cash"], want, tol=0.02)
    check("[S8-c] 两态机回归等价：1 笔 run Trade、reason=tp",
          (len(trades), trades[0]["reason"], trades[0]["volume"]),
          (1, "tp", 2))
    store.close()

print("\n══════════════════════════════")
print("PASS = %d  FAIL = %d" % (_PASS, _FAIL))
sys.exit(1 if _FAIL else 0)
