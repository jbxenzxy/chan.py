# -*- coding: utf-8 -*-
"""A/B 第二轮：跟踪层参数网格 + 保本层组合 + 样本外验证。

第一轮的关键反转：改跟踪层（A7: 3.28→6.76%）的收益 **远大于** 改保本层
（A5: 3.28→4.18%）。本轮回答两件事：
  ① 跟踪参数 (触发%, 回撤%) 的敏感性 —— 20%/10% 是不是撞上的最优？
  ② 最优组合在**样本外**（另 400 只完全不同的股票）是否仍成立。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

from ab_run import run_plan, metrics, recs_cached       # noqa: E402
from fetch_kline import stock_list                      # noqa: E402

P = lambda *s: list(s)  # noqa: E731

GRID = {
    "C0 基线（2R 启动 / 1R 跟踪）": {},
    "C1 跟踪 15% / 回撤 8%":   {"PCT_TRAIL_TRIGGER": 0.15, "PCT_TRAIL_DIST": 0.08},
    "C2 跟踪 15% / 回撤 10%":  {"PCT_TRAIL_TRIGGER": 0.15, "PCT_TRAIL_DIST": 0.10},
    "C3 跟踪 20% / 回撤 8%":   {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.08},
    "C4 跟踪 20% / 回撤 10%":  {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "C5 跟踪 20% / 回撤 12%":  {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.12},
    "C6 跟踪 25% / 回撤 10%":  {"PCT_TRAIL_TRIGGER": 0.25, "PCT_TRAIL_DIST": 0.10},
    "C7 跟踪 25% / 回撤 12%":  {"PCT_TRAIL_TRIGGER": 0.25, "PCT_TRAIL_DIST": 0.12},
    "C8 跟踪 30% / 回撤 12%":  {"PCT_TRAIL_TRIGGER": 0.30, "PCT_TRAIL_DIST": 0.12},
    "C9 跟踪 30% / 回撤 15%":  {"PCT_TRAIL_TRIGGER": 0.30, "PCT_TRAIL_DIST": 0.15},
}

COMBO = {
    "D0 基线": {},
    "D1 仅跟踪 20%/10%":      {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "D2 跟踪 + 保本15%":       {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10,
                               "STEPS": P(("pct", 0.15, "pct", 0.0))},
    "D3 跟踪 + 阶梯0.5R→−0.5R,1R→保本":
                              {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10,
                               "STEPS": P(("r", 0.5, "r", -0.5), ("r", 1.0, "r", 0.0))},
    "D4 跟踪 + 混合(10%或1R)→保本":
                              {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10,
                               "STEPS": P(("pct", 0.10, "pct", 0.0), ("r", 1.0, "r", 0.5))},
    "D5 跟踪 + 时间兜底12根":   {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10,
                               "TIME_STOP_BARS": 12},
    "D6 跟踪 + 降R阈值 1.5R":  {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10,
                               "OVR_WIN_LOSS_RATIO": 1.5},
}


def run_set(codes, freq, n, plans, workers, title):
    print("\n### " + title)
    print("{:<36} {:>5} {:>9} {:>7} {:>9} {:>9} {:>7} {:>7} {:>7} {:>8}".format(
        "方案", "笔数", "平均净收益%", "胜率%", "平均盈利%", "平均亏损%",
        "盈亏比", "均R倍数", "持仓根", "止损占比%"))
    print("-" * 124)
    out = {}
    for name, cfg in plans.items():
        rows = run_plan(codes, freq, n, cfg, workers)
        m = metrics(rows)
        out[name] = {"cfg": {k: (str(v) if isinstance(v, list) else v)
                             for k, v in cfg.items()}, "metrics": m}
        print("{:<36} {:>5} {:>9.2f} {:>7.1f} {:>9.2f} {:>9.2f} {:>7.2f} {:>7.2f} {:>7.1f} {:>8.1f}".format(
            name, m["n"], m["avg_net"], m["win_rate"], m["avg_win"], m["avg_loss"],
            m["pf"], m["avg_r"], m["avg_bars"], m["sl_pct"]), flush=True)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--oos", type=int, default=400, help="样本外额外取多少只")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="ab2_out.json")
    a = ap.parse_args()

    all_codes = stock_list(a.limit + a.oos)
    in_codes = all_codes[:a.limit]
    oos_codes = all_codes[a.limit:a.limit + a.oos]
    print("样本内 {} 只 / 样本外 {} 只 / {}".format(
        len(in_codes), len(oos_codes), a.freq), flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda c: recs_cached(c, a.freq, a.n), all_codes))
    print("缓存 {:.1f}s".format(time.time() - t0), flush=True)

    res = {}
    res["grid_in"] = run_set(in_codes, a.freq, a.n, GRID, a.workers,
                             "【样本内】跟踪参数网格（{} 只）".format(len(in_codes)))
    res["combo_in"] = run_set(in_codes, a.freq, a.n, COMBO, a.workers,
                              "【样本内】跟踪 × 保本 组合".format())
    res["grid_oos"] = run_set(oos_codes, a.freq, a.n, GRID, a.workers,
                              "【样本外】跟踪参数网格（{} 只全新标的）".format(len(oos_codes)))
    res["combo_oos"] = run_set(oos_codes, a.freq, a.n, COMBO, a.workers,
                               "【样本外】跟踪 × 保本 组合")

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
