# -*- coding: utf-8 -*-
"""Step 2.6 收官测试：2.0 / 2.0.1 / 2.0.2 / 2.0.3 / 2.0.3b 各 phase 专属断言补齐。

背景：2.1–2.5 各 phase 交付时都带了专属测试（test_period_profile /
test_engine_config / test_channel_timing / test_const_merge /
test_source_reconnect），但 2.0 系列的六层配置模型（2.0/2.0.1/2.0.2）、
策略选择器删除（2.0.3）、类名改名（2.0.3b）此前没有独立测试文件。
2.6 结构归一收官，补齐这块，使「每个 phase 至少一个专属测试」闭环。

覆盖：
  [1] 2.0/2.0.1/2.0.2 —— 六层配置模型：TradingConfig 七个子模型字段 +
      extra="forbid" 严格模式（根 + 子模型各抽查）
  [2] 2.0.3   —— 策略选择器删除：Strategy 包只导出两个策略；
      Exit.py/Entry.py 无注册表/register/build_*_policy 残留；
      main.py 源文本无选择器符号
  [3] 2.0.3b  —— 类名改名：EntryConfig/ExitConfig 生效，旧名不留定义；
      DefaultExitParamsConfig 历史说明文档不被误伤；
      main.py 经 cfg.entry_params / cfg.exit_params 构造策略
  [4] SSOT    —— 每个 *Config 模型在 Config.py 只定义一次

独立脚本风格（与全套 test_*.py 一致）：check() + 末尾统计 + sys.exit(1)。
跑法：python Test/test_step2_config_ssot.py（cwd=Trading/ 上层亦可）
"""
import os
import re
import sys

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


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG_TXT = open(os.path.join(_HERE, "..", "Config.py"),
                encoding="utf-8").read()
_MAIN_TXT = open(os.path.join(_HERE, "..", "main.py"),
                 encoding="utf-8").read()

print("=" * 60)
print("Step 2.6 收官测试：2.0 系列（配置分层 / 选择器删除 / 改名）")
print("=" * 60)

# ═══ [1] 2.0/2.0.1/2.0.2 六层配置模型 ═══
print("\n[1] 六层配置模型（2.0/2.0.1/2.0.2）：TradingConfig 七个子模型 + extra=forbid")
from Trading.Config import (BrokerConfig, ChannelTimingConfig, EngineConfig,
                            EntryConfig, ExitConfig, RiskConfig, SizingConfig,
                            SourceConfig, TradingConfig)

cfg = TradingConfig()
check("cfg.source -> SourceConfig", type(cfg.source), SourceConfig)
check("cfg.entry_params -> EntryConfig", type(cfg.entry_params), EntryConfig)
check("cfg.exit_params -> ExitConfig", type(cfg.exit_params), ExitConfig)
check("cfg.risk -> RiskConfig", type(cfg.risk), RiskConfig)
check("cfg.sizing -> SizingConfig", type(cfg.sizing), SizingConfig)
check("cfg.broker_params -> BrokerConfig", type(cfg.broker_params), BrokerConfig)
check("cfg.engine -> EngineConfig", type(cfg.engine), EngineConfig)
check("根模型拒未知键（extra=forbid）",
      _raises(lambda: TradingConfig(**{"not_a_field": 1})), True)
check("SourceConfig 拒未知键（extra=forbid）",
      _raises(lambda: SourceConfig(**{"not_a_field": 1})), True)
check("ExitConfig 拒未知键（extra=forbid）",
      _raises(lambda: ExitConfig(**{"not_a_field": 1})), True)

# ═══ [2] 2.0.3 策略选择器删除 ═══
print("\n[2] 选择器删除（2.0.3）：只留 DefaultEntryPolicy / LayeredExitPolicy")
from Trading import Strategy
check("Strategy.__all__ 精确 5 项",
      sorted(Strategy.__all__),
      ["DefaultEntryPolicy", "EntryPolicy", "ExitCheck", "ExitPolicy",
       "LayeredExitPolicy"])
check("Strategy.Exit 无 DefaultExitPolicy",
      hasattr(Strategy.Exit, "DefaultExitPolicy"), False)
for _sym in ("EXIT_POLICIES", "ENTRY_POLICIES",
             "register_exit_policy", "register_entry_policy",
             "build_exit_policy", "build_entry_policy"):
    check("Strategy.Exit/Entry 无 {} 残留".format(_sym),
          hasattr(Strategy.Exit, _sym) or hasattr(Strategy.Entry, _sym), False)
    check("main.py 无 {} 引用".format(_sym), _sym in _MAIN_TXT, False)
check("main.py 无 DefaultExitPolicy 引用", "DefaultExitPolicy" in _MAIN_TXT, False)

# ═══ [3] 2.0.3b 类名改名 ═══
print("\n[3] 类名改名（2.0.3b）：EntryConfig/ExitConfig 生效，旧名无定义")
check("Config.py 定义 class EntryConfig",
      bool(re.search(r"^class EntryConfig\b", _CFG_TXT, re.M)), True)
check("Config.py 定义 class ExitConfig",
      bool(re.search(r"^class ExitConfig\b", _CFG_TXT, re.M)), True)
check("Config.py 不再定义 class EntryParamsConfig",
      bool(re.search(r"^class EntryParamsConfig\b", _CFG_TXT, re.M)), False)
check("Config.py 不再定义 class ExitParamsConfig",
      bool(re.search(r"^class ExitParamsConfig\b", _CFG_TXT, re.M)), False)
check("DefaultExitParamsConfig 历史说明保留（负向环视未误伤）",
      "DefaultExitParamsConfig" in _CFG_TXT, True)
check("字段声明 entry_params: EntryConfig",
      bool(re.search(r"^    entry_params:\s*EntryConfig\b", _CFG_TXT, re.M)), True)
check("字段声明 exit_params: ExitConfig",
      bool(re.search(r"^    exit_params:\s*ExitConfig\b", _CFG_TXT, re.M)), True)
check("main.py 经 cfg.entry_params 构造入场策略",
      "DefaultEntryPolicy(cfg.entry_params" in _MAIN_TXT, True)
check("main.py 经 cfg.exit_params 构造出场策略",
      "LayeredExitPolicy(cfg.exit_params" in _MAIN_TXT, True)

# ═══ [4] SSOT：每个模型只定义一次 ═══
print("\n[4] SSOT：每个 *Config 在 Config.py 只定义一次")
for _m in ("TradingConfig", "SourceConfig", "EntryConfig", "ExitConfig",
           "RiskConfig", "SizingConfig", "BrokerConfig", "EngineConfig",
           "ChannelTimingConfig"):
    _n = len(re.findall(r"^class {}\b".format(_m), _CFG_TXT, re.M))
    check("class {} 定义次数 == 1".format(_m), _n, 1)

print("\n结果: {} 通过 / {} 失败".format(_PASSED, _FAILED))
sys.exit(1 if _FAILED else 0)
