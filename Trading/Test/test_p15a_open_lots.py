# -*- coding: utf-8 -*-
"""
P15a 一笔报单开仓测试（2026-09-08 仓位管理整体删除后）
===================================================
背景
    重构后开仓模型彻底归一：
      · 一个信号 = 一笔报单 = 一笔持仓（Position）
      · 每个买卖点只开一笔，一笔挂 N 手（FOK，全成或全撤），
        N = 风控层 cfg.risk.max_volume。仓位管理 PositionSizing/SizingConfig
        已整体删除，不再有 fixed_volume / sizer 定档。
      · 开仓手数不再有 20 手上限截断，也没有 over_exchange_limit 拒单；
        配 max_volume=25 就真开 25 手。
      · 同向持仓笔数已达 cfg.risk.max_open_positions → 静默跳过 open_silenced
      · 旧术语 split_positions / size_positions / #idx 分仓机制已全部删除；
        unlock_no_new_open 已从 SizingConfig 迁入 RiskConfig（cfg.risk.unlock_no_new_open）

硬性要求（本测试锁死）
    ① 术语纪律：config 无 sizing/split 键；RiskConfig 仅保留
       max_volume(默认2) / max_open_positions(默认1) / unlock_no_new_open(默认True)
    ② _open_position 直接调：
        · volume=1 → broker 1 单、簿 1 笔持仓、signal_key 无后缀
        · volume=5 → broker 1 单 5 手、簿 1 笔 5 手（一笔挂 N 手）
        · volume=0 → zero_volume 拒单
        · volume=21 → 一笔挂 21 手（无 20 手上限拒单）、signal_action=opened
        · 拒单 → rejected、state IDLE
        · 成交 → opened、state IN_TRADE、exit_plan 独立
    ③ max_open_positions 静默语义：已满 → open_silenced，不开不报错
    ④ 无分仓残留：簿内不会出现 #idx 后缀 signal_key
    ⑤ on_signal 集成：cfg.risk.max_volume=N → 一笔挂 N 手；N=25 真开 25 手

不需要真实 tqsdk / 网络；纯单测 + RejectDryBroker mock 测拒单路径。
跑法：python tests/test_p15a_open_lots.py
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
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p15a_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


from Trading import Broker  # noqa: E402  注册 dry_run
import json  # noqa: E402
from Trading.Broker.Base import OrderIntent  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    Bar, EngineState, Side, Signal,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r} expected={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_truthy(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类，可指定拒单次数。0=全过、1=首笔拒、-1=全拒。"""
    def __init__(self, spec, params=None, *, reject_first_n=0):
        super().__init__(spec, params)
        self.reject_first_n = reject_first_n
        self._calls = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note=""):
        self._calls += 1
        if self.reject_first_n == -1 or self._calls <= self.reject_first_n:
            from Trading.Infra.Types import Order
            o = Order(
                order_id="reject-{:06d}".format(self._calls),
                signal_key=signal_key, symbol=self.spec.trade_symbol,
                side=side, action="open", volume=int(volume),
                price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note)


def make_engine(tmpdir, *, max_open_positions=1, max_volume=1, broker=None,
                unlock_no_new_open=False):
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = max_open_positions
    cfg.risk.max_volume = max_volume
    # unlock_no_new_open：自 SizingConfig 迁入 RiskConfig，现读 cfg.risk
    cfg.risk.unlock_no_new_open = unlock_no_new_open

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def read_event_kinds(eng, tail_n=200):
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-tail_n:]:
            try:
                out.append(json.loads(line).get("kind"))
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return out


def make_sig(key="P15A-TEST|0|0", is_buy=True, price=4550.0, low=4540.0, high=4560.0):
    return Signal(
        key=key, symbol="KQ.m@CFFEX.IF", freq="5m",
        date="2026-09-01 09:30", timestamp=4000,
        bsp_type="B" if is_buy else "S", is_buy=is_buy,
        price=price, high=high, low=low, extra={})


def make_bar(date="2026-09-01 09:30", close=4550.0):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=4000, vol=0)


