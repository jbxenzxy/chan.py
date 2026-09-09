# -*- coding: utf-8 -*-
"""
P8 标准分层组合出场策略（LayeredExitPolicy）单元测试
====================================================
验证三层行为：L1 R 倍数基线、L2 ATR 宽窄、L3 保本+跟踪。
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
        if os.path.basename(d) == "Trading" and os.path.isfile(os.path.join(d, "__init__.py")):
            return d  # Trading 包目录本身（消 tg/ 层后 Trading 即包）
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包，请把本文件放在 Trading/ 或 Trading/tests/ 下。")
    raise SystemExit(2)
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Types import Bar, ExitPlan, Position, Signal, Side  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

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


def make_signal(side, price, high, low, date="2026-09-01 09:35",
                fractal_low=0.0, fractal_high=0.0):
    is_buy = side is Side.LONG
    return Signal(key="k|1|" + ("B" if is_buy else "S"), symbol="CFFEX.IF",
                  freq="5m", date=date, timestamp=0, bsp_type="1", is_buy=is_buy,
                  price=price, high=high, low=low,
                  fractal_low=fractal_low, fractal_high=fractal_high)


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
    # 多单：R=min_r_points=2，止损=入场-2，止盈=入场+4
    pol = LayeredExitPolicy({"use_atr": False,
                             "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                             "min_r_points": 2.0, "use_trailing": False})
    plan = pol.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("多单 止损 = 98（向上取整）", plan.stop_price, 98.0)
    check("多单 止盈 = 104（向下取整）", plan.tp_price, 104.0)
    check("多单 1:2（止盈距=2×止损距）",
          approx((plan.tp_price - 100.0), 2 * (100.0 - plan.stop_price)), True)
    # 空单镜像
    pol2 = LayeredExitPolicy({"use_atr": False,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "min_r_points": 2.0, "use_trailing": False})
    plan2 = pol2.plan(make_signal(Side.SHORT, 100.0, 101.0, 99.0), 100.0, spec)
    check("空单 止损 = 102（向下取整）", plan2.stop_price, 102.0)
    check("空单 止盈 = 96（向上取整）", plan2.tp_price, 96.0)
    # P2 防护：陈旧信号，极值已越过入场价 → 止损必须仍在 entry 不利侧
    pol3 = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": True,
                              "min_r_points": 2.0})
    # 信号最低价 102 > 入场 100（行情已涨），极端情况止损本应=102 在 entry 上方 → 必须被压回
    plan3 = pol3.plan(make_signal(Side.LONG, 100.0, 105.0, 102.0), 100.0, spec)
    check("P2 多单止损严格在 entry 下方", plan3.stop_price < 100.0, True)

    print("\n[2] L2 ATR 自适应宽窄（use_atr=True，喂 15 根 TR=2 的 K 线 → ATR=2）")
    pol4 = LayeredExitPolicy({"use_atr": True, "atr_period": 14,
                              "atr_sl_multiple": 2.0, "r_multiple_tp": 2.0,
                              "min_r_points": 2.0, "stop_at_signal_extreme": False,
                              "use_trailing": False})
    feed(pol4, 15)  # 每根 h=101,l=99,c=100 → TR=2 → ATR≈2 → R=4
    plan4 = pol4.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("ATR 路径 止损 = 96（R=2×ATR=4）", plan4.stop_price, 96.0)
    check("ATR 路径 止盈 = 108（2R=8）", plan4.tp_price, 108.0)
    check("ATR 路径 R 落盘", approx(plan4.params.get("R", 0), 4.0), True)

    print("\n[3] ATR 不可用（首根未喂）回退 L1（min_r_points 保底）")
    pol5 = LayeredExitPolicy({"use_atr": True,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "min_r_points": 2.0})
    plan5 = pol5.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("无 ATR 回退 止损 = 98", plan5.stop_price, 98.0)

    print("\n[4] 同根 K 线同时触止盈止损 → 按止损计（悲观）")
    pos = make_position(Side.LONG, 100.0, 90.0, 110.0)
    # bar.low=89（破止损）<=90 且 bar.high=111（破止盈）>=110 → 取 sl
    chk = LayeredExitPolicy({}).check(pos, make_bar(2000, 100, 111, 89, 100), spec, 5)
    check("同根 K 线优先 sl", chk.reason if chk else None, "sl")

    print("\n[5] L3 保本：浮盈 ≥ 1R 抬止损至保本（only_update）")
    pol6 = LayeredExitPolicy({"use_atr": False,
                              "stop_at_signal_extreme": False, "r_multiple_tp": 2.0,
                              "use_trailing": True, "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_ticks": 0.0, "trailing_trigger_r": 99.0,
                              "trailing_distance_points": 0.0})
    pos6 = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    # close=111 → 浮盈 11 ≥ 1R(10) → 保本位=100 > 90 → 更新
    chk6 = pol6.check(pos6, make_bar(2100, 100, 111, 100, 111), spec, 5)
    check("保本触发 only_update", chk6.only_update if chk6 else None, True)
    check("保本新止损 = 100", chk6.plan.stop_price if chk6 else None, 100.0)
    # 保本缓冲 >0：SL 抬到入场价之上 breakeven_buffer_ticks×tick，垫掉手续费+滑点
    pol6b = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": False,
                               "r_multiple_tp": 2.0, "use_trailing": True,
                               "breakeven_trigger_r": 1.0, "breakeven_buffer_ticks": 2.0,
                               "trailing_trigger_r": 99.0, "trailing_distance_points": 0.0})
    pos6b = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    chk6b = pol6b.check(pos6b, make_bar(2101, 100, 111, 100, 111), spec, 5)
    check("保本缓冲 2 tick → 止损=100.4（入场价之上）",
          chk6b.plan.stop_price if chk6b else None, 100.4)

    print("\n[6] L3 跟踪：浮盈 ≥ 2R 启动跟踪（用 trailing_distance_points 兜底）")
    pol7 = LayeredExitPolicy({"use_atr": False,
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

    print("\n[7] 全部关闭时（use_atr/use_trailing 均 False）只判硬出场")
    pol10 = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": False,
                               "use_trailing": False})
    pos10 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    chk10 = pol10.check(pos10, make_bar(2500, 100, 105, 95, 100), spec, 100)
    check("仅硬出场、无触发返回 None", chk10 is None, True)

    print("\n[8] T4: 裸构造默认值 = config.py 单一事实源")
    pol12 = LayeredExitPolicy()
    check("r_multiple_tp 默认 = config 2.0", pol12.r_multiple_tp, 2.0)
    check("atr_period 默认 = config 14", pol12.atr_period, 14)
    check("trailing_atr_multiple 默认 = config 1.0", pol12.trailing_atr_multiple, 1.0)
    check("use_trailing 默认 = True（B 方案）", pol12.use_trailing, True)

    print("\n[8b] B 方案（use_trailing=True 默认）：不设硬止盈，止盈交给 L3 跟踪")
    polB = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": False,
                              "r_multiple_tp": 2.0, "min_r_points": 2.0})
    planB = polB.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, spec)
    check("B 方案 tp_price = None（不硬止盈）", planB.tp_price is None, True)
    check("B 方案 止损仍照常 = 98", planB.stop_price, 98.0)

    print("\n[12] L3 口径统一（best 极值）：解耦参数 + 盘中冲高回落也抬损（钉住 P8-A 场景）")
    # 解耦配置：tp=3R（130）、trailing 提前到 1.5R 启动、保本层用 trigger=99 屏蔽隔离
    pol13 = LayeredExitPolicy({"use_atr": False,
                               "stop_at_signal_extreme": False, "r_multiple_tp": 3.0,
                               "use_trailing": True, "breakeven_trigger_r": 99.0,
                               "breakeven_buffer_ticks": 0.0, "trailing_trigger_r": 1.5,
                               "trailing_atr_multiple": 0.0, "trailing_distance_points": 1.0})
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

    print("\n[9] A=分型极值结构止损：R = max(A, 2×ATR, min_r_points)")
    # 做多：fractal_low=97（底分型最低点），entry=100，use_atr=False → A=3
    pol20 = LayeredExitPolicy({"use_atr": False, "stop_at_signal_extreme": True,
                               "r_multiple_tp": 2.0, "min_r_points": 2.0,
                               "use_trailing": False})
    plan20 = pol20.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                                    fractal_low=97.0), 100.0, spec)
    check("做多 A=entry−fractal_low=3 → 止损=97", plan20.stop_price, 97.0)
    check("做多 止盈 = entry+2R = 106", plan20.tp_price, 106.0)
    # 做空：fractal_high=103（顶分型最高点），entry=100 → A=3
    plan21 = pol20.plan(make_signal(Side.SHORT, 100.0, 101.0, 99.0,
                                    fractal_high=103.0), 100.0, spec)
    check("做空 A=fractal_high−entry=3 → 止损=103", plan21.stop_price, 103.0)
    check("做空 止盈 = entry−2R = 94", plan21.tp_price, 94.0)
    # max(A, 2×ATR)：A=3、2×ATR=4 → R=4（2×ATR 更大）
    pol22 = LayeredExitPolicy({"use_atr": True, "atr_period": 14,
                               "atr_sl_multiple": 2.0, "r_multiple_tp": 2.0,
                               "min_r_points": 2.0, "stop_at_signal_extreme": True})
    feed(pol22, 15)  # TR=2 → ATR≈2 → 2×ATR=4
    plan22 = pol22.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                                    fractal_low=97.0), 100.0, spec)
    check("max(A=3, 2×ATR=4)=4 → 止损=96", plan22.stop_price, 96.0)
    # A ≤ 0（行情已穿越分型）→ 交给 min_r_points 兜底
    plan23 = pol20.plan(make_signal(Side.LONG, 100.0, 99.0, 98.0,
                                    fractal_low=102.0), 100.0, spec)
    check("A≤0 → R=max(0,0,min_r)=2 → 止损=98", plan23.stop_price, 98.0)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
