# -*- coding: utf-8 -*-
"""
P20 Phase I1：自动下单开关（关闭锁仓 / live 配置）单元测试
==========================================================
背景（用户拍板关闭语义）
    关闭自动下单：
      ① 不再接收买卖点信号（on_signal 顶部拒收，幂等键照常消费）
      ② 簿内所有「未锁定」持仓全部锁仓（OPEN_FIRST / UNLOCK_FIRST 一律
         LOCK，落簿反向 LOCKED 仓，次日对向信号走解锁入场管线）
    状态持久化：auto_order_enabled 落盘 state.db，重启保持关闭语义。

Phase I1 配置（账户选择）
    config.json：broker = dry_run / simnow / live
    broker_params.tq_market：simnow=仿真；实盘填期货公司名（如"创元期货"）
    实盘安全闸门：broker=live 或 tq_market≠simnow 时必须显式
      confirm_live_trading=true，否则拒绝启动（AppTrader.start 预检）。

硬性要求（本测试锁死）
    [1] 关闭后 on_signal 拒收（signal_action=skip / note=auto_order_off）
    [2] shutdown_and_lock_all 锁全部未锁定持仓（OPEN_FIRST + UNLOCK_FIRST）
        → 簿内只剩 LOCKED；enabled=False 持久化；auto_order_off 事件
    [3] 幂等：重复 shutdown 不产生新单 / 新 trade
    [4] 重启保持关闭：同 store 新引擎 auto_order_enabled=False，信号仍拒收
    [5] 关闭态 on_bar 补锁：锁仓被拒的残留持仓在后续 bar 自动补锁
        （reason=auto_order_off_retry）
    [6] 开启恢复：_persist True → 重启后 on_signal 正常开仓
    [7] 实盘安全闸门（AppTrader._check_live_gate 三分支）
    [8] broker 路由：SimNowBroker.is_live 判定 + LiveCTPBroker 注册

不需要真实 tqsdk / 网络；纯单测 + 真实 sqlite tempfile。
跑法：python tests/test_p20_phase_i1.py
"""
from __future__ import annotations

import copy
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
_REPO_ROOT = os.path.dirname(_TG_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)   # App/ 包（AppTrader 安全闸门单测）


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p20_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg import brokers  # noqa: E402  注册 dry_run/simnow/live
from tg.brokers.base import BROKERS, build_broker  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.brokers.simnow import LiveCTPBroker, SimNowBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import (  # noqa: E402
    Bar, EntryMode, Order, OrderIntent, Position, ExitPlan, Side, Signal, now_cn,
)

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


def make_cfg(max_pos=2):
    d = copy.deepcopy(DEFAULT_CONFIG)
    d["risk"]["max_open_positions"] = max_pos
    return GatewayConfig.from_dict(d)


