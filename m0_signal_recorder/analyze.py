#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M0 信号分析器 —— 重绘率统计 + 止盈止损参数回测
=================================================================
读取 signal_recorder.py 录制的 signals.json / klines.json，回答三个问题：

  ① 信号重绘有多严重？（消失率 / 位移率）
  ② 信号 K 线振幅多大？（决定止损距离）
  ③ 「盈利 N 点止盈 + 跌破信号K线极值止损」这组参数，期望是正是负？

回测规则（与用户约定一致）
    买点：入场价 = 信号K线收盘价(price)；止损价 = 信号K线最低价(low)；
          止盈价 = 入场价 + take_profit_points
    卖点：镜像（止损价 = 信号K线最高价(high)；止盈价 = 入场价 - N）
    出场判定：信号K线**之后**逐根推进；同一根 K 线内同时触及止盈与止损时，
              按**止损**计（悲观假设，避免高估）。

成本模型（默认 IF，可用参数覆盖）
    开仓费率 0.0023%，平今费率 0.0345%（中金所标准，以交易所最新公告为准）
    滑点默认 0.2 点/边（IF 最小变动价位 0.2 点 = 1 tick）

用法
    python analyze.py --out ./out
    python analyze.py --out ./out --take-profit 10 --slippage 0.2
    python analyze.py --out ./out --scan        # 参数网格扫描
