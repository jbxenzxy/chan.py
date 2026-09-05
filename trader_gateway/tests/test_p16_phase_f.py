# -*- coding: utf-8 -*-
"""
Phase F 卡单复核（2026-09-05）
=============================
背景
    Phase F 解决两类幽灵/错配风险：
      · F1：UNLOCK 卡单 —— broker.submit 返回 filled 但 CTP 通道异常时真实未成交，
        引擎若直接信 filled 删 portfolio，次日同向信号进来时 has_opposite=False
        走正常开仓路径 → 真实账户持仓仍在 → 错配。
        解决：报单成功后记 _unlock_in_flight，5 bars 后调 broker.trade_confirmed 复核：
          · True  → 真成交（CTP 已收到回报）→ 清 in-flight
          · False → 调 _reconcile_positions 兜底（按真实持仓修正）
      · F2：_restore 末尾首拉真实持仓 —— 防止"本地 store 有持仓但真实账户已平"
        造成重启后第一根 bar 误判。

硬性要求（本测试锁死）
    ① broker.trade_confirmed(intent, signal_key) 接口：
        · base 默认 True（兜底）
        · dry_run 重写 True（同步撮合，submit 返回 filled 即确认）
        · 自定义 broker 可重写返回 False
    ② engine._check_unlock_stuck(bar)：
        · _unlock_in_flight 空 → skip
        · bars_elapsed < _unlock_stuck_bars → skip（不调 broker）
        · bars_elapsed = 5 + trade_confirmed=True → 清 in-flight + 写 unlock_confirmed
        · bars_elapsed = 5 + trade_confirmed=False + 真实持仓 0
              → 写 unlock_stuck_recovered
        · bars_elapsed = 5 + trade_confirmed=False + 真实持仓 > 0
              → 写 unlock_stuck_confirmed（卡单确认）
        · broker.trade_confirmed 抛异常 → 保守走 reconcile
        · UNLOCK 拒单不设 in-flight
    ③ engine._restore 末尾首拉真实持仓（source="restore"）：
        · 无持仓 → 不报错
        · broker 无 real_position → skip
        · real_vol == engine_vol → skip
        · real_vol < engine_vol → FIFO 部分平（trade reason=reconcile_external_partial）
        · real_vol == 0 → 全平
        · real_vol > engine_vol → 告警不接管（写 position_mismatch，source="restore"）
        · broker.real_position 抛异常 → 写 restore_reconcile_failed，不阻断启动
        · source="restore" 事件带 source="restore" 字段
        · source="restore" 不拦截 bars_held < 1（与 on_bar 路径差异）
    ④ 兼容现有行为：dry_run broker 默认 trade_confirmed=True → 不触发 reconcile
       （保证现有 P13 UNLOCK 路径零行为变化）
    ⑤ 集成：F2 _restore 末尾清残留 + F1 UNLOCK 卡单 5 bars 后 reconcile 兜底

不需要真实 tqsdk / 网络；纯单测 + ControlledTradeConfirmedBroker / RealPositionBroker mock。
跑法：python tests/test_p16_phase_f.py
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
    d = tempfile.mkdtemp(prefix="tg_p16_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg.brokers.base import Broker, OrderIntent, register_broker  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.position_book import PositionBook  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EngineState, EntryMode, ExitPlan, Position, Side,
)


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


def check_truthy(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_in(name, got, container):
    """检查 got 是否在 container 中（dict/list/set/tuple 任意）。"""
    global _PASS, _FAIL
    ok = got in container
    print(("✓" if ok else "✗") + " " + name +
          ("  -> got={!r} not in container".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


# ════════════════════════════════════════════════════════════════
# Mock Broker：可注入 trade_confirmed 返回值 + real_position 模拟
# ════════════════════════════════════════════════════════════════
class ControlledTradeConfirmedBroker(DryRunBroker):
    """DryRunBroker 子类：可注入 trade_confirmed 返回值 + 可选 raise。

    参数：
      · trade_confirmed_value: True / False，控制 trade_confirmed 返回
      · raise_on_trade_confirmed: 若 True，trade_confirmed 抛 RuntimeError
      · real_longs / real_shorts: 模拟外部真实持仓（用于 F2 集成）
    """
    name = "controlled_tc"

    def __init__(self, spec, params=None, *,
                 trade_confirmed_value=True,
                 raise_on_trade_confirmed=False,
                 real_longs=None, real_shorts=None):
        super().__init__(spec, params)
        self._tc_value = trade_confirmed_value
        self._tc_raise = raise_on_trade_confirmed
        self._real_longs = real_longs
        self._real_shorts = real_shorts
        self.tc_calls: list = []   # 记录 (intent, signal_key) 调用对

    def trade_confirmed(self, intent, signal_key=""):
        self.tc_calls.append((intent.value, signal_key))
        if self._tc_raise:
            raise RuntimeError("simulated trade_confirmed failure")
        return self._tc_value

    def real_position(self, side):
        if side is Side.LONG:
            return self._real_longs
        if side is Side.SHORT:
            return self._real_shorts
        return None


class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类：所有 submit 都拒单（用于测试 UNLOCK 拒单不设 in-flight）。"""
    name = "reject_dry"

    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        from tg.types import Order, now_cn
        o = Order(
            order_id="{}-REJ".format(self.name), signal_key=signal_key,
            symbol=self.spec.trade_symbol, side=side,
            action="close", volume=int(volume), price=float(ref_price),
            req_price=float(ref_price), filled_price=None, status="rejected",
            created_at=now_cn(), broker=self.name, note=note,
            meta={"reject_reason": "simulated reject", "intent": str(intent)},
        )
        self.orders.append(o)
        return o


