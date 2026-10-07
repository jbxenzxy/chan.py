# -*- coding: utf-8 -*-
"""等价性钉死：ExpExitPolicy 在【所有实验开关 = 关闭】时必须与原实现逐笔一致。

不通过就说明 exp_policy.py 抄错了，后面所有 A/B 结论都不可信。
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

from page_kline import fetch            # noqa: E402
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
    from page_kline import stock_list
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
