# -*- coding: utf-8 -*-
"""
P42 品种参数从行情获取（A′ fail-closed · 契约测试，Phase 8 / D20）
================================================================
背景（文档 §5.9 / §6.2 Phase 8 / §7.3 p42）
----------------------------------------------------
换品种用错 tick 会直接导致 CTP 拒单（R20）。Phase 8 起，实盘的合约参数
（price_tick / volume_multiple→multiplier / 涨跌停区间）**唯一真值来源是行情**
（SimNow._apply_instrument_quote 从真实月份合约回填 spec），配置值只在离线
（dry_run / replay）生效且必须标记 CONFIG_OFFLINE。

"取不到正确值就不许下单"这条 fail-closed 语义必须由反例钉死 ——
否则实现极易退化成"静默回退"，而回退值错的时候损失不可逆。

覆盖（对应 §7.3 p42 用例 ①~⑦ + 涨跌停护栏）
  [1] ① apply_quote 覆盖 price_tick / multiplier / 涨跌停，返回变更列表，
      且超价随之变为 overprice_ticks × 新 tick；
  [1b] apply_quote 原子性：nan / 0 / 缺字段 / 区间不自洽 → ValueError 且一个字段都不改；
  [2] ② dry_run/replay 不走行情：来源标记 CONFIG_OFFLINE；
      在线 broker（SimNow/live）is_offline=False，离线 broker is_offline=True；
  [3] ③ 行情取不到（nan）→ verified 保持 False，**不回退配置值**；
  [4] ④ 行情值与配置不一致 → 以行情值为准（覆盖 + source=QUOTE）；
  [5] ⑤ 实盘未验证 → Engine._pre_trade_check 拒单 "instrument_unverified"
      + severe 告警；离线通道闸门放行；
  [6] ⑥ 取值即冻结：首次成功后二次推送（换值）不再改变 spec；
  [7] ⑦ 实盘配 instrument_fetch_policy="off" → verified 恒 False → 闸门拒单
      （调试开关不得绕过 A′）；policy 非法值 → 配置期报错；宽容档 prefer 已删除；
  [8] 涨跌停护栏：broker 侧最终限价出区间 → 本地拒单（恰在涨跌停价上放行、
      区间未知不校验）；引擎侧参考价粗检同理。

跑法：python Trading/Test/test_p42_price_from_quote.py
"""
from __future__ import annotations

import copy
import math
import os
import sys
import tempfile
from types import SimpleNamespace

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
sys.path.insert(0, os.path.dirname(_TG_ROOT))

from Trading.Broker.Base import Broker  # noqa: E402
from Trading.Broker.DryRun import DryRunBroker  # noqa: E402
from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
from Trading.Config import BrokerConfig, TradingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.InstrumentSpec import InstrumentSpec  # noqa: E402
from Trading.Infra.Store import Store  # noqa: E402
from Trading.Infra.Types import OrderIntent, Side  # noqa: E402
from Trading.Strategy.Entry import EntryPolicy  # noqa: E402
from Trading.Strategy.Exit import LayeredExitPolicy  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def check_true(name, cond, detail=""):
    check(name + ("（%s）" % str(detail) if detail else ""), bool(cond), True)


# ════════════════════════════════════════════════════════════════
# 测试替身：鸭子类型 quote / api（不 import tqsdk —— apply_quote 是纯数据方法）
# ════════════════════════════════════════════════════════════════
class FakeQuote:
    """模拟 tqsdk quote：字段缺省给"合法值"，用例里逐项破坏。"""

    def __init__(self, symbol="CFFEX.IF2609", price_tick=2.0,
                 volume_multiple=50.0, upper_limit=5000.0, lower_limit=4000.0):
        self.symbol = symbol
        self.price_tick = price_tick
        self.volume_multiple = volume_multiple
        self.upper_limit = upper_limit
        self.lower_limit = lower_limit


class FakeApi:
    def __init__(self, quote):
        self._q = quote

    def get_quote(self, symbol):
        return self._q

    def wait_update(self, deadline=None):
        return True


