# -*- coding: utf-8 -*-
"""
P23 全 FOK 报单 单元测试（2026-09-06 全量化改造 · 用户拍板版）
===============================================================
背景
    四类报单（OPEN 开仓 / UNLOCK 解锁 / LOCK 锁仓 / CLOSE 平仓）**全部恒定
    附加 advanced="FOK"**（限价立即全部成交否则全部撤销，交易所撮合引擎强制执行）：
      · 入场（OPEN/UNLOCK）：全撤 → 本笔作废（rejected），不追价，等下一信号
        （"入场没成功，最多不赚钱，但不会亏钱"）
      · 离场（LOCK/CLOSE）：FOK 全撤 → 隔 chase_interval 秒按最新对手价 ± overprice
        重新定价重报，最多 close_max_chase 轮；轮数用尽由引擎跨 K 线继续重试
      · 不再有 open_advanced / overprice_points_fok 配置（旧版可回退 GFD 的
        参数已删除，恒定 FOK，杜绝配置漂移回旧路径）
      · fill_timeout_open/close 退化为通道异常兜底 watchdog（断线防挂死）
      · overprice_points 合并为单一值 1.0（四类报单共用）

    交易所事实依据（已核实）：
      · 中金所（IF/IH/IC/IM）官方支持限价+FOK（CFFEX 交易概览 + 2026 版异常交易管理办法）
      · tqsdk 限价+FOK 仅拒郑商所期货（api.py L1463），中金所放行
      · 报文映射：time_condition=IOC, volume_condition=ALL
      · 中金所限价单笔上限 20 手（引擎 _open_position 有 over_exchange_limit 守卫）

本测试用 mock api 对象验证，不需要真实 tqsdk / 网络。
跑法：python tests/test_p23_fok.py
"""
from __future__ import annotations

import itertools
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
    from Trading.Infra.Config import DEFAULT_CONFIG  # noqa: E402
    from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
    from Trading.Infra.Types import OrderIntent, Side  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    raise SystemExit(2)

print("[import] 被测类来自: {}/tg/brokers/simnow.py（真实代码，非副本）".format(_TG_ROOT))

_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    if isinstance(got, float) and isinstance(want, float):
        ok = abs(got - want) < 1e-9
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


# ---------------- mock 对象 ----------------
class MockQuote:
    def __init__(self, ask=None, bid=None):
        self.ask_price1 = ask
        self.bid_price1 = bid


class MockPos:
    """满足 _wait_position_ok 的最小持仓视图（LONG 侧 2 手）。"""

    pos_long_today = 2
    pos_long_his = 0
    pos_short_today = 0
    pos_short_his = 0


class MockRawOrder:
    """模拟 tqsdk Order：默认永不 FINISHED → 走 watchdog 超时撤单 → rejected。"""

    def __init__(self, oid):
        self.order_id = oid
        self.status = "ALIVE"
        self.volume_left = 1
        self.trade_price = None
        self.trade_records = {}
        self.last_msg = "Submitted"


class FilledRawOrder:
    """模拟 FOK 全成：FINISHED + volume_left=0 + CTP 成交明细（P6 权威层）。"""

    def __init__(self, oid, volume, price):
        self.order_id = oid
        self.status = "FINISHED"
        self.volume_left = 0
        self.trade_price = price
        self.trade_records = {"t-{}".format(oid): {"volume": volume, "price": price}}
        self.last_msg = "全部成交报单"