# ════════════════════════════════════════════════════════════════
# [1] 术语纪律：仓位管理（PositionSizing/split）已彻底删除
# ════════════════════════════════════════════════════════════════
print("\n[1] 术语纪律：仓位管理（PositionSizing/split）已删除")
check("config 无 sizing 键（PositionSizing 整体删除）", "sizing" in DEFAULT_CONFIG, False)
check("risk 保留 unlock_no_new_open（自 SizingConfig 迁入）",
      (DEFAULT_CONFIG.get("risk") or {}).get("unlock_no_new_open"), True)
check("config.broker_params 无 open_advanced 键",
      "open_advanced" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("config.broker_params 无 overprice_points_fok 键",
      "overprice_points_fok" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("超价合并为单参数 overprice_points=1.0",
      (DEFAULT_CONFIG.get("broker_params") or {}).get("overprice_points"), 1.0)


# ════════════════════════════════════════════════════════════════
# [2] _open_position：一笔报单挂 N 手
# ════════════════════════════════════════════════════════════════
print("\n[2] _open_position：一笔报单挂 N 手")
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-1")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 1)
    check("volume=1：broker 1 单", len(eng.broker.orders), 1)
    check("volume=1：簿 1 笔持仓", len(eng.positions), 1)
    check("volume=1：该笔 1 手", eng.positions.positions[0].volume, 1)
    check("volume=1：signal_key 无 #idx 后缀",
          eng.positions.positions[0].signal_key, "P15A-2-1")
    check("volume=1：state=IN_TRADE", eng._state, EngineState.IN_TRADE)
    check("volume=1：signal_action=opened", eng.store.signal_action(sig.key), "opened")

with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-5")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 5)
    check("volume=5：broker 只有 1 单（一笔挂 5 手）", len(eng.broker.orders), 1)
    check("volume=5：该单 5 手", eng.broker.orders[0].volume, 5)
    check("volume=5：簿 1 笔持仓", len(eng.positions), 1)
    check("volume=5：该笔 5 手", eng.positions.positions[0].volume, 5)
    check("volume=5：exit_plan 已生成",
          eng.positions.positions[0].exit_plan is not None, True)
    check("volume=5：signal_key 无 #idx", eng.positions.positions[0].signal_key, "P15A-2-5")

# 3) volume=0 → 拒单
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-0")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 0)
    check("volume=0：零报单", len(eng.broker.orders), 0)
    check("volume=0：signal_action=rejected", eng.store.signal_action(sig.key), "rejected")
    kinds = read_event_kinds(eng)
    check("volume=0：写 order_rejected", "order_rejected" in kinds, True)

# 4) volume=21 → 无 20 手上限拒单（over_exchange_limit 已随 PositionSizing 删除）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, max_volume=50)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-21")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 21)
    check("volume=21：一笔报单（无 20 手上限拒单）", len(eng.broker.orders), 1)
    check("volume=21：该单 21 手", eng.broker.orders[0].volume, 21)
    check("volume=21：signal_action=opened", eng.store.signal_action(sig.key), "opened")
    check("volume=21：state=IN_TRADE", eng._state, EngineState.IN_TRADE)

# 5) volume=20 边界：正常一笔 20 手成交
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, max_volume=50)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-20")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 20)
    check("volume=20：一笔报单", len(eng.broker.orders), 1)
    check("volume=20：该单 20 手", eng.broker.orders[0].volume, 20)
    check("volume=20：簿 1 笔 20 手", eng.positions.positions[0].volume, 20)


# ════════════════════════════════════════════════════════════════
# [3] 拒单与静默语义
# ════════════════════════════════════════════════════════════════
print("\n[3] 拒单 / max_open_positions 静默")
with tmp_dir() as td:
    bk = RejectDryBroker(InstrumentSpec(), {"sim_equity": 10_000_000.0}, reject_first_n=-1)
    eng = make_engine(td, max_open_positions=3, broker=bk)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-3-r")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 3)
    check("全场拒单：簿空（无幻影持仓）", eng.positions.is_empty(), True)
    check("全场拒单：signal_action=rejected", eng.store.signal_action(sig.key), "rejected")
    check("全场拒单：state=IDLE", eng._state, EngineState.IDLE)

with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=1)
    eng.on_bar(make_bar())
    s1 = make_sig(key="P15A-3-a")
    eng.store.try_mark_signal(s1.key, "processing")
    eng._open_position(s1, Side.LONG, 2)
    check("max=1 首笔成交：簿 1 笔", len(eng.positions), 1)
    s2 = make_sig(key="P15A-3-b")
    eng.store.try_mark_signal(s2.key, "processing")
    eng._open_position(s2, Side.LONG, 2)
    check("max=1 同向已满：不开（仍 1 笔）", len(eng.positions), 1)
    check("max=1 同向已满：signal_action=open_silenced",
          eng.store.signal_action(s2.key), "open_silenced")
    kinds = read_event_kinds(eng)
    check("max=1 同向已满：写 open_silenced 事件", "open_silenced" in kinds, True)

