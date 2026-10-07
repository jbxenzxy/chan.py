# -*- coding: utf-8 -*-
"""A/B：把 L3 两层（保本 / 跟踪）从「R 口径」换成「百分比口径」。

用户需求（上涨为例，下跌镜像）：
   ⑴ 原「浮盈 > 1R → 保本，保护价 = 入场 + 0.5R」
      改「浮盈 ≥ 5% → 保本，保护价 = 入场 × 1.02」
   ⑵ 原「浮盈 > 2R → 跟踪，保护价 = 最高价 − 1R」
      改「浮盈 ≥ 10% → 跟踪，保护价 = 最高价 × 0.95」
   触发值一律取 K 线**有利侧极值**（做多 high）相对入场价的百分比 —— 与改前同口径。

输出：干净样本（剔除过度复权负价标的）× 样本内/样本外 × 早期/近期 四切分，
     并按标的配对做 bootstrap 显著性检验（对照组 = 基线）。
"""
from __future__ import annotations

import json
import os
import random
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

from ab_run import run_plan, metrics, recs_cached        # noqa: E402
from fetch_kline import stock_list                       # noqa: E402

# ── 用户方案（严格 > 口径，与真实实现一致）──────────────────────────
USER = {"PCT_ONLY": True,
        "PCT_BE_TRIGGER": 0.05, "PCT_BE_BUFFER": 0.02,
        "PCT_TRAIL_TRIGGER": 0.10, "PCT_TRAIL_DIST": 0.05}

PLANS = {
    "基线（现状 R 口径）": {},
    "★用户方案 5%→+2% / 10%→−5%": dict(USER),
    "★用户方案（≥ 边界口径）": dict(USER, PCT_GE=True),
    "变体 只改保本 5%→+2%": {"PCT_BE_TRIGGER": 0.05, "PCT_BE_BUFFER": 0.02},
    "变体 只改跟踪 10%→−5%": {"PCT_TRAIL_TRIGGER": 0.10, "PCT_TRAIL_DIST": 0.05},
    "变体 用户方案+跟踪回撤8%": dict(USER, PCT_TRAIL_DIST=0.08),
    "变体 用户方案+保本触发10%": dict(USER, PCT_BE_TRIGGER=0.10),
    "参照 上轮最优 跟踪20%/10%+R保本": {"PCT_TRAIL_TRIGGER": 0.20,
                                        "PCT_TRAIL_DIST": 0.10},
}


def split(rows, in_codes, cut="2018-12-31"):
    ins = set(in_codes)
    return (metrics([r for r in rows if r["code"] in ins]),
            metrics([r for r in rows if r["code"] not in ins]),
            metrics([r for r in rows if r["entry_date"] <= cut]),
            metrics([r for r in rows if r["entry_date"] > cut]))


def per_code(rows):
    acc = {}
    for r in rows:
        acc.setdefault(r["code"], []).append(r["net"])
    return {c: sum(v) / len(v) for c, v in acc.items()}


def paired_boot(a, b, iters=10000, seed=7):
    """按标的配对 bootstrap：H0 = 两方案每标的平均净收益无差异。a=对照 b=处理。"""
    keys = sorted(set(a) & set(b))
    if not keys:
        return 0.0, 0.0, 0.0
    d = [b[k] - a[k] for k in keys]
    obs = sum(d) / len(d)
    rnd = random.Random(seed)
    n = len(d)
    cnt = 0
    for _ in range(iters):
        s = sum(d[rnd.randrange(n)] for _ in range(n)) / n
        if s <= 0:
            cnt += 1
    # H1 双侧：p = 2 × min(P(≤0), P(≥0))
    p_lo = cnt / iters
    p = 2 * min(p_lo, 1 - p_lo)
    return obs, min(p, 1.0), len(keys)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--n-in", type=int, default=400)
    ap.add_argument("--oos", type=int, default=400)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", default="ab_pct.json")
    a = ap.parse_args()

    all_codes = stock_list(a.n_in + a.oos)
    in_codes = all_codes[:a.n_in]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(lambda c: recs_cached(c, a.freq, a.n), all_codes))
    codes = [c for c in all_codes if recs_cached(c, a.freq, a.n)]
    print("有效标的 {} / {}（{} 只被质量门/接口剔除） {:.0f}s".format(
        len(codes), len(all_codes), len(all_codes) - len(codes), time.time() - t0),
        flush=True)

    res, rows_by, base_pc = {}, {}, None
    print("\n{:<28} {:>16} {:>16} {:>14} {:>14}".format(
        "方案", "样本内 净收益%", "样本外 净收益%", "早期(≤2018)", "近期(>2018)"))
    print("-" * 94)
    for name, cfg in PLANS.items():
        rows = run_plan(codes, a.freq, a.n, cfg, a.workers)
        mi, mo, me, ml = split(rows, in_codes)
        rows_by[name] = rows
        res[name] = {"cfg": {k: (str(v) if isinstance(v, list) else v)
                             for k, v in cfg.items()},
                     "in": mi, "oos": mo, "early": me, "late": ml, "all": metrics(rows)}
        print("{:<28} {:>8.2f}(n={:>4}) {:>8.2f}(n={:>4}) {:>6.2f}(n={:>4}) {:>6.2f}(n={:>4})".format(
            name, mi.get("avg_net", 0), mi.get("n", 0), mo.get("avg_net", 0), mo.get("n", 0),
            me.get("avg_net", 0), me.get("n", 0), ml.get("avg_net", 0), ml.get("n", 0)),
            flush=True)
        if base_pc is None:
            base_pc_i = per_code([r for r in rows if r["code"] in set(in_codes)])
            base_pc_o = per_code([r for r in rows if r["code"] not in set(in_codes)])
            base_pc = (base_pc_i, base_pc_o)

    print("\n{:<28} {:>7} {:>7} {:>8} {:>8} {:>8} {:>8} {:>8}".format(
        "方案（样本外）", "胜率%", "盈亏比", "平均盈利", "平均亏损",
        "止损占比", "保本占比", "跟踪占比"))
    print("-" * 88)
    for name, d in res.items():
        m = d["oos"]
        print("{:<28} {:>7.1f} {:>7.2f} {:>8.2f} {:>8.2f} {:>8.1f} {:>8.1f} {:>8.1f}".format(
            name, m.get("win_rate", 0), m.get("pf", 0), m.get("avg_win", 0),
            m.get("avg_loss", 0), m.get("sl_pct", 0), m.get("be_pct", 0),
            m.get("tr_pct", 0)))

    print("\n配对 bootstrap（按标的 10000 次；Δ = 方案 − 基线，单位 pp）")
    print("{:<28} {:>18} {:>18}".format("方案", "样本内 Δ / p", "样本外 Δ / p"))
    print("-" * 68)
    bc_i, bc_o = base_pc
    for name, rows in rows_by.items():
        if name.startswith("基线"):
            continue
        pci = per_code([r for r in rows if r["code"] in set(in_codes)])
        pco = per_code([r for r in rows if r["code"] not in set(in_codes)])
        oi, pi, ni = paired_boot(bc_i, pci)
        oo, po, no = paired_boot(bc_o, pco)
        res[name]["boot"] = {"in_delta": oi, "in_p": pi, "oos_delta": oo,
                             "oos_p": po, "n_in": ni, "n_oos": no}
        star = lambda p: "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."
        print("{:<28} {:>+9.2f} {:<8} {:>+9.2f} {:<8}".format(
            name, oi, "{:.4f}{}".format(pi, star(pi)),
            oo, "{:.4f}{}".format(po, star(po))))

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