def make_engine(tmpdir, *, max_open_positions=1, batch_open=1,
                cfg_risk_max_volume=10, tp_points=5.0, stop_points=10.0,
                close_before_session_end=False, broker=None):
    """构造引擎：默认 batch=1 + sizing 关闭（fixed_volume=1）。"""
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = max_open_positions
    cfg.risk.max_volume = cfg_risk_max_volume
    cfg.risk.enforce_session = False
    cfg.risk.close_before_session_end = close_before_session_end
    cfg.sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    cfg.sizing["enabled"] = False
    cfg.sizing["fixed_volume"] = 1
    cfg.sizing["batch_open"] = batch_open

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": tp_points,
                               "stop_loss_points": stop_points})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    eng = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return eng


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000):
    """构造 Bar。默认 ts=5000（大于所有 make_position 的 entry_bar_ts=4100+），避免 _settle_positions 跳过。"""
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST",
                  tp_offset=5.0, sl_offset=10.0):
    """构造手动 Position（不走 _open_positions），用于直接构造 portfolio 状态。"""
    from tg.types import now_cn
    if side is Side.LONG:
        tp = entry_price + tp_offset
        stop = entry_price - sl_offset
    else:
        tp = entry_price - tp_offset
        stop = entry_price + sl_offset
    return Position(
        symbol="KQ.m@CFFEX.IF", side=side, volume=vol,
        entry_price=entry_price, entry_at=now_cn(),
        entry_bar_ts=4000 + entry_bar_seq * 100,
        entry_bar_seq=entry_bar_seq,
        signal_key=signal_key, open_order_id="manual",
        exit_plan=ExitPlan(name="tp_sl", stop_price=stop, tp_price=tp,
                            params={"take_profit_points": tp_offset,
                                    "stop_loss_points": sl_offset}),
        entry_mode=EntryMode.OPEN_FIRST)


def make_signal(key, side, price=4550.0, date="2026-09-01 09:35", bsp_type="buy"):
    """构造手动 Signal（不走 chan.py 上游）。"""
    from tg.types import Signal
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m",
                  timestamp=5000, date=date, bsp_type=bsp_type,
                  is_buy=(side is Side.LONG), price=price,
                  high=price + 5.0, low=price - 5.0)


def read_events(eng, kinds=None, tail_n=200):
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-tail_n:]:
            try:
                rec = json.loads(line)
                if kinds is None or rec.get("kind") in kinds:
                    out.append(rec)
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def find_event(events, kind, **must_match):
    """在 events 列表里找第一条 kind 匹配且所有 must_match 字段相等的记录。"""
    for ev in events:
        if ev.get("kind") != kind:
            continue
        ok = True
        for k, v in must_match.items():
            if ev.get(k) != v:
                ok = False
                break
        if ok:
            return ev
    return None


# ════════════════════════════════════════════════════════════════
# [1] broker.trade_confirmed 接口
# ════════════════════════════════════════════════════════════════
print("\n[1] broker.trade_confirmed 接口")

