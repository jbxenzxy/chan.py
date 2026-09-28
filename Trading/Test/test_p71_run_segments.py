# -*- coding: utf-8 -*-
"""运行态保护价**分段历史**（run segments）—— 行为护栏（2026-09-28）。

需求（用户 2026-09-28）：期货开仓后先画止损线；触发 1R 画保本线；触发 2R
（盈亏比跟品种档案）画初始跟踪线；创新高后画移动跟踪线 —— 画的是引擎
**实际发生**的分层过程（入场价 = 实际成交价），不是右键推演的假设口径。
分段/标签口径照《股票页「止盈止损」v1.7》：每段左端 = 开仓 / 触发抬价 /
创新高那根 bar；末段延伸图右缘（前端）。

各断言防什么：
  · [1] 开仓即记初始止损段（phase=sl、价位=计划保护价、左端=开仓 bar）；
  · [2] 未触发抬价的 bar 不出新段（段数不膨胀）；
  · [3] 浮盈 > 1R（严格不等）→ breakeven 段（价位 = 锚 ± 缓冲×R 经 tick
    对齐，左端 = 触发根）；
  · [4] 浮盈 > 盈亏比×R → trailing 段（初始跟踪）；此后每根新高 bar 一段
    （移动跟踪，左端 = 新高根）；
  · [5] 投影 run.segments 带 end_date 链（= 下一段左端；末段 = 最近 bar），
    run.wlr = plan.params 的盈亏比 —— 与 API 侧投影同名同源（p62 [11]）；
  · [6] 段历史随 run kv 持久化：重启恢复后分段不丢；
  · [7] 同根先抬价再离场：段落了但 run 结束整体清空（run=None → 线消失，
    与单线时代同语义）；段价位全部对齐品种 tick；
  · [10] 同层同价去重：跟踪层启动后每根新高 bar 都会出一份新计划，价位没
    变就不开新段（trailing_trigger_r=0 的常态 —— 不去重会把同一价位切
    成几十段）；
  · [11] 旧库升级（kv 无 segments 键）：恢复出来的 run 有计划没有段历史，
    而前端只认 segments —— 首根 bar 用那份计划补一段并落盘（否则升级后
    保护价线整条消失到下次抬价，旧版一直显示单线）。

数值说明：DryRun 成交价带一个 tick 滑点（信号 4550.0 → 成交 4550.2），
锚/R/阈值全部由引擎实际值经策略参数推出（不写死档位数值）。
跑法：python Trading/Test/test_p71_run_segments.py
"""
from __future__ import annotations

import io
import json
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
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
_REPO = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO)

from Trading.Broker.DryRun import DryRunBroker                   # noqa: E402
from Trading.Config import DEFAULT_CONFIG, TradingConfig         # noqa: E402
from Trading.Engine.Engine import TradingEngine                  # noqa: E402
from Trading.Infra.EventLog import EventLog                      # noqa: E402
from Trading.Infra.Instrument import Instrument, InstrumentConfig  # noqa: E402
from Trading.Infra.Product import PRODUCT_PROFILES               # noqa: E402
from Trading.Infra.Records import Bar, Signal                    # noqa: E402
from Trading.Infra.StateDB import Store                          # noqa: E402
from Trading.Strategy.Entry import EntryPolicy                   # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy              # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
_SYM = "CFFEX.IF2609"
_TICK = float(_IF.price_tick)
_INST = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)  # 与引擎同构造：round_price 作期望值
_RESOLVED_WLR = 2.0   # 本用例策略显式口径（与 build_engine 的构造一致）
_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


@contextmanager
def tmp_dir():
    import tempfile
    d = tempfile.mkdtemp(prefix="p71_")
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir, out_name="state.db"):
    """与 test_p62 同款离线引擎；策略显式带盈亏比（贴近生产 resolved 口径）。"""
    cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
    spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    store = Store(os.path.join(tmpdir, out_name))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    engine = TradingEngine(cfg, broker, EntryPolicy({}),
                           LayeredExitPolicy({"win_loss_ratio": 2.0}),
                           store, ev)
    return engine, store, broker, ev


def make_signal(price=4550.0, fractal_low=4500.0):
    return Signal(key=Signal.make_key("2026-09-01 09:35", "1", True),
                  symbol=_SYM, freq="5m", date="2026-09-01 09:35",
                  timestamp=900, bsp_type="1", is_buy=True, price=price,
                  high=price + 2.0, low=price - 2.0,
                  fractal_low=fractal_low, fractal_high=4560.0)


