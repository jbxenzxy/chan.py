# -*- coding: utf-8 -*-
"""
Phase G 接入 SimNow 真实账户（2026-09-05）
==========================================
背景
    Phase F 留下的起步态：simnow.trade_confirmed 保守返回 False（未维护
    signal_key→order 索引）。Phase G 补齐真实成交复核链路：
      · G1：signal_key → raw_order_id 索引（_finalize 登记）+
        trade_confirmed 用 api.get_order 重新拉**当前**订单，累计
        trade_records 真实成交量判定（不信任 submit 时的 _finalize 判定）。
      · G2：cancel_pending(signal_key) 撤在途单 + 引擎 _check_unlock_stuck
        在 confirmed=False 时先撤单再按真实持仓修正 —— 防「重建 portfolio
        后挂单又成交」的双重平仓。

硬性要求（本测试锁死）
    ① simnow.trade_confirmed：
        · api 不可用 / 空 signal_key / 无索引 → False（保守）
        · 单笔全成（trade_records 累计 ≥ 委托量）→ True
        · 跨追价重试 partial + 全成累加 ≥ 委托量 → True
        · 在途未成交（trade_records 空）→ False
        · get_order 抛异常 → 该单不计入 → False
        · rejected 委托不计入 expected
    ② simnow.cancel_pending：
        · api 不可用 / 无索引 → 0
        · LIVE 单撤掉、FINISHED 单跳过，返回撤单请求数
    ③ simnow._finalize：成功路径登记 signal_key → raw_order_id
    ④ engine._check_unlock_stuck G2 集成：
        · confirmed=False → 先调 cancel_pending；cancelled>0 写
          unlock_pending_cancelled 事件，再走 real_position 对账
        · confirmed=True → 不调 cancel_pending
        · broker 无 cancel_pending → 不炸，走原 F1 逻辑
    ⑤ base.cancel_pending 默认 0；dry_run 继承（同步撮合无在途单）

不需要真实 tqsdk / 网络（SimNowBroker 无凭据实例化，手动注入 FakeApi）。
跑法：python tests/test_p17_phase_g.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace as NS

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

# 保险：测试进程内清掉可能的 SimNow 凭据环境变量，防止实例化触发真连
for _k in ("SN_ACCOUNT", "SN_PASSWORD", "TQ_ACCOUNT", "TQ_PASSWORD"):
    os.environ.pop(_k, None)


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p17_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from tg.brokers.base import OrderIntent, register_broker  # noqa: E402
from tg.brokers.dry_run import DryRunBroker  # noqa: E402
from tg.brokers.simnow import SimNowBroker  # noqa: E402
from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
from tg.engine import GatewayEngine  # noqa: E402
from tg.events import EventLog  # noqa: E402
from tg.store import Store  # noqa: E402
from tg.strategy.default_policy import DefaultEntryPolicy, DefaultExitPolicy  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import Bar, Position, Side  # noqa: E402

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


# ════════════════════════════════════════════════════════════════
# Fake tqsdk 对象
# ════════════════════════════════════════════════════════════════
class FakeRawOrder:
    """模拟 tqsdk Order 对象（只带 trade_confirmed / cancel_pending 用到的字段）。"""

    def __init__(self, order_id, records=None, status="FINISHED", volume_left=0):
        self.order_id = order_id
        self.status = status
        self.trade_records = records or {}
        self.volume_left = volume_left
        self.last_msg = ""
        self.trade_price = 0


class FakePosition:
    def __init__(self, long_=0, short=0):
        self.pos_long_today = long_
        self.pos_long_his = 0
        self.pos_short_today = short
        self.pos_short_his = 0


class FakeApi:
    """模拟 tqsdk TqApi 的最小面：get_order / get_position / wait_update / cancel_order。"""

    def __init__(self, orders=None, long_pos=0, short_pos=0, fail_get_order=False):
        self._orders = orders or {}
        self._positions = {}
        self._long_pos = long_pos
        self._short_pos = short_pos
        self.fail_get_order = fail_get_order
        self.cancelled = []
        self.wait_calls = 0

    def get_order(self, oid):
        if self.fail_get_order:
            raise RuntimeError("session lost")
        return self._orders.get(oid)

    def get_position(self, sym):
        if sym not in self._positions:
            self._positions[sym] = FakePosition(self._long_pos, self._short_pos)
        return self._positions[sym]

    def wait_update(self, deadline=None):
        self.wait_calls += 1
        return True

    def cancel_order(self, oid):
        self.cancelled.append(oid)


def _rec(vol, price=4547.0):
    """构造一条 trade_record。"""
    return {"volume": vol, "price": price}


def make_simnow_broker():
    """无凭据实例化 SimNowBroker（early return，_api=None，不碰网络）。"""
    return SimNowBroker(InstrumentSpec(), params={})


# ════════════════════════════════════════════════════════════════
# [1] G1：simnow.trade_confirmed
# ════════════════════════════════════════════════════════════════
print("── [1] G1 simnow.trade_confirmed ──")

b = make_simnow_broker()
check("1.1 api 不可用 → False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k1"), False)

fake = FakeApi()
b._api = fake
check("1.2 空 signal_key → False", b.trade_confirmed(OrderIntent.UNLOCK, ""), False)
check("1.2b 无索引（signal_key 未登记）→ False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k-none"), False)

# 1.3 单笔全成
b._sig_orders = {"k1": ["r1"]}
b.orders = [NS(signal_key="k1", status="filled", volume=2)]
fake._orders = {"r1": FakeRawOrder("r1", {"t1": _rec(2)})}
check("1.3 单笔全成（traded 2 >= expected 2）→ True",
      b.trade_confirmed(OrderIntent.UNLOCK, "k1"), True)

# 1.4 跨追价重试：partial(1, 已撤) + 重试全成(2) 累加 3 >= 2
b._sig_orders = {"k2": ["r1", "r2"]}
b.orders = [NS(signal_key="k2", status="filled", volume=2)]
fake._orders = {
    "r1": FakeRawOrder("r1", {"t1": _rec(1)}),        # 部分成交后被撤
    "r2": FakeRawOrder("r2", {"t2": _rec(2)}),        # 重试单全成
}
check("1.4 跨重试累加（1+2 >= 2）→ True",
      b.trade_confirmed(OrderIntent.UNLOCK, "k2"), True)

# 1.5 在途未成交（trade_records 空）
fake._orders = {"r1": FakeRawOrder("r1", {}, status="LIVE", volume_left=2)}
check("1.5 在途未成交（records 空）→ False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k1"), False)

# 1.6 get_order 抛异常 → 该单不计入 → False
fake.fail_get_order = True
check("1.6 get_order 异常 → False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k1"), False)
fake.fail_get_order = False

# 1.7 rejected 委托不计入 expected
b._sig_orders = {"k3": ["r1"]}
b.orders = [NS(signal_key="k3", status="rejected", volume=2),
            NS(signal_key="k3", status="filled", volume=2)]
fake._orders = {"r1": FakeRawOrder("r1", {"t1": _rec(2)})}
check("1.7 rejected 不计入 expected（filled 2 为基准）→ True",
      b.trade_confirmed(OrderIntent.UNLOCK, "k3"), True)

# 1.7b 只有 rejected 委托 → expected=0 → False
b.orders = [NS(signal_key="k3", status="rejected", volume=2)]
check("1.7b 只有 rejected → expected=0 → False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k3"), False)

# 1.8 成交不足（traded 1 < expected 2）
b.orders = [NS(signal_key="k1", status="filled", volume=2)]
fake._orders = {"r1": FakeRawOrder("r1", {"t1": _rec(1)})}
check("1.8 成交不足（traded 1 < expected 2）→ False",
      b.trade_confirmed(OrderIntent.UNLOCK, "k1"), False)

# ════════════════════════════════════════════════════════════════
# [2] G2：simnow.cancel_pending
# ════════════════════════════════════════════════════════════════
print("── [2] G2 simnow.cancel_pending ──")

b2 = make_simnow_broker()
check("2.1 api 不可用 → 0", b2.cancel_pending("k1"), 0)

b2._api = FakeApi()
check("2.2 无索引 → 0", b2.cancel_pending("k-none"), 0)

# 2.3 LIVE 单撤、FINISHED 单跳过
fake2 = FakeApi(orders={
    "r1": FakeRawOrder("r1", {}, status="LIVE", volume_left=2),
    "r2": FakeRawOrder("r2", {"t1": _rec(2)}, status="FINISHED"),
})
b2._api = fake2
b2._sig_orders = {"k1": ["r1", "r2"]}
check("2.3 LIVE 撤 + FINISHED 跳过 → 返回 1", b2.cancel_pending("k1"), 1)
check("2.3b 撤单请求落在 LIVE 单上", fake2.cancelled, ["r1"])

# 2.4 全部已终态 → 0
fake3 = FakeApi(orders={"r2": FakeRawOrder("r2", {"t1": _rec(2)}, status="FINISHED")})
b2._api = fake3
b2._sig_orders = {"k1": ["r2"]}
check("2.4 全部 FINISHED → 0", b2.cancel_pending("k1"), 0)

# ════════════════════════════════════════════════════════════════
# [3] _finalize 登记 signal_key → raw_order_id
# ════════════════════════════════════════════════════════════════
print("── [3] _finalize 登记索引 ──")

b3 = make_simnow_broker()
fake3 = FakeApi(long_pos=2)  # baseline=0 + delta=2 → 校验立即通过
b3._api = fake3
raw = FakeRawOrder("RAW-001", {"t1": _rec(2), "t2": _rec(1)})
o = b3._finalize(raw, "unlock", "close", Side.LONG, 2, 4550.0,
                 "sig-finalize", "test-note", baseline=0, expected_delta=2,
                 limit=4552.0)
check("3.1 _finalize 返回 filled（2 手成交明细达标）", o.status, "filled")
check("3.2 索引已登记", b3._sig_orders.get("sig-finalize"), ["RAW-001"])
check("3.3 meta.raw_order_id 记录正确", o.meta.get("raw_order_id"), "RAW-001")
check("3.4 meta.intent 记录 unlock", o.meta.get("intent"), "unlock")


# ════════════════════════════════════════════════════════════════
# [4] engine._check_unlock_stuck G2 集成
# ════════════════════════════════════════════════════════════════
print("── [4] engine._check_unlock_stuck G2 集成 ──")


class GMockBroker(DryRunBroker):
    """可注入 trade_confirmed / real_position / cancel_pending 的 mock。"""

    def __init__(self, spec, tc_value=False, real_longs=0, real_shorts=0,
                 cp_return=0):
        super().__init__(spec, {"sim_equity": 1_000_000.0})
        self._tc_value = tc_value
        self.real_longs = real_longs
        self.real_shorts = real_shorts
        self.cp_return = cp_return
        self.cp_calls = []
        self.name = "gmock"

    def trade_confirmed(self, intent, signal_key: str = "") -> bool:
        return self._tc_value

    def real_position(self, side):
        return self.real_longs if side is Side.LONG else self.real_shorts

    def cancel_pending(self, signal_key: str = "") -> int:
        self.cp_calls.append(signal_key)
        return self.cp_return


def make_engine(tmpdir, *, broker=None):
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = 1
    cfg.risk.max_volume = 10
    cfg.risk.enforce_session = False
    cfg.sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    cfg.sizing["enabled"] = False
    cfg.sizing["fixed_volume"] = 1
    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": 5.0, "stop_loss_points": 10.0})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return GatewayEngine(cfg, broker, entry, exitp, store, ev)


def make_bar(date="2026-09-01 09:30", close=4550.0, ts=5000):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=ts, vol=0)


def make_position(side, vol, entry_price, entry_bar_seq, signal_key="TEST"):
    from tg.types import EntryMode, ExitPlan, now_cn
    if side is Side.LONG:
        tp = entry_price + 5.0
        stop = entry_price - 10.0
    else:
        tp = entry_price - 5.0
        stop = entry_price + 10.0
    return Position(
        symbol="KQ.m@CFFEX.IF", side=side, volume=vol,
        entry_price=entry_price, entry_at=now_cn(),
        entry_bar_ts=4000 + entry_bar_seq * 100,
        entry_bar_seq=entry_bar_seq,
        signal_key=signal_key, open_order_id="manual",
        exit_plan=ExitPlan(name="tp_sl", stop_price=stop, tp_price=tp,
                           params={"take_profit_points": 5.0,
                                   "stop_loss_points": 10.0}),
        entry_mode=EntryMode.OPEN_FIRST)


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

with tmp_dir() as td:
    mb = GMockBroker(InstrumentSpec(), tc_value=False, real_longs=0, cp_return=2)
    eng = make_engine(td, broker=mb)
    snap = make_position(Side.LONG, 1, 4545.0, 1, signal_key="ghost-g2").to_dict()
    eng._unlock_in_flight = {
        "signal_key": "g2-sig",
        "target_signal_key": "ghost-g2",
        "target_side": "LONG",
        "target_snapshot": snap,
        "submit_bar_ts": 0,
        "submit_bar_seq": eng.bars_seen - 5,
    }
    eng._check_unlock_stuck(make_bar())
    check("4.1 confirmed=False → 调了 cancel_pending", mb.cp_calls, ["g2-sig"])
    evs = read_events(eng, kinds={"unlock_pending_cancelled"})
    check("4.2 cancelled>0 → 写 unlock_pending_cancelled", len(evs), 1)
    check("4.3 事件带 cancelled=2", evs[0].get("cancelled") if evs else None, 2)
    check("4.4 撤单后仍走 real_position 对账（real=0 → recovered）",
          eng._unlock_in_flight, None)
    evs2 = read_events(eng, kinds={"unlock_stuck_recovered"})
    check("4.5 写 unlock_stuck_recovered", len(evs2), 1)

with tmp_dir() as td:
    mb = GMockBroker(InstrumentSpec(), tc_value=False, real_longs=0, cp_return=0)
    eng = make_engine(td, broker=mb)
    eng._unlock_in_flight = {
        "signal_key": "g2-sig-b", "target_signal_key": "t",
        "target_side": "LONG", "target_snapshot": None,
        "submit_bar_ts": 0, "submit_bar_seq": eng.bars_seen - 5,
    }
    eng._check_unlock_stuck(make_bar())
    check("4.6 cancelled=0 → 不写 unlock_pending_cancelled",
          len(read_events(eng, kinds={"unlock_pending_cancelled"})), 0)

with tmp_dir() as td:
    mb = GMockBroker(InstrumentSpec(), tc_value=True, cp_return=2)
    eng = make_engine(td, broker=mb)
    eng._unlock_in_flight = {
        "signal_key": "g2-sig-c", "target_signal_key": "t",
        "target_side": "LONG", "target_snapshot": None,
        "submit_bar_ts": 0, "submit_bar_seq": eng.bars_seen - 5,
    }
    eng._check_unlock_stuck(make_bar())
    check("4.7 confirmed=True → 不调 cancel_pending", mb.cp_calls, [])
    check("4.8 confirmed=True → 清 in-flight", eng._unlock_in_flight, None)

with tmp_dir() as td:
    # broker 无 cancel_pending（老式 broker）→ 不炸，走原 F1 逻辑
    # （BareBroker 不继承 DryRunBroker，getattr(cancel_pending) 为 None）
    class BareBroker:
        name = "bare"

        def trade_confirmed(self, intent, signal_key: str = "") -> bool:
            return False

        def real_position(self, side):
            return 0
        # 故意不定义 cancel_pending

    eng = make_engine(td, broker=BareBroker())
    eng._unlock_in_flight = {
        "signal_key": "g2-sig-d", "target_signal_key": "t",
        "target_side": "LONG", "target_snapshot": None,
        "submit_bar_ts": 0, "submit_bar_seq": eng.bars_seen - 5,
    }
    eng._check_unlock_stuck(make_bar())  # 不应抛异常
    check("4.9 无 cancel_pending → 不炸", True, True)
    check("4.10 仍走 F1 recovered", eng._unlock_in_flight, None)


# ════════════════════════════════════════════════════════════════
# [5] base / dry_run 接口默认值
# ════════════════════════════════════════════════════════════════
print("── [5] base / dry_run cancel_pending 默认值 ──")

dr = DryRunBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0})
check("5.1 dry_run.cancel_pending 继承 base → 0", dr.cancel_pending("k1"), 0)
check("5.2 dry_run.trade_confirmed 仍为 True（Phase F 不变）",
      dr.trade_confirmed(OrderIntent.UNLOCK, "k1"), True)


# ════════════════════════════════════════════════════════════════
# [6] UNLOCK 与 OPEN 归一（2026-09-05 规格拍板：解锁≈开仓，入场不追价）
# ════════════════════════════════════════════════════════════════
print("── [6] UNLOCK 归一 OPEN：单次超价 + 超时撤单不追价 ──")


class FakeLiveOrder:
    """模拟 tqsdk Order：_wait_finished / _finalize 需要的最小字段面。"""

    def __init__(self, oid, volume, fill):
        self.order_id = oid
        self.status = "FINISHED" if fill else "ALIVE"
        self.volume_left = 0 if fill else volume
        self.trade_records = ({"t1": {"volume": volume, "price": 4547.0}}
                              if fill else {})
        self.trade_price = 4547.0
        self.last_msg = "Submitted"


class FakeInsertApi(FakeApi):
    """在 FakeApi 之上加 insert_order；成交场景在 insert 瞬间同步扣减持仓。"""

    def __init__(self, long_pos=0, fill=True):
        super().__init__(long_pos=long_pos)
        self.fill = fill
        self.inserted = []

    def get_position(self, sym):
        # 不缓存：每次返回当前最新持仓（模拟 wait_update 推送后的视图）
        return FakePosition(self._long_pos, self._short_pos)

    def insert_order(self, symbol, direction, offset, volume, limit_price):
        self.inserted.append({"direction": direction, "offset": offset,
                              "volume": volume, "limit_price": limit_price})
        if self.fill:
            # UNLOCK 平多（SELL）→ 多头持仓同步减少
            if direction == "SELL":
                self._long_pos -= volume
            else:
                self._long_pos += volume
        o = FakeLiveOrder("raw-{}".format(len(self.inserted)), volume, self.fill)
        self._orders[o.order_id] = o  # get_order 可查（trade_confirmed 复核路径用）
        return o


bu = SimNowBroker(InstrumentSpec(), params={"fill_timeout_open": 0.3,
                                            "overprice_points": 0.6})
bu._conn_error = None  # 无凭据实例化会置连接错误，测试注入 api 前先清掉

# 6.1 未成交：单次报单 + 超时撤单 + rejected，不追价
api1 = FakeInsertApi(long_pos=2, fill=False)
bu._api = api1
o = bu.submit(OrderIntent.UNLOCK, Side.LONG, 2, 4550.0, "u-key-timeout")
check("6.1a 超时未成交 → rejected", o.status, "rejected")
check("6.1b 只报了 1 次单（不追价）", len(api1.inserted), 1)
check("6.1c 报文是 CLOSEYESTERDAY（平昨不变）",
      api1.inserted[0]["offset"], "CLOSEYESTERDAY")
check("6.1d 方向 SELL（平多单）", api1.inserted[0]["direction"], "SELL")
check("6.1e 超时后撤单被调用", len(api1.cancelled), 1)

# 6.2 立即成交：单次成交、不撤单、成交价来自 trade_records
api2 = FakeInsertApi(long_pos=2, fill=True)
bu._api = api2
o = bu.submit(OrderIntent.UNLOCK, Side.LONG, 2, 4550.0, "u-key-fill")
check("6.2a 立即成交 → filled", o.status, "filled")
check("6.2b 只报了 1 次单", len(api2.inserted), 1)
check("6.2c 未撤单", len(api2.cancelled), 0)
check("6.2d 成交价取 CTP 明细加权", o.filled_price, 4547.0)

# 6.3 单次路径也登记 signal_key→raw_order_id 索引（trade_confirmed 可复核）
check("6.3a 索引已登记", bu._sig_orders.get("u-key-fill"), ["raw-1"])
check("6.3b trade_confirmed 对单次路径成交 → True",
      bu.trade_confirmed(OrderIntent.UNLOCK, "u-key-fill"), True)

# ════════════════════════════════════════════════════════════════
print("")
print("P17 Phase G：{} 通过 / {} 失败".format(_PASS, _FAIL))
if _FAIL:
    sys.exit(1)
