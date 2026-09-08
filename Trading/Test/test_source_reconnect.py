# -*- coding: utf-8 -*-
"""Step 2.5 专项测试：SSE 重连三参数收口到 SourceConfig.reconnect_*。

背景（Step 2 路线图 2.5）：
  · Source/SSE.py 的 reconnect / reconnect_max / max_retry 三参数原本是
    `params.get(key, d) or d` 双默认源写法——且 SourceConfig extra=forbid 下
    这些键根本传不进来，实际是不可配置的死旋钮。
  · Tool/Recorder/SignalRecorder.py 的 --reconnect/--reconnect-max 与 SSE 的
    5/60 是同一对值的两个副本。
  收口后：唯一事实源 = Config.SourceConfig（reconnect_wait / reconnect_wait_max /
  reconnect_max_retry），SSE 与 Recorder 都从它取默认。

独立脚本风格（与全套 test_*.py 一致）：check() + 末尾统计 + sys.exit(1)。
"""
import importlib.util
import os
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


print("=" * 60)
print("Step 2.5 测试：SSE 重连参数收口 SourceConfig.reconnect_*")
print("=" * 60)

# ═══ [1] SourceConfig 三字段与默认值 ═══
print("\n[1] SourceConfig 新字段（默认值 == 原硬编码，行为等价）")
from Trading.Config import SourceConfig
sc = SourceConfig()
check("reconnect_wait 默认 5.0", sc.reconnect_wait, 5.0)
check("reconnect_wait_max 默认 60.0", sc.reconnect_wait_max, 60.0)
check("reconnect_max_retry 默认 0（无限）", sc.reconnect_max_retry, 0)
check("旧键名 'reconnect' 仍被 extra=forbid 拒绝",
      _raises(lambda: SourceConfig(**{"reconnect": 5})), True)

# ═══ [2] SseSource 离线构造：默认读 SSOT ═══
print("\n[2] SseSource 默认参数来自 SourceConfig（不连网）")
from Trading.Infra.InstrumentSpec import InstrumentSpec
from Trading.Source.SSE import SseSource
s = SseSource({}, InstrumentSpec())
check("默认 reconnect == 5.0", s.reconnect, 5.0)
check("默认 reconnect_max == 60.0", s.reconnect_max, 60.0)
check("默认 max_retry == 0（无限）", s.max_retry, 0)

# 读配置实证：改 SourceConfig 值 → SseSource 跟随
sc2 = SourceConfig(reconnect_wait=2.0, reconnect_wait_max=30.0,
                   reconnect_max_retry=7)
s2 = SseSource(sc2.model_dump(), InstrumentSpec())
check("reconnect_wait=2.0 传入 -> s.reconnect == 2.0", s2.reconnect, 2.0)
check("reconnect_wait_max=30.0 传入 -> s.reconnect_max == 30.0", s2.reconnect_max, 30.0)
check("reconnect_max_retry=7 传入 -> s.max_retry == 7", s2.max_retry, 7)

# ═══ [3] 构造期 fail-fast 守卫 ═══
print("\n[3] 非法值构造期报错（fail-fast）")


def _mk(**over):
    return SseSource(SourceConfig(**over).model_dump(), InstrumentSpec())


check("reconnect_wait=0 -> ValueError", _raises(lambda: _mk(reconnect_wait=0.0)), True)
check("reconnect_wait_max=0 -> ValueError",
      _raises(lambda: _mk(reconnect_wait_max=0.0)), True)
check("reconnect_max_retry=-1 -> ValueError",
      _raises(lambda: _mk(reconnect_max_retry=-1)), True)
check("合法显式值（1.0/10.0/3）不报错",
      (_mk(reconnect_wait=1.0, reconnect_wait_max=10.0,
           reconnect_max_retry=3).max_retry), 3)

# ═══ [4] SignalRecorder CLI 默认值同源 ═══
print("\n[4] SignalRecorder argparse 默认值取 SourceConfig（同源实证）")
_rec_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "Tool", "Recorder", "SignalRecorder.py")
_spec = importlib.util.spec_from_file_location("signal_recorder", _rec_path)
_rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rec)
args = _rec._build_parser().parse_args([])
check("--reconnect 默认 == SourceConfig().reconnect_wait",
      args.reconnect, SourceConfig().reconnect_wait)
check("--reconnect-max 默认 == SourceConfig().reconnect_wait_max",
      args.reconnect_max, SourceConfig().reconnect_wait_max)
args2 = _rec._build_parser().parse_args(["--reconnect", "3"])
check("CLI 显式 --reconnect 3 仍可覆盖", args2.reconnect, 3.0)

# ═══ [5] 旧双默认源写法已清除（静态） ═══
print("\n[5] 旧写法静态清除")
import pathlib
_sse_src = pathlib.Path(SseSource.__module__.replace(".", "/") + ".py")
_sse_src = pathlib.Path(sys.modules[SseSource.__module__].__file__)
_t = _sse_src.read_text(encoding="utf-8")
check("SSE.py 无旧 'params.get(\"reconnect\", 5)' 写法",
      'params.get("reconnect", 5)' in _t, False)
check("SSE.py 无旧 'params.get(\"max_retry\", 0)' 写法",
      'params.get("max_retry", 0)' in _t, False)
check("SSE.py 引用 SourceConfig 取默认", "_sc.reconnect_wait" in _t, True)

# ═══ 汇总 ═══
print("\n" + "=" * 60)
print("结果: {} 通过 / {} 失败".format(_PASSED, _FAILED))
print("=" * 60)
sys.exit(1 if _FAILED else 0)