def make_signal(is_buy, price=4500.0, high=4552.0, low=4548.0,
                date="2026-09-03 09:35", bsp_type="1", sig_key=None):
    sig_key = sig_key or ("{}|{}|{}".format(date, bsp_type, "B" if is_buy else "S"))
    return Signal(key=sig_key, symbol="CFFEX.IF", freq="5m", date=date,
                  timestamp=0, bsp_type=bsp_type, is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o=4500.0, h=4510.0, l=4490.0, c=4505.0,
             date="2026-09-03 09:40"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_pos(symbol="CFFEX.IF", side=Side.LONG, vol=1, entry_price=4500.0,
             entry_mode=EntryMode.OPEN_FIRST, signal_key="P20-pos",
             entry_bar_seq=1):
    return Position(
        symbol=symbol, side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-02 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=signal_key,
        open_order_id="p20-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq,
        entry_mode=entry_mode)


class LockRejectBroker(DryRunBroker):
    """前 reject_n 次 LOCK submit 拒绝（模拟锁仓拒单/卡单），之后放行。"""

    def __init__(self, spec, params, reject_n=1):
        super().__init__(spec, params)
        self._reject_left = reject_n

    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        intent = self._resolve_intent(intent, side)
        if intent is OrderIntent.LOCK and self._reject_left > 0:
            self._reject_left -= 1
            o = Order(
                order_id="dry-reject-{:06d}".format(len(self.orders)),
                signal_key=signal_key, symbol=self.spec.trade_symbol, side=side,
                action="open", volume=int(volume), price=0.0,
                req_price=float(ref_price), filled_price=None,
                status="rejected", created_at=now_cn(), broker=self.name, note=note,
                meta={"intent": intent.value, "offset": "OPEN",
                      "reject_reason": "sim_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key,
                              note=note)


def build_engine(tmpdir, exit_policy=None, broker=None, cfg=None,
                 store=None, ev=None):
    cfg = cfg or make_cfg(max_pos=2)
    spec = InstrumentSpec()
    broker = broker or DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy
    entry = DefaultEntryPolicy({})
    exitp = exit_policy or DefaultExitPolicy({})
    store = store or Store(os.path.join(tmpdir, "state.db"))
    ev = ev or EventLog(os.path.join(tmpdir, "events.jsonl"),
                        echo=False, echo_kinds=None)
    engine = GatewayEngine(cfg, broker, entry, exitp, store, ev)
    return engine, store, broker, ev


def event_kinds(ev_path):
    kinds = []
    with open(ev_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


def lock_orders(broker):
    return [o for o in broker.orders if o.meta.get("intent") == "lock"]


# ════════════════════════════════════════════════════════════════
# [1] 关闭后 on_signal 拒收（signal_action=skip / note=auto_order_off）
# ════════════════════════════════════════════════════════════════
print("\n[1] 关闭后 on_signal 拒收（幂等键消费 + skip 落盘 + 事件）")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.shutdown_and_lock_all()          # 空簿：无锁仓操作，只关信号门
    sig = make_signal(is_buy=True, sig_key="P20-1|1|B")
    engine.on_signal(sig)
    check("[1a] auto_order_enabled=False",
          engine.auto_order_enabled, False)
    check("[1b] 信号 action=skip", store.signal_action(sig.key), "skip")
    check("[1c] 零报单（信号被拒收）", len(broker.orders), 0)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[1d] signal_skip 事件已写", "signal_skip" in kinds, True)
    check("[1e] auto_order_off 事件已写", "auto_order_off" in kinds, True)


# ════════════════════════════════════════════════════════════════
# [2] shutdown_and_lock_all 锁全部未锁定持仓（OPEN_FIRST + UNLOCK_FIRST）
# ════════════════════════════════════════════════════════════════
print("\n[2] shutdown_and_lock_all：2 笔未锁持仓 → 全部 LOCKED 落簿")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-2A", entry_bar_seq=1,
                                       entry_mode=EntryMode.OPEN_FIRST))
    engine.positions.add(make_pos(signal_key="P20-2B", entry_bar_seq=2,
                                       entry_mode=EntryMode.UNLOCK_FIRST))

    engine.shutdown_and_lock_all()
    check("[2a] enabled=False", engine.auto_order_enabled, False)
    modes = sorted(p.entry_mode.value for p in engine.positions.positions)
    check("[2b] 簿内全是 LOCKED（2 笔反向锁仓）", modes,
          ["locked", "locked"])
    check("[2c] 锁仓信号键 = 原键#lock",
          sorted(p.signal_key for p in engine.positions.positions),
          ["P20-2A#lock", "P20-2B#lock"])
    check("[2d] 全部 LOCKED → state=IDLE", engine._state.name, "IDLE")
    check("[2e] 2 笔 LOCK 报单", len(lock_orders(broker)), 2)
    check("[2f] 2 笔锁仓 Trade 落盘（reason=auto_order_off）",
          sum(1 for t in store.trades() if t["reason"] == "auto_order_off"), 2)
    check("[2g] enabled=False 已持久化",
          store.get_json("auto_order_enabled", True), False)
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[2h] auto_order_off 事件 locked_n=2/remaining_unlocked=0",
          kinds.count("auto_order_off"), 1)
    check("[2i] lock_booked ×2", kinds.count("lock_booked"), 2)


