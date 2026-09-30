# -*- coding: utf-8 -*-
"""
P6 成交裁决 · `_finalize` **接线级**护栏（2026-09-30 事故根修）
================================================================
与 `test_p6_fix.py` 的分工（为什么必须另立一份）
    `test_p6_fix.py` 测的是**判据函数**（`_p6_is_filled` / `_traded_*_from_records`），
    不经过 `_finalize` —— 那里的**接线**（哪一条分支拥有否决权、成交价怎么取、
    `Order.status` / `filled_price` 怎么落）改了它不会红。
    2026-09-30 的 IM 实盘事故恰恰出在接线上：判据函数本身没写错，是 `_finalize`
    把"成交明细手数不足"接成了**成交事实的单方向否决权**。

事件原型（2026-09-30 10:29 CFFEX.IM2612，实盘）
    状态回报流（orders）与成交明细流（trades）经 otg-simnow 中继**独立到达**。
    10:29:18 状态包到达：`status=FINISHED, volume_left=0, last_msg=全部成交报单已提交`；
    明细包 2 秒后才到。引擎的同步终判恰好落在两包之间 → 旧实现据此判成 rejected、
    不落账本，留下无风控锚的孤儿多仓 2 手（快期3 里却是成交的）。
    ——这不是"时序碰巧"，规则写死了在窗口内必判反。

不变量（本文件钉住的规格）
    1. 成交必须由**正向证据**宣告（终态文案宣告成交，或明细手数足够）；
       绝不因**证据缺失**（明细未到）被否证；
    2. 判成 filled 的 `Order`，其 `filled_price` 必然是一个有限正数 ——
       否则引擎 `Engine._execute` 的 `o.filled_price is None` 会把成交单
       重新当成 rejected，账本照样不落（"判成交"白判）；
    3. 09-04 幻影防护不降级：拒单文案 + 残留余量 0 → 仍判未成交。

本测试直接调**真实** `_finalize`（`object.__new__` 手工装配，仓库单测同款惯例），
不 mock 判定逻辑本身。跑法：python Trading/Test/test_p6_finalize_wiring.py
"""
from __future__ import annotations

