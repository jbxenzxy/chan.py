# -*- coding: utf-8 -*-
"""
test_channel_timing.py — Step 2.3：Broker/Channel 时序参数归一到 ChannelTimingConfig
====================================================================================
验证点（对应可行性分析 §3 拍板结论 A1 + B1 + C1）：
  [1] ChannelTimingConfig 10 字段默认值 == 原硬编码值（行为等价），extra="forbid"
  [2] BrokerConfig.channel 嵌套挂载 + default_factory 向后兼容（旧配置无 channel 键）
  [3] SimNowBroker.__init__ 严格模式：params 含完整 channel（BrokerConfig 补齐）
  [4] _timing() 严格读取：缺 key 抛 KeyError（配置模型漏字段=代码 bug fail-fast）
  [5] _quote_stale 读配置（原 _QUOTE_STALE_SECONDS 模块常量已删）
  [6] 微轮询命名常量存在（_POLL_INTERVAL_FAST/SLOW，字面量消灭）
  [7] LiveCTPBroker 继承不破坏（channel 自动可用）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from Trading.Config import (BrokerConfig, ChannelTimingConfig, DEFAULT_CONFIG,
                            TradingConfig)

_checks = {"pass": 0, "fail": 0}


def check(name, got, expected):
    ok = got == expected
    _checks["pass" if ok else "fail"] += 1
    print("  [{}] {}  got={!r} expected={!r}".format(
        "PASS" if ok else "FAIL", name, got, expected))


# ═══ [1] 10 字段默认值 == 原硬编码值 ═══
print("\n[1] ChannelTimingConfig 默认值（行为等价锚点）")
ct = ChannelTimingConfig()
check("quote_stale_seconds == 30.0（原 _QUOTE_STALE_SECONDS）",
      ct.quote_stale_seconds, 30.0)
check("connect_backoff_factor == 1.5（原硬编码 ×1.5）",
      ct.connect_backoff_factor, 1.5)
check("probe_alive_timeout == 8.0（原 _probe_alive 默认）",
      ct.probe_alive_timeout, 8.0)
check("keepalive_wait == 0.2（原 poll_market 心跳）", ct.keepalive_wait, 0.2)
check("baseline_settle_wait == 0.5（原 _take_baseline）",
      ct.baseline_settle_wait, 0.5)
check("recover_settle_wait == 5.0（原恢复路径）", ct.recover_settle_wait, 5.0)
check("position_ok_timeout == 10.0（原 _wait_position_ok）",
      ct.position_ok_timeout, 10.0)
check("verify_delta_timeout == 5.0（原 :855 调用）",
      ct.verify_delta_timeout, 5.0)
check("underlying_map_timeout == 20.0（原主连映射）",
      ct.underlying_map_timeout, 20.0)
check("cancel_settle_wait == 5.0（原撤单后等回报）",
      ct.cancel_settle_wait, 5.0)
try:
    ChannelTimingConfig(bogus=1)
    check("extra='forbid' 拒未知字段", "no_raise", "raise")
except Exception:
    check("extra='forbid' 拒未知字段", "raise", "raise")

# ═══ [2] BrokerConfig.channel 嵌套 + 向后兼容 ═══
print("\n[2] BrokerConfig.channel 挂载")
bc = BrokerConfig()
check("channel 默认产出 ChannelTimingConfig",
      type(bc.channel).__name__, "ChannelTimingConfig")
check("报单参数与时序参数独立（overprice 不受影响）",
      bc.overprice_ticks, 5)
cfg_old = TradingConfig.from_dict(DEFAULT_CONFIG)  # 旧配置无 channel 键场景由 default_factory 兜底
check("TradingConfig 默认含 channel（向后兼容）",
      type(cfg_old.broker_params.channel).__name__, "ChannelTimingConfig")
bc2 = BrokerConfig(**dict(bc.model_dump(),
                          channel={"quote_stale_seconds": 15.0}))
check("channel 显式覆盖 quote_stale_seconds=15", bc2.channel.quote_stale_seconds, 15.0)
check("覆盖后其余 channel 字段保持默认", bc2.channel.probe_alive_timeout, 8.0)

# ═══ [3] SimNowBroker 严格模式（不连网构造）═══
print("\n[3] SimNowBroker.params 含完整 channel")
from Trading.Broker.SimNow import SimNowBroker  # noqa: E402
b = SimNowBroker.__new__(SimNowBroker)
b.params = BrokerConfig().model_dump()   # 与 test_simnow_guards._make 同一注入方式
check("params['channel'] 存在且含 10 键",
      len(b.params["channel"]), 10)
check("_timing 读 quote_stale_seconds", b._timing("quote_stale_seconds"), 30.0)
check("_timing 读 probe_alive_timeout", b._timing("probe_alive_timeout"), 8.0)

# ═══ [4] _timing 严格抛错 ═══
print("\n[4] _timing 严格模式（漏字段=代码 bug，fail-fast）")
try:
    b._timing("nonexistent_key")
    check("缺 key 抛 KeyError", "no_raise", "raise")
except KeyError:
    check("缺 key 抛 KeyError", "raise", "raise")
b_nochan = SimNowBroker.__new__(SimNowBroker)
b_nochan.params = {"fill_timeout_open": 5.0}   # 人为去掉 channel
try:
    b_nochan._timing("quote_stale_seconds")
    check("params 无 channel 键抛 KeyError", "no_raise", "raise")
except KeyError:
    check("params 无 channel 键抛 KeyError", "raise", "raise")

# ═══ [5] _quote_stale 读配置 ═══
print("\n[5] _quote_stale 走配置（原模块常量已删）")
import time as _time
from Trading.Broker.SimNow import _POLL_INTERVAL_FAST, _POLL_INTERVAL_SLOW


class _Q:
    def __init__(self, dt):
        self.datetime = dt


b._quote = _Q(_time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - 2)))
check("2 秒前 tick → 新鲜（30s 阈值）", b._quote_stale(), False)
b._quote = _Q(_time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - 600)))
check("600 秒前 tick → 陈旧（30s 阈值）", b._quote_stale(), True)
# 收紧阈值到 1s 后，同一条 2 秒前的 tick 变陈旧 → 证明判定读的是配置而非常量
b.params["channel"]["quote_stale_seconds"] = 1.0
b._quote = _Q(_time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() - 2)))
check("阈值改 1s 后同 tick → 陈旧（证明读配置）", b._quote_stale(), True)

# ═══ [6] 微轮询命名常量 ═══
print("\n[6] 微轮询命名常量（拍板 C1）")
check("_POLL_INTERVAL_FAST == 0.1", _POLL_INTERVAL_FAST, 0.1)
check("_POLL_INTERVAL_SLOW == 0.2", _POLL_INTERVAL_SLOW, 0.2)
try:
    from Trading.Broker import SimNow as _sn_mod  # noqa: F401
    has_mod = True
except ImportError:
    _sn_mod = None
    has_mod = False
if has_mod:
    import Trading.Broker.SimNow as _sn
    check("模块已无 _QUOTE_STALE_SECONDS",
          hasattr(_sn, "_QUOTE_STALE_SECONDS"), False)

# ═══ [7] LiveCTPBroker 继承 ═══
print("\n[7] LiveCTPBroker 继承")
from Trading.Broker.SimNow import LiveCTPBroker  # noqa: E402
check("LiveCTPBroker 是 SimNowBroker 子类",
      issubclass(LiveCTPBroker, SimNowBroker), True)

# ═══ 汇总 ═══
print("\n结果: {} 通过 / {} 失败".format(_checks["pass"], _checks["fail"]))
sys.exit(0 if _checks["fail"] == 0 else 1)
