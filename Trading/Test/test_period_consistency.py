# -*- coding: utf-8 -*-
"""
周期表对账测试：Trading 自持 FREQ_SEC ↔ 主程序 Common.CEnum.FREQ_SEC_MAP
========================================================================
Trading 为了保持"对 chan.py 零 import、独立部署"的架构约定，自己维护了一份
`freq → 秒` 映射（Trading/Infra/PeriodProfile.py 的 FREQ_SEC）。

代价是两份表可能漂移：主程序在 Common/CEnum.py 新增周期或改了秒数，
而 Trading 没跟上 → 网关会按错误的 bar 秒数去算时间止损与收盘强平，
而且**不会报错**，只是悄悄算错。

本测试是防止漂移的唯一手段（应在 CI 里跑）：
    · 正向：Trading 表里的每个周期，主程序必须有一致的定义
    · 反向：主程序"期货可用周期"里的项，Trading 表必须都覆盖
主程序 import 失败（依赖缺失 / 不在仓库内）时跳过并告警，不算通过。

跑法：python test_period_consistency.py
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

from Trading.Infra.PeriodProfile import (  # noqa: E402
    FREQ_SEC, PERIOD_PROFILES, PeriodProfile, SUPPORTED_FREQS, bar_secs_for,
)

_PASS = 0
_FAIL = 0
_SKIPPED = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> {}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def main():
    global _SKIPPED
    print("\n[1] Trading 自持周期表")
    check("支持 4 个周期", len(SUPPORTED_FREQS), 4)
    check("15s = 15 秒", FREQ_SEC.get("15s"), 15)
    check("1m = 60 秒", FREQ_SEC.get("1m"), 60)
    check("5m = 300 秒", FREQ_SEC.get("5m"), 300)
    check("30m = 1800 秒", FREQ_SEC.get("30m"), 1800)
    check("SUPPORTED_FREQS 按粗细升序", list(SUPPORTED_FREQS),
          ["15s", "1m", "5m", "30m"])

    print("\n[2] PeriodProfile 档案自洽")
    for f in SUPPORTED_FREQS:
        p = PERIOD_PROFILES[f]
        check("{} 档案 bar_secs 与 FREQ_SEC 一致".format(f),
              p.bar_secs, FREQ_SEC[f])
    # 档案构造会校验与 FREQ_SEC 一致 → 人为写错必须抛异常
    try:
        PeriodProfile(freq="5m", bar_secs=60)
        check("档案写错秒数应抛异常", "no-raise", "ValueError")
    except ValueError:
        check("档案写错秒数抛 ValueError", True, True)

    print("\n[3] bar_secs_for 严格模式")
    check("已知周期返回秒数", bar_secs_for("5m"), 300)
    check("未知周期默认 None", bar_secs_for("7m"), None)
    check("未知周期可给 default", bar_secs_for("7m", default=0), 0)
    try:
        bar_secs_for("7m", strict=True)
        check("strict 未知周期应抛异常", "no-raise", "ValueError")
    except ValueError:
        check("strict 未知周期抛 ValueError", True, True)

    # ── 与主程序对账 ──
    print("\n[4] 与主程序 Common.CEnum.FREQ_SEC_MAP 对账")
    try:
        from Common.CEnum import FREQ_SEC_MAP as MAIN_FREQ_SEC_MAP
        from Common.CEnum import FREQ_TABLE as MAIN_FREQ_TABLE
    except Exception as e:
        print("⚠ 跳过：主程序 Common.CEnum 导入失败（{}: {}）".format(
            type(e).__name__, e))
        _SKIPPED += 1
        MAIN_FREQ_SEC_MAP = None
        MAIN_FREQ_TABLE = None

    if MAIN_FREQ_SEC_MAP:
        for f, secs in sorted(FREQ_SEC.items(), key=lambda kv: kv[1]):
            check("{} 主程序也是 {} 秒".format(f, secs),
                  MAIN_FREQ_SEC_MAP.get(f), secs)
        # 反向：主程序登记的"期货可用周期"必须都被 Trading 覆盖
        try:
            from App.AppConfig import FUTURES_FREQS  # noqa: E402
        except Exception:
            FUTURES_FREQS = None
        if FUTURES_FREQS:
            missing = [f for f in FUTURES_FREQS if f not in FREQ_SEC]
            check("主程序期货周期全部被 Trading 覆盖（缺: {}）".format(missing),
                  missing, [])
        else:
            print("⚠ 跳过反向对账：取不到 App.AppConfig.FUTURES_FREQS")
            _SKIPPED += 1
        # 主程序该周期的"秒数"列（FREQ_TABLE 第 2 列）必须与 FREQ_SEC_MAP 一致
        if MAIN_FREQ_TABLE:
            for f in FREQ_SEC:
                check("{} FREQ_TABLE 秒数列 = FREQ_SEC_MAP".format(f),
                      MAIN_FREQ_TABLE[f][1], MAIN_FREQ_SEC_MAP.get(f))

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败 / {} 跳过".format(_PASS, _FAIL, _SKIPPED))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
