# -*- coding: utf-8 -*-
"""
P38 CLOSE 命中昨仓 契约测试（D12 · §5.8.4 第 4 项 / §5.8.5，2026-09-13）
=======================================================================
不变量（为什么必须钉死）
------------------------------------------------
本系统的 `CLOSE` **恒作用于跨日仓** —— 这是不变量 6，由
`Engine._pre_trade_check` 的 `close_target_is_today` 在**引擎侧**拒绝"今仓 CLOSE"。
所以中金所下 CLOSE 报文恒为平昨（`offset="CLOSE"`，没有 CLOSETODAY 分支）。

但引擎侧那道闸门只保证"**引擎想让**它平昨"，**不保证柜台真的平昨**：

  中金所撮合规则是"同一合约同时有今仓和昨仓时**默认先平今**"。
  于是"今仓 2 手 + 昨仓 0 手"这种状态下，一个只看**总量**（`today + his`）的就绪判据
  会放行，而 CTP 要么把这笔单撮合成**平今**（中金所平今费率 0.0345% ≈ 平昨 15 倍），
  要么因昨仓不足直接拒单 —— 两种结果都与"引擎以为在平昨"的假设不相容。

`SimNow` 侧的旧实现恰恰只看了总量（`_position_total` = today + his），
本文件把"**只看昨仓**"这条判据钉死。

本文件钉死的断言
------------------------------------------------
  [1] ★ `_wait_position_ok` 的判据是**昨仓 ≥ volume**：
      · 昨仓够、今仓 0            → 通过；
      · **今仓够、昨仓 0 → 必须不通过**（本不变量存在的全部理由）；
      · 今 1 + 昨 1、要平 2 手     → 不通过（总量够但昨仓不够）；
      · 昨仓够 + 今仓也有          → 通过；
      · `require_yesterday=False`（旧口径，仅对照）→ 今仓够就通过。
  [2] 端到端：只有今仓时 `_submit_close` **一个委托都不发**，
      且拒单带 `reject_class="position"`（让引擎认得出来是"柜台无此仓"）。
  [3] ★ `_verify_yesterday_delta`（成交后半段）：
      · 昨仓降、今仓不动 → True；容忍 ±1 帧同步漂移；
      · **今仓下降 → False**（成交被打到今仓 = 平今费率，必须有人知道）；
      · 昨仓不动 → False（超时）；负基线 → False（不猜、不放行）。
  [4] 端到端：昨仓够 → filled；若柜台实际平掉的是今仓 → 只发 P4/P5 诊断告警，
      **不推翻已成交事实**（权威判据仍是 P6 `trade_records`）。
  [5] ★ D10 分类器新增第 4 类 `position`：错误码 30/50/51 与中文关键字
      （"平仓量超过持仓量" 等）都归它；且它属于"追价无用"（NO_CHASE）。

跑法：python Trading/Test/test_p38_close_hits_yesterday_only.py
"""
from __future__ import annotations

import itertools
import logging
import os
import sys
from contextlib import contextmanager

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

from Trading.Broker.Base import (  # noqa: E402
    NO_CHASE_REJECT_CLASSES, REJECT_FUNDS, REJECT_NOT_TRADABLE, REJECT_POSITION,
    REJECT_PRICE, classify_ctp_reject)
from Trading.Broker.SimNow import (  # noqa: E402
    SimNowBroker, _position_split, _position_total, _verify_yesterday_delta)
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


@contextmanager
def capture_warnings():
    """抓 `tg.brokers.simnow` 的 WARNING，用于断言"诊断告警有没有发"。"""
    msgs = []

    class _H(logging.Handler):
        def emit(self, rec):
            msgs.append(rec.getMessage())

    lg = logging.getLogger("tg.brokers.simnow")
    h = _H()
    old_level, old_prop = lg.level, lg.propagate
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    try:
        yield msgs
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
        lg.propagate = old_prop


# ════════════════════════════════════════════════════════════════
# mock 层
# ════════════════════════════════════════════════════════════════
class P:
    def __init__(self, lt=0, lh=0, st=0, sh=0):
        self.pos_long_today, self.pos_long_his = lt, lh
        self.pos_short_today, self.pos_short_his = st, sh


class Q:
    def __init__(self, ask=4565.0, bid=4560.0):
        self.ask_price1, self.bid_price1 = ask, bid


