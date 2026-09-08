# -*- coding: utf-8 -*-
"""
四周期回归矩阵（30m / 5m / 1m / 15s × 毫秒源 / 秒源）
=====================================================
Step 1 的产物：**文档会过时，测试不会**。这份测试把"换个周期代码还能不能
正确跑通"钉成可执行的断言 —— 以后谁再动周期相关代码，改错就红。

2026-09-08 精简：原 BUG-3（EOD 收盘强平）、BUG-5（max_hold_bars 根数语义）、
BUG-6（追价窗口）随 L4 时间/收盘兜底与风控硬闸门一并删除，相关测试块移除。
本测试只保留**周期秒数与时间戳归一**相关的断言：
    · freq → bar_secs 映射（bar_secs_for）
    · 时间戳单位判定（ts_scale / norm_delta_sec）
    · BUG-1 旧口径复算（毫秒当秒 → 推断失败）
    · parse_hhmmss 带秒/不带秒解析

跑法：python test_period_matrix.py
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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Infra.PeriodProfile import (  # noqa: E402
    bar_secs_for, norm_delta_sec, parse_hhmmss, ts_scale,
)

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


FREQ_CASES = [
    ("15s", 15),
    ("1m", 60),
    ("5m", 300),
    ("30m", 1800),
]


def main():
    print("=" * 60)
    print("四周期回归矩阵：{}".format(", ".join(f for f, _ in FREQ_CASES)))
    print("=" * 60)

    # ── [1] 周期 → 秒 ────────────────────────────────────────────
    print("\n[1] freq → bar_secs 映射")
    for freq, secs in FREQ_CASES:
        check("bar_secs_for('{}') = {}".format(freq, secs),
              bar_secs_for(freq), secs)

    # ── [2] 时间戳单位判定（毫秒 / 秒）────────────────────────────
    print("\n[2] 时间戳单位判定")
    check("毫秒时间戳（1.7e12）→ 除数 1000", ts_scale(1756000000000), 1000.0)
    check("秒时间戳（1.7e9）→ 除数 1", ts_scale(1756000000), 1.0)
    check("norm_delta_sec 毫秒 300000 → 300 秒", norm_delta_sec(300000, 0), 300.0)
    check("norm_delta_sec 秒 300 → 300 秒", norm_delta_sec(300, 0), 300.0)

    # ── [3b] 旧口径复算（回归证据：修复前 4 个周期全部失效）──────
    print("\n[3b] 旧口径复算（回归证据：修复前 4 个周期全部失效）")
    for freq, secs in FREQ_CASES:
        # 旧代码：secs = int(bar.timestamp - prev_ts); if 60 <= secs <= 14400
        raw_ms = secs * 1000                      # SSE 源实际是毫秒
        old_ok = 60 <= raw_ms <= 14400
        check("{} 旧口径（毫秒当秒比 60~14400）判定失败".format(freq),
              old_ok, False)

    # ── [6] date 带秒的解析 ───────────────────────────────────────
    print("\n[6] 15s 的 date 带秒：解析不能丢秒")
    check("parse_hhmmss('14:54:45') = 53685", parse_hhmmss("14:54:45"), 53685)
    check("parse_hhmmss('14:54') = 53640", parse_hhmmss("14:54"), 53640)
    check("parse_hhmmss 兼容 '2026-09-01 14:54:45' 取时分秒",
          parse_hhmmss("2026-09-01 14:54:45"), 53685)
    check("旧口径 s[:5] 会丢 45 秒（53640 vs 53685）", 53685 - 53640, 45)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()