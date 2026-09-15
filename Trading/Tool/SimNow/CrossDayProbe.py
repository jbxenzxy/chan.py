#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
跨日仓构造 + 平昨 探针（D12 · §5.8.3 盲区②，2026-09-13）
=========================================================
为什么需要这个工具
------------------------------------------------
本系统的 `CLOSE` 恒作用于**跨日仓**（不变量 6）→ 中金所下恒为**平昨**。
但"昨仓真的存在、真的能被平掉"这件事，**在 SimNow 第二套（7x24）环境下永远测不出来**：
官方说明该环境"不提供结算等其它服务"，账户 / 钱 / 仓与上一交易日保持一致
—— 也就是说那里**没有日切**，永远造不出真正的昨仓。

所以平昨路径只能在**第一套环境**（服务时间与生产一致、有日终结算）或实盘小资金上验。
本工具就是把"构造跨日仓 → 次日验证平昨"这件事做成一条可重复执行的命令，
免得每次靠手工下单 + 肉眼看仓位数字。

它验的是**生产代码的真实路径**（`SimNowBroker.submit` 里的 `_wait_position_ok` /
`_verify_yesterday_delta`），不是另写一遍判据 —— 否则验的是探针、不是系统。
唯一直接调用内部函数的地方是读取今/昨分解（`_position_split`），
刻意复用同一实现，避免"探针和 broker 对同一个字段有两种读法"。

三个阶段（跨两个交易日）
------------------------------------------------
    --phase arm      第 1 日盘中：开 1 手（今天建的仓 = **今仓**），然后**收工**
    --phase check    第 2 日盘中（日切之后）：断言昨仓 ≥ 1，再发 1 手 CLOSE，
                     并断言成交后 **昨仓下降、今仓不变**（这才是平昨的证据）
    --phase cleanup  任何时点：把多空两侧所有仓平掉（收尾，别留敞口）

    --self-test      离线自检：不连柜台、不需要凭据，用假 api 验证两条判据
                     （只有今仓必须被拦下 / 昨仓够才放行、成交后昨仓降今仓不动）

⚠️ 使用前必读
    · 必须跑在 **SimNow 第一套环境**（7x24 环境无日切，`--phase check` 必然报"昨仓=0"，
      这不是 bug，是盲区②本身）。
    · `arm` 与 `check` 之间必须**真的有日终结算**（跨过一个交易日），
      同一天连着跑 check 一定失败。
    · 凭据走环境变量：SN_ACCOUNT / SN_PASSWORD / TQ_ACCOUNT / TQ_PASSWORD。

用法
    python Trading/Tool/SimNow/CrossDayProbe.py --self-test
    python Trading/Tool/SimNow/CrossDayProbe.py --phase arm     --symbol CFFEX.IF2609
    #   …… 隔一个交易日 ……
    python Trading/Tool/SimNow/CrossDayProbe.py --phase check   --symbol CFFEX.IF2609
    python Trading/Tool/SimNow/CrossDayProbe.py --phase cleanup --symbol CFFEX.IF2609
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time


