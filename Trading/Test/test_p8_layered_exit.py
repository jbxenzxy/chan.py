# -*- coding: utf-8 -*-
"""
P8 标准分层组合出场策略（LayeredExitPolicy）单元测试
====================================================
验证三层行为：L1 R 倍数基线、L2 ATR 宽窄、L3 保本+跟踪。
以及二者交互：P2 防护、_trail_best 落盘。
价格输入口径（2026-09-22 用户拍板）：L1-L3 的价格判定**只读 bar.close**，
不读 bar.high / bar.low —— 旧「同根 K 线 SL 优先于 TP」的悲观兜底规则随口径
删除（该场景已不可达）；等价护栏见 Trading/Test/test_p61_exit_close_only.py。

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

from Trading.Infra.Instrument import Instrument  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402


_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.Records import Bar, ExitPlan, Position, Signal, Side  # noqa: E402

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


def make_state():
    """出场策略读的是**运行时状态**（有效 tick + 定价方法）。

    拆分后 Engine 传给 exit_policy 的是运行时状态
    双类合并后它就是 Instrument —— 本测试走同一条路径，避免"测试钉死
    旧对象"的假绿灯。tick 取 IF 档案真值（无播种桥，构造时取档案）。
    """
    return (Instrument(None, _IF))


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
        pol.on_bar(make_bar(start_ts + i, o, h, l, c), make_state())


# ============================ 测试 ============================
def main():
    state = make_state()

    print("\n[1] L1 R 倍数基线（分型极值 A=2）：多/空方向与 1:2 比例 + P2 防护")
    # 多单：A = entry−fractal_low = 100−98 = 2 → R=2，止损=入场-2，止盈=入场+4
    pol = LayeredExitPolicy({"use_atr": False,
                             "r_multiple_tp": 2.0,
                             "use_trailing": False})
    plan = pol.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0, fractal_low=98.0), 100.0, state)
    check("多单 止损 = 98（向上取整）", plan.stop_price, 98.0)
    check("多单 止盈 = 104（向下取整）", plan.tp_price, 104.0)
    check("多单 1:2（止盈距=2×止损距）",
          approx((plan.tp_price - 100.0), 2 * (100.0 - plan.stop_price)), True)
    # 空单镜像
    pol2 = LayeredExitPolicy({"use_atr": False,
                              "r_multiple_tp": 2.0,
                              "use_trailing": False})
    plan2 = pol2.plan(make_signal(Side.SHORT, 100.0, 101.0, 99.0, fractal_high=102.0), 100.0, state)
    check("空单 止损 = 102（向下取整）", plan2.stop_price, 102.0)
    check("空单 止盈 = 96（向上取整）", plan2.tp_price, 96.0)
    # P2 防护：陈旧信号，极值已越过入场价 → 止损必须仍在 entry 不利侧
    pol3 = LayeredExitPolicy({"use_atr": False})
    # 行情已涨（最低价 102 > 入场 100），fractal_low 缺省=0（哨兵）→ A=0 → R=0 → P2 压回
    plan3 = pol3.plan(make_signal(Side.LONG, 100.0, 105.0, 102.0), 100.0, state)
    check("P2 多单止损严格在 entry 下方", plan3.stop_price < 100.0, True)

    print("\n[2] L2 ATR 自适应宽窄（use_atr=True，喂 15 根 TR=2 的 K 线 → ATR=2）")
    pol4 = LayeredExitPolicy({"use_atr": True, "atr_period": 14,
                              "atr_sl_multiple": 2.0, "r_multiple_tp": 2.0,
                              "use_trailing": False})
    feed(pol4, 15)  # 每根 h=101,l=99,c=100 → TR=2 → ATR≈2 → R=4
    plan4 = pol4.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, state)
    check("ATR 路径 止损 = 96（R=2×ATR=4）", plan4.stop_price, 96.0)
    check("ATR 路径 止盈 = 108（2R=8）", plan4.tp_price, 108.0)
    check("ATR 路径 R 落盘", approx(plan4.params.get("R", 0), 4.0), True)

    print("\n[3] ATR 不可用（首根未喂）+ 无结构 → R=0 → P2 边界保护（止损压在入场价 1 tick 外）")
    pol5 = LayeredExitPolicy({"use_atr": True,
                              "r_multiple_tp": 2.0})
    plan5 = pol5.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, state)
    check("无 ATR/无结构 R=0 → 止损 = 99.8（P2 边界保护）", plan5.stop_price, 99.8)

    print("\n[3b] A 观测告警（2026-09-15 评审补 · 阈值放宽为 A < 3.0）：不改 R 口径，只打 WARNING 抓样本")
    # 口径（用户拍板）：有分型才有买卖点 → 入场时 A 恒 > 0，故 R 不设下限、不兜底。
    #   但 A 偏小时必须出声：评审后阈值由 A==0 放宽为 **A < 3.0**
    #   （= 已删 min_r_points 地板原值），三支文案便于 grep ——
    #   [R 结构距离缺失] / [R 结构距离归零] / [R 结构距离偏小]；另有 [R 归零]（R=0）。
    import logging as _logging

    class _CapLog(_logging.Handler):
        def __init__(self):
            _logging.Handler.__init__(self)
            self.msgs = []

        def emit(self, rec):
            self.msgs.append(rec.getMessage())

    _lg = _logging.getLogger(LayeredExitPolicy.__module__)
    _cap = _CapLog()
    _lg.addHandler(_cap)
    _old_lvl, _old_prop = _lg.level, _lg.propagate
    _lg.setLevel(_logging.WARNING)
    # ① 分型贴身（fractal_low == entry）→ A=0 → 打「归零」告警
    pol5w = LayeredExitPolicy({"use_atr": False,
                               "r_multiple_tp": 2.0})
    plan5w = pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                                    fractal_low=100.0), 100.0, state)
    check("A=0（分型贴身）→ R=0，止损仍走 P2 = 99.8（口径不变）",
          plan5w.stop_price, 99.8)
    check("A=0 → 打 WARNING 观测日志（[R 结构距离归零]）",
          any("R 结构距离归零" in m for m in _cap.msgs), True)
    # ② 信号未携带分型（fractal 哨兵 0）→ 打「缺失」告警（与「归零」区分开，便于 grep）
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0), 100.0, state)
    check("未带分型 → 打 WARNING [R 结构距离缺失]",
          any("R 结构距离缺失" in m for m in _cap.msgs), True)
    check("两支告警文案不混（缺失 ≠ 归零）",
          any("R 结构距离归零" in m for m in _cap.msgs), False)
    # ③ A > 0 正常路径 → 不打告警
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                           fractal_low=97.0), 100.0, state)
    check("A=3.0 正常路径 → 不打告警（阈值严格小于）", _cap.msgs, [])
    # ④ 阈值放宽后的边界（评审 · 用户拍板「改为 A < 3.0」）：
    #    A=2.9 → 必须出声；A=3.0 → 必须安静。这两条把"风险窗口 0 < A < 地板"的
    #    内部真正钉住 —— 原实现只测 A=0 这个极端点，区间内部全是盲区。
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                           fractal_low=97.1), 100.0, state)
    check("A=2.9 < 阈值 3.0 → 打 WARNING [R 结构距离偏小]",
          any("R 结构距离偏小" in m for m in _cap.msgs), True)
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                           fractal_low=97.0), 100.0, state)
    check("A=3.0 == 阈值 → 严格小于，不打告警", _cap.msgs, [])
    # ⑤ R 归零观测（评审补 · 用户要求"R=0 加控制台告警"）：
    #    R > 0 时不得出 [R 归零]（与 [R 结构距离偏小] 严格分开）；R = 0 时必须出。
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                           fractal_low=97.1), 100.0, state)
    check("A=2.9（R>0）→ 不打 [R 归零]（两支文案不混）",
          any("R 归零" in m for m in _cap.msgs), False)
    _cap.msgs = []
    pol5w.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                           fractal_low=100.0), 100.0, state)
    check("A=0 且无 ATR（R=0）→ 打 [R 归零] 控制台告警",
          any("R 归零" in m for m in _cap.msgs), True)
    _lg.removeHandler(_cap)
    _lg.setLevel(_old_lvl)
    _lg.propagate = _old_prop

    print("\n[4] 触发判定只看收盘价（同根 low/high 双破 ≠ 触发）")
    # 旧口径：bar.low=89（破止损 90）+ bar.high=111（破止盈 110）→ 取 sl（悲观兜底）。
    # 2026-09-22 新口径：价格输入只有 bar.close —— 本根 close=100 两侧都没穿 → **不触发**；
    #   "同根双破取悲观"在收盘价口径下不可达，规则已随口径删除（见 Exit.py 模块 docstring）。
    pos = make_position(Side.LONG, 100.0, 90.0, 110.0)
    chk = LayeredExitPolicy({}).check(pos, make_bar(2000, 100, 111, 89, 100), state, 5)
    check("low/high 双穿但收盘在区间内 → 不触发", chk, None)
    chk_sl = LayeredExitPolicy({}).check(pos, make_bar(2001, 100, 111, 89, 89), state, 5)
    check("同一根若收盘 89 ≤ 止损 90 → 触发 sl",
          chk_sl.reason if chk_sl else None, "sl")
    check("触发价 = 止损线（不是收盘价）",
          chk_sl.price if chk_sl else None, 90.0)

    print("\n[5] L3 保本：浮盈 ≥ 1R 抬止损至保本（only_update）")
    pol6 = LayeredExitPolicy({"use_atr": False,
                              "use_trailing": True, "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.0, "r_multiple_tp": 99.0})
    pos6 = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    # close=111 → 浮盈 11 ≥ 1R(10) → 保本位=100 > 90 → 更新
    chk6 = pol6.check(pos6, make_bar(2100, 100, 111, 100, 111), state, 5)
    check("保本触发 only_update", chk6.only_update if chk6 else None, True)
    check("保本新止损 = 100", chk6.plan.stop_price if chk6 else None, 100.0)
    # 缓冲 >0：SL 抬到入场价之上 breakeven_buffer_r×R（0.5R，与品种/周期解耦）
    pol6b = LayeredExitPolicy({"use_atr": False,
                               "use_trailing": True,
                               "breakeven_trigger_r": 1.0, "breakeven_buffer_r": 0.5,
                               "r_multiple_tp": 99.0})
    pos6b = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    chk6b = pol6b.check(pos6b, make_bar(2101, 100, 111, 100, 111), state, 5)
    check("保本缓冲 0.5R → 止损=105（入场价之上 0.5R=5）",
          chk6b.plan.stop_price if chk6b else None, 105.0)
    # 缓冲默认值 = 0.5R 的**行为层钉子**（评审补 · 用户要求
    #   "盯死 buffer 为 0.5R，避免后续被改"）：**不显式传 breakeven_buffer_r**，
    #   走 config 单一事实源的默认值，断言落点 = 入场价 + 0.5×R。
    #   配置层默认值另由 [8] 钉住 → 双保险：改默认值这里红，改落点公式这里也红。
    pol6c = LayeredExitPolicy({"use_atr": False,
                               "use_trailing": True, "breakeven_trigger_r": 1.0,
                               "r_multiple_tp": 99.0})
    pos6c = make_position(Side.LONG, 100.0, 90.0, 120.0, params={"R": 10.0, "_trail_best": 100.0})
    chk6c = pol6c.check(pos6c, make_bar(2102, 100, 111, 100, 111), state, 5)
    check("缓冲默认值（未显式传）= 0.5R → 止损 = 入场价 + 0.5×10 = 105",
          chk6c.plan.stop_price if chk6c else None, 105.0)

    print("\n[6] L3 跟踪：浮盈 ≥ 2R 启动跟踪（trail_dist = trailing_trigger_r × R = 0.5×10 = 5）")
    pol7 = LayeredExitPolicy({"use_atr": False,
                              "use_trailing": True, "breakeven_trigger_r": 1.0,
                              "breakeven_buffer_r": 0.0, "r_multiple_tp": 2.0})
    # 已先保本到 100；本根 close=130（浮盈30≥2R=20），跟踪距离 = 0.5R = 5 → 跟踪=131-5=126
    # 用 tp=9999 排除止盈线干扰，low=101>保本止损100 排除止损线干扰，只验跟踪
    pos7 = make_position(Side.LONG, 100.0, 100.0, 9999.0, params={"R": 10.0, "_trail_best": 131.0})
    chk7 = pol7.check(pos7, make_bar(2200, 100, 131, 101, 130), state, 5)
    check("跟踪触发 only_update", chk7.only_update if chk7 else None, True)
    check("跟踪新止损 = 126", chk7.plan.stop_price if chk7 else None, 126.0)
    check("跟踪极值 _trail_best 落盘", chk7.plan.params.get("_trail_best"), 131.0)

    print("\n[7] 全部关闭时（use_atr/use_trailing 均 False）只判止损线 / 止盈线")
    pol10 = LayeredExitPolicy({"use_atr": False,
                               "use_trailing": False})
    pos10 = make_position(Side.LONG, 100.0, 90.0, 120.0)
    chk10 = pol10.check(pos10, make_bar(2500, 100, 105, 95, 100), state, 100)
    check("止损线 / 止盈线均未触发 → 返回 None", chk10 is None, True)

    print("\n[8] T4: 裸构造默认值 = config.py 单一事实源")
    pol12 = LayeredExitPolicy()
    check("r_multiple_tp 默认 = config 2.0", pol12.r_multiple_tp, 2.0)
    check("atr_period 默认 = config 14", pol12.atr_period, 14)
    check("trailing_trigger_r 默认 = config 0.5", pol12.trailing_trigger_r, 0.5)
    check("use_trailing 默认 = True（跟踪止盈模式）", pol12.use_trailing, True)
    # 评审补 · 用户要求：把"保本缓冲 = 0.5R"钉死，避免后续被顺手改掉。
    #   三层钉子：① 配置层默认值（此处）② 行为层落点（[5]）③ 跨品种 resolved
    #   一致性（test_p45 / p46 / p47 / test_period_profile 已各自断言）。
    check("breakeven_buffer_r 默认 = 0.5（锁定半 R，全局不随品种）",
          pol12.p.breakeven_buffer_r, 0.5)
    check("breakeven_trigger_r 默认 = 1.0（缓冲 < 触发的不变式基准）",
          pol12.p.breakeven_trigger_r, 1.0)
    check("r_alert_a_floor 默认 = 3.0（A 告警灵敏度，取自被删 min_r_points 原值）",
          pol12.r_alert_a_floor, 3.0)

    print("\n[8b] 跟踪止盈模式（use_trailing=True 默认）：不落固定止盈单，止盈交给 L3 跟踪")
    polB = LayeredExitPolicy({"use_atr": False,
                              "r_multiple_tp": 2.0})
    planB = polB.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0, fractal_low=98.0), 100.0, state)
    check("跟踪止盈模式 tp_price = None（不落固定止盈单）", planB.tp_price is None, True)
    check("跟踪止盈模式 止损仍照常 = 98", planB.stop_price, 98.0)

    # [12] / [12b] 旧用例已删（2026-09-22 · 用户拍板）
    # ────────────────────────────────────────────────────────────────
    # 原两节把 L3 的浮盈口径钉在**根内极值**上（`best = max(best, bar.high)`），
    # 且显式声明"旧收盘口径应红"：[12] 用"盘中冲高 2R、收盘回落 1.2R 也抬损"钉口径，
    # [12b] 用 `_l3_started(pol, high, ts)` 把"high 抬多高"当浮盈，再断言 IC=3R / IF=2R
    # 的启动阈值边界。
    # 2026-09-22 口径统一为"整层 L1-L3 只读 bar.close"（用户拍板）→ 两节整体作废并删除。
    # 等价守护搬到了 `Trading/Test/test_p61_exit_close_only.py`：
    #   · L3 浮盈只看收盘价（冲高回落不算达标）；
    #   · L3 启动阈值仍严格 = r_multiple_tp（2.99R 不启动 / 3.0R 恰好启动，用收盘价重述）。
    # 旧实现与旧断言见 git 历史（本次改动前的版本）。

    print("\n[9] A=分型极值结构止损：R = max(A, 2×ATR)"
          "（min_r_points 地板已删，且不再补任何下限）")
    # 做多：fractal_low=97（底分型最低点），entry=100，use_atr=False → A=3
    pol20 = LayeredExitPolicy({"use_atr": False,
                               "r_multiple_tp": 2.0,
                               "use_trailing": False})
    plan20 = pol20.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                                    fractal_low=97.0), 100.0, state)
    check("做多 A=entry−fractal_low=3 → 止损=97", plan20.stop_price, 97.0)
    check("做多 止盈 = entry+2R = 106", plan20.tp_price, 106.0)
    # 做空：fractal_high=103（顶分型最高点），entry=100 → A=3
    plan21 = pol20.plan(make_signal(Side.SHORT, 100.0, 101.0, 99.0,
                                    fractal_high=103.0), 100.0, state)
    check("做空 A=fractal_high−entry=3 → 止损=103", plan21.stop_price, 103.0)
    check("做空 止盈 = entry−2R = 94", plan21.tp_price, 94.0)
    # max(A, 2×ATR)：A=3、2×ATR=4 → R=4（2×ATR 更大）
    pol22 = LayeredExitPolicy({"use_atr": True, "atr_period": 14,
                               "atr_sl_multiple": 2.0, "r_multiple_tp": 2.0,
                               })
    feed(pol22, 15)  # TR=2 → ATR≈2 → 2×ATR=4
    plan22 = pol22.plan(make_signal(Side.LONG, 100.0, 101.0, 99.0,
                                    fractal_low=97.0), 100.0, state)
    check("max(A=3, 2×ATR=4)=4 → 止损=96", plan22.stop_price, 96.0)
    # max(A, 2×ATR)：A ≤ 0（行情已穿越分型）且无 ATR → R=0，由 P2 守卫压在入场价外 1 tick
    plan23 = pol20.plan(make_signal(Side.LONG, 100.0, 99.0, 98.0,
                                    fractal_low=102.0), 100.0, state)
    check("A≤0 且无 ATR → R=0", approx(plan23.params.get("R", 0), 0.0), True)
    check("A≤0 且无 ATR → 止损 = 入场−1tick = 99.8（P2 边界保护）",
          plan23.stop_price, 99.8)

    print("\n[10] 构造期参数校验（2026-09-15 评审补 · Config.ExitConfig._check_exit_param_order）")
    # ① breakeven_buffer_r < breakeven_trigger_r：
    #    缓冲 ≥ 触发时，保本位会落在**当前浮盈之上**（浮盈 1.1R 却把止损抬到 1.5R），
    #    下一根 bar 立刻触发止损离场 —— 旧实现用 tick 计量，天然越不过 trigger。
    _be_err = ""
    try:
        LayeredExitPolicy({"breakeven_trigger_r": 1.0, "breakeven_buffer_r": 1.5})
    except Exception as e:  # pydantic ValidationError
        _be_err = str(e)
    check("breakeven_buffer_r(1.5) ≥ trigger(1.0) → 构造期报错（保本止损会越过市价）",
          "breakeven_buffer_r" in _be_err, True)
    _be_eq_err = ""
    try:
        LayeredExitPolicy({"breakeven_trigger_r": 1.0, "breakeven_buffer_r": 1.0})
    except Exception as e:
        _be_eq_err = str(e)
    check("buffer == trigger 同样报错（边界必须是严格小于）",
          "breakeven_buffer_r" in _be_eq_err, True)
    _be_ok_err = ""
    try:
        LayeredExitPolicy({"breakeven_trigger_r": 1.0, "breakeven_buffer_r": 0.5})
    except Exception as e:
        _be_ok_err = str(e)
    check("默认口径 buffer(0.5) < trigger(1.0) 正常构造", _be_ok_err, "")
    # trigger=0 = 关闭保本层，那一层根本不跑 → 不做大小校验
    LayeredExitPolicy({"breakeven_trigger_r": 0.0, "breakeven_buffer_r": 0.5})
    # ② 已删除的 min_r_points 不能"悄悄复活"：ExitConfig 是 extra=forbid，
    #    显式写旧键必须报错（防止有人照着旧文档/旧 .env 把地板加回来）
    _mr_err = ""
    try:
        LayeredExitPolicy({"min_r_points": 3.0})
    except Exception as e:  # pydantic ValidationError（extra=forbid）
        _mr_err = str(e)
    check("显式写已删除的 min_r_points → 构造期报错（防地板悄悄复活）",
          "min_r_points" in _mr_err, True)
    # ③ 同理：stop_at_signal_extreme 开关删除（R 口径唯一 = max(分型, 2×ATR)）
    _sse_err = ""
    try:
        LayeredExitPolicy({"stop_at_signal_extreme": False})
    except Exception as e:  # pydantic ValidationError（extra=forbid）
        _sse_err = str(e)
    check("显式写已删除的 stop_at_signal_extreme → 构造期报错（防开关悄悄复活）",
          "stop_at_signal_extreme" in _sse_err, True)

    print("\n" + "=" * 60)
    print("结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
