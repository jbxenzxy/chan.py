# -*- coding: utf-8 -*-
"""
P42 品种参数从行情获取（A′ fail-closed · 契约测试，Phase 8 / D20）
================================================================
背景（文档 §5.9 / §6.2 Phase 8 / §7.3 p42）
----------------------------------------------------
换品种用错 tick 会直接导致 CTP 拒单（R20）。Phase 8 起，实盘的合约参数
（price_tick / volume_multiple→multiplier / 涨跌停区间）**唯一真值来源是行情**
（SimNow._apply_instrument_quote 从真实月份合约回填 Instrument），配置值
只在离线（dry_run / replay）生效且必须标记 CONFIG_OFFLINE。

"取不到正确值就不许下单"这条 fail-closed 语义必须由反例钉死 ——
否则实现极易退化成"静默回退"，而回退值错的时候损失不可逆。

Phase 3（Fix B · 2026-09-14）归属变更
----------------------------------------------------
P-B（2026-09-15）把 InstrumentSpec+InstrumentState 合并为唯一运行时对象 Instrument：
apply_quote 回填的 price_tick / multiplier / upper_limit / lower_limit 与
A′ 的 verified / source 落在 Instrument 上。本测试随之迁移，
并**新增**两条只有拆分后才存在的断言：
  · [1f] apply_quote 不改静态规格（spec.price_tick 恒为配置种子）；
  · [4e]/[5e] Engine 与 Broker 必须共用**同一份** state（否则 SimNow 置的
    verified 引擎读不到 → 闸门恒拒单，且原因极难查）。

覆盖（对应 §7.3 p42 用例 ①~⑦ + 涨跌停护栏）
  [1] ① apply_quote 覆盖 state 的 price_tick / multiplier / 涨跌停，返回变更列表，
      且超价随之变为 overprice_ticks × 新 tick；
  [1b] apply_quote 原子性：nan / 0 / 缺字段 / 区间不自洽 → ValueError 且一个字段都不改；
  [2] ② dry_run/replay 不走行情：来源标记 CONFIG_OFFLINE；
      在线 broker（SimNow/live）is_offline=False，离线 broker is_offline=True；
  [3] ③ 行情取不到（nan）→ verified 保持 False，**不回退配置值**；
  [4] ④ 行情值与配置不一致 → 以行情值为准（覆盖 + source=QUOTE）；
  [5] ⑤ 实盘未验证 → Engine._pre_trade_check 拒单 "instrument_unverified"
      + severe 告警；离线通道闸门放行；
  [6] ⑥ 取值即冻结：首次成功后二次推送（换值）不再改变 state；
  [7] ⑦ 实盘配 instrument_fetch_policy="off" → verified 恒 False → 闸门拒单
      （调试开关不得绕过 A′）；policy 非法值 → 配置期报错；宽容档 prefer 已删除；
  [8] 涨跌停护栏：broker 侧最终限价出区间 → 本地拒单（恰在涨跌停价上放行、
      区间未知不校验）；引擎侧参考价粗检同理；
  [8i-8m] Phase 8.1（B-1）：nan / inf 限价与参考价一律 fail-closed（原版
      nan 放行是 fail-open 隐患）；
  [9] Phase 8.1（O-2/O-3）：broker 侧故障诊断经 notify/drain_alerts 回流
      Engine.alert（D11 通道，§5.9.4 项 5 "Broker → Engine.alert()"）；
  [10] Phase 8.1（O-6）：组合反例 policy=off × 坏行情 → 仍 unverified + 拒单；
  （原 [11] Phase 8.1（O-1）「symbol → 交易所」对账已于 2026-09-16 整节删除：
    exchange 曾是纯备案字段（该字段已于 2026-09-16 B 批整体删除），
    对账只会给一个不参与逻辑的字段发噪音告警。）

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
from Trading.Config import (BrokerConfig, TradingConfig,  # noqa: E402
                            resolved_exit_params)
from Trading.Engine.Engine import TradingEngine, _Action  # noqa: E402
from Trading.Infra.EventLog import EventLog  # noqa: E402
from Trading.Infra.Instrument import (Instrument,  # noqa: E402
                                      InstrumentConfig)
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402


_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.StateDB import Store  # noqa: E402

from Trading.Infra.Records import OrderIntent, Side  # noqa: E402

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

    def _param(self, key, default=None):
        # 与 SimNowBroker._param 同语义（供 Engine 闸门 duck 读取 policy）
        return (self.params or {}).get(key, default)

    def submit(self, intent, side, volume, ref_price, signal_key="", note="",
               entry_date="", is_exit=False):
        raise NotImplementedError("P42 替身不应触发真实报单")


# Phase 3（Fix B）：本次运行的那一份运行时状态。生产路径由 main.py 建好后
# 同时交给 Broker 与 Engine（一次运行只有一份）；测试里同样显式共享同一个
# 对象，避免"两边各建一份 → verified 谁都看不见"的假绿灯。
def _st(spec=None) -> Instrument:
    # P-B：双类合并 —— 有效值初值直接取档案；spec/state 是同一个对象。
    return spec if spec is not None else Instrument(None, _IF)


def make_simnow(quote, policy="strict", spec=None, state=None, frozen=False):
    """跳过 __init__（不连 CTP）手工装配 SimNowBroker，只填闸门路径用到的字段。

    Phase 3：手工装配也要显式挂上 state（基类的惰性自建是给"直连 broker"
    用的便利路径，本测试要断言的现象都发生在 state 上，故显式给）。
    """
    b = SimNowBroker.__new__(SimNowBroker)
    b.state = spec if spec is not None else Instrument(None, _IF)
    b.state = state if state is not None else b.state
    b.params = {"instrument_fetch_policy": policy, "overprice_ticks": 5,
                "channel": {"underlying_map_timeout": 0.5,
                            "instrument_fetch_timeout": 0.5}}  # Phase 8.1（B-2）独立超时
    b._api = FakeApi(quote)
    b._trade_symbol = "CFFEX.IF2609"
    b._instrument_frozen = frozen
    return b


def make_engine(broker, spec, tmpdir, state=None):
    cfg = TradingConfig()
    cfg.instrument = spec
    alerts = []
    eng = TradingEngine(
        cfg, broker, EntryPolicy({}),
        LayeredExitPolicy(resolved_exit_params(TradingConfig())),
        Store(os.path.join(tmpdir, "state.db")),
        EventLog(os.path.join(tmpdir, "events.jsonl"), echo=False,
                 echo_kinds=None),
        state=state)
    # 告警记录器：替换实例方法，断言"拒单必须伴随严重告警"（D11 通道）
    eng.alert = lambda level, code, msg, **extra: alerts.append((level, code))
    return eng, alerts


print("\nP42：品种参数从行情获取（A′ fail-closed）")

# ════════════════════════════════════════════════════════════════
print("\n[1] ① apply_quote 覆盖 + 变更列表 + 超价随新 tick 缩放")
st1 = _st()                                   # IF 基线：tick=0.2, multiplier=300
changed = st1.apply_quote(FakeQuote())
check("[1a] price_tick 被覆盖为 2.0", st1.price_tick, 2.0)
check("[1b] multiplier 被覆盖为 50（quote.volume_multiple）", st1.multiplier, 50.0)
check("[1c] upper_limit / lower_limit 被覆盖", (st1.upper_limit, st1.lower_limit),
      (5000.0, 4000.0))
check_true("[1d] 返回值含 price_tick / multiplier / upper_limit / lower_limit",
           set(changed) == {"price_tick", "multiplier", "upper_limit", "lower_limit"},
           changed)
# P-B（2026-09-15）双类合并：原"静态规格一个字段都不动"断言随拆分消亡 ——
# spec 与 state 是同一对象，行情回填的就是唯一一份运行时有效值。
check("[1f] P-B：无双类影子 —— tick/乘数只有这一份（= 行情值，非配置种子）",
      (st1.price_tick, st1.multiplier), (2.0, 50.0))
b1 = make_simnow(FakeQuote(), spec=st1, state=st1, frozen=True)
check("[1e] 超价随之变为 overprice_ticks × 新 tick = 5×2.0", b1._overprice(), 10.0)

print("\n[1b] apply_quote 原子性：非法输入 → ValueError 且一个字段都不改")
st1b = _st()
_q_nan = FakeQuote(price_tick=float("nan"))
try:
    st1b.apply_quote(_q_nan)
    check("[1b-1] nan tick → 抛 ValueError", False, True)
except ValueError:
    check("[1b-1] nan tick → 抛 ValueError", True, True)
check_true("[1b-2] nan 后 state 一个字段都没改（原子性）",
           st1b.price_tick == 0.2 and st1b.multiplier == 300.0
           and st1b.upper_limit == 0.0 and st1b.lower_limit == 0.0)
for tag, q in (("[1b-3] tick=0", FakeQuote(price_tick=0.0)),
               ("[1b-4] 缺字段", SimpleNamespace(price_tick=2.0)),
               ("[1b-5] 区间不自洽", FakeQuote(upper_limit=4000.0, lower_limit=5000.0)),
               ("[1b-6] tick 为字符串", FakeQuote(price_tick="abc"))):
    s = _st()
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
st2 = _st()
st2.mark_config_offline()
check("[2d] 离线来源标记", st2.source, "CONFIG_OFFLINE")
check("[2e] 默认 policy = strict", BrokerConfig().instrument_fetch_policy, "strict")

# ════════════════════════════════════════════════════════════════
print("\n[3] ③ 行情取不到（nan）→ 不回退配置值，verified 保持 False")
st3 = _st()                                   # 配置基线 tick=0.2
b3 = make_simnow(FakeQuote(price_tick=float("nan")), spec=st3, state=st3)
b3._apply_instrument_quote()
check("[3a] verified 仍为 False", st3.verified, False)
check("[3b] tick 保持配置值 0.2（没有被行情 nan 污染，也**没有**被当作'成功'）",
      st3.price_tick, 0.2)
check("[3c] source 未标 QUOTE", st3.source, "")

# ════════════════════════════════════════════════════════════════
print("\n[4] ④ 行情值与配置不一致 → 以行情值为准 + source=QUOTE")
st4 = _st()                                   # 配置 tick=0.2 vs 行情 2.0
b4 = make_simnow(FakeQuote(), spec=st4, state=st4)
b4._apply_instrument_quote()
check("[4a] 以行情值为准 tick=2.0", st4.price_tick, 2.0)
check("[4b] verified = True", st4.verified, True)
check("[4c] source = QUOTE", st4.source, "QUOTE")
check("[4d] 冻结标志置位", b4._instrument_frozen, True)
check_true("[4e] Broker 与外部持有的是**同一份** state（写入可见）",
           b4.state is st4, (id(b4.state), id(st4)))

# ════════════════════════════════════════════════════════════════
print("\n[5] ⑤ 实盘未验证 → _pre_trade_check 拒单 + severe 告警；离线放行")
tmp5 = tempfile.mkdtemp(prefix="tg_p42_gate_")
st5 = _st()                                   # 未 verified
eng5, alerts5 = make_engine(FakeLiveBroker(st5, state=st5), st5, tmp5,
                            state=st5)
check_true("[5e] Engine 与 Broker 共用同一份 state（否则闸门恒拒单）",
           eng5.state is st5 and eng5.broker.state is st5)
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
st5b = _st()
st5b.mark_config_offline()
eng5b, alerts5b = make_engine(DryRunBroker(st5b, state=st5b), st5b,
                              tempfile.mkdtemp(prefix="tg_p42_off_"), state=st5b)
why5b = eng5b._pre_trade_check(act_past_close, "2026-09-02", None)
check("[5c] 离线通道闸门放行（走到后续校验，None = 全链通过）", why5b, None)
check("[5d] 离线通道不产生告警", alerts5b, [])

# ════════════════════════════════════════════════════════════════
print("\n[6] ⑥ 取值即冻结：二次推送（换值）不再改变 state")
st6 = _st()
q_v1 = FakeQuote(price_tick=2.0, volume_multiple=50.0,
                 upper_limit=5000.0, lower_limit=4000.0)
b6 = make_simnow(q_v1, spec=st6, state=st6)
b6._apply_instrument_quote()                 # 首次：取到并冻结
check("[6a] 首次取到 tick=2.0", st6.price_tick, 2.0)
b6._api = FakeApi(FakeQuote(price_tick=3.0, volume_multiple=60.0,
                            upper_limit=6000.0, lower_limit=4500.0))
b6._apply_instrument_quote()                 # 二次推送：换月/异常推送场景
check("[6b] 冻结后二次推送不改 tick", st6.price_tick, 2.0)
check("[6c] 冻结后二次推送不改乘数", st6.multiplier, 50.0)
check("[6d] 冻结后二次推送不改涨跌停", (st6.upper_limit, st6.lower_limit),
      (5000.0, 4000.0))

# ════════════════════════════════════════════════════════════════
print("\n[7] ⑦ 实盘配 policy=off → 闸门拒单；非法档位配置期报错；prefer 已删除")
st7 = _st()
b7 = make_simnow(FakeQuote(), policy="off", spec=st7, state=st7)
b7._apply_instrument_quote()                 # off：在线通道直接返回，不取不标
check("[7a] policy=off 在线通道 verified 恒 False", st7.verified, False)
tmp7 = tempfile.mkdtemp(prefix="tg_p42_offlive_")
eng7, alerts7 = make_engine(FakeLiveBroker(st7, state=st7), st7, tmp7,
                            state=st7)
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
st8 = _st()
st8.apply_quote(FakeQuote())                 # 区间 [4000, 5000]（apply_quote 是纯数据方法）
st8.verified = True                          # 模拟"已通过 SimNow 行情校验"→ 过闸门
b8 = make_simnow(FakeQuote(), spec=st8, state=st8, frozen=True)
check("[8a] 限价出上界 → 拒单原因", b8._price_out_of_band(5001.0) is not None, True)
check("[8b] 限价出下界 → 拒单原因", b8._price_out_of_band(3999.0) is not None, True)
check("[8c] 恰等于涨停价（区间内）→ 放行", b8._price_out_of_band(5000.0), None)
check("[8d] 区间内正常价 → 放行", b8._price_out_of_band(4500.0), None)
st8_unknown = _st()                          # 区间未知（离线）
b8b = make_simnow(FakeQuote(), spec=st8_unknown, state=st8_unknown,
                  frozen=True)
check("[8e] 区间未知 → 不校验（放行）", b8b._price_out_of_band(99999.0), None)
# 引擎侧粗检：参考价出区间同样拦下（走 _pre_trade_check 校验链）
tmp8 = tempfile.mkdtemp(prefix="tg_p42_band_")
eng8, alerts8 = make_engine(FakeLiveBroker(st8, state=st8), st8, tmp8,
                            state=st8)
why_out = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=5500.0)
check("[8f] 参考价 5500 出区间 → price_out_of_limit",
      str(why_out).startswith("price_out_of_limit"), True)
why_in = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=4500.0)
check("[8g] 参考价 4500 在区间 → 校验链通过（None）", why_in, None)
why_edge = eng8._pre_trade_check(act_past_close, "2026-09-02", None, ref_price=5000.0)
check("[8h] 参考价恰等于涨停价 → 放行", why_edge, None)

# ── Phase 8.1（B-1）：非有限限价 / 参考价一律 fail-closed ──
check("[8i] broker 侧 nan 限价 → 拒单（原版放行是 fail-open）",
      b8._price_out_of_band(float("nan")) is not None, True)
check("[8j] broker 侧 inf 限价 → 拒单",
      b8._price_out_of_band(float("inf")) is not None, True)
check_true("[8k] 拒单原因带 price_invalid 前缀",
           str(b8._price_out_of_band(float("nan"))).startswith("price_invalid"))
eng8_nan, _ = make_engine(FakeLiveBroker(st8, state=st8), st8,
                          tempfile.mkdtemp(prefix="tg_p42_bandnan_"), state=st8)
why_nan = eng8_nan._pre_trade_check(act_past_close, "2026-09-02", None,
                                    ref_price=float("nan"))
check("[8l] 引擎侧 nan 参考价 → price_invalid（区间已知时不再放行）",
      str(why_nan).startswith("price_invalid"), True)
# 区间未知时参考价校验整体跳过（含 nan）—— 走离线放行路径才能真正到达粗检
st8_off = _st()
st8_off.mark_config_offline()
eng8_nan2, _ = make_engine(DryRunBroker(st8_off, state=st8_off), st8_off,
                           tempfile.mkdtemp(prefix="tg_p42_bandnan2_"), state=st8_off)
check("[8m] 区间未知 → 参考价校验整体跳过（nan 也不拦，语义不变）",
      eng8_nan2._pre_trade_check(act_past_close, "2026-09-02", None,
                                 ref_price=float("nan")),
      None)

# ════════════════════════════════════════════════════════════════
print("\n[9] Phase 8.1（O-2/O-3）：broker 故障诊断经 notify/drain 回流 D11")
st9 = _st()
b9 = make_simnow(FakeQuote(price_tick=float("nan")), spec=st9, state=st9)
b9._apply_instrument_quote()                 # 超时失败 → _instrument_warn → notify 入队
q9 = b9.drain_alerts()
check_true("[9a] 超时诊断进 broker 告警队列（code=instrument_quote_timeout）",
           any(a.get("code") == "instrument_quote_timeout" for a in q9), q9)
check_true("[9b] 队列条目带 level=warn 与 broker 名",
           all(a.get("level") == "warn" and a.get("broker") == "simnow" for a in q9), q9)
check("[9c] drain 后队列清空", b9.drain_alerts(), [])
# 与配置不一致 → instrument_spec_conflict（带 field/quote/cfg 诊断字段）
st9b = _st()
b9b = make_simnow(FakeQuote(), spec=st9b, state=st9b)  # 配置 tick=0.2 vs 行情 2.0
b9b._apply_instrument_quote()
conf = [a for a in b9b.drain_alerts() if a.get("code") == "instrument_spec_conflict"]
check_true("[9d] 覆盖不一致写 conflict 告警且带 field 诊断",
           any(a.get("field") == "price_tick" for a in conf), conf)
# 引擎回流：_drain_broker_alerts 把 broker 队列转手 Engine.alert（D11）
tmp9 = tempfile.mkdtemp(prefix="tg_p42_route_")
eng9, alerts9 = make_engine(b9, st9, tmp9, state=st9)
b9._apply_instrument_quote()                 # 未冻结，再失败一次 → 重新入队
eng9._drain_broker_alerts()
check_true("[9e] broker 诊断经 _drain_broker_alerts 进入 D11（warn 级）",
           any(lv == "warn" and str(cd).startswith("instrument_")
               for lv, cd in alerts9), alerts9)
eng9_nobroker, alerts9b = make_engine(FakeLiveBroker(st9, state=st9), st9,
                                      tempfile.mkdtemp(prefix="tg_p42_route2_"),
                                      state=st9)
eng9_nobroker._drain_broker_alerts()         # 无 drain_alerts 能力的通道
check("[9f] 鸭子兼容：无 notify 能力的通道静默跳过", alerts9b, [])

# ════════════════════════════════════════════════════════════════
print("\n[10] Phase 8.1（O-6）：组合反例 policy=off × 坏行情")
st10 = _st()
b10 = make_simnow(FakeQuote(price_tick=float("nan")), policy="off",
                  spec=st10, state=st10)
b10._apply_instrument_quote()                # off：直接 return，行情好坏都无关
check("[10a] off + nan → verified 仍 False", st10.verified, False)
check("[10b] off + nan → tick 不被污染（保持配置 0.2）", st10.price_tick, 0.2)
tmp10 = tempfile.mkdtemp(prefix="tg_p42_combo_")
eng10, alerts10 = make_engine(FakeLiveBroker(st10, state=st10), st10,
                              tmp10, state=st10)
why10 = eng10._pre_trade_check(act_past_close, "2026-09-02", None)
check("[10c] 组合下闸门照样拒单", why10, "instrument_unverified")
check_true("[10d] 组合下仍伴 severe 告警",
           ("severe", "instrument_unverified") in alerts10, alerts10)
# [10e] 用未替换的 alert 通道验证 extra.policy（O-4 收窄：告警点名原因）
tmp10b = tempfile.mkdtemp(prefix="tg_p42_combo2_")
eng10b, alerts10b = make_engine(FakeLiveBroker(st10, state=st10), st10,
                                tmp10b, state=st10)
eng10b.broker.params = {"instrument_fetch_policy": "off"}  # duck _param 数据源
_kw = {}


def _cap_alert(level, code, msg, **extra):
    _kw.update(extra)
    alerts10b.append((level, code))


eng10b.alert = _cap_alert
eng10b._pre_trade_check(act_past_close, "2026-09-02", None)
check("[10e] 闸门告警 extra.policy == 'off'", _kw.get("policy"), "off")

print("\n" + "=" * 60)
print("P42 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
