# -*- coding: utf-8 -*-
"""
P15a 一笔报单开仓测试（2026-09-06 全 FOK 重构后）
===================================================
背景
    重构后开仓模型彻底归一：
      · 一个信号 = 一笔报单 = 一笔持仓（Position）
      · 无论非仓位管理模式（fixed_volume=N）还是仓位管理模式（sizer 算出 N），
        都是一笔挂 N 手（FOK，全成或全撤），成交后簿面记 1 笔 N 手的持仓
      · 中金所限价单每次最大下单 20 手：N > 20 → 直接拒单 over_exchange_limit
        （不再有"逐笔回退"这类兜底路径）
      · 同向持仓笔数已达 cfg.risk.max_open_positions → 静默跳过 open_silenced
      · 旧术语 split_positions / size_positions / #idx 分仓机制已全部删除

硬性要求（本测试锁死）
    ① 术语纪律：config 无 split_positions/split_unlock；sizer 无 split 字段
    ② RiskGate.check_open 签名不再有 position_count/existing_same_side
    ③ _open_position 直接调：
        · volume=1 → broker 1 单、簿 1 笔持仓、signal_key 无后缀
        · volume=5 → broker 1 单 5 手、簿 1 笔 5 手（一笔挂 N 手）
        · volume=0 → zero_volume 拒单
        · volume=21 → over_exchange_limit 拒单、零报单、簿空
        · 拒单 → rejected、state IDLE
        · 成交 → opened、state IN_TRADE、exit_plan 独立
    ④ max_open_positions 静默语义：已满 → open_silenced，不开不报错
    ⑤ 无分仓残留：簿内不会出现 #idx 后缀 signal_key

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
from Trading.Config import DEFAULT_CONFIG, GatewayConfig, SizingConfig  # noqa: E402
from Trading.Engine.Engine import GatewayEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Risk.RiskGate import RiskGate  # noqa: E402
from Trading.Risk.PositionSizing import PositionSizer  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy
from Trading.Strategy.Exit import DefaultExitPolicy  # noqa: E402
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


def make_engine(tmpdir, *, max_open_positions=1, fixed_volume=1, broker=None,
                cfg_risk_max_volume=20, unlock_no_new_open=False,
                sizing_max_volume=0):
    cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_open_positions = max_open_positions
    cfg.risk.max_volume = cfg_risk_max_volume
    cfg.risk.enforce_session = False
    # 严格模式：sizing 是配置模型，覆盖走 SizingConfig（未知键会报错）
    sizing = dict(DEFAULT_CONFIG.get("sizing") or {})
    sizing.update({"enabled": False, "fixed_volume": fixed_volume,
                   "unlock_no_new_open": unlock_no_new_open})
    if sizing_max_volume:
        sizing["max_volume"] = sizing_max_volume
    cfg.sizing = SizingConfig(**sizing)

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = DefaultExitPolicy({"take_profit_points": 10.0})
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return GatewayEngine(cfg, broker, entry, exitp, store, ev)


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
# [1] 术语纪律：分仓机制已彻底删除
# ════════════════════════════════════════════════════════════════
print("\n[1] 术语纪律：分仓（split）机制已删除")
sz = PositionSizer(SizingConfig())
check("sizer 无 split_positions 字段", hasattr(sz, "split_positions"), False)
check("sizer 无 split_unlock 字段", hasattr(sz, "split_unlock"), False)
check("sizer 无 size_positions 方法", hasattr(sz, "size_positions"), False)
check("config.sizing 无 split_positions 键",
      "split_positions" in (DEFAULT_CONFIG.get("sizing") or {}), False)
check("config.sizing 无 split_unlock 键",
      "split_unlock" in (DEFAULT_CONFIG.get("sizing") or {}), False)
check("config.broker_params 无 open_advanced 键",
      "open_advanced" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("config.broker_params 无 overprice_points_fok 键",
      "overprice_points_fok" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("超价合并为单参数 overprice_points=1.0",
      (DEFAULT_CONFIG.get("broker_params") or {}).get("overprice_points"), 1.0)
check("sizer 保留 unlock_no_new_open", hasattr(sz, "unlock_no_new_open"), True)

import inspect  # noqa: E402
_sig = inspect.signature(RiskGate.check_open)
check("RiskGate.check_open 无 position_count 参数",
      "position_count" in _sig.parameters, False)
check("RiskGate.check_open 无 existing_same_side 参数",
      "existing_same_side" in _sig.parameters, False)


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

# 4) volume=21 → over_exchange_limit 拒单（中金所 20 手上限）
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, cfg_risk_max_volume=50)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-21")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 21)
    check("volume=21：零报单（超交易所上限直接拒）", len(eng.broker.orders), 0)
    check("volume=21：簿空", eng.positions.is_empty(), True)
    check("volume=21：signal_action=rejected", eng.store.signal_action(sig.key), "rejected")
    check("volume=21：state=IDLE", eng._state, EngineState.IDLE)

# 5) volume=20 边界：恰好 20 手放行
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, cfg_risk_max_volume=50)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-2-20")
    eng.store.try_mark_signal(sig.key, "processing")
    eng._open_position(sig, Side.LONG, 20)
    check("volume=20：放行（=上限）", len(eng.broker.orders), 1)
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
# [4] on_signal 集成：非仓位管理 / 仓位管理同一条报单路径
# ════════════════════════════════════════════════════════════════
print("\n[4] on_signal 集成（fixed_volume=N → 一笔挂 N 手）")
with tmp_dir() as td:
    eng = make_engine(td, max_open_positions=3, fixed_volume=8)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-8", is_buy=True)
    eng.on_signal(sig)
    check("fixed_volume=8：broker 1 单", len(eng.broker.orders), 1)
    check("fixed_volume=8：该单 8 手", eng.broker.orders[0].volume, 8)
    check("fixed_volume=8：簿 1 笔 8 手", eng.positions.positions[0].volume, 8)

with tmp_dir() as td:
    # 场景 1：fixed_volume=25 > 中金所 20 手上限，sizer.max_volume 默认 20 → 风控单笔上限拦截
    eng = make_engine(td, max_open_positions=3, fixed_volume=25, cfg_risk_max_volume=30)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-25", is_buy=True)
    eng.on_signal(sig)
    check("fixed_volume=25（>20）：零报单", len(eng.broker.orders), 0)
    check("fixed_volume=25（>20）：signal_action=risk_block（单笔上限 20 拦）",
          eng.store.signal_action(sig.key), "risk_block")
    check("fixed_volume=25（>20）：簿空", eng.positions.is_empty(), True)

with tmp_dir() as td:
    # 场景 2：sizing.max_volume 显式设 30（>20）→ 风控放行 → 引擎按交易所硬上限 20 拒单
    #         （over_exchange_limit 防御兜底，防止把截断上限设超交易所规则）
    eng = make_engine(td, max_open_positions=3, fixed_volume=25, cfg_risk_max_volume=30,
                      sizing_max_volume=30)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-25b", is_buy=True)
    eng.on_signal(sig)
    check("sizing.max_volume=30：fixed_volume=25 仍超交易所 20 手 → rejected",
          eng.store.signal_action(sig.key), "rejected")
    check("sizing.max_volume=30：零报单", len(eng.broker.orders), 0)
    check("sizing.max_volume=30：簿空", eng.positions.is_empty(), True)


# ════════════════════════════════════════════════════════════════
# [5] 无分仓残留断言
# ════════════════════════════════════════════════════════════════
print("\n[5] 无分仓残留")
import Trading.Engine.Engine as _engine_mod  # noqa: E402
_src = open(os.path.join(_TG_ROOT, "Engine", "Engine.py"), encoding="utf-8").read()
check("engine 无 _open_positions（复数）方法", hasattr(_engine_mod.GatewayEngine,
                                                  "_open_positions"), False)
check("engine 无 _book_positions 方法", hasattr(_engine_mod.GatewayEngine,
                                              "_book_positions"), False)
check("engine 无 _unlock_round_entry 方法", hasattr(_engine_mod.GatewayEngine,
                                                 "_unlock_round_entry"), False)
check("engine 无 _check_unlock_round 方法", hasattr(_engine_mod.GatewayEngine,
                                                  "_check_unlock_round"), False)
check("engine 无 _unlock_round_settle 方法", hasattr(_engine_mod.GatewayEngine,
                                                   "_unlock_round_settle"), False)
check("engine 源码无 H2 轮次残留", "unlock_round" in _src, False)
check("engine 有 _open_position（单笔）", hasattr(_engine_mod.GatewayEngine,
                                              "_open_position"), True)
check("engine 有 _unlock_position（单笔解锁）", hasattr(_engine_mod.GatewayEngine,
                                                   "_unlock_position"), True)


print("\n" + "=" * 60)
print("P15a 一笔报单开仓 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