class MockApi:
    """最小 api：记录 insert_order 的完整报文（含 advanced），默认返回 ALIVE 单。

    filled=True 时返回 FilledRawOrder（模拟 FOK 瞬间全成）。
    """

    def __init__(self, ask=4565.0, bid=4560.0, pos=None, filled=False,
                 fill_volume=1, fill_price=4560.0):
        self._quote = MockQuote(ask=ask, bid=bid)
        self._pos = pos
        self._filled = filled
        self._fill_volume = fill_volume
        self._fill_price = fill_price
        self.inserted = []
        self.cancelled = []

    def get_quote(self, symbol):
        return self._quote

    def wait_update(self, deadline=None):
        return True

    def get_position(self, sym=None):
        return self._pos

    def get_order(self, oid):
        return None

    def insert_order(self, symbol, direction, offset, volume, limit_price, advanced=None):
        self.inserted.append({"symbol": symbol, "direction": direction,
                              "offset": offset, "volume": volume,
                              "limit_price": limit_price, "advanced": advanced})
        if self._filled:
            return FilledRawOrder("raw-{}".format(len(self.inserted)),
                                  volume, self._fill_price)
        return MockRawOrder("raw-{}".format(len(self.inserted)))

    def cancel_order(self, oid):
        self.cancelled.append(oid)


def make_broker(api=None, params=None):
    """用 object.__new__ 绕过 __init__（避免真实 _connect 连 SimNow）。"""
    b = object.__new__(SimNowBroker)
    b.spec = InstrumentSpec()
    b.params = dict(params or {})
    b._api = api
    b._trade_symbol = b.spec.trade_symbol
    b._seq = itertools.count(1)
    b.orders = []
    b._sig_orders = {}
    b._conn_error = None
    return b


_FAST = {"fill_timeout_open": 0.05, "fill_timeout_close": 0.05,
         "close_max_chase": 2, "chase_interval": 0.01}

print("\n[1] 配置默认值（单一事实源 tg/config.py）")
bp = DEFAULT_CONFIG["broker_params"]
check("overprice_points 合并为 1.0（四类报单共用）", bp["overprice_points"], 1.0)
check("open_advanced 已删除（恒定 FOK，无 GFD 回退）", "open_advanced" in bp, False)
check("overprice_points_fok 已删除（参数合并）", "overprice_points_fok" in bp, False)
check("close_max_chase 默认 20 轮", bp["close_max_chase"], 20)
check("chase_interval 默认 1.0 秒（追价重报间隔）", bp["chase_interval"], 1.0)
check("fill_timeout_open 默认 5.0（watchdog 兜底）", bp["fill_timeout_open"], 5.0)
check("fill_timeout_close 默认 5.0（watchdog 兜底）", bp["fill_timeout_close"], 5.0)

print("\n[2] OPEN：恒定 FOK 报文 + 超价 1.0（ask + 1.0 = 4566.0），全撤不追价")
api = MockApi()
b = make_broker(api=api, params=_FAST)
o = b.submit(OrderIntent.OPEN, Side.LONG, 1, 4550.0, "k-open")
check("报单次数 = 1（入场全撤不追价）", len(api.inserted), 1)
msg = api.inserted[0]
check("advanced == FOK", msg["advanced"], "FOK")
check("direction BUY", msg["direction"], "BUY")
check("offset OPEN", msg["offset"], "OPEN")
check("限价 = ask + overprice_points(1.0) = 4566.0", msg["limit_price"], 4566.0)
check("watchdog 超时兜底撤单仍被调用（通道异常）", len(api.cancelled), 1)
check("Order 判定 rejected（未成交）", o.status, "rejected")

print("\n[3] OPEN 全成路径：FOK 瞬间全成 → filled，成交价取 CTP 明细加权均价")
api = MockApi(filled=True, fill_volume=1, fill_price=4561.2)
b = make_broker(api=api, params=_FAST)
o = b.submit(OrderIntent.OPEN, Side.LONG, 1, 4550.0, "k-open-ok")
check("报单次数 = 1", len(api.inserted), 1)
check("advanced == FOK", api.inserted[0]["advanced"], "FOK")
check("Order 判定 filled", o.status, "filled")
check("成交价 = CTP 成交明细价 4561.2（P6 权威层）", o.filled_price, 4561.2)

