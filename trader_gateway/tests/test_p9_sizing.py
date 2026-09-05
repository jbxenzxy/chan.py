# -*- coding: utf-8 -*-
"""
P9 仓位管理（手数定档）单元测试
================================
背景
    加这个模块之前，开仓手数是硬编码的 `int(cfg.risk.max_volume)`（默认 1 手），
    跟账户里有多少钱完全无关 —— 100 万也只开 1 手。
    P9 新增 PositionSizer 回答"这笔开几手"，三种模式：
      fixed        固定手数
      capital_pct  按保证金占比：手数 = 权益 × pct / (现价 × 乘数 × 保证金率)
      atr_risk     按风险敞口：手数 = 权益 × risk_pct / (止损距离 × 乘数)

硬性要求（本测试锁死）
    ① **默认关闭**：DEFAULT_CONFIG 里 sizing.enabled 必须是 False，
       关闭时无论传多少权益都返回固定手数 —— 保证引入这个模块零行为变化。
    ② **取不到权益就保守回退**，绝不能因为账户查询失败就乱开仓。
    ③ NaN / 0 / 负数权益必须被拦（tqsdk 在集合竞价/断连时确实会给出 NaN）。
    ④ 手数永远要过 max_volume 硬上限。

本测试不联网、不依赖 tqsdk。
跑法：python tests/test_p9_sizing.py
"""
from __future__ import annotations

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