# 1.1 base.Broker 默认返回 True
class BareBroker(Broker):
    name = "bare"
    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        raise NotImplementedError

base_default = Broker.trade_confirmed(None, "")
check("1.1 base.Broker 默认 trade_confirmed=True", base_default, True)

# 1.2 dry_run.DryRunBroker 重写返回 True
dry_b = DryRunBroker(InstrumentSpec())
check("1.2 dry_run.DryRunBroker trade_confirmed=True",
      dry_b.trade_confirmed(OrderIntent.UNLOCK, "x"), True)

# 1.3 自定义 broker 可重写返回 False
ctl = ControlledTradeConfirmedBroker(InstrumentSpec(), trade_confirmed_value=False)
check("1.3 自定义 broker trade_confirmed=False",
      ctl.trade_confirmed(OrderIntent.UNLOCK, "x"), False)

# 1.4 自定义 broker 可抛异常
ctl_raise = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           raise_on_trade_confirmed=True)
raised = False
try:
    ctl_raise.trade_confirmed(OrderIntent.UNLOCK, "x")
except RuntimeError:
    raised = True
check_truthy("1.4 自定义 broker trade_confirmed 抛异常被捕获", raised)

# 1.5 intent / signal_key 参数被接收并记录
ctl.tc_calls = []
ctl.trade_confirmed(OrderIntent.UNLOCK, "sig-key-1")
ctl.trade_confirmed(OrderIntent.OPEN, "sig-key-2")
check("1.5a 记录 intent=UNLOCK 调用",
      (ctl.tc_calls[0][0], ctl.tc_calls[0][1]), ("unlock", "sig-key-1"))
check("1.5b 记录 intent=OPEN 调用",
      (ctl.tc_calls[1][0], ctl.tc_calls[1][1]), ("open", "sig-key-2"))


# ════════════════════════════════════════════════════════════════
# [2] F1 _check_unlock_stuck 直接调用
# ════════════════════════════════════════════════════════════════
print("\n[2] F1 _check_unlock_stuck 直接调用")

# 2.1 _unlock_in_flight 空 → skip
with tmp_dir() as td:
    eng = make_engine(td)
    eng.last_bar = make_bar()
    eng.bars_seen = 10
    before = eng._unlock_in_flight
    eng._check_unlock_stuck(eng.last_bar)
    check("2.1 in-flight 空 → 不变", eng._unlock_in_flight, before)