class FakeLiveBroker(Broker):
    """在线 broker 替身：is_offline=False，行为同 SimNow 的闸门语义。"""
    name = "fake_live"
    is_offline = False

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        raise NotImplementedError("P42 替身不应触发真实报单")


def make_simnow(quote, policy="strict", spec=None, frozen=False):
    """跳过 __init__（不连 CTP）手工装配 SimNowBroker，只填闸门路径用到的字段。"""
    b = SimNowBroker.__new__(SimNowBroker)
    b.spec = spec if spec is not None else InstrumentSpec()
    b.params = {"instrument_fetch_policy": policy, "overprice_ticks": 5,
                "channel": {"underlying_map_timeout": 0.5}}
    b._api = FakeApi(quote)
    b._trade_symbol = "CFFEX.IF2609"
    b._instrument_frozen = frozen
    return b


def make_engine(broker, spec, tmpdir):
    cfg = TradingConfig()
    cfg.instrument = spec
    alerts = []
    eng = TradingEngine(
        cfg, broker, EntryPolicy({}),
        LayeredExitPolicy(TradingConfig().exit_params.model_dump()),
        Store(os.path.join(tmpdir, "state.db")),
        EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                 echo_kinds=None))
    # 告警记录器：替换实例方法，断言"拒单必须伴随严重告警"（D11 通道）
    eng.alert = lambda level, code, msg, **extra: alerts.append((level, code))
    return eng, alerts


print("\nP42：品种参数从行情获取（A′ fail-closed）")

# ════════════════════════════════════════════════════════════════
print("\n[1] ① apply_quote 覆盖 + 变更列表 + 超价随新 tick 缩放")
spec1 = InstrumentSpec()                     # IF 基线：tick=0.2, multiplier=300
changed = spec1.apply_quote(FakeQuote())
check("[1a] price_tick 被覆盖为 2.0", spec1.price_tick, 2.0)
check("[1b] multiplier 被覆盖为 50（quote.volume_multiple）", spec1.multiplier, 50.0)
check("[1c] upper_limit / lower_limit 被覆盖", (spec1.upper_limit, spec1.lower_limit),
      (5000.0, 4000.0))
check_true("[1d] 返回值含 price_tick / multiplier / upper_limit / lower_limit",
           set(changed) == {"price_tick", "multiplier", "upper_limit", "lower_limit"},
           changed)
b1 = make_simnow(FakeQuote(), spec=spec1, frozen=True)
check("[1e] 超价随之变为 overprice_ticks × 新 tick = 5×2.0", b1._overprice(), 10.0)

print("\n[1b] apply_quote 原子性：非法输入 → ValueError 且一个字段都不改")
spec1b = InstrumentSpec()
_q_nan = FakeQuote(price_tick=float("nan"))
try:
    spec1b.apply_quote(_q_nan)
    check("[1b-1] nan tick → 抛 ValueError", False, True)
except ValueError:
    check("[1b-1] nan tick → 抛 ValueError", True, True)
check_true("[1b-2] nan 后 spec 一个字段都没改（原子性）",
           spec1b.price_tick == 0.2 and spec1b.multiplier == 300.0
           and spec1b.upper_limit == 0.0 and spec1b.lower_limit == 0.0)
for tag, q in (("[1b-3] tick=0", FakeQuote(price_tick=0.0)),
               ("[1b-4] 缺字段", SimpleNamespace(price_tick=2.0)),
               ("[1b-5] 区间不自洽", FakeQuote(upper_limit=4000.0, lower_limit=5000.0)),
               ("[1b-6] tick 为字符串", FakeQuote(price_tick="abc"))):
    s = InstrumentSpec()
    try:
        s.apply_quote(q)
        check("{} → 抛 ValueError".format(tag), False, True)
    except (ValueError, TypeError):
        check("{} → 抛 ValueError".format(tag), True, True)