"""
import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# IF 默认参数
DEFAULT_MULTIPLIER = 300.0      # 元/点
DEFAULT_OPEN_RATE = 0.000023    # 开仓费率
DEFAULT_CLOSE_TODAY_RATE = 0.000345  # 平今费率
DEFAULT_SLIPPAGE = 0.2          # 点/边


def load(out_dir):
    with open(os.path.join(out_dir, "signals.json"), "r", encoding="utf-8") as f:
        sig = json.load(f)
    with open(os.path.join(out_dir, "klines.json"), "r", encoding="utf-8") as f:
        kl = json.load(f)
    return sig, kl


def build_index(klines):
    """返回 {timestamp: i} 与 {date_str: i}，双索引兜底匹配。"""
    by_ts, by_date = {}, {}
    for i, k in enumerate(klines):
        by_ts[k.get("timestamp")] = i
        if k.get("date"):
            by_date[k["date"]] = i
    return by_ts, by_date


def stats(signals, klines):
    print("=" * 72)
    print("一、信号重绘统计")
    print("=" * 72)
    total = len(signals)
    if total == 0:
        print("  无信号数据，请先运行 signal_recorder.py 录制。")
        return None
    buys = [s for s in signals.values() if s.get("is_buy")]
    sells = [s for s in signals.values() if not s.get("is_buy")]
    disappeared = [s for s in signals.values() if s.get("status") == "gone"]
    repainted = [s for s in signals.values() if s.get("disappear_count", 0) > 0]
    moved = [s for s in signals.values() if len(s.get("revisions", [])) > 1]

    print("  信号总数        : {}  (买 {} / 卖 {})".format(total, len(buys), len(sells)))
    print("  已消失(未回来)  : {}  ({:.1%})".format(len(disappeared), len(disappeared) / total))
    print("  曾消失又回来    : {}  ({:.1%})".format(len(repainted), len(repainted) / total))
    print("  价格/极值位移过 : {}  ({:.1%})".format(len(moved), len(moved) / total))
    unstable = len(disappeared) + len(repainted) + len(moved)
    print("  >>> 不稳定信号合计: {}  ({:.1%})".format(unstable, unstable / total))

    print()
    print("=" * 72)
    print("二、信号 K 线振幅分布（止损距离 = high - low）")
    print("=" * 72)
    amps = []
    for s in signals.values():
        f = s.get("final", {})
        h, low = f.get("high"), f.get("low")
        if h is not None and low is not None:
            amps.append(abs(h - low))
    if amps:
        amps.sort()
        n = len(amps)

        def pct(p):
            return amps[min(n - 1, int(n * p))]
        print("  样本数 : {}".format(n))
        print("  最小/最大 : {:.2f} / {:.2f} 点".format(amps[0], amps[-1]))
        print("  分位数 : P25={:.2f}  中位={:.2f}  P75={:.2f}  P90={:.2f}".format(
            pct(0.25), pct(0.5), pct(0.75), pct(0.9)))
        print("  均值   : {:.2f} 点".format(sum(amps) / n))
    return amps


def backtest(signals, klines, take_profit, slippage,
             multiplier, open_rate, close_today_rate, use_close_today=True):
    """返回逐笔交易明细 + 汇总。"""
    by_ts, by_date = build_index(klines)
    close_rate = close_today_rate if use_close_today else open_rate
    trades, skipped = [], 0

    for key, s in signals.items():
        f = s.get("final", {})
        entry, high, low = f.get("price"), f.get("high"), f.get("low")
        if entry is None or high is None or low is None:
            skipped += 1
            continue
        i = by_ts.get(s.get("timestamp"))
        if i is None:
            i = by_date.get(s.get("date"))
        if i is None or i + 1 >= len(klines):
            skipped += 1
            continue

        is_buy = bool(s.get("is_buy"))
        if is_buy:
            stop = low
            target = entry + take_profit
        else:
            stop = high
            target = entry - take_profit

        result, exit_price, exit_i = "open", None, None
        for j in range(i + 1, len(klines)):
            k = klines[j]
            kh, kl_ = k.get("high"), k.get("low")
            if kh is None or kl_ is None:
                continue
            if is_buy:
                hit_stop = kl_ <= stop
                hit_target = kh >= target
            else:
                hit_stop = kh >= stop
                hit_target = kl_ <= target
            if hit_stop:            # 悲观：同根 K 线内先判止损
                result, exit_price, exit_i = "stop", stop, j
                break
            if hit_target:
                result, exit_price, exit_i = "target", target, j
                break
        if exit_price is None:      # 未触及 → 按最后一根收盘价浮动平
            result = "open"
            exit_price = klines[-1].get("close")
            exit_i = len(klines) - 1

        if is_buy:
            gross = exit_price - entry
        else:
            gross = entry - exit_price
        gross -= slippage * 2                      # 双边滑点
        cost_pts = entry * open_rate + exit_price * close_rate   # 元→点
        net = gross - cost_pts

        trades.append({
            "key": key, "is_buy": is_buy, "result": result,
            "entry": entry, "exit": exit_price, "stop": stop, "target": target,
            "gross_points": round(gross, 3),
            "cost_points": round(cost_pts, 3),
            "net_points": round(net, 3),
            "net_yuan": round(net * multiplier, 1),
            "bars_held": (exit_i - i) if exit_i else None,
            "signal_date": s.get("date"),
        })

    # 汇总
    closed = [t for t in trades if t["result"] in ("stop", "target")]
    wins = [t for t in closed if t["net_points"] > 0]
    losses = [t for t in closed if t["net_points"] <= 0]
    net_sum = sum(t["net_points"] for t in closed)
    avg_win = (sum(t["net_points"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["net_points"] for t in losses) / len(losses)) if losses else 0.0
    summary = {
        "take_profit": take_profit,
        "trades": len(trades), "closed": len(closed),
        "unclosed": len(trades) - len(closed), "skipped": skipped,
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "wins": len(wins), "losses": len(losses),
        "avg_win_points": round(avg_win, 3),
        "avg_loss_points": round(avg_loss, 3),
        "profit_factor": (abs(avg_win / avg_loss) if avg_loss else 0.0),
        "expectancy_points": (net_sum / len(closed)) if closed else 0.0,
        "total_net_points": round(net_sum, 3),
        "total_net_yuan": round(net_sum * multiplier, 1),
    }
    return trades, summary


def print_summary(summary, multiplier):
    print()
    print("  止盈 {} 点 | 平仓 {} 笔（未触及 {} 笔，跳过 {} 笔）".format(
        summary["take_profit"], summary["closed"],
        summary["unclosed"], summary["skipped"]))
    if summary["closed"] == 0:
        print("  样本不足，无法评估。")
        return
    print("  胜率        : {:.1%}  ({} 胜 / {} 负)".format(
        summary["win_rate"], summary["wins"], summary["losses"]))
    print("  平均盈利    : {:+.2f} 点   平均亏损: {:+.2f} 点   盈亏比: {:.2f}".format(
        summary["avg_win_points"], summary["avg_loss_points"],
        summary["profit_factor"]))
    print("  单笔期望    : {:+.3f} 点  ({:+.1f} 元/手)".format(
        summary["expectancy_points"],
        summary["expectancy_points"] * multiplier))
    print("  累计净盈亏  : {:+.2f} 点  ({:+.1f} 元/手)".format(
        summary["total_net_points"], summary["total_net_yuan"]))
    verdict = "正期望" if summary["expectancy_points"] > 0 else "负期望"
    print("  >>> 结论: {}".format(verdict))


def scan(signals, klines, tps, slippage, multiplier, open_rate, close_rate):
    print()
    print("=" * 72)
    print("四、止盈参数扫描（止损 = 信号K线极值，成本含平今费 + 双边滑点）")
    print("=" * 72)
    print("  {:>8} {:>7} {:>8} {:>10} {:>10} {:>12} {:>10}".format(
        "止盈点", "平仓数", "胜率", "平均盈", "平均亏", "单笔期望", "累计元/手"))
    rows = []
    for tp in tps:
        _, sm = backtest(signals, klines, tp, slippage, multiplier,
                         open_rate, close_rate)
        rows.append(sm)
        if sm["closed"]:
            print("  {:>8} {:>7} {:>7.1%} {:>10.2f} {:>10.2f} {:>12.3f} {:>10.0f}".format(
                tp, sm["closed"], sm["win_rate"], sm["avg_win_points"],
                sm["avg_loss_points"], sm["expectancy_points"], sm["total_net_yuan"]))
        else:
            print("  {:>8} {:>7}  样本不足".format(tp, sm["closed"]))
    best = max((r for r in rows if r["closed"] >= 5),
               key=lambda r: r["expectancy_points"], default=None)
    if best:
        print()
        print("  >>> 本组数据下最优止盈: {} 点（单笔期望 {:+.3f} 点，胜率 {:.1%}）".format(
            best["take_profit"], best["expectancy_points"], best["win_rate"]))
        print("  ⚠ 样本量较小时最优参数极不稳定，仅供方向参考，勿直接外推。")


def main():
    ap = argparse.ArgumentParser(description="M0 信号分析与参数回测")
    ap.add_argument("--out", default="./out", help="录制输出目录")
    ap.add_argument("--take-profit", type=float, default=10.0, help="止盈点数")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE, help="单边滑点(点)")
    ap.add_argument("--multiplier", type=float, default=DEFAULT_MULTIPLIER, help="合约乘数(元/点)")
    ap.add_argument("--open-rate", type=float, default=DEFAULT_OPEN_RATE, help="开仓费率")
    ap.add_argument("--close-today-rate", type=float,
                    default=DEFAULT_CLOSE_TODAY_RATE, help="平今费率")
    ap.add_argument("--no-close-today", action="store_true",
                    help="按平昨费率计算（隔夜持仓）")
    ap.add_argument("--scan", action="store_true", help="启用止盈参数扫描")
    ap.add_argument("--scan-list", default="5,8,10,12,15,20,30", help="扫描的止盈点数")
    ap.add_argument("--dump", default="", help="导出逐笔明细到 JSON 文件")
    args = ap.parse_args()

    sig_data, kl_data = load(args.out)
    signals = sig_data.get("signals", {})
    klines = sorted(kl_data.get("klines", []), key=lambda x: x["timestamp"])

    print("合约: {}  周期: {}   K线 {} 根".format(
        sig_data.get("meta", {}).get("symbol"),
        sig_data.get("meta", {}).get("freq"), len(klines)))
    if klines:
        print("K线区间: {} ~ {}".format(klines[0].get("date"), klines[-1].get("date")))

    stats(signals, klines)

    print()
    print("=" * 72)
    print("三、单组参数回测（止盈 {} 点 / 止损=信号K线极值 / 滑点 {} 点/边）".format(
        args.take_profit, args.slippage))
    print("=" * 72)
    trades, summary = backtest(
        signals, klines, args.take_profit, args.slippage, args.multiplier,
        args.open_rate, args.close_today_rate, not args.no_close_today)
    print_summary(summary, args.multiplier)

    if args.scan:
        tps = [float(x) for x in args.scan_list.split(",")]
        scan(signals, klines, tps, args.slippage, args.multiplier,
             args.open_rate, args.close_today_rate)

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "trades": trades}, f,
                      ensure_ascii=False, indent=1)
        print("\n  逐笔明细已导出: {}".format(args.dump))


if __name__ == "__main__":
    main()