# 2.2 bars_elapsed < 5 → skip（不调 broker）
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           trade_confirmed_value=False)
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 5  # 报单 bar_seq = 1，bars_elapsed = 4 < 5
    eng._unlock_in_flight = {
        "signal_key": "sig-2-2", "target_signal_key": "tgt-2-2",
        "target_side": "SHORT", "target_snapshot": None,
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._check_unlock_stuck(eng.last_bar)
    check("2.2 bars_elapsed<5 → in-flight 保留", eng._unlock_in_flight is not None, True)
    check("2.2b 不调 broker.trade_confirmed", len(ctl_b.tc_calls), 0)

# 2.3 bars_elapsed = 5 + trade_confirmed=True → 清 in-flight + 写 unlock_confirmed
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           trade_confirmed_value=True)
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 6  # bars_elapsed = 5
    eng._unlock_in_flight = {
        "signal_key": "sig-2-3", "target_signal_key": "tgt-2-3",
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._check_unlock_stuck(eng.last_bar)
    check("2.3a trade_confirmed=True → 清 in-flight", eng._unlock_in_flight, None)
    check("2.3b 调了 broker.trade_confirmed", len(ctl_b.tc_calls), 1)
    evs = read_events(eng, kinds={"unlock_confirmed"})
    check("2.3c 写 unlock_confirmed 事件", len(evs) >= 1, True)
    if evs:
        check("2.3d 事件 signal_key 正确", evs[0].get("signal_key"), "sig-2-3")
        check("2.3e 事件 bars_elapsed 正确", evs[0].get("bars_elapsed"), 5)

# 2.4 bars_elapsed = 5 + trade_confirmed=False + 真实持仓 0 → 写 unlock_stuck_recovered
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           trade_confirmed_value=False,
                                           real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 6
    # 设 portfolio 已空（UNLOCK 已删 target）
    eng._unlock_in_flight = {
        "signal_key": "sig-2-4", "target_signal_key": "tgt-2-4",
        "target_side": "SHORT", "target_snapshot": None,
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._check_unlock_stuck(eng.last_bar)
    check("2.4a 清 in-flight", eng._unlock_in_flight, None)
    evs = read_events(eng, kinds={"unlock_stuck_recovered"})
    check("2.4b 写 unlock_stuck_recovered", len(evs) >= 1, True)
    if evs:
        check("2.4c 事件 reason 正确",
              evs[0].get("reason"), "real_position_zero_after_stuck_window")

# 2.5 bars_elapsed = 5 + trade_confirmed=False + 真实持仓 > 0 → 写 unlock_stuck_confirmed
#     并用 target_snapshot 重建 portfolio
with tmp_dir() as td:
    # target 是 SHORT 仓（UNLOCK 平昨仓 SHORT）→ 真实 SHORT 仍有持仓 → 卡单确认
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           trade_confirmed_value=False,
                                           real_longs=0, real_shorts=2)
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 6
    # 模拟 UNLOCK 卡单：engine 已删 target，但真实账户仍有反向持仓 2 手
    # 引擎 portfolio 应为空 → 用 target_snapshot 重建 target 回去
    target_snapshot = make_position(Side.SHORT, 2, 4545.0, 1,
                                    signal_key="yesterday-target-2-5").to_dict()
    eng._unlock_in_flight = {
        "signal_key": "sig-2-5", "target_signal_key": "yesterday-target-2-5",
        "target_side": "SHORT", "target_snapshot": target_snapshot,
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._check_unlock_stuck(eng.last_bar)
    check("2.5a 清 in-flight", eng._unlock_in_flight, None)
    evs = read_events(eng, kinds={"unlock_stuck_confirmed"})
    check("2.5b 写 unlock_stuck_confirmed", len(evs) >= 1, True)
    if evs:
        check("2.5c 事件 reason 正确",
              evs[0].get("reason"), "real_position_still_held_after_stuck_window")
    check("2.5d 用 snapshot 重建 portfolio",
          len(eng.positions), 1)
    if len(eng.positions) == 1:
        check("2.5e 重建仓位 signal_key 正确",
              eng.positions.positions[0].signal_key, "yesterday-target-2-5")
        check("2.5f 重建仓位 side 正确",
              eng.positions.positions[0].side, Side.SHORT)
        check("2.5g 重建仓位 volume 正确",
              eng.positions.positions[0].volume, 2)

# 2.6 broker.trade_confirmed 抛异常 → 保守走 reconcile（视作未确认）
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(),
                                           raise_on_trade_confirmed=True,
                                           real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 6
    eng._unlock_in_flight = {
        "signal_key": "sig-2-6", "target_signal_key": "tgt-2-6",
        "target_side": "SHORT", "target_snapshot": None,
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._check_unlock_stuck(eng.last_bar)
    check("2.6a 异常被吞掉，不阻断", eng._unlock_in_flight, None)
    # 写 unlock_stuck_recovered 事件（保守按恢复处理：trade_confirmed 抛异常 → False，
    # broker.real_position(SHORT) 也保守按 None 处理 → reason 带 unknown）
    evs_recovered = read_events(eng, kinds={"unlock_stuck_recovered"})
    check("2.6b 保守按恢复处理（写 recovered 事件）", len(evs_recovered) >= 1, True)

# 2.7 多次提交 in-flight 替换（每次新报单覆盖）
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec())
    eng = make_engine(td, broker=ctl_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 10   # bars_elapsed = 10 - 3 = 7 >= 5
    eng._unlock_in_flight = {
        "signal_key": "old-sig", "target_signal_key": "old-tgt",
        "target_side": "SHORT", "target_snapshot": None,
        "submit_bar_ts": 4000, "submit_bar_seq": 1}
    eng._unlock_in_flight = {
        "signal_key": "new-sig", "target_signal_key": "new-tgt",
        "target_side": "SHORT", "target_snapshot": None,
        "submit_bar_ts": 5000, "submit_bar_seq": 3}
    eng._check_unlock_stuck(eng.last_bar)
    # 调了 broker 一次（new-sig），signal_key 应是 new
    check("2.7 in-flight 被替换 → broker 收到新 sig",
          ctl_b.tc_calls[0][1] if ctl_b.tc_calls else None, "new-sig")

# 2.8 UNLOCK 拒单不设 in-flight
with tmp_dir() as td:
    rej_b = RejectDryBroker(InstrumentSpec())
    eng = make_engine(td, broker=rej_b)
    eng.last_bar = make_bar()
    eng.bars_seen = 5
    # 设 portfolio 有一个反向仓（UNLOCK 触发条件）
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="yesterday-pos")
    eng.positions.add(pos)
    sig = make_signal("unlock-sig", Side.SHORT)
    eng.on_signal(sig)
    check("2.8a UNLOCK 拒单 → portfolio 仍保留 pos", len(eng.positions), 1)
    check("2.8b UNLOCK 拒单 → in-flight 未设", eng._unlock_in_flight, None)
    check("2.8c state 回 IDLE", eng._state, EngineState.IDLE)


# ════════════════════════════════════════════════════════════════
# [3] F2 _restore 末尾首拉真实持仓（source="restore"）
# ════════════════════════════════════════════════════════════════
print("\n[3] F2 _restore 末尾首拉真实持仓")

# 3.1 无持仓 → 不报错但不触发 reconcile
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=0, real_shorts=0)
    # 注意：直接 make_engine 走 _restore 会调用 reconcile
    # 无持仓时不应触发任何 reconcile 事件
    eng = make_engine(td, broker=ctl_b)
    evs = read_events(eng, kinds={"position_externally_closed_summary",
                                  "position_mismatch", "restore_reconcile_failed"})
    check("3.1 无持仓 → 无 reconcile 事件", len(evs), 0)

# 3.2 有持仓 + broker 无 real_position → skip（默认 DryRunBroker real_position=None）
#     已经在 P14a 测试过；此处快速回归一下
with tmp_dir() as td:
    spec = InstrumentSpec()
    dry_b = DryRunBroker(spec)  # 默认 real_position=None
    # 手动构造 engine 前先把 store 写入持仓
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = 1
    cfg.risk.max_volume = 10
    cfg.sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    cfg.sizing["enabled"] = False
    cfg.sizing["fixed_volume"] = 1
    cfg.sizing["batch_open"] = 1
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": 5.0, "stop_loss_points": 10.0})
    store = Store(os.path.join(td, "state.db"))
    # 写入持仓
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-test").to_dict()
    store.set_json("positions", [pos_dict])
    store.set_json("bars_seen", 5)
    ev = EventLog(os.path.join(td, "events.jsonl"), echo=False, echo_kinds=None)
    eng = GatewayEngine(cfg, dry_b, entry, exitp, store, ev)
    # broker 无 real_position → skip reconcile → portfolio 保留
    check("3.2 broker 无 real_position → portfolio 保留", len(eng.positions), 1)

# 3.3 有持仓 + real_vol == engine_vol → skip
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-3").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=1, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)   # 触发 _restore
    check("3.3 一致 → portfolio 保留", len(eng.positions), 1)
    evs = read_events(eng, kinds={"position_externally_closed_summary",
                                  "position_mismatch", "restore_reconcile_failed"})
    check("3.3b 一致 → 无 reconcile 事件", len(evs), 0)

# 3.4 有持仓 + real_vol < engine_vol → FIFO 部分平（trade reason=reconcile_external_partial）
#     构造：2 仓各 2 手（engine_vol=4），real_longs=2（差 2 手）→ 平最早仓 p_a 整笔 2 手，剩 p_b
with tmp_dir() as td:
    pos_a = make_position(Side.LONG, 2, 4545.0, 1, signal_key="restore-3-4-A").to_dict()
    pos_b = make_position(Side.LONG, 2, 4547.0, 2, signal_key="restore-3-4-B").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_a, pos_b])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=2, real_shorts=0)
    eng = make_engine(td, max_open_positions=3, broker=ctl_b)
    check("3.4a 部分平后剩 1 仓", len(eng.positions), 1)
    check("3.4b 保留最晚建仓的 (entry_bar_seq=2，FIFO 平最早)",
          eng.positions.positions[0].signal_key, "restore-3-4-B")
    trades = eng.store.trades()
    check("3.4c 1 条 reconcile_external_partial trade", len(trades), 1)
    if trades:
        check("3.4d 平的是第 1 仓 (A)", trades[0]["signal_key"], "restore-3-4-A")
        check("3.4e trade reason", trades[0]["reason"], "reconcile_external_partial")
    evs = read_events(eng, kinds={"position_externally_closed_summary"})
    check("3.4f 写 reconcile_partial summary", len(evs) >= 1, True)
    if evs:
        check("3.4g summary 带 source=restore",
              evs[0].get("source"), "restore")

