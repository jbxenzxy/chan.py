# -*- coding: utf-8 -*-
"""
test_engine_config.py — 引擎时序配置：自动兜底拆除后的形态
====================================================================
背景（2026-09-17 拍板：例外 → 弹窗 → 用户干预，引擎不自动兜底）：
  冷却重试 / 连拒清幻影仓 / 卡单复核三套机制整体删除，原 EngineConfig
  （close_retry_bars / close_max_streak / close_stuck_bars）随之删除。

验证点：
  [1] EngineConfig 已删除（类 + __all__ 导出 + TradingConfig.engine 字段）
  [2] 未知键仍被拒（extra="forbid" 语义不受影响）
  [3] 引擎不再持有旧时序属性（_close_retry_bars / _close_max_streak /
      _close_stuck_bars 均不存在）
  [4] PositionBook 容量（D2：笔数上限已删除）+ 旧配置键丢弃（D17）
  [5] Period.SESSION_SECS 收口（原 main.py 硬编码 4.5h）
  [6] bars_per_day(SESSION_SECS) 与四周期对账
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from Trading.Config import DEFAULT_CONFIG, TradingConfig, default_config

_checks = {"pass": 0, "fail": 0}


def check(name, got, expected):
    ok = got == expected
    _checks["pass" if ok else "fail"] += 1
    print("  [{}] {}  got={!r} expected={!r}".format(
        "PASS" if ok else "FAIL", name, got, expected))


# ═══ [1] EngineConfig 已整体删除 ═══
print("\n[1] EngineConfig 已删除（冷却/连拒/卡单三套兜底随之移除）")
import Trading.Config as _cfgmod
check("Config 模块已无 EngineConfig 类",
      hasattr(_cfgmod, "EngineConfig"), False)
check("TradingConfig 已无 engine 字段",
      "engine" in TradingConfig.model_fields, False)
check("default_config() 已无 engine 键",
      hasattr(default_config(), "engine"), False)

# ═══ [2] extra="forbid" 语义不受影响 ═══
print("\n[2] 未知键仍被拒（extra='forbid'）")
try:
    TradingConfig.from_dict(dict(
        DEFAULT_CONFIG, engine={"close_retry_bars": 9}))
    check("engine 键已被删，作为未知键应报错", "no_raise", "raise")
except Exception:
    check("engine 键已被删，作为未知键应报错", "raise", "raise")

# ═══ [3] 引擎不再持有旧时序属性 ═══
print("\n[3] 引擎实例不持有旧时序属性")
import tempfile
from Trading import Broker  # noqa: F401,E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker
from Trading.Engine.Engine import TradingEngine
from Trading.Infra.EventLog import EventLog
from Trading.Infra.Instrument import Instrument
from Trading.Infra.Product import PRODUCT_PROFILES  # noqa: E402

_IF = PRODUCT_PROFILES["IF"]
from Trading.Infra.StateDB import Store
from Trading.Strategy import EntryPolicy, LayeredExitPolicy


def build_engine(cfg):
    spec = Instrument(None, _IF)
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = EntryPolicy({})
    exitp = LayeredExitPolicy()
    tmp = tempfile.mkdtemp(prefix="p22_")
    store = Store(os.path.join(tmp, "state.db"))
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


eng = build_engine(TradingConfig.from_dict(DEFAULT_CONFIG))
for _attr in ("_close_retry_bars", "_close_max_streak", "_close_stuck_bars",
              "_close_fail_streak", "_close_in_flight", "_last_close_failed_bar_seq"):
    check("engine 已无 {}".format(_attr), hasattr(eng, _attr), False)

# ═══ [4] PositionBook 容量 + 旧配置键丢弃（D2 / D17）═══
print("\n[4] PositionBook 容量（D2：max_open_positions 已删）+ 旧配置键丢弃（D17）")
from Trading.Engine.PositionBook import PositionBook, PositionBookError  # noqa: F401,E402
from Trading.Config import RiskConfig  # noqa: E402
check("PositionBook.DEFAULT_MAX 为 None（D2 后容器不限容量）",
      PositionBook.DEFAULT_MAX, None)
check("无参构造 → max_positions=None", PositionBook().max_positions, None)
check("显式 max_positions=3 仍可用（容器能力保留）",
      PositionBook(max_positions=3).max_positions, 3)
check("RiskConfig 已无 max_open_positions 字段",
      "max_open_positions" in RiskConfig.model_fields, False)
check("RiskConfig 已无 unlock_no_new_open 字段",
      "unlock_no_new_open" in RiskConfig.model_fields, False)
check("RiskConfig 已无 max_volume 字段（手数旋钮迁品种执行策略表）",
      "max_volume" in RiskConfig.model_fields, False)
# D17：老配置里的旧键必须被**静默丢弃 + 记录**（与 D16 的 state.db 严格拒绝有意不同）
risk_legacy = RiskConfig(**{"max_open_positions": 3, "unlock_no_new_open": True,
                            "max_volume": 2})
check("已删键不阻断构造（D17）", risk_legacy.delivery_guard_days, 1)
check("已删键被记录进 dropped_legacy_keys（D17，含 max_volume）",
      ("max_open_positions" in RiskConfig.dropped_legacy_keys
       and "unlock_no_new_open" in RiskConfig.dropped_legacy_keys
       and "max_volume" in RiskConfig.dropped_legacy_keys), True)

# ═══ [5] SESSION_SECS 收口 ═══
print("\n[5] Period.SESSION_SECS（原 main.py 硬编码 4.5h）")
from Trading.Infra.Period import bars_per_day  # noqa: E402
from Trading.Infra.Clock import SESSION_SECS  # noqa: E402
check("SESSION_SECS == 4.5h", SESSION_SECS, 4.5 * 3600)

# ═══ [6] bars_per_day 四周期对账 ═══
print("\n[6] bars_per_day(SESSION_SECS) 四周期")
from Trading.Infra.Period import FREQ_SEC  # noqa: E402
for freq, secs in FREQ_SEC.items():
    expect = int(SESSION_SECS // secs)
    check("freq={} → {} 根/交易日".format(freq, expect),
          bars_per_day(secs, SESSION_SECS), expect)
check("bar_secs=0 → None（防除零）", bars_per_day(0, SESSION_SECS), None)

# ═══ 汇总 ═══
print("\n结果: {} 通过 / {} 失败".format(_checks["pass"], _checks["fail"]))
sys.exit(0 if _checks["fail"] == 0 else 1)
