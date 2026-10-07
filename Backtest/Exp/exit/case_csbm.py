# -*- coding: utf-8 -*-
"""复现常山北明(sz000158) 周K 的一笔交易，打印全部逐笔明细。"""
from __future__ import annotations

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

from page_kline import fetch, to_records           # noqa: E402
from Backtest.Runner import run                     # noqa: E402
from Backtest.ExitParams import STOCK_EXIT_PARAMS   # noqa: E402


def show(code: str, freq: str, n: int, start: str = None, end: str = None,
         exit_params=None, bsp_types=None, verbose=True):
    rows = fetch(code, freq, n)
    recs = to_records(rows)
    bf = None
    if bsp_types:
        from Backtest.Filter import filter_from_choices
        bf = filter_from_choices(bsp_types)
    res = run(code[:2], code[2:], freq, start, end, records=recs,
              bsp_filter=bf, exit_params=exit_params)
    return res


def dump(res, title=""):
    print("=" * 100)
    print(title, "| bars=", res.bars_total, "| signals=", res.signals_seen,
          "| params=", res.exit_params)
    print("-" * 100)
    print("{:<4} {:<6} {:<5} {:<12} {:>8} {:>8} {:>6} {:<12} {:>8} {:>7} {:>7} {:>7} {}".format(
        "id", "type", "side", "entry_dt", "entry", "R", "R%", "exit_dt",
        "exit", "Rmult", "gross%", "net%", "reason"))
    for t in res.trades:
        rpct = (t.r_distance / t.entry_price * 100) if t.entry_price else 0
        print("{:<4} {:<6} {:<5} {:<12} {:>8.3f} {:>8.3f} {:>6.1f} {:<12} {:>8} {:>7} {:>7} {:>7} {}".format(
            t.trade_id, t.bsp_type, t.side, t.entry_date[:10], t.entry_price,
            t.r_distance or 0, rpct,
            (t.exit_date or "-")[:10],
            ("%.3f" % t.exit_price) if t.exit_price else "-",
            ("%.2f" % t.r_multiple) if t.r_multiple is not None else "-",
            ("%.1f" % (t.gross_return * 100)) if t.gross_return is not None else "-",
            ("%.1f" % (t.net_return * 100)) if t.net_return is not None else "-",
            t.exit_reason or ("OPEN" if t.open_ else "-")))


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "sz000158"
    freq = sys.argv[2] if len(sys.argv) > 2 else "w"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 800
    dump(show(code, freq, n), "{} {} 基线（STOCK_EXIT_PARAMS）".format(code, freq))
