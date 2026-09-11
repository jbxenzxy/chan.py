# -*- coding: utf-8 -*-
"""
test_engine_config.py — Step 2.2：引擎时序常量归一到 EngineConfig
====================================================================
验证点（对应可行性分析 §3 拍板结论 E1 + F1 + G2）：
  [1] EngineConfig 字段与默认值（5 / 20 / 5，SSOT 唯一声明处）
  [2] TradingConfig.engine 默认工厂 + 显式覆盖 + extra="forbid" 拒未知字段
  [3] 引擎接线：Engine.__init__ 从 cfg.engine 读值，属性名不变
  [4] PositionBook 容量（D2：笔数上限已删除）+ 旧配置键丢弃（D17）
  [5] PeriodProfile.SESSION_SECS 收口（原 main.py 硬编码 4.5h）
  [6] bars_per_day(SESSION_SECS) 与四周期对账
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from Trading.Config import (DEFAULT_CONFIG, EngineConfig, TradingConfig,
                            default_config)

_checks = {"pass": 0, "fail": 0}


def check(name, got, expected):
    ok = got == expected
    _checks["pass" if ok else "fail"] += 1
    print("  [{}] {}  got={!r} expected={!r}".format(
        "PASS" if ok else "FAIL", name, got, expected))


# ═══ [1] EngineConfig 字段与默认值 ═══
print("\n[1] EngineConfig 默认值（SSOT：Engine/Reconcile 不再自带兜底数字）")
e = EngineConfig()
check("close_retry_bars 默认 5", e.close_retry_bars, 5)
check("close_max_streak 默认 20", e.close_max_streak, 20)
check("close_stuck_bars 默认 5", e.close_stuck_bars, 5)
try:
    EngineConfig(unknown_field=1)
    check("extra='forbid' 拒未知字段", "no_raise", "raise")
except Exception:
    check("extra='forbid' 拒未知字段", "raise", "raise")

# ═══ [2] TradingConfig.engine 接线 ═══
print("\n[2] TradingConfig.engine 字段")
cfg = TradingConfig.from_dict(DEFAULT_CONFIG)
check("默认工厂产出 EngineConfig",
      type(cfg.engine).__name__, "EngineConfig")
check("默认值透传", cfg.engine.close_retry_bars, 5)
cfg2 = TradingConfig.from_dict(dict(
    DEFAULT_CONFIG, engine={"close_retry_bars": 9}))
check("显式覆盖 close_retry_bars=9", cfg2.engine.close_retry_bars, 9)
check("覆盖后其余字段保持默认", cfg2.engine.close_max_streak, 20)
check("覆盖后 close_max_streak 与 unlock 独立",
      cfg2.engine.close_stuck_bars, 5)
try:
    TradingConfig.from_dict(dict(DEFAULT_CONFIG, engine={"bogus": 1}))
    check("engine 未知键报错（extra=forbid）", "no_raise", "raise")
except Exception:
    check("engine 未知键报错（extra=forbid）", "raise", "raise")
check("default_config() 也含 engine",
      type(default_config().engine).__name__, "EngineConfig")

# ═══ [3] 引擎接线（照 test_p12 的 build_engine 模式，dry_run 不起网络）═══
print("\n[3] Engine.__init__ 从 cfg.engine 读值，属性名不变")
import tempfile
from Trading import Broker  # noqa: F401,E402  注册 dry_run
from Trading.Broker.DryRun import DryRunBroker
from Trading.Engine.Engine import TradingEngine
from Trading.Infra.EventLog import EventLog
from Trading.Infra.InstrumentSpec import InstrumentSpec
from Trading.Infra.Store import Store
from Trading.Strategy import DefaultEntryPolicy, LayeredExitPolicy


def build_engine(cfg):
    spec = InstrumentSpec()
    broker = DryRunBroker(spec, {"sim_equity": 1_000_000.0})
    entry = DefaultEntryPolicy({})
    exitp = LayeredExitPolicy()
    tmp = tempfile.mkdtemp(prefix="p22_")
    store = Store(os.path.join(tmp, "state.db"))
    ev = EventLog(os.path.join(tmp, "events.jsonl"), echo=False, echo_kinds=None)
    return TradingEngine(cfg, broker, entry, exitp, store, ev)


eng = build_engine(TradingConfig.from_dict(DEFAULT_CONFIG))
check("engine._close_retry_bars == cfg.engine.close_retry_bars",
      eng._close_retry_bars, 5)
check("engine._close_max_streak == cfg.engine.close_max_streak",
      eng._close_max_streak, 20)
check("engine._close_stuck_bars == cfg.engine.close_stuck_bars",
      eng._close_stuck_bars, 5)
cfg9 = TradingConfig.from_dict(dict(
    DEFAULT_CONFIG, engine={"close_retry_bars": 9,
                            "close_max_streak": 40,
                            "close_stuck_bars": 7}))
eng9 = build_engine(cfg9)
check("覆盖后引擎读到 9/40/7",
      (eng9._close_retry_bars, eng9._close_max_streak,
       eng9._close_stuck_bars), (9, 40, 7))

# ═══ [4] PositionBook 容量 + 旧配置键丢弃（D2 / D17）═══
print("\n[4] PositionBook 容量（D2：max_open_positions 已删）+ 旧配置键丢弃（D17）")
from Trading.Engine.PositionBook import PositionBook, PositionBookError
from Trading.Config import RiskConfig
check("PositionBook.DEFAULT_MAX 为 None（D2 后容器不限容量）",
      PositionBook.DEFAULT_MAX, None)
check("无参构造 → max_positions=None", PositionBook().max_positions, None)
check("显式 max_positions=3 仍可用（容器能力保留）",
      PositionBook(max_positions=3).max_positions, 3)
check("RiskConfig 已无 max_open_positions 字段",
      "max_open_positions" in RiskConfig.model_fields, False)
check("RiskConfig 已无 unlock_no_new_open 字段",
      "unlock_no_new_open" in RiskConfig.model_fields, False)
# D17：老配置里的旧键必须被**静默丢弃 + 记录**（与 D16 的 state.db 严格拒绝有意不同）
risk_legacy = RiskConfig(**{"max_open_positions": 3, "unlock_no_new_open": True,
                            "max_volume": 2})
check("老配置键不阻断构造（D17）", risk_legacy.max_volume, 2)
check("老配置键被记录进 dropped_legacy_keys（D17）",
      ("max_open_positions" in RiskConfig.dropped_legacy_keys
       and "unlock_no_new_open" in RiskConfig.dropped_legacy_keys), True)

# ═══ [5] SESSION_SECS 收口 ═══
print("\n[5] PeriodProfile.SESSION_SECS（原 main.py 硬编码 4.5h）")
from Trading.Infra.PeriodProfile import SESSION_SECS, bars_per_day
check("SESSION_SECS == 4.5h", SESSION_SECS, 4.5 * 3600)

# ═══ [6] bars_per_day 四周期对账 ═══
print("\n[6] bars_per_day(SESSION_SECS) 四周期")
from Trading.Infra.PeriodProfile import FREQ_SEC
for freq, secs in FREQ_SEC.items():
    expect = int(SESSION_SECS // secs)
    check("freq={} → {} 根/交易日".format(freq, expect),
          bars_per_day(secs, SESSION_SECS), expect)
check("bar_secs=0 → None（防除零）", bars_per_day(0, SESSION_SECS), None)

# ═══ 汇总 ═══
print("\n结果: {} 通过 / {} 失败".format(_checks["pass"], _checks["fail"]))
sys.exit(0 if _checks["fail"] == 0 else 1)
