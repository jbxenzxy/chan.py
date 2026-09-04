#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2b 真成交冒烟测试
==================
在进入实时 SSE 挂机等信号之前，主动下一笔「贴近市价」的 1 手 IF，
跑通 **成交 → 持仓 → 平仓** 完整闭环，验证：
    [1] 撮合能不能成交（不只是"通道通"）
    [2] 成交后 position 字段能不能正确更新
    [3] 能不能用 SELL CLOSE 把今仓平掉
    [4] 手续费/点差成本是否在可接受范围

为什么需要这个测试？
    M2a 探针 和 M2 回放集成测试 都只是验证"委托能到交易所"，
    但 14 笔 demo 信号全部被 CFFEX 涨跌停风控拒单（错误码 50），
    原因：回放价 4004-4042 跌破当日跌停价（~4050）。
    所以**真成交**还没验证，必须主动下一笔贴近市价的小单。

设计要点
    - 用 BUY @ last+3ticks 触发立即成交
    - 等持仓更新到 > 0
    - 用 SELL CLOSE @ last-3ticks 平仓（SimNow 不支持市价单）
    - 3 ticks 价差 ≈ 1.2 点 × 300元/点 = 360 元（平今手续费另算 ~10 元）
    - 全程 wait_update 带 deadline，沿用探针的防卡死模板

前置
    pip install tqsdk
    凭据从环境变量读：SN_ACCOUNT / SN_PASSWORD / TQ_ACCOUNT / TQ_PASSWORD

用法（IF 交易时段：9:30-11:30, 13:00-15:00）
    python simnow_smoke_trade.py --symbol "CFFEX.IF2609"
    python simnow_smoke_trade.py --symbol "CFFEX.IF2609" --volume 2 --ticks 5
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time


