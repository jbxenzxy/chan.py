# -*- coding: utf-8 -*-
"""跨周期 × 信号类型 交叉表 —— 把 `signal_quality.py` 的单周期输出拼成一张矩阵。

回答的问题
----------
  ⑴ 周期维度：同一套缠论买卖点，在 周K / 日K / 30分 / 15分 / 5分 上，"入场后
     能否走出反向笔"（口径一）的成功率分别是多少？
  ⑵ 类型维度：信号类型（0/1/2/3 类）在不同周期上的表现差异。
  ⑶ 交互维度：**哪个周期 + 哪类信号**同时具备(高结构成功率 + 正超额)？

口径与 `signal_quality.py` 完全一致（不重算，直接读它的 json），
所以这里的每个格子都可回查到 `sq_*.json` 的 `rows`。

用法::
    python tab_cycle.py                      # 自动找 sq_w/sq_d/sq_30m/sq_15m/sq_5m
    python tab_cycle.py --files a.json b.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
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

LABELS = {"w": "周K", "d": "日K", "30m": "30分", "15m": "15分",
          "5m": "5分", "60m": "60分",
          "w800": "周K", "d800": "日K"}          # 旧命名兼容


def _avg(v):
    return (sum(v) / len(v)) if v else 0.0


def paired_boot(d, iters=10000, seed=7):
    if not d:
        return 0.0, 1.0, 0
    obs = sum(d) / len(d)
    rnd = random.Random(seed)
    n = len(d)
    cnt = sum(1 for _ in range(iters)
              if sum(d[rnd.randrange(n)] for _ in range(n)) / n <= 0)
    p = 2 * min(cnt / iters, 1 - cnt / iters)
    return obs, min(p, 1.0), n


def stars(p):
    return ("***" if p < 0.001 else "**" if p < 0.01 else
            "*" if p < 0.05 else "n.s.")


def cell(rows, base_by_code):
    """一个格子的统计：结构成功率 + 净收益 + 对随机入场的超额（配对 bootstrap）。"""
    if not rows:
        return None
    n = len(rows)
    ns = sum(1 for r in rows if r["outcome"] == "succ")
    nf = sum(1 for r in rows if r["outcome"] == "fail")
    rets = [r["ret_H"] for r in rows if r["ret_H"] is not None]
    ex = []
    by_code = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    for c, rs in by_code.items():
        b = base_by_code.get(c)
        if not b:
            continue
        d = [r["ret_H"] - (1.0 if r["side"] == "long" else -1.0) * b["mean"]
             for r in rs if r["ret_H"] is not None]
        if d:
            ex.append(_avg(d))
    obs, p, nc = paired_boot(ex)
    return {"n": n, "succ": ns / n * 100, "fail": nf / n * 100,
            "censor": (n - ns - nf) / n * 100,
            "A_pct": _avg([r["A_pct"] for r in rows]),
            "ret": _avg(rets) * 100, "delta": obs * 100, "p": p, "codes": nc}


def load(fp):
    with open(fp, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*", default=None)
    a = ap.parse_args()

    if a.files:
        files = a.files
    else:
        # 兼容两种命名：旧版 sq_w800.json / 现行 sq_w.json —— 同一周期只取一个
        files = []
        for base in ("w", "d", "30m", "15m", "5m"):
            for cand in (base, base + "800"):
                fp = os.path.join(HERE, "sq_{}.json".format(cand))
                if os.path.exists(fp):
                    files.append(fp)
                    break

    data = []
    for fp in files:
        key = os.path.basename(fp)[3:-5]
        d = load(fp)
        data.append((LABELS.get(key, key), key, d))

    types = sorted({r["type"] for _, _, d in data for r in d["rows"]})
    order = [t for t in ("0", "1", "2", "3") if t in types] + \
            [t for t in types if t not in ("0", "1", "2", "3")]

    # ── 表 1：周期 × 结构成功率（口径一） ───────────────────────────
    print("=" * 112)
    print("表 1  周期 × 信号类型 —— 口径一「入场后走出反向笔」成功率"
          "（括号内为原笔延伸率；窗口 = H 根）")
    print("=" * 112)
    hdr = "{:<10} {:>7} {:>9}".format("周期", "H根", "全部")
    for t in order:
        hdr += " {:>13}".format(t + " 类")
    print(hdr)
    print("-" * 112)
    for lab, key, d in data:
        H = d["meta"].get("horizon", 30)
        ov = cell(d["rows"], d["baseline"])
        row = "{:<10} {:>7} {:>9}".format(
            lab, H, "{:.1f}%".format(ov["succ"]) if ov else "-")
        for t in order:
            c = cell([r for r in d["rows"] if r["type"] == t], d["baseline"])
            row += " {:>13}".format(
                "{:.1f}%({:>4.0f})".format(c["succ"], c["fail"])
                if c else "（无样本）")
        print(row)

    # ── 表 2：周期 × 类型的样本量 ────────────────────────────────
    print()
    print("=" * 112)
    print("表 2  样本量（信号条数）与平均结构距离 A%（A = |入场价 − 分型极值|）")
    print("=" * 112)
    hdr = "{:<10} {:>7} {:>10}".format("周期", "A%", "全部 n")
    for t in order:
        hdr += " {:>9} {:>7}".format(t + " 类 n", "A%")
    print(hdr)
    print("-" * 112)
    for lab, key, d in data:
        ov = cell(d["rows"], d["baseline"])
        row = "{:<10} {:>7} {:>10}".format(lab, "{:.2f}".format(ov["A_pct"]),
                                           ov["n"])
        for t in order:
            c = cell([r for r in d["rows"] if r["type"] == t], d["baseline"])
            row += " {:>9} {:>7}".format(c["n"] if c else 0,
                                         "{:.2f}".format(c["A_pct"]) if c else "-")
        print(row)

    # ── 表 3：周期 × 类型 的超额收益（口径二） ─────────────────────
    print()
    print("=" * 112)
    print("表 3  周期 × 信号类型 —— 口径二 事件研究：信号 H 根收益 − 同标的随机入场")
    print("       （Δ>0 = 信号挑得比随机好；配对 bootstrap 按标的，10000 次）")
    print("=" * 112)
    hdr = "{:<10} {:>7} {:>14}".format("周期", "H根", "全部信号 Δ")
    for t in order:
        hdr += " {:>18}".format(t + " 类 Δ")
    print(hdr)
    print("-" * 112)
    for lab, key, d in data:
        H = d["meta"].get("horizon", 30)
        ov = cell(d["rows"], d["baseline"])
        row = "{:<10} {:>7} {:>14}".format(
            lab, H, "{:+.2f}%{}".format(ov["delta"], stars(ov["p"])))
        for t in order:
            c = cell([r for r in d["rows"] if r["type"] == t], d["baseline"])
            row += " {:>18}".format(
                "{:+.2f}%{}".format(c["delta"], stars(c["p"])) if c
                else "（无样本）")
        print(row)

    # ── 表 4：周期 × 类型 × 方向 ────────────────────────────────
    print()
    print("=" * 112)
    print("表 4  周期 × 类型 × 方向（做多≈买点 / 做空≈卖点）—— 结构成功率 | 净收益 ret_H | 超额 Δ")
    print("=" * 112)
    print("{:<8} {:<6} {:>9} {:>9} {:>10} {:>11} {:>7}".format(
        "周期", "方向", "类型", "n", "走出反向笔", "ret_H", "Δ"))
    print("-" * 112)
    for lab, key, d in data:
        for side, slab in (("long", "做多"), ("short", "做空")):
            for t in order:
                sub = [r for r in d["rows"]
                       if r["side"] == side and r["type"] == t]
                c = cell(sub, d["baseline"])
                if not c or c["n"] < 20:
                    continue
                print("{:<8} {:<6} {:>9} {:>9} {:>10} {:>11} {:>7}".format(
                    lab, slab, t, c["n"], "{:.1f}%".format(c["succ"]),
                    "{:+.2f}%".format(c["ret"]),
                    "{:+.2f}%{}".format(c["delta"], stars(c["p"]))))
        print("-" * 112)

    # ── 结论扫描：同时满足「高成功率 + 正超额显著」的格子 ──────────
    print()
    print("=" * 112)
    print("筛选：结构成功率 ≥ 65% 且 超额 Δ 显著为正（p<0.05）的 周期×类型 组合")
    print("=" * 112)
    hits = []
    for lab, key, d in data:
        for t in order:
            for side in ("long", "short", None):
                sub = [r for r in d["rows"] if r["type"] == t
                       and (side is None or r["side"] == side)]
                c = cell(sub, d["baseline"])
                if not c or c["n"] < 30:
                    continue
                if c["succ"] >= 65.0 and c["delta"] > 0 and c["p"] < 0.05:
                    hits.append((lab, side or "双侧", t, c))
    if not hits:
        print("（无）—— 即：没有任何「周期×类型」组合能同时做到结构达标 + 跑赢随机入场")
    else:
        for lab, side, t, c in sorted(hits, key=lambda x: -x[3]["delta"]):
            print("{:<6} {:<6} {:<4} n={:<5} 成功率 {:.1f}%  超额 {:+.2f}% p={:.4f}{}".format(
                lab, side, t + "类", c["n"], c["succ"], c["delta"], c["p"],
                stars(c["p"])))
    print()


if __name__ == "__main__":
    main()
