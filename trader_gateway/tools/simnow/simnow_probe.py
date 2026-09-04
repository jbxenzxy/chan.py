#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
M2a · SimNow 通道探针
=====================
在写完整 SimNow broker 之前，先用这个脚本验证四件事：

    [1] SimNow 能不能连上（登录）
    [2] 账户资金是否正常（确认是仿真账号、有余额）
    [3] IF 主连 KQ.m@CFFEX.IF 当前映射到哪个具体合约（underlying_symbol）
    [4] 下单通道通不通（可选，默认只查不下单）

跑通这四步，M2b 的完整 broker 就只是把这段代码包进 Broker 接口。

凭据（全部从环境变量读，不硬编码、不落盘明文）
    SN_ACCOUNT / SN_PASSWORD    SimNow 仿真账号
    TQ_ACCOUNT / TQ_PASSWORD    天勤（快期）账号 —— 与 chan.py 行情同源

用法
    # 只验证连接 + 资金 + 主连映射（不碰下单，最安全）
    python simnow_probe.py --symbol "KQ.m@CFFEX.IF"

    # 追加：下一笔远离市价的限价测试单，观察后立即撤单（验证下单/撤单通道）
    python simnow_probe.py --symbol "KQ.m@CFFEX.IF" --place-order

Windows 设置环境变量（CMD）：
    set SN_ACCOUNT=你的simnow账号&& set SN_PASSWORD=你的simnow密码&& ^
    set TQ_ACCOUNT=你的天勤账号&& set TQ_PASSWORD=你的天勤密码&& ^
    python simnow_probe.py --symbol "KQ.m@CFFEX.IF"

前置：pip install tqsdk
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time


def _finite(v) -> bool:
    """v 是否为有限的数值（排除 None 与 NaN）。tqsdk 未填充字段用 NaN 表示。"""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return not math.isnan(v)
    return False


def _load_creds():
    """从环境变量读四组凭据，缺失即报错退出。"""
    keys = {
        "SN_ACCOUNT": "SimNow 账号",
        "SN_PASSWORD": "SimNow 密码",
        "TQ_ACCOUNT": "天勤账号",
        "TQ_PASSWORD": "天勤密码",
    }
    missing = [k for k, _ in keys.items() if not os.environ.get(k, "").strip()]
    if missing:
        print("[X] 缺少环境变量: {}".format(", ".join(missing)))
        print("    说明: {}".format("; ".join("{}={}".format(k, v) for k, v in keys.items())))
        print("")
        print("Windows CMD 示例:")
        print("  set SN_ACCOUNT=xxx&& set SN_PASSWORD=xxx&& ^")
        print("  set TQ_ACCOUNT=xxx&& set TQ_PASSWORD=xxx&& ^")
        print("  python simnow_probe.py --symbol \"KQ.m@CFFEX.IF\"")
        return None
    return (os.environ["SN_ACCOUNT"].strip(), os.environ["SN_PASSWORD"].strip(),
            os.environ["TQ_ACCOUNT"].strip(), os.environ["TQ_PASSWORD"].strip())


def _fmt_account(acct) -> str:
    """把 get_account() 的 dict 格式化为可读字符串。"""
    keys = ["balance", "available", "margin", "frozen_margin", "position_profit",
            "close_profit", "commission", "risk_ratio"]
    parts = []
    for k in keys:
        if k in acct:
            v = acct[k]
            if _finite(v):
                parts.append("{}={:.2f}".format(k, v))
            else:
                parts.append("{}={}".format(k, v))
    return "  ".join(parts)


