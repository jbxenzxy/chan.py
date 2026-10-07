# -*- coding: utf-8 -*-
"""A/B：出场阈值「固定百分比」vs「k × ATR」—— 验证哪种量纲能跨周期迁移。

背景（见 `atr_scale.py` 的实测表）
----------------------------------
上轮把 L3 两层从 R 口径改成百分比口径，标定样本是**股票周K**（保本 5% / 跟踪 10% /
跟踪回撤 8%）。但百分比是绝对量纲，实测同一个 5% ：

    股票 周K  = 0.51 × ATR      股票 日K  = 1.27 × ATR
    IF 1 分钟 = 76.5 × ATR      IF 日线 = 3.27 × ATR

⇒ 「5%」在不同周期上根本不是一个东西。若换成 `k × ATR₀`，k 是**无量纲倍数**，
   理论上可跨周期迁移。

本脚本在**同一批标的**上跑周K与日K两轮，比较：
  · 固定 %（周K 标定值）
  · ATR k = 0.51/0.20/1.01/0.81   （= 上述固定 % 在**周K**上对应的倍数）
  · ATR k = 1.27/0.51/2.54/2.03   （= 上述固定 % 在**日K**上对应的倍数）
  · ATR k = 1.0/0.4/2.0/1.5 与 1.5/0.6/3.0/2.0（整数候选）
看「哪个方案更好」的排序是否在两个周期上一致 —— 一致则说明 k 可迁移、% 不可。

用法::

    python ab_atr.py --limit 755 --workers 10 --out ab_atr.json
"""
from __future__ import annotations

import json
import os
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

from ab_run import metrics, run_plan                     # noqa: E402
from ab_pct import per_code, paired_boot, split         # noqa: E402
from page_kline import stock_list                      # noqa: E402
from ab_run import recs_cached                          # noqa: E402
from concurrent.futures import ThreadPoolExecutor       # noqa: E402

# 上轮推荐的固定 %（股票周K 标定）
FIXED = {"PCT_ONLY": True, "PCT_BE_TRIGGER": 0.05, "PCT_BE_BUFFER": 0.02,
         "PCT_TRAIL_TRIGGER": 0.10, "PCT_TRAIL_DIST": 0.08}


def atr(kbe, kbuf, ktr, kdist):
    return {"PCT_ONLY": True, "ATR_BE_K": kbe, "ATR_BE_BUFFER_K": kbuf,
            "ATR_TRAIL_K": ktr, "ATR_TRAIL_DIST_K": kdist}


PLANS = {
    "基线（现状 R 口径）": {},
    "固定% 5/2 / 10/8（周K标定）": dict(FIXED),
    "ATR k=0.51/0.20/1.01/0.81": atr(0.51, 0.20, 1.01, 0.81),
    "ATR k=1.27/0.51/2.54/2.03": atr(1.27, 0.51, 2.54, 2.03),
    "ATR k=1.0/0.4/2.0/1.5": atr(1.0, 0.4, 2.0, 1.5),
    "ATR k=1.5/0.6/3.0/2.0": atr(1.5, 0.6, 3.0, 2.0),
}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--limit", type=int, default=755)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="ab_atr.json")
    a = ap.parse_args()

    all_codes = stock_list(a.limit)
    res = {}
    for freq in ("w", "d"):
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(lambda c: recs_cached(c, freq, a.n), all_codes))
        codes = [c for c in all_codes if recs_cached(c, freq, a.n)]
        print("\n" + "=" * 100, flush=True)
        print("周期 {}：有效标的 {}/{}，预热 {:.0f}s".format(
            freq, len(codes), len(all_codes), time.time() - t0), flush=True)
        print("=" * 100, flush=True)
        print("{:<30} {:>6} {:>10} {:>10} {:>8} {:>8} {:>7} {:>7} {:>7}".format(
            "方案", "笔数", "全样本净%", "样本外净%", "胜率%", "盈亏比",
            "止损%", "保本%", "跟踪%"))
        print("-" * 100)
        res[freq] = {}
        oos_codes = set(codes[len(codes) // 2:])       # 样本外 = 后半批标的
        base_pc = None
        for name, cfg in PLANS.items():
            rows = run_plan(codes, freq, a.n, cfg, a.workers)
            m = metrics(rows)
            res[freq][name] = {"cfg": cfg, "all": m,
                               "oos": metrics([r for r in rows
                                               if r["code"] in oos_codes])}
            print("{:<30} {:>6} {:>10.2f} {:>10.2f} {:>8.1f} {:>8.2f} "
                  "{:>7.1f} {:>7.1f} {:>7.1f}".format(
                      name, m["n"], m["avg_net"],
                      res[freq][name]["oos"].get("avg_net", 0),
                      m["win_rate"], m["pf"], m["sl_pct"], m["be_pct"],
                      m["tr_pct"]), flush=True)
            pc = per_code([r for r in rows if r["code"] in oos_codes])
            if base_pc is None:
                base_pc = pc
            else:
                d, p, k = paired_boot(base_pc, pc)
                res[freq][name]["boot_oos"] = {"delta": d, "p": p, "n": k}
                print("     样本外配对 vs 基线: Δ{:+.2f}pp  p={:.4f}{}".format(
                    d, p, "***" if p < 0.001 else "**" if p < 0.01 else
                    "*" if p < 0.05 else " n.s."), flush=True)

    with open(os.path.join(HERE, a.out), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n→ " + a.out)


if __name__ == "__main__":
    main()
