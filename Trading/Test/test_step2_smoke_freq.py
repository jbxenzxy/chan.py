# -*- coding: utf-8 -*-
"""Step 2.6 收官测试：--freq {30m,5m,1m,15s} 四周期启动冒烟全通。

范围（交接文档 §5.1 的 2.6 定义）：
  对四个支持周期各完整走一遍 main.build_runtime() 启动链路（不联网）：
    TradingConfig() → CLI --freq 覆盖 → bar_secs_for 校验（fail-fast 在这）
    → bars_per_day → build_broker(dry_run) → DefaultEntryPolicy / LayeredExitPolicy
    → Store / EventLog / TradingEngine → build_source("sse")
  断言：bar_secs、根数/日、period_profile 选到对应档案、
  引擎与策略与信号源对象构造齐全。

说明：build_runtime 会把 stdout/stderr 重定向到 {out}/gateway.log，
每轮跑完恢复。out 用临时目录，跑完清理。SSE 源构造不联网
（连接延迟到 events()），全程离线安全。

独立脚本风格：check() + 末尾统计 + sys.exit(1)。
跑法：python Test/test_step2_smoke_freq.py
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

_PASSED = 0
_FAILED = 0


def check(name, got, expected):
    global _PASSED, _FAILED
    ok = got == expected
    if ok:
        _PASSED += 1
        print("  [PASS] {}".format(name))
    else:
        _FAILED += 1
        print("  [FAIL] {}\n         got={!r}, expected={!r}".format(name, got, expected))


from Trading import main as gw
from Trading.Config import TradingConfig
from Trading.Engine.Engine import TradingEngine
from Trading.Infra.PeriodProfile import (PERIOD_PROFILES, SESSION_SECS,
                                         bar_secs_for, bars_per_day)
from Trading.Source.SSE import SseSource
from Trading.Strategy import DefaultEntryPolicy, LayeredExitPolicy

# 期望值（SSOT 校验对账：PeriodProfile.FREQ_SEC / 4.5h 交易日近似）
_EXPECT_BAR_SECS = {"30m": 1800, "5m": 300, "1m": 60, "15s": 15}
_EXPECT_BPD = {"30m": 9, "5m": 54, "1m": 270, "15s": 1080}

print("=" * 60)
print("Step 2.6 测试：--freq 四周期启动冒烟（30m / 5m / 1m / 15s）")
print("=" * 60)

for freq in ("30m", "5m", "1m", "15s"):
    print("\n── freq={} ──".format(freq))
    out_dir = os.path.join(tempfile.mkdtemp(prefix="gw_smoke_"), "out")
    _real_stdout, _real_stderr = sys.stdout, sys.stderr
    try:
        args = argparse.Namespace(
            source="sse", broker=None, replay_dir=None, symbol=None,
            freq=freq, sse_base=None, speed=None, only_alive=None,
            bar_mode=None, out=out_dir, fresh=None, summary_json=None,
            max_bars=None, quiet=True, echo_all=None)
        cfg, engine, source, store, ev, out, src = gw.build_runtime(args)
        sys.stdout, sys.stderr = _real_stdout, _real_stderr

        check("cfg.source.freq == {}".format(freq), cfg.source.freq, freq)
        check("bar_secs_for == {}s".format(_EXPECT_BAR_SECS[freq]),
              bar_secs_for(freq), _EXPECT_BAR_SECS[freq])
        check("bars_per_day == {}（按 4.5h 近似）".format(_EXPECT_BPD[freq]),
              bars_per_day(_EXPECT_BAR_SECS[freq], SESSION_SECS),
              _EXPECT_BPD[freq])

        prof = cfg.period_profile
        check("period_profile 选到档案", getattr(prof, "freq", None), freq)
        # 信号新鲜度容差是非周期敏感项：任意 freq 下都取配置值，不与 profile 联动
        check("source.signal_k_tol_bars == 1（非周期敏感）",
              cfg.source.signal_k_tol_bars, 1)

        check("引擎构造 TradingEngine", type(engine), TradingEngine)
        check("入场策略 DefaultEntryPolicy", type(engine.entry_policy),
              DefaultEntryPolicy)
        check("出场策略 LayeredExitPolicy", type(engine.exit_policy),
              LayeredExitPolicy)
        check("默认 broker dry_run（离线）", engine.broker.name, "dry_run")
        check("SSE 源构造且 freq 透传",
              (type(source), source.freq), (SseSource, freq))
        check("gateway.log 已生成",
              os.path.isfile(os.path.join(out, "gateway.log")), True)

        # 清理本轮资源
        ev.close(); store.close()
        engine.broker.close(); source.close()
    finally:
        sys.stdout, sys.stderr = _real_stdout, _real_stderr
        shutil.rmtree(os.path.dirname(out_dir), ignore_errors=True)

# 反向用例：不支持的周期必须在启动期 fail-fast（bar_secs_for 校验）
print("\n── 反向：freq=15m 应被启动校验拦截 ──")
args_bad = argparse.Namespace(
    source="sse", broker=None, replay_dir=None, symbol=None,
    freq="15m", sse_base=None, speed=None, only_alive=None,
    bar_mode=None, out=tempfile.mkdtemp(prefix="gw_smoke_bad_"),
    fresh=None, summary_json=None, max_bars=None, quiet=True, echo_all=None)
try:
    gw.build_runtime(args_bad)
    check("freq=15m 启动即 SystemExit", "no_raise", "raise")
except SystemExit:
    check("freq=15m 启动即 SystemExit", "raise", "raise")
except Exception as e:
    check("freq=15m 启动即 SystemExit（实际 {}）".format(type(e).__name__),
          "wrong_exc", "raise")

print("\n结果: {} 通过 / {} 失败".format(_PASSED, _FAILED))
sys.exit(1 if _FAILED else 0)