def _wait_until(api, predicate, timeout_s=30.0, step_s=0.5):
    """反复 wait_update 直到 predicate 为真或超时。返回是否命中。

    key: wait_update 必须带 deadline（秒级 unix 时间戳），
    None 表示无限等待——数据已就绪且无新推送时会永久卡死。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        api.wait_update(deadline=deadline)
        if predicate():
            return True
        time.sleep(step_s)
    return False


def step1_login(api_factory):
    print("[1/4] 登录 SimNow（天勤中转）...")
    try:
        api = api_factory()
    except Exception as e:
        print("[X] 登录失败: {}: {}".format(type(e).__name__, e))
        print("    常见原因：① 账号/密码错 ② 非交易时段且非 7x24 环境 ③ 网络不通")
        print("    请核对环境变量，并确认 SimNow 环境当前开放。")
        return None
    print("[OK] 登录成功")
    return api


def step2_account(api):
    print("[2/4] 查询账户资金 ...")
    try:
        acct = api.get_account()
        # 等账户数据真正填充（balance 变为有效数值），10s 超时，防无限阻塞
        hit = _wait_until(api, lambda: _finite(acct.get("balance")), timeout_s=10.0)
        if not hit:
            print("[!] 10s 内账户数据未填充（可能网络慢或非交易时段），打印当前值：")
        print("    {}".format(_fmt_account(acct)))
        bal = acct.get("balance", 0)
        if _finite(bal) and bal <= 0:
            print("[!] 注意：权益为 0 或负数，可能账户未初始化或已穿仓。")
    except Exception as e:
        print("[!] 查询资金异常: {}: {}".format(type(e).__name__, e))
    return True


def step3_underlying(api, symbol):
    print("[3/4] 查询主连映射 {} ...".format(symbol))
    try:
        q = api.get_quote(symbol)
        hit = _wait_until(api, lambda: bool(getattr(q, "underlying_symbol", None)))
        if not hit:
            print("[X] 30s 内未取到 underlying_symbol（行情未到或合约无效）")
            print("    last_price =", getattr(q, "last_price", None))
            return None
        print("[OK] 主连 {} -> 主力合约 {}".format(symbol, q.underlying_symbol))
        print("    last_price =", getattr(q, "last_price", None),
              "  datetime =", getattr(q, "datetime", None))
        return q
    except Exception as e:
        print("[!] 查询主连异常: {}: {}".format(type(e).__name__, e))
        return None


def step4_place_order(api, underlying):
    print("[4/4] 下单通道测试（远离市价的限价单，观察后立即撤）...")
    if not underlying:
        print("[X] 跳过：未取得主力合约，无法下单")
        return
    try:
        q = api.get_quote(underlying)
        hit = _wait_until(api, lambda: _finite(getattr(q, "last_price", None)),
                          timeout_s=15.0)
        last = getattr(q, "last_price", None) if hit else None
        if not _finite(last):
            print("[!] 15s 内无最新价，改用固定参考价 4000")
            last = 4000.0
        # 远离市价的买单：不会成交，但能验证下单/撤单通道
        limit = round(last * 0.95, 1)
        order = api.insert_order(symbol=underlying, direction="BUY",
                                 offset="OPEN", volume=1, limit_price=limit)
        print("    已发出测试单 order_id={} {} BUY OPEN 1手 @{}".format(
            order.order_id, underlying, limit))
        api.wait_update(deadline=time.time() + 15)
        status = getattr(order, "status", "?")
        print("    订单状态:", status)
        # 撤单
        try:
            api.cancel_order(order.order_id)
            api.wait_update(deadline=time.time() + 15)
            print("    已撤单（cancel_order 成功）")
        except Exception as ce:
            print("[!] 撤单异常（若已成交则无法撤，需手动平仓）: {}".format(ce))
    except Exception as e:
        print("[X] 下单/撤单异常: {}: {}".format(type(e).__name__, e))
        print("    可能：① 该合约不可交易 ② SimNow 不支持此下单方式 ③ 时段限制")


def main() -> int:
    ap = argparse.ArgumentParser(description="SimNow 通道探针（M2a）")
    ap.add_argument("--symbol", default="KQ.m@CFFEX.IF", help="主连代码")
    ap.add_argument("--place-order", action="store_true",
                    help="追加下单/撤单通道测试（默认不碰下单）")
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

    def api_factory():
        return TqApi(TqAccount("simnow", sn_user, sn_pass),
                     auth=TqAuth(tq_user, tq_pass))

    api = step1_login(api_factory)
    if api is None:
        return 1
    try:
        step2_account(api)
        q = step3_underlying(api, args.symbol)
        if args.place_order:
            step4_place_order(api, q.underlying_symbol if q else None)
        print("")
        print("=== 探针完成 ===")
        print("若四步全 OK，可进入 M2b：把这段连接逻辑包进 tg/brokers/simnow.py")
    finally:
        try:
            api.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
