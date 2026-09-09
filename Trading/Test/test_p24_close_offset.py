# -*- coding: utf-8 -*-
"""
P24 平仓 offset 今/昨仓判定 + tqsdk 白名单 单元测试（2026-09-10 新增）
=====================================================================
背景（两个 P0 缺陷的回归测试）

    场景 Y（锁仓态跨日解锁入场 → 昨仓硬离场）在实盘上整条链路是断的，根因都在
    CTP 报文 offset 上：

    1. UNLOCK 用 offset="CLOSEYESTERDAY"
       → 不在 tqsdk 白名单。tqsdk 3.10.2 三处硬校验
         （api.py:1353 / lib/utils.py:39 / scenario/tqscenario.py:445）
         `offset not in ("OPEN", "CLOSE", "CLOSETODAY")` → 直接 raise，
         被 SimNow 的 try/except 吞成 rejected → 跨日解锁 100% 失败，
         账户永久锁死在锁仓态。
       修正：改 "CLOSE"（tqsdk 文档：上期所/上期能源平昨用 CLOSE，
         **其他交易所（含中金所）直接用 CLOSE**）。

    2. CLOSE 无条件发 "CLOSETODAY"（spec.close_today_first=True）
       → UNLOCK_FIRST 腿必然是昨仓（它只在跨日解锁时由 _upgrade_lock_pair 产生），
         昨仓发平今：中金所无今仓 → CTP 拒单 → 引擎连续失败达 close_max_streak
         触发 phantom 清仓（真实持仓还在却从引擎簿消失 → 账实不符）；
         若账户恰有同向今仓 → 平错腿；即便成交也按 0.0345% 平今费率计费。
       修正：按被平腿 entry_date 判定（昨仓→CLOSE，今仓→CLOSETODAY）。

    3. 附带：close_today_first=False 时的 "CLOSEANY" 同样不在白名单 → 改 "CLOSE"。

本测试用 mock api 对象验证，不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p24_close_offset.py
"""
from __future__ import annotations

import datetime as _dt
import itertools
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(5):
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading.Broker.Base import INTENT_TO_OFFSET  # noqa: E402
    from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
    from Trading.Config import DEFAULT_CONFIG  # noqa: E402
    from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
    from Trading.Infra.Types import OrderIntent, Side  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    raise SystemExit(2)

_PASS = 0
_FAIL = 0

# tqsdk 硬性白名单（三处校验一致）
_TQSDK_OFFSETS = ("OPEN", "CLOSE", "CLOSETODAY")

_TODAY = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
_YESTERDAY = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date()
              - _dt.timedelta(days=1)).isoformat()


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


# ---------------- mock 对象（同 P23，最小可用） ----------------
class MockQuote:
    def __init__(self, ask=None, bid=None):
        self.ask_price1 = ask
        self.bid_price1 = bid


class MockPos:
    pos_long_today = 2
    pos_long_his = 2
    pos_short_today = 0
    pos_short_his = 0


class MockRawOrder:
    def __init__(self, oid):
        self.order_id = oid
        self.status = "ALIVE"
        self.volume_left = 1
        self.trade_price = None
        self.trade_records = {}
        self.last_msg = "Submitted"


class MockApi:
    def __init__(self, ask=4565.0, bid=4560.0, pos=None):
        self._quote = MockQuote(ask=ask, bid=bid)
        self._pos = pos
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
        return MockRawOrder("raw-{}".format(len(self.inserted)))

    def cancel_order(self, oid):
        self.cancelled.append(oid)


def make_broker(api=None, params=None, spec=None):
    b = object.__new__(SimNowBroker)
    b.spec = spec or InstrumentSpec()
    b.params = dict(DEFAULT_CONFIG["broker_params"], **(params or {}))
    b._api = api
    b._trade_symbol = b.spec.trade_symbol
    b._seq = itertools.count(1)
    b.orders = []
    b._sig_orders = {}
    b._conn_error = None
    return b