class StaticApi:
    """持仓恒定；`spawn` 指定"成交后持仓怎么变"（用于端到端正/负例）。"""

    def __init__(self, lt=0, lh=2, st=0, sh=0, spawn=None):
        self.lt, self.lh, self.st, self.sh = lt, lh, st, sh
        self.spawn = spawn
        self.inserted = []
        self.cancelled = []

    def wait_update(self, deadline=None):
        return True

    def get_position(self, symbol=None):
        return P(self.lt, self.lh, self.st, self.sh)

    def get_quote(self, symbol):
        return Q()

    def get_order(self, oid):
        return None

    def cancel_order(self, oid):
        self.cancelled.append(oid)

    def insert_order(self, symbol, direction, offset, volume, limit_price,
                     advanced=None):
        self.inserted.append({"direction": direction, "offset": offset,
                              "volume": volume, "limit_price": limit_price,
                              "advanced": advanced})
        if self.spawn is not None:
            self.lt, self.lh, self.st, self.sh = self.spawn(
                self.lt, self.lh, self.st, self.sh, direction, volume)

        class _O:                       # FOK 瞬间全成（SimNow 的常态，p37 覆盖异常态）
            order_id = "raw-{}".format(len(self.inserted))
            status = "FINISHED"
            volume_left = 0
            trade_price = limit_price
            last_msg = "全部成交报单"

            def __init__(self):
                self.trade_records = {
                    "t": {"volume": volume, "price": limit_price}}
        return _O()


