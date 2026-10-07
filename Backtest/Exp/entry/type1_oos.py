# -*- coding: utf-8 -*-
"""1 类（及 0/3 类）买卖点的**样本外**验证。

为什么单独一个脚本：`signal_quality.py` 给出的是"全样本"超额，而任何"某类信号更好"
的结论都必须先过样本外这一关。本脚本只做这一件事，且两种切分都做：

  ① **按标的切**（本项目一贯口径）：股票池按固定种子 `shuffle_seed=7` 打散后，
     前半 = 样本内、后半 = 样本外。检验的是"换一批票还成不成立"。
  ② **按时间切**：以该周期全部信号的日期中位数为界，前段 = 样本内、后段 = 样本外。
     检验的是"换一段时间还成不成立"。

配对口径与 `signal_quality.py` 口径二**逐字一致**：
    超额 = ret_H − sg × baseline(同标的、同周期、同一段 K 线的全样本前瞻均值)
    sg = +1（买点/做多）或 −1（卖点/做空）—— 卖点的对照是"随机做空"。
显著性用**按标的**的配对 bootstrap（10 000 次，seed 7）。

用法::

    python type1_oos.py sq_d.json sq_30m.json sq_15m.json sq_5m.json sq_w.json
"""
from __future__ import annotations

import json
import os
import random
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = HERE if os.path.basename(HERE) == "Exp" else os.path.dirname(HERE)

ITERS, SEED = 10000, 7
TYPES = [("1", "1 类"), ("0", "0 类"), ("3", "3 类")]


def _excess_rows(rows, baseline):
    """逐条信号 → 超额（与 signal_quality 口径二完全同式）。"""
    out = []
    for r in rows:
        b = baseline.get(r["code"])
        if not b or r.get("ret_H") is None:
            continue
        sg = 1.0 if r["side"] in (1, "long", "buy", "买") else -1.0
        out.append({"code": r["code"], "date": r["date"],
                    "x": r["ret_H"] - sg * b["mean"]})
    return out


def _by_code(rs):
    acc = {}
    for r in rs:
        acc.setdefault(r["code"], []).append(r["x"])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def _boot(diff, iters=ITERS, seed=SEED):
    """配对 bootstrap：P(均值 ≤ 0) 的双尾 p。"""
    n = len(diff)
    if n < 3:
        return None, None, n
    obs = sum(diff) / n
    rnd = random.Random(seed)
    le = sum(1 for _ in range(iters)
             if sum(diff[rnd.randrange(n)] for _ in range(n)) / n <= 0)
    return obs * 100, min(2 * min(le / iters, 1 - le / iters), 1.0), n


def _star(p):
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."


def _compare(xs, base_codes):
    """{code: 超额均值} → 配对 bootstrap 结果。"""
    ks = [c for c in base_codes if c in xs]
    if len(ks) < 3:
        return None
    return _boot([xs[c] for c in ks])


def report(path):
    d = json.load(open(os.path.join(HERE, path), encoding="utf-8"))
    rows, baseline = d["rows"], d["baseline"]
    freq = d["meta"]["freq"]
    order = list(baseline)                       # = stock_list(limit) 的顺序（seed 7）
    half = len(order) // 2
    IN = set(order[:half])
    dates = sorted(r["date"] for r in rows if r.get("ret_H") is not None)
    tmid = dates[len(dates) // 2] if dates else ""

    print("═" * 104)
    print("周期 %-4s   信号 %5d 条 / 有基准标的 %d 只   时间中位 %s"
          % (freq, len(rows), len(order), tmid))
    print("═" * 104)
    print("%-8s %-28s %8s %9s %9s %11s"
          % ("类型", "切分", "标的数", "超额Δ", "配对 p", "结论"))
    print("-" * 104)

    for t, lab in [("1", "1 类（缠论原文）"), ("0", "0 类（本项目扩展）"),
                   ("3", "3 类"), ("all", "全部信号")]:
        sub = rows if t == "all" else [r for r in rows if r["type"] == t]
        if len(sub) < 5:
            print("%-8s %-28s %8s" % (lab, "样本不足", len(sub)))
            continue
        ex = _excess_rows(sub, baseline)
        xc = _by_code(ex)
        segs = [("全样本", list(baseline)),
                ("样本内（前 1/2 标的）", [c for c in order if c in IN]),
                ("样本外（后 1/2 标的）", [c for c in order if c not in IN])]
        for name, codes in segs:
            r = _compare(xc, codes)
            if not r:
                print("%-8s %-28s %8s" % (lab, name, "样本不足"))
                continue
            obs, p, n = r
            verdict = ("显著为正" if obs > 0 and p < 0.05 else
                       "显著为负" if obs < 0 and p < 0.05 else "不显著")
            print("%-8s %-28s %8d %+8.2f%% %9.4f  %-8s %s"
                  % (lab, name, n, obs, p, _star(p) if p >= 0.05 else _star(p), verdict))
        # 时间切
        for name, keep in (("样本内（时间前段）", True), ("样本外（时间后段）", False)):
            sel = [r for r in sub if (r["date"] < tmid) == keep]
            exc = _excess_rows(sel, baseline)
            r = _compare(_by_code(exc), list(baseline))
            if not r:
                print("%-8s %-28s %8s" % (lab, name, "样本不足"))
                continue
            obs, p, n = r
            print("%-8s %-28s %8d %+8.2f%% %9.4f  %-8s %s"
                  % (lab, name, n, obs, p, _star(p),
                     "显著为正" if obs > 0 and p < 0.05 else
                     "显著为负" if obs < 0 and p < 0.05 else "不显著"))
        print("-" * 104)
    print()


def main():
    files = sys.argv[1:] or ["sq_d.json", "sq_30m.json", "sq_15m.json",
                             "sq_5m.json", "sq_w.json"]
    print()
    print("说明：超额 Δ = 信号 ret_H − 同标的随机入场（买点减做多基准、卖点减做空基准）的期望。")
    print("     「样本外」两种口径都给：换一批票（按标的切）与换一段时间（按时间切）。")
    print("     单周期只有一段历史（vipdoc 日K 1393 根 / 周K 294 根 / 分钟线约 242 个交易日），")
    print("     所以时间切分对分钟周期尤其弱，别只看它的 p。")
    print()
    for f in files:
        if os.path.exists(os.path.join(HERE, f)):
            report(f)
        else:
            print("（缺 %s，跳过）\n" % f)


if __name__ == "__main__":
    main()
