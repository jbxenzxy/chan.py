# -*- coding: utf-8 -*-
"""
test_instrument_spec_ssot.py — 合约参数 SSOT 与 A′ 在线闸门（2026-09-17 改造）
=============================================================================
改造拍板（用户，2026-09-17）：
  · 有效 tick/乘数 SSOT = 品种档案 Product（构造期播种进 Instrument）——
    **没有任何信息需要从行情获取**；
  · verified = 在线通道连接成功（SimNow._connect 置位，source=CONFIG）；
    离线（dry_run/replay）走 mark_config_offline（source=CONFIG_OFFLINE）；
  · 涨跌停机制整体删除（报出必然被废的价格由交易所拒单 + 软件弹窗人工干预）；
  · 离场追价：终态回报到手立即重报（不 sleep 等待），chase_max_number=3 轮；
  · 主连映射失败（含超时）fail-fast，不再有默认月份兜底。

本测试锁死（防回潮）：
  [1] EffectiveSpec 只有两字段（tick/乘数）且 frozen；Instrument 初值 == 品种档案值
  [2] SOURCE 常量只剩 CONFIG / CONFIG_OFFLINE
  [3] 已删除符号不许回潮（属性级 + 模块源码级负向断言）
  [4] BrokerConfig：chase_max_number 默认 3；旧键残留 → 构造期显式报错（extra=forbid）
  [5] _resolve_trade_symbol 映射失败（异常 / 超时 / 空月份）→ RuntimeError（fail-fast）
  [6] 防回潮护栏仍在：_check_spec_drift / _check_spec_drift_online
  [7] 品种档案的三字段**书写顺序**（格式约定，非语义）：
      quote_unit → price_tick → multiplier（用户定序，2026-09-22）

不需要真实 tqsdk / 网络。
跑法：python Trading/Test/test_instrument_spec_ssot.py
"""
from __future__ import annotations

import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import Trading.Config as _cfg_mod  # noqa: E402
import Trading.Engine.Engine as _eng_mod  # noqa: E402
import Trading.Infra.Instrument as _ins_mod  # noqa: E402
import Trading.Broker.SimNow as _sn_mod  # noqa: E402
from Trading.Config import BrokerConfig, ChannelTimingConfig  # noqa: E402
from Trading.Engine.Engine import TradingEngine  # noqa: E402
from Trading.Infra.Instrument import (  # noqa: E402
    EffectiveSpec, Instrument, InstrumentConfig)
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402

_checks = {"pass": 0, "fail": 0}


def check(name, got, expected):
    ok = got == expected
    _checks["pass" if ok else "fail"] += 1
    print("  [{}] {}  got={!r} expected={!r}".format(
        "PASS" if ok else "FAIL", name, got, expected))


def check_true(name, got):
    check(name, bool(got), True)


_IF = PRODUCT_PROFILES["IF"]

# ═══ [1] EffectiveSpec：两字段值对象，SSOT=档案 ═══
print("\n[1] EffectiveSpec 两字段 + Instrument 初值=档案值（SSOT）")
check("EffectiveSpec 字段集 == {price_tick, multiplier}",
      set(EffectiveSpec.__dataclass_fields__), {"price_tick", "multiplier"})
check("EffectiveSpec frozen（不可变值对象）",
      EffectiveSpec.__dataclass_params__.frozen, True)
st = Instrument(None, _IF)
check("Instrument 初值 price_tick == 档案 0.2", st.price_tick, 0.2)
check("Instrument 初值 multiplier == 档案 300.0", st.multiplier, 300.0)
check_true("price_tick 无 setter（运行期没有改写路径）",
           Instrument.price_tick.fset is None)
check_true("multiplier 无 setter", Instrument.multiplier.fset is None)
check("初值未 verified（等在线连接置位）", st.verified, False)

# ═══ [2] SOURCE 常量 ═══
print("\n[2] SOURCE 常量只剩 CONFIG / CONFIG_OFFLINE")
check("SOURCE_CONFIG == 'CONFIG'", Instrument.SOURCE_CONFIG, "CONFIG")
check_true("SOURCE_CONFIG_OFFLINE 存在",
           hasattr(Instrument, "SOURCE_CONFIG_OFFLINE"))
st.mark_config_offline()
check("mark_config_offline → source=CONFIG_OFFLINE",
      st.source, Instrument.SOURCE_CONFIG_OFFLINE)

# ═══ [3] 已删除符号不许回潮 ═══
print("\n[3] 已删除符号负向断言（属性级 + 源码级）")
from Trading.Broker.SimNow import SimNowBroker  # noqa: E402

