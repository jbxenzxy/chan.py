#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P49 合约规格漂移校验（spec_drift）契约测试
==========================================
背景（2026-09-13，用户拍板「保留 + 漂移校验」）：
  · PRODUCT_PROFILES 的 multiplier / price_tick 语义 = **离线兜底**
    （dry_run 模拟成交 / replay 回测没有行情来源，必须有个确定的数算钱）；
    实盘真值 = 行情（SimNow.apply_quote 原子覆盖 + A′ fail-closed）。
  · 双源风险：交易所改合约规格后档案值过期 → 实盘没事，但离线回测静默
    用错数。处置：verified 首次为真时，引擎比对 spec（行情值）与
    product_profile（档案兜底值），不一致 → warn 告警（D11 通道，前端
    toast），**不拒单**（实盘本就以行情为准）。

本测试锁死的断言：
  [1] verified=False（dry_run 离线兜底，无行情可比）→ 不告警、不置位
  [2] verified=True 且 spec 与档案一致 → 不告警，但置位（查过即止）
  [3] verified=True 且 price_tick/multiplier 漂移 → 告警 level=warn、
      code=spec_drift，msg 同时含档案值与行情值
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
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
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
    """构造引擎：IF 档案随 model_validator 注入（profile 一定非 None）。"""
    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    broker = DryRunBroker(cfg.instrument, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def find_alert(engine, code):
    return next((a for a in engine._alerts if a.get("code") == code), None)


def main():
    p = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF"}).product_profile
    check("[0] IF 档案已注入（price_tick=0.2/multiplier=300）",
          (p.price_tick, p.multiplier), (0.2, 300.0))

    print("\n[1] verified=False：不告警、不置位")
    with tmp_dir() as td:
        eng = build_engine(td)
        check("初始 instrument_verified=False",
              eng.spec.instrument_verified, False)
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("未置位（下次 verified 时仍会查）",
              eng._spec_drift_checked, False)

    print("\n[2] verified=True 且与档案一致：不告警、置位")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.spec.instrument_verified = True
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("已置位", eng._spec_drift_checked, True)

    print("\n[3] verified=True 且漂移：warn 告警，msg 含档案值与行情值")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.spec.instrument_verified = True
        eng.spec.price_tick = 0.5      # 模拟 apply_quote 覆盖后的行情值
        eng.spec.multiplier = 100.0
        eng._check_spec_drift()
        a = find_alert(eng, "spec_drift")
        check_true("spec_drift 告警存在", a is not None)
        if a is not None:
            check("级别 = warn（toast 轻提示，不阻塞）", a.get("level"), "warn")
            check("msg 含档案 tick", "0.2" in a.get("msg", ""), True)
            check("msg 含行情 tick", "0.5" in a.get("msg", ""), True)
            check("msg 含档案乘数", "300" in a.get("msg", ""), True)
            check("msg 含行情乘数", "100" in a.get("msg", ""), True)
            check("msg 指明实盘不受影响", "不受影响" in a.get("msg", ""), True)
            check("msg 指明离线兜底过期", "兜底已过期" in a.get("msg", ""), True)

    print("\n[4] 一次性：告警后重复调用不新增条目")
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.spec.instrument_verified = True
        eng.spec.multiplier = 100.0
        eng._check_spec_drift()
        n1 = len(eng._alerts)
        eng._check_spec_drift()
        eng._check_spec_drift()
        check("重复调用后条目数不变", len(eng._alerts), n1)

    print("\n[5] 挂点在 A3 校验链：_pre_trade_check 触发漂移检查")
    from Trading.Engine.Engine import _Action
    from Trading.Infra.Types import OrderIntent, Side
    with tmp_dir() as td:
        eng = build_engine(td)
        eng.spec.instrument_verified = True
        eng.spec.multiplier = 100.0    # 漂移
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
        eng.spec.instrument_verified = True
        # product_profile 是只读 property 且 P48 白名单保证非 None（直构造
        # 到不了 None 分支）——换桩 cfg 走防御分支。
        class _CfgStub:
            product_profile = None
        eng.cfg = _CfgStub()
        eng.spec.multiplier = 100.0
        eng._check_spec_drift()
        check("无 spec_drift 告警", find_alert(eng, "spec_drift"), None)
        check("未置位（None 档案没做过比对，早退零成本）",
              eng._spec_drift_checked, False)

    print("\nP49 合约规格漂移校验 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