print("\n[4] UNLOCK：恒定 FOK + CLOSEYESTERDAY 报文，全撤不追价")
api = MockApi(pos=MockPos())
b = make_broker(api=api, params=_FAST)
o = b.submit(OrderIntent.UNLOCK, Side.LONG, 1, 4550.0, "k-unlock")
check("报单次数 = 1（入场语义不追价）", len(api.inserted), 1)
msg = api.inserted[0]
check("advanced == FOK", msg["advanced"], "FOK")
check("offset CLOSEYESTERDAY（平昨报文不变）", msg["offset"], "CLOSEYESTERDAY")
check("direction SELL（平多）", msg["direction"], "SELL")
check("限价 = bid - 1.0 = 4559.0（卖方向向下取整）", msg["limit_price"], 4559.0)

print("\n[5] LOCK：FOK 全撤立即重报追价，close_max_chase 轮全 FOK，offset=OPEN")
api = MockApi()
b = make_broker(api=api, params=_FAST)
b.submit(OrderIntent.LOCK, Side.SHORT, 1, 4550.0, "k-lock")
check("LOCK 追价报单次数 = close_max_chase", len(api.inserted), 2)
check("所有 LOCK 报单 advanced 均为 FOK",
      all(m["advanced"] == "FOK" for m in api.inserted), True)
check("LOCK 超价用 overprice_points 1.0（开空 bid-1.0=4559.0）",
      api.inserted[0]["limit_price"], 4559.0)
check("LOCK 报文 offset=OPEN（反向开仓）", api.inserted[0]["offset"], "OPEN")

print("\n[6] CLOSE：FOK 全撤立即重报追价，close_max_chase 轮全 FOK")
api = MockApi(pos=MockPos())
b = make_broker(api=api, params=_FAST)
b.submit(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close")
check("CLOSE 追价报单次数 = close_max_chase", len(api.inserted), 2)
check("所有 CLOSE 报单 advanced 均为 FOK",
      all(m["advanced"] == "FOK" for m in api.inserted), True)
check("CLOSE 报文 CLOSEANY/CLOSETODAY（非 CLOSEYESTERDAY）",
      api.inserted[0]["offset"] in ("CLOSEANY", "CLOSETODAY"), True)
check("CLOSE 追价限价 = bid - 1.0 = 4559.0（平多=卖方向）",
      api.inserted[0]["limit_price"], 4559.0)

print("\n[7] CLOSE 半路全成：第 1 轮全撤、第 2 轮全成即停（不空跑剩余轮数）")
api = MockApi(pos=MockPos())
b = make_broker(api=api, params=_FAST)
n_target = 2


class HalfFillApi(MockApi):
    """第 1 笔 ALIVE（全撤），第 2 笔起 FOK 全成。"""

    def insert_order(self, symbol, direction, offset, volume, limit_price, advanced=None):
        self.inserted.append({"symbol": symbol, "direction": direction,
                              "offset": offset, "volume": volume,
                              "limit_price": limit_price, "advanced": advanced})
        if len(self.inserted) >= n_target:
            return FilledRawOrder("raw-{}".format(len(self.inserted)),
                                  volume, 4558.8)
        return MockRawOrder("raw-{}".format(len(self.inserted)))


api = HalfFillApi(pos=MockPos())
b = make_broker(api=api, params=_FAST)
o = b.submit(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close-half")
check("第 2 轮全成后停止报单（总报单 = 2）", len(api.inserted), 2)
check("Order 判定 filled", o.status, "filled")
check("成交价 = 第 2 轮 CTP 明细价 4558.8", o.filled_price, 4558.8)

print("\n[8] 判别力（负向校验）：params 残留旧键不改变行为，恒定 FOK 不可配置回退")
api = MockApi()
b = make_broker(api=api, params=dict(_FAST, open_advanced="GFD",
                                     overprice_points_fok=0.6))
b.submit(OrderIntent.OPEN, Side.LONG, 1, 4550.0, "k-neg")
check("残留 open_advanced=GFD 仍是 FOK（恒定不可回退）",
      api.inserted[0]["advanced"], "FOK")
check("残留 overprice_points_fok 不生效（仍用 overprice_points=1.0）",
      api.inserted[0]["limit_price"], 4566.0)

print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