# 3.5 有持仓 + real_vol == 0 → 全平
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-5").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("3.5a real_vol==0 → 全平", len(eng.positions), 0)
    check("3.5b state IDLE", eng._state, EngineState.IDLE)
    trades = eng.store.trades()
    check("3.5c 1 条 reconcile_external_partial trade", len(trades), 1)

# 3.6 有持仓 + real_vol > engine_vol → 告警不接管
with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-6").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=5, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    check("3.6a real_vol>engine_vol → portfolio 保留", len(eng.positions), 1)
    evs = read_events(eng, kinds={"position_mismatch"})
    check("3.6b 写 position_mismatch 告警", len(evs) >= 1, True)
    if evs:
        check("3.6c 告警带 source=restore",
              evs[0].get("source"), "restore")

# 3.7 broker.real_position 抛异常 → 写 restore_reconcile_failed，不阻断启动
class RaisingRealPosBroker(DryRunBroker):
    name = "raising_rp"
    def real_position(self, side):
        raise RuntimeError("simulated real_position failure")

with tmp_dir() as td:
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-7").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    rp_b = RaisingRealPosBroker(InstrumentSpec())
    eng = make_engine(td, broker=rp_b)
    check("3.7a 异常被吞 → 引擎能启动（portfolio 保留）", len(eng.positions), 1)
    evs = read_events(eng, kinds={"restore_reconcile_failed"})
    check("3.7b 写 restore_reconcile_failed", len(evs) >= 1, True)

