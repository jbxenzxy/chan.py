# -*- coding: utf-8 -*-
"""Step 2.4 专项测试：重复常量合并（CFFEX 单笔上限 20）+ 删 0.15 重复兜底。

背景（Step 2 路线图 2.4，~30 行最小 phase）：
  · 语义相同的两个 `20`：Risk/PositionSizing.py 的 sizing.max_volume 默认截断上限
    与 Engine/Engine.py 开仓前的交易所限单检查上限 → 收口为单一常量 CFFEX_LIMIT_MAX。
  · 仓位管理精简（2026-09-08）：仅固定手数，动态模式与保证金/资金闸门相关字段
    （mode / capital_pct / margin_rate / risk_per_trade_pct / equity_source）已从
    SizingConfig 删除，per_lot_margin 亦移除。

独立脚本风格（与全套 test_*.py 一致）：check() + 末尾统计 + sys.exit(1)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

_PASSED = 0
_FAILED = 0


def check(name, got, expected):
    global _PASSED, _FAILED
    ok = got == expected
    if ok:
        _PASSED += 1
        print("  [PASS] {}".format(name))
    else:
        _FAILED += 1
        print("  [FAIL] {}\n         got={}, expected={}".format(name, got, expected))


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


print("=" * 60)
print("Step 2.4 测试：CFFEX_LIMIT_MAX 合并 + sizing 精简")
print("=" * 60)

# ═══ [1] 常量唯一源 ═══
print("\n[1] CFFEX_LIMIT_MAX 单一定义、值不变")
import Trading.Risk.PositionSizing as PS
check("CFFEX_LIMIT_MAX == 20", PS.CFFEX_LIMIT_MAX, 20)
check("旧名 _CFFEX_SINGLE_ORDER_MAX 已删除",
      hasattr(PS, "_CFFEX_SINGLE_ORDER_MAX"), False)
check("CFFEX_LIMIT_MAX 是 int", isinstance(PS.CFFEX_LIMIT_MAX, int), True)

# ═══ [2] Engine 引用同一对象（SSOT 实证）═══
print("\n[2] Engine 与 PositionSizing 共用同一常量对象")
import Trading.Engine.Engine as ENG
check("Engine 模块可见 CFFEX_LIMIT_MAX",
      hasattr(ENG, "CFFEX_LIMIT_MAX"), True)
check("Engine.CFFEX_LIMIT_MAX is PositionSizing.CFFEX_LIMIT_MAX（同一对象）",
      ENG.CFFEX_LIMIT_MAX is PS.CFFEX_LIMIT_MAX, True)

# 静态防回归：_do_open 源码不得再出现局部赋值
src_open = inspect.getsource(ENG.TradingEngine._open_position)
check("_do_open 不再含 '_CFFEX_LIMIT_MAX = 20' 局部赋值",
      "_CFFEX_LIMIT_MAX = 20" in src_open, False)
check("_do_open 引用 CFFEX_LIMIT_MAX",
      "CFFEX_LIMIT_MAX" in src_open, True)
# 整个 Risk/Engine 不得再出现旧名
import pathlib
_root = pathlib.Path(PS.__file__).parent.parent
_residue = []
for _p in [_root / "Risk" / "PositionSizing.py", _root / "Engine" / "Engine.py"]:
    if "_CFFEX_SINGLE_ORDER_MAX" in _p.read_text(encoding="utf-8"):
        _residue.append(_p.name)
check("旧常量名全树 0 残留", _residue, [])

# ═══ [3] sizing.max_volume 默认截断仍用该常量 ═══
print("\n[3] sizing.max_volume 默认（0）→ 截断上限 = CFFEX_LIMIT_MAX")
from Trading.Config import SizingConfig
from Trading.Infra.InstrumentSpec import InstrumentSpec
spec = InstrumentSpec()  # 默认 IF 规格
sz = PS.PositionSizer(
        SizingConfig(enabled=True, fixed_volume=0,
                     max_volume=0, min_volume=1, fallback_volume=1),
        spec, risk_max_volume=1)
check("max_volume 默认 == CFFEX_LIMIT_MAX", sz.max_volume, PS.CFFEX_LIMIT_MAX)
check("显式 max_volume=5 不受影响",
      PS.PositionSizer(
          SizingConfig(enabled=True, fixed_volume=0,
                       max_volume=5, min_volume=1, fallback_volume=1),
          spec, risk_max_volume=1).max_volume, 5)

# ═══ 汇总 ═══
print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASSED, _FAILED))
print("=" * 60)
sys.exit(1 if _FAILED else 0)
