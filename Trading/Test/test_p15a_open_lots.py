# -*- coding: utf-8 -*-
"""
P15a 一笔报单开仓测试（2026-09-11 Phase 7 改写）
================================================
本文件原来测的是两样**已被重构删除**的东西：
  · `cfg.risk.max_open_positions`（同时持仓笔数上限）—— D2 判定删除：
    "资金是唯一闸门"，同向笔数门连同 `open_silenced` 事件一起消失；
  · `eng._open_position(sig, side, N)` 直接调 —— Phase 4 删除，开仓唯一路径改为
    `on_signal` → `_decide_action` → `_pre_trade_check` → `_execute` → `_book_open`。
  另 `RiskConfig.unlock_no_new_open` 随"解锁"概念一并删除（D17 丢弃旧键但不静默）。

新口径（本测试锁死）
    [1] 术语纪律：config 无 sizing 键；`RiskConfig` 只剩 `max_volume` 一个字段；
        两个已删键（max_open_positions / unlock_no_new_open）按 D17 丢弃 + 可观测。
    [2] 一笔报单挂 N 手：`max_volume=N` → broker **恰好 1 单 N 手**、簿 **1 笔 N 手**、
        事件里恰好 1 条 order + 1 条 open（不是 N 单，也不是 1 笔拆 N 笔）。
    [3] `max_volume` 启动期校验 1..20（配置层 fail-fast）—— 取代已删的运行期
        20 手拦截（`over_exchange_limit` 随 PositionSizing 删除）。
    [4] 拒单路径：全场拒 → 簿空 / `account_state()==FLAT` / `_state==IDLE` /
        signal_action=rejected，且**不留幻影持仓**。
    [5] 无分仓残留 + 唯一报单出口存在性（A3）。

不需要真实 tqsdk / 网络；纯单测 + RejectDryBroker mock 测拒单路径。
跑法：python Trading/Test/test_p15a_open_lots.py
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
    print("\u2717 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
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
from Trading.Config import DEFAULT_CONFIG, RiskConfig, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, EngineState, Signal,
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


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("\u2713" if ok else "\u2717") + " " + name +
          ("  -> got={!r}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


class RejectDryBroker(DryRunBroker):
    """DryRunBroker 子类，可指定拒单次数。0=全过、1=首笔拒、-1=全拒。

    2026-09-11：`submit` 签名已加到 8 参（多出 entry_date / is_exit，D12/D13），
    子类必须同步，否则 TypeError 会被引擎当成 channel 故障。
    """
    def __init__(self, spec, params=None, *, reject_first_n=0):
        super().__init__(spec, params)
        self.reject_first_n = reject_first_n
        self._calls = 0

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        self._calls += 1
        if self.reject_first_n == -1 or self._calls <= self.reject_first_n:
            from Trading.Infra.Types import Order
            o = Order(
                order_id="reject-{:06d}".format(self._calls),
                signal_key=signal_key, symbol=self.spec.trade_symbol,
                side=side,
                action="open" if intent is OrderIntent.OPEN else "close",
                volume=int(volume),
                price=0.0, req_price=float(ref_price),
                filled_price=None, status="rejected",
                created_at="2026-09-01 09:30", broker=self.name, note=note,
                meta={"intent": intent.value if hasattr(intent, "value") else str(intent),
                      "entry_date": entry_date,
                      "reject_reason": "test_reject"})
            self.orders.append(o)
            return o
        return super().submit(intent, side, volume, ref_price, signal_key, note,
                              entry_date, is_exit)


def make_engine(tmpdir, *, max_volume=2, broker=None):
    """构造引擎。

    2026-09-11：不再设 `max_open_positions` / `unlock_no_new_open`（D2/D17 已删）。
    开仓手数唯一来源 = `cfg.risk.max_volume` → `engine.lots_per_signal`。
    """
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = max_volume

    spec = InstrumentSpec()
    if broker is None:
        broker = DryRunBroker(spec, {"sim_equity": 10_000_000.0})
    entry = DefaultEntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def read_events(eng, tail_n=400):
    """读事件日志尾部 → [(kind, 整个 dict), ...]（flush 后再读，保证不漏）。"""
    eng.ev.flush()
    out = []
    try:
        with open(eng.ev.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out
    for line in lines[-tail_n:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        out.append((d.get("kind"), d))
    return out


def kinds_of(eng, tail_n=400):
    return [k for k, _d in read_events(eng, tail_n)]


def make_sig(key="P15A-TEST|0|0", is_buy=True, price=4550.0, low=4540.0, high=4560.0):
    return Signal(
        key=key, symbol="KQ.m@CFFEX.IF", freq="5m",
        date="2026-09-01 09:30", timestamp=4000,
        bsp_type="B" if is_buy else "S", is_buy=is_buy,
        price=price, high=high, low=low, extra={})


def make_bar(date="2026-09-01 09:30", close=4550.0):
    return Bar(date=date, open=close, high=close, low=close, close=close,
               timestamp=4000, vol=0)


def _mk_risk(**kw):
    """构造 RiskConfig，返回 (实例 or None, 异常串 or None)。"""
    try:
        return RiskConfig(**kw), None
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, str(e).replace("\n", " ")[:200])


# ════════════════════════════════════════════════════════════════
# [1] 术语纪律：仓位管理（PositionSizing/split）与 D2/D17 已删键
# ════════════════════════════════════════════════════════════════
print("\n[1] 术语纪律：仓位管理已删 + D2/D17 已删键")
check("[1a] config 无 sizing 键（PositionSizing 整体删除）",
      "sizing" in DEFAULT_CONFIG, False)
check("[1b] RiskConfig 只剩 max_volume 一个字段（D2 删除 max_open_positions）",
      sorted(RiskConfig.model_fields), ["max_volume"])
check("[1c] DEFAULT_CONFIG.risk 无 max_open_positions",
      "max_open_positions" in (DEFAULT_CONFIG.get("risk") or {}), False)
check("[1d] DEFAULT_CONFIG.risk 无 unlock_no_new_open",
      "unlock_no_new_open" in (DEFAULT_CONFIG.get("risk") or {}), False)
check("[1e] max_volume 默认 2",
      (DEFAULT_CONFIG.get("risk") or {}).get("max_volume"), 2)

# D17：旧键按"丢弃 + 可观测"处理（不静默、也不 fail-fast）
RiskConfig.dropped_legacy_keys.clear()
legacy_cfg, legacy_err = _mk_risk(max_volume=2, max_open_positions=3,
                                  unlock_no_new_open=True)
check("[1f] 带两个旧键的配置仍能构造（不 fail-fast）", legacy_err, None)
check("[1g] 旧键被丢弃后 max_volume 原样保留",
      (legacy_cfg.max_volume if legacy_cfg else None), 2)
check("[1h] 丢弃动作**可观测**（dropped_legacy_keys 记账）",
      sorted(set(RiskConfig.dropped_legacy_keys)),
      ["max_open_positions", "unlock_no_new_open"])
# 但 extra="forbid" 仍在：真正不认识的键必须报错（否则拼错键名会被静默吞掉）
_bogus, _bogus_err = _mk_risk(max_volume=2, max_open_position=3)
check_true("[1i] 未列入白名单的未知键仍 fail-fast（extra=forbid）",
           _bogus_err is not None and "ValidationError" in _bogus_err)

check("[1j] config.broker_params 无 open_advanced 键",
      "open_advanced" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("[1k] config.broker_params 无 overprice_points_fok 键",
      "overprice_points_fok" in (DEFAULT_CONFIG.get("broker_params") or {}), False)
check("[1l] 超价合并为单参数 overprice_points=1.0",
      (DEFAULT_CONFIG.get("broker_params") or {}).get("overprice_points"), 1.0)


# ════════════════════════════════════════════════════════════════
# [2] 一笔报单挂 N 手（唯一开仓路径：on_signal）
# ════════════════════════════════════════════════════════════════
print("\n[2] 一笔报单挂 N 手（max_volume=N → 1 单 N 手）")
for _n in (1, 2, 5, 20):
    with tmp_dir() as td:
        eng = make_engine(td, max_volume=_n)
        eng.on_bar(make_bar())
        sig = make_sig(key="P15A-2-{}".format(_n))
        eng.on_signal(sig)

        check("[2] N={}：broker 恰好 1 单（不是 N 单）".format(_n),
              len(eng.broker.orders), 1)
        check("[2] N={}：该单 {} 手".format(_n, _n), eng.broker.orders[0].volume, _n)
        check("[2] N={}：簿 1 笔（不是拆成 N 笔）".format(_n), len(eng.positions), 1)
        check("[2] N={}：该笔 {} 手".format(_n, _n),
              eng.positions.positions[0].volume, _n)
        check("[2] N={}：signal_key 无 #idx 后缀".format(_n),
              eng.positions.positions[0].signal_key, sig.key)
        check("[2] N={}：lots_per_signal 就取自 cfg.risk.max_volume".format(_n),
              eng.lots_per_signal, _n)
        check("[2] N={}：signal_action=opened".format(_n),
              eng.store.signal_action(sig.key), "opened")
        check("[2] N={}：account_state=RUNNING".format(_n),
              eng.account_state(), AccountState.RUNNING)
        check("[2] N={}：净敞口 = {}".format(_n, _n),
              eng.positions.net_volume(), _n)
        check("[2] N={}：_state=IN_TRADE".format(_n), eng._state, EngineState.IN_TRADE)

        # 事件账：1 条 order + 1 条 open（"一笔报单"在事件层同样成立）
        evs = read_events(eng)
        ks = [k for k, _d in evs]
        check("[2] N={}：事件里恰好 1 条 order".format(_n), ks.count("order"), 1)
        check("[2] N={}：事件里恰好 1 条 open".format(_n), ks.count("open"), 1)
        _o = [d for k, d in evs if k == "order"][0]
        check("[2] N={}：order.volume={}".format(_n, _n), _o.get("volume"), _n)
        check("[2] N={}：order.transition=1（空仓开新仓）".format(_n),
              _o.get("transition"), 1)

# 同向第二信号：D2 删掉的是"笔数静默门"，但**规则 ⑶（运行态不响应信号）仍在**
#   —— 已持仓（net≠0 → RUNNING）时第二信号被整条忽略，不是被笔数上限挡掉。
#   两者的可观测区别：旧口径写 open_silenced，新口径写 signal_skip/running_ignore_signal。
with tmp_dir() as td:
    eng = make_engine(td, max_volume=2)
    eng.on_bar(make_bar())
    s1 = make_sig(key="P15A-2-c")
    eng.on_signal(s1)
    check("[2m] 首信号：簿 1 笔 2 手", len(eng.positions), 1)
    s2 = make_sig(key="P15A-2-d")
    eng.on_signal(s2)
    check("[2n] 运行态第二信号（规则 ⑶）→ 不开新仓（仍 1 笔）",
          len(eng.positions), 1)
    check("[2o] 运行态第二信号 → 净敞口不变（仍 2）",
          eng.positions.net_volume(), 2)
    check("[2p] 运行态第二信号 → signal_action=skip",
          eng.store.signal_action(s2.key), "skip")
    check("[2q] 运行态第二信号 → 簿内 signal_key 仍只有第一笔",
          sorted(p.signal_key for p in eng.positions.positions), ["P15A-2-c"])
    _ks2 = kinds_of(eng)
    check_true("[2r] 运行态第二信号 → 写 signal_skip 事件",
               "signal_skip" in _ks2)
    _skip = [d for k, d in read_events(eng) if k == "signal_skip"]
    check("[2s] 跳过原因 = running_ignore_signal（不是笔数上限）",
          (_skip[-1].get("reason") if _skip else None), "running_ignore_signal")
    check_true("[2t] 事件里无 open_silenced（D2 已删该事件）",
               "open_silenced" not in _ks2)


# ════════════════════════════════════════════════════════════════
# [3] max_volume 启动期校验 1..20
# ════════════════════════════════════════════════════════════════
print("\n[3] max_volume 启动期校验 1..20（配置层 fail-fast）")
_r1, _e1 = _mk_risk(max_volume=1)
check("[3a] max_volume=1 合法（下界）", _e1, None)
_r20, _e20 = _mk_risk(max_volume=20)
check("[3b] max_volume=20 合法（上界 = 中金所限价单单笔上限）", _e20, None)
_r0, _e0 = _mk_risk(max_volume=0)
check_true("[3c] max_volume=0 → 构造期报错", _e0 is not None)
_r21, _e21 = _mk_risk(max_volume=21)
check_true("[3d] max_volume=21 → 构造期报错（取代已删的运行期拦截）", _e21 is not None)
check_true("[3e] 报错信息点明 1..20 区间",
           _e21 is not None and "1..20" in _e21)
# 诚实记录：pydantic 未开 validate_assignment，**构造之后**的属性赋值绕过校验。
# 生产路径恒走 `TradingConfig.from_dict`（即构造期），故不构成实际风险；
# 这里钉住现状，避免"以为赋值也会被拦"的错觉。
_c = TradingConfig.from_dict(DEFAULT_CONFIG)
try:
    _c.risk.max_volume = 99
    _post = _c.risk.max_volume
except Exception:
    _post = "RAISED"
check("[3f] 现状记录：属性赋值不经校验（仅构造期生效）", _post, 99)


# ════════════════════════════════════════════════════════════════
# [4] 拒单路径（不留幻影持仓）
# ════════════════════════════════════════════════════════════════
print("\n[4] 拒单路径")
with tmp_dir() as td:
    bk = RejectDryBroker(InstrumentSpec(), {"sim_equity": 10_000_000.0},
                         reject_first_n=-1)
    eng = make_engine(td, max_volume=3, broker=bk)
    eng.on_bar(make_bar())
    sig = make_sig(key="P15A-4-r")
    eng.on_signal(sig)
    check("[4a] 全场拒单：簿空（无幻影持仓）", eng.positions.is_empty(), True)
    check("[4b] 全场拒单：净敞口 0", eng.positions.net_volume(), 0)
    check("[4c] 全场拒单：account_state=FLAT", eng.account_state(), AccountState.FLAT)
    check("[4d] 全场拒单：_state=IDLE", eng._state, EngineState.IDLE)
    check("[4e] 全场拒单：signal_action=rejected",
          eng.store.signal_action(sig.key), "rejected")
    _ks = kinds_of(eng)
    check_true("[4f] 全场拒单：写 order_rejected 事件", "order_rejected" in _ks)
    check("[4g] 全场拒单：不写 open 事件", _ks.count("open"), 0)

# 首笔拒 + 第二笔过：拒单不污染后续
with tmp_dir() as td:
    bk = RejectDryBroker(InstrumentSpec(), {"sim_equity": 10_000_000.0},
                         reject_first_n=1)
    eng = make_engine(td, max_volume=2, broker=bk)
    eng.on_bar(make_bar())
    s1 = make_sig(key="P15A-4-s1")
    eng.on_signal(s1)
    check("[4h] 首笔拒：簿空", eng.positions.is_empty(), True)
    s2 = make_sig(key="P15A-4-s2")
    eng.on_signal(s2)
    check("[4i] 第二笔过：簿 1 笔 2 手", len(eng.positions), 1)
    check("[4j] 第二笔过：净敞口 2", eng.positions.net_volume(), 2)
    check("[4k] 第二笔过：signal_action=opened",
          eng.store.signal_action(s2.key), "opened")


# ════════════════════════════════════════════════════════════════
# [5] 无分仓残留 + 唯一报单出口（A3）
# ════════════════════════════════════════════════════════════════
print("\n[5] 无分仓残留 + 唯一报单出口")
import Trading.Engine.Engine as _engine_mod  # noqa: E402
_src = open(os.path.join(_TG_ROOT, "Engine", "Engine.py"), encoding="utf-8").read()
_TE = _engine_mod.TradingEngine
check("[5a] engine 无 _open_position（Phase 4 已删，开仓唯一路径走 _execute）",
      hasattr(_TE, "_open_position"), False)
check("[5b] engine 无 _close_position（Phase 4 已删）",
      hasattr(_TE, "_close_position"), False)
check("[5c] engine 无 _open_positions（复数，E3.2 批次开仓已删）",
      hasattr(_TE, "_open_positions"), False)
check("[5d] engine 无 _book_positions 方法", hasattr(_TE, "_book_positions"), False)
check("[5e] engine 无 _unlock_position（「解锁」概念已删）",
      hasattr(_TE, "_unlock_position"), False)
for _m in ("_unlock_round_entry", "_check_unlock_round", "_unlock_round_settle"):
    check("[5f] engine 无 {} 方法".format(_m), hasattr(_TE, _m), False)
check("[5g] engine 源码无 unlock_round 残留", "unlock_round" in _src, False)
check("[5h] engine 源码无 open_silenced 残留", "open_silenced" in _src, False)

# A3：唯一报单出口 + 决策/落账分层
for _m in ("_execute", "_book_open", "_book_close",
           "_decide_action", "_decide_exit", "_pre_trade_check", "account_state"):
    check("[5i] engine 有 {}".format(_m), hasattr(_TE, _m), True)
check("[5j] engine 有 _sync_state（_state 派生镜像刷新点）",
      hasattr(_TE, "_sync_state"), True)


print("\n" + "=" * 60)
print("P15a 一笔报单开仓 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
