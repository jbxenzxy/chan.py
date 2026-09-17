# -*- coding: utf-8 -*-
"""
P37 FOK 全撤 / 部分成交契约测试（D12 盲区①，2026-09-13）
==================================================================
为什么必须有这个文件（不是"p23 已经覆盖了"）
------------------------------------------------
`SimNow` **不撮合**：只要价格与对手价合适就**直接全部成交**，既不会部分成交、
也不会因盘口深度不足而全撤。所以下面这三条分支在 SimNow 上**根本跑不到**，
"SimNow 全绿"对它们**零证据力**：

  ① 追价循环（`_submit_close` / `_submit_open(is_exit=True)` 的
     close_max_chase 轮 + 每轮重取对手价重定价）；
  ② `_finalize` 的全撤分支（`status != FINISHED` → rejected）；
  ③ **部分成交**处理（`trade_records` 手数 < 委托手数 → 必须判未成交）。

实盘又是另一套规则：中金所 FOK 要求**盘口挂单量 ≥ 委托手数**，否则整笔全撤，
所以 ①②③ 恰恰是实盘天天发生的事。契约测试是唯一能在本地钉死它们的办法
（盲区①的对策原文："用 FakeApi 契约测试覆盖"）。

本文件钉死的断言
------------------------------------------------
  [1] 配置前置：close_max_chase / chase_interval / order_advanced 取值；
  [2] ★ 离场单（CLOSE, is_exit=True）连续全撤 → 报单轮数 = close_max_chase，
      且**每轮都按当期对手价重定价**（不是拿的价格反复报）；
  [3] 入场单（OPEN, is_exit=False）全撤 → **只报 1 次**，不追价（转移 ①②③ 语义）；
  [4] ★ **部分成交必须判未成交**：`status=FINISHED` + `volume_left=0` 但
      `trade_records` 累计只有 1 手（委托 2 手）→ P6 权威层必须拦下；
      OPEN / CLOSE 两条路径各测一遍，且整轮追价结束后**任何一笔都不得是 filled**；
  [5] 正例对照：`trade_records` 手数 == 委托手数 → 判 filled（证明 [4] 有判别力，
      不是"恒 rejected"的假绿）。

  另：`_traded_volume_from_records` 是 P6 的判据来源，顺带钉一下它的累加语义。

跑法：python Trading/Test/test_p37_fok_fullcancel_partial.py
"""
from __future__ import annotations

import itertools
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.SimNow import (  # noqa: E402
    SimNowBroker, _traded_volume_from_records)
from Trading.Config import DEFAULT_CONFIG  # noqa: E402
from Trading.Infra.Instrument import Instrument  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.Records import OrderIntent, Side  # noqa: E402


_PASS = 0
_FIX = 0


def check(name, got, want) -> None:
    global _PASS, _FIX
    ok = got == want
    if isinstance(got, float) and isinstance(want, float):
        ok = abs(got - want) < 1e-9
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FIX += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail="") -> None:
    check(name + ("（{}）".format(detail) if detail else ""), bool(cond), True)


# ════════════════════════════════════════════════════════════════
# mock 层：够小、够准 —— 只实现 SimNow.py 真正会调的那几个接口
# ════════════════════════════════════════════════════════════════
class MQuote:
    def __init__(self, ask, bid):
        self.ask_price1 = ask
        self.bid_price1 = bid


class MPos:
    """LONG 侧 2 手**昨仓**：CLOSE 在本系统里恒作用于跨日仓（不变量 6），
    `_submit_close` 的下单前判据是"昨仓 ≥ volume"（D12/p38）。"""

    def __init__(self, lh=2, lt=0):
        self.pos_long_today = lt
        self.pos_long_his = lh
        self.pos_short_today = 0
        self.pos_short_his = 0


class MRawOrder:
    """tqsdk Order 的最小面。

    `mode="cancel"`  —— ALIVE 永不终态 → 走 watchdog 超时撤单 → rejected（模拟全撤）；
    `mode="partial"` —— FINISHED + volume_left=0，但 CTP 明细只有 `rec_volume` 手；
    `mode="fill"`    —— FINISHED + volume_left=0 + 明细足额。
    """

    def __init__(self, oid, volume, mode, rec_volume, price):
        self.order_id = oid
        if mode == "cancel":
            self.status = "ALIVE"
            self.volume_left = volume
            self.trade_records = {}
            self.last_msg = "已撤单"
        else:
            self.status = "FINISHED"
            self.volume_left = 0
            self.trade_records = {"t-{}".format(oid): {
                "volume": rec_volume, "price": price}}
            self.last_msg = ("全部成交报单" if mode == "fill"
                             else "部分成交")
        self.trade_price = price


