# -*- coding: utf-8 -*-
"""Step 2.6 收官测试：--freq {30m,5m,1m,15s} 四周期启动冒烟全通。

范围（交接文档的 2.6 定义）：
  对四个支持周期各完整走一遍 main.build_runtime() 启动链路（不联网）：
    TradingConfig() → CLI --freq 覆盖 → bar_secs_for 校验（fail-fast 在这）
    → bars_per_day → build_broker(dry_run) → EntryPolicy / LayeredExitPolicy
    → Store / EventLog / TradingEngine → build_source("sse")
  断言：bar_secs、根数/日、period_profile 选到对应档案、
  引擎与策略与信号源对象构造齐全。

说明：build_runtime 会把 stdout/stderr 重定向到 {out}/gateway.log，
每轮跑完恢复。out 用临时目录，跑完清理。SSE 源构造不联网
（连接延迟到 events()），全程离线安全。

⚠️ 配置来源隔离（2026-09-14 第 6 批补）：
  `TradingConfig` 是 pydantic-settings，会读**仓库根 `.env`**。本机 `.env` 里
  `TRADING_BROKER=simnow` 会覆盖 broker 的字段默认值，于是"冒烟测试"会去构造
  simnow broker —— 既让「默认 broker dry_run」断言失败（4 条），又因构造时走网络
  把本测试从 1.4s 拖到 ~200s。
  根因不是代码错，而是**测试没有隔离配置来源**：它断言的其实是"字段默认值"，
  却在一个可能被 `.env`/环境变量覆盖的上下文里跑。
  修法：用 `offline_cfg()` 显式把 broker 钉成 dry_run（环境变量优先级高于 `.env`，
  见 pydantic-settings 的 init > env > dotenv > default 顺序），
  并**不修改也不依赖**用户的 `.env`（生产代码零改动）。
  另补 2 条真正测"字段默认值"的断言：清掉 TRADING_* 环境变量 + `_env_file=None`
  隔离 `.env` 后，broker 必须是 dry_run（离线安全默认，防被改成实盘/仿真默认）。

独立脚本风格：check() + 末尾统计 + sys.exit(1)。
跑法：python Test/test_step2_smoke_freq.py
"""
import argparse
import contextlib
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
from Trading.Infra.Period import PERIOD_PROFILES, bar_secs_for, bars_per_day
from Trading.Infra.Clock import SESSION_SECS
from Trading.Source.SSE import SseSource
from Trading.Strategy import EntryPolicy, LayeredExitPolicy

_TRADING_PREFIX = "TRADING_"


@contextlib.contextmanager
def isolated_trading_env(**overrides):
    """隔离本机配置来源：临时清掉全部 `TRADING_*` 环境变量，按需注入 overrides。

    为什么必须这样：
      ① `TradingConfig` 读仓库根 `.env`（`TRADING_BROKER=simnow`）→ 会覆盖字段默认；
      ② pydantic-settings 的优先级是 **init > 环境变量 > .env > 字段默认值**，
         所以设环境变量即可压过 `.env`，**不必也不该去改/删用户的 `.env`**；
      ③ 本测试的语义是"未显式指定 broker 时走离线默认"，必须先把这条口径钉住，
         否则同一份代码在不同机器上跑出不同结果（本机带 .env 就红）。

    退出时原样恢复，绝不把隔离泄漏到别的测试（脚本式测试共享进程环境）。
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith(_TRADING_PREFIX)}
    try:
        for k in saved:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[_TRADING_PREFIX + k] = v
        yield
    finally:
        for k in [k for k in os.environ if k.startswith(_TRADING_PREFIX)]:
            os.environ.pop(k, None)
        os.environ.update(saved)


# 期望值（SSOT 校验对账：Period.FREQ_SEC / 4.5h 交易日近似）
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
        # 显式钉住"离线默认"：args.broker=None 时 build_runtime 取 cfg.broker，
        # 而 cfg 会读 .env → 本机 .env 的 TRADING_BROKER=simnow 会把冒烟测试
        # 变成"构造 simnow 并走网络"。这里把口径钉死，不依赖机器环境。
        with isolated_trading_env(BROKER="dry_run"):
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
        check("入场策略 EntryPolicy", type(engine.entry_policy),
              EntryPolicy)
        check("出场策略 LayeredExitPolicy", type(engine.exit_policy),
              LayeredExitPolicy)
        check("离线口径下 engine.broker.name == dry_run", engine.broker.name, "dry_run")
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

# ── 配置来源隔离的**自证** ─────────────────────────────────────────
#  本轮修复的核心是"隔离机制有效"，所以必须自证，否则以后有人调整
#  pydantic-settings 的优先级/写法时，本测试会**静默**退回到随机器环境漂移。
print("\n── 配置隔离自证（防 .env 再污染本测试）──")
with isolated_trading_env(BROKER="dry_run"):
    check("环境变量可压过 .env（隔离机制有效）", TradingConfig().broker, "dry_run")
with isolated_trading_env():
    check("完全隔离（清 TRADING_* + _env_file=None）后 == 字段默认 dry_run",
          TradingConfig(_env_file=None).broker, "dry_run")
_env_before = {k: v for k, v in os.environ.items() if k.startswith(_TRADING_PREFIX)}
with isolated_trading_env(BROKER="dry_run"):
    pass
_env_after = {k: v for k, v in os.environ.items() if k.startswith(_TRADING_PREFIX)}
check("隔离退出后环境原样恢复（不泄漏到其他测试）", _env_after, _env_before)

# 反向用例：不支持的周期必须在启动期 fail-fast（bar_secs_for 校验）
print("\n── 反向：freq=15m 应被启动校验拦截 ──")
args_bad = argparse.Namespace(
    source="sse", broker=None, replay_dir=None, symbol=None,
    freq="15m", sse_base=None, speed=None, only_alive=None,
    bar_mode=None, out=tempfile.mkdtemp(prefix="gw_smoke_bad_"),
    fresh=None, summary_json=None, max_bars=None, quiet=True, echo_all=None)
try:
    with isolated_trading_env(BROKER="dry_run"):
        gw.build_runtime(args_bad)
    check("freq=15m 启动即 SystemExit", "no_raise", "raise")
except SystemExit:
    check("freq=15m 启动即 SystemExit", "raise", "raise")
except Exception as e:
    check("freq=15m 启动即 SystemExit（实际 {}）".format(type(e).__name__),
          "wrong_exc", "raise")

print("\n结果: {} 通过 / {} 失败".format(_PASSED, _FAILED))
sys.exit(1 if _FAILED else 0)