# ════════════════════════════════════════════════════════════════
# [3] 幂等：重复 shutdown 不产生新单 / 新 trade
# ════════════════════════════════════════════════════════════════
print("\n[3] 幂等：簿内只剩 LOCKED 时重复 shutdown 无操作")
with tmp_dir() as tmp:
    engine, store, broker, ev = build_engine(tmp)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-3A", entry_bar_seq=1))
    engine.shutdown_and_lock_all()
    n_orders = len(broker.orders)
    n_trades = len(store.trades())

    engine.shutdown_and_lock_all()          # 第二次关闭：全部已 LOCKED → no-op
    check("[3a] 无新增报单", len(broker.orders), n_orders)
    check("[3b] 无新增 trade", len(store.trades()), n_trades)
    check("[3c] 簿仍 1 笔 LOCKED",
          [p.entry_mode for p in engine.positions.positions],
          [EntryMode.LOCKED])
    check("[3d] enabled 仍 False", engine.auto_order_enabled, False)


# ════════════════════════════════════════════════════════════════
# [4] 重启保持关闭：同 store 新引擎 enabled=False，信号仍拒收
# ════════════════════════════════════════════════════════════════
print("\n[4] 重启保持关闭语义（state.db 持久化）")
with tmp_dir() as tmp:
    engine1, store1, broker1, ev1 = build_engine(tmp)
    engine1.positions.add(make_pos(signal_key="P20-4A", entry_bar_seq=1))
    engine1.on_bar(make_bar(1000))
    engine1.shutdown_and_lock_all()

    engine2, store2, broker2, ev2 = build_engine(tmp)
    check("[4a] 重启后 enabled=False", engine2.auto_order_enabled, False)
    check("[4b] 簿内 LOCKED 已恢复",
          [p.entry_mode for p in engine2.positions.positions],
          [EntryMode.LOCKED])
    sig = make_signal(is_buy=True, sig_key="P20-4|2|B")
    engine2.on_signal(sig)
    check("[4c] 重启后信号仍拒收（skip）",
          store2.signal_action(sig.key), "skip")
    check("[4d] 重启后零报单", len(broker2.orders), 0)


# ════════════════════════════════════════════════════════════════
# [5] 关闭态 on_bar 补锁：锁仓被拒的残留持仓自动补锁
# ════════════════════════════════════════════════════════════════
print("\n[5] 关闭态 on_bar 补锁（首轮锁仓被拒 → 后续 bar 自动补锁）")
with tmp_dir() as tmp:
    broker = LockRejectBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0},
                              reject_n=1)
    engine, store, broker, ev = build_engine(tmp, broker=broker)
    engine.on_bar(make_bar(1000))
    engine.positions.add(make_pos(signal_key="P20-5A", entry_bar_seq=1))

    engine.shutdown_and_lock_all()
    check("[5a] 首轮锁仓被拒：持仓未锁（state=EXITING）",
          engine._state.name, "EXITING")
    check("[5b] 簿内仍 1 笔未锁（OPEN_FIRST）",
          [p.entry_mode for p in engine.positions.positions],
          [EntryMode.OPEN_FIRST])

    # 下一根 bar（冷却期外）→ 关闭态自动补锁
    engine.on_bar(make_bar(2000))
    check("[5c] 补锁后簿内 LOCKED",
          [p.entry_mode for p in engine.positions.positions],
          [EntryMode.LOCKED])
    check("[5d] 补锁后 state=IDLE", engine._state.name, "IDLE")
    ev.flush()
    kinds = event_kinds(os.path.join(tmp, "events.jsonl"))
    check("[5e] 补锁 trade reason=auto_order_off_retry",
          sum(1 for t in store.trades()
              if t["reason"] == "auto_order_off_retry"), 1)


# ════════════════════════════════════════════════════════════════
# [6] 开启恢复：_persist True → 重启后 on_signal 正常开仓
# ════════════════════════════════════════════════════════════════
print("\n[6] 开启恢复：显式置 True 持久化 → 重启后信号正常开仓")
with tmp_dir() as tmp:
    engine1, store1, broker1, ev1 = build_engine(tmp)
    engine1.shutdown_and_lock_all()
    check("[6a] 先关闭", engine1.auto_order_enabled, False)

    engine1.auto_order_enabled = True       # 模拟 AppTrader._reset_engine_switch
    engine1._persist()
    engine2, store2, broker2, ev2 = build_engine(tmp)
    check("[6b] 重启后 enabled=True", engine2.auto_order_enabled, True)
    sig = make_signal(is_buy=True, sig_key="P20-6|1|B")
    engine2.on_signal(sig)
    check("[6c] 信号 action=opened", store2.signal_action(sig.key), "opened")
    check("[6d] 1 笔开仓报单", len(broker2.orders), 1)
    check("[6e] 簿内 1 笔 OPEN_FIRST",
          [p.entry_mode for p in engine2.positions.positions],
          [EntryMode.OPEN_FIRST])


