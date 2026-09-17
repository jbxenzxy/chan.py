#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P49 合约规格漂移校验（spec_drift）契约测试
==========================================
背景（2026-09-17 改造后语义：防回潮护栏）：
  · 有效 tick/乘数的 **SSOT = 品种档案 Product**（构造期播种进 Instrument，
    运行期不再改写，无任何行情取值路径）—— state 与档案同源，
    结构上恒无漂移。
  · Engine._check_spec_drift 保留作**防回潮护栏**：若将来有人重新引入
    "非档案来源写入 state"的通道而档案未同步，这里会把双源漂移从静默
    变显性（warn 告警，D11 通道前端 toast），**不拒单**。

本测试锁死的断言：
  [1] verified=False（离线兜底态）→ 不告警、不置位
  [2] verified=True 且 state 与档案一致 → 不告警，但置位（查过即止）
  [3] verified=True 且 price_tick/multiplier 漂移 → 告警 level=warn、
      code=spec_drift，msg 同时含档案值与 state 值
  [4] 一次性：漂移告警后再调 _check_spec_drift() → 不新增条目（同 code
      合并/置位防刷）
  [5] 挂点在 A3 校验链：_pre_trade_check 通过 A′ 闸门后触发漂移检查
  [6] 无档案（product_profile=None，防御分支）→ 不告警

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p49_spec_drift.py
"""
import contextlib
import os
import shutil
import sys
import tempfile


def _locate_tg_root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "Trading")):
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
# _TG_ROOT = 含 Trading/ 的仓库根（phase9/）；再兜一层父目录，两种定位口径都兼容
sys.path.insert(0, _TG_ROOT)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading import main as _main  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import EffectiveSpec, Instrument  # noqa: E402
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextlib.contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p49_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def build_engine(tmpdir, signal_symbol="KQ.m@CFFEX.IF"):
    """构造引擎：Instrument 构造时直接取品种档案（播种桥已删），
    再建 broker/引擎 —— 与 main.py 的启动次序一致（profile 一定非 None）。

    state 归属：不显式传 state 时引擎沿用 broker 的那一份（Broker.state
    惰性自建），所以一次运行仍只有**一份** state —— 测试直接改 `eng.state`
    即可，读到的正是引擎对账时用的那个对象。
    """
    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    # 播种桥已删 —— Instrument 构造时直接取品种档案
    inst = Instrument(cfg.instrument, cfg.product_profile)
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    broker = DryRunBroker(inst, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def find_alert(engine, code):
    return next((a for a in engine._alerts if a.get("code") == code), None)


def _drift(engine, tick=None, mult=None):
    """把有效值"漂移"到指定 tick / 乘数（模拟非档案来源的越权写入）。

    D-D：Instrument 的有效值收进不可变 `EffectiveSpec`（只读 property 转发，
    没有 setter）。2026-09-17 改造后有效值 SSOT=品种档案，运行期没有任何
    合法改写路径 —— 这里直接整体替换 `_effective` 值对象，正是"防回潮
    护栏"要抓的那类写入（真发生时 _check_spec_drift 必须出声）。
    """
    st = engine.state
    st._effective = EffectiveSpec(
        price_tick=tick if tick is not None else st.price_tick,
        multiplier=mult if mult is not None else st.multiplier)


def main():
    p = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF"}).product_profile
    check("[0] IF 档案已注入（price_tick=0.2/multiplier=300）",
          (p.price_tick, p.multiplier), (0.2, 300.0))

    print("\n[1] verified=False：不告警、不置位")
    with tmp_dir() as td:
        eng = build_engine(td)
        check("初始 state.verified=False",
              eng.state.verified, False)
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("未置位（下次 verified 时仍会查）",
              eng._spec_drift_checked, False)

    print("\n[2] verified=True 且与档案一致：不告警、置位")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.state.verified = True
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("已置位", eng._spec_drift_checked, True)

    print("\n[3] verified=True 且漂移：warn 告警，msg 含档案值与行情值")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.state.verified = True
        _drift(eng, tick=0.5, mult=100.0)   # 越权写入：模拟非档案来源的漂移
        eng._check_spec_drift()
        a = find_alert(eng, "spec_drift")
        check_true("spec_drift 告警存在", a is not None)
        if a is not None:
            check("级别 = warn（toast 轻提示，不阻塞）", a.get("level"), "warn")
            check("msg 含档案 tick", "0.2" in a.get("msg", ""), True)
            check("msg 含行情 tick", "0.5" in a.get("msg", ""), True)
            check("msg 含档案乘数", "300" in a.get("msg", ""), True)
            check("msg 含行情乘数", "100" in a.get("msg", ""), True)
            check("msg 指明 SSOT=品种档案", "SSOT=品种档案" in a.get("msg", ""), True)
            check("msg 指明防回潮护栏触发", "防回潮护栏触发" in a.get("msg", ""), True)

    print("\n[4] 一次性：告警后重复调用不新增条目")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.state.verified = True
        _drift(eng, mult=100.0)
        eng._check_spec_drift()
        n1 = len(eng._alerts)
        eng._check_spec_drift()
        eng._check_spec_drift()
        check("重复调用后条目数不变", len(eng._alerts), n1)

    print("\n[5] 挂点在 A3 校验链：_pre_trade_check 触发漂移检查")
    from Trading.Engine.Engine import _Action
    from Trading.Infra.Records import OrderIntent, Side
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.state.verified = True
        _drift(eng, mult=100.0)    # 漂移
        act = _Action(OrderIntent.OPEN, Side.LONG, 1, None, is_exit=False,
                      transition=1)
        reason = eng._pre_trade_check(act, "2026-09-13")
        # 无时间锚（no_time_anchor）会在漂移检查**之后**拒单——本测试没喂
        # 行情锚，属预期；关键是漂移不产生 instrument_unverified 拒单。
        check("漂移不产生 instrument_unverified 拒单",
              reason != "instrument_unverified", True)
        check_true("漂移告警已由校验链触发",
                   find_alert(eng, "spec_drift") is not None)

    print("\n[6] 无档案（防御分支）：不告警")
    with tmp_dir() as td:
        eng = build_engine(td)
        # 早退判据由 `cfg.product_profile`（**实时**按
        #   cfg.instrument.signal_symbol 查表）换成 `self.state.product`
        #   （唯一运行时对象）→ 桩必须打在 **state** 上。
        #   打在 cfg 上已经触发不了这条分支了：品种在册却没有档案，会在引擎
        #   构造期被 `_assert_product_ssot` 直接拒绝启动（test_p55 [3a]）。
        eng.state = Instrument(None, None)
        # ⚠️ 顺序：`verified` 必须在**替换 state 之后**置位 —— 新 Instrument 的
        #   verified 初值是 False，先置位再替换等于把它重置掉，于是
        #   `if self.state.verified:` 直接为假 → 本用例会退化成"恒真断言"
        #   （不管早退分支在不在都绿）。这是本文件第一次改写时踩到的坑。
        eng.state.verified = True
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("未置位（None 档案没做过比对，早退零成本）",
              eng._spec_drift_checked, False)

    print("\nP49 合约规格漂移校验 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
