# -*- coding: utf-8 -*-
"""
Step 2.1 补充：上期所金属品种档案（AU/AG/CU）注入契约
====================================================
验证 Phase 9.5 新增的 3 个商品品种档案能被 Trading 网关正确识别与注入：

    ① parse_product 从主连符号提取品种代码（SHFE.AU → AU）
    ② PRODUCT_PROFILES 含 AU/AG/CU，且 multiplier/price_tick 为合约真值
    ③ TradingConfig 品种档案（Fix A · 2026-09-14；Phase 3 起为**显式播种**）：
       multiplier / price_tick 真值源 = 品种档案（P-B：播种桥已删，Instrument 构造时取档案，原 `InstrumentSpec.
       for_product(profile)`）播种；r_multiple_tp 经 resolved_exit_params() 合并
       （品种档案是唯一默认值来源；min_r_points / breakeven_buffer_ticks 已于
       2026-09-14 删除，保本缓冲改为全局比例 breakeven_buffer_r）
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

from Trading.Config import TradingConfig, resolved_exit_params  # noqa: E402
from Trading.Infra.InstrumentSpec import Instrument  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES, parse_product  # noqa: E402

from Trading import main as _main  # noqa: E402

_PASS = 0
_FAIL = 0


def seeded(signal_symbol: str) -> TradingConfig:
    """构造配置（P-B：tick/乘数真值源 = 品种档案，配置不再携带、无播种动作）。

    Phase 3 的"显式播种"（main._seed_instrument）已随 P-B 删除：Instrument
    有效值初值在**构造时**直接取 Product（档案→运行时单向取值，
    结构上保证一致，无需运行时对账）。本 helper 保留名字只为改动最小；
    下方 product_profile 命中断言即覆盖"档案解析"这条链。
    """
    return TradingConfig(instrument={"signal_symbol": signal_symbol})


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

    print("\n[3] TradingConfig 品种档案：AU（显式播种 + resolved 合并）")
    c_au = seeded("KQ.m@SHFE.AU")
    _res_au = resolved_exit_params(c_au)
    check("AU resolved 已无 min_r_points（2026-09-14 删除）", "min_r_points" in _res_au, False)
    check("AU resolved r_multiple_tp=2.0", _res_au["r_multiple_tp"], 2.0)
    check("AU multiplier=1000.0", c_au.product_profile.multiplier, 1000.0)
    check("AU resolved 已无 breakeven_buffer_ticks（改为全局比例）",
          "breakeven_buffer_ticks" in _res_au, False)
    check("AU resolved breakeven_buffer_r=0.5（全局，不随品种）",
          _res_au["breakeven_buffer_r"], 0.5)
    check("AU price_tick=0.02", c_au.product_profile.price_tick, 0.02)
    check("AU product_profile 命中", c_au.product_profile.product, "AU")

    print("\n[4] TradingConfig 品种档案：AG / CU")
    c_ag = seeded("KQ.m@SHFE.AG")
    _res_ag = resolved_exit_params(c_ag)
    check("AG resolved 已无 min_r_points（2026-09-14 删除）", "min_r_points" in _res_ag, False)
    check("AG multiplier=15.0", c_ag.product_profile.multiplier, 15.0)
    check("AG price_tick=1.0", c_ag.product_profile.price_tick, 1.0)
    c_cu = seeded("KQ.m@SHFE.CU")
    _res_cu = resolved_exit_params(c_cu)
    check("CU resolved 已无 min_r_points（2026-09-14 删除）", "min_r_points" in _res_cu, False)
    check("CU multiplier=5.0", c_cu.product_profile.multiplier, 5.0)
    check("CU price_tick=10.0", c_cu.product_profile.price_tick, 10.0)
    check("CU resolved breakeven_buffer_r=0.5（全局，不随品种）",
          _res_cu["breakeven_buffer_r"], 0.5)

    print("\n[5] effective_order_advanced：SHFE 仍走默认 FOK（P-B：exchange 归档案，"
          "直接用现成品种构造）")
    sp_au = Instrument(None, PRODUCT_PROFILES["AU"])
    check("AU(SHFE) advanced=FOK", sp_au.effective_order_advanced(), "FOK")
    sp_ag = Instrument(None, PRODUCT_PROFILES["AG"])
    check("AG(SHFE) advanced=FOK", sp_ag.effective_order_advanced(), "FOK")
    sp_cu = Instrument(None, PRODUCT_PROFILES["CU"])
    check("CU(SHFE) advanced=FOK", sp_cu.effective_order_advanced(), "FOK")
    # 对照：CZCE 仍强制 FAK（回归，确保金属改动未误伤 Phase 9）
    sp_czce = Instrument(None, PRODUCT_PROFILES["TA"])
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
