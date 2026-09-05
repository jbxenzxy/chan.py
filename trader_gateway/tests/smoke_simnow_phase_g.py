# -*- coding: utf-8 -*-
"""
Phase G SimNow 真连冒烟脚本（2026-09-05）
==========================================
验证 Phase G 两条新链路在**真实 SimNow 环境**下不炸、行为符合预期：

    ① simnow.trade_confirmed(UNLOCK, signal_key)
       —— 假 signal_key（无索引）必须安全返回 False；真 signal_key 需在
          有真实 UNLOCK 报单后才能验证（本脚本默认只读，不做报单）。
    ② simnow.cancel_pending(signal_key)
       —— 假 signal_key 必须返回 0；真实在途单场景由引擎 G2 路径覆盖。

只读检查（默认）：连接 → real_position → trade_confirmed(假 key) →
cancel_pending(假 key) → stats()。**不发任何委托**。

用法（请在交易时段运行，SimNow 非交易时段可能连接失败/行情停推）：
    cd trader_gateway
    python tests/smoke_simnow_phase_g.py --config config.json

可选真单验证（自担风险，1 手开平一轮，验证 trade_confirmed 对真单的判定）：
    python tests/smoke_simnow_phase_g.py --config config.json --trade
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        for cand in (os.path.join(d, "tg"), os.path.join(d, "trader_gateway", "tg")):
            if os.path.isdir(cand) and os.path.isfile(os.path.join(cand, "__init__.py")):
                return os.path.dirname(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 tg 包")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

from tg.brokers import base as _brokers_base  # noqa: E402,F401  触发注册
from tg.brokers import simnow  # noqa: E402,F401  触发 SimNowBroker 注册
from tg.brokers.base import OrderIntent, build_broker  # noqa: E402
from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import Side  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, ok, detail=""):
    global _PASS, _FAIL
    print(("✓" if ok else "✗") + " " + name + (("  " + detail) if detail else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase G SimNow 真连冒烟（默认只读）")
    ap.add_argument("--config", default=os.path.join(_TG_ROOT, "config.json"),
                    help="config.json 路径（默认 trader_gateway/config.json）")
    ap.add_argument("--trade", action="store_true",
                    help="【有风险】追加 1 手真实开平，验证 trade_confirmed 对真单判定")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print("✗ 找不到配置文件: {}（SimNow 凭据在 broker_params 或环境变量）".format(args.config))
        return 2
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    inst = cfg.get("instrument") or {}
    spec = InstrumentSpec(
        signal_symbol=inst.get("signal_symbol", "KQ.m@CFFEX.IF"),
        trade_symbol=inst.get("trade_symbol", ""),
        price_tick=float(inst.get("price_tick", 0.2)),
    )
    params = cfg.get("broker_params") or {}
    if str(cfg.get("broker", "")) != "simnow":
        print("⚠ config.broker={}，本脚本用于 simnow 通道；继续尝试（凭据可能来自环境变量）"
              .format(cfg.get("broker")))

    print("── ① 连接 SimNow ──")
    b = build_broker("simnow", spec, params)
    ok_conn = getattr(b, "_api", None) is not None
    check("SimNow 连接（_api 就绪）", ok_conn,
          "" if ok_conn else "（连接失败：{}；注意 SimNow 非交易时段可能拒绝登录）"
          .format(getattr(b, "_conn_error", None)))
    if not ok_conn:
        print("P17 冒烟：{} 通过 / {} 失败（连接失败，后续步骤跳过）".format(_PASS, _FAIL))
        return 1

    print("── ② real_position（Phase F2 对账数据源）──")
    try:
        lp = b.real_position(Side.LONG)
        sp = b.real_position(Side.SHORT)
        check("real_position 查询不炸", True,
              "LONG={} SHORT={}（None=查询失败）".format(lp, sp))
    except Exception as e:
        check("real_position 查询不炸", False, "{}: {}".format(type(e).__name__, e))

    print("── ③ G1 trade_confirmed（假 signal_key，无索引 → 必须安全 False）──")
    try:
        v = b.trade_confirmed(OrderIntent.UNLOCK, "smoke-fake-key-不存在")
        check("trade_confirmed(假 key) == False", v is False,
              "got={!r}".format(v))
    except Exception as e:
        check("trade_confirmed(假 key) 不炸", False,
              "{}: {}".format(type(e).__name__, e))

    print("── ④ G2 cancel_pending（假 signal_key → 必须 0）──")
    try:
        n = b.cancel_pending("smoke-fake-key-不存在")
        check("cancel_pending(假 key) == 0", n == 0, "got={!r}".format(n))
    except Exception as e:
        check("cancel_pending(假 key) 不炸", False,
              "{}: {}".format(type(e).__name__, e))

    print("── ⑤ stats / 启动基线 ──")
    try:
        st = b.stats()
        ias = st.get("initial_account_state") or {}
        check("stats() 可读", True,
              "启动基线: {}".format(ias if ias else "0（无历史遗留仓，干净）"))
        if ias:
            print("⚠ 启动基线非 0：CTP 重发了上一轮未确认回报，存在历史遗留仓，"
                  "引擎 _restore 首拉对账（F2）会处理")
    except Exception as e:
        check("stats() 可读", False, "{}: {}".format(type(e).__name__, e))

    if args.trade:
        print("── ⑥【--trade】1 手真实开平 + trade_confirmed 真单判定 ──")
        try:
            from tg.brokers.base import OrderIntent
            # 记录 baseline，随后真开 1 手
            bl_l = b.real_position(Side.LONG) or 0
            bl_s = b.real_position(Side.SHORT) or 0
            print("  baseline: LONG={} SHORT={}".format(bl_l, bl_s))
            # 上期所/中金所 id 规则交给 broker；这里用一次 OPEN 一次 CLOSE
            o_open = b.submit(OrderIntent.OPEN, Side.LONG, 1, 0.0, "smoke-trade-open",
                              note="冒烟真单-开")
            print("  open  -> status={} filled_price={}".format(
                o_open.status, o_open.filled_price))
            key = o_open.signal_key
            confirmed_open = b.trade_confirmed(OrderIntent.OPEN, key)
            check("trade_confirmed(真单 open) 与 status 一致",
                  confirmed_open == (o_open.status == "filled"),
                  "confirmed={} status={}".format(confirmed_open, o_open.status))
            o_close = b.submit(OrderIntent.CLOSE, Side.LONG, 1, 0.0, "smoke-trade-close",
                               note="冒烟真单-平")
            print("  close -> status={} filled_price={}".format(
                o_close.status, o_close.filled_price))
            confirmed_close = b.trade_confirmed(OrderIntent.CLOSE, o_close.signal_key)
            check("trade_confirmed(真单 close) 与 status 一致",
                  confirmed_close == (o_close.status == "filled"),
                  "confirmed={} status={}".format(confirmed_close, o_close.status))
            lp2 = b.real_position(Side.LONG) or 0
            sp2 = b.real_position(Side.SHORT) or 0
            check("平仓后持仓回到 baseline", (lp2, sp2) == (bl_l, bl_s),
                  "now LONG={} SHORT={}".format(lp2, sp2))
        except Exception as e:
            check("--trade 流程", False, "{}: {}".format(type(e).__name__, e))

    try:
        b.close()
    except Exception:
        pass

    print("")
    print("P17 冒烟：{} 通过 / {} 失败".format(_PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