class FakeApi:
    """每次 `insert_order` 后把盘口**整体下移 `step` 点**（模拟行情在两次报单之间走掉）。

    这样"每轮重新取对手价"才有可观测的痕迹：若实现偷懒复用的价格，
    开始的 limit_price 就不会跟着盘口走 —— 断言立刻红。
    """

    def __init__(self, ask=4565.0, bid=4560.0, step=10.0, mode="cancel",
                 rec_volume=0, his=2):
        self.ask, self.bid = ask, bid
        self.step = step
        self.mode = mode
        self.rec_volume = rec_volume
        self.his = his
        self.inserted = []
        self.cancelled = []
        self.quote_calls = 0

    # ---- tqsdk 面 ----
    def get_quote(self, symbol):
        self.quote_calls += 1
        return MQuote(self.ask, self.bid)

    def wait_update(self, deadline=None):
        return True

    def get_position(self, symbol=None):
        return MPos(lh=self.his)

    def get_order(self, oid):
        return None

    def cancel_order(self, oid):
        self.cancelled.append(oid)

    def insert_order(self, symbol, direction, offset, volume, limit_price,
                     advanced=None):
        self.inserted.append({"symbol": symbol, "direction": direction,
                              "offset": offset, "volume": volume,
                              "limit_price": limit_price, "advanced": advanced})
        order = MRawOrder("raw-{}".format(len(self.inserted)), volume,
                          self.mode,
                          self.rec_volume if self.rec_volume else volume,
                          limit_price)
        # 行情在下一轮报单前走掉（不利方向 = 价格下移）
        self.ask -= self.step
        self.bid -= self.step
        return order


def make_broker(api, over=None):
    """`object.__new__` 绕过真实 `_connect`（不碰网络、不需要凭据）。"""
    b = object.__new__(SimNowBroker)
    b.state = Instrument(None, _IF)
    params = dict(DEFAULT_CONFIG["broker_params"])
    params["channel"] = dict(DEFAULT_CONFIG["broker_params"]["channel"])
    for k, v in (over or {}).items():
        if k == "channel":
            params["channel"].update(v)
        else:
            params[k] = v
    b.params = params
    b._api = api
    b._trade_symbol = b.state.trade_symbol
    b._seq = itertools.count(1)
    b.orders = []
    b._sig_orders = {}
    b._conn_error = None
    return b


# 快参数：3 轮追价、无间隔、watchdog 0.05s、持仓/增量校验窗口 0.05s
_FAST = {"close_max_chase": 3, "chase_interval": 0.0,
         "fill_timeout_open": 0.05, "fill_timeout_close": 0.05,
         "channel": {"position_ok_timeout": 0.05, "verify_delta_timeout": 0.05,
                     "baseline_settle_wait": 0.0}}
_CAP = _FAST["close_max_chase"]


print("\n[1] 配置前置（单一事实源 Trading/Config.py）")
# ════════════════════════════════════════════════════════════════
bp = DEFAULT_CONFIG["broker_params"]
check("close_max_chase 默认 20 轮（实盘追价上限）", bp["close_max_chase"], 20)
check("chase_interval 默认 1.0 秒（防报撤单频率超限 / FOK 撤单计数爆量）",
      bp["chase_interval"], 1.0)
check("order_advanced 默认 FOK（A2：值在 spec，不在 Broker 正文）",
      Instrument(None, _IF).order_advanced, "FOK")
check("本用例实际使用的追价上限（快参数）", _CAP, 3)


print("\n[2] ★ 离场单连续全撤 → 轮数 = close_max_chase，且每轮按当期对手价重定价")
# ════════════════════════════════════════════════════════════════
api = FakeApi(mode="cancel", ask=4565.0, bid=4560.0, step=10.0)
b = make_broker(api, _FAST)
o = b.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-chase", is_exit=True)
check("报单轮数 = close_max_chase", len(api.inserted), _CAP)
check("全部未成交 → rejected", o.status, "rejected")
check("每轮都是 FOK", sorted({m["advanced"] for m in api.inserted}), ["FOK"])
check("每轮都是平昨报文 offset=CLOSE / 方向 SELL",
      sorted({(m["offset"], m["direction"]) for m in api.inserted}),
      [("CLOSE", "SELL")])