# ════════════════════════════════════════════════════════════════
print("\n[2] ② 离线模式：不走行情，来源标记 CONFIG_OFFLINE")
check("[2a] DryRunBroker.is_offline = True", DryRunBroker.is_offline, True)
check("[2b] SimNowBroker.is_offline = False", SimNowBroker.is_offline, False)
check("[2c] Broker 基类默认 is_offline = False（未知通道保守受闸门管束）",
      Broker.is_offline, False)
spec2 = InstrumentSpec()
spec2.mark_config_offline()
check("[2d] 离线来源标记", spec2.instrument_source, "CONFIG_OFFLINE")
check("[2e] 默认 policy = strict", BrokerConfig().instrument_fetch_policy, "strict")

# ════════════════════════════════════════════════════════════════
print("\n[3] ③ 行情取不到（nan）→ 不回退配置值，verified 保持 False")
spec3 = InstrumentSpec()                     # 配置基线 tick=0.2
b3 = make_simnow(FakeQuote(price_tick=float("nan")), spec=spec3)
b3._apply_instrument_quote()
check("[3a] verified 仍为 False", spec3.instrument_verified, False)
check("[3b] tick 保持配置值 0.2（没有被行情 nan 污染，也**没有**被当作'成功'）",
      spec3.price_tick, 0.2)
check("[3c] source 未标 QUOTE", spec3.instrument_source, "")

# ════════════════════════════════════════════════════════════════
print("\n[4] ④ 行情值与配置不一致 → 以行情值为准 + source=QUOTE")
spec4 = InstrumentSpec()                     # 配置 tick=0.2 vs 行情 2.0
b4 = make_simnow(FakeQuote(), spec=spec4)
b4._apply_instrument_quote()
check("[4a] 以行情值为准 tick=2.0", spec4.price_tick, 2.0)
check("[4b] verified = True", spec4.instrument_verified, True)
check("[4c] source = QUOTE", spec4.instrument_source, "QUOTE")
check("[4d] 冻结标志置位", b4._instrument_frozen, True)

# ════════════════════════════════════════════════════════════════
print("\n[5] ⑤ 实盘未验证 → _pre_trade_check 拒单 + severe 告警；离线放行")
tmp5 = tempfile.mkdtemp(prefix="tg_p42_gate_")
spec5 = InstrumentSpec()                     # 未 verified
eng5, alerts5 = make_engine(FakeLiveBroker(spec5), spec5, tmp5)
act_past_close = _Action(intent=OrderIntent.CLOSE, side=Side.SHORT, volume=2,
                         target=SimpleNamespace(entry_date="2026-09-01", volume=2,
                                                side=Side.LONG, signal_key="X|buy|1"),
                         is_exit=True, transition=5)
why5 = eng5._pre_trade_check(act_past_close, "2026-09-02", None)
check("[5a] 实盘未验证 → 拒单原因 instrument_unverified",
      why5, "instrument_unverified")
check_true("[5b] 拒单伴随 severe 告警（D11）",
           ("severe", "instrument_unverified") in alerts5, alerts5)
# 对照：同样的校验链，离线通道放行（闸门不拦 dry_run）
spec5b = InstrumentSpec()
spec5b.mark_config_offline()
eng5b, alerts5b = make_engine(DryRunBroker(spec5b), spec5b,
                              tempfile.mkdtemp(prefix="tg_p42_off_"))
why5b = eng5b._pre_trade_check(act_past_close, "2026-09-02", None)
check("[5c] 离线通道闸门放行（走到后续校验，None = 全链通过）", why5b, None)
check("[5d] 离线通道不产生告警", alerts5b, [])

# ════════════════════════════════════════════════════════════════
print("\n[6] ⑥ 取值即冻结：二次推送（换值）不再改变 spec")
spec6 = InstrumentSpec()
q_v1 = FakeQuote(price_tick=2.0, volume_multiple=50.0,
                 upper_limit=5000.0, lower_limit=4000.0)
b6 = make_simnow(q_v1, spec=spec6)
b6._apply_instrument_quote()                 # 首次：取到并冻结
check("[6a] 首次取到 tick=2.0", spec6.price_tick, 2.0)
b6._api = FakeApi(FakeQuote(price_tick=3.0, volume_multiple=60.0,
                            upper_limit=6000.0, lower_limit=4500.0))
