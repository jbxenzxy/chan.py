# -*- coding: utf-8 -*-
"""单笔交易「浮盈回吐」解剖：常山北明 2025-04-30 那笔 0 类做多。"""
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

from page_kline import fetch                       # noqa: E402
from replay import replay                           # noqa: E402
from Backtest.Runner import run                     # noqa: E402


def analyze(code: str, freq: str, n: int, pick: str = None):
    bars = fetch(code, freq, n)
    from datetime import datetime
    recs = []
    for r in bars:
        rr = dict(r)
        rr["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
        recs.append(rr)
    res = run(code[:2], code[2:], freq, None, None, records=recs)
    idx = {b["dt"]: i for i, b in enumerate(bars)}
    return bars, res, idx


def main():
    code, freq, n = "sz000158", "w", 800
    bars, res, didx = analyze(code, freq, n)
    print("全部交易：")
    for t in res.trades:
        print("  #{} {} {} {} entry={} R={} exit={} Rm={} reason={}".format(
            t.trade_id, t.bsp_type, t.side, t.entry_date[:10], t.entry_price,
            t.r_distance, t.exit_date[:10] if t.exit_date else "-",
            t.r_multiple, t.exit_reason))

    tgt = [t for t in res.trades if t.entry_date[:10] == "2025-04-30"]
    if not tgt:
        print("\n未找到 2025-04-30 的笔")
        return
    t = tgt[0]
    ei = next(i for i, b in enumerate(bars) if b["dt"][:10] == t.entry_date[:10])
    side = 1 if t.side == "long" else -1
    rp = replay(bars, ei, t.entry_price, t.r_distance, side)
    print("\n" + "=" * 96)
    print("解剖：{} 周K  {} 入场  entry={:.3f}  R={:.3f}  R%={:.1f}%".format(
        code, t.entry_date[:10], t.entry_price, t.r_distance,
        t.r_distance / t.entry_price * 100))
    print("=" * 96)
    print("保本触发线（需 best > 1R）：{:.3f}  = 入场 +{:.1f}%".format(
        t.entry_price + t.r_distance, t.r_distance / t.entry_price * 100))
    print("保本落点（触发后保护价）= 入场 + 0.5R = {:.3f}".format(
        t.entry_price + 0.5 * t.r_distance))
    print("初始止损 = 入场 − R = {:.3f}".format(t.entry_price - t.r_distance))
    print("\n逐根轨迹（entry 之后）：")
    print("{:<12} {:>7} {:>7} {:>7} {:>7} {:>8} {:>8} {:>7} {:>8} {}".format(
        "date", "open", "high", "low", "close", "MFE点", "MFE%", "MFE/R", "stop", "phase"))
    for r in rp.rows:
        print("{:<12} {:>7.2f} {:>7.2f} {:>7.2f} {:>7.2f} {:>8.2f} {:>8.1f} {:>7.2f} {:>8.2f} {}".format(
            r.date, r.open, r.high, r.low, r.close, r.fav, r.fav_pct * 100,
            r.fav_r, r.stop, r.phase + ("  ← 离场" if r.exited else "")))
    print("\n结论：MFE = {:.3f} 点 = {:.1f}% = {:.2f}R；保本需 {:.2f}R ⇒ {}".format(
        rp.mfe, rp.mfe_pct * 100, rp.mfe_r, 1.0,
        "已触发保本" if rp.be_reached else "** 差 {:.2f}R（{:.3f} 点 / {:.1f}个百分点）未触发 **".format(
            1.0 - rp.mfe_r, t.r_distance - rp.mfe,
            (1.0 - rp.mfe_r) * t.r_distance / t.entry_price * 100)))
    print("最终离场：{} close={:.3f}  R倍数={:.2f}  毛收益={:.1f}%  reason={}".format(
        rp.rows[-1].date, rp.exit_price, rp.r_multiple, rp.net_pct * 100, rp.exit_reason))


if __name__ == "__main__":
    main()