# 3.8 source="on_bar" vs "restore" 差异：on_bar 拦截 bars_held<1，restore 不拦
with tmp_dir() as td:
    # 建一个仓后 bars_seen=1（同侧 bars_held=0）→ on_bar 应拦截，restore 不拦
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="restore-3-8").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 1)  # bars_held = 1 - 1 = 0
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    # restore 路径不拦 → 应该全平
    check("3.8 restore 路径不拦 bars_held<1 → 全平", len(eng.positions), 0)


# ════════════════════════════════════════════════════════════════
# [4] F1+F2 集成
# ════════════════════════════════════════════════════════════════
print("\n[4] F1+F2 集成")

# 4.1 兼容回归：dry_run broker 默认 trade_confirmed=True → 现有 P13 UNLOCK 行为不变
with tmp_dir() as td:
    eng = make_engine(td)  # 默认 DryRunBroker → trade_confirmed=True
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="yesterday-4-1")
    eng.positions.add(pos)
    eng.last_bar = make_bar()
    eng.bars_seen = 5
    sig = make_signal("unlock-sig-4-1", Side.SHORT)
    eng.on_signal(sig)
    check("4.1a UNLOCK 成交 → portfolio 删", len(eng.positions), 0)
    check("4.1b 设了 _unlock_in_flight", eng._unlock_in_flight is not None, True)
    # 跑 5 根 bar（每根 bars_seen+1，bars_elapsed 累计 1,2,3,4,5）
    for i in range(5):
        eng.bars_seen += 1
        eng._check_unlock_stuck(eng.last_bar)
    # 第 5 次调（bars_elapsed=5）→ trade_confirmed=True → 清 in-flight
    check("4.1c 5 bars 后 in-flight 清掉", eng._unlock_in_flight, None)
    evs = read_events(eng, kinds={"unlock_confirmed"})
    check("4.1d 写 unlock_confirmed 事件", len(evs) >= 1, True)