# ════════════════════════════════════════════════════════════════
# [7] 实盘安全闸门（AppTrader._check_live_gate 三分支）
# ════════════════════════════════════════════════════════════════
print("\n[7] 实盘安全闸门（AppTrader._check_live_gate）")
from App.AppTrader import AppTrader  # noqa: E402
from App.AppErrors import AppError  # noqa: E402


def cfg_with(broker, bp):
    d = copy.deepcopy(DEFAULT_CONFIG)
    d["broker"] = broker
    d["broker_params"].update(bp)
    return d


def raises_apperror(fn, name):
    try:
        fn()
    except AppError:
        check(name, True, True)
        return
    check(name, True, False)


# [7a] broker=live 但 tq_market 仍 simnow → 配置矛盾拒绝
raises_apperror(
    lambda: AppTrader._check_live_gate(cfg_with("live", {}), "live"),
    "[7a] broker=live + tq_market=simnow → 拒绝")

# [7b] tq_market≠simnow 但未开 confirm_live_trading → 拒绝
raises_apperror(
    lambda: AppTrader._check_live_gate(
        cfg_with("simnow", {"tq_market": "创元期货"}), "simnow"),
    "[7b] tq_market=创元期货 未确认实盘 → 拒绝")

# [7c] tq_market≠simnow + confirm_live_trading=true → 放行
try:
    AppTrader._check_live_gate(
        cfg_with("simnow", {"tq_market": "创元期货",
                            "confirm_live_trading": True}), "simnow")
    check("[7c] 实盘双确认 → 放行", True, True)
except AppError as e:
    check("[7c] 实盘双确认 → 放行", str(e), "NO_ERROR")

# [7d] dry_run / simnow 仿真 → 放行
try:
    AppTrader._check_live_gate(cfg_with("dry_run", {}), "dry_run")
    AppTrader._check_live_gate(cfg_with("simnow", {}), "simnow")
    check("[7d] dry_run / simnow 仿真 → 放行", True, True)
except AppError as e:
    check("[7d] dry_run / simnow 仿真 → 放行", str(e), "NO_ERROR")


# ════════════════════════════════════════════════════════════════
# [8] broker 路由：is_live 判定 + LiveCTPBroker 注册
# ════════════════════════════════════════════════════════════════
print("\n[8] broker 路由（SimNowBroker.is_live / LiveCTPBroker 注册）")
spec = InstrumentSpec()

b_sim = SimNowBroker(spec, {"tq_market": "simnow"})
check("[8a] tq_market=simnow → is_live=False", b_sim.is_live, False)

b_live = SimNowBroker(spec, {"tq_market": "创元期货",
                             "confirm_live_trading": True})
check("[8b] tq_market=创元期货 → is_live=True", b_live.is_live, True)
check("[8c] 无凭据 → _conn_error 指向实盘/天勤",
      b_live._conn_error is not None
      and "实盘/天勤" in b_live._conn_error, True)

b_gate = SimNowBroker(spec, {"tq_market": "创元期货"})
check("[8d] 未确认实盘 → 安全闸门拦截",
      b_gate._conn_error is not None
      and "confirm_live_trading" in b_gate._conn_error, True)

check("[8e] LiveCTPBroker 已注册", "live" in BROKERS, True)
b_live2 = build_broker("live", spec, {})
check("[8f] build_broker('live') 返回 LiveCTPBroker",
      isinstance(b_live2, LiveCTPBroker), True)
check("[8g] LiveCTPBroker.is_live=True", b_live2.is_live, True)
check("[8h] live 别名未覆盖 simnow 注册", "simnow" in BROKERS, True)


# ════════════════════════════════════════════════════════════════
# [9] AppTrader.start 子进程 cmd 组装（--source sse + 品种/周期/地址 + 日志落盘）
#     回归：曾经不带 --source sse → cfg.source 缺省 type=replay → 回放源
#     无数据立即跑完 → 子进程退出 → 前端开关自动关闭（用户报告的问题）。
# ════════════════════════════════════════════════════════════════
print("\n[9] AppTrader.start：--source sse 组装 + gateway.log 落盘")
from App import AppTrader as AT  # noqa: E402


