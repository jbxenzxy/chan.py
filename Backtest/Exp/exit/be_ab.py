# -*- coding: utf-8 -*-
"""保本触发门槛：1.0R → 0.75R / 0.5R，端到端 A/B（含样本内/外 + 配对显著性）。

为什么单独做这一轮
------------------
`exit_fate.py` 把「结构止损」拆成两族后发现：**①「走出反向笔却仍止损」那一族，
离场前的最大浮盈只有 0.52~0.59R**（5 个周期一致）—— 全部**低于 1.0R 保本触发线**
⇒ 保本层从未启动 ⇒ 最终回到初始保护价。常山北明就是这一族：MFE 0.9781R，差 0.13 点。

本脚本回答的正是"把门槛降下来到底能不能救" —— 注意这是**端到端重跑**
（含"出场变早 → 持仓期拒收信号变化"的二阶效应），不是离线反事实。

口径提醒
--------
`ab_run.one()` 返回的 `net` **已经是百分数**（`t.net_return * 100`）⇒ 本脚本不再 ×100。
第一版忘了这点，打出 "样本内 +127%" 这种荒谬值 —— 保留在日志里作为反面教材。

用法::

    python be_ab.py --freq w --n 800 --limit 800 --procs 8
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

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

from page_kline import stock_list                      # noqa: E402
from ab_run import close_pool, run_plan                 # noqa: E402

PLANS = [
    ("基线：保本触发 1.0R（现状）", {}),
    ("保本触发 0.75R", {"OVR_BE_TRIGGER_R": 0.75}),
    ("保本触发 0.5R", {"OVR_BE_TRIGGER_R": 0.5}),
    ("保本触发 0.5R + 落点 0.25R", {"OVR_BE_TRIGGER_R": 0.5,
                                    "OVR_BE_BUFFER_R": 0.25}),
    ("保本触发 0.5R + 跟踪距离 0.5R", {"OVR_BE_TRIGGER_R": 0.5,
                                        "OVR_TRAIL_DIST_R": 0.5}),
]


def metrics(rows):
    n = len(rows)
    if not n:
        return {}
    nets = [r["net"] for r in rows]                      # 已是百分数
    wins = [x for x in nets if x > 0]
    loss = [x for x in nets if x <= 0]
    return {
        "n": n, "avg_net": sum(nets) / n,
        "win_rate": len(wins) / n * 100,
        "pf": (sum(wins) / abs(sum(loss))) if loss and sum(loss) else float("inf"),
        "avg_bars": sum((r["bars"] or 0) for r in rows) / n,
        "sl_pct": sum(1 for r in rows if r["reason"] == "sl") / n * 100,
        "be_pct": sum(1 for r in rows if r["reason"] == "breakeven") / n * 100,
        "tr_pct": sum(1 for r in rows if r["reason"] == "trailing") / n * 100,
        "worst": min(nets),
    }


def per_code(rows):
    """标的 → 该标的全部笔的**等权**平均净收益（配对检验的单位）。"""
    acc = {}
    for r in rows:
        acc.setdefault(r["code"], []).append(r["net"])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def boot_paired(a, b, iters=10000, seed=7):
    """按标的配对 bootstrap（双侧）：Δ = a − b 的均值是否≠0。"""
    keys = sorted(set(a) & set(b))
    if not keys:
        return 0.0, 1.0, 0
    d = [a[k] - b[k] for k in keys]
    rnd = random.Random(seed)
    n = len(d)
    obs = sum(d) / n
    cnt = sum(1 for _ in range(iters)
              if sum(d[rnd.randrange(n)] for _ in range(n)) / n <= 0)
    return obs, min(2 * min(cnt / iters, 1 - cnt / iters), 1.0), n


def star(p):
    return ("***" if p < 0.001 else "**" if p < 0.01 else
            "*" if p < 0.05 else "n.s.")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--n-in", type=int, default=400)
    ap.add_argument("--procs", type=int, default=8)
    ap.add_argument("--out", default="be_ab.json")
    a = ap.parse_args()

    codes = list(stock_list(a.limit))
    in_set = set(codes[:a.n_in])
    print("周期 {} / {} 根 / 标的 {} 只（样本内 {} / 样本外 {}）进程 {}\n".format(
        a.freq, a.n, len(codes), a.n_in, len(codes) - a.n_in, a.procs), flush=True)

    print("{:<32} {:>7} {:>11} {:>11} {:>11} {:>11}".format(
        "方案", "笔数", "全样本", "样本内", "样本外", "均持仓"))
    print("-" * 88)
    res, base_pc, base_all = {}, None, None
    for name, cfg in PLANS:
        t0 = time.time()
        rows = run_plan(codes, a.freq, a.n, cfg, procs=a.procs)
        pc = per_code(rows)
        ins = [v for k, v in pc.items() if k in in_set]
        oos = [v for k, v in pc.items() if k not in in_set]
        m = metrics(rows)
        m["in"] = sum(ins) / len(ins) if ins else 0.0
        m["oos"] = sum(oos) / len(oos) if oos else 0.0
        m["in_codes"], m["oos_codes"] = len(ins), len(oos)
        res[name] = {"cfg": cfg, "metrics": m}
        print("{:<32} {:>7} {:>+10.2f}% {:>+10.2f}% {:>+10.2f}% {:>10.1f}  [{:.0f}s]".format(
            name, m["n"], sum(pc.values()) / len(pc), m["in"], m["oos"],
            m["avg_bars"], time.time() - t0), flush=True)
        if base_pc is None:
            base_pc, base_all = pc, rows
            print("  └ 止损 {:.1f}% / 保本 {:.1f}% / 跟踪 {:.1f}%｜胜率 {:.1f}%｜盈亏比 {:.2f}".format(
                m["sl_pct"], m["be_pct"], m["tr_pct"], m["win_rate"], m["pf"]), flush=True)
        else:
            for lab, sub, bp in (
                    ("样本内", {k: v for k, v in pc.items() if k in in_set}, base_pc),
                    ("样本外", {k: v for k, v in pc.items() if k not in in_set}, base_pc)):
                bb = {k: v for k, v in bp.items() if (k in in_set) == (lab == "样本内")}
                o, p, nc = boot_paired(sub, bb)
                res[name].setdefault("boot", {})[lab] = {"delta_pp": o, "p": p, "n": nc}
                print("  └ vs 基线·{} Δ={:+.2f}pp p={:.4f}{} (n={})".format(
                    lab, o, p, star(p), nc), flush=True)
            print("  └ 止损 {:.1f}% / 保本 {:.1f}% / 跟踪 {:.1f}%｜胜率 {:.1f}%｜盈亏比 {:.2f}".format(
                m["sl_pct"], m["be_pct"], m["tr_pct"], m["win_rate"], m["pf"]), flush=True)

    close_pool()
    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump({"meta": vars(a), "out": res}, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
