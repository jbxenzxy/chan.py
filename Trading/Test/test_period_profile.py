# -*- coding: utf-8 -*-
"""
Step 2.1 周期档案测试：周期时间语义 + 品种档案
================================================
周期只承载时间语义（freq / bar_secs），收口在 Infra/PeriodProfile.py 的
PERIOD_PROFILES；止盈止损等盈利参数随品种变，收口在 Infra/ProductProfile.py。

本测试验证：
    ① PeriodProfile 只有 freq / bar_secs（无 note / 无周期敏感副字段）
    ② TradingConfig 默认 flat 字段（信号新鲜度容差非周期敏感，不随周期变）
    ③ signal_k_tol_bars 越界 fail-fast
    ④ 未知 freq 容错（不 fail-fast，交给 main.py）
    ⑤ period_profile 随 freq 动态跟随（只读视图，无影子覆盖）
    ⑥ ProductProfile 品种档案：instrument 字段播种（multiplier/price_tick）+
       exit 三参数经 resolved_exit_params() 合并（Fix A · 2026-09-14）
    ⑦ 品种档案注入的双档语义（B-3 修复锚点）：初始加载 user-explicit-wins
      （用户显式写的字段不被档案覆盖）、--symbol 换品种 force=True 整块覆盖
      （含用户显式值 —— 换品种后旧品种参数必须让位）

跑法：python test_period_profile.py
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
from Trading.Infra.PeriodProfile import (  # noqa: E402
    FREQ_SEC, PERIOD_PROFILES, SUPPORTED_FREQS,
)
from Trading.Infra.ProductProfile import PRODUCT_PROFILES, parse_product  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> {}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def main():
    print("\n[1] PeriodProfile 周期档案（仅 freq / bar_secs 时间语义）")
    check("PERIOD_PROFILES 仍是 4 周期", len(PERIOD_PROFILES), 4)
    for f in SUPPORTED_FREQS:
        p = PERIOD_PROFILES[f]
        check("{} bar_secs 与 FREQ_SEC 一致".format(f), p.bar_secs, FREQ_SEC[f])
        check("{} 档案无 note 字段".format(f), hasattr(p, "note"), False)

    print("\n[2] TradingConfig 默认 flat 字段（信号新鲜度容差非周期敏感，不随 profile 变）")
    c = TradingConfig()
    check("默认 freq=5m", c.source.freq, "5m")
    check("默认 period_profile.freq=5m", c.period_profile.freq, "5m")
    check("source.signal_k_tol_bars 默认=1", c.source.signal_k_tol_bars, 1)

    c15 = TradingConfig(source={"freq": "15s"})
    check("15s period_profile.freq", c15.period_profile.freq, "15s")
    check("15s profile.bar_secs=15", c15.period_profile.bar_secs, 15)
    check("15s 容差仍=1（周期无关）", c15.source.signal_k_tol_bars, 1)

    print("\n[3] signal_k_tol_bars 越界 fail-fast（负值被拒）")
    try:
        TradingConfig(source={"signal_k_tol_bars": -1})
        check("负容差被拒", False, True)
    except Exception:
        check("负容差被拒", True, True)

    print("\n[4] 未知 freq 容错（fail-fast 在 main.py）")
    try:
        c_unk = TradingConfig(source={"freq": "15m"})
        check("未知 freq 不抛异常", True, True)
        check("未知 freq period_profile=None", c_unk.period_profile, None)
    except Exception as e:
        check("未知 freq 不抛异常（却抛了 {}）".format(type(e).__name__), False, True)

    print("\n[5] period_profile 随 freq 动态跟随（只读视图，无影子覆盖）")
    c2 = TradingConfig()
    c2.source.freq = "30m"
    check("改 freq=30m 后 period_profile=30m", c2.period_profile.freq, "30m")
    check("30m profile.bar_secs=1800", c2.period_profile.bar_secs, 1800)

    print("\n[6] ProductProfile 品种档案：随品种参数影子覆盖（2026-09-09）")
    check("PRODUCT_PROFILES 已是 8 品种（期指 4 + 上期所金属 3 + 郑商所 PTA）", len(PRODUCT_PROFILES), 8)
    check("parse KQ.m@CFFEX.IF → IF", parse_product("KQ.m@CFFEX.IF"), "IF")
    check("parse KQ.m@CFFEX.IC → IC", parse_product("KQ.m@CFFEX.IC"), "IC")
    check("parse 无点串 → ''", parse_product("IF2609"), "")
    check("parse 月合约 CFFEX.IF2609 → IF2609（非品种代码用 parse 前先取品种）",
          parse_product("CFFEX.IF2609"), "IF2609")

    c_if = TradingConfig()
    _res_if = resolved_exit_params(c_if)
    check("IF resolved min_r_points=3.0", _res_if["min_r_points"], 3.0)
    check("IF resolved r_multiple_tp=2.0", _res_if["r_multiple_tp"], 2.0)
    check("IF multiplier=300.0", c_if.instrument.multiplier, 300.0)
    check("IF 生效的品种档案 product=IF", c_if.product_profile.product, "IF")

    c_ic = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC"})
    _res_ic = resolved_exit_params(c_ic)
    check("IC resolved min_r_points=5.0", _res_ic["min_r_points"], 5.0)
    check("IC resolved r_multiple_tp=3.0", _res_ic["r_multiple_tp"], 3.0)
    check("IC multiplier=200.0", c_ic.instrument.multiplier, 200.0)

    # 未知品种：档案缺失（product_profile=None）。Fix A 后 exit_params 上没有
    # 品种三参数 —— resolved_exit_params 启动期即抛（与白名单闸门同文案，
    # 把「品种参数没标定」拦在启动期，与周期 fail-fast 同一纪律）。
    c_unk = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.XX"})
    check("未知品种 product_profile=None", c_unk.product_profile, None)
    try:
        resolved_exit_params(c_unk)
        _raised = ""
    except ValueError as e:
        _raised = str(e)
    check("未知品种 resolved_exit_params 抛 ValueError（启动期闸门，文案含支持清单）",
          "支持清单" in _raised, True)

    print("\n[7] 品种档案注入双档语义（B-3 锚点：初始 user-explicit-wins / 换品种 force 覆盖）")
    # (a) 初始加载：用户在配置里**显式写**的字段不被档案覆盖（写错了也是用户的决定，
    #     实盘还有 SimNow 行情回填 + fail-closed 闸门兜底）；未显式写的字段照常注入。
    #     Fix A（2026-09-14）：品种三参数已不在 ExitConfig —— 显式写旧键直接
    #     ValidationError（extra=forbid，D1 拍板：放弃 .env 覆盖，调参 = 改档案）。
    c_exp = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC", "multiplier": 999.0})
    check("显式 multiplier=999 初始加载不被档案覆盖", c_exp.instrument.multiplier, 999.0)
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC"},
                      exit_params={"min_r_points": 7.0})
        _old_key_err = ""
    except Exception as e:  # pydantic ValidationError（extra=forbid）
        _old_key_err = str(e)
    check("ExitConfig 显式写品种参数（min_r_points）直接报错（D1 放弃 env 覆盖）",
          "min_r_points" in _old_key_err, True)

    # (b) --symbol 换品种重载（main.py: apply_product_profile）：品种已切换，
    #     档案是新品种的权威真值，整块覆盖 instrument 字段 —— **含用户显式值**，
    #     否则沿用旧品种的 multiplier/price_tick 会造成限价口径漂移。
    #     ⚠️ 这里必须绕过 model_fields_set 守护：初始注入已把字段写进 fields_set，
    #     若按 (a) 的规则跳过，换品种后旧品种的值会残留。
    #     品种三参数（Fix A）不在注入范围 —— 换品种后经 resolved_exit_params
    #     跟随新品种档案（IC 5.0/3.0 → IF 3.0/2.0）。
    c_exp.instrument.signal_symbol = "KQ.m@CFFEX.IF"
    c_exp.apply_product_profile()
    check("换品种 force 后 multiplier 覆盖为 IF 档案 300.0", c_exp.instrument.multiplier, 300.0)
    _res_exp = resolved_exit_params(c_exp)
    check("换品种后 resolved min_r_points 跟随 IF 档案 3.0", _res_exp["min_r_points"], 3.0)
    check("换品种后 resolved r_multiple_tp 跟随 IF 档案 2.0", _res_exp["r_multiple_tp"], 2.0)
    check("换品种后 resolved breakeven_buffer_ticks 跟随 IF 档案 2.0",
          _res_exp["breakeven_buffer_ticks"], 2.0)

    # (c) 换品种后 price_tick 回档案值：显式 0.5 在初始加载存活、被 force 覆盖回 0.2
    c_tick = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF", "price_tick": 0.5})
    check("显式 price_tick=0.5 初始加载不被档案覆盖", c_tick.instrument.price_tick, 0.5)
    c_tick.instrument.signal_symbol = "KQ.m@CFFEX.IM"
    c_tick.apply_product_profile()
    check("换品种 force 后 price_tick 回档案值 0.2", c_tick.instrument.price_tick, 0.2)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
