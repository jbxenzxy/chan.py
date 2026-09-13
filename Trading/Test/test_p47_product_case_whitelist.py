# -*- coding: utf-8 -*-
"""
P47 品种代码大小写归一 + 白名单硬约束 契约测试
================================================
背景（2026-09-13，用户拍板）：
  ① 真 bug 修复：前端别名表（DataAPI/TqSdkAPI.py FUTURES_ALIASES）把
    SHFE/DCE 品种解析成 tqsdk 惯例的**小写**主连（AU → "KQ.m@SHFE.au"、
    RB → "KQ.m@SHFE.rb"），而 parse_product 原先大小写敏感 →
    PRODUCT_PROFILES.get("au") 返回 None —— 用户输入 AU/AG/CU/TA 这
    4 个**已支持**品种时档案也匹配不上（档案键为大写）。修复 = 在
    parse_product 内统一归一为大写，调用方无需各自 upper()。
  ② 品种白名单硬约束：品种不在 PRODUCT_PROFILES → **交易引擎拒绝启动**
    （Engine._restore 抛 ValueError），不发告警。实盘入口在
    App/AppTrader.start 前置拦截（AppError → 400 → 前端 alert）。

本测试锁死的断言：
  [1] parse_product 大小写归一：小写主连 → 大写品种代码；无点串/缺失 → ""
  [2] TradingConfig 以**前端真实形态**（别名解析后的小写主连）构造时，
      AU/AG/CU/TA/IF 档案全部命中、五字段正确注入（回归 ①）
  [3] 白名单硬约束（回归 ②）：未知品种（RB/ZZ）构造引擎抛 ValueError；
      异常 msg 含支持清单与"禁止启动"；已知品种（小写 au 主连）正常启动

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p47_product_case_whitelist.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))

# 自定位仓库根（Test/ → Trading/ → 仓库根），与本目录其他 test_p* 一致
_RROOT = os.path.dirname(os.path.dirname(_HERE))
if _RROOT not in sys.path:
    sys.path.insert(0, _RROOT)

from Trading.Infra.ProductProfile import (  # noqa: E402
    PRODUCT_PROFILES, parse_product,
)
from Trading.Config import TradingConfig  # noqa: E402

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


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="p47_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir, signal_symbol):
    """构造引擎（与本目录 test_p46 同款最小装配）。未知品种应抛 ValueError。"""
    from Trading import Broker  # noqa: F401  # 注册 dry_run broker
    from Trading.Broker.DryRun import DryRunBroker
    from Trading.Engine.Engine import TradingEngine
    from Trading.Infra.EventLog import EventLog
    from Trading.Infra.Store import Store
    from Trading.Strategy.Entry import EntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy

    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    broker = DryRunBroker(cfg.instrument, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def main():
    print("\n[1] parse_product 大小写归一（回归：别名表产出小写主连）")
    check("parse KQ.m@SHFE.au（前端别名形态）→ AU", parse_product("KQ.m@SHFE.au"), "AU")
    check("parse KQ.m@SHFE.rb → RB", parse_product("KQ.m@SHFE.rb"), "RB")
    check("parse KQ.m@SHFE.AU（大写）→ AU", parse_product("KQ.m@SHFE.AU"), "AU")
    check("parse KQ.m@CZCE.TA → TA", parse_product("KQ.m@CZCE.TA"), "TA")
    check("parse KQ.m@CFFEX.if → IF", parse_product("KQ.m@CFFEX.if"), "IF")
    check("parse 无点串 → ''", parse_product("IF2609"), "")
    check("parse 空串 → ''", parse_product(""), "")
    check("parse None → ''", parse_product(None), "")

    print("\n[2] 前端真实形态（小写主连）构造 TradingConfig：档案全部命中")
    for sym, product, mult, tick in [
            ("KQ.m@SHFE.au", "AU", 1000.0, 0.02),
            ("KQ.m@SHFE.ag", "AG", 15.0, 1.0),
            ("KQ.m@SHFE.cu", "CU", 5.0, 10.0),
            ("KQ.m@CZCE.ta", "TA", 5.0, 2.0),
            ("KQ.m@CFFEX.if", "IF", 300.0, 0.2)]:
        c = TradingConfig(instrument={"signal_symbol": sym})
        check("{} product_profile 命中 {}".format(sym, product),
              (c.product_profile.product if c.product_profile else None), product)
        check("{} multiplier 注入 {}".format(product, mult),
              c.instrument.multiplier, mult)
        check("{} price_tick 注入 {}".format(product, tick),
              c.instrument.price_tick, tick)
        check("{} R 下限 = 默认 3 点（2026-09-13 拍板）".format(product),
              c.exit_params.min_r_points, 3.0)

    print("\n[3] 白名单硬约束：未知品种拒绝启动、已知品种正常启动")
    for sym in ["KQ.m@SHFE.rb", "KQ.m@SHFE.ZZ"]:
        with tmp_dir() as td:
            try:
                build_engine(td, sym)
                raised = None
            except ValueError as e:
                raised = str(e)
            check_true("{} 构造引擎抛 ValueError（拒绝启动）".format(sym),
                       raised is not None)
            if raised:
                check("{} 异常列出支持清单(含 AU)".format(sym), "AU" in raised, True)
                check("{} 异常说明'禁止启动'".format(sym), "禁止启动" in raised, True)
    for sym in ["KQ.m@SHFE.au", "KQ.m@CFFEX.IF"]:
        with tmp_dir() as td:
            eng = build_engine(td, sym)
            check("{}（小写/大写主连）正常启动".format(sym), eng is not None, True)

    print("\n============================================================")
    print("P47 大小写归一 + 白名单硬约束 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