import logging
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    """从本文件位置向上找 Trading 包目录（与仓库其余 Trading/Test/*.py 同款）。"""
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
    print("✗ 找不到 Trading 包。请把本文件放在 Trading/ 或 Trading/Test/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 Trading 目录。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

try:
    from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
    from Trading.Infra.Records import Side  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法 import 被测模块: {}: {}".format(type(e).__name__, e))
    print("  Trading 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)


class MockTqOrder:
    """tqsdk Order 替身：只带 `_finalize` 读得到的字段。

    默认值刻意对齐**事故现场**那一份终态 —— 后续用例只改"到达时序"那一个量。
    """

    def __init__(self, status="FINISHED", volume_left=0, trade_records=None,
                 last_msg="", trade_price=None, order_id="PYSDK_insert_test"):
        self.status = status
        self.volume_left = volume_left
        self.trade_records = trade_records
        self.last_msg = last_msg
        self.trade_price = trade_price
        self.order_id = order_id


def make_broker() -> SimNowBroker:
    """`object.__new__` 手工装配：只给 `_finalize` 真正触及的属性/方法。

    刻意不构造真 broker（`__init__` 在凭据齐全时会真的 `_connect()`，测试不该联网）。
    方法一律绑到**类的真实实现**上（不是重写），保证测的是生产代码。
    """
    b = object.__new__(SimNowBroker)
    b.name = "simnow"
    b._trade_symbol = "CFFEX.IM2612"
    b._submit_t0 = 0.0
    b._watchdog_fired_at = None
    b._order_seq = 3
    b.orders = []                                # _finalize 尾部会 append 落单
    b._sig_orders = {}                           # signal_key → raw_order_id 登记
    b.order_seq = lambda: 3                      # 审计号自增源（Base._next_order_id）
    b._trade_price = lambda o: SimNowBroker._trade_price(b, o)
    b._note_reject = lambda *a, **k: SimNowBroker._note_reject(b, *a, **k)
    b._note_position_lag = lambda *a, **k: SimNowBroker._note_position_lag(b, *a, **k)
    return b


def finalize(order, volume=2, ref_price=7193.60, limit=None):
    """调真实 `_finalize`，并把 `tg.brokers.simnow` 的日志整段捕获回来。

    limit=None 时与 ref_price 同值（既有用例语义不变）；事故用例显式传
    ref_price=7191.0 / limit=7193.60（现场实数），让「回落取哪个价」可判别。
    """
    b = make_broker()
    recs = []
    h = logging.Handler()
    h.emit = lambda r: recs.append(r.getMessage())
    lg = logging.getLogger("tg.brokers.simnow")
    lg.addHandler(h)
    try:
        o = b._finalize(
            order, intent_str="open", action="open", side=Side.LONG,
            volume=volume, ref_price=ref_price,
            signal_key="2026/09/30 10:29:00|0|B", note="signal",
            baseline=None, expected_delta=volume,
            limit=ref_price if limit is None else limit)
    finally:
        lg.removeHandler(h)
    return o, recs


# ---------------- 断言助手 ----------------
_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {}   got={!r}  want={!r}".format(name, got, want))


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


# ---------------- 用例 ----------------
def test_accident_shape() -> None:
    """事故现场：状态包已宣告全部成交，明细包未到。"""
    print("\n[1] 事故形态：FINISHED + volume_left=0 + 明细空 + last_msg=全部成交报单已提交")
    o, recs = finalize(
        MockTqOrder(status="FINISHED", volume_left=0, trade_records={},
                    last_msg="全部成交报单已提交", trade_price=float("nan")),
        volume=2, ref_price=7191.0, limit=7193.60)
    check("[1a] 判成交（明细未到不得翻转成交事实）", o.status, "filled")
    check_true("[1b] filled_price 是有限正数（nan/None 都不可接受）",
               o.filled_price is not None and math.isfinite(o.filled_price)
               and o.filled_price > 0)
    check("[1c] 成交价回落链落到委托限价（不是信号价 ref_price）",
          o.filled_price, 7193.60)
    check("[1d] meta 标注成交价来源", o.meta.get("price_source"), "limit")
    check("[1e] **不写**「委托被拒」日志（现场正是这一行把事故写进 events）",
          [m for m in recs if "委托被拒" in m], [])
    check("[1f] 不留 reject_reason", o.meta.get("reject_reason"), None)
    check("[1g] 不留 reject_class", o.meta.get("reject_class"), "")


def test_records_arrived() -> None:
    """对照：明细包已到（同秒到达的正常路径）。"""
    print("\n[2] 明细已到：同结构终态 + trade_records 2 手")
    o, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=0,
                    trade_records={"t1": {"volume": 2, "price": 7192.40}},
                    last_msg="全部成交报单已提交", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[2a] 判成交", o.status, "filled")
    check("[2b] 成交价取明细加权均价", o.filled_price, 7192.40)
    check("[2c] meta 标注来源为明细", o.meta.get("price_source"), "trade_records")


def test_partial_records_but_announced() -> None:
    """明细**部分**到达（1/2 手）—— 旧实现会判未成交；明细无权否决已宣告的成交。"""
    print("\n[3] 明细只到 1 手但终态已宣告全部成交 → 仍判成交")
    o, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=0,
                    trade_records={"t1": {"volume": 1, "price": 7192.40}},
                    last_msg="全部成交报单已提交", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[3a] 判成交（明细不足不得否决）", o.status, "filled")
    check("[3b] 成交价取委托对象均价（明细不全，不用明细价）", o.filled_price, 7192.40)
    check("[3c] meta 标注来源", o.meta.get("price_source"), "order.trade_price")

    print("\n[3d] 无文案宣告 + 明细只到 1 手 → 维持未成交（保守方向不变）")
    o2, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=0,
                    trade_records={"t1": {"volume": 1, "price": 7192.40}},
                    last_msg="", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[3d] 判未成交", o2.status, "rejected")


def test_reject_shape() -> None:
    """拒单的正常形态：什么都没成交 → 余量 = 委托量。"""
    print("\n[4] 拒单形态：FINISHED + volume_left=2 + last_msg=资金不足")
    o, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=2, trade_records={},
                    last_msg="资金不足，报单被拒绝"),
        volume=2, ref_price=7193.60)
    check("[4a] 判未成交", o.status, "rejected")
    check("[4b] 拒因分类 = funds（供引擎停追）", o.meta.get("reject_class"), "funds")
    check_true("[4c] 走的是 P3 层分支（判词为『未真正成交』）",
               "未真正成交" in (o.meta.get("reject_reason") or ""))
    check_true("[4d] 未落 filled_price", o.filled_price is None)


