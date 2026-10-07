# -*- coding: utf-8 -*-
"""诊断 R 的构成：A（结构距离）vs B = atr_sl_multiple × ATR，谁在主导。

这决定了"1R 保本门槛"到底等价于几个 ATR —— 若 R 被 A（结构距离）撑得远大于
ATR，那么"浮盈 1R 才保本"就要涨掉一个远超 1×ATR 的幅度，门槛异常高。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

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

from page_kline import fetch, stock_list            # noqa: E402
from Backtest.Runner import run                      # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_REC = []
_orig = LayeredExitPolicy._initial_r


def patched(self, signal, entry_price, state):
    is_long = signal.side.value > 0 if hasattr(signal.side, "value") else True
    A = 0.0
    if is_long and signal.fractal_low > 0:
        A = max(entry_price - signal.fractal_low, 0.0)
    elif (not is_long) and signal.fractal_high > 0:
        A = max(signal.fractal_high - entry_price, 0.0)
    atr = self._atr() if self.use_atr else None
    B = self.atr_sl_multiple * atr if atr else 0.0
    R = _orig(self, signal, entry_price, state)
    _REC.append({"A": A, "B": B, "R": R, "entry": entry_price, "atr": atr or 0.0})
    return R


LayeredExitPolicy._initial_r = patched


def one(code, freq, n):
    rows = fetch(code, freq, n)
    recs = []
    for r in rows:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        recs.append(rr)
    return run(code[:2], code[2:], freq, None, None, records=recs)


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    freq = sys.argv[2] if len(sys.argv) > 2 else "w"
    codes = stock_list(limit)
    for c in codes:
        try:
            one(c, freq, 800)
        except Exception:                             # noqa: BLE001
            pass
    print("样本：{} 笔入场的 R 构成（{}，{} 只标的）".format(len(_REC), freq, limit))
    print("-" * 96)
    print("{:<12} {:>8} {:>8} {:>8} {:>8} {:>8} {:>8}".format(
        "主导项", "笔数", "占比%", "平均A%", "平均B%", "平均R%", "R/ATR"))
    a_dom = [x for x in _REC if x["A"] > x["B"]]
    b_dom = [x for x in _REC if x["B"] >= x["A"]]

    def avg(g, k, base="entry"):
        return sum(x[k] / x[base] * 100 for x in g) / max(len(g), 1)

    def ratr(g):
        v = [x["R"] / x["atr"] for x in g if x["atr"] > 0]
        return sum(v) / max(len(v), 1)

    print("{:<12} {:>8} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.2f}".format(
        "A(结构)主导", len(a_dom), len(a_dom) / len(_REC) * 100,
        avg(a_dom, "A"), avg(a_dom, "B"), avg(a_dom, "R"), ratr(a_dom)))
    print("{:<12} {:>8} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.2f}".format(
        "B(ATR)主导", len(b_dom), len(b_dom) / len(_REC) * 100,
        avg(b_dom, "A"), avg(b_dom, "B"), avg(b_dom, "R"), ratr(b_dom)))
    print("{:<12} {:>8} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.1f} {:>8.2f}".format(
        "全部", len(_REC), 100.0, avg(_REC, "A"), avg(_REC, "B"),
        avg(_REC, "R"), ratr(_REC)))

    print("\n1R 门槛 = 几个 ATR？（R/ATR 分布）")
    import collections
    c = collections.Counter()
    for x in _REC:
        if x["atr"] <= 0:
            continue
        v = x["R"] / x["atr"]
        c["<1" if v < 1 else "1-1.5" if v < 1.5 else "1.5-2" if v < 2
          else "2-3" if v < 3 else "3-5" if v < 5 else ">=5"] += 1
    for k in ["<1", "1-1.5", "1.5-2", "2-3", "3-5", ">=" + "5"]:
        if c[k]:
            print("   R/ATR ∈ {:<8} {:>5} 笔  {:>5.1f}%".format(
                k, c[k], c[k] / len(_REC) * 100))


if __name__ == "__main__":
    main()
