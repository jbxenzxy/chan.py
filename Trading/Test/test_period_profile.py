# -*- coding: utf-8 -*-
"""
Step 2.1 周期档案测试：周期时间语义 + 品种档案
================================================
周期只承载时间语义（freq / bar_secs），收口在 Infra/Period.py 的
PERIOD_PROFILES；止盈止损等盈利参数随品种变，收口在 Infra/Product.py。

本测试验证：
    ① Period 只有 freq / bar_secs（无 note / 无周期敏感副字段）
    ② TradingConfig 默认 flat 字段（信号新鲜度容差非周期敏感，不随周期变）
    ③ signal_k_tol_bars 越界 fail-fast
    ④ 未知 freq 容错（不 fail-fast，交给 main.py）
    ⑤ period_profile 随 freq 动态跟随（只读视图，无影子覆盖）
    ⑥ Product 品种档案：Instrument 构造时直接取
       档案（播种桥 for_product 已删除，注入副作用更早删除）+
       exit 品种相关参数（现只剩 r_multiple_tp）经 resolved_exit_params() 合并
    ⑦ 播种语义（**取代**原"注入双档"锚点）：
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
from Trading.Infra.Period import FREQ_SEC, PERIOD_PROFILES, SUPPORTED_FREQS
from Trading.Infra.Instrument import InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES, parse_product  # noqa: E402


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
    """构造配置（播种桥 _seed_instrument 已删）。

    "显式播种"是启动路径上的一次调用；该桥
    消亡 —— 配置不再携带 tick/乘数，Instrument 构造时直接取品种档案
    （档案→运行时单向取值，结构上保证一致）。本 helper 保留名字只为改动
    最小；下方档案真值断言改读 product_profile。
    """
    return TradingConfig(instrument={"signal_symbol": signal_symbol})


def main():
    print("\n[1] Period 周期档案（仅 freq / bar_secs 时间语义）")
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

    print("\n[6] Product 品种档案：显式播种（Phase 3 · Fix B）")
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
          c_if.product_profile.multiplier, 300.0)
    check("IF 生效的品种档案 product=IF", c_if.product_profile.product, "IF")

    # IC 档案值（200.0 / 0.2）与模型默认（300.0）不同 —— 用它证明"播种真的发生了"，
    # 而不是恰好等于模型默认值。
    c_ic = seeded("KQ.m@CFFEX.IC")
    _res_ic = resolved_exit_params(c_ic)
    check("IC resolved 已无 min_r_points（2026-09-14 删除）",
          "min_r_points" in _res_ic, False)
    check("IC resolved r_multiple_tp=3.0", _res_ic["r_multiple_tp"], 3.0)
    check("IC 播种后 multiplier=200.0（≠ 模型默认 300 → 播种生效）",
          c_ic.product_profile.multiplier, 200.0)

    # 未知品种：档案缺失（product_profile=None）。此后 exit_params 上没有
    # 品种相关出场参数（现只剩 r_multiple_tp） —— resolved_exit_params 启动期即抛（与白名单闸门同文案，
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

    print("\n[7] P-B 归位语义：tick/乘数真值源 = 品种档案；配置里写旧键直接报错")
    # (a) 语义演进锚点：
    #     user-explicit-wins；档案唯一真值（播种覆盖显式值）。
    #     播种桥（_seed_instrument）删除 —— 配置里根本不再
    #       有 tick/乘数字段，旧键显式报错（_check_removed_keys，不静默吞）。
    #       真值在 Product（调参 = 改档案 = git 评审 + 对账测试守护），
    #       运行时由 Instrument 构造时直接取档案；实盘再被行情原子覆盖。
    _err999 = ""
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC",
                                  "multiplier": 999.0})
    except Exception as e:
        _err999 = str(e)
    check("配置里显式写 multiplier → 构造期显式报错（P-B 归位，非静默吞掉）",
          "multiplier" in _err999, True)

    # 出场品种参数：保持 —— 显式写品种相关键直接 ValidationError（extra=forbid）。
    #   ⚠️ 探针键由 min_r_points 换成 r_multiple_tp（评审修）：
    #   min_r_points 在 2026-09-14 被删除，用它当探针只能证明「extra=forbid 拒绝未知键」，
    #   **证明不了「品种参数不能写进 ExitConfig」** —— 守卫失去了判别力。
    #   r_multiple_tp 是今天真实存在的品种级键；将来若有人把它加回 ExitConfig
    #   （双源回潮），本条会立刻变红。
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC"},
                      exit_params={"r_multiple_tp": 7.0})
        _old_key_err = ""
    except Exception as e:  # pydantic ValidationError（extra=forbid）
        _old_key_err = str(e)
    check("ExitConfig 显式写品种参数（r_multiple_tp）直接报错（D1 放弃 env 覆盖）",
          "r_multiple_tp" in _old_key_err, True)

    # (b) 换品种 = 重建配置（frozen，与 main.py --symbol 路径同款）：
    #     signal_symbol 变 → cfg.product_profile 跟随新品种档案；
    #     r_multiple_tp 经 resolved_exit_params 跟随新品种（IC 3.0 → IF 2.0）。
    c_exp = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF"})
    check("换品种后 resolved r_multiple_tp 跟随 IF 档案 2.0",
          resolved_exit_params(c_exp)["r_multiple_tp"], 2.0)
    _res_exp = resolved_exit_params(c_exp)
    check("换品种后 resolved r_multiple_tp 跟随 IF 档案 2.0", _res_exp["r_multiple_tp"], 2.0)
    check("换品种后 resolved breakeven_buffer_r=0.5（全局，不随品种）",
          _res_exp["breakeven_buffer_r"], 0.5)

    # (c) price_tick 同理：配置里写旧键 → 构造期显式报错（归位）。
    _err05 = ""
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IF",
                                  "price_tick": 0.5})
    except Exception as e:
        _err05 = str(e)
    check("配置里显式写 price_tick → 构造期显式报错（P-B 归位）",
          "price_tick" in _err05, True)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