for gone in ("_price_out_of_band", "_apply_instrument_quote",
             "_quote_params_ready", "_instrument_warn",
             "INSTRUMENT_RETRY_EVERY_BARS"):
    check("SimNowBroker 已无 {}".format(gone), hasattr(SimNowBroker, gone), False)
for gone in ("apply_quote", "upper_limit", "lower_limit"):
    check("Instrument 已无 {}".format(gone), hasattr(Instrument, gone), False)
check("Engine 已无 _ref_price_out_of_band",
      hasattr(TradingEngine, "_ref_price_out_of_band"), False)
check_true("InstrumentConfig.trade_symbol 默认空串（无默认月份兜底）",
           InstrumentConfig.model_fields["trade_symbol"].default == "")

sn_src = inspect.getsource(_sn_mod)
for gone in ("close_max_chase", "chase_interval", "_price_out_of_band",
             "_apply_instrument_quote", "_quote_params_ready", "_instrument_warn",
             "INSTRUMENT_RETRY_EVERY_BARS"):
    check("SimNow 源码无 {}".format(gone), gone in sn_src, False)
eng_src = inspect.getsource(_eng_mod)
for gone in ("close_max_chase", "chase_interval", "chase_window_exceeds_bar",
             "_ref_price_out_of_band", "price_out_of_limit",
             "instrument_fetch_policy", "bar_secs_for"):
    check("Engine 源码无 {}".format(gone), gone in eng_src, False)
cfg_src = inspect.getsource(_cfg_mod)
for gone in ("instrument_fetch_policy", "instrument_fetch_timeout",
             "close_max_chase", "chase_interval"):
    check("Config 源码无 {}".format(gone), gone in cfg_src, False)
ins_src = inspect.getsource(_ins_mod)
for gone in ("apply_quote", "_QUOTE_FIELD_MAP", "SOURCE_QUOTE"):
    check("Instrument 源码无 {}".format(gone), gone in ins_src, False)

check("ChannelTimingConfig 字段数 == 10（2026-09-18 撤 terminal_settle_wait）",
      len(ChannelTimingConfig.model_fields), 10)
check_true("channel 无合约参数就绪独立超时字段",
           "instrument_fetch_timeout" not in ChannelTimingConfig.model_fields)

# ═══ [4] BrokerConfig：追价新键 + 旧键显式报错 ═══
print("\n[4] BrokerConfig：chase_max_number 默认 3；旧键残留 → 构造期显式报错")
bc = BrokerConfig()
check("chase_max_number 默认 3", bc.chase_max_number, 3)
check_true("close_chase_ticks 保留（离场追价兜底步长）",
           "close_chase_ticks" in BrokerConfig.model_fields)
for gone in ("instrument_fetch_policy", "instrument_fetch_timeout",
             "close_max_chase", "chase_interval"):
    check("BrokerConfig 已无 {}".format(gone),
          gone in BrokerConfig.model_fields, False)
    raised = False
    try:
        BrokerConfig(**{gone: 1})
    except Exception:
        raised = True
    check("{} 残留 → 构造期显式报错（extra=forbid，非静默）".format(gone),
          raised, True)

# ═══ [5] _resolve_trade_symbol 映射失败 fail-fast ═══
print("\n[5] _resolve_trade_symbol 映射失败 → RuntimeError（不再静默兜底）")


class _UnderlyingQuote:
    """主连行情替身：只有 underlying_symbol 一个相关字段。"""

    def __init__(self, underlying=None):
        self.underlying_symbol = underlying


class _ApiBoom:
    def get_quote(self, sym):
        raise RuntimeError("boom")


class _ApiStall:
    def __init__(self, q):
        self._q = q

    def get_quote(self, sym):
        return self._q


def make_rs(signal_symbol="KQ.m@CFFEX.IF", trade_symbol=""):
    b = SimNowBroker.__new__(SimNowBroker)   # 跳过 __init__，不连 CTP
    b.state = Instrument(InstrumentConfig(signal_symbol=signal_symbol,
                                          trade_symbol=trade_symbol), _IF)
    b.params = BrokerConfig().model_dump()
    b._trade_symbol = trade_symbol
    b._api = None
    return b


# (a) 行情接口异常 → RuntimeError
b = make_rs()
b._api = _ApiBoom()
err = None
try:
    b._resolve_trade_symbol()
except Exception as e:  # noqa: BLE001
    err = str(e)
