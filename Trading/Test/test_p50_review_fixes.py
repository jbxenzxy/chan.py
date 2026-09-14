# -*- coding: utf-8 -*-
"""
P50 · 2026-09-14 评审问题修复 契约测试
=======================================
把 `09132 → custom-dev(0914)` 评审里列的 3 个 P1 + 4 个 P2 **逐条钉死**，
防止回潮（这些全是"改动没写错、但边界形态不对"的问题，靠人眼 review 抓不住）。

  [1] P1-1 parse_product_key：真实月份合约写法（CFFEX.IF2609）能查到档案
  [2] P1-1 + P2-1 assert_product_allowed：白名单单一事实源 + 引擎不再误杀
  [3] P2-2 ProductProfile kw_only：位置构造硬失败（原会静默错位到 price_tick）
  [4] P1-2 pulse 重试节流：不再每根 bar 阻塞 30s
  [5] P1-3 quote_partial 逃生舱档：只强制 tick/乘数，涨跌停缺失时降级并出声
  [6] P2-3 离线模式规格漂移对账：dry_run/replay 用错规格不再静默
  [7] P2-4 未知品种日志去重：Config 侧不再重复打 WARNING
  [8] 死字段清理（2026-09-14）：InstrumentSpec 不含从未被消费的 max_order_volume

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_p50_review_fixes.py
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_RROOT = os.path.dirname(os.path.dirname(_HERE))     # Test/ → Trading/ → 仓库根
if _RROOT not in sys.path:
    sys.path.insert(0, _RROOT)

from Trading import main as _main                                      # noqa: E402
from Trading.Config import BrokerConfig, TradingConfig                 # noqa: E402
from Trading.Infra.InstrumentSpec import (                             # noqa: E402
    InstrumentSpec, InstrumentState)
from Trading.Infra.ProductProfile import (                             # noqa: E402
    PRODUCT_PROFILES, ProductProfile, assert_product_allowed,
    describe_unknown_product, parse_product, parse_product_key,
)

_PASS = 0
_FAIL = 0


def check(name, got, expected):
    global _PASS, _FAIL
    ok = got == expected
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r} expected={!r}".format(got, expected)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def check_true(name, got):
    global _PASS, _FAIL
    ok = bool(got)
    print(("✓" if ok else "✗") + " " + name +
          ("" if ok else "  -> got={!r}".format(got)))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


@contextmanager
def tmp_dir():
    d = tempfile.mkdtemp(prefix="p50_")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def build_engine(tmpdir, signal_symbol, broker=None):
    """最小装配引擎（与本目录 p46/p47 同款）。未知品种应抛 ValueError。"""
    from Trading import Broker  # noqa: F401  # 注册 dry_run broker
    from Trading.Broker.DryRun import DryRunBroker
    from Trading.Engine.Engine import TradingEngine
    from Trading.Infra.EventLog import EventLog
    from Trading.Infra.Store import Store
    from Trading.Strategy.Entry import EntryPolicy
    from Trading.Strategy.Exit import LayeredExitPolicy

    # Phase 3（Fix B）：品种档案播种改为启动路径上的一次显式调用。
    cfg = TradingConfig(instrument={"signal_symbol": signal_symbol})
    _main._seed_instrument(cfg)
    store = Store(os.path.join(tmpdir, "state.db"))
    ev = EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False, echo_kinds=None)
    b = broker if broker is not None else DryRunBroker(
        cfg.instrument, {"sim_equity": 10_000_000.0})
    return TradingEngine(cfg, b, EntryPolicy({"reverse_on_opposite_signal": False}),
                         LayeredExitPolicy(), store, ev)


# ══════════════════════════════════════════════════════════════════
# [1] P1-1：parse_product_key —— 剥合约月份后查档案
# ══════════════════════════════════════════════════════════════════
def t1_parse_product_key():
    print("\n[1] P1-1 parse_product_key：真实月份合约写法 → 品种键")
    check("key CFFEX.IF2609 → IF", parse_product_key("CFFEX.IF2609"), "IF")
    check("key SHFE.au2512 → AU", parse_product_key("SHFE.au2512"), "AU")
    check("key CZCE.TA501 → TA", parse_product_key("CZCE.TA501"), "TA")
    check("key DCE.i2601 → I（未标定 → 仍会被白名单拦）",
          parse_product_key("DCE.i2601"), "I")
    # 主连形态不受影响（回归 p45/p46/p47 既有断言）
    check("key KQ.m@CFFEX.IF → IF（主连不受影响）",
          parse_product_key("KQ.m@CFFEX.IF"), "IF")
    check("key KQ.m@SHFE.au → AU", parse_product_key("KQ.m@SHFE.au"), "AU")
    check("key KQ.m@CZCE.TA → TA", parse_product_key("KQ.m@CZCE.TA"), "TA")
    check("key 无点串 → ''", parse_product_key("IF2609"), "")
    check("key 空串 → ''", parse_product_key(""), "")
    check("key None → ''", parse_product_key(None), "")
    # 原 parse_product 语义必须保持不变（test_period_profile.py:114 断言 "IF2609"）
    check("parse_product(CFFEX.IF2609) 仍为 'IF2609'（既有断言不被打红）",
          parse_product("CFFEX.IF2609"), "IF2609")

    print("\n[1b] P1-1 回归：真实月份合约不再被白名单误杀（B 树会抛 ValueError）")
    for sym in ["CFFEX.IF2609", "SHFE.AU2512", "CZCE.TA501"]:
        with tmp_dir() as td:
            err = None
            try:
                build_engine(td, sym)
            except Exception as e:                       # noqa: BLE001
                err = "{}: {}".format(type(e).__name__, e)
            check("{} 引擎正常启动".format(sym), err, None)
    print("\n[1c] 未标定品种仍必须被拦（白名单没被改松）")
    for sym in ["CFFEX.IF2609X", "KQ.m@SHFE.rb", "DCE.i2601"]:
        with tmp_dir() as td:
            raised = None
            try:
                build_engine(td, sym)
            except ValueError as e:
                raised = str(e)
            check_true("{} 引擎拒绝启动（ValueError）".format(sym), raised)
            if raised:
                check("{} 异常列出支持清单".format(sym),
                      "AG/AU/CU/IC/IF/IH/IM/TA" in raised, True)


# ══════════════════════════════════════════════════════════════════
# [2] P1-1 + P2-1：白名单单一事实源
# ══════════════════════════════════════════════════════════════════
def t2_assert_product_allowed():
    print("\n[2] P2-1 assert_product_allowed：白名单判定 + 文案的唯一实现")
    check("allowed(CFFEX.IF2609) → IF", assert_product_allowed("CFFEX.IF2609"), "IF")
    check("allowed(KQ.m@CZCE.TA) → TA", assert_product_allowed("KQ.m@CZCE.TA"), "TA")
    for sym in ["KQ.m@SHFE.rb", "DCE.i2601", "IF2609"]:
        msg = None
        try:
            assert_product_allowed(sym)
        except ValueError as e:
            msg = str(e)
        check_true("allowed({}) 抛 ValueError".format(sym), msg)
        if msg:
            check("{} 文案与 describe_unknown_product 一致".format(sym),
                  msg, describe_unknown_product(sym))
            # 两条既有契约的措辞必须同时命中（p47 断言"禁止启动"、
            # p20 [9w3] 断言"拒绝启动交易引擎"）—— 单一文案的兼容底线
            check("{} 文案含'禁止启动'（p47 契约）".format(sym),
                  "禁止启动" in msg, True)
            check("{} 文案含'拒绝启动交易引擎'（p20 [9w3] 契约）".format(sym),
                  "拒绝启动交易引擎" in msg, True)

    # 引擎侧抛出的文案必须与 ProductProfile 的单一文案完全一致
    # （原 App/Engine 两份实现，措辞一个是"已拒绝"、一个是"禁止"）
    with tmp_dir() as td:
        eng_msg = None
        try:
            build_engine(td, "KQ.m@SHFE.rb")
        except ValueError as e:
            eng_msg = str(e)
    check("引擎 ValueError 文案 == assert_product_allowed 文案",
          eng_msg, describe_unknown_product("KQ.m@SHFE.rb"))


# ══════════════════════════════════════════════════════════════════
# [3] P2-2：ProductProfile kw_only
# ══════════════════════════════════════════════════════════════════
def t3_kw_only():
    print("\n[3] P2-2 ProductProfile(kw_only=True)：位置构造硬失败")
    # 字段顺序变动曾导致位置构造把 note 静默落进 price_tick（dataclass 不做类型
    # 校验）。加 kw_only 后位置构造必须 TypeError —— 与具体字段顺序解耦。
    err = None
    try:
        ProductProfile("IF", 2.0, 300.0, 0.2, "note")
    except TypeError as e:
        err = str(e)
    check_true("位置构造 ProductProfile(...) 抛 TypeError", err)
    # 关键字构造不受影响
    p = ProductProfile(product="IF", r_multiple_tp=2.0,
                       multiplier=300.0, price_tick=0.2, note="x")
    check("关键字构造 OK（price_tick 落对位置）", p.price_tick, 0.2)
    check("关键字构造 OK（note 落对位置）", p.note, "x")
    # 8 个档案条目仍然全部可构造（防 kw_only 改动把既有表打坏）
    check("PRODUCT_PROFILES 8 条全部可构造", len(PRODUCT_PROFILES), 8)


# ══════════════════════════════════════════════════════════════════
# [4] P1-2：pulse() 合约参数重试节流
# ══════════════════════════════════════════════════════════════════
def t4_pulse_throttle():
    print("\n[4] P1-2 pulse 重试节流（原：每根 bar 阻塞 30s）")
    from Trading.Broker.SimNow import SimNowBroker

    class _StubApi:
        def wait_update(self, deadline=None):
            return True

    def make_broker(start_tick=0):
        b = SimNowBroker.__new__(SimNowBroker)     # 跳过 __init__，不连 CTP
        b.params = BrokerConfig().model_dump()
        b._api = _StubApi()
        b._instrument_frozen = False
        b._instrument_retry_tick = start_tick
        b.calls = 0

        def _stub():
            b.calls += 1
        b._apply_instrument_quote = _stub            # 屏蔽真实（阻塞）取值
        return b

    N = SimNowBroker.INSTRUMENT_RETRY_EVERY_BARS
    check("节流间隔常量 = 20 根", N, 20)

    # 触发点：tick ∈ {1, N, 2N, 3N, 4N} → 4N 根 bar 内共 5 次
    b = make_broker(start_tick=0)                    # 启动期没试过
    for _ in range(4 * N):
        b.pulse()
    check("4N 根 bar：取值次数 = 首根 + 每 N 根一次 = 5", b.calls, 5)

    b2 = make_broker(start_tick=1)                   # _connect 已试过一次
    for _ in range(N - 2):                           # tick 走到 N-1
        b2.pulse()
    check("启动期已试过：第 2..N-1 根不再阻塞（0 次）", b2.calls, 0)
    b2.pulse()                                       # tick = N
    check("启动期已试过：第 N 根重试 1 次", b2.calls, 1)
    for _ in range(N - 1):
        b2.pulse()
    check("启动期已试过：再 N-1 根内不再重试（仍 1 次）", b2.calls, 1)

    b3 = make_broker(start_tick=0)
    b3._instrument_frozen = True                     # 取到即冻结
    for _ in range(2 * N):
        b3.pulse()
    check("冻结后完全空转（0 次）", b3.calls, 0)


# ══════════════════════════════════════════════════════════════════
# [5] P1-3：quote_partial 逃生舱档
# ══════════════════════════════════════════════════════════════════
def t5_quote_partial():
    print("\n[5] P1-3 quote_partial 逃生舱：只强制 tick/乘数，涨跌停缺失 → 降级并出声")
    check("policy 默认仍为 strict", BrokerConfig().instrument_fetch_policy, "strict")
    check("quote_partial 合法",
          BrokerConfig(instrument_fetch_policy="quote_partial")
          .instrument_fetch_policy, "quote_partial")
    for bad in ("prefer", "whatever", ""):
        err = None
        try:
            BrokerConfig(instrument_fetch_policy=bad)
        except Exception as e:                       # noqa: BLE001
            err = type(e).__name__
        check_true("非法档 {!r} 仍被拒（p42 既有断言不回退）".format(bad), err)

    nan = float("nan")

    class _Q:
        """tick/乘数就绪、涨跌停缺失（nan）—— 模拟不走 tqsdk 的自研通道。"""

        def __init__(self, hi=nan, lo=nan, tick=0.5, mult=200.0):
            # tick/乘数刻意与 InstrumentSpec 默认值（0.2 / 300）不同，
            # 便于断言"行情值确实落进了 spec"。
            self.symbol = "CFFEX.IF2609"
            self.price_tick = tick
            self.volume_multiple = mult
            self.upper_limit = hi
            self.lower_limit = lo

    # 5a InstrumentState.apply_quote 的 require_band 开关
    #   Phase 3（Fix B）：apply_quote 回填的是**运行时有效值**，故返回/断言
    #   一律针对 InstrumentState；InstrumentSpec 只存离线种子。
    s = InstrumentState(InstrumentSpec())
    ok = s.apply_quote(_Q(), require_band=False)
    check("partial：tick 已从行情填入", s.price_tick, 0.5)
    check("partial：乘数已从行情填入", s.multiplier, 200.0)
    check("partial：band 缺失 → 不填（保持 0 = 未知）", s.upper_limit, 0.0)
    check("partial：apply_quote 返回变更字段", sorted(ok), ["multiplier", "price_tick"])
    s2 = InstrumentState(InstrumentSpec())
    err = None
    try:
        s2.apply_quote(_Q())                          # 默认 require_band=True
    except ValueError as e:
        err = str(e)
    check_true("strict：band 缺失仍 fail-closed（抛 ValueError）", err)

    # partial 档下 band 可用时照样填（不是"一律不填"）
    s3 = InstrumentState(InstrumentSpec())
    s3.apply_quote(_Q(hi=5000.0, lo=4000.0), require_band=False)
    check("partial：band 可用时照样填", (s3.lower_limit, s3.upper_limit),
          (4000.0, 5000.0))

    # 5b 谓词
    from Trading.Broker.SimNow import _quote_params_ready
    q = _Q()
    check("ready(band 缺失, require_band=True) → False",
          _quote_params_ready(q, "CFFEX.IF2609")(), False)
    check("ready(band 缺失, require_band=False) → True",
          _quote_params_ready(q, "CFFEX.IF2609", require_band=False)(), True)

    # 5c 端到端：SimNow 走 partial 档 → verified 置位 + 降级告警出声
    from Trading.Broker.SimNow import SimNowBroker

    class _Api:
        def __init__(self, quote):
            self._q = quote

        def get_quote(self, sym):
            return self._q

        def wait_update(self, deadline=None):
            return True

    def make(quote, policy):
        b = SimNowBroker.__new__(SimNowBroker)
        b.spec = InstrumentSpec()
        b.params = BrokerConfig(instrument_fetch_policy=policy).model_dump()
        b.params["channel"]["instrument_fetch_timeout"] = 0.3
        b._api = _Api(quote)
        b._trade_symbol = "CFFEX.IF2609"
        b._instrument_frozen = False
        b._instrument_retry_tick = 0
        b._pending_alerts = None
        b._quote = None
        return b

    b = make(_Q(), "quote_partial")
    b._apply_instrument_quote()
    check("partial 档：verified 置位（不再永拒单）", b.state.verified, True)
    check("partial 档：band 缺失 → 来源标 QUOTE_PARTIAL",
          b.state.source, InstrumentState.SOURCE_QUOTE_PARTIAL)
    codes = [a.get("code") for a in (b.drain_alerts() or [])]
    check_true("partial 档：降级必须出声（instrument_band_degraded 告警）",
               "instrument_band_degraded" in codes)

    b2 = make(_Q(), "strict")
    b2._apply_instrument_quote()
    check("strict 档：band 缺失 → verified 保持 False（fail-closed 不回退）",
          b2.state.verified, False)

    b3 = make(_Q(hi=5000.0, lo=4000.0), "quote_partial")
    b3._apply_instrument_quote()
    check("partial 档：band 可用 → 来源标 QUOTE（不降级）",
          b3.state.source, InstrumentState.SOURCE_QUOTE)


# ══════════════════════════════════════════════════════════════════
# [6] P2-3：离线模式规格漂移对账
# ══════════════════════════════════════════════════════════════════
def t6_offline_drift():
    print("\n[6] P2-3 离线（dry_run/replay）规格漂移对账：用错规格不再静默")
    with tmp_dir() as td:
        eng = build_engine(td, "KQ.m@CFFEX.IF")      # DryRunBroker.is_offline = True
        check("离线引擎：verified 恒 False", eng.state.verified, False)
        eng._alerts = []
        eng._check_spec_drift()
        check("规格与档案一致 → 不告警",
              [a.get("code") for a in eng._alerts], [])
        # 手填一个与档案不符的乘数（= 行情取到的值与档案不一致）
        eng.state.multiplier = 999.0
        eng._alerts = []
        eng._spec_drift_offline_checked = False
        eng._check_spec_drift()
        codes = [a.get("code") for a in eng._alerts]
        check("离线条目规格漂移 → spec_drift_offline 告警",
              "spec_drift_offline" in codes, True)


# ══════════════════════════════════════════════════════════════════
# [7] P2-4：未知品种日志去重
# ══════════════════════════════════════════════════════════════════
def t7_log_dedup():
    print("\n[7] P2-4 未知品种不再双份告警（Config 侧 WARN → INFO）")

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.msgs = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    cap = _Cap()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.WARNING)
    try:
        TradingConfig(instrument={"signal_symbol": "KQ.m@SHFE.rb"})
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
    dup = [m for m in cap.msgs if "不在 PRODUCT_PROFILES" in m]
    check("未知品种不再产生 WARNING 级重复日志（引擎侧异常才是唯一用户可见文案）",
          dup, [])


def t8_dead_field_removed():
    """[8] 2026-09-14：死字段 `max_order_volume` 已删除，不许回潮。

    背景：该字段在 Phase 8 作为 §5.6「六个缺字段」之一加入，但**从未被消费**
    （全仓只有字段定义一处、0 处读取）。单笔手数的唯一来源是 `risk.max_volume`
    —— Config.py → Engine.lots_per_signal → Engine._open_volume；CZCE 钉 1 手
    由 Engine._open_volume 按交易所硬编码，与本字段无关。

    为什么必须钉死：这类"看起来像配置项、实际没有读取点"的字段是最危险的文档
    噪声 —— 下一个人会以为"手数上限在这里配"，改完发现没生效，再花时间排查。
    发现即删，并用断言拦住回潮。Q9（中金所限价单上限）仍未决，真要落地时按真实
    需求重新设计，不要靠恢复这个字段。
    """
    print("\n[8] 死字段 max_order_volume 已删除（InstrumentSpec 不含该字段）")
    fields = set(InstrumentSpec.model_fields)
    check("InstrumentSpec 不含 max_order_volume",
          "max_order_volume" in fields, False)
    # 顺带把 Phase 8 真正在用的字段点一遍，防止"删过头"：
    for keep in ("exchange", "limit_up_pct", "limit_down_pct",
                 "last_trade_date", "night_session"):
        check_true("InstrumentSpec 保留 Phase 8 在用字段 {}".format(keep),
                   keep in fields)
    # `extra="forbid"` 下老配置若仍写该键会构造失败 —— 确认这一行为是显式的
    # （报错而非静默忽略），这样"配置文件没清干净"能被立刻发现。
    _raised = False
    try:
        InstrumentSpec(max_order_volume=1)
    except Exception:
        _raised = True
    check_true("老配置残留该键 → 构造期显式报错（extra=forbid，非静默）", _raised)

    # ── Phase 3（Fix B）完成判据：静态规格**不含**任何运行时可变状态字段 ──
    #   这是乱源②（InstrumentSpec 三重身份）被消掉的**可执行证据**：
    #   配置对象 TradingConfig.instrument 构造后只读，行情回填一律落在
    #   InstrumentState 上。若有人把 did 字段加回 spec，本断言立刻红。
    print("\n[8b] Phase 3 拆分的完成判据：运行时字段已在 spec 上消失")
    runtime_fields = ("instrument_verified", "instrument_source", "fee_source",
                      "upper_limit", "lower_limit")
    for f in runtime_fields:
        check("InstrumentSpec 不含运行时字段 {}".format(f), f in fields, False)
    # 反向：这些字段必须真的存在于 InstrumentState（防止"删了但没搬走"）
    st = InstrumentState(InstrumentSpec())
    for f in ("verified", "source", "fee_source", "upper_limit", "lower_limit"):
        check_true("InstrumentState 拥有运行时字段 {}".format(f),
                   f in vars(st))
    # 静态项仍在 spec 上（Phase 3 只搬运行时，没顺手搬静态）
    for keep in ("signal_symbol", "trade_symbol", "price_tick", "multiplier",
                 "open_fee_rate", "close_fee_rate", "closetoday_fee_rate",
                 "slippage_ticks", "order_advanced", "closetoday_first"):
        check_true("静态项仍留 InstrumentSpec：{}".format(keep), keep in fields)
    # 定价/成本五方法也归 state（读的是有效 tick / 有效费率）
    for m in ("round_price", "align_entry", "align_exit", "slip_price",
              "cost_points", "points_to_cash"):
        check_true("定价/成本方法已迁到 InstrumentState：{}".format(m),
                   callable(getattr(st, m, None)))
        check("InstrumentSpec 不再有同名方法：{}".format(m),
              hasattr(InstrumentSpec, m), False)
    # 静态判定**留** spec（不读任何有效值）
    for m in ("effective_order_advanced", "delivery_guard_blocked", "is_new_day"):
        check_true("静态判定仍留 InstrumentSpec：{}".format(m),
                   callable(getattr(InstrumentSpec, m, None)))
    check_true("supports_closetoday 仍是 InstrumentSpec 的静态属性",
               isinstance(InstrumentSpec.supports_closetoday, property))


def main():
    t1_parse_product_key()
    t2_assert_product_allowed()
    t3_kw_only()
    t4_pulse_throttle()
    t5_quote_partial()
    t6_offline_drift()
    t7_log_dedup()
    t8_dead_field_removed()
    print("\n============================================================")
    print("P50 评审问题修复 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
    print("============================================================")
    raise SystemExit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
