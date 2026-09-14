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
    ⑥ ProductProfile 品种档案：经 `InstrumentSpec.for_product` **显式播种**
       multiplier/price_tick（Phase 3 起不再由 TradingConfig 构造副作用注入）+
       exit 三参数经 resolved_exit_params() 合并（Fix A · 2026-09-14）
    ⑦ 播种语义（Phase 3 · Fix B 起，**取代**原"注入双档"锚点）：
       档案是 price_tick / multiplier 的**唯一真值来源** —— 用户显式写的值
       在播种时同样被档案覆盖（D1：放弃配置覆盖品种参数的能力）；
       初始加载与换品种走**同一个** `main._seed_instrument()`，无 force 双语义。

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
from Trading import main as _main  # noqa: E402

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


def seeded(signal_symbol: str) -> TradingConfig:
    """构造配置 + **按启动路径显式播种**品种档案（Phase 3 · Fix B）。

    为什么测试要绕这一道：Phase 3 把"档案播种"从 TradingConfig 的构造副作用
    （model_validator + model_fields_set）改成启动路径上的一次显式调用
    （Trading/main.py::_seed_instrument）。若测试仍只构造 TradingConfig 就断言
    `instrument.multiplier`，测的其实是模型默认值 —— 档案播种整条通道被漏接
    也照样绿灯（假阳性）。故这里走与生产**完全相同**的入口；
    初始加载与 --symbol 换品种都是同一个函数，Phase 3 已删掉 force 双档语义。
    """
    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    _main._seed_instrument(cfg)
    return cfg


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

    print("\n[6] ProductProfile 品种档案：显式播种（Phase 3 · Fix B）")
    check("PRODUCT_PROFILES 已是 8 品种（期指 4 + 上期所金属 3 + 郑商所 PTA）", len(PRODUCT_PROFILES), 8)
    check("parse KQ.m@CFFEX.IF → IF", parse_product("KQ.m@CFFEX.IF"), "IF")
    check("parse KQ.m@CFFEX.IC → IC", parse_product("KQ.m@CFFEX.IC"), "IC")
    check("parse 无点串 → ''", parse_product("IF2609"), "")
    check("parse 月合约 CFFEX.IF2609 → IF2609（非品种代码用 parse 前先取品种）",
          parse_product("CFFEX.IF2609"), "IF2609")

    c_if = seeded("KQ.m@CFFEX.IF")
    _res_if = resolved_exit_params(c_if)
    check("IF resolved 已无 min_r_points（2026-09-14 删除）",
          "min_r_points" in _res_if, False)
    check("IF resolved r_multiple_tp=2.0", _res_if["r_multiple_tp"], 2.0)
    check("IF 播种后 multiplier=300.0（与模型默认同值，此处不作强断言）",
          c_if.instrument.multiplier, 300.0)
    check("IF 生效的品种档案 product=IF", c_if.product_profile.product, "IF")

    # IC 档案值（200.0 / 0.2）与模型默认（300.0）不同 —— 用它证明"播种真的发生了"，
    # 而不是恰好等于模型默认值。
    c_ic = seeded("KQ.m@CFFEX.IC")
    _res_ic = resolved_exit_params(c_ic)
    check("IC resolved 已无 min_r_points（2026-09-14 删除）",
          "min_r_points" in _res_ic, False)
    check("IC resolved r_multiple_tp=3.0", _res_ic["r_multiple_tp"], 3.0)
    check("IC 播种后 multiplier=200.0（≠ 模型默认 300 → 播种生效）",
          c_ic.instrument.multiplier, 200.0)

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

    print("\n[7] 播种语义（Phase 3：档案是 tick/乘数唯一真值来源，无 user-explicit-wins）")
    # (a) 语义反转锚点（Phase 2 → Phase 3）：
    #     Phase 2 及之前是"初始加载 user-explicit-wins"——用户在配置里显式写的
    #       price_tick / multiplier 不被档案覆盖（靠 model_fields_set 判据）。
    #     Phase 3 按 D1 拍板取消这条：档案是这两个字段**唯一**的真值来源，
    #       显式写进去的值在播种时同样被档案覆盖。理由与 Fix A 把
    #       r_multiple_tp 从 ExitConfig 删掉是同一个决策 —— 调参 = 改档案
    #       = git 评审 + 对账测试守护（min_r_points / breakeven_buffer_ticks
    #       已于 2026-09-14 一并删除）。
    #     安全性：实盘 tick/乘数由 SimNow 行情原子回填 + A′ fail-closed 兜底，
    #       配置值本就只是离线（dry_run/replay）的种子，被档案覆盖无损。
    c_exp = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC",
                                     "multiplier": 999.0})
    check("播种前：显式 multiplier=999 原样保留（构造已无副作用）",
          c_exp.instrument.multiplier, 999.0)
    _main._seed_instrument(c_exp)
    check("播种后：显式 999 被档案覆盖回 IC 的 200.0（D1 · 档案唯一真值）",
          c_exp.instrument.multiplier, 200.0)

    # 出场三参数：Fix A 保持 —— 显式写旧键直接 ValidationError（extra=forbid）。
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC"},
                      exit_params={"min_r_points": 7.0})
        _old_key_err = ""
    except Exception as e:  # pydantic ValidationError（extra=forbid）
        _old_key_err = str(e)
    check("ExitConfig 显式写品种参数（min_r_points）直接报错（D1 放弃 env 覆盖）",
          "min_r_points" in _old_key_err, True)

    # (b) 换品种：与初始加载**同一条路径**（同一个 _seed_instrument），
    #     Phase 3 删掉了 apply_product_profile/force 那套双档语义 ——
    #     品种已切换，档案立即成为新品种的权威真值（含 tick/乘数整块覆盖），
    #     否则沿用旧品种的 multiplier/price_tick 会造成限价口径漂移。
    #     r_multiple_tp 不在播种范围 —— 换品种后经 resolved_exit_params 跟随
    #     新品种档案（IC 3.0 → IF 2.0）。
    c_exp.instrument.signal_symbol = "KQ.m@CFFEX.IF"
    _main._seed_instrument(c_exp)
    check("换品种播种后 multiplier 覆盖为 IF 档案 300.0", c_exp.instrument.multiplier, 300.0)
    _res_exp = resolved_exit_params(c_exp)
    check("换品种后 resolved r_multiple_tp 跟随 IF 档案 2.0", _res_exp["r_multiple_tp"], 2.0)
    check("换品种后 resolved breakeven_buffer_r=0.5（全局，不随品种）",
          _res_exp["breakeven_buffer_r"], 0.5)

    # (c) price_tick 同理：显式 0.5 在构造后存活（无副作用），一经播种即回档案值，
    #     换品种再播种继续跟新品种档案走。
    c_tick = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF",
                                       "price_tick": 0.5})
    check("播种前：显式 price_tick=0.5 原样保留", c_tick.instrument.price_tick, 0.5)
    _main._seed_instrument(c_tick)
    check("播种后：price_tick 回 IF 档案值 0.2", c_tick.instrument.price_tick, 0.2)
    c_tick.instrument.signal_symbol = "KQ.m@CFFEX.IM"
    _main._seed_instrument(c_tick)
    check("换品种播种后 price_tick 回 IM 档案值 0.2", c_tick.instrument.price_tick, 0.2)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
