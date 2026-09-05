# -*- coding: utf-8 -*-
"""
P8 标准分层组合出场策略（LayeredExitPolicy）单元测试
====================================================
验证四层行为：L1 R 倍数基线、L2 ATR 宽窄、L3 保本+跟踪、L4 时间/收盘兜底。
以及二者交互：同根 K 线 SL 优先于 TP、P2 防护、_trail_best 落盘；
[12] 钉住 L3 best 极值口径（解耦参数 + 盘中冲高回落，旧收盘口径应红）。

不需要真实 tqsdk / 网络，全部用本地构造的 Bar/Signal/Position。
跑法：python test_p8_layered_exit.py
"""
from __future__ import annotations

import os
import sys

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
    print("✗ 找不到 tg 包，请把本文件放在 trader_gateway/ 或 trader_gateway/tests/ 下。")
    raise SystemExit(2)
sys.path.insert(0, _TG_ROOT)

from tg.symbols import InstrumentSpec  # noqa: E402
from tg.types import Bar, ExitPlan, Position, Signal, Side  # noqa: E402
from tg.strategy.layered_exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name + ("  -> {}".format(got) if not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def make_spec():
    return InstrumentSpec()  # price_tick 默认 0.2


def make_signal(side, price, high, low, date="2026-09-01 09:35"):
    is_buy = side is Side.LONG
    return Signal(key="k|1|" + ("B" if is_buy else "S"), symbol="CFFEX.IF",
                  freq="5m", date=date, timestamp=0, bsp_type="1", is_buy=is_buy,
                  price=price, high=high, low=low)


def make_bar(ts, o, h, l, c, date="2026-09-01 09:35"):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def make_position(side, entry, stop, tp, params=None, entry_bar_seq=10):
    plan = ExitPlan(name="LayeredExitPolicy", stop_price=stop, tp_price=tp,
                    params=params or {})
    return Position(symbol="CFFEX.IF", side=side, volume=1, entry_price=entry,
                    entry_at="2026-09-01 09:35", entry_bar_ts=0,
                    signal_key="k", open_order_id="o1", exit_plan=plan,
                    entry_bar_seq=entry_bar_seq)


def feed(pol, n, o=100.0, h=101.0, l=99.0, c=100.0, start_ts=1000):
    for i in range(n):
        pol.on_bar(make_bar(start_ts + i, o, h, l, c), make_spec())


# ============================ 测试 ============================
def main():
    spec = make_spec()

    print("\n[1] L1 R 倍数基线（use_atr=False）：多/空方向与 1:2 比例 + P2 防护")
    # 多单：R=10，止损=入场-10，止盈=入场+20
    pol = LayeredExitPolicy({"use_atr": False, "initial_risk_points": 10.0,
                             "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                             "min_r_points": 2.0})
    plan = pol.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("多单 止损 = 90（向上取整）", plan.stop_price, 90.0)
    check("多单 止盈 = 120（向下取整）", plan.tp_price, 120.0)
    check("多单 1:2（止盈距=2×止损距）",
          approx((plan.tp_price - 100.0), 2 * (100.0 - plan.stop_price)), True)
    # 空单镜像
    pol2 = LayeredExitPolicy({"use_atr": False, "initial_risk_points": 10.0,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0})
    plan2 = pol2.plan(make_signal(Side.SHORT, 100.0, 101.0, 99.0), 100.0, spec)
    check("空单 止损 = 110（向下取整）", plan2.stop_price, 110.0)
    check("空单 止盈 = 80（向上取整）", plan2.tp_price, 80.0)
    # P2 防护：陈旧信号，极值已越过入场价 → 止损必须仍在 entry 不利侧
    pol3 = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": True,
                              "initial_risk_points": 10.0, "min_r_points": 2.0})
    # 信号最低价 102 > 入场 100（行情已涨），极端情况止损本应=102 在 entry 上方 → 必须被压回
    plan3 = pol3.plan(make_signal(Side.LONG, 100.0, 105.0, 102.0), 100.0, spec)
    check("P2 多单止损严格在 entry 下方", plan3.stop_price < 100.0, True)

    print("\n[2] L2 ATR 自适应宽窄（use_atr=True，喂 15 根 TR=2 的 K 线 → ATR=2）")
    pol4 = LayeredExitPolicy({"use_atr": True, "atr_period": 14,
                              "atr_sl_multiple": 2.0, "r_multiple_tp": 2.0,
                              "min_r_points": 2.0, "stop_at_signal_extreme": False})
    feed(pol4, 15)  # 每根 h=101,l=99,c=100 → TR=2 → ATR≈2 → R=4
    plan4 = pol4.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("ATR 路径 止损 = 96（R=2×ATR=4）", plan4.stop_price, 96.0)
    check("ATR 路径 止盈 = 108（2R=8）", plan4.tp_price, 108.0)
    check("ATR 路径 R 落盘", approx(plan4.params.get("R", 0), 4.0), True)

    print("\n[3] ATR 不可用（首根未喂）回退 L1 固定基线")
    pol5 = LayeredExitPolicy({"use_atr": True, "initial_risk_points": 10.0,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "min_r_points": 2.0})
    plan5 = pol5.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("无 ATR 回退 止损 = 90", plan5.stop_price, 90.0)

    print("\n[4] 同根 K 线同时触止盈止损 → 按止损计（悲观）")
    pos = make_position(Side.LONG, 100.0, 90.0, 110.0)
    # bar.low=89（破止损）<=90 且 bar.high=111（破止盈）>=110 → 取 sl
    chk = LayeredExitPolicy({}).check(pos, make_bar(2000, 100, 111, 89, 100), spec, 5)
    check("同根 K 线优先 sl", chk.reason if chk else None, "sl")

    print("\n[5] L3 保本：浮盈 ≥ 1R 抬止损至保本（only_update）")
    pol6 = LayeredExitPolicy({"use_atr": False, "initial_risk_points": 10.0,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "use_trailing": True, "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_ticks": 0.0, "trailing_trigger_r": 99.0,
                              "trailing_distance_points": 0.0})
    pos6 = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    # close=111 → 浮盈 11 ≥ 1R(10) → 保本位=100 > 90 → 更新
    chk6 = pol6.check(pos6, make_bar(2100, 100, 111, 100, 111), spec, 5)
    check("保本触发 only_update", chk6.only_update if chk6 else None, True)
    check("保本新止损 = 100", chk6.plan.stop_price if chk6 else None, 100.0)

    print("\n[6] L3 跟踪：浮盈 ≥ 2R 启动跟踪（用 trailing_distance_points 兜底）")
    pol7 = LayeredExitPolicy({"use_atr": False, "initial_risk_points": 10.0,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "use_trailing": True, "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_ticks": 0.0, "trailing_trigger_r": 2.0,
                              "trailing_distance_points": 5.0})
    # 已先保本到 100；本根 close=130（浮盈30≥2R=20），最高 131 → 跟踪=131-5=126
    # 用 tp=9999 排除"硬止盈"干扰，low=101>保本止损100 排除"硬止损"干扰，只验跟踪
    pos7 = make_position(Side.LONG, 100.0, 100.0, 9999.0, params={"R": 10.0, "_trail_best": 131.0})
    chk7 = pol7.check(pos7, make_bar(2200, 100, 131, 101, 130), spec, 5)
    check("跟踪触发 only_update", chk7.only_update if chk7 else None, True)
    check("跟踪新止损 = 126", chk7.plan.stop_price if chk7 else None, 126.0)
    check("跟踪极值 _trail_best 落盘", chk7.plan.params.get("_trail_best"), 131.0)

    print("\n[7] L4 时间止损：bars_held ≥ max_hold_bars")
    pol8 = LayeredExitPolicy({"max_hold_bars": 3})
    pos8 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    # 第 3 根（bars_held=3）且未触 SL/TP（bar 在 95~105 之间）
    chk8 = pol8.check(pos8, make_bar(2300, 100, 105, 95, 100), spec, 3)
    check("时间止损 reason=time", chk8.reason if chk8 else None, "time")

    print("\n[8] L4 收盘兜底：到点(session_end_hhmm)强平")
    pol9 = LayeredExitPolicy({"session_end_hhmm": "14:55"})
    pos9 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    chk9 = pol9.check(pos9, make_bar(2400, 100, 105, 95, 100, "2026-09-01 14:55"), spec, 5)
    check("到点强平 reason=eod_time", chk9.reason if chk9 else None, "eod_time")
    # 未到点不触发
    chk9b = pol9.check(pos9, make_bar(2401, 100, 105, 95, 100, "2026-09-01 14:50"), spec, 5)
    check("未到点不触发", chk9b is None, True)

    print("\n[9] 全部关闭时（use_atr/use_trailing 均 False，无 time）只判硬出场")
    pol10 = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": False,
                               "initial_risk_points": 10.0, "use_trailing": False,
                               "max_hold_bars": 0, "session_end_hhmm": ""})
    pos10 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    chk10 = pol10.check(pos10, make_bar(2500, 100, 105, 95, 100), spec, 100)
    check("仅硬出场、无触发返回 None", chk10 is None, True)

    print("\n[10] T3: EOD 以 bar 结束时刻判定（14:50 起点那根在 14:55 到达即触发）")
    pol11 = LayeredExitPolicy({"session_end_hhmm": "14:55"})
    # 喂两根间隔 300s 的闭合 bar，让策略推断出 bar 周期
    pol11.on_bar(make_bar(1000, 100, 101, 99, 100, "2026-09-01 14:40"), spec)
    pol11.on_bar(make_bar(1300, 100, 101, 99, 100, "2026-09-01 14:45"), spec)
    pos11 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    # 14:50 起点的 bar 覆盖 14:50-14:55、14:55 推送 → 结束时刻 14:55 ≥ 阈值 → 触发（留足缓冲）
    chk11 = pol11.check(pos11, make_bar(1600, 100, 105, 95, 100, "2026-09-01 14:50"), spec, 5)
    check("14:50 起点 bar（结束 14:55）触发 eod_time", chk11.reason if chk11 else None, "eod_time")
    # 14:45 起点的 bar 结束时刻 14:50 < 14:55 → 不触发
    chk11b = pol11.check(pos11, make_bar(1300, 100, 105, 95, 100, "2026-09-01 14:45"), spec, 5)
    check("14:45 起点 bar（结束 14:50）不触发", chk11b is None, True)
    # 14:55 起点的 bar（结束 15:00，若上一根 EOD 单失败）→ 仍触发作重试兜底
    chk11c = pol11.check(pos11, make_bar(1900, 100, 105, 95, 100, "2026-09-01 14:55"), spec, 6)
    check("14:55 起点 bar（结束 15:00）仍触发", chk11c.reason if chk11c else None, "eod_time")

    print("\n[11] T4: 裸构造默认值 = config.py 单一事实源")
    pol12 = LayeredExitPolicy()
    check("max_hold_bars 默认 = config 30", pol12.max_hold_bars, 30)
    check("session_end_hhmm 默认 = config 14:55", pol12.session_end_hhmm, "14:55")
    check("r_multiple_tp 默认 = config 2.0", pol12.r_multiple_tp, 2.0)
    check("atr_period 默认 = config 14", pol12.atr_period, 14)

    print("\n[12] L3 口径统一（best 极值）：解耦参数 + 盘中冲高回落也抬损（钉住 P8-A 场景）")
    # 解耦配置：tp=3R（130）、trailing 提前到 1.5R 启动、保本层用 trigger=99 屏蔽隔离
    pol13 = LayeredExitPolicy({"use_atr": False, "initial_risk_points": 10.0,
                               "stop_at_signal_extreme": False, "r_multiple_tp": 3.0,
                               "use_trailing": True, "breakeven_trigger_r": 99.0,
                               "breakeven_buffer_ticks": 0.0, "trailing_trigger_r": 1.5,
                               "trailing_atr_multiple": 0.0, "trailing_distance_points": 1.0,
                               "max_hold_bars": 0, "session_end_hhmm": ""})
    # A 场景（R=10）：盘中冲 2R（high=120，未到 3R 止盈 130）、收盘回落 1.2R（112）
    #   旧口径（fav 看收盘 1.2R=12 点 < 1.5R=15 点）漏检；
    #   新口径（best=120，20 点 ≥ 15 点）抬损 = best-1 = 119
    pos13 = make_position(Side.LONG, 100.0, 90.0, 130.0, params={"R": 10.0, "_trail_best": 100.0})
    chk13 = pol13.check(pos13, make_bar(2600, 110.0, 120.0, 105.0, 112.0), spec, 5)
    check("A 冲高回落触发跟踪 only_update", chk13.only_update if chk13 else None, True)
    check("A 新止损 = best-1 = 119", chk13.plan.stop_price if chk13 else None, 119.0)
    check("A 极值 _trail_best = 120 落盘",
          chk13.plan.params.get("_trail_best") if chk13 else None, 120.0)
    # A' 对照：未达阈值（1.4R=14 点 < 15 点）不得误触发
    pos13b = make_position(Side.LONG, 100.0, 90.0, 130.0, params={"R": 10.0, "_trail_best": 100.0})
    chk13b = pol13.check(pos13b, make_bar(2601, 108.0, 114.0, 106.0, 113.0), spec, 5)
    check("A' 1.4R 未达阈值不触发", chk13b is None, True)
    # A'' 跨 bar 极值记忆：用 A 返回的计划续喂新高 bar（low=119.5>新止损 避开硬 SL），跟踪续抬
    if chk13 is not None:
        pos13.exit_plan = chk13.plan
    chk13c = pol13.check(pos13, make_bar(2602, 122.0, 125.0, 119.5, 122.0), spec, 6)
    check("A'' 跨 bar 极值续抬损 = 125-1 = 124",
          chk13c.plan.stop_price if chk13c else None, 124.0)
    check("A'' 极值续记 _trail_best = 125",
          chk13c.plan.params.get("_trail_best") if chk13c else None, 125.0)
    # 空单镜像：盘中下探 2R（low=80，2R=20 点 ≥ 15 点）、收盘收回 1.2R（88）
    pos14 = make_position(Side.SHORT, 100.0, 110.0, 70.0, params={"R": 10.0, "_trail_best": 100.0})
    chk14 = pol13.check(pos14, make_bar(2600, 90.0, 95.0, 80.0, 88.0), spec, 5)
    check("空单镜像 only_update", chk14.only_update if chk14 else None, True)
    check("空单镜像新止损 = best+1 = 81", chk14.plan.stop_price if chk14 else None, 81.0)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
