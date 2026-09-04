# -*- coding: utf-8 -*-
"""
收盘后 SimNow 可交易性探测
=========================
目的：回答「现在收盘了，SimNow 回归还能不能跑？」

探测四件事：
  ① 能否登录（SimNow 日终结算窗口会拒登录）
  ② 行情是否还在推送 / 合约状态（是否处于交易时段）
  ③ 非交易时段下报单会被 CTP 接受还是拒绝（关键）
  ④ 成交明细 trade_records 在非交易时段的表现

安全设计：
  - 报单价 = max(跌停价+5, 最新价-50)，即「远离市价但仍在涨跌停板内」，
    保证即便 CTP 接受也绝不会成交（买单挂在当前市价下方 50 点）
  - 探测结束后立即撤单并验证撤单成功，不留过夜委托
用法：
  python probe_after_close.py
"""
from __future__ import annotations

import os
import time

SYMBOL = "CFFEX.IF2609"


def main() -> int:
    from tqsdk import TqApi, TqAuth, TqAccount

    sn_acct = os.environ.get("SN_ACCOUNT", "")
    sn_pwd = os.environ.get("SN_PASSWORD", "")
    tq_user = os.environ.get("TQ_ACCOUNT", "")
    tq_pwd = os.environ.get("TQ_PASSWORD", "")
    if not all([sn_acct, sn_pwd, tq_user, tq_pwd]):
        print("✗ 缺少环境变量 SN_ACCOUNT/SN_PASSWORD/TQ_ACCOUNT/TQ_PASSWORD")
        return 2

    print("=" * 68)
    print("收盘后 SimNow 可交易性探测   ", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 68)

    # ---------- ① 登录 ----------
    print("\n[1] 登录 SimNow")
    t0 = time.time()
    try:
        api = TqApi(TqAccount("simnow", sn_acct, sn_pwd),
                    auth=TqAuth(tq_user, tq_pwd))
    except Exception as e:
        print("  ✗ 登录失败: {}: {}".format(type(e).__name__, e))
        return 3
    print("  ✓ 登录成功  用时 {:.1f}s".format(time.time() - t0))

    try:
        # ---------- ② 行情 / 合约状态 ----------
        print("\n[2] 行情与合约状态  {}".format(SYMBOL))
        q = api.get_quote(SYMBOL)
        deadline = time.time() + 10
        while time.time() < deadline and not q.datetime:
            api.wait_update(deadline=deadline)
        last = float(q.last_price) if q.last_price == q.last_price else 0.0
        print("  合约名称   : {}".format(getattr(q, "instrument_name", "?")))
        print("  行情时间   : {}".format(q.datetime))
        print("  最新价     : {}".format(q.last_price))
        print("  涨停/跌停  : {} / {}".format(q.upper_limit, q.lower_limit))
        print("  成交量     : {}".format(getattr(q, "volume", "?")))
        print("  昨结算     : {}".format(q.pre_settlement))
        print("  持仓量     : {}".format(getattr(q, "open_interest", "?")))

        # ---------- ③ 账户与持仓 ----------
        print("\n[3] 账户与持仓")
        acc = api.get_account()
        pos = api.get_position(SYMBOL)
        api.wait_update(deadline=time.time() + 3)
        print("  balance={:>14,.2f}  available={:>14,.2f}".format(
            acc.balance, acc.available))
        long_t = int(getattr(pos, "pos_long_today", 0) or 0)
        long_h = int(getattr(pos, "pos_long_his", 0) or 0)
        short_t = int(getattr(pos, "pos_short_today", 0) or 0)
        short_h = int(getattr(pos, "pos_short_his", 0) or 0)
        print("  持仓: 多今={} 多昨={} 空今={} 空昨={}".format(
            long_t, long_h, short_t, short_h))
        if (long_t + long_h + short_t + short_h) != 0:
            print("  ⚠️ 账户有非零持仓，跑回归前请先平掉")

        # ---------- ④ 报单探测 ----------
        print("\n[4] 非交易时段报单探测（远离市价，绝不会成交）")
        lower = float(q.lower_limit) if q.lower_limit == q.lower_limit else 0.0
        safe_price = max(lower + 5.0, last - 50.0)
        safe_price = round(safe_price, 1)
        print("  报单: BUY OPEN 1 手 @ {}   (最新价 {}, 偏离 {:.1f} 点)".format(
            safe_price, last, last - safe_price))

        order = api.insert_order(SYMBOL, "BUY", "OPEN", 1, safe_price)
        deadline = time.time() + 8
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            if order.status in ("FINISHED",):
                break
        print("  --- CTP 回报 ---")
        print("  order_id      : {}".format(order.order_id))
        print("  status        : {}".format(order.status))
        print("  volume_left   : {}".format(order.volume_left))
        print("  last_msg      : {!r}".format(order.last_msg))
        print("  error_id      : {}".format(getattr(order, "error_id", "?")))
        print("  trade_price   : {}".format(getattr(order, "trade_price", None)))
        recs = getattr(order, "trade_records", None) or {}
        print("  trade_records : {}  (成交量合计 {})".format(
            len(recs), sum(int(r.get("volume", 0)) for r in recs.values())))

        # ---------- ⑤ 撤单并检查 ----------
        print("\n[5] 撤单")
        if order.status == "ALIVE":
            api.cancel_order(order)
            deadline = time.time() + 8
            while time.time() < deadline and order.status == "ALIVE":
                api.wait_update(deadline=deadline)
            print("  撤单后 status : {}  last_msg={!r}".format(
                order.status, order.last_msg))
        else:
            print("  委托未存活（status={}），无需撤单".format(order.status))

        # ---------- ⑥ 收尾复核 ----------
        print("\n[6] 收尾复核：账户是否仍干净")
        api.wait_update(deadline=time.time() + 3)
        pos2 = api.get_position(SYMBOL)
        api.wait_update(deadline=time.time() + 2)
        tot2 = (int(getattr(pos2, "pos_long_today", 0) or 0)
                + int(getattr(pos2, "pos_long_his", 0) or 0))
        print("  多头总持仓 = {}".format(tot2))
        if tot2 != 0:
            print("  ⚠️ 探测产生了持仓，请平掉")
        else:
            print("  ✓ 无持仓，探测未污染账户")

        # ---------- 结论 ----------
        print("\n" + "=" * 68)
        print("结论")
        print("=" * 68)
        accepted = order.status == "ALIVE" or (
            order.status == "FINISHED" and not order.last_msg)
        if accepted:
            print("  CTP 【接受】了报单（status=ALIVE 或 FINISHED 且无报错）")
            print("  → 收盘后仍可报单，回归能跑，但不可能成交（无人撮合）")
        else:
            print("  CTP 【拒绝】了报单：{!r}".format(order.last_msg))
            print("  → 收盘后无法报单，回归跑出来必然 7 笔全 rejected")
            print("    这样的结果无法区分「P6 生效」与「市场关门」，价值有限")
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