_FAST = {"fill_timeout_open": 0.05, "fill_timeout_close": 0.05,
         "close_max_chase": 1, "chase_interval": 0.01}

print("\n[1] _close_offset 今/昨仓判定（spec.close_today_first=True）")
b = make_broker(api=MockApi(pos=MockPos()), params=_FAST)
check("昨仓（entry_date={}）→ CLOSE（平昨）".format(_YESTERDAY),
      b._close_offset(_YESTERDAY), "CLOSE")
check("今仓（entry_date={}）→ CLOSETODAY（平今）".format(_TODAY),
      b._close_offset(_TODAY), "CLOSETODAY")
check("entry_date 缺失 → CLOSE（保守按昨仓，与引擎 '' < today 口径一致）",
      b._close_offset(""), "CLOSE")

print("\n[2] close_today_first=False → 一律 CLOSE（原 CLOSEANY 不在白名单）")
spec_false = InstrumentSpec(close_today_first=False)
b2 = make_broker(api=MockApi(pos=MockPos()), params=_FAST, spec=spec_false)
check("今仓 + close_today_first=False → CLOSE", b2._close_offset(_TODAY), "CLOSE")
check("昨仓 + close_today_first=False → CLOSE", b2._close_offset(_YESTERDAY), "CLOSE")

print("\n[3] UNLOCK 实发报文（P0-1：CLOSEYESTERDAY → CLOSE）")
api = MockApi(pos=MockPos())
b3 = make_broker(api=api, params=_FAST)
b3._submit_unlock(OrderIntent.UNLOCK, Side.LONG, 1, 4550.0, "k-unlock", "")
check("UNLOCK 报单已发出", len(api.inserted) >= 1, True)
check("UNLOCK offset = CLOSE（平昨，且 tqsdk 接受）",
      api.inserted[0]["offset"], "CLOSE")
check("UNLOCK direction = SELL（平多）", api.inserted[0]["direction"], "SELL")
check("UNLOCK advanced = FOK", api.inserted[0]["advanced"], "FOK")

print("\n[4] CLOSE 实发报文：昨仓 vs 今仓（P0-2）")


def _first_close_offset(entry_date: str) -> str:
    a = MockApi(pos=MockPos())
    bb = make_broker(api=a, params=_FAST)
    bb._submit_close(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close", "",
                     entry_date)
    return a.inserted[0]["offset"]


check("昨仓离场 → CLOSE（中金所不会因'无今仓'拒单）",
      _first_close_offset(_YESTERDAY), "CLOSE")
check("今仓离场 → CLOSETODAY（保持原平今行为）",
      _first_close_offset(_TODAY), "CLOSETODAY")
check("无 entry_date 保守 → CLOSE", _first_close_offset(""), "CLOSE")

print("\n[5] 白名单总校验：实际发出的 offset 必须都被 tqsdk 接受")
_emitted = [api.inserted[0]["offset"], _first_close_offset(_YESTERDAY),
            _first_close_offset(_TODAY), _first_close_offset("")]
check("所有实发 offset ∈ ('OPEN','CLOSE','CLOSETODAY')",
      all(x in _TQSDK_OFFSETS for x in _emitted), True)
check("INTENT_TO_OFFSET 全表 ∈ 白名单",
      all(v in _TQSDK_OFFSETS for v in INTENT_TO_OFFSET.values()), True)
check("UNLOCK 映射不再是 CLOSEYESTERDAY",
      INTENT_TO_OFFSET[OrderIntent.UNLOCK] != "CLOSEYESTERDAY", True)

print("\n[6] 场景 Y 关键回归：UNLOCK_FIRST 腿（昨仓）离场不得发平今")
# UNLOCK_FIRST 腿只在跨日解锁后产生 → entry_date 必然 < 今日
check("UNLOCK_FIRST 腿（entry_date=昨）离场 = CLOSE ≠ CLOSETODAY",
      _first_close_offset(_YESTERDAY) != "CLOSETODAY", True)

print("\n" + "=" * 60)
print("P24 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