# 4.2 F2+F1 联动：_restore 末尾清残留 → UNLOCK 不再被卡
#     （场景：上轮 UNLOCK 卡单 + 真实持仓已平 → 重启后 F2 清掉残留仓，
#      F1 看不到 in-flight（已清），新 UNLOCK 重新走 _unlock_position）
with tmp_dir() as td:
    # 准备：store 写一个残留多仓（模拟上轮卡单留下的幽灵）
    pos_dict = make_position(Side.LONG, 1, 4545.0, 1, signal_key="ghost-pos").to_dict()
    pre_store = Store(os.path.join(td, "state.db"))
    pre_store.set_json("positions", [pos_dict])
    pre_store.set_json("bars_seen", 5)
    pre_store.close()
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=0, real_shorts=0)
    eng = make_engine(td, broker=ctl_b)
    # F2 已把幽灵清掉
    check("4.2a F2 清掉幽灵后 portfolio 空", len(eng.positions), 0)
    check("4.2b F2 清掉幽灵后 state IDLE", eng._state, EngineState.IDLE)
    # 新信号进来（反向 SHORT）→ IDLE + portfolio 空 → 走正常开仓路径（不是 UNLOCK）
    eng.last_bar = make_bar()
    sig = make_signal("new-sig-4-2", Side.SHORT)
    eng.on_signal(sig)
    check("4.2c 新 SHORT 信号 → 开空仓（非 UNLOCK）", len(eng.positions), 1)
    check("4.2d 开空仓 side 正确",
          eng.positions.positions[0].side, Side.SHORT)

# 4.3 F1 持久化 + 重启：UNLOCK 报单 + 进程崩 + _restore 读出 in_flight + 5 bars 后复核
#     场景：UNLOCK 报单成功 → 引擎进程崩（UNLOCK broker 返回 filled 但真实未成交，
#              真实账户仍持仓）→ 重启：
#       · _restore 末尾读出 in_flight（持久化生效）
#       · 第 (5+1)=6 根 bar 调 _check_unlock_stuck → trade_confirmed=False
#       · reconcile 检查 real_position=0（broker 真实未成交） → 写 unlock_stuck_recovered
#       · _unlock_in_flight 清掉
#     这是 F1 卡单检测在"持久化 + 重启"链路下的真正价值：
#     若不持久化，引擎崩后 in_flight 丢了，5 bar 复核永远不会触发，
#     F1 卡单检测在重启场景下形同虚设。
with tmp_dir() as td:
    ctl_b = ControlledTradeConfirmedBroker(InstrumentSpec(), real_longs=0, real_shorts=0,
                                           trade_confirmed_value=False)
    eng = make_engine(td, broker=ctl_b)
    # 预置一个 LONG 仓（模拟"昨仓"），UNLOCK 把它清掉
    pos = make_position(Side.LONG, 1, 4545.0, 1, signal_key="ghost-4-3")
    eng.positions.add(pos)
    eng.last_bar = make_bar()
    eng.bars_seen = 5
    sig = make_signal("unlock-sig-4-3", Side.SHORT)
    eng.on_signal(sig)
    # UNLOCK 报单成功（dry_run 同步撮合 → portfolio 立即清），
    # 但 ControlledTradeConfirmedBroker.tc_value=False → _check_unlock_stuck 后续复核时
    #   trade_confirmed=False → 走卡单兜底
    check("4.3a UNLOCK 成交 → portfolio 删", len(eng.positions), 0)
    check("4.3b 设了 _unlock_in_flight（含 submit_bar_seq）",
          eng._unlock_in_flight is not None, True)
    # _persist 已把 in_flight 写入 store（_persist 是 _unlock_position 内末尾必走步骤）
    persisted = eng.store.get_json("_unlock_in_flight")
    check("4.3c store 已存 _unlock_in_flight（含目标仓 signal_key='ghost-4-3'）",
          isinstance(persisted, dict) and persisted.get("target_signal_key") == "ghost-4-3",
          True)

    # ── 模拟引擎进程崩溃 + 重启 ──
    eng2 = make_engine(td, broker=ctl_b)
    check("4.3d 重启后 _restore 读出 _unlock_in_flight",
          eng2._unlock_in_flight is not None, True)
    check("4.3e 重启后 portfolio 仍为空（UNLOCK 已删残留）",
          len(eng2.positions), 0)

    # 跑 5 根 bar（bars_elapsed 累计到 5）→ 触发 _check_unlock_stuck 复核
    eng2.last_bar = make_bar()
    for i in range(5):
        eng2.bars_seen += 1
        eng2._check_unlock_stuck(eng2.last_bar)
    check("4.3f 5 bars 后 _check_unlock_stuck → 清 in-flight",
          eng2._unlock_in_flight, None)
    evs = read_events(eng2, kinds={"unlock_stuck_recovered"})
    check("4.3g 写 unlock_stuck_recovered（trade_confirmed=False + real_position=0）",
          len(evs) >= 1, True)


# ════════════════════════════════════════════════════════════════
# 结果汇总
# ════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("P16 Phase F 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(0 if _FAIL == 0 else 1)
