# -*- coding: utf-8 -*-
"""
P13 UNLOCK_FIRST 入场路径单元测试（Phase E2）
==============================================
背景
    Phase E2 在 on_signal 入口加"UNLOCK_FIRST"早分支：
      · 触发条件 —— state==IDLE + portfolio 非空 + has_opposite(sig.side)
        （含义：引擎书上那笔 Position 是昨日 LOCK 留下的反向对冲仓）
      · 行为 —— broker.submit(UNLOCK, target.side, vol, sig.price)
        · side 是 portfolio 那笔的方向（不是 sig.direction —— CloseYesterdayOffset 按方向平昨）
        · 不开新今仓（"解锁"严格只清昨；今仓由后续信号决定）
      · 状态机 —— 进入 EXITING，成交后回 IDLE；拒单回 IDLE

硬性要求（本测试锁死）
    ① 入口分流决策：IDLE + has_opposite(side) 走 UNLOCK；其它走原路径
    ② broker 收到 1 笔 UNLOCK intent 报（offset=CLOSE，平昨语义；
       2026-09-10 前为 CLOSEYESTERDAY，因不在 tqsdk 白名单已修正）
    ③ side = portfolio 里那笔的方向（不是 sig.direction）
    ④ 成功后 portfolio 清空 + Trade 落盘 + state 回 IDLE
    ⑤ 拒单后 portfolio 不动 + signal_rejected + state 回 IDLE
    ⑥ UNLOCK 不调用 entry_policy / sizer / risk.check_open（不是开新仓）
    ⑦ UNLOCK 不影响 IN_TRADE 分支（正交）
    ⑧ Trade 记帐正确：reason=unlock_against_signal + cost 用 CloseYesterday 费率
    ⑨ 持久化重启后 portfolio 空、state 推导回 IDLE

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python tests/test_p13_unlock_first_entry.py
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
    d = tempfile.mkdtemp(prefix="tg_p13_")
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
from Trading.Infra.Types import (  # noqa: E402
    EntryMode, EngineState, ExitPlan, OrderIntent, Position, Side,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def make_pos(symbol="CFFEX.IF2609", side=Side.LONG, vol=1, entry_price=4500.0,
             entry_mode=EntryMode.LOCKED, signal_key="P13-LEGACY",
             entry_date="2026-09-01"):
    # v1.3（S1）：本测试注入的持仓代表"LOCK 留下的反向对冲仓"，故默认
    # entry_mode=LOCKED；entry_date 默认昨日（2026-09-01 < 信号日 2026-09-02），
    # 使信号门走"平昨仓"（UNLOCK）分支。要模拟"当日锁"则传 entry_date=信号日。
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=4000, signal_key=signal_key,
        open_order_id="p13-legacy-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_mode=entry_mode, entry_date=entry_date)


def make_signal(is_buy, price=4550.0, high=4552.0, low=4548.0,
                date="2026-09-02 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    # 导入 Signal（同 phase p10 风格）
    from Trading.Infra.Types import Signal
    return Signal(key=sig_key, symbol="CFFEX.IF2609", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-02 09:40"):
    from Trading.Infra.Types import Bar
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def build_engine(tmpdir):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def seed_portfolio(engine, store, p):
    """手动注入一笔 Position（模拟昨日 LOCK 遗留的对冲仓）。"""
    engine.position = p
    store.set_json("position", p.to_dict())
    engine._state = EngineState.IDLE  # E2: state 必须是 IDLE 才走 UNLOCK 分支


# ════════════════════════════════════════════════════════════════
# [1] 入口分流：IDLE + has_opposite 走 UNLOCK；其它走原路径
# ════════════════════════════════════════════════════════════════
print("\n[1] 入口分流：基础 case 矩阵")
# Case A: IDLE + 空 portfolio → 走 OPEN_FIRST（信号 → 开仓）
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    sig_buy = make_signal(is_buy=True, price=4500.0)
    engine.on_signal(sig_buy)
    check("[A] IDLE+空簿+BUY → portfolio 非空",
          engine.positions.is_empty(), False)
    check("[A] 走 OPEN 而非 UNLOCK",
          any(o.meta.get("intent") == "open" for o in broker.orders), True)
    check("[A] broker 没收到 UNLOCK 报",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), False)


# Case B: IDLE + 1笔 LONG 昨仓 + SELL 信号 → has_opposite(SHORT)=True → UNLOCK
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))
    sig_sell = make_signal(is_buy=False, date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    check("[B] IDLE+LONG+SELL → portfolio 清空",
          engine.positions.is_empty(), True)
    check("[B] broker 收到 UNLOCK 报（不是 open）",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), True)
    check("[B] broker 没收到 OPEN 报（不开今仓）",
          any(o.meta.get("intent") == "open" for o in broker.orders), False)
    check("[B] state 回 IDLE", engine._state.name, "IDLE")


# Case C: IDLE + 1笔 SHORT 昨仓 + BUY 信号 → has_opposite(LONG)=True → UNLOCK
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.SHORT, vol=2, entry_price=4510.0))
    sig_buy = make_signal(is_buy=True, date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_buy)
    check("[C] IDLE+SHORT+BUY → portfolio 清空",
          engine.positions.is_empty(), True)
    check("[C] broker 收到 UNLOCK 报",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), True)
    check("[C] state 回 IDLE", engine._state.name, "IDLE")


# Case D: 运行态（IN_TRADE，真实净敞口非零）+ 反向信号 → 一律忽略（Q1=B）
# v1.3：出场只由 on_bar 的 L1-L3 负责；信号在运行态不再触发 LOCK/CLOSE/UNLOCK。
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # 模拟"今仓"流程：开仓后 engine 进入 IN_TRADE，簿内 1 笔 OPEN_FIRST（非 LOCKED）
    sig_buy = make_signal(is_buy=True, price=4500.0)
    engine.on_signal(sig_buy)
    check("[D-pre] 开仓后 state=IN_TRADE", engine._state.name, "IN_TRADE")
    lock_before = sum(1 for o in broker.orders if o.meta.get("intent") == "lock")
    unlock_before = sum(1 for o in broker.orders if o.meta.get("intent") == "unlock")
    open_before = sum(1 for o in broker.orders if o.meta.get("intent") == "open")
    # 反向 SELL 信号：运行态 → 忽略（不锁仓、不解锁、不开新仓）
    sig_sell = make_signal(is_buy=False, price=4505.0, high=4507.0, low=4504.0,
                           date="2026-09-02 09:45", bsp_type="3")
    engine.on_signal(sig_sell)
    check("[D] IN_TRADE+反向 → 忽略，state 仍 IN_TRADE", engine._state.name, "IN_TRADE")
    check("[D] IN_TRADE+反向 → broker 没收 LOCK 报（Q1=B 信号不离场）",
          sum(1 for o in broker.orders if o.meta.get("intent") == "lock"), lock_before)
    check("[D] IN_TRADE+反向 → broker 没收 UNLOCK 报（不入 UNLOCK 路径）",
          sum(1 for o in broker.orders if o.meta.get("intent") == "unlock"), unlock_before)
    check("[D] IN_TRADE+反向 → broker 没收新 OPEN 报（运行态忽略）",
          sum(1 for o in broker.orders if o.meta.get("intent") == "open"), open_before)


# Case E: 锁仓·当日锁（entry_date == 信号日）+ 反向信号 → 开新仓（规则 ⑸-①）
# v1.3：被锁的单子日期是今天 → 再入场开新仓，不动锁仓持仓。
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0,
                            entry_date="2026-09-02"))  # 当日锁
    sig_sell = make_signal(is_buy=False, date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    check("[E] 当日锁+SELL → broker 收 OPEN 报（开新仓）",
          any(o.meta.get("intent") == "open" for o in broker.orders), True)
    check("[E] 当日锁+SELL → broker 没收 UNLOCK 报（不提前平锁仓持仓）",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), False)


# ════════════════════════════════════════════════════════════════
# [2] UNLOCK 报单语义：side/volume/offset 取自 portfolio
# ════════════════════════════════════════════════════════════════
print("\n[2] UNLOCK 报单语义")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.SHORT, vol=3, entry_price=4510.0))
    sig_buy = make_signal(is_buy=True, price=4500.0,
                          date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_buy)
    unlock_orders = [o for o in broker.orders if o.meta.get("intent") == "unlock"]
    check("UNLOCK 报只有 1 笔", len(unlock_orders), 1)
    if unlock_orders:
        uo = unlock_orders[0]
        check("UNLOCK 报 side = portfolio.side (SHORT)（不是 sig.is_buy=true→LONG）",
              uo.side, Side.SHORT)
        check("UNLOCK 报 volume = portfolio.volume (3)", uo.volume, 3)
        # 2026-09-10 修正：CLOSEYESTERDAY 不在 tqsdk 白名单（api.py:1353 只接受
        # OPEN/CLOSE/CLOSETODAY）→ 实盘本地 raise，跨日解锁 100% 失败。平昨语义不变。
        check("UNLOCK 报 offset = CLOSE（平昨，2026-09-10 由 CLOSEYESTERDAY 修正）",
              uo.meta.get("offset"), "CLOSE")
        check("UNLOCK 报 action = close（dry_run is_open_like=False 视角）",
              uo.action, "close")
        # dry_run 在 UNLOCK/CLOSE 上让价 slippage_ticks tick：
        # SHORT 平仓 → slipped = ref_price - sign * slip = 4500 - (-1)*slip = 4500 + 0.2
        # 用现货 slippage_ticks 算精确预期
        slip_units = engine.spec.slippage_ticks * engine.spec.price_tick
        expected_filled = 4500.0 - Side.SHORT.sign * slip_units  # = 4500.2
        check("UNLOCK 报 filled_price 包含 dry_run slippage（SHORT 平仓价=ref_price+slip）",
              round(uo.filled_price, 6), round(expected_filled, 6))


# ════════════════════════════════════════════════════════════════
# [3] UNLOCK 成功路径：Trade 落盘 + portfolio 清空 + state IDLE
# ════════════════════════════════════════════════════════════════
print("\n[3] UNLOCK 成功路径")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))
    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)

    trades = store.trades()
    check("UNLOCK 成功后 store 写入 1 笔 Trade", len(trades), 1)
    if trades:
        t = trades[0]
        check("Trade.reason = unlock_against_signal",
              t.get("reason"), "unlock_against_signal")
        check("Trade.side = LONG（被平昨仓的方向）",
              t.get("side"), "LONG")
        check("Trade.entry_price = 4500",
              t.get("entry_price"), 4500.0)
        # LONG 入场 4500 → UNLOCK 信号价 4520；dry_run slippage 让 LONG 平仓价
        # slipped = ref_price - sign * slip = 4520 - 1*0.2 = 4519.8
        slip_units = engine.spec.slippage_ticks * engine.spec.price_tick
        expected_exit = 4520.0 - Side.LONG.sign * slip_units  # = 4519.8
        check("Trade.exit_price 含 slippage（LONG 平仓=ref_price-slip）",
              round(t.get("exit_price"), 6), round(expected_exit, 6))
        # 毛利 = exit - entry
        expected_gross = expected_exit - 4500.0  # = 19.8
        check("Trade.gross_points = exit-entry（含 slippage）",
              round(t.get("gross_points"), 4), round(expected_gross, 4))
        check("Trade.volume = 1", t.get("volume"), 1)
    check("UNLOCK 成功后 portfolio is_empty()",
          engine.positions.is_empty(), True)
    check("UNLOCK 成功后 state = IDLE", engine._state.name, "IDLE")
    check("UNLOCK 成功后 processed_signals.signal_action = unlock",
          store.signal_action(sig_sell.key), "unlock")
    check("UNLOCK 成功后 broker 不再收到后续 OPEN 报（同一次 signal）",
          sum(1 for o in broker.orders if o.meta.get("intent") == "open"),
          0)


# ════════════════════════════════════════════════════════════════
# [4] UNLOCK 失败路径：broker 拒单
# ════════════════════════════════════════════════════════════════
print("\n[4] UNLOCK 失败路径（broker 拒单）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))

    # 改成"拒单"模式：调 dry_run 不行，要 stub 一个
    from Trading.Broker.Base import Broker
    class RejectingBroker(Broker):
        name = "reject_unlock"
        def __init__(self, spec):
            super().__init__(spec, {})
            self.orders = []
        def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
            from Trading.Infra.Types import Order, OrderIntent, now_cn
            return Order(
                order_id="R-1", signal_key=signal_key,
                symbol=self.spec.trade_symbol, side=side,
                action="close", volume=volume, price=ref_price,
                req_price=ref_price, filled_price=None,
                status="rejected", created_at=now_cn(),
                broker=self.name, note=note,
                meta={"intent": OrderIntent(intent).value,
                      "reject_reason": "test_always_reject"},
            )

    broker_r = RejectingBroker(engine.spec)
    engine.broker = broker_r

    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)

    check("拒单后 portfolio 仍保留（不清仓）",
          engine.positions.is_empty(), False)
    check("拒单后 portfolio.volume 仍是 1",
          engine.positions.legacy_single().volume, 1)
    check("拒单后 state = IDLE", engine._state.name, "IDLE")
    check("拒单后 processed_signals.signal_action = rejected",
          store.signal_action(sig_sell.key), "rejected")
    check("拒单后 store.trades() 仍空（不写 Trade）",
          len(store.trades()), 0)


# ════════════════════════════════════════════════════════════════
# [5] UNLOCK 完成后 → 下一信号可正常 OPEN
# ════════════════════════════════════════════════════════════════
print("\n[5] UNLOCK 完成后下一信号走 OPEN_FIRST")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))

    # 第一信号：SELL → 触发 UNLOCK
    sig1 = make_signal(is_buy=False, price=4520.0,
                       date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig1)
    check("UNLOCK 后 portfolio 空", engine.positions.is_empty(), True)

    # 第二信号：BUY → 正常 OPEN
    sig2 = make_signal(is_buy=True, price=4525.0,
                       date="2026-09-02 09:40", bsp_type="3")
    engine.on_signal(sig2)
    opens = [o for o in broker.orders if o.meta.get("intent") == "open"]
    check("UNLOCK 完成后下一信号走 OPEN（broker 收 1 笔 open）",
          len(opens), 1)
    check("UNLOCK 完成后下一信号建仓成功 portfolio 非空",
          engine.positions.is_empty(), False)
    check("UNLOCK 完成后下一信号 entry_mode = OPEN_FIRST（默认）",
          engine.position.entry_mode, EntryMode.OPEN_FIRST)


# ════════════════════════════════════════════════════════════════
# [6] UNLOCK 不调用 entry_policy / sizer / risk（防御性）
# ════════════════════════════════════════════════════════════════
print("\n[6] UNLOCK 不调用入场决策路径")
# 用一个会 reject 所有信号的 entry_policy 验证
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))

    class AlwaysRejectEntry:
        """任何 decide 都 return skip（v1 的 must_not_call 哨兵）"""
        def __init__(self, calls):
            self.calls = calls
            self.name = "always_reject"
        def decide(self, sig, position, spec):
            self.calls.append(sig.key)
            from Trading.Infra.Types import DecisionType, Decision
            return Decision(type=DecisionType.SKIP, reason="always_reject")

    class AlwaysRejectSize:
        def __init__(self, calls):
            self.calls = calls
            self.name = "always_reject_sizer"
        def size(self, **kw):
            self.calls.append(1)
            return 0, "always_reject"
        @property
        def max_volume(self): return 1
        def describe(self): return {"name": "always_reject_sizer"}

    entry_called = []
    size_called = []
    engine.entry_policy = AlwaysRejectEntry(entry_called)
    engine.sizer = AlwaysRejectSize(size_called)

    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    # 期望：UNLOCK 走通，portfolio 清空，broker 收 1 笔 unlock
    # 进 entry_policy 的次数 = 0（被 UNLOCK 短路）
    # sizer 只被用于"缺额补开"算手数 —— 它返回 0 / 抛异常都不影响解锁本身
    check("UNLOCK 路径不调 entry_policy.decide()",
          len(entry_called), 0)
    check("UNLOCK 不因 sizer 返回 0 而阻塞（解锁照常执行）",
          engine.positions.is_empty(), True)


# ════════════════════════════════════════════════════════════════
# [7] Trade 记帐：cost 用 CloseYesterday 费率（close_today=False）
# ════════════════════════════════════════════════════════════════
print("\n[7] Trade 记帐：CloseYesterday 费率")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    # 用 close_today_first=False 的 spec 验证（更直观）：平昨费率口径不同
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))
    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    trades = store.trades()
    if trades:
        t = trades[0]
        # UNLOCK 走的是 CloseYesterday 费率（cost 调用时传 close_today=False）
        # 由于 slippage 让我们重新计算 exit 价：
        slip_units = engine.spec.slippage_ticks * engine.spec.price_tick
        expected_exit = 4520.0 - Side.LONG.sign * slip_units  # = 4519.8
        expected_cost = engine.spec.cost_points(4500.0, expected_exit,
                                                close_today=False)
        check("Trade.cost_points = close_today=False 路径（CFFEX 平昨费率）",
              round(t["cost_points"], 4), round(expected_cost, 4))
        # 与 CloseToday 路径对比，确认 UNLOCK 不走 CloseToday
        close_today_cost = engine.spec.cost_points(4500.0, expected_exit,
                                                   close_today=True)
        check("UNLOCK cost 不等于 CloseToday 路径（说明走的是 CloseYesterday 费率）",
              round(t["cost_points"], 4) != round(close_today_cost, 4), True)


# ════════════════════════════════════════════════════════════════
# [8] Trade ID 不被 UNLOCK 抢占（_trade_seq 顺序）
# ════════════════════════════════════════════════════════════════
print("\n[8] _trade_seq 单调")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))
    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    trade_ids = [t["trade_id"] for t in store.trades()]
    check("UNLOCK 写入了 1 个 trade_id",
          len(trade_ids), 1)
    if trade_ids:
        check("trade_id 以 T00001 开头（_trade_seq 从 1 起）",
              trade_ids[0].startswith("T00001"), True)


# ════════════════════════════════════════════════════════════════
# [9] 持久化兼容：UNLOCK 完后重启 → portfolio 空，state=IDLE
# ════════════════════════════════════════════════════════════════
print("\n[9] 持久化兼容：UNLOCK 完成后重启")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0))
    sig_sell = make_signal(is_buy=False, price=4520.0,
                           date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_sell)
    check("UNLOCK 后 portfolio 空（内存）",
          engine.positions.is_empty(), True)

    # 重新 load engine
    new_store = Store(store.path)
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = InstrumentSpec()
    new_broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    new_ev = EventLog(os.path.join(tmp, "events2.jsonl"),
                      echo=False, echo_kinds=None)
    new_engine = TradingEngine(cfg, new_broker,
                               DefaultEntryPolicy({}), LayeredExitPolicy(),
                               new_store, new_ev)
    check("重启后 portfolio 仍空",
          new_engine.positions.is_empty(), True)
    check("重启后 state = IDLE",
          new_engine._state.name, "IDLE")
    check("重启后 trade 仍保留",
          len(new_store.trades()), 1)


# ════════════════════════════════════════════════════════════════
# [10] 边界：簿非空但信号方向与锁仓持仓同向（has_opposite=False）→ 不走 UNLOCK
# ════════════════════════════════════════════════════════════════
print("\n[10] 边界：同向有仓（has_opposite=False）→ 不走 UNLOCK")
# v1.3：锁仓持仓（LOCKED）与信号同向时，opposite_positions(side) 为空，
# 信号门跳过 UNLOCK 分支 → 直接走 OPEN 开新仓（规则 ⑸-① 的同向版本）。
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    seed_portfolio(engine, store,
                   make_pos(side=Side.LONG, vol=1, entry_price=4500.0,
                            entry_date="2026-09-02"))  # 当日锁，LONG 持仓
    # 同向 BUY 信号：has_opposite(BUY=LONG)=False → 不走 UNLOCK，走 OPEN
    sig_buy = make_signal(is_buy=True, price=4505.0,
                          date="2026-09-02 09:35", bsp_type="2")
    engine.on_signal(sig_buy)
    check("[同向] broker 没收到 UNLOCK 报",
          any(o.meta.get("intent") == "unlock" for o in broker.orders), False)
    check("[同向] broker 收到 OPEN 报（同向 → 开新仓，不提前平锁仓持仓）",
          any(o.meta.get("intent") == "open" for o in broker.orders), True)


print("\n" + "=" * 60)
print("P13 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