try:
    from tg.config import DEFAULT_CONFIG, GatewayConfig  # noqa: E402
    from tg.sizing import PositionSizer  # noqa: E402
    from tg.symbols import InstrumentSpec  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    print("  tg 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)

print("[import] 被测类来自: {}/tg/sizing.py（真实代码，非副本）".format(_TG_ROOT))

# ---------------- 测试基建 ----------------
_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    if isinstance(got, float) and isinstance(want, float):
        ok = abs(got - want) < 1e-9
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def make_spec() -> InstrumentSpec:
    return InstrumentSpec(signal_symbol="KQ.m@CFFEX.IF",
                          trade_symbol="CFFEX.IF2609",
                          price_tick=0.2, multiplier=300.0)


SPEC = make_spec()
# IF@4550，乘数 300，保证金 15% → 每手保证金 = 4550×300×0.15 = 204,750 元
PRICE = 4550.0


def make_sizer(**over) -> PositionSizer:
    """构造 PositionSizer；未覆盖的字段走默认（enabled=False, min_volume=1,
    fallback_volume=1, max_volume 沿用 risk.max_volume=1）。"""
    base = {"enabled": True, "mode": "fixed", "fixed_volume": 0,
            "capital_pct": 0.0, "risk_per_trade_pct": 0.0, "margin_rate": 0.15,
            "max_volume": 0, "min_volume": 1, "fallback_volume": 1,
            "equity_source": "available"}
    base.update(over)
    return PositionSizer(base, SPEC, risk_max_volume=1)


# =========================================================
print("\n[1] 默认关闭：固定手数，完全不看权益（引入模块零行为变化）")
# ---- 1a. DEFAULT_CONFIG 必须是关闭的（这条是"默认不变更"的契约）----
check("DEFAULT_CONFIG.sizing.enabled == False",
      DEFAULT_CONFIG.get("sizing", {}).get("enabled"), False)
_cfg = GatewayConfig.from_dict(DEFAULT_CONFIG)
check("GatewayConfig 能解析出 sizing 段", isinstance(_cfg.sizing, dict), True)
check(" GatewayConfig.sizing.enabled == False", _cfg.sizing.get("enabled"), False)

_s_default = PositionSizer(_cfg.sizing, _cfg.instrument, _cfg.risk.max_volume)
check("默认 sizer.enabled == False", _s_default.enabled, False)
check("默认：不传权益 -> 1 手", _s_default.size(equity=None, price=PRICE)[0], 1)
check("默认：100 万权益 -> 仍然 1 手",
      _s_default.size(equity=1_000_000.0, price=PRICE)[0], 1)
check("默认：1 亿权益 -> 仍然 1 手",
      _s_default.size(equity=100_000_000.0, price=PRICE)[0], 1)
check("默认：原因串标记 disabled",
      _s_default.size(equity=1_000_000.0, price=PRICE)[1], "sizing:disabled(fixed)")

# ---- 1b. 关闭但显式配了 fixed_volume ----
_s_fix3 = make_sizer(enabled=False, fixed_volume=3)
check("关闭 + fixed_volume=3 -> 3 手", _s_fix3.size(equity=1_000_000.0, price=PRICE)[0], 3)

# ---- 1c. 关闭时 fixed_volume=0 -> 沿用 risk.max_volume ----
_s_fix0 = PositionSizer({"enabled": False, "fixed_volume": 0}, SPEC, risk_max_volume=4)
check("关闭 + fixed_volume=0 -> 沿用 risk.max_volume=4",
      _s_fix0.size(equity=1_000_000.0, price=PRICE)[0], 4)


# =========================================================
print("\n[2] capital_pct 模式：手数 = 权益 × pct / 每手保证金")
# 每手保证金 = 4550 × 300 × 0.15 = 204,750
check("每手保证金 per_lot_margin(4550)",
      make_sizer(mode="capital_pct").per_lot_margin(PRICE), 204750.0)
check("每手保证金 @ price=0 -> 0（防除零）",
      make_sizer(mode="capital_pct").per_lot_margin(0.0), 0.0)

# 100 万 × 50% = 50 万 / 20.475 万 = 2.442 → 2 手（max_volume=10 不截）
_s_cap = make_sizer(mode="capital_pct", capital_pct=0.50, max_volume=10)
check("100万 pct=0.50 -> 2 手", _s_cap.size(equity=1_000_000.0, price=PRICE)[0], 2)
# 100 万 × 100% = 100 万 / 20.475 万 = 4.884 → 4 手（"全仓开满"）
_s_full = make_sizer(mode="capital_pct", capital_pct=1.00, max_volume=10)
check("100万 pct=1.00（全仓） -> 4 手", _s_full.size(equity=1_000_000.0, price=PRICE)[0], 4)
# 100 万 × 20% = 20 万 / 20.475 万 = 0.9767 → floor 0 → min_volume=1 提升到 1
_s_small = make_sizer(mode="capital_pct", capital_pct=0.20, max_volume=10)
_v, _why = _s_small.size(equity=1_000_000.0, price=PRICE)
check("100万 pct=0.20 只够 0.97 手 -> 提升为 1 手", _v, 1)
check(" 原因串含 raised(min_volume)", "raised(min_volume=1)" in _why, True)
# min_volume=0 时则真的不开仓
_s_zero = make_sizer(mode="capital_pct", capital_pct=0.20, max_volume=10, min_volume=0)
check("同上但 min_volume=0 -> 0 手（不开仓）",
      _s_zero.size(equity=1_000_000.0, price=PRICE)[0], 0)
# 200 万 × 50% = 100 万 / 20.475 万 = 4.884 → 4 手
check("200万 pct=0.50 -> 4 手", _s_cap.size(equity=2_000_000.0, price=PRICE)[0], 4)


# =========================================================
print("\n[3] atr_risk 模式：手数 = 权益 × risk_pct / (止损距离 × 乘数)")
check("每手风险 per_lot_risk(stop=8)",
      make_sizer(mode="atr_risk").per_lot_risk(8.0, None), 2400.0)
check("止损距为 0 时回落到 ATR: per_lot_risk(0, atr=4)",
      make_sizer(mode="atr_risk").per_lot_risk(0.0, 4.0), 1200.0)
check("止损距与 ATR 都无效 -> 0（防除零）",
      make_sizer(mode="atr_risk").per_lot_risk(None, None), 0.0)
check("止损距为 NaN 时回落到 ATR",
      make_sizer(mode="atr_risk").per_lot_risk(float("nan"), 5.0), 1500.0)

# 100 万 × 1% = 1 万风险预算；止损 8 点 → 每手风险 2400 → 4.1667 → 4 手
_s_risk = make_sizer(mode="atr_risk", risk_per_trade_pct=0.01, max_volume=10)
check("100万 risk=1% 止损8点 -> 4 手",
      _s_risk.size(equity=1_000_000.0, price=PRICE, stop_distance_points=8.0)[0], 4)
# 止损距取不到 → 回落 ATR=4 点 → 每手风险 1200 → 8.33 → 8 手
check("止损距缺失，ATR=4 -> 8 手",
      _s_risk.size(equity=1_000_000.0, price=PRICE,
                   stop_distance_points=None, atr_points=4.0)[0], 8)
# 止损距极小（2 点）→ 每手风险 600 → 16.67 → 被 max_volume=10 截断
check("止损距仅 2 点 -> 算 16 手，被 max_volume=10 截断",
      _s_risk.size(equity=1_000_000.0, price=PRICE, stop_distance_points=2.0)[0], 10)
_v, _why = _s_risk.size(equity=1_000_000.0, price=PRICE, stop_distance_points=2.0)
check(" 原因串含 capped(max_volume)", "capped(max_volume=10)" in _why, True)
# risk=2% → 2 万 / 2400 = 8.33 → 8 手
check("risk=2% 止损8点 -> 8 手",
      make_sizer(mode="atr_risk", risk_per_trade_pct=0.02, max_volume=20).size(
          equity=1_000_000.0, price=PRICE, stop_distance_points=8.0)[0], 8)


# =========================================================
print("\n[4] 权益缺失 / 脏数据：一律回退 fallback_volume，绝不乱开仓")
_s_fb = make_sizer(mode="capital_pct", capital_pct=1.0, max_volume=10)
check("equity=None -> fallback 1 手", _s_fb.size(equity=None, price=PRICE)[0], 1)
check("equity=0 -> fallback 1 手", _s_fb.size(equity=0.0, price=PRICE)[0], 1)
check("equity=-5万 -> fallback 1 手", _s_fb.size(equity=-50000.0, price=PRICE)[0], 1)
check("equity=NaN -> fallback 1 手", _s_fb.size(equity=float("nan"), price=PRICE)[0], 1)
check("equity=inf -> fallback 1 手", _s_fb.size(equity=float("inf"), price=PRICE)[0], 1)
check("equity=字符串 -> fallback 1 手", _s_fb.size(equity="abc", price=PRICE)[0], 1)
check("equity=None 原因串标记 no_equity",
      "no_equity" in _s_fb.size(equity=None, price=PRICE)[1], True)
# fallback 可配
_s_fb5 = make_sizer(mode="capital_pct", capital_pct=1.0, max_volume=10,
                    fallback_volume=5)
check("fallback_volume=5 时回退 5 手", _s_fb5.size(equity=None, price=PRICE)[0], 5)


# =========================================================
print("\n[5] 上下限截断")
# max_volume 硬上限（即便 risk.max_volume 更大，sizing.max_volume 说了算）
_s_cap2 = make_sizer(mode="capital_pct", capital_pct=1.0, max_volume=2)
check("算 4 手但 max_volume=2 -> 2 手", _s_cap2.size(equity=1_000_000.0, price=PRICE)[0], 2)
# max_volume=0 表示沿用 risk.max_volume
_s_inherit = PositionSizer({"enabled": True, "mode": "capital_pct",
                            "capital_pct": 1.0, "max_volume": 0},
                           SPEC, risk_max_volume=3)
check("max_volume=0 -> 沿用 risk.max_volume=3",
      _s_inherit.size(equity=1_000_000.0, price=PRICE)[0], 3)
# min_volume 提升不能超过 max_volume
_s_bad = make_sizer(mode="capital_pct", capital_pct=0.01, max_volume=0, min_volume=5)
check("min_volume(5) > max_volume(0→risk 1) -> 不提升，返回 0",
      _s_bad.size(equity=1_000_000.0, price=PRICE)[0], 0)


# =========================================================
print("\n[6] 参数异常：坏参数回退，不抛异常")
check("capital_pct=0 -> fallback", make_sizer(mode="capital_pct", capital_pct=0.0).size(
    equity=1_000_000.0, price=PRICE)[0], 1)
check("risk_pct=0 -> fallback", make_sizer(mode="atr_risk", risk_per_trade_pct=0.0).size(
    equity=1_000_000.0, price=PRICE, stop_distance_points=8.0)[0], 1)
check("price=0（per_lot_margin=0）-> fallback",
      make_sizer(mode="capital_pct", capital_pct=1.0).size(
          equity=1_000_000.0, price=0.0)[0], 1)
check("price=NaN -> fallback", make_sizer(mode="capital_pct", capital_pct=1.0).size(
    equity=1_000_000.0, price=float("nan"))[0], 1)
# 非法 mode 当作 fixed
_s_badmode = make_sizer(mode="no_such_mode", fixed_volume=2, max_volume=10)
check("非法 mode -> 退化 fixed", _s_badmode.mode, "fixed")
check("非法 mode 取 fixed_volume=2", _s_badmode.size(equity=1_000_000.0, price=PRICE)[0], 2)
# margin_rate=0 时按默认 15% 兜底
check("margin_rate=0 -> 内部兜底 15%",
      make_sizer(mode="capital_pct", margin_rate=0.0).per_lot_margin(PRICE), 204750.0)


# =========================================================
print("\n[7] equity_source 与 describe()")
check("默认 equity_source=available",
      make_sizer().equity_source, "available")
check("显式 balance", make_sizer(equity_source="balance").equity_source, "balance")
check("非法 equity_source -> available",
      make_sizer(equity_source="xxx").equity_source, "available")
_d = make_sizer(mode="atr_risk", risk_per_trade_pct=0.01, max_volume=5).describe()
check("describe() 含 mode", _d["mode"], "atr_risk")
check("describe() 含 max_volume", _d["max_volume"], 5)
check("describe() 含 risk_per_trade_pct", _d["risk_per_trade_pct"], 0.01)


# =========================================================
print("\n[8] 真实场景回归：100 万本金、IF@4550，各档位手数")
_rows = []
for pct in (0.2, 0.4, 0.5, 0.8, 1.0):
    s = make_sizer(mode="capital_pct", capital_pct=pct, max_volume=20)
    _rows.append((pct, s.size(equity=1_000_000.0, price=PRICE)[0]))
print("     capital_pct ->  手数:", _rows)
check("pct 单调递增时手数不递减",
      all(_rows[i][1] <= _rows[i + 1][1] for i in range(len(_rows) - 1)), True)
check("pct=0.2 时 1 手（资金只够 1 手）", _rows[0][1], 1)
check("pct=1.0 时 4 手（全仓）", _rows[-1][1], 4)

_rows2 = []
for stop in (2.0, 4.0, 8.0, 16.0):
    s = make_sizer(mode="atr_risk", risk_per_trade_pct=0.01, max_volume=20)
    _rows2.append((stop, s.size(equity=1_000_000.0, price=PRICE,
                                stop_distance_points=stop)[0]))
print("     止损距离(点) -> 手数:", _rows2)
check("止损越宽手数越少",
      all(_rows2[i][1] >= _rows2[i + 1][1] for i in range(len(_rows2) - 1)), True)
check("止损 8 点 -> 4 手", dict(_rows2)[8.0], 4)

print("\n[9] 与 RiskGate 的集成：手数上限不能互相打架")
# 曾经踩过的坑：sizing 算出 4 手，但 RiskGate 仍按 risk.max_volume=1 拦截，
# 结果启用仓位管理后一笔都开不出来。修复办法是引擎把 sizer 的有效上限
# 透传给 check_open 的 max_volume 参数。
from tg.risk import RiskGate  # noqa: E402
from tg.config import RiskConfig  # noqa: E402
from tg.types import Side  # noqa: E402

_gate = RiskGate(RiskConfig(max_volume=1), SPEC)
check("风控默认上限 1：4 手被拦",
      _gate.check_open(Side.LONG, 4, "2026-09-01 09:35")[0], False)
check(" 拦截原因提到上限 1",
      "上限 1" in _gate.check_open(Side.LONG, 4, "2026-09-01 09:35")[1], True)
# 透传 sizer 的有效上限后放行
check("传 max_volume=20 后 4 手放行",
      _gate.check_open(Side.LONG, 4, "2026-09-01 09:35", max_volume=20)[0], True)
check(" 超过 20 手才拦",
      _gate.check_open(Side.LONG, 21, "2026-09-01 09:35", max_volume=20)[0], False)
# 不传时行为与改动前完全一致（向后兼容）
check("省略 max_volume -> 仍用 RiskConfig.max_volume=1",
      _gate.check_open(Side.LONG, 1, "2026-09-01 09:35")[0], True)
# sizing 关闭时 sizer.max_volume 应等于 risk.max_volume —— 保证透传不改变默认行为
_s_off = PositionSizer(_cfg.sizing, _cfg.instrument, _cfg.risk.max_volume)
check("sizing 关闭时 sizer.max_volume == risk.max_volume",
      _s_off.max_volume, _cfg.risk.max_volume)

print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