check_true("(a) get_quote 异常 → RuntimeError（主连映射失败）",
           err is not None and "主连映射失败" in err)

# (b) underlying_symbol 超时未就绪 → RuntimeError
b = make_rs()
b._api = _ApiStall(_UnderlyingQuote(None))
b._wait = lambda fn, timeout_s=0.0: False   # 桩：永远等不到
err = None
try:
    b._resolve_trade_symbol()
except Exception as e:  # noqa: BLE001
    err = str(e)
check_true("(b) underlying_symbol 超时 → RuntimeError（主连映射超时）",
           err is not None and "主连映射超时" in err)

# (c) 成功映射 → trade_symbol 更新 + state 回填
b = make_rs()
b._api = _ApiStall(_UnderlyingQuote("CFFEX.IF2612"))
b._wait = lambda fn, timeout_s=0.0: True
b._resolve_trade_symbol()
check("(c) 成功映射 → _trade_symbol 更新", b._trade_symbol, "CFFEX.IF2612")
check("(c) 成功映射 → state.trade_symbol 回填",
      b.state.trade_symbol, "CFFEX.IF2612")

# (d-1) 非主连且 trade_symbol 空 → RuntimeError（不再回退默认月份）
b = make_rs(signal_symbol="CFFEX.IF2609", trade_symbol="")
err = None
try:
    b._resolve_trade_symbol()
except Exception as e:  # noqa: BLE001
    err = str(e)
check_true("(d-1) 非主连且 trade_symbol 空 → RuntimeError", err is not None)

# (d-2) 非主连但已显式指定 trade_symbol → 直接放行
b = make_rs(signal_symbol="CFFEX.IF2609", trade_symbol="CFFEX.IF2609")
raised = False
try:
    b._resolve_trade_symbol()
except Exception:  # noqa: BLE001
    raised = True
check_true("(d-2) 非主连但已显式指定 trade_symbol → 直接放行", not raised)

# ═══ [6] 防回潮护栏仍在 ═══
print("\n[6] 防回潮护栏保留")
check_true("Engine._check_spec_drift 保留",
           callable(getattr(TradingEngine, "_check_spec_drift", None)))
check_true("Engine._check_spec_drift_online 保留",
           callable(getattr(TradingEngine, "_check_spec_drift_online", None)))

# ═══ [7] 品种档案三字段书写顺序（格式约定，防回潮）═══
print("\n[7] 档案实参书写顺序：quote_unit → price_tick → multiplier")

_TRIPLET = ("quote_unit", "price_tick", "multiplier")
_PRODUCT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Infra", "Product.py")


def _triplet_order(src):
    """从源码取出 PRODUCT_PROFILES 里每个档案的三个关键字**书写顺序**。

    返回 {品种代码: 实际顺序元组}。用 ast 而非正则：关键字顺序正是语法事实
    （`ast.Call.keywords` 按源码先后排列），正则会被缩进/折行/注释干扰。
    只收集本三元组内的字段 —— 其余实参（product/win_loss_ratio/note/…）不参与。
    """
    out = {}
    for node in ast.walk(ast.parse(src)):
        # 真值是**带注解**赋值（`PRODUCT_PROFILES: Dict[str, Product] = {...}`）→ AnnAssign；
        # 无注解写法才是 Assign —— 两种都要收，否则本判定会因为"没扫到"而恒真。
        if isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        if not isinstance(value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PRODUCT_PROFILES"
                   for t in targets):
            continue
        for key, val in zip(value.keys, value.values):
            if not (isinstance(val, ast.Call)
                    and getattr(val.func, "id", "") == "Product"):
                continue
            out[key.value] = tuple(kw.arg for kw in val.keywords
                                   if kw.arg in _TRIPLET)
    return out


with open(_PRODUCT_SRC, encoding="utf-8") as _f:
    _orders = _triplet_order(_f.read())

check("解析到 8 个档案条目", len(_orders), 8)
check("每个档案三字段书写顺序 == quote_unit → price_tick → multiplier",
      {c: o for c, o in _orders.items() if o != _TRIPLET}, {})
check_true("判别力自证：顺序写反时本判定会红（非恒真）",
           _triplet_order(
               'PRODUCT_PROFILES = {"XX": Product(product="XX", price_tick=0.2,'
               ' multiplier=1.0, quote_unit="点")}'
           ).get("XX") != _TRIPLET)

# ═══ 汇总 ═══
print("\n结果: {} 通过 / {} 失败".format(_checks["pass"], _checks["fail"]))
sys.exit(0 if _checks["fail"] == 0 else 1)