def _locate_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "Trading")) and \
                os.path.isfile(os.path.join(d, "Trading", "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_root()
if not _ROOT:
    print("[X] 找不到仓库根目录（应含 Trading/__init__.py）。")
    raise SystemExit(2)
sys.path.insert(0, _ROOT)

from Trading.Broker.SimNow import (  # noqa: E402
    SimNowBroker, _position_split, _verify_yesterday_delta)
from Trading.Config import DEFAULT_CONFIG, TradingConfig  # noqa: E402
from Trading.Infra.InstrumentSpec import Instrument, InstrumentConfig as InstrumentSpec_cls  # noqa: E402
from Trading.Infra.Records import OrderIntent, Side  # noqa: E402


_PASS = 0
_FAIL = 0


def check(name, ok, detail="") -> None:
    global _PASS, _FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + (("  " + str(detail)) if detail else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _creds():
    keys = ("SN_ACCOUNT", "SN_PASSWORD", "TQ_ACCOUNT", "TQ_PASSWORD")
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if missing:
        print("[X] 缺少环境变量: {}".format(", ".join(missing)))
        return None
    return tuple(os.environ[k].strip() for k in keys)


def _make_broker(symbol: str):
    """走**生产同一条构造路径**（TradingConfig + build_broker），不是手搓 broker。"""
    cfg = TradingConfig()
    # P-B（2026-09-15）：部署配置 InstrumentConfig（frozen）+ 唯一运行时对象
    #   Instrument —— 换 trade_symbol 用「重建配置」表达（不能就地改 frozen 字段）。
    _d = cfg.instrument.model_dump()
    _d["trade_symbol"] = symbol or cfg.instrument.trade_symbol
    spec = Instrument(InstrumentSpec_cls(**_d), cfg.product_profile)
    params = cfg.broker_params.model_dump()
    from Trading.Broker.Base import build_broker
    return build_broker("simnow", spec, params), spec


def _split(b, side: Side):
    """(今仓, 昨仓) —— 复用 broker 内部的同一实现，避免两套读法。"""
    return _position_split(b._api, b._trade_symbol,
                           "LONG" if side is Side.LONG else "SHORT")


def _show(b, tag: str) -> None:
    lt, lh = _split(b, Side.LONG)
    st, sh = _split(b, Side.SHORT)
    print("    [{}] LONG 今={} 昨={}   SHORT 今={} 昨={}".format(tag, lt, lh, st, sh))


# ════════════════════════════════════════════════════════════════
# 离线自检：不需要柜台
# ════════════════════════════════════════════════════════════════
class _P:
    def __init__(self, lt=0, lh=0):
        self.pos_long_today, self.pos_long_his = lt, lh
        self.pos_short_today, self.pos_short_his = 0, 0


class _Q:
    ask_price1, bid_price1 = 4565.0, 4560.0


class _FakeApi:
    """`spawn(lt, lh, volume)` 决定"成交后持仓怎么变"，用来复现平昨 / 平今两种柜台行为。"""

    def __init__(self, lt=0, lh=2, spawn=None):
        self.lt, self.lh, self.spawn = lt, lh, spawn
        self.inserted = []

    def wait_update(self, deadline=None):
        # 让 _verify_yesterday_delta 的多帧轮询能收敛：`spawn` 立即生效
        if self.spawn is not None and self.inserted:
            self.lt, self.lh = self.spawn(self.lt, self.lh)
            self.spawn = None
        return True

    def get_position(self, symbol=None):
        return _P(self.lt, self.lh)

    def get_quote(self, symbol):
        return _Q()

    def get_order(self, oid):
        return None

    def cancel_order(self, oid):
        pass

    def insert_order(self, symbol, direction, offset, volume, limit_price,
                     advanced=None):
        self.inserted.append({"direction": direction, "offset": offset,
                              "volume": volume, "limit_price": limit_price,
                              "advanced": advanced})

        class _O:
            order_id = "raw-{}".format(len(self.inserted))
            status = "FINISHED"
            volume_left = 0
            trade_price = limit_price
            last_msg = "全部成交报单"

            def __init__(self):
                self.trade_records = {"t": {"volume": volume,
                                            "price": limit_price}}
        return _O()


def _fake_broker(api):
    b = object.__new__(SimNowBroker)
    b.spec = Instrument()  # P-B：唯一运行时对象（默认 IF 档案）
    params = dict(DEFAULT_CONFIG["broker_params"])
    params["channel"] = dict(DEFAULT_CONFIG["broker_params"]["channel"])
    params["channel"].update({"position_ok_timeout": 0.2,
                              "verify_delta_timeout": 0.1,
                              "baseline_settle_wait": 0.0})
    params.update({"close_max_chase": 1, "chase_interval": 0.0,
                   "fill_timeout_open": 0.05, "fill_timeout_close": 0.05})
    b.params = params
    b._api = api
    b._trade_symbol = b.spec.trade_symbol
    b._seq = itertools.count(1)
    b.orders, b._sig_orders, b._conn_error = [], {}, None
    return b


def self_test() -> int:
    print("=== 离线自检：平昨判据（不连柜台）===")

    print("[1] 只有今仓时，CLOSE 必须被拦下（一个委托都不发）")
    api = _FakeApi(lt=2, lh=0)
    b = _fake_broker(api)
    o = b.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "selftest-today", is_exit=True)
    check("未发出委托", len(api.inserted) == 0, len(api.inserted))
    check("判 rejected", o.status == "rejected", o.status)
    check("拒单类别 = position", o.meta.get("reject_class") == "position",
          o.meta.get("reject_class"))

    print("[2] 昨仓够 → 放行，且成交后昨仓降、今仓不变")
    api2 = _FakeApi(lt=0, lh=2, spawn=lambda lt, lh: (lt, lh - 2))
    b2 = _fake_broker(api2)
    o2 = b2.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "selftest-his", is_exit=True)
    check("发出 1 笔委托", len(api2.inserted) == 1, len(api2.inserted))
    check("报文 offset=CLOSE（平昨）", api2.inserted[0]["offset"] == "CLOSE",
          api2.inserted[0]["offset"])
    check("判 filled", o2.status == "filled", o2.status)
    check("成交后校验：昨仓降、今仓不动",
          _verify_yesterday_delta(api2, b2._trade_symbol, "LONG",
                                  0, 2, 2, timeout_s=0.2) is True)

    print("[3] 柜台实际平掉今仓 → 校验必须判 False（成交事实不推翻）")
    api3 = _FakeApi(lt=2, lh=2, spawn=lambda lt, lh: (lt - 2, lh))
    b3 = _fake_broker(api3)
    o3 = b3.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "selftest-jin", is_exit=True)
    check("仍判 filled（权威判据是 P6 成交明细）", o3.status == "filled", o3.status)
    check("平昨校验 = False（today 被扣）",
          _verify_yesterday_delta(api3, b3._trade_symbol, "LONG",
                                  2, 2, 2, timeout_s=0.1) is False)

    print("\n自检结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    return 1 if _FAIL else 0


# ════════════════════════════════════════════════════════════════
# 真柜台三阶段
# ════════════════════════════════════════════════════════════════
def phase_arm(b, spec) -> int:
    print("=== phase arm：第 1 日盘中，开 1 手（今天建的仓 = 今仓）===")
    _show(b, "开仓前")
    ref = 0.0
    o = b.submit(OrderIntent.OPEN, Side.LONG, 1, ref, "crossday-arm",
                 note="跨日仓探针-开仓")
    print("    open -> status={} filled_price={}".format(o.status, o.filled_price))
    check("开仓成交", o.status == "filled",
          "{} / {}".format(o.status, o.meta.get("reject_reason")))
    _show(b, "开仓后")
    lt, lh = _split(b, Side.LONG)
    check("今天建的仓应落在**今仓**（pos_long_today ≥ 1）", lt >= 1,
          "今={} 昨={}".format(lt, lh))
    print("\n>>> 现在收工，**隔一个交易日**（等日终结算）后再跑：")
    print("    python Trading/Tool/SimNow/CrossDayProbe.py --phase check")
    return 1 if _FAIL else 0


def phase_check(b, spec) -> int:
    print("=== phase check：第 2 日盘中，验证平昨真的减昨仓 ===")
    _show(b, "平仓前")
    lt0, lh0 = _split(b, Side.LONG)
    if lh0 < 1:
        print("  ! 昨仓 = {} —— 说明没有发生日切。".format(lh0))
        print("    · 若用的是 SimNow **7x24** 环境：这是已知盲区（无结算），本项测不了；")
        print("    · 若用的是第一套但同一天跑的 arm：请等到下一个交易日再跑 check。")
        _FAIL_LOCAL = 1
        print("\n探针结果: 无法验证（昨仓不存在）")
        return _FAIL_LOCAL if lh0 < 1 else 0

    check("日切生效：昨仓 ≥ 1（pos_long_his）", lh0 >= 1, "昨={}".format(lh0))
    o = b.submit(OrderIntent.CLOSE, Side.LONG, 1, 0.0, "crossday-check",
                 note="跨日仓探针-平昨", is_exit=True)
    print("    close -> status={} filled_price={}".format(o.status, o.filled_price))
    check("平昨成交", o.status == "filled",
          "{} / {}".format(o.status, o.meta.get("reject_reason")))
    _show(b, "平仓后")
    lt1, lh1 = _split(b, Side.LONG)
    check("★ 昨仓下降（pos_long_his 减少 1）", lh1 == lh0 - 1,
          "昨 {} → {}".format(lh0, lh1))
    check("★ 今仓不变（pos_long_today 未动）", lt1 == lt0,
          "今 {} → {}".format(lt0, lt1))
    if lh1 != lh0 - 1 or lt1 != lt0:
        print("  ! 柜台可能把 CLOSE 撮合到了今仓（中金所默认先平今）→ 会走平今费率，")
        print("    请核对结算单，并把样本反馈回来重新校准判据。")
    return 1 if _FAIL else 0


def phase_cleanup(b, spec) -> int:
    print("=== phase cleanup：平掉多空两侧所有仓（收尾）===")
    _show(b, "清理前")
    for side in (Side.LONG, Side.SHORT):
        lt, lh = _split(b, side)
        total = lt + lh
        while total > 0:
            # 昨仓走 CLOSE（平昨）；今仓走反向 OPEN（本系统不实现平今，见 A4）
            if lh > 0:
                o = b.submit(OrderIntent.CLOSE, side, lh, 0.0,
                             "cleanup-close-{}".format(side), is_exit=True)
            else:
                o = b.submit(OrderIntent.OPEN,
                             Side.SHORT if side is Side.LONG else Side.LONG,
                             lt, 0.0, "cleanup-open-{}".format(side), is_exit=True)
            print("    {} -> status={}".format(side, o.status))
            if o.status != "filled":
                print("    ! 未成交，停止清理，请人工处理：{}".format(
                    o.meta.get("reject_reason")))
                break
            lt, lh = _split(b, side)
            total = lt + lh
    _show(b, "清理后")
    lt, lh = _split(b, Side.LONG)
    st, sh = _split(b, Side.SHORT)
    check("多空两侧均已清零", lt + lh + st + sh == 0,
          "LONG {} SHORT {}".format(lt + lh, st + sh))
    return 1 if _FAIL else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="跨日仓构造 + 平昨 探针（D12/§5.8.3 盲区②）")
    ap.add_argument("--self-test", action="store_true",
                    help="离线自检（不连柜台、不需凭据）")
    ap.add_argument("--phase", choices=("arm", "check", "cleanup"),
                    help="真柜台阶段：arm（第1日开仓）/ check（第2日验平昨）/ cleanup（清仓）")
    ap.add_argument("--symbol", default="", help="具体合约（默认取配置 instrument.trade_symbol）")
    args = ap.parse_args()

    if args.self_test or not args.phase:
        if not args.self_test and not args.phase:
            print("未指定 --phase，仅跑离线自检。真柜台用法见 -h\n")
        return self_test()

    if _creds() is None:
        return 2
    b, spec = _make_broker(args.symbol)
    if getattr(b, "_api", None) is None:
        print("[X] 连接失败：{}".format(getattr(b, "_conn_error", None)))
        return 3
    print("[OK] 已连接，合约 = {}".format(b._trade_symbol))
    try:
        return {"arm": phase_arm, "check": phase_check,
                "cleanup": phase_cleanup}[args.phase](b, spec)
    finally:
        try:
            b.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
