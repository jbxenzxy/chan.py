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

    B. 离场方式判定改为按日期（规则 ⑸ 落地，原按 origin 联动）
       `Engine._exit_intent(pos, today)`：
         · SOFT_EXIT_LOCK             → (UNLOCK, pos.side)     防御分支
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
    ExitPlan, OrderIntent, Position, Side,
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
    """走**公开** submit（新签名已无 _submit_unlock，且 _submit_close 不再收 entry_date）。"""
    a = MockApi(pos=MockPos())
    bb = make_broker(api=a, params=_FAST, spec=spec)
    bb.submit(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close",
              entry_date=entry_date, is_exit=True)
    return a.inserted[0]["offset"]


print("\n[1] CLOSE 报文恒为平昨 CLOSE（今/昨仓分支已删除，CLOSETODAY 不可达）")
check("昨仓离场 → CLOSE", _first_close_offset(_YESTERDAY), "CLOSE")
check("今仓离场 → CLOSE（规则 ⑸：今仓离场走反向 OPEN 软离场，不会走到 CLOSE）",
      _first_close_offset(_TODAY), "CLOSE")
check("entry_date 缺失 → CLOSE", _first_close_offset(""), "CLOSE")
check("close_today_first=False 时同样 CLOSE",
      _first_close_offset(_TODAY, InstrumentSpec(close_today_first=False)), "CLOSE")

print("\n[2] CLOSE 拆锁实发报文（旧 UNLOCK 路径；P0-1：CLOSEYESTERDAY → CLOSE）")
api = MockApi(pos=MockPos())
b3 = make_broker(api=api, params=_FAST)
b3.submit(OrderIntent.CLOSE, Side.LONG, 1, 4550.0, "k-close-unlock")
check("CLOSE 拆锁报单已发出", len(api.inserted) >= 1, True)
check("offset = CLOSE（平昨，且 tqsdk 接受）",
      api.inserted[0]["offset"], "CLOSE")
check("direction = SELL（平多）", api.inserted[0]["direction"], "SELL")
check("advanced = FOK", api.inserted[0]["advanced"], "FOK")

print("\n[3] 白名单总校验：实际发出的 offset 必须都被 tqsdk 接受")
_emitted = [api.inserted[0]["offset"], _first_close_offset(_YESTERDAY),
            _first_close_offset(_TODAY), _first_close_offset("")]
check("所有实发 offset ∈ ('OPEN','CLOSE','CLOSETODAY')",
      all(x in _TQSDK_OFFSETS for x in _emitted), True)
check("INTENT_TO_OFFSET 全表 ∈ 白名单",
      all(v in _TQSDK_OFFSETS for v in INTENT_TO_OFFSET.values()), True)
check("INTENT_TO_OFFSET 恰为二值 {OPEN:'OPEN', CLOSE:'CLOSE'}（四值已收敛）",
      {k.value: v for k, v in INTENT_TO_OFFSET.items()},
      {"open": "OPEN", "close": "CLOSE"})
check("CLOSE 映射 = CLOSE（不再是 CLOSEANY / CLOSETODAY / CLOSEYESTERDAY）",
      INTENT_TO_OFFSET[OrderIntent.CLOSE], "CLOSE")


print("\n[4] 场景 Y 关键回归：跨日单（昨仓）离场不得发平今")
check("昨仓离场 ≠ CLOSETODAY", _first_close_offset(_YESTERDAY) != "CLOSETODAY", True)


# ---------------- [5] 规则 ⑸：_decide_exit 按 entry_date vs today 判定 ----------------
import shutil                      # noqa: E402
import tempfile                    # noqa: E402

from Trading.Broker.DryRun import DryRunBroker            # noqa: E402
from Trading.Config import TradingConfig                  # noqa: E402
from Trading.Infra.EventLog import EventLog               # noqa: E402
from Trading.Infra.Store import Store                     # noqa: E402
from Trading.Strategy.Entry import DefaultEntryPolicy     # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy       # noqa: E402


def mk_pos(side=Side.LONG, entry_date=_TODAY, vol=2):
    """新口径：Position **不再有** origin / lock_pair_id（Phase 1-4 已删）。"""
    return Position(symbol="CFFEX.IF2609", side=side, volume=vol,
                    entry_price=4000.0, entry_at="", entry_bar_ts=0,
                    signal_key="k", open_order_id="o",
                    exit_plan=ExitPlan(name="x", stop_price=3990.0),
                    entry_date=entry_date)


