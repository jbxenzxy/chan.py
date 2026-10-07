# -*- coding: utf-8 -*-
"""批量统计：有多少笔交易「曾大幅浮盈，却因未达 1R 保本线而最终止损离场」。

口径（均按 L1-L3 的真实判定复现，见 replay.py）：
  · MFE  = 持仓期内 best（根内有利极值，单调）相对入场价的最大有利偏移
  · 「保本未达」= exit_reason == sl（保护价从未被抬过）
  · 关注桶：MFE 百分比分桶 × MFE/R 分桶
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

from datetime import datetime                        # noqa: E402

from fetch_kline import fetch                        # noqa: E402
from replay import replay                            # noqa: E402
from Backtest.Runner import run                      # noqa: E402
from Backtest.ExitParams import STOCK_EXIT_PARAMS    # noqa: E402

PCT_BUCKETS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 1000)]
R_BUCKETS = [(0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 1000)]


def pct_bucket(x):
    for lo, hi in PCT_BUCKETS:
        if lo <= x < hi:
            return "{}-{}%".format(lo, hi)
    return "1000%+".replace("1000", "30")


def r_bucket(x):
    for lo, hi in R_BUCKETS:
        if lo <= x < hi:
            return "{}-{}R".format(lo, hi)
    return "2R+"


def one(code: str, freq: str, n: int, exit_params=None):
    bars = fetch(code, freq, n)
    recs = []
    for r in bars:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        recs.append(rr)
    res = run(code[:2], code[2:], freq, None, None, records=recs,
              exit_params=exit_params)
    didx = {b["dt"][:10]: i for i, b in enumerate(bars)}
    out = []
    for t in res.trades:
        if t.open_ or t.exit_reason is None:
            continue
        ei = didx.get(t.entry_date[:10])
        if ei is None:
            continue
        side = 1 if t.side == "long" else -1
        rp = replay(bars, ei, t.entry_price, t.r_distance, side,
                    **(exit_params or {}))
        out.append({
            "code": code, "freq": freq, "type": t.bsp_type,
            "entry_date": t.entry_date[:10], "entry": t.entry_price,
            "R": t.r_distance, "R_pct": t.r_distance / t.entry_price * 100,
            "side": t.side,
            "mfe": rp.mfe, "mfe_pct": rp.mfe_pct * 100, "mfe_r": rp.mfe_r,
            "mae_pct": rp.mae_pct * 100,
            "exit_reason": t.exit_reason,
            "r_multiple": t.r_multiple,
            "gross_pct": (t.gross_return or 0) * 100,
            "bars_held": t.bars_held,
            "exit_date": (t.exit_date or "")[:10],
        })
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq", default="w")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--codes", default="")
    ap.add_argument("--out", default="batch_out.json")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    codes = a.codes.split(",") if a.codes else fetch_universe(a.limit)
    print("标的数 =", len(codes), flush=True)

    from concurrent.futures import ThreadPoolExecutor
    rows = []
    t0 = time.time()
    done = [0]

    def work(c):
        try:
            r = one(c, a.freq, a.n)
        except Exception as e:                        # noqa: BLE001
            r = []
            print("  [skip] {}: {}: {}".format(c, type(e).__name__, str(e)[:60]),
                  flush=True)
        done[0] += 1
        if done[0] % 10 == 0:
            print("  ...{}/{}  {:.1f}s".format(done[0], len(codes), time.time() - t0),
                  flush=True)
        return r

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(work, codes):
            rows.extend(r)

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("\n完成：{} 只标的 / {} 笔交易 / {:.1f}s".format(
        len(codes), len(rows), time.time() - t0))
    report(rows)


def fetch_universe(limit: int):
    from fetch_kline import stock_list
    return stock_list(limit)


def report(rows):
    if not rows:
        print("\n无交易样本")
        return
    print("\n" + "=" * 100)
    print("【A】全部已平仓交易：按 exit_reason 分布")
    for k, v in Counter(r["exit_reason"] for r in rows).most_common():
        print("   {:<12} {:>5} 笔  {:>5.1f}%".format(k, v, v / len(rows) * 100))

    sl = [r for r in rows if r["exit_reason"] == "sl"]
    print("\n【B】止损离场（从未进保本/跟踪）的 {} 笔中，曾浮盈（MFE>0）的分布".format(len(sl)))
    print("   {:<12} {:>6} {:>8} {:>10} {:>10} {:>12}".format(
        "MFE% 桶", "笔数", "占比", "平均MFE%", "平均MFE/R", "平均最终收益%"))
    for lo, hi in PCT_BUCKETS:
        g = [r for r in sl if lo <= r["mfe_pct"] < hi]
        if not g:
            continue
        print("   {:<12} {:>6} {:>8.1f}% {:>10.1f} {:>10.2f} {:>12.1f}".format(
            "{}-{}%".format(lo, hi), len(g), len(g) / len(sl) * 100,
            sum(x["mfe_pct"] for x in g) / len(g),
            sum(x["mfe_r"] for x in g) / len(g),
            sum(x["gross_pct"] for x in g) / len(g)))
    g0 = [r for r in sl if r["mfe_pct"] <= 0]
    print("   {:<12} {:>6} {:>8.1f}%  （入场后从未浮盈）".format(
        "<=0%", len(g0), len(g0) / len(sl) * 100))

    print("\n【C】止损离场中「MFE≥5%」的笔（浮盈可观却回吐为亏损）")
    g = [r for r in sl if r["mfe_pct"] >= 5]
    print("   笔数 = {}  占全部交易 {:.1f}%  占止损笔 {:.1f}%".format(
        len(g), len(g) / len(rows) * 100, len(g) / len(sl) * 100))
    print("   这些笔：平均 MFE={:.1f}%  平均最终收益={:.1f}%  平均回吐={:.1f}个百分点".format(
        sum(x["mfe_pct"] for x in g) / max(len(g), 1),
        sum(x["gross_pct"] for x in g) / max(len(g), 1),
        sum(x["mfe_pct"] - x["gross_pct"] for x in g) / max(len(g), 1)))

    print("\n【D】止损离场按 MFE/R 分桶（1.0R = 现行保本门槛）")
    print("   {:<12} {:>6} {:>8} {:>12} {:>12}".format(
        "MFE/R 桶", "笔数", "占比", "平均MFE%", "平均最终收益%"))
    for lo, hi in R_BUCKETS:
        g = [r for r in sl if lo <= r["mfe_r"] < hi]
        if not g:
            continue
        print("   {:<12} {:>6} {:>8.1f}% {:>12.1f} {:>12.1f}".format(
            "{}-{}R".format(lo, hi), len(g), len(g) / len(sl) * 100,
            sum(x["mfe_pct"] for x in g) / len(g),
            sum(x["gross_pct"] for x in g) / len(g)))

    near = [r for r in sl if 0.8 <= r["mfe_r"] < 1.0]
    print("\n【E】「差一点点」：0.8R ≤ MFE < 1.0R 却止损离场 = {} 笔".format(len(near)))
    print("   平均 MFE={:.1f}%  平均最终收益={:.1f}%".format(
        sum(x["mfe_pct"] for x in near) / max(len(near), 1),
        sum(x["gross_pct"] for x in near) / max(len(near), 1)))
    for r in sorted(near, key=lambda x: -x["mfe_r"])[:10]:
        print("     {} {} {} entry={:.2f} R={:.2f}({:.1f}%) MFE={:.2f}R/{:.1f}% 终={:.1f}%".format(
            r["code"], r["entry_date"], r["type"], r["entry"], r["R"], r["R_pct"],
            r["mfe_r"], r["mfe_pct"], r["gross_pct"]))

    print("\n【F】R 占入场价的百分比分布（1R 门槛对应的涨幅要求）")
    print("   {:<12} {:>6} {:>8}".format("R% 桶", "笔数", "占比"))
    for lo, hi in [(0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 1000)]:
        g = [r for r in rows if lo <= r["R_pct"] < hi]
        if not g:
            continue
        print("   {:<12} {:>6} {:>8.1f}%".format("{}-{}%".format(lo, hi), len(g),
                                                len(g) / len(rows) * 100))
    print("   平均 R% = {:.1f}%".format(sum(r["R_pct"] for r in rows) / max(len(rows), 1)))


if __name__ == "__main__":
    main()
