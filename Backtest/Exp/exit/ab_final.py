# -*- coding: utf-8 -*-
"""最终验证：干净样本（剔除过度复权负价标的）× 样本内/样本外 × 时间外切分。

三层稳健性：
  ① 样本内 400 只 vs 样本外 400 只（完全不同的标的）
  ② 早期（入场 ≤2018）vs 近期（>2018）—— 结论是否跨时间成立
  ③ 参数网格已在 ab_run2 验证：不是撞上单个最优点
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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

from ab_run import run_plan, metrics, recs_cached        # noqa: E402
from page_kline import stock_list                       # noqa: E402

P = lambda *s: list(s)  # noqa: E731

TRAIL = {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10}

PLANS = {
    "基线（现状）": {},
    "①保本10%（你的方案）": {"STEPS": P(("pct", 0.10, "pct", 0.0))},
    "②保本15%": {"STEPS": P(("pct", 0.15, "pct", 0.0))},
    "③仅改跟踪 20%/10%": dict(TRAIL),
    "④跟踪+保本15%": dict(TRAIL, **{"STEPS": P(("pct", 0.15, "pct", 0.0))}),
    "⑤跟踪+保本10%": dict(TRAIL, **{"STEPS": P(("pct", 0.10, "pct", 0.0))}),
    "⑥跟踪+阶梯0.5R→−0.5R,1R→保本":
        dict(TRAIL, **{"STEPS": P(("r", 0.5, "r", -0.5), ("r", 1.0, "r", 0.0))}),
    "⑦跟踪+时间兜底12根": dict(TRAIL, **{"TIME_STOP_BARS": 12}),
    "⑧跟踪15%/10%": {"PCT_TRAIL_TRIGGER": 0.15, "PCT_TRAIL_DIST": 0.10},
}


def split_metrics(rows, in_codes):
    ins = set(in_codes)
    a = [r for r in rows if r["code"] in ins]
    b = [r for r in rows if r["code"] not in ins]
    e = [r for r in rows if r["entry_date"] <= "2018-12-31"]
    l = [r for r in rows if r["entry_date"] > "2018-12-31"]
    return metrics(a), metrics(b), metrics(e), metrics(l)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--in", dest="n_in", type=int, default=400)
    ap.add_argument("--oos", type=int, default=400)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="ab_final.json")
    a = ap.parse_args()

    all_codes = stock_list(a.n_in + a.oos)
    in_codes, oos_codes = all_codes[:a.n_in], all_codes[a.n_in:a.n_in + a.oos]

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda c: recs_cached(c, a.freq, a.n), all_codes))
    ok = [c for c in all_codes if recs_cached(c, a.freq, a.n)]
    print("有效标的 {} / {}（{} 只被质量门或接口剔除）  {:.0f}s".format(
        len(ok), len(all_codes), len(all_codes) - len(ok), time.time() - t0), flush=True)
    codes = ok

    res = {}
    print("\n{:<30} {:>18} {:>18} {:>18} {:>18}".format(
        "方案", "样本内 平均净收益%", "样本外 平均净收益%", "早期(≤2018)", "近期(>2018)"))
    print("-" * 108)
    for name, cfg in PLANS.items():
        rows = run_plan(codes, a.freq, a.n, cfg, a.workers)
        mi, mo, me, ml = split_metrics(rows, in_codes)
        res[name] = {"cfg": {k: (str(v) if isinstance(v, list) else v)
                             for k, v in cfg.items()},
                     "in": mi, "oos": mo, "early": me, "late": ml,
                     "all": metrics(rows)}
        print("{:<30} {:>10.2f} (n={:>4}) {:>10.2f} (n={:>4}) {:>10.2f} (n={:>4}) {:>10.2f} (n={:>4})".format(
            name, mi.get("avg_net", 0), mi.get("n", 0),
            mo.get("avg_net", 0), mo.get("n", 0),
            me.get("avg_net", 0), me.get("n", 0),
            ml.get("avg_net", 0), ml.get("n", 0)), flush=True)

    print("\n{:<30} {:>8} {:>8} {:>8} {:>8} {:>8}".format(
        "方案（样本外）", "胜率%", "盈亏比", "平均盈利%", "平均亏损%", "止损占比%"))
    print("-" * 80)
    for name, d in res.items():
        m = d["oos"]
        print("{:<30} {:>8.1f} {:>8.2f} {:>8.2f} {:>8.2f} {:>8.1f}".format(
            name, m.get("win_rate", 0), m.get("pf", 0), m.get("avg_win", 0),
            m.get("avg_loss", 0), m.get("sl_pct", 0)))

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
