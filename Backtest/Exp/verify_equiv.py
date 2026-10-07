# -*- coding: utf-8 -*-
"""等价性钉死：ExpExitPolicy 在【所有实验开关 = 关闭】时必须与原实现逐笔一致。

不通过就说明 exp_policy.py 抄错了，后面所有 A/B 结论都不可信。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(os.path.dirname(HERE), "wt_latest")
if not os.path.isdir(REPO):  # 入库布局（<repo>/Backtest/Exp/）⇒ 回退到仓库根
    REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
os.chdir(REPO)

from fetch_kline import fetch            # noqa: E402
from Backtest.Runner import run          # noqa: E402
import exp_policy                        # noqa: E402


def recs_of(code, freq, n):
    rows = fetch(code, freq, n)
    out = []
    for r in rows:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        out.append(rr)
    return out


def one(code, freq, n):
    recs = recs_of(code, freq, n)
    res = run(code[:2], code[2:], freq, None, None, records=recs)
    return [(t.trade_id, t.bsp_type, t.side, t.entry_date, round(t.entry_price, 6),
             round(t.r_distance or 0, 6), t.exit_date,
             None if t.exit_price is None else round(t.exit_price, 6),
             t.exit_reason, t.r_multiple, t.bars_held) for t in res.trades]


def main():
    from fetch_kline import stock_list
    codes = stock_list(60)
    bad = 0
    checked = 0
    for idx, c in enumerate(codes):
        try:
            a = one(c, "w", 800)
        except Exception as e:                        # noqa: BLE001
            print("  [skip-base] {}: {}".format(c, e))
            continue
        exp_policy.install()
        exp_policy.reset()
        try:
            b = one(c, "w", 800)
        finally:
            exp_policy.uninstall()
        checked += 1
        if a != b:
            bad += 1
            print("  ✗ 不一致 {}：base={} exp={}".format(c, len(a), len(b)))
            for x, y in zip(a, b):
                if x != y:
                    print("      base={}\n      exp ={}".format(x, y))
        if (idx + 1) % 15 == 0:
            print("  ...{}/{} checked={} bad={}".format(idx + 1, len(codes), checked, bad),
                  flush=True)
    print("\n等价性结果：检查 {} 只，不一致 {} 只 ⇒ {}".format(
        checked, bad, "PASS ✅" if bad == 0 else "FAIL ❌"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
