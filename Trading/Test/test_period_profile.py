# -*- coding: utf-8 -*-
"""
Step 2.1 周期档案测试：周期敏感参数入 PeriodProfile + 影子覆盖 reconcile
=========================================================================
2.1 把 8 项周期敏感参数从「散落各层 + PERIOD_SENSITIVE_FIELDS 索引」收口到
Infra/PeriodProfile.py 的 PERIOD_PROFILES（每周期一份，含 note 标定记录），
TradingConfig 构造期按 source.freq 把 6 项「影子覆盖」进 flat 字段。

本测试验证：
    ① PeriodProfile 有 6 项周期敏感字段 + note，默认 = BASELINE（5m 默认值）
    ② TradingConfig 影子覆盖：freq → profile → flat 字段（默认时填入）
    ③ 用户显式覆盖优先（不 profile 覆盖）
    ④ 未知 freq 容错（不 fail-fast，交给 main.py）
    ⑤ period_sensitive_summary 派生视图（4 周期 × 周期敏感值）
    ⑥ apply_period_profile：改 freq 后重对齐

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

from Trading.Config import (  # noqa: E402
    TradingConfig, period_sensitive_summary,
)
from Trading.Infra.PeriodProfile import (  # noqa: E402
    FREQ_SEC, PERIOD_PROFILES, SUPPORTED_FREQS,
)
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


def main():
    print("\n[1] PeriodProfile 周期字段 + note（freq / bar_secs，无周期敏感副字段）")
    check("PERIOD_PROFILES 仍是 4 周期", len(PERIOD_PROFILES), 4)
    for f in SUPPORTED_FREQS:
        p = PERIOD_PROFILES[f]
        check("{} bar_secs 与 FREQ_SEC 一致".format(f), p.bar_secs, FREQ_SEC[f])
        check("{} note 非空（有标定记录）".format(f), bool(p.note.strip()), True)

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

    print("\n[5] apply_period_profile：改 freq 后重对齐")
    c2 = TradingConfig()
    c2.source.freq = "30m"
    c2.apply_period_profile()
    check("改 freq=30m 后 period_profile=30m", c2.period_profile.freq, "30m")
    check("30m profile.bar_secs=1800", c2.period_profile.bar_secs, 1800)

    print("\n[6] period_sensitive_summary 派生视图")
    rows = period_sensitive_summary()
    check("summary 4 行", len(rows), 4)
    check("summary 覆盖全部周期", sorted(r["freq"] for r in rows),
          ["15s", "1m", "30m", "5m"])
    r5 = next(r for r in rows if r["freq"] == "5m")
    check("summary 5m bar_secs=300", r5["bar_secs"], 300)
    check("summary 行不含退役的 signal_max_age_minutes",
          "signal_max_age_minutes" not in r5, True)
    check("summary 行含 note", bool(r5["note"].strip()), True)

    print("\n[7] ProductProfile 品种档案：随品种参数影子覆盖（2026-09-09）")
    check("PRODUCT_PROFILES 仍是 4 品种", len(PRODUCT_PROFILES), 4)
    check("parse KQ.m@CFFEX.IF → IF", parse_product("KQ.m@CFFEX.IF"), "IF")
    check("parse KQ.m@CFFEX.IC → IC", parse_product("KQ.m@CFFEX.IC"), "IC")
    check("parse 无点串 → ''", parse_product("IF2609"), "")
    check("parse 月合约 CFFEX.IF2609 → IF2609（非品种代码用 parse 前先取品种）",
          parse_product("CFFEX.IF2609"), "IF2609")

    c_if = TradingConfig()
    check("IF min_r_points=3.0", c_if.exit_params.min_r_points, 3.0)
    check("IF r_multiple_tp=2.0", c_if.exit_params.r_multiple_tp, 2.0)
    check("IF multiplier=300.0", c_if.instrument.multiplier, 300.0)
    check("IF 生效的品种档案 product=IF", c_if.product_profile.product, "IF")

    c_ic = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.IC"})
    check("IC min_r_points=5.0", c_ic.exit_params.min_r_points, 5.0)
    check("IC r_multiple_tp=3.0", c_ic.exit_params.r_multiple_tp, 3.0)
    check("IC multiplier=200.0", c_ic.instrument.multiplier, 200.0)

    # 未知品种：不套任何品种档案，保留 flat 默认（= IF 基线）
    c_unk = TradingConfig(instrument={"signal_symbol": "KQ.m@CFFEX.XX"})
    check("未知品种 product_profile=None", c_unk.product_profile, None)
    check("未知品种 min_r_points 保默认 3.0", c_unk.exit_params.min_r_points, 3.0)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
