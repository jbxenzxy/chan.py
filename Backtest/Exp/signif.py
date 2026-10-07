# -*- coding: utf-8 -*-
"""显著性检验：按**标的配对**比较候选方案与基线。

为什么不直接比两组的均值：两方案笔数不同（出场变早 ⇒ 持仓期拒收信号变化 ⇒
交易序列不同），标的构成也会漂。按标的配对做差，可以把"这只票好不好做"
这个共同因子消掉，剩下的才是出场所致的差异。

n≈700 只标的 ⇒ 正态近似足够（|t|>1.96 ⇒ p<0.05）。
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

from ab_run import run_plan, recs_cached           # noqa: E402
from fetch_kline import stock_list                 # noqa: E402
from ab_final import PLANS                         # noqa: E402

TARGETS = ["基线（现状）", "①保本10%（你的方案）", "③仅改跟踪 20%/10%",
           "⑤跟踪+保本10%", "⑦跟踪+时间兜底12根", "⑧跟踪15%/10%"]


def agg(rows):
    """按标的聚合：总净收益 与 平均单笔净收益。"""
    s = defaultdict(float)
    n = defaultdict(int)
    for r in rows:
        s[r["code"]] += r["net"]
        n[r["code"]] += 1
    return s, n


def paired_t(diffs):
    m = len(diffs)
    if m < 10:
        return 0.0, 0.0, 1.0
    mu = sum(diffs) / m
    var = sum((x - mu) ** 2 for x in diffs) / (m - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(m)
    t = mu / se if se else 0.0
    p = math.erfc(abs(t) / math.sqrt(2))
    return mu, t, p


def main():
    freq, n, n_in, n_oos = "w", 800, 400, 400
    all_codes = stock_list(n_in + n_oos)
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(lambda c: recs_cached(c, freq, n), all_codes))
    codes = [c for c in all_codes if recs_cached(c, freq, n)]
    in_codes = set(all_codes[:n_in])
    oos_codes = set(all_codes[n_in:n_in + n_oos])
    print("有效标的", len(codes), flush=True)

    store = {}
    for name in TARGETS:
        rows = run_plan(codes, freq, n, PLANS[name], 12)
        store[name] = rows
        print("  done", name, len(rows), flush=True)

    base = store["基线（现状）"]
    bs, bn = agg(base)

    def report(group_name, cs):
        print("\n### 配对检验（{}，{} 只标的）".format(group_name, len(cs)))
        print("{:<26} {:>12} {:>10} {:>12} {:>12} {:>10} {:>10}".format(
            "方案 vs 基线", "Δ总净收益(pp)", "t", "p", "Δ单笔均值(pp)", "t", "p"))
        print("-" * 100)
        for name in TARGETS[1:]:
            s, nn = agg(store[name])
            d_sum, d_avg = [], []
            for c in cs:
                if c not in bs or c not in s:
                    continue
                d_sum.append(s[c] - bs[c])
                d_avg.append(s[c] / nn[c] - bs[c] / bn[c])
            m1, t1, p1 = paired_t(d_sum)
            m2, t2, p2 = paired_t(d_avg)
            star1 = "***" if p1 < 0.001 else "**" if p1 < 0.01 else "*" if p1 < 0.05 else ""
            star2 = "***" if p2 < 0.001 else "**" if p2 < 0.01 else "*" if p2 < 0.05 else ""
            print("{:<26} {:>12.2f} {:>10.2f} {:>12.4f} {:>12.2f} {:>10.2f} {:>10.4f}  {}{}".format(
                name, m1, t1, p1, m2, t2, p2, star1, star2))

    report("全样本", [c for c in codes])
    report("样本外", [c for c in codes if c in oos_codes])
    report("样本内", [c for c in codes if c in in_codes])

    with open(os.path.join(HERE, "signif_rows.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in store.items()}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