# 平多=卖出 → 用 bid 作对手价，超价 1.0 点（5 tick × 0.2）后向下取整
_limits = [m["limit_price"] for m in api.inserted]
check("第 1 轮限价 = 首帧 bid(4560) - 1.0 = 4559.0", _limits[0], 4559.0)
check("每轮限价随行情下移 10 点（证明重新取了对手价，不是复用首轮价）",
      _limits, [4559.0 - 10.0 * i for i in range(_CAP)])
check_true("确实每轮都取了一次行情", api.quote_calls >= _CAP, api.quote_calls)
check_true("全撤路径走的是 watchdog 撤单（超时兜底，非交易所撤单）",
           len(api.cancelled) >= 1, len(api.cancelled))


print("\n[3] 入场单全撤 → 只报 1 次，不追价（转移 ①②③ 语义）")
# ════════════════════════════════════════════════════════════════
api2 = FakeApi(mode="cancel")
b2 = make_broker(api2, _FAST)
o2 = b2.submit(OrderIntent.OPEN, Side.LONG, 1, 4550.0, "k-entry", is_exit=False)
check("入场全撤只报 1 次单", len(api2.inserted), 1)
check("入场全撤 → rejected（本笔作废，等下一信号）", o2.status, "rejected")
check("入场报文 offset=OPEN", api2.inserted[0]["offset"], "OPEN")


print("\n[4] ★ 部分成交必须判未成交（P6 权威层：trade_records 手数不足）")
# ════════════════════════════════════════════════════════════════
print("  -- 4a 离场 CLOSE：委托 2 手，CTP 明细只有 1 手 --")
api3 = FakeApi(mode="partial", rec_volume=1)
b3 = make_broker(api3, _FAST)
o3 = b3.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-part-close", is_exit=True)
check("部分成交后仍继续追价（不是终态 filled）", len(api3.inserted), _CAP)
check("★ 追完仍是 rejected（不得判为已成交）", o3.status, "rejected")
check("最后一笔成交价为 None", o3.filled_price, None)
check_true("reject_reason 明确指向 P6 权威层",
           "P6" in str(o3.meta.get("reject_reason") or ""),
           str(o3.meta.get("reject_reason"))[:80])
check_true("全程没有任何一笔被判 filled",
           all(x.status != "filled" for x in b3.orders),
           [x.status for x in b3.orders])

print("  -- 4b 入场 OPEN：委托 2 手，CTP 明细只有 1 手 --")
api4 = FakeApi(mode="partial", rec_volume=1)
b4 = make_broker(api4, _FAST)
o4 = b4.submit(OrderIntent.OPEN, Side.LONG, 2, 4550.0, "k-part-open", is_exit=False)
check("入场部分成交 → 只报 1 次", len(api4.inserted), 1)
check("★ 判 rejected（不得判为已成交）", o4.status, "rejected")


print("\n[5] 正例对照：明细足额 → filled（证明 [4] 不是「恒 rejected」的假绿）")
# ════════════════════════════════════════════════════════════════
api5 = FakeApi(mode="fill")
b5 = make_broker(api5, _FAST)
o5 = b5.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-ok", is_exit=True)
check("足额成交 → filled，且第 1 轮即停", o5.status, "filled")
check("只报了 1 次单（成交即返回，不空跑剩余轮次）", len(api5.inserted), 1)
check("成交价 = 限价（mock 明细价=限价）", o5.filled_price,
      api5.inserted[0]["limit_price"])


print("\n[6] `_traded_volume_from_records` 累加语义（P6 判据来源）")
# ════════════════════════════════════════════════════════════════


class _R:
    def __init__(self, recs):
        self.trade_records = recs


check("空 records → 0", _traded_volume_from_records(_R({})), 0)
check("无 trade_records 属性 → 0", _traded_volume_from_records(object()), 0)
check("两条明细累加", _traded_volume_from_records(
    _R({"a": {"volume": 2, "price": 1.0}, "b": {"volume": 3, "price": 1.0}})), 5)


print("\n" + "=" * 60)
print("P37 结果: {} 通过 / {} 失败".format(_PASS, _FIX))
print("=" * 60)
sys.exit(1 if _FIX else 0)
