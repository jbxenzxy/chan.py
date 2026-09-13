# -*- coding: utf-8 -*-
"""
P51 平今开关（Phase 10 · D6）契约（2026-09-14）
===============================================
背景（阻塞点 3 · D6，文档 §5.4）
--------------------------------
对"平今免收/平今便宜"的品种（如沪金 AU 平今免收、沪银 AG 平今=平昨费率、
部分农产品、原油 SC），"锁仓再跨日平"反而更贵（多一次开仓费 + 一次跨日平仓费）。
Phase 10 落地品种开关 `prefer_lock_over_closetoday`（默认 True = 锁仓优先）：
为 False 时转移 ④（今仓离场）改走 **CLOSE_TODAY 直接平今** → 今仓清零直接回
空仓态，不再进锁仓态 —— 对这类品种，状态机退化为「空仓态 + 运行态」两态
（见文档「账户三态（空仓-运行-锁仓）.html」尾部注释）。

硬约束（A4 / 全交易所安全性）：
  · CLOSETODAY 平今指令**仅上期所（SHFE）/ 上期能源（INE）**可用，其余四家
    （CFFEX/DCE/CZCE/GFEX）传平今会直接报错 —— 由 `InstrumentSpec.supports_close_today`
    守卫，转移④ 分支条件 + `_pre_trade_check` 校验链**双处**消费；
  · 平今目标恒为**今仓**（`_pre_trade_check` 断言 `close_today_target_is_yesterday`）；
  · 开关缺省（无品种档案）/ 交易所不支持 → 走锁仓，保守侧（宁可多花一次开仓费，
    不生成一张会被拒的平今单）。

覆盖
--------------------------------------------------------------------------
  [1] `supports_close_today`：SHFE/INE → True；其余四家 + "" → False
  [2] 品种档案开关值：AU/AG = False（平今免收/便宜）；IF/IH/IC/IM/CU/TA = 默认 True
  [3] 转移 ④ 三分支（_decide_exit）：
        a. 开关 True（默认，IF）→ OPEN 反向开仓锁仓
        b. 开关 False 但交易所非 SHFE/INE → OPEN（保守侧）
        c. 开关 False + SHFE（AU）→ CLOSE_TODAY / target=今仓 / transition=4
        d. 无品种档案 → True 保守侧 → OPEN
  [4] `_pre_trade_check` 对 CLOSE_TODAY 的校验链：
        a. 非 SHFE/INE → "close_today_not_supported"
        b. 今仓目标 + SHFE → 通过（None）
        c. 昨仓目标 + SHFE → "close_today_target_is_yesterday"
        d. 量超过目标手数 → "close_volume_exceeds_target"
  [5] DryRun 报单：CLOSE_TODAY → offset=CLOSETODAY（meta 审计可见）
  [6] 平今成交后状态机：今仓直接回**空仓态**（不经锁仓态）—— 两态退化实证

跑法：python Trading/Test/test_p51_close_today_switch.py
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from types import SimpleNamespace

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

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.ProductProfile import (  # noqa: E402
    PRODUCT_PROFILES,
)
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import (  # noqa: E402
    AccountState, Bar, ExitPlan, OrderIntent, Position, Side, Signal,
)
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0
D1 = "2026-09-02"


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


@contextmanager
def tmp_dir(tag):
    d = tempfile.mkdtemp(prefix="tg_p51_%s_" % tag)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_bar(ts, date=D1 + " 09:40", o=4500.0, h=4510.0, l=4490.0, c=4505.0):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_sig(key, is_buy=True, price=4500.0, ts=1001, date=D1 + " 09:41"):
    return Signal(key=key, symbol="KQ.m@CFFEX.IF", freq="5m", date=date,
                  timestamp=ts, bsp_type="1" if is_buy else "2",
                  is_buy=is_buy, price=price, high=price + 10.0,
                  low=price - 10.0, fractal_low=price - 12.0,
                  fractal_high=price + 12.0)


def make_pos(side=Side.LONG, vol=2, entry_price=4500.0, key="P51",
             entry_bar_seq=1, entry_date=D1):
    return Position(
        symbol="SHFE.AU2602", side=side, volume=vol,
        entry_price=entry_price, entry_at="2026-09-01 09:00",
        entry_bar_ts=entry_bar_seq * 1000, signal_key=key, open_order_id="p51-o1",
        exit_plan=ExitPlan(name="x", stop_price=entry_price - 10.0),
        entry_bar_seq=entry_bar_seq, entry_date=entry_date)


def build_engine(tmpdir, cfg: TradingConfig, spec: InstrumentSpec,
                 tag="a"):
    return TradingEngine(
        cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        EntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def make_cfg(signal_symbol: str = "KQ.m@CFFEX.IF",
             exchange: str = ""):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["instrument"]["signal_symbol"] = signal_symbol
    base["instrument"]["exchange"] = exchange
    base["risk"]["max_volume"] = 2
    base["exit_params"].update({"use_atr": False, "min_r_points": 3.0,
                                "use_trailing": False})
    return TradingConfig.from_dict(base)


# ══════════════════════════════════════════════════════════════
print("\n[1] supports_close_today：平今指令仅 SHFE/INE 可用")
# ══════════════════════════════════════════════════════════════
for ex, want in (("SHFE", True), ("INE", True),
                 ("CFFEX", False), ("DCE", False), ("CZCE", False),
                 ("GFEX", False), ("", False), ("shfe", True)):
    check("exchange={!r} → {}".format(ex, want),
          InstrumentSpec(exchange=ex).supports_close_today, want)


# ══════════════════════════════════════════════════════════════
print("\n[2] 品种档案开关：AU/AG 已关（平今免收/便宜），其余默认 True")
# ══════════════════════════════════════════════════════════════
check("[2a] AU 开关 = False", PRODUCT_PROFILES["AU"].prefer_lock_over_closetoday, False)
check("[2b] AG 开关 = False", PRODUCT_PROFILES["AG"].prefer_lock_over_closetoday, False)
for p in ("IF", "IH", "IC", "IM", "CU", "TA"):
    check("[2c] {} 开关 = 默认 True".format(p),
          PRODUCT_PROFILES[p].prefer_lock_over_closetoday, True)


# ══════════════════════════════════════════════════════════════
print("\n[3] 转移 ④ 三分支 + 保守侧（_decide_exit）")
# ══════════════════════════════════════════════════════════════
# 3a：默认开关 True（IF 档案 + CFFEX）→ OPEN 锁仓
with tmp_dir("t3a") as tmp:
    eng = build_engine(tmp, make_cfg(), InstrumentSpec())
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1))
    act = eng._decide_exit(eng.last_bar)
    check("[3a] 开关 True（IF）→ ④ OPEN 反向锁仓",
          (act.transition, act.intent.value, act.side, act.is_exit),
          (4, "open", Side.SHORT, True))
    check("[3a2] target=None（锁仓不指定被平仓单）", act.target, None)

# 3b：开关 False 但交易所非 SHFE/INE → 保守侧 OPEN
with tmp_dir("t3b") as tmp:
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="CFFEX")
    eng = build_engine(tmp, cfg, InstrumentSpec(exchange="CFFEX"), "b")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1))
    act = eng._decide_exit(eng.last_bar)
    check("[3b] 开关 False + 非 SHFE/INE → 仍走 OPEN 锁仓（保守侧）",
          (act.intent.value, act.transition), ("open", 4))

# 3c：开关 False + SHFE（AU）→ CLOSE_TODAY 平今
with tmp_dir("t3c") as tmp:
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    eng = build_engine(tmp, cfg, InstrumentSpec(exchange="SHFE"), "c")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1, vol=3))
    act = eng._decide_exit(eng.last_bar)
    check("[3c] 开关 False + SHFE → ④ CLOSE_TODAY 平今",
          (act.transition, act.intent.value, act.is_exit),
          (4, "close_today", True))
    check("[3c2] 平今方向 = 净敞口方向（LONG 仓 → 平多）", act.side, Side.LONG)
    check("[3c3] 平今目标 = 今仓那一笔", act.target.entry_date >= D1, True)
    check("[3c4] 平今量 = min(|净敞口|, 今仓目标手数) = 3",
          act.volume, min(3, 3))

# 3d：无品种档案 → 开关取 True 保守侧 → OPEN
#   注意：白名单硬约束（_restore）拒绝对未知品种（如 ZZ）构造引擎，实盘启动时
#   "无档案"本身不会出现 → 这里用 AU 引擎 + 覆写 cfg 模拟"配置注入缺档"
#   （引擎仅经 _prefer_lock_over_closetoday 读取 cfg.product_profile，其余不受影响）。
with tmp_dir("t3d") as tmp:
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    eng = build_engine(tmp, cfg, InstrumentSpec(exchange="SHFE"), "d")
    eng.cfg = SimpleNamespace(product_profile=None)
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1))
    check("[3d] 无品种档案 → _prefer_lock_over_closetoday=True（保守侧）",
          eng._prefer_lock_over_closetoday(), True)
    act = eng._decide_exit(eng.last_bar)
    check("[3d2] 无档案 → 仍走 OPEN 锁仓（宁可多花一次开仓费，不生成平今单）",
          (act.intent.value, act.transition), ("open", 4))


# ══════════════════════════════════════════════════════════════
print("\n[4] _pre_trade_check 对 CLOSE_TODAY 的校验链")
# ══════════════════════════════════════════════════════════════
today_target = SimpleNamespace(entry_date=D1, volume=2, side=Side.LONG,
                               signal_key="X|buy|1")
past_target = SimpleNamespace(entry_date="2026-09-01", volume=2,
                              side=Side.LONG, signal_key="X|buy|1")

# 4a：非 SHFE/INE → 兜底拒单
with tmp_dir("t4a") as tmp:
    eng = build_engine(tmp, make_cfg(), InstrumentSpec(exchange="CFFEX"), "a")
    act = _Action(intent=OrderIntent.CLOSE_TODAY, side=Side.LONG, volume=2,
                  target=today_target, is_exit=True, transition=4)
    check("[4a] CLOSE_TODAY + 非 SHFE/INE → 'close_today_not_supported'",
          eng._pre_trade_check(act, D1, None), "close_today_not_supported")

# 4b/4c/4d：SHFE 引擎
with tmp_dir("t4b") as tmp:
    eng = build_engine(tmp, make_cfg("KQ.m@SHFE.AU", exchange="SHFE"),
                       InstrumentSpec(exchange="SHFE"), "b")
    act_ok = _Action(intent=OrderIntent.CLOSE_TODAY, side=Side.LONG, volume=2,
                     target=today_target, is_exit=True, transition=4)
    check("[4b] CLOSE_TODAY + SHFE + 今仓目标 → 通过（None）",
          eng._pre_trade_check(act_ok, D1, None), None)
    act_past = _Action(intent=OrderIntent.CLOSE_TODAY, side=Side.LONG, volume=2,
                       target=past_target, is_exit=True, transition=4)
    check("[4c] CLOSE_TODAY 目标=昨仓 → 'close_today_target_is_yesterday'",
          eng._pre_trade_check(act_past, D1, None),
          "close_today_target_is_yesterday")
    act_big = _Action(intent=OrderIntent.CLOSE_TODAY, side=Side.LONG, volume=5,
                      target=today_target, is_exit=True, transition=4)
    check("[4d] 平今量 > 目标手数 → 'close_volume_exceeds_target'",
          eng._pre_trade_check(act_big, D1, None), "close_volume_exceeds_target")
    # 对称：部分平仓也不允许（PositionBook 无减仓 API）
    act_small = _Action(intent=OrderIntent.CLOSE_TODAY, side=Side.LONG, volume=1,
                        target=today_target, is_exit=True, transition=4)
    check("[4e] 平今量 < 目标手数 → 'close_volume_below_target'",
          eng._pre_trade_check(act_small, D1, None), "close_volume_below_target")


# ══════════════════════════════════════════════════════════════
print("\n[5] DryRun 报单：CLOSE_TODAY → offset=CLOSETODAY（meta 审计可见）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t5") as tmp:
    spec = InstrumentSpec(exchange="SHFE")
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    o = broker.submit(OrderIntent.CLOSE_TODAY, Side.LONG, 2, 4500.0,
                      "p51|t5", is_exit=True)
    check("[5a] status=filled（dry_run 立即成交）", o.status, "filled")
    check("[5b] meta.offset = CLOSETODAY", o.meta.get("offset"), "CLOSETODAY")
    check("[5c] meta.intent = close_today", o.meta.get("intent"), "close_today")
    check("[5d] 平今按平仓让价方向（sell 向）", o.side, Side.LONG)


# ══════════════════════════════════════════════════════════════
print("\n[6] 平今成交后状态机：今仓直接回**空仓态**（不经锁仓态）—— 两态退化实证")
# ══════════════════════════════════════════════════════════════
# 完全复刻 p35 的触发剧本（价格/配置），只把品种换成 AU + SHFE：
#   同一交易日 D1 内：先开仓（今仓），同根不利 K 线触发 L1 离场。
with tmp_dir("t6") as tmp:
    P0 = 4520.0
    P_EXIT = 4110.0
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    eng = build_engine(tmp, cfg, InstrumentSpec(exchange="SHFE"), "a")
    eng.on_bar(make_bar(1000, D1 + " 09:40", P0, P0 + 10, P0 - 10, P0))
    eng.on_signal(make_sig("P51|buy|1", is_buy=True, price=P0))
    check("[6a] 开仓后 RUNNING", eng.account_state(), AccountState.RUNNING)
    eng.on_bar(make_bar(2000, D1 + " 14:55", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    check("[6b] 平今成交后净敞口归 0 → 回**空仓态**（非锁仓态）",
          eng.account_state(), AccountState.FLAT)
    intents = [o.meta.get("intent") for o in eng.broker.orders]
    check("[6c] 离场那笔是 intent=close_today（④ 平今，非反向开仓锁仓）",
          "close_today" in intents, True)
    check("[6d] 全程恰好两笔：open（开仓）+ close_today（平今），"
          "无反向开仓锁仓单", intents, ["open", "close_today"])


print("\n" + "=" * 62)
print("P51 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
sys.exit(1 if _FAIL else 0)