def test_ghost_shape() -> None:
    """09-04 幻影形态：拒单文案 + 余量残留 0 —— 防护不得降级。"""
    print("\n[5] 幻影形态：FINISHED + volume_left=0 + 明细空 + last_msg=资金不足")
    o, recs = finalize(
        MockTqOrder(status="FINISHED", volume_left=0, trade_records={},
                    last_msg="资金不足，报单被拒绝", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[5a] 判未成交（09-04 幻影防护不降级）", o.status, "rejected")
    check_true("[5b] 判词走 P6 分支（文案未宣告成交）",
               (o.meta.get("reject_reason") or "").startswith("P6:"))
    check_true("[5c] 仍写「委托被拒」日志（可追溯）",
               len([m for m in recs if "委托被拒" in m]) == 1)
    check_true("[5d] 未落 filled_price", o.filled_price is None)

    print("\n[5e] 幻影形态的撤单文案（已撤单）同样判未成交")
    o2, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=0, trade_records={},
                    last_msg="已撤单报单已提交", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[5e] 判未成交", o2.status, "rejected")

    print("\n[5f] 幻影形态的否定式文案（未全部成交，已撤单）同样判未成交")
    o3, _ = finalize(
        MockTqOrder(status="FINISHED", volume_left=0, trade_records={},
                    last_msg="未全部成交，已撤单", trade_price=7192.40),
        volume=2, ref_price=7193.60)
    check("[5f] 判未成交（否定语境护栏生效）", o3.status, "rejected")


def test_non_terminal_and_cancel() -> None:
    """非终态 / 撤单余量未清零：P3 层直接挡（本修复未改这几条）。"""
    print("\n[6] P3 层既有判据不受影响")
    o, _ = finalize(MockTqOrder(status="ALIVE", volume_left=2, trade_records={},
                                last_msg="未成交还在队列中"), volume=2)
    check("[6a] 非终态 → 未成交", o.status, "rejected")

    o2, _ = finalize(MockTqOrder(status="FINISHED", volume_left=2, trade_records={},
                                 last_msg="全部成交报单已提交"), volume=2)
    check("[6b] 文案宣告成交但余量未清零 → 仍未成交（P3 优先）", o2.status, "rejected")


def test_filled_price_never_none() -> None:
    """不变量 2 的直证：判成 filled 的单子绝不能带 None 价。

    引擎 `Engine._execute` 的落账判据是 `o.status != "filled" or o.filled_price is None`
    —— 价格缺失会让"判成交"在引擎侧退化成"判拒单"，账本照样不落。
    """
    print("\n[7] filled ⇒ filled_price 有限正数（引擎落账的必要条件）")
    shapes = [
        ("明细到 + 委托均价 nan", dict(trade_records={"t1": {"volume": 2, "price": 7192.4}},
                                        trade_price=float("nan"))),
        ("明细空 + 委托均价 nan", dict(trade_records={}, trade_price=float("nan"))),
        ("明细空 + 委托均价 None", dict(trade_records={}, trade_price=None)),
        ("明细空 + 委托均价 0", dict(trade_records={}, trade_price=0.0)),
    ]
    for name, kw in shapes:
        o, _ = finalize(MockTqOrder(status="FINISHED", volume_left=0,
                                    last_msg="全部成交报单已提交", **kw),
                        volume=2, ref_price=7193.60)
        check("[7] {} → filled 且价有限".format(name),
              (o.status, o.filled_price is not None
               and math.isfinite(o.filled_price) and o.filled_price > 0),
              ("filled", True))


def main() -> int:
    print("=" * 72)
    print("P6 成交裁决 · _finalize 接线级护栏（2026-09-30 事故根修）")
    print("被测: {}/Broker/SimNow.py（真 import，非副本）".format(_TG_ROOT))
    print("=" * 72)
    test_accident_shape()
    test_records_arrived()
    test_partial_records_but_announced()
    test_reject_shape()
    test_ghost_shape()
    test_non_terminal_and_cancel()
    test_filled_price_never_none()
    print("\n" + "=" * 72)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 72)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
