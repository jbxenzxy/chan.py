# -*- coding: utf-8 -*-
"""各周期下，百分比止盈层「到底会不会被触发」—— 量化"参数是死的还是活的"。

背景
----
百分比口径（保本 5% / 跟踪 10%）是在**股票周K** 上标定的。周K 的平均结构距离
A% ≈ 14.3%、ATR% ≈ 9.9%，所以 5% 是"够得着"的。

但跨到别的周期后，同一个 5% 可能**根本够不到**：
  · 5分 A% ≈ 0.95% ⇒ 5% ≈ 5.3×A ⇒ 保本层形同虚设（结构止损早就在 −1A 打掉了）
  · 30分 A% ≈ 2.06% ⇒ 5% ≈ 2.4×A ⇒ 也基本够不到

本脚本直接算**触发率**：入场后窗口内浮盈是否曾达到该百分比。

口径
----
  `mfe_pct = mfe_A × A_pct`（两者都在 `signal_quality.py` 的 rows 里）
  → 触发率(k%) = `mfe_pct ≥ k` 的信号占比

用法::
    python layer_activation.py            # 读 sq_w800/sq_d800/sq_30m/sq_15m/sq_5m
"""
from __future__ import annotations

import json
import os
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
LABELS = {"w800": "周K", "d800": "日K", "30m": "30分", "15m": "15分", "5m": "5分"}
ORDER = ("w800", "d800", "30m", "15m", "5m")
# 当前上线的百分比口径（§6 基线锚点里的"百分比口径 5%→+2% / 10%→−8%"）
THRESH = (5.0, 8.0, 10.0)


def main():
    acc = {}
    print("=" * 104)
    print("各周期下百分比止盈层的**实际触发率**（入场后窗口内浮盈是否曾达到该百分比）")
    print("=" * 104)
    print("{:<8} {:>9} {:>9} {:>11} {:>22} {:>22}".format(
        "周期", "信号数", "均 A%", "5% 相当于", "保本层(5%)触发率",
        "跟踪层(10%)触发率"))
    print("-" * 104)
    for key in ORDER:
        fp = os.path.join(HERE, "sq_{}.json".format(key))
        if not os.path.exists(fp):
            continue
        with open(fp, "r", encoding="utf-8") as f:
            d = json.load(f)
        rows = d["rows"]
        n = len(rows)
        a_pct = sorted(r["A_pct"] for r in rows)
        a_med = a_pct[len(a_pct) // 2]
        rates = {}
        for k in THRESH:
            hit = sum(1 for r in rows
                      if r["mfe_A"] is not None
                      and r["mfe_A"] * r["A_pct"] >= k)
            rates[k] = hit / n * 100
        acc[key] = {"n": n, "a_pct": a_med, "rates": rates}
        print("{:<8} {:>9} {:>9.2f} {:>11} {:>22} {:>22}".format(
            LABELS[key], n, a_med, "{:.1f}×A".format(5.0 / a_med),
            "{:.1f}%  (≈{:.1f}×A)".format(rates[5.0], 5.0 / a_med),
            "{:.1f}%  (≈{:.1f}×A)".format(rates[10.0], 10.0 / a_med)))

    print()
    print("=" * 104)
    print("同一张表换一个角度：**保本层（5%）触发率 / 结构止损触发率** 之比")
    print("=" * 104)
    print("{:<8} {:>14} {:>16} {:>14} {:>26}".format(
        "周期", "保本层触发率", "结构止损触发率", "比值", "含义"))
    print("-" * 104)
    for key in ORDER:
        fp = os.path.join(HERE, "sq_{}.json".format(key))
        if not os.path.exists(fp):
            continue
        with open(fp, "r", encoding="utf-8") as f:
            d = json.load(f)
        rows = d["rows"]
        n = len(rows)
        be = sum(1 for r in rows
                 if r["mfe_A"] is not None and r["mfe_A"] * r["A_pct"] >= 5.0) / n
        sl = sum(1 for r in rows if r["outcome"] == "fail") / n
        ratio = be / sl if sl else 0
        verdict = ("保本层基本不生效" if ratio < 0.2 else
                   "保本层部分生效" if ratio < 1.0 else "保本层主导离场")
        print("{:<8} {:>14} {:>16} {:>14.2f} {:>26}".format(
            LABELS[key], "{:.1f}%".format(be * 100), "{:.1f}%".format(sl * 100),
            ratio, verdict))

    with open(os.path.join(HERE, "layer_activation.json"), "w",
              encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, indent=1)
    print("\n→ layer_activation.json")


if __name__ == "__main__":
    main()
