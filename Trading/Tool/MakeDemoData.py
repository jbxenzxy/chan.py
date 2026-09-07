#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成 demo 回放数据
==================
产出格式与 M0 录制器**完全一致**（signals.json + klines.json），
用于在还没有真实录制数据时先把网关跑通、把链路验证一遍。
拿到 M0 真实数据后，把 --replay-dir 指向 M0 的输出目录即可，无需任何改动。

    python tools/make_demo_data.py --out ./replay_data

数据特点
    - 3 个交易日 × 48 根 5m K 线（09:30-11:30 / 13:00-15:00，与 IF 交易时段一致）
    - 价格用带漂移的随机游走，信号由短期趋势方向决定（有胜有负，不是白噪声）
    - 含若干 status="gone" 的信号，用于验证重绘信号的处理路径
"""
from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta

SYMBOL = "KQ.m@CFFEX.IF"
FREQ = "5m"


def session_times(day: datetime, bars_per_session: int = 24):
    """IF 交易时段：09:30-11:30 与 13:00-15:00，每根 5 分钟。"""
    out = []
    for start_h, start_m in ((9, 30), (13, 0)):
        t = day.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        for i in range(bars_per_session):
            out.append(t + timedelta(minutes=5 * i))
    return out


def gen(out_dir: str, days: int = 3, seed: int = 7, start_price: float = 4000.0,
        sigma: float = 2.2, every: int = 10, gone_every: int = 5):
    rnd = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)

    day0 = datetime(2026, 9, 1)
    times = []
    for d in range(days):
        times.extend(session_times(day0 + timedelta(days=d)))

    klines = []
    price = start_price
    for t in times:
        o = price
        drift = rnd.gauss(0, sigma)
        c = o + drift
        h = max(o, c) + abs(rnd.gauss(0, sigma * 0.5))
        l = min(o, c) - abs(rnd.gauss(0, sigma * 0.5))
        klines.append({
            "timestamp": int(t.timestamp() * 1000),
            "date": t.strftime("%Y-%m-%d %H:%M"),
            "open": round(o, 2), "high": round(h, 2),
            "low": round(l, 2), "close": round(c, 2),
            "vol": rnd.randint(500, 3000),
        })
        price = c

    signals = {}
    n = 0
    for i in range(every, len(klines) - 2, every):
        k = klines[i]
        look = klines[i - 5]["close"]
        is_buy = k["close"] > look            # 短期趋势跟随
        btype = "1" if n % 3 else "2"
        key = "{}|{}|{}".format(k["date"], btype, "B" if is_buy else "S")
        gone = (n % gone_every == gone_every - 1)
        signals[key] = {
            "key": key, "symbol": SYMBOL, "freq": FREQ,
            "date": k["date"], "timestamp": k["timestamp"],
            "type": btype, "is_buy": is_buy,
            "first_seen_at": k["date"] + ":00",
            "last_seen_at": k["date"] + ":00",
            "seen_frames": 1,
            "disappear_count": 1 if gone else 0,
            "status": "gone" if gone else "alive",
            "revisions": [{"at": k["date"] + ":00", "price": k["close"],
                           "high": k["high"], "low": k["low"]}],
            "final": {"price": k["close"], "high": k["high"], "low": k["low"]},
        }
        n += 1

    with open(os.path.join(out_dir, "klines.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": {"symbol": SYMBOL, "freq": FREQ, "count": len(klines)},
                   "klines": klines}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "signals.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": {"symbol": SYMBOL, "freq": FREQ, "count": len(signals)},
                   "signals": signals}, f, ensure_ascii=False, indent=1)
    print("已生成: {}  K线 {} 根  信号 {} 条（其中已消失 {} 条）".format(
        os.path.abspath(out_dir), len(klines), len(signals),
        sum(1 for s in signals.values() if s["status"] == "gone")))


def main():
    ap = argparse.ArgumentParser(description="生成 demo 回放数据")
    ap.add_argument("--out", default="./replay_data", help="输出目录")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start-price", type=float, default=4000.0)
    ap.add_argument("--sigma", type=float, default=2.2, help="每根K线波动（点）")
    ap.add_argument("--every", type=int, default=10, help="每 N 根K线放一个信号")
    a = ap.parse_args()
    gen(a.out, days=a.days, seed=a.seed, start_price=a.start_price,
        sigma=a.sigma, every=a.every)


if __name__ == "__main__":
    main()