class _FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.pid = os.getpid()   # 存活 pid，让 running 属性为 True

    def poll(self):
        return None

    def send_signal(self, sig):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


with tmp_dir() as tmp:
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"broker": "dry_run", "state_dir": os.path.join(tmp, "state")},
                  f)
    out_dir = os.path.join(tmp, "state")
    state_file = os.path.join(tmp, "auto_trader_state.json")

    captured = {}
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = state_file
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(cfg_path=cfg_path, out_dir=out_dir,
                      symbol="KQ.m@CFFEX.SH", freq="5m",
                      sse_base="http://127.0.0.1:18081")
        cmd = captured.get("cmd") or []
        check("[9a] 带 --source sse", "--source" in cmd
              and cmd[cmd.index("--source") + 1] == "sse", True)
        check("[9b] 带 --symbol 当前页面品种",
              "--symbol" in cmd
              and cmd[cmd.index("--symbol") + 1] == "KQ.m@CFFEX.SH", True)
        check("[9c] 带 --freq 当前页面周期",
              "--freq" in cmd
              and cmd[cmd.index("--freq") + 1] == "5m", True)
        check("[9d] 带 --sse-base 服务地址",
              "--sse-base" in cmd
              and cmd[cmd.index("--sse-base") + 1] == "http://127.0.0.1:18081",
              True)
        check("[9e] gateway.log 已创建", os.path.isfile(
            os.path.join(out_dir, "gateway.log")), True)
        check("[9f] handle 记录 symbol/freq/sse_base",
              (res.get("symbol"), res.get("freq"), res.get("sse_base")),
              ("KQ.m@CFFEX.SH", "5m", "http://127.0.0.1:18081"))
        check("[9g] 状态文件含来源参数",
              _read_json(state_file).get("symbol"), "KQ.m@CFFEX.SH")
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file

# [9i] P1-3：start() 必须清除上次遗留 .stop_request，否则第二次开启秒退
#（残留 flag 会让新看护线程一眼就叫停并锁仓退出）。
with tmp_dir() as tmp:
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"broker": "dry_run", "state_dir": os.path.join(tmp, "state")},
                  f)
    out_dir = os.path.join(tmp, "state")
    os.makedirs(out_dir, exist_ok=True)
    # 预置"上次 stop 残留"的 flag
    leftover = os.path.join(out_dir, ".stop_request")
    with open(leftover, "w", encoding="utf-8") as f:
        f.write("leftover from previous round\n")
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        AT.subprocess.Popen = (lambda cmd, **kw: _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(cfg_path=cfg_path, out_dir=out_dir)   # 不应抛错、不应秒退
        check("[9i] start() 清除了上一轮遗留的 .stop_request（P1-3）",
              not os.path.exists(leftover), True)
        check("[9i] 清除后子进程仍正常启动（running=True）", res.get("running"), True)
        # 清理用极短 timeout：_FakeProc 永不退出，默认 timeout=150s 会空等满
        # 并走强杀分支（在 Windows 上旧代码还会因 SIGKILL 崩溃，P0）；短
        # timeout 让强杀分支立即执行，兼作 P0 回归（不应抛 AttributeError）。
        t.stop(timeout=0.1)   # 收尾清理，避免污染本目录
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file

# [9j] P2-5 回援分支修正：只认本轮新增事件，历史 off + 上轮残留 state=false
# 不得判优雅（否则"本轮超时强杀没锁仓"仍被谎报 graceful=True）。
with tmp_dir() as tmp:
    out_dir = tmp
    evt = os.path.join(out_dir, "events.jsonl")
    # 历史已有 2 条 off（模拟往常有两次成功收尾）+ off_before=2（本轮无新增）
    with open(evt, "w", encoding="utf-8") as f:
        for _ in range(2):
            json.dump({"kind": "auto_order_off"}, f)
            f.write("\n")
    from tg.store import Store  # noqa: E402
    s = Store(os.path.join(out_dir, "state.db"))
    s.set_json("auto_order_enabled", False)   # 恰好是上一轮遗留的 false
    s.close()
    check("[9j] 历史off且state=false 不下回援（拒绝谎报 graceful）",
          AT._graceful_by_result(out_dir, 2), False)
    # 本轮确实新增 1 条 -> 判优雅
    with open(evt, "a", encoding="utf-8") as f:
        json.dump({"kind": "auto_order_off"}, f)
        f.write("\n")
    check("[9j] 本轮新增 auto_order_off 判优雅",
          AT._graceful_by_result(out_dir, 2), True)
    # 全新目录 events 从未有 off、state=false -> 唯一合法回援场景
    fresh = os.path.join(tmp, "fresh")
    os.makedirs(fresh, exist_ok=True)
    s2 = Store(os.path.join(fresh, "state.db"))
    s2.set_json("auto_order_enabled", False)
    s2.close()
    check("[9j] 空events+state=false 才回援判优雅",
          AT._graceful_by_result(fresh, 0), True)

# [9h] 未传 symbol/freq → 回落到 cfg.source / 内置默认（不再落到 replay）
with tmp_dir() as tmp:
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"broker": "dry_run",
                   "source": {"symbol": "KQ.m@CFFEX.RB", "freq": "15m"},
                   "state_dir": os.path.join(tmp, "state")}, f)
    captured = {}
    orig_popen = AT.subprocess.Popen
    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(cfg_path=cfg_path, out_dir=os.path.join(tmp, "state"))
        cmd = captured.get("cmd") or []
        check("[9h] 缺省品种走 cfg.source.symbol",
              cmd[cmd.index("--symbol") + 1], "KQ.m@CFFEX.RB")
        check("[9i] 缺省周期走 cfg.source.freq",
              cmd[cmd.index("--freq") + 1], "15m")
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file


