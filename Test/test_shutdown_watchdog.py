# -*- coding: utf-8 -*-
"""收尾硬退出看护（盘后关闭自动下单滞留 150s 强杀的兜底修复）—— 行为护栏。

背景（2026-09-28 实盘）：午盘后（无行情事件）点关闭，空仓/锁仓态收尾本应
零柜台交互、秒级完成，实际子进程滞留到父进程 150s 强杀（rc=1，已写盘的
收尾事件被判不优雅）。卡点在收尾链某一步（主循环退出 / 连接关闭），无法
给每个环节单独加超时 → 统一由"硬退出看护"兜底：
  · RUNNING → 长宽限 130s（覆盖最坏柜台追价 ~100s，早于父进程 150s 强杀）；
  · FLAT/LOCKED → 先等主循环退出、再等收尾完成（短宽限 15s）；
  · 收尾完成后进程仍滞留 → 2s 后兜底硬退。

各断言防什么：
  · [1] 分档：RUNNING 必走长宽限、绝不看门控事件（收尾在途就硬退 = 把
    在途委托留成柜台幽灵仓，只能靠长宽限覆盖）；账户态读不出来也走长宽限
    （按最坏情况，不许静默跳过看护）；
  · [2] 安静档顺序：必须先等主循环退出（broker.submit 同步阻塞在主线程，
    主循环未退出 = 可能有在途委托）、再等收尾完成、最后才是滞留兜底 ——
    顺序错 = 幽灵仓风险回归；
  · [3] 接线：run() 真的布防了看护线程，两个门控事件真的在 finally 的
    正确时点置位（主循环退出 = finally 首行、收尾完成 = 末行）—— 接线
    断了看护就是死代码；宽限必须早于父进程强杀阈值；
  · [4] 父进程强杀告警带回 gateway.log 尾部（强杀即失去现场，卡点当场可见）。

跑法：python Test/test_shutdown_watchdog.py
"""
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO)

from Trading.Infra.Records import AccountState                    # noqa: E402
from Trading.main import (_WATCHDOG_GRACE_QUIET_S,                # noqa: E402
                          _WATCHDOG_GRACE_RUNNING_S,
                          _WATCHDOG_LINGER_S, _watchdog_body)

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  \u2713 {}".format(name))
    else:
        _FAIL += 1
        print("  \u2717 {} -> got {!r}, want {!r}".format(name, got, want))


class _FakeEvent:
    """wait(timeout) 直接返回预设结果并记录 timeout；不真阻塞。"""

    def __init__(self, result=True):
        self._result = result
        self.waited = []

    def wait(self, timeout=None):
        self.waited.append(timeout)
        return self._result

    def set(self):
        self._result = True


class _Rec:
    """注入 sleep/exit_fn/echo 的记录器：不发真退出、不真睡。"""

    def __init__(self):
        self.sleeps = []
        self.exits = []
        self.echos = []

    def sleep(self, s):
        self.sleeps.append(s)

    def exit_fn(self, code):
        self.exits.append(code)

    def echo(self, msg):
        self.echos.append(msg)


def _run_body(state, *, loop_result=True, done_result=True, state_raises=False):
    """按给定账户态/门控结果跑一遍看护主体，返回 (记录器, 三个门控)。"""
    rec = _Rec()
    stop = _FakeEvent(result=True)
    loop = _FakeEvent(result=loop_result)
    done = _FakeEvent(result=done_result)

    def state_fn():
        if state_raises:
            raise RuntimeError("boom")
        return state

    _watchdog_body(stop, state_fn, loop, done,
                   sleep=rec.sleep, exit_fn=rec.exit_fn, echo=rec.echo)
    return rec, loop, done


with open(os.path.join(REPO, "Trading", "main.py"), encoding="utf-8") as f:
    MAIN_SRC = f.read()
with open(os.path.join(REPO, "App", "AppTrader.py"), encoding="utf-8") as f:
    APPTRADER_SRC = f.read()