with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=2)
    eng.on_bar(make_bar())
    for k in ("P15A-3-c", "P15A-3-d"):
        s = make_sig(key=k)
        eng.store.try_mark_signal(s.key, "processing")
        eng._open_position(s, Side.LONG, 2)
    check("max=2：两个信号各开 1 笔 → 簿 2 笔", len(eng.positions), 2)
    keys = sorted(p.signal_key for p in eng.positions.positions)
    check("max=2：signal_key 各自独立、无 #idx 后缀",
          keys, ["P15A-3-c", "P15A-3-d"])


# ════════════════════════════════════════════════════════════════
# [4] on_signal 集成：cfg.risk.max_volume=N → 一笔挂 N 手
# ════════════════════════════════════════════════════════════════
print("\n[4] on_signal 集成（cfg.risk.max_volume=N → 一笔挂 N 手）")
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, max_volume=8)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-8", is_buy=True)
    eng.on_signal(sig)
    check("max_volume=8：broker 1 单", len(eng.broker.orders), 1)
    check("max_volume=8：该单 8 手", eng.broker.orders[0].volume, 8)
    check("max_volume=8：簿 1 笔 8 手", eng.positions.positions[0].volume, 8)
    check("max_volume=8：signal_action=opened", eng.store.signal_action(sig.key), "opened")

with tmp_dir() as td:
    # max_volume=25：一笔挂 25 手，不再被 sizer.max_volume 20 手上限截断、
    #   也不再走 over_exchange_limit 拒单 —— 配多大就真开多少手。
    eng = make_engine(td, max_open_positions=3, max_volume=25)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-25", is_buy=True)
    eng.on_signal(sig)
    check("max_volume=25：一笔挂 25 手（无 20 手上限截断/拒单）", len(eng.broker.orders), 1)
    check("max_volume=25：该单 25 手", eng.broker.orders[0].volume, 25)
    check("max_volume=25：簿 1 笔 25 手", eng.positions.positions[0].volume, 25)
    check("max_volume=25：signal_action=opened", eng.store.signal_action(sig.key), "opened")


# ════════════════════════════════════════════════════════════════
# [5] 无分仓残留断言
# ════════════════════════════════════════════════════════════════
print("\n[5] 无分仓残留")
import Trading.Engine.Engine as _engine_mod  # noqa: E402
_src = open(os.path.join(_TG_ROOT, "Engine", "Engine.py"), encoding="utf-8").read()
check("engine 无 _open_positions（复数）方法", hasattr(_engine_mod.TradingEngine,
                                                  "_open_positions"), False)
check("engine 无 _book_positions 方法", hasattr(_engine_mod.TradingEngine,
                                              "_book_positions"), False)
check("engine 无 _unlock_round_entry 方法", hasattr(_engine_mod.TradingEngine,
                                                 "_unlock_round_entry"), False)
check("engine 无 _check_unlock_round 方法", hasattr(_engine_mod.TradingEngine,
                                                  "_check_unlock_round"), False)
check("engine 无 _unlock_round_settle 方法", hasattr(_engine_mod.TradingEngine,
                                                   "_unlock_round_settle"), False)
check("engine 源码无 H2 轮次残留", "unlock_round" in _src, False)
check("engine 有 _open_position（单笔）", hasattr(_engine_mod.TradingEngine,
                                              "_open_position"), True)
check("engine 有 _unlock_position（单笔解锁）", hasattr(_engine_mod.TradingEngine,
                                                   "_unlock_position"), True)


print("\n" + "=" * 60)
print("P15a 一笔报单开仓 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