# ════════════════════════════════════════════════════════════════
# [10] AppTrader.status：进程退出后附带 gateway.log 尾部（定位"自动关闭"）
# ════════════════════════════════════════════════════════════════
print("\n[10] AppTrader.status：退出后附带日志尾部")
with tmp_dir() as tmp:
    out_dir = os.path.join(tmp, "state")
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "gateway.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("[gw] 启动 pid=123 source=sse symbol=KQ.m@CFFEX.IF freq=5m\n")
        f.write("Traceback (most recent call last):\n")
        f.write("  File \"tg/engine.py\", line 42, in on_bar\n")
        f.write("RuntimeError: boom\n")

    class _ExitedProc:
        pid = 123

        def poll(self):
            return 1   # 已退出

    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        t = AT.AppTrader()
        t._handle = AT._TraderProc(
            _ExitedProc(), cfg_path=os.path.join(tmp, "config.json"),
            out_dir=out_dir, started_at="2026-09-06 10:00:00",
            broker="dry_run", symbol="KQ.m@CFFEX.IF", freq="5m",
            sse_base="http://127.0.0.1:18081")
        st = t.status()
        check("[10a] running=False", st["running"], False)
        check("[10b] log_file 路径正确", st["log_file"], log_path)
        check("[10c] log_tail 带回异常尾部",
              "RuntimeError: boom" in (st.get("log_tail") or ""), True)
        check("[10d] 日志尾部含启动摘要",
              "source=sse" in (st.get("log_tail") or ""), True)
        check("[10g] exit_rc 带回退出码", st.get("exit_rc"), 1)
        # 日志雪崩回归：退出上报只写一次，多次轮询 status() 不得把日志
        # 尾部反复 Echo 回 gateway.log（曾导致指数级自嵌套膨胀）。
        size_after_first = 0
        with open(log_path, "r", encoding="utf-8") as f:
            size_after_first = len(f.read())
        for _ in range(5):
            st2 = t.status()
            check("[10e] 退出上报一次后仍带回 log_tail",
                  "RuntimeError: boom" in (st2.get("log_tail") or ""), True)
        with open(log_path, "r", encoding="utf-8") as f:
            size_after_polls = len(f.read())
        check("[10f] 多次轮询日志不再增长（无雪崩）",
              size_after_polls == size_after_first, True)
    finally:
        AT._STATE_FILE = orig_state_file


