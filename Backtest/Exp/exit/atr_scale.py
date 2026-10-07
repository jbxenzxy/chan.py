# -*- coding: utf-8 -*-
"""跨周期波动量级标定 —— 百分比阈值到底"等于几个 ATR"？

背景
----
出场参数上一轮从 R 口径改成百分比口径（保本 5% / 跟踪 10% / 跟踪回撤 5~8%），
标定样本是**股票周K**。但百分比是**绝对量纲**：同一个 5%，在股票周K上是"涨了
一小段"，在 IF 1 分钟上可能是"涨了十几个 ATR"。本脚本量化这件事，并给出
把参数改成**周期无关**形式（k × ATR%）时该取的 k。

口径
----
  ATR%    = Wilder ATR(14) / 当根收盘价 × 100
  均幅%   = (high − low) / close × 100
  单根σ%  = 当根收益率的标准差 × 100
  k       = 触发百分比 / ATR%  ⇒  "这个阈值相当于几个 ATR"

用法::

    python atr_scale.py                 # 股票(w/d/30m/15m/5m) + 期货 IF/IH/IC/IM(1m~d)
    python atr_scale.py --no-fut        # 只算股票（期货段要联网，离线/排障时用）
"""
from __future__ import annotations

import json
import os
import statistics as st
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

import fetch_fut                                        # noqa: E402
from page_kline import stock_list                        # noqa: E402


def atr_pct(recs, period=14):
    """Wilder ATR / close × 100 的逐根序列。"""
    if len(recs) < period + 2:
        return []
    trs, prev_c = [], None
    for r in recs:
        h, l, c = r["high"], r["low"], r["close"]
        trs.append(h - l if prev_c is None
                   else max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    atr = sum(trs[1:period + 1]) / period
    out = []
    for i in range(period + 1, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        c = recs[i]["close"]
        if c:
            out.append(atr / c * 100.0)
    return out


def stats(recs):
    if len(recs) < 20:
        return None
    ap = atr_pct(recs)
    if not ap:
        return None
    rng = [(r["high"] - r["low"]) / r["close"] * 100.0
           for r in recs if r["close"]]
    rets = [(recs[i]["close"] - recs[i - 1]["close"]) / recs[i - 1]["close"] * 100.0
            for i in range(1, len(recs)) if recs[i - 1]["close"]]
    return {"n": len(recs), "atr": st.median(ap), "rng": st.median(rng),
            "sd": st.pstdev(rets) if len(rets) > 1 else 0.0}


def load_stocks(freq, n=None, limit=800):
    """**页面同源** K 线（通达信 vipdoc + 自实现前复权）——五个周期同一个函数。

    用 `use_cache=False` 直取：`tdx_source.records()` 的 memo 会把**每只标的的整段
    K 线**留在内存里（日K 800 只 ≈ 1.1M 个 dict，已实测涨到 1.8GB 且被 GC 拖死，
    5m 会更糟）。本脚本每只标的只留统计量，不需要 memo。
    """
    import tdx_source
    out = []
    for c in stock_list(limit):
        try:
            recs = tdx_source.records(c, freq, use_cache=False)
        except Exception:                                    # noqa: BLE001
            recs = []
        s = stats(recs) if recs else None
        if s:
            out.append(s)
    return out


def load_stocks_min(freq, n=None, limit=300):
    """分钟周期（5m/15m/30m）—— 与 `load_stocks` 同源（页面也是前复权）。"""
    return load_stocks(freq, n, limit)


def merge(rows):
    """多标的 → 取各指标的中位数（先按标的算，再跨标的取中位 ⇒ 不被单只长历史带偏）。"""
    if not rows:
        return None
    def med(f):
        return st.median([f(r) for r in rows])
    return {"codes": len(rows), "n": int(med(lambda r: r["n"])),
            "atr": med(lambda r: r["atr"]), "rng": med(lambda r: r["rng"]),
            "sd": med(lambda r: r["sd"])}


def main():
    no_fut = "--no-fut" in sys.argv          # 期货段要联网（新浪）；离线/排障时跳过
    print("=" * 104)
    print("一、各周期 / 各标的的波动量级（中位数）", flush=True)
    print("=" * 104)
    print("{:<22} {:>7} {:>8} {:>9} {:>9} {:>9} {:>9} {:>9}".format(
        "标的 / 周期", "标的数", "根数", "ATR%", "均幅%", "单根σ%",
        "5%/ATR", "10%/ATR"))
    print("-" * 104)
    out = {}

    for freq, lab in (("w", "股票 周K"), ("d", "股票 日K")):
        m = merge(load_stocks(freq))
        if m:
            out[lab] = m
            print("{:<22} {:>7} {:>8} {:>9.3f} {:>9.3f} {:>9.3f} {:>9.2f} {:>9.2f}".format(
                lab, m["codes"], m["n"], m["atr"], m["rng"], m["sd"],
                5.0 / m["atr"], 10.0 / m["atr"]), flush=True)

    for freq, lab in (("30m", "股票 30分"), ("15m", "股票 15分"),
                      ("5m", "股票 5分")):
        m = merge(load_stocks_min(freq))
        if m:
            out[lab] = m
            print("{:<22} {:>7} {:>8} {:>9.3f} {:>9.3f} {:>9.3f} {:>9.2f} {:>9.2f}".format(
                lab, m["codes"], m["n"], m["atr"], m["rng"], m["sd"],
                5.0 / m["atr"], 10.0 / m["atr"]), flush=True)

    if no_fut:
        print("（--no-fut：跳过期货段）", flush=True)
    for sym in (() if no_fut else fetch_fut.products()):
        for f in ("1m", "5m", "15m", "30m", "60m", "d"):
            try:
                recs = fetch_fut.fetch(sym, f)
            except Exception:                                # noqa: BLE001
                continue                                     # 联网失败 ⇒ 跳过该格
            s = stats(recs) if recs else None
            if not s:
                continue
            lab = "{} {}".format(sym, f)
            out[lab] = s
            print("{:<22} {:>7} {:>8} {:>9.3f} {:>9.3f} {:>9.3f} {:>9.2f} {:>9.2f}".format(
                lab, 1, s["n"], s["atr"], s["rng"], s["sd"],
                5.0 / s["atr"], 10.0 / s["atr"]), flush=True)
        print("-" * 104, flush=True)

    print()
    print("=" * 104)
    print("二、把「股票周K 上标定的 5% / 8% / 10%」换算到别的周期")
    print("=" * 104)
    base = out.get("股票 周K")
    if base:
        k_be = 5.0 / base["atr"]
        k_tr = 10.0 / base["atr"]
        print("标定基准（股票周K）：ATR% = {:.3f}% ⇒ 保本 5% = {:.2f}×ATR，"
              "跟踪 10% = {:.2f}×ATR".format(base["atr"], k_be, k_tr))
        print()
        print("{:<22} {:>9} {:>14} {:>14} {:>12}".format(
            "换到该周期", "ATR%", "保本 5%/ATR", "跟踪 10%/ATR", "同 k 的阈值%"))
        print("-" * 76)
        for lab, m in out.items():
            if lab == "股票 周K":
                continue
            print("{:<22} {:>9.3f} {:>14.2f} {:>14.2f} {:>12}".format(
                lab, m["atr"], 5.0 / m["atr"], 10.0 / m["atr"],
                "保本 {:.2f}% / 跟踪 {:.2f}%".format(k_be * m["atr"],
                                                    k_tr * m["atr"])))

    with open(os.path.join(HERE, "atr_scale.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n→ atr_scale.json")


if __name__ == "__main__":
    main()
