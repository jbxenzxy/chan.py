# -*- coding: utf-8 -*-
"""
P60 关键动作轻提示 + 对账盲区 + 报单终态兜底泵
================================================
背景（2026-09-18 当日 IF 案件）：
  09:41:33 引擎 FOK 报空 2 手 → 5s 窗口内只见 ALIVE → 判「未真正成交」拒单；
  实际柜台 09:41:3x 已成交（快期3 立即可见），otg-simnow 回报链路滞后 25~55s，
  tqsdk 09:42:02 才处理到「下单成功」、09:42:33 才处理到「成交」。
  结果：账本无仓、L1-L3 从头到尾没见过这笔仓，浮盈回吐无人止盈。

三项修复（本测试钉死）：
  [A] SimNow._wait_finished：超时撤单（watchdog 语义不变）后**继续泵到 CTP
      真终态**再返回 —— _finalize 必须按真实终态落账，不许带 ALIVE 快照终判。
  [B] 对账盲区：账本空侧但柜台有量 → severe 告警不接管（2026-09-17 拍板口径
      的「告警」此前对该方向不可达）；账本完全为空时也照常对账。
  [C] 关键动作轻提示（需求 ⑷）：开仓（含止损 1R 点位）/ 平仓（说明止盈还是
      止损）/ 盈利达 1R 进保本 / 盈利达 2R 进移动止盈 / 账单同步 —— 引擎
      notify() 落 state.db kv `toasts`，AppTrader.status 投影，前端 5 秒 toast。

覆盖清单：
  [1] _wait_finished：开跑即终态不撤单 / 撤单确认窗口内终态晚到能等到 /
      永不到终态带 ALIVE 返回（只等 cancel_settle_wait，无 60s 阻塞兜底）+
      otg_latency 时延测量（交易所侧时间戳 vs 本地处理时刻）
  [2] Engine.notify：落 kv `toasts`、有界（尾部 30 条）、写 toast 事件
  [3] 开仓成交 toast：含方向/手数/成交价/止损(1R) 点位
  [4] 盈利达 1R → 保本 toast；达 2R → 移动止盈 toast（阶段跃迁只弹一次）
  [5] 平仓 toast 文案：保本止损 / 移动止盈触发 / 初始止损 / 锁仓离场
  [6] 对账盲区：账本空 + 柜台有量 → position_mismatch severe（restore 与
      on_bar 两路）；无时间宽限（P61 撤销：宁误报不漏报）
  [7] 账单同步 toast：柜台比账本少 → 账本修正 + toast
  [8] on_bar 空账本 + 柜台无量 → 静默返回（无告警、无异常）
  [9] 跨会话清场：上一场次关闭收尾告警（shutdown_result_*/account_frozen）
      新会话启动不重播（内存队列 + kv 投影同步清），现状类告警保留
  [10] 护栏：关键符号必须存在（防回潮）
  [11] 证据门（P64，14:46 事故）：镜像从未见过该仓（会话 max < 账本量）→
      不采纳（不删仓/不补盈亏/不收 run），reconcile_mirror_untrusted 告警
      + reconcile_gate_blocked 留痕；确认后（读到 ≥ 账本量）自动放行；
      mirror_snapshot 读数变化留痕（otg 持仓通道测量）

跑法：python Trading/Test/test_p60_trade_toasts_reconcile_gap.py
"""
from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import time
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

from Trading.Broker.DryRun import DryRunBroker                   # noqa: E402
from Trading.Broker.SimNow import SimNowBroker                   # noqa: E402
from Trading.Config import (DEFAULT_CONFIG, ChannelTimingConfig,  # noqa: E402
                            TradingConfig)
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                      # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES               # noqa: E402
from Trading.Infra.Records import (AccountState, Bar, ExitPlan,   # noqa: E402
                                   Position, Side, Signal)
from Trading.Infra.StateDB import Store                          # noqa: E402
from Trading.Strategy.Entry import EntryPolicy                   # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy              # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
_SYM = "CFFEX.IF2609"

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p60_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


