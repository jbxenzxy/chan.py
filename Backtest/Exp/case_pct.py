# -*- coding: utf-8 -*-
"""新百分比口径对「常山北明 2025-04-30 那一笔」的具体影响。"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

import exp_policy                                    # noqa: E402
from ab_run import recs_cached                       # noqa: E402
from Backtest.Runner import run                      # noqa: E402

USER = {"PCT_ONLY": True, "PCT_BE_TRIGGER": 0.05, "PCT_BE_BUFFER": 0.02,
        "PCT_TRAIL_TRIGGER": 0.10, "PCT_TRAIL_DIST": 0.05}

PLANS = {
    "基线（1R→+0.5R, 2R→−1R）": {},
    "★用户方案 5%→+2%,10%→−5%": dict(USER),
    "★用户方案（≥ 口径）": dict(USER, PCT_GE=True),
    "只改保本 5%→+2%": {"PCT_BE_TRIGGER": 0.05, "PCT_BE_BUFFER": 0.02},
    "只改跟踪 10%→−5%": {"PCT_TRAIL_TRIGGER": 0.10, "PCT_TRAIL_DIST": 0.05},
    "用户方案+跟踪回撤8%": dict(USER, PCT_TRAIL_DIST=0.08),
    "用户方案+保本触发10%": dict(USER, PCT_BE_TRIGGER=0.10),
    "保本5%→落点0%": dict(USER, PCT_BE_BUFFER=0.0),
    "参照 上轮最优 20%/10%": {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
}


def main():
    code, freq, n = "sz000158", "w", 800
    recs = recs_cached(code, freq, n)
    print("常山北明 {} {} —— 目标笔：2025-04-30 入场（type 0 做多）\n".format(code, freq))
    print("{:<28} {:>9} {:>11} {:>9} {:>9} {:>8} {:>9}".format(
        "方案", "入场", "离场日", "离场价", "净收益%", "R倍数", "离场原因"))
    print("-" * 92)
    for name, cfg in PLANS.items():
        exp_policy.install()
        exp_policy.reset()
        exp_policy.configure(**cfg)
        try:
            res = run(code[:2], code[2:], freq, None, None, records=recs)
        finally:
            exp_policy.uninstall()
            exp_policy.reset()
        hits = [t for t in res.trades if t.entry_date[:10] == "2025-04-30"]
        if not hits:
            print("{:<28} {:>9}  （该笔在本方案下不存在）".format(name, "-"))
            continue
        t = hits[0]
        print("{:<28} {:>9.3f} {:>11} {:>9.3f} {:>9.2f} {:>8.2f} {:>9}".format(
            name, t.entry_price, (t.exit_date or "-")[:10], t.exit_price or 0,
            (t.net_return or 0) * 100, t.r_multiple or 0,
            t.exit_reason or ("持仓中" if t.open_ else "-")))
    print("\n（基线实测：入场 22.533，R=5.940（26.4%），MFE 28.34 = +25.8%；"
          "保本线 28.473 未达 → 2026-05-22 以 15.423 止损离场，净亏 31.6%）")


if __name__ == "__main__":
    main()
