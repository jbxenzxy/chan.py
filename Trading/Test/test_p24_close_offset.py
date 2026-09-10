# -*- coding: utf-8 -*-
"""
P24 平仓 offset 定稿 + 离场方式按日期判定 单元测试（2026-09-10 修订）
=====================================================================
背景（两个 P0 缺陷的回归测试 + 规则 ⑸ 改造）

    A. CTP 报文 offset 的两个 P0 缺陷（已修）

    1. UNLOCK 用 offset="CLOSEYESTERDAY"
       → 不在 tqsdk 白名单。tqsdk 3.10.2 三处硬校验
         （api.py:1353 / lib/utils.py:39 / scenario/tqscenario.py:445）
         `offset not in ("OPEN", "CLOSE", "CLOSETODAY")` → 直接 raise，
         被 SimNow 的 try/except 吞成 rejected → 跨日解锁 100% 失败，
         账户永久锁死在锁仓态。
       修正：改 "CLOSE"（tqsdk 文档：上期所/上期能源平昨用 CLOSE，
         **其他交易所（含中金所）直接用 CLOSE**）。

    2. CLOSE 无条件发 "CLOSETODAY"（spec.close_today_first=True）
       → 昨仓发平今：中金所无今仓 → CTP 拒单 → 引擎连续失败达 close_max_streak
         触发 phantom 清仓（真实持仓还在却从引擎簿消失 → 账实不符）；
         若账户恰有同向今仓 → 平错持仓；即便成交也按 0.0345% 平今费率计费。
       修正（2026-09-10 用户拍板）：**删除今/昨仓分支**（原 `_close_offset`）。
         规则 ⑸ 定稿为"今日单离场 = LOCK 反向开仓（offset=OPEN）"、
         "跨日单离场 = CLOSE 平昨（offset=CLOSE）"，故 CLOSE 恒为平昨，
         CLOSETODAY 在代码中**不可达** —— 留着只会误导后来人以为今日单可能走平今。

    B. 离场方式判定改为按日期（规则 ⑸ 落地，原按 entry_mode 联动）
       `Engine._exit_intent(pos, today)`：
         · LOCKED             → (UNLOCK, pos.side)     防御分支
         · entry_date < today → (CLOSE,   pos.side)    跨日单：硬离场平昨
         · entry_date >= today→ (LOCK,    反向 side)   今日单：软离场锁仓
         · today 缺省/为空    → (LOCK,    反向 side)   无法判日期 → 选永不拒单一侧

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
    from Trading.Engine.Engine import TradingEngine  # noqa: E402
    from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
    from Trading.Infra.Types import (  # noqa: E402
        EntryMode, ExitPlan, OrderIntent, Position, Side,
    )
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    raise SystemExit(2)

_PASS = 0
_FAIL = 0

# tqsdk 硬性白名单（三处校验一致）
_TQSDK_OFFSETS = ("OPEN", "CLOSE", "CLOSETODAY")

_CN = _dt.timezone(_dt.timedelta(hours=8))
_TODAY = _dt.datetime.now(_CN).date().isoformat()
_YESTERDAY = (_dt.datetime.now(_CN).date() - _dt.timedelta(days=1)).isoformat()


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


def _first_close_offset(entry_date: str, spec=None) -> str:
    a = MockApi(pos=MockPos())
    bb = make_broker(api=a, params=_FAST, spec=spec)
    bb._submit_close(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close", "",
                     entry_date)
    return a.inserted[0]["offset"]


print("\n[1] CLOSE 报文恒为平昨 CLOSE（今/昨仓分支已删除，CLOSETODAY 不可达）")
check("昨仓离场 → CLOSE", _first_close_offset(_YESTERDAY), "CLOSE")
check("今仓离场 → CLOSE（规则 ⑸：今日单离场走 LOCK，不会走到 CLOSE）",
      _first_close_offset(_TODAY), "CLOSE")
check("entry_date 缺失 → CLOSE", _first_close_offset(""), "CLOSE")
check("close_today_first=False 时同样 CLOSE",
      _first_close_offset(_TODAY, InstrumentSpec(close_today_first=False)), "CLOSE")

print("\n[2] UNLOCK 实发报文（P0-1：CLOSEYESTERDAY → CLOSE）")
api = MockApi(pos=MockPos())
b3 = make_broker(api=api, params=_FAST)
b3._submit_unlock(OrderIntent.UNLOCK, Side.LONG, 1, 4550.0, "k-unlock", "")
check("UNLOCK 报单已发出", len(api.inserted) >= 1, True)
check("UNLOCK offset = CLOSE（平昨，且 tqsdk 接受）",
      api.inserted[0]["offset"], "CLOSE")
check("UNLOCK direction = SELL（平多）", api.inserted[0]["direction"], "SELL")
check("UNLOCK advanced = FOK", api.inserted[0]["advanced"], "FOK")

print("\n[3] 白名单总校验：实际发出的 offset 必须都被 tqsdk 接受")
_emitted = [api.inserted[0]["offset"], _first_close_offset(_YESTERDAY),
            _first_close_offset(_TODAY), _first_close_offset("")]
check("所有实发 offset ∈ ('OPEN','CLOSE','CLOSETODAY')",
      all(x in _TQSDK_OFFSETS for x in _emitted), True)
check("INTENT_TO_OFFSET 全表 ∈ 白名单",
      all(v in _TQSDK_OFFSETS for v in INTENT_TO_OFFSET.values()), True)
check("UNLOCK 映射不再是 CLOSEYESTERDAY",
      INTENT_TO_OFFSET[OrderIntent.UNLOCK] != "CLOSEYESTERDAY", True)
check("CLOSE 映射 = CLOSE（不再是 CLOSEANY / CLOSETODAY）",
      INTENT_TO_OFFSET[OrderIntent.CLOSE], "CLOSE")


print("\n[4] 场景 Y 关键回归：跨日单（昨仓）离场不得发平今")
check("昨仓离场 ≠ CLOSETODAY", _first_close_offset(_YESTERDAY) != "CLOSETODAY", True)


# ---------------- [5] 规则 ⑸：_exit_intent 按日期判定 ----------------
def mk_pos(side=Side.LONG, entry_mode=EntryMode.OPEN_FIRST, entry_date=_TODAY):
    return Position(symbol="CFFEX.IF2609", side=side, volume=2,
                    entry_price=4000.0, entry_at="", entry_bar_ts=0,
                    signal_key="k", open_order_id="o",
                    exit_plan=ExitPlan(name="x", stop_price=3990.0),
                    entry_mode=entry_mode, entry_date=entry_date)


print("\n[5] 规则 ⑸：_exit_intent 按 entry_date vs today 判定（不再看 entry_mode）")
i, s = TradingEngine._exit_intent(mk_pos(entry_date=_TODAY), _TODAY)
check("今日单 → LOCK（反向开仓锁仓）", i, OrderIntent.LOCK)
check("今日单 → 反向 side（LONG 仓 → 开 SHORT）", s, Side.SHORT)

i, s = TradingEngine._exit_intent(mk_pos(entry_date=_YESTERDAY), _TODAY)
check("跨日单 → CLOSE（平昨）", i, OrderIntent.CLOSE)
check("跨日单 → 原 side（LONG 仓 → 卖平）", s, Side.LONG)

# 关键改造点：OPEN_FIRST 但已跨日 → 必须走 CLOSE（旧实现会错走 LOCK）
i, _s = TradingEngine._exit_intent(
    mk_pos(entry_mode=EntryMode.OPEN_FIRST, entry_date=_YESTERDAY), _TODAY)
check("当日开仓、隔日才离场 → CLOSE 平昨（旧实现误走 LOCK）", i, OrderIntent.CLOSE)

# UNLOCK_FIRST 若 entry_date 恰好=今日（异常数据）→ 按今日单锁仓，不再无条件 CLOSE
i, _s = TradingEngine._exit_intent(
    mk_pos(entry_mode=EntryMode.UNLOCK_FIRST, entry_date=_TODAY), _TODAY)
check("UNLOCK_FIRST 但 entry_date=今日 → LOCK（按日期而非 entry_mode）",
      i, OrderIntent.LOCK)

i, s = TradingEngine._exit_intent(
    mk_pos(entry_mode=EntryMode.LOCKED, entry_date=_YESTERDAY), _TODAY)
check("LOCKED → UNLOCK + 原 side（防御分支）", (i, s), (OrderIntent.UNLOCK, Side.LONG))

i, _s = TradingEngine._exit_intent(mk_pos(entry_date=_YESTERDAY), "")
check("today 缺省 → LOCK（无法判日期时选永不拒单的一侧）", i, OrderIntent.LOCK)

print("\n" + "=" * 60)
print("P24 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
