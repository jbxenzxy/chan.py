# -*- coding: utf-8 -*-
"""
P7 超价下单 + 平仓追价 单元测试
===============================
背景（M4 改动）
    P0-P6 之前的挂单是「信号 K 线收盘价朝不利方向取整」的保守限价单，快速行情里
    容易挂偏一两档 → 卡单不成交。M4 改为「超价」：下单瞬间取实时对手价
    （买方向=ask / 卖方向=bid），再 ± overprice_points（默认 0.6 点 = IF 3 tick，
    朝成交方向取整到 tick），主动跨过价差确保成交。

    方向映射（最容易搞反）：
      开多 = 买 → 对手价 ask → 超价 ask + 0.5（向上取整）
      开空 = 卖 → 对手价 bid → 超价 bid - 0.5（向下取整）
      平多 = 卖 → 对手价 bid → 超价 bid - 0.5（向下取整）
      平空 = 买 → 对手价 ask → 超价 ask + 0.5（向上取整）

本测试用 mock api 对象验证，不需要真实 tqsdk / 网络。
跑法：python test_p7_pricing.py
"""
from __future__ import annotations

import itertools
import os
import sys

# ---- import simnow.py 里的【真实】类（懒加载 tqsdk，import 无需连接） ----
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
    print("✗ 找不到 tg 包。请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下，"
          "或设环境变量 TRADER_GATEWAY_HOME 指向 trader_gateway 目录。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

try:
    from tg.brokers.simnow import SimNowBroker  # noqa: E402
    from tg.symbols import InstrumentSpec  # noqa: E402
    from tg.types import Side  # noqa: E402
except Exception as e:  # pragma: no cover
    print("✗ 无法导入被测类: {}: {}".format(type(e).__name__, e))
    print("  tg 根目录解析为: {}".format(_TG_ROOT))
    raise SystemExit(2)

print("[import] 被测类来自: {}/tg/brokers/simnow.py（真实代码，非副本）".format(_TG_ROOT))


# ---------------- mock 对象 ----------------
class MockQuote:
    def __init__(self, ask=None, bid=None):
        self.ask_price1 = ask
        self.bid_price1 = bid


class MockApi:
    def __init__(self, quote=None, raise_on_quote=False):
        self._quote = quote
        self._raise = raise_on_quote

    def get_quote(self, symbol):
        if self._raise:
            raise RuntimeError("no quote")
        return self._quote


def make_broker(api=None, spec=None):
    """用 object.__new__ 绕过 __init__（避免真实 _connect 连 SimNow）。"""
    b = object.__new__(SimNowBroker)
    b.spec = spec or InstrumentSpec()
    b.params = {}
    b._api = api
    b._trade_symbol = b.spec.trade_symbol
    b._seq = itertools.count(1)
    b.orders = []
    return b


# ---------------- 测试用例 ----------------
_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    # 浮点：允许 1e-9 误差
    if isinstance(got, float) and isinstance(want, float):
        ok = abs(got - want) < 1e-9
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


print("\n[1] 方向映射 _is_buy（买方向=ask，卖方向=bid）")
check("open 多 -> 买", SimNowBroker._is_buy("open", Side.LONG), True)
check("open 空 -> 卖", SimNowBroker._is_buy("open", Side.SHORT), False)
check("close 多(平多=卖) -> 卖", SimNowBroker._is_buy("close", Side.LONG), False)
check("close 空(平空=买) -> 买", SimNowBroker._is_buy("close", Side.SHORT), True)

print("\n[2] 超价限价 _overprice_limit（对手价 ± 0.5，朝成交方向取整）")
# IF tick=0.2。ask=4565.0, overprice=0.5 -> 4565.5 -> 向上取整 -> 4565.6
b = make_broker(api=MockApi(MockQuote(ask=4565.0, bid=4560.0)))
check("开多 ask+0.5 向上取整", b._overprice_limit("open", Side.LONG, 0.5), 4565.6)
check("平空 ask+0.5 向上取整", b._overprice_limit("close", Side.SHORT, 0.5), 4565.6)
# 卖方向：bid=4560.0, -0.5 -> 4559.5 -> 向下取整 -> 4559.4
check("开空 bid-0.5 向下取整", b._overprice_limit("open", Side.SHORT, 0.5), 4559.4)
check("平多 bid-0.5 向下取整", b._overprice_limit("close", Side.LONG, 0.5), 4559.4)

print("\n[3] 取不到行情 -> 返回 None（回退到 align_*）")
b_none = make_broker(api=None)
check("api=None -> None", b_none._overprice_limit("open", Side.LONG, 0.5), None)
b_raise = make_broker(api=MockApi(raise_on_quote=True))
check("get_quote 抛异常 -> None", b_raise._overprice_limit("open", Side.LONG, 0.5), None)
b_empty = make_broker(api=MockApi(MockQuote(ask=None, bid=None)))
check("ask/bid 为空 -> None", b_empty._overprice_limit("open", Side.LONG, 0.5), None)

print("\n[4] _build_limit_price 回退：无行情时用 align_entry/exit（信号价）")
# 无行情 -> 回退 align_entry：开多 ref=4565.1 -> 向上取整 -> 4565.2
b_fb = make_broker(api=None)
check("开多回退 align_entry(4565.1,+)", b_fb._build_limit_price("open", Side.LONG, 4565.1), 4565.2)
# 平多回退 align_exit：ref=4565.1 -> 向下取整 -> 4565.0
check("平多回退 align_exit(4565.1,+)", b_fb._build_limit_price("close", Side.LONG, 4565.1), 4565.0)
# 有行情时优先超价，不回退
b_ok = make_broker(api=MockApi(MockQuote(ask=4565.0, bid=4560.0)))
check("有行情优先超价(开多)", b_ok._build_limit_price("open", Side.LONG, 9999.0), 4565.6)

print("\n[5] 参数覆盖：overprice_points 从 params 读取（默认 spec=0.6）")
b_p = make_broker(api=MockApi(MockQuote(ask=4565.0, bid=4560.0)))
b_p.params = {"overprice_points": 1.0}
check("params overprice=1.0 -> 开多 ask+1.0", b_p._build_limit_price("open", Side.LONG, 9999.0), 4566.0)

print("\n[6] 平仓兜底限价 _close_fallback_limit（仅行情取不到时使用）")
b_fb2 = make_broker(api=None)
# 首笔（prev_limit=None）-> 回退 align_exit(信号价)：平多=卖，向下取整
check("首笔无 prev -> align_exit(4565.1)", b_fb2._close_fallback_limit(Side.LONG, 4565.1, None, -1, 2), 4565.0)
# 后续轮次：上一笔 4559.4（卖方向），朝成交方向（降价）推 2 跳 = -0.4 -> 4559.0
check("卖方向 prev=4559.4 推 2 跳", b_fb2._close_fallback_limit(Side.LONG, 4565.1, 4559.4, -1, 2), 4559.0)
# 买方向（平空）：上一笔 4565.6，加价推 2 跳 = +0.4 -> 4566.0
check("买方向 prev=4565.6 推 2 跳", b_fb2._close_fallback_limit(Side.SHORT, 4565.1, 4565.6, 1, 2), 4566.0)
# 非整 tick 中间值：prev=4559.3 卖方向 -0.4=4558.9 -> 向下取整 4558.8
check("卖方向非整 tick 向下取整", b_fb2._close_fallback_limit(Side.LONG, 4565.1, 4559.3, -1, 2), 4558.8)

print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
