# -*- coding: utf-8 -*-
"""A/B 对照：用真实回测引擎（Runner + 状态机）跑各出场方案，比较组合级指标。

每个方案都是完整重跑（含"出场变早 → 持仓期拒收信号变化"的二阶效应），
不是离线重放。方案开关见 exp_policy.ExpExitPolicy 的类属性。
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

import exp_policy                                  # noqa: E402
from fetch_kline import fetch, stock_list          # noqa: E402
from Backtest.Runner import run                    # noqa: E402
from Backtest.ExitParams import (STOCK_EXIT_PARAMS, round_trip_cost,  # noqa: E402
                                 shares_for)

# ── 方案表 ────────────────────────────────────────────────────────────
PLANS = {
    "A0 基线（现状 1R→保本+0.5R）": {},
    "A1 降门槛 0.5R":               {"OVR_BE_TRIGGER_R": 0.5},
    "A2 +百分比保本 10%（用户方案）": {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0},
    "A3 +百分比保本 10% 落点+2%":    {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.02},
    "A4 +百分比保本 8%":             {"PCT_BE_TRIGGER": 0.08, "PCT_BE_BUFFER": 0.0},
    "A5 +百分比保本 15%":            {"PCT_BE_TRIGGER": 0.15, "PCT_BE_BUFFER": 0.0},
    "A6 双条件 min(0.5R,10%)":      {"OVR_BE_TRIGGER_R": 0.5, "PCT_BE_TRIGGER": 0.10,
                                     "PCT_BE_BUFFER": 0.0},
    "A7 百分比跟踪 20%/回撤10%":     {"PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "A8 A2 + A7":                   {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0,
                                     "PCT_TRAIL_TRIGGER": 0.20, "PCT_TRAIL_DIST": 0.10},
    "A9 A2 + 时间兜底 12 根":        {"PCT_BE_TRIGGER": 0.10, "PCT_BE_BUFFER": 0.0,
                                     "TIME_STOP_BARS": 12},
}


def recs_of(code, freq, n):
    rows = fetch(code, freq, n)
    out = []
    for r in rows:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        out.append(rr)
    return out


_CACHE = {}


def recs_cached(code, freq, n):
    key = (code, freq, n)
    if key not in _CACHE:
        _CACHE[key] = recs_of(code, freq, n)
    return _CACHE[key]


def one(code, freq, n):
    """返回该标的在该方案下的逐笔净收益序列。"""
    try:
        res = run(code[:2], code[2:], freq, None, None,
                  records=recs_cached(code, freq, n))
    except Exception:                                # noqa: BLE001
        return []
    out = []
    for t in res.trades:
        if t.open_ or t.net_return is None:
            continue
        out.append({
            "code": code, "freq": freq, "type": t.bsp_type, "side": t.side,
            "entry_date": t.entry_date[:10],
            "exit_date": (t.exit_date or "")[:10],
            "entry": t.entry_price, "R": t.r_distance,
            "R_pct": (t.r_distance / t.entry_price * 100) if t.entry_price else 0,
            "net": t.net_return * 100, "gross": (t.gross_return or 0) * 100,
            "r_mult": t.r_multiple, "reason": t.exit_reason,
            "bars": t.bars_held,
        })
    return out


def run_plan(codes, freq, n, cfg, workers=10):
    exp_policy.install()
    exp_policy.reset()
    exp_policy.configure(**cfg)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res = list(ex.map(lambda c: one(c, freq, n), codes))
    finally:
        exp_policy.uninstall()
        exp_policy.reset()
    rows = []
    for r in res:
        rows.extend(r)
    return rows


def metrics(rows):
    if not rows:
        return {}
    nets = [r["net"] for r in rows]
    wins = [x for x in nets if x > 0]
    loss = [x for x in nets if x <= 0]
    rms = [r["r_mult"] for r in rows if r["r_mult"] is not None]
    return {
        "n": len(rows),
        "avg_net": sum(nets) / len(nets),
        "win_rate": len(wins) / len(rows) * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(loss) / len(loss) if loss else 0.0,
        "pf": (sum(wins) / abs(sum(loss))) if loss and sum(loss) else float("inf"),
        "avg_r": sum(rms) / len(rms) if rms else 0.0,
        "avg_bars": sum(r["bars"] or 0 for r in rows) / len(rows),
        "sl_pct": sum(1 for r in rows if r["reason"] == "sl") / len(rows) * 100,
        "be_pct": sum(1 for r in rows if r["reason"] == "breakeven") / len(rows) * 100,
        "tr_pct": sum(1 for r in rows if r["reason"] == "trailing") / len(rows) * 100,
        "worst": min(nets),
        "sum_net": sum(nets),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="ab_out.json")
    a = ap.parse_args()

    codes = stock_list(a.limit)
    print("标的 {} 只 / {} / {} 根".format(len(codes), a.freq, a.n), flush=True)
    # 预热缓存
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda c: recs_cached(c, a.freq, a.n), codes))
    print("K 线缓存完成 {:.1f}s".format(time.time() - t0), flush=True)

    all_out = {}
    print("\n{:<32} {:>5} {:>9} {:>8} {:>9} {:>9} {:>7} {:>7} {:>7} {:>8}".format(
        "方案", "笔数", "平均净收益%", "胜率%", "平均盈利%", "平均亏损%",
        "盈亏比", "均R倍数", "持仓根", "止损占比%"))
    print("-" * 118)
    for name, cfg in PLANS.items():
        t1 = time.time()
        rows = run_plan(codes, a.freq, a.n, cfg, a.workers)
        m = metrics(rows)
        all_out[name] = {"cfg": cfg, "metrics": m, "rows": rows}
        print("{:<32} {:>5} {:>9.2f} {:>8.1f} {:>9.2f} {:>9.2f} {:>7.2f} {:>7.2f} {:>7.1f} {:>8.1f}".format(
            name, m["n"], m["avg_net"], m["win_rate"], m["avg_win"], m["avg_loss"],
            m["pf"], m["avg_r"], m["avg_bars"], m["sl_pct"]),
            flush=True)
        print("     [{:.1f}s]  保本离场 {:.1f}% / 跟踪离场 {:.1f}% / 最差单笔 {:.1f}%".format(
            time.time() - t1, m["be_pct"], m["tr_pct"], m["worst"]), flush=True)

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(all_out, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
