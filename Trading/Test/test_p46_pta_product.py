# -*- coding: utf-8 -*-
"""
P46 郑商所 PTA 品种档案 + 品种白名单弹窗 契约测试
==================================================
背景（2026-09-13，用户拍板）：
  · Trading 网关品种档案补齐第 8 个品种：PTA（CZCE，Tier 2 能源化工），
    使 PRODUCT_PROFILES = IF/IH/IC/IM + AU/AG/CU + PTA；
  · 用户输入的品种代码**不在支持清单内**时，启动期发 severe 告警（D11 通道，
    前端阻塞弹窗）告知"不可用"—— 不阻断启动（回放/测试需要任意代码造合约；
    实盘安全性由 A′ fail-closed 闸门兜底），但用户必须看见。

本测试锁死的断言：
  [1] parse_product：KQ.m@CZCE.TA → PTA
  [2] PRODUCT_PROFILES 含 PTA 且 multiplier/price_tick 为合约真值（5 吨/手、tick 2）
  [3] TradingConfig 初始加载：PTA 注入五个随品种可变字段
  [4] PTA 的 CZCE 报单语义（Phase 9）：exchange=CZCE → effective_order_advanced()
      返回 FAK、Engine._open_volume() 钉 1 手 —— 档案只管品种参数、不管报单属性
  [5] 品种白名单弹窗：未知品种（ZZ）引擎启动 → _alerts 含 code="unknown_product"
      level="severe"；已知品种（PTA/IF）→ 无该告警

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p46_pta_product.py
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
_REPO_ROOT = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO_ROOT)

from Trading import Broker  # noqa: E402,F401  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Config import TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.ProductProfile import PRODUCT_PROFILES, parse_product  # noqa: E402
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


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="tg_p46_")
    try:
        yield d
    finally:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def build_engine(tmpdir, signal_symbol):
    """构造引擎：cfg 按 signal_symbol 生成（品种档案随 model_validator 注入）。"""
    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    entry = EntryPolicy({"reverse_on_opposite_signal": False})
    exitp = LayeredExitPolicy()
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    broker = DryRunBroker(cfg.instrument, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


def has_alert(engine, code):
    return any(a.get("code") == code for a in engine._alerts)


def main():
    print("\n[1] parse_product：郑商所主连 → 品种代码（PTA 的符号代码是 TA）")
    check("parse KQ.m@CZCE.TA → TA", parse_product("KQ.m@CZCE.TA"), "TA")

    print("\n[2] PRODUCT_PROFILES 含 TA(PTA) 且合约真值（8 品种）")
    check("品种总数 = 8", len(PRODUCT_PROFILES), 8)
    check("支持清单 = IF/IH/IC/IM/AU/AG/CU/TA",
          sorted(PRODUCT_PROFILES),
          ["AG", "AU", "CU", "IC", "IF", "IH", "IM", "TA"])
    p = PRODUCT_PROFILES.get("TA")
    check("TA(PTA) 在 PRODUCT_PROFILES", p is not None, True)
    if p is not None:
        check("TA product=TA", p.product, "TA")
        check("TA multiplier=5.0（5 吨/手）", p.multiplier, 5.0)
        check("TA price_tick=2.0", p.price_tick, 2.0)
        check("TA min_r_points=30.0（15 tick）", p.min_r_points, 30.0)
        check("TA r_multiple_tp=2.0", p.r_multiple_tp, 2.0)
        check("TA breakeven_buffer_ticks=2.0", p.breakeven_buffer_ticks, 2.0)

    print("\n[3] TradingConfig 初始加载：TA(PTA) 注入五个随品种可变字段")
    c_ta = TradingConfig(instrument={"signal_symbol": "KQ.m@CZCE.TA"})
    check("TA min_r_points 注入 30.0", c_ta.exit_params.min_r_points, 30.0)
    check("TA r_multiple_tp 注入 2.0", c_ta.exit_params.r_multiple_tp, 2.0)
    check("TA breakeven_buffer_ticks 注入 2.0",
          c_ta.exit_params.breakeven_buffer_ticks, 2.0)
    check("TA multiplier 注入 5.0", c_ta.instrument.multiplier, 5.0)
    check("TA price_tick 注入 2.0", c_ta.instrument.price_tick, 2.0)
    check("TA product_profile 命中", c_ta.product_profile.product, "TA")

    print("\n[4] PTA 的 CZCE 报单语义（档案不管报单属性，exchange 决定）")
    sp_ta = InstrumentSpec(signal_symbol="KQ.m@CZCE.TA", exchange="CZCE",
                           order_advanced="FOK")
    check("TA(CZCE) advanced=FAK", sp_ta.effective_order_advanced(), "FAK")
    with tmp_dir() as td:
        eng_ta = build_engine(td, "KQ.m@CZCE.TA")
        eng_ta.cfg.instrument.exchange = "CZCE"
        check("TA(CZCE) _open_volume()=1（钉 1 手）", eng_ta._open_volume(), 1)
        eng_ta.cfg.instrument.exchange = "SHFE"
        check("TA 配错交易所(SHFE) _open_volume() 走 lots_per_signal=2（配置责任）",
              eng_ta._open_volume(), 2)

    print("\n[5] 品种白名单弹窗：未知品种 severe 告警、已知品种无告警")
    with tmp_dir() as td:
        eng_unk = build_engine(td, "KQ.m@SHFE.ZZ")
        check_true("未知品种(ZZ) 启动即发 unknown_product 告警",
                   has_alert(eng_unk, "unknown_product"))
        if has_alert(eng_unk, "unknown_product"):
            a = next(a for a in eng_unk._alerts if a.get("code") == "unknown_product")
            check("告警级别 = severe（前端阻塞弹窗）", a.get("level"), "severe")
            check("告警含品种符号", a.get("signal_symbol"), "KQ.m@SHFE.ZZ")
            check("告警 msg 列出支持清单", "TA" in a.get("msg", ""), True)
    with tmp_dir() as td:
        eng_ta = build_engine(td, "KQ.m@CZCE.TA")
        check("已知品种(TA/PTA) 无 unknown_product 告警",
              has_alert(eng_ta, "unknown_product"), False)
    with tmp_dir() as td:
        eng_if = build_engine(td, "KQ.m@CFFEX.IF")
        check("已知品种(IF) 无 unknown_product 告警",
              has_alert(eng_if, "unknown_product"), False)

    print("\n============================================================")
    print("P46 PTA 档案 + 品种白名单弹窗 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