def make_bar(ts, date, o=4550.0, h=4552.0, l=4548.0, c=4551.0):
    return Bar(timestamp=ts, date=date, open=o, high=h, low=l, close=c, vol=1)


def run_view(engine):
    return engine.auto_order_status().get("run")


def main():
    print("\n[1] 开仓 → 初始止损段（左端 = 开仓 bar）")
    with tmp_dir() as tmp:
        engine, store, broker, ev = build_engine(tmp)
        engine.on_bar(make_bar(1000, "2026-09-01 09:40"))
        engine.on_signal(make_signal())
        rv = run_view(engine)
        segs = rv["segments"]
        _anchor = engine._run_anchor                    # 实际成交价（含滑点）
        _R = engine._run_plan.params["R"]               # R 快照（锚 − 分型低点）
        _pol = engine.exit_policy
        check("[1a] 恰 1 段", len(segs), 1)
        check("[1b] phase=sl / price=计划保护价（锚 − R 经 tick 对齐）/ 左端=开仓 bar",
              (segs[0]["phase"], segs[0]["price"], segs[0]["start_date"]),
              ("sl", _INST.round_price(_anchor - _R, "up"), "2026-09-01 09:40"))
        check("[1c] 末段 end_date = 最近 bar（右端由前端延伸）",
              segs[0]["end_date"], "2026-09-01 09:40")
        check("[1d] run.wlr = plan.params 的盈亏比", rv["wlr"], 2.0)

        print("\n[2] 未触发抬价的 bar 不出新段")
        engine.on_bar(make_bar(2000, "2026-09-01 09:45"))
        check("[2] 段数仍为 1", len(run_view(engine)["segments"]), 1)

        print("\n[3] 浮盈 > 1R（严格不等）→ breakeven 段（左端 = 触发根）")
        # 触发根的高点 = 锚 + 1R + 0.8（严格大于阈值且避开浮点噪声）
        _h3 = _anchor + _pol.breakeven_trigger_r * _R + 0.8
        engine.on_bar(make_bar(3000, "2026-09-01 09:50",
                               o=4550.0, h=_h3, l=4548.0, c=4590.0))
        segs = run_view(engine)["segments"]
        _be_exp = _INST.round_price(
            _anchor + _pol.breakeven_buffer_r * _R, "up")
        check("[3a] 恰 2 段，第二段 phase=breakeven",
              [s["phase"] for s in segs], ["sl", "breakeven"])
        check("[3b] 保本位 = round_up(锚 ± 缓冲×R)（tick 对齐）", segs[1]["price"],
              _be_exp)
        check("[3c] 左端 = 触发抬价那根 bar", segs[1]["start_date"],
              "2026-09-01 09:50")

        print("\n[4] 浮盈 > 盈亏比×R → 初始跟踪段；创新高 → 移动跟踪段")
        _h4 = _anchor + _RESOLVED_WLR * _R + 0.8         # 越过跟踪阈值
        _tr1_exp = _INST.round_price(
            _h4 - _pol.trailing_trigger_r * _R, "up")
        engine.on_bar(make_bar(4000, "2026-09-01 09:55",
                               o=4590.0, h=_h4, l=4548.0,
                               c=_tr1_exp + 2 * _TICK))  # 收盘在新保护价上方 → 存活
        _h5 = _h4 + 30.0                                 # 再创新高 → 移动跟踪
        _tr2_exp = _INST.round_price(
            _h5 - _pol.trailing_trigger_r * _R, "up")
        engine.on_bar(make_bar(5000, "2026-09-01 10:00",
                               o=_tr1_exp + 2 * _TICK, h=_h5,
                               l=4548.0, c=_tr2_exp + 2 * _TICK))
        segs = run_view(engine)["segments"]
        check("[4a] 段序列 = sl → breakeven → trailing → trailing",
              [s["phase"] for s in segs],
              ["sl", "breakeven", "trailing", "trailing"])
        check("[4b] 初始跟踪位 = round_up(新高 − 跟踪距离)（tick 对齐）",
              segs[2]["price"], _tr1_exp)
        check("[4c] 移动跟踪位（创新高 bar 新起一段）/ 左端 = 新高根",
              (segs[3]["price"], segs[3]["start_date"]),
              (_tr2_exp, "2026-09-01 10:00"))

        print("\n[5] 投影 end_date 链 = 下一段左端；末段 = 最近 bar")
        check("[5] end_date 链",
              [s["end_date"] for s in segs],
              ["2026-09-01 09:50", "2026-09-01 09:55", "2026-09-01 10:00",
               "2026-09-01 10:00"])

        print("\n[6] 段历史随 run kv 持久化：重启恢复不丢")
        engine._persist()
        engine2, _, _, _ = build_engine(tmp, out_name="state.db")
        rv2 = run_view(engine2)
        check("[6a] 恢复后段数与内容一致", rv2["segments"], segs)
        check("[6b] 恢复后 wlr 一致", rv2["wlr"], 2.0)

        print("\n[7] 同根先抬价再离场：段落了、run 结束整体清空")
        engine.on_bar(make_bar(6000, "2026-09-01 10:05",
                               o=_tr2_exp + 2 * _TICK, h=_tr2_exp + 40.0,
                               l=4548.0, c=_tr2_exp - 5 * _TICK))
        check("[7a] 离场后 run 投影为 None（线消失，与单线时代同语义）",
              run_view(engine), None)
        check("[7b] run 段缓存与计划一并清空",
              (engine._run_segments, engine._run_plan), ([], None))

        print("\n[8] 段价位全部对齐品种 tick（{}）".format(_TICK))
        all_prices = [s["price"] for s in segs]
        check("[8] tick 对齐", all(abs(p / _TICK - round(p / _TICK)) < 1e-6
                                   for p in all_prices), True)

        print("\n[9] trailing_trigger_r=0：与正值同一公式（跟踪线贴最好极值）")
        with tmp_dir() as tmp:
            cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
            spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
            store = Store(os.path.join(tmp, "state.db"))
            ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False,
                          echo_kinds=None)
            engine = TradingEngine(cfg, DryRunBroker(spec, {"sim_equity": 1e6}),
                                   EntryPolicy({}),
                                   LayeredExitPolicy({"win_loss_ratio": 2.0,
                                                      "trailing_trigger_r": 0.0}),
                                   store, ev)
            engine.on_bar(make_bar(1000, "2026-09-01 09:40"))
            engine.on_signal(make_signal())
            anchor = engine._run_anchor
            R = engine._run_plan.params["R"]
            # 越阈根： favorable 极值越过盈亏比阈值，且**收盘 == 高点**（收盘
            # 恰在跟踪线上 → 严格不等 → 存活），观察段阶梯。
            _h3 = anchor + _RESOLVED_WLR * R + 0.8
            engine.on_bar(make_bar(2000, "2026-09-01 09:45",
                                   o=4550.0, h=_h3, l=4548.0, c=_h3))
            segs = run_view(engine)["segments"]
            # t=0 下保本抬价与跟踪抬价同根发生、保护价一步到极值 —— 保本位从未
            # 生效过，段历史如实记 sl → trailing（不虚构一个保本段）。
            check("[9a] 段序列 = sl → trailing（保本位被跟踪位同根超越，不虚构段）",
                  [s["phase"] for s in segs], ["sl", "trailing"])
            check("[9a2] 收盘恰在跟踪线上不触发（严格不等，run 仍在）",
                  run_view(engine) is not None, True)
            check("[9b] 初始跟踪位 = 最好极值本身（距离 0 贴极值，tick 对齐）",
                  segs[1]["price"], _INST.round_price(_h3, "up"))
            # 次根回落 → 任意回落即离场（保护价贴着极值）
            engine.on_bar(make_bar(3000, "2026-09-01 09:50",
                                   o=_h3, h=_h3 + 0.2, l=4548.0,
                                   c=_h3 - 5 * _TICK))
            check("[9c] 回落即离场（run 清空，与正值配置同语义）",
                  (run_view(engine), engine._run_segments), (None, []))

        # 同根先抬价再离场：越阈根收盘明显低于极值 → 当根离场，reason=trailing
        with tmp_dir() as tmp:
            cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
            spec = Instrument(InstrumentConfig(trade_symbol=_SYM), _IF)
            store = Store(os.path.join(tmp, "state.db"))
            ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False,
                          echo_kinds=None)
            engine = TradingEngine(cfg, DryRunBroker(spec, {"sim_equity": 1e6}),
                                   EntryPolicy({}),
                                   LayeredExitPolicy({"win_loss_ratio": 2.0,
                                                      "trailing_trigger_r": 0.0}),
                                   store, ev)
            engine.on_bar(make_bar(1000, "2026-09-01 09:40"))
            engine.on_signal(make_signal())
            anchor = engine._run_anchor
            R = engine._run_plan.params["R"]
            engine.on_bar(make_bar(2000, "2026-09-01 09:45",
                                   o=4550.0, h=anchor + _RESOLVED_WLR * R + 0.8,
                                   l=4548.0, c=4595.0))
            check("[9d] 越阈根收盘低于极值 → 先抬价到极值、当根即离场",
                  (run_view(engine), engine._run_segments), (None, []))
            engine.ev.flush()
            _upd = [r for r in (json.loads(l) for l in io.open(
                os.path.join(tmp, "events.jsonl"), encoding="utf-8"))
                if r.get("kind") == "exit_plan_update"]
            check("[9e] 抬价事件 reason=trailing、stop=极值（标签与画线价位一致）",
                  [(u.get("reason"), u.get("stop")) for u in _upd],
                  [("trailing", _INST.round_price(
                      anchor + _RESOLVED_WLR * R + 0.8, "up"))])

        print("\n[10] 同层同价去重：跟踪层不抬价时不开新段")
        with tmp_dir() as tmp:
            engine, store, broker, ev = build_engine(tmp)
            engine.on_bar(make_bar(1000, "2026-09-01 09:40"))
            engine.on_signal(make_signal())
            _plan = engine._run_plan
            _n0 = len(engine._run_segments)
            # 模拟跟踪层启动后每根新高 bar 都出一份新计划（价位未变）：
            #   trailing_trigger_r=0（贴极值）时这就是常态。
            _plan.params["_phase"] = "trailing"
            engine._record_run_segment(_plan, make_bar(2000, "2026-09-01 09:45"))
            _n1 = len(engine._run_segments)
            engine._record_run_segment(_plan, make_bar(3000, "2026-09-01 09:50"))
            _n2 = len(engine._run_segments)
            check("[10a] 开仓 1 段 + 跟踪层首记 1 段", (_n0, _n1), (1, 2))
            check("[10b] 同层同价再记 → 不开新段（去重）", _n2, 2)
            check("[10c] 去重保留首次记录那根 bar 作左端",
                  engine._run_segments[-1]["start_date"], "2026-09-01 09:45")
            _plan.stop_price = _INST.round_price(_plan.stop_price + _TICK, "up")
            engine._record_run_segment(_plan, make_bar(4000, "2026-09-01 09:55"))
            check("[10d] 同层但价位变了 → 开新段", len(engine._run_segments), 3)
            check("[10e] 新段左端 = 变价那根 bar",
                  engine._run_segments[-1]["start_date"], "2026-09-01 09:55")

        print("\n[11] 旧库升级（kv 无 segments）：首根 bar 补一段并落盘")
        with tmp_dir() as tmp:
            engine, store, broker, ev = build_engine(tmp)
            engine.on_bar(make_bar(1000, "2026-09-01 09:40"))
            engine.on_signal(make_signal())
            engine._persist()
            _stop0 = engine._run_plan.stop_price
            _phase0 = str(engine._run_plan.params.get("_phase") or "") or "sl"
            # 抹掉 segments 键 → 等价于旧版引擎写入的库（有 run、有 plan、无段历史）
            _raw = store.get_json("run")
            check("[11a] 新库落盘带 segments 键（对照）", "segments" in _raw, True)
            _raw.pop("segments", None)
            store.set_json("run", _raw)
            engine2, store2, _, _ = build_engine(tmp, out_name="state.db")
            check("[11b] 旧库恢复：段表为空（前端会整条不画）",
                  len(engine2._run_segments), 0)
            engine2.on_bar(make_bar(2000, "2026-09-01 09:45"))
            _segs = run_view(engine2)["segments"]
            check("[11c] 首根 bar 即补一段（保护价线恢复显示）", len(_segs), 1)
            check("[11d] 补的段 = 恢复出来的计划（价位 / 层，不虚构历史）",
                  (_segs[0]["price"], _segs[0]["phase"]), (_stop0, _phase0))
            check("[11e] 补的段左端 = 这根 bar（库里没有可对齐的更早日期）",
                  _segs[0]["start_date"], "2026-09-01 09:45")
            check("[11f] 补段已随 run kv 落盘（API 侧投影立即可见）",
                  len(engine2.store.get_json("run").get("segments") or []), 1)
            engine2.on_bar(make_bar(3000, "2026-09-01 09:50"))
            check("[11g] 只补一次（后续 bar 不再追加同层同价段）",
                  len(run_view(engine2)["segments"]), 1)

    print("\n" + "=" * 60)
    print("p71_run_segments: {} passed, {} failed".format(_PASS, _FAIL))
    print("=" * 60)
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