b6._apply_instrument_quote()                 # 二次推送：换月/异常推送场景
check("[6b] 冻结后二次推送不改 tick", spec6.price_tick, 2.0)
check("[6c] 冻结后二次推送不改乘数", spec6.multiplier, 50.0)
check("[6d] 冻结后二次推送不改涨跌停", (spec6.upper_limit, spec6.lower_limit),
      (5000.0, 4000.0))

# ════════════════════════════════════════════════════════════════
print("\n[7] ⑦ 实盘配 policy=off → 闸门拒单；非法档位配置期报错；prefer 已删除")
spec7 = InstrumentSpec()
b7 = make_simnow(FakeQuote(), policy="off", spec=spec7)
b7._apply_instrument_quote()                 # off：在线通道直接返回，不取不标
check("[7a] policy=off 在线通道 verified 恒 False", spec7.instrument_verified, False)
tmp7 = tempfile.mkdtemp(prefix="tg_p42_offlive_")
eng7, alerts7 = make_engine(FakeLiveBroker(spec7), spec7, tmp7)
why7 = eng7._pre_trade_check(act_past_close, "2026-09-02", None)
check("[7b] 实盘配 off → 闸门拒单（调试开关不得绕过 A′）",
      why7, "instrument_unverified")
check_true("[7c] 拒单伴随 severe 告警", ("severe", "instrument_unverified") in alerts7)
try:
    BrokerConfig(instrument_fetch_policy="prefer")
    check("[7d] 宽容档 prefer → 配置期报错", False, True)
except Exception:
    check("[7d] 宽容档 prefer → 配置期报错", True, True)
try:
    BrokerConfig(instrument_fetch_policy="whatever")
    check("[7e] 非法档位 → 配置期报错", False, True)
except Exception:
    check("[7e] 非法档位 → 配置期报错", True, True)
check("[7f] off 是合法配置值（离线专用）",
      BrokerConfig(instrument_fetch_policy="off").instrument_fetch_policy, "off")

# ════════════════════════════════════════════════════════════════
print("\n[8] 涨跌停护栏：broker 侧精确校验 + 引擎侧参考价粗检")
spec8 = InstrumentSpec()
spec8.apply_quote(FakeQuote())               # 区间 [4000, 5000]（apply_quote 是纯数据方法）
spec8.instrument_verified = True             # 模拟"已通过 SimNow 行情校验"→ 过闸门
b8 = make_simnow(FakeQuote(), spec=spec8, frozen=True)
check("[8a] 限价出上界 → 拒单原因", b8._price_out_of_band(5001.0) is not None, True)
check("[8b] 限价出下界 → 拒单原因", b8._price_out_of_band(3999.0) is not None, True)
check("[8c] 恰等于涨停价（区间内）→ 放行", b8._price_out_of_band(5000.0), None)
check("[8d] 区间内正常价 → 放行", b8._price_out_of_band(4500.0), None)
spec8_unknown = InstrumentSpec()             # 区间未知（离线）
b8b = make_simnow(FakeQuote(), spec=spec8_unknown, frozen=True)
check("[8e] 区间未知 → 不校验（放行）", b8b._price_out_of_band(99999.0), None)
# 引擎侧粗检：参考价出区间同样拦下（走 _pre_trade_check 校验链）
tmp8 = tempfile.mkdtemp(prefix="tg_p42_band_")
eng8, alerts8 = make_engine(FakeLiveBroker(spec8), spec8, tmp8)
why_out = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=5500.0)
check("[8f] 参考价 5500 出区间 → price_out_of_limit",
      str(why_out).startswith("price_out_of_limit"), True)
why_in = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=4500.0)
check("[8g] 参考价 4500 在区间 → 校验链通过（None）", why_in, None)
why_edge = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=5000.0)
check("[8h] 参考价恰等于涨停价 → 放行", why_edge, None)

print("\n" + "=" * 60)
print("P42 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