class RealPosBroker(DryRunBroker):
    """DryRun + 可编程 real_position（对账用）。real = {"LONG": n, "SHORT": m}。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._real = {"LONG": 0, "SHORT": 0}

    def real_position(self, side):
        return self._real.get("LONG" if side is Side.LONG else "SHORT", 0)


def build_engine(tmpdir, exit_policy=None, broker=None):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                  echo_kinds=None)
    engine = TradingEngine(cfg, broker, EntryPolicy({}),
                           exit_policy or LayeredExitPolicy(), store, ev)
    return engine, store, broker, ev


def make_signal(is_buy=True, bsp_type="1", date="2026-09-01 09:35",
                price=4550.0, fractal_low=0.0, fractal_high=0.0):
    return Signal(key=Signal.make_key(date, bsp_type, is_buy), symbol=_SYM,
                  freq="5m", date=date, timestamp=0, bsp_type=bsp_type,
                  is_buy=is_buy, price=price, high=price + 2.0,
                  low=price - 2.0, fractal_low=fractal_low,
                  fractal_high=fractal_high)


def make_bar(ts, o=4550.0, h=4552.0, l=4548.0, c=4551.0):
    return Bar(timestamp=ts, date="2026-09-01 09:40", open=o, high=h,
               low=l, close=c, vol=1)


def toasts_of(store):
    return store.get_json("toasts") or []


def last_toast_msg(store):
    ts = toasts_of(store)
    return ts[-1]["msg"] if ts else ""


def ev_kinds(path):
    kinds = []
    with io.open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kinds.append(e.get("kind"))
    return kinds


print("P60/P61：关键动作轻提示 + 对账盲区 + 时延测量与空闲泵")

# ════════════════════════════════════════════════════════════════
# [1] SimNow._wait_finished：watchdog 撤单 + 撤单确认窗口；otg_latency 测量
# ════════════════════════════════════════════════════════════════
print("\n[1] SimNow._wait_finished 撤单确认窗口 + otg_latency 时延测量")


class _RawOrder:
    def __init__(self, oid, status="ALIVE", volume_left=2):
        self.order_id = oid
        self.status = status
        self.volume_left = volume_left
        self.trade_price = float("nan")
        self.trade_records = {}
        self.last_msg = ""


class _FlipApi:
    """最小 tqsdk 面：wait_update 立即返回；cancel 可翻转订单状态。"""

    def __init__(self, order, flip_on_cancel=False):
        self._order = order
        self.flip = flip_on_cancel
        self.cancelled = []

    def wait_update(self, deadline=None):
        return True

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if self.flip:
            self._order.status = "FINISHED"


def _simnow_broker(cancel_wait=0.05):
    p = dict(DEFAULT_CONFIG["broker_params"])
    p["channel"] = ChannelTimingConfig(
        cancel_settle_wait=cancel_wait).model_dump()
    return SimNowBroker(Instrument(None, _IF), params=p)


with tmp_dir() as _t:  # 占位：保持 with 风格一致（broker 不落盘）
    pass

# 1a 开跑即终态 → 立即返回，不撤单
b = _simnow_broker()
order = _RawOrder("r-a", status="FINISHED")
b._api = _FlipApi(order)
b._wait_finished(order, timeout_s=0.2)
check("[1a] 终态在手 → 不触发撤单", b._api.cancelled, [])

# 1b 撤单确认窗口内终态已翻转 → 返回时已是真终态（_finalize 据此判成交）
b = _simnow_broker()
order = _RawOrder("r-b")            # ALIVE，撤单时翻成 FINISHED（迟到回报已到）
b._api = _FlipApi(order, flip_on_cancel=True)
b._wait_finished(order, timeout_s=0.15)
check("[1b] 撤单恰一次", len(b._api.cancelled), 1)
check("[1b] 返回时已是 FINISHED（真终态）", order.status, "FINISHED")

# 1c 永不到终态 → 只等 cancel_settle_wait 即返回，仍带 ALIVE（交对账兜底+
# otg_latency 留时延证据）；无 60s 阻塞兜底（P61 撤销）
b = _simnow_broker(cancel_wait=0.05)
order = _RawOrder("r-c")            # 永远 ALIVE
b._api = _FlipApi(order)
t0 = time.time()
b._wait_finished(order, timeout_s=0.1)
elapsed = time.time() - t0
check("[1c] 撤单恰一次", len(b._api.cancelled), 1)
check("[1c] 仍带 ALIVE 返回（终态确实没来）", order.status, "ALIVE")
check("[1c] 总耗时被 cancel_settle_wait 上界约束（<5s）", elapsed < 5.0, True)

# 1d otg_latency 时延测量 helper：交易所侧时间戳 vs 本地处理时刻
from types import SimpleNamespace as _NS
_now = time.time()
fake = _NS(order_id="r-d", insert_date_time=(_now - 3.0) * 1e9,
           trade_records={"t1": _NS(trade_date_time=(_now - 1.0) * 1e9)})
f = SimNowBroker._otg_latency_fields(fake, _now - 5.0, _now - 4.0)
check("[1d] insert→终判 ≈3s", abs(f["insert_lag_s"] - 3.0) < 0.2, True)
check("[1d] 成交→终判 ≈1s", abs(f["trade_lag_s"] - 1.0) < 0.2, True)
check("[1d] submit→终判 ≈5s", abs(f["since_submit_s"] - 5.0) < 0.2, True)
check("[1d] watchdog→终判 ≈4s", abs(f["since_watchdog_s"] - 4.0) < 0.2, True)
fake2 = _NS(order_id="r-e", insert_date_time=None, trade_records={})
f2 = SimNowBroker._otg_latency_fields(fake2, time.time(), None)
check("[1d] 无时间戳 → None 不崩",
      f2["insert_lag_s"] is None and f2["trade_lag_s"] is None
      and f2["since_watchdog_s"] is None, True)

# ════════════════════════════════════════════════════════════════
# [2] Engine.notify：落 kv、有界、写事件
# ════════════════════════════════════════════════════════════════
print("\n[2] Engine.notify 落 kv toasts（有界 + 事件留痕）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    for i in range(35):
        engine.notify("提示{}".format(i), code="c{}".format(i % 3))
    check("[2a] kv 键名 toasts", os.path.exists(
        os.path.join(tmp, "state.db")), True)
    items = toasts_of(store)
    check("[2b] 队列有界=30（尾部保留）", len(items), 30)
    check("[2c] 保留的是最新 35−30 条", items[0]["msg"], "提示5")
    check("[2d] 每条含 ts", all("ts" in t for t in items), True)
    ev.flush()
    check("[2e] 写了 toast 事件", "toast" in ev_kinds(
        os.path.join(tmp, "events.jsonl")), True)

# ════════════════════════════════════════════════════════════════
# [3] 开仓成交 toast：含止损价 + 1R 量值（需求 ⑷(1)、⑶）
# ════════════════════════════════════════════════════════════════
print("\n[3] 开仓成交 toast（含止损价 + 1R 量值）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    # 分型低点 4500 → A = 4550−4500 = 50 = R（bar 太少 ATR 未就绪 → B=0）
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    check("[3a] 已开仓", len(engine.positions.positions), 1)
    stop = engine._run_plan.stop_price
    r_plan = engine._run_plan.params["R"]
    msg = last_toast_msg(store)
    check("[3b] toast 报开仓成交", "开仓成交" in msg, True)
    # 文案曾是「止损(1R) = 4010」——把**距离**标成**价格**。2026-09-22 改为
    # 「止损 = <价>（距入场 1R = <R> 点）」，本组断言钉住新措辞 + 1R 量值。
    check("[3c] toast 不再写「止损(1R)」旧措辞", "止损(1R)" in msg, False)
    check("[3d] toast 含「止损 = 」与「距入场 1R = 」",
          ("止损 = " in msg and "距入场 1R = " in msg), True)
    check("[3e] toast 止损点位 == 计划止损 {:g}".format(stop),
          "{:g}".format(stop) in msg, True)
    check("[3f] 1R 量值 == 计划 R {:g} 点（带品种报价单位）".format(r_plan),
          "距入场 1R = {:g} 点".format(r_plan) in msg, True)

# ════════════════════════════════════════════════════════════════
# [4] 盈利达 1R → 保本 toast；达 2R → 移动止盈 toast（需求 ⑷(3)(4)）
# ════════════════════════════════════════════════════════════════
print("\n[4] 保本 / 移动止盈阶段 toast（阶段跃迁各弹一次）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    r_mult = engine.exit_policy.win_loss_ratio
    be_trig = engine.exit_policy.breakeven_trigger_r
    entry = engine._run_anchor
    r = engine._run_plan.params["R"]
    # 冲到 ≥ 1R（未到 2R）
    h1 = entry + be_trig * r + 10.0
    engine.on_bar(make_bar(2000, h=h1, l=4548.0, c=h1 - 5.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[4a] 保本 toast 出现", any("保本" in m for m in msgs), True)
    check("[4b] 移动止盈 toast 尚未出现",
          any("移动止盈" in m and "平仓" not in m for m in msgs), False)
    # 冲到 ≥ 2R（win_loss_ratio）
    h2 = entry + r_mult * r + 10.0
    engine.on_bar(make_bar(3000, h=h2, l=h1 - 5.0, c=h2 - 5.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[4c] 移动止盈 toast 出现",
          any("移动止盈" in m for m in msgs), True)
    n_be = sum(1 for m in msgs if "保本" in m)
    n_tr = sum(1 for m in msgs if "移动止盈" in m)
    check("[4d] 保本 toast 只弹一次（阶段跃迁语义，不逐 bar 刷）", n_be, 1)
    check("[4e] 移动止盈 toast 只弹一次", n_tr, 1)

# ════════════════════════════════════════════════════════════════
# [5] 平仓 toast 文案（需求 ⑷(2)）：保本止损 / 移动止盈触发 / 初始止损
# ════════════════════════════════════════════════════════════════
print("\n[5] 平仓 toast 说明止盈还是止损")
# 说明：本测试里所有持仓都是**当日仓** → 引擎的今仓离场 = 反向开仓锁仓
# （转移④），toast 前缀是「锁仓离场」；「平仓成交」前缀对应跨日仓 CLOSE
# （转移⑤），在 [5e] 用 _notify_close 单测覆盖（前缀路由：intent 分支）。


def _drive_to_breakeven(engine):
    """开多（fractal_low=4500）→ 冲到 ≥1R 触发保本（阶段=breakeven）。"""
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    r_mult = engine.exit_policy.win_loss_ratio
    entry = engine._run_anchor
    r = engine._run_plan.params["R"]
    stop = engine._run_plan.stop_price
    engine.on_bar(make_bar(2000, h=entry + r_mult * r * 0.75 + 5.0,
                           l=4548.0, c=entry + r_mult * r * 0.5))
    return entry, r, stop, r_mult


with tmp_dir() as tmp:
    # 5a 保本阶段触发止损 → 文案「锁仓离场·保本止损」（今仓 → 锁仓）
    engine, store, broker, ev = build_engine(tmp)
    entry, r, stop, r_mult = _drive_to_breakeven(engine)
    be_stop = engine._run_plan.stop_price
    engine.on_bar(make_bar(3000, h=be_stop + 2.0, l=be_stop - 5.0,
                           c=be_stop - 2.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[5a] 保本止损文案出现",
          any("锁仓离场" in m and "保本止损" in m for m in msgs), True)
    #  2026-09-22：文案与落盘记录同源（引擎 _notify_close 只按 reason 路由），故同时钉
    #  落盘的 reason —— 它记的是"哪一层保护价被跌破"，与这笔实际赚没赚无关。
    _tr5a = store.trades()
    check("[5a-2] 落盘 Trade.reason = breakeven（保本层保护价被跌破）",
          _tr5a[0]["reason"] if _tr5a else None, "breakeven")

with tmp_dir() as tmp:
    # 5b 移动止盈阶段触发止损 → 文案「锁仓离场·移动止盈」
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    r_mult = engine.exit_policy.win_loss_ratio
    entry = engine._run_anchor
    r = engine._run_plan.params["R"]
    engine.on_bar(make_bar(2000, h=entry + r_mult * r + 10.0,
                           l=4548.0, c=entry + r_mult * r))
    stop = engine._run_plan.stop_price
    engine.on_bar(make_bar(3000, h=entry + r_mult * r + 2.0,
                           l=stop - 5.0, c=stop - 2.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[5b] 移动止盈触发文案出现",
          any("锁仓离场" in m and "移动止盈" in m for m in msgs), True)
    _tr5b = store.trades()
    check("[5b-2] 落盘 Trade.reason = trailing（跟踪层保护价被跌破）",
          _tr5b[0]["reason"] if _tr5b else None, "trailing")

with tmp_dir() as tmp:
    # 5d 今仓止损离场 = 反向开仓锁仓 → 文案「锁仓离场」
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    stop = engine._run_plan.stop_price
    engine.on_bar(make_bar(2000, h=4552.0, l=stop - 5.0, c=stop - 2.0))
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[5d] 锁仓离场文案出现",
          any("锁仓离场" in m and "止损" in m for m in msgs), True)
    #  未进 L3 的初始止损段：reason 仍是 "sl"（细分没有把"止损"这个默认值改掉）。
    _tr5d = store.trades()
    check("[5d-2] 落盘 Trade.reason = sl（初始止损，未进 L3）",
          _tr5d[0]["reason"] if _tr5d else None, "sl")

with tmp_dir() as tmp:
    # 5e CLOSE intent（跨日仓转移⑤）→ 前缀「平仓成交」（_notify_close 单测）
    from types import SimpleNamespace
    from Trading.Infra.Records import OrderIntent
    engine, store, broker, ev = build_engine(tmp)
    act = SimpleNamespace(intent=OrderIntent.CLOSE)
    o = SimpleNamespace(side=Side.SHORT, volume=2, filled_price=4525.0)
    engine._notify_close(o, act, "trailing")
    check("[5e] CLOSE intent 前缀为 平仓成交·移动止盈",
          "平仓成交·移动止盈" in last_toast_msg(store), True)

# ════════════════════════════════════════════════════════════════
# [6] 对账盲区：账本空 + 柜台有量 → severe 告警（restore / on_bar / 宽限）
# ════════════════════════════════════════════════════════════════
print("\n[6] 对账盲区（账本空侧但柜台有量）")


def _alerts(engine):
    return engine._alerts


with tmp_dir() as tmp:
    # 6a restore 路径：账本空 + 柜台空单 2 手 → 告警不接管
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    broker._real = {"LONG": 0, "SHORT": 2}
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine._reconcile_positions(source="restore")
    al = _alerts(engine)
    check("[6a] position_mismatch 告警产生", len(al), 1)
    check("[6a] 级别 severe", al[0]["level"] if al else None, "severe")
    check("[6a] 账本未被接管（仍空仓）",
          len(engine.positions.positions), 0)
    ev.flush()
    kinds = ev_kinds(os.path.join(tmp, "events.jsonl"))
    check("[6a] position_mismatch 事件留痕", "position_mismatch" in kinds, True)

with tmp_dir() as tmp:
    # 6b 无时间宽限（P61 撤销 120s 宽限）：on_bar 路径账本空 + 柜台有量
    #    → 立即告警（宁可偶尔误报、不可静默漏报）
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    broker._real = {"LONG": 0, "SHORT": 2}
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine._reconcile_positions(source="on_bar")
    check("[6b] 无宽限：立刻对账也告警", len(_alerts(engine)), 1)

with tmp_dir() as tmp:
    # 6c 账本空 + 柜台无量 → 静默（不告警、不落任何状态写入）
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    check("[6c] on_bar 空账本静默返回", len(_alerts(engine)), 0)
    check("[6c] 无异常且簿仍空", len(engine.positions.positions), 0)

# ════════════════════════════════════════════════════════════════
# [7] 账单同步 toast（需求 ⑷(5)）：柜台比账本少 → 修正 + toast
# ════════════════════════════════════════════════════════════════
print("\n[7] 账单同步 toast")
with tmp_dir() as tmp:
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    check("[7a] 开仓成功（账本 1 笔）",
          len(engine.positions.positions), 1)
    # 证据门前提（P64）：镜像先「见过」这笔仓（读到 ≥ 账本量），它后来说
    # 「仓没了」才可信 —— 快期3 手工平仓前，镜像本来就看得到这笔仓。
    broker._real = {"LONG": 2, "SHORT": 0}   # 镜像确认成交（同步正常）
    engine.on_bar(make_bar(2000))            # 越过「入场当根 K 线跳过」守卫
    broker._real = {"LONG": 0, "SHORT": 0}   # 之后柜台被（手工）平光
    engine._reconcile_positions(source="on_bar")
    check("[7b] 账本已按柜台修正（簿空）",
          len(engine.positions.positions), 0)
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[7c] 账单已同步 toast 出现",
          any("账单已同步" in m for m in msgs), True)

# ════════════════════════════════════════════════════════════════
# [9] 跨会话清场：上一场次关闭收尾告警不重播（2026-09-18 用户实录：
#     开启自动下单却弹"自动下单已关闭"——上一场次的收尾结论跑错时间送达）
# ════════════════════════════════════════════════════════════════
print("\n[9] 跨会话清场：关闭收尾告警不重播")

with tmp_dir() as tmp:
    seed = Store(os.path.join(tmp, "state.db"))
    old = time.time() - 3600.0
    seed.set_json("alerts", [
        {"level": "warn", "code": "shutdown_result_flat",
         "msg": "自动下单已关闭 —— 已清仓，无残留持仓", "ts": old,
         "last_ts": old, "n": 1},
        {"level": "warn", "code": "account_frozen",
         "msg": "自动下单已关闭，账户停在锁仓态", "ts": old,
         "last_ts": old, "n": 1},
        {"level": "severe", "code": "position_mismatch",
         "msg": "柜台有量、账本无仓", "ts": old, "last_ts": old, "n": 2},
    ])
    seed.close()
    engine, store, broker, ev = build_engine(tmp)
    codes = sorted(str(a.get("code")) for a in engine._alerts)
    check("[9a] shutdown_result_flat 已清场",
          "shutdown_result_flat" in codes, False)
    check("[9b] account_frozen 已清场",
          "account_frozen" in codes, False)
    check("[9c] 现状类告警保留（position_mismatch）",
          "position_mismatch" in codes, True)
    # API 侧直接读 kv 投影，光清内存没用 —— kv 必须同步清
    left = [str(a.get("code")) for a in (store.get_json("alerts") or [])]
    check("[9d] kv 投影同步清场（API 侧不再下发）",
          "shutdown_result_flat" in left or "account_frozen" in left, False)
    check("[9e] 清场留痕（alert_session_prune 事件）",
          "alert_session_prune" in ev_kinds(os.path.join(tmp, "events.jsonl")),
          True)

# ── [9g/9h] API 侧清场：_reset_engine_switch 拉起前同步清（关竞态窗口）──
#   状态轮询直接读 kv 投影；若只靠子进程起来再清，开启后头几秒旧告警会
#   抢先弹出（2026-09-18 用户实录：开启自动下单却看到"已关闭"消息）。
from App.AppTrader import AppTrader as _AppTrader             # noqa: E402

with tmp_dir() as tmp:
    seed = Store(os.path.join(tmp, "state.db"))
    old = time.time() - 3600.0
    seed.set_json("alerts", [
        {"level": "warn", "code": "shutdown_result_flat",
         "msg": "自动下单已关闭 —— 已清仓", "ts": old, "last_ts": old, "n": 1},
        {"level": "severe", "code": "position_mismatch",
         "msg": "柜台有量、账本无仓", "ts": old, "last_ts": old, "n": 2},
    ])
    seed.close()
    _AppTrader._reset_engine_switch(tmp)
    after = Store(os.path.join(tmp, "state.db"))
    left = [str(a.get("code")) for a in (after.get_json("alerts") or [])]
    check("[9g] API 侧拉起前清场：关闭收尾告警已清",
          "shutdown_result_flat" in left, False)
    check("[9h] API 侧清场不误伤现状类告警",
          "position_mismatch" in left, True)
    check("[9i] 开关照常恢复 True",
          bool(after.get_json("auto_order_enabled")), True)
    after.close()
def _src_early(rel):
    """[9] 段专用源码读取（_src 在 [10] 段才定义，此处不能引用）。"""
    return io.open(os.path.join(os.path.dirname(_TG_ROOT),
                                rel.replace("/", os.sep)),
                   "r", encoding="utf-8").read()


check("[9j] 清场规则唯一存放（AppTrader 引用 Records.is_closure_alert）",
      "is_closure_alert" in _src_early("App/AppTrader.py")
      and "is_closure_alert" in _src_early("Trading/Infra/Records.py"), True)

# ════════════════════════════════════════════════════════════════
# [11] 证据门（P64，2026-09-18 14:46 事故）：镜像从未见过该仓 → 不采纳
# ════════════════════════════════════════════════════════════════
print("\n[11] 证据门：镜像未确认过的仓不许「被手工平仓」")
with tmp_dir() as tmp:
    # 事故复现：otg 持仓通道整场未同步 → 镜像恒 0（连开仓确认读数都没有）
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    check("[11a] 开仓成功（账本 1 笔）",
          len(engine.positions.positions), 1)
    broker._real = {"LONG": 0, "SHORT": 0}   # 镜像从未确认过这笔仓
    engine.on_bar(make_bar(2000))            # 越过「入场当根 K 线跳过」守卫
    engine._reconcile_positions(source="on_bar")
    check("[11b] 证据门拦截：账本不删",
          len(engine.positions.positions), 1)
    codes = [str(a.get("code")) for a in _alerts(engine)]
    check("[11c] reconcile_mirror_untrusted 告警产生",
          "reconcile_mirror_untrusted" in codes, True)
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[11d] 无「账单已同步」toast（不补虚构盈亏）",
          any("账单已同步" in m for m in msgs), False)
    check("[11e] run 未被收掉（L1-L3 锚保留）",
          engine._run_plan is not None, True)
    ev.flush()
    kinds = ev_kinds(os.path.join(tmp, "events.jsonl"))
    check("[11f] reconcile_gate_blocked 留痕",
          "reconcile_gate_blocked" in kinds, True)
    check("[11g] mirror_snapshot 测量留痕",
          "mirror_snapshot" in kinds, True)

with tmp_dir() as tmp:
    # 门自动放行：镜像同步出真实持仓（读到 ≥ 账本量）→ 后续「仓没了」照常采纳
    broker = RealPosBroker(Instrument(InstrumentConfig(trade_symbol=_SYM), _IF),
                           {"sim_equity": 1_000_000.0})
    engine, store, b2, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    engine.on_signal(make_signal(is_buy=True, bsp_type="1",
                                 fractal_low=4500.0))
    broker._real = {"LONG": 2, "SHORT": 0}
    engine.on_bar(make_bar(2000))            # 镜像确认（读到 2）
    broker._real = {"LONG": 0, "SHORT": 0}
    engine._reconcile_positions(source="on_bar")
    check("[11h] 确认后采纳放行（簿空）",
          len(engine.positions.positions), 0)
    msgs = [t["msg"] for t in toasts_of(store)]
    check("[11i] 账单已同步 toast 出现",
          any("账单已同步" in m for m in msgs), True)

# ════════════════════════════════════════════════════════════════
# [10] 护栏：关键符号存在（防回潮）
# ════════════════════════════════════════════════════════════════
print("\n[10] 护栏（关键符号防回潮）")


def _src(rel):
    """rel 相对**仓库根**（_TG_ROOT 指向 Trading 目录本身）。"""
    return io.open(os.path.join(os.path.dirname(_TG_ROOT),
                                rel.replace("/", os.sep)),
                   "r", encoding="utf-8").read()


check("[8a] 60s 阻塞兜底已撤（SimNow 无 terminal_settle_wait）",
      "terminal_settle_wait" not in _src("Trading/Broker/SimNow.py"), True)
check("[8b] 60s 阻塞兜底已撤（Config 无 terminal_settle_wait）",
      "terminal_settle_wait" not in _src("Trading/Config.py"), True)
check("[8c] 引擎 notify 存在",
      "def notify(" in _src("Trading/Engine/Engine.py"), True)
check("[8d] 引擎开仓 toast 挂在成交落账路径",
      "_notify_open(o)" in _src("Trading/Engine/Engine.py"), True)
check("[8e] 策略层阶段标记存在",
      '"_phase"' in _src("Trading/Strategy/Exit.py"), True)
check("[8f] 对账空侧检查存在",
      "_reconcile_empty_side" in _src("Trading/Engine/Reconcile.py"), True)
check("[8g] on_bar 无条件对账（盲区补）",
      "self._reconcile_positions()" in _src("Trading/Engine/Engine.py"), True)
check("[8h] AppTrader.status 投影 toasts",
      '"toasts"' in _src("App/AppTrader.py"), True)
check("[8i] 前端轻提示处理存在",
      "handleAutoOrderToasts" in _src("Frontend/app.js"), True)
check("[8j] 前端 toast 时长可指定（缺省 5 秒）",
      "ms || TOAST_MS" in _src("Frontend/app.js"), True)
check("[8k] 分层：Reconcile 不 import 引擎主体",
      "from ..Engine" not in _src("Trading/Engine/Reconcile.py")
      and "import Engine" not in _src("Trading/Engine/Reconcile.py"), True)
check("[8l] 时延测量 helper 存在（otg_latency）",
      "_otg_latency_fields" in _src("Trading/Broker/SimNow.py"), True)
check("[8m] 空闲泵挂点存在（SSE on_idle + main.py 接线）",
      "on_idle" in _src("Trading/Source/SSE.py")
      and "on_idle" in _src("Trading/main.py"), True)
check("[8n] 时间宽限已撤（Reconcile 无 _EMPTY_SIDE_GRACE_S / Engine 无 _last_fill_at）",
      "_EMPTY_SIDE_GRACE_S" not in _src("Trading/Engine/Reconcile.py")
      and "_last_fill_at" not in _src("Trading/Engine/Engine.py"), True)
check("[10o] 跨会话清场存在（_load_alerts 内 alert_session_prune）",
      "alert_session_prune" in _src("Trading/Engine/Engine.py"), True)
check("[10p] 证据门符号存在（防回潮）",
      "reconcile_mirror_untrusted" in _src("Trading/Engine/Reconcile.py")
      and "_mirror_note" in _src("Trading/Engine/Reconcile.py"), True)
check("[10q] 对账告警文案不再单独称「柜台」（改本地柜台镜像）",
      "对账发现不一致：柜台 " not in _src("Trading/Engine/Reconcile.py"), True)
check("[10r] SimNow 登录持仓镜像初读存在（otg 通道测量）",
      "持仓镜像初读" in _src("Trading/Broker/SimNow.py"), True)

print("\n==== P60：{} passed, {} failed ====".format(_PASS, _FAIL))
if _FAIL:
    sys.exit(1)
