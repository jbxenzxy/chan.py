# -*- coding: utf-8 -*-
"""候选方案对「常山北明 2025-04-30 那一笔」的具体影响。"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
def _find_repo(_p):
    """向上找到仓库根（含 Chan.py + DataAPI 的那一级）—— 从任意深度都成立。"""
    for _ in range(7):
        if (os.path.isfile(os.path.join(_p, "Chan.py"))
                and os.path.isdir(os.path.join(_p, "DataAPI"))):
            return _p
        _p = os.path.dirname(_p)
    return ""


EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)
# 优先级：显式 CHAN_REPO > 向上找仓库根 > 沙盒布局（<work>/wt_latest）
REPO = (os.environ.get("CHAN_REPO") or _find_repo(HERE)
        or os.path.join(os.path.dirname(os.path.dirname(EXP)), "wt_latest"))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(EXP, "common"))     # 共享层（tdx_source / exp_policy …）
sys.path.insert(0, HERE)
os.chdir(REPO)

import exp_policy                                    # noqa: E402
from ab_run import recs_cached                       # noqa: E402
from Backtest.Runner import run                      # noqa: E402

P = lambda *s: list(s)  # noqa: E402,E731

PLANS = {
    "现状（1R→+0.5R）": {},
    "0.5R→保本, 1R→+0.5R":      {"STEPS": P(("r", 0.5, "r", 0.0), ("r", 1.0, "r", 0.5))},
    "10%或1R→保本/+0.5R":       {"STEPS": P(("pct", 0.10, "pct", 0.0), ("r", 1.0, "r", 0.5))},
    "10%→保本+2%, 1R→+0.5R":    {"STEPS": P(("pct", 0.10, "pct", 0.02), ("r", 1.0, "r", 0.5))},
    "8%或1R→保本/+0.5R":        {"STEPS": P(("pct", 0.08, "pct", 0.0), ("r", 1.0, "r", 0.5))},
    "15%或1R→保本/+0.5R":       {"STEPS": P(("pct", 0.15, "pct", 0.0), ("r", 1.0, "r", 0.5))},
    "0.75R→+0.25R,1.5R→+0.75R": {"STEPS": P(("r", 0.75, "r", 0.25), ("r", 1.5, "r", 0.75))},
    "10%→保本,20%→+10%":        {"STEPS": P(("pct", 0.10, "pct", 0.0),
                                            ("pct", 0.20, "pct", 0.10),
                                            ("r", 1.0, "r", 0.5))},
}


def main():
    code, freq, n = "sz000158", "w", 800
    recs = recs_cached(code, freq, n)
    print("常山北明 {} {} —— 目标笔：2025-04-30 入场（type 0 做多）\n".format(code, freq))
    print("{:<28} {:>10} {:>10} {:>10} {:>8} {:>8} {:>10}".format(
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
            print("{:<28} {:>10}  （该笔在本方案下不存在）".format(name, "-"))
            continue
        t = hits[0]
        print("{:<28} {:>10.3f} {:>10} {:>10.3f} {:>8.2f} {:>8.2f} {:>10}".format(
            name, t.entry_price, (t.exit_date or "-")[:10],
            t.exit_price or 0, (t.net_return or 0) * 100,
            t.r_multiple or 0, t.exit_reason or ("持仓中" if t.open_ else "-")))
    print("\n（基线实际：入场 22.533，R=5.940，MFE 25.8% = 0.98R，"
          "2026-05-22 以 15.423 止损离场，净亏 31.6%）")


if __name__ == "__main__":
    main()