# ═══ [1] 分档 ═══
print("\n[1] 分档：RUNNING / 账户态异常 → 长宽限；不看门控事件")
rec, loop, done = _run_body(AccountState.RUNNING)
check("[1] RUNNING 走长宽限", rec.sleeps, [_WATCHDOG_GRACE_RUNNING_S])
check("[1] RUNNING 硬退出码 0", rec.exits, [0])
check("[1] RUNNING 不看主循环门控", loop.waited, [])
check("[1] RUNNING 不看收尾门控", done.waited, [])
rec, _, _ = _run_body(None, state_raises=True)
check("[1] 账户态读不出来 → 仍走长宽限（不跳过看护）",
      (rec.sleeps, rec.exits), ([_WATCHDOG_GRACE_RUNNING_S], [0]))

# ═══ [2] 安静档顺序 ═══
print("\n[2] 安静档：主循环退出 → 收尾完成 → 滞留兜底，顺序与门控不可错")
rec, loop, done = _run_body(AccountState.FLAT, loop_result=False)
check("[2] 主循环未退出 → 只等它（上限=长宽限）", loop.waited,
      [_WATCHDOG_GRACE_RUNNING_S])
check("[2] 主循环未退出就不碰收尾门控", done.waited, [])
check("[2] 主循环未退出 → 硬退", rec.exits, [0])
rec, loop, done = _run_body(AccountState.FLAT, loop_result=True,
                            done_result=False)
check("[2] 主循环已退出 → 短宽限等收尾", done.waited,
      [_WATCHDOG_GRACE_QUIET_S])
check("[2] 收尾未完成 → 硬退", rec.exits, [0])
check("[2] 此路径不 sleep（等待都在 Event.wait 上）", rec.sleeps, [])
rec, _, _ = _run_body(AccountState.LOCKED, loop_result=True,
                      done_result=True)
check("[2] 收尾完成 → 只剩滞留兜底宽限", rec.sleeps,
      [_WATCHDOG_LINGER_S])
check("[2] 滞留兜底硬退", rec.exits, [0])
check("[2] LOCKED 同 FLAT 走安静档（不 sleep 长宽限）",
      _WATCHDOG_GRACE_RUNNING_S in rec.sleeps, False)

# ═══ [3] 接线 ═══
print("\n[3] 接线：看护已布防、门控置位点正确、宽限早于父进程强杀")
check("[3] 看护线程已布防（名字可查）",
      'name="gw-hard-exit-watchdog"' in MAIN_SRC, True)
check("[3] 布防用的就是 _watchdog_body",
      "target=_watchdog_body" in MAIN_SRC, True)
i_loop = MAIN_SRC.index("_main_loop_exited.set()")
i_shutdown = MAIN_SRC.index("engine.shutdown_and_lock_all()")
i_evclose = MAIN_SRC.index("ev.close()")
i_done = MAIN_SRC.index("_main_done.set()")
check("[3] 门控① 在 finally 里先于收尾置位（主循环退出=无在途委托）",
      i_loop < i_shutdown, True)
check("[3] 门控② 在收尾全部完成后置位（各路 close 之后）",
      i_evclose < i_done, True)
check("[3] 长宽限早于父进程强杀阈值",
      _WATCHDOG_GRACE_RUNNING_S < 150.0, True)
check("[3] 长宽限覆盖最坏柜台追价（≥100s）",
      _WATCHDOG_GRACE_RUNNING_S >= 100.0, True)
check("[3] 安静短宽限宽松于本地收尾实测（几秒级）",
      5.0 <= _WATCHDOG_GRACE_QUIET_S <= 30.0, True)
check("[3] 滞留兜底宽限存在且为秒级",
      0.0 < _WATCHDOG_LINGER_S <= 10.0, True)

# ═══ [4] 父进程强杀告警带回日志尾部 ═══
print("\n[4] AppTrader 强杀告警带 gateway.log 尾部（现场不丢）")
check("[4] 强杀告警含日志尾部字样",
      "gateway.log 尾部" in APPTRADER_SRC, True)
check("[4] 强杀前真的读了尾部（_read_log_tail）",
      'self._read_log_tail(str(log_file), 3)' in APPTRADER_SRC, True)

print("\n" + "=" * 60)
print("shutdown_watchdog: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