def mk_engine(tmp, book):
    """建引擎后在**构造之后**灌簿 —— 避免触发 G2「有敞口无 run → 拒绝启动」。"""
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    cfg.risk.max_volume = 2
    cfg.exit_params.use_atr = False
    eng = TradingEngine(
        cfg, DryRunBroker(InstrumentSpec(), {"sim_equity": 1_000_000.0}),
        DefaultEntryPolicy({}), LayeredExitPolicy(),
        Store(os.path.join(tmp, "state_p24.db")),
        EventLog(os.path.join(tmp, "events_p24.jsonl"), echo=False, echo_kinds=None))
    for p in book:
        eng.positions.add(p)
    return eng


def with_tmp(fn):
    d = tempfile.mkdtemp(prefix="p24_")
    try:
        return fn(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


print("\n[5] 规则 ⑸：_decide_exit 按 entry_date vs today 判定"
      "（④ 今仓反向 OPEN 软离场 / ⑤ 跨日 CLOSE 硬离场）")


def _today_case(tmp):
    eng = mk_engine(tmp, [mk_pos(entry_date=_TODAY)])
    act = eng._decide_exit(None)
    check("今日单 → intent=OPEN（反向开仓软离场）", act.intent, OrderIntent.OPEN)
    check("今日单 → 反向 side（LONG 仓 → 开 SHORT）", act.side, Side.SHORT)
    check("今日单 → is_exit=True（软离场必须追价）", act.is_exit, True)
    check("今日单 → 转移 ④", act.transition, 4)
    check("今日单 → 量 = 净敞口 2", act.volume, 2)


def _yesterday_case(tmp):
    p_old = mk_pos(entry_date="2026-09-01", vol=3)
    p_new = mk_pos(entry_date="2026-09-01", vol=2)
    eng = mk_engine(tmp, [p_old, p_new])
    act = eng._decide_exit(None)
    check("跨日单 → intent=CLOSE（平昨）", act.intent, OrderIntent.CLOSE)
    check("跨日单 → 原 side（LONG 仓 → 卖平）", act.side, Side.LONG)
    check("跨日单 → is_exit=True（硬离场必须追价）", act.is_exit, True)
    check("跨日单 → 转移 ⑤", act.transition, 5)
    check("CLOSE 目标 = 同向 FIFO 最早一笔", act.target, p_old)
    check("CLOSE 量 = min(净敞口 5, 目标 3) = 3", act.volume, 3)


def _locked_case(tmp):
    eng = mk_engine(tmp, [mk_pos(Side.LONG, "2026-09-01", 2),
                          mk_pos(Side.SHORT, "2026-09-01", 2)])
    check("净敞口 0（锁仓态）→ _decide_exit 返回 None", eng._decide_exit(None), None)
    check("同时 account_state = locked", eng.account_state().value, "locked")


def _cross_day_case(tmp):
    # 关键改造点：当日开仓、隔日才离场 → 必须走 ⑤ CLOSE（旧实现误走 LOCK）
    eng = mk_engine(tmp, [mk_pos(entry_date=_YESTERDAY)])
    act = eng._decide_exit(None)
    check("当日开仓、隔日才离场 → ⑤ CLOSE 平昨（旧实现误走 LOCK）",
          (act.intent, act.transition), (OrderIntent.CLOSE, 5))


def _latest_wins_case(tmp):
    # 判定"今日/跨日"只看簿内**最近一笔**（entry_bar_seq 最大者）
    eng = mk_engine(tmp, [mk_pos(entry_date=_YESTERDAY, vol=2),
                          mk_pos(entry_date=_TODAY, vol=2)])
    act = eng._decide_exit(None)
    check("簿内最近一笔是今仓 → ④ 软离场（与旧 _exit_intent 的'只看该笔'同口径）",
          act.transition, 4)
    check("但仍按净敞口取量（2+2=4）", act.volume, 4)


with_tmp(_today_case)
with_tmp(_yesterday_case)
with_tmp(_locked_case)
with_tmp(_cross_day_case)
with_tmp(_latest_wins_case)

check("旧方法已不存在（_exit_intent / _close_positions）",
      (hasattr(TradingEngine, "_exit_intent"),
       hasattr(TradingEngine, "_close_positions")), (False, False))

print("\n" + "=" * 60)
print("P24 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