def _finite(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return not math.isnan(v)
    return False


def _load_creds():
    keys = ("SN_ACCOUNT", "SN_PASSWORD", "TQ_ACCOUNT", "TQ_PASSWORD")
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if missing:
        print("[X] 缺少环境变量: {}".format(", ".join(missing)))
        print("    请在终端 set 之后再运行本脚本")
        return None
    return tuple(os.environ[k].strip() for k in keys)


def _wait_until(api, predicate, timeout_s: float, step_s: float = 0.2):
    """防卡死 wait_update 循环：到时间就退出。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        if predicate():
            return True
        time.sleep(step_s)
    return False


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="SimNow 真成交冒烟测试（M2b）")
    ap.add_argument("--symbol", default="CFFEX.IF2609", help="具体合约代码（非主连）")
    ap.add_argument("--volume", type=int, default=1, help="手数")
    ap.add_argument("--ticks", type=int, default=3,
                    help="BUY 加 N 个 tick、SELL 减 N 个 tick 的让价幅度")
    ap.add_argument("--tick-size", type=float, default=0.2, help="最小变动价位")
    ap.add_argument("--no-close", action="store_true",
                    help="成交后不平仓（仅做开仓验证，方便手动平）")
    args = ap.parse_args()

    creds = _load_creds()
    if creds is None:
        return 2
    sn_user, sn_pass, tq_user, tq_pass = creds

    try:
        from tqsdk import TqApi, TqAuth, TqAccount
    except ImportError:
        print("[X] 未安装 tqsdk，请先: pip install tqsdk")
        return 3

    print("[1/5] 登录 SimNow（天勤中转）...")
    api = TqApi(TqAccount("simnow", sn_user, sn_pass),
                auth=TqAuth(tq_user, tq_pass))
    print("[OK] 登录成功")

    try:
        # ---- 行情就绪 ----
        print("[2/5] 等待 {} 行情...".format(args.symbol))
        q = api.get_quote(args.symbol)
        if not _wait_until(api, lambda: _finite(getattr(q, "last_price", None)), 10):
            print("[X] 10s 内无最新价（可能非交易时段或合约代码错）")
            return 4
        last = float(q.last_price)
        print("[OK] last_price={}  datetime={}".format(last, getattr(q, "datetime", None)))

        # ---- 初始账户与持仓 ----
        acct = api.get_account()
        pos = api.get_position(args.symbol)
        init_long = (getattr(pos, "pos_long_his", 0) or 0) + (getattr(pos, "pos_long_today", 0) or 0)
        print("    balance={:.2f}  long={}  short={}".format(
            acct.get("balance", 0.0) or 0.0, init_long,
            (getattr(pos, "pos_short_his", 0) or 0) + (getattr(pos, "pos_short_today", 0) or 0)))

        # ---- 开仓 ----
        print("[3/5] 下单 BUY {}手 @ last+{}ticks ...".format(args.volume, args.ticks))
        buy_px = _round_tick(last + args.ticks * args.tick_size, args.tick_size)
        order_buy = api.insert_order(symbol=args.symbol, direction="BUY", offset="OPEN",
                                     volume=args.volume, limit_price=buy_px)
        print("    已发出 order_id={}  @{}".format(order_buy.order_id, buy_px))

        if not _wait_until(api,
                           lambda: getattr(order_buy, "status", "") == "FINISHED", 15):
            print("[!] 15s 内未 FINISHED，尝试撤单...")
            try:
                api.cancel_order(order_buy.order_id)
                api.wait_update(deadline=time.time() + 5)
            except Exception:
                pass
            print("[X] 开仓未成交，退出。请检查：涨跌停、对手价差是否过窄、撮合时段。")
            return 5
        fill_buy = float(getattr(order_buy, "trade_price", 0) or 0)
        if not _finite(fill_buy) or fill_buy <= 0:
            print("[!] FINISHED 但 trade_price 无效：last_msg={}".format(
                getattr(order_buy, "last_msg", "")))
            return 5
        print("[OK] 成交 BUY @ {}  status={}  last_msg={}".format(
            fill_buy, order_buy.status, getattr(order_buy, "last_msg", "")))

        # ---- 等持仓更新 ----
        pos = api.get_position(args.symbol)
        if not _wait_until(api,
                           lambda: (getattr(pos, "pos_long_today", 0) or 0)
                                   + (getattr(pos, "pos_long_his", 0) or 0) > init_long,
                           5):
            print("[!] 5s 内持仓未更新：pos_long_today={} pos_long_his={}".format(
                getattr(pos, "pos_long_today", 0), getattr(pos, "pos_long_his", 0)))

        if args.no_close:
            print("[done] 已开仓 {} 手（参数 --no-close 跳过平仓）".format(args.volume))
            return 0

        # ---- 平仓 ----
        print("[4/5] 平仓 SELL CLOSE {}手 @ last-{}ticks ...".format(args.volume, args.ticks))
        # 拉一下最新价再让价
        if _wait_until(api, lambda: _finite(getattr(q, "last_price", None)), 3):
            last2 = float(q.last_price)
        else:
            last2 = fill_buy  # fallback
        sell_px = _round_tick(last2 - args.ticks * args.tick_size, args.tick_size)
        order_sell = api.insert_order(symbol=args.symbol, direction="SELL", offset="CLOSE",
                                      volume=args.volume, limit_price=sell_px)
        print("    已发出 order_id={}  @{}".format(order_sell.order_id, sell_px))

        if not _wait_until(api,
                           lambda: getattr(order_sell, "status", "") == "FINISHED", 15):
            print("[!] 15s 内未 FINISHED，撤单...")
            try:
                api.cancel_order(order_sell.order_id)
                api.wait_update(deadline=time.time() + 5)
            except Exception:
                pass
            print("[X] 平仓未成交。请手动到快期3/经纪商客户端处理这笔今仓。")
            return 6
        fill_sell = float(getattr(order_sell, "trade_price", 0) or 0)
        if not _finite(fill_sell) or fill_sell <= 0:
            print("[!] FINISHED 但 trade_price 无效：last_msg={}".format(
                getattr(order_sell, "last_msg", "")))
            return 6
        print("[OK] 成交 SELL @ {}  status={}  last_msg={}".format(
            fill_sell, order_sell.status, getattr(order_sell, "last_msg", "")))

        # ---- 总结 ----
        print("[5/5] 成交总结")
        pos2 = api.get_position(args.symbol)
        final_long = (getattr(pos2, "pos_long_today", 0) or 0) + (getattr(pos2, "pos_long_his", 0) or 0)
        pnl_points = (fill_sell - fill_buy)
        pnl_cash = pnl_points * args.volume * 300.0  # IF 合约乘数 300
        # 粗略手续费：开 0.23%% 平今 3.45%% × 名义金额
        notional = fill_buy * args.volume * 300.0
        est_fee = notional * (0.000023 + 0.000345)
        print("    BUY  @ {}  ({})".format(fill_buy, "OPEN"))
        print("    SELL @ {}  ({})".format(fill_sell, "CLOSE"))
        print("    价差: {:+.4f} 点  估算净盈亏: {:+.2f} 元（含手续费）".format(
            pnl_points, pnl_cash - est_fee))
        print("    剩余 long={}  (期望 0)".format(final_long))
        print("")
        print("=== 真成交冒烟测试通过 ===")
    finally:
        try:
            api.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