# ════════════════════════════════════════════════════════════════
# [11] 无 config.json：目录与 gateway.log 也必须创建，并记录失败原因
#      回归：曾经校验失败在 makedirs 之前 raise → 什么都没有，无法定位
# ════════════════════════════════════════════════════════════════
print("\n[11] 配置缺失：仍落盘 gateway.log 记录失败原因")
with tmp_dir() as tmp:
    orig_state_file = AT._STATE_FILE
    try:
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        t = AT.AppTrader()
        try:
            t.start(cfg_path=os.path.join(tmp, "no_such_config.json"),
                    out_dir=os.path.join(tmp, "state"))
            check("[11a] 应抛 AppError", "no_raise", "AppError")
        except AppError as e:
            check("[11a] 应抛 AppError", "AppError", "AppError")
            check("[11b] 报错含配置路径",
                  os.path.join(tmp, "no_such_config.json") in str(e), True)
        log_path = os.path.join(tmp, "state", "gateway.log")
        check("[11c] state 目录已创建", os.path.isdir(
            os.path.join(tmp, "state")), True)
        check("[11d] gateway.log 已创建", os.path.isfile(log_path), True)
        content = ""
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            pass
        check("[11e] 日志含开启请求", "收到开启请求" in content, True)
        check("[11f] 日志含失败原因", "配置文件不存在" in content, True)
    finally:
        AT._STATE_FILE = orig_state_file


# ════════════════════════════════════════════════════════════════
# [12] 相对 state_dir（默认 "./state"）以配置文件所在目录为基准
#      回归：曾经 os.path.abspath("./state") 落到后端进程 CWD，
#      用户按 trader_gateway/state 找不到目录/日志
# ════════════════════════════════════════════════════════════════
print("\n[12] 相对 state_dir 解析到配置文件所在目录（trader_gateway/state）")
with tmp_dir() as tmp:
    orig_state_file = AT._STATE_FILE
    orig_popen = AT.subprocess.Popen
    try:
        cfg_dir = os.path.join(tmp, "tg_home")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"broker": "dry_run", "state_dir": "./state"}, f)
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state.json")
        captured = {}
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured.update(cmd=cmd) or _FakeProc(cmd))
        t = AT.AppTrader()
        res = t.start(cfg_path=cfg_path)   # 不传 out_dir：走 state_dir 解析
        expected = os.path.join(cfg_dir, "state")
        cmd = captured.get("cmd") or []
        check("[12a] --out 解析到配置目录/state",
              "--out" in cmd and cmd[cmd.index("--out") + 1] == expected, True)
        check("[12b] state 目录在配置目录下", os.path.isdir(expected), True)
        check("[12c] gateway.log 在配置目录下",
              os.path.isfile(os.path.join(expected, "gateway.log")), True)
        # 模拟用户后端从仓库根启动：config 在 trader_gateway/ 时
        # 必须落到 trader_gateway/state，而不是 CWD/state
        os.chdir(tmp)
        cfg2 = os.path.join(tmp, "trader_gateway")
        os.makedirs(cfg2, exist_ok=True)
        cfg_path2 = os.path.join(cfg2, "config.json")
        with open(cfg_path2, "w", encoding="utf-8") as f:
            json.dump({"broker": "dry_run", "state_dir": "./state"}, f)
        AT._STATE_FILE = os.path.join(tmp, "auto_trader_state_2.json")  # 新文件，避免误恢复
        captured2 = {}
        AT.subprocess.Popen = (lambda cmd, **kw:
                               captured2.update(cmd=cmd) or _FakeProc(cmd))
        t2 = AT.AppTrader()
        t2.start(cfg_path=cfg_path2)
        cmd2 = captured2.get("cmd") or []
        check("[12d] --out 落到 trader_gateway/state",
              "--out" in cmd2
              and cmd2[cmd2.index("--out") + 1] == os.path.join(cfg2, "state"),
              True)
        check("[12e] 未污染进程 CWD", os.path.isdir(os.path.join(tmp, "state")),
              False)
        check("[12f] 落到 trader_gateway/state",
              os.path.isdir(os.path.join(cfg2, "state")), True)
        check("[12g] gateway.log 已创建",
              os.path.isfile(os.path.join(cfg2, "state", "gateway.log")), True)
    finally:
        AT.subprocess.Popen = orig_popen
        AT._STATE_FILE = orig_state_file


print("\n" + "=" * 60)
print("P20 Phase I1 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
