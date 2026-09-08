# -*- coding: utf-8 -*-
"""
P9 仓位管理（手数定档）单元测试 —— 2026-09-08 精简版
=============================================
背景
    仓位管理只保留**固定手数**功能：开几手完全由配置决定，不查账户、不联网、
    不看权益/价格/止损距/ATR。
    已删除的动态模式（capital_pct 按保证金占比 / atr_risk 按风险敞口）与
    资金闸门相关字段（risk_unit_pct / equity_source）随功能一并移除。

硬性要求（本测试锁死）
    ① **默认关闭**：DEFAULT_CONFIG 里 sizing.enabled 必须是 False，
       关闭时返回固定手数 —— 保证引入这个模块零行为变化（默认不传 → 沿用 risk.max_volume=2）。
    ② 手数永远要过 max_volume 硬上限（默认=中金所单笔上限 20）。
    ③ 手数低于 min_volume 时提升到 min_volume（min>max 则不开仓）。
    ④ 严格模式：未知配置键 / 非法参数（裸 dict、None）在构造期直接报错（fail-fast）。

本测试不联网、不依赖 tqsdk。
跑法：python tests/test_p9_sizing.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading.Config import DEFAULT_CONFIG, TradingConfig, SizingConfig  # noqa: E402
    from Trading.Risk.PositionSizing import PositionSizer  # noqa: E402
    from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    print("  Trading 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)

print("[import] 被测类来自: {}/Risk/PositionSizing.py（真实代码，非副本）".format(_TG_ROOT))

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
PRICE = 4550.0   # 仅作演示用（固定手数不再消费价格/权益）


def _raises(fn) -> bool:
    """严格模式断言用：fn() 必须抛异常。"""
    try:
        fn()
    except Exception:
        return True
    return False


def make_sizer(**over) -> PositionSizer:
    """构造 PositionSizer；未覆盖的字段走默认（enabled=True, fixed_volume=0,
    min_volume=1, fallback_volume=1, max_volume=0 表示默认中金所单笔上限 20）。"""
    base = {"enabled": True, "fixed_volume": 0,
            "max_volume": 0, "min_volume": 1, "fallback_volume": 1}
    base.update(over)
    return PositionSizer(SizingConfig(**base), SPEC, risk_max_volume=1)


# =========================================================
print("\n[1] 固定手数：完全不看权益/价格（引入模块零行为变化）")
# ---- 1a. DEFAULT_CONFIG 必须是关闭的（这条是"默认不变更"的契约）----
check("DEFAULT_CONFIG.sizing.enabled == False",
      DEFAULT_CONFIG.get("sizing", {}).get("enabled"), False)
_cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
check("TradingConfig 能解析出 sizing 段", isinstance(_cfg.sizing, SizingConfig), True)
check(" TradingConfig.sizing.enabled == False", _cfg.sizing.enabled, False)

_s_default = PositionSizer(_cfg.sizing, _cfg.instrument, _cfg.risk.max_volume)
check("默认 sizer.enabled == False", _s_default.enabled, False)
check("默认 risk.max_volume == 2", _cfg.risk.max_volume, 2)
# 固定手数下 0 保存算数值，沿用 risk.max_volume=2；size() 忽略权益/价格入参
check("默认：不传权益 -> 2 手", _s_default.size(equity=None, price=PRICE)[0], 2)
check("默认：100 万权益 -> 仍然 2 手",
      _s_default.size(equity=1_000_000.0, price=PRICE)[0], 2)
check("默认：1 亿权益 -> 仍然 2 手",
      _s_default.size(equity=100_000_000.0, price=PRICE)[0], 2)
check("默认：权益为 NaN/负值也照开 2 手（不看权益）",
      _s_default.size(equity=float("nan"), price=PRICE)[0], 2)
check("默认：原因串标记 sizing:fixed",
      _s_default.size(equity=1_000_000.0, price=PRICE)[1], "sizing:fixed")

# ---- 1b. 显式配 fixed_volume ----
_s_fix3 = make_sizer(enabled=False, fixed_volume=3)
check("fixed_volume=3 -> 3 手", _s_fix3.size(equity=1_000_000.0, price=PRICE)[0], 3)

# ---- 1c. fixed_volume=0 -> 沿用 risk.max_volume ----
_s_fix0 = PositionSizer(SizingConfig(enabled=False, fixed_volume=0), SPEC,
                         risk_max_volume=4)
check("fixed_volume=0 -> 沿用 risk.max_volume=4",
      _s_fix0.size(equity=1_000_000.0, price=PRICE)[0], 4)


# =========================================================
print("\n[2] 上下限截断")
# 超过中金所单笔上限 20 被截断（默认 max_volume=0 -> CFFEX_LIMIT_MAX=20）
_s_cap = make_sizer(fixed_volume=25)
check("fixed_volume=25 超默认 20 上限 -> 截为 20 手", _s_cap.size()[0], 20)
check(" 原因串含 capped(max_volume=20)",
      "capped(max_volume=20)" in _s_cap.size()[1], True)
# 显式 max_volume=5
_s_cap5 = make_sizer(fixed_volume=25, max_volume=5)
check("显式 max_volume=5 -> 截为 5 手", _s_cap5.size()[0], 5)
# max_volume=0 表示默认中金所单笔上限 20（与 risk.max_volume 解耦）
_s_inherit = PositionSizer(SizingConfig(enabled=True, fixed_volume=0, max_volume=0),
                            SPEC, risk_max_volume=3)
check("max_volume=0 -> 默认 20（与 risk.max_volume 解耦）",
      _s_inherit.max_volume, 20)
check("max_volume=0(默认20) 时 fixed_volume=0 -> 沿用 3", _s_inherit.size()[0], 3)
# 下限提升：算出来少于 min_volume 则提升到 min_volume
_s_min = make_sizer(fixed_volume=0, min_volume=3, max_volume=5)
check("fixed_volume=0(基础1) < min_volume=3 -> 提升到 3 手", _s_min.size()[0], 3)
check(" 原因串含 raised(min_volume=3)",
      "raised(min_volume=3)" in _s_min.size()[1], True)
# min_volume > max_volume 时不提升，直接不开仓
_s_bad = make_sizer(fixed_volume=0, min_volume=5, max_volume=3)
check("min_volume(5) > max_volume(3) -> 不提升，返回 0（不开仓）", _s_bad.size()[0], 0)
check(" 原因串含 below_min(no_open)",
      "below_min(no_open)" in _s_bad.size()[1], True)


# =========================================================
print("\n[3] 参数异常：严格模式 fail-fast")
check("严格模式：SizingConfig 未知键报错",
      _raises(lambda: SizingConfig(bogus_key=1)), True)
check("严格模式：PositionSizer 传裸 dict 报错",
      _raises(lambda: PositionSizer({"enabled": True})), True)
check("严格模式：PositionSizer 传 None 报错",
      _raises(lambda: PositionSizer(None)), True)


# =========================================================
print("\n[4] describe() 快照")
_d = make_sizer(fixed_volume=3, max_volume=5).describe()
check("describe() 含 mode=fixed", _d["mode"], "fixed")
check("describe() 含 fixed_volume", _d["fixed_volume"], 3)
check("describe() 含 max_volume", _d["max_volume"], 5)
check("describe() 含 min_volume", _d["min_volume"], 1)
check("describe() 含 fallback_volume", _d["fallback_volume"], 1)
check("describe() 含 unlock_no_new_open=True",
      make_sizer().describe()["unlock_no_new_open"], True)


# =========================================================
print("\n[5] unlock_no_new_open 默认值（2026-09-07：锁死\"不补开\"默认）")
# 修复背景：全 FOK 重构时把默认值误设成 False（补开），与设计
# 「解锁只平昨仓、绝不新开今仓（规避平今高手续费）」相反。本组断言锁死默认方向。
check("不显式配置 -> unlock_no_new_open == True",
      PositionSizer(SizingConfig(), SPEC,
                    risk_max_volume=1).unlock_no_new_open, True)
check("make_sizer() 未传 -> 默认 True",
      make_sizer().unlock_no_new_open, True)
check("DEFAULT_CONFIG.sizing.unlock_no_new_open == True",
      DEFAULT_CONFIG.get("sizing", {}).get("unlock_no_new_open"), True)
check("显式 True -> 不补开",
      PositionSizer(SizingConfig(unlock_no_new_open=True), SPEC,
                    risk_max_volume=1).unlock_no_new_open, True)
check("显式 False -> 补开（可选能力，需主动关闭）",
      PositionSizer(SizingConfig(unlock_no_new_open=False), SPEC,
                    risk_max_volume=1).unlock_no_new_open, False)

print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)