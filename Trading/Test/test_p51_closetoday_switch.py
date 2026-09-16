# -*- coding: utf-8 -*-
"""
P51 平今开关（Phase 10 · D6）契约（2026-09-14）
===============================================
背景（阻塞点 3 · D6，文档 §5.4）
--------------------------------
对"平今免收/平今便宜"的品种（如沪金 AU 平今免收、沪银 AG 平今=平昨费率、
部分农产品、原油 SC），"锁仓再跨日平"反而更贵（多一次开仓费 + 一次跨日平仓费）。
Phase 10 落地品种平今取舍（P-A · 2026-09-15 起为**单源派生**）：
为 False 时转移 ④（今仓离场）改走 **CLOSETODAY 直接平今** → 今仓清零直接回
空仓态，不再进锁仓态 —— 对这类品种，状态机退化为「空仓态 + 运行态」两态
（见文档「账户三态（空仓-运行-锁仓）.html」尾部注释）。

硬约束（2026-09-16 起）
--------------------------------
  · 走不走平今 = **品种执行策略表第 1 列**（`EXEC_POLICY[code].close_mode`），
    由用户按费率自己算定后填表 —— 代码不从费率推导、也不看交易所名字
    （原「按交易所能力守卫的闸门」与「按费率 3× 口径派生的开关」**均已删除**）；
    转移④ 分支条件 + `_pre_trade_check` 校验链**双处**消费同一张表；
  · 平今目标恒为**今仓**（`_pre_trade_check` 断言 `closetoday_target_is_yesterday`）；
  · 无品种档案 → 走锁仓，保守侧（宁可多花一次开仓费，不生成一张会被拒的平今单）。

覆盖
--------------------------------------------------------------------------
  [1] 品种执行策略表：第 1 列（今仓离场 offset）—— 8 行取值 + 与档案同源
  [2] 硬断言：`FAK ⟹ 一笔 1 手`；非法 close_mode / order_advanced / N 构造期拒绝
  [3] 转移 ④ **两路平铺**（_decide_exit）：
        a. 表 = R-OPEN（IF）→ OPEN 反向开仓锁仓
        b. 表 = CLOSETODAY（AU）→ CLOSETODAY / target=当日仓 / transition=4
        c. 无品种档案 → 保守侧 OPEN
        d. 两态机前提 + **运行时守卫**：CLOSETODAY 品种开仓后仓单数 = 1；
           注入第 2 笔仓单 → SEVERE `two_state_invariant_broken`（守卫必须咬人）
  [4] `_pre_trade_check` 对 CLOSETODAY 的校验链（判据 = 表第 1 列）：
        a. 表第 1 列 = R-OPEN 却发 CLOSETODAY → "closetoday_not_supported"
        b. 今仓目标 + 表 = CLOSETODAY → 通过（None）
        c. 昨仓目标 → "closetoday_target_is_yesterday"
        d. 量超过目标手数 → "close_volume_exceeds_target"（不足 → below）
  [5] DryRun 报单：CLOSETODAY → offset=CLOSETODAY（meta 审计可见）
  [6] 平今成交后状态机：今仓直接回**空仓态**（不经锁仓态）—— 两态退化实证

跑法：python Trading/Test/test_p51_closetoday_switch.py
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
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import (EXEC_POLICY,  # noqa: E402
                                   PRODUCT_PROFILES, ExecPolicy)

from dataclasses import replace as _dc_replace  # noqa: E402

_IF = PRODUCT_PROFILES["IF"]


def _prod(ex):
    """现场档案：以 IF 档案为模板换交易所（P-B：exchange 真值源 = 品种档案）。"""
    return _dc_replace(_IF, exchange=ex)
from Trading.Infra.Product import PRODUCT_PROFILES
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.Records import AccountState, Bar, ExitPlan, OrderIntent, Position, Side, Signal
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


def build_engine(tmpdir, cfg: TradingConfig, spec: "Instrument",
                 tag="a"):
    return TradingEngine(
        cfg, DryRunBroker(spec, {"sim_equity": 1_000_000.0}),
        EntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmpdir, "state_%s.db" % tag)),
        EventLog(os.path.join(tmpdir, "events_%s.jsonl" % tag),
                 echo=False, echo_kinds=None))


def make_cfg(signal_symbol: str = "KQ.m@CFFEX.IF",
             exchange: str = ""):
    """exchange 参数保留只为调用点兼容：P-B 起交易所归品种档案（frozen 配置
    不携带该键，写入会触发 _check_removed_keys 显式报错）；交易所差异由
    build_engine 的 Instrument 档案参数表达。"""
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["instrument"]["signal_symbol"] = signal_symbol
    # 手数 = 品种执行策略表第 3 列（IF → 2 手）；原 risk.max_volume 已于 2026-09-16 删除
    base["exit_params"].update({"use_atr": False,
                                "use_trailing": False})
    return TradingConfig.from_dict(base)


print("\n[1] 品种执行策略表：今仓离场口径 = 表第 1 列（不看交易所名字、不算费率）")
check("[1a] AU/AG/CU → CLOSETODAY（用户按费率算定：平今更省）",
      [EXEC_POLICY[k].close_mode for k in ("AU", "AG", "CU")],
      ["CLOSETODAY", "CLOSETODAY", "CLOSETODAY"])
check("[1b] IF/IH/IC/IM/TA → R-OPEN（反向开仓锁仓）",
      [EXEC_POLICY[k].close_mode for k in ("IF", "IH", "IC", "IM", "TA")],
      ["R-OPEN"] * 5)
check("[1c] ★ 交易所名字不参与判断：TA 档案改 exchange='SHFE' → 仍按表 = CLOSE",
      _dc_replace(PRODUCT_PROFILES["TA"], exchange="SHFE").exec_policy.close_mode,
      "R-OPEN")
check("[1d] 表与档案同源（Product.exec_policy 就是 EXEC_POLICY 那一行）",
      all(PRODUCT_PROFILES[k].exec_policy is EXEC_POLICY[k] for k in EXEC_POLICY),
      True)
check("[1e] 8 行齐全（与 PRODUCT_PROFILES 同键）",
      sorted(EXEC_POLICY) == sorted(PRODUCT_PROFILES), True)


print("\n[2] 硬断言：FAK ⟹ 一笔挂 1 手（构造期拒绝，不留静默默认值）")


def _ctor_rejects(close_mode, adv, n):
    try:
        ExecPolicy(close_mode, adv, n)
        return False
    except ValueError:
        return True


check("[2a] 表里 FAK 的品种 = 只有 TA",
      sorted(k for k in EXEC_POLICY if EXEC_POLICY[k].order_advanced == "FAK"),
      ["TA"])
check("[2b] TA 一笔 1 手", EXEC_POLICY["TA"].lots_per_order, 1)
check("[2c] ★ FAK + N=2 → 构造期 ValueError（硬断言，不是警告）",
      _ctor_rejects("R-OPEN", "FAK", 2), True)
check("[2d] FAK + N=1 → 合法", ExecPolicy("R-OPEN", "FAK", 1).lots_per_order, 1)
check("[2e] 非法 close_mode / order_advanced / N=0 同样构造期拒绝",
      [_ctor_rejects("CLOSEX", "FOK", 2), _ctor_rejects("R-OPEN", "FOKX", 2),
       _ctor_rejects("R-OPEN", "FOK", 0)], [True, True, True])
check("[2f] 8 品种全部 N ≥ 1",
      all(EXEC_POLICY[k].lots_per_order >= 1 for k in EXEC_POLICY), True)


print("\n[3] 转移 ④ 两分支（_decide_exit）：按表第 1 列平铺，无兜底分支")
with tmp_dir("t3a") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF))
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1))
    act = eng._decide_exit(eng.last_bar)
    check("[3a] IF（表=R-OPEN）→ ④ OPEN 反向锁仓",
          (act.transition, act.intent.value, act.side, act.is_exit),
          (4, "open", Side.SHORT, True))
    check("[3a2] target=None（锁仓不指定被平仓单）", act.target, None)

with tmp_dir("t3c") as tmp:
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    eng = build_engine(tmp, cfg, Instrument(None, PRODUCT_PROFILES["AU"]), "c")
    eng.on_bar(make_bar(1000))
    eng.positions.add(make_pos(entry_date=D1, vol=3))
    act = eng._decide_exit(eng.last_bar)
    check("[3c] AU（表=CLOSETODAY）→ ④ CLOSETODAY 平今",
          (act.transition, act.intent.value, act.is_exit),
          (4, "closetoday", True))
    check("[3c2] 平今方向 = 净敞口方向（LONG 仓 → 平多）", act.side, Side.LONG)
    check("[3c3] 平今目标 = 那笔当日仓（两态机：latest 即唯一目标）",
          act.target.entry_date >= D1, True)
    check("[3c4] 平今量 = min(|净敞口|, 目标手数) = 3", act.volume, min(3, 3))

with tmp_dir("t3d") as tmp:
    # 2026-09-16 A 批（⑵+⑶-b）：原版这里用 `Instrument(None, None)` 构造引擎，
    #   断言"无品种档案 → 仍走 OPEN 锁仓（保守侧）"。
    #   但这条路径在**引擎层已经结构不可达**：品种既然过了白名单（AU 在册），
    #   运行时对象就必须拿到该品种档案，否则 `_assert_product_ssot` 在引擎
    #   构造期直接拒绝启动。所以断言口径改成两头：
    #     [3d]  引擎层 —— 品种在册却没档案 → **拒绝启动**（不再静默走保守侧）；
    #     [3d2] Instrument 层 —— 保守侧本身仍在（未标定品种的离线探针用）。
    #   这样"保守侧存在"与"生产路径不依赖它"两件事都被钉住。
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    _err = ""
    try:
        build_engine(tmp, cfg, Instrument(None, None), "d")
    except ValueError as e:
        _err = str(e)
    check("[3d] ★ 品种在册却没拿到档案 → 拒绝启动（不再静默走保守侧）",
          "品种档案缺失" in _err, True)

    _bare = Instrument(None, None)
    check("[3d2] 保守侧仍在 Instrument 层：无档案 → exec_policy=None ＋ 报单 FOK",
          (_bare.exec_policy, _bare.effective_order_advanced()), (None, "FOK"))

with tmp_dir("t3e") as tmp:
    cfg = make_cfg("KQ.m@SHFE.AU", exchange="SHFE")
    eng = build_engine(tmp, cfg, Instrument(None, PRODUCT_PROFILES["AU"]), "e")
    eng.on_bar(make_bar(1000))
    # 走**真实路径**开仓（转移①）再数仓单。
    # ⚠️ 旧版这里是 `eng.positions.add(...)` 之后断言 `len(...) == 1` ——
    #    那测的是 add 方法本身，**永远通过**，跟两态前提无关（恒真断言）。
    eng.on_signal(make_sig("P51|buy|1", is_buy=True, price=4500.0))
    check("[3e] ★ 两态机前提：CLOSETODAY 品种开仓后运行态仓单数 = 1"
          "（锁仓态不可达）", len(eng.positions.positions), 1)

    def _broken_alerts(e):
        return [a for a in e._alerts
                if a["code"] == "two_state_invariant_broken"]

    check("[3e2] 前提成立时，两态守卫不发告警", _broken_alerts(eng), [])

    # ── 负向：这两条**能失败**，才算守护（旧版缺的就是这一段）──
    # 人工制造「改表（R-OPEN → CLOSETODAY）后带旧 state.db 重启」的现场：
    # 已有 1 笔仓单，再落一笔 OPEN → 仓单数变 2。
    eng._book_open(
        _Action(intent=OrderIntent.OPEN, side=Side.LONG, volume=2,
                target=None, is_exit=False, transition=1),
        eng.broker.orders[-1], None)
    broken = _broken_alerts(eng)
    check("[3e3] ★ 守卫会咬人：注入第 2 笔仓单 → SEVERE "
          "two_state_invariant_broken（带 positions_n）",
          [(a["level"], a.get("positions_n"), a.get("where")) for a in broken],
          [("severe", 2, "book_open")])
    # 恢复路径（真实破口）走同一个守卫 → 同 code 合并计数、不新增条目
    eng._check_two_state_invariant("restore")
    check("[3e4] 恢复路径守卫同源（同 code 合并，n: 1 → 2，where 覆盖为 restore）",
          [(a["n"], a.get("where")) for a in _broken_alerts(eng)],
          [(2, "restore")])


print("\n[4] _pre_trade_check 对 CLOSETODAY 的校验链（判据 = 表第 1 列）")
today_target = SimpleNamespace(entry_date=D1, volume=2, side=Side.LONG,
                               signal_key="X|buy|1")
past_target = SimpleNamespace(entry_date="2026-09-01", volume=2,
                              side=Side.LONG, signal_key="X|buy|1")

with tmp_dir("t4a") as tmp:
    eng = build_engine(tmp, make_cfg(), Instrument(None, _IF), "a")
    act = _Action(intent=OrderIntent.CLOSETODAY, side=Side.LONG, volume=2,
                  target=today_target, is_exit=True, transition=4)
    check("[4a] CLOSETODAY 但该品种表第 1 列 = R-OPEN → 'closetoday_not_supported'",
          eng._pre_trade_check(act, D1, None), "closetoday_not_supported")

with tmp_dir("t4b") as tmp:
    eng = build_engine(tmp, make_cfg("KQ.m@SHFE.AU", exchange="SHFE"),
                       Instrument(None, PRODUCT_PROFILES["AU"]), "b")
    act_ok = _Action(intent=OrderIntent.CLOSETODAY, side=Side.LONG, volume=2,
                     target=today_target, is_exit=True, transition=4)
    check("[4b] 表第 1 列 = CLOSETODAY + 今仓目标 + SHFE → 通过（None）",
          eng._pre_trade_check(act_ok, D1, None), None)
    act_past = _Action(intent=OrderIntent.CLOSETODAY, side=Side.LONG, volume=2,
                       target=past_target, is_exit=True, transition=4)
    check("[4c] CLOSETODAY 目标=昨仓 → 'closetoday_target_is_yesterday'",
          eng._pre_trade_check(act_past, D1, None),
          "closetoday_target_is_yesterday")
    act_big = _Action(intent=OrderIntent.CLOSETODAY, side=Side.LONG, volume=5,
                      target=today_target, is_exit=True, transition=4)
    check("[4d] 平今量 > 目标手数 → 'close_volume_exceeds_target'",
          eng._pre_trade_check(act_big, D1, None), "close_volume_exceeds_target")
    act_small = _Action(intent=OrderIntent.CLOSETODAY, side=Side.LONG, volume=1,
                        target=today_target, is_exit=True, transition=4)
    check("[4e] 平今量 < 目标手数 → 'close_volume_below_target'",
          eng._pre_trade_check(act_small, D1, None), "close_volume_below_target")


# ══════════════════════════════════════════════════════════════
print("\n[5] DryRun 报单：CLOSETODAY → offset=CLOSETODAY（meta 审计可见）")
# ══════════════════════════════════════════════════════════════
with tmp_dir("t5") as tmp:
    spec = Instrument(None, PRODUCT_PROFILES["AU"])
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    o = broker.submit(OrderIntent.CLOSETODAY, Side.LONG, 2, 4500.0,
                      "p51|t5", is_exit=True)
    check("[5a] status=filled（dry_run 立即成交）", o.status, "filled")
    check("[5b] meta.offset = CLOSETODAY", o.meta.get("offset"), "CLOSETODAY")
    check("[5c] meta.intent = closetoday", o.meta.get("intent"), "closetoday")
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
    eng = build_engine(tmp, cfg, Instrument(None, PRODUCT_PROFILES["AU"]), "a")
    eng.on_bar(make_bar(1000, D1 + " 09:40", P0, P0 + 10, P0 - 10, P0))
    eng.on_signal(make_sig("P51|buy|1", is_buy=True, price=P0))
    check("[6a] 开仓后 RUNNING", eng.account_state(), AccountState.RUNNING)
    eng.on_bar(make_bar(2000, D1 + " 14:55", P_EXIT, P_EXIT + 10, 4000.0, P_EXIT))
    check("[6b] 平今成交后净敞口归 0 → 回**空仓态**（非锁仓态）",
          eng.account_state(), AccountState.FLAT)
    intents = [o.meta.get("intent") for o in eng.broker.orders]
    check("[6c] 离场那笔是 intent=closetoday（④ 平今，非反向开仓锁仓）",
          "closetoday" in intents, True)
    check("[6d] 全程恰好两笔：open（开仓）+ closetoday（平今），"
          "无反向开仓锁仓单", intents, ["open", "closetoday"])


print("\n" + "=" * 62)
print("P51 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
sys.exit(1 if _FAIL else 0)
