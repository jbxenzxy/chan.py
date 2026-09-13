# -*- coding: utf-8 -*-
"""
P44 防回潮：Trading 层不再做任何"信号质量过滤"
==============================================
入场三道过滤（信号 K 线振幅上限 / 止损距离下限 / 止损距离上限）已删除，
"信号值不值得开仓"的职责归缠论分析引擎（信号生产者）。本测试把这条钉死，
防止后人"好心"在 Trading 层重新加回过滤。

核心断言：
  [1][2] 一个振幅极其巨大的买/卖信号，Trading 层仍按信号方向开仓（OPEN），
         而不是被过滤成 SKIP。
  [3]    配置层 EntryConfig 也已无三道过滤字段（防止"配置先回来、代码再回来"的半吊子回潮）。
  [4]    信号价格无效（price<=0）仍 SKIP —— 数据兜底保留，未随过滤一起删（确认没过度删除）。

跑法：python Trading/Test/test_p44_no_signal_filter.py
"""
import sys

from Trading.Infra.Types import Signal, Side, DecisionType
from Trading.Strategy.Entry import EntryPolicy
from Trading.Config import EntryConfig

_PASS, _FAIL = 0, 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [PASS] {}".format(name))
    else:
        _FAIL += 1
        print("  [FAIL] {}".format(name))


def _buy_sig(price, high, low):
    return Signal(key="P44-BUY", symbol="CFFEX.IF2609", freq="5m",
                  timestamp=price, date="2026-09-13 09:35", bsp_type="buy",
                  is_buy=True, price=float(price), high=float(high), low=float(low))


def _sell_sig(price, high, low):
    return Signal(key="P44-SELL", symbol="CFFEX.IF2609", freq="5m",
                  timestamp=price, date="2026-09-13 09:35", bsp_type="sell",
                  is_buy=False, price=float(price), high=float(high), low=float(low))


print("P44：Trading 层不做信号质量过滤（职责归缠论引擎）")

policy = EntryPolicy({})

# [1] 振幅极其巨大的买点信号 → 仍按信号开多（不被过滤成 SKIP）
sig_big_buy = _buy_sig(4000.0, 9000.0, 100.0)   # 振幅 = 8900 点，远超任何合理阈值
d = policy.decide(sig_big_buy, None, None)
check("[1a] 巨幅买点信号 → OPEN", d.type == DecisionType.OPEN)
check("[1b] 方向 = LONG", d.side == Side.LONG)

# [2] 振幅极其巨大的卖点信号 → 仍按信号开空
sig_big_sell = _sell_sig(4000.0, 9000.0, 100.0)
d2 = policy.decide(sig_big_sell, None, None)
check("[2a] 巨幅卖点信号 → OPEN", d2.type == DecisionType.OPEN)
check("[2b] 方向 = SHORT", d2.side == Side.SHORT)

# [3] 配置层也无三道过滤字段（防止"配置先回来、代码再回来"的半吊子回潮）
check("[3a] EntryConfig 无 max_signal_range_points",
      "max_signal_range_points" not in EntryConfig.model_fields)
check("[3b] EntryConfig 无 min_stop_distance_points",
      "min_stop_distance_points" not in EntryConfig.model_fields)
check("[3c] EntryConfig 无 max_stop_distance_points",
      "max_stop_distance_points" not in EntryConfig.model_fields)

# [4] 信号价格无效（price<=0）→ 仍 SKIP（数据兜底保留，未随过滤一起删）
sig_bad = _buy_sig(0.0, 0.0, 0.0)
d3 = policy.decide(sig_bad, None, None)
check("[4a] 价格无效信号 → SKIP", d3.type == DecisionType.SKIP)

print("")
print("=" * 60)
print("P44 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
