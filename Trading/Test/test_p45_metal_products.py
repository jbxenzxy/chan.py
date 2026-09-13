# -*- coding: utf-8 -*-
"""
Step 2.1 补充：上期所金属品种档案（AU/AG/CU）注入契约
====================================================
验证 Phase 9.5 新增的 3 个商品品种档案能被 Trading 网关正确识别与注入：

    ① parse_product 从主连符号提取品种代码（SHFE.AU → AU）
    ② PRODUCT_PROFILES 含 AU/AG/CU，且 multiplier/price_tick 为合约真值
    ③ TradingConfig 初始加载按品种注入 min_r_points / r_multiple_tp /
       multiplier / breakeven_buffer_ticks / price_tick（五个随品种可变字段）
    ④ InstrumentSpec.effective_order_advanced 对 SHFE 仍返回 FOK（不破坏 Phase 9 的
       交易所分支；CZCE 才切 FAK）—— 确认金属走默认 order_advanced
    ⑤ 未知品种仍 product_profile=None（不误伤）

跑法：python test_p45_metal_products.py
"""
from __future__ import annotations

import os
import sys

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

from Trading.Config import TradingConfig  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.ProductProfile import PRODUCT_PROFILES, parse_product  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> got={!r} exp={!r}".format(got, expected) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def main():
    print("\n[1] parse_product：上期所主连 → 品种代码")
    check("parse KQ.m@SHFE.AU → AU", parse_product("KQ.m@SHFE.AU"), "AU")
    check("parse KQ.m@SHFE.AG → AG", parse_product("KQ.m@SHFE.AG"), "AG")
    check("parse KQ.m@SHFE.CU → CU", parse_product("KQ.m@SHFE.CU"), "CU")

    print("\n[2] PRODUCT_PROFILES 含 AU/AG/CU 且合约真值")
    for code, mult, tick in (("AU", 1000.0, 0.02), ("AG", 15.0, 1.0), ("CU", 5.0, 10.0)):
        p = PRODUCT_PROFILES.get(code)
        check("{} 在 PRODUCT_PROFILES".format(code), p is not None, True)
        if p is not None:
            check("{} multiplier={}".format(code, mult), p.multiplier, mult)
            check("{} price_tick={}".format(code, tick), p.price_tick, tick)

    print("\n[3] TradingConfig 初始加载：AU 注入五个随品种可变字段")
    c_au = TradingConfig(instrument={"signal_symbol": "KQ.m@SHFE.AU"})
    check("AU min_r_points=0.5", c_au.exit_params.min_r_points, 0.5)
    check("AU r_multiple_tp=2.0", c_au.exit_params.r_multiple_tp, 2.0)
    check("AU multiplier=1000.0", c_au.instrument.multiplier, 1000.0)
    check("AU breakeven_buffer_ticks=2.0", c_au.exit_params.breakeven_buffer_ticks, 2.0)
    check("AU price_tick=0.02", c_au.instrument.price_tick, 0.02)
    check("AU product_profile 命中", c_au.product_profile.product, "AU")

    print("\n[4] TradingConfig 初始加载：AG / CU 注入")
    c_ag = TradingConfig(instrument={"signal_symbol": "KQ.m@SHFE.AG"})
    check("AG min_r_points=20.0", c_ag.exit_params.min_r_points, 20.0)
    check("AG multiplier=15.0", c_ag.instrument.multiplier, 15.0)
    check("AG price_tick=1.0", c_ag.instrument.price_tick, 1.0)
    c_cu = TradingConfig(instrument={"signal_symbol": "KQ.m@SHFE.CU"})
    check("CU min_r_points=100.0", c_cu.exit_params.min_r_points, 100.0)
    check("CU multiplier=5.0", c_cu.instrument.multiplier, 5.0)
    check("CU price_tick=10.0", c_cu.instrument.price_tick, 10.0)
    check("CU breakeven_buffer_ticks=3.0", c_cu.exit_params.breakeven_buffer_ticks, 3.0)

    print("\n[5] effective_order_advanced：SHFE 仍走默认 FOK（不破坏 Phase 9 交易所分支）")
    sp_au = InstrumentSpec(signal_symbol="KQ.m@SHFE.AU", exchange="SHFE", order_advanced="FOK")
    check("AU(SHFE) advanced=FOK", sp_au.effective_order_advanced(), "FOK")
    sp_ag = InstrumentSpec(signal_symbol="KQ.m@SHFE.AG", exchange="SHFE", order_advanced="FOK")
    check("AG(SHFE) advanced=FOK", sp_ag.effective_order_advanced(), "FOK")
    sp_cu = InstrumentSpec(signal_symbol="KQ.m@SHFE.CU", exchange="SHFE", order_advanced="FOK")
    check("CU(SHFE) advanced=FOK", sp_cu.effective_order_advanced(), "FOK")
    # 对照：CZCE 仍强制 FAK（回归，确保金属改动未误伤 Phase 9）
    sp_czce = InstrumentSpec(signal_symbol="KQ.m@CZCE.TA", exchange="CZCE", order_advanced="FOK")
    check("CZCE 对照仍强制 FAK", sp_czce.effective_order_advanced(), "FAK")

    print("\n[6] 未知品种不误伤")
    c_unk = TradingConfig(instrument={"signal_symbol": "KQ.m@SHFE.ZZ"})
    check("未知品种 product_profile=None", c_unk.product_profile, None)

    print("\n============================================================")
    print("上期所金属品种档案 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
