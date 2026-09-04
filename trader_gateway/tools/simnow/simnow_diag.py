# -*- coding: utf-8 -*-
"""
SimNow 持仓诊断脚本
====================
用途：
    在跑任何 gateway 之前，先查清楚 SimNow 账户的实际状态：
    ① 当前账户资金
    ② 当前所有品种持仓（含今/昨仓、手数、浮动盈亏）
    ③ 当前所有未成交委托（避免幽灵委托堆积）
    ④ 可选：一键市价平仓所有非零持仓（--close-all）

为啥要这个脚本
    上一轮回归测试 (run_regression_v4) 出现 20+ 笔 close 全被 CTP 拒为
    "平仓量超过持仓量"。tqsdk 报 order.filled=True，但 CTP 端没有对应仓位。
    最大嫌疑是：
        (a) 用户账户里有上一轮 SSE 留下来的真实仓位（被 tqsdk 当成"我们刚开仓的"）；
        (b) 上一轮 SSE 的"成交"其实是历史的、平掉了就没了；
        (c) 上一轮 gateway 自己造的、从来没真到 CTP。
    不论哪种，先看下账户实情再说。

用法
    cd C:\\my_chan_project\\trader_gateway\\tools\\simnow
    python.exe simnow_diag.py                    # 只查询，不动手
    python.exe simnow_diag.py --close-all        # 查完市价平掉所有非零持仓（强平）
    python.exe simnow_diag.py --symbol CFFEX.IF2609   # 指定只看一个品种

凭据
    与 simnow broker 同源：环境变量 SN_ACCOUNT/SN_PASSWORD/TQ_ACCOUNT/TQ_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List

from tqsdk import TqApi, TqAuth, TqAccount


def _wait_update_deadline(api: TqApi, deadline: float) -> None:
    """带 deadline 的 wait_update，避免卡死"""
    api.wait_update(deadline=deadline)


def _wait_until(api: TqApi, predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        if predicate():
            return True
        time.sleep(0.1)
    return False


def list_positions(api: TqApi, target_symbol: str = "") -> List[dict]:
    """列出账户所有品种的持仓，过滤出有数量的"""
    rows = []
    for symbol, pos in api.get_position().items():
        try:
            long_total = (getattr(pos, "pos_long_today", 0) or 0) \
                       + (getattr(pos, "pos_long_his", 0) or 0)
            short_total = (getattr(pos, "pos_short_today", 0) or 0) \
                        + (getattr(pos, "pos_short_his", 0) or 0)
            long_his = getattr(pos, "pos_long_his", 0) or 0
            short_his = getattr(pos, "pos_short_his", 0) or 0
            float_pnl = getattr(pos, "position_profit", 0) or 0
            margin = getattr(pos, "margin", 0) or 0
        except Exception as e:
            print("  [WARN] 读取 {} 持仓异常: {}".format(symbol, e))
            continue
        if long_total == 0 and short_total == 0:
            continue
        if target_symbol and symbol != target_symbol:
            continue
        rows.append({
            "symbol": symbol,
            "long": long_total,
            "short": short_total,
            "long_his": long_his,
            "short_his": short_his,
            "float_pnl": float_pnl,
            "margin": margin,
        })
    return rows


def list_pending_orders(api: TqApi, target_symbol: str = "") -> List[dict]:
    rows = []
    orders = api.get_order()
    for oid, order in orders.items():
        status = getattr(order, "status", "")
        if status != "ALIVE":
            continue
        symbol = getattr(order, "symbol", "")
        if target_symbol and symbol != target_symbol:
            continue
        rows.append({
            "order_id": oid,
            "symbol": symbol,
            "direction": getattr(order, "direction", ""),
            "offset": getattr(order, "offset", ""),
            "limit_price": getattr(order, "limit_price", None),
            "volume": getattr(order, "volume_left", 0) or 0,
        })
    return rows


def close_all(api: TqApi, positions: List[dict]) -> None:
    """市价平仓所有非零持仓（用对手价±N ticks 模拟市价）"""
    if not positions:
        print("[close-all] 没有非零持仓，无需操作")
        return
    for p in positions:
        symbol = p["symbol"]
        quote = api.get_quote(symbol)
        # 等 quote 就绪
        _wait_until(api, lambda: bool(getattr(quote, "bid_price1", 0) and getattr(quote, "ask_price1", 0)), 10.0)
        bid = float(getattr(quote, "bid_price1", 0) or 0)
        ask = float(getattr(quote, "ask_price1", 0) or 0)
        price_tick = float(getattr(quote, "price_tick", 0.2) or 0.2)

        if p["long"] > 0:
            # 平多：以对手价（bid）减 3 ticks 让价确保成交
            close_px = round(bid - 3 * price_tick, 2)
            print("[close-all] 平多 {} {} 手 @ {}".format(symbol, p["long"], close_px))
            try:
                order = api.insert_order(symbol=symbol, direction="SELL",
                                         offset="CLOSE", volume=int(p["long"]),
                                         limit_price=close_px)
                _wait_until(api, lambda: getattr(order, "status", "") == "FINISHED", 15.0)
                print("  状态: {}, 成交价: {}".format(
                    getattr(order, "status", "?"), getattr(order, "trade_price", None)))
            except Exception as e:
                print("  [WARN] 平多失败: {}".format(e))

        if p["short"] > 0:
            close_px = round(ask + 3 * price_tick, 2)
            print("[close-all] 平空 {} {} 手 @ {}".format(symbol, p["short"], close_px))
            try:
                order = api.insert_order(symbol=symbol, direction="BUY",
                                         offset="CLOSE", volume=int(p["short"]),
                                         limit_price=close_px)
                _wait_until(api, lambda: getattr(order, "status", "") == "FINISHED", 15.0)
                print("  状态: {}, 成交价: {}".format(
                    getattr(order, "status", "?"), getattr(order, "trade_price", None)))
            except Exception as e:
                print("  [WARN] 平空失败: {}".format(e))


def main():
    p = argparse.ArgumentParser(description="SimNow 持仓诊断")
    p.add_argument("--symbol", default="", help="只看一个品种（如 CFFEX.IF2609）")
    p.add_argument("--close-all", action="store_true",
                   help="市价平掉所有非零持仓（破坏性，慎用）")
    args = p.parse_args()

    sn_account = (os.environ.get("SN_ACCOUNT") or "").strip()
    sn_password = (os.environ.get("SN_PASSWORD") or "").strip()
    tq_account = (os.environ.get("TQ_ACCOUNT") or "").strip()
    tq_password = (os.environ.get("TQ_PASSWORD") or "").strip()

    missing = [k for k, v in
        (("SN_ACCOUNT", sn_account), ("SN_PASSWORD", sn_password),
         ("TQ_ACCOUNT", tq_account), ("TQ_PASSWORD", tq_password)) if not v]
    if missing:
        print("[ERROR] 缺少环境变量: {}".format(", ".join(missing)))
        print("        请设置后再运行：")
        print("        $env:SN_ACCOUNT=\"xxx\"; $env:SN_PASSWORD=\"xxx\"; ...")
        sys.exit(1)

    print("[1/4] 登录 SimNow...")
    api = TqApi(TqAccount("simnow", sn_account, sn_password),
                auth=TqAuth(tq_account, tq_password))

    # 等账户就绪
    account = api.get_account()
    if not _wait_until(api, lambda: bool(getattr(account, "balance", 0)), 15.0):
        print("[ERROR] 账户数据 15s 内未同步")
        sys.exit(2)

    print("\n[2/4] 账户资金")
    balance = float(getattr(account, "balance", 0) or 0)
    available = float(getattr(account, "available", 0) or 0)
    margin = float(getattr(account, "margin", 0) or 0)
    frozen = float(getattr(account, "frozen_margin", 0) or 0)
    risk = float(getattr(account, "risk_ratio", 0) or 0)
    print("  balance={:>12.2f}  available={:>12.2f}".format(balance, available))
    print("  margin={:>12.2f}    frozen_margin={:>12.2f}  risk_ratio={:.4f}".format(
        margin, frozen, risk))

    print("\n[3/4] 当前持仓{}".format("(过滤:" + args.symbol + ")" if args.symbol else ""))
    positions = list_positions(api, args.symbol)
    if not positions:
        print("  (无非零持仓)")
    for p in positions:
        print("  {:<16s}  多 {} 手 (昨 {})  | 空 {} 手 (昨 {})  | 浮盈 {:>8.2f}  | 保证金 {:>8.2f}".format(
            p["symbol"], p["long"], p["long_his"], p["short"], p["short_his"],
            p["float_pnl"], p["margin"]))

    print("\n[3.5/4] 当前未成交委托")
    pending = list_pending_orders(api, args.symbol)
    if not pending:
        print("  (无未成交委托)")
    for o in pending:
        print("  {:<24s}  {} {} {} 手 @ {}".format(
            o["order_id"], o["direction"], o["offset"],
            o["volume"], o["limit_price"]))

    print("\n[4/4] 诊断建议")
    if positions:
        print("  ⚠ 账户里有非零持仓！")
        print("    如果这些持仓不是你预期的，请用以下任一方式处理：")
        print("      (a) python.exe simnow_diag.py --close-all    # 市价平掉所有")
        print("      (b) 打开快期3手动平仓")
        print("    如果这些持仓是预期的（比如上个测试留下的），告诉 AI 让它据此调整策略。")
    else:
        print("  ✓ 账户无持仓，状态干净，可以跑 gateway 回归。")

    if args.close_all and positions:
        print("\n[--close-all] 开始市价平仓...")
        close_all(api, positions)
        # 再查一遍确认
        time.sleep(1.0)
        api.wait_update(deadline=time.time() + 5)
        new_list = list_positions(api, args.symbol)
        if new_list:
            print("[--close-all] ⚠ 平仓后还有剩余:")
            for p in new_list:
                print("  ", p)
        else:
            print("[--close-all] ✓ 全部平仓完成")

    api.close()


if __name__ == "__main__":
    main()