class SeqApi:
    """按帧吐持仓：每次 `wait_update` 前进一帧（末帧保持），供增量校验用。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.i = 0

    def wait_update(self, deadline=None):
        if self.i < len(self.frames) - 1:
            self.i += 1
        return True

    def get_position(self, symbol=None):
        t, h = self.frames[self.i]
        return P(lt=t, lh=h)


def make_broker(api, over=None):
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


_FAST = {"close_max_chase": 1, "chase_interval": 0.0,
         "fill_timeout_open": 0.05, "fill_timeout_close": 0.05,
         "channel": {"position_ok_timeout": 0.15, "verify_delta_timeout": 0.08,
                     "baseline_settle_wait": 0.0}}


print("\n[1] ★ `_wait_position_ok` 判据 = 昨仓 ≥ volume（不是今+昨）")
# ════════════════════════════════════════════════════════════════


def wait_ok(lt, lh, volume, require_yesterday=True):
    api = StaticApi(lt=lt, lh=lh)
    b = make_broker(api, _FAST)
    return b._wait_position_ok(Side.LONG, volume, timeout_s=0.15,
                               require_yesterday=require_yesterday)


check("昨仓 2 / 今仓 0，平 2 手 → 通过", wait_ok(0, 2, 2), True)
check("昨仓 2 / 今仓 1，平 2 手 → 通过", wait_ok(1, 2, 2), True)
check("★ 今仓 2 / 昨仓 0，平 2 手 → **不通过**（不可用今仓顶替平昨）",
      wait_ok(2, 0, 2), False)
check("今仓 1 / 昨仓 1，平 2 手 → 不通过（总量够但昨仓不够）",
      wait_ok(1, 1, 2), False)
check("昨仓 1 / 今仓 5，平 2 手 → 不通过", wait_ok(5, 1, 2), False)
check("对照：require_yesterday=False（旧口径）→ 今仓够就放行",
      wait_ok(2, 0, 2, require_yesterday=False), True)


print("\n[2] 端到端：只有今仓 → 一个委托都不发，且拒单类别 = position")
# ════════════════════════════════════════════════════════════════
api = StaticApi(lt=2, lh=0, spawn=lambda lt, lh, st, sh, d, v: (lt, lh, st, sh))
b = make_broker(api, _FAST)
o = b.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-today-only", is_exit=True)
check("★ 未向柜台发出任何委托", len(api.inserted), 0)
check("判 rejected", o.status, "rejected")
check("★ reject_class = position（引擎据此认定「柜台无此仓」）",
      o.meta.get("reject_class"), REJECT_POSITION)
check_true("拒单原因点名昨仓", "昨仓" in str(o.meta.get("reject_reason") or ""),
           o.meta.get("reject_reason"))


print("\n[3] ★ `_verify_yesterday_delta`：昨仓降 + 今仓不动 才算成立")
# ════════════════════════════════════════════════════════════════
check("昨仓 2→0、今仓 0→0 → True",
      _verify_yesterday_delta(SeqApi([(0, 2), (0, 2), (0, 0)]),
                              "X", "LONG", 0, 2, 2, timeout_s=0.2), True)
check("±1 帧漂移容忍（期望昨仓 3-2=1，实际看到 2）",
      _verify_yesterday_delta(SeqApi([(0, 3), (0, 2)]),
                              "X", "LONG", 0, 3, 2, timeout_s=0.2), True)
check("★ 今仓被平掉（今 2→0）→ False（成交打到了今仓 = 平今费率）",
      _verify_yesterday_delta(SeqApi([(2, 2), (0, 2)]),
                              "X", "LONG", 2, 2, 2, timeout_s=0.2), False)
check("昨仓未动 → False（超时）",
      _verify_yesterday_delta(SeqApi([(0, 2)]),
                              "X", "LONG", 0, 2, 2, timeout_s=0.1), False)
check("负基线（读仓失败）→ False（不猜、不放行）",
      _verify_yesterday_delta(SeqApi([(0, 2)]),
                              "X", "LONG", -1, -1, 2, timeout_s=0.1), False)


print("\n[4] 端到端：昨仓够 → filled；若实际平掉今仓 → 只发诊断，不推翻成交")
# ════════════════════════════════════════════════════════════════
api2 = StaticApi(lt=0, lh=2,
                 spawn=lambda lt, lh, st, sh, d, v: (lt, lh - v, st, sh))
b2 = make_broker(api2, _FAST)
with capture_warnings() as w:
    o2 = b2.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-ok", is_exit=True)
check("昨仓够 → filled", o2.status, "filled")
check("报文 offset=CLOSE（平昨）/ 方向 SELL",
      (api2.inserted[0]["offset"], api2.inserted[0]["direction"]),
      ("CLOSE", "SELL"))
check("平昨校验通过 → 无 P4/P5 诊断告警",
      [m for m in w if "P4/P5" in m], [])

api3 = StaticApi(lt=2, lh=2,
                 spawn=lambda lt, lh, st, sh, d, v: (lt - v, lh, st, sh))
b3 = make_broker(api3, _FAST)
with capture_warnings() as w3:
    o3 = b3.submit(OrderIntent.CLOSE, Side.LONG, 2, 4550.0, "k-today", is_exit=True)
check("★ 柜台把 CLOSE 撮合到今仓时，成交事实不推翻（仍是 filled，权威判据是 P6）",
      o3.status, "filled")
check_true("★ 但必须发 P4/P5 诊断（today_dropped，须人工核对平今费率）",
           any("today_dropped" in m for m in w3), w3)


print("\n[5] ★ D10 分类器：新增第 4 类 position（错误码 + 中文关键字）")
# ════════════════════════════════════════════════════════════════
check("错误码 30 → position", classify_ctp_reject("CTP:30,平仓量超过持仓量"),
      REJECT_POSITION)
check("错误码 50 → position", classify_ctp_reject("[50] error"), REJECT_POSITION)
check("错误码 51 → position", classify_ctp_reject("错误码 51"), REJECT_POSITION)
check("中文：平仓量超过持仓量 → position",
      classify_ctp_reject("CTP:平仓量超过持仓量"), REJECT_POSITION)
check("中文：平昨仓位不足 → position",
      classify_ctp_reject("平昨仓位不足"), REJECT_POSITION)
check("中文：可平仓位不足 → position",
      classify_ctp_reject("可平仓位不足"), REJECT_POSITION)
check("资金不足仍归 funds（不得被 position 抢走）",
      classify_ctp_reject("CTP:31 资金不足"), REJECT_FUNDS)
check("非交易时段仍归 not_tradable",
      classify_ctp_reject("非交易时间段"), REJECT_NOT_TRADABLE)
check("代码 17/28 仍归 not_tradable",
      (classify_ctp_reject("CTP:17"), classify_ctp_reject("CTP:28")),
      (REJECT_NOT_TRADABLE, REJECT_NOT_TRADABLE))
check("未识别 → price（宁可多追一次）",
      classify_ctp_reject("CTP:9999 未知错误"), REJECT_PRICE)
check("空串 → price", classify_ctp_reject(""), REJECT_PRICE)
check_true("★ position 属于「追价无用」（命中即 break）",
           REJECT_POSITION in NO_CHASE_REJECT_CLASSES,
           NO_CHASE_REJECT_CLASSES)
check_true("position ≠ not_tradable（两者对追价的判断同，但引擎记账语义必须分开）",
           REJECT_POSITION != REJECT_NOT_TRADABLE)


print("\n[6] `_position_split` / `_position_total` 一致性")
# ════════════════════════════════════════════════════════════════


class DictApi:
    """`get_position()` 返回 {合约: 持仓} 的形态（真实 tqsdk 的账户视图）。"""

    def __init__(self, d):
        self._d = d

    def get_position(self, symbol=None):
        return self._d


sp_api = StaticApi(lt=1, lh=2)
check("对象形态 → split 返回 (今, 昨)", _position_split(sp_api, "X", "LONG"), (1, 2))
check("对象形态 → total = 今 + 昨", _position_total(sp_api, "X", "LONG"), 3)
check("dict 缺本合约 → (0, 0)",
      _position_split(DictApi({}), "X", "LONG"), (0, 0))
check("dict 只有别的品种 → (0, 0)（不跨品种误读）",
      _position_split(DictApi({"CFFEX.IM2509": P(lt=5)}),
                      "CFFEX.IF2609", "LONG"), (0, 0))
check("dict 命中本合约 → 正确分拆",
      _position_split(DictApi({"CFFEX.IF2609": P(lt=1, lh=2)}),
                      "CFFEX.IF2609", "LONG"), (1, 2))
check("short 侧同样分拆",
      _position_split(DictApi({"CFFEX.IF2609": P(st=3, sh=4)}),
                      "CFFEX.IF2609", "SHORT"), (3, 4))
check("api 异常 → None（不可信）", _position_split(None, "X", "LONG"), None)
check("api 异常 → total = -1", _position_total(None, "X", "LONG"), -1)


print("\n" + "=" * 60)
print("P38 结果: {} 通过 / {} 失败".format(_PASS, _FIX))
print("=" * 60)
sys.exit(1 if _FIX else 0)
